"""The DSH surface must be able to say WHICH loop item failed.

`get_run_summary` is the one tool a driving agent has for "what broke". Inside a
fan-out it returned repeated identical rows —

    {"step": "t_impl", "status": "completed"}
    {"step": "t_impl", "status": "failed"}

— with nothing to tell the six tasks apart. That is the same blindness the run
page had before skillflow started stamping `loop_item`; the data now exists and
this surface is where a remote agent sees it.
"""
from unittest.mock import MagicMock

from core.run_driver import summarise_run


def _sf(steps):
    sf = MagicMock()
    sf.get_run.return_value = {"id": "r1", "status": "failed",
                               "graph_name": "gen_x", "project_id": "p1"}
    sf.get_steps.return_value = steps
    return sf


def _ws():
    ws = MagicMock()
    ws.get_final_path.return_value.exists.return_value = False
    return ws


def test_each_loop_instance_names_its_item():
    out = summarise_run(_sf([
        {"step_id": "plan", "status": "completed", "loop_item": None},
        {"step_id": "t_impl", "status": "completed", "loop_item": "alpha"},
        {"step_id": "t_impl", "status": "failed", "loop_item": "beta",
         "error": "boom"},
    ]), _ws(), MagicMock(), "r1")
    assert out["steps"] == [
        {"step": "plan", "status": "completed"},
        {"step": "t_impl", "status": "completed", "item": "alpha"},
        {"step": "t_impl", "status": "failed", "item": "beta", "error": "boom"},
    ]


def test_the_first_failure_says_which_item_it_was():
    """The single most useful line in the summary — without the item it names a
    step that ran six times and leaves the fix half guessing."""
    out = summarise_run(_sf([
        {"step_id": "t_impl", "status": "completed", "loop_item": "alpha"},
        {"step_id": "t_impl", "status": "failed", "loop_item": "beta",
         "error": "boom"},
    ]), _ws(), MagicMock(), "r1")
    assert out["first_failure"] == {"step": "t_impl", "item": "beta",
                                    "error": "boom"}


def test_a_step_outside_a_loop_carries_no_item_key():
    """Absent, not null: an `item: None` on every ordinary step is noise in a
    payload whose whole purpose is being small enough to read."""
    out = summarise_run(_sf([{"step_id": "review", "status": "completed",
                              "loop_item": None}]), _ws(), MagicMock(), "r1")
    assert out["steps"] == [{"step": "review", "status": "completed"}]


def test_a_skillflow_without_the_column_still_summarises():
    """The container installs skillflow from PyPI and can be a release behind. A
    KeyError here would take down the only tool that answers "what broke"."""
    out = summarise_run(_sf([{"step_id": "t_impl", "status": "failed",
                              "error": "boom"}]),   # no loop_item key at all
                        _ws(), MagicMock(), "r1")
    assert out["steps"] == [{"step": "t_impl", "status": "failed",
                             "error": "boom"}]
    assert out["first_failure"] == {"step": "t_impl", "error": "boom"}
