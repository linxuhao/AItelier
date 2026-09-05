"""Which tree a RUN owns — decided once, recorded, and never inferred.

AItelier advances up to `AITELIER_MAX_CONCURRENT_PROJECTS` different projects at
a time and nothing in that path keys on `repo_path`. Two runs pointed at one
checkout therefore both reach `repo_apply`, which copies its step output in and
runs `git add -A`: whichever commits first stages the other run's files too,
under its own project and task name, with a file count that is wrong, and no
error anywhere. Until now the only thing preventing that was a human promise of
one writer per checkout.

This module is where that promise becomes a record:

* a code-producing run gets its own git worktree, on its own branch, pinned to
  the commit the source checkout was at when the run was created;
* a run that owns no repository but was launched AGAINST one gets a detached
  read snapshot — because a SHA written in a row is not a pinned READ of a
  checkout that keeps moving;
* a run explicitly asked to work in the source checkout takes an exclusive
  LEASE on it, which the supported host mutation APIs honour too;
* every one of those decisions is written down before the run starts, and
  resolution reads the record. A directory that happens to exist decides
  nothing, and a decision that cannot be honoured raises rather than falling
  back to the checkout the run was isolated from.

Nothing here deletes anything. Worktrees are retained unconditionally in this
delivery; `is_disposable` answers whether a disposal WOULD be safe and is the
whole of the retention story — a cleanup command is a separate piece of work,
and the failure mode of not having one is disk, while the failure mode of a
wrong reap is somebody's unmerged delivery.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from core import datadir
from skillflow.exceptions import IsolationUnavailable

MODE_WORKTREE = "worktree"
MODE_DIRECT = "direct"
MODE_NONE = "none"
MODE_READ_SNAPSHOT = "read_snapshot"

BRANCH_PREFIX = "codex/run/"

_GIT_ENV = {**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}


class CheckoutLeased(RuntimeError):
    """Someone else is working in this checkout, by declaration.

    Raised for a second `direct` run on a checkout a run already holds, and for
    a host operator mutation (commit/push/pull/reset through the supported
    WorkspaceManager APIs) while a run holds it. Deliberately NOT an
    `IsolationUnavailable`: nothing is broken, the answer is "not now, and here
    is who".
    """


# ── git ──────────────────────────────────────────────────────────────

def _git(cwd, *args, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=_GIT_ENV, check=check)


def canonical_checkout(path) -> str:
    """One identity for one working tree, whatever path names it.

    `~/x`, `/home/me/x/`, a symlink to it and the same path with `..` in it are
    the same checkout and must contend for the same lease. Git's own answer
    (`--show-toplevel`) is used when the path is inside a repository, so a
    subdirectory of a checkout does not lease a different thing than its root;
    `realpath` collapses the aliases either way.
    """
    p = Path(path).expanduser()
    top = _git(p if p.is_dir() else p.parent, "rev-parse", "--show-toplevel")
    if top.returncode == 0 and top.stdout.strip():
        return os.path.realpath(top.stdout.strip())
    return os.path.realpath(str(p))


def _is_git_repo(path) -> bool:
    p = Path(path)
    return p.is_dir() and (p / ".git").exists()


def _head_sha(repo) -> str:
    r = _git(repo, "rev-parse", "HEAD")
    if r.returncode != 0 or not r.stdout.strip():
        raise IsolationUnavailable(
            f"{repo} has no commit to pin a run to "
            f"({(r.stderr or '').strip()[:200]})")
    return r.stdout.strip()


def _is_worktree_of(path: Path, source_repo: str) -> bool:
    """Is `path` a live linked worktree of `source_repo`?

    Structural, not conventional: a linked worktree's `.git` is a gitfile
    pointing into the source repository's admin directory, and both sides must
    agree on the same common dir. A plain directory left at the recorded path
    answers False, which is the point — the recorded path existing is not
    evidence that the run's tree does.
    """
    if not path.is_dir() or not (path / ".git").is_file():
        return False
    a = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    b = _git(source_repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if a.returncode != 0 or b.returncode != 0:
        return False
    return os.path.realpath(a.stdout.strip()) == os.path.realpath(b.stdout.strip())


# ── records ──────────────────────────────────────────────────────────

def isolation_since(db) -> str:
    """When this deployment started recording isolation decisions.

    A run created after this has a record or it has nothing legitimate: that is
    what lets a missing record fail closed instead of being read as "legacy, use
    the old answer". Written once, by the schema init, and never updated.
    """
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM run_isolation_meta WHERE key = 'since'"
        ).fetchone()
        return row["value"] if row else ""


def record(db, run_id: str) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM run_isolation WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def retained(db) -> list[dict]:
    """Every tree this module has made and not been told to forget.

    Report-only, by design: it is the whole of retention management here.
    """
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM run_isolation WHERE worktree_path IS NOT NULL "
            "AND worktree_path != '' ORDER BY created_at").fetchall()]


def lease_holder(db, canonical: str) -> dict | None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM checkout_leases WHERE canonical_checkout = ?",
            (canonical,)).fetchone()
        return dict(row) if row else None


def require_no_foreign_lease(db, path, run_id: str | None = None) -> None:
    """Refuse a mutation of a checkout a different run is holding.

    Called by the host's own repository operations. It closes the gap between
    "the pipeline is serialised" and "the repository is quiet": an operator
    committing or resetting through the dashboard/CLI during a leased run is
    the same collision as a second pipeline, and the lease is worth nothing if
    only pipelines respect it.

    What it CANNOT cover, stated where the limit is: a shell running `git` in
    that directory by hand. Nothing in a process boundary can stop that, and
    pretending otherwise would be the more dangerous claim.
    """
    holder = lease_holder(db, canonical_checkout(path))
    if holder and (run_id is None or holder["run_id"] != run_id):
        raise CheckoutLeased(
            f"{path} is held by run {holder['run_id']} "
            f"(config {holder['config_name']}, since {holder['acquired_at']}). "
            f"Refusing to mutate a checkout another run is working in.")


def _acquire_lease(db, canonical: str, run_id: str, config_name: str) -> None:
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT run_id, config_name, acquired_at FROM checkout_leases "
            "WHERE canonical_checkout = ?", (canonical,)).fetchone()
        if row and row["run_id"] != run_id:
            raise CheckoutLeased(
                f"{canonical} is held by run {row['run_id']} "
                f"(config {row['config_name']}, since {row['acquired_at']}); "
                f"run {run_id} cannot work in it at the same time.")
        conn.execute(
            "INSERT OR REPLACE INTO checkout_leases "
            "(canonical_checkout, run_id, config_name, acquired_at) "
            "VALUES (?, ?, ?, COALESCE((SELECT acquired_at FROM checkout_leases "
            "WHERE canonical_checkout = ?), datetime('now')))",
            (canonical, run_id, config_name, canonical))
        conn.commit()


def _write_record(db, **kw) -> dict:
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_isolation "
            "(run_id, project_id, config_name, mode, source_repo, "
            " worktree_path, branch, base_sha, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (kw["run_id"], kw["project_id"], kw["config_name"], kw["mode"],
             kw.get("source_repo"), kw.get("worktree_path"),
             kw.get("branch"), kw.get("base_sha"), kw.get("note")))
        conn.commit()
    return record(db, kw["run_id"])


# ── the decision ─────────────────────────────────────────────────────

def _source_repo_for(db, project_id: str) -> str | None:
    try:
        info = db.get_repo_info(project_id) or {}
    except Exception:
        info = {}
    path = info.get("repo_path")
    if path:
        return str(path)
    if (info.get("repo_type") or "") == "none":
        return None
    return str(datadir.projects_dir() / project_id)


def ensure_for_run(db, *, run_id: str, project_id: str, config_name: str,
                   repo_mode: str = "code",
                   requested_mode: str | None = None) -> dict:
    """Decide (once) and provision what this run works in. Idempotent.

    Called before the run is started, and again on every resume — the second
    call returns the existing record and touches no git, which is what keeps a
    resumed run in the tree that holds its work in flight.

    `repo_mode` is the config's DECLARATION of whether it produces code, and it
    is the only thing consulted for that: a run that owns no repository is not
    handed a worktree, but it is still handed the repository it was launched
    against, as a read snapshot. Owning and reading are separate axes and
    conflating them is what took the codebase away from on-repo review.
    """
    existing = record(db, run_id)
    if existing:
        if existing["mode"] == MODE_DIRECT and existing["source_repo"]:
            # Re-assert the lease: a restart that lost the row would otherwise
            # leave a running direct run unprotected.
            _acquire_lease(db, canonical_checkout(existing["source_repo"]),
                           run_id, config_name)
        return existing

    source = _source_repo_for(db, project_id)
    owns_repo = (repo_mode or "code") != "none"

    if not owns_repo:
        # Owning no repository and reading one are separate axes. A run
        # launched `against_project` reads a real codebase and must keep doing
        # so — but a SHA in this row would not pin that read, because the
        # checkout keeps moving under it, so the read target is a detached
        # snapshot. With no target at all, the run is repo-less, unchanged.
        if not source or not _is_git_repo(source):
            return _write_record(db, run_id=run_id, project_id=project_id,
                                 config_name=config_name, mode=MODE_NONE)
        return _provision_tree(db, run_id=run_id, project_id=project_id,
                               config_name=config_name, source=source,
                               mode=MODE_READ_SNAPSHOT)

    mode = requested_mode or MODE_WORKTREE
    if mode == MODE_DIRECT:
        if not source:
            raise IsolationUnavailable(
                f"run {run_id} asked for direct mode and its project "
                f"{project_id!r} names no checkout")
        _acquire_lease(db, canonical_checkout(source), run_id, config_name)
        return _write_record(db, run_id=run_id, project_id=project_id,
                             config_name=config_name, mode=MODE_DIRECT,
                             source_repo=source)
    if mode != MODE_WORKTREE:
        raise IsolationUnavailable(f"unknown isolation mode {mode!r}")
    if not source or not _is_git_repo(source):
        # There is no repository to isolate FROM. Recorded as direct with the
        # reason attached, rather than refused, because the two failure modes
        # are not equal: this run cannot cross-stage into anything (repo_apply
        # commits through git and will fail loudly on the first delivery), while
        # refusing here would turn "your project points at a path that is not a
        # repository" into a run that never starts and says so only in a tick
        # log. It is a DECISION with its reason on the row — not the silent
        # fallback that a resolvable-but-broken worktree gets, which raises.
        return _write_record(
            db, run_id=run_id, project_id=project_id, config_name=config_name,
            mode=MODE_DIRECT, source_repo=source,
            note=f"no git repository at {source!r} when the run was created; "
                 f"nothing to cut a worktree from")
    return _provision_tree(db, run_id=run_id, project_id=project_id,
                           config_name=config_name, source=source,
                           mode=MODE_WORKTREE)


def _provision_tree(db, *, run_id, project_id, config_name, source, mode) -> dict:
    base = _head_sha(source)
    root = datadir.worktrees_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / run_id
    if path.exists():
        raise IsolationUnavailable(
            f"{path} already exists and run {run_id} has no record naming it; "
            f"refusing to adopt a directory whose provenance is unknown")
    if mode == MODE_WORKTREE:
        branch = f"{BRANCH_PREFIX}{run_id}"
        r = _git(source, "worktree", "add", "-b", branch, str(path), base)
    else:
        branch = None
        r = _git(source, "worktree", "add", "--detach", str(path), base)
    if r.returncode != 0:
        raise IsolationUnavailable(
            f"could not create the {mode} tree for run {run_id} from {source}: "
            f"{(r.stderr or r.stdout).strip()[:400]}")
    return _write_record(db, run_id=run_id, project_id=project_id,
                         config_name=config_name, mode=mode,
                         source_repo=source, worktree_path=str(path),
                         branch=branch, base_sha=base)


# ── resolution ───────────────────────────────────────────────────────

def resolve_for_resolver(db, run_id: str,
                         run_created_at: str | None = None) -> str | bool | None:
    """The answer the code-path resolver gives for THIS run.

    Three answers, and they are all different statements:

    * a path  — this run works in that tree;
    * ``False`` — this run owns and reads no repository;
    * ``None`` — no opinion, use the project-keyed answer (direct mode, and
      runs that predate isolation).

    Raises `IsolationUnavailable` when a decision exists and cannot be honoured.
    That is the whole point of the module: the tempting fallback — "the worktree
    is gone, use the project's checkout" — is precisely the shared tree the run
    was isolated from, and a run that resumes there quietly writes into it.
    """
    rec = record(db, run_id)
    if rec is None:
        return _no_record(db, run_id, run_created_at)

    mode = rec["mode"]
    if mode == MODE_NONE:
        return False
    if mode == MODE_DIRECT:
        return None
    path = Path(rec["worktree_path"] or "")
    if not rec["worktree_path"] or not _is_worktree_of(path, rec["source_repo"]):
        raise IsolationUnavailable(
            f"run {run_id} is recorded as {mode} in {rec['worktree_path']!r} "
            f"(branch {rec['branch']!r}, base {rec['base_sha']}), and that is "
            f"not a live worktree of {rec['source_repo']!r}. Refusing to "
            f"substitute the source checkout. Re-create the tree at that path "
            f"or record a disposition for the run.")
    return rec["worktree_path"]


_UNKNOWN_RUN = object()


def _no_record(db, run_id: str, run_created_at: str | None):
    """No record. Three genuinely different situations, and only one is safe.

    * The deployment predates isolation (no marker) or the run does — the
      project-keyed answer is the only one that run ever had, and taking it
      away would strand it. No opinion.
    * The run is not a run of THIS deployment at all: the engine has no row for
      it. Nothing was ever declared about it here, so there is no declaration to
      betray. No opinion. (This is what a harness that drives a SkillFlow of its
      own looks like from here.)
    * The run exists here, was created after the marker, and its record is
      gone. That is a declaration that has lost its record, and answering it
      with the project-keyed path would hand a run that may well have been
      isolated the shared checkout — silently. Refuse.

    The distinction is drawn from the engine row, not from a timestamp alone,
    because "created after the marker" and "created here" are different claims
    and only the second one licenses a refusal.
    """
    since = isolation_since(db)
    if not since:
        return None
    created = run_created_at if run_created_at is not None else _run_created_at(run_id)
    if created is _UNKNOWN_RUN:
        return None
    if created and created < since:
        return None
    raise IsolationUnavailable(
        f"run {run_id} was created at {created or 'an unknown time'}, at or "
        f"after this deployment began recording isolation ({since}), and has no "
        f"isolation record. A newly declared run does not silently fall back to "
        f"the shared checkout.")


def _run_created_at(run_id: str):
    """When this deployment created the run, `_UNKNOWN_RUN` if it did not.

    A lookup that RAISES is not "unknown": it is "cannot tell", and cannot tell
    resolves to the refusal above, because the alternative is to answer a
    question about isolation on the strength of a failed query.
    """
    from api.dependencies import get_skillflow
    row = get_skillflow().get_run(run_id)
    if row is None:
        return _UNKNOWN_RUN
    return row.get("created_at")


# ── retention (report-only) ──────────────────────────────────────────

def release(db, run_id: str, disposition: str) -> dict | None:
    """Record what happened to a run's tree and drop any lease it held.

    Records. Does not delete: `disposition='discarded'` means an operator has
    decided the work is not wanted, not that this module removed it.
    """
    rec = record(db, run_id)
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE run_isolation SET released_at = datetime('now'), "
            "disposition = ? WHERE run_id = ?", (disposition, run_id))
        conn.execute("DELETE FROM checkout_leases WHERE run_id = ?", (run_id,))
        conn.commit()
    return record(db, run_id) if rec else None


def is_disposable(db, run_id: str, *, run_status: str, admitted_ops: int,
                  integration_ref: str | None = None,
                  require_merge: bool = False) -> tuple[bool, str]:
    """Would deleting this run's tree lose nothing? Advisory, and only that.

    `run_status` and `admitted_ops` come from skillflow and are asked for
    explicitly rather than looked up, so a caller cannot accidentally answer
    this question about a run it has not actually observed.

    `paused` is NOT terminal: a checkpoint is a run waiting for a person.
    An admitted operation is not a running one either — it is an operation that
    was allowed to proceed and cannot be called off, which is exactly the state
    in which its effects may still be landing. Neither a dead owner nor elapsed
    time appears here: skillflow's cancellation contract says the death of an
    owner establishes nothing about a `git commit` its child may still be
    finishing, and time establishes less.
    """
    if require_merge and not integration_ref:
        raise TypeError(
            "is_disposable(require_merge=True) needs an integration_ref: the "
            "branch a delivery is integrated into is a caller's decision, not "
            "'master'")
    rec = record(db, run_id)
    if rec is None:
        return False, f"no isolation record for run {run_id}"
    if not rec["worktree_path"]:
        return True, "this run owns no tree"
    if run_status not in ("completed", "failed"):
        return False, (f"run is {run_status!r}, not terminal "
                       f"(paused is a run waiting for a person)")
    if admitted_ops:
        return False, (f"{admitted_ops} admitted operation(s) have not retired; "
                       f"effects may still be landing")
    path = Path(rec["worktree_path"])
    if not path.is_dir():
        return False, "the recorded tree is already gone; nothing to decide"
    if _git(path, "status", "--porcelain").stdout.strip():
        return False, "uncommitted work in the tree"
    if integration_ref:
        merged = _git(rec["source_repo"], "branch", "--merged", integration_ref)
        if (rec["branch"] or "") not in [
                b.strip().lstrip("* ") for b in merged.stdout.splitlines()]:
            return False, (f"branch {rec['branch']} is unmerged into "
                           f"{integration_ref}")
    return True, "terminal, quiet, clean" + (
        f", merged into {integration_ref}" if integration_ref else
        ", merge not checked (no integration_ref supplied)")
