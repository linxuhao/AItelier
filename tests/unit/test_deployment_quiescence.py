"""Deployment changes must observe every project and preserve failed evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core import deployment_quiescence as dq

pytestmark = pytest.mark.usefixtures("resource_authority")


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
    return [{"kind": "docker", "id": "quiet-container",
             "name": "zvec-grep", "active": False}]


def _producer_shaped_observation(**changes):
    observation = {
        "schema_version": dq.OBSERVATION_SCHEMA_VERSION,
        "observed_at": "2026-09-14T00:00:00+00:00",
        "projects": [],
        "runs": [],
        "sidecar_owners": [],
        "godot_render_owners": [],
        "external_owners": [],
        "registered_external_owners": [],
        "blockers": {},
        "errors": [],
        "quiescent": True,
        **changes,
    }
    for row in observation.get("runs", []):
        row.setdefault("active_operations", 0)
        row.setdefault("audit", {
            "lost": [], "unknown": [], "alive": row["active_operations"],
        })
    for row in observation.get("sidecar_owners", []):
        row.setdefault("root", "/worktrees/" + str(row.get("run_id", "unknown")))
        row.setdefault("source", "/repositories/source")
        row.setdefault("revision", 1)
        row.setdefault("done_revision", 0)
        row.setdefault("activity_at", 1.0)
        row.setdefault("error", "")
    observation.pop("digest", None)
    observation["digest"] = dq._digest(observation)
    return observation


def _journal_event(event_id, **changes):
    event = {
        "event_id": event_id,
        "action": "restart",
        "inventory_digest": "f" * 64,
        "status": "authorized",
        "pending": True,
        "usable": False,
        "replayed": False,
    }
    event.update(changes)
    return event


def _write_journal(path, events):
    latest = dict(events[-1]) if events else None
    state = {"version": 2, "journal_id": "0" * 32, "events": events, "latest": latest}
    dq._persist_journal(path, state)
    return path.read_bytes()


def _invoke_journal_operation(operation, journal):
    if operation == "load":
        return dq._load_journal(journal)
    if operation == "authorize":
        return dq.authorize(
            "restart", _producer_shaped_observation(), journal=journal)
    if operation == "reconcile":
        return dq.reconcile(
            observation=_producer_shaped_observation(), journal=journal)
    if operation == "finalize":
        latest = json.loads(journal.read_text())["latest"]
        return dq.finalize({"event": latest}, success=True, journal=journal)
    raise AssertionError(operation)


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
        return [{"kind": "docker", "id": "active-container", "active": True}]

    observed = dq.measure(skillflow=sf, db=db, sidecar_db=sidecar_path,
                          external_probe=active_probe)

    assert observed["projects"] == ["project-a", "project-b"]
    assert {r["run_id"] for r in observed["blockers"]["active_runs"]} == {"run-a", "run-b"}
    assert len(observed["blockers"]["checkout_leases"]) == 2
    assert observed["blockers"]["checkout_leases"][0]["run_id"] == "run-a"
    assert observed["blockers"]["sidecar_owners"][0]["run_id"] == "run-a"
    assert observed["blockers"]["external_active"] == [
        {"kind": "docker", "id": "active-container", "active": True}]
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
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    observation = _producer_shaped_observation(
        quiescent=False,
        runs=[run],
        blockers={"active_runs": [run]},
    )
    with pytest.raises(dq.DeploymentBlocked, match="aborted/unusable"):
        dq.authorize("restart", observation, journal=journal)
    data = json.loads(journal.read_text())
    assert data["latest"]["status"] == "aborted"
    assert data["latest"]["usable"] is False
    assert data["latest"]["inventory_digest"] == observation["digest"]


def test_override_is_audited_and_bound_to_fresh_inventory(tmp_path):
    journal = tmp_path / "journal.json"
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    observation = _producer_shaped_observation(
        quiescent=False, runs=[run], blockers={"active_runs": [run]})
    base = {"action": "restart", "actor": "operator@example", "reason": "urgent fix",
            "ticket": "INC-42", "inventory_digest": observation["digest"],
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


class DigestThenMutate(dict):
    """Reviewer reproduction: mutate the caller mapping after digest iteration."""
    def __init__(self, value):
        super().__init__(value)
        self.arm_after_items = False
        self.armed = False

    def items(self):
        source = list(super().items())
        parent = self

        class Items:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self.index < len(source):
                    item = source[self.index]
                    self.index += 1
                    return item
                if parent.arm_after_items:
                    parent.armed = True
                raise StopIteration

        return Items()

    def get(self, key, default=None):
        if self.armed and key == "runs":
            super().get("blockers", {}).clear()
            super().__setitem__("runs", [])
            super().__setitem__("quiescent", True)
            self.armed = False
        return super().get(key, default)


@pytest.mark.parametrize("active_operations", [-1, False, float("nan")])
def test_reviewer_malformed_operation_counts_fail_closed(tmp_path, active_operations):
    run = {
        "run_id": "run-a", "project_id": "project-a", "status": "completed",
        "active_operations": active_operations,
        "audit": {"lost": [], "unknown": [], "alive": 0},
    }
    observation = _producer_shaped_observation(runs=[run])

    with pytest.raises(dq.DeploymentBlocked, match="malformed"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    assert json.loads((tmp_path / "journal.json").read_text())["latest"]["usable"] is False


@pytest.mark.parametrize("alive", [-1, False, float("nan")])
def test_measure_records_malformed_audit_as_an_error(alive):
    sf = FakeSkillFlow(
        [{"id": "run-a", "project_id": "project-a", "status": "completed"}],
        {"run-a": {"lost": [], "unknown": [], "alive": alive}},
    )

    observed = dq.measure(skillflow=sf, external_probe=list)

    assert observed["quiescent"] is False
    assert any("audit" in error and "malformed" in error
               for error in observed["errors"])


def test_reviewer_non_boolean_external_activity_fails_closed(tmp_path):
    owner = {"kind": "docker", "id": "container-a", "active": 1}
    observation = _producer_shaped_observation(external_owners=[owner])

    with pytest.raises(dq.DeploymentBlocked, match="active must be a boolean"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")


def test_reviewer_unknown_run_status_fails_closed(tmp_path):
    run = {
        "run_id": "run-a", "project_id": "project-a", "status": "future-running",
        "active_operations": 0, "audit": {"lost": [], "unknown": [], "alive": 0},
    }
    observation = _producer_shaped_observation(runs=[run])

    with pytest.raises(dq.DeploymentBlocked, match="unknown status"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")


@pytest.mark.parametrize(("inventory", "row", "reason"), [
    ("sidecar_owners", {
        "run_id": "run-a", "root": "/worktrees/run-a", "source": "/repo",
        "desired": "future", "revision": 1, "done_revision": 1,
        "outcome": "ready", "error": "", "activity_at": 1.0,
    }, "unknown desired state"),
    ("sidecar_owners", {
        "run_id": "run-a", "root": "/worktrees/run-a", "source": "/repo",
        "desired": "ready", "revision": False, "done_revision": 1,
        "outcome": "ready", "error": "", "activity_at": 1.0,
    }, "revision must be a nonnegative integer"),
    ("registered_external_owners", {
        "attempt_id": "attempt-a", "status": "future-active",
    }, "unknown status"),
    ("godot_render_owners", {
        "owner_id": "owner-a", "status": "future-active",
    }, "unknown status"),
    ("godot_render_owners", {
        "owner_id": "owner-a", "status": "released", "generation": float("inf"),
    }, "non-JSON"),
])
def test_every_owner_inventory_rejects_unknown_statuses_and_bad_counts(
        tmp_path, inventory, row, reason):
    observation = _producer_shaped_observation(**{inventory: [row]})

    with pytest.raises(dq.DeploymentBlocked, match=reason):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")


def test_reviewer_future_schema_and_inventory_fail_closed(tmp_path):
    observation = _producer_shaped_observation()
    observation["schema_version"] = 2
    observation["future_owner_inventory"] = [{"owner_id": "future", "active": True}]
    observation["digest"] = dq._observation_digest(observation)

    with pytest.raises(dq.DeploymentBlocked, match="schema_version"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")


def test_reviewer_digest_to_normalization_mutation_is_rejected(tmp_path):
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    base = _producer_shaped_observation(
        runs=[run], blockers={"active_runs": [run]}, quiescent=False)
    mutable = DigestThenMutate(base)
    mutable.arm_after_items = True

    with pytest.raises(dq.DeploymentBlocked, match="non-JSON"):
        dq.authorize("restart", mutable, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted" and event["usable"] is False
    assert event["inventory_digest"] is None


@pytest.mark.parametrize("replacement", [(), {"custom": object()}])
def test_non_json_observations_are_rejected_before_journaling_fields(tmp_path, replacement):
    observation = _producer_shaped_observation()
    observation["runs"] = replacement

    with pytest.raises(dq.DeploymentBlocked, match="non-JSON"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["blockers"] is None and event["errors"] is None


def test_incident_unknown_ack_cannot_cross_real_owner_amid_147_noise(tmp_path):
    noise = [_unknown_process(index) for index in range(147)]
    godot_owner = {
        "generation": 35,
        "owner_id": "owner-35",
        "project_id": "two-step-r1b",
        "run_id": "unknown-run",
        "operation_id": "operation-35",
        "resource": "render",
        "status": "active",
    }
    observation = _producer_shaped_observation(
        quiescent=False,
        external_owners=noise,
        godot_render_owners=[godot_owner],
        blockers={
            "active_runs": [],
            "active_operations": [],
            "checkout_leases": [],
            "sidecar_owners": [],
            "external_active": noise,
            "registered_external_owners": [],
            "godot_render_owners": [godot_owner],
        },
        errors=[
            "unregistered external measurement process has unknown ownership: "
            + row["command"] for row in noise
        ],
    )

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
    owner = {
        "attempt_id": "attempt-real",
        "project_id": "wuxia-myth",
        "external_id": "real-worker",
        "status": "active",
    }
    observation = _producer_shaped_observation(
        quiescent=False,
        registered_external_owners=[owner],
        blockers={"registered_external_owners": [owner]},
    )

    with pytest.raises(dq.DeploymentBlocked, match="authoritative"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )


def test_noise_only_unknown_ack_remains_explicit_and_audited(tmp_path):
    noise = [_unknown_process(index) for index in range(3)]
    observation = _producer_shaped_observation(
        quiescent=False,
        external_owners=noise,
        blockers={"external_active": noise},
        errors=[
            "unregistered external measurement process has unknown ownership: "
            + row["command"] for row in noise
        ],
    )
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
    observation = _producer_shaped_observation(
        blockers={
            "active_runs": [],
            "active_operations": [],
            "checkout_leases": [],
            "sidecar_owners": [],
            "external_active": [],
            "registered_external_owners": [],
            "godot_render_owners": [],
        },
    )
    result = dq.authorize(
        "restart", observation, journal=tmp_path / "journal.json")

    assert result["allowed"] is True
    assert result["event"]["status"] == "authorized"


def test_top_level_active_run_cannot_be_omitted_from_quiescent_blockers(tmp_path):
    observation = _producer_shaped_observation(
        projects=["project-a"],
        runs=[{"run_id": "run-a", "project_id": "project-a",
               "status": "running", "active_operations": 0}],
    )

    with pytest.raises(dq.DeploymentBlocked, match="normalized runs inventory"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False


def test_clean_observation_without_inventory_digest_is_unusable(tmp_path):
    observation = _producer_shaped_observation()
    del observation["digest"]

    with pytest.raises(dq.DeploymentBlocked, match="digest must be"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False


def test_mutated_blockers_cannot_reuse_pre_mutation_inventory_digest(tmp_path):
    observation = _producer_shaped_observation()
    observation["blockers"] = {
        "active_runs": [{"run_id": "run-a", "project_id": "project-a",
                         "status": "running"}],
    }
    observation["quiescent"] = False

    with pytest.raises(dq.DeploymentBlocked, match="digest does not match"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(observation["digest"]),
        )
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False
    assert "audit" not in event


@pytest.mark.parametrize("digest", [None, "A" * 64, "a" * 63, "g" * 64, 123])
def test_malformed_inventory_digest_is_unusable(tmp_path, digest):
    observation = _producer_shaped_observation()
    observation["digest"] = digest

    with pytest.raises(dq.DeploymentBlocked, match="digest must be"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False


@pytest.mark.parametrize("inventory_name", [
    "runs", "sidecar_owners", "external_owners",
    "registered_external_owners", "godot_render_owners",
])
def test_required_top_level_owner_inventory_cannot_be_omitted(
        tmp_path, inventory_name):
    observation = _producer_shaped_observation()
    del observation[inventory_name]
    observation["digest"] = dq._digest(
        {key: value for key, value in observation.items() if key != "digest"})

    with pytest.raises(dq.DeploymentBlocked, match=f"{inventory_name} must be a list"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False


@pytest.mark.parametrize(("inventory_name", "blocker_name", "owner"), [
    ("runs", "active_operations",
     {"run_id": "run-a", "project_id": "project-a", "status": "completed",
      "active_operations": 1}),
    ("sidecar_owners", "sidecar_owners",
     {"run_id": "run-a", "desired": "ready", "outcome": "pending",
      "revision": 2, "done_revision": 1}),
    ("external_owners", "external_active",
     {"kind": "docker", "id": "container-a", "active": True}),
    ("registered_external_owners", "registered_external_owners",
     {"attempt_id": "attempt-a", "status": "active"}),
    ("godot_render_owners", "godot_render_owners",
     {"owner_id": "owner-a", "status": "owner_lost"}),
])
def test_top_level_owner_cannot_be_omitted_from_normalized_blockers(
        tmp_path, inventory_name, blocker_name, owner):
    observation = _producer_shaped_observation(
        **{inventory_name: [owner]}, blockers={}, quiescent=True)

    with pytest.raises(dq.DeploymentBlocked, match=f"blockers.{blocker_name}"):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json")
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("attempt_id", "attempt-real"),
    ("run_id", "run-real"),
    ("operation_id", "operation-real"),
    ("owner_id", "owner-real"),
])
def test_noise_shaped_row_with_authoritative_identity_is_malformed(
        tmp_path, field, value):
    row = {**_unknown_process(0), field: value}
    observation = {
        "quiescent": False,
        "digest": "2" * 64,
        "blockers": {"external_active": [row]},
        "errors": [
            "unregistered external measurement process has unknown ownership: "
            + row["command"]
        ],
    }

    with pytest.raises(dq.DeploymentBlocked, match="observation is malformed"):
        dq.authorize(
            "restart", observation, journal=tmp_path / (field + ".json"),
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )
    event = json.loads((tmp_path / (field + ".json")).read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False
    assert event["blockers"] == observation["blockers"]
    assert event["errors"] == observation["errors"]
    assert "audit" not in event


@pytest.mark.parametrize(("patch", "reason"), [
    ({"blockers": {"active_runs": {}}}, "must be a list"),
    ({"blockers": None}, "must be an object"),
    ({"errors": {}}, "errors must be a list of strings"),
    ({"blockers": {"active_runs": ["run-a"]}}, "rows must be objects"),
    ({"blockers": {"active_runs": [{}]}}, "ownership identity"),
    ({"blockers": {"future_category": []}}, "unknown blocker category"),
    ({"errors": ["valid", None]}, "errors must be a list of strings"),
    ({"quiescent": None}, "quiescent must be a boolean"),
])
def test_malformed_observation_cannot_be_overridden(tmp_path, patch, reason):
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    values = {
        "quiescent": False,
        "runs": [run],
        "blockers": {"active_runs": [run]},
        "errors": [],
        **patch,
    }
    observation = _producer_shaped_observation(**values)

    with pytest.raises(dq.DeploymentBlocked, match=reason):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(observation["digest"]),
        )
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False
    assert event["blockers"] == observation.get("blockers")
    assert event["errors"] == observation.get("errors")
    assert "audit" not in event


def test_noise_row_requires_non_empty_command(tmp_path):
    row = {**_unknown_process(0), "command": "  "}
    observation = _producer_shaped_observation(
        quiescent=False,
        external_owners=[row],
        blockers={"external_active": [row]},
        errors=[dq.UNKNOWN_PROCESS_ERROR_PREFIX + row["command"]],
    )

    with pytest.raises(dq.DeploymentBlocked, match="non-empty command"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False
    assert "audit" not in event


@pytest.mark.parametrize("quiescent", [True, False])
def test_declared_quiescence_must_match_validated_blockers_and_errors(
        tmp_path, quiescent):
    if quiescent:
        run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
        runs = [run]
        blockers = {"active_runs": [run]}
        errors = []
    else:
        runs = []
        blockers = {}
        errors = []
    observation = _producer_shaped_observation(
        quiescent=quiescent, runs=runs, blockers=blockers, errors=errors)

    with pytest.raises(dq.DeploymentBlocked, match="quiescent contradicts"):
        dq.authorize(
            "restart", observation, journal=tmp_path / "journal.json",
            override=_deployment_override(observation["digest"]),
        )
    event = json.loads((tmp_path / "journal.json").read_text())["latest"]
    assert event["status"] == "aborted"
    assert event["usable"] is False
    assert "audit" not in event


@pytest.mark.parametrize(("blocker_key", "owner"), [
    ("active_runs", {"run_id": "run-a", "project_id": "project-a",
                     "status": "running"}),
    ("active_operations", {"run_id": "run-a", "project_id": "project-a",
                           "status": "completed", "active_operations": 1}),
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
    inventory_by_blocker = {
        "active_runs": "runs",
        "active_operations": "runs",
        "registered_external_owners": "registered_external_owners",
        "godot_render_owners": "godot_render_owners",
        "external_active": "external_owners",
        "sidecar_owners": "sidecar_owners",
    }
    observation = _producer_shaped_observation(
        quiescent=False,
        blockers={blocker_key: [owner]},
        **{inventory_by_blocker[blocker_key]: [owner]},
    )

    with pytest.raises(dq.DeploymentBlocked, match="authoritative"):
        dq.authorize(
            "restart", observation, journal=tmp_path / (blocker_key + ".json"),
            override=_deployment_override(
                observation["digest"], acknowledge_unknown=True),
        )


def test_reconciliation_never_replays_before_or_after_quiescence(tmp_path):
    journal = tmp_path / "journal.json"
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    blocked = _producer_shaped_observation(
        quiescent=False, runs=[run], blockers={"active_runs": [run]})
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("redeploy", blocked, journal=journal)
    still_blocked = dq.reconcile(observation=blocked, journal=journal)
    assert still_blocked["reconciled"] is False
    assert still_blocked["replayed"] is False
    quiet = _producer_shaped_observation()
    settled = dq.reconcile(observation=quiet, journal=journal)
    assert settled == {"reconciled": True, "replayed": False,
                       "event": settled["event"]}
    data = json.loads(journal.read_text())
    assert data["events"][0]["status"] == "aborted"
    assert data["events"][-1]["status"] == "reconciled_quiescent"
    assert not any(e.get("replayed") is True for e in data["events"])


def test_reviewer_reconciliation_rejects_malformed_quiet_evidence(tmp_path):
    journal = tmp_path / "journal.json"
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    blocked = _producer_shaped_observation(
        runs=[run], blockers={"active_runs": [run]}, quiescent=False)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("restart", blocked, journal=journal)

    result = dq.reconcile(observation={"quiescent": True}, journal=journal)

    assert result["reconciled"] is False and result["replayed"] is False
    event = json.loads(journal.read_text())["latest"]
    assert event["status"] == "aborted" and event["usable"] is False
    assert "malformed" in event["reason"]


def test_reviewer_finalize_cannot_upgrade_aborted_event(tmp_path):
    journal = tmp_path / "journal.json"
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    blocked = _producer_shaped_observation(
        runs=[run], blockers={"active_runs": [run]}, quiescent=False)
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("restart", blocked, journal=journal)
    aborted = json.loads(journal.read_text())["latest"]

    with pytest.raises(dq.DeploymentBlocked, match="pending authorization"):
        dq.finalize({"event": aborted}, success=True, journal=journal)
    assert json.loads(journal.read_text())["latest"] == aborted


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("event_id", "different-event", "superseded"),
    ("action", "redeploy", "action does not match"),
    ("inventory_digest", "f" * 64, "digest does not match"),
    ("status", "aborted", "not a pending authorization"),
    ("status", "overridden", "state does not match"),
    ("pending", False, "not a pending authorization"),
])
def test_reviewer_finalize_binds_caller_to_persisted_pending_event(
        tmp_path, field, value, reason):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    persisted = json.loads(journal.read_text())["latest"]
    clearance["event"][field] = value

    with pytest.raises(dq.DeploymentBlocked, match=reason):
        dq.finalize(clearance, success=True, journal=journal)
    assert json.loads(journal.read_text())["latest"] == persisted


@pytest.mark.parametrize(("field", "reason"), [
    ("event_id", "malformed"),
    ("action", "malformed"),
    ("inventory_digest", "malformed"),
    ("status", "not a pending authorization"),
    ("pending", "not a pending authorization"),
])
def test_finalize_rejects_clearance_missing_a_durable_binding_field(
        tmp_path, field, reason):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    persisted = json.loads(journal.read_text())["latest"]
    del clearance["event"][field]

    with pytest.raises(dq.DeploymentBlocked, match=reason):
        dq.finalize(clearance, success=True, journal=journal)
    assert json.loads(journal.read_text())["latest"] == persisted


@pytest.mark.parametrize("event_id", [
    7, True, None, "not-a-uuid", "A" * 32, "a" * 31,
])
def test_finalize_rejects_matching_malformed_persisted_event_ids_without_rewrite(
        tmp_path, event_id):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    state = json.loads(journal.read_text())
    state["events"][-1]["event_id"] = event_id
    state["latest"]["event_id"] = event_id
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()
    clearance["event"]["event_id"] = event_id

    with pytest.raises(dq.DeploymentBlocked, match="event_id is malformed"):
        dq.finalize(clearance, success=True, journal=journal)

    assert journal.read_bytes() == corrupt_bytes


def test_finalize_rejects_orphan_latest_without_rewrite(tmp_path):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    state = json.loads(journal.read_text())
    orphan = {**state["latest"], "event_id": "f" * 32}
    state["latest"] = orphan
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()
    clearance["event"] = orphan

    with pytest.raises(dq.DeploymentBlocked, match="orphaned"):
        dq.finalize(clearance, success=True, journal=journal)

    assert journal.read_bytes() == corrupt_bytes


def test_finalize_rejects_divergent_latest_without_rewrite(tmp_path):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    state = json.loads(journal.read_text())
    state["latest"]["blockers"] = {"tampered": []}
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()

    with pytest.raises(dq.DeploymentBlocked, match="divergent"):
        dq.finalize(clearance, success=True, journal=journal)

    assert journal.read_bytes() == corrupt_bytes


def test_finalize_rejects_reordered_transition_history_without_rewrite(tmp_path):
    journal = tmp_path / "journal.json"
    observation = _producer_shaped_observation()
    dq.authorize("restart", observation, journal=journal)
    clearance = dq.authorize("restart", observation, journal=journal)
    state = json.loads(journal.read_text())
    state["events"][0], state["events"][1] = (
        state["events"][1], state["events"][0])
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()

    with pytest.raises(dq.DeploymentBlocked, match="broken event order"):
        dq.finalize(clearance, success=True, journal=journal)

    assert journal.read_bytes() == corrupt_bytes


def test_finalize_rejects_truncated_transition_history_without_rewrite(tmp_path):
    journal = tmp_path / "journal.json"
    observation = _producer_shaped_observation()
    first = dq.authorize("restart", observation, journal=journal)
    dq.finalize(first, success=False, journal=journal)
    clearance = dq.authorize("restart", observation, journal=journal)
    state = json.loads(journal.read_text())
    del state["events"][0]
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()

    with pytest.raises(dq.DeploymentBlocked, match="broken event order"):
        dq.finalize(clearance, success=True, journal=journal)

    assert journal.read_bytes() == corrupt_bytes


@pytest.mark.parametrize("operation", ["load", "authorize", "reconcile", "finalize"])
@pytest.mark.parametrize("events", [
    [
        _journal_event("a" * 32),
        _journal_event("a" * 32, prior_event_id="a" * 32,
                       status="completed", pending=False, usable=True),
    ],
    [
        _journal_event("a" * 32),
        _journal_event("b" * 32),
    ],
    [
        _journal_event("a" * 32, prior_event_id="a" * 32),
    ],
])
def test_complete_unique_predecessor_chain_is_required_without_rewrite(
        tmp_path, operation, events):
    journal = tmp_path / "journal.json"
    corrupt_bytes = _write_journal(journal, events)

    with pytest.raises(dq.DeploymentBlocked):
        _invoke_journal_operation(operation, journal)

    assert journal.read_bytes() == corrupt_bytes


@pytest.mark.parametrize(("status", "pending", "usable"), [
    ("mystery", False, False),
    ("authorized", False, False),
    ("authorized", True, True),
    ("overridden", False, False),
    ("completed", True, True),
    ("completed", False, False),
    ("aborted", True, False),
    ("aborted", False, True),
    ("reconciled_quiescent", True, False),
    ("reconciled_quiescent", False, True),
])
@pytest.mark.parametrize("operation", ["load", "authorize", "reconcile", "finalize"])
def test_status_pending_and_usable_invariants_fail_closed_without_rewrite(
        tmp_path, operation, status, pending, usable):
    journal = tmp_path / "journal.json"
    corrupt_bytes = _write_journal(journal, [
        _journal_event("a" * 32, status=status, pending=pending, usable=usable),
    ])

    with pytest.raises(dq.DeploymentBlocked):
        _invoke_journal_operation(operation, journal)

    assert journal.read_bytes() == corrupt_bytes


@pytest.mark.parametrize(("predecessor", "successor"), [
    (
        _journal_event("a" * 32, status="aborted", pending=False, usable=False),
        _journal_event("b" * 32, status="completed", pending=False, usable=True,
                       prior_event_id="a" * 32),
    ),
    (
        _journal_event("a" * 32, status="completed", pending=False, usable=True),
        _journal_event("b" * 32, status="completed", pending=False, usable=True,
                       prior_event_id="a" * 32),
    ),
    (
        _journal_event("a" * 32),
        _journal_event("b" * 32, status="reconciled_quiescent", pending=False,
                       usable=False, prior_event_id="a" * 32),
    ),
    (
        _journal_event("a" * 32),
        _journal_event("b" * 32, prior_event_id="a" * 32),
    ),
    (
        _journal_event("a" * 32),
        _journal_event("b" * 32, action="redeploy", status="completed",
                       pending=False, usable=True, prior_event_id="a" * 32),
    ),
    (
        _journal_event("a" * 32),
        _journal_event("b" * 32, inventory_digest="e" * 64,
                       status="completed", pending=False, usable=True,
                       prior_event_id="a" * 32),
    ),
])
@pytest.mark.parametrize("operation", ["load", "authorize", "reconcile", "finalize"])
def test_illegal_journal_transitions_fail_closed_without_rewrite(
        tmp_path, operation, predecessor, successor):
    journal = tmp_path / "journal.json"
    corrupt_bytes = _write_journal(journal, [predecessor, successor])

    with pytest.raises(dq.DeploymentBlocked):
        _invoke_journal_operation(operation, journal)

    assert journal.read_bytes() == corrupt_bytes


def test_current_writer_emits_a_linked_legal_state_machine(tmp_path):
    journal = tmp_path / "journal.json"
    observation = _producer_shaped_observation()
    clearance = dq.authorize("restart", observation, journal=journal)
    completed = dq.finalize(clearance, success=True, journal=journal)
    next_clearance = dq.authorize("restart", observation, journal=journal)
    aborted = dq.finalize(next_clearance, success=False, journal=journal)
    reconciled = dq.reconcile(observation=observation, journal=journal)

    events = json.loads(journal.read_text())["events"]
    assert [event["status"] for event in events] == [
        "authorized", "completed", "authorized", "aborted",
        "reconciled_quiescent",
    ]
    assert len({event["event_id"] for event in events}) == len(events)
    assert "prior_event_id" not in events[0]
    for index in range(1, len(events)):
        assert events[index]["prior_event_id"] == events[index - 1]["event_id"]
    assert completed["event"]["usable"] is True
    assert aborted["event"]["usable"] is False
    assert reconciled["event"]["usable"] is False
    assert all(event["pending"] is (event["status"] in {"authorized", "overridden"})
               for event in events)
    assert json.loads(journal.read_text())["latest"] == events[-1]


def test_generated_duplicate_event_id_fails_before_rewriting_journal(
        tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    dq.authorize("restart", _producer_shaped_observation(), journal=journal)
    before = journal.read_bytes()
    event_id = json.loads(before)["latest"]["event_id"]
    monkeypatch.setattr(dq.uuid, "uuid4", lambda: SimpleNamespace(hex=event_id))

    with pytest.raises(dq.DeploymentBlocked, match="duplicate event_id"):
        dq.authorize("restart", _producer_shaped_observation(), journal=journal)

    assert journal.read_bytes() == before


@pytest.mark.parametrize("operation", ["authorize", "reconcile"])
def test_mutation_paths_reject_missing_latest_without_overwriting_evidence(
        tmp_path, operation):
    journal = tmp_path / "journal.json"
    dq.authorize("restart", _producer_shaped_observation(), journal=journal)
    state = json.loads(journal.read_text())
    del state["latest"]
    journal.write_text(json.dumps(state, sort_keys=True))
    corrupt_bytes = journal.read_bytes()

    with pytest.raises(dq.DeploymentBlocked, match="missing"):
        if operation == "authorize":
            dq.authorize(
                "restart", _producer_shaped_observation(), journal=journal)
        else:
            dq.reconcile(
                observation=_producer_shaped_observation(), journal=journal)

    assert journal.read_bytes() == corrupt_bytes


def test_compatible_empty_and_minimal_legacy_journals_remain_usable(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"version": 1, "events": []}))
    dq.migrate_legacy_journal(
        journal=empty, expected_sha256=dq.hashlib.sha256(empty.read_bytes()).hexdigest(),
        actor="test", provenance="empty v1 fixture", legacy_format="linked-v1")
    empty_clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=empty)
    assert empty_clearance["event"]["status"] == "authorized"

    legacy = tmp_path / "legacy.json"
    legacy_event = {
        "event_id": "a" * 32,
        "status": "aborted",
        "usable": False,
    }
    legacy.write_text(json.dumps({
        "version": 1, "events": [legacy_event], "latest": legacy_event,
    }))
    dq.migrate_legacy_journal(
        journal=legacy, expected_sha256=dq.hashlib.sha256(legacy.read_bytes()).hexdigest(),
        actor="test", provenance="minimal aborted fixture", legacy_format="linked-v1")
    legacy_bytes = legacy.read_bytes()
    legacy_reconcile = dq.reconcile(
        observation=_producer_shaped_observation(), journal=legacy)
    assert legacy_reconcile["reconciled"] is False
    assert "no deployment action binding" in legacy_reconcile["reason"]
    assert legacy.read_bytes() == legacy_bytes
    legacy_clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=legacy)
    result = dq.finalize(legacy_clearance, success=True, journal=legacy)
    assert result["event"]["status"] == "completed"
    assert result["event"]["usable"] is True


def test_corrupt_journal_fails_closed(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text("{}")
    corrupt_bytes = journal.read_bytes()
    with pytest.raises(dq.DeploymentBlocked, match="malformed"):
        dq.authorize("restart", {"quiescent": True, "digest": "f" * 64,
                                  "blockers": {}, "errors": []}, journal=journal)
    assert journal.read_bytes() == corrupt_bytes
    assert list(tmp_path.glob("journal.json.corrupt.*")) == []


def test_malformed_override_is_aborted_and_unusable(tmp_path):
    override = tmp_path / "override.json"
    override.write_text("not json")
    assert dq.load_override(override)["_invalid_override"]
    journal = tmp_path / "journal.json"
    run = {"run_id": "run-a", "project_id": "project-a", "status": "running"}
    observation = _producer_shaped_observation(
        quiescent=False, runs=[run], blockers={"active_runs": [run]})
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


def test_resident_godot_requires_its_own_authority_not_semantic_ledger(tmp_path):
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
    assert any("Godot owner ledger is missing" in error for error in observed["errors"])
    assert not any("semantic sidecar ledger" in error for error in observed["errors"])
    assert all(not row["active"] for row in observed["external_owners"] if row["kind"] == "process")


def test_authorized_action_is_aborted_when_compose_fails(tmp_path):
    journal = tmp_path / "journal.json"
    clearance = dq.authorize(
        "restart", _producer_shaped_observation(), journal=journal)
    assert clearance["event"]["pending"] is True
    result = dq.finalize(clearance, success=False, error="compose failed", journal=journal)
    assert result["event"]["status"] == "aborted"
    data = json.loads(journal.read_text())
    assert data["latest"]["usable"] is False
    assert data["latest"]["reason"] == "compose failed"


@pytest.mark.parametrize(("success", "status", "usable"), [
    (True, "completed", True),
    (False, "aborted", False),
])
def test_cli_clearance_with_transport_fence_reaches_terminal_journal_state(
        tmp_path, monkeypatch, success, status, usable):
    from cli import server

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    clearance = dq.authorize("restart", _producer_shaped_observation())
    fence = dq.acquire_cutover_fence()
    clearance["_cutover_fence"] = fence

    result = server._finish_deployment(
        clearance, success=success,
        error=None if success else RuntimeError("compose exit 137"))

    latest = json.loads(dq.evidence_path().read_text())["latest"]
    assert result["event"] == latest
    assert latest["status"] == status
    assert latest["pending"] is False
    assert latest["usable"] is usable
    if not success:
        assert latest["reason"] == "compose exit 137"
    assert fence.closed


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("event_id", "stale-event", "superseded"),
    ("action", "redeploy", "action does not match"),
    ("inventory_digest", "f" * 64, "digest does not match"),
    ("status", "aborted", "not a pending authorization"),
    ("pending", False, "not a pending authorization"),
])
def test_cli_rejects_mutated_clearance_and_releases_transport_fence(
        tmp_path, monkeypatch, field, value, reason):
    from cli import server

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    clearance = dq.authorize("restart", _producer_shaped_observation())
    persisted = json.loads(dq.evidence_path().read_text())["latest"]
    clearance["event"][field] = value
    fence = dq.acquire_cutover_fence()
    clearance["_cutover_fence"] = fence

    with pytest.raises(dq.DeploymentBlocked, match=reason):
        server._finish_deployment(clearance, success=True)

    assert json.loads(dq.evidence_path().read_text())["latest"] == persisted
    assert fence.closed


def test_pending_authorization_is_not_replayed_by_reconcile(tmp_path):
    journal = tmp_path / "journal.json"
    dq.authorize("restart", _producer_shaped_observation(), journal=journal)
    result = dq.reconcile(
        observation=_producer_shaped_observation(), journal=journal)
    assert result["replayed"] is False and result["reconciled"] is False
    assert json.loads(journal.read_text())["latest"]["status"] == "aborted"


def test_new_authorization_explicitly_aborts_prior_pending_event(tmp_path):
    journal = tmp_path / "journal.json"
    observation = _producer_shaped_observation()
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


def test_generic_external_measurement_process_is_diagnostic():
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="4321 1 python3 /tmp/external_measurement_fixture.py --long-gate\n",
            stderr="",
        )

    owners, errors = dq.external_owners(runner=runner)
    assert all(row["diagnostic_only"] and not row["active"] for row in owners)
    assert errors == []


@pytest.mark.parametrize("command_line", [
    "4322 1 python3 /tmp/eval_job.py --duration 600",
    "4323 1 python3 /tmp/judge_fixture.py --duration 600",
    "4324 1 python3 /tmp/metrics_collection.py --duration 600",
])
def test_evaluator_judge_and_metrics_names_are_diagnostic(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is False
    assert owners[0]["diagnostic_only"] is True
    assert errors == []


@pytest.mark.parametrize("command_line", [
    "4326 1 python3 /tmp/grader_worker.py --duration 600",
    "4327 1 python3 /tmp/scoring_job.py --duration 600",
    "4328 1 python3 /tmp/assessment_worker.py --duration 600",
    "4329 1 python3 /tmp/quality_check.py --duration 600",
])
def test_grader_scoring_assessment_and_quality_names_are_diagnostic(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is False
    assert owners[0]["diagnostic_only"] is True
    assert errors == []


@pytest.mark.parametrize("command_line", [
    "4331 1 python3 /tmp/rater_worker.py --duration 600",
    "4332 1 python3 /tmp/review_worker.py --duration 600",
    "4333 1 python3 /tmp/metric_worker.py --duration 600",
    "4334 1 python3 /tmp/evaluationWorker.py --duration 600",
])
def test_nearby_evaluation_worker_names_are_diagnostic(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is False
    assert errors == []


@pytest.mark.parametrize("command_line", [
    "4335 1 python3 /tmp/grader_worker.py --repo /tmp/skillflow-candidate",
    "4336 1 python3 /tmp/rater_worker.py --index /tmp/zvec-grep-results",
])
def test_service_names_in_measurement_arguments_do_not_assert_ownership(command_line):
    def runner(command):
        if command[0] == "docker":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout=command_line + "\n", stderr="")

    owners, errors = dq.external_owners(runner=runner)
    assert owners[0]["active"] is False
    assert "resource" not in owners[0]
    assert owners[0]["diagnostic_only"] is True
    assert errors == []


def test_registered_external_id_never_binds_diagnostic_process_tokens():
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
    assert owners[0]["diagnostic_only"] is True
    assert errors == []


def test_registered_external_owner_is_not_inferred_from_process_text():
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
    assert owners[0]["diagnostic_only"] is True
    assert "attempt_id" not in owners[0]


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

    # This route-order fixture replaces the gate; capability mechanics have real-gate tests.
    monkeypatch.setattr(server, "_mint_deployment_command", lambda *args: object())

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
                        lambda action, _plan: events.append(("gate", action)) or {})
    monkeypatch.setattr(
        server, "_compose_up", lambda _args, **_kwargs: events.append(("compose",)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    monkeypatch.setattr(server, "_require_guarded_services_ready", lambda: None)
    assert server._ensure_docker_backend("http://localhost:4444", 1) is True
    assert events == [("gate", "redeploy"), ("compose",)]


def test_guarded_compose_start_includes_both_sidecars(monkeypatch):
    from cli import server

    # Argument construction is isolated here; actual missing/stale authority
    # refusal is exercised in test_deployment_authority.
    calls = []
    monkeypatch.setattr(server, "_ensure_host_dirs", lambda: None)
    monkeypatch.setattr(server, "_image_exists", lambda: True)
    monkeypatch.setattr(server, "_image_deps_are_stale", lambda: False)
    monkeypatch.setattr(server, "_warn_if_edge_network_is_alone", lambda: None)
    monkeypatch.setattr(
        server, "_compose",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    server._compose_up(server._compose_start_args(17), capability=object())
    assert calls == [("up", "-d", "--wait", "--wait-timeout", "17",
                      "zvec-grep", "godot-builder", "aitelier")]


def test_compose_reuse_requires_every_guarded_service(monkeypatch):
    from cli import server

    def compose(*args, **kwargs):
        return SimpleNamespace(stdout="aitelier\nzvec-grep\n")

    monkeypatch.setattr(server, "_compose", compose)
    assert server._container_running() is False


_DEFAULT_CONTAINER_ID = object()
_TEST_CONTAINER_IDS = {
    "zvec-grep": "1" * 64,
    "godot-builder": "2" * 64,
    "aitelier": "3" * 64,
}


def _compose_health_row(
        service, *, name=None, state="running", health="healthy",
        container_id=_DEFAULT_CONTAINER_ID):
    from cli import server
    row = {
        "Name": name or server._COMPOSE_CONTAINERS[service],
        "Service": service,
        "State": state,
        "Health": health,
    }
    if container_id is _DEFAULT_CONTAINER_ID:
        row["ID"] = _TEST_CONTAINER_IDS[service]
    elif container_id is not None:
        row["ID"] = container_id
    return json.dumps([row])


@pytest.fixture
def corroborated_containers(monkeypatch):
    from cli import server
    monkeypatch.setattr(server, "_compose_project", lambda: "aitelier")
    def inspect(container_id):
        service = next(key for key, value in _TEST_CONTAINER_IDS.items() if value == container_id)
        return {"Id": container_id, "Name": "/" + server._COMPOSE_CONTAINERS[service],
                "Config": {"Labels": {"com.docker.compose.project": "aitelier", "com.docker.compose.service": service, "com.docker.compose.oneoff": "False"}},
                "State": {"Running": True, "Status": "running", "Paused": False,
                          "Restarting": False, "Dead": False, "Health": {"Status": "healthy"}}}
    monkeypatch.setattr(server, "_inspect_guarded_container", inspect)


def test_guarded_service_readiness_proves_exact_topology(monkeypatch, corroborated_containers):
    from cli import server

    calls = []
    monkeypatch.setattr(
        server, "_compose",
        lambda *args, **kwargs: (
            calls.append(args)
            or SimpleNamespace(returncode=0, stdout=_compose_health_row(args[-1]))))

    assert server._guarded_service_errors() == []
    assert calls == [
        ("ps", "--all", "--format", "json", "zvec-grep"),
        ("ps", "--all", "--format", "json", "godot-builder"),
        ("ps", "--all", "--format", "json", "aitelier"),
    ]


def test_guarded_service_identity_matches_shipped_compose_topology():
    from cli import server

    root = Path(__file__).resolve().parents[2]
    services = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]
    assert server._COMPOSE_CONTAINERS == {
        service: services[service]["container_name"]
        for service in server._COMPOSE_SERVICES
    }
    assert all("healthcheck" in services[service]
               for service in server._COMPOSE_SERVICES)


@pytest.mark.parametrize(
    ("service", "override", "fragment"),
    [
        ("zvec-grep", {"health": "starting"}, "health=starting"),
        ("godot-builder", {"health": "unhealthy"}, "health=unhealthy"),
        ("aitelier", {"state": "exited", "health": "healthy"}, "state=exited"),
        ("zvec-grep", {"name": "aitelier-zg-copy"}, "identity mismatch"),
        ("godot-builder", {"service_name": "godot-builder-copy"}, "identity mismatch"),
        ("aitelier", {"missing": True}, "observed 0"),
        ("zvec-grep", {"container_id": None}, "no concrete"),
        ("zvec-grep", {"container_id": ""}, "no concrete"),
        ("godot-builder", {"container_id": "   "}, "no concrete"),
        ("aitelier", {"container_id": "fabricated-id"}, "no concrete"),
        ("aitelier", {"container_id": "a" * 13}, "no concrete"),
    ],
)
def test_guarded_service_readiness_fails_closed(
        monkeypatch, service, override, fragment, corroborated_containers):
    from cli import server

    def compose(*args, **kwargs):
        selected = args[-1]
        if selected != service:
            return SimpleNamespace(
                returncode=0, stdout=_compose_health_row(selected))
        if override.get("missing"):
            return SimpleNamespace(returncode=0, stdout="[]")
        row = json.loads(_compose_health_row(
            selected,
            name=override.get("name"),
            state=override.get("state", "running"),
            health=override.get("health", "healthy"),
            container_id=override.get("container_id", _DEFAULT_CONTAINER_ID),
        ))
        if "service_name" in override:
            row[0]["Service"] = override["service_name"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(row))

    monkeypatch.setattr(server, "_compose", compose)
    errors = server._guarded_service_errors()
    assert any(service in error and fragment in error for error in errors)


def test_guarded_service_readiness_rejects_distinct_short_docker_ids(monkeypatch, corroborated_containers):
    from cli import server

    short_ids = {"zvec-grep": "1" * 12, "godot-builder": "2" * 12,
                 "aitelier": "3" * 12}
    monkeypatch.setattr(
        server, "_compose",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=_compose_health_row(
                args[-1], container_id=short_ids[args[-1]]),
        ),
    )
    assert len(server._guarded_service_errors()) == 3


@pytest.mark.parametrize("semantic_id,godot_id", [
    ("a" * 64, "a" * 64),
    ("a" * 12, "a" * 64),
])
def test_guarded_service_readiness_rejects_reused_container_id(
        monkeypatch, semantic_id, godot_id, corroborated_containers):
    from cli import server

    ids = {"zvec-grep": semantic_id, "godot-builder": godot_id,
           "aitelier": "b" * 64}
    monkeypatch.setattr(
        server, "_compose",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=_compose_health_row(
                args[-1], container_id=ids[args[-1]]),
        ),
    )
    errors = server._guarded_service_errors()
    assert any("reuses container ID" in error or "no concrete" in error for error in errors)


def test_reuse_requires_exact_health_for_all_services(monkeypatch):
    from cli import server

    class Client:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(server.httpx, "Client", Client)
    monkeypatch.setattr(server, "_container_running", lambda: True)
    monkeypatch.setattr(server, "_is_healthy", lambda _client: True)
    monkeypatch.setattr(server, "_guarded_service_errors", lambda: [])
    monkeypatch.setattr(
        server, "_require_deployment_clearance",
        lambda _action: (_ for _ in ()).throw(AssertionError("gate invoked")),
    )
    assert server._ensure_docker_backend("http://localhost:4444", 1) is True

    monkeypatch.setattr(
        server, "_guarded_service_errors",
        lambda: ["zvec-grep is not ready: health=unhealthy"],
    )
    assert server._ensure_docker_backend("http://localhost:4444", 1) is False


def test_redeploy_health_failure_aborts_pending_journal(monkeypatch):
    from cli import server

    # This route-order fixture replaces the gate; capability mechanics have real-gate tests.
    monkeypatch.setattr(server, "_mint_deployment_command", lambda *args: object())

    terminal = []
    monkeypatch.setattr(server.httpx, "Client", lambda *args, **kwargs: object())
    monkeypatch.setattr(server, "_container_running", lambda: False)
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda _action, _plan: {"event": {"event_id": "gate"}})
    monkeypatch.setattr(server, "_compose_up", lambda _args, **_kwargs: None)
    monkeypatch.setattr(server, "_wait_healthy", lambda *_args: True)
    monkeypatch.setattr(
        server, "_require_guarded_services_ready",
        lambda: (_ for _ in ()).throw(RuntimeError("godot-builder unhealthy")),
    )
    monkeypatch.setattr(
        server, "_finish_deployment",
        lambda _clearance, *, success, error=None: terminal.append((success, str(error))),
    )

    with pytest.raises(RuntimeError, match="godot-builder unhealthy"):
        server._ensure_docker_backend("http://localhost:4444", 1)
    assert terminal == [(False, "godot-builder unhealthy")]


from core.deployment_lifecycle import shell_actions, unguarded_findings


def _has_compose_lifecycle_mutation(source):
    return bool(shell_actions(source))


@pytest.mark.parametrize("command", [
    "docker compose up -d",
    "docker-compose restart",
    "docker compose -f docker-compose.yml up -d",
    "docker compose --project-name aitelier restart",
    "docker compose --file=docker-compose.yml --project-name aitelier restart",
    "docker-compose -p aitelier stop",
    "docker --context desktop compose -f docker-compose.yml down",
    "docker -H unix:///tmp/docker.sock compose --project-name demo kill",
])
def test_operator_inventory_recognizes_compose_global_options(command):
    assert _has_compose_lifecycle_mutation(command)


@pytest.mark.parametrize("command", [
    "aitelier server --recreate",
    "docker compose build aitelier",
    "docker compose logs -f",
    "docker compose ps --all",
    "docker composure -f docker-compose.yml up -d",
])
def test_operator_inventory_keeps_non_lifecycle_commands_clean(command):
    assert not _has_compose_lifecycle_mutation(command)


def test_all_tracked_operator_surfaces_avoid_direct_compose_mutations():
    root = Path(__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    hits = []
    for relative in tracked:
        if (not relative
                or relative.startswith(("tests/", "design/evidence/", "evidence/"))
                or relative.endswith((".lock", ".png", ".pdf", ".sqlite3"))):
            continue
        try:
            source = (root / relative).read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for finding in unguarded_findings(relative, source):
            hits.append(f"{relative}:{finding.line}:{finding.function}:{finding.action}")
    assert hits == [], "direct Compose lifecycle bypasses the cutover gate:\n" + "\n".join(hits)


def test_server_redeploy_never_kills_a_listener_before_gate(monkeypatch):
    from cli import server

    # This route-order fixture replaces the gate; capability mechanics have real-gate tests.
    monkeypatch.setattr(server, "_mint_deployment_command", lambda *args: object())

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
                        lambda action, _plan: events.append(("gate", action)) or {})
    monkeypatch.setattr(
        server, "_compose_up", lambda _args, **_kwargs: events.append(("compose",)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    monkeypatch.setattr(server, "_require_guarded_services_ready", lambda: None)
    assert server._ensure_docker_backend("http://localhost:4444", 1) is True
    assert events == [("gate", "redeploy"), ("compose",)]


def test_server_restart_gate_runs_before_restart(monkeypatch):
    from cli import server

    # This route-order fixture replaces the gate; capability mechanics have real-gate tests.
    monkeypatch.setattr(server, "_mint_deployment_command", lambda *args: object())

    events = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(server.httpx, "Client", Client)
    monkeypatch.setattr(server, "_require_docker", lambda: events.append(("docker",)))
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda action, _plan: events.append(("gate", action)) or {})
    monkeypatch.setattr(server, "_compose",
                        lambda *args, **kwargs: (
                            events.append(("compose", *args))
                            or SimpleNamespace(returncode=0)))
    monkeypatch.setattr(server, "_wait_healthy", lambda client, max_wait: True)
    monkeypatch.setattr(server, "_require_guarded_services_ready", lambda: None)
    assert server.restart_server("http://localhost:4444", 1) is True
    assert events[:3] == [("docker",), ("gate", "restart"),
                          ("compose", "up", "-d", "--force-recreate",
                           "--wait", "--wait-timeout", "1",
                           "zvec-grep", "godot-builder", "aitelier")]


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

    # This route-order fixture replaces the gate; capability mechanics have real-gate tests.
    monkeypatch.setattr(server, "_mint_deployment_command", lambda *args: object())

    events = []
    monkeypatch.setattr(server, "_require_docker", lambda: None)
    monkeypatch.setattr(server, "_require_deployment_clearance",
                        lambda _a, _plan: {"event": {"event_id": "gate"}})
    monkeypatch.setattr(server, "_compose", lambda *_a, **_kw: SimpleNamespace(returncode=137))
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
