"""Recovery through actual isolated State/host/SDK bindings, offline.

The imported live fixture replaces composition-root getters with initialized
isolated objects and disables scheduler wake/driver attachment. No guard,
binding, status, owner audit or disposition result is mocked.
"""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.core import StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition

from api import dependencies as deps, meta_routers as mr, project_routers as pr, run_routers as rr
from api.auth import get_optional_user
from core.db_manager import DBManager
from core.meta_agent import MetaAgent
from core.run_launcher import start_config_run
from tests.integration.test_state_graph_entrypoints import live, start, failed_code_attempt

pytestmark = pytest.mark.parametrize("live", ["host"], indirect=True)
REFUSAL = "Generic recovery is unavailable; use an explicit State successor disposition."


def client_for(live, monkeypatch):
    monkeypatch.setattr(mr, "get_skillflow", lambda: live.sf)
    monkeypatch.setattr(rr, "get_skillflow", lambda: live.sf)
    app = FastAPI()
    for module in (pr, mr, rr):
        app.include_router(module.router)
        app.dependency_overrides[module.get_db_manager] = lambda: live.db
    app.dependency_overrides[get_optional_user] = lambda: None
    return TestClient(app)


def snapshot(live):
    with live.db.get_connection() as conn:
        host = list(conn.iterdump())
    with live.sf._lock:
        engine = list(live.sf._conn.iterdump())
    return host, engine


def retained_task(live, pid):
    task, = live.db.create_tasks_from_manifest(pid, {"tasks": [{"id": "retained", "description": "finish"}]})
    with live.db.get_connection() as conn:
        conn.execute("UPDATE tasks SET status='failed',current_step='t_impl',"
                     "completed_steps='[\"t_plan\"]',task_meta_state='retained task error' WHERE id=?", (task,))
        conn.commit()
    live.db.set_project_meta_state(pid, json.dumps({"error": "retained project error"}))


@pytest.mark.parametrize("reconciled", [False, True], ids=["stale-state", "terminal-state"])
@pytest.mark.parametrize("entry", ["api", "db", "butler", "reactivate_run", "re_run", "resume_run", "pause_run"])
def test_generic_recovery_refuses_before_any_durable_change(live, monkeypatch, reconciled, entry):
    attempt = start(live)
    rid, pid = attempt["run_id"], attempt["execution_project_id"]
    live.sf.fail_run(rid, "retained first failure")
    assert live.sf.get_run(rid)["status"] == "failed"
    if reconciled:
        attempt = live.service.reconcile_attempt(attempt["attempt_id"])
    assert live.service.attempts.get(attempt["attempt_id"])["status"] == ("failed" if reconciled else "running")
    retained_task(live, pid)
    # Butler cleanup uses the default DPE task directories.
    live.ws.write_draft(pid, "t_impl", "retained.txt", "retained draft")
    draft = live.ws._draft_dir(pid, "t_impl") / "retained.txt"
    before = snapshot(live)
    with client_for(live, monkeypatch) as client:
        if entry == "api":
            response = client.post(f"/api/projects/{pid}/retry")
            assert response.status_code == 409, response.text
            assert response.json() == {"detail": REFUSAL}
            for private in (attempt["attempt_id"], rid, attempt["context_hash"], "Long-running game"):
                assert private not in response.text
        else:
            with pytest.raises(ValueError, match="successor disposition"):
                if entry == "db":
                    live.db.retry_project(pid)
                elif entry == "butler":
                    MetaAgent._tool_retry_project(SimpleNamespace(db=live.db, ws=live.ws), {"project_id": pid})
                else:
                    getattr(live.sf, entry)(rid)
    assert snapshot(live) == before
    assert draft.read_text() == "retained draft"


@pytest.mark.parametrize("entry", ["api", "db"])
@pytest.mark.parametrize("state_schema", [False, True], ids=["no-state-table", "state-initialized"])
def test_unbound_retry_restores_budget_and_actually_completes(live, monkeypatch, entry, state_schema):
    if not state_schema:
        # A second initialized host DB without State; the same initialized SDK
        # and workspace still execute the actual run.
        live.db = DBManager(str(live.tmp / "unbound-host.sqlite"))
        monkeypatch.setattr(deps, "get_db_manager", lambda: live.db)
        monkeypatch.setattr(deps, "db_instance", live.db)
    name, pid = "ordinary_retry", "ordinary-retry"
    live.sf.register_graph(PipelineGraph(name=name, begin="work", steps=[StepNode(id="work", max_retries=1)]))
    live.registry.register_one(live.sf, name, hint_overrides={"scheduler_owned": True, "repo_mode": "none"})
    launched = start_config_run(live.db, live.ws, name, pid)
    assert launched["status"] != "error", launched
    rid = launched["run_id"]
    for _ in range(2):
        live.sf.advance_run(rid)
        claimed = live.sf.claim_next_step(rid)
        assert claimed is not None
        live.sf.fail_step(claimed.token, "ordinary exhausted failure", retryable=True)
    assert live.sf.get_run(rid)["status"] == "failed"
    failed = live.sf.get_steps(rid)[0]
    assert failed["status"] == "failed" and failed["retry_count"] == failed["max_retries"]
    retained_task(live, pid)
    first_trace = live.sf.get_trace(rid)
    with live.db.get_connection() as conn:
        assert bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_attempts'").fetchone()) == state_schema
    if entry == "api":
        with client_for(live, monkeypatch) as client:
            response = client.post(f"/api/projects/{pid}/retry")
        assert response.status_code == 200, response.text
    else:
        assert live.db.retry_project(pid)
    assert live.sf.get_run(rid)["status"] == "running"
    restored = live.sf.get_steps(rid)[0]
    assert restored["status"] == "pending"
    assert restored["retry_count"] == restored["validation_retry_count"] == 0
    assert live.db.list_tasks_by_project(pid)[0]["status"] == "pending"
    live.sf.advance_run(rid)
    claim = live.sf.claim_next_step(rid)
    assert claim is not None
    live.sf.confirm_step(claim.token, StepResult(outputs={"proof": "retried work"}))
    live.sf.advance_run(rid)
    assert live.sf.get_run(rid)["status"] == "completed"
    assert live.sf.get_trace(rid)[:len(first_trace)] == first_trace


@pytest.mark.parametrize("disposition", ["continue-workflow", "handoff-external"])
def test_supported_successor_keeps_original_failure_and_context(live, disposition):
    attempt = failed_code_attempt(live)
    old_run = live.sf.get_run(attempt["run_id"])
    old_trace = live.sf.get_trace(attempt["run_id"])
    kwargs = {"harness": "fixture-owned", "external_id": "fixture/successor"} if disposition == "handoff-external" else {}
    result = live.service.disposition_failed_attempt(
        attempt["attempt_id"], disposition, request_key="supported-successor",
        relay_digest=attempt["relay_inventory"]["digest"], **kwargs)
    successor = result["attempt"]
    assert result["dispatchable"] and successor["attempt_id"] != attempt["attempt_id"]
    assert successor["execution_project_id"] != attempt["execution_project_id"]
    assert successor["run_id"] != attempt["run_id"]
    if disposition == "continue-workflow":
        assert live.sf.get_run(successor["run_id"])["status"] == "running"
    else:
        assert successor["execution_kind"] == "external" and successor["run_id"] is None
    current = live.service.attempts.get(attempt["attempt_id"])
    for field in ("status", "error", "context", "context_hash", "graph_version", "graph_digest", "owner_driver_id", "owner_fence"):
        assert current[field] == attempt[field]
    assert current["status"] == "failed"
    assert live.sf.get_run(attempt["run_id"]) == old_run
    assert live.sf.get_trace(attempt["run_id"]) == old_trace


def test_state_checkpoint_continuation_and_pause_resume_remain_useful(live):
    live.sf.register_graph(PipelineGraph(name="state_fixture", begin="work", steps=[
        StepNode(id="work", checkpoint=True, transitions=[Transition(to="after")]), StepNode(id="after")]))
    attempt = start(live)
    rid = attempt["run_id"]
    live.sf.pause_run(rid)
    live.sf.resume_run(rid)
    live.sf.advance_run(rid)
    claim = live.sf.claim_next_step(rid)
    assert claim is not None
    live.sf.confirm_step(claim.token, StepResult(outputs={"plan": "retained"}))
    live.sf.advance_run(rid)
    assert live.sf.get_run(rid)["status"] == "paused"
    live.service.reconcile_attempt(attempt["attempt_id"])
    checkpoint = live.sf.get_checkpoint(rid)
    live.sf.approve_checkpoint(rid, expected_checkpoint=checkpoint["expected_checkpoint"], decision_id="normal-state-checkpoint")
    assert live.sf.get_run(rid)["status"] == "running"
    assert live.service.attempts.get(attempt["attempt_id"])["context_hash"] == attempt["context_hash"]


@pytest.mark.parametrize("table", ["sqlite_master", "state_attempts"], ids=["scope-detection-error", "binding-read-error"])
def test_actual_sqlite_read_refusal_is_conservative_and_private(live, monkeypatch, table):
    attempt = start(live)
    live.sf.fail_run(attempt["run_id"], "retained failure")
    retained_task(live, attempt["execution_project_id"])
    before = snapshot(live)
    connect = live.db.get_connection

    @contextmanager
    def denied_read():
        with connect() as conn:
            # Actual SQLite read authorization fault, not a guard/binding result.
            conn.set_authorizer(lambda action, name, column, database, trigger:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and name == table else sqlite3.SQLITE_OK)
            yield conn

    with monkeypatch.context() as scoped:
        scoped.setattr(live.db, "get_connection", denied_read)
        with client_for(live, scoped) as client:
            response = client.post(f"/api/projects/{attempt['execution_project_id']}/retry")
        assert response.status_code == 409, response.text
        assert response.json() == {"detail": "Cannot establish recovery scope; recovery refused."}
    assert snapshot(live) == before


@pytest.mark.parametrize("prefix", ["/api/meta", "/api/runs"])
def test_failed_checkpoint_rescue_refuses_before_state_controller(live, monkeypatch, prefix):
    attempt = start(live)
    live.sf.fail_run(attempt["run_id"], "retained checkpoint failure")
    before = snapshot(live)
    identity = attempt["run_id"] if prefix == "/api/runs" else attempt["execution_project_id"]
    with client_for(live, monkeypatch) as client:
        response = client.post(f"{prefix}/{identity}/checkpoint/approve", json={
            "run_id": attempt["run_id"], "checkpoint": "work", "rescue_failed": True})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": REFUSAL}
    assert snapshot(live) == before


@pytest.mark.parametrize("prefix", ["/api/meta", "/api/runs"])
def test_unbound_failed_checkpoint_rescue_still_continues_real_work(live, monkeypatch, prefix):
    name, pid = "ordinary_checkpoint_rescue", "ordinary-checkpoint-rescue"
    live.sf.register_graph(PipelineGraph(name=name, begin="plan", steps=[
        StepNode(id="plan", checkpoint=True, transitions=[Transition(to="work")]),
        StepNode(id="work")]))
    live.registry.register_one(live.sf, name, hint_overrides={"scheduler_owned": True, "repo_mode": "none"})
    launched = start_config_run(live.db, live.ws, name, pid)
    rid = launched["run_id"]
    live.sf.advance_run(rid)
    claimed = live.sf.claim_next_step(rid)
    live.sf.confirm_step(claimed.token, StepResult(outputs={"plan": "retain this plan"}))
    live.sf.advance_run(rid)
    assert live.sf.get_run(rid)["status"] == "paused"
    live.sf.fail_run(rid, "ordinary checkpoint failure")
    first_trace = live.sf.get_trace(rid)
    identity = rid if prefix == "/api/runs" else pid
    with client_for(live, monkeypatch) as client:
        response = client.post(f"{prefix}/{identity}/checkpoint/approve", json={
            "run_id": rid, "checkpoint": "plan", "rescue_failed": True})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rescued"
    assert live.sf.get_run(rid)["status"] == "running"
    live.sf.advance_run(rid)
    claimed = live.sf.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == "work"
    live.sf.confirm_step(claimed.token, StepResult(outputs={"done": True}))
    live.sf.advance_run(rid)
    assert live.sf.get_run(rid)["status"] == "completed"
    assert live.sf.get_trace(rid)[:len(first_trace)] == first_trace
