"""Record a gate that DIDN'T run, where the fact survives the run.

A validation tool's only channel back to the pipeline is its return dict, and
skillflow's StepValidator (`step_validation.py:_add_issues`) reads exactly one
key from it:

    passed = result.get("all_passed", result.get("passed", ...))
    if passed:
        return                      # <- everything else in the dict is dropped

So a validator that answers `{"all_passed": True, "gate_skipped": True}` is,
from every reader's point of view, a validator that answered "clean". No agent
prompt, no review context, no report file carries the flag. `gate_skipped` was
dead data on that path — the flag existed, and nothing could read it.

That is the `unity_compile` fail-open one layer down: there the gate fell open
silently and seven C# errors shipped. A gate tool that legitimately declines to
run (its sidecar is down, its runner is missing) must not fail the step — an
infra outage is not a code defect, and failing every task in the loop over it is
worse than the skip. But "must not fail the step" is not "may vanish". The skip
is a fact about how much this run was actually verified, and it has to land
somewhere a person or an agent can still find afterwards.

`~/.AItelier/logs/` is that somewhere: mounted, so it survives container
recreation (the container log does not), rotated, and greppable next to
`scheduler_ticks.log`, which exists for the same reason — a silent return and a
healthy one used to look identical.

    grep gate=gdscript_check ~/.AItelier/logs/gate_skips.log
"""
from __future__ import annotations

import logging

_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_BACKUPS = 3
_logger = None


def _get_logger():
    global _logger
    if _logger is not None:
        return _logger
    from logging.handlers import RotatingFileHandler
    lg = logging.getLogger("aitelier.gate.skip")
    lg.propagate = False           # its own file; not the container log
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        try:
            from core.datadir import aitelier_home
            d = aitelier_home() / "logs"
            d.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(d / "gate_skips.log", maxBytes=_LOG_MAX_BYTES,
                                    backupCount=_LOG_BACKUPS, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                             datefmt="%Y-%m-%dT%H:%M:%SZ"))
            lg.addHandler(h)
        except Exception:
            lg.addHandler(logging.NullHandler())   # never break a gate over logging
    _logger = lg
    return lg


def log_gate_skip(gate: str, reason: str, **detail) -> None:
    """One line per skipped gate: which gate, why, and what went unchecked.

    Best-effort in both directions — it must never raise into the gate, and the
    gate must never depend on it having succeeded.
    """
    try:
        bits = " ".join(f"{k}={v}" for k, v in detail.items() if v not in (None, ""))
        _get_logger().info("gate=%s SKIPPED reason=%s%s", gate, reason,
                           (" " + bits) if bits else "")
    except Exception:
        pass          # observability must never be able to break the thing observed
