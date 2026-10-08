# Event-loop lag observation

The scheduler's control plane runs on a single asyncio event loop. When that
loop stops turning — a synchronous tool step on the loop, a burst of in-process
work, a stalled transport — every open stream and the scheduler stop with it,
which is why the ordering in `api/main.py` moves blocking identity resolution
off the loop.

The existing watchdog does not report this. `core.scheduler._check_hung_claims`
answers "is a claimed STEP stale" and runs on the same loop it supervises; a
loop that is not turning cannot run it. A hung *claim* and a hung *loop* are
different facts, and only the second is what "the backend is unresponsive"
usually means.

## What is measured

`core/event_loop_lag.EventLoopLagMonitor` schedules one `asyncio.sleep` on the
loop and compares how long it actually took against how long it asked for. The
excess **is** the lag: a synchronous block inside any callback delays this
sleep's wakeup by exactly the time the loop spent not turning. There is no
profiler, no tracing hook, no monkeypatch, and no per-callback work added.

Each observation records the lag in seconds, the threshold in force, and an
attribution. Attribution is deliberately narrow: the only control-plane
operations this process can name from here are the detached scheduler ticks,
and a tick whose task has already finished is filtered out, so an observation is
attributed to the projects whose ticks are live *at the moment of sampling*
(`tick:<project>`) or to `unknown`. Attribution names the active context, not a
proven blocking cause: a stalled loop and a live tick can be coincidental. No
payload, prompt, or credential is read, a non-string attribution is never
rendered, and the label is truncated to a byte budget.


## Bounds

* One monitor per scheduler lifecycle, attached once by `_add_scheduler_job`.
  An owner that cannot be weakly referenced is rejected before any sampling task
  is created, so no untracked monitor can be started.
* Its sampling task is cancelled and awaited on `EVENT_SCHEDULER_SHUTDOWN` (and
  by `stop_scheduler`), so it cannot outlive the scheduler that owns it —
  including when the stop is issued from another thread or before the loop is
  turning; the awaited teardown always runs on the monitor's own loop.
* Owner loss is covered too, and not by polling from the outside: the monitor
  holds only a WEAK reference to its scheduler, so a lifecycle that is collected
  without a shutdown cannot be pinned by its own monitor, and its registry entry
  disappears with it. Since nothing is then left to ask, the sampler notices on
  its own next wake that its owner is gone and settles itself on the loop that
  owns the task — at most one interval later. No reaper thread and no registry
  of dead owners.
* A monitor whose task has exited is not reported as running: the registry tracks
  the live task, not a sticky flag, so a replaced lifecycle still observes lag.
* Observations live in a fixed-length `deque`
  (`AITELIER_LOOP_LAG_MAX_OBSERVATIONS`, default 100).
* Warnings are rate-limited (`AITELIER_LOOP_LAG_LOG_COOLDOWN_SECONDS`, default
  30 s) and use the existing `aitelier.event_loop_lag` logger.
* Interval, threshold and retention are clamped, and a nan/inf configuration is
  rejected in favour of the default, so no configuration turns loop jitter into
  a storm of records.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AITELIER_LOOP_LAG_INTERVAL_SECONDS` | `1.0` | sampling period |
| `AITELIER_LOOP_LAG_THRESHOLD_SECONDS` | `0.5` | minimum lag recorded |
| `AITELIER_LOOP_LAG_MAX_OBSERVATIONS` | `100` | retained observations |
| `AITELIER_LOOP_LAG_LOG_COOLDOWN_SECONDS` | `30.0` | warning cooldown |

## What this is not

It is observability, not policy: it does not reclaim claims, cancel work, or
change when the reaper fires, and it leaves `_check_hung_claims`,
`misfire_grace_time` and every task/admission/owner path untouched. It reports
current-source facts about this process's loop; it is not a claim about
production performance.
