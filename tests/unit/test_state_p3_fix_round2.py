"""Multi-driver P3, fix round 2: regression tests of the CONFIRMED items of the
second Codex review (P3_REVIEW2_CODEX.md) whose mechanisms survive the
slimming (P3_SLIM.md): R1 replays are judged on the named claim, N1 only a
verified quiescence report settles an abandoned owner (MUST-FIX R2#1), N5 an
admin is an ordinary reporter where claims are not enforced (MUST-FIX R2#5).
"""
from __future__ import annotations

import sqlite3

import pytest

from core.state_claims import ClaimError
from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    a_file, claim, clock, code_of, db, events, external, project, reclaimable, report, svc, write)
from tests.unit.test_state_p3_fix_round1 import claims_of, reserved_skillflow_attempt


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
        # Round 3: the replacement is bound only when NAMED (no silent rebind).
        with pytest.raises(ClaimError) as refused:
            codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex")
        assert refused.value.code == "claim_required"
        assert codex.attempts.claim_launch("attempt-res", "codex", False, "driver:codex",
                                           claim_id=c2["claim_id"], fence=c2["fence"]) is True
        assert claims_of(db, claim_id=c2["claim_id"])[0]["attempt_id"] == "attempt-res"
        assert events(db, "attempt_launching")[-1]["claim_id"] == c2["claim_id"]


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


# N5 --------------------------------------------------------------------------
@pytest.mark.parametrize("enforce", [False, True])
def test_n5_admin_reporting_bypass_exists_only_where_claims_are_enforced(db, clock, enforce):
    # Slim (P3_SLIM.md): break_glass is an audit flag on dispatch/launch/reclaim
    # writes only; there is no admin reporting path at all, so an admin is an
    # ordinary reporter with enforcement off (P1, this regression) and on.
    project(db, enforce=enforce)
    reporter = svc(db, "codex")
    held = claim(reporter, "a") if enforce else None
    attempt = external(reporter, "a", **({"claim_id": held["claim_id"], "fence": 1} if enforce else {}))
    for admin in (svc(db, "owner-cli", admin=True),
                  StateService(StateDatabase(str(db)), actor="owner:o@example.com", project_read_trusted=True,
                               is_admin=True)):
        with pytest.raises(StateConflict) as refused:         # P1: only the recorded reporter continues it
            report(admin, attempt, "running")
        assert getattr(refused.value, "code", None) != "break_glass" and "break_glass" not in str(refused.value)
        assert events(db, "external_attempt_observed") == []
    assert report(reporter, attempt, "running", fence=1 if enforce else None)["observation"]["version"] == 1
    assert "break_glass" not in events(db, "external_attempt_observed")[-1]
