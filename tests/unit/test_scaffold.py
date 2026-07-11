"""Regression tests for the scaffold tool (aitelier/tools/scaffold)."""

from aitelier.tools.scaffold.impl import scaffold


def test_writes_dot_prefixed_asset_as_dotfile(tmp_path):
    r = scaffold(project_root=str(tmp_path), addon="game_harness")
    assert ".gitignore" in r["written"]
    gi = (tmp_path / ".gitignore").read_text()
    assert ".godot/" in gi  # the Godot ignore landed


def test_merges_into_existing_gitignore_without_clobber(tmp_path):
    # The workspace pre-creates a default .gitignore — scaffold must MERGE, not skip.
    (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    r = scaffold(project_root=str(tmp_path), addon="game_harness")
    assert ".gitignore" in r["merged"] and r["written"] == []
    gi = (tmp_path / ".gitignore").read_text()
    assert "__pycache__/" in gi   # original preserved
    assert ".godot/" in gi        # Godot lines merged in


def test_merge_is_idempotent(tmp_path):
    scaffold(project_root=str(tmp_path), addon="game_harness")
    first = (tmp_path / ".gitignore").read_text()
    r2 = scaffold(project_root=str(tmp_path), addon="game_harness")
    # second run adds nothing new
    assert r2["merged"] == [] and ".gitignore" in r2["skipped"]
    assert (tmp_path / ".gitignore").read_text() == first


def test_unknown_addon_is_noop(tmp_path):
    r = scaffold(project_root=str(tmp_path), addon="does_not_exist")
    assert r["written"] == [] and "no assets" in r["reason"]


def test_missing_repo_is_noop():
    r = scaffold(project_root="/nonexistent/xyz", addon="game_harness")
    assert r["written"] == [] and "repo not found" in r["reason"]
