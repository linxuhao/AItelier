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
    verdict = ("completed" if status == "completed" else
               "failed" if status == "failed" else
               "paused-at-checkpoint" if status == "paused" else
               "did-not-terminate (likely an unbounded loop or a step failing "
               "persistently)")
    return {
        "run_id": run_id, "project_id": project_id, "config": config_name,
        "status": status, "verdict": verdict,
        "steps": per_step, "first_failure": first_failure,
        "final_outputs": outputs,
        "run_error": run.get("error_reason") or run.get("error"),
    }
