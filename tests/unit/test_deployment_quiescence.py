"""Deployment changes must observe every project and preserve failed evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core import deployment_quiescence as dq


class FakeSkillFlow:
    def __init__(self, runs, audits):
        self.runs = runs
        self.audits = audits

    def list_runs(self):
        return list(self.runs)

    def audit_operation_owners(self, run_id):
        return self.audits[run_id]


class FakeDB:
    def __init__(self, path):
        self.path = path

    def get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _db(path, *, lease=False, admission=False):
    conn = sqlite3.connect(path)
    if lease:
        conn.execute("CREATE TABLE checkout_leases (run_id TEXT, canonical_checkout TEXT)")
        conn.execute("INSERT INTO checkout_leases VALUES ('run-a','/repo-a')")
    if admission:
        conn.execute("CREATE TABLE checkout_write_admissions (id INTEGER, owner TEXT)")
        conn.execute("INSERT INTO checkout_write_admissions VALUES (1,'worker-a')")
    conn.commit()
    conn.close()
    return FakeDB(path)


def _sidecar(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE indexes (run_id TEXT PRIMARY KEY, root TEXT, source TEXT, "
                 "desired TEXT, revision INTEGER, done_revision INTEGER, outcome TEXT, "
                 "error TEXT, activity_at REAL)")
    for row in rows:
        conn.execute("INSERT INTO indexes VALUES (?,?,?,?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()


def _quiet_sf():
    return FakeSkillFlow(
        [{"id": "run-a", "project_id": "project-a", "status": "completed"},
         {"id": "run-b", "project_id": "project-b", "status": "failed"}],
        {"run-a": {"lost": [], "unknown": [], "alive": 0},
         "run-b": {"lost": [], "unknown": [], "alive": 0}},
    )


def _quiet_probe():
    return [{"kind": "docker", "name": "zvec-grep", "active": False}]


def test_measurement_is_cross_project_and_includes_external_sidecar_owners(tmp_path):
    db = _db(tmp_path / "app.sqlite", lease=True, admission=True)
    sidecar_path = tmp_path / "control.sqlite3"
    _sidecar(sidecar_path, [
        ("run-a", "/worktrees/run-a", "/repo-a", "ready", 2, 1, "pending", "", 1.0),
        ("run-b", "/worktrees/run-b", "/repo-b", "released", 3, 3, "released", "", 1.0),
    ])
    sf = FakeSkillFlow(
        [{"id": "run-a", "project_id": "project-a", "status": "running"},
         {"id": "run-b", "project_id": "project-b", "status": "paused"}],
        {"run-a": {"lost": [], "unknown": [], "alive": 1},
         "run-b": {"lost": [], "unknown": [], "alive": 0}},
    )

    def active_probe():
        return [{"kind": "worker", "active": True}]

    observed = dq.measure(skillflow=sf, db=db, sidecar_db=sidecar_path,
                          external_probe=active_probe)

    assert observed["projects"] == ["project-a", "project-b"]
    assert {r["run_id"] for r in observed["blockers"]["active_runs"]} == {"run-a", "run-b"}
    assert len(observed["blockers"]["checkout_leases"]) == 2
    assert observed["blockers"]["checkout_leases"][0]["run_id"] == "run-a"
    assert observed["blockers"]["sidecar_owners"][0]["run_id"] == "run-a"
    assert observed["blockers"]["external_active"] == [{"kind": "worker", "active": True}]
    assert observed["quiescent"] is False


def test_quiet_measurement_requires_all_boundaries_to_settle(tmp_path):
    db = _db(tmp_path / "app.sqlite")
    sidecar_path = tmp_path / "control.sqlite3"
    _sidecar(sidecar_path, [
        ("run-a", "/worktrees/run-a", "/repo-a", "released", 2, 2, "released", "", 1.0),
    ])
    observed = dq.measure(skillflow=_quiet_sf(), db=db, sidecar_db=sidecar_path,
                          external_probe=_quiet_probe)
    assert observed["projects"] == ["project-a", "project-b"]
    assert observed["blockers"] == {
        "active_runs": [], "active_operations": [], "checkout_leases": [],
        "sidecar_owners": [], "external_active": [],
        "registered_external_owners": [],
    }
    assert observed["errors"] == []
    assert observed["quiescent"] is True


def test_non_quiet_gate_persists_aborted_unusable_evidence(tmp_path):
    journal = tmp_path / "journal.json"
    observation = {
        "quiescent": False,
        "digest": "a" * 64,
        "blockers": {"active_runs": [{"run_id": "run-a"}]},
        "errors": [],
    }
    with pytest.raises(dq.DeploymentBlocked, match="aborted/unusable"):
        dq.authorize("restart", observation, journal=journal)
    data = json.loads(journal.read_text())
    assert data["latest"]["status"] == "aborted"
    assert data["latest"]["usable"] is False
    assert data["latest"]["inventory_digest"] == "a" * 64


def test_override_is_audited_and_bound_to_fresh_inventory(tmp_path):
    journal = tmp_path / "journal.json"
    observation = {"quiescent": False, "digest": "b" * 64,
                   "blockers": {"active_runs": [{"run_id": "run-a"}]}, "errors": []}
    base = {"action": "restart", "actor": "operator@example", "reason": "urgent fix",
            "ticket": "INC-42", "inventory_digest": "b" * 64,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    result = dq.authorize("restart", observation, journal=journal, override=base)
    assert result["allowed"] is True and result["replayed"] is False
    assert result["event"]["status"] == "overridden"
    assert result["event"]["usable"] is False
    assert result["event"]["audit"]["override_scope"] == "authoritative_blockers"
    assert result["event"]["audit"]["affected_ownership"] == observation["blockers"]

    with pytest.raises(dq.DeploymentBlocked, match="aborted/unusable"):
        dq.authorize("restart", observation, journal=tmp_path / "bad.json",
                     override={**base, "inventory_digest": "c" * 64})


def _deployment_override(digest, **extra):
    return {
        "action": "restart",
        "actor": "operator@example",
        "reason": "incident-scoped deployment",
        "ticket": "INC-20260914",
        "inventory_digest": digest,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        **extra,
    }


def _unknown_process(index):
    return {
        "kind": "process",
        "command": f"{index + 4} 2 [kworker/{index}:0H-events_highpri]",
        "active": True,
        "resource": "external_measurement",
        "ownership": "unregistered",
    }


def test_incident_unknown_ack_cannot_cross_real_owner_amid_147_noise(tmp_path):
    noise = [_unknown_process(index) for index in range(147)]
    observation = {
        "quiescent": False,
        "digest": "7" * 64,
        "blockers": {
            "active_runs": [],
            "active_operations": [],
            "checkout_leases": [],
            "sidecar_owners": [],
            "external_active": noise,
            "registered_external_owners": [],
            "godot_render_owners": [{
                "generation": 35,
                "owner_id": "owner-35",
                "project_id": "two-step-r1b",
                "run_id": "unknown-run",
                "operation_id": "operation-35",
                "resource": "render",
                "status": "active",
            }],
        },
        "errors": [
            "unregistered external measurement process has unknown ownership: "
            + row["command"] for row in noise
        ],
    }

    with pytest.raises(dq.DeploymentBlocked, match="authoritative"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["blockers"]["godot_render_owners"][0]["owner_id"] == "owner-35"


def test_unknown_ack_cannot_cross_known_owner_without_noise(tmp_path):
    observation = {
        "quiescent": False,
        "digest": "8" * 64,
        "blockers": {
            "registered_external_owners": [{
                "attempt_id": "attempt-real",
                "project_id": "wuxia-myth",
                "external_id": "real-worker",
                "status": "active",
            }],
        },
        "errors": [],
    }

    with pytest.raises(dq.DeploymentBlocked, match="authoritative"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )


def test_noise_only_unknown_ack_remains_explicit_and_audited(tmp_path):
    noise = [_unknown_process(index) for index in range(3)]
    observation = {
        "quiescent": False,
        "digest": "9" * 64,
        "blockers": {"external_active": noise},
        "errors": [
            "unregistered external measurement process has unknown ownership: "
            + row["command"] for row in noise
        ],
    }
    result = dq.authorize(
        "restart", observation, journal=tmp_path / "journal.json",
        override=_deployment_override(
            observation["digest"], acknowledge_unknown=True),
    )

    assert result["allowed"] is True
    assert result["event"]["status"] == "overridden"
    assert result["event"]["audit"]["override_scope"] == "unknown_process_noise"
    assert result["event"]["audit"]["affected_ownership"] == noise


def test_clean_no_owner_path_does_not_need_an_override(tmp_path):
    observation = {
        "quiescent": True,
        "digest": "0" * 64,
        "blockers": {
            "active_runs": [],
            "active_operations": [],
            "checkout_leases": [],
            "sidecar_owners": [],
            "external_active": [],
            "registered_external_owners": [],
            "godot_render_owners": [],
        },
        "errors": [],
    }
    result = dq.authorize(
        "restart", observation, journal=tmp_path / "journal.json")

    assert result["allowed"] is True
    assert result["event"]["status"] == "authorized"


@pytest.mark.parametrize(("blocker_key", "owner"), [
    ("active_runs", {"run_id": "run-a", "project_id": "project-a",
                     "status": "running"}),
    ("active_operations", {"run_id": "run-a", "project_id": "project-a",
                           "active_operations": 1}),
    ("registered_external_owners", {"attempt_id": "attempt-a",
                                    "project_id": "project-a", "status": "active"}),
    ("godot_render_owners", {"owner_id": "render-a", "operation_id": "operation-a",
                             "project_id": "project-a", "status": "active"}),
    ("external_active", {"kind": "process", "command": "godot --headless",
                         "active": True, "resource": "render"}),
    ("sidecar_owners", {"run_id": "run-a", "source": "/repo-a",
                        "desired": "ready", "outcome": "pending"}),
])
def test_unknown_ack_cannot_cross_each_authoritative_owner_variant(
        tmp_path, blocker_key, owner):
    observation = {
        "quiescent": False,
        "digest": "a" * 64,
        "blockers": {blocker_key: [owner]},
        "errors": [],
    }

    with pytest.raises(dq.DeploymentBlocked, match="authoritative"):
        dq.authorize(
            "restart", observation, journal=tmp_path / (blocker_key + ".json"),
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )


def test_reconciliation_never_replays_before_or_after_quiescence(tmp_path):
    journal = tmp_path / "journal.json"
    blocked = {"quiescent": False, "digest": "d" * 64,
               "blockers": {"active_runs": [{"run_id": "run-a"}]}, "errors": []}
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("redeploy", blocked, journal=journal)
    still_blocked = dq.reconcile(observation=blocked, journal=journal)
    assert still_blocked["reconciled"] is False
    assert still_blocked["replayed"] is False
    quiet = {"quiescent": True, "digest": "e" * 64, "blockers": {}, "errors": []}
    settled = dq.reconcile(observation=quiet, journal=journal)
    assert settled == {"reconciled": True, "replayed": False,
                       "event": settled["event"]}
    data = json.loads(journal.read_text())
    assert data["events"][0]["status"] == "aborted"
    assert data["events"][-1]["status"] == "reconciled_quiescent"
    assert not any(e.get("replayed") is True for e in data["events"])


def test_corrupt_journal_fails_closed(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text("{}")
    with pytest.raises(dq.DeploymentBlocked, match="malformed"):
        dq.authorize("restart", {"quiescent": True, "digest": "f" * 64,
                                  "blockers": {}, "errors": []}, journal=journal)
    backups = list(tmp_path.glob("journal.json.corrupt.*"))
    assert backups and backups[0].read_bytes() == b"{}"
    recovered = json.loads(journal.read_text())
    assert recovered["latest"]["status"] == "aborted"
    assert recovered["latest"]["usable"] is False


def test_malformed_override_is_aborted_and_unusable(tmp_path):
    override = tmp_path / "override.json"
    override.write_text("not json")
    assert dq.load_override(override)["_invalid_override"]
    journal = tmp_path / "journal.json"
    observation = {"quiescent": False, "digest": "1" * 64,
                   "blockers": {"active_runs": [{"run_id": "run-a"}]}, "errors": []}
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("restart", observation, journal=journal,
                     override=dq.load_override(override))
    assert json.loads(journal.read_text())["latest"]["usable"] is False


def test_unknown_run_status_and_sidecar_symlink_fail_closed(tmp_path):
    sf = FakeSkillFlow([{"id": "run-a", "project_id": "p", "status": "mystery"}],
                       {"run-a": {"lost": [], "unknown": [], "alive": 0}})
    sidecar = tmp_path / "sidecar.sqlite3"
    sidecar.symlink_to(tmp_path / "missing.sqlite3")
    observed = dq.measure(skillflow=sf, sidecar_db=sidecar,
                          external_probe=list)
    assert observed["quiescent"] is False
    assert any("unknown status" in error for error in observed["errors"])
    assert any("symlink" in error for error in observed["errors"])


def test_external_inventory_uses_real_command_shape():
    calls = []

    def runner(command):
        calls.append(command)
        if command[0] == "docker":
            return SimpleNamespace(returncode=0,
                                   stdout="cid\taitelier-zvec\tzvec-grep\tUp 2 minutes\n",
                                   stderr="")
        return SimpleNamespace(returncode=0,
                               stdout=" 42  1 zvec-grep-worker --serve\n",
                               stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert errors == []
    assert any(row["kind"] == "docker" and row["service"] == "zvec-grep" for row in owners)
    assert any(row["kind"] == "process" for row in owners)
    assert calls[0][:2] == ["docker", "ps"]


def test_active_godot_process_blocks_without_semantic_index_ledger(tmp_path):
    sf = _quiet_sf()
    calls = []

    def runner(command):
        calls.append(command)
        if command[0] == "docker":
            return SimpleNamespace(returncode=0,
                                   stdout="cid\taitelier-godot\tgodot-builder\tUp 2 minutes\n",
                                   stderr="")
        return SimpleNamespace(returncode=0,
                               stdout=" 42  1 godot --path /tmp/fixture --editor --headless\n",
                               stderr="")

    observed = dq.measure(skillflow=sf, sidecar_db=tmp_path / "missing.sqlite3",
                          command_runner=runner)
    assert observed["quiescent"] is False
    assert observed["blockers"]["external_active"][0]["resource"] == "render"
    assert any(row["kind"] == "process" and row["active"] for row in observed["external_owners"])


def test_authorized_action_is_aborted_when_compose_fails(tmp_path):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize("restart", {"quiescent": True, "digest": "a" * 64,
                                           "blockers": {}, "errors": []}, journal=journal)
    assert clearance["event"]["pending"] is True
    result = dq.finalize(clearance, success=False, error="compose failed", journal=journal)
    assert result["event"]["status"] == "aborted"
    data = json.loads(journal.read_text())
    assert data["latest"]["usable"] is False
    assert data["latest"]["reason"] == "compose failed"


def test_pending_authorization_is_not_replayed_by_reconcile(tmp_path):
    journal = tmp_path / "journal.json"
    dq.authorize("restart", {"quiescent": True, "digest": "a" * 64,
                              "blockers": {}, "errors": []}, journal=journal)
    result = dq.reconcile(observation={"quiescent": True, "digest": "b" * 64,
                                       "blockers": {}, "errors": []}, journal=journal)
    assert result["replayed"] is False and result["reconciled"] is False
    assert json.loads(journal.read_text())["latest"]["status"] == "aborted"


def test_new_authorization_explicitly_aborts_prior_pending_event(tmp_path):
    journal = tmp_path / "journal.json"
    observation = {"quiescent": True, "digest": "a" * 64,
                   "blockers": {}, "errors": []}
    first = dq.authorize("restart", observation, journal=journal)
    second = dq.authorize("restart", observation, journal=journal)
    events = json.loads(journal.read_text())["events"]
    assert events[-2]["status"] == "aborted"
    assert events[-2]["prior_event_id"] == first["event"]["event_id"]
    assert events[-2]["reason"] == "deployment authorization was interrupted"
    assert second["event"]["status"] == "authorized"


def test_registered_external_attempt_is_a_durable_blocker_and_shares_fence(tmp_path, monkeypatch):
    from core.db_manager import DBManager
    from core.state_attempts import StateAttempts
    from core.state_external import ExternalAttempts
    from core.state_graph import StateGraphStore

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.sqlite3"))
    store = StateGraphStore(db)
    store.create_project("portfolio", "Portfolio")
    store.add_nodes("portfolio", [{
        "key": "external-gate", "goal": "measure externally", "dependencies": [],
        "acceptance": [{"id": "result", "kind": "test", "description": "result"}],
    }])
    external = ExternalAttempts(StateAttempts(store), "director")
    entered = threading.Event()

    def register_external():
        external.register("portfolio", "external-gate", 1, "remote-harness",
                          "worker-1", "request-1")
        entered.set()

    cutover = dq.acquire_cutover_fence()
    worker = threading.Thread(target=register_external)
    worker.start()
    time.sleep(0.05)
    assert not entered.is_set()
    dq.release_cutover_fence(cutover)
    worker.join(timeout=2)
    assert entered.is_set()

    observed = dq.measure(skillflow=_quiet_sf(), db=db, external_probe=list)
    assert observed["quiescent"] is False
    assert observed["blockers"]["registered_external_owners"][0]["external_id"] == "worker-1"
    attempt = external.attempts.list("portfolio", "external-gate")[0]
    report = tmp_path / "external-failure.txt"
    report.write_bytes(
        b'{"status":"failed","settled":true,"usable":true,'
        b'"reason":"worker settled with failure"}')
    external.observe(
        attempt["attempt_id"], "settled", 0, attempt["context_hash"], "failed",
        str(report), hashlib.sha256(report.read_bytes()).hexdigest(), quiescent=True,
        detail="worker settled")
    observed = dq.measure(skillflow=_quiet_sf(), db=db, external_probe=list)
    assert observed["blockers"]["registered_external_owners"] == []


def test_missing_external_owner_row_fails_closed_against_active_attempt(tmp_path, monkeypatch):
    from core.db_manager import DBManager
    from core.state_attempts import StateAttempts
    from core.state_external import ExternalAttempts
    from core.state_graph import StateGraphStore

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.sqlite3"))
    store = StateGraphStore(db)
    store.create_project("portfolio", "Portfolio")
    store.add_nodes("portfolio", [{
        "key": "external-gate", "goal": "measure externally", "dependencies": [],
        "acceptance": [{"id": "result", "kind": "test", "description": "result"}],
    }])
    attempt = ExternalAttempts(StateAttempts(store), "director").register(
        "portfolio", "external-gate", 1, "remote-harness", "worker-1", "request-1")
    with db.get_connection() as conn:
        conn.execute("UPDATE state_external_owners SET harness='wrong-harness' WHERE attempt_id=?",
                     (attempt["attempt_id"],))
        conn.commit()
    mismatched = dq.measure(skillflow=_quiet_sf(), db=db, external_probe=list)
    assert mismatched["quiescent"] is False
    assert any("identity mismatch" in error for error in mismatched["errors"])
    with db.get_connection() as conn:
        conn.execute("UPDATE state_external_owners SET harness='remote-harness' WHERE attempt_id=?",
                     (attempt["attempt_id"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM state_external_owners WHERE attempt_id=?", (attempt["attempt_id"],))
        conn.execute("DROP TRIGGER state_external_owners_no_delete")
        conn.execute("DELETE FROM state_external_owners WHERE attempt_id=?", (attempt["attempt_id"],))
        conn.commit()
    observed = dq.measure(skillflow=_quiet_sf(), db=db, external_probe=list)
    assert observed["quiescent"] is False
    assert any("registry" in error and "missing" in error for error in observed["errors"])
    assert any(row["attempt_id"] == attempt["attempt_id"]
               for row in observed["blockers"]["registered_external_owners"])


def test_generic_external_measurement_process_fails_closed_when_unregistered():
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="4321 1 python3 /tmp/external_measurement_fixture.py --long-gate\n",
            stderr="",
        )

    owners, errors = dq.external_owners(runner=runner)
    assert any(owner.get("resource") == "external_measurement" and owner.get("active")
               for owner in owners)
    assert any("unregistered external measurement" in error for error in errors)


@pytest.mark.parametrize("command_line", [
    "4322 1 python3 /tmp/eval_job.py --duration 600",
    "4323 1 python3 /tmp/judge_fixture.py --duration 600",
    "4324 1 python3 /tmp/metrics_collection.py --duration 600",
])
def test_unregistered_evaluator_judge_and_metrics_processes_fail_closed(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is True
    assert owners[0]["ownership"] == "unregistered"
    assert any("unknown ownership" in error for error in errors)


@pytest.mark.parametrize("command_line", [
    "4326 1 python3 /tmp/grader_worker.py --duration 600",
    "4327 1 python3 /tmp/scoring_job.py --duration 600",
    "4328 1 python3 /tmp/assessment_worker.py --duration 600",
    "4329 1 python3 /tmp/quality_check.py --duration 600",
])
def test_grader_scoring_assessment_and_quality_processes_fail_closed(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is True
    assert owners[0]["ownership"] == "unregistered"
    assert errors


@pytest.mark.parametrize("command_line", [
    "4331 1 python3 /tmp/rater_worker.py --duration 600",
    "4332 1 python3 /tmp/review_worker.py --duration 600",
    "4333 1 python3 /tmp/metric_worker.py --duration 600",
    "4334 1 python3 /tmp/evaluationWorker.py --duration 600",
])
def test_nearby_evaluation_worker_names_fail_closed(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is True
    assert errors


@pytest.mark.parametrize("command_line", [
    "4335 1 python3 /tmp/grader_worker.py --repo /tmp/skillflow-candidate",
    "4336 1 python3 /tmp/rater_worker.py --index /tmp/zvec-grep-results",
])
def test_service_names_in_measurement_arguments_do_not_suppress_unknown_work(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is True
    assert owners[0]["resource"] == "external_measurement"
    assert owners[0]["ownership"] == "unregistered"
    assert errors


def test_registered_external_id_does_not_match_a_longer_token():
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="4330 1 python3 /tmp/evaluator.py --job evaluator-worker-10\n",
            stderr="",
        )

    owners, errors = dq.external_owners(
        runner=runner,
        registered_external_owners=[{
            "attempt_id": "attempt-1", "external_id": "evaluator-worker-1",
            "status": "active",
        }],
    )
    assert owners[0]["ownership"] == "unregistered"
    assert errors


def test_registered_supported_measurement_is_bound_to_its_state_owner():
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="4325 1 python3 /tmp/evaluator_worker.py --job evaluator-worker\n",
            stderr="",
        )

    owners, errors = dq.external_owners(
        runner=runner,
        registered_external_owners=[{
            "attempt_id": "attempt-1", "harness": "evaluator",
            "external_id": "evaluator-worker", "status": "active",
        }],
    )
    assert errors == []
    assert owners[0]["ownership"] == "registered"
    assert owners[0]["attempt_id"] == "attempt-1"


@pytest.mark.parametrize("harness", ["evaluator", "judge", "metrics"])
def test_supported_external_launch_registers_durable_owner_before_work(
        tmp_path, monkeypatch, harness):
    from core.db_manager import DBManager
    from core.state_attempts import StateAttempts
    from core.state_external import ExternalAttempts
    from core.state_graph import StateGraphStore

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / (harness + ".sqlite3")))
    store = StateGraphStore(db)
    store.create_project("portfolio", "Portfolio")
    store.add_nodes("portfolio", [{
        "key": "work", "goal": "external work", "dependencies": [],
        "acceptance": [{"id": "result", "kind": "test",
                        "description": "external result"}],
    }])
    external = ExternalAttempts(StateAttempts(store), "director")
    cutover = dq.acquire_cutover_fence()
    work_started = threading.Event()
    observed = {}

    def launch():
        attempt = external.register(
            "portfolio", "work", 1, harness, harness + "-worker", "request")
        with db.get_connection() as conn:
            observed.update(dict(conn.execute(
                "SELECT * FROM state_external_owners WHERE attempt_id=?",
                (attempt["attempt_id"],)).fetchone()))
        work_started.set()

    worker = threading.Thread(target=launch)
    worker.start()
    time.sleep(0.05)
    assert not work_started.is_set()
    dq.release_cutover_fence(cutover)
    worker.join(timeout=2)
    assert work_started.is_set()
    assert observed["status"] == "active"
    assert observed["harness"] == harness


def test_server_redeploy_gate_runs_before_compose(monkeypatch):
    from cli import server

    events = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(server.httpx, "Client", Client)
    monkeypatch.setattr(server, "_container_running", lambda: False)
    monkeypatch.setattr(server, "_find_server_pid", lambda port: None)
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda action: events.append(("gate", action)) or {})
    monkeypatch.setattr(server, "_compose_up", lambda: events.append(("compose",)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    assert server._ensure_docker_backend("http://localhost:4444", 1) is True
    assert events == [("gate", "redeploy"), ("compose",)]


def test_server_redeploy_never_kills_a_listener_before_gate(monkeypatch):
    from cli import server

    events = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
    monkeypatch.setattr(server.httpx, "Client", Client)
    monkeypatch.setattr(server, "_container_running", lambda: False)
    monkeypatch.setattr(server, "_find_server_pid", lambda port: 4242)
    monkeypatch.setattr(server.os, "kill",
                        lambda *args: events.append(("kill", *args)))
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda action: events.append(("gate", action)) or {})
    monkeypatch.setattr(server, "_compose_up", lambda: events.append(("compose",)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    assert server._ensure_docker_backend("http://localhost:4444", 1) is True
    assert events == [("gate", "redeploy"), ("compose",)]


def test_server_restart_gate_runs_before_restart(monkeypatch):
    from cli import server

    events = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(server.httpx, "Client", Client)
    monkeypatch.setattr(server, "_require_docker", lambda: events.append(("docker",)))
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda action: events.append(("gate", action)) or {})
    monkeypatch.setattr(server, "_compose",
                        lambda *args, **kwargs: (
                            events.append(("compose", *args))
                            or SimpleNamespace(returncode=0)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    assert server.restart_server("http://localhost:4444", 1) is True
    assert events[:3] == [("docker",), ("gate", "restart"),
                          ("compose", "restart", "aitelier")]


def test_server_measurement_failure_persists_aborted_evidence(tmp_path, monkeypatch):
    from api import dependencies
    from cli import server

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))

    def unavailable():
        raise RuntimeError("SkillFlow unavailable")

    monkeypatch.setattr(dependencies, "get_skillflow", unavailable)
    with pytest.raises(RuntimeError, match="aborted/unusable"):
        server._require_deployment_clearance("restart")
    journal = tmp_path / "home" / "deployment-quiescence" / "journal.json"
    assert json.loads(journal.read_text())["latest"]["status"] == "aborted"


def test_resident_godot_sidecar_without_owner_ledger_fails_closed(tmp_path):
    semantic = tmp_path / "semantic-index-control" / "control.sqlite3"
    semantic.parent.mkdir()
    with sqlite3.connect(semantic) as conn:
        conn.execute("CREATE TABLE indexes(run_id,root,source,desired,revision,done_revision,outcome,error,activity_at)")
    observed = dq.measure(
        skillflow=_quiet_sf(), sidecar_db=semantic,
        external_probe=lambda: [{"kind": "docker", "id": "godot-cid",
                                 "name": "aitelier-godot",
                                 "service": "godot-builder", "active": False}])
    assert observed["quiescent"] is False
    assert any("Godot owner ledger is missing" in e for e in observed["errors"])


def test_restart_exit_137_is_aborted_even_if_old_health_still_answers(tmp_path, monkeypatch):
    from cli import server

    events = []
    monkeypatch.setattr(server, "_require_docker", lambda: None)
    monkeypatch.setattr(server, "_require_deployment_clearance", lambda _a: {"event": {"event_id": "gate"}})
    monkeypatch.setattr(server, "_compose", lambda *_a: SimpleNamespace(returncode=137))
    monkeypatch.setattr(server, "_wait_healthy", lambda *_a: True)
    monkeypatch.setattr(server, "_finish_deployment",
                        lambda _c, *, success, error=None: events.append((success, str(error))))
    with pytest.raises(RuntimeError, match="exit 137"):
        server.restart_server("http://localhost:4444", 1)
    assert len(events) == 1 and events[0][0] is False


def test_cutover_fence_blocks_new_operation_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    cutover = dq.acquire_cutover_fence()
    entered = threading.Event()

    def admit():
        with dq.operation_admission_fence():
            entered.set()

    worker = threading.Thread(target=admit)
    worker.start()
    time.sleep(0.05)
    assert not entered.is_set()
    dq.release_cutover_fence(cutover)
    worker.join(timeout=2)
    assert entered.is_set()
