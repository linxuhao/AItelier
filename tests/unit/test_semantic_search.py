"""semantic_search: one MCP call to the zvec-grep sidecar, guarded, honest on failure."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import aitelier.tools.semantic_search.impl as impl
from aitelier.tools.semantic_search.impl import semantic_search


class _FakeZg(BaseHTTPRequestHandler):
    calls: list = []
    sse = False

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).calls.append((body.get("method"), self.headers.get("Mcp-Session-Id"), body))
        if body.get("method") == "initialize":
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-1"); self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18"}}).encode())
            return
        if body.get("method") == "notifications/initialized":
            self.send_response(202); self.end_headers(); return
        if self.headers.get("Mcp-Session-Id") != "sess-1":
            self.send_response(404); self.end_headers(); return
        args = body["params"]["arguments"]
        text = f"freshness: fresh\n{args['root']} q={args['query']} limit={args['limit']}\nscripts/a.gd:10-12\n10  var x"
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}]}}
        if type(self).sse:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(("event: message\ndata: " + json.dumps(msg) + "\n\n").encode())
        else:
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(msg).encode())

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def zg(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _FakeZg)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    monkeypatch.setenv("ZVEC_GREP_SERVER_URL", f"http://127.0.0.1:{srv.server_port}/mcp")
    monkeypatch.setattr(impl, "_session_id", None)
    _FakeZg.calls = []; _FakeZg.sse = False
    yield srv
    srv.shutdown()


def test_initializes_once_then_calls_with_the_session(zg, tmp_path):
    r = semantic_search("where is damage applied", project_root=str(tmp_path))
    assert "error" not in r and "scripts/a.gd:10-12" in r["content"]
    methods = [m for m, _, _ in _FakeZg.calls]
    assert methods[:2] == ["initialize", "notifications/initialized"] and methods[-1] == "tools/call"
    assert _FakeZg.calls[-1][1] == "sess-1"
    assert _FakeZg.calls[-1][2]["params"]["name"] == "zvec_grep_search"
    assert _FakeZg.calls[-1][2]["params"]["arguments"]["root"] == str(tmp_path.resolve())
    semantic_search("second", project_root=str(tmp_path))
    assert [m for m, _, _ in _FakeZg.calls].count("initialize") == 1


def test_sse_responses_are_parsed(zg, tmp_path):
    _FakeZg.sse = True
    r = semantic_search("x", project_root=str(tmp_path))
    assert "scripts/a.gd" in r["content"]


def test_limit_is_clamped_and_globs_fts_forwarded(zg, tmp_path):
    r = semantic_search("x", limit=500, globs=["scripts/**"], fts=["apply_damage"], project_root=str(tmp_path))
    args = _FakeZg.calls[-1][2]["params"]["arguments"]
    assert r["limit"] == 50 and args["limit"] == 50
    assert args["globs"] == ["scripts/**"] and args["fts"] == ["apply_damage"]


def test_missing_root_is_an_error_not_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = semantic_search("x", project_root="")
    assert "error" in r and "hint" in r
    assert "error" in semantic_search("x", project_root="rel/path")


def test_sidecar_down_is_an_honest_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ZVEC_GREP_SERVER_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setattr(impl, "_session_id", None)
    r = semantic_search("x", project_root=str(tmp_path))
    assert "unavailable" in r["error"] and "search" in r["hint"]
