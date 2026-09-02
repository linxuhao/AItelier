"""Unit tests for the file-deletion tooling:
  repo_remove_file  — agent tool: validate + queue a repo path into _deletions.json
  repo_delete  — deliver hook: git rm the queued paths + commit + clear manifest
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aitelier.tools.repo_remove_file.impl import (
    repo_remove_file, _validate_rel, _append_deletion,
)
from aitelier.tools.repo_delete.impl import repo_delete


# ── repo_remove_file: the path jail ──────────────────────────────────────────────

class TestValidateRel:
    def test_normalizes_clean_paths(self):
        assert _validate_rel("web/js/api.js") == "web/js/api.js"
        assert _validate_rel(" web/js/api.js ") == "web/js/api.js"  # trimmed
        assert _validate_rel("web\\js\\api.js") == "web/js/api.js"  # backslashes

    @pytest.mark.parametrize("bad", [
        "", "   ", "../secret", "a/../../b", ".git", ".git/config",
        "/etc/passwd", "/../x",
    ])
    def test_rejects_unsafe(self, bad):
        with pytest.raises(ValueError):
            _validate_rel(bad)


class TestAppendDeletion:
    def test_appends_dedups_and_persists(self, tmp_path):
        assert _append_deletion(tmp_path, "a.js") == 1
        assert _append_deletion(tmp_path, "b.js") == 2
        assert _append_deletion(tmp_path, "a.js") == 2   # dedup, no growth
        assert json.loads((tmp_path / "_deletions.json").read_text()) == ["a.js", "b.js"]

    def test_creates_missing_dir(self, tmp_path):
        nested = tmp_path / "x" / "y.tmp"
        _append_deletion(nested, "z.js")
        assert (nested / "_deletions.json").exists()


# ── repo_remove_file: end-to-end with mocked host singletons ─────────────────────

class _FakeWS:
    def __init__(self, root):
        self.root = Path(root)

    def _draft_dir(self, project_id, step_id, graph_name=None):
        return self.root / project_id / (graph_name or "g") / f"{step_id}.tmp"


class _FakeSF:
    def __init__(self, run):
        self._run = run

    def get_run(self, run_id):
        return self._run


def test_repo_remove_file_queues_into_resolved_draft(tmp_path):
    ws = _FakeWS(tmp_path)
    sf = _FakeSF({"project_id": "proj1", "graph_name": "dpe_default_v2"})
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_workspace_manager", return_value=ws):
        r1 = repo_remove_file("web/js/api.js", run_id="rid", step_id="t_impl")
        r2 = repo_remove_file("web/js/sse.js", run_id="rid", step_id="t_impl")
    assert r1["queued_for_deletion"] == "web/js/api.js"
    assert r2["pending_deletions"] == 2
    manifest = ws._draft_dir("proj1", "t_impl", "dpe_default_v2") / "_deletions.json"
    assert json.loads(manifest.read_text()) == ["web/js/api.js", "web/js/sse.js"]


def test_repo_remove_file_rejects_unsafe_before_touching_host(tmp_path):
    # Jail rejection must happen before any singleton access — no patches needed.
    r = repo_remove_file("../../etc/passwd", run_id="rid", step_id="t_impl")
    assert "error" in r and "queued_for_deletion" not in r


def test_repo_remove_file_errors_when_project_unresolved(tmp_path):
    with patch("api.dependencies.get_skillflow", return_value=_FakeSF(None)), \
         patch("api.dependencies.get_workspace_manager", return_value=_FakeWS(tmp_path)):
        r = repo_remove_file("a.js", run_id="missing", step_id="t_impl")
    assert "error" in r


# ── repo_delete: apply the manifest against a real git repo ─────────────────

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    return path


def _commit_all(repo: Path, msg="init"):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


def test_repo_delete_git_rms_commits_and_clears(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "web").mkdir()
    (repo / "web" / "old.js").write_text("// old\n")
    (repo / "keep.py").write_text("x = 1\n")
    _commit_all(repo)

    step = tmp_path / "step"
    step.mkdir()
    (step / "_deletions.json").write_text(json.dumps(["web/old.js"]))

    r = repo_delete(source_dir=str(step), project_root=str(repo),
                    step_id="t_impl", project_id="proj1")

    assert r["deleted"] == ["web/old.js"]
    assert r["committed"] is True
    assert not (repo / "web" / "old.js").exists()
    assert (repo / "keep.py").exists()
    assert not (step / "_deletions.json").exists()          # manifest cleared
    # the removal is a real commit
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "delete" in log and "1 file(s)" in log


def test_repo_delete_skips_a_path_the_step_delivered(tmp_path):
    """R3b: the implementer queued a scenario for deletion to get `create` past
    "already exists", kept editing it, and delivery wrote it then erased it.
    A path present in the delivered step dir must never be git rm'd."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "playtest").mkdir()
    (repo / "playtest" / "route.yaml").write_text("name: route\n")
    (repo / "old.txt").write_text("old\n")
    _commit_all(repo)

    step = tmp_path / "step"
    (step / "playtest").mkdir(parents=True)
    (step / "playtest" / "route.yaml").write_text("name: route\nrewritten: true\n")
    (step / "_deletions.json").write_text(json.dumps(["playtest/route.yaml", "old.txt"]))

    r = repo_delete(source_dir=str(step), project_root=str(repo),
                    step_id="t_impl", project_id="proj1")

    assert r["deleted"] == ["old.txt"]
    assert (repo / "playtest" / "route.yaml").exists()
    assert [x["path"] for x in r["skipped"]] == ["playtest/route.yaml"]
    assert "delivered in this step" in r["skipped"][0]["reason"]


def test_repo_remove_file_refuses_a_path_present_in_staging(tmp_path):
    ws = _FakeWS(tmp_path / "ws")
    draft = ws._draft_dir("proj1", "t_impl")
    (draft / "playtest").mkdir(parents=True)
    (draft / "playtest" / "route.yaml").write_text("name: route\n")
    with patch("api.dependencies.get_skillflow", return_value=_FakeSF({"project_id": "proj1"})), \
         patch("api.dependencies.get_workspace_manager", return_value=ws):
        r = repo_remove_file("playtest/route.yaml", run_id="run1", step_id="t_impl")
    assert "staging output" in r["error"]
    assert not (draft / "_deletions.json").exists()


def test_repo_delete_noop_when_no_manifest(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    step = tmp_path / "step"
    step.mkdir()
    r = repo_delete(source_dir=str(step), project_root=str(repo))
    assert r == {"deleted": [], "committed": False}


def test_repo_delete_noop_on_empty_manifest(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    step = tmp_path / "step"
    step.mkdir()
    (step / "_deletions.json").write_text("[]")
    r = repo_delete(source_dir=str(step), project_root=str(repo))
    assert r["deleted"] == [] and r["committed"] is False
    assert not (step / "_deletions.json").exists()          # still cleared


def test_repo_delete_skips_unsafe_and_missing(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\n")
    _commit_all(repo)
    step = tmp_path / "step"
    step.mkdir()
    (step / "_deletions.json").write_text(
        json.dumps(["../escape", ".git/config", "missing.js"]))

    r = repo_delete(source_dir=str(step), project_root=str(repo))

    assert r["deleted"] == []
    assert r["committed"] is False
    assert len(r["skipped"]) == 3                            # 2 unsafe + 1 missing
    assert (repo / "a.py").exists()                          # untouched


def test_repo_delete_rolls_back_and_keeps_manifest_on_commit_failure(tmp_path):
    """A commit failure must NOT strand a staged deletion (for the next
    repo_apply `git add -A` to fold in) or lose the manifest: roll the git rm
    back, keep the manifest, return passed=False — then a retry succeeds."""
    repo = _git_repo(tmp_path / "repo")
    (repo / "web").mkdir()
    (repo / "web" / "old.js").write_text("// old\n")
    (repo / "keep.py").write_text("x = 1\n")
    _commit_all(repo)

    # Force `git commit` to fail deterministically via a failing pre-commit hook.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    step = tmp_path / "step"
    step.mkdir()
    (step / "_deletions.json").write_text(json.dumps(["web/old.js"]))

    r = repo_delete(source_dir=str(step), project_root=str(repo), step_id="t_impl")

    assert r["committed"] is False
    assert r.get("passed") is False
    # Rolled back: the file is restored, and nothing is left staged for a later
    # `git add -A` to fold into an unrelated commit.
    assert (repo / "web" / "old.js").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "old.js" not in status, f"staged deletion left dangling:\n{status}"
    # Manifest KEPT so the retry can re-attempt.
    assert (step / "_deletions.json").exists()

    # Remove the failing hook → a retry now succeeds and clears the manifest.
    hook.unlink()
    r2 = repo_delete(source_dir=str(step), project_root=str(repo), step_id="t_impl")
    assert r2["deleted"] == ["web/old.js"]
    assert r2["committed"] is True
    assert not (repo / "web" / "old.js").exists()
    assert not (step / "_deletions.json").exists()


# ── the reserved-prefix jail ────────────────────────────────────────────────
# Why this test exists (measured 2026-08-31, jinyong-wuxia round):
# skillflow's step-tool dispatcher (core.py, "Write/create/edit tools" block)
# routes on the tool NAME PREFIX alone — any call whose name starts with
# write_/create_/edit_/delete_ is handed to write_tools.execute_*, with the
# text after the first underscore taken as an output SLOT, and it never falls
# through to host-tool dispatch. A host tool named `delete_file` was therefore
# unreachable from every agent step for its whole life: each call became
# execute_delete(slot="file"), which found no such slot and answered
#   "'file' is a single required output () — it cannot be deleted, only rewritten."
# The empty parens are the tell — that is the missing pattern of a phantom slot.
# Cost: zero `_deletions.json` manifests ever written since the tool was added,
# and an implementer that burned 5 attempts and then wrote the WRONG root cause
# ("the config declares this file as a required output") into a design record.
# Host tools must stay off those four prefixes.
RESERVED_TOOL_PREFIXES = ("write_", "create_", "edit_", "delete_")


def test_no_host_tool_uses_a_skillflow_reserved_prefix():
    import pathlib
    tools_dir = pathlib.Path(__file__).resolve().parents[2] / "aitelier" / "tools"
    offenders = [d.name for d in tools_dir.iterdir()
                 if (d / "tool.yaml").exists()
                 and d.name.startswith(RESERVED_TOOL_PREFIXES)]
    assert offenders == [], (
        f"host tools shadowed by skillflow's write-tool dispatcher: {offenders} — "
        f"every call is routed to write_tools.execute_* and never reaches the impl"
    )
