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


_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 2
_logger = None


def _sink():
    """Rotating handler for the durable copy.

    It used to be a bare ``open(..., "a")``: every line ever written, kept
    forever, on the mounted data volume — 5.1 MB by the time anyone looked, with
    nothing to stop it. A diagnostic that outlives its investigation and grows
    without bound is a disk leak, not a diagnostic.
    """
    global _logger
    if _logger is not None:
        return _logger
    import logging
    from logging.handlers import RotatingFileHandler
    lg = logging.getLogger("aitelier.orphan_dbg")
    lg.propagate = False           # its own file; stdout is handled separately
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        try:
            from core import datadir
            h = RotatingFileHandler(datadir.orphan_log_path(),
                                    maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
                                    encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03dZ %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S"))
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())
    _logger = lg
    return lg


def odbg(msg: str) -> None:
    line = f"[ORPHAN-DBG] {msg}"
    print(line, flush=True)
    try:
        _sink().info(line)
    except Exception:
        pass          # the diagnostic must never crash a caller
