"""Real isolated State + SkillFlow correction flow; no worker, network or production path."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from api.state_http import create_state_router
from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from core.state_claims import ClaimError
from core.state_commands import describe, execute
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


ACTION = "retire_context_run_protection"


def node(key):
    return {"key": key, "goal": "Independent " + key,
            "acceptance": [{"id": "proof", "kind": "test", "description": "Exact proof"}]}


def call(service, action, **args):
    return execute(service, action, args, allow_write=True)


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.db"))
    sf = SkillFlow(str(tmp_path / "engine.db"))
    sf.register_graph(PipelineGraph(name="fixture", begin="work", steps=[StepNode(id="work")]))
    ws = WorkspaceManager(str(tmp_path / "ws"), str(tmp_path / "projects"))
    registry = ConfigRegistry()
    registry.register_one(sf, "fixture", hint_overrides={"repo_mode": "none", "seed_file": "plan.md",
                         "output_step": "work", "scheduler_owned": True})
    admin = StateService(db, ws, sf, registry, actor="owner:test", is_admin=True, project_read_trusted=True)
    for pid in ("local", "foreign"):
        admin.create_project(pid, pid)
        admin.store.add_nodes(pid, [node("source"), node("other")])
    attempt = admin.attempts.reserve("foreign", "source", 1, "fixture", "foreign-work")
    rid = sf.create_run("fixture", project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    admin.attempts.bind_run(attempt["attempt_id"], rid, sf)
    member = StateService(db, ws, sf, registry, actor="driver:member", driver_id="member", project_read_trusted=True)
    ref = call(member, "add_reference", project_id="local", node_key="source", reference_id="context",
               kind="run", ref=rid, label="Context only, never execution", provenance_actor="auditor", protect=False)
    args = dict(project_id="local", node_key="source", reference_id="context", run_id=rid,
                expected_revision=1, expected_payload_hash=ref["payload_hash"], request_key="correct-context",
                reason="Audit context belongs wholly to another State project")
    yield SimpleNamespace(db=db, sf=sf, admin=admin, member=member, args=args, ref=ref, rid=rid, attempt=attempt)
    sf._conn.close()


def snapshot(system):
    # Only reads from this test's isolated handle; covers immutable bytes and owners.
    with system.admin.store.transaction() as conn:
        return {table: [dict(row) for row in conn.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("state_history_links", "state_nodes", "state_attempts", "state_node_holds",
                              "state_node_claims", "state_external_owners")}


def test_baseline_active_context_blocks_ordinary_release(system):
    s = system
    assert s.ref["protection"] == 1
    held = s.admin.store.get_node("local", "source")["node_hold"]
    with pytest.raises(StateConflict, match="active or unknown"):
        call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
             expected_revision=held["revision"], reason="Need independent Source review")
    assert s.sf.get_run(s.rid)["status"] == "running"


def test_normal_typed_flow_retires_only_context_then_explicit_release_and_admission(system):
    s = system
    before = snapshot(s)
    run = s.sf.get_run(s.rid)
    audit = s.sf.audit_operation_owners(s.rid)
    with pytest.raises(StateConflict, match="active or unknown"):
        call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
             expected_revision=1, reason="Baseline refusal")
    correction = call(s.admin, ACTION, **s.args)
    assert correction["actor"] == "owner:test" and correction["effective_protection"] == 0
    assert correction["foreign_project_id"] == "foreign"
    assert correction["foreign_attempt_id"] == s.attempt["attempt_id"]
    assert snapshot(s) == before
    assert s.sf.get_run(s.rid) == run
    assert {key: s.sf.audit_operation_owners(s.rid)[key] for key in ("alive", "lost", "unknown")} == {
        key: audit[key] for key in ("alive", "lost", "unknown")}
    assert s.admin.store.get_node("local", "source")["readiness"] == "held"
    ref = execute(s.admin, "references", {"project_id": "local"})["references"][0]
    assert {key: ref[key] for key in s.ref} == s.ref
    assert ref["protection"] == 1 and ref["effective_protection"] == 0 and ref["protection_retired"]
    assert ref["protection_retirement"]["request_key"] == s.args["request_key"]
    assert s.admin.project_visibility("local")["visibility"] == "private"
    call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
         expected_revision=1, reason="Explicit owner release", override_reason="Independent Source work")
    claim = call(s.member, "claim_node", project_id="local", node_key="source", purpose="implement",
                 expected_revision=1, request_key="source-admission", workspace="isolated:test")
    assert claim["node_key"] == "source"
    # Reservation/claim admission only: never launch or reconcile the foreign run.
    attempt = s.admin.attempts.reserve("local", "source", 1, "fixture", "admitted-after-correction")
    assert attempt["status"] == "reserved" and attempt["run_id"] is None
    assert s.sf.get_run(s.rid) == run


@pytest.mark.parametrize("changes", [
    {"expected_revision": 2}, {"expected_payload_hash": "0" * 64},
    {"node_key": "other"}, {"reference_id": "missing"}, {"run_id": "missing"},
    {"project_id": "foreign"}, {"reason": "   "}, {"request_key": ""},
])
def test_wrong_identity_cas_or_required_inputs_write_nothing(system, changes):
    s = system
    before, events = snapshot(s), s.admin.store.events("local")
    with pytest.raises((StateGraphError, StateConflict)):
        call(s.admin, ACTION, **(s.args | changes))
    assert snapshot(s) == before and s.admin.store.events("local") == events
    assert execute(s.admin, "references", {"project_id": "local"})["references"][0]["effective_protection"] == 1


def test_nonadmin_and_anonymous_cannot_correct_even_with_spoofed_labels(system):
    s = system
    before = snapshot(s)
    for service in (s.member, StateService(s.db, actor="owner:spoof", is_admin=True, project_read_trusted=False)):
        with pytest.raises(ClaimError, match="admin_required"):
            call(service, ACTION, **s.args)
    with pytest.raises(StateGraphError):
        call(s.admin, ACTION, **(s.args | {"is_admin": True, "foreign_project_id": "foreign"}))
    assert snapshot(s) == before


def test_correction_does_not_compose_or_call_the_runtime(system):
    s = system
    def forbidden():
        raise AssertionError("correction must use durable State binding, never runtime")
    offline = StateService(s.db, actor="owner:test", is_admin=True, project_read_trusted=True,
                           runtime_factory=forbidden)
    assert call(offline, ACTION, **s.args)["effective_protection"] == 0
    assert offline.sf is None


@pytest.mark.parametrize("binding", ["same-project", "legacy-unbound", "unknown"])
def test_only_real_foreign_state_binding_authorizes_retirement(system, binding):
    s = system
    if binding == "same-project":
        attempt = s.admin.attempts.reserve("local", "other", 1, "fixture", "own-work")
        rid = s.sf.create_run("fixture", project_id=attempt["execution_project_id"])
        s.sf.start_run(rid)
        s.admin.attempts.bind_run(attempt["attempt_id"], rid, s.sf)
    else:
        rid = s.sf.create_run("fixture", project_id="legacy-not-a-state-binding")
        s.sf.start_run(rid)
        if binding == "unknown":
            rid = "missing-run"
    # Portfolio leaf lets us construct old unknown history without claiming it is a binding.
    ref = s.admin.portfolio.add_reference("local", "source", "negative", "run", rid,
                "foreign project=foreign; owner=foreign; contextual", "owner:label", protect=True)
    before = snapshot(s)
    with pytest.raises(StateConflict, match="binding wholly to another"):
        call(s.admin, ACTION, **(s.args | {"reference_id": "negative", "run_id": rid,
                                         "expected_payload_hash": ref["payload_hash"]}))
    assert snapshot(s) == before


def test_other_effective_protection_still_blocks_release(system):
    s = system
    rid = s.sf.create_run("fixture", project_id="legacy-still-working")
    s.sf.start_run(rid)
    call(s.admin, "add_reference", project_id="local", node_key="source", reference_id="other",
         kind="run", ref=rid, label="Still active", provenance_actor="tester")
    call(s.admin, ACTION, **s.args)
    with pytest.raises(StateConflict, match="active or unknown"):
        call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
             expected_revision=2, reason="Other protection must remain")
    assert s.sf.get_run(rid)["status"] == "running"


def test_retirement_does_not_bypass_foreign_placer_override(system):
    s = system
    call(s.admin, "set_claim_enforcement", project_id="local", claim_enforcement="on",
         expected_revision=0, reason="Exercise ordinary foreign placer guard")
    call(s.admin, ACTION, **s.args)
    other = StateService(s.db, actor="driver:other", driver_id="other", project_read_trusted=True)
    release = dict(project_id="local", node_key="source", held=False, expected_revision=1,
                   reason="Ordinary release after correction")
    with pytest.raises(ClaimError, match="override_reason_required"):
        call(other, "set_node_hold", **release)
    assert call(other, "set_node_hold", **(release | {"override_reason": "Independent Source review"}))["held"] == 0


@pytest.mark.parametrize("invalid_audit", [
    {"alive": 1, "lost": [], "unknown": []},
    {"alive": 0, "lost": ["retained-first-failure"], "unknown": []},
    {"alive": 0, "lost": [], "unknown": ["unobserved-owner"]},
])
def test_other_protected_run_operations_still_block_release(system, monkeypatch, invalid_audit):
    s = system
    rid = s.sf.create_run("fixture", project_id="other-protected")
    s.sf.start_run(rid)
    call(s.admin, "add_reference", project_id="local", node_key="source", reference_id="other",
         kind="run", ref=rid, label="Still owns operations", provenance_actor="tester")
    s.sf.fail_run(rid, "Fixture terminal run with unresolved operation witness")
    call(s.admin, ACTION, **s.args)
    monkeypatch.setattr(s.sf, "audit_operation_owners", lambda run: invalid_audit)
    with pytest.raises(StateConflict, match="unretired/unknown operations"):
        call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
             expected_revision=2, reason="Operation owners still protected")


@pytest.mark.parametrize("kind,protect", [("note", True), ("run", False)])
def test_nonrun_and_unprotected_references_cannot_be_retired(system, kind, protect):
    s = system
    if kind == "run":
        s.sf.fail_run(s.rid, "Fixture completed before historical link")
    ref = call(s.admin, "add_reference", project_id="local", node_key="source", reference_id="not-protected-run",
               kind=kind, ref=s.rid, label="Not eligible", provenance_actor="tester", protect=protect)
    before = snapshot(s)
    with pytest.raises(StateConflict, match="exact protected run reference"):
        call(s.admin, ACTION, **(s.args | {"reference_id": ref["reference_id"],
                                         "expected_payload_hash": ref["payload_hash"]}))
    assert snapshot(s) == before


def test_replay_persistence_append_only_and_drift(system):
    s = system
    one = call(s.admin, ACTION, **s.args)
    events = s.admin.store.events("local")
    reopened = StateService(s.db, sf=s.sf, actor="owner:test", is_admin=True, project_read_trusted=True)
    assert call(reopened, ACTION, **s.args) == one
    assert s.admin.store.events("local") == events
    for args in (s.args | {"reason": "different"}, s.args | {"expected_revision": 2},
                 s.args | {"request_key": "different"}):
        with pytest.raises(StateConflict):
            call(reopened, ACTION, **args)
    other = StateService(s.db, actor="owner:other", is_admin=True, project_read_trusted=True)
    with pytest.raises(StateConflict):
        call(other, ACTION, **s.args)
    # Retrying the original add_reference cannot re-enable its effective protection.
    original = call(s.member, "add_reference", project_id="local", node_key="source", reference_id="context",
                    kind="run", ref=s.rid, label="Context only, never execution", provenance_actor="auditor", protect=False)
    assert original == s.ref
    assert execute(reopened, "references", {"project_id": "local"})["references"][0]["effective_protection"] == 0
    with s.db.get_connection() as conn:
        for table in ("state_context_run_protection_retirements", "state_history_links"):
            for command in ("DELETE FROM " + table, "UPDATE " + table + " SET reference_id='changed'"):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(command)
    assert s.admin.store.events("local") == events
    call(s.admin, "revise_node", project_id="local", node_key="source", expected_revision=1,
         reason="Later independent contract revision", goal="Independent source with new goal")
    assert call(reopened, ACTION, **s.args) == one


def test_rollback_and_two_request_race(system, monkeypatch):
    s = system
    original = s.admin.store._event
    def broken(conn, pid, key, kind, payload):
        if kind == "context_run_protection_retired":
            raise RuntimeError("first failure before commit")
        return original(conn, pid, key, kind, payload)
    monkeypatch.setattr(s.admin.store, "_event", broken)
    with pytest.raises(RuntimeError, match="first failure"):
        call(s.admin, ACTION, **s.args)
    refs = execute(s.admin, "references", {"project_id": "local"})["references"]
    assert refs[0]["effective_protection"] == 1
    monkeypatch.setattr(s.admin.store, "_event", original)
    def change(request):
        try:
            return call(s.admin, ACTION, **(s.args | {"request_key": request}))["request_key"]
        except StateConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, ("race-a", "race-b")))
    assert outcomes.count("conflict") == 1
    assert len([event for event in s.admin.store.events("local")
                if event["event_type"] == "context_run_protection_retired"]) == 1


def test_public_effective_view_omits_private_retirement_witness(system):
    s = system
    call(s.admin, ACTION, **s.args)
    call(s.admin, "open_project", project_id="local")
    reader = StateService(s.db, project_read_trusted=False)
    ref = execute(reader, "references", {"project_id": "local"})["references"][0]
    assert ref["protection"] == 1 and ref["effective_protection"] == 0
    assert "protection_retirement" not in ref
    assert "foreign_attempt_id" not in json.dumps(ref)
    assert s.admin.project_visibility("foreign")["visibility"] == "private"
    context = execute(reader, "get_node", {"project_id": "local", "node_key": "source"})
    assert context["references"]["references"][0]["effective_protection"] == 0
    assert "protection_retirement" not in context["references"]["references"][0]


def test_protected_insertion_during_release_observation_is_not_lost(system, monkeypatch):
    s = system
    terminal = s.sf.create_run("fixture", project_id="terminal-protected")
    s.sf.start_run(terminal)
    call(s.admin, "add_reference", project_id="local", node_key="source", reference_id="terminal",
         kind="run", ref=terminal, label="Terminal witness", provenance_actor="tester")
    s.sf.fail_run(terminal, "Fixture terminal")
    call(s.admin, ACTION, **s.args)
    incoming = s.sf.create_run("fixture", project_id="new-protection")
    s.sf.start_run(incoming)
    original = s.sf.audit_operation_owners
    def insert_during_audit(run):
        result = original(run)
        call(s.admin, "add_reference", project_id="local", node_key="source", reference_id="incoming",
             kind="run", ref=incoming, label="Arrived during observation", provenance_actor="tester")
        return result
    monkeypatch.setattr(s.sf, "audit_operation_owners", insert_during_audit)
    with pytest.raises(StateConflict, match="protected references changed|hold revision changed"):
        call(s.admin, "set_node_hold", project_id="local", node_key="source", held=False,
             expected_revision=2, reason="Stale snapshot must refuse")
    held = s.admin.store.get_node("local", "source")["node_hold"]
    assert held["held"] == 1 and held["revision"] == 3


def test_http_schema_auth_and_typed_command_surface(system):
    s = system
    app = FastAPI()
    def writer(request: Request):
        if request.headers.get("authorization") not in {"Admin", "Member"}:
            raise HTTPException(403, "authentication required")
    def service(request: Request):
        return s.admin if request.headers.get("authorization") == "Admin" else s.member
    app.include_router(create_state_router(service, writer))
    shape = describe()["operations"][ACTION]["arguments"]
    assert set(s.args) <= set(shape["required"])
    with TestClient(app) as client:
        assert client.post("/api/state/commands/" + ACTION, json=s.args).status_code == 403
        member = client.post("/api/state/commands/" + ACTION, json=s.args, headers={"authorization": "Member"})
        assert member.status_code in (409, 422) and "admin_required" in member.text
        assert client.post("/api/state/commands/" + ACTION, json=s.args,
                           headers={"authorization": "Admin"}).status_code == 200
