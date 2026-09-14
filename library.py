"""
Services shared by the Flask routes and the agent tools.

A route and a tool that do the same thing must do it the same way - otherwise
"add to collection" from the UI and from the copilot drift apart. Both call in
here, and this module composes store, openalex_client, context_pipeline,
retrieval and reading_plan.
"""

from __future__ import annotations

import logging
import os

import context_pipeline
import reading_plan
import retrieval
import store
from openalex_client import OpenAlexClient, OpenAlexError

logger = logging.getLogger(__name__)

# Full text costs 100 OpenAlex credits and adds dozens of chunks, so it is only
# fetched when a paper is SAVED (imported), never for search results.
FETCH_CONTENT_ON_IMPORT = os.environ.get("COPILOT_FETCH_CONTENT", "true").lower() in ("1", "true", "yes")
MAX_IMPORT = 25


# Words that describe the learner's intent rather than the topic. OpenAlex's
# title_and_abstract.search requires every term to match, so "Understand
# retrieval augmented generation" finds nothing - no abstract says "understand"
# alongside the topic. Measured on the live API: 0 results with the verb,
# thousands without it.
_GOAL_FILLER = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "about", "how", "what",
    "why", "i", "my", "me", "want", "learn", "learning", "understand", "understanding", "study",
    "studying", "master", "get", "into", "basics", "basic", "fundamentals", "intro",
    "introduction", "overview", "deep", "dive", "better", "works", "work",
}


def query_from_goal(title: str, max_terms: int = 6) -> str:
    """Turn a goal title into a topic query for OpenAlex."""
    words = [w.strip(".,:;!?()\"'").lower() for w in (title or "").split()]
    terms = [w for w in words if w and w not in _GOAL_FILLER]
    return " ".join(terms[:max_terms]) or (title or "").strip()


def discover(client: OpenAlexClient, query: str, **filters) -> list[dict]:
    """Search OpenAlex. Results are NOT stored - discovery is free of side effects."""
    works = client.search_works(query, **filters)
    return [_search_card(w) for w in works]


def import_papers(client: OpenAlexClient, work_ids: list[str], *,
                  fetch_content: bool | None = None, embed: bool = True) -> dict:
    """Fetch works by id, store them, optionally pull full text, and embed them.

    Single-work GETs are free on OpenAlex, so importing by id costs nothing
    unless full text is fetched. Failures are per paper: one bad id does not
    abandon the rest.
    """
    fetch_content = FETCH_CONTENT_ON_IMPORT if fetch_content is None else fetch_content
    ids = list(dict.fromkeys(work_ids))[:MAX_IMPORT]
    works, errors = [], []
    for work_id in ids:
        try:
            works.append(client.get_work(work_id))
        except OpenAlexError as err:
            errors.append({"id": work_id, "error": str(err)})

    result = store.upsert_works(works)
    imported = [w["paper"]["id"] for w in works]

    content = {"fetched": [], "unavailable": [], "skipped_no_key": not client.api_key}
    if fetch_content and client.api_key:
        for work in works:
            paper = work["paper"]
            if not paper.get("has_content"):
                content["unavailable"].append(paper["id"])
                continue
            try:
                fetched = client.fetch_content_text(paper["id"])
            except OpenAlexError as err:
                errors.append({"id": paper["id"], "error": f"full text: {err}"})
                continue
            if fetched:
                text, truncated = fetched
                store.set_paper_content(paper["id"], text)
                content["fetched"].append({"id": paper["id"], "chars": len(text), "truncated": truncated})
            else:
                content["unavailable"].append(paper["id"])

    embedded = None
    if embed and imported:
        try:
            embedded = context_pipeline.embed_papers(imported)
        except Exception as err:  # noqa: BLE001 - embedding is recoverable later
            # The papers are stored; the backlog job will embed them. Say so
            # rather than failing an import that mostly succeeded.
            logger.exception("inline embed failed for %s", imported)
            embedded = {"error": f"embedding deferred: {type(err).__name__}"}

    return {"imported": imported, "errors": errors, "content": content,
            "embedded": embedded, "openalex_usage": client.last_usage, **result}


def build_plan(user_id: int, goal_id: int | None = None, collection_id: int | None = None,
               max_papers: int = 15, save: bool = True) -> dict:
    """Sequence a goal's (or a collection's) papers and persist the order."""
    level = "intermediate"
    if collection_id is not None:
        collection = store.get_collection(user_id, collection_id)
        paper_ids = [p["id"] for p in collection["papers"]]
        goal_id = goal_id if goal_id is not None else collection.get("goal_id")
        if collection.get("goal_level"):
            level = collection["goal_level"]
    elif goal_id is not None:
        paper_ids = store.goal_paper_ids(user_id, goal_id)
    else:
        raise ValueError("A plan needs a goal_id or a collection_id.")

    if goal_id is not None:
        level = store.get_goal(user_id, goal_id)["level"]

    papers = store.get_papers(paper_ids)
    relevance = retrieval.goal_relevance(goal_id, user_id, paper_ids) if goal_id is not None else {}
    for paper in papers:
        paper["relevance"] = relevance.get(paper["id"])

    # Too many papers makes a plan no one follows: keep the most relevant, then
    # the most cited.
    papers.sort(key=lambda p: (-(p.get("relevance") or 0), -(p.get("cited_by_count") or 0)))
    papers = papers[:max(1, int(max_papers))]

    items = [item.as_dict() for item in reading_plan.build_plan(papers, level=level)]
    if save:
        store.save_plan(user_id, goal_id, items)
    return {"goal_id": goal_id, "level": level, "items": items,
            "dropped": max(0, len(paper_ids) - len(items))}


def next_paper(user_id: int, goal_id: int | None) -> dict:
    plan = store.get_plan(user_id, goal_id)
    if not plan:
        return {"next": None, "reason": "There is no reading plan for this goal yet.",
                "waiting_on": [], "completed": 0, "total": 0}
    in_plan = {row["paper_id"] for row in plan}
    for row in plan:
        row["prerequisites"] = [r for r in row.get("referenced_works") or [] if r in in_plan]
    recommendation = reading_plan.recommend_next(plan)
    if recommendation["next"]:
        recommendation["next"] = {k: v for k, v in recommendation["next"].items()
                                  if k != "referenced_works"}
    return recommendation


def _search_card(work: dict) -> dict:
    paper = work["paper"]
    abstract = paper.get("abstract") or ""
    return {
        "id": paper["id"],
        "title": paper["title"],
        "publication_year": paper.get("publication_year"),
        "venue": paper.get("venue"),
        "work_type": paper.get("work_type"),
        "cited_by_count": paper.get("cited_by_count"),
        "is_oa": paper.get("is_oa"),
        "has_content": paper.get("has_content"),
        "doi": paper.get("doi"),
        "primary_topic": paper.get("primary_topic"),
        "authors": [a["display_name"] for a in work.get("authors") or []][:6],
        "abstract_preview": abstract[:600] + ("..." if len(abstract) > 600 else ""),
    }
