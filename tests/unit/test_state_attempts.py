"""Real SkillFlow + SQLite scenarios; no manufactured completed state nodes."""
import hashlib
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode

from core.db_manager import DBManager
from core.state_graph import StateConflict, StateGraphError, StateGraphStore
from core.state_attempts import StateAttempts

ARTIFACT = "a" * 40
REPORT = hashlib.sha256(b"isolated test evidence\n").hexdigest()


def spec(k, deps=None):
    return {"key": k, "goal": "Implement " + k, "dependencies": deps or [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour validated"},
        {"id": "review", "kind": "review", "description": "Source and scope reviewed"}]}


@pytest.fixture
def system(tmp_path):
    store = StateGraphStore(DBManager(str(tmp_path / "state.db")), project_read_trusted=True)
    store.create_project("game", "Long-running game")
    store.add_nodes("game", [spec("a"), spec("b", ["a"]), spec("c", ["b"])])
    attempts = StateAttempts(store)
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(PipelineGraph(name="feature", begin="implementation", steps=[StepNode(id="implementation")]))
    yield store, attempts, sf
    # SkillFlow 1.5.67 has no public close(); this is only fixture teardown.
    sf._conn.close()


def reserve(system, nk="a", request="request1", revision=1):
    return system[1].reserve("game", nk, revision, "feature", request)


def launch(system, attempt):
    _, attempts, sf = system
    rid = sf.create_run(attempt["workflow"], project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    attempts.bind_run(attempt["attempt_id"], rid, sf)
    return rid


def finish(system, attempt, artifact=ARTIFACT):
    _, attempts, sf = system
    rid = attempt.get("run_id") or launch(system, attempt)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert claimed is not None, "fixture must execute a real step"
    sf.confirm_step(claimed.token, StepResult(outputs={"implementation": "candidate"}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    return attempts.reconcile(attempt["attempt_id"], sf, artifact)


def attest(system, attempt, check, verdict="pass", suffix="", artifact=ARTIFACT):
    return system[1].record_evidence(attempt["attempt_id"], attempt["attempt_id"] + "-" + check + suffix,
        check, verdict, artifact, "reports/" + check + suffix + ".json", REPORT, "isolated-verifier", "Explicit fixture attestation")


def accept(system, attempt, artifact=ARTIFACT):
    for criterion in ["behaviour", "review"]:
        attest(system, attempt, criterion, artifact=artifact)
    return system[1].verify("game", attempt["node_key"], attempt["node_revision"], attempt["attempt_id"], "independent-review-fixture")


def test_real_run_completion_is_candidate_not_fact(system):
    store, attempts, _ = system
    a = finish(system, reserve(system))
    assert a["status"] == "candidate"
    assert store.get_node("game", "a")["status"] == "CANDIDATE"
    assert store.get_node("game", "b")["readiness"] == "blocked"
    with pytest.raises(StateConflict, match="evidence"):
        attempts.verify("game", "a", 1, a["attempt_id"], "reviewer")
    accept(system, a)
    assert store.get_node("game", "a")["status"] == "VERIFIED"
    assert store.get_node("game", "b")["readiness"] == "ready"
    assert store.get_node("game", "c")["readiness"] == "blocked"


def test_request_retry_and_launch_claim_are_idempotent(system):
    store, attempts, _ = system
    first = reserve(system)
    assert first == reserve(system)
    with pytest.raises(StateConflict):
        attempts.reserve("game", "a", 1, "feature", "request1", instruction="different")
    assert attempts.claim_launch(first["attempt_id"]) is True
    assert attempts.claim_launch(first["attempt_id"]) is False
    assert store.get_node("game", "a")["readiness"] == "in_progress"
    with pytest.raises(StateConflict):
        reserve(system, request="different-id")


def test_competing_requests_have_one_active_owner(system):
    def do(request):
        try:
            return reserve(system, request=request)["attempt_id"]
        except StateConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(do, ["r1", "r2"]))
    assert sum(r is not None for r in results) == 1
    assert len(system[1].list("game", "a")) == 1


def test_unknown_launch_stays_reserved_until_run_recovered(system):
    _, attempts, sf = system
    a = reserve(system)
    attempts.claim_launch(a["attempt_id"])
    attempts.launch_uncertain(a["attempt_id"], "Transport ended before response")
    assert attempts.get(a["attempt_id"])["status"] == "unknown"
    assert attempts.claim_launch(a["attempt_id"]) is False
    with pytest.raises(StateConflict):
        reserve(system, request="replacement")
    rid = launch(system, a)
    assert attempts.get(a["attempt_id"])["run_id"] == rid
    assert sf.get_run_by_project(a["execution_project_id"])["id"] == rid


def test_bind_refuses_unknown_or_other_project_run(system):
    _, attempts, sf = system
    a = reserve(system)
    with pytest.raises(StateConflict, match="unknown"):
        attempts.bind_run(a["attempt_id"], "does-not-exist", sf)
    rid = sf.create_run("feature", project_id="other-project")
    with pytest.raises(StateConflict, match="different execution"):
        attempts.bind_run(a["attempt_id"], rid, sf)
    assert attempts.get(a["attempt_id"])["run_id"] is None


def test_run_binding_cannot_be_replaced(system):
    _, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    assert attempts.bind_run(a["attempt_id"], rid, sf)["run_id"] == rid
    other = sf.create_run("feature", project_id=a["execution_project_id"])
    with pytest.raises(StateConflict):
        attempts.bind_run(a["attempt_id"], other, sf)


def test_pause_keeps_attempt_active_and_cannot_verify(system):
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.pause_run(rid)
    assert attempts.reconcile(a["attempt_id"], sf)["status"] == "paused"
    assert store.get_node("game", "a")["readiness"] == "in_progress"
    with pytest.raises(StateConflict):
        attempts.verify("game", "a", 1, a["attempt_id"], "operator")
    assert sf.get_run(rid)["status"] == "paused", "state code must never approve a checkpoint"


def test_failed_run_retains_history_and_allows_new_attempt(system):
    _, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.fail_run(rid, "The implementation failed")
    assert attempts.reconcile(a["attempt_id"], sf)["status"] == "failed"
    b = reserve(system, request="second-attempt")
    assert b["attempt_id"] != a["attempt_id"]
    assert len(attempts.list("game", "a")) == 2


def test_new_spec_rejects_late_result_from_old_run(system):
    store, attempts, _ = system
    a = reserve(system)
    launch(system, a)
    a = attempts.get(a["attempt_id"])
    store.revise_node("game", "a", 1, "Owner changed the requirement", goal="New design")
    # An already-running attempt may finish, but not certify the new revision.
    a = finish(system, a)
    assert a["status"] == "superseded"
    assert a["stale_inputs"] is True
    assert store.get_node("game", "a")["status"] == "STALE"
    with pytest.raises(StateConflict):
        attest(system, a, "behaviour")
    assert reserve(system, request="v2", revision=2)["node_revision"] == 2


def test_dependency_revision_invalidates_candidate_and_descendant_receipts(system):
    store, attempts, sf = system
    a = finish(system, reserve(system))
    ra = accept(system, a)
    b = finish(system, reserve(system, "b"))
    rb = accept(system, b)
    c = reserve(system, "c")
    store.revise_node("game", "a", 1, "Change dependency behaviour")
    assert store.get_node("game", "b")["status"] == "STALE"
    assert store.get_node("game", "c")["readiness"] == "in_progress"
    with pytest.raises(StateConflict, match="stale"):
        attempts.verify("game", "b", 1, b["attempt_id"], "reviewer")
    assert attempts.reconcile(b["attempt_id"], sf)["status"] == "superseded"
    with store.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_acceptances WHERE receipt_id IN (?,?)", (ra["receipt_id"], rb["receipt_id"])).fetchone()[0] == 2
    assert c["dependencies"]["b"]["verified_receipt"] == rb["receipt_id"]


@pytest.mark.parametrize("verdict", ["fail", "skip"])
def test_failed_or_skipped_evidence_cannot_unlock_nodes(system, verdict):
    store, attempts, _ = system
    a = finish(system, reserve(system))
    attest(system, a, "behaviour", verdict)
    attest(system, a, "review")
    with pytest.raises(StateConflict, match="behaviour"):
        attempts.verify("game", "a", 1, a["attempt_id"], "operator")
    assert store.get_node("game", "b")["readiness"] == "blocked"


def test_evidence_correction_invalidates_already_accepted_chain(system):
    store, attempts, _ = system
    a = finish(system, reserve(system))
    accept(system, a)
    b = finish(system, reserve(system, "b"))
    accept(system, b)
    attest(system, a, "behaviour", "fail", "-regression")
    assert store.get_node("game", "a")["status"] == "STALE"
    assert store.get_node("game", "b")["status"] == "STALE"
    with pytest.raises(StateConflict, match="behaviour"):
        attempts.verify("game", "a", 1, a["attempt_id"], "reviewer")


def test_evidence_and_acceptance_retries_do_not_duplicate_history(system):
    store, attempts, sf = system
    a = finish(system, reserve(system))
    first = attest(system, a, "behaviour")
    assert attest(system, a, "behaviour") == first
    attest(system, a, "review")
    first = attempts.verify("game", "a", 1, a["attempt_id"], "reviewer")
    before = store.events("game")
    assert attempts.verify("game", "a", 1, a["attempt_id"], "reviewer") == first
    attempts.reconcile(a["attempt_id"], sf, ARTIFACT)
    assert store.events("game") == before
    assert store.get_node("game", "a")["status"] == "VERIFIED"


def test_evidence_cannot_change_payload_under_same_id(system):
    _, attempts, _ = system
    a = finish(system, reserve(system))
    attest(system, a, "behaviour")
    with pytest.raises(StateConflict):
        attest(system, a, "behaviour", "fail")
    assert len(attempts.evidence(a["attempt_id"])) == 1


def test_wrong_artifact_criterion_or_revision_cannot_verify(system):
    _, attempts, sf = system
    a = finish(system, reserve(system))
    with pytest.raises(StateConflict, match="different artifact"):
        attest(system, a, "behaviour", artifact="b" * 40)
    with pytest.raises(StateGraphError, match="criterion"):
        attest(system, a, "non-contract-check")
    with pytest.raises(StateConflict):
        attempts.reconcile(a["attempt_id"], sf, "b" * 40)
    with pytest.raises(StateConflict):
        attempts.verify("game", "a", 2, a["attempt_id"], "operator")
    with pytest.raises(StateConflict):
        attempts.verify("game", "b", 1, a["attempt_id"], "operator")


def test_old_candidate_cannot_be_accepted_after_new_attempt_reserved(system):
    _, attempts, _ = system
    a = finish(system, reserve(system))
    attest(system, a, "behaviour")
    attest(system, a, "review")
    reserve(system, request="new-choice")
    with pytest.raises(StateConflict, match="newer attempt"):
        attempts.verify("game", "a", 1, a["attempt_id"], "operator")


def test_disconnected_driver_can_reopen_and_continue_same_attempt(system):
    store, attempts, sf = system
    a = finish(system, reserve(system))
    fresh_store = StateGraphStore(DBManager(store.db.db_path), project_read_trusted=True)
    fresh = StateAttempts(fresh_store)
    assert fresh.get(a["attempt_id"])["run_id"] == a["run_id"]
    attest((fresh_store, fresh, sf), a, "behaviour")
    attest((fresh_store, fresh, sf), a, "review")
    fresh.verify("game", "a", 1, a["attempt_id"], "new-driver-reviewer")
    assert store.get_node("game", "b")["readiness"] == "ready"


@pytest.mark.parametrize("audit", [None, {}, {"lost": [], "unknown": []}, {"lost": [], "unknown": [], "alive": -1}])
def test_incomplete_quiescence_audit_fails_closed(system, monkeypatch, audit):
    _, attempts, sf = system
    a = finish(system, reserve(system))
    monkeypatch.setattr(sf, "audit_operation_owners", lambda _rid: audit)
    with pytest.raises(StateConflict, match="audit"):
        attempts.reconcile(a["attempt_id"], sf)


def test_admitted_operation_holds_terminal_result(system, monkeypatch):
    _, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult())
    sf.advance_run(rid)
    monkeypatch.setattr(sf, "audit_operation_owners", lambda _rid: {"lost": [], "unknown": ["unobserved"], "alive": 0})
    assert attempts.reconcile(a["attempt_id"], sf)["status"] == "unknown"
    with pytest.raises(StateConflict):
        reserve(system, request="new")


@pytest.mark.parametrize("table", ["state_evidence", "state_acceptances"])
def test_evidence_and_receipts_are_append_only(system, table):
    store, _, _ = system
    a = finish(system, reserve(system))
    accept(system, a)
    with store.db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")


def test_real_git_artifact_and_report_digest_are_preserved(system, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in [["init", "-q", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@localhost"]]:
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "feature.py").write_text("def value(): return 2\n")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "feature"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    a = finish(system, reserve(system), sha)
    receipt = accept(system, a, sha)
    assert receipt["artifact_ref"] == sha
    assert len(json.loads(receipt["evidence_ids"])) == 2
    assert all(e["report_sha256"] == REPORT for e in system[1].evidence(a["attempt_id"]))


def test_stale_unlaunched_reservation_does_not_permanently_block_the_node(system):
    store, attempts, sf = system
    a = reserve(system)
    store.revise_node("game", "a", 1, "New requirement before dispatch")
    assert attempts.claim_launch(a["attempt_id"]) is False
    assert attempts.get(a["attempt_id"])["status"] == "superseded"
    assert sf.get_run_by_project(a["execution_project_id"]) is None
    assert reserve(system, revision=2, request="new-revision")["status"] == "reserved"


def test_run_with_missing_graph_pin_cannot_bind(system, monkeypatch):
    _, attempts, sf = system
    a = reserve(system)
    rid = sf.create_run("feature", project_id=a["execution_project_id"])
    row = sf.get_run(rid)
    row["graph_digest"] = None
    monkeypatch.setattr(sf, "get_run", lambda _rid: row)
    with pytest.raises(StateConflict, match="pin"):
        attempts.bind_run(a["attempt_id"], rid, sf)


def test_operation_audit_error_is_not_a_success(system, monkeypatch):
    _, attempts, sf = system
    a = finish(system, reserve(system))
    def broken(_rid):
        raise OSError("engine unavailable")
    monkeypatch.setattr(sf, "audit_operation_owners", broken)
    with pytest.raises(StateConflict, match="quiescence"):
        attempts.reconcile(a["attempt_id"], sf)


def test_only_an_undispatched_reservation_can_be_retired(system):
    _, attempts, _ = system
    a = reserve(system)
    attempts.retire_reservation(a["attempt_id"], "Choose another workflow")
    assert attempts.get(a["attempt_id"])["status"] == "superseded"
    b = reserve(system, request="second")
    attempts.claim_launch(b["attempt_id"])
    with pytest.raises(StateConflict, match="unlaunched"):
        attempts.retire_reservation(b["attempt_id"], "Cannot assume dispatch failed")


def test_accepted_result_later_reported_failed_invalidates_fact(system, monkeypatch):
    store, attempts, sf = system
    a = finish(system, reserve(system))
    accept(system, a)
    row = sf.get_run(a["run_id"])
    row["status"] = "failed"
    monkeypatch.setattr(sf, "get_run", lambda _rid: row)
    assert attempts.reconcile(a["attempt_id"], sf)["status"] == "failed"
    assert store.get_node("game", "a")["status"] == "STALE"
    assert store.get_node("game", "b")["readiness"] == "blocked"


@pytest.mark.parametrize("version", [None, {"digest": "sha256:" + "0" * 64, "graph": {}}])
def test_missing_or_corrupt_historical_graph_refuses_acceptance(system, monkeypatch, version):
    _, attempts, sf = system
    a = finish(system, reserve(system))
    monkeypatch.setattr(sf, "get_graph_version", lambda *_: version)
    with pytest.raises(StateConflict, match="version"):
        attempts.reconcile(a["attempt_id"], sf)


def test_lifecycle_projection_preserves_quiescence_and_never_verifies(system, tmp_path):
    from core.state_changes import reconcile_workflow_project
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    sf.pause_run(rid)
    result = reconcile_workflow_project(store.db, ws, sf, a["execution_project_id"])
    assert result[0]["status"] == "paused"
    count = len(store.events("game"))
    reconcile_workflow_project(store.db, ws, sf, a["execution_project_id"])
    assert len(store.events("game")) == count
    sf.resume_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert claimed is not None
    sf.confirm_step(claimed.token, StepResult(outputs={"implementation": "candidate"}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    assert attempts.get(a["attempt_id"])["status"] == "paused"
    result = reconcile_workflow_project(store.db, ws, sf, a["execution_project_id"])
    assert result[0]["status"] == "candidate"
    assert store.get_node("game", "a")["status"] == "CANDIDATE"
    assert store.get_node("game", "b")["readiness"] == "blocked"


@pytest.mark.asyncio
async def test_wait_recovers_missed_workflow_projection(system, tmp_path):
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    cursor = store.events("game")[-1]["seq"]
    sf.pause_run(rid)
    service = StateService(store.db, WorkspaceManager(str(tmp_path / "workspaces")), sf, {},
                           project_read_trusted=True)
    result = await service.wait_for_state_change("game", after=cursor, timeout_seconds=2)
    assert result["events"][-1]["payload"]["status"] == "paused"
    assert attempts.get(a["attempt_id"])["status"] == "paused"
    assert sf.get_run(rid)["status"] == "paused"


def test_scheduler_lifecycle_sync_projects_state_attempt(system, tmp_path, monkeypatch):
    from core import scheduler
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.pause_run(rid)
    monkeypatch.setattr(scheduler, "db", store.db)
    monkeypatch.setattr(scheduler, "ws", WorkspaceManager(str(tmp_path / "workspace")))
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    scheduler._sync_project_status_to_db(a["execution_project_id"])
    assert attempts.get(a["attempt_id"])["status"] == "paused"
    assert sf.get_run(rid)["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("nodes", [None, ["b"]])
async def test_director_wait_refreshes_stored_paused_before_disposition(system, tmp_path, completed, nodes):
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.pause_run(rid)
    attempts.reconcile(a["attempt_id"], sf)
    cursor = store.events("game")[-1]["seq"]
    sf.resume_run(rid)
    if completed:
        sf.advance_run(rid)
        claimed = sf.claim_next_step(rid)
        assert claimed is not None
        sf.confirm_step(claimed.token, StepResult(outputs={"implementation": "candidate"}))
        sf.advance_run(rid)
        assert sf.get_run(rid)["status"] == "completed"
    assert attempts.get(a["attempt_id"])["status"] == "paused"
    service = StateService(store.db, WorkspaceManager(str(tmp_path / "workspaces")), sf, {},
                           project_read_trusted=True)
    result = await service.wait_for_state_change("game", after=cursor,
        return_when_idle=True, timeout_seconds=.2, node_keys=nodes)
    if completed:
        assert result["events"][-1]["payload"]["status"] == "candidate"
        assert not result["timed_out"]
    else:
        assert result["events"] == [] and result["timed_out"]
        assert attempts.get(a["attempt_id"])["status"] == "running"
    assert "reason" not in result


@pytest.mark.asyncio
async def test_director_recovery_failure_does_not_return_cached_paused(system, tmp_path, monkeypatch):
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    rid = launch(system, a)
    sf.pause_run(rid)
    attempts.reconcile(a["attempt_id"], sf)
    cursor = store.events("game")[-1]["seq"]
    def unavailable(_rid):
        raise OSError("first observation failure")
    monkeypatch.setattr(sf, "get_run", unavailable)
    service = StateService(store.db, WorkspaceManager(str(tmp_path / "workspaces")), sf, {},
                           project_read_trusted=True)
    result = await service.wait_for_state_change("game", after=cursor,
        return_when_idle=True, timeout_seconds=2, node_keys=["b"])
    assert result == {"events": [], "next_after": cursor, "timed_out": False,
                      "reason": "observation_unavailable"}
    assert attempts.get(a["attempt_id"])["status"] == "paused"


# ── relay: continue_from a failed attempt ────────────────────────────

def _fail(system, request="first"):
    _, attempts, sf = system
    a = reserve(system, request=request)
    sf.fail_run(launch(system, a), "Step implement: native turn budget exhausted (32/32)")
    return attempts.reconcile(a["attempt_id"], sf)


def test_continue_from_records_the_relayed_attempt_and_is_idempotent(system):
    store, attempts, _ = system
    a = _fail(system)
    b = attempts.reserve("game", "a", 1, "feature", "relay-1", "finish it", continue_from=a["attempt_id"])
    assert b["context"]["relay_of"]["attempt_id"] == a["attempt_id"]
    assert b["context"]["relay_of"]["run_id"] == a["run_id"]
    assert "turn budget" in b["context"]["relay_of"]["error"]
    again = attempts.reserve("game", "a", 1, "feature", "relay-1", "finish it", continue_from=a["attempt_id"])
    assert again["attempt_id"] == b["attempt_id"]
    with pytest.raises(StateConflict, match="different launch request"):
        attempts.reserve("game", "a", 1, "feature", "relay-1", "finish it")
    assert store.events("game")[-1]["payload"]["relay_of"]["attempt_id"] == a["attempt_id"]


def test_chained_continue_from_preserves_original_first_failure(system):
    _, attempts, sf = system
    first = _fail(system, request="first-chain")
    second = attempts.reserve(
        "game", "a", 1, "feature", "relay-chain-1", "continue",
        continue_from=first["attempt_id"],
    )
    assert second["context"]["relay_of"]["first_failure_run_id"] == first["run_id"]
    sf.fail_run(launch(system, second), "Step implement: native turn budget exhausted (40/40)")
    second = attempts.reconcile(second["attempt_id"], sf)
    third = attempts.reserve(
        "game", "a", 1, "feature", "relay-chain-2", "continue again",
        continue_from=second["attempt_id"],
    )
    assert third["context"]["relay_of"]["run_id"] == second["run_id"]
    assert third["context"]["relay_of"]["first_failure_run_id"] == first["run_id"]


@pytest.mark.parametrize("wrong", ["not-failed", "other-node", "other-workflow", "unknown"])
def test_continue_from_refuses_anything_but_a_failed_attempt_of_this_goal(system, wrong):
    _, attempts, sf = system
    if wrong == "not-failed":
        a = reserve(system)
        launch(system, a)
        with pytest.raises(StateConflict, match="FAILED"):
            attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"])
        return
    a = _fail(system)
    if wrong == "other-node":
        system[0].add_nodes("game", [spec("d")])  # ready, unlike b, so the relay guard answers
        with pytest.raises(StateConflict, match="of this node"):
            attempts.reserve("game", "d", 1, "feature", "relay", continue_from=a["attempt_id"])
    elif wrong == "other-workflow":
        with pytest.raises(StateConflict, match="workflow"):
            attempts.reserve("game", "a", 1, "other", "relay", continue_from=a["attempt_id"])
    else:
        with pytest.raises(StateConflict, match="of this node"):
            attempts.reserve("game", "a", 1, "feature", "relay", continue_from="attempt-nope")


def test_continue_from_refuses_a_draft_of_a_stale_revision(system):
    store, attempts, _ = system
    a = _fail(system)
    store.revise_node("game", "a", 1, goal="Implement a differently", reason="scope change")
    with pytest.raises(StateConflict, match="different revision"):
        attempts.reserve("game", "a", 2, "feature", "relay", continue_from=a["attempt_id"])


def test_pin_relay_freezes_the_inventory_before_dispatch(system):
    _, attempts, _ = system
    a = _fail(system)
    b = attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"])
    relay = {"attempt_id": a["attempt_id"], "base_sha": "b" * 40, "commits": [], "staged_files": {"implementation": ["x.py"]}}
    pinned = attempts.pin_relay(b["attempt_id"], relay)
    assert pinned["context"]["relay"] == relay
    assert attempts.pin_relay(b["attempt_id"], relay)["context"]["relay"] == relay
    with pytest.raises(StateConflict, match="changed"):
        attempts.pin_relay(b["attempt_id"], {**relay, "base_sha": "c" * 40})
    with pytest.raises(StateConflict, match="not reserved with continue_from"):
        attempts.pin_relay(a["attempt_id"], relay)


# ── candidate artifact whose owned worktree is unavailable ───────────

def _complete_without_artifact(system, attempt):
    """Drive a real run to completion and observe it WITHOUT pinning an
    artifact, so the attempt is a candidate with artifact_ref NULL."""
    _, attempts, sf = system
    rid = attempt.get("run_id") or launch(system, attempt)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert claimed is not None, "fixture must execute a real step"
    sf.confirm_step(claimed.token, StepResult(outputs={"implementation": "candidate"}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    return attempts.reconcile(attempt["attempt_id"], sf), rid


def _record_dead_worktree(store, rid, tmp_path):
    """An isolation record whose owned worktree no longer exists: exactly the
    shape resolve_for_resolver refuses by raising IsolationUnavailable."""
    from core import run_isolation as ri
    ri._write_record(store.db, run_id=rid, project_id="game", config_name="feature",
                     mode=ri.MODE_WORKTREE, source_repo=str(tmp_path / "src"),
                     worktree_path=str(tmp_path / "gone"), branch=None,
                     base_sha=None, note=None)


def test_unavailable_owned_worktree_is_a_named_artifact_pending_not_a_conflict(system, tmp_path):
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    observed, rid = _complete_without_artifact(system, a)
    assert observed["status"] == "candidate"
    _record_dead_worktree(store, rid, tmp_path)
    service = StateService(store.db, WorkspaceManager(str(tmp_path / "workspaces")), sf, {},
                           project_read_trusted=True)
    result = service.reconcile_attempt(a["attempt_id"])
    assert result["status"] == "candidate"
    assert result["artifact_pending"] is True
    assert result["artifact_ref"] is None
    assert result["artifact_pending_reason"] == "owned_worktree_unavailable"
    required = result["action_required"]
    assert required["reason"] == "candidate_artifact_unavailable"
    assert required["attempt_id"] == a["attempt_id"]
    assert required["attempt_status"] == "candidate"
    assert required["artifact_ref"] is None
    assert required["run_id"] == rid
    # Ownership and candidate state are unchanged; nothing is auto-verified.
    with store.transaction() as conn:
        row = conn.execute("SELECT status,artifact_ref FROM state_attempts WHERE attempt_id=?",
                           (a["attempt_id"],)).fetchone()
    assert row["status"] == "candidate" and row["artifact_ref"] is None
    # A second reconcile is the same named condition, not a new effect.
    again = service.reconcile_attempt(a["attempt_id"])
    assert again["artifact_pending_reason"] == "owned_worktree_unavailable"


@pytest.mark.asyncio
async def test_wait_names_the_unpinned_candidate_and_keeps_healthy_scope_usable(system, tmp_path):
    from core.state_changes import wait_disposition
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    store, attempts, sf = system
    a = reserve(system)
    _, rid = _complete_without_artifact(system, a)
    _record_dead_worktree(store, rid, tmp_path)
    store.add_nodes("game", [spec("d")])
    other = reserve(system, "d", request="other-owner")
    service = StateService(store.db, WorkspaceManager(str(tmp_path / "workspaces")), sf, {},
                           project_read_trusted=True)
    cursor = store.events("game")[-1]["seq"]
    result = await service.wait_for_state_change("game", after=cursor,
        return_when_idle=True, timeout_seconds=.2)
    # The completed workflow with the unavailable owned worktree is exposed
    # under its exact identity, with the artifact still unset — never as idle.
    assert result["timed_out"] is False
    assert result["reason"] == "action_required"
    assert result["attempts"] == [
        {"attempt_id": a["attempt_id"], "status": "candidate", "artifact_ref": None}]
    # A scoped wait still surfaces the same named condition, and an unrelated
    # scope stays usable with its own cursor and empty disposition.
    assert wait_disposition(service.store, "game", cursor, None, [a["attempt_id"]], None) == {
        "reason": "action_required",
        "attempts": [{"attempt_id": a["attempt_id"], "status": "candidate", "artifact_ref": None}]}
    assert wait_disposition(service.store, "game", cursor, None, ["nope"], None) == {
        "reason": "nothing_to_wait"}
    empty = await service.wait_for_state_change("game", after=cursor,
        attempt_ids=["nope"], timeout_seconds=0, return_when_idle=True)
    assert empty == {"events": [], "next_after": cursor, "timed_out": False,
                     "reason": "nothing_to_wait"}
    # Negative control: once the artifact IS pinned, the same candidate is no
    # longer actionable and the reservation of another owner stays waitable.
    attempts.reconcile(a["attempt_id"], sf, ARTIFACT)
    assert attempts.get(other["attempt_id"])["status"] == "reserved"


# ── recovery watermark, scope propagation and post-pin replay ────────

def _recovery_service(system, tmp_path):
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    return StateService(system[0].db, WorkspaceManager(str(tmp_path / "workspaces")),
                        system[2], {}, project_read_trusted=True)


def test_recovery_page_uses_a_fresh_watermark_and_rescans_a_partial_sweep(system, tmp_path):
    """A partial recovery sweep must NOT hand back a stale watermark.

    The page cursor is only authoritative once a full ten-row page was rotated;
    otherwise a caller holding a remembered value (a stale ``8``) could skip
    the very rows it has not observed and let a real completed owner stay
    cached as running. Returning 0 forces the rescan. It must also never
    delete checks: the owner stays exactly as recorded for the rescan to find.
    """
    from core.state_changes import recover_page
    store, attempts, sf = system
    a = reserve(system)
    _, rid = _complete_without_artifact(system, a)
    _record_dead_worktree(store, rid, tmp_path)
    service = _recovery_service(system, tmp_path)
    with store.transaction() as conn:
        seq = conn.execute("SELECT seq FROM state_attempts WHERE attempt_id=?",
                           (a["attempt_id"],)).fetchone()["seq"]
    # The row IS in scope for this call, so a partial page of one row is swept.
    after, ok = recover_page(service, "game", seq - 1, None, None)
    assert ok is True
    assert after == 0, "a partial page must not retain a stale watermark"
    # The rescan is what exposes the completed owner under its exact identity.
    result = service.reconcile_attempt(a["attempt_id"])
    assert result["artifact_pending_reason"] == "owned_worktree_unavailable"
    assert attempts.get(a["attempt_id"])["artifact_ref"] is None
    # A stale watermark (8) still yields 0, i.e. "rescan", and never deletes
    # the checks the director must re-read.
    after, ok = recover_page(service, "game", 8, None, None)
    assert ok is True and after == 0
    assert attempts.get(a["attempt_id"])["status"] == "candidate"


@pytest.mark.asyncio
async def test_recovery_scope_propagates_any_union_and_all_intersection(system, tmp_path):
    """recover_page/`_recover_page` must apply the SAME OR/AND filter group as
    the wait: filter_mode="any" unions a selected node with a watched attempt,
    and "all" intersects them. A union scope cannot leave a real completed
    owner untouched merely because it also names an unrelated node."""
    from core.state_changes import recover_page

    store, attempts, sf = system
    store.add_nodes("game", [spec("d")])
    a = reserve(system)               # node "a"
    launch(system, a)
    d = reserve(system, "d", request="d-owner")
    rid_d = launch(system, d)
    sf.pause_run(rid_d)
    # Stored status is still running until a recovery sweep observes the pause.
    assert attempts.get(d["attempt_id"])["status"] == "running"
    service = _recovery_service(system, tmp_path)
    # "all" intersects node_key="a" with attempt_id=d, so the paused owner is
    # not swept even though it is real running-row work.
    _, ok = recover_page(service, "game", 0, ["a"], [d["attempt_id"]], "all")
    assert ok is True
    assert attempts.get(d["attempt_id"])["status"] == "running"
    # "any" unions them: the watched attempt is swept and its real pause is
    # projected, so it can no longer stay cached as running.
    _, ok = recover_page(service, "game", 0, ["a"], [d["attempt_id"]], "any")
    assert ok is True
    assert attempts.get(d["attempt_id"])["status"] == "paused"


@pytest.mark.asyncio
async def test_wait_replays_event_after_artifact_pin_with_a_fresh_watermark(system, tmp_path):
    """A committed observation is replayable from a fresh watermark cursor.

    The unavailable-owned-worktree candidate is first actionable, never idle.
    Once its owner pins the exact artifact, the committed attempt_observed
    event is replayed from the fresh watermark — the wait never silently
    reports idle over a change it had not yet read.
    """
    store, attempts, sf = system
    a = reserve(system)
    _, rid = _complete_without_artifact(system, a)
    _record_dead_worktree(store, rid, tmp_path)
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    unpinned = await service.wait_for_state_change("game", after=cursor,
        return_when_idle=True, timeout_seconds=0)
    assert unpinned["reason"] == "action_required"
    assert unpinned["attempts"] == [
        {"attempt_id": a["attempt_id"], "status": "candidate", "artifact_ref": None}]
    before = store.events("game")[-1]["seq"]
    attempts.reconcile(a["attempt_id"], sf, ARTIFACT)
    fresh = store.events("game")[-1]["seq"]
    assert fresh > before
    replayed = await service.wait_for_state_change("game", after=before,
        return_when_idle=True, timeout_seconds=0)
    assert replayed["events"][-1]["event_type"] == "attempt_observed"
    assert replayed["events"][-1]["payload"]["artifact_ref"] == ARTIFACT



# ── project/node waits distinguish current candidates from history ──

def _wait_history_rows(store):
    with store.transaction() as conn:
        return {name: [dict(row) for row in conn.execute("SELECT * FROM " + name + " ORDER BY rowid")]
                for name in ("state_attempts", "state_nodes", "state_evidence", "state_acceptances", "state_events")}


def _old_unpinned_and_accepted_successor(system, tmp_path):
    store, attempts, _ = system
    old = reserve(system, request="old-unpinned")
    _, rid = _complete_without_artifact(system, old)
    _record_dead_worktree(store, rid, tmp_path)
    newer = finish(system, reserve(system, request="accepted-successor"))
    receipt = accept(system, newer)
    assert store.get_node("game", "a")["status"] == "VERIFIED"
    assert attempts.get(old["attempt_id"])["artifact_ref"] is None
    return old, newer, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("nodes", [None, ["a"]])
@pytest.mark.parametrize("timeout", [0, .2])
async def test_wait_ignores_obsolete_unpinned_candidate_after_new_acceptance(system, tmp_path, nodes, timeout):
    store, attempts, _ = system
    old, newer, receipt = _old_unpinned_and_accepted_successor(system, tmp_path)
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    before = _wait_history_rows(store)
    result = await service.wait_for_state_change("game", after=cursor, node_keys=nodes,
        return_when_idle=True, timeout_seconds=timeout)
    print(json.dumps({"old_attempt": old["attempt_id"], "old_artifact": None,
                      "current_attempt": newer["attempt_id"], "current_receipt": receipt["receipt_id"],
                      "actual_wait": result}))
    assert result == {"events": [], "next_after": cursor, "timed_out": False,
                      "reason": "nothing_to_wait"}
    assert _wait_history_rows(store) == before
    assert attempts.get(old["attempt_id"])["status"] == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_changed", [False, True])
async def test_explicit_historical_candidate_wait_is_diagnostic_without_reconciliation(system, tmp_path, contract_changed):
    store, attempts, _ = system
    old, _, _ = _old_unpinned_and_accepted_successor(system, tmp_path)
    if contract_changed:
        store.revise_node("game", "a", 1, "new contract", goal="New revision")
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    before = _wait_history_rows(store)
    result = await service.wait_for_state_change("game", after=cursor, attempt_ids=[old["attempt_id"]],
        return_when_idle=True, timeout_seconds=.2)
    assert result == {"events": [], "next_after": cursor, "timed_out": False,
        "reason": "action_required", "attempts": [
            {"attempt_id": old["attempt_id"], "status": "candidate", "artifact_ref": None}]}
    assert _wait_history_rows(store) == before


@pytest.mark.asyncio
async def test_wait_does_not_recover_candidate_from_old_node_revision(system, tmp_path):
    store, attempts, _ = system
    old = reserve(system)
    _, rid = _complete_without_artifact(system, old)
    _record_dead_worktree(store, rid, tmp_path)
    store.revise_node("game", "a", 1, "changed", goal="New revision")
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    before = _wait_history_rows(store)
    result = await service.wait_for_state_change("game", after=cursor, node_keys=["a"],
        return_when_idle=True, timeout_seconds=.2)
    assert result["reason"] == "nothing_to_wait" and not result["timed_out"]
    assert _wait_history_rows(store) == before
    assert attempts.get(old["attempt_id"])["status"] == "candidate"


@pytest.mark.asyncio
async def test_obsolete_candidate_exception_names_only_explicit_attempts_and_preserves_filter_modes(system, tmp_path):
    store, attempts, _ = system
    old, _, _ = _old_unpinned_and_accepted_successor(system, tmp_path)
    store.add_nodes("game", [spec("d")])
    healthy = reserve(system, "d", request="healthy-unrelated")
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    before = _wait_history_rows(store)
    any_result = await service.wait_for_state_change("game", after=cursor, node_keys=["d"],
        attempt_ids=[old["attempt_id"]], filter_mode="any", return_when_idle=True, timeout_seconds=.02)
    assert any_result["reason"] == "action_required"
    assert any_result["attempts"] == [{"attempt_id": old["attempt_id"], "status": "candidate", "artifact_ref": None}]
    all_result = await service.wait_for_state_change("game", after=cursor, node_keys=["d"],
        attempt_ids=[old["attempt_id"]], filter_mode="all", return_when_idle=True, timeout_seconds=.02)
    assert all_result["reason"] == "nothing_to_wait"
    unnamed = await service.wait_for_state_change("game", after=cursor, node_keys=["a"],
        attempt_ids=[healthy["attempt_id"]], filter_mode="any", return_when_idle=True, timeout_seconds=.02)
    assert unnamed["timed_out"] and "reason" not in unnamed
    assert _wait_history_rows(store) == before


@pytest.mark.asyncio
async def test_latest_failed_attempt_does_not_make_wait_blind_to_unrelated_actual_owner(system, tmp_path):
    store, attempts, sf = system
    old = reserve(system)
    _, rid = _complete_without_artifact(system, old)
    _record_dead_worktree(store, rid, tmp_path)
    successor = reserve(system, request="failed-successor")
    failed_run = launch(system, successor)
    sf.fail_run(failed_run, "actual failed successor")
    assert attempts.reconcile(successor["attempt_id"], sf)["status"] == "failed"
    store.add_nodes("game", [spec("d")])
    healthy = reserve(system, "d", request="healthy-owner")
    healthy_run = launch(system, healthy)
    service = _recovery_service(system, tmp_path)
    cursor = store.events("game")[-1]["seq"]
    result = await service.wait_for_state_change("game", after=cursor, return_when_idle=True,
                                               timeout_seconds=.02)
    assert result["timed_out"] and "reason" not in result
    assert attempts.get(healthy["attempt_id"])["status"] == "running"
    assert attempts.get(old["attempt_id"])["status"] == "candidate"
    sf.pause_run(healthy_run)
    paused = await service.wait_for_state_change("game", after=result["next_after"],
                                                return_when_idle=True, timeout_seconds=.2)
    assert paused["events"][0]["payload"]["attempt_id"] == healthy["attempt_id"]
    assert paused["events"][0]["payload"]["status"] == "paused"
    assert attempts.get(old["attempt_id"])["artifact_ref"] is None
