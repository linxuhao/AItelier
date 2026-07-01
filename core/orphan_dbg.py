"""Shared [ORPHAN-DBG] diagnostic sink (TEMPORARY — remove together with the
rest of the orphaned-claim instrumentation once the root cause is pinned).

Writes each line to stdout AND appends to a durable ``~/.AItelier/orphan_dbg.log``
that survives container recreation (``docker logs`` is per-container and is wiped
on an image rebuild — which lost a real recurrence once). Both the scheduler and
the runner import this so the run_step ENTER/EXIT thread traces land durably too,
not just the scheduler's tick logs. Best-effort: the diagnostic must never crash
a caller.
"""
from __future__ import annotations


def odbg(msg: str) -> None:
    line = f"[ORPHAN-DBG] {msg}"
    print(line, flush=True)
    try:
        import datetime as _dt
        from pathlib import Path as _P
        ts = _dt.datetime.now(_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3]
        with (_P.home() / ".AItelier" / "orphan_dbg.log").open(
                "a", encoding="utf-8") as fh:
            fh.write(f"{ts}Z {line}\n")
    except Exception:
        pass
