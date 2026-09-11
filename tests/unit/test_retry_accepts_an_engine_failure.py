"""POST /retry must not refuse a run the engine says is already failed.

The route reads the RAW `runs.status` column on purpose: enrichment flips
failed -> running:<node> once reactivate has been called, and treating that as
"still running" would make a retried project un-retryable. That guard covers one
direction only.

The other direction had no guard and no test. The raw column is written by a
scheduler tick, and a project stops being ticked the moment its run fails — so a
run that dies on its FIRST tick leaves the column at "running:<node>" for good.
The endpoint whose whole purpose is recovering a failed run therefore refused
exactly the runs that failed earliest. Measured 2026-09-11: the
release.mainline-green r5 relay failed at isolation provisioning on tick one;
the engine said failed, the row said running:state_seed, /retry said "Only
failed projects can be retried", and nothing could move it.
"""
import pytest

from api import project_routers


def test_a_run_that_failed_on_its_first_tick_is_still_retryable(client, monkeypatch):
    """The row never caught up; the engine is the one telling the truth."""
    pid = "sg-first-tick-failure"
    seen = {}

    monkeypatch.setattr(project_routers, "check_write_owner", lambda user, p: None)

    class _DB:
        def get_project(self, project_id):
            # RAW column: stuck where the tick left it before the run failed.
            return {"id": project_id, "status": "running:state_seed",
                    "current_project_step": "state_seed", "owner_email": ""}
        def set_project_meta_state(self, *a, **k):
            seen["cleared"] = True
        def is_project_planning_complete(self, project_id):
            seen["reached_the_body"] = True
            raise RuntimeError("stop here — the gate is what this test is about")

    import api.dependencies as deps
    monkeypatch.setattr(deps, "enrich_project_status",
                        lambda p: dict(p, status="failed:Tool step 'state_seed' crashed"))

    with pytest.raises(RuntimeError, match="stop here"):
        project_routers.retry_project(pid, user=None, db=_DB())

    assert seen.get("reached_the_body"), (
        "the gate rejected a project the engine reports as failed — the same "
        "refusal that left the r5 relay unrecoverable")


def test_a_project_that_is_genuinely_running_is_still_refused(monkeypatch):
    """The gate must keep its job: this test fails if the fix removed it."""
    from fastapi import HTTPException

    monkeypatch.setattr(project_routers, "check_write_owner", lambda user, p: None)

    class _DB:
        def get_project(self, project_id):
            return {"id": project_id, "status": "running:t_impl", "owner_email": ""}
        def set_project_meta_state(self, *a, **k):
            raise AssertionError("a running project must not get past the gate")
        def is_project_planning_complete(self, project_id):
            raise AssertionError("a running project must not get past the gate")

    import api.dependencies as deps
    monkeypatch.setattr(deps, "enrich_project_status",
                        lambda p: dict(p, status="running:t_impl"))

    with pytest.raises(HTTPException) as exc:
        project_routers.retry_project("sg-busy", user=None, db=_DB())
    assert exc.value.status_code == 400
