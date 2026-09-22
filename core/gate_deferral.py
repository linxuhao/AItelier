"""An absence that outlives the wall clock is still an absence.

The fifth round of the "a gate that never ran is not a failing gate" card
splits one deadlock into two resources:

* the WALL CLOCK bound protects a shared resource — the scheduler and its
  poller must never be held by one step forever;
* the ACCOUNTING rule protects the ledger — a gate that produced no verdict
  may never be charged to the implementer.

Both can hold at once: the episode below lets the run STOP, and when it stops
it names the absence.  It never says ``Cycle limit exceeded`` and it never
says the tests failed, because neither of those is what happened.

Two execution points read this module (``core/scheduler.py``):

* the tick skips a run whose deferral is still inside its wait, so no
  implement cycle is spent while the gate is silent;
* the per-instance re-claim valve (``_MAX_CLAIMS_PER_INSTANCE``) is bypassed
  while a deferral is live, because a deferral resets a completed row back to
  ``pending`` on purpose — counting that as a runaway resume would kill the
  run with a message that blames the step ("a terminal state is being resumed
  instead of ending"), which is exactly the false attribution this card
  exists to remove.  The episode's own clock is the bound that applies then.

Everything here is pure and clock-injected: no sleeping, no globals beyond
the module ledger, no dependency on skillflow.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

# How long ONE wait lasts before the tick is allowed to look at the run again.
# This is a poll interval, not a repair: a shorter value only means more ticks
# that spend nothing and change nothing.
GATE_DEFERRAL_WAIT_SECONDS = float(
    os.getenv("AITELIER_GATE_DEFERRAL_WAIT_SECONDS", "300"))

# The wall-clock ceiling on ONE episode of absence.  Past this the run may
# end; it must end NAMING the absence.  Both directions are guarded:
# `_positive_seconds` refuses a non-positive value (a zero ceiling would end
# every absence instantly) and the ceiling is enforced by `expired`, so a
# caller cannot get an unbounded episode by raising the number alone.
GATE_DEFERRAL_EPISODE_MAX_SECONDS = float(
    os.getenv("AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS", "10800"))

# Absolute ceilings on the two knobs above. Both are read from the
# environment, and both are things a caller could "fix" a stuck run by
# widening: a 24-hour poll interval or a 1e9-second episode makes the
# wall-clock bound vanish, which is the very thing the shared scheduler may
# not lose. So the environment may TUNE them and may not REMOVE them.
GATE_DEFERRAL_WAIT_MAX = float(
    os.getenv("AITELIER_GATE_DEFERRAL_WAIT_MAX", "900"))
GATE_DEFERRAL_EPISODE_MAX_CEILING = float(
    os.getenv("AITELIER_GATE_DEFERRAL_EPISODE_MAX_CEILING", str(6 * 3600)))

# The sentence an expired absence is allowed to end with.  It names the
# absence and nothing else.  In particular it carries no word that could be
# read as a code failure — see `absent_terminal_names_no_failure`, which the
# tests assert is True of it.
ABSENCE_TERMINAL = "gate did not run: no verdict was measured"

#: Phrases that name a CODE outcome. Multi-word ones are matched anywhere;
#: single words are matched on a WORD BOUNDARY, because a naive `in` check
#: fires on the inside of an innocent word — "measured" ends in "red", and the
#: absence sentence itself contains "measured".
_FORBIDDEN_PHRASES = ("cycle limit exceeded", "max total steps")
_FORBIDDEN_WORDS = ("failed", "failure", "fail", "regression", "red", "error")


def _positive_seconds(value: float, fallback: float) -> float:
    """Never let a hostile or mis-parsed bound disable the guard."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    return seconds if seconds > 0 else fallback


def wait_seconds() -> float:
    return min(_positive_seconds(GATE_DEFERRAL_WAIT_SECONDS, 300.0),
               max(1.0, _positive_seconds(GATE_DEFERRAL_WAIT_MAX, 900.0)))


def episode_max_seconds() -> float:
    ceiling = max(1.0, _positive_seconds(GATE_DEFERRAL_EPISODE_MAX_CEILING,
                                         6 * 3600.0))
    return min(_positive_seconds(GATE_DEFERRAL_EPISODE_MAX_SECONDS, 10800.0),
               ceiling)


@dataclass
class _Episode:
    started_at: float
    last_absence_at: float
    absences: int = 1
    gate: str = ""


@dataclass
class DeferralLedger:
    """One process's record of which runs are currently waiting on an absence.

    Deliberately in-process, like the provider-quota hold: a restart during an
    absence costs one look at the gate and re-establishes the episode.  A
    durable hold could outlive the condition it describes, which is the failure
    the quota hold documents at its own cap.
    """

    episodes: dict[str, _Episode] = field(default_factory=dict)

    def note_absence(self, run_id: str, *, now: float | None = None,
                     gate: str = "") -> _Episode:
        """Record that `run_id`'s gate produced no verdict this pass.

        The episode START is kept across calls: the ceiling measures how long
        the absence has lasted, not how long ago the last tick was.
        """
        moment = time.time() if now is None else now
        episode = self.episodes.get(run_id)
        if episode is None:
            episode = _Episode(started_at=moment, last_absence_at=moment,
                               gate=gate)
            self.episodes[run_id] = episode
        else:
            episode.last_absence_at = moment
            episode.absences += 1
            if gate:
                episode.gate = gate
        return episode

    def episode_seconds(self, run_id: str, *, now: float | None = None) -> float:
        episode = self.episodes.get(run_id)
        if episode is None:
            return 0.0
        moment = time.time() if now is None else now
        return max(0.0, moment - episode.started_at)

    def hold_remaining(self, run_id: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait before looking at the run again."""
        if run_id not in self.episodes:
            return 0.0
        moment = time.time() if now is None else now
        episode = self.episodes[run_id]
        return max(0.0, episode.last_absence_at + wait_seconds() - moment)

    def expired(self, run_id: str, *, now: float | None = None) -> bool:
        """Has this absence outlived the wall-clock ceiling for one episode?"""
        if run_id not in self.episodes:
            return False
        return self.episode_seconds(run_id, now=now) > episode_max_seconds()

    def deferring(self, run_id: str, *, now: float | None = None) -> bool:
        """Should this run be left alone entirely on this tick?"""
        return run_id in self.episodes and not self.expired(run_id, now=now)

    def terminal_reason(self, run_id: str) -> str:
        """The sentence an expired absence ends with.

        It names the gate when one is known and always names the absence; it
        never names a code failure, which is the whole point of the card.
        """
        episode = self.episodes.get(run_id)
        gate = (episode.gate if episode else "") or "the repository gate"
        return f"{ABSENCE_TERMINAL} ({gate}, {self.episode_count(run_id)} attempt(s))"

    def episode_count(self, run_id: str) -> int:
        episode = self.episodes.get(run_id)
        return episode.absences if episode else 0

    def clear(self, run_id: str) -> None:
        self.episodes.pop(run_id, None)


#: The ledger the scheduler uses.  Exposed so a test can drive the same object
#: the tick reads, rather than a copy of it.
LEDGER = DeferralLedger()


def guard_per_instance_valve(claims: int, run_id: str, *,
                             max_claims: int,
                             ledger: DeferralLedger | None = None,
                             now: float | None = None) -> bool:
    """May the per-instance re-claim valve fire for this run?

    ``core/scheduler.py`` counts ``claimed`` trace events per step instance and
    fails the run past ``_MAX_CLAIMS_PER_INSTANCE``.  The comment beside that
    constant states its premise: a single instance is only re-claimed when
    something reset a completed row back to ``pending``.  A deferral does
    exactly that, on purpose — so while an episode is live the valve is not a
    runaway signal and must not fire.  The episode ceiling ends the run
    instead, naming the absence.
    """
    book = LEDGER if ledger is None else ledger
    if book.deferring(run_id, now=now):
        return False
    return claims > max_claims


def absent_terminal_names_no_failure(reason: str) -> bool:
    """True when a terminal reason really names only the absence."""
    lowered = str(reason).lower()
    if ABSENCE_TERMINAL.lower() not in lowered:
        return False
    if any(phrase in lowered for phrase in _FORBIDDEN_PHRASES):
        return False
    words = set(re.findall(r"[a-z]+", lowered))
    return not (words & set(_FORBIDDEN_WORDS))


def hold_blocks_advance(run_id: str, *, now: float | None = None,
                        ledger: DeferralLedger | None = None) -> bool:
    """May the host refuse to advance this run at all right now?

    Read by ``AItelierSkillFlow.advance_run``.  A live episode of absence means
    advancing the run would route the absence back into the implement loop,
    which is the charge this card removes; the run is left exactly where it is
    until the gate speaks or the ceiling ends it.
    """
    book = LEDGER if ledger is None else ledger
    return book.deferring(run_id, now=now)


def _row_get(row, name, index):
    try:
        return row[name] if hasattr(row, "keys") else row[index]
    except Exception:
        return None


def find_test_report(sf, run_id: str):
    """Path to the run's most recent `test`-step report, or None.

    The absence lives in `test_report.json`, which is an artifact on disk.
    skillflow does NOT keep the tool's whole return dict in the trace — it
    stores `{"source": "tool_step", "written": ..., "passed": ...}` and drops
    the rest — so the return value cannot be the evidence.  What the trace
    DOES keep is the CALL's params, including the `out_dir` the step wrote to;
    that is the same route `scheduler._test_gate_attribution` already takes to
    the very same file, so this is a path the run itself recorded rather than
    one reconstructed here.
    """
    from pathlib import Path
    try:
        rows = sf.trace_query(
            run_id,
            "SELECT step_id, payload_json FROM skillflow_trace "
            "WHERE run_id = ? ORDER BY seq DESC LIMIT 40",
            (run_id,))
    except Exception:
        return None
    for row in rows or []:
        if _row_get(row, "step_id", 0) != "test":
            continue
        try:
            payload = json.loads(_row_get(row, "payload_json", 1) or "{}")
        except (TypeError, ValueError):
            continue
        params = (payload or {}).get("params") or {}
        if params.get("step_name") not in (None, "", "test", "run_tests"):
            continue
        out_dir = params.get("out_dir")
        if not out_dir:
            continue
        report = Path(out_dir) / "test_report.json"
        if report.is_file():
            return report
    return None


def read_absence(report_path) -> dict | None:
    """The absence a `test_report.json` states, or None if it states none.

    Only the report's OWN flag counts: a report that says the run was graded
    and the code failed states no absence, whatever else it mentions in prose.
    """
    from pathlib import Path
    if not report_path:
        return None
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not (data.get("repo_gate_absent") or data.get("repo_gate_unmeasured")):
        return None
    gate = data.get("repo_gate") or {}
    return {"gate": str(gate.get("script") or "the repository gate")}


def last_gate_absence(sf, run_id: str, report_path=None) -> dict | None:
    """The absence this run's repository gate left behind, or None."""
    path = report_path if report_path is not None else find_test_report(sf, run_id)
    return read_absence(path)


def observe_run(sf, run_id: str, *, now: float | None = None,
                ledger: DeferralLedger | None = None,
                report_path=None) -> dict:
    """What this tick owes a run whose repository gate produced no verdict.

    Returns one of three states:

    * ``none``     — there is no absence to account for; the tick proceeds.
    * ``silent``   — an absence is in progress and inside its wall-clock
                     ceiling: do nothing, spend nothing, end nothing.
    * ``expired``  — the absence outlived the ceiling: the run may end, and it
                     ends with ``reason`` NAMING the absence.
    """
    book = LEDGER if ledger is None else ledger
    absence = last_gate_absence(sf, run_id, report_path=report_path)
    if absence is None:
        book.clear(run_id)
        return {"state": "none", "remaining": 0.0, "gate": "", "reason": ""}
    moment = time.time() if now is None else now
    episode = book.note_absence(run_id, now=moment, gate=absence["gate"])
    if book.expired(run_id, now=moment):
        return {"state": "expired", "remaining": 0.0, "gate": episode.gate,
                "reason": book.terminal_reason(run_id)}
    return {"state": "silent",
            "remaining": book.hold_remaining(run_id, now=moment),
            "gate": episode.gate, "reason": ""}

