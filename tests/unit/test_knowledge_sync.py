"""Unit tests for the knowledge_sync tool.

Covers: distillation from design + verify artifacts, the git-ignore wiring
(.git/info/exclude, idempotent, file stays untracked), per-project dedup so the
goal-loop's repeated passes don't pile up, newest-N capping, and the
no-artifacts / non-repo graceful no-ops.
"""

import json
import subprocess
from pathlib import Path

from aitelier.tools.knowledge_sync import impl as knowledge_impl
from aitelier.tools.knowledge_sync.impl import _MAX_SECTIONS, knowledge_sync

GRAPH = "dpe_default_v2"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "knowledge@test.invalid")
    _git(repo, "config", "user.name", "Knowledge test")
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


def test_locates_artifacts_via_out_dir_only(tmp_path):
    """Production path: skillflow injects out_dir ($STEP_DIR) but NOT workspace_root
    for tool steps, so the tool must work with workspace_root='' and only out_dir."""
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": True,
                                           "verified_subtasks": ["a"]})
    res = knowledge_sync(
        project_root=str(repo), workspace_root="", config_name=GRAPH,
        project_id="proj1", out_dir=str(ws / GRAPH / "5_knowledge"))
    assert res["written"] is True
    assert "goals_met=true" in (repo / ".aitelier" / "knowledge.md").read_text()


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


def test_tracked_generated_knowledge_is_migrated_exactly(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": False})
    assert _call(repo, ws, "legacy")["written"] is True
    original = (repo / ".aitelier" / "knowledge.md").read_bytes()
    assert _git(repo, "add", "-f", "--", ".aitelier/knowledge.md").returncode == 0
    assert _git(repo, "commit", "-qm", "legacy tracked knowledge").returncode == 0
    legacy_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Reproduce an older knowledge_sync having rewritten the tracked file.
    (repo / ".aitelier" / "knowledge.md").write_bytes(
        original.replace(b"goals_met=false", b"goals_met=true"))
    assert _git(repo, "status", "--porcelain").stdout == \
        " M .aitelier/knowledge.md\n"

    (ws / GRAPH / "5" / "final" / "verify_report.json").write_text(
        json.dumps({"all_goals_met": True}), encoding="utf-8")
    res = _call(repo, ws, "legacy")

    assert res["written"] is True and res["migration"] == "untracked_generated_knowledge"
    assert _git(repo, "ls-files", "--error-unmatch", ".aitelier/knowledge.md").returncode != 0
    assert _git(repo, "check-ignore", ".aitelier/knowledge.md").returncode == 0
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout == ""
    assert _git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD").stdout == \
        "D\t.aitelier/knowledge.md\n"
    assert _git(repo, "rev-parse", "HEAD^").stdout.strip() == legacy_head
    updated = (repo / ".aitelier" / "knowledge.md").read_bytes()
    assert updated != original and b"goals_met=true" in updated


def test_tracked_unowned_knowledge_fails_closed_with_recovery(tmp_path):
    repo = _make_repo(tmp_path)
    path = repo / ".aitelier" / "knowledge.md"
    path.parent.mkdir()
    path.write_text("user-owned notes\n", encoding="utf-8")
    _git(repo, "add", ".aitelier/knowledge.md")
    _git(repo, "commit", "-qm", "user tracked file")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before = path.read_bytes()

    res = _call(repo, _make_workspace(tmp_path), "legacy")

    assert res["written"] is False and "error" in res
    assert res["tracked_path"] == ".aitelier/knowledge.md"
    assert res["owner"] == "unproven"
    assert "git rm --cached -- .aitelier/knowledge.md" in res["recovery_action"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert path.read_bytes() == before
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout == ""


def test_tracked_generated_knowledge_does_not_adopt_unrelated_changes(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path)
    assert _call(repo, ws, "legacy")["written"] is True
    _git(repo, "add", "-f", "--", ".aitelier/knowledge.md")
    _git(repo, "commit", "-qm", "legacy tracked knowledge")
    unrelated = repo / "README.md"
    unrelated.write_text("owner change", encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_knowledge = (repo / ".aitelier" / "knowledge.md").read_bytes()

    res = _call(repo, ws, "legacy")

    assert res["written"] is False and "error" in res
    assert res["tracked_path"] == ".aitelier/knowledge.md"
    assert res["owner"] == "aitelier-generated"
    assert "make the worktree clean" in res["recovery_action"].lower()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert unrelated.read_text(encoding="utf-8") == "owner change"
    assert (repo / ".aitelier" / "knowledge.md").read_bytes() == before_knowledge
    assert _git(repo, "diff", "--name-only").stdout == "README.md\n"


def test_untracked_common_dir_failure_refuses_before_write(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_index = (repo / ".git" / "index").read_bytes()
    real_git = knowledge_impl._git

    def unavailable(repo_arg, *args, **kwargs):
        if args == ("rev-parse", "--git-common-dir"):
            return subprocess.CompletedProcess(["git", *args], 128, "", "injected")
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", unavailable)
    res = _call(repo, ws, "untracked")

    assert res["written"] is False and res["owner"] == "untracked"
    assert "git-common-dir" in res["recovery_action"]
    assert not (repo / ".aitelier" / "knowledge.md").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_untracked_final_check_ignore_failure_restores_absence(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path)
    real_git = knowledge_impl._git

    def unavailable(repo_arg, *args, **kwargs):
        if args == ("check-ignore", "--quiet", "--", ".aitelier/knowledge.md"):
            return subprocess.CompletedProcess(["git", *args], 128, "", "injected")
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", unavailable)
    res = _call(repo, ws, "untracked")

    assert res["written"] is False and res["owner"] == "untracked"
    assert "knowledge bytes were restored" in res["error"]
    assert not (repo / ".aitelier" / "knowledge.md").exists()
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_untracked_final_status_failure_restores_existing_bytes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    first_ws = _make_workspace(tmp_path, report={"all_goals_met": False})
    assert _call(repo, first_ws, "first")["written"] is True
    path = repo / ".aitelier" / "knowledge.md"
    before = path.read_bytes()
    second_ws = tmp_path / "second-ws"
    graph = second_ws / GRAPH / "2"
    graph.mkdir(parents=True)
    (graph / "step2_design.md").write_text(
        "# Design\n\n## Overview\n\nSecond result.\n", encoding="utf-8")
    real_git = knowledge_impl._git
    calls = 0

    def unavailable(repo_arg, *args, **kwargs):
        nonlocal calls
        target = ("status", "--porcelain=v1", "--untracked-files=all",
                  "--", ".aitelier/knowledge.md")
        if args == target:
            calls += 1
            if calls == 2:
                return subprocess.CompletedProcess(["git", *args], 128, "", "injected")
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", unavailable)
    res = _call(repo, second_ws, "second")

    assert res["written"] is False and calls == 2
    assert "knowledge bytes were restored" in res["error"]
    assert path.read_bytes() == before
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def _tracked_generated_fixture(tmp_path):
    repo = _make_repo(tmp_path)
    ws = _make_workspace(tmp_path, report={"all_goals_met": False})
    assert _call(repo, ws, "legacy")["written"] is True
    path = repo / ".aitelier" / "knowledge.md"
    _git(repo, "add", "-f", "--", ".aitelier/knowledge.md")
    _git(repo, "commit", "-qm", "legacy tracked knowledge")
    path.write_bytes(path.read_bytes().replace(b"goals_met=false", b"goals_met=true"))
    return repo, ws, path


def test_tracked_update_ref_failure_preserves_exact_state(tmp_path, monkeypatch):
    repo, ws, path = _tracked_generated_fixture(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_index = (repo / ".git" / "index").read_bytes()
    before_bytes = path.read_bytes()
    before_status = _git(repo, "status", "--porcelain=v1", "-z",
                         "--untracked-files=all").stdout
    real_git = knowledge_impl._git

    def unavailable(repo_arg, *args, **kwargs):
        if args and args[0] == "update-ref":
            return subprocess.CompletedProcess(["git", *args], 128, "", "injected")
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", unavailable)
    res = _call(repo, ws, "legacy")

    assert res["written"] is False
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert path.read_bytes() == before_bytes
    assert _git(repo, "status", "--porcelain=v1", "-z",
                "--untracked-files=all").stdout == before_status


def test_tracked_staged_path_refusal_preserves_exact_state(tmp_path):
    repo, ws, path = _tracked_generated_fixture(tmp_path)
    _git(repo, "add", "-f", "--", ".aitelier/knowledge.md")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_index = (repo / ".git" / "index").read_bytes()
    before_bytes = path.read_bytes()

    res = _call(repo, ws, "legacy")

    assert res["written"] is False and "staged changes" in res["error"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert path.read_bytes() == before_bytes


def test_tracked_status_failure_refuses_before_mutation(tmp_path, monkeypatch):
    repo, ws, path = _tracked_generated_fixture(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_index = (repo / ".git" / "index").read_bytes()
    before_bytes = path.read_bytes()
    real_git = knowledge_impl._git

    def unavailable(repo_arg, *args, **kwargs):
        if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            return subprocess.CompletedProcess(["git", *args], 128, "", "injected")
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", unavailable)
    res = _call(repo, ws, "legacy")

    assert res["written"] is False and "Git status is unavailable" in res["error"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert (repo / ".git" / "index").read_bytes() == before_index
    assert path.read_bytes() == before_bytes


def test_tracked_index_publish_failure_rolls_back_exactly(tmp_path, monkeypatch):
    repo, ws, path = _tracked_generated_fixture(tmp_path)
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    index = repo / ".git" / "index"
    before_index = index.read_bytes()
    before_bytes = path.read_bytes()
    before_status = _git(repo, "status", "--porcelain=v1", "-z",
                         "--untracked-files=all").stdout
    real_replace = knowledge_impl.os.replace

    def unavailable(source, destination):
        if Path(destination) == index:
            raise OSError("injected index publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(knowledge_impl.os, "replace", unavailable)
    res = _call(repo, ws, "legacy")

    assert res["written"] is False
    assert "rolled back exactly" in res["error"]
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert index.read_bytes() == before_index
    assert path.read_bytes() == before_bytes
    assert _git(repo, "status", "--porcelain=v1", "-z",
                "--untracked-files=all").stdout == before_status


def test_tracked_success_has_no_fallible_git_check_after_publication(
        tmp_path, monkeypatch):
    repo, ws, _ = _tracked_generated_fixture(tmp_path)
    real_git = knowledge_impl._git
    commands = []

    def recording_git(repo_arg, *args, **kwargs):
        commands.append(args)
        return real_git(repo_arg, *args, **kwargs)

    monkeypatch.setattr(knowledge_impl, "_git", recording_git)
    res = _call(repo, ws, "legacy")

    assert res["written"] is True
    assert commands[-1][0] == "update-ref"
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


def test_loadable_via_tool_loader():
    """knowledge_sync is discoverable by AItelier's ToolLoader."""
    from api.dependencies import get_tool_loader
    loader = get_tool_loader()
    schema = loader.load_schema("knowledge_sync")
    assert schema["name"] == "knowledge_sync"
    assert callable(loader.load_fn("knowledge_sync"))


def test_linked_worktree_uses_common_git_exclude(tmp_path):
    """Linked worktrees read excludes from the common git dir, not their gitdir."""
    primary = _make_repo(tmp_path)
    linked = tmp_path / "linked"
    made = _git(primary, "worktree", "add", "-q", "-b", "linked-test", str(linked))
    assert made.returncode == 0, made.stderr
    tracked_ignore = linked / ".gitignore"
    tracked_ignore.write_text("user-owned/\n", encoding="utf-8")
    _git(linked, "add", ".gitignore")
    _git(linked, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "user ignore")
    before = tracked_ignore.read_bytes()

    ws = _make_workspace(tmp_path, report={"all_goals_met": True})
    res = _call(linked, ws, "proj1")

    assert res["written"] is True
    assert tracked_ignore.read_bytes() == before
    assert _git(linked, "check-ignore", ".aitelier/knowledge.md").returncode == 0
    assert ".aitelier" not in _git(linked, "status", "--short").stdout
    common = _git(linked, "rev-parse", "--git-common-dir").stdout.strip()
    common_dir = Path(common) if Path(common).is_absolute() else (linked / common).resolve()
    assert (common_dir / "info" / "exclude").read_text().count(".aitelier/") == 1
