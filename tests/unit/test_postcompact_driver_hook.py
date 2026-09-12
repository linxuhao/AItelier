import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".codex" / "hooks" / "postcompact-driver-state.sh"


class StateStub(BaseHTTPRequestHandler):
    revision = 1
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        StateStub.requests.append(body)
        params = body["params"]
        name, args = params["name"], params["arguments"]
        if name == "state_graph_help":
            result = {
                "driver_resource": "aitelier://state/driver-guide",
                "driver_guide": """# State DAG director protocol
State owns goals and evidence.

## Resume safely
Read current state first.

## Dispatch through either executor
Register attempts before dispatch.

## Wait instead of repeatedly querying
Use one bounded wait and retain the cursor.

## Director notebook: context, not a second State database
Select the note by project_id.

## Unselected huge section
DO_NOT_INCLUDE_UNSELECTED_GUIDE_SECTION
""",
            }
        elif args["action"] == "get_driver_note":
            assert args["arguments"] == {"project_id": "aitelier"}
            result = {
                "project_id": "aitelier", "revision": StateStub.revision,
                "permanent": f"permanent revision {StateStub.revision}",
                "temporary": "fresh temporary; Authorization: Bearer forbidden-secret",
                "updated_at": "2026-09-12T00:00:00Z",
            }
        else:
            assert args == {"action": "project_overview", "arguments": {"project_id": "aitelier"}}
            result = {
                "event_seq": 57,
                "nodes": [
                    {"node_key": "ready", "status": "READY"},
                    {"node_key": "busy", "status": "IN_PROGRESS", "latest_attempt": {"attempt_id": "attempt-1", "status": "running"}},
                ],
            }
        text = json.dumps({"result": result})
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def state_server():
    StateStub.revision = 1
    StateStub.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), StateStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def invoke(tmp_path, url, cwd=None, event="SessionStart"):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    helper = tmp_path / "headers"
    helper.write_text("#!/bin/sh\nprintf '%s' '{\"X-AItelier-Admin-Token\":\"helper-secret\"}'\n")
    helper.chmod(0o700)
    (codex_home / "config.toml").write_text(
        f'[mcp_servers.aitelier]\nurl = "{url}"\nhttp_headers_helper = "{helper}"\n'
    )
    result = subprocess.run(
        [str(HOOK)], input=json.dumps({"hook_event_name": event, "source": "compact"}),
        text=True, capture_output=True, cwd=cwd or tmp_path,
        env={**os.environ, "CODEX_HOME": str(codex_home)}, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_hook_emits_legal_bounded_fresh_project_context_from_any_cwd(tmp_path, state_server):
    output = invoke(tmp_path, state_server, cwd=tmp_path)
    assert set(output) == {"hookSpecificOutput"}
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    context = specific["additionalContext"]
    assert len(context) <= 12_000
    assert "project_id=aitelier (fixed project isolation)" in context
    assert "driver_note_revision=1" in context
    assert "state_event_cursor=57" in context
    assert "ready: node=READY" in context and "attempt=running id=attempt-1" in context
    assert "DO_NOT_INCLUDE_UNSELECTED_GUIDE_SECTION" not in context
    assert "helper-secret" not in context and "forbidden-secret" not in context
    assert "[REDACTED]" in context
    assert len(StateStub.requests) == 3
    wire = json.dumps(StateStub.requests)
    assert "aitelier" in wire and "wuxia-myth" not in wire and "DRIVER_STATE.md" not in wire


def test_next_hook_invocation_reads_new_note_revision_without_cache(tmp_path, state_server):
    first = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    StateStub.revision = 2
    second = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=1" in first
    assert "driver_note_revision=2" in second
    assert "permanent revision 2" in second
    assert first != second
    assert len(StateStub.requests) == 6


def test_unreachable_state_still_emits_bounded_recovery_context(tmp_path):
    output = invoke(tmp_path, "http://127.0.0.1:1/mcp")
    context = output["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= 12_000
    assert "recovery required" in context
    assert 'get_driver_note' in context and '"project_id":"aitelier"' in context
    assert "Do not use ~/.AItelier/DRIVER_STATE.md" in context
    assert "secret" not in context.lower()


def test_postcompact_lifecycle_event_is_a_legal_noop_before_compact_session_start(tmp_path, state_server):
    assert invoke(tmp_path, state_server, event="PostCompact") == {"continue": True}
    assert StateStub.requests == []


def test_tracked_hook_config_uses_current_command_shape_and_move_safe_lookup():
    config = json.loads((REPO / ".codex" / "hooks.json").read_text())
    postcompact = config["hooks"]["PostCompact"][0]["hooks"][0]
    handler = config["hooks"]["SessionStart"][0]["hooks"][0]
    assert config["hooks"]["SessionStart"][0]["matcher"] == "^compact$"
    assert postcompact["type"] == handler["type"] == "command"
    assert postcompact["async"] is handler["async"] is False
    assert postcompact["timeout"] == handler["timeout"] == 20
    assert "additionalContextLimit" not in postcompact
    assert handler["additionalContextLimit"] == 4000
    assert "git rev-parse --show-toplevel" in handler["command"]
    assert "/Users/" not in handler["command"] and "/home/" not in handler["command"]
    assert HOOK.stat().st_mode & 0o111
