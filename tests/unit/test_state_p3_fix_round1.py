"""Multi-driver P3, fix round 1: one regression test per CONFIRMED review finding
(P3_REVIEW_CODEX.md, 2026-10-10). Each test fails on 200b73c3 and passes after
the fix named in P3_REPORT.md "Fix round 1".
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from core import drivers
from core.state_claims import ClaimError
from core.state_commands import ProjectPrivate, execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError, digest
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    GRACE, LEASE, NODE, _insert_skillflow_attempt, a_file, claim, clock, code_of, db, events, external,
    notices, project, read, reclaimable, register, report, svc, write)


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
        # An admin launches as break glass; the owner is told.
        assert owner.attempts.claim_launch("attempt-res", "owner-cli", True, "driver:owner-cli") is True
        launching = events(db, "attempt_launching")[-1]
        assert launching["break_glass"] is True and launching["owner_driver_id"] == "codex"
        assert notices(codex, kinds=["break_glass"])[0]["refs"]["attempt_id"] == "attempt-res"

    def test_owner_with_live_claim_launches(self, db, clock):
        project(db, enforce=True)
        codex = svc(db, "codex")
        claim(codex, "b")
        reserved_skillflow_attempt(db, "attempt-sf", "codex")
        assert codex.attempts.claim_launch("attempt-sf", "codex", False, "driver:codex") is True
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
        assert code_of(lambda: claim(third, "b", workspace="linxuhaserver:/w#other")) == "workspace_in_use"
        offer = write(grok, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=2,
                      attempt_id=attempt["attempt_id"], to_driver_id="third",
                      package={"next_step": "x", "workers": {"quiescent": True}})
        moved = write(third, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=2)
        assert moved["claim"]["workspace"] == "linxuhaserver:/w#c" and moved["claim"]["driver_id"] == "third"
        assert code_of(lambda: claim(grok, "b", workspace="linxuhaserver:/w#again")) == "workspace_in_use"


# 3 ---------------------------------------------------------------------------
class TestFinding3OrphanKeepsCheckout:
    def test_orphaned_checkout_is_reserved_until_settled_even_on_another_branch(self, db, clock, tmp_path):
        project(db, enforce=True)
        codex, grok = svc(db, "codex"), svc(db, "grok")
        held = claim(codex, "a", workspace="linxuhaserver:/w#c")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        register(codex, attempt, tmp_path, "mac", host="macbook-air", workspace="macbook-air:/w#mac")
        reclaimable(clock)
        write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        write(grok, "adopt_subagent", project_id="p", subagent_id="codex/mac", fence=2, observability="unobservable",
              reason="mac")
        assert code_of(lambda: register(grok, attempt, tmp_path, "mac2", host="macbook-air",
                                        workspace="macbook-air:/w#new-branch")) == "workspace_in_use"
        assert code_of(lambda: claim(grok, "b", workspace="macbook-air:/w#plan")) == "workspace_in_use"
        assert register(grok, attempt, tmp_path, "mac3", host="macbook-air", workspace="macbook-air:/w2#mac3")["status"] == "active"
        ref, sha = a_file(tmp_path, "settled.json", {"process": "exited"})
        write(codex, "report_subagent_settled", project_id="p", subagent_id="codex/mac", quiescent=True,
              report_ref=ref, report_sha256=sha)
        assert register(grok, attempt, tmp_path, "mac4", host="macbook-air", workspace="macbook-air:/w#new-branch")["status"] == "active"


# 4 ---------------------------------------------------------------------------
class TestFinding4FormerOwnerIsFencedOut:
    def test_former_owner_cannot_claim_attest_or_renew_under_a_transferred_worker(self, db, clock, tmp_path):
        project(db, enforce=True)
        codex, grok = svc(db, "codex"), svc(db, "grok")
        held = claim(codex, "a", workspace="h:/w#c")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        register(codex, attempt, tmp_path, "mac", host="macbook-air", workspace="macbook-air:/w#mac")
        side = claim(codex, "b", "review", subagent="codex/mac")      # a claim on ANOTHER node for the worker
        reclaimable(clock)
        write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        # Claims held for the worker moved with it; the old row is transferred.
        assert claims_of(db, claim_id=side["claim_id"])[0]["status"] == "transferred"
        moved = [c for c in claims_of(db, node_key="b", status="live")]
        assert moved and moved[0]["driver_id"] == "grok" and moved[0]["subagent"] == "codex/mac" and moved[0]["purpose"] == "review"
        # The former owner neither claims nor attests under that identity, and renews nothing.
        assert code_of(lambda: claim(codex, "c", "review", subagent="codex/mac", request_key="again")) == "subagent_unregistered"
        assert code_of(lambda: write(codex, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e1",
                                     criterion_id="c", verdict="pass", artifact="a" * 40, report_ref="/x",
                                     report_sha256="0" * 64, director_identity="codex/mac")) == "subagent_unregistered"
        beat = write(codex, "heartbeat", project_id="p", subagents=[{"subagent_id": "codex/mac"}])
        assert beat["renewed"] == [] and beat["refused"][0]["error"] == "not_subagent_owner"
        # Orphaning revokes the worker's claims; nobody renews them.
        write(grok, "adopt_subagent", project_id="p", subagent_id="codex/mac", fence=2, observability="unobservable",
              reason="mac")
        assert {c["status"] for c in claims_of(db, subagent="codex/mac")} == {"transferred", "revoked"}
        beat = write(grok, "heartbeat", project_id="p", subagents=[{"subagent_id": "codex/mac"}])
        assert beat["renewed"] == [] and beat["refused"][0]["error"] == "orphaned"
        assert events(db, "subagent_orphaned")[-1]["revoked_claims"] == [moved[0]["claim_id"]]


# 5 ---------------------------------------------------------------------------
def test_finding5_attempt_handoff_needs_an_explicit_quiescence_attestation(db, clock, tmp_path):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    register(codex, attempt, tmp_path, "mac", host="macbook-air")
    offer = lambda pkg, key: write(codex, "offer_handoff", project_id="p", request_key=key, expected_owner_fence=1,
                                   attempt_id=attempt["attempt_id"], to_driver_id="grok", package=pkg)
    assert code_of(lambda: offer({}, "h0")) == "quiescence_required"
    assert code_of(lambda: offer({"next_step": "x"}, "h1")) == "quiescence_required"
    with pytest.raises(StateGraphError):
        offer({"workers": {"detail": "no boolean"}}, "h2")
    stated = offer({"workers": {"quiescent": False}}, "h3")
    assert code_of(lambda: write(grok, "accept_handoff", project_id="p", handoff_id=stated["handoff_id"],
                                 expected_owner_fence=1)) == "receiver_cannot_observe_workers"


# 6 ---------------------------------------------------------------------------
def test_finding6_structural_guard_write_and_notices_are_one_transaction(db, clock, monkeypatch):
    project(db, enforce=True)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    claim(codex, "a")
    from core import driver_notices

    def boom(*args, **kwargs):
        raise RuntimeError("notice store unavailable")

    monkeypatch.setattr(driver_notices, "notify", boom)
    with pytest.raises(RuntimeError):
        write(grok, "revise_node", project_id="p", node_key="a", expected_revision=1, reason="r", override_reason="x")
    assert read(grok, "get_node", project_id="p", node_key="a")["node"]["revision"] == 1
    assert events(db, "node_revised") == [] and events(db, "claim_overridden") == []
    monkeypatch.undo()
    assert write(grok, "revise_node", project_id="p", node_key="a", expected_revision=1, reason="r",
                 override_reason="x")["revision"] == 2
    assert len(events(db, "claim_overridden")) == 1 and len(notices(codex, kinds=["override_notice"])) == 1
    # The hold path is one transaction too.
    write(codex, "set_node_hold", project_id="p", node_key="b", held=True, expected_revision=0, reason="w")
    monkeypatch.setattr(driver_notices, "notify", boom)
    with pytest.raises(RuntimeError):
        write(grok, "set_node_hold", project_id="p", node_key="b", held=False, expected_revision=1, reason="g",
              override_reason="x")
    assert read(grok, "get_node", project_id="p", node_key="b")["node"]["node_hold"]["held"] == 1


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


# 8 ---------------------------------------------------------------------------
def test_finding8_admin_reports_as_break_glass_and_the_owner_is_told(db, clock):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    owner_cli = svc(db, "owner-cli", admin=True)
    out = report(owner_cli, attempt, "running", fence=1)
    assert out["break_glass"] is True and out["observation"]["actor"] == "driver:owner-cli"
    assert events(db, "external_attempt_observed")[-1]["break_glass"] is True
    assert notices(codex, kinds=["break_glass"])[0]["refs"]["observation_id"] == "o1"
    email = StateService(StateDatabase(str(db)), actor="owner:o@example.com", project_read_trusted=True, is_admin=True)
    out = report(email, {**attempt, "observation_version": 1}, "running", oid="o2")
    assert out["break_glass"] is True
    # A plain driver still cannot.
    assert code_of(lambda: report(svc(db, "grok"), {**attempt, "observation_version": 2}, "running", fence=1, oid="o3")) \
        == "not_attempt_owner"


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


# 11 --------------------------------------------------------------------------
def test_finding11_completed_workers_settle_and_release_their_checkout(db, clock, tmp_path):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/sub#w1")
    assert code_of(lambda: claim(codex, "b", workspace="linxuhaserver:/sub#plan")) == "workspace_in_use"
    out = terminal_report(codex, attempt, tmp_path, fence=1)
    assert out["settled_subagents"] == ["codex/w1"]
    statuses = {s["subagent_id"]: s["status"] for s in read(codex, "list_subagents", project_id="p")["subagents"]}
    assert statuses == {"codex/w1": "settled"}
    assert claim(codex, "b", workspace="linxuhaserver:/sub#plan")["status"] == "live"


def test_finding11_owner_settles_a_worker_of_an_abandoned_attempt(db, clock, tmp_path):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/sub#w1")
    ref, sha = a_file(tmp_path, "settled.json", {"process": "exited"})
    settle = lambda who, **e: write(who, "report_subagent_settled", project_id="p", subagent_id="codex/w1",
                                    quiescent=True, report_ref=ref, report_sha256=sha, **e)
    assert code_of(lambda: settle(codex, fence=1)) == "not_orphaned", "still active: report the attempt instead"
    reclaimable(clock)
    write(grok, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")
    assert code_of(lambda: settle(grok, fence=1)) == "not_subagent_owner"
    assert code_of(lambda: settle(codex, fence=7)) == "stale_fence"
    assert settle(codex, fence=1)["status"] == "settled"
    assert settle(codex)["idempotent"] is True


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


# 13 --------------------------------------------------------------------------
def test_finding13_subagent_context_bytes_are_private(db, clock, tmp_path):
    owner = project(db)
    codex = svc(db, "codex")
    attempt = external(codex, "a")
    sub = register(codex, attempt, tmp_path, "w1")
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM state_external_report_blobs WHERE report_sha256=?",
                        (sub["context_sha256"],)).fetchone()[0] == 0
    assert conn.execute("SELECT context_bytes FROM state_subagent_contexts WHERE context_sha256=?",
                        (sub["context_sha256"],)).fetchone()[0] == tmp_path.joinpath("ctx-w1.json").read_bytes()
    conn.close()
    owner.open_project("p")
    anonymous = svc(db, trusted=False)
    with pytest.raises(ProjectPrivate):
        with anonymous.store.db.get_connection() as armed:
            armed.execute("SELECT context_bytes FROM state_subagent_contexts").fetchall()
    with anonymous.store.db.get_connection() as armed:
        assert armed.execute("SELECT COUNT(*) FROM state_external_report_blobs").fetchone()[0] == 0


# 14 --------------------------------------------------------------------------
def test_finding14_pool_offers_reach_project_members(db, clock, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "secrets"))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / drivers.PEPPER_SECRET_NAME).write_text("p" * 48)
    monkeypatch.setenv(drivers.FEATURE_ENV, "on")
    drivers._REGISTRIES.clear()
    try:
        project(db)
        registry = drivers.registry_for(StateDatabase(str(db)))
        for who in ("codex", "grok", "gone"):
            registry.register(who, who.title(), actor="t")
            registry.set_membership("p", who, "member", 0, "join", actor="t")
        registry.set_membership("p", "gone", "removed", 1, "left", actor="t")
        codex = svc(db, "codex")
        attempt = external(codex, "a")
        offer = write(codex, "offer_handoff", project_id="p", request_key="pool", expected_owner_fence=1,
                      attempt_id=attempt["attempt_id"], package={"next_step": "anyone", "workers": {"quiescent": True}})
        assert [n["target_driver_id"] for n in offer["notified"]] == ["grok"]
        assert notices(svc(db, "grok"), kinds=["handoff_offer"])[0]["refs"]["handoff_id"] == offer["handoff_id"]
        assert notices(svc(db, "gone"), kinds=["handoff_offer"]) == []
    finally:
        drivers._REGISTRIES.clear()


# 15 --------------------------------------------------------------------------
def test_finding15_attempt_scoped_waits_see_their_notices(db, clock):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    cursor = read(codex, "project_overview", project_id="p")["event_seq"]
    reclaimable(clock)
    write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
    got = asyncio.run(codex.wait_for_state_change("p", after=cursor, attempt_ids=[attempt["attempt_id"]],
                                                  timeout_seconds=0))
    kinds = [(e["event_type"], e["payload"].get("target_driver_id")) for e in got["events"]]
    assert ("driver_notice", "codex") in kinds, kinds
    notice = next(e for e in got["events"] if e["event_type"] == "driver_notice")
    assert notice["payload"]["attempt_id"] == attempt["attempt_id"] and notice["payload"]["node_key"] == "a"


# 16 --------------------------------------------------------------------------
def test_finding16_inbox_message_normalizes_a_notice_to_the_p2_contract():
    from core.driver_notices import INBOX_REF_KEYS, inbox_message
    notice = {"notice_id": "notice-1", "target_driver_id": "codex", "sender_driver_id": None, "project_id": "p",
              "kind": "break_glass", "delivery_mode": "transient", "subject": "s" * 300, "body": "b",
              "refs": {"attempt_id": "attempt-1", "node_key": "a", "fence": 2, "break_glass": True, "held": [{}],
                       "claim_id": None}}
    message = inbox_message(notice)
    assert message["kind"] == "lease_notice" and message["_system"] is True
    assert message["request_key"] == "notice-1" and len(message["subject"]) == 200
    assert set(message["refs"]) <= set(INBOX_REF_KEYS) and message["refs"] == {"attempt_id": "attempt-1", "node_key": "a"}
    assert message["body"].startswith("[break_glass] ")
    reply = inbox_message({**notice, "kind": "handoff_reply", "sender_driver_id": "grok", "subject": "ok", "body": "b"})
    assert reply["kind"] == "handoff_reply" and reply["_system"] is False
    pooled = inbox_message({**notice, "kind": "handoff_offer", "sender_driver_id": "grok"}, project_members="p")
    assert pooled["target_driver_id"] is None and pooled["project_members"] == "p"
    # The keyword set is exactly P2's send_driver_message signature (branch 12fbef9a), JSON-serializable.
    assert set(message) == {"request_key", "subject", "body", "target_driver_id", "project_members", "project_id",
                            "kind", "delivery_mode", "refs", "reply_to_message_id", "_system"}
    assert json.loads(json.dumps(message)) == message
    stored = {**notice, "refs_json": json.dumps(notice["refs"])}
    stored.pop("refs")
    assert inbox_message(stored)["refs"] == {"attempt_id": "attempt-1", "node_key": "a"}, "stored rows carry refs_json"
    with pytest.raises(StateGraphError):
        inbox_message({k: v for k, v in notice.items() if k != "sender_driver_id"})
