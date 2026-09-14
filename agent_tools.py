"""
The copilot's tools: JSON schemas for Claude plus the functions behind them.

Layering is the same as the day-3 MCP server - thin tool wrappers over services
(library.py / store.py), and every tool returns {"status": "success", ...} or
{"status": "error", "message": ...} instead of raising. The one addition is
ToolContext: it carries the signed-in user's id, and no tool accepts a user id
as an argument, so nothing the model writes can widen a tool's reach.

Tool list, mapped to the capstone brief:

    find papers matching a goal      find_papers, import_papers
    summarize and compare research   retrieve_evidence, get_paper
    generate a sequenced plan        generate_reading_plan
    add papers to a collection       create_collection, add_to_collection
    track progress / next paper      update_progress, recommend_next_paper
    context                          list_learning_context, add_note
    citations                        every evidence-bearing result carries citation keys
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import context_pipeline
import library
import retrieval
import store
from openalex_client import OpenAlexClient, OpenAlexError

logger = logging.getLogger(__name__)

# Bounds per agent run. OpenAlex searches cost credits and imports write rows,
# so a looping model must hit a ceiling, not the daily budget.
MAX_SEARCHES_PER_RUN = 4
MAX_IMPORTS_PER_RUN = 20


@dataclass
class ToolContext:
    user_id: int
    openalex: OpenAlexClient
    searches: int = 0
    imports: int = 0
    # Every citation key the agent has legitimately seen this run, mapped to a
    # display record. agent.py validates the final answer against it.
    citable: dict[str, dict] = field(default_factory=dict)
    # Writes performed, surfaced to the UI so it can refresh what changed.
    changes: list[dict] = field(default_factory=list)

    def remember_papers(self, papers: list[dict]) -> None:
        for paper in papers:
            pid = paper.get("id") or paper.get("paper_id")
            if pid:
                self.citable.setdefault(pid, {
                    "key": pid, "kind": "paper", "title": paper.get("title"),
                    "year": paper.get("publication_year"), "doi": paper.get("doi"),
                    "url": f"https://openalex.org/{pid}",
                })


def _ok(**payload) -> dict:
    return {"status": "success", **payload}


def _error(message: str, hint: str | None = None) -> dict:
    return {"status": "error", "message": message, **({"hint": hint} if hint else {})}


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------


def list_learning_context(ctx: ToolContext, goal_id: int | None = None) -> dict:
    goals = store.list_goals(ctx.user_id)
    collections = store.list_collections(ctx.user_id, goal_id=goal_id)
    return _ok(
        goals=[{k: g[k] for k in ("id", "title", "description", "level", "status",
                                  "planned_papers", "completed_papers")} for g in goals],
        collections=[{k: c[k] for k in ("id", "name", "goal_id", "goal_title", "paper_count")}
                     for c in collections],
    )


def find_papers(ctx: ToolContext, query: str, limit: int = 8, from_year: int | None = None,
                open_access_only: bool = False, sort: str = "relevance") -> dict:
    if ctx.searches >= MAX_SEARCHES_PER_RUN:
        return _error(f"Search limit for this request reached ({MAX_SEARCHES_PER_RUN}).",
                      "Work with the results you already have, or ask the learner to refine the goal.")
    ctx.searches += 1
    try:
        cards = library.discover(ctx.openalex, query, limit=max(1, min(int(limit), 15)),
                                 from_year=from_year, open_access_only=open_access_only, sort=sort)
    except OpenAlexError as err:
        return _error(str(err))
    ctx.remember_papers(cards)
    return _ok(query=query, count=len(cards), results=cards,
               note="Search results are not saved. Call import_papers before adding them to a collection.")


def import_papers(ctx: ToolContext, paper_ids: list[str]) -> dict:
    remaining = MAX_IMPORTS_PER_RUN - ctx.imports
    if remaining <= 0:
        return _error(f"Import limit for this request reached ({MAX_IMPORTS_PER_RUN}).")
    paper_ids = paper_ids[:remaining]
    ctx.imports += len(paper_ids)
    result = library.import_papers(ctx.openalex, paper_ids)
    ctx.remember_papers(store.get_papers(result["imported"]))
    if result["imported"]:
        ctx.changes.append({"type": "papers_imported", "paper_ids": result["imported"]})
    return _ok(imported=result["imported"], errors=result["errors"],
               full_text_fetched=[c["id"] for c in result["content"]["fetched"]],
               embedded=result["embedded"])


def get_paper(ctx: ToolContext, paper_id: str) -> dict:
    paper = store.get_paper(paper_id, user_id=ctx.user_id)
    ctx.remember_papers([paper])
    abstract = paper.pop("abstract", None)
    paper.pop("referenced_works", None)
    return _ok(paper={**paper, "abstract": abstract, "citation_key": paper["id"]})


def retrieve_evidence(ctx: ToolContext, question: str, collection_id: int | None = None,
                      goal_id: int | None = None, paper_ids: list[str] | None = None,
                      top_k: int = 8, per_paper: int = 2) -> dict:
    if paper_ids:
        scope, scope_label = paper_ids, f"{len(paper_ids)} named paper(s)"
    elif collection_id is not None:
        scope, scope_label = store.collection_paper_ids(ctx.user_id, collection_id), f"collection {collection_id}"
    elif goal_id is not None:
        scope, scope_label = store.goal_paper_ids(ctx.user_id, goal_id), f"goal {goal_id}"
    else:
        scope, scope_label = None, "whole library"

    evidence = retrieval.retrieve_evidence(question, ctx.user_id, paper_ids=scope,
                                           top_k=top_k, per_paper=per_paper)
    for item in evidence:
        if item["source_type"] == "note":
            ctx.citable.setdefault(item["citation_key"], {
                "key": item["citation_key"], "kind": "note", "title": item.get("title"),
                "paper_id": item.get("paper_id"),
            })
        else:
            ctx.remember_papers([{"id": item["paper_id"], "title": item["title"],
                                  "publication_year": item["publication_year"], "doi": item["doi"]}])
    papers_covered = sorted({i["paper_id"] for i in evidence if i.get("paper_id")})
    return _ok(
        scope=scope_label,
        passages=len(evidence),
        papers_covered=papers_covered,
        evidence=retrieval.format_evidence(evidence),
        note=("Cite claims with the bracketed keys, e.g. [W4389984066] or [N12]. "
              "If the passages do not support a claim, say so instead of making it."),
    )


def create_collection(ctx: ToolContext, name: str, description: str | None = None,
                      goal_id: int | None = None) -> dict:
    collection = store.create_collection(ctx.user_id, name, description, goal_id)
    ctx.changes.append({"type": "collection_saved", "collection_id": collection["id"]})
    return _ok(collection={k: collection[k] for k in ("id", "name", "description", "goal_id")})


def add_to_collection(ctx: ToolContext, collection_id: int, paper_ids: list[str], reason: str) -> dict:
    result = store.add_papers_to_collection(ctx.user_id, collection_id, paper_ids,
                                            added_by="agent", reason=reason)
    if result["added"]:
        ctx.changes.append({"type": "collection_papers_added", "collection_id": collection_id,
                            "paper_ids": result["added"]})
    hint = None
    if result["not_in_library"]:
        hint = "Call import_papers for the ids in not_in_library, then add them again."
    return _ok(**result, **({"hint": hint} if hint else {}))


def generate_reading_plan(ctx: ToolContext, goal_id: int | None = None,
                          collection_id: int | None = None, max_papers: int = 12) -> dict:
    if goal_id is None and collection_id is None:
        return _error("Pass a goal_id or a collection_id.",
                      "Call list_learning_context to find the learner's goals and collections.")
    plan = library.build_plan(ctx.user_id, goal_id=goal_id, collection_id=collection_id,
                              max_papers=max(1, min(int(max_papers), 25)))
    if not plan["items"]:
        return _error("There are no papers attached to that goal or collection yet.",
                      "Find and import papers, add them to a collection linked to the goal, then plan.")
    ctx.remember_papers([{"id": i["paper_id"], "title": i["title"]} for i in plan["items"]])
    ctx.changes.append({"type": "plan_saved", "goal_id": plan["goal_id"]})
    return _ok(**plan, note="The order is computed from in-plan citations, stage and relevance. "
                            "Explain it; do not reorder it.")


def update_progress(ctx: ToolContext, paper_id: str, status: str, goal_id: int | None = None) -> dict:
    if status not in store.PROGRESS_STATUSES:
        return _error(f"status must be one of {list(store.PROGRESS_STATUSES)}.")
    row = store.update_progress(ctx.user_id, paper_id, status, goal_id)
    ctx.changes.append({"type": "progress_updated", "paper_id": paper_id, "status": status})
    return _ok(paper_id=paper_id, status=row["status"], goal_id=row["goal_id"])


def recommend_next_paper(ctx: ToolContext, goal_id: int) -> dict:
    recommendation = library.next_paper(ctx.user_id, goal_id)
    if recommendation["next"]:
        ctx.remember_papers([{"id": recommendation["next"]["paper_id"], **recommendation["next"]}])
    return _ok(**recommendation)


def add_note(ctx: ToolContext, body: str, paper_id: str | None = None, goal_id: int | None = None) -> dict:
    note = store.create_note(ctx.user_id, body, paper_id=paper_id, goal_id=goal_id)
    try:
        context_pipeline.embed_note(note["id"])
    except Exception:  # noqa: BLE001 - the backlog job will pick it up
        logger.exception("inline note embed failed")
    ctx.changes.append({"type": "note_added", "note_id": note["id"], "paper_id": paper_id})
    return _ok(note_id=note["id"], citation_key=f"N{note['id']}")


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

_ID_LIST = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}

# Order is fixed and the list is built once: tools render first in the prompt,
# so any change here would invalidate the prompt cache for every request.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_learning_context",
        "description": "List the learner's learning goals (with level and progress counts) and "
                       "collections. Call this first when a request refers to 'my goal' or "
                       "'my collection' without an id.",
        "input_schema": {"type": "object", "properties": {
            "goal_id": {"type": "integer", "description": "Only collections linked to this goal."},
        }},
    },
    {
        "name": "find_papers",
        "description": "Search OpenAlex for papers whose title and abstract match a query. Returns "
                       "id, title, year, venue, citation count, authors and an abstract preview. "
                       "Results are not saved. Costs OpenAlex credits, so write one focused query "
                       "per concept rather than many near-duplicates.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Topic keywords, e.g. 'retrieval augmented generation'."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 15},
            "from_year": {"type": "integer", "description": "Earliest publication year."},
            "open_access_only": {"type": "boolean"},
            "sort": {"type": "string", "enum": ["relevance", "citations", "recent"],
                     "description": "'citations' surfaces foundational work; 'recent' the frontier."},
        }, "required": ["query"]},
    },
    {
        "name": "import_papers",
        "description": "Save papers into the library by OpenAlex id (e.g. 'W4389984066'): stores "
                       "metadata and authors, fetches open-access full text when available, and "
                       "embeds them so retrieve_evidence can use them. Required before "
                       "add_to_collection.",
        "input_schema": {"type": "object", "properties": {"paper_ids": _ID_LIST},
                         "required": ["paper_ids"]},
    },
    {
        "name": "get_paper",
        "description": "Metadata, full abstract, authors, the learner's progress, notes and "
                       "collections for one saved paper.",
        "input_schema": {"type": "object", "properties": {"paper_id": {"type": "string"}},
                         "required": ["paper_id"]},
    },
    {
        "name": "retrieve_evidence",
        "description": "Semantic search over embedded abstracts, full text and the learner's own "
                       "notes, diversified so several papers are represented. Use it for every "
                       "summary, comparison or factual answer about papers - never answer those "
                       "from memory. Scope with paper_ids, collection_id or goal_id (checked in "
                       "that order); no scope searches the whole library. Passages come back "
                       "headed by citation keys.",
        "input_schema": {"type": "object", "properties": {
            "question": {"type": "string", "description": "What the evidence should answer. For "
                         "a comparison, call once per aspect (e.g. 'evaluation datasets')."},
            "paper_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "collection_id": {"type": "integer"},
            "goal_id": {"type": "integer"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            "per_paper": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "Max passages from one paper. Raise it to go deep on one paper."},
        }, "required": ["question"]},
    },
    {
        "name": "create_collection",
        "description": "Create a collection (or return the existing one with that name).",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "goal_id": {"type": "integer", "description": "Link the collection to a learning goal."},
        }, "required": ["name"]},
    },
    {
        "name": "add_to_collection",
        "description": "Add saved papers to one of the learner's collections, with a one-sentence "
                       "reason the learner will see next to each paper.",
        "input_schema": {"type": "object", "properties": {
            "collection_id": {"type": "integer"},
            "paper_ids": _ID_LIST,
            "reason": {"type": "string"},
        }, "required": ["collection_id", "paper_ids", "reason"]},
    },
    {
        "name": "generate_reading_plan",
        "description": "Compute and save a sequenced reading plan for a goal or collection. The "
                       "order comes from citations between the papers, stage (orientation, "
                       "foundations, core, frontier), goal relevance and the goal's level; each "
                       "item includes a rationale and prerequisites. Existing progress is kept.",
        "input_schema": {"type": "object", "properties": {
            "goal_id": {"type": "integer"},
            "collection_id": {"type": "integer"},
            "max_papers": {"type": "integer", "minimum": 1, "maximum": 25},
        }},
    },
    {
        "name": "update_progress",
        "description": "Record reading progress for a paper: planned, reading, completed or skipped.",
        "input_schema": {"type": "object", "properties": {
            "paper_id": {"type": "string"},
            "status": {"type": "string", "enum": list(store.PROGRESS_STATUSES)},
            "goal_id": {"type": "integer"},
        }, "required": ["paper_id", "status"]},
    },
    {
        "name": "recommend_next_paper",
        "description": "The next paper to read in a goal's saved plan, why, and anything it is "
                       "waiting on, plus completed/total counts.",
        "input_schema": {"type": "object", "properties": {"goal_id": {"type": "integer"}},
                         "required": ["goal_id"]},
    },
    {
        "name": "add_note",
        "description": "Save a note for the learner, optionally about a paper or goal. Only do "
                       "this when the learner asks you to record something.",
        "input_schema": {"type": "object", "properties": {
            "body": {"type": "string"},
            "paper_id": {"type": "string"},
            "goal_id": {"type": "integer"},
        }, "required": ["body"]},
    },
]

_HANDLERS: dict[str, Callable[..., dict]] = {
    "list_learning_context": list_learning_context,
    "find_papers": find_papers,
    "import_papers": import_papers,
    "get_paper": get_paper,
    "retrieve_evidence": retrieve_evidence,
    "create_collection": create_collection,
    "add_to_collection": add_to_collection,
    "generate_reading_plan": generate_reading_plan,
    "update_progress": update_progress,
    "recommend_next_paper": recommend_next_paper,
    "add_note": add_note,
}

assert {t["name"] for t in TOOL_DEFINITIONS} == set(_HANDLERS), "tool schema/handler mismatch"


def execute(name: str, arguments: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Run one tool call. Returns (json_text, is_error) for the tool_result block."""
    handler = _HANDLERS.get(name)
    if handler is None:
        result = _error(f"Unknown tool {name!r}.")
    else:
        try:
            result = handler(ctx, **(arguments or {}))
        except store.NotFound as err:
            result = _error(str(err), "Call list_learning_context to see valid ids.")
        except (TypeError, ValueError) as err:
            result = _error(f"Invalid arguments for {name}: {err}")
        except Exception:  # noqa: BLE001 - the model gets a clean error, logs get the trace
            logger.exception("tool %s failed", name)
            result = _error(f"{name} failed unexpectedly. Try a different approach or tell the learner.")
    return json.dumps(result, default=str), result.get("status") == "error"
