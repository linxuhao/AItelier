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
from skillflow.exceptions import RequiredContextMissing
from skillflow.identity import owner_is_dead
from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow
from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded
from core.workspace_manager import DPE_GRAPH_NAME
from aitelier.step_labels import COARSE_MAP
from core.orphan_dbg import odbg as _odbg

# NB-1 runaway-loop guard: max total step executions before a run is force-failed.
# A normal DPE run is well under this; this only trips on a non-converging loop.
import os as _os
_MAX_STEPS_PER_RUN = int(_os.getenv("AITELIER_MAX_STEPS_PER_RUN", "300"))
# Per-instance companion: how often ONE step instance may be re-claimed before the
# run is force-failed. In-framework re-claims of a single instance are bounded by
# max_retries (3); anything far above that is a resumed terminal state.
_MAX_CLAIMS_PER_INSTANCE = int(_os.getenv("AITELIER_MAX_CLAIMS_PER_INSTANCE", "20"))

# ── Provider quota hold ──────────────────────────────────────────────────────
# A spent usage window is the one provider failure that is BOTH certain to clear
# and certain not to clear soon, and the scheduler had no way to express that.
# Every tick re-claimed, every claim burned a step retry against an API that
# could not answer, and after max_retries the run was `failed` — permanently,
# for a condition with a published expiry. Live on 2026-08-26: the jinyong-jianghu
# run died at 00:59 on a quota that reopened at 03:18.
#
# So: park. One process-wide instant (the quota belongs to the API key, not to a
# project), consulted before any project is advanced. The hold is capped so a
# mis-parsed or hostile timestamp can idle the scheduler for hours at most, not
# forever, and every held tick says so in the tick log — "nothing is moving" must
# stay answerable from that one file.
# Deliberately in-process, not persisted: a restart during an outage costs one
# claim, one retry and a re-established hold — self-healing and bounded, which
# is cheaper than a durable hold that can outlive the condition it describes.
_QUOTA_HOLD_UNTIL = 0.0          # epoch seconds; 0 = not held
_QUOTA_HOLD_REASON = ""
_QUOTA_HOLD_MAX = 6 * 3600       # never park longer than this on one report
_QUOTA_HOLD_FALLBACK = 300       # provider named no reset time
_QUOTA_HOLD_GRACE = 30           # don't fire on the exact tick of the reset


def _note_quota_exhausted(err) -> float:
    """Park the scheduler until the provider's window reopens. Returns the hold."""
    global _QUOTA_HOLD_UNTIL, _QUOTA_HOLD_REASON
    import logging
    from datetime import timezone
    from core.llm_quota import quota_reset_at

    reset = quota_reset_at(err)
    if reset is not None:
        until = reset.timestamp() + _QUOTA_HOLD_GRACE
    else:
        until = _time.time() + _QUOTA_HOLD_FALLBACK
    # Clamp: a past instant means the window already reopened (nothing to hold),
    # a wild future one is not trusted further than the cap.
    until = min(until, _time.time() + _QUOTA_HOLD_MAX)

    # The escaping error is the LAST candidate's — the pay-as-you-go tail of the
    # route — so its reset instant is the latest of the bunch. Parking on it
    # idles past the moment the FIRST plan reopens, which is the whole failure
    # this feature exists to avoid, merely postponed. So shorten it to the
    # earliest reopening among the endpoints that could serve THIS model.
    #
    # Scoped to that model's own candidates, which the gateway stamps on the
    # exception, because the process-wide cooldown map spans every model: taking
    # the minimum over all of it let a 5-minute window on the vision judge cut a
    # 5-hour hold on flash, and the run then woke into a still-spent plan once
    # per window until max_retries killed it — exactly the outage this exists to
    # prevent. No stamp (an error from somewhere else) means no shortening.
    try:
        from core.ai_router import endpoint_cooldowns
        candidates = getattr(err, "_aitelier_candidates", None)
        if candidates:
            cooling = endpoint_cooldowns()
            mine = [v for k, v in cooling.items() if k in candidates]
            if mine:
                # `+ _QUOTA_HOLD_GRACE` again, not a bare `min`: the cooldown
                # map stores the RAW reset instant (`_note_endpoint_spent`),
                # while `until` above is grace-padded. Comparing the two forms
                # discarded the grace on every single call, not occasionally —
                # `_note_endpoint_spent` runs before the escaping candidate's
                # `return False`, so that candidate is always in the map at
                # exactly the instant the error names, and always in
                # `_candidates`. The scheduler then woke on the precise tick of
                # the reset, which is the one case the grace was added for.
                until = min(until, min(mine) + _QUOTA_HOLD_GRACE)
    except Exception:                                    # noqa: BLE001
        pass    # no gateway state to consult: the error's own instant stands

    if until <= _time.time():
        return 0.0
    # `max`, so a hold can be extended but never cut short by a later call. The
    # shortening above is therefore WITHIN one call only: two steps failing
    # concurrently in different executor threads, a 5-hour report landing before
    # a 5-minute one, leaves the process parked for 5 hours. Kept deliberately —
    # the hold stops ALL ticks, so releasing it while any model is still spent
    # sends every project back into the wall. Over-waiting costs latency;
    # under-waiting costs the retry budget that the run needs to survive.
    _QUOTA_HOLD_UNTIL = max(_QUOTA_HOLD_UNTIL, until)
    _QUOTA_HOLD_REASON = str(err)[:200]
    logging.getLogger("aitelier.scheduler").warning(
        "provider quota exhausted — holding all ticks for %.0fs (until %s UTC): %s",
        _QUOTA_HOLD_UNTIL - _time.time(),
        datetime.fromtimestamp(_QUOTA_HOLD_UNTIL, timezone.utc).strftime("%H:%M:%S"),
        _QUOTA_HOLD_REASON)
    return _QUOTA_HOLD_UNTIL


def _quota_hold_remaining() -> float:
    """Seconds left on the hold; 0 when the scheduler may run."""
    return max(0.0, _QUOTA_HOLD_UNTIL - _time.time())

# Hung-step detection: warn when a claimed step has run longer than
# timeout_seconds * this multiplier.  Detection runs on a separate periodic
# job so it fires even when the main scheduler tick is blocked by a hung call.
_HUNG_WARN_MULTIPLIER = 3
_HUNG_WARNING_COOLDOWN = 120  # seconds between repeated warnings for same step

# Module-level state: track last warning time to avoid log spam
_hung_warnings: dict[tuple, float] = {}  # (run_id, step_id, step_instance_id) -> last_warn_time

# Checkpoint SSE emission guard: track emitted (run_id, step_id) pairs so each
# pause emits exactly once. Cleared when the run leaves "paused", so rejection
# loop-backs and task-goal loops re-pausing the same step re-emit.
_checkpoint_emitted: set[tuple] = set()

# ORPHAN-DBG (temporary diagnostic — remove after the orphaned-claim root cause
# is pinned). `_odbg` (shared with aitelier/runner.py via core.orphan_dbg) mirrors
# every line to stdout AND a durable ~/.AItelier/orphan_dbg.log.
_orphan_snapshots: set = set()  # (run_id, step_instance_id) already-dumped, for dedup


db = get_db_manager()
ws = get_workspace_manager()


# Module-level scheduler instance for wake_scheduler()
_scheduler_instance: AsyncIOScheduler | None = None
# Run ids whose generated pipeline has already been registered on completion
# (fire-once guard for the scheduler-owned generator registration hook).
_registered_gen_runs: set[str] = set()
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
    from core import datadir
    return datadir.aitelier_home() / "scheduler.lock"


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
    from core import datadir
    return datadir.aitelier_home() / "aitelier.lock"


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
        # A persistent failure here silently stalls the run (no claim → no
        # execution) with no other signal.
        import logging
        logging.getLogger("aitelier.scheduler").warning(
            "claim_next_step failed for run %s", run_id, exc_info=True)
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
        # Read step 3 from the project's ACTUAL config, not the hardcoded base:
        # a composed addon config (e.g. dpe_game = dpe_default_v2 + game_harness)
        # writes its steps under `.../<config_name>/3/`, so keying on
        # DPE_GRAPH_NAME looked in the wrong dir, found no manifest, and left the
        # project's task list empty in the UI for every non-base config.
        _proj = db.get_project(project_id)
        _graph = (_proj.get("config_name") if _proj else None) or DPE_GRAPH_NAME
        final_3 = ws.get_final_path(project_id, "3", _graph)
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
    project = db.get_project(project_id)
    if not project:
        return None
    config_name = project.get("config_name") or "dpe_default_v2"

    # Skillflow's get_run_by_project only sees active runs; we need
    # ANY recent run (including completed) to detect the "already done"
    # case. Query skillflow_runs directly.
    #
    # Scoped to THIS project's pipeline graph. Without the graph_name filter a
    # project's `meta_conversation` run counts as "the project's most recent
    # run" — and a completed meta run then reads as "pipeline already done", so
    # the tick returns None forever and the real pipeline never advances. That
    # is invisible in the normal ordering (meta is created first, so the DPE run
    # is always newer), and fires the moment a meta run is created or re-run
    # after the pipeline run exists. Live: jinyong-turn, 2026-08-22 — a DPE run
    # created at 11:45 sat at gh_scaffold while a meta run finalized at 11:47
    # answered this query.
    row = sf._conn.execute(
        """SELECT id, status FROM skillflow_runs
           WHERE project_id = ? AND graph_name = ?
           ORDER BY created_at DESC LIMIT 1""",
        (project_id, config_name),
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

    # Gate: don't create a run for projects whose meta conversation hasn't
    # finished. The meta agent sets meta_state='drafting' on create_project
    # and the approve_checkpoint handler clears it when the user approves
    # the brief. Creating a run before the brief is ready causes the first
    # Researcher (step 1) to run without a [Project Brief].
    if project.get("meta_state") == "drafting":
        return None

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
    """Is somebody still working on a step of this run?

    Ask the owner, not the clock. Every claim records the process that made it
    — `skillflow_steps.claimed_by` carries host, pid, boot id, pid namespace
    and process start time — so "is the worker still working?" has a real
    answer, and the answer does not expire.

    It used to be a stopwatch: a claim counted as in-flight only while it was
    younger than the node's `timeout_seconds` (600 s otherwise). Past that the
    tick walked straight into a step that was still executing, because the only
    thing the guard actually knew was how long ago the claim was made. Agent
    steps legitimately run past ten minutes here (measured: 1367 s), so the
    window expired on healthy work routinely.

      alive   → in flight. The tick returns and the next one asks again.
      dead    → NOT in flight, whatever the clock says: nothing is running, so
                let the tick proceed and let skillflow's reaper reset the row.
      unknown → no identity to probe (a pre-1.5.36 claim, another kernel boot,
                no /proc). Fall back to the old window, which is all there was
                before and is still the only thing available there.
    """
    try:
        row = sf._conn.execute(
            "SELECT step_id, claimed_at, claimed_by FROM skillflow_steps "
            "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
            (run_id,),
        ).fetchone()
        if not row:
            return False

        dead = owner_is_dead(row["claimed_by"])
        if dead is not None:
            return not dead

        # Unknown owner — the pre-identity fallback, unchanged.
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
    """Periodic supervisor: RECLAIM dead claims, warn about merely tardy ones.

    Runs independently from the main scheduler tick so it fires even when
    poll_and_execute is blocked awaiting a hung LLM call. That independence was
    the right instinct and, until now, entirely wasted: skillflow's reaper
    (recover_stale_claims) was called from exactly ONE place — the top of
    advance_run — and _run_skillflow_tick returns before reaching it on five
    paths (run_start_failed, no_run, active_claim, and both runaway valves).
    Combine that with one-project-per-tick and the consequence is that whenever
    the single project the poller picked merely LOOKED in-flight, nothing was
    swept anywhere in the system that tick — including a genuinely dead claim on
    a different project. So the loop with the right cadence could not reclaim and
    the code that could reclaim had the wrong cadence, and the only cure left in
    the runbook was "restart the container" (used three times on 2026-08-22).

    Policy — two signals, never merged:
      - DEAD: the process named in `claimed_by` is gone — checked against the OS,
        not the clock (skillflow.identity). Reaped back to 'pending' — reap is not
        fail: the step did not fail, its owner vanished, and those are different
        facts. A claim whose owner cannot be probed at all (pre-identity row,
        other kernel boot, no /proc) still falls back to the silence lease.
      - TARDY: alive, but claimed for longer than timeout_seconds *
        _HUNG_WARN_MULTIPLIER. There is nothing to reclaim (it is alive), so it is
        only warned about — the early signal that a step is going long, which is
        worth keeping precisely because the reaper will never fire on it.
        This sentence was already written here and was already untrue: the reaper
        DID fire on live steps, because it was reading the activity clock, and an
        agent step inside a single ten-minute LLM call emits no trace to
        heartbeat with. 8 reclaims against 13 t_impl executions on one run, each
        one throwing away work that was still being done.
      The reap runs FIRST, so anything the warning scan still finds 'claimed' is
      by construction under the reclaim threshold.
      - Warnings are rate-limited by _HUNG_WARNING_COOLDOWN to avoid log spam.
    """
    import time as _time
    import datetime as _dt
    import logging
    logger = logging.getLogger("aitelier.scheduler")

    try:
        sf = get_skillflow()

        # Reap first — this is the authority the loop was missing. skillflow's
        # reaper is safe to run on a fixed interval (activity clock + the
        # never-stale tool guard), it just had nowhere to run FROM. Returns the
        # run ids it reset to pending.
        try:
            reclaimed = sf.recover_stale_claims(sf._stale_threshold) or []
        except Exception as e:
            reclaimed = []
            logger.warning("stale-claim reclaim failed: %s", e)
        for _rid in reclaimed:
            try:
                _pid = (sf.get_run(_rid) or {}).get("project_id") or "unknown"
            except Exception:
                _pid = "unknown"
            # Into the tick log next to the other outcomes: that file exists so
            # "nothing is moving" is answerable from one place, and a reclaim is
            # the single most load-bearing thing that can happen to a stuck run.
            tick_log(_pid, "reclaimed", run=str(_rid)[:8], reason="owner_gone")
            logger.warning(
                "RECLAIMED abandoned claim: project=%s run=%s — the process "
                "that held it is gone; reset to pending, no restart needed",
                _pid, _rid)

        # Scan all running skillflow runs
        runs = sf.list_runs(status="running")
        if not runs:
            return

        for run in runs:
            run_id = run["id"]
            project_id = run.get("project_id", "unknown")

            # Find any claimed step in this run. `step_instance_id` IS the
            # skillflow_steps row id — skillflow only spells it that way in
            # skillflow_trace (core.py: "step_instance_id": step_row["id"]).
            # skillflow_steps has no such column, so this SELECT raised
            # OperationalError on EVERY run and the bare `except: continue`
            # below swallowed it, silently reducing the entire supervisor —
            # hung warnings AND the orphan snapshot — to a no-op. Which is the
            # real reason nobody ever caught the orphan in the act.
            try:
                row = sf._conn.execute(
                    "SELECT id AS step_instance_id, step_id, claimed_at "
                    "FROM skillflow_steps "
                    "WHERE run_id = ? AND status = 'claimed' LIMIT 1",
                    (run_id,),
                ).fetchone()
            except Exception as e:
                logger.warning("hung-claim scan failed for run %s: %s", run_id, e)
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

            # ORPHAN-DBG: trace-recency heartbeat. A claimed step whose latest
            # skillflow_trace row is stale = executor dead/zombie/hung. Dump a
            # ONE-SHOT forensic snapshot (all thread stacks) BEFORE the reaper
            # above resets it to pending, so the next occurrence is captured in
            # the act rather than after the fact.
            #
            # This was a hardcoded 120 under a comment claiming re-dispatch at
            # "~300s" — skillflow's DEFAULT, not the threshold AItelier actually
            # passes to SkillFlow(). Against the real value the trap sprang
            # strictly AFTER the event it exists to catch, every single time. A
            # restated constant drifts from the configured one, so derive it:
            # half the live window is comfortably before the reap and still
            # several 25s heartbeat intervals of silence, so it cannot fire on a
            # merely slow step. Floor at 50s for the same reason.
            try:
                _ORPHAN_TRACE_STALE_S = max(
                    50.0, float(getattr(sf, "_stale_threshold", 300)) / 2.0)
                _osnap_key = (run_id, row["step_instance_id"])
                if _osnap_key not in _orphan_snapshots:
                    rows = sf.trace_query(run_id,
                        "SELECT MAX(created_at) FROM skillflow_trace "
                        "WHERE run_id = ? AND step_id = ?",
                        (run_id, row["step_id"]))
                    _lt = rows[0] if rows else None
                    _last_trace = _lt[0] if _lt else None
                    if _last_trace:
                        _lt_dt = _dt.datetime.strptime(
                            _last_trace[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=_dt.timezone.utc)
                        _trace_age = (now_dt - _lt_dt).total_seconds()
                        if _trace_age > _ORPHAN_TRACE_STALE_S:
                            _orphan_snapshots.add(_osnap_key)
                            import sys as _sys, traceback as _tb, threading as _th
                            _lock_held = _get_tick_lock(project_id).locked()
                            _odbg(f"ORPHAN/HUNG SNAPSHOT project={project_id} "
                                  f"step={row['step_id']} inst={row['step_instance_id']} "
                                  f"claim_age={duration_s:.0f}s trace_age={_trace_age:.0f}s "
                                  f"tick_lock_held={_lock_held} last_trace={_last_trace}")
                            _frames = _sys._current_frames()
                            for _to in _th.enumerate():
                                _fr = _frames.get(_to.ident)
                                if _fr is not None:
                                    _odbg(f"  thread {_to.name} ({_to.ident}):\n"
                                          + "".join(_tb.format_stack(_fr)))
            except Exception as _e:
                _odbg(f"orphan-snapshot check failed: {_e}")

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
                f"Still heartbeating, so the reaper leaves it alone — slow, not "
                f"dead. No restart needed either way."
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


def _tick_lock_held(project_id: str) -> bool:
    """Is a tick for this project already running? Read-only — never acquires."""
    return _get_tick_lock(project_id).locked()


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
        tick_log(project_id, "locked")      # a tick for this project is in flight
        return
    try:
        await _run_skillflow_tick(project_id, loop)
    finally:
        lock.release()


async def _run_skillflow_tick(project_id: str, loop):
    """Advance the skillflow pipeline for one project by one step."""
    held = _quota_hold_remaining()
    if held > 0:
        # Before the run is even resolved: claiming a step we cannot execute is
        # what spends the retry budget, so the cheapest correct move is not to
        # claim. The project stays active and is re-picked normally afterwards.
        tick_log(project_id, "quota_hold", remaining=f"{held:.0f}s",
                 reason=_QUOTA_HOLD_REASON[:120])
        return
    sf = get_skillflow()
    try:
        run_id = _get_or_create_skillflow_run(project_id)
    except Exception as e:
        # A run that dies inside create_run has no run row, so no trace and no
        # failed status to enrich — the project just sits at 'planning' forever
        # while every tick re-raises into APScheduler, and the only record is a
        # container log line. (A config with two max_loop edges on one (from, to)
        # pair does exactly this: UNIQUE constraint on skillflow_edge_counts.)
        # Write a terminal status so the user sees the reason and the project
        # stops being re-picked by get_next_active_project's planning guard.
        import logging
        logging.getLogger("aitelier.scheduler").error(
            f"could not start a skillflow run for {project_id}: {e}", exc_info=True)
        try:
            db.update_project(project_id,
                              status=f"failed:could not start run — {e}"[:160])
        except Exception:
            pass    # a DB write failure here must not mask the original error
        tick_log(project_id, "run_start_failed", error=str(e)[:160])
        return
    if not run_id:
        # Self-heal stuck task states when the DPE run is terminal.
        # _sync_project_status_to_db marks running tasks as completed/failed
        # and bumps updated_at so the project no longer starves active
        # projects in get_next_active_project's ORDER BY updated_at ASC.
        _sync_project_status_to_db(project_id)
        # Was the one tick outcome with no log line. A project that
        # get_next_active_project keeps handing us while this returns None reads
        # exactly like an idle scheduler — which is how a stalled run hid behind
        # "nothing to do" for 20 minutes.
        tick_log(project_id, "no_run")
        return

    # Don't re-enter a run that's actively executing (in-flight guard).
    # With max_instances=1 (SF-5 fix), concurrent ticks are prevented at the
    # APScheduler level. This is a safety net for edge cases.
    if _has_active_claim(sf, run_id):
        tick_log(project_id, "active_claim", run=run_id[:8])
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
        rows = sf.trace_query(run_id,
            "SELECT COUNT(*) FROM skillflow_trace "
            "WHERE run_id = ? AND event = 'claimed'",
            (run_id,))
        n_exec = rows[0][0] if rows else 0
        if n_exec > _MAX_STEPS_PER_RUN:
            sf.fail_run(run_id, f"Aborted: exceeded {_MAX_STEPS_PER_RUN} step "
                                f"executions ({n_exec}) — likely a non-converging "
                                f"loop (e.g. a verify gate that never passes).")
            _sync_project_status_to_db(project_id)
            return
        # …and bound re-execution of a SINGLE step instance, which trips far
        # earlier and on a sharper signal. A legitimate loop opens a NEW instance
        # row per iteration (a 15-task run claims t_impl 15 times across 15 rows);
        # re-claiming ONE instance only happens when something reset a completed
        # row back to pending. In-framework that is bounded (validation/fail_step
        # retries stop at max_retries=3); out-of-framework it is not — a resume
        # (reactivate_run, reject_checkpoint) recycles the row with no counter at
        # all. That is the shape all three of today's spins share: a TERMINAL
        # condition the host kept treating as retryable. On the benchmark's
        # arxiv-mcp-server task, 5_review's instance 256 was claimed 227 times
        # while every other instance in the run was claimed exactly once, and the
        # whole-run valve above finished 1 claim short of firing (301 vs 300)
        # when the 3-hour wall clock killed the task.
        inst = sf.trace_query(run_id,
            "SELECT step_id, step_instance_id, COUNT(*) FROM skillflow_trace "
            "WHERE run_id = ? AND event = 'claimed' "
            "AND step_instance_id IS NOT NULL "
            "GROUP BY step_instance_id ORDER BY 3 DESC LIMIT 1",
            (run_id,))
        if inst and inst[0][2] > _MAX_CLAIMS_PER_INSTANCE:
            sf.fail_run(run_id,
                        f"Aborted: step '{inst[0][0]}' (instance {inst[0][1]}) was "
                        f"re-executed {inst[0][2]} times without the run advancing "
                        f"— a terminal state is being resumed instead of ending.")
            _sync_project_status_to_db(project_id)
            return
    except Exception:
        pass  # never let the guard itself break a tick

    # Phase A: Resolve next step
    next_node = await _advance_off_the_loop(sf, run_id, project_id)

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
            next_node = await _advance_off_the_loop(sf, run_id, project_id)
            _drain += 1
    except Exception:
        pass

    if next_node is None:
        # Handle terminal states
        run = sf.get_run(run_id)
        # Carry error_reason on a FAILED run. advance_run() ends a routing dead
        # end by failing the run in-DB ("No matching transition from
        # 't_impl_review' with flags {}") rather than raising, so this branch is
        # the only tick surface that sees it — and logging just `status=failed`
        # sent the operator of the 104-task sweep to sqlite to find out why one
        # project stopped.
        tick_log(project_id, "terminal", run=run_id[:8], status=run["status"],
                 reason=(run.get("error_reason") or "")[:200]
                 if run["status"] == "failed" else None)
        if run["status"] in ("paused", "completed", "failed"):
            # skillflow notification bus emits checkpoint_paused / run_completed /
            # run_failed; we just sync the AItelier DB status.
            _sync_project_status_to_db(project_id)
        return

    # Phase B: Claim
    try:
        claimed = sf.claim_next_step(run_id)
    except RequiredContextMissing as e:
        # TERMINAL, not retryable. claim_next_step resolves the same node's
        # context every tick, and the only thing that would ever write the
        # missing file is a step this run will never reach — so the next tick
        # raises the identical error, forever. That is the 47-minute running:1
        # wedge: a dpe_default run started without its meta_conversation
        # predecessor, missing `finalize`, re-claiming every tick with nobody
        # watching. Fail the run so the reason reaches the dashboard and the
        # project stops being re-picked.
        _record_tick_error(sf, run_id, project_id, e, "claim_failed")
        try:
            sf.fail_run(run_id, f"Missing required input: {e}")
        except Exception:
            pass    # a fail_run failure must not mask the original error
        _sync_project_status_to_db(project_id)
        tick_log(project_id, "claim_terminal", run=run_id[:8], error=str(e)[:160])
        return
    except Exception as e:
        # This swallowed the reason ENTIRELY — no log, no trace, no status — and
        # the tick just returned, so the run sat at its current node looking
        # healthy while every tick re-raised and re-swallowed. Live: a dpe_default
        # run started without its meta_conversation predecessor sat at
        # `running:1` for 47 minutes; `claim_next_step` was raising
        # `RequiredContextMissing: Required context source resolved to no
        # content: finalize` every single tick — a perfectly actionable sentence
        # that no surface ever showed. Control flow is unchanged (still returns,
        # still retries next tick); only the silence is removed.
        _record_tick_error(sf, run_id, project_id, e, "claim_failed")
        _sync_project_status_to_db(project_id)
        tick_log(project_id, "claim_failed", run=run_id[:8], error=str(e)[:160])
        return
    if claimed is None:
        tick_log(project_id, "no_claim", run=run_id[:8], node=next_node)
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

    # ORPHAN-DBG: correlation id = step instance + claim version (unique per attempt,
    # available on both sides of the executor boundary — no signature change needed).
    _cid = f"inst{claimed.token.step_instance_id}.v{claimed.token.version}"
    _t0 = _time.time()
    _cur = asyncio.current_task()
    _odbg(f"{_cid} tick execute BEGIN step={claimed.step_id} project={project_id} "
          f"task={_cur.get_name() if _cur else '?'}")
    _executed = False
    try:
        result = await runner.execute(claimed)
        _executed = True
        _odbg(f"{_cid} execute returned; confirm BEGIN step={claimed.step_id}")
        sf.confirm_step(claimed.token, result)
        _odbg(f"{_cid} confirm OK step={claimed.step_id}")

        # Sync task manifest to DB when the PM step or its review completes.
        # FW-2: also sync on "3" so a re-run (3_review reject → 3) refreshes the
        # DB even if the new manifest is produced before the next 3_review.
        if claimed.step_id in ("3", "3_review"):
            _sync_task_manifest_to_db(project_id)
    except MaxRetriesExceeded as e:
        sf.fail_step(claimed.token, str(e), retryable=False)
    except asyncio.CancelledError:
        # ORPHAN-DBG: THE silent path that strands the claim in 'claimed'.
        # CancelledError is BaseException — NOT caught by `except Exception` below —
        # so neither confirm_step nor fail_step runs. Log richly + RE-RAISE (no
        # behavior change: the orphan still happens, then skillflow re-dispatches).
        import traceback as _tb
        _odbg(f"{_cid} *** CANCELLED *** step={claimed.step_id} "
              f"execute_returned={_executed} elapsed={_time.time() - _t0:.1f}s — "
              f"claim left status=claimed (ORPHAN). stack:\n"
              + "".join(_tb.format_stack()))
        raise
    except Exception as e:
        from core.llm_quota import is_quota_exhausted
        if is_quota_exhausted(e):
            # Park first, THEN release the claim. This still spends one retry —
            # the claim has to go back to 'pending' somehow and skillflow has no
            # unclaim — but exactly one, instead of max_retries in 15 minutes.
            _note_quota_exhausted(e)
            tick_log(project_id, "quota_exhausted", run=run_id[:8],
                     step=claimed.step_id,
                     hold=f"{_quota_hold_remaining():.0f}s")
        sf.fail_step(claimed.token, str(e), retryable=True)

    # Sync project status to DB after each tick
    _sync_project_status_to_db(project_id)
    tick_log(project_id, "executed", run=run_id[:8], step=claimed.step_id,
             confirmed=_executed, elapsed=f"{_time.time() - _t0:.1f}s")
    _odbg(f"{_cid} tick END step={claimed.step_id} confirmed={_executed}")


def _emit_checkpoint_sse(project_id: str, run_id: str, step_id: str, label: str):
    """Push a checkpoint_reached SSE event to the global stream."""
    try:
        from api.sse_manager import stream_manager
        payload = json.dumps({
            "type": "checkpoint_reached",
            "project_id": project_id,
            "run_id": run_id,
            "step_id": step_id,
            "label": label,
        })
        loop = asyncio.get_event_loop()
        loop.create_task(stream_manager.push_log("__global__", payload))
    except Exception:
        pass  # Best-effort; checkpoint polling via GET still works


# A failed run's own error is often useless on its own: skillflow reports loop
# exhaustion as a bare "Cycle limit exceeded" and frequently leaves error_reason
# unset entirely. The text that actually tells you what to fix — which gate failed
# and why — was written to the trace by the failing tool. Recover it here so the
# dashboard shows the reason instead of a shrug.
_VAGUE_FAILURES = ("", "unknown", "cycle limit exceeded", "none",
                   "max total steps", "max_total_steps")


def _is_vague(base: str) -> bool:
    """Is this failure reason still a shrug once its framework detail is stripped?

    skillflow >=1.5.30 no longer computes-and-discards the reason on its own side:
    a run that dies on an exhausted `max_loop` re-reads the `from_file` targets of
    the edges it could not match and prepends the human-readable field, then names
    the edge —

        Cycle limit exceeded — continuity_report.json violations: 字数超限 …
                               (edges: All transitions from 'continuity_check' …)

    That is strictly more than we had, but it broke the check it fed: an EXACT
    match against the whole string stopped recognising "Cycle limit exceeded", so
    the tool feedback in the trace — often more specific than the routing file, and
    the only thing that speaks when the gate wrote no file at all — stopped being
    looked up. Match the HEAD of the reason instead, so both details survive and
    are concatenated. skillflow leaves the bare base byte-identical when it finds
    no reason (a flag-routed loop has no `from_file` edge to read), so the plain
    string must keep testing vague too.
    """
    head = base.lower().split(" — ")[0].split(" (edges:")[0].strip()
    return head in _VAGUE_FAILURES


def _already_said(base: str, detail: str) -> bool:
    """True when the trace detail is what skillflow already put in the base.

    Both sides can end up quoting the same routing file, and "X — Y — Y" reads
    like two failures. The detail is `"<step_id>: <message>"`; only the message is
    comparable, since the base never carries a step id.
    """
    message = detail.split(": ", 1)[1] if ": " in detail else detail
    return bool(message) and message in base


async def _advance_off_the_loop(sf, run_id: str, project_id: str = ""):
    """``advance_run`` in a worker thread, so a tool step cannot stop the server.

    advance_run executes inline TOOL steps itself, synchronously. Those are not
    all quick: the Godot gates POST to the builder sidecar with
    ``urllib.request.urlopen`` and wait — 5m41s for the play-test and 4m43s for
    the vision pass, measured 2026-08-23. Called straight from the tick, that
    ran on the event loop, so for those twelve minutes the process answered
    nothing: /health, the SSE stream and every API request returned code 000
    while `docker stats` showed the container at 102% CPU, cheerfully working.
    It came back only after a restart. Raising or lowering the gate timeouts is
    no answer — they were raised on purpose, because at 420 s the play-test gate
    timed out silently and recorded `gate_skipped: true` beside `passed: true`,
    which is a gate vanishing as a pass.

    Nothing about the tick needed to be ON the loop; it blocked there only
    because a tick that blocks is a tick that cannot be re-entered, and that
    accident was carrying the mutual exclusion. It no longer has to: the
    per-project lock in `_execute_skillflow_tick` is a threading.Lock held
    across every await of the tick, so a second tick for the same project still
    fails its non-blocking acquire and logs `locked` — and a claim now names its
    owner, so nothing has to infer "still working" from "still blocked". Two
    concurrent `godot_compile` runs writing one $STEP_DIR is exactly what this
    ordering exists to prevent.

    skillflow's connection is opened with check_same_thread=False and its
    notifications bridge back to the loop with call_soon_threadsafe — agent
    steps have run in the executor pool all along, so this is the same
    boundary, not a new one.
    """
    return await asyncio.to_thread(
        _advance_recording_crashes, sf, run_id, project_id)


def _advance_recording_crashes(sf, run_id: str, project_id: str = ""):
    """``sf.advance_run`` — but a tool-step crash lands in the TRACE first.

    An inline tool step that raises produces a `tool_call` trace row and nothing
    else: no `tool_result`, no error. The exception unwinds through advance_run
    to APScheduler, which prints it to container stdout, and skillflow eventually
    fails the run with the generic "Tool step 'X' crashed 3 times — failing
    (likely a bug in the tool, not a transient error)". So the ONE fact that
    explains the failure exists only in a log nobody reads, while every surface
    that could show it — the trace viewer, `_failure_reason`, the dashboard — has
    nothing to work with.

    Observed on a live novel run whose three retries raised three DIFFERENT
    errors: an unregistered character, then a chapter-number mismatch caused by
    the first crash's partial write. The generic message hid both, and the
    second one is the interesting one — it says the tool is not atomic.

    Record, then re-raise: skillflow's crash counter and every retry semantic
    stay exactly as they were; only the silence is removed.
    """
    try:
        return sf.advance_run(run_id)
    except Exception as e:
        _record_tick_error(sf, run_id, project_id, e, "tool_step_crashed")
        raise


# ── Tick log ────────────────────────────────────────────────────────
# The tick has nine ways to return and most of them were silent, so "the run is
# not moving" gave you nothing to look at: a stuck project looked exactly like an
# idle one. This is a rolling per-tick record of WHICH project was considered and
# WHAT the tick decided, on its own file so the 5-second cadence does not drown
# the container log.
#
# Idle ticks are coalesced to one heartbeat a minute. At 5s they are ~17k lines a
# day of "nothing to do", which would push the informative lines out of the
# rotation window — the opposite of the point. Every tick that picks a project is
# logged in full.
_TICK_LOG_MAX_BYTES = 5 * 1024 * 1024
_TICK_LOG_BACKUPS = 3
_TICK_IDLE_HEARTBEAT_S = 60
# A quota hold is the same shape of noise for the same reason: it repeats at tick
# cadence and says nothing new each time. A 5-hour window at 5s is ~3600 lines —
# enough on its own to evict the lines that explain what happened before it.
_TICK_HOLD_HEARTBEAT_S = 60
_tick_logger = None
_tick_last_idle = 0.0
_tick_last_hold = 0.0


def _get_tick_logger():
    global _tick_logger
    if _tick_logger is not None:
        return _tick_logger
    import logging
    from logging.handlers import RotatingFileHandler
    lg = logging.getLogger("aitelier.scheduler.tick")
    lg.propagate = False          # its own file; not the container log
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        try:
            from core.datadir import aitelier_home
            d = aitelier_home() / "logs"
            d.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(d / "scheduler_ticks.log",
                                    maxBytes=_TICK_LOG_MAX_BYTES,
                                    backupCount=_TICK_LOG_BACKUPS,
                                    encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())   # never break a tick over logging
    _tick_logger = lg
    return lg


def tick_log(project_id: str, outcome: str, **detail) -> None:
    """One line per tick: which project, what the tick decided, why.

    `outcome` is a short stable token so the log greps cleanly — `idle`,
    `locked`, `advanced`, `claim_failed`, `no_claim`, `executed`, `terminal`,
    `quota_exhausted`, `quota_hold`.

    `idle` and `quota_hold` are coalesced to one heartbeat a minute: both repeat
    at tick cadence and carry no new information, and at 5s either one alone
    fills the rotation window with lines nobody needs.
    """
    global _tick_last_idle, _tick_last_hold
    try:
        if outcome == "idle":
            now = _time.time()
            if now - _tick_last_idle < _TICK_IDLE_HEARTBEAT_S:
                return
            _tick_last_idle = now
        elif outcome == "quota_hold":
            now = _time.time()
            if now - _tick_last_hold < _TICK_HOLD_HEARTBEAT_S:
                return
            _tick_last_hold = now
        bits = " ".join(f"{k}={v}" for k, v in detail.items() if v not in (None, ""))
        _get_tick_logger().info(
            "project=%s outcome=%s%s", project_id or "-", outcome,
            (" " + bits) if bits else "")
    except Exception:
        pass          # observability must never be able to break the thing observed


def _record_tick_error(sf, run_id: str, project_id: str, exc: BaseException,
                       event: str) -> None:
    """Put a tick-path exception where someone can read it.

    Shared by the two phases that can blow up with the answer in hand and no
    surface to put it on. Best-effort: it runs while something is already going
    wrong and must never replace the original error with its own.
    """
    try:
        run = sf.get_run(run_id) or {}
        node = run.get("current_node") or ""
        sf.trace(run_id, "tool_result", event,
                 {"error": f"{type(exc).__name__}: {exc}", "step_id": node},
                 step_id=node,
                 project_id=project_id or run.get("project_id") or "")
    except Exception:
        pass          # the logging below is the fallback surface
    import logging as _lg
    _lg.getLogger("aitelier.scheduler").warning(
        "%s on run %s (%s): %s", event, run_id, project_id, exc)


def _last_trace_error(run_id: str) -> str:
    """Newest trace payload for this run that records an error or a failed check."""
    from api.dependencies import get_skillflow
    try:
        rows = get_skillflow().trace_query(
            run_id,
            "SELECT step_id, payload_json FROM skillflow_trace "
            "WHERE run_id = ? AND (payload_json LIKE '%error%' OR payload_json LIKE '%false%') "
            "ORDER BY seq DESC LIMIT 25",
            (run_id,))
    except Exception:
        return ""
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError):
            continue
        err = payload.get("error")
        if isinstance(err, str) and err.strip():
            return f"{row['step_id']}: {' '.join(err.split())}"
        if payload.get("passed") is False:
            fb = payload.get("feedback")
            detail = " ".join(fb.split()) if isinstance(fb, str) and fb.strip() else "check failed"
            return f"{row['step_id']}: {detail}"
    return ""


# A failed run is terminal: its reason cannot change, but the status sync runs on
# EVERY poll tick for such a project (_get_or_create_skillflow_run returns None for
# a failed run, so the tick falls through to _sync_project_status_to_db). Without a
# cache that is an unindexed LIKE scan of the whole trace table, forever.
_failure_reason_cache: dict[str, str] = {}
_FAILURE_CACHE_MAX = 256


def _failure_reason(run: dict) -> str:
    """The most actionable description of why a run failed (computed once per run)."""
    base = (run.get("error_reason") or run.get("error") or "").strip()
    if base and not _is_vague(base):
        return base
    run_id = run.get("id") or ""
    if run_id in _failure_reason_cache:
        return _failure_reason_cache[run_id]
    detail = _last_trace_error(run_id)
    if detail and _already_said(base, detail):
        detail = ""
    reason = (f"{base} — {detail}" if base else detail) if detail else (base or "unknown")
    if run_id:
        if len(_failure_reason_cache) >= _FAILURE_CACHE_MAX:
            _failure_reason_cache.clear()      # bounded; recompute is cheap and rare
        _failure_reason_cache[run_id] = reason
    return reason


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
            if all_runs:
                # Prefer the run whose graph_name matches the project's
                # original config_name, so later runs (e.g. coding_task
                # after DPE) don't shadow the primary run's terminal status.
                proj = db.get_project(project_id)
                proj_config = proj.get("config_name", "") if proj else ""
                run = all_runs[0]  # default: newest
                if proj_config:
                    for r in all_runs:
                        if r.get("graph_name") == proj_config:
                            run = r
                            break
            else:
                run = None
        if not run:
            return
        # Is this a DPE-style config (task loop, coarse step mapping)?
        has_task_loop = False
        manifest = None
        try:
            from api.dependencies import get_config_registry
            manifest = get_config_registry().get(run["graph_name"])
            has_task_loop = bool(manifest and manifest.has_task_loop)
        except Exception:
            has_task_loop = run["graph_name"] == "dpe_default_v2"

        # (see _failure_reason below for why a failed run's status is enriched)

        # Scheduler-owned generator (e.g. pipeline_forge): on completion, persist +
        # live-register the pipeline it produced so it's runnable as gen_<slug>.
        # Butler-driven generators register in _run_pipeline_until_checkpoint; a
        # scheduler-driven one has no such hook, so do it here (fire-once per run).
        if (run["status"] == "completed" and manifest
                and manifest.registers_generated_pipeline
                and run["id"] not in _registered_gen_runs):
            _registered_gen_runs.add(run["id"])
            try:
                from api.dependencies import register_pipeline_from_run
                proj = db.get_project(project_id) or {}
                pname = proj.get("name") or project_id
                reg = register_pipeline_from_run(run["id"], pname)
                _glog = logging.getLogger("aitelier.scheduler")
                if reg.get("error"):
                    _glog.warning("gen-pipeline registration failed for %s: %s",
                                  run["id"], reg["error"])
                else:
                    _glog.info("registered generated pipeline %s (%s)",
                               reg.get("config_name"), reg.get("action"))
            except Exception:
                logging.getLogger("aitelier.scheduler").warning(
                    "gen-pipeline registration errored for %s", run["id"],
                    exc_info=True)

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
            checkpoint_step_id = ""
            if resolver:
                for s in reversed(steps):
                    if s["status"] == "completed":
                        node = resolver.get_node(s["step_id"])
                        if node and node.checkpoint:
                            label = node.checkpoint_label or s["step_id"]
                            checkpoint_step_id = s["step_id"]
                            break
            status = f"checkpoint:{label}"

            # Emit checkpoint_reached SSE (once per pause; cleared on resume)
            key = (run["id"], checkpoint_step_id or label)
            if checkpoint_step_id and key not in _checkpoint_emitted:
                _checkpoint_emitted.add(key)
                _emit_checkpoint_sse(project_id, run["id"], checkpoint_step_id, label)
        elif status == "running" and current_step:
            # AT-15: use fine-grained step_id so the dashboard shows
            # "▶ Implementer" instead of "▶ PM" for all task-loop steps.
            status = f"running:{current_step}"
        elif status == "failed":
            status = f"failed:{_failure_reason(run)[:160]}"

        # Clear checkpoint emission keys when run leaves paused — enables
        # re-emission on the NEXT pause (e.g. rejection loop-back, task loop).
        if run["status"] != "paused":
            to_drop = [k for k in _checkpoint_emitted if k[0] == run["id"]]
            for k in to_drop:
                _checkpoint_emitted.discard(k)

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
            # Re-read the PM's manifest on EVERY tick, not only when the task
            # table is empty. Step 3 is not written once: a goal loop, a review
            # reject or a retry promotes a NEW tasks/ set over the old one. The
            # only other resync hook lives inside the tick's try-block *after*
            # sf.confirm_step, so a re-plan whose confirm raised never reaches
            # it (live: jinyong-cultivate 2026-08-23 — step 3 re-ran into 8
            # `fix_*` tasks, confirm died on "version mismatch" after a reclaim,
            # and the rows stayed frozen on the superseded 14-task manifest:
            # the API read 0/14 while the loop was at 6/8). Cheap to call:
            # _sync_task_manifest_to_db is content-addressed via the
            # .tasks_synced_hash digest and no-ops when nothing changed.
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

# How many DIFFERENT projects one tick may advance concurrently. Same project
# stays strictly serial — that is the per-project lock's job and it is unchanged.
# The bound exists because each slot can hold an LLM call: unbounded fan-out would
# turn a queue of projects into a burst of concurrent model requests.
MAX_CONCURRENT_PROJECTS = int(_os.getenv("AITELIER_MAX_CONCURRENT_PROJECTS", "4"))


async def poll_and_execute():
    """Advance up to MAX_CONCURRENT_PROJECTS different projects, one step each.

    One project per tick was the old rule, and its cost was not the serialism —
    it was that a project whose tick is ALREADY IN FLIGHT still consumed the
    pick. A 400s step therefore produced 80 consecutive `outcome=locked` ticks
    with every other project frozen behind it, which is how a freshly generated
    pipeline sat at its begin node for an hour while a game build ran.

    So: ask for several candidates in priority order, skip the ones already
    running, and start the rest concurrently. The per-project lock still makes
    the SAME project serial; different projects no longer wait on each other.
    """
    import asyncio
    loop = asyncio.get_running_loop()

    projects = db.get_active_projects(limit=MAX_CONCURRENT_PROJECTS)
    if not projects:
        tick_log("", "idle")
        return
    # Filter here as well as in _execute_skillflow_tick: a busy project should not
    # consume one of this tick's slots, and skipping it silently is what made the
    # old starvation invisible — `locked` is still logged, by the tick itself.
    free = [p for p in projects if not _tick_lock_held(p["project_id"])]
    if not free:
        tick_log(projects[0]["project_id"], "locked")
        return
    await asyncio.gather(*(_execute_skillflow_tick(p["project_id"], loop)
                           for p in free))


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

    # ORPHAN-DBG: surface APScheduler anomalies (max-instances skip, misfire, job
    # error) so a skip/miss at the orphan moment is visible in `docker logs`.
    try:
        from apscheduler.events import (
            EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_MAX_INSTANCES,
        )

        def _log_job_event(ev):
            _odbg(f"apscheduler job={getattr(ev, 'job_id', '?')} code={ev.code} "
                  f"scheduled={getattr(ev, 'scheduled_run_time', None)} "
                  f"exc={getattr(ev, 'exception', None)}")

        scheduler.add_listener(
            _log_job_event,
            EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES,
        )
    except Exception:
        pass


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
