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
    permanent = None
    temporary = None
    guide = None
    nodes = None

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
                "driver_guide": StateStub.guide or """# State DAG director protocol
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
                "permanent": StateStub.permanent or f"permanent revision {StateStub.revision}",
                "temporary": StateStub.temporary or "fresh temporary; Authorization: Bearer forbidden-secret",
                "updated_at": "2026-09-12T00:00:00Z",
            }
        else:
            assert args == {"action": "project_overview", "arguments": {"project_id": "aitelier"}}
            result = {
                "event_seq": 57,
                "nodes": StateStub.nodes or [
                    {"node_key": "ready", "status": "OPEN", "readiness": "ready", "next_action": "new_attempt"},
                    {"node_key": "busy", "status": "OPEN", "readiness": "in_progress", "next_action": None,
                     "latest_attempt": {"attempt_id": "attempt-1", "status": "running"}},
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
    StateStub.permanent = StateStub.temporary = StateStub.guide = StateStub.nodes = None
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
    assert "ready: node=OPEN readiness=ready next_action=new_attempt" in context
    assert "busy" not in context
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


def test_credentials_are_redacted_and_frontier_excludes_closed_or_busy_history(tmp_path, state_server):
    synthetic_values = [
        "synthetic-password", "synthetic-passphrase", "synthetic-private", "synthetic-credential",
        "synthetic-basic", "synthetic-url-password", "SYNTHETICPEMBODY",
    ]
    StateStub.permanent = (
        'password="synthetic-password" passwd=synthetic-password pwd: synthetic-password\n'
        "passphrase='synthetic-passphrase' private_key=synthetic-private credential: synthetic-credential\n"
        "Authorization: Basic synthetic-basic https://director:synthetic-url-password@example.invalid\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETICPEMBODY\n-----END OPENSSH PRIVATE KEY-----"
    )
    StateStub.guide = """# State DAG director protocol
client_secret=synthetic-credential
## Resume safely
password: synthetic-password
## Dispatch through either executor
private-key=synthetic-private
## Wait instead of repeatedly querying
credential=synthetic-credential
## Director notebook: context, not a second State database
passphrase=synthetic-passphrase
"""
    StateStub.nodes = [
        {"node_key": "open-ready", "status": "OPEN", "readiness": "ready", "next_action": "new_attempt"},
        {"node_key": "candidate-ready", "status": "CANDIDATE", "readiness": "ready", "next_action": "candidate_review"},
        {"node_key": "verified-history", "status": "VERIFIED", "readiness": "closed", "next_action": None,
         "latest_attempt": {"attempt_id": "old", "status": "candidate"}},
        {"node_key": "open-busy", "status": "OPEN", "readiness": "in_progress", "next_action": None,
         "latest_attempt": {"attempt_id": "live", "status": "running"}},
        {"node_key": "open-blocked", "status": "OPEN", "readiness": "blocked", "next_action": None},
    ]
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    for value in synthetic_values:
        assert value not in context
    assert "[REDACTED" in context
    assert "open-ready: node=OPEN readiness=ready next_action=new_attempt" in context
    assert "candidate-ready: node=CANDIDATE readiness=ready next_action=candidate_review" in context
    assert "verified-history" not in context and "open-busy" not in context and "open-blocked" not in context


def test_bounded_multilingual_context_is_complete_with_spilling_disabled(tmp_path, state_server):
    StateStub.permanent = "永久导演笔记 START — current ownership — 结束 END"
    StateStub.temporary = "临时状态 START — next action / 下一步 — 尾部 END"
    StateStub.guide = """# State DAG director protocol
协议开头 GUIDE-START
## Resume safely
恢复当前状态 RESUME-COMPLETE
## Dispatch through either executor
先注册 attempt DISPATCH-COMPLETE
## Wait instead of repeatedly querying
保留 cursor WAIT-COMPLETE
## Director notebook: context, not a second State database
项目隔离 GUIDE-END
"""
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    for marker in ("永久导演笔记 START", "结束 END", "临时状态 START", "尾部 END",
                   "GUIDE-START", "RESUME-COMPLETE", "DISPATCH-COMPLETE", "WAIT-COMPLETE", "GUIDE-END"):
        assert marker in context
    assert len(context) <= 12_000


def test_tracked_hook_config_uses_current_command_shape_and_move_safe_lookup():
    config = json.loads((REPO / ".codex" / "hooks.json").read_text())
    postcompact = config["hooks"]["PostCompact"][0]["hooks"][0]
    handler = config["hooks"]["SessionStart"][0]["hooks"][0]
    assert config["hooks"]["SessionStart"][0]["matcher"] == "^compact$"
    assert postcompact["type"] == handler["type"] == "command"
    assert postcompact["async"] is handler["async"] is False
    assert postcompact["timeout"] == handler["timeout"] == 20
    assert "additionalContextLimit" not in postcompact
    # Zero disables Codex spilling. The script's own MAX_CONTEXT_CHARS remains
    # the strict safety boundary for complete mixed-language delivery.
    assert handler["additionalContextLimit"] == 0
    assert "git rev-parse --show-toplevel" in handler["command"]
    assert "/Users/" not in handler["command"] and "/home/" not in handler["command"]
    assert HOOK.stat().st_mode & 0o111
