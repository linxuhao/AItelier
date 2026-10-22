"""Godot-only audited decisions: named metadata coordination, every physical veto kept.

The guard tests drive ``authorize`` directly with sealed inventories; the normal
path tests drive ``recreate_godot_builder`` through the live runtime observation,
the real cutover fence, the journal and the effect/finalization code, with only
Docker and the process table replaced at their boundary.
"""
import json
import sqlite3
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli import server
from core import datadir, deployment_quiescence as dq
from tests.unit.test_backend_passive_deployment import packet, seal
from tests.unit.test_host_live_deployment_observation import live  # noqa: F401 -- fixture
from tests.unit.test_live_measurement_override import (  # noqa: F401 -- fixtures
    assert_fence_held, latest, normal, override)
from tests.unit.test_normal_gate_preparation import QUOTAS, bound  # noqa: F401 -- fixture

RESEARCH = ("research-a", "research-b")


def godot_packet():
    """Current-shaped Godot mix: retained ready demand, the own deployment controller,
    two unaffected active remote research owners, paused recovery and SSH noise."""
    observation = packet()
    observation["registered_external_owners"] += [
        {"attempt_id": name, "project_id": "research", "node_key": "research.remote",
         "status": "active"} for name in RESEARCH]
    observation["registered_external_owners"].append(
        {"attempt_id": "recovery", "status": "paused", "abandon_kind": None})
    return seal(observation)


def cover(*extra, omit=()):
    rows = [{"kind": "registered_external_owners", "id": name, "scope_ref": f"inbox seq {n}"}
            for n, name in enumerate(RESEARCH, start=21) if name not in omit]
    return rows + list(extra)


def godot_decision(observation, **changes):
    return {"action": "redeploy", "actor": "driver:director", "reason": "Godot-only owner decision",
            "ticket": "explicit-owner-coordination", "inventory_digest": observation["digest"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "acknowledge_unknown": True, "godot_only": True,
            "deployment_attempt_id": "deployment-current", "coordinated_owners": cover(),
            **changes}


def godot_authorize(tmp_path, observation, override, **trusted):
    return dq.authorize("redeploy", observation, journal=tmp_path / "journal.json",
                        override=override, **({"godot_only": True} if trusted == {} else trusted))


def refuse(tmp_path, observation, override, *, match, **trusted):
    before = deepcopy(observation)
    with pytest.raises(dq.DeploymentBlocked, match=match):
        godot_authorize(tmp_path, observation, override, **trusted)
    event = dq._load_journal(tmp_path / "journal.json")["latest"]
    assert event["status"] == "aborted" and event["usable"] is False
    assert event["inventory_digest"] == observation["digest"]
    assert event["blockers"] == observation["blockers"] and event["errors"] == observation["errors"]
    assert observation == before
    return event


# ── causal baseline: the normal generic decision cannot express the mix ───────

@pytest.mark.parametrize("trusted", [{"backend_only": True}, {"backend_only": False}, {}])
@pytest.mark.parametrize("acknowledge,match", [
    (True, "cannot bypass authoritative active owners"),
    (False, "must acknowledge unknown"),
])
def test_generic_decision_still_refuses_the_mixed_godot_inventory(tmp_path, trusted, acknowledge,
                                                                   match):
    # {"backend_only": True} is what the Godot caller passed before the Godot-only scope.
    observation = godot_packet()
    plain = godot_decision(observation, acknowledge_unknown=acknowledge)
    for key in ("godot_only", "deployment_attempt_id", "coordinated_owners"):
        plain.pop(key)
    refuse(tmp_path, observation, plain, match=match, **trusted)


# ── the new scope on the guard ─────────────────────────────────────────────────

def test_exact_metadata_cover_is_journaled_with_every_original_fact(tmp_path):
    observation = godot_packet()
    before = deepcopy(observation)
    clearance = godot_authorize(tmp_path, observation, godot_decision(observation))
    event = clearance["event"]
    assert clearance["allowed"] is True and event["status"] == "overridden"
    audit = event["audit"]
    assert audit["godot_only"] is True and "backend_only" not in audit
    assert audit["override_scope"] == "coordinated_owners"
    assert audit["coordinated_owners"] == cover() and audit["acknowledge_unknown"] is True
    assert audit["deployment_attempt_id"] == "deployment-current"
    assert audit["deployment_controller_identity"] == "recorded_owner"
    affected = audit["affected_ownership"]
    for attempt in (*RESEARCH, "recovery"):
        assert any(row.get("attempt_id") == attempt for row in affected)
    assert all(row in affected for row in observation["external_owners"])
    assert not any(row.get("attempt_id") == "deployment-current" for row in affected)
    assert event["blockers"] == observation["blockers"] and event["errors"] == observation["errors"]
    assert event["inventory_digest"] == observation["digest"]
    assert observation == before and observation["quiescent"] is False
    done = dq.finalize(clearance, success=False, error="CPU decision only; no deployment",
                       journal=tmp_path / "journal.json")
    assert done["event"]["status"] == "aborted" and done["event"]["usable"] is False


def test_same_inventory_under_the_backend_scope_keeps_its_own_semantics(tmp_path):
    # Differential: a render owner is nameable for the backend effect, never for Godot.
    observation = godot_packet()
    render = {"owner_id": "render-a", "status": "active"}
    observation["godot_render_owners"] = [render]
    observation["blockers"]["godot_render_owners"] = [render]
    seal(observation)
    names = cover({"kind": "godot_render_owners", "id": "render-a", "scope_ref": "inbox seq 30"})
    backend = godot_decision(observation, godot_only=False, backend_only=True,
                             action="restart", coordinated_owners=names)
    accepted = dq.authorize("restart", observation, journal=tmp_path / "backend.json",
                            override=backend, backend_only=True)
    assert accepted["event"]["audit"]["backend_only"] is True
    assert "godot_only" not in accepted["event"]["audit"]
    refuse(tmp_path, observation, godot_decision(observation, coordinated_owners=names),
           match="cannot be named")
    refuse(tmp_path, observation, godot_decision(observation),
           match="non-nameable authoritative owners: godot_render_owners")


@pytest.mark.parametrize("trusted,match", [
    ({"backend_only": True}, "trusted Godot recreation caller"),        # backend service caller
    ({"backend_only": False}, "trusted Godot recreation caller"),       # direct unscoped caller
])
def test_only_the_trusted_godot_caller_honors_the_scope(tmp_path, trusted, match):
    observation = godot_packet()
    refuse(tmp_path, observation, godot_decision(observation), match=match, **trusted)


@pytest.mark.parametrize("change,match", [
    ({"backend_only": True, "godot_only": False},
     "backend-only override requires the trusted backend caller"),
    ({"backend_only": True}, "mutually exclusive"),
    ({"godot_only": "true"}, "trusted Godot recreation caller"),
    ({"godot_only": 1}, "trusted Godot recreation caller"),
    ({"godot_only": None}, "trusted Godot recreation caller"),
    ({"godot_only": False}, "exact recorded owner"),
    ({"godot_only": False, "deployment_attempt_id": None},
     "trusted backend caller and backend-only scope"),
    ({"acknowledge_unknown": False}, "acknowledge unknown"),
    ({"inventory_digest": "0" * 64}, "digest"),
    ({"expires_at": "2000-01-01T00:00:00+00:00"}, "expired"),
    ({"expires_at": "2100-01-01"}, "timezone"),
    ({"action": "restart"}, "not 'redeploy'"),
    ({"actor": "driver:other"}, "exact recorded owner"),
    ({"deployment_attempt_id": "research-a"}, "exact recorded owner"),
    ({"deployment_attempt_id": ""}, "exact recorded owner"),
    ({"_invalid_override": False}, "override is malformed"),
])
def test_every_existing_decision_check_is_kept(tmp_path, change, match):
    observation = godot_packet()
    refuse(tmp_path, observation, godot_decision(observation, **change), match=match)


def test_conflicting_scopes_refuse_whichever_caller_is_trusted(tmp_path):
    observation = godot_packet()
    both = godot_decision(observation, backend_only=True)
    refuse(tmp_path, observation, both, match="mutually exclusive")
    refuse(tmp_path, observation, both, match="mutually exclusive", backend_only=True)
    refuse(tmp_path, observation, both, match="mutually exclusive", backend_only=False)
    with pytest.raises(ValueError, match="mutually exclusive"):
        dq.authorize("redeploy", observation, journal=tmp_path / "journal.json",
                     override=both, backend_only=True, godot_only=True)
    with pytest.raises(ValueError, match="explicit boolean"):
        dq.authorize("redeploy", observation, journal=tmp_path / "journal.json",
                     override=both, godot_only="yes")


@pytest.mark.parametrize("coordinated,match", [
    (cover(omit=("research-b",)), "exactly cover"),
    (cover({"kind": "registered_external_owners", "id": "stale", "scope_ref": "r"}), "exactly cover"),
    (cover({"kind": "registered_external_owners", "id": "recovery", "scope_ref": "r"}), "exactly cover"),
    (cover({"kind": "registered_external_owners", "id": "deployment-current", "scope_ref": "r"}),
     "exactly cover"),
    (cover({"kind": "registered_external_owners", "id": "research-a", "scope_ref": "again"}), "twice"),
    (cover({"kind": "godot_render_owners", "id": "render-a", "scope_ref": "r"}), "cannot be named"),
    (cover({"kind": "external_active", "id": "x", "scope_ref": "r"}), "cannot be named"),
    (cover({"kind": "checkout_leases", "id": "x", "scope_ref": "r"}), "cannot be named"),
    (cover({"kind": "registered_external_owners", "id": "research-a ", "scope_ref": " "}), "non-empty"),
    (cover({"kind": "registered_external_owners", "id": "r", "scope_ref": "r", "proof": "x"}), "exactly"),
    (cover(["registered_external_owners", "research-a", "r"]), "exactly"),
    ([], "non-empty list"), (None, "non-empty list"), ({"kind": "x"}, "non-empty list"),
])
def test_missing_extra_duplicate_or_malformed_cover_refuses(tmp_path, coordinated, match):
    observation = godot_packet()
    refuse(tmp_path, observation, godot_decision(observation, coordinated_owners=coordinated),
           match=match)


def test_without_a_cover_active_metadata_owners_keep_vetoing(tmp_path):
    observation = godot_packet()
    override = godot_decision(observation)
    override.pop("coordinated_owners")
    refuse(tmp_path, observation, override, match="cannot bypass authoritative active owners")


@pytest.mark.parametrize("effect", ["render", "render-lost", "run", "operation", "lease",
                                    "write", "known-process", "controller-child", "pending-sidecar",
                                    "revision", "semantic-error", "measurement-failure",
                                    "inventory-error", "docker-render"])
def test_physical_effects_veto_an_exact_metadata_cover(tmp_path, effect):
    observation = godot_packet()
    names = cover()
    if effect in {"render", "render-lost"}:
        row = {"owner_id": "render-1", "status": "active" if effect == "render" else "owner_lost"}
        observation["godot_render_owners"] = [row]
        observation["blockers"]["godot_render_owners"] = [row]
    elif effect in {"run", "operation"}:
        row = {"run_id": "real", "project_id": "foreign", "status": "running",
               "active_operations": int(effect == "operation"),
               "audit": {"alive": int(effect == "operation"), "lost": [], "unknown": []}}
        observation["runs"] = [row]
        observation["blockers"]["active_runs"] = [row]
        observation["blockers"]["active_operations"] = [row] if effect == "operation" else []
    elif effect == "lease":
        observation["blockers"]["checkout_leases"] = [{"run_id": "w", "owner": "writer",
                                                       "canonical_checkout": "/source"}]
    elif effect == "write":
        observation["blockers"]["checkout_leases"] = [{"canonical_checkout": "/source",
                                                       "owner": "writer", "kind": "external"}]
    elif effect in {"known-process", "controller-child"}:
        observation["external_owners"].append(
            {"kind": "process", "command": "ssh host bash -s < research", "active": True,
             "resource": "external_measurement", "ownership": "registered",
             "attempt_id": "research-a" if effect == "known-process" else "deployment-current"})
    elif effect == "docker-render":
        observation["external_owners"].append(
            {"kind": "docker", "id": "c1", "name": "aitelier-godot-render", "service": "render",
             "active": True, "resource": "render"})
    elif effect == "measurement-failure":
        observation["blockers"]["measurement_failure"] = [{"reason": "inventory unavailable"}]
        observation["errors"].append("inventory unavailable")
    elif effect == "inventory-error":
        observation["errors"].append("process inventory failed: unavailable")
    else:
        row = observation["sidecar_owners"][0]
        if effect == "pending-sidecar":
            row["outcome"] = "pending"
        if effect == "revision":
            row["done_revision"] = 1
        if effect == "semantic-error":
            row["error"] = "worker error"
    if effect == "render":
        names = cover({"kind": "godot_render_owners", "id": "render-1", "scope_ref": "r"})
    observation["blockers"]["external_active"] = [
        row for row in observation["external_owners"] if row["active"]]
    seal(observation)
    refuse(tmp_path, observation, godot_decision(observation, coordinated_owners=names),
           match="cannot be named" if effect == "render" else "non-nameable authoritative owners")


@pytest.mark.parametrize("controller", ["absent", "null", "paused"])
def test_unproven_controller_is_unknown_and_needs_acknowledgement(tmp_path, controller):
    observation = godot_packet()
    row = observation["registered_external_owners"][0]
    if controller == "absent":
        row.pop("owner_driver_id")
    elif controller == "null":
        row["owner_driver_id"] = None
    else:
        row["status"] = "paused"
    seal(observation)
    if controller != "paused":
        refuse(tmp_path, observation, godot_decision(observation, acknowledge_unknown=False),
               match="acknowledge unknown")
    event = godot_authorize(tmp_path, observation, godot_decision(observation))["event"]
    identity = "recorded_owner" if controller == "paused" else "unknown"
    assert event["audit"]["deployment_controller_identity"] == identity
    assert (row in event["audit"]["affected_ownership"]) is (controller != "paused")
    assert event["blockers"]["registered_external_owners"] == observation["registered_external_owners"]


@pytest.mark.parametrize("status,kind,settled", [
    ("paused", None, None), ("unknown", None, None),
    ("abandoned", "unknown", "2026-10-01T00:00:00+00:00"), ("settled", None, None),
])
def test_recovery_uncertainty_stays_disclosed_and_unnameable(tmp_path, status, kind, settled):
    observation = godot_packet()
    row = {"attempt_id": "uncertain", "status": status, "abandon_kind": kind, "settled_at": settled}
    observation["registered_external_owners"].append(row)
    seal(observation)
    refuse(tmp_path, observation, godot_decision(observation, coordinated_owners=cover(
        {"kind": "registered_external_owners", "id": "uncertain", "scope_ref": "r"})),
        match="exactly cover")
    event = godot_authorize(tmp_path, observation, godot_decision(observation))["event"]
    assert row in event["audit"]["affected_ownership"]
    assert row in event["blockers"]["registered_external_owners"]


def test_unknown_noise_alone_needs_explicit_acknowledgement(tmp_path):
    observation = godot_packet()
    refuse(tmp_path, observation, godot_decision(observation, acknowledge_unknown=False),
           match="acknowledge unknown")
    observation["external_owners"].clear()
    observation["blockers"]["external_active"] = []
    observation["errors"].clear()
    observation["registered_external_owners"].pop()          # recovery uncertainty
    seal(observation)
    event = godot_authorize(tmp_path, observation,
                            godot_decision(observation, acknowledge_unknown=False))["event"]
    assert event["audit"]["acknowledge_unknown"] is False
    assert event["audit"]["godot_only"] is True


@pytest.mark.parametrize("coordinated", [None, "stale"])
def test_quiescent_godot_scope_is_validated_and_journaled(tmp_path, coordinated):
    observation = packet()
    for name in ("runs", "sidecar_owners", "external_owners", "registered_external_owners",
                 "godot_render_owners"):
        observation[name] = []
    observation["blockers"], observation["errors"], observation["quiescent"] = {}, [], True
    seal(observation)
    override = godot_decision(observation, deployment_attempt_id=None)
    override.pop("coordinated_owners")
    if coordinated:
        refuse(tmp_path, observation, godot_decision(observation, deployment_attempt_id=None),
               match="exactly cover")
        return
    refuse(tmp_path, observation, override, match="trusted Godot recreation caller",
           backend_only=True)
    event = godot_authorize(tmp_path, observation, override)["event"]
    assert event["status"] == "authorized"
    assert event["audit"]["godot_only"] is True
    assert event["audit"]["override_scope"] == "quiescent_restriction"


# ── the normal recreate-godot caller ───────────────────────────────────────────

def seed_registry(db, *, own_controller=False):
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS state_attempts (attempt_id TEXT PRIMARY KEY, "
                     "project_id TEXT, node_key TEXT, harness TEXT, external_id TEXT, status TEXT, "
                     "execution_kind TEXT, abandon_kind TEXT, owner_driver_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS state_external_owners (attempt_id TEXT PRIMARY KEY, "
                     "project_id TEXT NOT NULL, node_key TEXT NOT NULL, harness TEXT NOT NULL, "
                     "external_id TEXT NOT NULL, status TEXT NOT NULL, admitted_at TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL, settled_at TEXT)")
        rows = [(name, "research", "research.remote", "remote", f"remote-job-{name}", None)
                for name in RESEARCH]
        if own_controller:
            rows.append(("deployment-current", "aitelier", "deployment.coordinated-godot",
                         "director", "godot-cutover", "director"))
        for attempt, project, node, harness, external, driver in rows:
            conn.execute("INSERT INTO state_attempts VALUES (?,?,?,?,?,'running','external',NULL,?)",
                         (attempt, project, node, harness, external, driver))
            conn.execute("INSERT INTO state_external_owners VALUES (?,?,?,?,?,'active','t0','t0',NULL)",
                         (attempt, project, node, harness, external))
        conn.commit()


def registry_rows(db):
    with db.get_connection() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT attempt_id,status,settled_at FROM state_external_owners ORDER BY attempt_id")]


def seed_render_owner(status="active"):
    path = datadir.godot_control_dir() / "owners.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS render_owners (owner_id TEXT PRIMARY KEY, "
                     "resource TEXT, project_id TEXT, run_id TEXT, operation_id TEXT, "
                     "generation INTEGER, status TEXT, actor TEXT, started_at REAL, "
                     "heartbeat_at REAL, ended_at REAL, reason TEXT)")
        conn.execute("INSERT INTO render_owners VALUES ('render-live','godot','game','run-1','op-1',"
                     "1,?,'harness',1.0,2.0,NULL,NULL)", (status,))


def godot_args(tmp_path, bound, monkeypatch, *, identities=None):
    manifest, binding, _ = bound
    quota = tmp_path / "quota.json"
    quota.write_text(json.dumps({"services": {"godot-builder": {"environment": QUOTAS}}}))
    expected = {"cid": "b" * 64, "pid": 123, "image": "sha256:" + "c" * 64}
    target = "sha256:" + "e" * 64
    after = {**expected, "cid": "d" * 64, "pid": 456, "image": target}
    values = iter(identities or [expected, after, after])
    monkeypatch.setattr(server, "_godot_identity", lambda: next(values))
    monkeypatch.setattr(server, "_godot_health", lambda cid: {
        "ok": True, "retention_limits": {"files": 4096, "bytes": 536870912, "patterns": 16,
                                         "search_entries": 4096},
        "source_identity": {"path": "/srv/godot_harness.py", "sha256": binding["engine_sha256"],
                            "loaded_sha256": binding["engine_sha256"]}})
    return dict(override_file=str(quota), binding_file=str(manifest), expected_cid=expected["cid"],
                expected_pid=123, expected_image=expected["image"], target_image=target,
                report_file=str(tmp_path / "effect.json"))


def names_for(snapshot):
    rows = [row for row in snapshot["registered_external_owners"] if row["project_id"] == "research"]
    assert [row["attempt_id"] for row in rows] == list(RESEARCH)
    return [{"kind": "registered_external_owners", "id": row["attempt_id"],
             "scope_ref": f"research owner {row['attempt_id']} says Godot is unaffected"}
            for row in rows]


@pytest.mark.parametrize("acknowledge,match", [
    (True, "cannot bypass authoritative active owners"),
    (False, "must acknowledge unknown"),
])
def test_normal_godot_path_mixed_refusal_is_the_retained_baseline(
        normal, bound, tmp_path, monkeypatch, acknowledge, match):
    seed_registry(normal[0][1])
    args = godot_args(tmp_path, bound, monkeypatch, identities=[])
    seen = []
    def decide(action, snapshot):
        seen.append(snapshot)
        return override(action, snapshot, acknowledge_unknown=acknowledge)
    with pytest.raises(RuntimeError, match=match):
        server.recreate_godot_builder(**args, override_decision=decide)
    event = latest()
    assert event["status"] == "aborted" and event["usable"] is False
    assert event["inventory_digest"] == seen[0]["digest"]
    assert event["blockers"] == seen[0]["blockers"] and event["errors"] == seen[0]["errors"]
    assert event["blockers"]["registered_external_owners"] and event["blockers"]["external_active"]
    assert normal[1] == [] and registry_rows(normal[0][1])[0][1] == "active"


@pytest.mark.parametrize("resolution", [
    pytest.param(None, id="quota-only"),
    pytest.param("1920x1080", id="display-1920x1080"),
])
@pytest.mark.parametrize("controller", [False, True])
def test_normal_godot_path_honors_exact_metadata_cover_with_godot_effect_only(
        normal, bound, tmp_path, monkeypatch, controller, resolution):
    db = normal[0][1]
    seed_registry(db, own_controller=controller)
    before = registry_rows(db)
    args = godot_args(tmp_path, bound, monkeypatch)
    environment = dict(QUOTAS)
    if resolution is not None:
        environment["GODOT_PLAYTEST_RES"] = resolution
    Path(args["override_file"]).write_text(json.dumps(
        {"services": {"godot-builder": {"environment": environment}}}))
    overlays = []
    compose_boundary = server._compose
    def compose(*command, **kwargs):
        if command[:1] == ("-f",):
            assert_fence_held()
            overlays.append(json.loads(Path(command[1]).read_text()))
        return compose_boundary(*command, **kwargs)
    monkeypatch.setattr(server, "_compose", compose)
    seen = []
    def decide(action, snapshot):
        assert_fence_held()
        seen.append(snapshot)
        return override(action, snapshot, godot_only=True, coordinated_owners=names_for(snapshot),
                        actor="driver:director" if controller else "owned-operator",
                        **({"deployment_attempt_id": "deployment-current"} if controller else {}))
    receipt = server.recreate_godot_builder(**args, override_decision=decide)
    assert len(seen) == 1
    assert overlays == [{"services": {"godot-builder": {
        "image": args["target_image"], "environment": environment}}}]
    assert normal[1] == [("-f", normal[1][0][1], "up", "-d", "--no-deps", "--force-recreate",
                          "--no-build", "godot-builder")]
    events = json.loads(dq.evidence_path().read_text())["events"]
    authorization, completion = events[-2:]
    assert receipt["journal"]["event"] == completion
    assert authorization["status"] == "overridden" and completion["status"] == "completed"
    assert completion["usable"] is True and completion["inventory_digest"] == seen[0]["digest"]
    audit = authorization["audit"]
    assert audit["godot_only"] is True and "backend_only" not in audit
    assert audit["override_scope"] == "coordinated_owners"
    assert audit["coordinated_owners"] == names_for(seen[0])
    assert audit["deployment_controller_identity"] == ("recorded_owner" if controller else "not_declared")
    assert all(row in audit["affected_ownership"] for row in seen[0]["blockers"]["external_active"])
    assert authorization["blockers"] == seen[0]["blockers"]
    assert authorization["errors"] == seen[0]["errors"] and seen[0]["errors"]
    assert registry_rows(db) == before                       # nothing settled or cleared
    report = json.loads((tmp_path / "effect.json").read_text())
    assert report["journal"]["event"]["status"] == "completed"
    assert report["after"]["image"] == args["target_image"]


@pytest.mark.parametrize("case,match", [
    ("restart", "trusted Godot recreation caller"),
    ("redeploy-backend", "trusted Godot recreation caller"),
    ("unscoped", "trusted Godot recreation caller"),
])
def test_other_callers_cannot_assert_the_godot_scope(normal, monkeypatch, case, match):
    seed_registry(normal[0][1])
    def decide(action, snapshot):
        return override(action, snapshot, godot_only=True, coordinated_owners=names_for(snapshot))
    if case == "restart":
        monkeypatch.setattr(server, "_wait_healthy", lambda *_a: True)
        with pytest.raises(RuntimeError, match=match):
            server.restart_server(override_decision=decide)
    else:
        with pytest.raises(RuntimeError, match=match):
            server._require_deployment_clearance(
                "redeploy", override_decision=decide,
                **({"backend_service": True} if case == "redeploy-backend" else {}))
    assert normal[1] == [] and latest()["status"] == "aborted" and latest()["usable"] is False
    with pytest.raises(ValueError, match="one service"):
        server._require_deployment_clearance("redeploy", backend_service=True, godot_service=True)


@pytest.mark.parametrize("resolution", [None, "1920x1080"])
@pytest.mark.parametrize("case,match", [
    ("render-named", "cannot be named"),
    ("render-unnamed", "non-nameable authoritative owners: godot_render_owners"),
    ("render-lost", "non-nameable authoritative owners: godot_render_owners"),
    ("known-process", "non-nameable authoritative owners: external_active"),
    ("lease", "non-nameable authoritative owners: checkout_leases"),
    ("missing", "exactly cover"),
    ("extra", "exactly cover"),
    ("backend-scope", "backend-only decisions are honored only"),
    ("both-scopes", "backend-only decisions are honored only"),
    ("unacknowledged", "acknowledge unknown"),
    ("stale-digest", "digest"),
    ("declined", "operator declined"),
    ("raises", "operator decision failed"),
])
def test_normal_godot_path_counterexamples_refuse_with_unchanged_facts(
        normal, bound, tmp_path, monkeypatch, case, match, resolution):
    db = normal[0][1]
    seed_registry(db)
    if case in {"render-named", "render-unnamed", "render-lost"}:
        seed_render_owner("owner_lost" if case == "render-lost" else "active")
    if case == "lease":
        with db.get_connection() as conn:
            conn.execute("INSERT INTO checkout_write_admissions (canonical_checkout,owner,kind) "
                         "VALUES ('/owned/control','foreign-owner','external')")
            conn.commit()
    if case == "known-process":
        real_run = server.subprocess.run
        def run(command, *a, **k):
            if command[:2] == ["ps", "-axo"]:
                return type("R", (), {"returncode": 0, "stderr": "", "stdout":
                                      "999998 1 ssh host run remote-job-research-a\n"})()
            return real_run(command, *a, **k)
        monkeypatch.setattr(server.subprocess, "run", run)
    before = registry_rows(db)
    args = godot_args(tmp_path, bound, monkeypatch, identities=[])
    if resolution is not None:
        Path(args["override_file"]).write_text(json.dumps({"services": {"godot-builder": {
            "environment": {**QUOTAS, "GODOT_PLAYTEST_RES": resolution}}}}))
    seen = []
    def decide(action, snapshot):
        seen.append(snapshot)
        if case == "declined":
            return None
        if case == "raises":
            raise ValueError("owned decision failure")
        names = names_for(snapshot)
        if case == "render-named":
            names.append({"kind": "godot_render_owners", "id": "render-live", "scope_ref": "r"})
        if case == "missing":
            names = names[:1]
        if case == "extra":
            names.append({"kind": "registered_external_owners", "id": "ghost", "scope_ref": "r"})
        scope = ({"backend_only": True} if case == "backend-scope"
                 else {"backend_only": True, "godot_only": True} if case == "both-scopes"
                 else {"godot_only": True})
        return override(action, snapshot, coordinated_owners=names, **scope,
                        **({"acknowledge_unknown": False} if case == "unacknowledged" else {}),
                        **({"inventory_digest": "0" * 64} if case == "stale-digest" else {}))
    with pytest.raises(RuntimeError, match=match):
        server.recreate_godot_builder(**args, override_decision=decide)
    assert len(seen) == 1
    event = latest()
    assert event["status"] == "aborted" and event["usable"] is False
    assert event["inventory_digest"] == seen[0]["digest"]
    assert event["blockers"] == seen[0]["blockers"] and event["errors"] == seen[0]["errors"]
    assert normal[1] == [] and not (tmp_path / "effect.json").exists()
    assert registry_rows(db) == before


def test_normal_godot_file_decision_must_match_the_fresh_measurement(normal, bound, tmp_path,
                                                                    monkeypatch):
    db = normal[0][1]
    seed_registry(db)
    snapshot = dq.measure(runtime_facts=server._live_runtime_observation(server._DEFAULT_URL))
    path = tmp_path / "godot-scope.json"
    path.write_text(json.dumps(override("redeploy", snapshot, godot_only=True,
                                        coordinated_owners=names_for(snapshot))))
    monkeypatch.setenv("AITELIER_DEPLOY_OVERRIDE_FILE", str(path))
    args = godot_args(tmp_path, bound, monkeypatch, identities=[])
    time.sleep(1.05)                     # observation times have one-second resolution
    with pytest.raises(RuntimeError, match="digest does not match"):
        server.recreate_godot_builder(**args)
    with pytest.raises(RuntimeError, match="choose a file override or a fresh operator decision"):
        server.recreate_godot_builder(**args, override_decision=lambda a, s: pytest.fail("called"))
    assert normal[1] == [] and latest()["status"] == "aborted"


def test_accepted_godot_scope_still_rechecks_identity_before_effect(normal, bound, tmp_path,
                                                                    monkeypatch):
    seed_registry(normal[0][1])
    moved = {"cid": "f" * 64, "pid": 123, "image": "sha256:" + "c" * 64}
    args = godot_args(tmp_path, bound, monkeypatch, identities=[moved])
    def decide(action, snapshot):
        return override(action, snapshot, godot_only=True, coordinated_owners=names_for(snapshot))
    with pytest.raises(ValueError, match="identity changed before effect"):
        server.recreate_godot_builder(**args, override_decision=decide)
    assert normal[1] == []
    events = json.loads(dq.evidence_path().read_text())["events"]
    assert events[-2]["status"] == "overridden" and events[-2]["audit"]["godot_only"] is True
    assert events[-1]["status"] == "aborted" and events[-1]["usable"] is False
    report = json.loads((tmp_path / "effect.json").read_text())
    assert "identity changed" in report["error"] and report["journal"]["event"] == events[-1]


@pytest.mark.parametrize("configuration", [
    {"services": {"godot-builder": {
        "environment": {**QUOTAS, "GODOT_PLAYTEST_RES": value}}}}
    for value in (None, True, 19201080, 1920.108, ["1920x1080"], {"width": 1920},
                  "", "1280x720", "1920x900", "1920X1080", " 1920x1080",
                  "1920x1080 ", "1920x1080x24", "0x0", "-1x1080",
                  "999999999x999999999")
] + [
    {"services": {"godot-builder": {"environment": QUOTAS}, "aitelier": {}}},
    {"services": {"godot-builder": {"environment": QUOTAS, "image": "other"}}},
    {"services": {"godot-builder": {"environment": QUOTAS, "profiles": ["display"]}}},
    {"services": {"godot-builder": {"environment": QUOTAS}}, "name": "other"},
    {"services": {"godot-builder": {"environment": {**QUOTAS, "OTHER": "1920x1080"}}}},
    {"services": {"godot-builder": {
        "environment": {**QUOTAS, "GODOT_RETAIN_MAX_FILES": "0",
                        "GODOT_PLAYTEST_RES": "1920x1080"}}}},
    {"services": {"godot-builder": {"environment": {
        key: value for key, value in QUOTAS.items() if key != "GODOT_RETAIN_MAX_FILES"}}}},
    {"services": {"godot-builder": {"environment": [
        *(f"{key}={value}" for key, value in QUOTAS.items()),
        "GODOT_PLAYTEST_RES=1920x1080"]}}},
    None, [],
])
def test_normal_godot_configuration_refuses_before_deployment(
        normal, bound, tmp_path, monkeypatch, configuration):
    args = godot_args(tmp_path, bound, monkeypatch)
    Path(args["override_file"]).write_text(json.dumps(configuration))
    journal = dq.evidence_path()
    journal_before = journal.read_bytes() if journal.exists() else None
    def unexpected(*_a, **_k):
        pytest.fail("invalid configuration reached a deployment boundary")
    for name in ("_require_docker", "_require_deployment_clearance", "_compose",
                 "_godot_identity", "_godot_health", "_finish_deployment"):
        monkeypatch.setattr(server, name, unexpected)
    with pytest.raises(ValueError, match="override may only set the four owned finite Godot quotas"):
        server.recreate_godot_builder(**args, override_decision=unexpected)
    assert normal[1] == [] and not Path(args["report_file"]).exists()
    assert (journal.read_bytes() if journal.exists() else None) == journal_before


@pytest.mark.parametrize("changed_resolution", [None, "1280x720"])
def test_normal_godot_display_configuration_is_frozen_after_clearance(
        normal, bound, tmp_path, monkeypatch, changed_resolution):
    db = normal[0][1]
    seed_registry(db)
    before = registry_rows(db)
    args = godot_args(tmp_path, bound, monkeypatch)
    path = Path(args["override_file"])
    path.write_text(json.dumps({"services": {"godot-builder": {
        "environment": {**QUOTAS, "GODOT_PLAYTEST_RES": "1920x1080"}}}}))
    observe_identity = server._godot_identity
    def identity():
        assert_fence_held()
        assert latest()["status"] == "overridden"
        environment = dict(QUOTAS)
        if changed_resolution is not None:
            environment["GODOT_PLAYTEST_RES"] = changed_resolution
        path.write_text(json.dumps({"services": {"godot-builder": {"environment": environment}}}))
        return observe_identity()
    monkeypatch.setattr(server, "_godot_identity", identity)
    seen = []
    def decide(action, snapshot):
        assert_fence_held()
        seen.append(snapshot)
        return override(action, snapshot, godot_only=True, coordinated_owners=names_for(snapshot))
    with pytest.raises(ValueError, match="owned configuration changed before effect"):
        server.recreate_godot_builder(**args, override_decision=decide)
    assert len(seen) == 1 and normal[1] == []
    assert registry_rows(db) == before
    events = json.loads(dq.evidence_path().read_text())["events"]
    assert events[-2]["status"] == "overridden" and events[-2]["audit"]["godot_only"] is True
    assert events[-1]["status"] == "aborted" and events[-1]["usable"] is False
    assert events[-1]["inventory_digest"] == seen[0]["digest"]
    assert events[-1]["blockers"] == seen[0]["blockers"]
    assert events[-1]["errors"] == seen[0]["errors"]
    report = json.loads(Path(args["report_file"]).read_text())
    assert "owned configuration changed before effect" in report["error"]
    assert report["journal"]["event"] == events[-1]
