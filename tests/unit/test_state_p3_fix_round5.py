"""Multi-driver P3, fix round 5: regression tests for the two code items of the
fifth Codex review (P3_REVIEW5_CODEX.md). Each fails on e46ba98e and passes
after the fix named in P3_REPORT.md "Fix round 5". Both drive the ACTUAL
doors (REST route through TestClient, MCP tool callable), not helpers.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core import drivers
from tests.unit.test_state_p3_enforced_claims import clock  # noqa: F401 - fixture
from tests.unit.test_state_p3_fix_round2 import _no_inbox_adapter  # noqa: F401 - autouse reset
from tests.unit.test_state_p3_fix_round4 import INFO, _as_codex, _bind_state_attempt, _decisions


def _delayed_get_run(clock, status):
    """An engine read that takes longer than the whole decision lifetime: the
    handler was suspended between opening the decision and the mutation."""
    def get_run(run_id):
        clock.advance(121)
        return {"status": status, "id": run_id, "project_id": "cp-delay-proj"}
    return get_run


# 1 ---------------------------------------------------------------------------
class TestItem1FailedRunRescueIsFenced:
    def test_rest_failed_run_approval_never_reactivates_after_the_decision_expired(self, client, monkeypatch, clock):
        import api.meta_routers as mr
        from tests.integration.test_meta_routers import _ensure_project
        from api.main import app
        from api.dependencies import get_db_manager
        _ensure_project(client, "cp-delay-proj")
        database = app.dependency_overrides[get_db_manager]()
        _bind_state_attempt(database)
        _as_codex(monkeypatch)
        sf = MagicMock()
        sf.get_run.side_effect = _delayed_get_run(clock, "failed")
        monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
        monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid: INFO)
        resp = client.post("/api/meta/cp-delay-proj/checkpoint/approve", json={"checkpoint": "gather", "feedback": ""})
        assert resp.status_code == 409 and "checkpoint_decision_expired" in resp.text, resp.text
        sf.reactivate_run.assert_not_called()
        sf.resume_run.assert_not_called()
        decisions = _decisions(database)
        assert len(decisions) == 1 and decisions[0]["finished_at"] is not None, "the door still finishes it"
        # The same handler, not delayed: the rescue path runs both mutations.
        sf.get_run.side_effect = None
        sf.get_run.return_value = {"status": "failed"}
        resp = client.post("/api/meta/cp-delay-proj/checkpoint/approve", json={"checkpoint": "gather", "feedback": ""})
        assert resp.status_code == 200, resp.text
        sf.reactivate_run.assert_called_once_with("run-1")
        sf.resume_run.assert_called_once_with("run-1")

    def test_run_scoped_rest_delegate_is_fenced_too(self, client, monkeypatch, clock):
        import api.meta_routers as mr
        import api.run_routers as rr
        from tests.integration.test_meta_routers import _ensure_project
        from api.main import app
        from api.dependencies import get_db_manager
        _ensure_project(client, "cp-delay-proj")
        database = app.dependency_overrides[get_db_manager]()
        _bind_state_attempt(database)
        _as_codex(monkeypatch)
        sf = MagicMock()
        sf.get_run.side_effect = _delayed_get_run(clock, "failed")
        monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
        monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid: INFO)
        monkeypatch.setattr(rr, "_run_to_project_id", lambda run_id: "cp-delay-proj")
        resp = client.post("/api/runs/run-1/checkpoint/approve", json={"checkpoint": "gather", "feedback": ""})
        assert resp.status_code == 409 and "checkpoint_decision_expired" in resp.text, resp.text
        sf.reactivate_run.assert_not_called()
        sf.resume_run.assert_not_called()


# 2 ---------------------------------------------------------------------------
class TestItem2McpRejectResolvesTargetBeforeTheFence:
    @staticmethod
    def _tool(monkeypatch, sf, database):
        from api import mcp_router
        import api.dependencies as deps
        from core import trace_reader
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        monkeypatch.setattr(deps, "get_db_manager", lambda: database)
        monkeypatch.setattr(trace_reader, "resolve_run_ref",
                            lambda sf_, ref: ({"id": "run-1", "project_id": "p", "status": sf.get_run("run-1")["status"],
                                               "graph_name": "meta_conversation"}, {}))
        captured = {}

        def tool(name, kind, description):
            def deco(fn):
                captured[name] = fn
                return fn
            return deco
        mcp_router._register_run_tools(tool)
        return captured["answer_checkpoint"]

    def test_target_resolution_precedes_the_fence_and_an_expired_decision_never_mutates(self, tmp_path, monkeypatch, clock):
        from core.db_manager import DBManager
        import api.meta_routers as mr
        import core.run_driver as run_driver
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
        database = DBManager(str(tmp_path / "mcp.sqlite3"))
        _bind_state_attempt(database)
        _as_codex(monkeypatch)
        sf = MagicMock()
        sf.get_run.return_value = {"status": "paused", "id": "run-1", "project_id": "p"}
        answer = self._tool(monkeypatch, sf, database)
        monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid, rid=None: INFO)
        order = []
        monkeypatch.setattr(run_driver, "checkpoint_reject_target",
                            lambda *a: order.append("target") or "redirect")
        real_fence = mr.assert_state_checkpoint_decision_open
        monkeypatch.setattr(mr, "assert_state_checkpoint_decision_open",
                            lambda controller, db: order.append("fence") or real_fence(controller, db))
        out = answer("run-1", "reject", "redo it")
        assert "error" not in out, out
        assert order == ["target", "fence"], order
        sf.reject_checkpoint.assert_called_once_with("run-1", "gather", "redo it", redirect_to="redirect")
        assert all(d["finished_at"] is not None for d in _decisions(database))
        # A handler delayed past the decision (the target resolution took 121 s) never mutates.
        sf.reset_mock()
        sf.get_run.return_value = {"status": "paused", "id": "run-1", "project_id": "p"}
        monkeypatch.setattr(run_driver, "checkpoint_reject_target",
                            lambda *a: clock.advance(121) or "redirect")
        out = answer("run-1", "reject", "redo it again")
        assert "checkpoint_decision_expired" in out.get("error", ""), out
        sf.reject_checkpoint.assert_not_called()
