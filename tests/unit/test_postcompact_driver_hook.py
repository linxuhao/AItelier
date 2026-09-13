import importlib.util
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".codex" / "hooks" / "postcompact-driver-state.sh"
HOOK_PY = REPO / ".codex" / "hooks" / "postcompact_driver_state.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("postcompact_driver_state_candidate", HOOK_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def invoke(tmp_path, url, cwd=None, event="SessionStart", session_id="session-a"):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    helper = tmp_path / "headers"
    if not helper.exists():
        helper.write_text("#!/bin/sh\nprintf '%s' '{\"X-AItelier-Admin-Token\":\"helper-secret\"}'\n")
        helper.chmod(0o700)
    config = codex_home / "config.toml"
    if not config.exists():
        config.write_text(
            f'[mcp_servers.aitelier]\nurl = "{url}"\nhttp_headers_helper = "{helper}"\n'
        )
    hook_input = {
        "session_id": session_id,
        "transcript_path": str(tmp_path / "rollout.jsonl"),
        "cwd": str(cwd or tmp_path),
        "hook_event_name": event,
        "model": "gpt-test",
    }
    if event == "SessionStart":
        hook_input.update({"source": "compact", "permission_mode": "never"})
    elif event == "PostCompact":
        hook_input.update({"turn_id": "turn-compact", "trigger": "auto"})
    elif event == "UserPromptSubmit":
        hook_input.update({"turn_id": "turn-next", "permission_mode": "never", "prompt": "continue"})
    result = subprocess.run(
        [str(HOOK)], input=json.dumps(hook_input),
        text=True, capture_output=True, cwd=cwd or tmp_path,
        env={**os.environ, "CODEX_HOME": str(codex_home)}, timeout=10, check=False,
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
    assert "busy: attempt_id=attempt-1 run_id=none attempt_status=running" in context
    assert "DO_NOT_INCLUDE_UNSELECTED_GUIDE_SECTION" not in context
    assert "helper-secret" not in context and "forbidden-secret" not in context
    assert "[REDACTED]" in context
    assert len(StateStub.requests) == 3
    wire = json.dumps(StateStub.requests)
    assert "aitelier" in wire and "wuxia-myth" not in wire and "DRIVER_STATE.md" not in wire


def test_next_hook_invocation_reads_new_note_revision_without_cache(tmp_path, state_server):
    first = invoke(tmp_path, state_server, session_id="session-a")["hookSpecificOutput"]["additionalContext"]
    StateStub.revision = 2
    second = invoke(tmp_path, state_server, session_id="session-b")["hookSpecificOutput"]["additionalContext"]
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


def test_postcompact_marker_failure_never_blocks_and_is_actionable(tmp_path, state_server):
    blocked_home = tmp_path / "not-a-directory"
    blocked_home.write_text("file blocks marker directory")
    hook_input = {
        "session_id": "session-marker-failure",
        "turn_id": "turn-compact",
        "transcript_path": str(tmp_path / "rollout.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": "PostCompact",
        "model": "gpt-test",
        "trigger": "auto",
    }
    result = subprocess.run(
        [str(HOOK)], input=json.dumps(hook_input), text=True, capture_output=True,
        cwd=tmp_path, env={
            **os.environ,
            "CODEX_HOME": str(blocked_home),
            "AITELIER_MCP_URL": state_server,
        }, timeout=10, check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    output = json.loads(result.stdout)
    assert output["continue"] is True
    assert "driver_note_revision=1" in output["systemMessage"]
    assert "follow-up could not be queued" in output["systemMessage"]
    assert len(output["systemMessage"]) <= 12_000


def test_damaged_marker_fails_closed_for_repeated_model_context_events(tmp_path, monkeypatch):
    hook = load_hook_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    hook_input = {"session_id": "damaged-session", "turn_id": "turn-damaged"}
    marker = hook._pending_path(hook_input)
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True)
    marker.mkdir(mode=0o700)
    for event, standalone in (("SessionStart", True), ("UserPromptSubmit", False)):
        outputs = []
        hook._write_output = outputs.append
        for _ in range(2):
            assert hook._deliver_once(hook_input, event, standalone=standalone) == hook.DELIVERY_FAILED
        assert outputs == []


def test_malformed_regular_marker_is_preserved_and_reports_recovery(
    tmp_path, state_server, monkeypatch
):
    hook = load_hook_module()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    marker = hook._pending_path({"session_id": "malformed-session"})
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True)
    raw = "{malformed marker}"
    marker.write_text(raw)
    postcompact = invoke(tmp_path, state_server, event="PostCompact", session_id="malformed-session")
    assert "follow-up could not be queued" in postcompact["systemMessage"]
    assert marker.read_text() == raw
    for event in ("SessionStart", "UserPromptSubmit"):
        output = invoke(tmp_path, state_server, event=event, session_id="malformed-session")
        assert set(output) == {"continue", "systemMessage"}
        assert "handoff recovery required" in output["systemMessage"]
        assert "malformed" in output["systemMessage"]
        assert marker.read_text() == raw


@pytest.mark.parametrize(
    "marker_fields",
    [
        pytest.param({"version": 2, "status": "pending"}, id="v2-pending"),
        pytest.param({"version": 3, "status": "pending"}, id="v3-pending"),
        pytest.param(
            {"version": 3, "status": "delivery_attempted", "acknowledgement": "none"},
            id="v3-delivery-attempted",
        ),
    ],
)
@pytest.mark.parametrize(
    "generation",
    [
        pytest.param("", id="empty"),
        pytest.param(" \t\n", id="whitespace"),
        pytest.param(7, id="wrong-type"),
    ],
)
def test_malformed_generation_marker_is_preserved_across_context_events(
    tmp_path, state_server, monkeypatch, marker_fields, generation
):
    hook = load_hook_module()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    marker = hook._pending_path({"session_id": "invalid-generation-session"})
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True)
    raw = json.dumps({**marker_fields, "generation": generation}, separators=(",", ":"))
    marker.write_text(raw)

    postcompact = invoke(
        tmp_path, state_server, event="PostCompact", session_id="invalid-generation-session"
    )
    assert "follow-up could not be queued" in postcompact["systemMessage"]
    assert marker.read_text() == raw
    for event in ("SessionStart", "UserPromptSubmit"):
        output = invoke(
            tmp_path, state_server, event=event, session_id="invalid-generation-session"
        )
        assert set(output) == {"continue", "systemMessage"}
        assert "handoff recovery required" in output["systemMessage"]
        assert marker.read_text() == raw


@pytest.mark.parametrize("generation", ["", " \t\n", None, False, 0, 1.5, [], {}])
def test_invalid_generation_never_matches_legal_writer_invariant(
    tmp_path, monkeypatch, generation
):
    hook = load_hook_module()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    hook_input = {
        "session_id": f"writer-generation-{type(generation).__name__}",
        "turn_id": generation,
    }
    assert hook._mark_pending(hook_input) is False
    marker = hook._pending_path(hook_input)
    assert marker is not None and not marker.exists()


def test_forced_delivery_persistence_failure_never_repeats_context(tmp_path, monkeypatch):
    hook = load_hook_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    hook_input = {"session_id": "write-failure-session", "turn_id": "turn-write-failure"}
    marker = hook._pending_path(hook_input)
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True)
    marker.write_text(json.dumps({"version": 2, "generation": "turn-write-failure", "status": "pending"}))
    monkeypatch.setattr(hook, "build_context", lambda: "synthetic context")
    monkeypatch.setattr(hook, "_write_marker", lambda *_args: False)
    outputs = []
    monkeypatch.setattr(hook, "_write_output", outputs.append)
    for event, standalone in (("SessionStart", True), ("UserPromptSubmit", False)):
        outputs.clear()
        for _ in range(2):
            assert hook._deliver_once(hook_input, event, standalone=standalone) == hook.DELIVERY_FAILED
        assert outputs == []
    assert json.loads(marker.read_text())["status"] == "pending"


def test_delivery_lock_failure_is_fail_closed_for_both_context_events(monkeypatch):
    hook = load_hook_module()
    monkeypatch.setattr(hook, "_acquire_marker", lambda _hook_input: (None, None))
    for event, standalone, expected in (
        ("SessionStart", True, hook.DELIVERY_FAILED),
        ("UserPromptSubmit", False, hook.DELIVERY_NOOP),
    ):
        assert hook._deliver_once({"session_id": "lock-failure"}, event, standalone=standalone) == expected


def test_stdout_failure_leaves_explicit_no_ack_attempt_and_blocks_retry(tmp_path, monkeypatch):
    hook = load_hook_module()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    hook_input = {"session_id": "stdout-failure-session", "turn_id": "generation-1"}
    marker = hook._pending_path(hook_input)
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True)
    marker.write_text(json.dumps({"version": 3, "generation": "generation-1", "status": "pending"}))
    monkeypatch.setattr(hook, "build_context", lambda: "synthetic context")

    def broken_output(_output):
        raise OSError("synthetic broken stdout")

    monkeypatch.setattr(hook, "_write_output", broken_output)
    with pytest.raises(OSError, match="synthetic broken stdout"):
        hook._deliver_once(hook_input, "SessionStart", standalone=True)
    attempted = json.loads(marker.read_text())
    assert attempted == {
        "acknowledgement": "none",
        "generation": "generation-1",
        "status": "delivery_attempted",
        "version": 3,
    }

    outputs = []
    monkeypatch.setattr(hook, "_write_output", outputs.append)
    assert hook._deliver_once(hook_input, "SessionStart", standalone=True) == hook.DELIVERY_NOOP
    assert outputs == []


def test_marker_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    hook = load_hook_module()
    marker = tmp_path / "pending.json"
    fsync_kinds = []
    real_fsync = hook.os.fsync

    def recording_fsync(fd):
        fsync_kinds.append(hook.stat.S_ISDIR(hook.os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(hook.os, "fsync", recording_fsync)
    assert hook._write_marker(marker, {"version": 3, "generation": "g1", "status": "pending"})
    assert fsync_kinds == [False, True]


def test_malformed_or_unrelated_event_fails_open_without_state_access(tmp_path, state_server):
    result = subprocess.run(
        [str(HOOK)], input="not json", text=True, capture_output=True,
        cwd=tmp_path, env={**os.environ, "AITELIER_MCP_URL": state_server}, timeout=10,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {"continue": True}
    assert StateStub.requests == []


def test_recorded_postcompact_then_user_prompt_order_injects_fresh_context(tmp_path, state_server):
    postcompact = invoke(tmp_path, state_server, event="PostCompact")
    assert postcompact["continue"] is True
    assert "driver_note_revision=1" in postcompact["systemMessage"]
    assert "forbidden-secret" not in postcompact["systemMessage"]
    assert "[REDACTED]" in postcompact["systemMessage"]
    assert len(postcompact["systemMessage"]) <= 12_000

    StateStub.revision = 2
    follow_up = invoke(tmp_path, state_server, event="UserPromptSubmit")
    assert set(follow_up) == {"hookSpecificOutput"}
    assert follow_up["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context = follow_up["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=2" in context
    assert "permanent revision 2" in context

    # The marker is consumed after the supported model-context event.
    assert invoke(tmp_path, state_server, event="UserPromptSubmit") == {"continue": True}
    assert len(StateStub.requests) == 6


def test_concurrent_user_prompt_fallbacks_atomically_inject_once(tmp_path, state_server):
    invoke(tmp_path, state_server, event="PostCompact")
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return invoke(tmp_path, state_server, event="UserPromptSubmit")

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _index: submit(), range(2)))

    injected = [output for output in outputs if "hookSpecificOutput" in output]
    noops = [output for output in outputs if output == {"continue": True}]
    assert len(injected) == len(noops) == 1
    assert injected[0]["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert len(StateStub.requests) == 6


def test_user_prompt_then_late_compact_session_start_does_not_inject_twice(tmp_path, state_server):
    postcompact = invoke(tmp_path, state_server, event="PostCompact")
    prompt = invoke(tmp_path, state_server, event="UserPromptSubmit")
    late_start = invoke(tmp_path, state_server, event="SessionStart")

    assert "systemMessage" in postcompact
    assert prompt["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert late_start == {"continue": True}
    assert len(StateStub.requests) == 6


def test_session_and_project_marker_isolation(tmp_path, state_server):
    invoke(tmp_path, state_server, event="PostCompact", session_id="session-a")
    assert invoke(tmp_path, state_server, event="UserPromptSubmit", session_id="session-b") == {"continue": True}
    output = invoke(tmp_path, state_server, event="UserPromptSubmit", session_id="session-a")
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "project_id=aitelier (fixed project isolation)" in output["hookSpecificOutput"]["additionalContext"]
    wire = json.dumps(StateStub.requests)
    assert "wuxia-myth" not in wire and "DRIVER_STATE.md" not in wire


def test_credentials_are_redacted_and_frontier_retains_only_active_or_actionable_state(tmp_path, state_server):
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
    assert "open-busy: attempt_id=live run_id=none attempt_status=running" in context
    assert "verified-history" not in context and "open-blocked" not in context


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
Use search_driver_note_history for bounded recovery. Do not load driver_note_history in full.
项目隔离 GUIDE-END
"""
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    for marker in ("永久导演笔记 START", "结束 END", "临时状态 START", "尾部 END",
                   "GUIDE-START", "RESUME-COMPLETE", "DISPATCH-COMPLETE", "WAIT-COMPLETE", "GUIDE-END"):
        assert marker in context
    assert "search_driver_note_history for bounded recovery" in context
    assert "Do not load driver_note_history in full" in context
    assert len(context) <= 12_000


def test_large_live_guide_retains_bounded_history_search_guidance(tmp_path, state_server):
    StateStub.guide = f"""# State DAG director protocol
{"P" * 3_000}

## Resume safely
{"R" * 3_000}

## Dispatch through either executor
{"D" * 3_000}

## Wait instead of repeatedly querying
{"W" * 3_000}

## Director notebook: context, not a second State database
{"N" * 3_000}

Use search_driver_note_history for bounded recovery. Do not load driver_note_history in full.

{"T" * 3_000}
"""
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "search_driver_note_history for bounded recovery" in context
    assert "Do not load driver_note_history in full" in context
    assert len(context) <= 12_000


def test_exact_compact_event_pair_retains_middle_of_huge_history_paragraph(tmp_path, state_server):
    StateStub.revision = 91
    StateStub.guide = f"""# State DAG director protocol
## Resume safely
{"A" * 3_100} Use search_driver_note_history for bounded recovery. Do not load the full driver_note_history. {"B" * 3_100}
"""

    postcompact = invoke(tmp_path, state_server, event="PostCompact")
    assert postcompact["continue"] is True
    assert "driver_note_revision=91" in postcompact["systemMessage"]

    output = invoke(tmp_path, state_server, event="SessionStart")
    context = output["hookSpecificOutput"]["additionalContext"]
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "driver_note_revision=91" in context
    assert "search_driver_note_history for bounded recovery" in context
    assert "Do not load the full driver_note_history" in context
    assert len(context) <= 12_000
    # SessionStart consumes the pending marker, so the next user prompt cannot
    # inject the same bootstrap a second time.
    assert invoke(tmp_path, state_server, event="UserPromptSubmit") == {"continue": True}
    assert [request["params"]["name"] for request in StateStub.requests] == [
        "state_graph_read", "state_graph_read", "state_graph_help",
        "state_graph_read", "state_graph_read", "state_graph_help",
    ]


def test_context_retains_active_run_checkpoint_attempt_and_candidate_without_mutation(tmp_path, state_server):
    StateStub.temporary = (
        "Keep checkpoint checkpoint-pending-7 pending for run run-live-7; "
        "owner remains director-a."
    )
    StateStub.nodes = [
        {"node_key": "running-node", "status": "OPEN", "readiness": "in_progress", "next_action": None,
         "latest_attempt": {"attempt_id": "attempt-live-7", "run_id": "run-live-7", "status": "running"}},
        {"node_key": "candidate-node", "status": "CANDIDATE", "readiness": "ready",
         "next_action": "candidate_review", "latest_attempt": {
             "attempt_id": "attempt-candidate-8", "artifact_ref": "candidate-sha-8", "status": "candidate"}},
    ]
    before = deepcopy((StateStub.temporary, StateStub.nodes, StateStub.revision))

    output = invoke(tmp_path, state_server, event="PostCompact")
    context = output["systemMessage"]
    assert "checkpoint-pending-7" in context and "owner remains director-a" in context
    assert "attempt_id=attempt-live-7 run_id=run-live-7 attempt_status=running" in context
    assert "attempt_id=attempt-candidate-8 artifact_ref=candidate-sha-8" in context
    assert (StateStub.temporary, StateStub.nodes, StateStub.revision) == before
    assert {request["params"]["name"] for request in StateStub.requests} == {
        "state_graph_read", "state_graph_help",
    }


def test_tracked_hook_config_uses_current_command_shape_and_move_safe_lookup():
    config = json.loads((REPO / ".codex" / "hooks.json").read_text())
    postcompact = config["hooks"]["PostCompact"][0]["hooks"][0]
    handler = config["hooks"]["SessionStart"][0]["hooks"][0]
    prompt_handler = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert config["hooks"]["SessionStart"][0]["matcher"] == "^compact$"
    assert postcompact["type"] == handler["type"] == prompt_handler["type"] == "command"
    assert postcompact["async"] is handler["async"] is prompt_handler["async"] is False
    assert postcompact["timeout"] == handler["timeout"] == prompt_handler["timeout"] == 20
    assert "additionalContextLimit" not in postcompact
    # Zero disables Codex spilling. The script's own MAX_CONTEXT_CHARS remains
    # the strict safety boundary for complete mixed-language delivery.
    assert handler["additionalContextLimit"] == prompt_handler["additionalContextLimit"] == 0
    assert handler["command"] == prompt_handler["command"] == postcompact["command"]
    assert "git rev-parse --show-toplevel" in handler["command"]
    assert "/Users/" not in handler["command"] and "/home/" not in handler["command"]
    assert HOOK.stat().st_mode & 0o111
