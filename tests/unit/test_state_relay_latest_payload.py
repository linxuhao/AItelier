"""Latest failure accounting must survive the real external handoff."""
import copy
import subprocess
from pathlib import Path

import pytest
from skillflow.core import graph_digest

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_graph import StateConflict, digest
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

FIRST = {"turns": 12, "max_turns": 12,
         "read_accounting": {"bytes": 9000, "repeated_bytes": 7000},
         "remaining_delivery": ["test first draft", "deliver report"]}
LATEST = {"turns": 20, "max_turns": 20,
          "read_accounting": {"bytes": 3100, "repeated_bytes": 1700},
          "remaining_delivery": ["deliver latest report"]}

def budget(seq, payload):
    return {"seq": seq, "step_id": "work", "step_instance_id": seq,
            "category": "step", "event": "turn_budget_exhausted",
            "created_at": f"2026-10-04T10:{seq:02d}:00Z", "payload": copy.deepcopy(payload)}

class PublicTraceRuntime:
    """Scripted public APIs only; never create/start/claim/execute a workflow."""
    def __init__(self):
        self.graph = {"name": "feature", "begin": "work", "steps": [{"id": "work"}]}
        self.pin = graph_digest(self.graph)
        self.runs, self.rows, self.trace_calls = {}, {}, []

    def get_run(self, run_id):
        return copy.deepcopy(self.runs[run_id])

    def get_graph_version(self, name, version):
        assert name == "feature" and version == 1
        return {"digest": self.pin, "graph": self.graph}

    def audit_operation_owners(self, run_id):
        return {"lost": [], "unknown": [], "alive": 0}

    def get_steps(self, run_id):
        return [{"step_id": "work", "status": "failed",
                 "error": self.runs[run_id]["error_reason"]}]

    def get_trace(self, run_id, *, order="asc", limit=None, **filters):
        self.trace_calls.append((run_id, order, limit))
        rows = copy.deepcopy(self.rows[run_id])
        if order == "desc":
            rows.reverse()
        return rows if limit is None else rows[:limit]

def owned_world(tmp_path, monkeypatch, service_class=StateService):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.db"))
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    runtime = PublicTraceRuntime()
    service = service_class(db, ws, runtime, {}, project_read_trusted=True)
    service.create_project("game", "Owned relay fixture")
    service.store.add_nodes("game", [{"key": "a", "goal": "Retain delivery",
        "dependencies": [], "acceptance": [{"id": "result", "kind": "test",
                                             "description": "Retained delivery"}]}])
    source = tmp_path / "source"
    source.mkdir()
    (source / "seed.txt").write_text("owned baseline\n")
    for args in [("init", "-q", "-b", "main"), ("config", "user.email", "fixture@local"),
                 ("config", "user.name", "fixture"), ("add", "seed.txt"),
                 ("commit", "-qm", "owned baseline")]:
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    return service, runtime, source

def fail_round(service, runtime, source, request, rows, prior=None):
    attempt = service.attempts.reserve("game", "a", 1, "feature", request,
                                      continue_from=prior["attempt_id"] if prior else None)
    run_id = "owned-" + request
    runtime.runs[run_id] = {"id": run_id, "project_id": attempt["execution_project_id"],
        "graph_name": "feature", "graph_version": 1, "graph_digest": runtime.pin,
        "status": "failed", "current_node": "work", "error_reason": request + " retained failure"}
    runtime.rows[run_id] = copy.deepcopy(rows)
    service.attempts.bind_run(attempt["attempt_id"], run_id, runtime)
    service.db.ensure_project(attempt["execution_project_id"], name=request,
                              repo_type="existing", repo_path=str(source))
    ri.ensure_for_run(service.db, run_id=run_id, project_id=attempt["execution_project_id"],
                      config_name="feature", repo_mode="code")
    service.ws.write_draft(attempt["execution_project_id"], "work", "remaining.txt",
                           request + " draft", graph_name="feature")
    return service.reconcile_attempt(attempt["attempt_id"])

def handoff_chain(service, runtime, source, first_rows=None, latest_rows=None):
    first = fail_round(service, runtime, source, "first", [budget(1, FIRST)] if first_rows is None else first_rows)
    latest = fail_round(service, runtime, source, "latest",
                        [budget(9, LATEST)] if latest_rows is None else latest_rows, first)
    before = copy.deepcopy(runtime.rows)
    inventory = latest["relay_inventory"]
    result = service.disposition_failed_attempt(
        latest["attempt_id"], "handoff-external", request_key="handoff",
        relay_digest=inventory["digest"], instruction="finish retained delivery",
        harness="owned-worker", external_id="owned/next")
    assert runtime.rows == before
    assert result["automatic_retry"] is False and result["checkpoints"] == "ask"
    assert result["attempt"]["run_id"] is None
    return first, latest, result

def assert_latest_handoff(first, latest, result):
    frozen = result["attempt"]["context"]["relay_handoff"]
    assert frozen == result["handoff"]
    assert frozen["remaining_delivery"] == LATEST["remaining_delivery"]
    latest_trace = frozen["relay_inventory"]["remaining_delivery_trace"]
    assert latest_trace["payload"] == LATEST
    assert latest_trace["seq"] == 9 and latest_trace["step_instance_id"] == 9
    original = frozen["original_first_failure"]
    assert original["attempt_id"] == first["attempt_id"]
    assert original["run_id"] == first["run_id"]
    assert original["error"] == first["error"]
    assert original["trace"]["payload"] == FIRST
    assert original["trace"]["seq"] == 1
    assert original["trace"]["payload"] != latest_trace["payload"]
    assert result["attempt"]["context_hash"] == digest(result["attempt"]["context"])

def test_latest_payload_is_frozen_by_real_disposition(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first, latest, result = handoff_chain(service, runtime, source)
    assert_latest_handoff(first, latest, result)
    reread = service.external.inspect(result["attempt"]["attempt_id"])
    assert reread["context"] == result["attempt"]["context"]
    assert service.attempts.get(latest["attempt_id"])["status"] == "failed"
    duplicate = service.disposition_failed_attempt(
        latest["attempt_id"], "handoff-external", request_key="handoff",
        relay_digest=latest["relay_inventory"]["digest"], instruction="finish retained delivery",
        harness="owned-worker", external_id="owned/next")
    assert duplicate["idempotent"] is True
    assert duplicate["attempt"]["attempt_id"] == result["attempt"]["attempt_id"]
    runtime.rows[latest["run_id"]][-1]["payload"]["read_accounting"]["bytes"] = 1
    assert service.external.inspect(result["attempt"]["attempt_id"])["context"] == reread["context"]
    for run_id in (first["run_id"], latest["run_id"]):
        assert (run_id, "desc", 500) in runtime.trace_calls

def test_single_failure_keeps_one_exact_payload_for_both_roles(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first = fail_round(service, runtime, source, "first", [budget(1, FIRST)])
    metadata = first["relay_inventory"]
    assert metadata["remaining_delivery_trace"]["payload"] == FIRST
    assert metadata["original_first_failure"]["trace"]["payload"] == FIRST
    assert metadata["original_first_failure"]["attempt_id"] == first["attempt_id"]

@pytest.mark.parametrize("payload", [None, "unparsed retained row", [], {}, {"remaining_delivery": ["ok", 3]}])
def test_malformed_payload_is_retained_without_inventing_remaining(tmp_path, monkeypatch, payload):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first, latest, result = handoff_chain(service, runtime, source, latest_rows=[budget(9, payload)])
    frozen = result["attempt"]["context"]["relay_handoff"]
    assert frozen["remaining_delivery"] == []
    assert frozen["relay_inventory"]["remaining_delivery_trace"]["payload"] == payload
    assert frozen["original_first_failure"]["trace"]["payload"] == FIRST

def test_absent_latest_budget_does_not_borrow_first_payload(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first, latest, result = handoff_chain(service, runtime, source, latest_rows=[
        {"seq": 9, "event": "other_event", "payload": LATEST}])
    frozen = result["attempt"]["context"]["relay_handoff"]
    assert frozen["remaining_delivery"] == []
    assert frozen["relay_inventory"]["remaining_delivery_trace"] is None
    assert frozen["original_first_failure"]["trace"]["payload"] == FIRST

def test_trace_tail_is_bounded_and_selects_latest_event(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    rows = [budget(i, FIRST) for i in range(1, 510)] + [budget(510, LATEST)]
    first = fail_round(service, runtime, source, "first", rows)
    metadata = first["relay_inventory"]
    assert metadata["remaining_delivery_trace"]["seq"] == 510
    assert metadata["remaining_delivery_trace"]["payload"] == LATEST
    assert ("owned-first", "desc", 500) in runtime.trace_calls
    assert metadata["original_first_failure"]["trace"]["seq"] == 11

def test_handoff_refuses_a_different_reporter_and_changed_digest(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first, latest, result = handoff_chain(service, runtime, source)
    from core.state_external import ExternalAttempts
    outsider = ExternalAttempts(service.attempts, "different-owner")
    with pytest.raises(StateConflict, match="different authenticated"):
        outsider.observe(result["attempt"]["attempt_id"], "wrong-owner", 0,
                         result["attempt"]["context_hash"], "running", "unused", "0" * 64)
    with pytest.raises(StateConflict, match="changed since"):
        service.disposition_failed_attempt(
            latest["attempt_id"], "handoff-external", request_key="other-handoff",
            relay_digest="0" * 64, harness="owned-worker", external_id="owned/other")

def test_no_budget_rows_retains_origin_without_invented_trace(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    first, latest, result = handoff_chain(service, runtime, source, first_rows=[], latest_rows=[])
    frozen = result["attempt"]["context"]["relay_handoff"]
    assert frozen["remaining_delivery"] == []
    assert frozen["relay_inventory"]["remaining_delivery_trace"] is None
    assert frozen["original_first_failure"]["trace"] is None
    assert frozen["original_first_failure"]["run_id"] == first["run_id"]
    assert frozen["original_first_failure"]["error"] == first["error"]

def test_legacy_public_trace_api_keeps_the_bounded_tail(tmp_path, monkeypatch):
    service, runtime, source = owned_world(tmp_path, monkeypatch)
    rows = [budget(i, FIRST) for i in range(1, 701)]
    monkeypatch.setattr(runtime, "get_trace", lambda run_id: copy.deepcopy(rows))
    got = service._trace_rows("legacy-run")
    assert len(got) == 500
    assert got[0]["seq"] == 700 and got[-1]["seq"] == 201
