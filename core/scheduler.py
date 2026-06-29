# core/scheduler.py
# Project-priority-first scheduler using skillflow.
# Each cron tick picks the highest-priority project with work to do,
# then advances its pipeline via skillflow (advance → claim → execute → confirm).
#
# Wakeup: submit_project/submit_task call wake_scheduler() to trigger
# an immediate tick instead of waiting for the next interval.

import asyncio
import json
import threading
import time as _time
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow
from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded
from core.workspace_manager import DPE_GRAPH_NAME
from aitelier.step_labels import COARSE_MAP

# NB-1 runaway-loop guard: max total step executions before a run is force-failed.
# A normal DPE run is well under this; this only trips on a non-converging loop.
import os as _os
_MAX_STEPS_PER_RUN = int(_os.getenv("AITELIER_MAX_STEPS_PER_RUN", "300"))

# Hung-step detection: warn when a claimed step has run longer than
# timeout_seconds * this multiplier.  Detection runs on a separate periodic
# job so it fires even when the main scheduler tick is blocked by a hung call.
_HUNG_WARN_MULTIPLIER = 3
_HUNG_WARNING_COOLDOWN = 120  # seconds between repeated warnings for same step

# Module-level state: track last warning time to avoid log spam
_hung_warnings: dict[tuple, float] = {}  # (run_id, step_id, step_instance_id) -> last_warn_time

db = get_db_manager()
ws = get_workspace_manager()


# Module-level scheduler instance for wake_scheduler()
_scheduler_instance: AsyncIOScheduler | None = None
# Per-user scheduler registry for web_api wake support
_user_scheduler_map: dict[str, AsyncIOScheduler] = {}

# SF-26 / tick serialization. The interval job and the wake-on-confirm 'date'
# job are SEPARATE APScheduler jobs (per-job max_instances=1 does NOT serialize
# them), AND agent steps run in a thread-pool executor (runner.py:
# loop.run_in_executor) while inline tool steps run on the loop — so a tick's
# work spans BOTH the event loop and worker threads. A plain set + "atomic
# check-and-add" only holds for single-thread cooperative asyncio; under
# thread-pool execution two ticks raced and double-advanced the same run
# (version-mismatch reopen loops, concurrent run_tests, the 5_review deadlock).
# A per-project threading.Lock with non-blocking acquire serializes ticks across
# the loop AND threads; acquire(False) returns False for the same loop-thread
# (re-entrant tick during an await) and for any worker thread. Per project (not
# global) so multi-tenant ticks on DIFFERENT runs still proceed concurrently.
_tick_locks: dict[str, threading.Lock] = {}
_tick_locks_meta = threading.Lock()


def _get_tick_lock(project_id: str) -> threading.Lock:
    with _tick_locks_meta:
        lk = _tick_locks.get(project_id)
        if lk is None:
            lk = _tick_locks[project_id] = threading.Lock()
        return lk

# P0-1: cross-process advisory lock so only ONE scheduler runs even if the API
# is (mis)launched with uvicorn --workers N. Multiple AsyncIOSchedulers polling
# the same skillflow.db race the optimistic-version UPDATE in confirm_step and
# corrupt runs ("version mismatch: expected N"). The lock file handle must stay
# open for the process lifetime to hold the lock — keep a module reference.
_scheduler_lock_fh = None


def _scheduler_lock_path():
    """Path to the single-scheduler advisory lock file.

    Overridable via the ``AITELIER_SCHEDULER_LOCK`` env var so the test suite
    (and any isolated deployment) uses its own lock and never contends with a
    running/orphaned AItelier instance holding the production lock.
    """
    override = _os.getenv("AITELIER_SCHEDULER_LOCK")
    if override:
        return override
    from api.dependencies import _AITELIER_HOME
    return _AITELIER_HOME / "scheduler.lock"


def _acquire_scheduler_lock() -> bool:
    """Try to take the single-scheduler advisory lock (non-blocking).

    Returns True if this process should run the polling scheduler, False if
    another worker already holds it. On platforms without fcntl (e.g. Windows)
    this is a best-effort no-op that returns True.
    """
    global _scheduler_lock_fh
    if _scheduler_lock_fh is not None:
        return True  # already held by this process
    try:
        import fcntl
        fh = open(_scheduler_lock_path(), "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fh.close()
            return False
        _scheduler_lock_fh = fh  # hold it for the process lifetime
        return True
    except Exception:
        # fcntl missing or any unexpected error → don't block startup.
        return True


# Whole-process single-instance lock (data-dir scoped). Unlike the scheduler
# lock above (which gracefully degrades a 2nd uvicorn worker to read-only), this
# one is meant to FAIL-FAST the entire backend: exactly ONE AItelier backend may
# run per data directory. Host + Docker bind-mount ~/.AItelier at the same path
# → same inode → flock is mutually exclusive across host AND container. Held for
# the process lifetime via a module-level fd; the OS releases it when the process
# dies. This guarantees a stray/second backend can never silently shadow the real
# (Cloudflare-fronted) one — if the real one is down, it is unambiguously down.
_instance_lock_fh = None


def _instance_lock_path():
    """Path to the single-backend lock file (overridable via AITELIER_INSTANCE_LOCK
    so the test suite uses its own and never contends with a running instance)."""
    override = _os.getenv("AITELIER_INSTANCE_LOCK")
    if override:
        return override
    from api.dependencies import _AITELIER_HOME
    return _AITELIER_HOME / "aitelier.lock"


def acquire_instance_lock() -> bool:
    """Take the single-backend lock (non-blocking).

    Returns True if this process is the sole backend, False if another already
    holds it. Held for the process lifetime (auto-released on death). On platforms
    without fcntl this is a best-effort no-op that returns True.
    """
    global _instance_lock_fh
    if _instance_lock_fh is not None:
        return True  # already held by this process (re-entrant)
    try:
        import fcntl
        fh = open(_instance_lock_path(), "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fh.close()
            return False
        _instance_lock_fh = fh  # hold for process lifetime
        return True
    except Exception:
        return True


def wake_scheduler(owner_email: str = None):
    """Trigger an immediate scheduler tick."""
    if owner_email and owner_email in _user_scheduler_map:
        sched = _user_scheduler_map[owner_email]
        if sched and sched.running:
            sched.add_job(
                lambda: poll_and_execute_owner(owner_email),
                'date', run_date=datetime.now(),
            )
            return
    if _scheduler_instance and _scheduler_instance.running:
        _scheduler_instance.add_job(poll_and_execute, 'date', run_date=datetime.now())


def _get_event_bus():
    import core.event_bus as eb_module
    return eb_module.event_bus


# ── Backward-compat shims ──────────────────────────────────────────

def run_project_step_sync(project_id: str, step_id: str, loop=None):
    """Legacy shim: execute one planning step via skillflow.

    Kept for tests and backward compat. New code should use the
    skillflow-based _execute_skillflow_tick path directly.
    """
    from aitelier.runner import AgentStepRunner
    from skillflow.core import ClaimedStep, ClaimToken, StepResult

    sf = get_skillflow()
    run_id = _get_or_create_skillflow_run(project_id)
    if not run_id:
        return

    sf.advance_run(run_id)
    try:
        claimed = sf.claim_next_step(run_id)
    except Exception:
        return
    if claimed is None:
        return

    runner = AgentStepRunner(
        db_manager=db, workspace_manager=ws,
        agent_factory=None, prompt_assembler=None,
        event_bus=_get_event_bus(),
    )

    try:
        result = asyncio.get_event_loop().run_until_complete(
            runner.execute(claimed)
        ) if loop is None else None

        if loop is not None:
            import asyncio as _asyncio
            future = _asyncio.run_coroutine_threadsafe(runner.execute(claimed), loop)
            result = future.result(timeout=600)
    except RuntimeError:
        # No event loop — run sync in thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                lambda: asyncio.new_event_loop().run_until_complete(
                    runner.execute(claimed)
                )
            )
            result = future.result(timeout=600)

    sf.confirm_step(claimed.token, result)


def run_task_step_sync(task_id: int, task: dict, loop=None):
    """Legacy shim: execute one task step via skillflow."""
    run_project_step_sync(task["project_id"], task.get("current_step", "t_plan"), loop)


# ── Skillflow-based scheduler tick ──────────────────────────────────

def _sync_task_manifest_to_db(project_id: str):
    """Read task specs from 3/tasks/ card files and sync to DB.

    AT-26: task details are stored in individual tasks/{id}.json card files
    (full spec: requirements, interface contract, artifact, dependencies).
    tasks_manifest.json is a lightweight index (execution_order + one-line
    descriptions).  We read the card files for the full spec; fall back to
    the manifest's tasks array only when card files are absent.
    """
    try:
        import json as _json, hashlib
        ws = get_workspace_manager()
        final_3 = ws.get_final_path(project_id, "3", DPE_GRAPH_NAME)
        tasks_dir = final_3 / "tasks"
        mf = final_3 / "tasks_manifest.json"
        if not mf.exists():
            return
        manifest_data = _json.loads(mf.read_text(encoding="utf-8"))
        manifest = {
            "tasks": [],
            "execution_order": manifest_data.get("execution_order", []),
        }
        # Read full task specs from individual card files (primary source)
        if tasks_dir.exists():
            for tf in sorted(tasks_dir.glob("*.json")):
                try:
                    manifest["tasks"].append(_json.loads(tf.read_text(encoding="utf-8")))
                except Exception:
                    pass
        # Fallback: if no card files exist, use manifest's lightweight tasks array
        if not manifest["tasks"]:
            manifest["tasks"] = manifest_data.get("tasks", [])
        if not manifest["tasks"]:
            return

        # Resync only when content changed
        digest = hashlib.sha256(
            _json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        marker = final_3 / ".tasks_synced_hash"
        existing = db.list_tasks_by_project(project_id)
        if existing and marker.exists() and marker.read_text(encoding="utf-8").strip() == digest:
            return
        # Merge, preserving completed tasks. On a goal-loop re-decomposition the PM
        # often writes only the new/changed cards; a delete-all+recreate would wipe
        # the completed history from the UI ("old tasks disappear"). sync_* keeps
        # completed rows (matched by manifest_key) and only (re)creates the rest.
        db.sync_tasks_from_manifest(project_id, manifest)
        marker.write_text(digest, encoding="utf-8")
    except Exception:
        pass  # Best-effort; tasks remain file-only

def _get_or_create_skillflow_run(project_id: str) -> str | None:
    """Get the skillflow run_id for a project, creating or reactivating one if needed.

    A9 fix: skillflow's get_run_by_project filters out completed/failed
    runs. If a project was already completed, the scheduler used to
    see "no active run" and silently create a fresh one — restarting
    the whole pipeline from Step 1. This is wrong: the project is
    done. We now look at the most recent run of any status, and:
      - if it's running/paused, return as-is
      - if it's failed/reactivate, return after reactivate
      - if it's completed, return None so the caller (and the project
        status API) shows the project is done — no fresh run
    """
    sf = get_skillflow()

    # Skillflow's get_run_by_project only sees active runs; we need
    # ANY recent run (including completed) to detect the "already done"
    # case. Query skillflow_runs directly.
    conn = sf._lock.__class__ and sf._conn  # cheap accessor
    row = sf._conn.execute(
        """SELECT id, status FROM skillflow_runs
           WHERE project_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if row:
        run_id, status = row[0], row[1]
        if status in ("running", "paused"):
            return run_id
        if status == "failed":
            # NB-5: do NOT auto-reactivate failed runs on every poll. That made a
            # force-failed / runaway / aborted run resume forever on the next tick
            # (and on server restart). A failed run stays dormant; only an explicit
            # user retry (POST /api/projects/{pid}/retry, which calls
            # reactivate_run) brings it back.
            return None
        if status == "completed":
            # Pipeline already done — do NOT create a fresh run.
            return None

    # No run at all (shouldn't happen for projects that went through
    # submit_project) — create one.
    project = db.get_project(project_id)
    if not project:
        return None

    # Gate: don't create a run for projects whose meta conversation hasn't
    # finished. The meta agent sets meta_state='drafting' on create_project
    # and the approve_checkpoint handler clears it when the user approves
    # the brief. Creating a run before the brief is ready causes the first
    # Researcher (step 1) to run without a [Project Brief].
    if project.get("meta_state") == "drafting":
        return None

    config_name = project.get("config_name") or "dpe_default_v2"
    run_id = sf.get_or_create_run(config_name, project_id, {
        "project_id": project_id,
        "brief": project.get("brief", ""),
    })
    run = sf.get_run(run_id)
    if run and run["status"] == "pending":
        sf.start_run(run_id)
    return run_id


def recover_claims_on_startup():
    """Reset ALL claimed steps to pending at server startup.

    The server is a singleton (enforced by the scheduler advisory lock).
    Any step still in 'claimed' status from a previous process is
    definitively stale — the claiming process no longer exists.
    No time-based threshold needed.
    """
    sf = get_skillflow()
    try:
        stale = sf._conn.execute(
            "SELECT id, run_id, step_id FROM skillflow_steps WHERE status = 'claimed'"
        ).fetchall()
        if not stale:
            return
        with sf._lock:
            for row in stale:
                sf._conn.execute(
                    """UPDATE skillflow_steps SET status = 'pending',
                       version = version + 1, claimed_at = NULL,
                       claimed_by = NULL, updated_at = datetime('now')
                       WHERE id = ?""", (row["id"],))
                sf._conn.execute(
                    "UPDATE skillflow_runs SET current_node = NULL, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (row["run_id"],))
            sf._conn.commit()
        import logging
        logging.getLogger("aitelier.scheduler").info(
            f"Startup recovery: reset {len(stale)} stale claim(s) to pending"
        )
    except Exception:
        pass  # Best-effort; scheduler will recover via stale threshold later


def _has_active_claim(sf, run_id: str) -> bool:
    """A claimed step still within its timeout is considered in-flight.

    Uses the step node's timeout_seconds (from the graph config) as the
    guard window.  Falls back to 600 s if the resolver or node isn't
    available.  This prevents re-entrant execution of the same run when
    max_instances > 1 (interval + wake date job).

    This is an optimization, not a safety mechanism. skillflow's
    advance_run() independently detects and times out stale claims.
    The early return merely avoids wasted advance_run() calls while a
    step is healthy and executing. If this function breaks (e.g. due
    to skillflow API changes), the except clause returns False and the
    tick proceeds normally — worst case is one extra advance_run()
    call per tick, which is harmless.
    """
    try:
        row = sf._conn.execute(
            "SELECT step_id, claimed_at FROM skillflow_steps "
            "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return False

        # Look up the step node's configured timeout to use as the window.
        # Default 600 s covers all DPE steps (max configured is 300 s).
        window_s = 600
        try:
            resolver = sf._get_resolver_for_run(run_id)
            node = resolver.get_node(row["step_id"])
            if node and node.timeout_seconds > 0:
                window_s = node.timeout_seconds
        except Exception:
            pass

        # Use Python strftime (ISO 8601) to match skillflow's claimed_at format.
        # SQLite datetime() produces space-separated format which compares
        # incorrectly against the T-separated ISO timestamps skillflow stores.
        import time as _time
        threshold = _time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            _time.gmtime(_time.time() - window_s),
        )
        claimed = sf._conn.execute(
            "SELECT 1 FROM skillflow_steps "
            "WHERE run_id = ? AND status = 'claimed'"
            "  AND claimed_at > ? "
            "LIMIT 1",
            (run_id, threshold),
        ).fetchone()
        return claimed is not None
    except Exception:
        return False


# ── Hung-step detection ─────────────────────────────────────────────

async def _check_hung_claims():
    """Periodic check: warn if any claimed step exceeds its timeout window.

    Runs independently from the main scheduler tick so it fires even when
    poll_and_execute is blocked awaiting a hung LLM call.

    Detection policy:
      - A step is "hung" when its claim duration > timeout_seconds * _HUNG_WARN_MULTIPLIER.
      - We only *detect and surface* — never auto-kill. The user can restart the
        server to trigger recover_claims_on_startup.
      - Warnings are rate-limited by _HUNG_WARNING_COOLDOWN to avoid log spam.
    """
    import time as _time
    import datetime as _dt
    import logging
    logger = logging.getLogger("aitelier.scheduler")

    try:
        sf = get_skillflow()

        # Scan all running skillflow runs
        runs = sf.list_runs(status="running")
        if not runs:
            return

        for run in runs:
            run_id = run["id"]
            project_id = run.get("project_id", "unknown")

            # Find any claimed step in this run
            try:
                row = sf._conn.execute(
                    "SELECT step_id, claimed_at, step_instance_id "
                    "FROM skillflow_steps "
                    "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
                    (run_id,),
                ).fetchone()
            except Exception:
                continue
            if not row:
                continue

            # Look up the step node's configured timeout as the baseline
            window_s = 600  # default fallback
            try:
                resolver = sf._get_resolver_for_run(run_id)
                node = resolver.get_node(row["step_id"])
                if node and node.timeout_seconds > 0:
                    window_s = node.timeout_seconds
            except Exception:
                pass

            warn_threshold_s = window_s * _HUNG_WARN_MULTIPLIER

            # Compute claim duration from the ISO 8601 claimed_at timestamp
            try:
                claimed_at_dt = _dt.datetime.strptime(
                    row["claimed_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=_dt.timezone.utc)
                now_dt = _dt.datetime.now(_dt.timezone.utc)
                duration_s = (now_dt - claimed_at_dt).total_seconds()
            except Exception:
                continue

            if duration_s <= warn_threshold_s:
                continue

            # Rate-limit: don't repeat the same warning too often
            warn_key = (run_id, row["step_id"], row["step_instance_id"])
            now = _time.time()
            last_warn = _hung_warnings.get(warn_key, 0)
            if now - last_warn < _HUNG_WARNING_COOLDOWN:
                continue
            _hung_warnings[warn_key] = now

            # Garbage-collect stale entries from _hung_warnings occasioanlly
            if len(_hung_warnings) > 200:
                cutoff = now - 3600
                for k in list(_hung_warnings):
                    if _hung_warnings[k] < cutoff:
                        del _hung_warnings[k]

            duration_min = duration_s / 60.0
            logger.warning(
                f"Step may be hung: project={project_id} step={row['step_id']} "
                f"claimed for {duration_min:.0f} min "
                f"(threshold: {warn_threshold_s}s = {window_s}s timeout "
                f"× {_HUNG_WARN_MULTIPLIER}). "
                f"Restart the server if the step does not progress."
            )

            # Publish event for TUI / API consumers
            try:
                eb = _get_event_bus()
                eb.publish("step_hung_warning", {
                    "project_id": project_id,
                    "run_id": run_id,
                    "step_id": row["step_id"],
                    "step_instance_id": row["step_instance_id"],
                    "claimed_at": row["claimed_at"],
                    "duration_s": round(duration_s, 1),
                    "timeout_seconds": window_s,
                    "warn_threshold_s": warn_threshold_s,
                })
            except Exception:
                pass

    except Exception:
        pass  # Never let hung detection itself break the scheduler


async def _execute_skillflow_tick(project_id: str, loop):
    """Advance the skillflow pipeline for one project by one step.

    Serializes the real tick under a per-project threading.Lock so the interval
    job, the wake-on-confirm date job, and any thread-pool re-entry can never
    advance the same run concurrently (which double-executed steps → version
    conflicts, concurrent run_tests, deadlocks). Non-blocking acquire: if a tick
    for this project is already in flight (on the loop OR a worker thread), skip.
    """
    lock = _get_tick_lock(project_id)
    if not lock.acquire(blocking=False):
        return
    try:
        await _run_skillflow_tick(project_id, loop)
    finally:
        lock.release()


async def _run_skillflow_tick(project_id: str, loop):
    """Advance the skillflow pipeline for one project by one step."""
    sf = get_skillflow()
    run_id = _get_or_create_skillflow_run(project_id)
    if not run_id:
        # Self-heal stuck task states when the DPE run is terminal.
        # _sync_project_status_to_db marks running tasks as completed/failed
        # and bumps updated_at so the project no longer starves active
        # projects in get_next_active_project's ORDER BY updated_at ASC.
        _sync_project_status_to_db(project_id)
        return

    # Don't re-enter a run that's actively executing (in-flight guard).
    # With max_instances=1 (SF-5 fix), concurrent ticks are prevented at the
    # APScheduler level. This is a safety net for edge cases.
    if _has_active_claim(sf, run_id):
        return

    # NB-1 safety valve: bound any runaway loop regardless of root cause. If a run
    # has executed an unreasonable number of steps (e.g. a chronically-failing
    # verify gate cycling t_plan -> t_impl forever), fail the run cleanly instead
    # of hanging the scheduler indefinitely. A normal multi-task DPE run uses well
    # under this many step executions.
    #
    # Count step *executions* (claim events from the durable trace), NOT step
    # rows: an in-place loop re-claims the SAME rows hundreds of times (a tool
    # gate that never passes pushed 5_review to 479 claims while only ~27 rows
    # existed), so a row-count guard never trips on exactly the loop it's meant
    # to catch.
    try:
        n_exec = sf._conn.execute(
            "SELECT COUNT(*) FROM skillflow_trace "
            "WHERE run_id = ? AND event = 'claimed'",
            (run_id,),
        ).fetchone()[0]
        if n_exec > _MAX_STEPS_PER_RUN:
            sf.fail_run(run_id, f"Aborted: exceeded {_MAX_STEPS_PER_RUN} step "
                                f"executions ({n_exec}) — likely a non-converging "
                                f"loop (e.g. a verify gate that never passes).")
            _sync_project_status_to_db(project_id)
            return
    except Exception:
        pass  # never let the guard itself break a tick

    # Phase A: Resolve next step
    next_node = sf.advance_run(run_id)

    # Drain consecutive inline tool steps. advance_run() executes ONE inline tool
    # per call (framework mode) and returns the FOLLOWING node; when two tool
    # steps are adjacent (e.g. 5_test → 5_compile) that returned node is itself an
    # inline tool. Claiming a tool step would hand it to the agent runner, which
    # has no agent_config → "Agent config '' not found". skillflow's design is for
    # the host to re-enter advance_run so the fast-path executes it (see core.py
    # tool fast-path), so re-advance until the next node is not an inline tool.
    try:
        _resolver = sf._get_resolver_for_run(run_id)
        _drain = 0
        while next_node is not None and _drain < 20 and _resolver.is_tool(next_node):
            next_node = sf.advance_run(run_id)
            _drain += 1
    except Exception:
        pass

    if next_node is None:
        # Handle terminal states
        run = sf.get_run(run_id)
        if run["status"] in ("paused", "completed", "failed"):
            # skillflow notification bus emits checkpoint_paused / run_completed /
            # run_failed; we just sync the AItelier DB status.
            _sync_project_status_to_db(project_id)
        return

    # Phase B: Claim
    try:
        claimed = sf.claim_next_step(run_id)
    except Exception:
        _sync_project_status_to_db(project_id)
        return
    if claimed is None:
        _sync_project_status_to_db(project_id)
        return

    # Phase C+D: Execute
    from aitelier.runner import AgentStepRunner
    from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded

    runner = AgentStepRunner(
        db_manager=db,
        workspace_manager=ws,
        agent_factory=None,  # PipelineEngine creates its own
        prompt_assembler=None,
        event_bus=_get_event_bus(),
    )

    try:
        result = await runner.execute(claimed)
        sf.confirm_step(claimed.token, result)

        # Sync task manifest to DB when the PM step or its review completes.
        # FW-2: also sync on "3" so a re-run (3_review reject → 3) refreshes the
        # DB even if the new manifest is produced before the next 3_review.
        if claimed.step_id in ("3", "3_review"):
            _sync_task_manifest_to_db(project_id)
    except MaxRetriesExceeded as e:
        sf.fail_step(claimed.token, str(e), retryable=False)
    except Exception as e:
        sf.fail_step(claimed.token, str(e), retryable=True)

    # Sync project status to DB after each tick
    _sync_project_status_to_db(project_id)


def _sync_project_status_to_db(project_id: str):
    """Write skillflow run status back to AItelier DB so the UI is not stale.

    A5 fix: also push current_project_step and completed_project_steps
    on every tick, not just on terminal states. Coarse-maps fine-grained
    skillflow node IDs (t_plan, t_impl, t_verify, ...) into the coarse
    DPE step IDs ("1", "2", "3", "5") the schema was designed for.
    """
    try:
        sf = get_skillflow()
        # get_run_by_project excludes completed runs, so the completing tick would
        # return early here and the project row would keep its last "running:N"
        # status forever. Fall back to the most recent run of any status.
        run = sf.get_run_by_project(project_id)
        if not run:
            all_runs = sf.list_runs(project_id)  # newest first
            run = all_runs[0] if all_runs else None
        if not run:
            return
        # Is this a DPE-style config (task loop, coarse step mapping)?
        has_task_loop = False
        try:
            from api.dependencies import get_config_registry
            manifest = get_config_registry().get(run["graph_name"])
            has_task_loop = bool(manifest and manifest.has_task_loop)
        except Exception:
            has_task_loop = run["graph_name"] == "dpe_default_v2"

        steps = sf.get_steps(run["id"])
        completed = [s["step_id"] for s in steps if s["status"] == "completed"]
        current_step = run.get("current_node", "")

        # Derive a human-readable status label
        status = run["status"]
        if status == "paused" and current_step:
            resolver = sf._get_resolver(run["graph_name"])
            # current_node is the step AFTER the checkpoint (e.g. the review step).
            # Find the actual checkpoint step among completed steps to get its label.
            label = current_step
            if resolver:
                for s in reversed(steps):
                    if s["status"] == "completed":
                        node = resolver.get_node(s["step_id"])
                        if node and node.checkpoint:
                            label = node.checkpoint_label or s["step_id"]
                            break
            status = f"checkpoint:{label}"
        elif status == "running" and current_step:
            # AT-15: use fine-grained step_id so the dashboard shows
            # "▶ Implementer" instead of "▶ PM" for all task-loop steps.
            status = f"running:{current_step}"
        elif status == "failed":
            status = f"failed:{run.get('error_reason', 'unknown')[:80]}"

        # Push step + status into aitelier.db so the UI sees live progress.
        # AT-15: use fine-grained step_id (e.g. "t_impl") not coarse ("3").
        if has_task_loop:
            completed_coarse = sorted({COARSE_MAP.get(s, s) for s in completed})
            db.update_project(
                project_id,
                status=status,
                current_project_step=current_step,
                completed_project_steps=json.dumps(completed_coarse),
            )
        else:
            # Generic config: no coarse DPE-step mapping, no task loop.
            db.update_project(
                project_id,
                status=status,
                current_project_step=current_step,
            )
        db.set_project_meta_state(project_id, run["status"])

        if has_task_loop:
            # Check for tasks created by PM (step 3_review → tasks/ dir)
            existing_tasks = db.list_tasks_by_project(project_id)
            if not existing_tasks:
                _sync_task_manifest_to_db(project_id)
            # Derive per-task status from the skillflow task-loop progress so the
            # dashboard task badge isn't stuck at "pending" after tasks finish.
            _sync_task_statuses(project_id, run, sf)
    except Exception as e:
        import logging
        logging.getLogger("aitelier.scheduler").error(
            f"_sync_project_status_to_db failed for {project_id}: {e}",
            exc_info=True,
        )


def _sync_task_statuses(project_id: str, run: dict, sf):
    """Update aitelier.db `tasks` rows from the skillflow task-loop index.

    The DPE task loop iterates over manifest items; nothing was advancing the
    `tasks` table, so rows stayed 'pending' even after the project completed.
    We map loop progress -> task rows by order (rows are created in manifest
    order; the loop iterates that same order):
      - run completed                -> all tasks completed
      - index i: tasks[<i] completed, tasks[i] running, tasks[>i] pending
    """
    try:
        from models.schemas import TaskStatus
    except Exception:
        return
    tasks = db.list_tasks_by_project(project_id)
    if not tasks:
        return
    tasks = sorted(tasks, key=lambda t: t["id"])  # manifest insertion order

    if run["status"] == "completed":
        for t in tasks:
            # Don't resurrect SUPERSEDED audit rows into completed.
            if t["status"] not in (TaskStatus.COMPLETED.value,
                                   TaskStatus.SUPERSEDED.value):
                db.complete_task(t["id"])
        return
    if run["status"] == "failed":
        # AT-16: mark any running tasks as failed so the dashboard
        # doesn't show them as "running" forever after a run failure.
        for t in tasks:
            if t["status"] == TaskStatus.RUNNING.value:
                db.update_task_status(t["id"], TaskStatus.FAILED.value)
        return
    if run["status"] == "paused":
        return  # leave task states as-is (no task-loop progress to sync)

    # Active run: read the task-loop state.
    # Use completed_items (v2 set-based tracking) to compute the index.
    # current_index is deprecated and may be stale/absent.
    try:
        row = sf._conn.execute(
            "SELECT current_index, completed_items, items_json FROM skillflow_loop_state "
            "WHERE run_id = ?", (run["id"],),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return
    # Prefer completed_items (set of done task keys) over current_index.
    import json as _json
    try:
        completed = _json.loads(row[1]) if row[1] else []
    except Exception:
        completed = []
    completed_keys = set(completed)
    idx = len(completed)
    try:
        items = _json.loads(row[2]) if row[2] else []
    except Exception:
        items = []
    active_keys = set(items)              # keys in the current loop plan
    current_key = items[idx] if 0 <= idx < len(items) else None

    # Terminal states are immutable audit history — never downgrade them.
    # (This is the goal-loop data-loss fix: previously a positional sync
    # downgraded a COMPLETED task to PENDING when the loop reset
    # completed_items, after which the manifest resync deleted it.)
    TERMINAL = {TaskStatus.COMPLETED.value, TaskStatus.SUPERSEDED.value,
                TaskStatus.FAILED.value}

    # Supersede-and-clone: a COMPLETED task whose key is still planned (in the
    # loop's item list) but has dropped out of completed_items is being RE-RUN
    # by a goal-loop. Archive the prior attempt as SUPERSEDED and clone a fresh
    # PENDING re-run row, so the completed history is preserved (auditable
    # generations) instead of being overwritten. The clone owns row creation, so
    # this is correct regardless of when the manifest resync runs.
    keyed = all(t.get("manifest_key") for t in tasks)
    if keyed and active_keys:
        nonterminal_keys = {t["manifest_key"] for t in tasks
                            if t["status"] in (TaskStatus.PENDING.value,
                                               TaskStatus.RUNNING.value)}
        for t in tasks:
            key = t["manifest_key"]
            if (t["status"] == TaskStatus.COMPLETED.value
                    and key in active_keys and key not in completed_keys
                    and key not in nonterminal_keys):  # idempotent: no live re-run row yet
                db.supersede_task(t["id"])

    for i, t in enumerate(tasks):
        if t["status"] in TERMINAL:
            continue  # immutable — never downgrade
        if keyed:
            key = t["manifest_key"]
            if key in completed_keys:
                want = TaskStatus.COMPLETED.value
            elif key == current_key:
                want = TaskStatus.RUNNING.value
            else:
                want = TaskStatus.PENDING.value
        else:  # legacy rows without manifest_key: positional fallback
            want = (TaskStatus.COMPLETED.value if i < idx
                    else TaskStatus.RUNNING.value if i == idx
                    else TaskStatus.PENDING.value)
        if t["status"] != want:
            if want == TaskStatus.COMPLETED.value:
                db.complete_task(t["id"])
            else:
                db.update_task_status(t["id"], want)



# ── Polling ──────────────────────────────────────────────────────────

async def poll_and_execute():
    """Project-priority-first scheduler using skillflow."""
    import asyncio
    loop = asyncio.get_running_loop()

    project = db.get_next_active_project()
    if not project:
        return
    await _execute_skillflow_tick(project["project_id"], loop)


async def poll_and_execute_demo():
    """Demo mode: FIFO ordering."""
    import asyncio
    loop = asyncio.get_running_loop()

    project = db.get_next_active_project(fifo=True)
    if not project:
        return
    await _execute_skillflow_tick(project["project_id"], loop)


async def poll_and_execute_owner(owner_email: str):
    """Same as poll_and_execute but scoped to a single user's projects."""
    import asyncio
    loop = asyncio.get_running_loop()

    project = db.get_next_active_project(owner_email=owner_email)
    if not project:
        return
    await _execute_skillflow_tick(project["project_id"], loop)


# ── Scheduler management ────────────────────────────────────────────

def _get_default_settings() -> dict:
    """Read scheduler settings from DB, with defaults."""
    try:
        settings = db.get_scheduler_settings()
        return settings if settings else {"scheduler_type": "interval", "scheduler_interval": 5}
    except Exception:
        return {"scheduler_type": "interval", "scheduler_interval": 5}


def start_scheduler(demo: bool = False, owner_email: str = None):
    """Start the APScheduler for the CLI backend (single-user).

    P0-1: guarded by a cross-process advisory lock. If another uvicorn worker
    already runs the scheduler, this returns a no-op handle instead of starting
    a second competing scheduler (which would race and corrupt runs).
    """
    global _scheduler_instance
    if not owner_email and not _acquire_scheduler_lock():
        import logging
        logging.getLogger("aitelier.scheduler").warning(
            "Another worker already holds the scheduler lock; not starting a "
            "second scheduler in this process. Run the API with --workers 1 to "
            "avoid this — the in-process scheduler is single-instance by design."
        )
        return _NoopScheduler()
    settings = _get_default_settings()
    scheduler = AsyncIOScheduler()
    _add_scheduler_job(scheduler, settings, owner_email=owner_email, demo=demo)
    scheduler.start()
    if owner_email:
        _user_scheduler_map[owner_email] = scheduler
    else:
        _scheduler_instance = scheduler
    return scheduler


class _NoopScheduler:
    """Stand-in returned when this worker did not win the scheduler lock.

    Quacks like the bits of AsyncIOScheduler that lifespan/shutdown touch so
    callers don't need to special-case it.
    """
    running = False

    def shutdown(self, wait: bool = False):
        pass

    def get_jobs(self):
        return []


def start_user_scheduler(owner_email: str, settings: dict):
    """Start a per-user scheduler (web_api normal mode)."""
    sched = AsyncIOScheduler()
    _add_scheduler_job(sched, settings, owner_email=owner_email)
    sched.start()
    _user_scheduler_map[owner_email] = sched
    return sched


def stop_scheduler(owner_email: str = None):
    """Shut down a scheduler."""
    if owner_email:
        sched = _user_scheduler_map.pop(owner_email, None)
    else:
        global _scheduler_instance
        sched = _scheduler_instance
        _scheduler_instance = None
    if sched and sched.running:
        sched.shutdown(wait=False)


def reschedule_scheduler(scheduler: AsyncIOScheduler, settings: dict = None,
                         owner_email: str = None, demo: bool = False):
    """Remove old jobs and re-add with new settings."""
    if settings is None:
        settings = _get_default_settings()
    if scheduler and scheduler.running:
        for job in scheduler.get_jobs():
            scheduler.remove_job(job.id)
        _add_scheduler_job(scheduler, settings, owner_email=owner_email, demo=demo)


def _add_scheduler_job(scheduler: AsyncIOScheduler, settings: dict,
                       owner_email: str = None, demo: bool = False):
    """Add a poll_and_execute job based on settings dict."""
    if demo:
        job_func = poll_and_execute_demo
    elif owner_email:
        job_func = lambda: poll_and_execute_owner(owner_email)
    else:
        job_func = poll_and_execute
    scheduler_type = settings.get("scheduler_type", "interval")

    if scheduler_type == "cron":
        cron_expr = settings.get("scheduler_cron", "")
        if cron_expr:
            parts = cron_expr.split()
            scheduler.add_job(
                job_func, 'cron',
                minute=parts[0], hour=parts[1], day=parts[2],
                month=parts[3], day_of_week=parts[4],
            )
    else:
        interval = int(settings.get("scheduler_interval", 5))
        scheduler.add_job(
            job_func, 'interval', seconds=interval,
            misfire_grace_time=60,  # first tick may run a full LLM call (~30s)
            max_instances=1,  # SF-5: prevent concurrent ticks racing on same run
                              # (wake-on-confirm + interval both hitting advance_run
                              # caused step version conflicts and infinite retry loops)
        )

    # Hung-step detection: runs on a separate periodic job so it fires even
    # when the main tick is blocked awaiting a hung LLM call.  Lightweight
    # (only SQL queries), so a 30 s interval is safe.
    scheduler.add_job(
        _check_hung_claims, 'interval', seconds=30,
        max_instances=1,
    )


# ── Wake-on-confirm hook ──────────────────────────────────────────
# Patch SkillFlow.confirm_step once at import time so that any step completion
# wakes the scheduler immediately instead of waiting for the next interval.
# This is the FW-4 fix: without this, the 5s default interval still costs up
# to 5s of dead air between steps when an agent finishes mid-tick.
def _patch_skillflow_wake():
    try:
        from skillflow.core import SkillFlow
    except Exception:
        return
    if getattr(SkillFlow.confirm_step, "_aitelier_wake_patched", False):
        return  # idempotent: already patched in this process
    _orig_confirm = SkillFlow.confirm_step

    def _confirm_with_wake(self, token, result):
        try:
            _orig_confirm(self, token, result)
        finally:
            try:
                wake_scheduler()
            except Exception:
                pass

    _confirm_with_wake._aitelier_wake_patched = True
    SkillFlow.confirm_step = _confirm_with_wake


_patch_skillflow_wake()
