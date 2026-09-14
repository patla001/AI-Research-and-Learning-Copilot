"""
End-to-end test for the research copilot.

    python test_deployment.py http://localhost:8000
    python test_deployment.py https://<app>.databricksapps.com

Same method as the weather app: every write is checked twice - through the API,
and straight in Lakebase over lakebase.py. An app that cached in memory or wrote
somewhere else passes the first check and fails the second.

Spends ~20 OpenAlex credits (two searches). The copilot check is skipped unless
ANTHROPIC_API_KEY is set on the app; pass --chat to run it (one agent request).
Exits non-zero on any failure.
"""

from __future__ import annotations

import sys
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

import lakebase  # noqa: E402

passed = failed = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    return ok


def js(resp) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}


def sql_one(query: str, params=None) -> dict:
    rows = lakebase.run_query(query, params)
    return rows[0] if rows else {}


def main(base: str, run_chat: bool) -> int:
    base = base.rstrip("/")
    s = requests.Session()
    try:  # Databricks Apps sit behind OAuth; locally this is a no-op.
        from databricks.sdk import WorkspaceClient
        if "databricksapps.com" in base:
            s.headers.update(WorkspaceClient().config.authenticate())
    except Exception:
        pass
    run = uuid.uuid4().hex[:6]
    print(f"target: {base}  run: {run}\n")

    r = s.get(f"{base}/api", timeout=30)
    check(r.status_code == 200 and "endpoints" in js(r), "GET /api", f"HTTP {r.status_code}")

    r = s.get(f"{base}/me", timeout=60)
    me = js(r)
    if not check(r.status_code == 200 and me.get("user_id"), "GET /me creates the user", str(me)[:120]):
        print("\ncannot continue without a user")
        return 1
    user_id = me["user_id"]
    check(bool(sql_one("SELECT id FROM users WHERE id = %s", (user_id,))), "  user row exists in Lakebase")

    # -- goal ---------------------------------------------------------------
    # A realistic goal title, intent verb included: goal-based discovery has to
    # cope with how learners actually phrase objectives.
    r = s.post(f"{base}/goals", json={"title": "Understand retrieval augmented generation",
                                      "description": f"How RAG systems retrieve, ground and get evaluated ({run})",
                                      "level": "beginner"}, timeout=120)
    goal = js(r).get("goal") or {}
    check(r.status_code == 201 and goal.get("id"), "POST /goals", f"HTTP {r.status_code}")
    goal_id = goal.get("id")
    check(sql_one("SELECT COUNT(*) AS n FROM context_chunks WHERE goal_id = %s", (goal_id,)).get("n", 0) >= 1,
          "  goal was embedded (context_chunks)")

    # -- discovery ----------------------------------------------------------
    before = sql_one("SELECT COUNT(*) AS n FROM papers")["n"]
    r = s.get(f"{base}/papers/search", params={"goal_id": goal_id, "limit": 6}, timeout=60)
    results = js(r).get("results") or []
    check(r.status_code == 200 and len(results) > 0, "GET /papers/search?goal_id=",
          f"{len(results)} result(s) for query {js(r).get('query')!r}")
    check(all(x["id"].startswith("W") for x in results), "  results carry OpenAlex work ids")

    r = s.get(f"{base}/papers/search", params={"q": "attention transformer",
                                               "sort": "citations", "limit": 5}, timeout=60)
    cites = [x["cited_by_count"] for x in js(r).get("results") or []]
    check(r.status_code == 200 and cites == sorted(cites, reverse=True),
          "  sort=citations is ordered by citations", str(cites))
    check(sql_one("SELECT COUNT(*) AS n FROM papers")["n"] == before, "  search does not write papers")

    ids = [x["id"] for x in results[:4]]
    if not ids:
        print("\nno search results to import - cannot continue (check OpenAlex credits)")
        print(f"\n{passed} passed, {failed + 1} failed")
        return 1

    # -- import -------------------------------------------------------------
    r = s.post(f"{base}/papers/import", json={"paper_ids": ids, "fetch_content": False}, timeout=300)
    imported = js(r).get("imported") or []
    check(r.status_code == 200 and set(imported) == set(ids), "POST /papers/import", f"{len(imported)} imported")
    check(sql_one("SELECT COUNT(*) AS n FROM papers WHERE id = ANY(%s)", (ids,))["n"] == len(ids),
          "  papers present in Lakebase")
    check(sql_one("SELECT COUNT(*) AS n FROM paper_authors WHERE paper_id = ANY(%s)", (ids,))["n"] > 0,
          "  paper_authors populated")
    check(sql_one("""SELECT COUNT(*) AS n FROM papers p WHERE p.id = ANY(%s) AND p.abstract IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM context_chunks c
                                     WHERE c.paper_id = p.id AND c.source_type = 'abstract')""", (ids,))["n"] == 0,
          "  every imported abstract is embedded")

    r = s.post(f"{base}/papers/import", json={"paper_ids": ids, "fetch_content": False}, timeout=300)
    dupes = sql_one("SELECT COUNT(*) - COUNT(DISTINCT id) AS n FROM papers")["n"]
    chunk_dupes = sql_one("""SELECT COUNT(*) AS n FROM (SELECT paper_id, source_type, chunk_index
                             FROM context_chunks WHERE paper_id IS NOT NULL
                             GROUP BY 1,2,3 HAVING COUNT(*) > 1) x""")["n"]
    check(r.status_code == 200 and dupes == 0 and chunk_dupes == 0, "re-import is idempotent",
          f"{dupes} duplicate papers, {chunk_dupes} duplicate chunks")
    check(before <= sql_one("SELECT COUNT(*) AS n FROM papers")["n"], "  paper count only grows via import")

    r = s.get(f"{base}/papers/{ids[0]}", timeout=30)
    check(r.status_code == 200 and js(r).get("authors"), "GET /papers/<id> with authors")

    # -- collection ---------------------------------------------------------
    r = s.post(f"{base}/collections", json={"name": f"RAG basics {run}", "goal_id": goal_id}, timeout=30)
    collection_id = (js(r).get("collection") or {}).get("id")
    check(r.status_code == 201 and collection_id, "POST /collections")
    r = s.post(f"{base}/collections/{collection_id}/papers",
               json={"paper_ids": ids + ["W1"], "reason": "test"}, timeout=30)
    body = js(r)
    check(set(body.get("added") or []) == set(ids) and body.get("not_in_library") == ["W1"],
          "POST /collections/<id>/papers adds saved papers, reports unknown ones", str(body)[:160])
    check(sql_one("SELECT COUNT(*) AS n FROM collection_papers WHERE collection_id = %s",
                  (collection_id,))["n"] == len(ids), "  collection_papers rows in Lakebase")

    # -- plan + progress ----------------------------------------------------
    r = s.post(f"{base}/plans", json={"goal_id": goal_id}, timeout=60)
    items = js(r).get("items") or []
    check(r.status_code == 200 and len(items) == len(ids), "POST /plans", f"{len(items)} item(s)")
    check([i["position"] for i in items] == list(range(1, len(items) + 1)), "  positions are 1..n")
    check(all(i.get("rationale") and i.get("stage") for i in items), "  every item has a stage and rationale")
    check(sql_one("""SELECT COUNT(*) AS n FROM reading_progress WHERE user_id = %s AND goal_id = %s
                     AND plan_position IS NOT NULL""", (user_id, goal_id))["n"] == len(ids),
          "  plan persisted to reading_progress")

    first = items[0]["paper_id"] if items else None
    r = s.get(f"{base}/plans/next", params={"goal_id": goal_id}, timeout=30)
    check(r.status_code == 200 and (js(r).get("next") or {}).get("paper_id") == first,
          "GET /plans/next starts at position 1")
    r = s.put(f"{base}/progress", json={"paper_id": first, "status": "completed", "goal_id": goal_id}, timeout=30)
    check(r.status_code == 200, "PUT /progress completed")
    row = sql_one("""SELECT status, completed_at FROM reading_progress
                     WHERE user_id = %s AND paper_id = %s AND goal_id = %s""", (user_id, first, goal_id))
    check(row.get("status") == "completed" and row.get("completed_at"), "  completed_at set in Lakebase")
    r = s.get(f"{base}/plans/next", params={"goal_id": goal_id}, timeout=30)
    nxt = js(r)
    check((nxt.get("next") or {}).get("paper_id") != first and nxt.get("completed") == 1,
          "  next recommendation advances", str(nxt.get("reason"))[:80])
    r = s.post(f"{base}/plans", json={"goal_id": goal_id}, timeout=60)
    check(sql_one("SELECT status FROM reading_progress WHERE user_id = %s AND paper_id = %s AND goal_id = %s",
                  (user_id, first, goal_id)).get("status") == "completed", "  re-planning keeps progress")

    # -- notes + retrieval --------------------------------------------------
    r = s.post(f"{base}/notes", json={"paper_id": ids[0], "goal_id": goal_id,
                                      "body": f"My takeaway {run}: dense retrievers need hard negatives."}, timeout=60)
    note_id = (js(r).get("note") or {}).get("id")
    check(r.status_code == 201 and note_id, "POST /notes")
    check(sql_one("SELECT COUNT(*) AS n FROM context_chunks WHERE note_id = %s", (note_id,))["n"] >= 1,
          "  note embedded")

    r = s.post(f"{base}/retrieve", json={"query": "how is retrieval augmented generation evaluated",
                                         "collection_id": collection_id, "top_k": 6, "per_paper": 2}, timeout=60)
    evidence = js(r).get("evidence") or []
    check(r.status_code == 200 and evidence, "POST /retrieve (collection scope)", f"{len(evidence)} passage(s)")
    check(all(e["paper_id"] in ids for e in evidence if e["source_type"] != "note"),
          "  scoped evidence stays inside the collection")
    per_paper = {}
    for e in evidence:
        per_paper[e["citation_key"]] = per_paper.get(e["citation_key"], 0) + 1
    check(max(per_paper.values(), default=0) <= 2, "  at most 2 passages per paper", str(per_paper))
    sims = [e["similarity"] for e in evidence]
    check(sims == sorted(sims, reverse=True) and all(isinstance(x, float) for x in sims),
          "  ranked by similarity, as JSON numbers")

    r = s.post(f"{base}/retrieve", json={"query": "hard negatives", "paper_ids": [ids[0]]}, timeout=60)
    keys = [e["citation_key"] for e in js(r).get("evidence") or []]
    check(f"N{note_id}" in keys, "  the learner's note is retrievable evidence", str(keys))

    # -- isolation: another user cannot see this user's data ----------------
    if "databricksapps.com" not in base:
        other = {"X-Forwarded-Email": f"other-{run}@example.edu"}
        r = s.get(f"{base}/collections/{collection_id}", headers=other, timeout=30)
        check(r.status_code == 404, "another user gets 404 for this collection", f"HTTP {r.status_code}")
        r = s.post(f"{base}/retrieve", headers=other, json={"query": "hard negatives"}, timeout=60)
        leaked = [e for e in js(r).get("evidence") or [] if e["source_type"] == "note"]
        check(not leaked, "  and never retrieves this user's notes", f"{len(leaked)} leaked")

    # -- edge cases ---------------------------------------------------------
    check(s.post(f"{base}/goals", json={"title": ""}, timeout=30).status_code == 400, "empty goal title -> 400")
    check(s.post(f"{base}/goals", json={"title": "x", "level": "expert"}, timeout=30).status_code == 400,
          "invalid level -> 400")
    check(s.get(f"{base}/papers/search", params={"q": "x" * 400}, timeout=30).status_code == 400,
          "oversized query -> 400")
    check(s.post(f"{base}/papers/import", json={"paper_ids": "W1"}, timeout=30).status_code == 400,
          "paper_ids not a list -> 400")
    check(s.put(f"{base}/progress", json={"paper_id": ids[0], "status": "done"}, timeout=30).status_code == 400,
          "invalid progress status -> 400")
    check(s.get(f"{base}/collections/999999999", timeout=30).status_code == 404, "unknown collection -> 404")

    # -- copilot ------------------------------------------------------------
    stats = js(s.get(f"{base}/copilot/stats", timeout=60))
    check("agent" in stats and "context" in stats, "GET /copilot/stats")
    if run_chat and (stats.get("agent") or {}).get("enabled"):
        r = s.post(f"{base}/copilot/chat", json={
            "message": f"Using my collection {collection_id}, compare how these papers evaluate RAG. Cite sources.",
            "goal_id": goal_id}, timeout=600)
        data = js(r)
        check(r.status_code == 200 and data.get("answer"), "POST /copilot/chat", data.get("stop_reason", ""))
        check(any(t["tool"] == "retrieve_evidence" for t in data.get("tool_calls") or []),
              "  the agent retrieved evidence")
        check(len(data.get("citations") or []) > 0, "  the answer carries verified citations",
              f"{len(data.get('citations') or [])} verified, {data.get('unverified_citations')} unverified")
    else:
        print("[SKIP] copilot chat - pass --chat and set ANTHROPIC_API_KEY on the app")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(main(args[0] if args else "http://localhost:8000", "--chat" in sys.argv))
