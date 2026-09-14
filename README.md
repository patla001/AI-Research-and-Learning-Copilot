# Reading Route: AI Research and Learning Copilot

A learner names what they want to learn. The app finds papers on OpenAlex and saves
the useful ones into collections. It sequences them into a reading plan, tracks
progress, and answers questions about them with citations that are checked against
what was actually retrieved.

```
learning goal ──► OpenAlex discovery ──► import (metadata, authors, OA full text)
                                               │
                        chunk + embed (bge-small, 384d) ──► Lakebase pgvector
                                               │
   Claude agent (tool use) ◄── retrieve_evidence: scoped, diversified passages
        │
        ├── collections · reading plan · progress · next paper · notes   (Lakebase)
        └── answer with [W…]/[N…] citations ──► verified against retrieved keys
```

It is built on the same template as the weather retrieval service: a Flask app on
Databricks Apps, the same `lakebase.py`, pgvector with HNSW cosine, fastembed/ONNX,
a Next.js static export served from the same origin, `sql/` DDL, a secret bootstrap
script and an end-to-end test. The tool layering (thin tools over services,
`{"status": ...}` results, no exceptions reach the model) comes from the day-3 MCP
server.

---

## How it maps to the brief

| Requirement | Where |
|---|---|
| Third-party API: OpenAlex | `openalex_client.py`, which handles search, fetch, abstract reconstruction and GROBID full text |
| Lakebase tables `users`, `learning_goals`, `papers`, `authors`, `paper_authors`, `collections`, `collection_papers`, `reading_progress`, `notes` | `sql/01_schema.sql` |
| Embed abstracts, paper content, notes, goals | `context_chunks` (`sql/02`) + `context_pipeline.py` |
| Retrieve across papers instead of sending a collection | `retrieval.py`: scope, over-fetch, per-paper cap |
| Find papers matching a goal | `find_papers`, `import_papers` tools; `GET /papers/search?goal_id=` |
| Summarize and compare research | `retrieve_evidence` tool, called once per aspect being compared |
| Sequenced reading plan | `reading_plan.py` (deterministic) + `generate_reading_plan` tool |
| Add papers to a collection | `add_to_collection` tool; `POST /collections/<id>/papers` |
| Track progress, recommend next paper | `update_progress`, `recommend_next_paper`; `PUT /progress`, `GET /plans/next` |
| Citations in answers | `[W…]` / `[N…]` keys; `agent.validate_citations` |

---

## Design decisions

### OpenAlex, measured

| Call | Credits | Notes |
|---|---:|---|
| `GET /works/{id}` | 0 | Importing by id is free |
| `GET /works?filter=…` | 10 | Every search |
| `GET content.openalex.org/works/{id}.grobid-xml` | 100 | **Key required** (401 without one) |

The numbers come from the `X-RateLimit-Credits-Used` header on live calls. Without a
key the budget is 1,000 credits per day. With the free key the live `X-RateLimit-Limit`
header read 10,000, which is about 100 full-text downloads a day. That shaped these
choices:

- **Search results are not stored.** Discovery has no side effects. Only saving a
  paper writes rows.
- **Full text is fetched on import only**, never for search results, and it is
  capped at 60,000 characters and 40 chunks per paper.
- **Search ranks by relevance upstream, always.** On "retrieval augmented
  generation", OpenAlex's `sort=cited_by_count:desc` put a 2020 cognitive-science
  preprint and a 2016 walking-route paper in the top two, with or without a quoted
  phrase. "Most cited" and "Newest" therefore re-sort a relevance-ranked pool, which
  is the same single call and the same 10 credits.
- **Goal titles are cleaned before searching.** `title_and_abstract.search` requires
  every term, so "Understand retrieval augmented generation" matched nothing: no
  abstract contains "understand". `library.query_from_goal` strips intent words.
  The end-to-end test caught this.

### Schema

| Decision | Why |
|---|---|
| OpenAlex ids (`W…`, `A…`) are the primary keys of `papers` and `authors` | They are stable and global. Re-import upserts, and the agent cites a paper by the id the API uses |
| User-owned tables use `BIGSERIAL` and every query filters on `user_id` | `store.py` is the only data path for routes and tools, so an agent tool cannot reach another user's rows. The e2e test checks this with a second identity |
| Affiliations are stored on `paper_authors` as well as `authors` | Affiliation belongs to an authorship. People move |
| `reading_progress` doubles as the persisted plan (`plan_position`, `plan_stage`, `rationale`) | Re-planning reorders papers but never forgets that one was read |
| `UNIQUE NULLS NOT DISTINCT (user_id, paper_id, goal_id)` | Progress without a goal is still one row per paper; a plain `UNIQUE` lets NULLs duplicate. Needs PG15+, and Lakebase is 16 |
| `abstract_hash` / `content_hash` / `body_hash` | A changed abstract deletes its chunks, which puts the paper back in the embed backlog. Same stale-vector guard as the weather app's `text_hash` |
| One `context_chunks` table with **three nullable FKs** (`paper_id`, `note_id`, `goal_id`) plus a CHECK that exactly the right one is set | A polymorphic `source_id TEXT` could not cascade. Here, deleting a note or paper takes its vectors with it |
| `user_id` denormalized onto chunks | Retrieval can exclude other users' notes in the same `WHERE` as the vector sort |

### Context engineering

The agent never receives a collection. `retrieve_evidence` does four things:

1. **Scope** to a collection, a goal's papers, explicit ids or the whole library,
   always resolved through `store.py`.
2. **Over-fetch** `top_k × 5` nearest chunks by cosine.
3. **Diversify** to at most `per_paper` (default 2) chunks per paper. Without it, a
   single 40-chunk full text wins every comparison question.
4. **Label** each passage with a citation key: `W4389984066` for paper text,
   `N12` for the learner's notes.

Paper chunks are embedded as `title + chunk`, so a mid-paper paragraph that never
names its topic is still found by the topic. Goals are embedded too, but they are
never evidence. They rank papers by relevance when a plan is built.

**Model: `BAAI/bge-small-en-v1.5`, 384 dims**, through fastembed. It has the same
width as the MiniLM model the weather app used, so `VECTOR(384)` carries over. BGE
is trained for retrieval and reads 512 tokens instead of 256, which matters when a
chunk is a paragraph of a paper. Chunking is 1,000 characters (about 220 tokens)
with 150 characters of overlap, breaking on sentence and word boundaries. **These are
starting values:** `sql/03` reports chunk counts so they can be tuned on a real corpus.

### Reading plans are computed, not generated

`reading_plan.py` is a pure function, so it is unit-tested. The model explains the
order but does not choose it.

- **Prerequisites.** If A cites B and both are in the plan, B comes first. This is a
  topological sort over in-plan citations, with cycles broken and labelled.
- **Stages** never interleave. *Orientation* holds surveys, for beginner and
  intermediate goals only. *Foundations* are cited by others in the plan and build
  on none. *Frontier* is recent work nothing in the plan builds on. *Core* is the rest.
- **Ties** break on in-plan citations, global citations, goal relevance and year.

Stages cannot contradict prerequisites. A foundations paper has no in-plan
prerequisites, and nothing in the plan cites a frontier paper. A survey does cite the
originals, and that edge is deliberately ignored for orientation.

`recommend_next` finishes what is in progress first, then picks the earliest planned
paper whose prerequisites are done. Otherwise it names what the next paper is
waiting on.

### The agent

A manual tool-use loop (`agent.py`) over 11 tools (`agent_tools.py`), on `claude-opus-5`:

- **Adaptive thinking at `effort: medium`.** This is a multi-step tool workflow over
  already-retrieved evidence, where medium holds quality at lower cost. Raise
  `COPILOT_EFFORT` if evaluations say otherwise.
- **Refusal fallback:** `fallbacks: "default"` with beta
  `server-side-fallback-2026-07-01`. A declined request is re-run server-side on
  Anthropic's recommended fallback model instead of failing.
- **Prompt caching.** The tool list and system prompt form a frozen, cached prefix.
  The learner's goals and collections go in the user turn, after the cache
  breakpoint. The smoke test asserts the tool list is byte-identical across turns.
- **Parallel tool results** go back in one user message. Thinking blocks are echoed
  unchanged within a request.
- **Citations are verified.** Every key a tool returns during the request is recorded
  in `ToolContext.citable`. A key in the answer that no tool returned is flagged
  `unverified_citations` and drawn with a red underline in the console. It is not
  silently dropped, because an invented citation is exactly what a learner needs to see.
- **Scope of writes.** Tools take no user id, only the id in `ToolContext`. Text from
  abstracts and full text reaches the model as tool results, and the system prompt
  says to treat it as material rather than instructions. The worst an injected
  abstract could do is make edits within the learner's own library.

| Guardrail | Default |
|---|---|
| Message length | 2,000 chars |
| History resent | last 12 text turns |
| Model calls per request | 10 |
| OpenAlex searches / imports per request | 4 / 20 |
| Requests per UTC day, whole app | 200 (`COPILOT_DAILY_LIMIT`) |

> ⚠️ Also set a spend limit in the Anthropic Console. An in-process ceiling cannot
> help when the process itself is the bug.

---

## Verification

| Check | Result |
|---|---|
| `pytest`: OpenAlex normalization on a live-response fixture, TEI parsing, chunking, plan ordering, next-paper logic, diversification, citation validation, tool schemas | **30 passed** |
| `test_deployment.py` against the app on Postgres 16 + pgvector 0.8 (Docker): every write checked through the API **and** in SQL, idempotent re-import, plan persistence, progress, scoped retrieval, note retrieval, cross-user isolation, edge cases | **44 passed, 0 failed** |
| `scripts/agent_loop_smoke.py`: the real agent loop against a stub Messages API and the local database. Checks the fallback header and body, thinking, effort, cache markers, byte-stable tools, `tool_result` round trip, thinking echo, verified vs. invented citations | **20 passed, 0 failed** |

### Deployed

The app runs on Databricks Apps as `research-copilot`, backed by its own Lakebase instance
`copilot-db` (PG 16, pgvector 0.8.0). `scripts/bootstrap_lakebase.py` created the role,
extension, schema and secret. See [DEPLOY.md](DEPLOY.md).

| Check | Result |
|---|---|
| `test_deployment.py` against the local app on **Lakebase** `copilot-db` (TLS, `copilot_app` role) | **44 passed, 0 failed** |
| `test_deployment.py https://research-copilot-2808874854650870.aws.databricksapps.com --chat`: the full suite through the Databricks OAuth proxy, identity from `X-Forwarded-Email`, **plus a real Claude request and an open-access full-text import** | **48 passed, 0 failed**. The agent called `retrieve_evidence` and its answer carried only verified citations. W4389984066's full text was fetched and capped at 60,000 characters, stored in `papers.content_text`, and embedded as 40 `content` chunks (the per-paper cap) |

The app is behind Databricks OAuth, so a viewer needs a workspace identity. The
cross-user isolation checks run locally only, because on Databricks Apps the platform
sets `X-Forwarded-Email` and a client cannot spoof it.

**Found by deploying:** `content.openalex.org` serves `.grobid-xml` as an
`application/gzip` attachment (`W….grobid.xml.gz`) with **no** `Content-Encoding`
header, so nothing decompresses it on the way in. The first full-text import failed
with `not well-formed (invalid token): line 1, column 0`, after being charged 100
credits. `openalex_client.decode_content` now gunzips on the gzip magic number, and
`tests/test_pure_logic.py` covers gzipped, plain and corrupt bodies.

---

## Running it

```bash
uv venv --python 3.13 && uv pip install --python .venv/bin/python -r requirements-dev.txt
cp .env.example .env                      # set LAKEBASE_URL; optionally the API keys

# Schema: Lakebase SQL editor (instance page), as a superuser:
#   sql/00_grant_app_role.sql (if needed), sql/01_schema.sql, sql/02_context_chunks.sql
# Or locally:
docker run -d --name copilot-pg -e POSTGRES_USER=copilot_app -e POSTGRES_PASSWORD=localdev \
  -e POSTGRES_DB=databricks_postgres -p 55432:5432 pgvector/pgvector:pg16
for f in sql/01_schema.sql sql/02_context_chunks.sql; do
  docker exec -i copilot-pg psql -U copilot_app -d databricks_postgres < $f; done
# LAKEBASE_URL=postgresql://copilot_app:localdev@localhost:55432/databricks_postgres?sslmode=disable

.venv/bin/python app.py                                   # http://localhost:8000
.venv/bin/python -m pytest                                # offline unit tests
.venv/bin/python test_deployment.py http://localhost:8000 # end to end
.venv/bin/python scripts/agent_loop_smoke.py              # agent loop, no tokens spent
.venv/bin/python notebooks/ingest_embeddings.py           # embed any backlog

cd web && npm install && npm run build                    # console -> ../static
```

Deployment: [DEPLOY.md](DEPLOY.md).

---

## API

| Method | Path | Body / query |
|---|---|---|
| `GET` | `/api`, `/healthz`, `/me` | |
| `GET` | `/copilot/stats` | user counts, embed backlog, agent budget |
| `GET` / `POST` | `/goals` | `{title, description?, level: beginner\|intermediate\|advanced, target_date?}` |
| `PATCH` | `/goals/<id>` | any of title, description, level, status, target_date |
| `GET` | `/papers/search` | `q` or `goal_id`, `sort=relevance\|citations\|recent`, `open_access`, `from_year`, `limit` |
| `POST` | `/papers/import` | `{paper_ids: [...], fetch_content?}` |
| `GET` | `/papers/<id>` | paper + authors + your progress, notes, collections |
| `GET` / `POST` | `/collections` | `{name, description?, goal_id?}` |
| `GET` | `/collections/<id>` | papers with status and why they were added |
| `POST` / `DELETE` | `/collections/<id>/papers[/<paper_id>]` | `{paper_ids, reason?}` |
| `POST` / `GET` | `/plans` | `{goal_id \| collection_id, max_papers?}` / `?goal_id=` |
| `GET` | `/plans/next` | `?goal_id=` |
| `PUT` | `/progress` | `{paper_id, status, goal_id?}` |
| `GET` / `POST` / `DELETE` | `/notes[/<id>]` | `{body, paper_id?, goal_id?}` |
| `POST` | `/retrieve` | `{query, collection_id? \| goal_id? \| paper_ids?, top_k?, per_paper?}` |
| `POST` | `/copilot/embed` | `{limit?}` |
| `POST` | `/copilot/chat` | `{message, history?: [{role, content}], goal_id?}` |

Errors are JSON. Bad input returns 400, a missing or someone else's row returns 404,
an OpenAlex failure returns 502, and an unavailable copilot returns 503. Exception
text never reaches the client, because a psycopg2 error carries the DSN.

---

## Limitations and next steps

- **No reranker.** Retrieval is single-stage cosine. BGE similarities are compressed:
  a relevant abstract scored 0.60 and an unrelated one 0.48 in a spot check, so
  scores rank well but should not be thresholded without calibration.
- **Character chunking.** A token-aware or section-aware splitter using GROBID's
  `<div>` boundaries would make full-text chunks more coherent.
- **Filtered HNSW.** A narrow scope with `ORDER BY embedding <=>` can return fewer
  than `top_k` rows once the index is in play at scale. pgvector 0.8's
  `hnsw.iterative_scan` is the fix. At student-project scale the planner scans.
- **Conversation history lives in the browser**, as text only. Persisting threads
  would need an 11th table.
- **Plans use in-plan citations only.** Two papers that build on the same unsaved
  classic are not linked. Pulling `referenced_works` one hop out would fix that.
- **An MCP server** over `agent_tools` (the day-3 pattern) would let a Databricks
  Agent Bricks agent use the same tools. The handlers already take a context
  object rather than Flask state, so it would be a thin wrapper.
