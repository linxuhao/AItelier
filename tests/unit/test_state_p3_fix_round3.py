"""Multi-driver P3, fix round 3: regression tests of the CONFIRMED items of the
third Codex review (P3_REVIEW3_CODEX.md) whose mechanisms survive the slimming
(P3_SLIM.md): item 2, stale authorization at launch (MUST-FIX R3#2), and the
reclaim half of item 5 (reclaiming is for project members). The pool-offer
half of item 5 and items 1, 3, 4, 6, 7 and 8 belonged to removed mechanisms.
"""
from __future__ import annotations

import sqlite3

import pytest

from core import drivers
from core.state_claims import ClaimError
from core.state_database import StateDatabase
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    claim, clock, code_of, db, events, external, project, reclaimable, svc, write)
from tests.unit.test_state_p3_fix_round1 import claims_of, reserved_skillflow_attempt, terminal_report


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


# 5 ---------------------------------------------------------------------------
def test_5_only_members_reclaim(db, clock, tmp_path, monkeypatch):
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
        registry.set_membership("p", "late", "removed", 1, "left", actor="t")
        codex, grok, late, public = svc(db, "codex"), svc(db, "grok"), svc(db, "late"), svc(db, "stranger")
        first, second = external(codex, "a"), external(codex, "b", "rk-b")
        reclaimable(clock)
        assert code_of(lambda: write(public, "take_over_attempt", attempt_id=second["attempt_id"],
                                     expected_owner_fence=1, reason="mine")) == "not_project_member"
        assert code_of(lambda: write(late, "abandon_external_attempt", attempt_id=second["attempt_id"],
                                     expected_owner_fence=1, abandon_kind="unknown", reason="x")) == "not_project_member"
        taken = write(grok, "take_over_attempt", attempt_id=second["attempt_id"], expected_owner_fence=1, reason="gone")
        assert taken["owner_driver_id"] == "grok" and taken["break_glass"] is False
        # An admin that is not a member passes, audited as break glass.
        owner = svc(db, "owner-cli", admin=True)
        glass = write(owner, "take_over_attempt", attempt_id=first["attempt_id"], expected_owner_fence=1, reason="ops")
        assert glass["owner_driver_id"] == "owner-cli" and glass["break_glass"] is True
        assert events(db, "attempt_ownership_transferred")[-1]["break_glass"] is True
    finally:
        drivers._REGISTRIES.clear()
