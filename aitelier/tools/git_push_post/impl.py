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
_TIMEOUT_RC = 124      # timeout(1)'s shell convention


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # A slow first push of a large repo must not fail the run — that is
        # this tool's whole contract. Shape it like a failed git call so every
        # caller's returncode check routes it to the skip/error path.
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=_TIMEOUT_RC, stdout="",
            stderr=f"timed out after {_TIMEOUT_S}s")


def _skip(detail: str) -> dict:
    return {"pushed": False, "action": "skip", "detail": detail}


def _code_path(project_id: str, project_root: str,
                run_id: str = "") -> Path | None:
    """Where this project's code actually lives.

    `$PROJECT_ROOT` expands to skillflow's DEFAULT layout — `projects_base/<id>`
    — which is right for `repo_type: new|clone` and WRONG for every
    `repo_type: existing` project, whose repo is somewhere else entirely
    (recorded in the DB as `repo_path`). Measured 2026-08-27: on jinyong-neigong
    that token pointed at `projects/jinyong-neigong`, which does not exist,
    while the code was in `projects/jinyong-assets`.

    So ask the host's own resolver first — the same one skillflow is handed as
    `code_path_resolver`, which is what makes `repo_apply` commit into the real
    repo — and fall back to the token only when the resolver has no answer,
    which is exactly the new/clone case where the token is correct.
    """
    if project_id:
        try:
            from api.dependencies import _existing_repo_code_path
            resolved = _existing_repo_code_path(project_id, run_id or None)
        except Exception:
            resolved = None
        if resolved is False:
            # The run DECLARES it owns no code repository. That is an answer,
            # not the absence of one, so the `$PROJECT_ROOT` token must not step
            # in: it expands to skillflow's default layout, which does not become
            # right just because the resolver offered no path.
            return None
        if resolved:
            return Path(resolved).resolve()
    return Path(project_root).resolve() if project_root else None


def git_push_post(*, project_root: str = "", remote: str = "origin",
                  project_id: str = "", run_id: str = "",
                  policy: str = "direct_only", **_ignored) -> dict:
    # The whole contract in one wrapper: NOTHING that goes wrong in a push may
    # take down (or wedge) a run that already passed review. The Timeout shim
    # covers one class; a raised OSError (git binary missing, fork failure)
    # would make skillflow reopen the step and re-raise on every tick — the
    # run never fails, it just never reaches done.
    try:
        return _git_push_post(project_root=project_root, remote=remote,
                              project_id=project_id, run_id=run_id,
                              policy=policy)
    except Exception as e:
        return {"pushed": False, "action": "error",
                "error": f"{type(e).__name__}: {e}"[:400]}


def _declared_mode(project_id: str, run_id: str):
    """What the RUN declared, and how confidently we know it.

    Returns ``(mode, source)`` where source is ``"record"`` (the run declared
    it), ``"no-run"`` (no run id was supplied — the legacy invocation, whose
    contract is deliberately preserved), ``"missing"`` (a run was named and has
    no record) or a lookup error. Only the first two are answers.
    """
    if not run_id:
        return None, "no-run"
    try:
        from api.dependencies import get_db_manager
        from core import run_isolation
        rec = run_isolation.record(get_db_manager(), run_id)
    except Exception as e:
        return None, f"its isolation record could not be read " \
                     f"({type(e).__name__}: {e})"
    if rec is None:
        return None, "no isolation record exists for it"
    return (rec.get("mode") or ""), "record"


def _git_push_post(*, project_root: str, remote: str, project_id: str,
                   run_id: str = "", policy: str = "direct_only") -> dict:
    root = _code_path(project_id, project_root, run_id)
    if root is None:
        return _skip("no code path: neither the host resolver nor "
                     "$PROJECT_ROOT gave one")

    # A RUN WORKTREE IS NOT PUSHED UNLESS ASKED. Routing this tool at the run's
    # own tree (which is the correct root for it) changed what a push means:
    # the worktree is on `codex/run/<run_id>`, so every isolated run on a
    # project with a remote would create and push a new remote branch, one per
    # run, automatically and without bound. That is a delivery decision, and it
    # arrived here as a side effect of a routing fix — exactly what must not
    # happen. Default is no push at all; `policy: "run_branch"` opts in.
    #
    # WHICH runs those are is read from the run's DECLARED record, not from the
    # shape of the directory. `.git` being a gitfile says "a linked worktree",
    # which is a different statement from "this run was isolated into one": a
    # project whose repo_path is itself a worktree — routine for anyone working
    # out of worktrees — run in explicit direct mode had its intentional
    # private push silently skipped, and the refusal misdescribed it.
    mode, source = _declared_mode(project_id, run_id)
    if source not in ("record", "no-run"):
        # A run was named and its declaration cannot be read. The filesystem is
        # not an answer to what that run declared, so nothing is pushed and the
        # reason says which of the two it was.
        return _skip(
            f"run {run_id} did not declare where it works ({source}): refusing "
            f"to decide a push from the shape of {root}. Provision the run's "
            f"isolation record, or invoke without a run id for the legacy "
            f"contract.")
    if mode in ("worktree", "read_snapshot") and policy != "run_branch":
        return _skip(
            f"run {run_id} is declared {mode} at {root}: no automatic push. Its "
            f"branch is per-run, and pushing one remote branch per run is a "
            f"delivery policy, not a consequence of where the run works. Set "
            f"policy: \"run_branch\" on the step to push it.")

    if not (root / ".git").exists():
        # Name the path. `git_sync_pre` has been answering a bare
        # "not a git repository" on every existing-repo run, and without the
        # path there is nothing in the answer to tell a local-only project from
        # a misresolved one.
        return _skip(f"not a git repository: {root}")

    r = _git(root, "remote")
    if r.returncode != 0:
        # A timed-out or failed PROBE must not masquerade as a normal state:
        # rc 124 with empty stdout used to read "no remote configured
        # (local-only)" — a confident wrong diagnosis for a hung mount or a
        # stale index.lock, after which pushes silently stop forever.
        return {"pushed": False, "action": "error",
                "error": f"git remote failed: {(r.stderr or '').strip()[:300]}"}
    remotes = r.stdout.split()
    if not remotes:
        return _skip("no remote configured (local-only)")
    if remote not in remotes:
        return _skip(f"remote '{remote}' not configured (have: {', '.join(remotes)})")

    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = r.stdout.strip()
    if r.returncode != 0:
        return {"pushed": False, "action": "error",
                "error": f"git rev-parse failed: {(r.stderr or '').strip()[:300]}"}
    if not branch or branch == "HEAD":
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
