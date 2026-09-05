"""`git_history` — the reviewer's third evidence source, and its two guards.

The diff says what changed and the working tree says what the code is now; only
the history says what it used to be. Two review questions need it and neither of
the other sources can answer either: why a line is written the way it is, and
whether a change is reintroducing something a past commit deliberately removed.

Two properties matter more than the query modes themselves and are pinned here:

  * A run with no repository gets an ERROR, never a fallback root. Resolving an
    empty `project_root` to the process cwd is a documented trap in this
    codebase — it is how a native tool ended up rooted at the container's /app
    and answered questions about AItelier's own source when asked about a
    project. A diff-only reviewer must be told it has no history.
  * The git argv is assembled from an allowlisted mode and validated values, so
    no argument can reach a writing command or escape the repository.
"""
import subprocess

import pytest

from aitelier.tools.git_history.impl import git_history


@pytest.fixture
def repo(tmp_path):
    """A repo whose history contains a deliberate removal to find."""
    r = tmp_path / "repo"
    r.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(r), *args], check=True,
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (r / "lib.py").write_text(
        "def greet(name):\n    retries = 3\n    return name\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "add greet with a retry budget")
    # The removal a later change might unknowingly undo.
    (r / "lib.py").write_text(
        "def greet(name):\n    return name\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "drop the unused retry budget")
    return r


def test_a_run_with_no_repository_is_told_so_and_gets_no_fallback_root(
        repo, monkeypatch):
    """Called from INSIDE a real repository, so a CWD fallback would succeed.

    That is the whole hazard: `Path("")` is `Path(".")`, and a tool that took it
    would answer with the history of whatever repository the process happens to
    be standing in — in the container, AItelier's own source at /app.
    """
    monkeypatch.chdir(repo)

    out = git_history(mode="log", project_root="")

    assert "error" in out, (
        "an empty root resolved to the current directory's repository: %r" % out)
    assert "no repository" in out["error"]
    # The message must point at the fix, not merely refuse.
    assert "against_project" in out["error"]
    assert "commits" not in out


def test_log_reports_the_commits_touching_a_file(repo):
    out = git_history(mode="log", path="lib.py", project_root=str(repo))
    assert "error" not in out, out
    subjects = [c["subject"] for c in out["commits"]]
    assert "drop the unused retry budget" in subjects
    assert "add greet with a retry budget" in subjects
    assert all(c["sha"] for c in out["commits"])


def test_search_finds_the_commit_that_removed_a_string(repo):
    """The pickaxe: this is the "are we reintroducing this?" question."""
    out = git_history(mode="search", query="retries = 3", project_root=str(repo))
    assert "error" not in out, out
    subjects = [c["subject"] for c in out["commits"]]
    assert "drop the unused retry budget" in subjects, out
    assert "add greet with a retry budget" in subjects, out


def test_search_without_a_query_is_refused(repo):
    out = git_history(mode="search", project_root=str(repo))
    assert "error" in out and "query is required" in out["error"]


def test_blame_attributes_a_line_range(repo):
    out = git_history(mode="blame", path="lib.py", start_line=1, end_line=2,
                      project_root=str(repo))
    assert "error" not in out, out
    assert "def greet" in out["blame"]


def test_show_renders_a_commit_found_through_log(repo):
    log = git_history(mode="log", path="lib.py", project_root=str(repo))
    sha = log["commits"][0]["sha"]
    out = git_history(mode="show", sha=sha, project_root=str(repo))
    assert "error" not in out, out
    assert "retry budget" in out["commit"]


def test_show_refuses_anything_that_is_not_a_hex_sha(repo):
    """A revision expression is not a commit id. Rejecting non-hex keeps every
    `show` argument unable to name a range, a branch, or an option."""
    for bad in ("HEAD", "--output=/tmp/x", "main..HEAD", "-n1", "HEAD~1"):
        out = git_history(mode="show", sha=bad, project_root=str(repo))
        assert "error" in out, f"{bad!r} was accepted: {out}"
        assert "hex commit id" in out["error"]


def test_paths_cannot_escape_the_repository_or_read_dot_git(repo):
    for bad in ("../outside.py", "/etc/passwd", ".git/config"):
        out = git_history(mode="blame", path=bad, project_root=str(repo))
        assert "error" in out, f"{bad!r} was accepted: {out}"


def test_an_unknown_mode_names_the_ones_that_exist(repo):
    out = git_history(mode="checkout", project_root=str(repo))
    assert "error" in out
    assert "log, search, blame, show" in out["error"]


def test_a_path_that_is_not_a_git_repo_is_reported_as_such(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = git_history(mode="log", project_root=str(plain))
    assert "error" in out and "not a git repository" in out["error"]
