"""Multi-driver P3, fix round 3: one regression test per CONFIRMED item of the
third Codex review (P3_REVIEW3_CODEX.md). Each fails on 8c3d7079 and passes
after the fix named in P3_REPORT.md "Fix round 3".
"""
from __future__ import annotations

import sqlite3

import pytest

from core import driver_notices, drivers
from core.state_claims import ClaimError
from core.state_database import StateDatabase
from core import state_enforcement as enforcement


def _enforcement(name):
    """Round-3 API looked up lazily, so on the pre-fix tree each test fails for
    ITS reason instead of the whole module failing to import."""
    fn = getattr(enforcement, name, None)
    assert fn is not None, f"core.state_enforcement.{name} is missing (pre-fix tree)"
    return fn


def checkpoint_controller(*args, **kwargs):
    return _enforcement("checkpoint_controller")(*args, **kwargs)


def finish_checkpoint_decision(*args, **kwargs):
    return _enforcement("finish_checkpoint_decision")(*args, **kwargs)


def refuse_checkpoint_in_flight(*args, **kwargs):
    return _enforcement("refuse_checkpoint_in_flight")(*args, **kwargs)
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    NODE, _insert_skillflow_attempt, a_file, claim, clock, code_of, db, events, external, notices, project,
    read, reclaimable, register, report, svc, write)
from tests.unit.test_state_p3_fix_round1 import claims_of, reserved_skillflow_attempt, terminal_report
from tests.unit.test_state_p3_fix_round2 import _no_inbox_adapter  # noqa: F401 - autouse reset


# 1 ---------------------------------------------------------------------------
def test_1_same_driver_cannot_run_two_executors_in_one_checkout(db, clock):
    project(db, enforce=True)
    codex = svc(db, "codex")
    c1 = claim(codex, "a", workspace="linxuhaserver:/w#a")
    attempt = external(codex, "a", claim_id=c1["claim_id"], fence=1)
    write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="done claiming")
    # Same driver, DIFFERENT node, same checkout: a second executor, refused.
    assert code_of(lambda: claim(codex, "b", workspace="linxuhaserver:/w#b")) == "workspace_in_use"
    # Same driver, SAME node: re-associating with the attempt that runs there is allowed.
    again = claim(codex, "a", workspace="linxuhaserver:/w#a", request_key="again")
    assert again["status"] == "live"
    assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["status"] == "running"


# 2 (R1) ----------------------------------------------------------------------
class TestItem2StaleAuthorizationAtLaunch:
    def test_fence_only_replay_and_replacement_during_preflight(self, db, clock):
        project(db, enforce=True)
        codex = svc(db, "codex")
        c1 = claim(codex, "b", workspace="h:/w#c")
        reserved_skillflow_attempt(db, "attempt-res", "codex")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE state_node_claims SET attempt_id='attempt-res' WHERE claim_id=?", (c1["claim_id"],))
        conn.commit()
        conn.close()
        write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="oops")
        c2 = claim(codex, "b", workspace="h:/w#c", request_key="c2")
        # Replay with C1's fence only (no claim_id): stale, not silently C2.
        with pytest.raises(ClaimError) as refused:
            codex.attempts.reserve("p", "b", 1, "w", "rk-attempt-res", owner_driver_id="codex", fence=1)
        assert refused.value.code == "stale_fence"
        # Launch with no named claim: the bound claim is gone; a silent rebind is refused.
        with pytest.raises(ClaimError) as refused:
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex")
        assert refused.value.code == "claim_required" and refused.value.facts["claim_id"] == c2["claim_id"]
        # Launch naming C1 (replaced during preflight): stale.
        with pytest.raises(ClaimError) as refused:
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex", claim_id=c1["claim_id"], fence=1)
        assert refused.value.code == "stale_fence"
        with pytest.raises(ClaimError) as refused:
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex", claim_id=c2["claim_id"], fence=1)
        assert refused.value.code == "stale_fence"
        # Deliberate: name the replacement and its fence.
        assert codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex",
                                           claim_id=c2["claim_id"], fence=c2["fence"]) is True
        assert claims_of(db, claim_id=c2["claim_id"])[0]["attempt_id"] == "attempt-res"

    def test_external_replay_binds_the_named_claim_so_settlement_releases_it(self, db, clock, tmp_path):
        project(db, enforce=True)
        codex = svc(db, "codex")
        c1 = claim(codex, "a", workspace="h:/w#c")
        attempt = external(codex, "a", "k", claim_id=c1["claim_id"], fence=1)
        write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="oops")
        c2 = claim(codex, "a", workspace="h:/w#c", request_key="c2")
        assert code_of(lambda: external(codex, "a", "k", fence=1)) == "stale_fence", "fence-only replay is judged"
        replay = external(codex, "a", "k", claim_id=c2["claim_id"], fence=c2["fence"])
        assert replay["attempt_id"] == attempt["attempt_id"]
        assert claims_of(db, claim_id=c2["claim_id"])[0]["attempt_id"] == attempt["attempt_id"]
        terminal_report(codex, attempt, tmp_path, fence=1)
        assert claims_of(db, claim_id=c2["claim_id"])[0]["status"] == "released"

    def test_recover_attempt_contract_carries_claim_and_fence(self):
        from core.state_commands import WRITE_REQUESTS
        fields = WRITE_REQUESTS["recover_attempt"].model_fields
        assert {"attempt_id", "claim_id", "fence"} <= set(fields)


# 3 ---------------------------------------------------------------------------
def test_3_unknown_abandonment_keeps_the_executor_checkout_until_verified_settlement(db, clock, tmp_path):
    project(db, enforce=True)
    codex, grok, third = svc(db, "codex"), svc(db, "grok"), svc(db, "third")
    held = claim(codex, "a", workspace="linxuhaserver:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    reclaimable(clock)
    write(grok, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")
    assert claims_of(db, claim_id=held["claim_id"])[0]["status"] == "released"
    assert code_of(lambda: claim(third, "b", workspace="linxuhaserver:/w#b")) == "workspace_in_use"
    other = claim(third, "b", workspace="linxuhaserver:/elsewhere#b")
    second = external(third, "b", "rk-b", claim_id=other["claim_id"], fence=other["fence"])
    assert code_of(lambda: register(third, second, tmp_path, "w", host="linxuhaserver",
                                    workspace="linxuhaserver:/w#w")) == "workspace_in_use"
    # The original reporter attests quiescence with retained evidence: the owner row settles, the checkout frees.
    ref, sha = a_file(tmp_path, "quiet.json", {"workers": "exited"})
    write(codex, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id="quiet",
          expected_version=0, context_hash=attempt["context_hash"], status="running", report_ref=ref,
          report_sha256=sha, quiescent=True, fence=1)
    assert claim(third, "c", workspace="linxuhaserver:/w#c3")["status"] == "live"


# 4 ---------------------------------------------------------------------------
def test_4_checkpoint_decision_holds_ownership_until_finished(db, clock):
    project(db, enforce=True)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    claim(codex, "b", workspace="h:/w#c")
    _insert_skillflow_attempt(db, "codex")                      # run-1, paused, owned by codex
    database = StateDatabase(str(db))
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE state_attempts SET lease_expires_at='2026-01-01T00:00:00.000000+00:00' WHERE attempt_id='attempt-sf'")
    conn.commit()
    conn.close()
    reclaimable(clock)                                          # the lease is reclaimable...
    decision = checkpoint_controller(database, "run-1", "codex", False, "driver:codex")   # ...but codex is deciding
    assert decision["decision_id"]
    # While the decision is open, no transfer: takeover, handoff acceptance, abandon.
    assert code_of(lambda: write(grok, "take_over_attempt", attempt_id="attempt-sf", expected_owner_fence=1,
                                 reason="gone")) == "checkpoint_in_progress"
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id="attempt-sf", to_driver_id="grok", package={"next_step": "x", "workers": {"quiescent": True}})
    assert code_of(lambda: write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                                 expected_owner_fence=1)) == "checkpoint_in_progress"
    with codex.store.transaction() as conn:
        with pytest.raises(ClaimError):
            refuse_checkpoint_in_flight(conn, "attempt-sf")
    assert finish_checkpoint_decision(database, decision["decision_id"]) is True
    assert finish_checkpoint_decision(database, decision["decision_id"]) is False, "idempotent"
    taken = write(grok, "take_over_attempt", attempt_id="attempt-sf", expected_owner_fence=1, reason="gone")
    assert taken["owner_driver_id"] == "grok"
    # After the transfer the former owner cannot even open a decision.
    with pytest.raises(ClaimError) as refused:
        checkpoint_controller(database, "run-1", "codex", False, "driver:codex")
    assert refused.value.code == "not_attempt_owner"
    # A crashed decider expires: the hold lifts by itself.
    second = checkpoint_controller(database, "run-1", "grok", False, "driver:grok")
    clock.advance(121)
    with grok.store.transaction() as conn:
        refuse_checkpoint_in_flight(conn, "attempt-sf")          # expired: no refusal
    assert second["decision_id"]


def test_4_rest_door_finishes_the_decision_even_when_the_engine_refuses(db, clock, monkeypatch):
    from api import meta_routers
    project(db, enforce=True)
    codex = svc(db, "codex")
    claim(codex, "b")
    _insert_skillflow_attempt(db, "codex")
    database = StateDatabase(str(db))
    from api import state_graph_routers as routers
    monkeypatch.setattr(routers, "request_identity", lambda request: drivers.Identity("driver", "driver:codex", "codex"))
    monkeypatch.setattr(routers, "request_actor", lambda request: "driver:codex")
    controller = meta_routers.state_checkpoint_controller(object(), "run-1", database)
    with codex.store.transaction() as conn:
        with pytest.raises(ClaimError):
            refuse_checkpoint_in_flight(conn, "attempt-sf")
    meta_routers.finish_state_checkpoint_decision(controller, database)
    with codex.store.transaction() as conn:
        refuse_checkpoint_in_flight(conn, "attempt-sf")


# 5 ---------------------------------------------------------------------------
def test_5_only_members_accept_pool_offers_and_reclaim(db, clock, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "secrets"))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / drivers.PEPPER_SECRET_NAME).write_text("p" * 48)
    monkeypatch.setenv(drivers.FEATURE_ENV, "on")
    drivers._REGISTRIES.clear()
    try:
        project(db)
        registry = drivers.registry_for(StateDatabase(str(db)))
        for who in ("codex", "grok", "late", "stranger"):
            registry.register(who, who.title(), actor="t")
        registry.set_membership("p", "codex", "member", 0, "join", actor="t")
        registry.set_membership("p", "grok", "member", 0, "join", actor="t")
        registry.set_membership("p", "late", "member", 0, "join", actor="t")
        codex, grok, late, public = svc(db, "codex"), svc(db, "grok"), svc(db, "late"), svc(db, "stranger")
        attempt = external(codex, "a")
        pool = write(codex, "offer_handoff", project_id="p", request_key="pool", expected_owner_fence=1,
                     attempt_id=attempt["attempt_id"], package={"next_step": "anyone", "workers": {"quiescent": True}})
        assert code_of(lambda: write(public, "accept_handoff", project_id="p", handoff_id=pool["handoff_id"],
                                     expected_owner_fence=1)) == "not_project_member"
        registry.set_membership("p", "late", "removed", 1, "left after the offer", actor="t")
        assert code_of(lambda: write(late, "accept_handoff", project_id="p", handoff_id=pool["handoff_id"],
                                     expected_owner_fence=1)) == "not_project_member"
        assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "codex"
        second = external(codex, "b", "rk-b")
        reclaimable(clock)
        assert code_of(lambda: write(public, "take_over_attempt", attempt_id=second["attempt_id"],
                                     expected_owner_fence=1, reason="mine")) == "not_project_member"
        assert code_of(lambda: write(late, "abandon_external_attempt", attempt_id=second["attempt_id"],
                                     expected_owner_fence=1, abandon_kind="unknown", reason="x")) == "not_project_member"
        taken = write(grok, "take_over_attempt", attempt_id=second["attempt_id"], expected_owner_fence=1, reason="gone")
        assert taken["owner_driver_id"] == "grok" and taken["break_glass"] is False
        # An admin that is not a member passes, audited as break glass.
        owner = svc(db, "owner-cli", admin=True)
        glass = write(owner, "accept_handoff", project_id="p", handoff_id=pool["handoff_id"], expected_owner_fence=1)
        assert glass["attempt"]["owner_driver_id"] == "owner-cli"
        assert events(db, "attempt_ownership_transferred")[-1]["break_glass"] is True
    finally:
        drivers._REGISTRIES.clear()


# 6 ---------------------------------------------------------------------------
def test_6_inherited_workers_claim_and_attest_under_their_origin_prefix(db, clock, tmp_path):
    project(db, enforce=True)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/w1#w1")
    reclaimable(clock)
    write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
    write(grok, "adopt_subagent", project_id="p", subagent_id="codex/w1", fence=2, observability="controllable",
          reason="tmux here")
    # grok claims FOR the inherited worker, under its origin prefix.
    mine = claim(grok, "b", "plan", subagent="codex/w1", workspace="linxuhaserver:/w1#plan")
    assert mine["status"] == "live" and mine["subagent"] == "codex/w1"
    assert code_of(lambda: claim(grok, "c", "plan", subagent="grok/w1")) == "subagent_unregistered"
    assert code_of(lambda: claim(codex, "c", "plan", subagent="codex/w1", request_key="x")) == "subagent_unregistered"
    # ... and attests under it after settling the attempt.
    out = terminal_report(grok, attempt, tmp_path, fence=2)
    assert out["settled_subagents"] == ["codex/w1"]
    artifact = "a" * 40
    ref, sha = a_file(tmp_path, "evidence.json", {"status": "completed", "settled": True, "usable": True,
                                                   "criterion_id": "c", "verdict": "fail", "artifact": artifact})
    row = write(grok, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e1", criterion_id="c",
                verdict="fail", artifact=artifact, report_ref=ref, report_sha256=sha, director_identity="codex/w1")
    assert row["director_identity"] == "codex/w1"
    # The origin driver can no longer attest under it.
    from core.state_graph import StateGraphError
    with pytest.raises((ClaimError, StateGraphError)):
        write(codex, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e2", criterion_id="c",
              verdict="fail", artifact=artifact, report_ref=ref, report_sha256=sha, director_identity="codex/w1")


# 7 ---------------------------------------------------------------------------
def test_7_settling_a_worker_releases_its_side_claims(db, clock, tmp_path):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/w1#w1")
    side = claim(codex, "b", subagent="codex/w1", workspace="linxuhaserver:/side#b")      # attempt_id NULL
    out = terminal_report(codex, attempt, tmp_path, fence=1)
    assert out["settled_subagents"] == ["codex/w1"]
    assert claims_of(db, claim_id=side["claim_id"])[0]["status"] == "released"
    beat = write(codex, "heartbeat", project_id="p", claims=[{"claim_id": side["claim_id"], "fence": side["fence"]}])
    assert beat["refused"][0]["error"] == "claim_not_live"
    assert code_of(lambda: external(codex, "b", "rk-b", claim_id=side["claim_id"], fence=side["fence"])) == "stale_fence"
    assert claim(svc(db, "grok"), "c", workspace="linxuhaserver:/side#g")["status"] == "live", "checkout reusable"
    # The owner-settlement and orphan paths release side claims too.
    grok = svc(db, "grok")
    held2 = claim(codex, "b", workspace="h:/w2#c", request_key="b-again")
    second = external(codex, "b", "rk-b2", claim_id=held2["claim_id"], fence=held2["fence"])
    register(codex, second, tmp_path, "w2", host="macbook", workspace="macbook:/w2#w2")
    side2 = claim(codex, "c", "review", subagent="codex/w2", request_key="side2")
    reclaimable(clock)
    write(grok, "take_over_attempt", attempt_id=second["attempt_id"], expected_owner_fence=1, reason="gone")
    moved = [c for c in claims_of(db, node_key="c", subagent="codex/w2", status="live")]
    assert moved and moved[0]["driver_id"] == "grok"
    write(grok, "adopt_subagent", project_id="p", subagent_id="codex/w2", fence=2, observability="unobservable", reason="mac")
    assert claims_of(db, claim_id=moved[0]["claim_id"])[0]["status"] == "revoked"
    assert claims_of(db, claim_id=side2["claim_id"])[0]["status"] == "transferred"


# 8 (minor) --------------------------------------------------------------------
def test_8_delivery_hook_receives_the_complete_notice_with_its_sender(db, clock):
    seen = []
    driver_notices.set_inbox_adapter(deliver=lambda conn, message, notice: seen.append((message, notice)) or "m")
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
          attempt_id=attempt["attempt_id"], to_driver_id="grok", package={"next_step": "x", "workers": {"quiescent": True}})
    message, notice = seen[-1]
    assert notice["sender_driver_id"] == "codex" and notice["target_driver_id"] == "grok"
    assert notice["project_id"] == "p" and notice["refs"]["attempt_id"] == attempt["attempt_id"]
    assert message["_system"] is False and message["kind"] == "handoff_offer"
    assert driver_notices.inbox_message(notice) == message
