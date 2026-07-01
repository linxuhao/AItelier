"""Unit tests for the file-deletion tooling:
  delete_file  — agent tool: validate + queue a repo path into _deletions.json
  repo_delete  — deliver hook: git rm the queued paths + commit + clear manifest
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from aitelier.tools.delete_file.impl import (
    delete_file, _validate_rel, _append_deletion,
)
from aitelier.tools.repo_delete.impl import repo_delete


# ── delete_file: the path jail ──────────────────────────────────────────────

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


# ── delete_file: end-to-end with mocked host singletons ─────────────────────

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


def test_delete_file_queues_into_resolved_draft(tmp_path):
    ws = _FakeWS(tmp_path)
    sf = _FakeSF({"project_id": "proj1", "graph_name": "dpe_default_v2"})
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_workspace_manager", return_value=ws):
        r1 = delete_file("web/js/api.js", run_id="rid", step_id="t_impl")
        r2 = delete_file("web/js/sse.js", run_id="rid", step_id="t_impl")
    assert r1["queued_for_deletion"] == "web/js/api.js"
    assert r2["pending_deletions"] == 2
    manifest = ws._draft_dir("proj1", "t_impl", "dpe_default_v2") / "_deletions.json"
    assert json.loads(manifest.read_text()) == ["web/js/api.js", "web/js/sse.js"]


def test_delete_file_rejects_unsafe_before_touching_host(tmp_path):
    # Jail rejection must happen before any singleton access — no patches needed.
    r = delete_file("../../etc/passwd", run_id="rid", step_id="t_impl")
    assert "error" in r and "queued_for_deletion" not in r


def test_delete_file_errors_when_project_unresolved(tmp_path):
    with patch("api.dependencies.get_skillflow", return_value=_FakeSF(None)), \
         patch("api.dependencies.get_workspace_manager", return_value=_FakeWS(tmp_path)):
        r = delete_file("a.js", run_id="missing", step_id="t_impl")
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
