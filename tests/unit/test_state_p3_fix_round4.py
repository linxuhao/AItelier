"""Multi-driver P3, fix round 4: one regression test per CONFIRMED item of the
fourth Codex review (P3_REVIEW4_CODEX.md). Each fails on 0fa1175f and passes
after the fix named in P3_REPORT.md "Fix round 4".
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from core import drivers
from core import state_enforcement as enforcement
from core.state_claims import ClaimError
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import (  # noqa: F401 - fixtures `clock`, `db`
    NODE, _insert_skillflow_attempt, a_file, claim, clock, code_of, db, events, external, project, read,
    reclaimable, register, svc, write)
from tests.unit.test_state_p3_fix_round2 import _no_inbox_adapter  # noqa: F401 - autouse reset

INFO = ("gather", "Project conversation", "run-1", "meta_conversation", 7)


def _enf(name):
    fn = getattr(enforcement, name, None)
    assert fn is not None, f"core.state_enforcement.{name} is missing (pre-fix tree)"
    return fn


def _bind_state_attempt(database, run_id="run-1"):
    """An enforced State project whose node b has a paused SkillFlow attempt
    bound to ``run_id`` and owned by codex (who holds the implement claim)."""
    owner = StateService(database, actor="driver:owner-cli", project_read_trusted=True, driver_id="owner-cli",
                         is_admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE}])
    write(owner, "set_multi_driver", project_id="p", multi_driver="on", expected_revision=0, reason="r")
    write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=1, reason="r")
    codex = StateService(database, actor="driver:codex", project_read_trusted=True, driver_id="codex")
    claim(codex, "b")
    with database.get_connection() as conn:
        contract = conn.execute("SELECT contract_hash FROM state_nodes WHERE project_id='p' AND node_key='b'").fetchone()[0]
        conn.execute("INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                     "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,run_id,"
                     "status,created_at,updated_at,execution_kind,owner_driver_id,owner_fence,lease_expires_at,"
                     "last_heartbeat_at) VALUES('attempt-sf','p','b',1,?,'{}','{}','rk','rh','w','sg-x',?,'paused','t','t',"
                     "'skillflow','codex',1,'2999-01-01T00:00:00.000000+00:00','2026-01-01T00:00:00.000000+00:00')",
                     (contract, run_id))
        conn.commit()
    return codex


def _decisions(database):
    with database.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM driver_checkpoint_decisions ORDER BY started_at")]


def _as_codex(monkeypatch):
    from api import state_graph_routers as routers
    monkeypatch.setattr(routers, "request_identity", lambda request: drivers.Identity("driver", "driver:codex", "codex"))
    monkeypatch.setattr(routers, "request_actor", lambda request: "driver:codex")


# 1 ---------------------------------------------------------------------------
class TestItem1RestCheckpointRoutes:
    def test_reject_route_reaches_the_engine_with_feedback_and_target(self, client, monkeypatch):
        import api.meta_routers as mr
        from tests.integration.test_meta_routers import _ensure_project
        _ensure_project(client, "cp-rej-proj")
        sf = MagicMock()
        sf.get_run.return_value = {"status": "paused"}
        monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
        monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid: INFO)
        import core.run_driver as run_driver
        seen = {}
        monkeypatch.setattr(run_driver, "checkpoint_reject_target",
                            lambda sf_, graph, step, run: seen.update(graph=graph, step=step, run=run) or None)
        resp = client.post("/api/meta/cp-rej-proj/checkpoint/reject",
                           json={"checkpoint": "gather", "feedback": "MUST use PostgreSQL."})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"
        assert seen == {"graph": "meta_conversation", "step": "gather", "run": "run-1"}, "the graph reached the target resolver"
        args, _ = sf.reject_checkpoint.call_args
        assert args[:3] == ("run-1", "gather", "MUST use PostgreSQL.")

    def test_approve_body_emits_the_resolved_notification_with_its_label(self, client, monkeypatch):
        import api.meta_routers as mr
        from tests.integration.test_meta_routers import _ensure_project
        from api.main import app
        from api.dependencies import get_db_manager
        _ensure_project(client, "cp-note-proj")
        database = app.dependency_overrides[get_db_manager]()
        sf = MagicMock()
        sf.get_run.return_value = {"status": "paused"}
        monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
        import core.scheduler as scheduler
        monkeypatch.setattr(scheduler, "wake_scheduler", lambda: None)
        pushed = []

        async def push_log(channel, payload):
            pushed.append((channel, json.loads(payload)))

        monkeypatch.setattr(mr.stream_manager, "push_log", push_log)
        from api.meta_routers import CheckpointApprovalRequest

        async def drive():
            out = mr._approve_checkpoint_body("cp-note-proj", CheckpointApprovalRequest(checkpoint="gather", feedback=""),
                                              "run-1", "gather", "Project conversation", database)
            await asyncio.sleep(0)
            return out

        out = asyncio.run(drive())
        assert out["status"] == "approved"
        assert [p["label"] for _, p in pushed] == ["Project conversation", "Project conversation"]
        assert {c for c, _ in pushed} == {"cp-note-proj", "__global__"}

    def test_routes_open_and_finish_the_state_decision_on_success_and_refusal(self, client, monkeypatch, clock):
        import api.meta_routers as mr
        from tests.integration.test_meta_routers import _ensure_project
        from api.main import app
        from api.dependencies import get_db_manager
        _ensure_project(client, "cp-dec-proj")
        database = app.dependency_overrides[get_db_manager]()
        _bind_state_attempt(database)
        _as_codex(monkeypatch)
        sf = MagicMock()
        sf.get_run.return_value = {"status": "paused"}
        monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
        monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid: INFO)
        import core.run_driver as run_driver
        monkeypatch.setattr(run_driver, "checkpoint_reject_target", lambda *a: None)
        resp = client.post("/api/meta/cp-dec-proj/checkpoint/reject", json={"checkpoint": "gather", "feedback": "redo"})
        assert resp.status_code == 200, resp.text
        decisions = _decisions(database)
        assert len(decisions) == 1 and decisions[0]["finished_at"] is not None and decisions[0]["driver_id"] == "codex"
        sf.reject_checkpoint.assert_called_once()
        # Engine refusal (completed run): the decision is still finished.
        sf.get_run.return_value = {"status": "completed"}
        resp = client.post("/api/meta/cp-dec-proj/checkpoint/approve", json={"checkpoint": "gather", "feedback": ""})
        assert resp.status_code == 400, resp.text
        decisions = _decisions(database)
        assert len(decisions) == 2 and all(d["finished_at"] is not None for d in decisions)
        sf.approve_checkpoint.assert_not_called()
        # Another driver is refused at the door, with nothing opened.
        from api import state_graph_routers as routers
        monkeypatch.setattr(routers, "request_identity", lambda request: drivers.Identity("driver", "driver:grok", "grok"))
        resp = client.post("/api/meta/cp-dec-proj/checkpoint/reject", json={"checkpoint": "gather", "feedback": "x"})
        assert resp.status_code == 409 and "not_attempt_owner" in resp.text
        assert len(_decisions(database)) == 2


# 2 ---------------------------------------------------------------------------
def test_2_same_node_exemption_matches_the_executor_and_never_an_unknown_abandon(db, clock, tmp_path):
    project(db, enforce=True)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    c1 = claim(codex, "a", workspace="linxuhaserver:/w#a")
    attempt = external(codex, "a", claim_id=c1["claim_id"], fence=1)
    register(codex, attempt, tmp_path, "w1", host="linxuhaserver", workspace="linxuhaserver:/other#w1")
    write(codex, "release_claim", project_id="p", claim_id=c1["claim_id"], fence=1, reason="oops")
    # Same node, DIFFERENT worker: a distinct executor on A's checkout, refused.
    assert code_of(lambda: claim(codex, "a", subagent="codex/w1", workspace="linxuhaserver:/w#a",
                                 request_key="for-w1")) == "workspace_in_use"
    # Same node, same executor (no worker, like the dispatching claim): allowed.
    assert claim(codex, "a", workspace="linxuhaserver:/w#a", request_key="again")["status"] == "live"
    # Unknown abandonment: not even the original owner re-claims that checkout on that node.
    second_node_claim = claim(codex, "b", workspace="linxuhaserver:/b#b")
    second = external(codex, "b", "rk-b", claim_id=second_node_claim["claim_id"], fence=second_node_claim["fence"])
    reclaimable(clock)
    write(grok, "abandon_external_attempt", attempt_id=second["attempt_id"], expected_owner_fence=1,
          abandon_kind="unknown", reason="nobody knows")
    assert code_of(lambda: claim(codex, "b", workspace="linxuhaserver:/b#retry", request_key="b-retry")) == "workspace_in_use"
    ref, sha = a_file(tmp_path, "quiet.json", {"workers": "exited"})
    write(codex, "report_external_attempt", attempt_id=second["attempt_id"], observation_id="quiet",
          expected_version=0, context_hash=second["context_hash"], status="running", report_ref=ref,
          report_sha256=sha, quiescent=True, fence=1)
    assert claim(codex, "b", workspace="linxuhaserver:/b#retry", request_key="b-retry2")["status"] == "live"


# 3 ---------------------------------------------------------------------------
def test_3_a_delayed_handler_is_fenced_before_the_engine_is_told(db, clock):
    project(db, enforce=True)
    codex, grok = svc(db, "codex"), svc(db, "grok")
    claim(codex, "b", workspace="h:/w#c")
    _insert_skillflow_attempt(db, "codex")
    database = StateDatabase(str(db))
    decision = _enf("checkpoint_controller")(database, "run-1", "codex", False, "driver:codex")
    assert _enf("assert_decision_open")(database, decision["decision_id"])["driver_id"] == "codex"
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE state_attempts SET lease_expires_at='2026-01-01T00:00:00.000000+00:00' WHERE attempt_id='attempt-sf'")
    conn.commit()
    conn.close()
    clock.advance(121)                                          # the handler was delayed past the decision
    reclaimable(clock)
    write(grok, "take_over_attempt", attempt_id="attempt-sf", expected_owner_fence=1, reason="gone")
    with pytest.raises(ClaimError) as refused:                  # ... and must not touch the engine now
        _enf("assert_decision_open")(database, decision["decision_id"])
    assert refused.value.code == "checkpoint_decision_expired"
    # A finished decision is closed too; an unknown id is refused.
    fresh = _enf("checkpoint_controller")(database, "run-1", "grok", False, "driver:grok")
    _enf("finish_checkpoint_decision")(database, fresh["decision_id"])
    with pytest.raises(ClaimError):
        _enf("assert_decision_open")(database, fresh["decision_id"])
    with pytest.raises(ClaimError):
        _enf("assert_decision_open")(database, "decision-nope")
    # The REST door maps the fence to 409 before the engine call.
    from api import meta_routers
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as http:
        meta_routers.assert_state_checkpoint_decision_open({"decision_id": decision["decision_id"]}, database)
    assert http.value.status_code == 409


# 4 ---------------------------------------------------------------------------
def test_4_claim_only_admin_handoff_records_break_glass(db, clock, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "secrets"))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / drivers.PEPPER_SECRET_NAME).write_text("p" * 48)
    monkeypatch.setenv(drivers.FEATURE_ENV, "on")
    drivers._REGISTRIES.clear()
    try:
        project(db)
        registry = drivers.registry_for(StateDatabase(str(db)))
        registry.register("codex", "Codex", actor="t")
        registry.set_membership("p", "codex", "member", 0, "join", actor="t")
        codex, owner = svc(db, "codex"), svc(db, "owner-cli", admin=True)
        held = claim(codex, "a", "plan", workspace="h:/w#c")
        offer = write(codex, "offer_handoff", project_id="p", request_key="c", expected_owner_fence=held["fence"],
                      claim_id=held["claim_id"], to_driver_id="owner-cli", package={"next_step": "plan a"})
        out = write(owner, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                    expected_owner_fence=held["fence"])
        assert out["break_glass"] is True and out["claim"]["driver_id"] == "owner-cli"
        assert events(db, "claim_transferred")[-1]["break_glass"] is True
        # A member accepting records break_glass=False.
        registry.register("grok", "Grok", actor="t")
        registry.set_membership("p", "grok", "member", 0, "join", actor="t")
        second = claim(codex, "b", "review")
        offer2 = write(codex, "offer_handoff", project_id="p", request_key="c2", expected_owner_fence=second["fence"],
                       claim_id=second["claim_id"], to_driver_id="grok", package={"next_step": "review b"})
        out2 = write(svc(db, "grok"), "accept_handoff", project_id="p", handoff_id=offer2["handoff_id"],
                     expected_owner_fence=second["fence"])
        assert out2["break_glass"] is False and events(db, "claim_transferred")[-1]["break_glass"] is False
    finally:
        drivers._REGISTRIES.clear()


# 5 (nit) -----------------------------------------------------------------------
def test_5_adapter_seam_documents_the_three_argument_hook():
    import inspect
    from core import driver_notices
    source = inspect.getsource(driver_notices)
    assert "deliver(conn, message, notice)" in source and "sender_driver_id" in source.split("_ADAPTER = ")[0][-2500:]
