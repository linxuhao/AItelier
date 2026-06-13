# core/scheduler.py
# Project-priority-first scheduler using skillflow.
# Each cron tick picks the highest-priority project with work to do,
# then advances its pipeline via skillflow (advance → claim → execute → confirm).
#
# Wakeup: submit_project/submit_task call wake_scheduler() to trigger
# an immediate tick instead of waiting for the next interval.

import asyncio
import json
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

db = get_db_manager()
ws = get_workspace_manager()


# Module-level scheduler instance for wake_scheduler()
_scheduler_instance: AsyncIOScheduler | None = None
# Per-user scheduler registry for web_api wake support
_user_scheduler_map: dict[str, AsyncIOScheduler] = {}

# P0-1: cross-process advisory lock so only ONE scheduler runs even if the API
# is (mis)launched with uvicorn --workers N. Multiple AsyncIOSchedulers polling
# the same skillflow.db race the optimistic-version UPDATE in confirm_step and
# corrupt runs ("version mismatch: expected N"). The lock file handle must stay
# open for the process lifetime to hold the lock — keep a module reference.
_scheduler_lock_fh = None


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
        from api.dependencies import _AITELIER_HOME
        lock_path = _AITELIER_HOME / "scheduler.lock"
        fh = open(lock_path, "w")
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
        if existing:
            db.delete_tasks_by_project(project_id)
        db.create_tasks_from_manifest(project_id, manifest)
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

    run_id = sf.get_or_create_run("dpe_default_v2", project_id, {
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


async def _execute_skillflow_tick(project_id: str, loop):
    """Advance the skillflow pipeline for one project by one step."""
    sf = get_skillflow()
    run_id = _get_or_create_skillflow_run(project_id)
    if not run_id:
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
    # under this many step rows.
    try:
        n_steps = len(sf.get_steps(run_id))
        if n_steps > _MAX_STEPS_PER_RUN:
            sf.fail_run(run_id, f"Aborted: exceeded {_MAX_STEPS_PER_RUN} step "
                                f"executions ({n_steps}) — likely a non-converging "
                                f"loop (e.g. a verify gate that never passes).")
            _sync_project_status_to_db(project_id)
            return
    except Exception:
        pass  # never let the guard itself break a tick

    # Phase A: Resolve next step
    next_node = sf.advance_run(run_id)
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
        steps = sf.get_steps(run["id"])
        completed = [s["step_id"] for s in steps if s["status"] == "completed"]

        completed_coarse = sorted({COARSE_MAP.get(s, s) for s in completed})
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

        # Push step + completed into aitelier.db so the TUI sees live progress.
        # AT-15: use fine-grained step_id (e.g. "t_impl") not coarse ("3")
        # so the dashboard can show "▶ Implementer" instead of "▶ PM".
        db.update_project(
            project_id,
            status=status,
            current_project_step=current_step,
            completed_project_steps=json.dumps(completed_coarse),
        )
        db.set_project_meta_state(project_id, run["status"])
        # set_completed_project_steps is a deprecated no-op stub; the real
        # write now happens via update_project above. Call kept for compat
        # in case external callers rely on its side effects.
        db.set_completed_project_steps(project_id, completed)

        # Check for tasks created by PM (step 3_review → tasks/ dir)
        existing_tasks = db.list_tasks_by_project(project_id)
        if not existing_tasks:
            _sync_task_manifest_to_db(project_id)

        # Derive per-task status from the skillflow task-loop progress so the
        # dashboard task badge isn't stuck at "pending" after tasks finish.
        _sync_task_statuses(project_id, run, sf)
    except Exception:
        pass


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
            if t["status"] != TaskStatus.COMPLETED.value:
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

    # Active run: read the task-loop index.
    try:
        row = sf._conn.execute(
            "SELECT current_index, items_json FROM skillflow_loop_state "
            "WHERE run_id = ?", (run["id"],),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return
    idx = row[0]
    for i, t in enumerate(tasks):
        if i < idx:
            want = TaskStatus.COMPLETED.value
        elif i == idx:
            want = TaskStatus.RUNNING.value
        else:
            want = TaskStatus.PENDING.value
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
