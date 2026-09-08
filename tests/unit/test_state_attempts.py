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
    store = StateGraphStore(DBManager(str(tmp_path / "state.db")))
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
    fresh_store = StateGraphStore(DBManager(store.db.db_path))
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
