"""Process-local record of the last LLM stream chunk per run.

The hung-step warning can see that a claim is old and that the step
heartbeats, but a heartbeat only proves the PROCESS is alive — during the
2026-08-27 trickle hang it kept printing "slow, not dead" for 35 minutes
about a call that was never going to finish. Chunk arrival (AIGateway's
streaming transport) is the signal that the LLM call itself is progressing;
this registry carries it from the runner's progress callback to the
scheduler's diagnosis.

In-process on purpose: producer and reader live in the same backend process,
and a tick that survived a restart would only mislead. Never authoritative —
it refines a WARNING's wording; recovery itself stays with the layered
timeouts (300s read gap, 900s wall cap, activity reaper).
"""

import threading
import time

_lock = threading.Lock()
_last: dict[str, dict] = {}     # run_id -> {step_id, chars, at}
_CAP = 200                      # runs; FIFO-evicted by tick age


def note_progress(run_id: str, step_id: str, chars: int) -> None:
    """Record the newest stream tick for a run. Called from the LLM worker
    thread every few seconds while chunks arrive; must never raise."""
    try:
        with _lock:
            _last[run_id] = {"step_id": step_id, "chars": int(chars or 0),
                             "at": time.time()}
            if len(_last) > _CAP:
                oldest = min(_last, key=lambda k: _last[k]["at"])
                _last.pop(oldest, None)
    except Exception:
        pass


def last_progress(run_id: str) -> dict | None:
    """The newest tick for a run ({step_id, chars, at}), or None."""
    with _lock:
        rec = _last.get(run_id)
        return dict(rec) if rec else None
