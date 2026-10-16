import hashlib
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
    # The subprocess binds PROJECT_ID from the session's binding file (the
    # fixtures below bind "aitelier"); module-level marker helpers must agree
    # with that bound path or they would address a different marker file.
    module.PROJECT_ID = "aitelier"
    return module



class StateStub(BaseHTTPRequestHandler):
    revision = 1
    requests = []
    lines = None  # index_line strings; get_driver_note carries no section text
    extra = None  # fields an older server would still have sent
    items = None  # full index items (address, index_line, force, lifecycle)
    guide = None
    nodes = None
    standing = None
    operations = None  # state_graph_help advertisement; None = an older server
    identity = None  # driver_whoami response
    guidance = None  # driver_postcompact_guidance v1 envelope
    driver_tokens = None  # X-AItelier-Driver-Token seen per request
    credentials = None  # every credential-bearing header seen, per request
    project = "aitelier"  # the project this stub serves; every read must name it

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        StateStub.requests.append(body)
        if StateStub.driver_tokens is not None:
            StateStub.driver_tokens.append(self.headers.get("X-AItelier-Driver-Token"))
        if StateStub.credentials is not None:
            StateStub.credentials.append(sorted(
                (k.lower(), v) for k, v in self.headers.items()
                if k.lower() in {"authorization", "x-aitelier-admin-token", "x-aitelier-driver-token"}))
        params = body["params"]
        name, args = params["name"], params["arguments"]
        if name == "driver_whoami":
            result = StateStub.identity or {"actor": "driver:stub", "driver_id": "stub"}
        elif name == "state_graph_help":
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
            if StateStub.operations is not None:
                result["operations"] = StateStub.operations
        elif args.get("action") == "driver_postcompact_guidance":
            assert args["arguments"] == {"project_id": StateStub.project, "standing_only": True}
            result = StateStub.guidance
        elif args["action"] == "get_driver_note":
            assert args["arguments"] == {"project_id": StateStub.project}
            lines = StateStub.lines or [
                f"rule revision {StateStub.revision} [in force]",
                "fresh rule; Authorization: Bearer forbidden-secret [in force]"]
            index = StateStub.items or [
                {"address": f"note://{StateStub.project}/{n:012x}", "index_line": line}
                for n, line in enumerate(lines)]
            result = {
                "project_id": StateStub.project, "revision": StateStub.revision, "index": index,
                "entry_count": len(index), "listed_count": len(index), "delisted_count": 0,
                **(StateStub.extra or {}),
            }
        elif args["action"] == "project_overview":
            assert args == {"action": "project_overview", "arguments": {"project_id": StateStub.project}}
            result = {
                "event_seq": 57,
                "nodes": StateStub.nodes or [
                    {"node_key": "ready", "status": "OPEN", "readiness": "ready", "next_action": "new_attempt"},
                    {"node_key": "busy", "status": "OPEN", "readiness": "in_progress", "next_action": None,
                     "latest_attempt": {"attempt_id": "attempt-1", "status": "running"}},
                ],
            }
        else:
            assert args == {"action": "list_director_messages", "arguments": {
                "project_id": StateStub.project, "after": 0, "limit": 8,
                "delivery_mode": "standing", "statuses": ["unread", "acknowledged"]}}
            result = StateStub.standing or {
                "schema": "aitelier.director-messaging.v2",
                "result": {"project_id": StateStub.project, "items": [], "matched_total": 0,
                           "has_more": False, "next_after": 0},
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
    StateStub.lines = StateStub.extra = StateStub.items = StateStub.guide = StateStub.nodes = None
    StateStub.standing = None
    StateStub.operations = StateStub.identity = StateStub.guidance = None
    StateStub.driver_tokens = []
    StateStub.credentials = []
    StateStub.project = "aitelier"
    server = ThreadingHTTPServer(("127.0.0.1", 0), StateStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def write_binding(codex_home, session_id, project_id="aitelier", payload=None, driver_id="stub"):
    """Write the explicit session->project binding; the hook never infers one."""
    directory = codex_home / "director-project-bindings"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    body = payload if payload is not None else json.dumps(
        {"session_id": session_id, "project_id": project_id, "driver_id": driver_id})
    path = directory / f"{key}.json"
    path.write_text(body)
    return path


def invoke(
    tmp_path, url, cwd=None, event="SessionStart", session_id="session-a", source="compact",
    bind=True, project_id="aitelier", tracked=False, env=None, driver_id="stub",
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    if bind:
        write_binding(codex_home, session_id, project_id=project_id, driver_id=driver_id)
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
        hook_input.update({"source": source, "permission_mode": "never"})
    elif event == "PostCompact":
        hook_input.update({"turn_id": "turn-compact", "trigger": "auto"})
    elif event == "UserPromptSubmit":
        hook_input.update({"turn_id": "turn-next", "permission_mode": "never", "prompt": "continue"})
    elif event == "Stop":
        hook_input.update({
            "turn_id": "turn-compact", "permission_mode": "never",
            "stop_hook_active": False, "last_assistant_message": "continuing",
        })
    command, run_cwd = [str(HOOK)], cwd or tmp_path
    if tracked:
        # The actual tracked entry chain: the command Codex runs from the
        # checked-in hooks.json, resolved through `git rev-parse` in the checkout.
        config = json.loads((REPO / ".codex" / "hooks.json").read_text())
        command, run_cwd = config["hooks"][event][0]["hooks"][0]["command"], REPO
    result = subprocess.run(
        command, input=json.dumps(hook_input), shell=tracked,
        text=True, capture_output=True, cwd=run_cwd,
        env={**os.environ, "CODEX_HOME": str(codex_home), **(env or {})},
        timeout=10, check=False,
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
    assert "Active standing director guidance" in context
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 4
    wire = json.dumps(StateStub.requests)
    assert "aitelier" in wire and "wuxia-myth" not in wire and "DRIVER_STATE.md" not in wire


def test_hook_projects_only_bounded_redacted_active_standing_without_mutation(tmp_path, state_server):
    items = []
    for index in range(8):
        message_id = f"00000000-0000-4000-8000-{index:012d}"
        delivery_id = f"10000000-0000-4000-8000-{index:012d}"
        items.append({
            "message": {
                "message_id": message_id, "thread_id": message_id,
                "sender_project_id": "other", "director_identity": "director",
                "actor": "actor", "subject": "api_key=sk_abcdefghijklmnopqrstuvwxyz",
                "body": "secret=abcdefghijklmnopqrstuvwxyz " + "界" * 500,
                "created_at": "2026-09-14T00:00:00.000000Z",
                "reply_to_delivery_id": None, "delivery_mode": "standing"},
            "delivery": {
                "delivery_id": delivery_id, "message_id": message_id,
                "target_project_id": "aitelier", "delivery_seq": index + 1,
                "status": "unread" if index % 2 == 0 else "acknowledged", "version": 1},
        })
    StateStub.standing = {
        "schema": "aitelier.director-messaging.v2",
        "result": {"project_id": "aitelier", "items": items, "matched_total": 11,
                   "has_more": True, "next_after": 8},
    }
    before = deepcopy(StateStub.standing)
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    section = context.split("## Active standing director guidance (bounded, read-only)\n", 1)[1]
    assert len(section) <= 3000
    # No guidance operation advertised (an older server): the honest notice
    # leads, then whole inbox lines, then the omission marker.
    lines = section.splitlines()
    assert lines[0].startswith(f"- {OWN_STANDING_UNAVAILABLE}: server does not advertise ")
    assert section.splitlines()[-1].startswith("[omitted_active_standing=")
    for line in lines[1:-1]:
        json.loads(line)
    assert "abcdefghijklmnopqrstuvwxyz" not in section
    assert StateStub.standing == before


def test_hook_rejects_malformed_standing_response_into_recovery_context(tmp_path, state_server):
    StateStub.standing = {"schema": "aitelier.director-messaging.v2", "result": {
        "project_id": "wrong-project", "items": [], "matched_total": 0,
        "has_more": False, "next_after": 0}}
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "recovery required" in context


def test_next_hook_invocation_reads_new_note_revision_without_cache(tmp_path, state_server):
    first = invoke(tmp_path, state_server, session_id="session-a")["hookSpecificOutput"]["additionalContext"]
    StateStub.revision = 2
    second = invoke(tmp_path, state_server, session_id="session-b")["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=1" in first
    assert "driver_note_revision=2" in second
    assert "rule revision 2" in second
    assert first != second
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


def test_unreachable_state_still_emits_bounded_recovery_context(tmp_path):
    output = invoke(tmp_path, "http://127.0.0.1:1/mcp")
    context = output["systemMessage"]
    assert len(context) <= 12_000
    assert "session driver recovery required" in context
    assert "driver_id=stub, project_id=aitelier" in context
    assert "No State, note or inbox was queried" in context
    assert "Do not use ~/.AItelier/DRIVER_STATE.md" in context
    assert "secret" not in context.lower()
    assert not (tmp_path / "codex-home" / "project-handoff-pending").exists()


def test_postcompact_marker_failure_never_blocks_and_is_actionable(tmp_path, state_server):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    (codex_home / "project-handoff-pending").write_text("file blocks marker directory")
    write_binding(codex_home, "session-marker-failure")
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
            "CODEX_HOME": str(codex_home),
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
    assert "rule revision 2" in context

    # The marker is consumed after the supported model-context event.
    assert invoke(tmp_path, state_server, event="UserPromptSubmit") == {"continue": True}
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


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
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


def test_user_prompt_then_late_compact_session_start_does_not_inject_twice(tmp_path, state_server):
    postcompact = invoke(tmp_path, state_server, event="PostCompact")
    prompt = invoke(tmp_path, state_server, event="UserPromptSubmit")
    late_start = invoke(tmp_path, state_server, event="SessionStart")

    assert "systemMessage" in postcompact
    assert prompt["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert late_start == {"continue": True}
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


def test_actual_postcompact_lifecycle_uses_stop_continuation_without_later_prompt(
    tmp_path, state_server
):
    postcompact = invoke(tmp_path, state_server, event="PostCompact")
    assert postcompact["continue"] is True

    StateStub.revision = 2
    stop = invoke(tmp_path, state_server, event="Stop")
    assert stop["decision"] == "block"
    assert "driver_note_revision=2" in stop["reason"]
    assert "# State DAG director protocol" in stop["reason"]
    assert len(stop["reason"]) <= 12_000

    # The synthetic continuation and any late compatibility event cannot
    # deliver or dispatch the same compact generation again.
    assert invoke(tmp_path, state_server, event="Stop") == {"continue": True}
    assert invoke(tmp_path, state_server, event="UserPromptSubmit") == {"continue": True}
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


def test_concurrent_stop_session_and_prompt_fallbacks_deliver_once(tmp_path, state_server):
    invoke(tmp_path, state_server, event="PostCompact")
    barrier = threading.Barrier(3)

    def deliver(event):
        barrier.wait(timeout=5)
        return invoke(tmp_path, state_server, event=event)

    with ThreadPoolExecutor(max_workers=3) as executor:
        outputs = list(executor.map(deliver, ("Stop", "SessionStart", "UserPromptSubmit")))

    delivered = [output for output in outputs if output != {"continue": True}]
    assert len(delivered) == 1
    if "decision" in delivered[0]:
        assert delivered[0]["decision"] == "block"
    else:
        assert delivered[0]["hookSpecificOutput"]["hookEventName"] in {
            "SessionStart", "UserPromptSubmit",
        }
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 8


def test_compact_session_reordered_before_postcompact_does_not_double_deliver(
    tmp_path, state_server
):
    first = invoke(tmp_path, state_server, event="SessionStart", source="compact")
    assert first["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    invoke(tmp_path, state_server, event="PostCompact")
    assert invoke(tmp_path, state_server, event="Stop") == {"continue": True}
    assert invoke(tmp_path, state_server, event="SessionStart", source="compact") == {
        "continue": True,
    }


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
    StateStub.lines = [
        'password="synthetic-password" passwd=synthetic-password pwd: synthetic-password',
        "passphrase='synthetic-passphrase' private_key=synthetic-private credential: synthetic-credential",
        "Authorization: Basic synthetic-basic https://director:synthetic-url-password@example.invalid",
        "-----BEGIN OPENSSH PRIVATE KEY-----", "SYNTHETICPEMBODY", "-----END OPENSSH PRIVATE KEY-----",
    ]
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
    StateStub.lines = ["永久导演笔记 START — current ownership — 结束 END",
                       "临时状态 START — next action / 下一步 — 尾部 END"]
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


def test_hook_injects_the_entry_index_and_never_section_text(tmp_path, state_server):
    StateStub.extra = {"permanent": "PERMANENT-SECTION-CANARY",
                       "temporary": "TEMPORARY-SECTION-CANARY"}
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "SECTION-CANARY" not in context
    assert "rule revision 1 [in force]" in context
    assert "entry_count=2 listed=2 delisted=0" in context


def test_live_sized_index_renders_only_current_entries_and_reports_omissions(
        tmp_path, state_server):
    """A live-sized index (well over the cap, oldest-first) keeps the NEWEST
    current lines, says how many older ones it omitted, and never renders a
    superseded tombstone or an informational group: the notebook holds rules
    only and in-flight state lives in the State DAG (owner ruling 2026-10-06)."""
    module = load_hook_module()
    assert not hasattr(module, "ENTRY_INDEX_GROUPS")

    def item(n, lifecycle, text):
        line = f"{text} {n:03d} " + "x" * 120
        line += (" [superseded -> note://aitelier/ffffffffffff]" if lifecycle == "superseded"
                 else " [in force]")
        return {"address": f"note://aitelier/{n:012x}", "index_line": line,
                "force": "in_force", "lifecycle": lifecycle}

    items = [item(n, "current", "rule") for n in range(200)]
    items += [item(200 + n, "superseded", "OLD-TOMBSTONE") for n in range(35)]
    items += [item(235 + n, "current", "NEWEST rule") for n in range(15)]
    assert sum(len(i["index_line"]) for i in items) > 3 * module.MAX_ENTRY_INDEX_CHARS

    rendered = module._entry_index({"index": items})
    assert len(rendered) <= module.MAX_ENTRY_INDEX_CHARS
    assert "OLD-TOMBSTONE" not in rendered and "superseded ->" not in rendered
    assert "informational" not in rendered and "in flight" not in rendered
    for entry in items[235:]:
        assert entry["index_line"] in rendered
    header = rendered.splitlines()[0]
    assert header.startswith("### current entries: total=215 shown=")
    shown = int(header.split("shown=")[1].split()[0])
    omitted = int(header.split("omitted_entry_index_lines=")[1].split()[0])
    assert shown > 0 and omitted > 0 and shown + omitted == 215
    assert f"(oldest [0]..[{omitted - 1}] dropped)" in header
    # The oldest lines are the ones omitted; every kept line carries its position.
    assert items[0]["index_line"] not in rendered
    assert rendered.splitlines()[1].startswith(f"- [{omitted}] ")
    assert rendered.splitlines()[-1].startswith("- [214] ")

    StateStub.items = items
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    for entry in items[235:]:
        assert entry["index_line"] in context
    assert "OLD-TOMBSTONE" not in context
    assert f"omitted_entry_index_lines={omitted}" in context
    assert "State DAG (attempts, node holds, issues, priorities)" in context


def test_in_flight_entry_in_the_middle_of_the_index_retains_exact_identity_tuple(
        tmp_path, state_server):
    identity = (
        "owner=director-live attempt_id=attempt-live-9 run_id=run-live-9 "
        "checkpoint=review checkpoint_id=checkpoint-live-9"
    )
    StateStub.lines = ["A" * 190] * 10 + [identity] + ["B" * 190] * 10

    context = invoke(
        tmp_path, state_server, event="SessionStart", source="startup",
        session_id="long-identity-note",
    )["hookSpecificOutput"]["additionalContext"]
    assert identity in context
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
        "driver_whoami", "state_graph_read", "state_graph_read", "state_graph_help", "state_graph_read",
        "driver_whoami", "state_graph_read", "state_graph_read", "state_graph_help", "state_graph_read",
    ]


def test_context_retains_active_run_checkpoint_attempt_and_candidate_without_mutation(tmp_path, state_server):
    StateStub.lines = [
        "Keep checkpoint checkpoint-pending-7 pending for run run-live-7; "
        "owner remains director-a."
    ]
    StateStub.nodes = [
        {"node_key": "running-node", "status": "OPEN", "readiness": "in_progress", "next_action": None,
         "latest_attempt": {"attempt_id": "attempt-live-7", "run_id": "run-live-7", "status": "running",
                            "owner": "director-a", "checkpoint": "review",
                            "checkpoint_id": "checkpoint-pending-7"}},
        {"node_key": "candidate-node", "status": "CANDIDATE", "readiness": "ready",
         "next_action": "candidate_review", "latest_attempt": {
             "attempt_id": "attempt-candidate-8", "artifact_ref": "candidate-sha-8", "status": "candidate"}},
    ]
    before = deepcopy((StateStub.lines, StateStub.nodes, StateStub.revision))

    output = invoke(tmp_path, state_server, event="PostCompact")
    context = output["systemMessage"]
    assert "checkpoint-pending-7" in context and "owner remains director-a" in context
    assert "attempt_id=attempt-live-7 run_id=run-live-7 attempt_status=running" in context
    assert "owner=director-a checkpoint=review checkpoint_id=checkpoint-pending-7" in context
    assert "attempt_id=attempt-candidate-8 artifact_ref=candidate-sha-8" in context
    assert (StateStub.lines, StateStub.nodes, StateStub.revision) == before
    assert {request["params"]["name"] for request in StateStub.requests} == {
        "state_graph_read", "state_graph_help", "driver_whoami",
    }


def test_tracked_hook_config_uses_current_command_shape_and_move_safe_lookup():
    config = json.loads((REPO / ".codex" / "hooks.json").read_text())
    postcompact = config["hooks"]["PostCompact"][0]["hooks"][0]
    handler = config["hooks"]["SessionStart"][0]["hooks"][0]
    prompt_handler = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    stop_handler = config["hooks"]["Stop"][0]["hooks"][0]
    assert config["hooks"]["SessionStart"][0]["matcher"] == "^(startup|resume|compact)$"
    assert postcompact["type"] == handler["type"] == prompt_handler["type"] == stop_handler["type"] == "command"
    assert postcompact["async"] is handler["async"] is prompt_handler["async"] is stop_handler["async"] is False
    assert postcompact["timeout"] == handler["timeout"] == prompt_handler["timeout"] == stop_handler["timeout"] == 20
    assert "additionalContextLimit" not in postcompact
    # Zero disables Codex spilling. The script's own MAX_CONTEXT_CHARS remains
    # the strict safety boundary for complete mixed-language delivery.
    assert handler["additionalContextLimit"] == prompt_handler["additionalContextLimit"] == 0
    assert handler["command"] == prompt_handler["command"] == stop_handler["command"] == postcompact["command"]
    assert "git rev-parse --show-toplevel" in handler["command"]
    assert "/Users/" not in handler["command"] and "/home/" not in handler["command"]
    assert HOOK.stat().st_mode & 0o111


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_supported_session_activation_sources_bootstrap_once(tmp_path, state_server, source):
    output = invoke(
        tmp_path,
        state_server,
        event="SessionStart",
        session_id=f"activation-{source}",
        source=source,
    )
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "driver_note_revision=1" in output["hookSpecificOutput"]["additionalContext"]
    assert invoke(
        tmp_path,
        state_server,
        event="SessionStart",
        session_id=f"activation-{source}",
        source=source,
    ) == {"continue": True}


def test_reload_then_midturn_compact_uses_fresh_context_without_duplicate(
    tmp_path, state_server
):
    resumed = invoke(
        tmp_path,
        state_server,
        event="SessionStart",
        session_id="recorded-long-session",
        source="resume",
    )
    assert "driver_note_revision=1" in resumed["hookSpecificOutput"]["additionalContext"]

    postcompact = invoke(
        tmp_path,
        state_server,
        event="PostCompact",
        session_id="recorded-long-session",
    )
    assert postcompact["continue"] is True
    StateStub.revision = 2
    compact_start = invoke(
        tmp_path,
        state_server,
        event="SessionStart",
        session_id="recorded-long-session",
        source="compact",
    )
    assert "driver_note_revision=2" in compact_start["hookSpecificOutput"]["additionalContext"]
    assert invoke(
        tmp_path,
        state_server,
        event="UserPromptSubmit",
        session_id="recorded-long-session",
    ) == {"continue": True}
    assert len([r for r in StateStub.requests if r["params"]["name"] != "driver_whoami"]) == 12


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_supported_session_activation_sources_preserve_malformed_marker(
    tmp_path, state_server, source, monkeypatch
):
    hook = load_hook_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    marker = hook._pending_path({"session_id": f"malformed-{source}"})
    assert marker is not None
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = b'{"version":3,"generation":" ","status":"pending"}\n'
    marker.write_bytes(original)

    output = invoke(
        tmp_path,
        state_server,
        event="SessionStart",
        session_id=f"malformed-{source}",
        source=source,
    )
    assert output["continue"] is True
    assert "handoff recovery required" in output["systemMessage"]
    assert marker.read_bytes() == original


def test_lapsed_lease_stays_listed_as_active_ownership():
    """Multi-driver P1: in_progress_lease_expired is still an active owner."""
    summary = load_hook_module()._frontier_summary({"event_seq": 9, "nodes": [
        {"node_key": "lapsed", "status": "OPEN", "readiness": "in_progress_lease_expired",
         "next_action": None, "latest_attempt": {
             "attempt_id": "attempt-l", "status": "running", "reporting_actor": "driver:grok",
             "owner_driver_id": "grok", "lease_state": "expired"}},
        {"node_key": "legacy", "status": "OPEN", "readiness": "in_progress", "next_action": None,
         "latest_attempt": {"attempt_id": "attempt-old", "status": "running",
                            "lease_state": "legacy_unleased"}},
    ]})
    assert "- lapsed: attempt_id=attempt-l" in summary
    assert "owner_driver_id=grok lease_state=expired" in summary
    legacy = next(line for line in summary.splitlines() if line.startswith("- legacy:"))
    assert legacy.endswith("checkpoint_id=none")


# ---------------------------------------------------------------------------
# Session-bound project isolation (owner ruling 2026-10-15): the hook is a
# no-op for any event of an unbound session, and a malformed binding fails
# closed with an honest repair notice. Both leave ZERO State queries and no
# pending marker.


def test_unbound_session_performs_zero_state_queries_for_every_event(tmp_path, state_server):
    pending_root = tmp_path / "codex-home" / "project-handoff-pending"
    for event in ("PostCompact", "SessionStart", "UserPromptSubmit", "Stop"):
        before = len(StateStub.requests)
        output = invoke(tmp_path, state_server, event=event, bind=False)
        assert output == {"continue": True}
        assert len(StateStub.requests) == before
    assert not pending_root.exists()


def test_malformed_binding_fails_closed_with_honest_repair_notice(tmp_path, state_server):
    codex_home = tmp_path / "codex-home"
    for payload in (
        "{not json at all}",
        json.dumps({"session_id": "other-session", "project_id": "aitelier"}),
    ):
        session_id = f"malformed-binding-{len(payload)}"
        assert write_binding(codex_home, session_id, payload=payload).exists()
        for event in ("PostCompact", "SessionStart", "UserPromptSubmit", "Stop"):
            before = len(StateStub.requests)
            output = invoke(tmp_path, state_server, event=event, session_id=session_id, bind=False)
            assert output["continue"] is True
            message = output.get("systemMessage", "")
            assert "session binding recovery required" in message
            assert "No State was queried" in message
            assert len(message) <= 12_000
            assert len(StateStub.requests) == before
    assert not list((codex_home / "project-handoff-pending").glob("*.json"))


def test_binding_selection_separates_platform_and_wuxia_projects(tmp_path, monkeypatch):
    module = load_hook_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    write_binding(codex_home, "session-a", project_id="aitelier")
    write_binding(codex_home, "session-b", project_id="wuxia-myth")
    assert module._read_binding({"session_id": "session-a"}) == ("bound", "aitelier", "stub")
    assert module._read_binding({"session_id": "session-b"}) == ("bound", "wuxia-myth", "stub")
    assert module._read_binding({"session_id": "never-bound-session"}) == ("missing", None, None)
    module.PROJECT_ID = "aitelier"
    platform_marker = module._pending_path({"session_id": "session-a"})
    module.PROJECT_ID = "wuxia-myth"
    wuxia_marker = module._pending_path({"session_id": "session-b"})
    module.PROJECT_ID = None
    assert platform_marker is not None and wuxia_marker is not None
    assert platform_marker != wuxia_marker
    assert platform_marker.parent == wuxia_marker.parent


def test_binding_validation_boundaries_fail_closed(tmp_path, monkeypatch):
    module = load_hook_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert module._read_binding({}) == ("missing", None, None)
    assert module._read_binding({"session_id": "   "}) == ("missing", None, None)
    assert module._read_binding({"session_id": "x" * 129}) == ("missing", None, None)
    write_binding(codex_home, "long-ok", project_id="p" * 128)
    assert module._read_binding({"session_id": "long-ok"}) == ("bound", "p" * 128, "stub")
    write_binding(codex_home, "too-long", project_id="p" * 129)
    assert module._read_binding({"session_id": "too-long"}) == ("malformed", None, None)
    write_binding(codex_home, "blank-project", project_id="   ")
    assert module._read_binding({"session_id": "blank-project"}) == ("malformed", None, None)
    write_binding(codex_home, "whitespace-session", payload=json.dumps(
        {"session_id": "whitespace-session", "project_id": "  aitelier  ", "driver_id":"stub"}))
    assert module._read_binding({"session_id": "whitespace-session"}) == ("bound", "aitelier", "stub")


# ---------------------------------------------------------------------------
# Owner standing-only selection: the hook asks for standing_only=true only when
# the server advertises the typed selector and the identity check answers; any
# other case falls back to bounded project standing with an honest notice and
# never calls the unfiltered aggregator.


STANDING_ONLY_OPERATION = {
    "mutates": False,
    "arguments": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "standing_only": {"type": "boolean", "default": False},
        },
        "required": ["project_id"],
    },
}
LEGACY_OPERATION = {
    "mutates": False,
    "arguments": {
        "type": "object",
        "properties": {"project_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        "required": ["project_id"],
    },
}
GUIDANCE_V1 = {
    "schema": "aitelier.driver-guidance.v1",
    "project_id": "aitelier",
    "driver_id": "stub",
    "projection": "- STANDING-ONLY-PROJECT-LINE\n- STANDING-ONLY-DRIVER-LINE",
    "included": 2,
    "omitted": 0,
}


def test_hook_uses_standing_only_selector_when_advertised_and_authenticated(tmp_path, state_server):
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    StateStub.guidance = GUIDANCE_V1
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "STANDING-ONLY-PROJECT-LINE" in context and "STANDING-ONLY-DRIVER-LINE" in context
    assert "own standing driver notices unavailable" not in context
    assert "Active standing director guidance" in context
    names = [request["params"]["name"] for request in StateStub.requests]
    assert "driver_whoami" in names
    guidance_calls = [
        request for request in StateStub.requests
        if request["params"]["name"] == "state_graph_read"
        and request["params"]["arguments"]["action"] == "driver_postcompact_guidance"
    ]
    assert len(guidance_calls) == 1
    assert guidance_calls[0]["params"]["arguments"]["arguments"] == {
        "project_id": "aitelier", "standing_only": True}


def test_hook_never_calls_unfiltered_aggregator_when_selector_unadvertised(tmp_path, state_server):
    StateStub.operations = {"driver_postcompact_guidance": LEGACY_OPERATION}
    context = invoke(tmp_path, state_server)["hookSpecificOutput"]["additionalContext"]
    assert "own standing driver notices unavailable" in context
    assert "server does not advertise the standing_only selector" in context
    assert "STANDING-ONLY-PROJECT-LINE" not in context
    assert "Active standing director guidance" in context
    assert "driver_postcompact_guidance" not in json.dumps(StateStub.requests)


def test_hook_refuses_all_protected_reads_when_identity_not_established(tmp_path, state_server):
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    StateStub.identity = {"actor": "anonymous"}
    output = invoke(tmp_path, state_server)
    assert "session driver recovery required" in output["systemMessage"]
    assert [r["params"]["name"] for r in StateStub.requests] == ["driver_whoami"]
    assert not (tmp_path / "codex-home" / "project-handoff-pending").exists()


def test_driver_token_file_fallback_authenticates_without_helper_or_admin_token(tmp_path, state_server):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(mode=0o700)
    (codex_home / "config.toml").write_text(f'[mcp_servers.aitelier]\nurl = "{state_server}"\n')
    token_file = tmp_path / "driver.token"
    token_file.write_text("driver-file-token")
    token_file.chmod(0o600)
    write_binding(codex_home, "file-fallback-session")
    hook_input = {
        "session_id": "file-fallback-session",
        "transcript_path": str(tmp_path / "rollout.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "source": "startup",
        "permission_mode": "never",
    }
    result = subprocess.run(
        [str(HOOK)], input=json.dumps(hook_input), text=True, capture_output=True,
        cwd=tmp_path, env={
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "AITELIER_DRIVER_TOKEN_FILE": str(token_file),
        }, timeout=10, check=False,
    )
    assert result.returncode == 0 and result.stderr == ""
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=1" in context
    assert StateStub.requests
    assert StateStub.driver_tokens == ["driver-file-token"] * len(StateStub.requests)


OWN_STANDING_UNAVAILABLE = "own standing driver notices unavailable"


@pytest.mark.parametrize("operations", [
    None,  # an older server: state_graph_help carries no operations at all
    {},  # operations present, driver_postcompact_guidance absent
    {"get_driver_note": STANDING_ONLY_OPERATION},
    {"driver_postcompact_guidance": "advertised"},  # malformed advertisement
    ["driver_postcompact_guidance"],  # malformed operations table
])
def test_unadvertised_guidance_falls_back_with_honest_notice_on_tracked_entry(
    tmp_path, state_server, operations
):
    StateStub.operations = operations
    StateStub.guidance = GUIDANCE_V1
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=1" in context
    section = context.split("## Active standing director guidance (bounded, read-only)\n", 1)[1]
    assert section.startswith(f"- {OWN_STANDING_UNAVAILABLE}: ")
    assert "server does not advertise driver_postcompact_guidance" in section
    assert "only the bounded project standing below was projected" in section
    assert "STANDING-ONLY-PROJECT-LINE" not in context
    # Project-standing-only fallback: the four baseline reads, no identity probe
    # and never the unfiltered aggregator.
    assert [(r["params"]["name"], r["params"]["arguments"].get("action")) for r in StateStub.requests] == [
        ("driver_whoami", None),
        ("state_graph_read", "get_driver_note"), ("state_graph_read", "project_overview"),
        ("state_graph_help", None), ("state_graph_read", "list_director_messages")]


@pytest.mark.parametrize("arguments", [
    {"type": "object", "properties": ["project_id", "standing_only"]},
    {"type": "object", "properties": "standing_only"},
    {"type": "object", "properties": {"standing_only": "boolean"}},
    {"type": "object", "properties": {"standing_only": {"type": "string"}}},
    "standing_only",
])
def test_malformed_standing_only_selector_is_not_treated_as_advertised(
    tmp_path, state_server, arguments
):
    StateStub.operations = {"driver_postcompact_guidance": {"mutates": False, "arguments": arguments}}
    StateStub.guidance = GUIDANCE_V1
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert "server does not advertise the standing_only selector" in context
    assert "STANDING-ONLY-PROJECT-LINE" not in context
    assert "driver_postcompact_guidance" not in json.dumps(StateStub.requests)


def _large_standing_items(count=8):
    items = []
    for index in range(count):
        message_id = f"00000000-0000-4000-8000-{index:012d}"
        items.append({
            "message": {
                "message_id": message_id, "thread_id": message_id,
                "sender_project_id": "other", "director_identity": "director",
                "actor": "actor", "subject": f"standing {index}",
                "body": "界" * 500, "created_at": "2026-09-14T00:00:00.000000Z",
                "reply_to_delivery_id": None, "delivery_mode": "standing"},
            "delivery": {
                "delivery_id": f"10000000-0000-4000-8000-{index:012d}", "message_id": message_id,
                "target_project_id": "aitelier", "delivery_seq": index + 1,
                "status": "unread", "version": 1},
        })
    return items


@pytest.mark.parametrize("case", ["absent", "legacy"])
def test_fallback_notice_stays_inside_the_standing_cap_with_truthful_omissions(
    tmp_path, state_server, case
):
    StateStub.operations = {
        "absent": None,
        "legacy": {"driver_postcompact_guidance": LEGACY_OPERATION},
        "anonymous": {"driver_postcompact_guidance": STANDING_ONLY_OPERATION},
    }[case]
    StateStub.identity = {"actor": "anonymous"} if case == "anonymous" else None
    StateStub.standing = {
        "schema": "aitelier.director-messaging.v2",
        "result": {"project_id": "aitelier", "items": _large_standing_items(), "matched_total": 11,
                   "has_more": True, "next_after": 8},
    }
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= 12_000
    section = context.split("## Active standing director guidance (bounded, read-only)\n", 1)[1]
    lines = section.splitlines()
    assert len(section) <= 3000
    assert lines[0].startswith(f"- {OWN_STANDING_UNAVAILABLE}: ")
    inbox_lines = [json.loads(line) for line in lines[1:-1]]
    assert 1 <= len(inbox_lines) < 8
    assert lines[-1] == f"[omitted_active_standing={11 - len(inbox_lines)}]"
    assert "driver_postcompact_guidance" not in json.dumps(StateStub.requests[-1])


@pytest.mark.parametrize("identity,selected", [
    ({"actor": "driver:stub", "driver_id": "stub"}, True),
    ({"actor": "owner:human", "driver_id": None}, False),
    ({"actor": "legacy:admin", "driver_id": None}, False),
    ({"driver_id": None}, False),  # whoami answered without any identity
    ({"actor": "driver:spoofed-without-id", "driver_id": ""}, False),
])
def test_authenticated_versus_absent_identity_on_tracked_entry(
    tmp_path, state_server, identity, selected
):
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    StateStub.identity = identity
    StateStub.guidance = GUIDANCE_V1
    output = invoke(tmp_path, state_server, tracked=True)
    called = "driver_postcompact_guidance" in json.dumps(StateStub.requests)
    assert called is selected
    if selected:
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "STANDING-ONLY-DRIVER-LINE" in context
        assert OWN_STANDING_UNAVAILABLE not in context
    else:
        assert "session driver recovery required" in output["systemMessage"]
        assert [r["params"]["name"] for r in StateStub.requests] == ["driver_whoami"]
        assert not (tmp_path / "codex-home" / "project-handoff-pending").exists()


def test_platform_and_wuxia_bindings_query_only_their_own_project_on_tracked_entry(
    tmp_path, state_server
):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    write_binding(codex_home, "platform-session", project_id="aitelier")
    write_binding(codex_home, "wuxia-session", project_id="wuxia-myth")
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    for session, project in (("wuxia-session", "wuxia-myth"), ("platform-session", "aitelier")):
        StateStub.project = project
        StateStub.guidance = {**GUIDANCE_V1, "project_id": project}
        before = len(StateStub.requests)
        context = invoke(tmp_path, state_server, session_id=session, bind=False,
                         tracked=True)["hookSpecificOutput"]["additionalContext"]
        assert f"project_id={project} (fixed project isolation)" in context
        assert "STANDING-ONLY-PROJECT-LINE" in context
        reads = [r["params"]["arguments"] for r in StateStub.requests[before:]
                 if r["params"]["name"] == "state_graph_read"]
        assert len(reads) == 4
        assert {read["arguments"]["project_id"] for read in reads} == {project}
    # A guidance envelope for the other project is refused, never injected.
    StateStub.guidance = {**GUIDANCE_V1, "project_id": "wuxia-myth"}
    write_binding(codex_home, "platform-session-2", project_id="aitelier")
    context = invoke(tmp_path, state_server, session_id="platform-session-2", bind=False,
                     tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert "recovery required" in context and "STANDING-ONLY-PROJECT-LINE" not in context


def test_lapsed_lease_ownership_reaches_the_tracked_entry_context(tmp_path, state_server):
    StateStub.nodes = [
        {"node_key": "lapsed", "status": "OPEN", "readiness": "in_progress_lease_expired",
         "next_action": None, "latest_attempt": {
             "attempt_id": "attempt-l", "status": "running", "reporting_actor": "driver:grok",
             "owner_driver_id": "grok", "lease_state": "expired"}},
    ]
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert "- lapsed: attempt_id=attempt-l" in context
    assert "owner_driver_id=grok lease_state=expired" in context
    assert "whoami (driver_whoami must answer driver:<your id>) -> claim_node" in context


def _credential_case(tmp_path, url, case):
    """Write one credential layout; return the env overlay for the hook."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(exist_ok=True)
    helper = tmp_path / "headers"
    lines = [f'[mcp_servers.aitelier]\nurl = "{url}"\n']
    if case == "helper_wins":
        helper.write_text("#!/bin/sh\nprintf '%s' '{\"X-AItelier-Admin-Token\":\"helper-secret\"}'\n")
        helper.chmod(0o700)
        lines.append(f'http_headers_helper = "{helper}"\n')
    (codex_home / "config.toml").write_text("".join(lines))
    env = {"AITELIER_DRIVER_TOKEN": "", "AITELIER_DRIVER_TOKEN_FILE": "",
           "AITELIER_DRIVER_ID": "", "AITELIER_ADMIN_TOKEN": "", "AITELIER_MCP_URL": "",
           "HOME": str(tmp_path / "home")}
    token_file = tmp_path / "driver.token"
    token_file.write_text("driver-file-token\n")
    if case == "helper_wins":
        env.update(AITELIER_DRIVER_TOKEN="env-driver-token", AITELIER_ADMIN_TOKEN="env-admin-token")
    elif case == "env_driver_beats_admin":
        env.update(AITELIER_DRIVER_TOKEN="env-driver-token", AITELIER_ADMIN_TOKEN="env-admin-token",
                   AITELIER_DRIVER_TOKEN_FILE=str(token_file))
        token_file.chmod(0o600)
    elif case == "file_0600":
        env.update(AITELIER_DRIVER_TOKEN_FILE=str(token_file), AITELIER_ADMIN_TOKEN="env-admin-token")
        token_file.chmod(0o600)
    elif case == "file_0644_falls_back_to_admin":
        env.update(AITELIER_DRIVER_TOKEN_FILE=str(token_file), AITELIER_ADMIN_TOKEN="env-admin-token")
        token_file.chmod(0o644)
    elif case == "driver_id_home_file":
        drivers = tmp_path / "home" / ".aitelier-drivers"
        drivers.mkdir(parents=True)
        (drivers / "stub.token").write_text("home-driver-token")
        (drivers / "stub.token").chmod(0o600)
        env.update(AITELIER_DRIVER_ID="grok", AITELIER_ADMIN_TOKEN="env-admin-token")
    return env


@pytest.mark.parametrize("case,expected", [
    ("helper_wins", [("x-aitelier-admin-token", "helper-secret")]),
    ("env_driver_beats_admin", [("x-aitelier-driver-token", "env-driver-token")]),
    ("file_0600", [("x-aitelier-driver-token", "driver-file-token")]),
    ("file_0644_falls_back_to_admin", [("x-aitelier-admin-token", "env-admin-token")]),
    ("driver_id_home_file", [("x-aitelier-driver-token", "home-driver-token")]),
])
@pytest.mark.parametrize("advertised", [False, True])
def test_credential_priority_without_driver_admin_mixing_on_every_tracked_read(
    tmp_path, state_server, case, expected, advertised
):
    if advertised:
        StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
        StateStub.guidance = GUIDANCE_V1
    env = _credential_case(tmp_path, state_server, case)
    context = invoke(tmp_path, state_server, tracked=True, env=env)["hookSpecificOutput"]["additionalContext"]
    assert "driver_note_revision=1" in context
    # note, overview, help, standing (+ whoami, guidance when advertised)
    reads = 6 if advertised else 5
    assert len(StateStub.requests) == reads
    assert StateStub.credentials == [expected] * reads
    for secret in ("helper-secret", "env-driver-token", "env-admin-token",
                   "driver-file-token", "home-driver-token"):
        assert secret not in context


@pytest.mark.parametrize("event", ["PostCompact", "SessionStart", "UserPromptSubmit", "Stop"])
def test_tracked_binding_rejects_malformed_tail_beyond_read_cap(tmp_path, state_server, event):
    session = "binding-with-unread-tail"
    payload = json.dumps({"session_id": session, "project_id": "aitelier"})
    payload = payload.ljust(4097) + "MALFORMED-TRAILING-GARBAGE"
    write_binding(tmp_path / "codex-home", session, payload=payload)
    output = invoke(tmp_path, state_server, event=event, session_id=session,
                    bind=False, tracked=True, source="startup")
    assert "session binding recovery required" in output.get("systemMessage", "")
    assert StateStub.requests == []
    assert not (tmp_path / "codex-home" / "project-handoff-pending").exists()


@pytest.mark.parametrize("characters", [4096, 4097])
def test_tracked_binding_complete_size_boundary(tmp_path, state_server, characters):
    session = "binding-size-boundary"
    payload = json.dumps({"session_id": session, "project_id": "aitelier", "driver_id":"stub"}).ljust(characters)
    write_binding(tmp_path / "codex-home", session, payload=payload)
    output = invoke(tmp_path, state_server, session_id=session, bind=False,
                    tracked=True, source="startup")
    if characters == 4096:
        assert "driver_note_revision=1" in output["hookSpecificOutput"]["additionalContext"]
        assert StateStub.requests
    else:
        assert "session binding recovery required" in output.get("systemMessage", "")
        assert StateStub.requests == []
        assert not (tmp_path / "codex-home" / "project-handoff-pending").exists()


# ---------------------------------------------------------------------------
# driver-guidance.v1 omission display. The producer already redacts every row,
# serializes whole rows newline-delimited and bounds them to 3000 chars, and it
# reports how many active notices it could not include. The standing section
# must display that count without breaking the cap or inventing notices.


def _v1_marker(count):
    return f"[omitted_active_standing={count}]"


def _tracked_standing_section(tmp_path, state_server, envelope):
    """Run the actual tracked entry and return the standing section verbatim."""
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    StateStub.guidance = envelope
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    assert "recovery required" not in context
    assert "## Active standing director guidance (bounded, read-only)\n" in context
    return context.split("## Active standing director guidance (bounded, read-only)\n", 1)[1]


def _real_v1_envelope(tmp_path, standing=12, subject="s" * 60, body="界" * 320):
    """Run the real v1 producer over a real store: redacted whole JSON rows,
    newline-delimited, bounded to 3000 chars, carrying its own omission count."""
    from core.drivers import DriverRegistry
    from core.state_commands import execute
    from core.state_database import StateDatabase
    from core.state_service import StateService

    db = StateDatabase(str(tmp_path / "guidance-producer.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("aitelier", "aitelier")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for driver_id in ("ada", "bob"):
        registry.register(driver_id, driver_id, actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    registry.set_membership("aitelier", "bob", "member", 0, "fixture", actor="test")
    ada = StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True)
    bob = StateService(db, actor="driver:bob", driver_id="bob", project_read_trusted=True)
    for index in range(standing):
        execute(ada, "send_director_message", {
            "sender_project_id": "alpha", "target_project_id": "aitelier",
            "protocol_version": "v3", "director_identity": "ada",
            "request_key": f"standing-{index}", "subject": subject, "body": body,
            "delivery_mode": "standing"}, allow_write=True)
    # Ordinary/private driver mail and broadcast transients must stay out of the
    # standing section: they are read elsewhere, never injected here.
    execute(ada, "send_director_message", {
        "sender_project_id": "alpha", "target_project_id": "aitelier",
        "protocol_version": "v3", "director_identity": "ada",
        "request_key": "broadcast-1", "subject": "TRANSIENT-BROADCAST",
        "body": "broadcast body", "ack_mode": "broadcast"}, allow_write=True)
    bob.driver_inbox.send_driver_message("ada", "ORDINARY-PRIVATE", "ordinary body",
                                         target_driver_id="bob")
    return execute(bob, "driver_postcompact_guidance",
                   {"project_id": "aitelier", "standing_only": True})


def test_guidance_v1_omission_count_reaches_the_tracked_entry_with_one_whole_row_drop(
    tmp_path, state_server
):
    envelope = _real_v1_envelope(tmp_path)
    rows = envelope["projection"].split("\n")
    assert envelope["schema"] == "aitelier.driver-guidance.v1"
    assert envelope["standing_only"] is True
    assert envelope["included"] == len(rows) == 5
    assert envelope["omitted"] == 7
    # The producer's own bound leaves less room than the omission marker needs,
    # so exactly one whole producer row must go for the section to fit.
    assert len(envelope["projection"]) + 1 + len(_v1_marker(envelope["omitted"])) > 3000

    section = _tracked_standing_section(tmp_path, state_server, envelope)
    assert len(section) <= 3000
    lines = section.splitlines()
    kept, marker = lines[:-1], lines[-1]
    assert len(kept) == len(rows) - 1
    assert kept == rows[:len(kept)]  # whole leading rows, byte for byte
    assert marker == _v1_marker(envelope["omitted"] + 1)
    for line in kept:
        assert json.loads(line)["body_excerpt"] == "界" * 320  # no truncated row
    assert "TRANSIENT-BROADCAST" not in section
    assert "ORDINARY-PRIVATE" not in section
    names = [request["params"]["name"] for request in StateStub.requests]
    assert "driver_whoami" in names


def test_guidance_v1_zero_omission_is_not_falsely_marked(tmp_path, state_server):
    section = _tracked_standing_section(tmp_path, state_server, GUIDANCE_V1)
    assert section == GUIDANCE_V1["projection"]
    assert "[omitted_active_standing=" not in section


def test_guidance_v1_all_omitted_projection_still_reports_the_count(tmp_path, state_server):
    envelope = {
        "schema": "aitelier.driver-guidance.v1", "project_id": "aitelier",
        "driver_id": "stub", "standing_only": True,
        "projection": "", "included": 0, "omitted": 5,
    }
    section = _tracked_standing_section(tmp_path, state_server, envelope)
    assert section.splitlines() == [_v1_marker(5)]


@pytest.mark.parametrize("omitted", [None, "0", -1, True])
def test_guidance_v1_invalid_omission_counter_fails_closed(tmp_path, state_server, omitted):
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    envelope = {key: value for key, value in GUIDANCE_V1.items() if key != "omitted"}
    if omitted is not None:
        envelope["omitted"] = omitted
    StateStub.guidance = envelope
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    # An unreadable omission count is refused outright, never silently dropped.
    assert "recovery required" in context
    assert "STANDING-ONLY-PROJECT-LINE" not in context


def test_guidance_v1_row_count_beyond_included_fails_closed(tmp_path, state_server):
    StateStub.operations = {"driver_postcompact_guidance": STANDING_ONLY_OPERATION}
    StateStub.guidance = {
        "schema": "aitelier.driver-guidance.v1", "project_id": "aitelier",
        "driver_id": "stub", "standing_only": True,
        "projection": '{"a": 1}\n{"a": 2}', "included": 1, "omitted": 3,
    }
    context = invoke(tmp_path, state_server, tracked=True)["hookSpecificOutput"]["additionalContext"]
    # More rows than the counted notices: refuse rather than show what was not counted.
    assert "recovery required" in context




@pytest.mark.parametrize("event", ["PostCompact", "SessionStart", "UserPromptSubmit", "Stop"])
@pytest.mark.parametrize("identity", [
    {"actor":"driver:other", "driver_id":"other"},
    {"actor":"owner:human"}, {"actor":"legacy:admin"},
    {"actor":"driver:stub", "driver_id":"other"},
])
def test_bound_driver_mismatch_precedes_reads_and_marker_changes(tmp_path, state_server, event, identity):
    # Establish a real pending handoff, then prove rejection preserves its bytes.
    invoke(tmp_path, state_server, event="PostCompact")
    root = tmp_path / "codex-home" / "project-handoff-pending"
    before = {str(p.relative_to(root)):p.read_bytes() for p in root.iterdir() if p.is_file()}
    StateStub.requests = []
    StateStub.identity = identity
    output = invoke(tmp_path, state_server, event=event, bind=False)
    assert "session driver recovery required" in output["systemMessage"]
    assert [r["params"]["name"] for r in StateStub.requests] == ["driver_whoami"]
    assert {str(p.relative_to(root)):p.read_bytes() for p in root.iterdir() if p.is_file()} == before


def test_stdin_bound_identity_overrides_stale_helper_environment_for_both_drivers(tmp_path, state_server):
    helper = tmp_path / "headers"
    seen = tmp_path / "helper-selection.json"
    helper.write_text("#!/usr/bin/env python3\nimport os,json,pathlib\n"
                      + f"pathlib.Path({str(seen)!r}).write_text(json.dumps([os.environ.get(k) for k in ['AITELIER_DRIVER_ID','CODEX_THREAD_ID','CODEX_SESSION_ID']]))\n"
                      + "print(json.dumps({'X-AItelier-Driver-Token':os.environ['AITELIER_DRIVER_ID']}))\n")
    helper.chmod(0o700)
    for project, driver in [("aitelier","codex-aitelier"),("wuxia-myth","codex")]:
        session = "bound-" + driver
        StateStub.project = project
        StateStub.identity = {"actor":"driver:"+driver,"driver_id":driver}
        StateStub.requests = []; StateStub.driver_tokens = []
        output = invoke(tmp_path, state_server, session_id=session, project_id=project, driver_id=driver,
                        env={"AITELIER_DRIVER_ID":"wrong", "CODEX_THREAD_ID":"stale", "CODEX_SESSION_ID":"stale"})
        assert "driver_note_revision=1" in output["hookSpecificOutput"]["additionalContext"]
        assert json.loads(seen.read_text()) == [driver,session,session]
        assert StateStub.driver_tokens == [driver] * len(StateStub.requests)
        assert StateStub.requests[0]["params"]["name"] == "driver_whoami"
        assert {r["params"]["arguments"].get("arguments",{}).get("project_id")
                for r in StateStub.requests if r["params"]["name"] == "state_graph_read"} == {project}


@pytest.mark.parametrize("driver", [None,"", "  ","../codex","bad/name", "a"*129, 1,"bad\nname"])
def test_invalid_bound_driver_never_authenticates_or_mutates_markers(tmp_path, state_server, driver):
    home = tmp_path / "codex-home"
    write_binding(home,"bad-driver",payload=json.dumps({"session_id":"bad-driver","project_id":"aitelier","driver_id":driver}))
    output = invoke(tmp_path,state_server,session_id="bad-driver",bind=False)
    assert "session binding recovery required" in output["systemMessage"]
    assert StateStub.requests == []
    assert not (home/"project-handoff-pending").exists()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_quiet_bound_event_without_handoff_needs_no_credentials(tmp_path, state_server, event):
    output = invoke(tmp_path,state_server,event=event)
    assert output == {"continue":True}
    assert StateStub.requests == []
    assert not (tmp_path/"codex-home"/"project-handoff-pending").exists()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
def test_completed_handoff_does_not_reauthenticate_on_quiet_events(tmp_path, state_server, event):
    invoke(tmp_path,state_server,event="SessionStart")
    StateStub.requests = []
    assert invoke(tmp_path,state_server,event=event) == {"continue":True}
    assert StateStub.requests == []
