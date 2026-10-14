"""Backend cutover decisions preserve inventories and physical-effect refusal."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from core import deployment_quiescence as dq


def packet():
    ready = {"run_id": "retained-demand", "root": "/worktrees/retained",
             "source": "/source", "desired": "ready", "outcome": "ready",
             "revision": 2, "done_revision": 2, "activity_at": 1.0, "error": ""}
    controller = {"attempt_id": "deployment-current", "project_id": "aitelier",
                  "node_key": "deployment.reviewed-backend-cutover",
                  "owner_driver_id": "director", "status": "active", "settled_at": None}
    noise = {"kind": "process", "command": "tailscaled be-child ssh remote stdin-bash",
             "active": True, "resource": "external_measurement", "ownership": "unregistered"}
    observation = {"schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
                   "projects": ["aitelier"], "runs": [], "sidecar_owners": [ready],
                   "external_owners": [noise], "registered_external_owners": [controller],
                   "godot_render_owners": [],
                   "blockers": {"sidecar_owners": [ready], "external_active": [noise],
                                "registered_external_owners": [controller]},
                   "errors": [dq.UNKNOWN_PROCESS_ERROR_PREFIX + noise["command"]],
                   "quiescent": False}
    observation["blockers"]["external_active"] = observation["external_owners"]
    observation["blockers"]["registered_external_owners"] = observation["registered_external_owners"]
    return seal(observation)


def seal(observation):
    observation["digest"] = dq._observation_digest(observation)
    assert dq._validate_observation(observation) is None
    return observation


def decision(observation, **changes):
    return {"action": "restart", "actor": "driver:director", "reason": "backend-only owner decision",
            "ticket": "explicit-owner-coordination", "inventory_digest": observation["digest"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "acknowledge_unknown": True, "backend_only": True,
            "deployment_attempt_id": "deployment-current", **changes}


def refuse(tmp_path, observation, *, override=None, backend_only=True, match=None):
    with pytest.raises(dq.DeploymentBlocked, match=match):
        dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                     override=decision(observation) if override is None else override,
                     backend_only=backend_only)
    latest = dq._load_journal(tmp_path / "journal.json")["latest"]
    assert latest["status"] == "aborted" and latest["usable"] is False
    return latest


def test_mixed_passive_inventory_with_unknown_ack_is_bound_and_retained(tmp_path):
    observation = packet()
    before = deepcopy(observation)
    authoritative, unknowns = dq._classify_blockers(
        observation, backend_only=True, deployment_attempt_id="deployment-current",
        actor="driver:director")
    assert authoritative == {} and unknowns == observation["external_owners"]
    assert dq._classify_blockers(observation)[0]["sidecar_owners"]
    clearance = dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                             override=decision(observation), backend_only=True)
    event = clearance["event"]
    assert event["status"] == "overridden" and clearance["allowed"] is True
    assert event["inventory_digest"] == observation["digest"]
    assert event["blockers"] == observation["blockers"] and event["errors"] == observation["errors"]
    assert event["audit"]["backend_only"] is True
    assert event["audit"]["deployment_attempt_id"] == "deployment-current"
    assert event["audit"]["affected_ownership"] == observation["external_owners"]
    assert observation == before
    dq.finalize(clearance, success=False, error="CPU decision only; no deployment", journal=tmp_path / "journal.json")


@pytest.mark.parametrize("change", [
    {"acknowledge_unknown": False}, {"inventory_digest": "0" * 64},
    {"action": "redeploy"}, {"expires_at": "2000-01-01T00:00:00+00:00"},
    {"expires_at": "2100-01-01"}, {"actor": "driver:other"},
    {"backend_only": "true"}, {"deployment_attempt_id": "foreign"},
])
def test_invalid_or_unacknowledged_decisions_refuse(tmp_path, change):
    observation = packet()
    refuse(tmp_path, observation, override=decision(observation, **change))


def test_audit_scope_cannot_grant_trusted_caller_scope(tmp_path):
    observation = packet()
    refuse(tmp_path, observation, backend_only=False, match="trusted backend caller")


@pytest.mark.parametrize("change", [
    {"owner_driver_id": ""}, {"owner_driver_id": 1}, {"project_id": "foreign"},
    {"node_key": "research.cutover-name"}, {"status": "settled"},
])
def test_controller_identity_is_exact_and_authority_is_not_a_name(tmp_path, change):
    observation = packet()
    observation["registered_external_owners"][0].update(change)
    seal(observation)
    refuse(tmp_path, observation, match="exact recorded owner")


@pytest.mark.parametrize("status,kind,settled", [
    ("settled", None, "2026-10-01T00:00:00+00:00"),
    ("abandoned", "confirmed_stopped", "2026-10-01T00:00:00+00:00"),
])
def test_explicit_state_settlement_is_passive(tmp_path, status, kind, settled):
    observation = packet()
    row = {"attempt_id": "old-recovery", "status": status,
           "abandon_kind": kind, "settled_at": settled}
    observation["registered_external_owners"].append(row)
    seal(observation)
    result = dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                          override=decision(observation), backend_only=True)
    assert result["event"]["blockers"]["registered_external_owners"][-1] == row


@pytest.mark.parametrize("status,kind,settled", [
    ("paused", None, None), ("abandoned", "unknown", "2026-10-01T00:00:00+00:00"),
    ("abandoned", "confirmed_stopped", None), ("settled", None, None),
    ("unknown", None, None),
    ("settled", None, "malformed"), ("settled", None, "2100-01-01T00:00:00+00:00"),
    ("abandoned", "confirmed_stopped", "2026-10-01T00:00:00"),
])
@pytest.mark.parametrize("acknowledge", [False, True])
def test_uncertain_recovery_is_unknown_and_acknowledgement_is_explicit(tmp_path, status, kind, settled, acknowledge):
    observation = packet()
    row = {"attempt_id": "foreign-recovery", "status": status,
           "abandon_kind": kind, "settled_at": settled}
    observation["registered_external_owners"].append(row)
    seal(observation)
    authoritative, unknowns = dq._classify_blockers(
        observation, backend_only=True, deployment_attempt_id="deployment-current",
        actor="driver:director")
    assert row not in authoritative.get("registered_external_owners", [])
    assert row in unknowns
    override = decision(observation, acknowledge_unknown=acknowledge)
    if acknowledge:
        result = dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                              override=override, backend_only=True)
        event = result["event"]
        assert event["audit"]["override_scope"] == "unknown_ownership"
        assert row in event["audit"]["affected_ownership"]
    else:
        event = refuse(tmp_path, observation, override=override, match="acknowledge unknown")
    assert row in event["blockers"]["registered_external_owners"]


@pytest.mark.parametrize("effect", ["run", "alive", "lost", "unknown", "render", "render-lost",
                                    "lease", "write", "external", "controller-child", "pending",
                                    "revision", "semantic-error"])
def test_real_effects_survive_backend_ack(tmp_path, effect):
    observation = packet()
    if effect in {"run", "alive", "lost", "unknown"}:
        audit = {"alive": int(effect == "alive"), "lost": ["lost-op"] if effect == "lost" else [],
                 "unknown": ["opaque-op"] if effect == "unknown" else []}
        row = {"run_id": "real", "project_id": "foreign", "status": "running",
               "active_operations": int(effect != "run"), "audit": audit}
        observation["runs"] = [row]
        observation["blockers"]["active_runs"] = [row]
        observation["blockers"]["active_operations"] = [row] if effect != "run" else []
    elif effect in {"render", "render-lost"}:
        row = {"owner_id": "render-1", "status": "active" if effect == "render" else "owner_lost"}
        observation["godot_render_owners"] = [row]
        observation["blockers"]["godot_render_owners"] = [row]
    elif effect in {"lease", "write"}:
        observation["blockers"]["checkout_leases"] = [{"owner": "writer", "canonical_checkout": "/source"}]
    elif effect in {"external", "controller-child"}:
        row = {"kind": "process", "command": "python /source/benchmark.py", "active": True}
        if effect == "controller-child":
            row.update(attempt_id="deployment-current", ownership="registered")
        observation["external_owners"].append(row)
    else:
        row = observation["sidecar_owners"][0]
        if effect == "pending": row["outcome"] = "pending"
        if effect == "revision": row["done_revision"] = 1
        if effect == "semantic-error": row["error"] = "worker error"
    seal(observation)
    refuse(tmp_path, observation, match="authoritative")


@pytest.mark.parametrize("error", ["docker inventory failed: unavailable",
                                   "process lineage malformed", "semantic daemon operations are active",
                                   "semantic operation ownership is unknown", "partial runtime inventory"])
def test_inventory_errors_cannot_be_acknowledged_as_process_noise(tmp_path, error):
    observation = packet()
    observation["errors"].append(error)
    seal(observation)
    refuse(tmp_path, observation, match="authoritative")


def test_duplicate_controller_and_forged_inventory_refuse(tmp_path):
    observation = packet()
    observation["registered_external_owners"].append(deepcopy(observation["registered_external_owners"][0]))
    seal(observation)
    refuse(tmp_path, observation, match="exact recorded owner")
    observation = packet()
    observation["digest"] = "0" * 64
    refuse(tmp_path, observation, match="digest")


def test_backend_scope_without_noise_still_cannot_override_active_owner(tmp_path):
    observation = packet()
    observation["external_owners"].clear()
    observation["errors"].clear()
    row = {"attempt_id": "active-foreign", "status": "active"}
    observation["registered_external_owners"].append(row)
    seal(observation)
    refuse(tmp_path, observation, override=decision(observation, acknowledge_unknown=False),
           match="authoritative")


@pytest.mark.parametrize("missing", ["absent", "null"])
@pytest.mark.parametrize("acknowledge", [False, True])
@pytest.mark.parametrize("process_noise", [False, True])
def test_missing_controller_owner_is_unknown_not_proved_own(tmp_path, missing, acknowledge, process_noise):
    observation = packet()
    if not process_noise:
        observation["external_owners"].clear()
        observation["errors"].clear()
    controller = observation["registered_external_owners"][0]
    if missing == "absent":
        controller.pop("owner_driver_id")
    else:
        controller["owner_driver_id"] = None
    seal(observation)
    before = deepcopy(observation)
    authoritative, unknowns = dq._classify_blockers(
        observation, backend_only=True, deployment_attempt_id="deployment-current",
        actor="driver:director")
    assert controller not in authoritative.get("registered_external_owners", [])
    assert controller in unknowns
    override = decision(observation, acknowledge_unknown=acknowledge)
    if acknowledge:
        event = dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                             override=override, backend_only=True)["event"]
        assert event["audit"]["deployment_attempt_id"] == "deployment-current"
        assert event["audit"]["deployment_controller_identity"] == "unknown"
        assert event["audit"]["override_scope"] == "unknown_ownership"
        assert controller in event["audit"]["affected_ownership"]
    else:
        event = refuse(tmp_path, observation, override=override, match="acknowledge unknown")
    assert event["blockers"] == observation["blockers"]
    assert event["inventory_digest"] == observation["digest"] and observation == before


@pytest.mark.parametrize("status,abandon", [("paused", None), ("abandoned", "unknown")])
@pytest.mark.parametrize("effect", ["child", "operation", "error"])
def test_acknowledged_recovery_unknown_cannot_bypass_physical_effects(tmp_path, status, abandon, effect):
    observation = packet()
    observation["registered_external_owners"].append(
        {"attempt_id": "recovery", "status": status, "abandon_kind": abandon, "settled_at": None})
    if effect == "child":
        observation["external_owners"].append(
            {"kind": "process", "command": "ssh host bash -s < work", "active": True,
             "ownership": "registered", "attempt_id": "recovery"})
    elif effect == "operation":
        row = {"run_id": "work", "project_id": "foreign", "status": "running",
               "active_operations": 1, "audit": {"alive": 1, "lost": [], "unknown": []}}
        observation["runs"].append(row)
        observation["blockers"]["active_runs"] = [row]
        observation["blockers"]["active_operations"] = [row]
    else:
        observation["errors"].append("recovery effect inventory unavailable")
    seal(observation)
    refuse(tmp_path, observation, match="authoritative")


def test_foreign_active_registry_is_not_recovery_uncertainty(tmp_path):
    observation = packet()
    observation["registered_external_owners"].append(
        {"attempt_id": "actual-active-foreign", "status": "active"})
    seal(observation)
    refuse(tmp_path, observation, match="authoritative")


@pytest.mark.parametrize("status,abandon", [("paused", None), ("abandoned", "unknown")])
@pytest.mark.parametrize("acknowledge", [False, True])
def test_recovery_uncertainty_requires_ack_without_process_noise(tmp_path, status, abandon, acknowledge):
    observation = packet()
    observation["external_owners"].clear()
    observation["errors"].clear()
    row = {"attempt_id": "recovery", "status": status, "abandon_kind": abandon}
    observation["registered_external_owners"].append(row)
    seal(observation)
    override = decision(observation, acknowledge_unknown=acknowledge)
    if acknowledge:
        event = dq.authorize("restart", observation, journal=tmp_path / "journal.json",
                             override=override, backend_only=True)["event"]
        assert event["audit"]["affected_ownership"] == [row]
        assert event["audit"]["override_scope"] == "unknown_ownership"
    else:
        refuse(tmp_path, observation, override=override, match="acknowledge unknown")
