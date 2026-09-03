"""tasks_manifest_complete must answer to the call shape it is actually given.

It is wired as a step VALIDATION, and StepValidator hands a validation tool
`workspace_root` (set to the step's staging dir) plus the `files` list — the
same contract skillflow's own file_exists uses. Written first against a
`step_dir` kwarg that nothing passes, it read "." on every call and failed with
`tasks_manifest.json not found at tasks_manifest.json`, blocking the very step
it was added to protect. Live 2026-08-26: two consecutive step-3 attempts died
on it before the signature was corrected.
"""
import json

import pytest

from aitelier.tools.tasks_manifest_complete.impl import tasks_manifest_complete


@pytest.fixture
def plan(tmp_path):
    (tmp_path / "tasks").mkdir()
    for t in ("a", "b"):
        (tmp_path / "tasks" / f"{t}.json").write_text('{"id":"%s"}' % t,
                                                      encoding="utf-8")
    (tmp_path / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": [["a"], ["b"]]}), encoding="utf-8")
    return tmp_path


def test_the_validation_call_shape_works(plan):
    """StepValidator passes `files` positionally and `workspace_root` by name."""
    r = tasks_manifest_complete(["tasks_manifest.json"], workspace_root=str(plan))
    assert r["passed"] is True and r["tasks"] == 2


def test_the_tool_step_call_shape_still_works(plan):
    assert tasks_manifest_complete(out_dir=str(plan))["passed"] is True


def test_workspace_root_wins_when_several_are_given(plan, tmp_path_factory):
    empty = tmp_path_factory.mktemp("empty")
    r = tasks_manifest_complete(["tasks_manifest.json"],
                                workspace_root=str(plan), out_dir=str(empty))
    assert r["passed"] is True


def test_a_real_mismatch_still_fails(plan):
    (plan / "tasks" / "b.json").unlink()
    r = tasks_manifest_complete(["tasks_manifest.json"], workspace_root=str(plan))
    assert r["passed"] is False
    assert "b" in r["missing"]


# ── stale ids are caught HERE, before the owner's step-3 checkpoint ───────────

def test_a_manifest_of_only_dispatched_ids_fails_validation(plan, monkeypatch):
    import aitelier.tools.tasks_manifest_complete.impl as impl
    monkeypatch.setattr(impl, "_project_id_of", lambda root: "p")
    monkeypatch.setattr("aitelier.tools.task_budget_check.impl._completed_loop_items",
                        lambda pid: {"a", "b"})
    r = tasks_manifest_complete(workspace_root=str(plan))
    assert r["passed"] is False
    assert r["stale_ids"] == ["a", "b"]
    assert "NEW id" in r["error"]


def test_a_manifest_with_one_new_id_still_passes(plan, monkeypatch):
    import aitelier.tools.tasks_manifest_complete.impl as impl
    monkeypatch.setattr(impl, "_project_id_of", lambda root: "p")
    monkeypatch.setattr("aitelier.tools.task_budget_check.impl._completed_loop_items",
                        lambda pid: {"a"})
    assert tasks_manifest_complete(workspace_root=str(plan))["passed"] is True


def test_project_id_is_read_from_the_workspace_layout(tmp_path, monkeypatch):
    from aitelier.tools.tasks_manifest_complete.impl import _project_id_of
    monkeypatch.setattr("core.datadir.workspaces_dir", lambda: tmp_path / "ws")
    staging = tmp_path / "ws" / "jinyong-r3b" / "dpe_game" / "3.tmp"
    staging.mkdir(parents=True)
    assert _project_id_of(staging) == "jinyong-r3b"
    assert _project_id_of(tmp_path / "elsewhere") == ""
