"""
Lakebase repository for the nine application tables.

Every read and write that belongs to a person takes `user_id` and filters on it
in SQL. That is the whole authorization model: the Flask routes and the agent
tools both go through here, so an agent tool call cannot reach another user's
goals, collections, progress or notes even if a prompt-injected paper abstract
asks it to.

Papers and authors are shared: they are public OpenAlex records.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

import lakebase
from embeddings import text_hash
from lakebase import execute_values

logger = logging.getLogger(__name__)

GOAL_LEVELS = ("beginner", "intermediate", "advanced")
GOAL_STATUSES = ("active", "completed", "archived")
PROGRESS_STATUSES = ("planned", "reading", "completed", "skipped")


class NotFound(Exception):
    """The row does not exist, or it belongs to someone else. Deliberately the same."""


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def ensure_user(email: str, display_name: str | None = None) -> dict:
    # Not lakebase.run_query: that helper never commits, so the insert would be
    # rolled back when the connection closed and every request would look new.
    return _write_returning(
        """
        INSERT INTO users (email, display_name) VALUES (%(email)s, %(name)s)
        ON CONFLICT (email) DO UPDATE
            SET last_seen_at = now(),
                display_name = COALESCE(EXCLUDED.display_name, users.display_name)
        RETURNING id, email, display_name, created_at
        """,
        {"email": email.strip().lower(), "name": display_name},
    )


# ---------------------------------------------------------------------------
# learning_goals
# ---------------------------------------------------------------------------


def create_goal(user_id: int, title: str, description: str | None = None,
                level: str = "intermediate", target_date: str | None = None) -> dict:
    return _write_returning(
        """
        INSERT INTO learning_goals (user_id, title, description, level, target_date)
        VALUES (%(user_id)s, %(title)s, %(description)s, %(level)s, %(target_date)s)
        RETURNING *
        """,
        {"user_id": user_id, "title": title, "description": description,
         "level": level, "target_date": target_date},
    )


def list_goals(user_id: int, status: str | None = None) -> list[dict]:
    return lakebase.run_query(
        """
        SELECT g.*,
               (SELECT COUNT(*) FROM reading_progress rp
                 WHERE rp.goal_id = g.id AND rp.user_id = g.user_id) AS planned_papers,
               (SELECT COUNT(*) FROM reading_progress rp
                 WHERE rp.goal_id = g.id AND rp.user_id = g.user_id
                   AND rp.status = 'completed') AS completed_papers
        FROM learning_goals g
        WHERE g.user_id = %(user_id)s
          AND (%(status)s::text IS NULL OR g.status = %(status)s)
        ORDER BY g.status = 'active' DESC, g.created_at DESC
        """,
        {"user_id": user_id, "status": status},
    )


def get_goal(user_id: int, goal_id: int) -> dict:
    rows = lakebase.run_query(
        "SELECT * FROM learning_goals WHERE id = %(id)s AND user_id = %(user_id)s",
        {"id": goal_id, "user_id": user_id},
    )
    if not rows:
        raise NotFound(f"No learning goal {goal_id}.")
    return rows[0]


def update_goal(user_id: int, goal_id: int, fields: dict) -> dict:
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "description", "level", "target_date", "status")}
    if not allowed:
        return get_goal(user_id, goal_id)
    assignments = ", ".join(f"{k} = %({k})s" for k in allowed)
    row = _write_returning(
        f"""
        UPDATE learning_goals SET {assignments}, updated_at = now()
        WHERE id = %(id)s AND user_id = %(user_id)s
        RETURNING *
        """,
        {**allowed, "id": goal_id, "user_id": user_id},
        missing=f"No learning goal {goal_id}.",
    )
    if "title" in allowed or "description" in allowed:
        # The goal's vector described the old wording; drop it so the next
        # embed pass rebuilds it.
        lakebase.run_write("DELETE FROM context_chunks WHERE goal_id = %s", (goal_id,))
    return row


# ---------------------------------------------------------------------------
# papers, authors, paper_authors
# ---------------------------------------------------------------------------

_PAPER_COLUMNS = (
    "id", "doi", "title", "abstract", "publication_year", "publication_date", "venue",
    "work_type", "language", "cited_by_count", "is_oa", "oa_url", "primary_topic",
    "topics", "referenced_works", "related_works", "has_content", "abstract_hash", "payload",
)


def upsert_works(works: Iterable[dict]) -> dict:
    """Upsert normalized OpenAlex works (see openalex_client.normalize_work).

    Returns {"papers": n, "authors": m, "abstract_chunks_invalidated": k}.

    Same stale-vector guard as the weather app's text_hash: the stored abstract
    hash is read first, and any paper whose abstract changed loses its abstract
    chunks, which puts it back in the embed backlog.
    """
    works = [w for w in works if w and w.get("paper")]
    if not works:
        return {"papers": 0, "authors": 0, "abstract_chunks_invalidated": 0}

    papers: dict[str, dict] = {}
    for work in works:
        paper = dict(work["paper"])
        paper["abstract_hash"] = text_hash(paper.get("abstract"))
        papers[paper["id"]] = paper

    authors: dict[str, dict] = {}
    authorships: dict[tuple, dict] = {}
    for work in works:
        for author in work.get("authors") or []:
            authors[author["id"]] = author
        for link in work.get("authorships") or []:
            authorships[(link["paper_id"], link["author_id"])] = link

    paper_rows = [
        tuple(
            json.dumps(p.get(c)) if c in ("topics", "payload") else p.get(c)
            for c in _PAPER_COLUMNS
        )
        for p in papers.values()
    ]
    template = "(" + ",".join(
        "%s::jsonb" if c in ("topics", "payload") else "%s" for c in _PAPER_COLUMNS
    ) + ")"
    # has_content only ever turns on: a later search response selecting fewer
    # fields must not erase a paper's fetched full text.
    set_clause = ", ".join(
        f"{c} = EXCLUDED.{c}" for c in _PAPER_COLUMNS if c not in ("id", "has_content")
    )

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, abstract_hash FROM papers WHERE id = ANY(%s)", (list(papers),)
            )
            changed = [
                row["id"] for row in cur.fetchall()
                if row["abstract_hash"] != papers[row["id"]]["abstract_hash"]
            ]

            execute_values(
                cur,
                f"""
                INSERT INTO papers ({", ".join(_PAPER_COLUMNS)}) VALUES %s
                ON CONFLICT (id) DO UPDATE SET {set_clause},
                    has_content = papers.has_content OR EXCLUDED.has_content,
                    synced_at = now()
                """,
                paper_rows,
                template=template,
            )

            if authors:
                execute_values(
                    cur,
                    """
                    INSERT INTO authors (id, display_name, orcid, institutions) VALUES %s
                    ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name,
                        orcid = COALESCE(EXCLUDED.orcid, authors.orcid),
                        institutions = EXCLUDED.institutions, updated_at = now()
                    """,
                    [(a["id"], a["display_name"], a.get("orcid"), json.dumps(a["institutions"]))
                     for a in authors.values()],
                    template="(%s,%s,%s,%s::jsonb)",
                )
            if authorships:
                execute_values(
                    cur,
                    """
                    INSERT INTO paper_authors (paper_id, author_id, author_position,
                        position_index, is_corresponding, institutions) VALUES %s
                    ON CONFLICT (paper_id, author_id) DO UPDATE SET
                        author_position = EXCLUDED.author_position,
                        position_index = EXCLUDED.position_index,
                        is_corresponding = EXCLUDED.is_corresponding,
                        institutions = EXCLUDED.institutions
                    """,
                    [(l["paper_id"], l["author_id"], l.get("author_position"),
                      l["position_index"], l["is_corresponding"], json.dumps(l["institutions"]))
                     for l in authorships.values()],
                    template="(%s,%s,%s,%s,%s,%s::jsonb)",
                )

            invalidated = 0
            if changed:
                cur.execute(
                    "DELETE FROM context_chunks WHERE source_type = 'abstract' AND paper_id = ANY(%s)",
                    (changed,),
                )
                invalidated = cur.rowcount
        conn.commit()

    return {"papers": len(papers), "authors": len(authors),
            "abstract_chunks_invalidated": invalidated}


def set_paper_content(paper_id: str, content: str | None) -> int:
    """Store fetched full text; drop content chunks when the text changed."""
    new_hash = text_hash(content)
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content_hash FROM papers WHERE id = %s", (paper_id,))
            row = cur.fetchone()
            if row is None:
                raise NotFound(f"No paper {paper_id}.")
            cur.execute(
                """
                UPDATE papers SET content_text = %s, content_hash = %s,
                       has_content = has_content OR %s, content_fetched_at = now()
                WHERE id = %s
                """,
                (content, new_hash, content is not None, paper_id),
            )
            invalidated = 0
            if row["content_hash"] != new_hash:
                cur.execute(
                    "DELETE FROM context_chunks WHERE source_type = 'content' AND paper_id = %s",
                    (paper_id,),
                )
                invalidated = cur.rowcount
        conn.commit()
    return invalidated


_PAPER_SUMMARY_SQL = """
    SELECT p.id, p.doi, p.title, p.publication_year, p.venue, p.work_type,
           p.cited_by_count, p.is_oa, p.oa_url, p.primary_topic,
           p.has_content, p.content_text IS NOT NULL AS content_stored,
           p.referenced_works,
           COALESCE((
               SELECT json_agg(a.display_name ORDER BY pa.position_index)
               FROM paper_authors pa JOIN authors a ON a.id = pa.author_id
               WHERE pa.paper_id = p.id
           ), '[]'::json) AS authors
    FROM papers p
"""


def get_papers(paper_ids: list[str]) -> list[dict]:
    if not paper_ids:
        return []
    rows = lakebase.run_query(_PAPER_SUMMARY_SQL + " WHERE p.id = ANY(%(ids)s)",
                              {"ids": list(paper_ids)})
    order = {pid: i for i, pid in enumerate(paper_ids)}
    return sorted(rows, key=lambda r: order.get(r["id"], len(order)))


def paper_ids_with_content(paper_ids: list[str]) -> list[str]:
    """Which of these papers already have full text stored."""
    if not paper_ids:
        return []
    rows = lakebase.run_query(
        "SELECT id FROM papers WHERE id = ANY(%s) AND content_text IS NOT NULL", (list(paper_ids),)
    )
    return [r["id"] for r in rows]


def get_paper(paper_id: str, user_id: int | None = None) -> dict:
    rows = lakebase.run_query(
        _PAPER_SUMMARY_SQL.replace("p.referenced_works,", "p.referenced_works, p.abstract, p.topics,")
        + " WHERE p.id = %(id)s",
        {"id": paper_id},
    )
    if not rows:
        raise NotFound(f"No paper {paper_id} in the library yet. Import it first.")
    paper = rows[0]
    if user_id is not None:
        paper["progress"] = lakebase.run_query(
            """
            SELECT rp.goal_id, g.title AS goal_title, rp.status, rp.plan_position, rp.rationale
            FROM reading_progress rp LEFT JOIN learning_goals g ON g.id = rp.goal_id
            WHERE rp.user_id = %(user_id)s AND rp.paper_id = %(id)s
            """,
            {"user_id": user_id, "id": paper_id},
        )
        paper["notes"] = list_notes(user_id, paper_id=paper_id)
        paper["collections"] = lakebase.run_query(
            """
            SELECT c.id, c.name FROM collection_papers cp JOIN collections c ON c.id = cp.collection_id
            WHERE cp.paper_id = %(id)s AND c.user_id = %(user_id)s ORDER BY c.name
            """,
            {"user_id": user_id, "id": paper_id},
        )
    return paper


# ---------------------------------------------------------------------------
# collections, collection_papers
# ---------------------------------------------------------------------------


def create_collection(user_id: int, name: str, description: str | None = None,
                      goal_id: int | None = None) -> dict:
    if goal_id is not None:
        get_goal(user_id, goal_id)  # ownership check
    return _write_returning(
        """
        INSERT INTO collections (user_id, name, description, goal_id)
        VALUES (%(user_id)s, %(name)s, %(description)s, %(goal_id)s)
        ON CONFLICT (user_id, name) DO UPDATE SET
            description = COALESCE(EXCLUDED.description, collections.description),
            goal_id = COALESCE(EXCLUDED.goal_id, collections.goal_id),
            updated_at = now()
        RETURNING *
        """,
        {"user_id": user_id, "name": name.strip(), "description": description, "goal_id": goal_id},
    )


def list_collections(user_id: int, goal_id: int | None = None) -> list[dict]:
    return lakebase.run_query(
        """
        SELECT c.*, g.title AS goal_title,
               (SELECT COUNT(*) FROM collection_papers cp WHERE cp.collection_id = c.id) AS paper_count
        FROM collections c LEFT JOIN learning_goals g ON g.id = c.goal_id
        WHERE c.user_id = %(user_id)s AND (%(goal_id)s::bigint IS NULL OR c.goal_id = %(goal_id)s)
        ORDER BY c.updated_at DESC
        """,
        {"user_id": user_id, "goal_id": goal_id},
    )


def get_collection(user_id: int, collection_id: int) -> dict:
    rows = lakebase.run_query(
        """
        SELECT c.*, g.title AS goal_title, g.level AS goal_level
        FROM collections c LEFT JOIN learning_goals g ON g.id = c.goal_id
        WHERE c.id = %(id)s AND c.user_id = %(user_id)s
        """,
        {"id": collection_id, "user_id": user_id},
    )
    if not rows:
        raise NotFound(f"No collection {collection_id}.")
    collection = rows[0]
    collection["papers"] = lakebase.run_query(
        _PAPER_SUMMARY_SQL.replace(
            "FROM papers p",
            """, cp.added_by, cp.reason, cp.added_at,
               (SELECT rp.status FROM reading_progress rp
                 WHERE rp.user_id = %(user_id)s AND rp.paper_id = p.id
                 ORDER BY rp.updated_at DESC LIMIT 1) AS status
            FROM papers p JOIN collection_papers cp ON cp.paper_id = p.id""",
        )
        + " WHERE cp.collection_id = %(id)s ORDER BY cp.added_at",
        {"id": collection_id, "user_id": user_id},
    )
    return collection


def collection_paper_ids(user_id: int, collection_id: int) -> list[str]:
    get_collection_owner_check(user_id, collection_id)
    rows = lakebase.run_query(
        "SELECT paper_id FROM collection_papers WHERE collection_id = %s ORDER BY added_at",
        (collection_id,),
    )
    return [r["paper_id"] for r in rows]


def get_collection_owner_check(user_id: int, collection_id: int) -> None:
    rows = lakebase.run_query(
        "SELECT 1 FROM collections WHERE id = %s AND user_id = %s", (collection_id, user_id)
    )
    if not rows:
        raise NotFound(f"No collection {collection_id}.")


def add_papers_to_collection(user_id: int, collection_id: int, paper_ids: list[str],
                             added_by: str = "user", reason: str | None = None) -> dict:
    get_collection_owner_check(user_id, collection_id)
    known = {r["id"] for r in lakebase.run_query(
        "SELECT id FROM papers WHERE id = ANY(%s)", (list(paper_ids),))}
    missing = [p for p in paper_ids if p not in known]
    rows = [(collection_id, pid, added_by, reason) for pid in paper_ids if pid in known]
    added: list[dict] = []
    if rows:
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                added = execute_values(
                    cur,
                    """
                    INSERT INTO collection_papers (collection_id, paper_id, added_by, reason)
                    VALUES %s ON CONFLICT (collection_id, paper_id) DO NOTHING
                    RETURNING paper_id
                    """,
                    rows, template="(%s,%s,%s,%s)", fetch=True,
                )
                cur.execute("UPDATE collections SET updated_at = now() WHERE id = %s", (collection_id,))
            conn.commit()
    added_ids = [r["paper_id"] for r in added]
    return {
        "added": added_ids,
        "already_present": [p for p in paper_ids if p in known and p not in added_ids],
        "not_in_library": missing,
    }


def remove_paper_from_collection(user_id: int, collection_id: int, paper_id: str) -> int:
    get_collection_owner_check(user_id, collection_id)
    return lakebase.run_write(
        "DELETE FROM collection_papers WHERE collection_id = %s AND paper_id = %s",
        (collection_id, paper_id),
    )


def goal_paper_ids(user_id: int, goal_id: int) -> list[str]:
    """Papers attached to a goal: its collections plus anything already planned."""
    get_goal(user_id, goal_id)
    rows = lakebase.run_query(
        """
        SELECT DISTINCT cp.paper_id FROM collection_papers cp
        JOIN collections c ON c.id = cp.collection_id
        WHERE c.user_id = %(user_id)s AND c.goal_id = %(goal_id)s
        UNION
        SELECT paper_id FROM reading_progress WHERE user_id = %(user_id)s AND goal_id = %(goal_id)s
        """,
        {"user_id": user_id, "goal_id": goal_id},
    )
    return [r["paper_id"] for r in rows]


# ---------------------------------------------------------------------------
# reading_progress (also the persisted plan)
# ---------------------------------------------------------------------------


def save_plan(user_id: int, goal_id: int | None, items: list[dict]) -> int:
    """Persist plan order. Existing statuses survive a re-plan; positions do not.

    Papers that were in the old plan but not the new one keep their progress
    row with plan_position cleared, so finishing a paper is never forgotten.
    """
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE reading_progress SET plan_position = NULL, plan_stage = NULL, updated_at = now()
                WHERE user_id = %s AND goal_id IS NOT DISTINCT FROM %s
                """,
                (user_id, goal_id),
            )
            if items:
                execute_values(
                    cur,
                    """
                    INSERT INTO reading_progress
                        (user_id, paper_id, goal_id, plan_position, plan_stage, rationale)
                    VALUES %s
                    ON CONFLICT (user_id, paper_id, goal_id) DO UPDATE SET
                        plan_position = EXCLUDED.plan_position,
                        plan_stage = EXCLUDED.plan_stage,
                        rationale = EXCLUDED.rationale,
                        updated_at = now()
                    """,
                    [(user_id, i["paper_id"], goal_id, i["position"], i["stage"], i["rationale"])
                     for i in items],
                    template="(%s,%s,%s,%s,%s,%s)",
                )
        conn.commit()
    return len(items)


def get_plan(user_id: int, goal_id: int | None) -> list[dict]:
    return lakebase.run_query(
        """
        SELECT rp.paper_id, rp.status, rp.plan_position AS position, rp.plan_stage AS stage,
               rp.rationale, rp.started_at, rp.completed_at,
               p.title, p.publication_year, p.cited_by_count, p.referenced_works, p.oa_url, p.doi
        FROM reading_progress rp JOIN papers p ON p.id = rp.paper_id
        WHERE rp.user_id = %(user_id)s AND rp.goal_id IS NOT DISTINCT FROM %(goal_id)s
          AND rp.plan_position IS NOT NULL
        ORDER BY rp.plan_position
        """,
        {"user_id": user_id, "goal_id": goal_id},
    )


def update_progress(user_id: int, paper_id: str, status: str, goal_id: int | None = None) -> dict:
    if goal_id is not None:
        get_goal(user_id, goal_id)
    return _write_returning(
        """
        INSERT INTO reading_progress (user_id, paper_id, goal_id, status, started_at, completed_at)
        VALUES (%(user_id)s, %(paper_id)s, %(goal_id)s, %(status)s,
                CASE WHEN %(status)s IN ('reading', 'completed') THEN now() END,
                CASE WHEN %(status)s = 'completed' THEN now() END)
        ON CONFLICT (user_id, paper_id, goal_id) DO UPDATE SET
            status = EXCLUDED.status,
            started_at = COALESCE(reading_progress.started_at, EXCLUDED.started_at),
            completed_at = CASE WHEN EXCLUDED.status = 'completed'
                                THEN COALESCE(reading_progress.completed_at, now()) END,
            updated_at = now()
        RETURNING *
        """,
        {"user_id": user_id, "paper_id": paper_id, "goal_id": goal_id, "status": status},
    )


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


def create_note(user_id: int, body: str, paper_id: str | None = None,
                goal_id: int | None = None) -> dict:
    if goal_id is not None:
        get_goal(user_id, goal_id)
    if paper_id is not None and not lakebase.run_query("SELECT 1 FROM papers WHERE id = %s", (paper_id,)):
        raise NotFound(f"No paper {paper_id} in the library yet.")
    return _write_returning(
        """
        INSERT INTO notes (user_id, paper_id, goal_id, body, body_hash)
        VALUES (%(user_id)s, %(paper_id)s, %(goal_id)s, %(body)s, %(hash)s)
        RETURNING *
        """,
        {"user_id": user_id, "paper_id": paper_id, "goal_id": goal_id,
         "body": body, "hash": text_hash(body)},
    )


def list_notes(user_id: int, paper_id: str | None = None, goal_id: int | None = None) -> list[dict]:
    return lakebase.run_query(
        """
        SELECT n.*, p.title AS paper_title FROM notes n LEFT JOIN papers p ON p.id = n.paper_id
        WHERE n.user_id = %(user_id)s
          AND (%(paper_id)s::text IS NULL OR n.paper_id = %(paper_id)s)
          AND (%(goal_id)s::bigint IS NULL OR n.goal_id = %(goal_id)s)
        ORDER BY n.created_at DESC
        """,
        {"user_id": user_id, "paper_id": paper_id, "goal_id": goal_id},
    )


def delete_note(user_id: int, note_id: int) -> int:
    # context_chunks.note_id cascades, so the note's vectors go with it.
    return lakebase.run_write("DELETE FROM notes WHERE id = %s AND user_id = %s", (note_id, user_id))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def user_stats(user_id: int) -> dict:
    return lakebase.run_query(
        """
        SELECT
          (SELECT COUNT(*) FROM learning_goals WHERE user_id = %(u)s AND status = 'active') AS active_goals,
          (SELECT COUNT(*) FROM collections WHERE user_id = %(u)s) AS collections,
          (SELECT COUNT(DISTINCT cp.paper_id) FROM collection_papers cp
             JOIN collections c ON c.id = cp.collection_id WHERE c.user_id = %(u)s) AS saved_papers,
          (SELECT COUNT(*) FROM reading_progress WHERE user_id = %(u)s AND status = 'completed') AS completed,
          (SELECT COUNT(*) FROM reading_progress WHERE user_id = %(u)s AND status = 'reading') AS reading,
          (SELECT COUNT(*) FROM notes WHERE user_id = %(u)s) AS notes
        """,
        {"u": user_id},
    )[0]


def _write_returning(sql: str, params: dict, missing: str | None = None) -> dict:
    """Run a write with RETURNING, commit, and hand back the single row."""
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise NotFound(missing or "No matching row.")
    return dict(row)
