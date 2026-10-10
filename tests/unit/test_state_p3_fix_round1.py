"""Multi-driver P3, fix round 1: regression tests of the CONFIRMED review findings
(P3_REVIEW_CODEX.md, 2026-10-10) whose mechanisms survive the slimming
(P3_SLIM.md): 1 launch authorization, 2 transfers keep the workspace, 7 lapsed
claims authorize nothing, 9 enforcement off is P1, 10 the deployment gate and
abandoned owners, 12 a review-claim handoff. Findings 1, 2, 7 and 9 are
MUST-FIX regressions (P3_SLIM.md section c); the tests of removed mechanisms
(checkout occupancy, subagent adoption/orphans, admin reporting, pool offers,
notice adapter) went with them.
"""
from __future__ import annotations

import sqlite3

import pytest

from core.state_claims import ClaimError
from core.state_graph import StateConflict, digest
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    GRACE, LEASE, NODE, a_file, claim, clock, code_of, db, events, external, notices, project, read,
    reclaimable, registered, report, svc, write)


def terminal_report(service, attempt, tmp_path, fence=None, status="failed", oid="done"):
    ref, sha = a_file(tmp_path, f"{oid}.json", {"status": status, "settled": True, "usable": True,
                                                 "reason": "worker settled"})
    args = dict(attempt_id=attempt["attempt_id"], observation_id=oid, expected_version=attempt["observation_version"],
                context_hash=attempt["context_hash"], status=status, report_ref=ref, report_sha256=sha,
                quiescent=True, detail="settled")
    if fence is not None:
        args["fence"] = fence
    return write(service, "report_external_attempt", **args)


def reserved_skillflow_attempt(db, attempt_id, owner, node="b"):
    """A RESERVED SkillFlow attempt row that pins node ``node`` at its current
    contract (so claim_launch's freshness check passes) and is owned by ``owner``."""
    conn = sqlite3.connect(str(db))
    contract = conn.execute("SELECT contract_hash FROM state_nodes WHERE project_id='p' AND node_key=?",
                            (node,)).fetchone()[0]
    conn.execute("INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                 "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,"
                 "status,created_at,updated_at,execution_kind,owner_driver_id,owner_fence,lease_expires_at,"
                 "last_heartbeat_at) VALUES(?,'p',?,1,?,'{}','{}',?,?,'w',?,'reserved','t','t','skillflow',?,1,"
                 "'2999-01-01T00:00:00.000000+00:00','2026-01-01T00:00:00.000000+00:00')",
                 (attempt_id, node, contract, "rk-" + attempt_id,
                  digest({"revision": 1, "workflow": "w", "instruction": ""}), "sg-" + attempt_id, owner))
    conn.commit()
    conn.close()


def claims_of(db, **where):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM state_node_claims"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at, claim_id", tuple(where.values()))]
    finally:
        conn.close()


# 1 ---------------------------------------------------------------------------
class TestFinding1LaunchAuthorization:
    def test_reserved_to_launching_is_authorized_for_owner_claim_and_admin(self, db, clock):
        project(db, enforce=True)
        registered(db, "codex", "grok", "owner-cli")
        codex, grok, owner = svc(db, "codex"), svc(db, "grok"), svc(db, "owner-cli", admin=True)
        reserved_skillflow_attempt(db, "attempt-res", "codex")
        # Another driver (a recovery, a replay) cannot launch codex's reservation.
        with pytest.raises(ClaimError) as refused:
            grok.attempts.claim_launch("attempt-res", "grok", False, "driver:grok")
        assert refused.value.code == "not_attempt_owner"
        # The owner without a live implement claim cannot either.
        with pytest.raises(ClaimError) as refused:
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex")
        assert refused.value.code == "claim_required"
        assert read(codex, "get_attempt", attempt_id="attempt-res")["status"] == "reserved"
        # An admin launches as break glass: recorded on the event (slim: an audit
        # flag only, no notice).
        assert owner.attempts.claim_launch("attempt-res", "owner-cli", True, "driver:owner-cli") is True
        launching = events(db, "attempt_launching")[-1]
        assert launching["break_glass"] is True and launching["owner_driver_id"] == "codex"
        assert launching["launched_by"] == "owner-cli" and notices(db, "codex") == []

    def test_owner_with_live_claim_launches(self, db, clock):
        project(db, enforce=True)
        codex = svc(db, "codex")
        held = claim(codex, "b")
        reserved_skillflow_attempt(db, "attempt-sf", "codex")
        # Round 3: an unbound live claim launches only when the caller NAMES it.
        with pytest.raises(ClaimError):
            codex.attempts.claim_launch("attempt-sf", "codex", False, "driver:codex")
        assert codex.attempts.claim_launch("attempt-sf", "codex", False, "driver:codex",
                                           claim_id=held["claim_id"], fence=held["fence"]) is True
        assert "break_glass" not in events(db, "attempt_launching")[-1]

    def test_replaying_another_drivers_request_key_does_not_hand_over_the_attempt(self, db, clock):
        project(db, enforce=True)
        codex, grok = svc(db, "codex"), svc(db, "grok")
        reserved_skillflow_attempt(db, "attempt-res", "codex")      # request_key rk-attempt-res, codex's
        with pytest.raises(ClaimError) as refused:
            grok.attempts.reserve("p", "b", 1, "w", "rk-attempt-res", owner_driver_id="grok")
        assert refused.value.code == "not_attempt_owner"
        claim(codex, "b")
        again = codex.attempts.reserve("p", "b", 1, "w", "rk-attempt-res", owner_driver_id="codex")
        assert again["attempt_id"] == "attempt-res" and again["status"] == "reserved", "the owner's replay is idempotent"
        # An external replay by another driver was already refused by the request hash
        # (the reporting actor is part of it); it stays a conflict, never a hand-over.
        held = claim(codex, "a")
        external(codex, "a", "shared-key", claim_id=held["claim_id"], fence=1)
        with pytest.raises(StateConflict):
            external(grok, "a", "shared-key")


# 2 ---------------------------------------------------------------------------
class TestFinding2TransferKeepsWorkspace:
    def test_takeover_and_handoff_keep_the_checkout_reserved(self, db, clock):
        project(db, enforce=True)
        codex, grok, third = svc(db, "codex"), svc(db, "grok"), svc(db, "third")
        held = claim(codex, "a", workspace="linxuhaserver:/w#c")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        reclaimable(clock)
        taken = write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        assert taken["claim"]["workspace"] == "linxuhaserver:/w#c" and taken["claim"]["purpose"] == "implement"
        assert taken["claim"]["attempt_id"] == attempt["attempt_id"]
        # Slim scope is the node: nobody else can claim it while the moved claim lives
        # (the cross-node checkout check was removed with P3_SLIM.md).
        assert code_of(lambda: claim(third, "a", workspace="linxuhaserver:/w#other")) == "claimed_by_other"
        offer = write(grok, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=2,
                      attempt_id=attempt["attempt_id"], to_driver_id="third",
                      package={"next_step": "x", "workers": {"quiescent": True}})
        moved = write(third, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=2)
        assert moved["claim"]["workspace"] == "linxuhaserver:/w#c" and moved["claim"]["driver_id"] == "third"
        assert moved["claim"]["attempt_id"] == attempt["attempt_id"]
        assert code_of(lambda: claim(grok, "a", workspace="linxuhaserver:/w#again")) == "claimed_by_other"


# 7 ---------------------------------------------------------------------------
def test_finding7_a_lapsed_claim_authorizes_no_dispatch_even_before_any_sweep(db, clock):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    clock.advance(LEASE + GRACE)                 # no read, no sweep in between
    assert code_of(lambda: external(codex, "a")) == "claim_required"
    assert code_of(lambda: external(codex, "a", claim_id=held["claim_id"], fence=1)) == "claim_required"
    # Within the grace the claim still authorizes.
    fresh = claim(codex, "b", workspace="h:/w2#c")
    clock.advance(LEASE + GRACE - 1)
    assert external(codex, "b", "rk-b", claim_id=fresh["claim_id"], fence=fresh["fence"])["owner_driver_id"] == "codex"


# 9 ---------------------------------------------------------------------------
def test_finding9_enforcement_off_binds_and_releases_nothing(db, clock, tmp_path):
    project(db)                                   # multi_driver on, enforcement off: P1 behaviour
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a")
    assert claims_of(db, claim_id=held["claim_id"])[0]["attempt_id"] is None
    terminal_report(codex, attempt, tmp_path)
    row = claims_of(db, claim_id=held["claim_id"])[0]
    assert row["status"] == "live"
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM state_claim_history WHERE claim_id=?", (held["claim_id"],)).fetchone()[0] == 1
    conn.close()
    assert events(db, "claim_released") == []
    # Naming the claim explicitly is the opt-in: then it is bound and released.
    other = claim(codex, "b", workspace="h:/w2#c")
    second = external(codex, "b", "rk-b", claim_id=other["claim_id"], fence=other["fence"])
    assert claims_of(db, claim_id=other["claim_id"])[0]["attempt_id"] == second["attempt_id"]
    terminal_report(codex, second, tmp_path, oid="done-b")
    assert claims_of(db, claim_id=other["claim_id"])[0]["status"] == "released"


# 10 --------------------------------------------------------------------------
def test_finding10_deployment_gate_understands_abandoned_owners(tmp_path, monkeypatch, clock):
    from core import deployment_quiescence as dq
    from core.db_manager import DBManager
    from tests.unit.test_deployment_quiescence import _quiet_sf
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    database = DBManager(str(tmp_path / "state.sqlite3"))

    def service(driver, admin=False):
        return StateService(database, actor=f"driver:{driver}", project_read_trusted=True, driver_id=driver,
                            is_admin=admin)

    owner = service("owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE}])
    write(owner, "set_multi_driver", project_id="p", multi_driver="on", expected_revision=0, reason="r")
    codex, grok = service("codex"), service("grok")
    confirmed = external(codex, "a", "job-a")
    unknown = external(codex, "b", "job-b")
    reclaimable(clock)
    ref, sha = a_file(tmp_path, "quiet.json", {"workers": "exited"})
    write(grok, "abandon_external_attempt", attempt_id=confirmed["attempt_id"], expected_owner_fence=1,
          abandon_kind="confirmed_stopped", reason="codex confirmed", report_ref=ref, report_sha256=sha)
    write(grok, "abandon_external_attempt", attempt_id=unknown["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")
    observed = dq.measure(skillflow=_quiet_sf(), db=database, external_probe=list)
    assert observed["errors"] == [], observed["errors"]
    blockers = observed["blockers"]["registered_external_owners"]
    assert [b["attempt_id"] for b in blockers] == [unknown["attempt_id"]], "confirmed_stopped is settled; unknown blocks"
    assert blockers[0]["status"] == "abandoned" and observed["quiescent"] is False
    # The original reporter later attests quiescence: the owner row settles.
    late = report(codex, unknown, "running", fence=1, oid="late")
    assert late["late_after_abandon"] is True and late["owner_settled"] is False
    with pytest.raises(StateConflict):          # round 2 (N1): a bare path and hash settle nothing
        write(codex, "report_external_attempt", attempt_id=unknown["attempt_id"], observation_id="bogus",
              expected_version=1, context_hash=unknown["context_hash"], status="running",
              report_ref="/tmp/x", report_sha256="0" * 64, quiescent=True, fence=1)
    still = dq.measure(skillflow=_quiet_sf(), db=database, external_probe=list)
    assert [b["attempt_id"] for b in still["blockers"]["registered_external_owners"]] == [unknown["attempt_id"]]
    quiet_ref, quiet_sha = a_file(tmp_path, "late-quiet.json", {"workers": "exited", "checked_by": "codex"})
    late_quiet = write(codex, "report_external_attempt", attempt_id=unknown["attempt_id"], observation_id="quiet",
                       expected_version=1, context_hash=unknown["context_hash"], status="running",
                       report_ref=quiet_ref, report_sha256=quiet_sha, quiescent=True, fence=1)
    assert late_quiet["owner_settled"] is True and late_quiet["status"] == "abandoned"
    observed = dq.measure(skillflow=_quiet_sf(), db=database, external_probe=list)
    assert observed["blockers"]["registered_external_owners"] == [] and observed["errors"] == []


# 12 --------------------------------------------------------------------------
def test_finding12_review_claim_handoff_beside_an_implement_claim(db, clock):
    project(db, enforce=True)
    codex, grok, third = svc(db, "codex"), svc(db, "grok"), svc(db, "third")
    claim(grok, "a", workspace="h:/w#g")                         # the exclusive holder
    review = claim(codex, "a", "review")
    offer = write(codex, "offer_handoff", project_id="p", request_key="r", expected_owner_fence=review["fence"],
                  claim_id=review["claim_id"], to_driver_id="third", package={"next_step": "review a"})
    out = write(third, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                expected_owner_fence=review["fence"])
    assert out["claim"]["purpose"] == "review" and out["claim"]["driver_id"] == "third"
    history = read(third, "get_claim", project_id="p", claim_id=out["claim"]["claim_id"])["history"]
    assert [h["purpose"] for h in history] == ["review"]
    assert events(db, "claim_acquired")[-1]["purpose"] == "review"
    assert len([c for c in claims_of(db, node_key="a", status="live")]) == 2
