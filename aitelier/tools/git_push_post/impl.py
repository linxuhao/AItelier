"""git_push_post — a finished round ends up on its remote branch.

The mirror image of skillflow's `git_sync_pre`, which runs at the top of the
pipeline and pulls; this runs at the bottom, on the run's ONLY clean exit
(`5_review → done`), and pushes.

Its skip rules are `git_sync_pre`'s, for the same reason: a project with no
repo, no remote, or a detached HEAD is a perfectly normal local-only project,
and the push step must not be the thing that stops such a run from completing.
Every one of those returns `pushed: false` with `action: "skip"` and a reason —
never an error.

A real push failure (auth, non-fast-forward, network) is different: it is worth
seeing, and it goes into the result and the trace as `action: "error"`. It still
does not fail the RUN. The round's work was committed locally by `repo_apply`
long before this step; failing here would not undo one commit, it would only
replace "passed, but the push failed" with "failed", which is less true.
"""

import subprocess
from pathlib import Path

_TIMEOUT_S = 120


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=_TIMEOUT_S)


def _skip(detail: str) -> dict:
    return {"pushed": False, "action": "skip", "detail": detail}


def git_push_post(*, project_root: str = "", remote: str = "origin",
                  **_ignored) -> dict:
    if not project_root:
        return _skip("no project_root given")
    root = Path(project_root).resolve()

    if not (root / ".git").exists():
        return _skip("not a git repository")

    r = _git(root, "remote")
    remotes = r.stdout.split()
    if not remotes:
        return _skip("no remote configured (local-only)")
    if remote not in remotes:
        return _skip(f"remote '{remote}' not configured (have: {', '.join(remotes)})")

    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = r.stdout.strip()
    if r.returncode != 0 or not branch or branch == "HEAD":
        return _skip("detached HEAD — nothing to push to a branch")

    # Already there? `git push` would say "Everything up-to-date" and succeed,
    # but saying so ourselves keeps the trace readable and costs one rev-parse.
    upstream = _git(root, "rev-parse", "--verify", f"{remote}/{branch}")
    if upstream.returncode == 0:
        same = _git(root, "rev-list", "--count",
                    f"{remote}/{branch}..HEAD").stdout.strip()
        if same == "0":
            return {"pushed": False, "action": "skip", "branch": branch,
                    "detail": f"{remote}/{branch} already has this commit"}

    # --set-upstream is harmless when the upstream already exists and is what
    # makes the FIRST push of a new branch land.
    push = _git(root, "push", "--set-upstream", remote, branch)
    if push.returncode != 0:
        detail = (push.stderr or push.stdout or "").strip()
        return {"pushed": False, "action": "error", "branch": branch,
                "remote": remote,
                "error": f"git push {remote} {branch} failed: {detail[:600]}"}

    return {"pushed": True, "action": "pushed", "branch": branch,
            "remote": remote,
            "detail": (push.stderr or push.stdout or "").strip()[:600]}
