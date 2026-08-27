"""The poller must not start a build for a project that has no brief.

`start_config_run` has refused this since jinyong-usable (2026-08-23): a run
whose required cross-config input does not exist has no first move, and handing
back a run id for it reports a build that cannot begin. The POLLER's own
creation path never grew the same guard — it checked `meta_state == 'drafting'`
instead, a flag only the meta agent's create_project path sets.

Live 2026-08-27, jinyong-creation: created through `POST /api/projects` (a
public endpoint, config_name=dpe_game), so the flag was never set. One second
later the poller created and started a run that died on its first claim —
"Required context source resolved to no content: finalize" — and then sat
dormant, because a failed run is deliberately never auto-reactivated.
"""
from unittest.mock import MagicMock, patch

import pytest

from core import scheduler


def _project(**over):
    p = {"project_id": "pid_x", "config_name": "dpe_game", "meta_state": None,
         "brief": "", "status": "planning"}
    p.update(over)
    return p


def _tick(project, missing, existing_run=None):
    sf = MagicMock()
    sf._conn.execute.return_value.fetchone.return_value = existing_run
    sf.get_or_create_run.return_value = "run_new"
    sf.get_run.return_value = {"status": "pending"}
    db = MagicMock()
    db.get_project.return_value = project
    logged = []
    with patch.object(scheduler, "get_skillflow", return_value=sf), \
         patch.object(scheduler, "db", db), \
         patch("core.run_launcher.missing_cross_config_inputs", return_value=missing), \
         patch.object(scheduler, "tick_log", lambda pid, outcome, **kw: logged.append((outcome, kw))):
        run_id = scheduler._get_or_create_skillflow_run("pid_x")
    return run_id, sf, logged


_MISSING = [{"config": "meta_conversation", "step": "finalize",
             "output": "step1_goals.json", "reader": "1"}]


def test_no_run_is_created_while_the_brief_is_missing():
    run_id, sf, _ = _tick(_project(), _MISSING)
    assert run_id is None
    sf.get_or_create_run.assert_not_called()


def test_the_gated_project_is_logged_not_silent():
    # Returning None used to be completely silent: no run, no tick line, a
    # project that simply never moves.
    _, _, logged = _tick(_project(), _MISSING)
    assert logged, "a gated tick must leave a line in the tick log"
    outcome, detail = logged[0]
    assert outcome == "awaiting_brief"
    assert "step1_goals.json" in detail["reason"]
    assert "meta_conversation" in detail["reason"]


def test_the_drafting_flag_still_gates_and_is_logged():
    _, sf, logged = _tick(_project(meta_state="drafting"), [])
    sf.get_or_create_run.assert_not_called()
    assert logged[0][0] == "awaiting_brief"
    assert "drafting" in logged[0][1]["reason"]


def test_a_project_with_its_brief_in_place_starts_normally():
    run_id, sf, logged = _tick(_project(), [])
    assert run_id == "run_new"
    sf.get_or_create_run.assert_called_once()
    assert logged == []


def test_a_broken_guard_never_blocks_a_healthy_project():
    # The check consults the graph and the filesystem. If it throws, the honest
    # failure mode is to let the project run — the guard exists to catch a known
    # bad state, not to become a new way for everything to stop.
    sf = MagicMock()
    sf._conn.execute.return_value.fetchone.return_value = None
    sf.get_or_create_run.return_value = "run_new"
    sf.get_run.return_value = {"status": "pending"}
    db = MagicMock()
    db.get_project.return_value = _project()
    with patch.object(scheduler, "get_skillflow", return_value=sf), \
         patch.object(scheduler, "db", db), \
         patch("core.run_launcher.missing_cross_config_inputs",
               side_effect=RuntimeError("resolver exploded")), \
         patch.object(scheduler, "tick_log", lambda *a, **k: None):
        assert scheduler._get_or_create_skillflow_run("pid_x") == "run_new"


@pytest.mark.parametrize("status", ["running", "paused"])
def test_an_existing_live_run_is_returned_without_the_guard(status):
    # The guard is about CREATING a run. A live run has already begun and its
    # inputs are whatever they are — re-gating it here would strand it.
    run_id, sf, _ = _tick(_project(), _MISSING, existing_run=("run_live", status))
    assert run_id == "run_live"
