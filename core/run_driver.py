"""Advance a run that nothing else will, and summarise what it did.

AItelier has two classes of config and only one of them moves on its own:

* `scheduler_owned: true` — the APScheduler poller ticks it (dpe_default_v2,
  pipeline_forge, novel_*, …). It advances by itself, but it STOPS at every
  checkpoint and waits for a person.
* `scheduler_owned: false` — the poller never touches it (code_review,
  coding_task, meta_conversation, and **every generated `gen_*` pipeline**).
  The chat butler drives these inline; anything else that merely *starts* one
  has started a run that nobody will ever advance.

That second case is why this module exists. `run_pipeline` over MCP started
butler-owned runs and left them at `running` forever — verified live: a
`code_review` run sat on its first node with zero progress while `wait_for_run`
truthfully and uselessly reported "still running". Since the whole point of the
MCP surface is generated pipelines, that was the entire interesting class.

── ON AUTO-APPROVAL ─────────────────────────────────────────────────────────────

The driver can answer checkpoints itself. That is what a test-drive needs — the
generate → drive → observe → fix loop cannot stop for a human on every lap — and
it is what the butler's own `drive_pipeline` already does.

It is also a real bypass: checkpoints exist because someone wanted to look
(DPE's design review, the novel pipeline's CP#1/CP#2). A caller that wants those
honoured passes `auto_approve=False` and answers each one deliberately. The
choice is the caller's and the tool that exposes it says so in its first line.

NOTE: `core/meta_agent.py:_tool_drive_pipeline` implements the same step loop.
This module is the extraction; the butler should adopt it rather than keep a
second copy, but that edit is deliberately not made here.
"""

from __future__ import annotations

import asyncio

# A driven step loop is bounded so a pipeline that cannot terminate (an unbounded
# cycle, a step failing forever) surfaces as "did-not-terminate" instead of
# spinning a worker until the process dies.
DEFAULT_MAX_STEPS = 60
# How long an auto-approve watcher stays attached to a scheduler-driven run. Long
# enough for a real DPE run, short enough that a permanently stalled run cannot
# leak a subscriber for the life of the process.
DEFAULT_MAX_WATCH_S = 4 * 60 * 60

_TERMINAL = ("completed", "failed")

# How long the finest-grained liveness signal may sit unchanged before the
# summary stops calling the run healthy.
#
# This is a TURN budget, not a step budget, because that is what the trace
# measures. Observed on jinyong-affordance step 5 (2026-08-25): a healthy step
# wrote trace rows every 2-3 minutes for 34 minutes. 10 min leaves a wide
# margin over that while still catching a genuinely wedged turn.
#
# The earlier value here was 15 min against the RUN ROW, which was wrong twice
# over: the run row does not move between steps at all, so a normal 34-minute
# step would have been reported "possibly stalled" — and that is exactly the
# mistake I made by hand before writing this.
_STALL_HINT_S = 10 * 60


def restore_retry_budget(sf, run_id: str) -> dict | None:
    """Give a resumed run's failed step its retries back. Returns what it reset.

    skillflow's ``reactivate_run`` resets the last **completed** step — but a
    failed run's blocker is a **failed** one, and a step only ever reaches
    'failed' by exhausting max_retries. So the row the resume has to clear is
    precisely the row it does not touch: retry_count stays at the cap, and the
    very first attempt after the resume takes the "retries exhausted" branch and
    kills the run again. Retry, from the user's side, silently did nothing.

    Live on 2026-08-26: 5_review sat failed at retry_count 3/3 after DeepSeek's
    5-hour quota ran out. The quota reopened; the run could not.

    Called AFTER reactivate_run so it wins on current_node.
    """
    row = sf._conn.execute(
        "SELECT id, step_id, retry_count, max_retries FROM skillflow_steps "
        "WHERE run_id = ? AND status = 'failed' ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if not row:
        return None
    with sf._conn:
        sf._conn.execute(
            "UPDATE skillflow_steps SET status = 'pending', retry_count = 0, "
            "version = version + 1, claimed_at = NULL, claimed_by = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        sf._conn.execute(
            "UPDATE skillflow_runs SET current_node = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (row["step_id"], run_id),
        )
    return {"step": row["step_id"], "instance": row["id"],
            "was_retry_count": row["retry_count"],
            "max_retries": row["max_retries"]}


def _last_trace_at(sf, run_id: str) -> str | None:
    """Newest trace row timestamp for this run, or None if the trace is empty
    or unreadable. None means "no finer signal available", never "idle 0"."""
    try:
        rows = sf.trace_query(
            run_id,
            "SELECT created_at FROM skillflow_trace WHERE run_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (run_id, 1))
    except Exception:
        return None
    for r in rows or []:
        try:
            return r["created_at"]
        except (KeyError, TypeError, IndexError):
            return None
    return None


def _seconds_since(ts: str | None) -> float | None:
    """Seconds since `ts` (a skillflow UTC timestamp), or None if unreadable.

    Returns None rather than 0 on a bad parse: "I could not tell" must not
    render as "it just advanced".
    """
    if not ts:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(str(ts).strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    return None


def _human(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m) if h else ("%dm%02ds" % (m, s) if m else "%ds" % s)



async def drive_run(sf, db, ws, run_id: str, *, scheduler_owned: bool,
                    auto_approve: bool, max_steps: int = DEFAULT_MAX_STEPS,
                    max_watch_s: float = DEFAULT_MAX_WATCH_S) -> str:
    """Take a started run as far as it can go. Returns its final status.

    For a scheduler-owned run this only WATCHES (the poller does the advancing) —
    the sole thing it may contribute is answering checkpoints, and it does that
    event-driven rather than by polling.
    """
    if scheduler_owned:
        return await _watch(sf, run_id, auto_approve, max_watch_s)
    return await _step(sf, db, ws, run_id, auto_approve, max_steps)


async def _watch(sf, run_id: str, auto_approve: bool, max_watch_s: float) -> str:
    """Approve this run's checkpoints until it terminates. Poller does the rest."""
    if not auto_approve:
        return (sf.get_run(run_id) or {}).get("status") or "running"

    done = asyncio.Event()

    async def _on_event(notification):
        rid = notification.run_id or (notification.payload or {}).get("run_id")
        if not rid or rid != run_id:
            return
        if notification.event_type == "checkpoint_paused":
            try:
                sf.approve_checkpoint(run_id)
            except Exception:
                pass          # a checkpoint answered by someone else is not an error
        elif notification.event_type in ("run_completed", "run_failed",
                                         "pipeline_failed"):
            done.set()

    # Subscribe BEFORE the status read, for the same reason wait_for_run does: the
    # run can settle in the gap and its event would fire with nobody listening.
    sf.notifications.subscribe(_on_event)
    try:
        run = sf.get_run(run_id) or {}
        if run.get("status") == "paused":
            try:
                sf.approve_checkpoint(run_id)
            except Exception:
                pass
        elif run.get("status") in _TERMINAL:
            return run["status"]
        try:
            await asyncio.wait_for(done.wait(), max_watch_s)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            sf.notifications.unsubscribe(_on_event)
        except ValueError:
            pass
    return (sf.get_run(run_id) or {}).get("status") or "running"


async def _step(sf, db, ws, run_id: str, auto_approve: bool, max_steps: int) -> str:
    """Advance / claim / execute, one step at a time, because nothing else will."""
    from aitelier.runner import AgentStepRunner

    runner = AgentStepRunner(db_manager=db, workspace_manager=ws)
    attempts: dict[str, int] = {}
    for _ in range(max_steps):
        run = sf.get_run(run_id) or {}
        status = run.get("status")
        if status in _TERMINAL:
            break
        if status == "paused":
            if not auto_approve:
                break                 # deliberately left for answer_checkpoint
            sf.approve_checkpoint(run_id)
            continue
        nxt = sf.advance_run(run_id)
        if (sf.get_run(run_id) or {}).get("status") in ("paused", *_TERMINAL):
            continue                  # re-handled at the top, one place only
        if nxt is None:
            continue
        try:
            if sf._get_resolver_for_run(run_id).is_tool(nxt):
                continue              # a tool node advances without a claim
        except Exception:
            pass
        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            continue
        attempts[claimed.step_id] = attempts.get(claimed.step_id, 0) + 1
        try:
            result = await runner.execute(claimed)
            sf.confirm_step(claimed.token, result)
        except Exception as e:
            # Retry once, then let it fail: a step that fails identically forever
            # would otherwise eat the whole step budget and report "did not
            # terminate", hiding the actual error.
            sf.fail_step(claimed.token, str(e)[:300],
                         retryable=attempts[claimed.step_id] < 2)
    return (sf.get_run(run_id) or {}).get("status") or "running"


def summarise_run(sf, ws, registry, run_id: str) -> dict:
    """What happened, small enough to read: per-step status, the FIRST failure with
    its error, and the final outputs truncated.

    The fix half of a generate → drive → fix loop needs to know what broke. A bare
    status ("failed") names no step and no reason, and the raw trace is far too
    large to hand a model — this is the shape the butler's own test-drive returns.
    """
    run = sf.get_run(run_id) or {}
    if not run:
        return {"error": f"no run '{run_id}'"}
    config_name = run.get("graph_name") or ""
    project_id = run.get("project_id") or ""

    per_step, first_failure = [], None
    try:
        steps = sf.get_steps(run_id)
    except Exception as e:
        steps = []
        per_step.append({"step": "?", "status": f"unreadable: {e}"})
    for s in steps:
        entry = {"step": s["step_id"], "status": s["status"]}
        # WHICH loop item this instance ran for (skillflow >=1.5.41), omitted
        # outside a loop. Without it a fan-out reads as repeated identical rows —
        # `t_impl completed` / `t_impl pending` and no way to say which task —
        # which is precisely the question the fix half of the loop is asking.
        # `.get` because the container installs skillflow from PyPI and can be a
        # release behind: a KeyError here would take down the whole summary, the
        # one tool a driving agent has for "what broke".
        item = s.get("loop_item")
        if item:
            entry["item"] = item
        if s["status"] == "failed" and first_failure is None:
            err = (s.get("error") or "")[:300]
            entry["error"] = err
            first_failure = {"step": s["step_id"], "error": err}
            if item:
                first_failure["item"] = item
        per_step.append(entry)

    outputs: dict[str, str] = {}
    try:
        mf = registry.get(config_name)
        out_step = (getattr(mf, "output_step", None) if mf else None) or (
            steps[-1]["step_id"] if steps else "")
        if out_step and project_id:
            od = ws.get_final_path(project_id, out_step, config_name)
            if od.exists():
                for f in sorted(od.rglob("*")):
                    if f.is_file() and f.name != "_snapshot.json":
                        outputs[str(f.relative_to(od))] = f.read_text(
                            encoding="utf-8", errors="replace")[:1500]
    except Exception:
        pass          # a missing output dir is a fact about the run, not an error

    status = run.get("status") or "running"
    # A run can fail BEFORE any step row is marked failed: `claim_next_step`
    # rejecting at the node leaves every step `pending` and writes the reason to
    # the RUN row only. `first_failure` — which this function's docstring and the
    # get_run_summary tool description both promise holds "the FIRST failure with
    # its error" — was then null on a run whose status, verdict and run_error all
    # said failed (run 32ee04de: "Required context source resolved to no content:
    # finalize", every step pending). A promised field left empty reads as "no
    # failure found", which is the opposite of what happened.
    if first_failure is None and status == "failed":
        err = (run.get("error_reason") or run.get("error") or "")[:300]
        if err:
            # `current_node` is the last node CLAIMED, not where the run stopped,
            # so it is only an honest attribution when that step did not finish.
            # Measured on two live runs (75209504, 3cf2042e): both reported
            # first_failure.step = a step whose own status is `completed`, with a
            # note asserting "the run stopped at this node" — naming a step that
            # succeeded, which is worse than naming none. An operator `fail_run`
            # or a terminal-node stop leaves exactly that shape.
            node = run.get("current_node") or ""
            node_status = next((s["status"] for s in per_step
                                if s["step"] == node), None) if node else None
            if node and node_status in (None, "pending", "running", "claimed"):
                first_failure = {
                    "step": node,
                    "error": err,
                    "from": "run_error",
                    "note": ("no step row is marked failed — the run stopped at "
                             "this node without a step failing (a claim-time "
                             "rejection), so the reason is the run-level error."),
                }
            else:
                first_failure = {
                    "step": None,
                    "error": err,
                    "from": "run_error",
                    "note": ("no step row is marked failed and the run's last "
                             "claimed node ('%s', status %s) finished — so no step "
                             "is the culprit. The run was stopped from outside, or "
                             "ended at a terminal node. `step` is null on purpose: "
                             "naming a step that succeeded would be worse than "
                             "naming none." % (node or "?", node_status or "?")),
                }

    # A run that is still RUNNING has not "failed to terminate" — it has not
    # finished yet, and those are different claims. The old wording said
    # "did-not-terminate (likely an unbounded loop or a step failing
    # persistently)" for a run that was advancing normally one step earlier;
    # read literally it says the run is broken, and a caller who believes it
    # gives up on a healthy run. What separates the two is not the status, it
    # is HOW LONG the row has been unchanged — so report that, and let the
    # caller judge.
    # Liveness comes from the TRACE, not the run row. `skillflow_runs.updated_at`
    # only moves when a STEP completes, so a step that legitimately takes 34
    # minutes (measured: step 5 on jinyong-affordance, 2026-08-25) looks frozen
    # on that row while its trace is writing a turn every 2-3 minutes. I read the
    # run row, called it stalled, and restarted a healthy step. The trace is
    # per-TURN, which is the granularity a liveness claim needs.
    row_ts = (run.get("updated_at") or "") or None
    trace_ts = _last_trace_at(sf, run_id)
    updated_at = trace_ts or row_ts
    liveness_from = "trace" if trace_ts else ("run_row" if row_ts else "none")
    idle_s = _seconds_since(updated_at)
    if status == "completed":
        verdict = "completed"
    elif status == "failed":
        verdict = "failed"
    elif status == "paused":
        verdict = "paused-at-checkpoint"
    elif idle_s is None:
        verdict = ("in-progress (no updated_at on the run row — cannot tell "
                   "advancing from stalled)")
    elif idle_s < _STALL_HINT_S:
        verdict = ("in-progress (last advanced %s ago, liveness from %s)"
                   % (_human(idle_s), liveness_from))
    else:
        verdict = ("in-progress but NOT ADVANCING for %s (liveness from %s) — "
                   "possibly stalled: a wedged provider call, an unbounded "
                   "loop, or a step failing persistently. Re-read after a "
                   "minute: if idle_seconds keeps growing it is stuck; if it "
                   "resets, the step was merely slow."
                   % (_human(idle_s), liveness_from))
    return {
        "run_id": run_id, "project_id": project_id, "config": config_name,
        "status": status, "verdict": verdict,
        "updated_at": updated_at,
        "idle_seconds": idle_s,
        "liveness_from": liveness_from,
        "run_row_updated_at": row_ts,
        "steps": per_step, "first_failure": first_failure,
        "final_outputs": outputs,
        "run_error": run.get("error_reason") or run.get("error"),
    }
