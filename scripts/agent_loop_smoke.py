"""
Exercise agent.chat() end to end without spending a token.

A stub Messages API runs on localhost and the real Anthropic SDK is pointed at
it with ANTHROPIC_BASE_URL. Everything else is real: the SDK builds and sends
the request, the loop executes the tool against Lakebase, and the answer's
citations are validated. The stub scripts two turns:

    turn 1  tool_use  retrieve_evidence over a real collection
    turn 2  end_turn  an answer citing one retrieved key and one invented key

and then asserts on what the SDK actually put on the wire.

    LAKEBASE_URL=... python scripts/agent_loop_smoke.py

Needs a user with at least one non-empty collection (test_deployment.py makes one).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

captured: list[dict] = []
script: list[dict] = []


class StubMessages(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = script[min(len(captured) - 1, len(script) - 1)]
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", f"req_stub_{len(captured)}")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def message(content, stop_reason):
    return {"id": f"msg_stub_{len(script)}", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 900,
                      "cache_creation_input_tokens": 0}}


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), StubMessages)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stub"

    import agent
    import lakebase

    row = lakebase.run_query("""
        SELECT c.user_id, c.id AS collection_id, MIN(cp.paper_id) AS paper_id
        FROM collections c JOIN collection_papers cp ON cp.collection_id = c.id
        GROUP BY 1, 2 ORDER BY 2 DESC LIMIT 1""")
    if not row:
        print("no non-empty collection - run test_deployment.py first")
        return 1
    user_id, collection_id = row[0]["user_id"], row[0]["collection_id"]

    script.append(message([
        {"type": "thinking", "thinking": "", "signature": "stub-signature"},
        {"type": "tool_use", "id": "toolu_stub_1", "name": "retrieve_evidence",
         "input": {"question": "how is retrieval augmented generation evaluated", "collection_id": collection_id}},
    ], "tool_use"))

    passed = failed = 0

    def check(ok, label, detail=""):
        nonlocal passed, failed
        passed, failed = passed + bool(ok), failed + (not ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))

    # Turn 2 has to cite a key the tool really returned, which is only known
    # after the tool runs - so the stub answer is built lazily from the evidence.
    import agent_tools
    real_execute = agent_tools.execute

    def spying_execute(name, arguments, ctx):
        output, is_error = real_execute(name, arguments, ctx)
        keys = [k for k, v in ctx.citable.items() if v["kind"] == "paper"]
        cited = keys[0] if keys else "W0"
        script.append(message([{"type": "text", "text":
            f"Evaluation is covered by one paper [{cited}] and, supposedly, another [W999999999]."}], "end_turn"))
        return output, is_error

    agent_tools.execute = spying_execute
    result = agent.chat(user_id, "How do these papers evaluate RAG?", goal_id=None,
                        history=[{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "earlier"},
                                 {"role": "assistant", "content": "earlier answer"}])

    first, second = captured[0], captured[1] if len(captured) > 1 else {"body": {}, "headers": {}}
    body = first["body"]
    headers = {k.lower(): v for k, v in first["headers"].items()}
    check(first["path"].startswith("/v1/messages"), "SDK called the Messages API", first["path"])
    check("server-side-fallback-2026-07-01" in headers.get("anthropic-beta", ""), "fallback beta header sent",
          headers.get("anthropic-beta"))
    check(body.get("fallbacks") == "default", "fallbacks: default in the body")
    check(body.get("model") == agent.MODEL and body.get("thinking") == {"type": "adaptive"},
          "model and adaptive thinking", f"{body.get('model')} {body.get('thinking')}")
    check(body.get("output_config") == {"effort": agent.EFFORT}, "effort set", str(body.get("output_config")))
    check(len(body.get("tools") or []) == len(agent_tools.TOOL_DEFINITIONS), "all tools sent",
          str(len(body.get("tools") or [])))
    check(body.get("system", [{}])[0].get("cache_control") == {"type": "ephemeral"}, "system prompt cached")
    check(body.get("cache_control") == {"type": "ephemeral"}, "top-level auto caching on")
    check([m["role"] for m in body["messages"]] == ["user", "assistant", "user"],
          "history trimmed to start with a user turn", str([m["role"] for m in body["messages"]]))
    check("<learner_context>" in body["messages"][-1]["content"], "learner context in the final user turn")
    check(json.dumps(first["body"]["tools"]) == json.dumps(second["body"].get("tools")),
          "tool list byte-identical across turns (cache-stable)")

    msgs = second["body"].get("messages") or []
    check(len(msgs) == 5 and msgs[3]["role"] == "assistant" and msgs[4]["role"] == "user",
          "turn 2 appends assistant + tool results", str([m["role"] for m in msgs]))
    if len(msgs) == 5:
        check(any(b.get("type") == "thinking" for b in msgs[3]["content"]), "thinking block echoed back unchanged")
        results = msgs[4]["content"]
        check(results[0].get("type") == "tool_result" and results[0].get("tool_use_id") == "toolu_stub_1",
              "tool_result matches the tool_use id")
        payload = json.loads(results[0]["content"])
        check(payload.get("status") == "success" and payload.get("passages", 0) > 0,
              "retrieve_evidence ran against Lakebase", f"{payload.get('passages')} passages, scope {payload.get('scope')}")

    check(result["stop_reason"] == "end_turn" and "Evaluation is covered" in result["answer"], "final answer returned")
    check(len(result["citations"]) == 1, "the retrieved key is a verified citation",
          str([c["key"] for c in result["citations"]]))
    check(result["unverified_citations"] == ["W999999999"], "the invented key is flagged unverified")
    check(result["tool_calls"] and result["tool_calls"][0]["tool"] == "retrieve_evidence", "tool trace recorded")
    check(result["usage"]["cache_read_input_tokens"] == 1800, "usage summed across turns", str(result["usage"]))

    server.shutdown()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
