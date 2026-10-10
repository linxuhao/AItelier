"""Multi-driver P3, fix round 4: the regression test of item 1 of the fourth
Codex review (P3_REVIEW4_CODEX.md) that survives the slimming (P3_SLIM.md):
the REST reject route reaches the engine with its feedback and target. The
decision-row, same-node-exemption, break-glass-handoff and adapter items
belonged to removed mechanisms; the single owner check at the route entry is
tested in test_state_p3_checkpoint_entry.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

INFO = ("gather", "Project conversation", "run-1", "meta_conversation", 7)


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
