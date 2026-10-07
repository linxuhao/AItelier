"""Owned launch refusal must settle without starting a step or retrying itself."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_graph import StateGraphStore
from core.state_attempts import StateAttempts
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

@pytest.fixture
def launch_world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "host.db"))
    store = StateGraphStore(db, project_read_trusted=True)
    store.create_project("owner", "Owned source launch test")
    store.add_nodes("owner", [{"key": "a", "goal": "Launch safely", "acceptance": [
        {"id": "launch", "kind": "test", "description": "Owned launch"}]}])
    attempts = StateAttempts(store)
    sf = SkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "sf-ws"))
    sf.register_graph(PipelineGraph(name="launch_fixture", begin="implementation",
        steps=[StepNode(id="implementation")]))
    source = tmp_path / "source"
    source.mkdir()
    (source / "seed.txt").write_text("owned\n")
    for args in [("init", "-q", "-b", "main"), ("config", "user.email", "test@test"),
                 ("config", "user.name", "test"), ("add", "seed.txt"),
                 ("commit", "-qm", "owned source")]:
        subprocess.run(["git", *args], cwd=source, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    import core.scheduler as scheduler
    import api.dependencies as deps
    monkeypatch.setattr(scheduler, "db", db)
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    manifest = SimpleNamespace(config_name="launch_fixture", seed_file="", repo_mode="code", scheduler_owned=True)
    monkeypatch.setattr(deps, "get_config_registry", lambda: SimpleNamespace(get=lambda _: manifest, list=lambda: [manifest]))
    logs = []
    monkeypatch.setattr(scheduler, "tick_log", lambda pid, event, **data: logs.append(
        {"project": pid, "event": event, **data}))
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    world = SimpleNamespace(db=db, store=store, attempts=attempts, sf=sf, source=source,
        head=head, scheduler=scheduler, logs=logs,
        service=StateService(db, ws, sf, {}, project_read_trusted=True))
    yield world
    sf._conn.close()

def _reserve(w, request):
    a = w.attempts.reserve("owner", "a", 1, "launch_fixture", request)
    w.db.ensure_project(a["execution_project_id"], name=request,
        repo_type="existing", repo_path=str(w.source), config_name="launch_fixture")
    return a

def test_real_failed_base_reconciles_and_explicit_new_attempt_launches(launch_world):
    w = launch_world
    a = _reserve(w, "bad-base")
    pid = a["execution_project_id"]
    # Reproduce the historical registration status through the host API.
    w.db.update_project(pid, status="pending")
    ri.request_base(w.db, pid, "f" * 40, "unavailable source commit")
    assert w.scheduler._get_or_create_skillflow_run(pid) is None
    run = w.sf._conn.execute("SELECT * FROM skillflow_runs WHERE project_id=?", (pid,)).fetchone()
    rid = run["id"]
    assert w.db.get_project(pid)["status"] == "pending"
    assert not w.db.get_active_projects(limit=10)
    w.attempts.bind_run(a["attempt_id"], rid, w.sf)
    observed = w.attempts.reconcile(a["attempt_id"], w.sf)
    print(json.dumps({"first_run": w.sf.get_run(rid), "state": observed,
        "steps": w.sf.get_steps(rid), "logs": w.logs}, default=str))
    assert observed["status"] == "failed", "baseline strands State running over pending run"
    assert w.sf.get_run(rid)["started_at"] is None
    assert "f" * 40 in observed["error"] and str(w.source) in observed["error"]
    assert all(s["claim_epoch"] == 0 and s["claimed_at"] is None for s in w.sf.get_steps(rid))
    assert ri.record(w.db, rid) is None
    assert w.service.disposition_failed_attempt(a["attempt_id"], "leave-stopped")["disposition"] == "leave-stopped"
    assert w.scheduler._get_or_create_skillflow_run(pid) is None
    assert w.sf._conn.execute("SELECT COUNT(*) FROM skillflow_runs WHERE project_id=?", (pid,)).fetchone()[0] == 1
    b = _reserve(w, "explicit-corrected-new-attempt")
    ri.request_base(w.db, b["execution_project_id"], w.head, "corrected source")
    new = w.scheduler._get_or_create_skillflow_run(b["execution_project_id"])
    w.attempts.bind_run(b["attempt_id"], new, w.sf)
    assert w.attempts.reconcile(b["attempt_id"], w.sf)["status"] == "running"
    rec = ri.record(w.db, new)
    assert rec["base_sha"] == w.head and Path(rec["worktree_path"]).is_dir()
    assert Path(rec["worktree_path"]) != w.source
    assert w.scheduler._get_or_create_skillflow_run(b["execution_project_id"]) == new
    assert w.sf.get_run(rid)["error_reason"] == observed["error"]
    assert w.sf.get_run(rid)["status"] == "failed"
    assert w.sf.get_steps(new)[0]["claim_epoch"] == 0

def test_existing_pending_healthy_launch_is_idempotent(launch_world):
    w = launch_world
    a = _reserve(w, "healthy")
    pid = a["execution_project_id"]
    rid = w.sf.create_run("launch_fixture", project_id=pid)
    assert w.scheduler._get_or_create_skillflow_run(pid) == rid
    rec = ri.record(w.db, rid)
    assert w.sf.get_run(rid)["status"] == "running"
    assert w.scheduler._get_or_create_skillflow_run(pid) == rid
    assert ri.record(w.db, rid) == rec

@pytest.mark.parametrize("case", ["foreign-project", "foreign-graph", "missing", "running",
    "paused", "failed", "completed", "pending-started", "pending-claimed", "pending-operation",
    "unknown-operation", "audit-error", "incomplete-audit"])
def test_failure_guard_retains_runs_it_does_not_own_untouched(launch_world, monkeypatch, case):
    w = launch_world
    rid = w.sf.create_run("launch_fixture", project_id="owned")
    if case in {"running", "paused", "failed", "completed"}:
        w.sf.start_run(rid)
        if case == "paused":
            w.sf.pause_run(rid)
        elif case == "failed":
            w.sf.fail_run(rid, "prior failure")
        elif case == "completed":
            with w.sf._tx() as conn:
                conn.execute("UPDATE skillflow_runs SET status='completed' WHERE id=?", (rid,))
    elif case == "pending-started":
        with w.sf._tx() as conn:
            conn.execute("UPDATE skillflow_runs SET started_at=datetime('now') WHERE id=?", (rid,))
    elif case == "pending-claimed":
        with w.sf._tx() as conn:
            conn.execute("UPDATE skillflow_steps SET claim_epoch=1 WHERE run_id=?", (rid,))
    elif case in {"pending-operation", "unknown-operation"}:
        w.sf._admit_op("owned-review-placeholder", rid, detail="no effect executed")
        if case == "unknown-operation":
            with w.sf._tx() as conn:
                conn.execute("UPDATE skillflow_active_ops SET owner='unknown-fixture' WHERE run_id=?", (rid,))
    elif case == "audit-error":
        monkeypatch.setattr(w.sf, "audit_operation_owners", lambda _: (_ for _ in ()).throw(RuntimeError("unreadable")))
    elif case == "incomplete-audit":
        monkeypatch.setattr(w.sf, "audit_operation_owners", lambda _: {})
    before = w.sf.get_run(rid)
    if case == "audit-error":
        # The launch caller catches this observation failure; no mutation occurs.
        with pytest.raises(RuntimeError, match="unreadable"):
            w.scheduler._fail_unstarted_isolation_run(w.sf, rid, "owned",
                                                       "launch_fixture", "new isolation cause")
    else:
        assert not w.scheduler._fail_unstarted_isolation_run(w.sf,
            "absent" if case == "missing" else rid,
            "foreign" if case == "foreign-project" else "owned",
            "foreign" if case == "foreign-graph" else "launch_fixture", "new isolation cause")
    assert w.sf.get_run(rid) == before



def test_full_cause_is_retained_and_late_observation_cannot_fail_run(launch_world, monkeypatch):
    w = launch_world
    rid = w.sf.create_run("launch_fixture", project_id="owned")
    cause = "IsolationUnavailable: " + "retained-cause/" * 80
    assert w.scheduler._fail_unstarted_isolation_run(w.sf, rid, "owned", "launch_fixture", cause)
    assert w.sf.get_run(rid)["error_reason"] == cause
    late = w.sf.create_run("launch_fixture", project_id="owned")
    audit = w.sf.audit_operation_owners
    def concurrent_start(run_id):
        result = audit(run_id)
        w.sf.start_run(run_id)
        return result
    monkeypatch.setattr(w.sf, "audit_operation_owners", concurrent_start)
    assert not w.scheduler._fail_unstarted_isolation_run(w.sf, late, "owned", "launch_fixture", cause)
    assert w.sf.get_run(late)["status"] == "running"
    assert w.sf.get_run(late)["error_reason"] is None


@pytest.mark.parametrize("status", ["running", "paused", "failed", "completed"])
def test_existing_launch_status_does_not_reprovision_or_refail(launch_world, monkeypatch, status):
    w = launch_world
    a = _reserve(w, "existing-" + status)
    pid = a["execution_project_id"]
    rid = w.sf.create_run("launch_fixture", project_id=pid)
    w.sf.start_run(rid)
    if status == "paused":
        w.sf.pause_run(rid)
    elif status == "failed":
        w.sf.fail_run(rid, "preserved original cause")
    elif status == "completed":
        with w.sf._tx() as conn:
            conn.execute("UPDATE skillflow_runs SET status='completed' WHERE id=?", (rid,))
    before = w.sf.get_run(rid)
    monkeypatch.setattr(w.scheduler.run_isolation, "ensure_for_run",
        lambda *a, **k: pytest.fail("existing status must not be reprovisioned"))
    assert w.scheduler._get_or_create_skillflow_run(pid) == (rid if status in {"running", "paused"} else None)
    assert w.sf.get_run(rid) == before


def test_audit_observation_error_on_real_launch_retains_pending_owner(launch_world, monkeypatch):
    w = launch_world
    a = _reserve(w, "audit-unavailable")
    pid = a["execution_project_id"]
    ri.request_base(w.db, pid, "f" * 40)
    monkeypatch.setattr(w.sf, "audit_operation_owners",
        lambda _: (_ for _ in ()).throw(RuntimeError("operation observation unavailable")))
    assert w.scheduler._get_or_create_skillflow_run(pid) is None
    rid = w.sf._conn.execute("SELECT id FROM skillflow_runs WHERE project_id=?", (pid,)).fetchone()[0]
    assert w.sf.get_run(rid)["status"] == "pending"
    assert w.sf.get_run(rid)["cancel_requested_at"] is None
    assert "ffffffff" in w.logs[-1]["reason"]
