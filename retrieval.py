"""
Evidence retrieval across papers and notes - the context-engineering core.

The agent never receives a collection. It asks a question, and this module
returns a small, diverse set of passages that answer it:

  1. Scope. Resolve which papers are eligible (a collection, a goal's papers,
     explicit ids, or the whole library) - always through store.py, so scope
     can only ever be the caller's own.
  2. Over-fetch. Pull top_k * CANDIDATE_MULTIPLIER nearest chunks by cosine.
  3. Diversify. Keep at most `per_paper` chunks from any one paper, so a single
     long full text cannot crowd out the other papers - a comparison question
     needs evidence from several.
  4. Label. Every passage carries a citation key: the OpenAlex id for paper
     text ("W4389984066") and "N<id>" for the learner's own notes. The agent
     cites with those keys; agent.py checks every cited key against what was
     actually retrieved.

Goal chunks are never evidence. They exist so a goal can rank papers by
relevance (goal_relevance) and seed plan ordering.
"""

from __future__ import annotations

import lakebase
from embeddings import embed_query, to_vector_literal

DEFAULT_TOP_K = 8
MAX_TOP_K = 20
DEFAULT_PER_PAPER = 2
CANDIDATE_MULTIPLIER = 5
EVIDENCE_SOURCE_TYPES = ("abstract", "content", "note")


def retrieve_evidence(
    query: str,
    user_id: int,
    *,
    paper_ids: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    per_paper: int = DEFAULT_PER_PAPER,
    source_types: tuple[str, ...] = EVIDENCE_SOURCE_TYPES,
    include_notes: bool = True,
) -> list[dict]:
    """Nearest passages to `query`, diversified across papers.

    paper_ids=None searches the whole library; an empty list means an empty
    scope and returns nothing (a collection with no papers has no evidence).
    """
    if paper_ids is not None and not paper_ids:
        return []
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    per_paper = max(1, int(per_paper))
    types = [t for t in source_types if t in EVIDENCE_SOURCE_TYPES]
    if not include_notes:
        types = [t for t in types if t != "note"]

    vector = to_vector_literal(embed_query(query))
    rows = lakebase.run_query(
        """
        SELECT c.id AS chunk_id, c.source_type, c.paper_id, c.note_id, c.chunk_index,
               c.chunk_text, p.title, p.publication_year, p.doi, p.venue,
               ROUND((1 - (c.embedding <=> %(vec)s::vector))::numeric, 4)::float8 AS similarity
        FROM context_chunks c
        LEFT JOIN papers p ON p.id = c.paper_id
        WHERE c.source_type = ANY(%(types)s)
          -- Paper text is public; notes only ever match their owner.
          AND (c.source_type <> 'note' OR c.user_id = %(user_id)s)
          AND (%(scoped)s = false OR c.paper_id = ANY(%(paper_ids)s))
        ORDER BY c.embedding <=> %(vec)s::vector
        LIMIT %(candidates)s
        """,
        {
            "vec": vector,
            "types": types,
            "user_id": user_id,
            "scoped": paper_ids is not None,
            "paper_ids": list(paper_ids or []),
            "candidates": top_k * CANDIDATE_MULTIPLIER,
        },
    )
    return diversify(rows, top_k=top_k, per_paper=per_paper)


def diversify(rows: list[dict], top_k: int, per_paper: int) -> list[dict]:
    """Keep rank order, cap chunks per paper, attach citation keys. Pure."""
    kept: list[dict] = []
    per_source: dict[str, int] = {}
    for row in rows:
        # A standalone note (no paper) is its own source.
        source = row.get("paper_id") or f"N{row.get('note_id')}"
        if per_source.get(source, 0) >= per_paper:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        evidence = dict(row)
        evidence["citation_key"] = (
            f"N{row['note_id']}" if row.get("source_type") == "note" else row.get("paper_id")
        )
        kept.append(evidence)
        if len(kept) >= top_k:
            break
    return kept


def format_evidence(evidence: list[dict]) -> str:
    """Render passages for the model, each headed by the key it must cite."""
    if not evidence:
        return "No passages matched. Nothing in scope is embedded, or nothing is relevant."
    blocks = []
    for item in evidence:
        if item["source_type"] == "note":
            about = f" (about {item['title']})" if item.get("title") else ""
            header = f"[{item['citation_key']}] learner's own note{about}"
        else:
            year = f", {item['publication_year']}" if item.get("publication_year") else ""
            header = f"[{item['citation_key']}] {item.get('title')}{year} - {item['source_type']}"
        blocks.append(f"{header} (similarity {item['similarity']})\n{item['chunk_text']}")
    return "\n\n".join(blocks)


def goal_relevance(goal_id: int, user_id: int, paper_ids: list[str]) -> dict[str, float]:
    """Cosine similarity of each paper's abstract to the goal. Missing = not embedded."""
    if not paper_ids:
        return {}
    rows = lakebase.run_query(
        """
        SELECT c.paper_id, MAX(1 - (c.embedding <=> g.embedding))::float8 AS relevance
        FROM context_chunks c
        CROSS JOIN LATERAL (
            SELECT embedding FROM context_chunks
            WHERE goal_id = %(goal_id)s AND user_id = %(user_id)s AND source_type = 'goal'
            ORDER BY chunk_index LIMIT 1
        ) g
        WHERE c.source_type = 'abstract' AND c.paper_id = ANY(%(ids)s)
        GROUP BY c.paper_id
        """,
        {"goal_id": goal_id, "user_id": user_id, "ids": list(paper_ids)},
    )
    return {row["paper_id"]: round(float(row["relevance"]), 4) for row in rows}
