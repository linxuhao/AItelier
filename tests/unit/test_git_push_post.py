"""A finished round lands on its remote branch — and skips when there is none.

Mirrors skillflow's `git_sync_pre` at the other end of the pipeline. The point
of the skip rules is that this step runs on the run's ONLY clean exit: a
local-only project (no repo, no remote, detached HEAD) must not be stopped from
COMPLETING by the step that would have pushed it somewhere.

A real push failure is the opposite case — it is worth seeing, so it is reported
as `action: "error"` — but it still must not fail the run: `repo_apply`
committed the round's work locally, task by task, long before this step.
"""
import subprocess
from pathlib import Path

import pytest
import yaml

from aitelier.tools.git_push_post.impl import git_push_post

ROOT = Path(__file__).resolve().parents[2]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("x", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "one")
    return path


# ── the skips: every one of these must let the run finish ────────────────────

def test_no_project_root_skips(tmp_path):
    r = git_push_post(project_root="")
    assert r["action"] == "skip" and r["pushed"] is False


def test_a_plain_directory_skips(tmp_path):
    r = git_push_post(project_root=str(tmp_path))
    assert r["action"] == "skip"
    assert "not a git repository" in r["detail"]


def test_a_repo_with_no_remote_skips(tmp_path):
    # The case the user named: no remote → don't push, move on.
    r = git_push_post(project_root=str(_repo(tmp_path / "r")))
    assert r["action"] == "skip"
    assert "no remote" in r["detail"]


def test_a_different_remote_name_skips_and_says_what_exists(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "remote", "add", "upstream", str(tmp_path / "bare.git"))
    r = git_push_post(project_root=str(repo), remote="origin")
    assert r["action"] == "skip"
    assert "upstream" in r["detail"]


def test_a_detached_head_skips(tmp_path):
    repo = _repo(tmp_path / "r")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "checkout", "-q", sha)
    _git(repo, "remote", "add", "origin", str(tmp_path / "bare.git"))
    r = git_push_post(project_root=str(repo))
    assert r["action"] == "skip"
    assert "detached" in r["detail"]


# ── the push itself ──────────────────────────────────────────────────────────

def test_it_pushes_to_the_matching_remote_branch(tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path / "r")
    _git(repo, "remote", "add", "origin", str(bare))

    r = git_push_post(project_root=str(repo))

    assert r["pushed"] is True and r["action"] == "pushed"
    assert r["branch"] == "main"
    # The remote really has it — not just a zero exit code.
    out = subprocess.run(["git", "-C", str(bare), "rev-parse", "refs/heads/main"],
                         capture_output=True, text=True)
    assert out.returncode == 0


def test_a_second_run_with_nothing_new_skips(tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path / "r")
    _git(repo, "remote", "add", "origin", str(bare))
    git_push_post(project_root=str(repo))

    r = git_push_post(project_root=str(repo))
    assert r["action"] == "skip"
    assert "already has this commit" in r["detail"]


def test_a_push_failure_is_reported_loudly_and_is_not_a_skip(tmp_path):
    # A dangling remote path: the push genuinely fails. That must be visible,
    # not laundered into a skip — but it still returns rather than raising.
    repo = _repo(tmp_path / "r")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))

    r = git_push_post(project_root=str(repo))

    assert r["action"] == "error"
    assert r["pushed"] is False
    assert "git push" in r["error"]


# ── the wiring ───────────────────────────────────────────────────────────────

def _graph(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_the_base_pipeline_pushes_on_its_only_clean_exit():
    g = _graph("configs/dpe_default.yaml")
    steps = {s["id"]: s for s in g["steps"]}
    assert "git_push_post" in steps, "the push step belongs on the BASE pipeline"

    review = steps["5_review"]
    passed = [t for t in review["transitions"]
              if (t.get("match") or {}).get("value") is True]
    assert [t["to"] for t in passed] == ["git_push_post"], (
        "the pass edge must reach the push step, not jump straight to done")
    assert [t["to"] for t in steps["git_push_post"]["transitions"]] == ["done"]


def test_the_push_step_has_exactly_one_unconditional_edge():
    # No `match` on the way out: a push failure must not strand a passed run at
    # an unrouted node, which is how "no matching transition" kills a run.
    g = _graph("configs/dpe_default.yaml")
    step = next(s for s in g["steps"] if s["id"] == "git_push_post")
    assert len(step["transitions"]) == 1
    assert step["transitions"][0].get("match") is None


def test_the_game_pipeline_inherits_it():
    from skillflow.compose import compose_graph
    merged = compose_graph(_graph("configs/dpe_default.yaml"),
                           [_graph("configs/addons/game_harness.yaml")])
    steps = {s["id"]: s for s in merged["steps"]}
    assert "git_push_post" in steps
    assert [t["to"] for t in steps["git_push_post"]["transitions"]] == ["done"]
