"""
AI Research and Learning Copilot - REST API and console host.

    POST /goals                       create a learning objective
    GET  /papers/search?q=...         discover papers on OpenAlex (not saved)
    POST /papers/import               save papers: metadata, authors, full text, vectors
    POST /collections/<id>/papers     add saved papers to a collection
    POST /plans                       compute and save a sequenced reading plan
    PUT  /progress                    record reading progress
    GET  /plans/next?goal_id=         the next paper to read
    POST /retrieve                    evidence retrieval, exactly as the agent sees it
    POST /copilot/chat                the agent

load_dotenv() runs before the local imports on purpose: those modules read
os.environ at import time.
"""

import logging
import os
import re
import threading

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

load_dotenv()

import agent  # noqa: E402
import context_pipeline  # noqa: E402
import lakebase  # noqa: E402
import library  # noqa: E402
import retrieval  # noqa: E402
import store  # noqa: E402
from openalex_client import OpenAlexClient, OpenAlexError  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("copilot-app")

app = Flask(__name__)

ROOT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(ROOT, "static")
SCHEMA_FILE = os.path.join(ROOT, "sql", "01_schema.sql")

DEFAULT_USER = os.environ.get("COPILOT_DEFAULT_USER", "local-student@example.edu")
MAX_QUERY_CHARS = 300
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadRequest(ValueError):
    pass


# ---------------------------------------------------------------------------
# schema self-heal (once per process, as in the weather app)
# ---------------------------------------------------------------------------

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_schema(force: bool = False) -> None:
    """Apply sql/01_schema.sql once per process so a fresh deploy self-heals.

    The pgvector table (sql/02) is deliberately not applied here: CREATE
    EXTENSION and the HNSW build need a superuser, which the app role is not.
    Statements run one at a time because pg8000 rejects multi-statement
    strings; comments are stripped first since one contains a semicolon.
    """
    global _schema_ready
    if _schema_ready and not force:
        return
    with _schema_lock:
        if _schema_ready and not force:
            return
        with open(SCHEMA_FILE) as handle:
            sql = "\n".join(line.split("--", 1)[0] for line in handle)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        with lakebase.get_connection() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    if statement.upper().startswith("SELECT"):
                        continue
                    cur.execute(statement)
            conn.commit()
        _schema_ready = True
        logger.info("schema verified (%d statements)", len(statements))


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------

_user_ids: dict[str, int] = {}


def current_user_id() -> int:
    """The signed-in user, created on first sight.

    Databricks Apps puts the authenticated user's email in X-Forwarded-Email.
    Locally there is no proxy, so everything is attributed to DEFAULT_USER. The
    id is cached per process: it cannot change for an email once created.
    """
    if "user_id" in g:
        return g.user_id
    email = (request.headers.get("X-Forwarded-Email")
             or request.headers.get("X-Forwarded-User") or DEFAULT_USER).strip().lower()
    if email not in _user_ids:
        ensure_schema()
        _user_ids[email] = store.ensure_user(email)["id"]
    g.user_id = _user_ids[email]
    g.email = email
    return g.user_id


def body() -> dict:
    data = request.get_json(silent=True) if request.data else {}
    if data is None or not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object.")
    return data


def opt_int(value, name: str, low: int | None = None, high: int | None = None):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest(f'"{name}" must be an integer.')
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f'"{name}" must be an integer.') from None
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def req_str(data: dict, name: str, max_len: int = 2000) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f'Missing required field "{name}" (a non-empty string).')
    if len(value) > max_len:
        raise BadRequest(f'"{name}" is {len(value)} characters; the maximum is {max_len}.')
    return value.strip()


def id_list(data: dict, name: str = "paper_ids", max_items: int = 25) -> list[str]:
    values = data.get(name)
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise BadRequest(f'"{name}" must be a non-empty list of OpenAlex ids like "W4389984066".')
    if len(values) > max_items:
        raise BadRequest(f'"{name}" has {len(values)} items; the maximum is {max_items}.')
    return [v.strip().rsplit("/", 1)[-1].upper() for v in values]


@app.errorhandler(Exception)
def handle_exception(err):
    """JSON for every error. Internal exception text never reaches the client:
    a psycopg2 connection error carries the whole DSN, password included."""
    if isinstance(err, BadRequest):
        return jsonify({"error": str(err)}), 400
    if isinstance(err, store.NotFound):
        return jsonify({"error": str(err)}), 404
    if isinstance(err, OpenAlexError):
        return jsonify({"error": str(err)}), 502
    if isinstance(err, agent.CopilotUnavailable):
        return jsonify({"error": str(err)}), 503
    if isinstance(err, HTTPException):
        return jsonify({"error": err.description}), err.code
    logger.exception("Unhandled exception on %s", request.path)
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# console + health
# ---------------------------------------------------------------------------


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def index():
    if os.path.isfile(os.path.join(UI_DIR, "index.html")):
        return send_from_directory(UI_DIR, "index.html")
    return api_index()


@app.route("/api")
def api_index():
    rules = sorted(
        f"{','.join(sorted(r.methods - {'HEAD', 'OPTIONS'}))} {r.rule}"
        for r in app.url_map.iter_rules() if not r.rule.startswith("/static")
    )
    return jsonify({
        "service": "research-learning-copilot",
        "endpoints": rules,
        "ui": "built" if os.path.isfile(os.path.join(UI_DIR, "index.html")) else "not built",
    })


@app.route("/me")
def me():
    user_id = current_user_id()
    return jsonify({"user_id": user_id, "email": g.email, "stats": store.user_stats(user_id)})


@app.route("/copilot/stats")
def copilot_stats():
    user_id = current_user_id()
    payload = {"user": store.user_stats(user_id), "agent": agent.status()}
    if context_pipeline.chunks_table_exists():
        payload["context"] = context_pipeline.backlog()
    else:
        payload["context"] = {"error": "context_chunks does not exist - run sql/02_context_chunks.sql."}
    payload["openalex"] = {"api_key_configured": bool(os.environ.get("OPENALEX_API_KEY"))}
    return jsonify(payload)


# ---------------------------------------------------------------------------
# learning goals
# ---------------------------------------------------------------------------


@app.route("/goals", methods=["GET"])
def goals_list():
    status = request.args.get("status") or None
    if status and status not in store.GOAL_STATUSES:
        raise BadRequest(f"status must be one of {list(store.GOAL_STATUSES)}.")
    return jsonify({"goals": store.list_goals(current_user_id(), status)})


@app.route("/goals", methods=["POST"])
def goals_create():
    data = body()
    title = req_str(data, "title", 200)
    level = data.get("level") or "intermediate"
    if level not in store.GOAL_LEVELS:
        raise BadRequest(f"level must be one of {list(store.GOAL_LEVELS)}.")
    target_date = data.get("target_date") or None
    if target_date and not (isinstance(target_date, str) and _DATE.match(target_date)):
        raise BadRequest('target_date must be "YYYY-MM-DD".')
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        raise BadRequest('"description" must be a string.')

    user_id = current_user_id()
    goal = store.create_goal(user_id, title, (description or "").strip() or None, level, target_date)
    # Embed the goal now so it can rank papers immediately. Failure is not
    # fatal: the goal is saved, and the embed job will catch it up.
    embedded = _try_embed(lambda: context_pipeline.embed_goal(goal["id"]))
    return jsonify({"goal": goal, "embedded": embedded}), 201


@app.route("/goals/<int:goal_id>", methods=["PATCH"])
def goals_update(goal_id: int):
    data = body()
    if "level" in data and data["level"] not in store.GOAL_LEVELS:
        raise BadRequest(f"level must be one of {list(store.GOAL_LEVELS)}.")
    if "status" in data and data["status"] not in store.GOAL_STATUSES:
        raise BadRequest(f"status must be one of {list(store.GOAL_STATUSES)}.")
    user_id = current_user_id()
    goal = store.update_goal(user_id, goal_id, data)
    embedded = None
    if "title" in data or "description" in data:
        embedded = _try_embed(lambda: context_pipeline.embed_goal(goal_id))
    return jsonify({"goal": goal, "embedded": embedded})


# ---------------------------------------------------------------------------
# papers
# ---------------------------------------------------------------------------


@app.route("/papers/search")
def papers_search():
    query = (request.args.get("q") or request.args.get("query") or "").strip()
    goal_id = opt_int(request.args.get("goal_id"), "goal_id")
    if not query and goal_id:
        goal = store.get_goal(current_user_id(), goal_id)
        query = library.query_from_goal(goal["title"])
    if not query:
        raise BadRequest("Pass q (search text) or goal_id.")
    if len(query) > MAX_QUERY_CHARS:
        raise BadRequest(f"q is {len(query)} characters; the maximum is {MAX_QUERY_CHARS}.")
    sort = request.args.get("sort") or "relevance"
    client = OpenAlexClient()
    results = library.discover(
        client, query,
        limit=opt_int(request.args.get("limit"), "limit", 1, 50) or 12,
        from_year=opt_int(request.args.get("from_year"), "from_year"),
        open_access_only=(request.args.get("open_access") or "").lower() in ("1", "true", "yes"),
        sort=sort,
    )
    return jsonify({"query": query, "count": len(results), "results": results,
                    "openalex_usage": client.last_usage})


@app.route("/papers/import", methods=["POST"])
def papers_import():
    data = body()
    paper_ids = id_list(data, max_items=library.MAX_IMPORT)
    current_user_id()
    fetch_content = data.get("fetch_content")
    if fetch_content is not None and not isinstance(fetch_content, bool):
        raise BadRequest('"fetch_content" must be true or false.')
    embed = context_pipeline.chunks_table_exists()
    result = library.import_papers(OpenAlexClient(), paper_ids, fetch_content=fetch_content, embed=embed)
    if not embed:
        result["note"] = "context_chunks does not exist yet, so nothing was embedded. Run sql/02."
    status = 200 if result["imported"] else 502
    return jsonify(result), status


@app.route("/papers/<paper_id>")
def papers_get(paper_id: str):
    return jsonify(store.get_paper(paper_id.upper(), user_id=current_user_id()))


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


@app.route("/collections", methods=["GET"])
def collections_list():
    goal_id = opt_int(request.args.get("goal_id"), "goal_id")
    return jsonify({"collections": store.list_collections(current_user_id(), goal_id)})


@app.route("/collections", methods=["POST"])
def collections_create():
    data = body()
    collection = store.create_collection(
        current_user_id(), req_str(data, "name", 120),
        description=data.get("description") if isinstance(data.get("description"), str) else None,
        goal_id=opt_int(data.get("goal_id"), "goal_id"),
    )
    return jsonify({"collection": collection}), 201


@app.route("/collections/<int:collection_id>")
def collections_get(collection_id: int):
    return jsonify(store.get_collection(current_user_id(), collection_id))


@app.route("/collections/<int:collection_id>/papers", methods=["POST"])
def collections_add(collection_id: int):
    data = body()
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    result = store.add_papers_to_collection(current_user_id(), collection_id, id_list(data),
                                            added_by="user", reason=reason)
    return jsonify(result)


@app.route("/collections/<int:collection_id>/papers/<paper_id>", methods=["DELETE"])
def collections_remove(collection_id: int, paper_id: str):
    removed = store.remove_paper_from_collection(current_user_id(), collection_id, paper_id.upper())
    return jsonify({"removed": removed})


# ---------------------------------------------------------------------------
# plans + progress
# ---------------------------------------------------------------------------


@app.route("/plans", methods=["POST"])
def plans_build():
    data = body()
    goal_id = opt_int(data.get("goal_id"), "goal_id")
    collection_id = opt_int(data.get("collection_id"), "collection_id")
    if goal_id is None and collection_id is None:
        raise BadRequest("Pass goal_id or collection_id.")
    plan = library.build_plan(current_user_id(), goal_id=goal_id, collection_id=collection_id,
                              max_papers=opt_int(data.get("max_papers"), "max_papers", 1, 25) or 15)
    return jsonify(plan)


@app.route("/plans", methods=["GET"])
def plans_get():
    goal_id = opt_int(request.args.get("goal_id"), "goal_id")
    user_id = current_user_id()
    if goal_id is not None:
        store.get_goal(user_id, goal_id)
    plan = store.get_plan(user_id, goal_id)
    in_plan = {row["paper_id"] for row in plan}
    for row in plan:
        row["prerequisites"] = [r for r in row.pop("referenced_works") or [] if r in in_plan]
    return jsonify({"goal_id": goal_id, "items": plan})


@app.route("/plans/next")
def plans_next():
    goal_id = opt_int(request.args.get("goal_id"), "goal_id")
    user_id = current_user_id()
    if goal_id is not None:
        store.get_goal(user_id, goal_id)
    return jsonify(library.next_paper(user_id, goal_id))


@app.route("/progress", methods=["PUT"])
def progress_update():
    data = body()
    status = data.get("status")
    if status not in store.PROGRESS_STATUSES:
        raise BadRequest(f"status must be one of {list(store.PROGRESS_STATUSES)}.")
    paper_id = req_str(data, "paper_id", 40).upper()
    row = store.update_progress(current_user_id(), paper_id, status,
                                opt_int(data.get("goal_id"), "goal_id"))
    return jsonify({"progress": row})


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


@app.route("/notes", methods=["GET"])
def notes_list():
    paper_id = (request.args.get("paper_id") or "").upper() or None
    return jsonify({"notes": store.list_notes(current_user_id(), paper_id,
                                              opt_int(request.args.get("goal_id"), "goal_id"))})


@app.route("/notes", methods=["POST"])
def notes_create():
    data = body()
    paper_id = data.get("paper_id")
    if paper_id is not None and not isinstance(paper_id, str):
        raise BadRequest('"paper_id" must be a string.')
    note = store.create_note(current_user_id(), req_str(data, "body", 5000),
                             paper_id=paper_id.upper() if paper_id else None,
                             goal_id=opt_int(data.get("goal_id"), "goal_id"))
    embedded = _try_embed(lambda: context_pipeline.embed_note(note["id"]))
    return jsonify({"note": note, "embedded": embedded}), 201


@app.route("/notes/<int:note_id>", methods=["DELETE"])
def notes_delete(note_id: int):
    return jsonify({"deleted": store.delete_note(current_user_id(), note_id)})


# ---------------------------------------------------------------------------
# context engineering
# ---------------------------------------------------------------------------


@app.route("/retrieve", methods=["POST"])
def retrieve():
    """The retrieval step on its own - what the agent's retrieve_evidence sees."""
    data = body()
    query = req_str(data, "query", MAX_QUERY_CHARS)
    user_id = current_user_id()
    _require_chunks()
    if data.get("paper_ids"):
        scope = id_list(data)
    elif data.get("collection_id") is not None:
        scope = store.collection_paper_ids(user_id, opt_int(data["collection_id"], "collection_id"))
    elif data.get("goal_id") is not None:
        scope = store.goal_paper_ids(user_id, opt_int(data["goal_id"], "goal_id"))
    else:
        scope = None
    evidence = retrieval.retrieve_evidence(
        query, user_id, paper_ids=scope,
        top_k=opt_int(data.get("top_k"), "top_k", 1, retrieval.MAX_TOP_K) or retrieval.DEFAULT_TOP_K,
        per_paper=opt_int(data.get("per_paper"), "per_paper", 1, 5) or retrieval.DEFAULT_PER_PAPER,
    )
    return jsonify({"query": query, "scope_papers": None if scope is None else len(scope),
                    "count": len(evidence), "evidence": evidence})


@app.route("/copilot/embed", methods=["POST"])
def copilot_embed():
    _require_chunks()
    limit = opt_int(body().get("limit"), "limit", 1, 5000)
    result = context_pipeline.embed_pending(limit=limit)
    return jsonify({**result, "backlog": context_pipeline.backlog()})


@app.route("/copilot/chat", methods=["POST"])
def copilot_chat():
    data = body()
    message = req_str(data, "message", agent.MAX_MESSAGE_CHARS)
    history = data.get("history") or []
    if not isinstance(history, list):
        raise BadRequest('"history" must be a list of {"role", "content"} turns.')
    user_id = current_user_id()
    _require_chunks()
    return jsonify(agent.chat(user_id, message, history=history,
                              goal_id=opt_int(data.get("goal_id"), "goal_id")))


def _require_chunks():
    if not context_pipeline.chunks_table_exists():
        raise agent.CopilotUnavailable(
            "The context_chunks table does not exist. Run sql/02_context_chunks.sql in the "
            "Lakebase SQL editor as a superuser."
        )


def _try_embed(fn):
    try:
        if not context_pipeline.chunks_table_exists():
            return {"deferred": "context_chunks does not exist yet"}
        return fn()
    except Exception as err:  # noqa: BLE001
        logger.exception("inline embed failed")
        return {"deferred": type(err).__name__}


def _warm_up():
    """Schema check and model load off the request path, as in the weather app."""
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and app.debug:
        return

    def run():
        try:
            ensure_schema()
        except Exception:  # noqa: BLE001
            logger.exception("schema warm-up failed; will retry on first request")
        try:
            from embeddings import embed_query
            embed_query("warm up")
        except Exception:  # noqa: BLE001
            logger.exception("embedding model warm-up failed")

    threading.Thread(target=run, name="warm-up", daemon=True).start()


_warm_up()


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("DATABRICKS_APP_PORT") or os.getenv("FLASK_RUN_PORT") or 8000)
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    logger.info("Starting Flask on http://%s:%s (debug=%s)", host, port, debug)
    app.run(debug=debug, host=host, port=port)
