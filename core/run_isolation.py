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
import re
import subprocess
from contextlib import contextmanager
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


def _worker_identity(kind: str) -> str:
    """Who is holding this, in a form an operator can check.

    skillflow's own identity (pid + boot id) when available, because the point
    of naming an owner is that somebody can ask whether it still exists.
    """
    try:
        from skillflow.identity import worker_identity
        return worker_identity(kind)
    except Exception:
        return f"pid:{os.getpid()}"


def _owner_state(owner: str) -> str:
    try:
        from skillflow.identity import owner_is_dead
        dead = owner_is_dead(owner)
    except Exception:
        return "unknown"
    return "dead" if dead is True else "unknown" if dead is None else "alive"


def admit_write(db, path, *, kind: str, detail: str = "") -> dict:
    """Admit ONE write to a checkout, or refuse it. Returns the admission.

    This is the arbitration half of the exclusion, and it is why the guard it
    replaces was not enough: checking for a lease and then writing is two
    operations, and a direct run can take the checkout between them. Admission
    and the lease acquisition below share ONE `BEGIN IMMEDIATE` transaction on
    the same database, so whichever reaches it first wins and the other is
    refused — in both orders, which is the whole property.

    The transaction is short by construction: two statements, no I/O, and it is
    committed before the caller does any work. What outlives it is a ROW, and
    that is deliberate — the guard has to cover a `bash` that runs for minutes
    or an editor between read and write, and no transaction may be held across
    a subprocess, an engine call or a model call.

    A row that outlives its writer (a crash) BLOCKS, and it is not expired by
    time or by the death of its owner: neither says the write's effects ended.
    `clear_write_admission` is the operator's way out, with evidence, and the
    refusal says so.
    """
    canonical = canonical_checkout(path)
    owner = _worker_identity(kind)
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            lease = conn.execute(
                "SELECT run_id, config_name, acquired_at FROM checkout_leases "
                "WHERE canonical_checkout = ?", (canonical,)).fetchone()
            if lease:
                raise CheckoutLeased(
                    f"{canonical} is held by run {lease['run_id']} "
                    f"(config {lease['config_name']}, since "
                    f"{lease['acquired_at']}); refusing {kind} {detail!r}.")
            cur = conn.execute(
                "INSERT INTO checkout_write_admissions "
                "(canonical_checkout, owner, kind, detail, admitted_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (canonical, owner, kind, detail))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {"id": cur.lastrowid, "canonical_checkout": canonical,
            "owner": owner, "kind": kind, "detail": detail}


def retire_write(db, admission_id: int) -> None:
    """Retire ONE admission — this caller's own, by id, never a sweep."""
    if not admission_id:
        return
    with db.get_connection() as conn:
        conn.execute("DELETE FROM checkout_write_admissions WHERE id = ?",
                     (admission_id,))
        conn.commit()


@contextmanager
def write_admission(db, path, *, kind: str, detail: str = ""):
    """Admit, do the work, retire in `finally`. The supported write surface."""
    adm = admit_write(db, path, kind=kind, detail=detail)
    try:
        yield adm
    finally:
        retire_write(db, adm["id"])


def mark_write_admission_pending(db, admission_id: int, evidence: str) -> None:
    """Keep an admission BECAUSE its writers could not be confirmed ended.

    The row is not retired and does not expire. What changes is that it now
    carries why: the refusal a direct run gets prints `detail`, so the operator
    who has to decide reads the reason there rather than in a log they would
    have to know to look for.

    Appended, not replaced — the original command is what identifies the writer.
    """
    if not admission_id:
        return
    note = f" | PENDING WRITERS: {evidence}"[:800]
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE checkout_write_admissions SET detail = substr(COALESCE(detail,'') "
            "|| ?, 1, 2000) WHERE id = ?", (note, admission_id))
        conn.commit()
    import logging
    logging.getLogger("aitelier.isolation").warning(
        "write admission %s RETAINED: %s", admission_id, evidence[:2000])


def write_admissions(db, canonical: str | None = None) -> list[dict]:
    """In-flight writes, for an operator and for the refusal messages."""
    with db.get_connection() as conn:
        if canonical:
            rows = conn.execute(
                "SELECT * FROM checkout_write_admissions WHERE "
                "canonical_checkout = ? ORDER BY id", (canonical,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM checkout_write_admissions "
                                "ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def clear_write_admission(db, admission_id: int, *, evidence: str) -> dict:
    """Release an admission whose owner is gone — on EVIDENCE, never on time.

    Same standard skillflow sets for an admitted operation: the owner's death
    proves nothing about a subprocess it started, so what is required is a
    statement that the write's effects have ended. Recorded in the log with the
    row it released.
    """
    if not (evidence or "").strip():
        raise ValueError(
            "clear_write_admission requires evidence that the write has ENDED "
            "(not that its owner died or that time passed): what was checked, "
            "and by whom.")
    rows = [r for r in write_admissions(db) if r["id"] == admission_id]
    with db.get_connection() as conn:
        conn.execute("DELETE FROM checkout_write_admissions WHERE id = ?",
                     (admission_id,))
        conn.commit()
    import logging
    logging.getLogger("aitelier.isolation").warning(
        "write admission %s cleared by an operator: %s (row: %s)",
        admission_id, evidence[:2000], rows[0] if rows else "already gone")
    return {"cleared": bool(rows), "evidence": evidence[:2000],
            "admission": rows[0] if rows else None}


def _acquire_lease(db, canonical: str, run_id: str, config_name: str) -> None:
    """Take the checkout for a direct-mode run — if nothing is writing in it.

    The in-flight write check is inside the same `BEGIN IMMEDIATE` as the lease
    insert, which is what makes this a mutex with `admit_write` rather than two
    guards that happen to look at each other.
    """
    with db.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            inflight = conn.execute(
                "SELECT id, owner, kind, detail, admitted_at FROM "
                "checkout_write_admissions WHERE canonical_checkout = ? "
                "ORDER BY id", (canonical,)).fetchall()
            if inflight:
                w = inflight[0]
                raise CheckoutLeased(
                    f"{canonical} has {len(inflight)} write(s) in flight; run "
                    f"{run_id} cannot take it. First: {w['kind']} {w['detail']!r} "
                    f"admitted {w['admitted_at']} by {w['owner']} "
                    f"(owner looks {_owner_state(w['owner'])}). If that writer "
                    f"is gone, an operator releases it with "
                    f"clear_write_admission(db, {w['id']}, evidence=...) — it "
                    f"does not expire on its own.")
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
        except BaseException:
            conn.rollback()
            raise


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
    # A non-string is not a path. Guards against a caller (or a test double)
    # handing back something path-shaped only by accident; str() of it would be
    # an absolute-looking string no filesystem knows.
    if isinstance(path, str) and path.strip():
        return path
    if (info.get("repo_type") or "") == "none":
        return None
    return str(datadir.projects_dir() / project_id)


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_usable_run_id(run_id) -> str:
    """A run id becomes a directory name and a branch name. Check it is one.

    `worktrees_dir() / run_id` with a `..` in it leaves the worktree root, and
    `codex/run/<run_id>` with a space in it is not a branch git will create —
    the second is how this was noticed (a caller handing through a placeholder
    produced `fatal: not a valid branch name` from deep inside provisioning).
    Refusing here names the actual problem, and refusing at all is what keeps
    the first case from being interesting.
    """
    if not isinstance(run_id, str) or not _SAFE_RUN_ID.match(run_id):
        raise IsolationUnavailable(
            f"{run_id!r} is not a usable run id: it becomes a directory under "
            f"the worktree root and a branch name, so it must be a short "
            f"string of letters, digits, dot, dash or underscore.")
    return run_id


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
    _require_usable_run_id(run_id)
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
    if source and not _is_git_repo(source):
        # A project whose repository has not been created YET is a normal state
        # (registration and launch are separate operations), and the ordinary
        # workspace bootstrap is what creates it. Run that first — the same
        # setup a launch performs, not a git init invented here — and then ask
        # again.
        source = _bootstrap_source(db, project_id, source)
    if not source or not _is_git_repo(source):
        # FAIL CLOSED. This used to record direct mode with the reason attached,
        # and the independent review showed why that was wrong: the fallback
        # took no lease, so two code runs on the same non-repository path both
        # resolved to it with nothing refusing either, and `repo_apply` copies
        # its step output into the tree BEFORE it runs `git add` — so the
        # "it will fail loudly at the commit" argument protected the history and
        # not the files. If the path is git-inited later, the full cross-staging
        # hazard returns on a pair of runs holding no lease at all.
        #
        # No record is written. A row saying a run is working somewhere it is
        # not is worse than no row: resolution refuses a record-less run of this
        # deployment, so the run cannot be handed a root — and a copy needs a
        # root.
        raise IsolationUnavailable(
            f"run {run_id} produces code and its project {project_id!r} has no "
            f"git repository at {source!r} (bootstrap did not produce one). "
            f"Refusing to run it in a directory nothing owns; create the "
            f"repository, or launch this run in explicit direct mode, which "
            f"leases the checkout.")
    return _provision_tree(db, run_id=run_id, project_id=project_id,
                           config_name=config_name, source=source,
                           mode=MODE_WORKTREE)


def _bootstrap_source(db, project_id: str, source: str) -> str:
    """Let the ordinary workspace setup create the repository, then re-answer.

    Isolation must not invent a repository layout of its own: `setup_workspace`
    is what a launch calls, it knows what `new` / `clone` / `existing` mean, and
    it refuses the cases it should refuse (an `existing` path that is not a git
    repository stays a refusal, which is the correct answer for a project that
    was told the repository already exists).

    Best effort by design: a failure here is not the decision. It leaves the
    source exactly as it was and the caller fails closed on the next line.
    """
    try:
        info = db.get_repo_info(project_id) or {}
    except Exception:
        return source
    repo_type = (info.get("repo_type") or "").strip()
    if repo_type not in ("new", "clone"):
        return source
    try:
        from api.dependencies import get_workspace_manager
        get_workspace_manager().setup_workspace(
            project_id, repo_type=repo_type, repo_path=info.get("repo_path"),
            repo_url=info.get("repo_url"))
    except Exception:
        import logging
        logging.getLogger("aitelier.isolation").warning(
            "workspace bootstrap failed for %s before isolation", project_id,
            exc_info=True)
    return source


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


TERMINAL_RUN_STATUSES = ("completed", "failed")


def reconcile_run_lease(db, sf, run_id: str) -> tuple[bool, str]:
    """Release this run's lease if — and only if — the run is over and quiet.

    Two conditions, both verified against the engine, neither inferred:

    * the run is **terminal** (`completed`/`failed`). `paused` is a run waiting
      for a person and `running` is a run; a lease released under either hands
      the checkout to somebody else while the holder is still working in it;
    * **no admitted operation remains**. Terminal status is not quiescence:
      skillflow admits an operation before it can be called off, so a cancelled
      run can be `failed` while a `git commit` its child process started is
      still landing. A lost owner counts as admitted here, deliberately — the
      cancellation contract says a dead owner establishes nothing about the
      effects of the operation it started.

    Anything it cannot verify — a run the engine does not know, a query that
    raised — RETAINS the lease and says why. The cost of retaining wrongly is a
    checkout an operator must release by hand; the cost of releasing wrongly is
    two writers in one tree, which is the defect this whole mechanism exists to
    remove.

    No skillflow call happens inside an AItelier transaction and no AItelier
    connection is open across one: the engine is read first, the row is written
    after. Idempotent — a run with no lease is a no-op, so completion, cancel
    and startup reconciliation can all call it for the same run.
    """
    with db.get_connection() as conn:
        held = conn.execute(
            "SELECT canonical_checkout FROM checkout_leases WHERE run_id = ?",
            (run_id,)).fetchone()
    if held is None:
        return False, f"no lease is held by run {run_id}"

    try:
        row = sf.get_run(run_id)
    except Exception as e:
        return False, (f"could not read run {run_id} from the engine "
                       f"({type(e).__name__}: {e}); lease retained")
    if row is None:
        return False, (f"run {run_id} is unknown to the engine; lease retained "
                       f"— an unknown run is not a finished one")
    status = (row.get("status") if isinstance(row, dict) else row["status"]) or ""
    if status not in TERMINAL_RUN_STATUSES:
        return False, (f"run {run_id} is {status!r}, not terminal "
                       f"(paused is a run waiting for a person); lease retained")

    try:
        audit = sf.audit_operation_owners(run_id) or {}
    except Exception as e:
        return False, (f"could not count admitted operations for {run_id} "
                       f"({type(e).__name__}: {e}); lease retained")
    admitted = (len(audit.get("lost") or []) + len(audit.get("unknown") or [])
                + int(audit.get("alive") or 0))
    if admitted:
        return False, (f"{admitted} admitted operation(s) for run {run_id} have "
                       f"not retired; effects may still be landing, lease "
                       f"retained")

    release(db, run_id, disposition="auto_released_terminal_quiet")
    return True, (f"run {run_id} is {status} with no admitted operation; "
                  f"lease on {held['canonical_checkout']} released")


def reconcile_all_leases(db, sf) -> dict:
    """Every held lease, asked the same question. For startup.

    Needed as its own entry point because a lease outlives the process: a crash
    between "terminal" and "released" leaves a row that nothing else will ever
    revisit, and the operator sees a checkout nobody is using and everything
    refusing to touch it.
    """
    with db.get_connection() as conn:
        run_ids = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM checkout_leases ORDER BY acquired_at").fetchall()]
    released, retained = [], []
    for rid in run_ids:
        ok, why = reconcile_run_lease(db, sf, rid)
        (released.append(rid) if ok
         else retained.append({"run_id": rid, "reason": why}))
    return {"released": released, "retained": retained}


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
