"""Multi-driver P3, fix round 2: one regression test per CONFIRMED item of the
second Codex review (P3_REVIEW2_CODEX.md). Each fails on 89f5fcbc and passes
after the fix named in P3_REPORT.md "Fix round 2".
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from core import driver_notices
from core.state_claims import ClaimError
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    GRACE, LEASE, NODE, a_file, claim, clock, code_of, db, events, external, notices, project, read,
    reclaimable, register, report, svc, write)
from tests.unit.test_state_p3_fix_round1 import claims_of, reserved_skillflow_attempt, terminal_report


@pytest.fixture(autouse=True)
def _no_inbox_adapter():
    # Tolerant of the pre-fix tree (no adapter seam there), so every test in this
    # module fails on 89f5fcbc for ITS reason, not for a missing fixture.
    reset = getattr(driver_notices, "set_inbox_adapter", lambda **_: None)
    reset()
    yield
    reset()


# R1 --------------------------------------------------------------------------
class TestR1StaleFenceReplay:
    def test_external_replay_is_judged_on_the_named_claim(self, db, clock):
        project(db, enforce=True)
        codex = svc(db, "codex")
        c1 = claim(codex, "a", workspace="h:/w#c")
        external(codex, "a", "k", claim_id=c1["claim_id"], fence=1)
        write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="oops")
        c2 = claim(codex, "a", workspace="h:/w#c", request_key="c2")
        assert code_of(lambda: external(codex, "a", "k", claim_id=c1["claim_id"], fence=1)) == "stale_fence"
        assert external(codex, "a", "k", claim_id=c2["claim_id"], fence=c2["fence"])["owner_driver_id"] == "codex"
        assert code_of(lambda: external(codex, "a", "k", claim_id=c2["claim_id"], fence=99)) == "stale_fence"

    def test_launch_requires_a_live_claim_and_binds_the_current_one(self, db, clock):
        project(db, enforce=True)
        codex = svc(db, "codex")
        c1 = claim(codex, "b", workspace="h:/w#c")
        reserved_skillflow_attempt(db, "attempt-res", "codex")
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE state_node_claims SET attempt_id='attempt-res' WHERE claim_id=?", (c1["claim_id"],))
        conn.commit()
        conn.close()
        write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="oops")
        with pytest.raises(ClaimError) as refused:              # the bound claim is gone: memory is not authority
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex")
        assert refused.value.code == "claim_required"
        c2 = claim(codex, "b", workspace="h:/w#c", request_key="c2")
        assert codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex") is True
        assert claims_of(db, claim_id=c2["claim_id"])[0]["attempt_id"] == "attempt-res"
        assert events(db, "attempt_launching")[-1]["claim_id"] == c2["claim_id"]


# R2 --------------------------------------------------------------------------
def test_r2_takeover_after_a_sweep_still_keeps_the_executor_checkout(db, clock):
    project(db, enforce=True)
    codex, grok, third = svc(db, "codex"), svc(db, "grok"), svc(db, "third")
    held = claim(codex, "a", workspace="linxuhaserver:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    reclaimable(clock)
    swept = read(grok, "list_claims", project_id="p", statuses=["expired"])["claims"]   # the sweep retires it
    assert [c["claim_id"] for c in swept] == [held["claim_id"]]
    # Even with no live claim, the active attempt's executor checkout is reserved.
    assert code_of(lambda: claim(third, "b", workspace="linxuhaserver:/w#plan")) == "workspace_in_use"
    taken = write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
    assert taken["claim"]["workspace"] == "linxuhaserver:/w#c"
    assert code_of(lambda: claim(third, "b", workspace="linxuhaserver:/w#plan")) == "workspace_in_use"


# R4 --------------------------------------------------------------------------
def test_r4_evidence_authorization_runs_inside_the_write_transaction(db, clock, tmp_path):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    terminal_report(codex, attempt, tmp_path, fence=1)
    seen = []

    def refuse(conn, row):
        seen.append((conn.in_transaction, row["attempt_id"]))
        raise ClaimError("subagent_unregistered", "simulated transfer landed first")

    with pytest.raises(ClaimError):
        codex.attempts.record_evidence(attempt["attempt_id"], "e1", "c", "fail", "a" * 40, "/r", "0" * 64,
                                       "driver:codex", authorize=refuse)
    assert seen == [(True, attempt["attempt_id"])], "judged inside the write transaction, on the attempt row"
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM state_evidence").fetchone()[0] == 0
    conn.close()
    # The service wires the same check: a former owner cannot attest under a moved worker.
    assert code_of(lambda: write(codex, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e2",
                                 criterion_id="c", verdict="pass", artifact="a" * 40, report_ref="/x",
                                 report_sha256="0" * 64, director_identity="codex/ghost")) == "subagent_unregistered"


# R15 -------------------------------------------------------------------------
def test_r15_decline_replies_reach_attempt_scoped_waits(db, clock):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="grok",
                  package={"next_step": "x", "workers": {"quiescent": True}})
    cursor = read(codex, "project_overview", project_id="p")["event_seq"]
    write(grok, "decline_handoff", project_id="p", handoff_id=offer["handoff_id"], reason="busy")
    got = asyncio.run(codex.wait_for_state_change("p", after=cursor, attempt_ids=[attempt["attempt_id"]],
                                                  timeout_seconds=0))
    replies = [e for e in got["events"] if e["event_type"] == "driver_notice" and e["payload"]["kind"] == "handoff_reply"]
    assert replies and replies[0]["payload"]["attempt_id"] == attempt["attempt_id"]
    assert replies[0]["payload"]["handoff_id"] == offer["handoff_id"]
    assert got["next_after"] >= replies[0]["seq"]


# R16 / N7 ---------------------------------------------------------------------
class TestR16InboxAdapter:
    def test_stored_rows_normalize_with_refs_and_metadata(self, db, clock):
        project(db)
        codex, grok = svc(db, "codex"), svc(db, "grok")
        attempt = external(codex, "a")
        reclaimable(clock)
        write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM driver_notices WHERE target_driver_id='codex'").fetchone())
        conn.close()
        message = driver_notices.inbox_message(row)                   # the stored row, refs_json and all
        assert message["refs"] == {"attempt_id": attempt["attempt_id"], "node_key": "a"}
        assert message["project_id"] == "p" and message["_system"] is True and message["kind"] == "takeover_notice"
        assert message["target_driver_id"] == "codex" and message["request_key"] == row["notice_id"]
        offer = write(grok, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=2,
                      attempt_id=attempt["attempt_id"], to_driver_id="codex",
                      package={"next_step": "x", "workers": {"quiescent": True}})
        reply = driver_notices.inbox_message(offer["notified"][0])    # notify()'s return value, complete
        assert reply["_system"] is False and reply["kind"] == "handoff_offer" and reply["project_id"] == "p"
        assert reply["refs"]["attempt_id"] == attempt["attempt_id"] and "node_key" in reply["refs"]
        with pytest.raises(StateGraphError):
            driver_notices.inbox_message({k: v for k, v in row.items() if k != "sender_driver_id"})

    def test_adapter_hooks_share_the_ownership_transaction(self, db, clock):
        delivered, resolved = [], []
        driver_notices.set_inbox_adapter(
            deliver=lambda conn, message: delivered.append((conn.in_transaction, message)) or "inbox-msg",
            resolve=lambda conn, rows, reason: resolved.append((conn.in_transaction, [r["refs"] for r in rows], reason)))
        project(db)
        codex, grok = svc(db, "codex"), svc(db, "grok")
        attempt = external(codex, "a")
        offer = write(codex, "offer_handoff", project_id="p", request_key="h", expected_owner_fence=1,
                      attempt_id=attempt["attempt_id"], to_driver_id="grok",
                      package={"next_step": "x", "workers": {"quiescent": True}})
        assert offer["notified"][0]["inbox"] == "inbox-msg"
        assert delivered[-1][0] is True and delivered[-1][1]["kind"] == "handoff_offer"
        assert delivered[-1][1]["refs"] == {"attempt_id": attempt["attempt_id"], "node_key": "a"}
        write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
        assert resolved and resolved[-1][0] is True and resolved[-1][2] == "accepted"
        assert resolved[-1][1][0]["handoff_id"] == offer["handoff_id"], "the correlation key survives resolution"


# N1 --------------------------------------------------------------------------
def test_n1_only_a_verified_quiescence_report_settles_an_abandoned_owner(db, clock, tmp_path):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    reclaimable(clock)
    write(grok, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")

    def late(oid, ref, sha, version):
        return write(codex, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id=oid,
                     expected_version=version, context_hash=attempt["context_hash"], status="running",
                     report_ref=ref, report_sha256=sha, quiescent=True, fence=1)

    with pytest.raises(StateConflict):                      # nonexistent path, all-zero digest
        late("bogus", "/tmp/does-not-exist", "0" * 64, 0)
    ref, sha = a_file(tmp_path, "quiet.json", {"workers": "exited"})
    with pytest.raises(StateConflict):                      # real file, wrong digest
        late("wrong", ref, "f" * 64, 0)
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT status FROM state_external_owners WHERE attempt_id=?",
                        (attempt["attempt_id"],)).fetchone()[0] == "abandoned"
    assert conn.execute("SELECT COUNT(*) FROM state_external_observations WHERE attempt_id=?",
                        (attempt["attempt_id"],)).fetchone()[0] == 0
    conn.close()
    out = late("ok", ref, sha, 0)
    assert out["owner_settled"] is True
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT status FROM state_external_owners WHERE attempt_id=?",
                        (attempt["attempt_id"],)).fetchone()[0] == "settled"
    assert conn.execute("SELECT 1 FROM state_external_report_blobs WHERE report_sha256=?", (sha,)).fetchone()
    conn.close()


# N2 --------------------------------------------------------------------------
def test_n2_every_checkout_of_an_unresolved_orphan_stays_reserved(db, clock, tmp_path):
    project(db, enforce=True)
    codex, grok, third = svc(db, "codex"), svc(db, "grok"), svc(db, "third")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "mac", host="macbook-air", workspace="macbook-air:/w#mac")
    claim(codex, "b", "plan", subagent="codex/mac", workspace="macbook-air:/second#mac")   # another checkout of the worker
    reclaimable(clock)
    write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
    write(grok, "adopt_subagent", project_id="p", subagent_id="codex/mac", fence=2, observability="unobservable",
          reason="mac")
    assert code_of(lambda: claim(third, "c", workspace="macbook-air:/second#x")) == "workspace_in_use"
    assert code_of(lambda: register(grok, attempt, tmp_path, "m2", host="macbook-air",
                                    workspace="macbook-air:/second#m2")) == "workspace_in_use"
    ref, sha = a_file(tmp_path, "settled.json", {"process": "exited"})
    write(codex, "report_subagent_settled", project_id="p", subagent_id="codex/mac", quiescent=True,
          report_ref=ref, report_sha256=sha)
    assert claim(third, "c", workspace="macbook-air:/second#x")["status"] == "live"


# N3 --------------------------------------------------------------------------
def test_n3_a_worker_may_claim_its_own_checkout_but_not_another_executors(db, clock, tmp_path):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/w1#w1")
    own = claim(codex, "b", subagent="codex/w1", workspace="linxuhaserver:/w1#b")       # same executor
    assert own["status"] == "live"
    assert code_of(lambda: claim(codex, "c", workspace="linxuhaserver:/w1#c")) == "workspace_in_use"   # no worker: a second writer
    assert code_of(lambda: register(codex, attempt, tmp_path, "w2", host="linxuhaserver",
                                    workspace="linxuhaserver:/w1#w2")) == "workspace_in_use"           # distinct executor
    # Reverse order: the claim first, then the worker it is held for.
    other = claim(codex, "c", "plan", workspace="linxuhaserver:/w3#c", subagent=None)
    assert other["status"] == "live"
    assert code_of(lambda: register(codex, attempt, tmp_path, "w3", host="linxuhaserver",
                                    workspace="linxuhaserver:/w3#w3")) == "workspace_in_use"           # not held FOR w3
    write(codex, "release_claim", project_id="p", claim_id=other["claim_id"], fence=other["fence"], reason="redo")
    assert register(codex, attempt, tmp_path, "w3", host="linxuhaserver", workspace="linxuhaserver:/w3#w3")["status"] == "active"
    assert claim(codex, "c", "plan", subagent="codex/w3", workspace="linxuhaserver:/w3#c",
                 request_key="for-w3")["status"] == "live"


# N4 --------------------------------------------------------------------------
def test_n4_a_settled_worker_still_supplies_attributed_evidence(db, clock, tmp_path):
    project(db, enforce=True)
    codex = svc(db, "codex")
    held = claim(codex, "a", workspace="h:/w#c")
    attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/w1#w1")
    out = terminal_report(codex, attempt, tmp_path, fence=1)                 # failed, quiescent
    assert out["settled_subagents"] == ["codex/w1"]
    assert code_of(lambda: claim(codex, "b", subagent="codex/w1")) == "subagent_unregistered", "settled: no more writes"
    artifact = "a" * 40
    ref, sha = a_file(tmp_path, "evidence.json", {"status": "completed", "settled": True, "usable": True,
                                                   "criterion_id": "c", "verdict": "fail", "artifact": artifact})
    row = write(codex, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e1", criterion_id="c",
                verdict="fail", artifact=artifact, report_ref=ref, report_sha256=sha, director_identity="codex/w1")
    assert row["director_identity"] == "codex/w1" and row["verdict"] == "fail"
    # A worker the caller never owned is still refused.
    assert code_of(lambda: write(codex, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id="e2",
                                 criterion_id="c", verdict="fail", artifact=artifact, report_ref=ref,
                                 report_sha256=sha, director_identity="codex/nobody")) == "subagent_unregistered"


# N5 --------------------------------------------------------------------------
@pytest.mark.parametrize("multi_driver", [False, True])
def test_n5_admin_reporting_bypass_exists_only_where_claims_are_enforced(db, clock, multi_driver):
    project(db, multi_driver=multi_driver)
    reporter = svc(db, "codex")
    attempt = external(reporter, "a")
    for admin in (svc(db, "owner-cli", admin=True),
                  StateService(StateDatabase(str(db)), actor="owner:o@example.com", project_read_trusted=True,
                               is_admin=True)):
        with pytest.raises(StateConflict) as refused:         # P1: only the recorded reporter continues it
            report(admin, attempt, "running")
        assert getattr(refused.value, "code", None) != "break_glass" and "break_glass" not in str(refused.value)
        assert events(db, "external_attempt_observed") == []
    assert report(reporter, attempt, "running")["observation"]["version"] == 1
    assert events(db, "external_attempt_observed")[-1]["break_glass"] is False


# N6 --------------------------------------------------------------------------
def test_n6_settling_your_own_worker_requires_its_current_fence(db, clock, tmp_path):
    project(db)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    attempt = external(codex, "a")
    register(codex, attempt, tmp_path, "w1")
    reclaimable(clock)
    write(grok, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")
    ref, sha = a_file(tmp_path, "settled.json", {"process": "exited"})
    settle = lambda **e: write(codex, "report_subagent_settled", project_id="p", subagent_id="codex/w1",
                               quiescent=True, report_ref=ref, report_sha256=sha, **e)
    assert code_of(settle) == "fence_required"
    assert code_of(lambda: settle(fence=2)) == "stale_fence"
    assert settle(fence=1)["status"] == "settled"
