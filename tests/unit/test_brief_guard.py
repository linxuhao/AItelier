"""Regression test: the host-side brief guard in seed_and_trigger.

A DPE build must not start without a finalized brief (meta_conversation's
step1_goals.json) — otherwise the researcher runs brief-less and hallucinates.
"""

import types

import core.project_submit as ps


class _FakeDB:
    def __init__(self):
        self.brief = None
        self.steps = None

    def get_project(self, pid):
        return {"project_id": pid, "completed_project_steps": "[]"}

    def set_project_meta_state(self, pid, state):
        pass

    def set_project_brief(self, pid, md):
        self.brief = md

    def update_project(self, pid, **kw):
        self.steps = kw.get("completed_project_steps")


def _patch_skillflow(monkeypatch, ws_root):
    ws = types.SimpleNamespace(get_project_path=lambda pid: ws_root)
    sf = types.SimpleNamespace(_workspace=ws)
    import api.dependencies as dep
    monkeypatch.setattr(dep, "get_skillflow", lambda: sf)
    monkeypatch.setattr("core.scheduler.wake_scheduler", lambda *a, **k: None)


def test_refuses_without_finalized_brief(tmp_path, monkeypatch):
    _patch_skillflow(monkeypatch, tmp_path)  # no meta_conversation/finalize dir
    r = ps.seed_and_trigger(_FakeDB(), None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "error"
    assert "finalized brief" in r["message"]


def test_allows_with_finalized_brief(tmp_path, monkeypatch):
    fin = tmp_path / "meta_conversation" / "finalize"
    fin.mkdir(parents=True)
    (fin / "step1_goals.json").write_text('{"goals": ["x"], "user_stories": ["As a..."]}')
    _patch_skillflow(monkeypatch, tmp_path)
    r = ps.seed_and_trigger(_FakeDB(), None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "submitted"


def test_empty_goals_file_is_refused(tmp_path, monkeypatch):
    fin = tmp_path / "meta_conversation" / "finalize"
    fin.mkdir(parents=True)
    (fin / "step1_goals.json").write_text("   ")  # present but empty
    _patch_skillflow(monkeypatch, tmp_path)
    r = ps.seed_and_trigger(_FakeDB(), None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "error"
