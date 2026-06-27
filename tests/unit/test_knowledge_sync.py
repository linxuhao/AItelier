"""Unit tests for the knowledge_sync tool.

Covers: distillation from design + verify artifacts, the git-ignore wiring
(.git/info/exclude, idempotent, file stays untracked), per-project dedup so the
goal-loop's repeated passes don't pile up, newest-N capping, and the
no-artifacts / non-repo graceful no-ops.
"""

import json
import subprocess
from pathlib import Path

from aitelier.tools.knowledge_sync.impl import knowledge_sync, _MAX_SECTIONS

GRAPH = "dpe_default_v2"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "README.md").write_text("hi", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return repo


def _make_workspace(tmp_path, *, design=True, report=None) -> Path:
    """Build a workspace with step 2 design + step 5 verify report."""
    ws = tmp_path / "ws"
    gdir = ws / GRAPH
    if design:
        d2 = gdir / "2"
        d2.mkdir(parents=True, exist_ok=True)
        (d2 / "step2_design.md").write_text(
            "# Design\n\n## Overview\n\nA tiny app.\n\n## Modules\n\nfoo, bar\n",
            encoding="utf-8")
    if report is not None:
        d5 = gdir / "5" / "final"
        d5.mkdir(parents=True, exist_ok=True)
        (d5 / "verify_report.json").write_text(json.dumps(report), encoding="utf-8")
    return ws


def _call(repo, ws, pid):
    return knowledge_sync(
        project_root=str(repo), workspace_root=str(ws),
        config_name=GRAPH, project_id=pid,
        out_dir=str(ws / GRAPH / "5_knowledge"))


def test_writes_and_distills(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={
        "all_goals_met": True, "ready_for_deploy": True,
        "verified_subtasks": ["a", "b"], "issues": ["watch out for X"]})
    res = _call(repo, ws, "proj1")
    assert res["written"] is True
    text = (repo / ".aitelier" / "knowledge.md").read_text()
    assert "goals_met=true" in text
    assert "ready_for_deploy=true" in text
    assert "Verified working:** a, b" in text
    assert "watch out for X" in text          # known issue surfaced
    assert "A tiny app." in text              # design overview distilled
    assert "Sections:" in text                # design headers listed


def test_file_is_git_ignored(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": False})
    _call(repo, ws, "proj1")
    # .git/info/exclude carries the pattern
    excl = (repo / ".git" / "info" / "exclude").read_text()
    assert ".aitelier/" in excl
    # the written file does NOT show up as untracked
    st = _git(repo, "status", "--short").stdout
    assert ".aitelier" not in st
    # and git check-ignore confirms it
    ci = _git(repo, "check-ignore", ".aitelier/knowledge.md")
    assert ci.returncode == 0


def test_exclude_is_idempotent(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": True})
    _call(repo, ws, "proj1")
    _call(repo, ws, "proj1")
    excl = (repo / ".git" / "info" / "exclude").read_text()
    assert excl.count(".aitelier/") == 1


def test_same_project_section_is_replaced(tmp_path):
    """Goal-loop re-runs must rewrite, not duplicate, this project's section."""
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": False})
    _call(repo, ws, "proj1")
    ws2 = _make_workspace(tmp_path, report={"all_goals_met": True})
    _call(repo, ws2, "proj1")
    text = (repo / ".aitelier" / "knowledge.md").read_text()
    assert text.count("pid=proj1") == 1
    assert "goals_met=true" in text           # latest pass kept
    assert "goals_met=false" not in text      # stale pass dropped


def test_multiple_projects_accumulate_newest_first(tmp_path):
    repo = _make_repo(tmp_path)
    for n in range(_MAX_SECTIONS + 2):
        ws = _make_workspace(tmp_path, report={"all_goals_met": True})
        _call(repo, ws, f"proj{n}")
    text = (repo / ".aitelier" / "knowledge.md").read_text()
    # capped at _MAX_SECTIONS, newest (highest n) first
    assert text.count("<!-- aitelier:run") == _MAX_SECTIONS
    newest = f"proj{_MAX_SECTIONS + 1}"
    oldest_dropped = "proj0"
    assert newest in text
    assert oldest_dropped not in text
    assert text.index(newest) < text.index(f"proj{_MAX_SECTIONS}")


def test_no_artifacts_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, design=False, report=None)
    res = _call(repo, ws, "proj1")
    assert res["written"] is False
    assert not (repo / ".aitelier").exists()


def test_missing_repo_is_graceful(tmp_path):
    ws = _make_workspace(tmp_path, report={"all_goals_met": True})
    res = knowledge_sync(project_root=str(tmp_path / "nope"),
                         workspace_root=str(ws), config_name=GRAPH,
                         project_id="p")
    assert res["written"] is False


def test_report_only_no_design(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, design=False,
                         report={"all_goals_met": True, "issues": ["x"]})
    res = _call(repo, ws, "proj1")
    assert res["written"] is True
    text = (repo / ".aitelier" / "knowledge.md").read_text()
    assert "Architecture" not in text         # no design → section omitted
    assert "x" in text


def test_loadable_via_tool_loader():
    """knowledge_sync is discoverable by AItelier's ToolLoader."""
    from api.dependencies import get_tool_loader
    loader = get_tool_loader()
    schema = loader.load_schema("knowledge_sync")
    assert schema["name"] == "knowledge_sync"
    assert callable(loader.load_fn("knowledge_sync"))
