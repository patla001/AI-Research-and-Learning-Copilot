"""
Context pipeline: turn abstracts, paper content, notes and goals into vectors.

One anti-join per source type finds work that has no chunks yet; chunking and
embedding are shared. Three callers use it:

    notebooks/ingest_embeddings.py  batch, off .env or the secret scope
    POST /copilot/embed             the same batch, through the running app
    store writes (notes, goals)     embed one source inline, right after saving

Every insert is ON CONFLICT DO NOTHING against a position-derived primary key,
so all three can overlap without duplicating a vector.
"""

from __future__ import annotations

import logging
from typing import Callable, Sequence

import lakebase
from embeddings import (
    EMBED_MODEL,
    chunk_id,
    chunk_text,
    passage_for_embedding,
    to_vector_literal,
)
from lakebase import execute_values

logger = logging.getLogger(__name__)

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

# Each query yields rows shaped for embed_sources(). `source_key` is what the
# chunk id is derived from.
_PENDING_SQL = {
    "abstract": """
        SELECT 'abstract' AS source_type, p.id AS source_key, p.id AS paper_id,
               NULL::bigint AS note_id, NULL::bigint AS goal_id, NULL::bigint AS user_id,
               p.title, p.abstract AS body
        FROM papers p
        WHERE p.abstract IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM context_chunks c WHERE c.paper_id = p.id AND c.source_type = 'abstract')
    """,
    "content": """
        SELECT 'content' AS source_type, p.id AS source_key, p.id AS paper_id,
               NULL::bigint AS note_id, NULL::bigint AS goal_id, NULL::bigint AS user_id,
               p.title, p.content_text AS body
        FROM papers p
        WHERE p.content_text IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM context_chunks c WHERE c.paper_id = p.id AND c.source_type = 'content')
    """,
    "note": """
        SELECT 'note' AS source_type, n.id::text AS source_key, n.paper_id,
               n.id AS note_id, NULL::bigint AS goal_id, n.user_id,
               p.title, n.body
        FROM notes n LEFT JOIN papers p ON p.id = n.paper_id
        WHERE NOT EXISTS (SELECT 1 FROM context_chunks c WHERE c.note_id = n.id)
    """,
    "goal": """
        SELECT 'goal' AS source_type, g.id::text AS source_key, NULL::text AS paper_id,
               NULL::bigint AS note_id, g.id AS goal_id, g.user_id,
               NULL::text AS title,
               g.title || COALESCE(E'\n' || g.description, '') AS body
        FROM learning_goals g
        WHERE NOT EXISTS (SELECT 1 FROM context_chunks c WHERE c.goal_id = g.id)
    """,
}

SOURCE_TYPES = tuple(_PENDING_SQL)

# A single paper's full text can be dozens of chunks; cap what one paper
# contributes so a long PDF cannot dominate the embed run or the index.
MAX_CONTENT_CHUNKS_PER_PAPER = 40


def chunks_table_exists() -> bool:
    rows = lakebase.run_query("SELECT to_regclass('context_chunks') IS NOT NULL AS present")
    return bool(rows and rows[0]["present"])


def pending_sources(source_types: Sequence[str] = SOURCE_TYPES, limit: int | None = None,
                    where_extra: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    for source_type in source_types:
        sql = _PENDING_SQL[source_type]
        params: dict = {}
        if where_extra and source_type in where_extra:
            clause, params = where_extra[source_type]
            sql += f" AND {clause}"
        if limit is not None:
            sql += " LIMIT %(limit)s"
            params["limit"] = max(1, int(limit))
        rows.extend(lakebase.run_query(sql, params or None))
    return rows


def backlog() -> dict:
    """Counts of sources with no chunks, plus the index size."""
    counts = {}
    for source_type, sql in _PENDING_SQL.items():
        counts[source_type] = lakebase.run_query(f"SELECT COUNT(*) AS n FROM ({sql}) x")[0]["n"]
    total = lakebase.run_query(
        "SELECT source_type, COUNT(*) AS n FROM context_chunks GROUP BY source_type"
    )
    return {
        "pending": counts,
        "chunks": {row["source_type"]: row["n"] for row in total},
        "model": EMBED_MODEL,
    }


def embed_sources(sources: list[dict], embed: Embedder | None = None,
                  model_name: str = EMBED_MODEL, batch_size: int = 64,
                  log: Callable[[str], None] = logger.info) -> dict:
    """Chunk, embed and store sources. Returns {"sources", "chunks", "written"}."""
    if embed is None:
        from embeddings import embed_texts as embed

    units: list[tuple[dict, int, str]] = []
    for source in sources:
        pieces = chunk_text(source.get("body"))
        if source["source_type"] == "content":
            pieces = pieces[:MAX_CONTENT_CHUNKS_PER_PAPER]
        for index, piece in enumerate(pieces):
            units.append((source, index, piece))
    if not units:
        return {"sources": len(sources), "chunks": 0, "written": 0}

    written = 0
    for start in range(0, len(units), batch_size):
        batch = units[start:start + batch_size]
        vectors = embed([_embedding_input(src, piece) for src, _, piece in batch])
        rows = [
            (
                chunk_id(src["source_type"], src["source_key"], index),
                src["source_type"], src.get("paper_id"), src.get("note_id"),
                src.get("goal_id"), src.get("user_id"), index, piece,
                to_vector_literal(vector), model_name,
            )
            for (src, index, piece), vector in zip(batch, vectors)
        ]
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                inserted = execute_values(
                    cur,
                    """
                    INSERT INTO context_chunks (id, source_type, paper_id, note_id, goal_id,
                        user_id, chunk_index, chunk_text, embedding, model_name)
                    VALUES %s ON CONFLICT (id) DO NOTHING RETURNING id
                    """,
                    rows, template="(%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)", fetch=True,
                )
                written += len(inserted)
            conn.commit()
        log(f"embedded {min(start + batch_size, len(units))}/{len(units)} chunks")

    return {"sources": len(sources), "chunks": len(units), "written": written}


def embed_pending(limit: int | None = None, source_types: Sequence[str] = SOURCE_TYPES,
                  embed: Embedder | None = None, log: Callable[[str], None] = logger.info) -> dict:
    sources = pending_sources(source_types, limit=limit)
    result = embed_sources(sources, embed=embed, log=log)
    result["by_source"] = {}
    for source in sources:
        result["by_source"][source["source_type"]] = result["by_source"].get(source["source_type"], 0) + 1
    return result


def embed_papers(paper_ids: list[str], embed: Embedder | None = None) -> dict:
    """Embed just these papers' abstracts and content - used right after an import."""
    if not paper_ids:
        return {"sources": 0, "chunks": 0, "written": 0}
    clause = ("p.id = ANY(%(ids)s)", {"ids": list(paper_ids)})
    sources = pending_sources(("abstract", "content"),
                              where_extra={"abstract": clause, "content": clause})
    return embed_sources(sources, embed=embed)


def embed_note(note_id: int, embed: Embedder | None = None) -> dict:
    sources = pending_sources(("note",), where_extra={"note": ("n.id = %(id)s", {"id": note_id})})
    return embed_sources(sources, embed=embed)


def embed_goal(goal_id: int, embed: Embedder | None = None) -> dict:
    sources = pending_sources(("goal",), where_extra={"goal": ("g.id = %(id)s", {"id": goal_id})})
    return embed_sources(sources, embed=embed)


def _embedding_input(source: dict, piece: str) -> str:
    if source["source_type"] in ("abstract", "content"):
        return passage_for_embedding(source.get("title"), piece)
    if source["source_type"] == "note" and source.get("title"):
        return f"Note on {source['title']}:\n{piece}"
    return piece
