"""A quiescent terminal workflow observation releases only its still-bound claims.

Real State SQLite rows, real claim rows and a real SkillFlow engine; the only
monkeypatch is the engine's operation audit, to present a faithful non-quiescent
observation (alive/lost/unknown/incomplete) for a run the engine reports finished.
"""
from __future__ import annotations

import json

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode

from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_claims import ClaimError, StateClaims
from core.state_graph import StateConflict, StateGraphStore

ARTIFACT = "a" * 40
ACTOR = "director-reconciler"


def spec(k):
    return {"key": k, "goal": "Implement " + k, "dependencies": [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour validated"}]}


@pytest.fixture
def system(tmp_path):
    store = StateGraphStore(DBManager(str(tmp_path / "state.db")), project_read_trusted=True)
    store.create_project("game", "Long-running game")
    store.add_nodes("game", [spec("a"), spec("d")])
    attempts = StateAttempts(store)
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(PipelineGraph(name="feature", begin="implementation", steps=[StepNode(id="implementation")]))
    yield store, attempts, sf
    sf._conn.close()


def claims_for(store, driver):
    return StateClaims(store, f"driver:{driver}", driver)


def bound_attempt(system, driver="grok", node="a", request="r1"):
    """A live implement claim bound to a freshly reserved, launched attempt."""
    store, attempts, sf = system
    claim = claims_for(store, driver).claim_node("game", node, "implement", 1, f"{driver}-{node}-claim")
    attempt = attempts.reserve("game", node, 1, "feature", request, owner_driver_id=driver,
                               claim_id=claim["claim_id"], fence=claim["fence"])
    rid = sf.create_run(attempt["workflow"], project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    attempts.bind_run(attempt["attempt_id"], rid, sf)
    return claim, attempt, rid


def complete(sf, rid):
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert claimed is not None
    sf.confirm_step(claimed.token, StepResult(outputs={"implementation": "done"}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"


def claim_row(store, claim_id):
    with store.transaction() as conn:
        return dict(conn.execute("SELECT * FROM state_node_claims WHERE claim_id=?", (claim_id,)).fetchone())


def history(store, claim_id):
    with store.transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM state_claim_history WHERE claim_id=? ORDER BY seq", (claim_id,))]


def released_events(store, claim_id):
    with store.transaction() as conn:
        rows = conn.execute("SELECT payload_json FROM state_events WHERE event_type='claim_released'").fetchall()
    return [p for p in (json.loads(r[0]) for r in rows) if p["claim_id"] == claim_id]


def next_controller_claim(store):
    return claims_for(store, "codex").claim_node("game", "a", "implement", 1, "codex-next")


def test_baseline_symptom_is_a_blocked_next_controller(system):
    store, _, _ = system
    claim, _, _ = bound_attempt(system)
    with pytest.raises(ClaimError) as caught:
        next_controller_claim(store)
    assert caught.value.code == "claimed_by_other"
    assert claim_row(store, claim["claim_id"])["status"] == "live"


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_terminal_quiescent_reconcile_releases_only_its_bound_claim(system, terminal):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    other, _, _ = bound_attempt(system, driver="grok", node="d", request="r-other")
    unbound_review = claims_for(store, "reviewer").claim_node("game", "a", "review", 1, "rev-a")
    fence_before = claim["fence"]
    if terminal == "completed":
        complete(sf, rid)
        result = attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
        assert result["status"] == "candidate"
    else:
        sf.fail_run(rid, "implementation failed")
        result = attempts.reconcile(attempt["attempt_id"], sf, actor=ACTOR)
        assert result["status"] == "failed"

    row = claim_row(store, claim["claim_id"])
    assert row["status"] == "released" and row["fence"] == fence_before
    rows = history(store, claim["claim_id"])
    assert [h["status"] for h in rows] == ["live", "released"]
    assert rows[-1]["actor"] == ACTOR and rows[-1]["fence"] == fence_before
    events = released_events(store, claim["claim_id"])
    assert len(events) == 1
    assert events[0]["attempt_id"] == attempt["attempt_id"] and events[0]["actor"] == ACTOR
    assert events[0]["fence"] == fence_before and events[0]["break_glass"] is False
    # unrelated ownership is untouched
    assert claim_row(store, other["claim_id"])["status"] == "live"
    assert claim_row(store, unbound_review["claim_id"])["status"] == "live"
    assert released_events(store, other["claim_id"]) == []
    # a useful next controller can now claim normally
    nxt = next_controller_claim(store)
    assert nxt["status"] == "live" and nxt["driver_id"] == "codex" and nxt["fence"] == fence_before + 2


def test_repeated_terminal_reconcile_repairs_leftover_bound_claim_exactly_once(system):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    complete(sf, rid)
    attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert len(released_events(store, claim["claim_id"])) == 1
    # Leftover live bound claim on an already-terminal attempt (written before this fix).
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE state_node_claims SET status='live' WHERE claim_id=?", (claim["claim_id"],))
    unchanged = attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert unchanged["status"] == "candidate"
    assert claim_row(store, claim["claim_id"])["status"] == "released"
    assert len(released_events(store, claim["claim_id"])) == 2
    history_len = len(history(store, claim["claim_id"]))
    attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert len(released_events(store, claim["claim_id"])) == 2
    assert len(history(store, claim["claim_id"])) == history_len


def test_claim_rebound_to_another_attempt_is_not_released(system):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE state_node_claims SET attempt_id='attempt-newer' WHERE claim_id=?", (claim["claim_id"],))
    complete(sf, rid)
    attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    row = claim_row(store, claim["claim_id"])
    assert row["status"] == "live" and row["attempt_id"] == "attempt-newer"
    assert released_events(store, claim["claim_id"]) == []


def test_unbound_p1_claim_is_not_released(system):
    store, attempts, sf = system
    claim = claims_for(store, "grok").claim_node("game", "a", "implement", 1, "grok-a-claim")
    attempt = attempts.reserve("game", "a", 1, "feature", "r-unnamed", owner_driver_id="grok")
    rid = sf.create_run(attempt["workflow"], project_id=attempt["execution_project_id"])
    sf.start_run(rid)
    attempts.bind_run(attempt["attempt_id"], rid, sf)
    assert claim_row(store, claim["claim_id"])["attempt_id"] is None
    complete(sf, rid)
    attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert claim_row(store, claim["claim_id"])["status"] == "live"


def test_running_run_keeps_its_bound_claim(system):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    assert attempts.reconcile(attempt["attempt_id"], sf, actor=ACTOR)["status"] == "running"
    assert claim_row(store, claim["claim_id"])["status"] == "live"


def test_paused_run_keeps_its_bound_claim(system):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    sf.pause_run(rid)
    assert attempts.reconcile(attempt["attempt_id"], sf, actor=ACTOR)["status"] == "paused"
    assert claim_row(store, claim["claim_id"])["status"] == "live"


@pytest.mark.parametrize("audit", [
    {"lost": [], "unknown": [], "alive": 1},
    {"lost": ["op-1"], "unknown": [], "alive": 0},
    {"lost": [], "unknown": ["op-2"], "alive": 0},
])
def test_non_quiescent_audit_keeps_claim(system, monkeypatch, audit):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    complete(sf, rid)
    monkeypatch.setattr(sf, "audit_operation_owners", lambda _rid: audit)
    assert attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)["status"] == "unknown"
    assert claim_row(store, claim["claim_id"])["status"] == "live"
    assert released_events(store, claim["claim_id"]) == []


def test_incomplete_audit_refuses_and_keeps_claim(system, monkeypatch):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    complete(sf, rid)
    monkeypatch.setattr(sf, "audit_operation_owners", lambda _rid: {"alive": 0})
    with pytest.raises(StateConflict, match="incomplete"):
        attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert claim_row(store, claim["claim_id"])["status"] == "live"


def test_audit_exception_refuses_and_keeps_claim(system, monkeypatch):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    complete(sf, rid)

    def boom(_rid):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(sf, "audit_operation_owners", boom)
    with pytest.raises(StateConflict, match="quiescence"):
        attempts.reconcile(attempt["attempt_id"], sf, ARTIFACT, actor=ACTOR)
    assert claim_row(store, claim["claim_id"])["status"] == "live"


def test_revived_terminal_attempt_is_refused_and_keeps_claim(system, monkeypatch):
    store, attempts, sf = system
    claim, attempt, rid = bound_attempt(system)
    sf.fail_run(rid, "implementation failed")
    attempts.reconcile(attempt["attempt_id"], sf, actor=ACTOR)
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE state_node_claims SET status='live' WHERE claim_id=?", (claim["claim_id"],))
    row = dict(sf.get_run(rid))
    row["status"] = "running"
    monkeypatch.setattr(sf, "get_run", lambda _rid: row)
    with pytest.raises(StateConflict, match="revived"):
        attempts.reconcile(attempt["attempt_id"], sf, actor=ACTOR)
    assert claim_row(store, claim["claim_id"])["status"] == "live"
