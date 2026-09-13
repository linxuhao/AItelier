import json
import subprocess
from pathlib import Path

import pytest
import yaml

from aitelier import novel_state as ns
from aitelier.tools.scaffold_bible.impl import scaffold_bible


SEED = {
    "overview.md": "# 总纲\n\n一部可恢复的小说。",
    "compass.md": "终局：归家。",
    "world.yaml": {"rules": ["代价守恒"]},
    "pacing.yaml": {"min_chars_per_chapter": 100},
    "characters.yaml": [{"name": "林舟", "role": "protagonist"}],
    "threads.yaml": [{"name": "旧约", "description": "未解"}],
    "arcs.yaml": [{"name": "归途", "nodes": [{"id": "n1", "beat": "启程"}]}],
}


def git(root: Path, *args: str, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check,
                          capture_output=True, text=True)


def init_repo(root: Path, initial=False):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init")
    git(root, "config", "user.email", "novel@test")
    git(root, "config", "user.name", "Novel Test")
    if initial:
        (root / "README.md").write_text("base\n")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "base")


def seed(root: Path):
    bible = root / "novel" / "bible"
    bible.mkdir(parents=True)
    for name, value in SEED.items():
        (bible / name).write_text(
            value if isinstance(value, str)
            else yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")


def tag_sha(root: Path):
    return git(root, "rev-parse", "novel-genesis^{commit}").stdout.strip()


def assert_frozen(root: Path, result: dict):
    assert result["scaffolded"] is True
    assert result["committed"] is True
    assert result["genesis_tagged"] is True
    assert result["commit"] == tag_sha(root)
    shown = git(root, "show", "novel-genesis:novel/state/index.yaml").stdout
    assert "next_chapter" in shown
    assert git(root, "show", "novel-genesis:novel/bible/characters/林舟.yaml").stdout
    assert git(root, "cat-file", "-e",
               "novel-genesis:novel/bible/characters.yaml", check=False).returncode != 0


def test_normal_checkout_freezes_a_readable_unique_genesis(tmp_path):
    init_repo(tmp_path)
    seed(tmp_path)
    result = scaffold_bible(project_root=str(tmp_path))
    assert_frozen(tmp_path, result)
    assert result["recovered"] is False
    head = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(ValueError, match="already scaffolded"):
        scaffold_bible(project_root=str(tmp_path))
    assert tag_sha(tmp_path) == head


def test_linked_worktree_uses_gitfile_and_freezes_genesis(tmp_path):
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    init_repo(main, initial=True)
    git(main, "worktree", "add", "-b", "novel", str(linked))
    assert (linked / ".git").is_file()
    seed(linked)
    result = scaffold_bible(project_root=str(linked))
    assert_frozen(linked, result)
    assert git(main, "rev-parse", "novel-genesis^{commit}").stdout.strip() == result["commit"]
    assert ns.genesis_characters(linked)["林舟"]["role"] == "protagonist"


def test_commit_failure_preserves_bible_and_retry_completes(tmp_path):
    init_repo(tmp_path)
    seed(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho first-commit-failure >&2\nexit 23\n")
    hook.chmod(0o755)
    with pytest.raises(ValueError, match="retrying.*first-commit-failure"):
        scaffold_bible(project_root=str(tmp_path))
    assert (tmp_path / "novel/bible/characters/林舟.yaml").is_file()
    assert not (tmp_path / "novel/bible/characters.yaml").exists()
    # Simulate interruption between normalization and derived-index durability.
    # The private recovery record, rather than the once-only index guard, owns retry.
    (tmp_path / "novel/state/index.yaml").unlink()
    hook.unlink()
    result = scaffold_bible(project_root=str(tmp_path))
    assert result["recovered"] is True
    assert_frozen(tmp_path, result)


def test_tag_failure_preserves_commit_and_retry_tags_that_commit(tmp_path):
    init_repo(tmp_path)
    seed(tmp_path)
    lock = tmp_path / ".git" / "refs" / "tags" / "novel-genesis.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("locked")
    with pytest.raises(ValueError, match="retrying.*cannot lock ref"):
        scaffold_bible(project_root=str(tmp_path))
    committed = git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    lock.unlink()
    result = scaffold_bible(project_root=str(tmp_path))
    assert result["recovered"] is True and result["commit"] == committed
    assert_frozen(tmp_path, result)


def test_existing_genesis_conflict_is_never_overwritten_or_normalized(tmp_path):
    init_repo(tmp_path, initial=True)
    git(tmp_path, "tag", "novel-genesis")
    original = tag_sha(tmp_path)
    seed(tmp_path)
    raw = (tmp_path / "novel/bible/characters.yaml").read_bytes()
    with pytest.raises(ValueError, match="already scaffolded"):
        scaffold_bible(project_root=str(tmp_path))
    assert tag_sha(tmp_path) == original
    assert (tmp_path / "novel/bible/characters.yaml").read_bytes() == raw
    assert not (tmp_path / "novel/state/index.yaml").exists()


def test_no_git_nested_root_and_invalid_root_fail_before_writes(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    seed(plain)
    raw = (plain / "novel/bible/characters.yaml").read_bytes()
    with pytest.raises(ValueError, match="not a Git worktree"):
        scaffold_bible(project_root=str(plain))
    assert (plain / "novel/bible/characters.yaml").read_bytes() == raw

    repo = tmp_path / "repo"
    init_repo(repo)
    nested = repo / "nested"
    nested.mkdir()
    seed(nested)
    with pytest.raises(ValueError, match="must be the Git worktree root"):
        scaffold_bible(project_root=str(nested))
    assert (nested / "novel/bible/characters.yaml").is_file()

    with pytest.raises(ValueError, match="absolute path"):
        scaffold_bible(project_root="relative")
    with pytest.raises(ValueError, match="not a Git worktree"):
        scaffold_bible(project_root=str(tmp_path / "missing"))

    source_checkout = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="refusing.*AItelier source"):
        scaffold_bible(project_root=str(source_checkout))


def test_first_git_failure_survives_a_second_failed_retry(tmp_path):
    init_repo(tmp_path)
    seed(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho original-failure >&2\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(ValueError, match="original-failure"):
        scaffold_bible(project_root=str(tmp_path))
    hook.write_text("#!/bin/sh\necho later-failure >&2\nexit 2\n")
    with pytest.raises(ValueError, match="original-failure") as second:
        scaffold_bible(project_root=str(tmp_path))
    assert "later-failure" not in str(second.value)
    recovery = json.loads((tmp_path / ".git" / "aitelier-novel-genesis-recovery.json").read_text())
    assert "original-failure" in recovery["first_error"]


def test_freeze_commits_only_novel_and_preserves_unrelated_staging(tmp_path):
    init_repo(tmp_path, initial=True)
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("must remain staged\n")
    git(tmp_path, "add", "unrelated.txt")
    seed(tmp_path)

    result = scaffold_bible(project_root=str(tmp_path))

    assert_frozen(tmp_path, result)
    committed_paths = git(tmp_path, "-c", "core.quotepath=false", "show",
                          "--format=", "--name-only",
                          "novel-genesis").stdout.splitlines()
    assert committed_paths and all(path.startswith("novel/") for path in committed_paths)
    assert git(tmp_path, "diff", "--cached", "--name-only").stdout.splitlines() == [
        "unrelated.txt"]
