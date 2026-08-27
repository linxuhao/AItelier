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
        self.meta_state = None

    def get_project(self, pid):
        return {"project_id": pid, "completed_project_steps": "[]"}

    def set_project_meta_state(self, pid, state):
        self.meta_state = state

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


def test_refusal_rearms_the_drafting_gate(tmp_path, monkeypatch):
    """Returning an error is not enough — nothing downstream reads it.

    submit_project clears meta_state BEFORE calling us, and the scheduler starts
    a DPE run for any 'planning' project that isn't 'drafting'. So a refusal that
    only returns a message still lets the brief-less run be born on the next tick.
    """
    _patch_skillflow(monkeypatch, tmp_path)  # no meta_conversation/finalize dir
    db = _FakeDB()
    r = ps.seed_and_trigger(db, None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "error"
    assert db.meta_state == "drafting"
    assert db.brief is None and db.steps is None  # nothing was seeded


def test_guard_internal_error_fails_closed(tmp_path, monkeypatch):
    """A guard that can't run must refuse, not fall through to 'allow'."""
    def _boom():
        raise RuntimeError("skillflow singleton unavailable")

    import api.dependencies as dep
    monkeypatch.setattr(dep, "get_skillflow", _boom)
    monkeypatch.setattr("core.scheduler.wake_scheduler", lambda *a, **k: None)
    db = _FakeDB()
    r = ps.seed_and_trigger(db, None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "error"
    assert "guard could not run" in r["message"]
    assert db.meta_state == "drafting"


def test_gate_holds_through_the_submit_endpoint(client):
    """End-to-end: POST /projects then POST /projects/submit with no meta run.

    This is the exact live sequence. Before the fix the project stayed eligible
    (`planning` + meta_state NULL), so the scheduler created a dpe_default run
    that could never satisfy its required `finalize` context.
    """
    from api.dependencies import get_db_manager
    from api.main import app

    db = app.dependency_overrides[get_db_manager]()
    assert client.post("/api/projects", json={"project_id": "p-nometa",
                                              "name": "no meta"}).status_code == 201
    r = client.post("/api/projects/submit", json={
        "project_id": "p-nometa",
        "brief": {"project_name": "no meta", "user_stories": ["as a user..."]},
    })
    assert r.json()["status"] == "error"
    assert db.get_project("p-nometa")["meta_state"] == "drafting"
    assert db.get_next_active_project() is None  # scheduler will not start it


def test_a_failed_project_is_refused_with_directions(tmp_path, monkeypatch):
    """A failed project must be REFUSED with directions, not silently accepted:
    the scheduler leaves a failed run dormant (NB-5; only /retry reactivates),
    so accepting the brief produced "submitted" + a no_run tick forever — and
    quietly resuming a half-dead run under a NEW brief would serve old goals.
    `paused:*` stays untouched: that is a live run at a checkpoint."""
    fin = tmp_path / "meta_conversation" / "finalize"
    fin.mkdir(parents=True)
    (fin / "step1_goals.json").write_text('{"goals": ["x"], "user_stories": ["As a..."]}')
    _patch_skillflow(monkeypatch, tmp_path)

    class _DB(_FakeDB):
        def __init__(self, status):
            super().__init__()
            self._status = status
            self.updates = {}

        def get_project(self, pid):
            return {"project_id": pid, "completed_project_steps": "[]",
                    "status": self._status}

        def update_project(self, pid, **kw):
            self.updates.update(kw)

    db = _DB("failed:Cycle limit exceeded — review_verdict…")
    r = ps.seed_and_trigger(db, None, "p1", {"user_stories": ["x"]})
    # NOT silently "submitted": the scheduler's NB-5 leaves a failed run
    # dormant (only POST /retry reactivates), so the first fix's status flip
    # produced "submitted" + no_run on every tick. Refuse with directions.
    assert r["status"] == "error"
    assert "retry" in r["message"]
    assert "status" not in db.updates
    # …and BEFORE any mutation: the first refusal fired after the new brief
    # was cached and completed_project_steps reset, so following its own
    # /retry directions resumed the old run under half-clobbered state.
    assert db.brief is None
    assert db.meta_state is None
    assert "completed_project_steps" not in db.updates

    # The COMMON failure class: a run that failed in task_loop / step 5 has
    # all three planning steps synced complete — the refusal must beat the
    # already_planned early-return, or resubmit reads as success-shaped
    # "already_planned" and the /retry directions never appear.
    class _PlannedDB(_DB):
        def get_project(self, pid):
            return {"project_id": pid,
                    "completed_project_steps": '["1", "2", "3"]',
                    "status": self._status}

    db = _PlannedDB("failed:cycle limit — vision gate blind")
    r = ps.seed_and_trigger(db, None, "p1", {"user_stories": ["x"]})
    assert r["status"] == "error" and "retry" in r["message"]

    db = _DB("paused:checkpoint")
    ps.seed_and_trigger(db, None, "p1", {"user_stories": ["x"]})
    assert "status" not in db.updates          # live checkpoint left alone
