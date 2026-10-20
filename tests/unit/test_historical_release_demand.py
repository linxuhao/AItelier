"""Normal authenticated entry; real temporary Git, SQLite and ordinary worker."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import admin_routers, authz, dependencies
from core import datadir, run_isolation as ri, run_resources as resources
from core.semantic_index_control import deployment_admission, service_lock
from tests.unit.test_run_index_resources import rig as rig, git

AUTH = {"X-AItelier-Admin-Token": "owned-offline-admin"}


@pytest.fixture
def entry(rig, monkeypatch):
    monkeypatch.setattr(dependencies, "_skillflow_instance", rig.sf)
    monkeypatch.setattr(dependencies, "db_instance", rig.db)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", AUTH["X-AItelier-Admin-Token"])
    monkeypatch.setattr(authz, "driver_registry", lambda: None)
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    app = FastAPI()
    app.include_router(admin_routers.router)
    return rig, TestClient(app)


def historical(rig, monkeypatch, pid="historical"):
    # Normal activation did not adopt this old terminal root; no ledger row.
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")
    rec = rig.run(pid)
    rig.sf.stop_run(rec["run_id"], "owned historical fixture terminal")
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "1")
    assert rig.ctl.get(rec["run_id"]) is None
    return rec


def url(rec):
    return f"/api/admin/runs/{rec['run_id']}/semantic-resource"


def request(client, rec):
    response = client.post(url(rec) + "/release", headers=AUTH)
    print("NORMAL_ENTRY", response.status_code, response.text)
    return response


@pytest.mark.parametrize("absent_database", [False, True])
def test_normal_entry_historical_release_is_pending_until_useful_worker_ack(entry, monkeypatch, absent_database):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    other = historical(rig, monkeypatch, "unrelated-history")
    root = Path(rec["worktree_path"])
    cache = root / ".zvec-grep"
    cache.mkdir()
    manifest = cache / "manifest.json"
    manifest.write_bytes(b"owned derived semantic fixture\n" * 128)
    useful_bytes = manifest.stat().st_size
    (root / "unmerged-evidence.txt").write_text("preserve useful work")
    branch = git(root, "rev-parse", "HEAD")
    if absent_database:
        rig.ctl.database.unlink()  # empty fixture ledger, representing pre-ledger history
    resources.reconcile(rig.db, rig.sf)
    assert not rig.ctl.database.exists() if absent_database else rig.ctl.get(rec["run_id"]) is None
    assert not rig.ctl.database.exists() if absent_database else rig.ctl.get(other["run_id"]) is None
    before_rec = ri.record(rig.db, rec["run_id"])
    # Existing initialized instances are the only allowed management composition.
    def forbidden():
        raise AssertionError("second application/SDK composition root")
    monkeypatch.setattr(dependencies, "get_skillflow", forbidden)
    monkeypatch.setattr(dependencies, "get_db_manager", forbidden)
    before = client.get(url(rec), headers=AUTH)
    assert before.status_code == 200, before.text
    assert before.json()["ledger"] is None and before.json()["release_acknowledged"] is False
    assert not rig.ctl.database.exists() if absent_database else rig.ctl.get(rec["run_id"]) is None
    queued = request(client, rec)
    assert queued.status_code == 202, queued.text
    value = queued.json()
    assert value["run_id"] == rec["run_id"] and value["root"] == str(root)
    assert value["ledger"]["desired"] == "released" and value["ledger"]["outcome"] == "pending"
    assert value["release_acknowledged"] is False and manifest.stat().st_size == useful_bytes
    assert rig.ctl.get(other["run_id"]) is None
    # The ordinary generation-fenced worker executes a real child that removes
    # only this fixture's derived bytes. No live index service is contacted.
    commands = []
    def execute(command, *, timeout):
        assert command == ["zg", "index", str(root), "--drop", "--yes", "--mode", "server"]
        commands.append(command)
        child = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; p=Path(sys.argv[1]); n=p.stat().st_size; p.unlink(); print(n)", str(manifest)], check=True, capture_output=True, text=True, timeout=timeout)
        assert int(child.stdout) == useful_bytes
        print("USEFUL_WORKER", json.dumps({"root": str(root), "bytes_removed": useful_bytes, "child_exit": child.returncode, "command": command}))
    with service_lock(rig.ctl.directory, "worker.lock"), deployment_admission(rig.ctl.directory), service_lock(rig.ctl.directory, "operation.lock"):
        result = rig.ctl.process_once(execute=execute)
    assert result == [{"run_id": rec["run_id"], "outcome": "released"}] and len(commands) == 1
    after = client.get(url(rec), headers=AUTH).json()
    assert after["release_acknowledged"] is True
    assert after["ledger"]["done_revision"] == value["ledger"]["revision"]
    assert not cache.exists() and (root / "unmerged-evidence.txt").read_text() == "preserve useful work"
    assert git(root, "rev-parse", "HEAD") == branch and root.exists()
    # Demand does not grant discard, release checkout leases or mutate isolation.
    assert ri.record(rig.db, rec["run_id"]) == before_rec
    repeat = request(client, rec)
    assert repeat.status_code == 202 and repeat.json()["ledger"] == after["ledger"]
    assert repeat.json()["release_acknowledged"] is True
    assert rig.ctl.process_once(execute=execute) == []


@pytest.mark.parametrize("headers", [{}, {"X-AItelier-Admin-Token": "bad"}, {**AUTH, "Cf-Ray": "tunnel"}])
def test_both_normal_entries_require_original_local_admin(entry, monkeypatch, headers):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    assert client.get(url(rec), headers=headers).status_code == 403
    assert client.post(url(rec) + "/release", headers=headers).status_code == 403
    assert rig.ctl.get(rec["run_id"]) is None


@pytest.mark.parametrize("missing", ["sf", "db"])
def test_uninitialized_entry_refuses_without_getter(entry, monkeypatch, missing):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    monkeypatch.setattr(dependencies, "_skillflow_instance" if missing == "sf" else "db_instance", None)
    assert request(client, rec).status_code == 503
    assert rig.ctl.get(rec["run_id"]) is None


@pytest.mark.parametrize("fault", ["running", "paused", "unknown", "engine-error", "audit-malformed", "audit-unknown", "audit-lost", "audit-alive", "foreign-project", "foreign-config", "foreign-source", "path", "symlink", "write-admission", "disabled", "reaped"])
def test_explicit_release_retains_uncertain_or_foreign_resources(entry, monkeypatch, fault, tmp_path):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    old = rig.sf.get_run
    if fault in {"running", "paused"}:
        monkeypatch.setattr(rig.sf, "get_run", lambda rid: {**old(rid), "status": fault})
    elif fault == "unknown":
        monkeypatch.setattr(rig.sf, "get_run", lambda _: None)
    elif fault == "engine-error":
        def error(_): raise OSError("owned failed observation")
        monkeypatch.setattr(rig.sf, "get_run", error)
    elif fault.startswith("audit-"):
        audit = {"alive": 0, "lost": [], "unknown": []}
        audit.update({"malformed": {"alive": True}, "unknown": {"unknown": ["unsettled"]}, "lost": {"lost": ["not-cleared"]}, "alive": {"alive": 1}}[fault[6:]])
        monkeypatch.setattr(rig.sf, "audit_operation_owners", lambda _: audit)
    elif fault in {"foreign-project", "foreign-config", "foreign-source", "path", "reaped"}:
        field, value = {"foreign-project": ("project_id", "foreign"), "foreign-config": ("config_name", "foreign"), "foreign-source": ("source_repo", str(tmp_path)), "path": ("worktree_path", str(rig.source)), "reaped": ("disposition", "reaped_discarded")}[fault]
        with rig.db.get_connection() as conn:
            conn.execute("UPDATE run_isolation SET " + field + "=? WHERE run_id=?", (value, rec["run_id"]))
            conn.commit()
        assert ri.record(rig.db, rec["run_id"])[field] == value
    elif fault == "symlink":
        outside = tmp_path / "foreign-cache"
        outside.mkdir()
        (Path(rec["worktree_path"]) / ".zvec-grep").symlink_to(outside, target_is_directory=True)
    elif fault == "write-admission":
        with ri.write_admission(rig.db, rec["worktree_path"], kind="owned fixture"):
            assert request(client, rec).status_code == 409
        assert rig.ctl.get(rec["run_id"]) is None
        return
    else:
        monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")
    response = request(client, rec)
    assert response.status_code == 409, response.text
    assert rig.ctl.get(rec["run_id"]) is None
    assert Path(rec["worktree_path"]).exists()


def test_existing_ledger_error_is_not_acknowledged_or_reset(entry, monkeypatch):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    assert request(client, rec).status_code == 202
    def fail(*_, **__): raise OSError("controlled provider failure")
    assert rig.ctl.process_once(execute=fail)[0]["outcome"] == "error"
    before = rig.ctl.get(rec["run_id"])
    value = request(client, rec).json()
    assert value["ledger"] == before and value["release_acknowledged"] is False
    assert "controlled provider failure" in value["ledger"]["error"]


def test_resource_lock_and_ledger_ownership_remain_authoritative(entry, monkeypatch):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    with rig.ctl.lock(rec["run_id"]):
        assert request(client, rec).status_code == 409
    assert rig.ctl.get(rec["run_id"]) is None
    other = historical(rig, monkeypatch, "other")
    rig.ctl.request(other, "released")
    # A conflicting durable ledger owner cannot be rebound by management.
    with rig.ctl.connection() as conn:
        conn.execute("UPDATE indexes SET run_id=? WHERE run_id=?", (rec["run_id"], other["run_id"]))
    before = rig.ctl.get(rec["run_id"])
    assert request(client, rec).status_code == 409
    assert rig.ctl.get(rec["run_id"]) == before


def test_deployment_admission_vetoes_explicit_demand(entry, monkeypatch):
    from core import deployment_quiescence as dq
    rig, client = entry
    rec = historical(rig, monkeypatch)
    fence = dq.acquire_cutover_fence()
    try:
        assert request(client, rec).status_code == 409
    finally:
        dq.release_cutover_fence(fence)
    assert rig.ctl.get(rec["run_id"]) is None


def test_terminal_audit_is_rechecked_after_resource_lock(entry, monkeypatch):
    rig, client = entry
    rec = historical(rig, monkeypatch)
    old = rig.sf.get_run
    observed = []
    def changing(rid):
        observed.append(rid)
        return {**old(rid), "status": "paused"} if len(observed) > 1 else old(rid)
    monkeypatch.setattr(rig.sf, "get_run", changing)
    assert request(client, rec).status_code == 409
    assert len(observed) == 2 and rig.ctl.get(rec["run_id"]) is None
