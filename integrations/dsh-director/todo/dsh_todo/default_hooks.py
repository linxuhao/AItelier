"""The hooks a host that supplies none gets (see hooks.py for the contract).

A minimal, generic version of each: an in-process absence ledger with fixed bounds, the
`repo_gate_absent` flag read from a gate's own JSON report, a structural check of the
candidate report, and no template directory of its own. A host with its own gate policy points DSH_TODO_HOOKS at its module instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

WAIT_SECONDS = 300.0                 # one wait before an absent item is measured again
EPISODE_MAX_SECONDS = 12600.0        # past this an absence ends, naming itself
ABSENCE_TERMINAL = "gate did not run: no verdict was measured"
TEMPLATES_DIR = None                 # the package's own todo_templates/ (templates.default_dir)


def setting(name: str) -> str | None:
    """One of the package's settings (STATE_DIR, TEMPLATES, CLOCK_OVERRIDE): DSH_TODO_<name>."""
    return os.environ.get("DSH_TODO_" + name) or None


@dataclass
class _Episode:
    started_at: float
    last_absence_at: float
    absences: int = 1
    gate: str = ""
    report_key: tuple | None = None
    gate_runs: int = 1


@dataclass
class DeferralLedger:
    """Which keys are waiting on an absence. The episode START survives later sightings, so the
    ceiling measures how long the absence has lasted; the SAME report seen again does not restart
    the wait."""

    episodes: dict[str, _Episode] = field(default_factory=dict)

    def note_absence(self, run_id: str, *, now: float | None = None, gate: str = "",
                     report_key: tuple | None = None, gate_runs: int = 1) -> _Episode:
        moment = time.time() if now is None else now
        episode = self.episodes.get(run_id)
        if episode is None:
            episode = _Episode(started_at=moment, last_absence_at=moment, gate=gate,
                               report_key=report_key, gate_runs=gate_runs)
            self.episodes[run_id] = episode
        else:
            if report_key is None or report_key != episode.report_key:
                episode.last_absence_at = moment
                episode.report_key = report_key
                episode.gate_runs += gate_runs
            episode.absences += 1
            if gate:
                episode.gate = gate
        return episode

    def episode_seconds(self, run_id: str, *, now: float | None = None) -> float:
        episode = self.episodes.get(run_id)
        if episode is None:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - episode.started_at)

    def hold_remaining(self, run_id: str, *, now: float | None = None) -> float:
        episode = self.episodes.get(run_id)
        if episode is None:
            return 0.0
        return max(0.0, episode.last_absence_at + WAIT_SECONDS - (time.time() if now is None else now))

    def expired(self, run_id: str, *, now: float | None = None) -> bool:
        return run_id in self.episodes and self.episode_seconds(run_id, now=now) > EPISODE_MAX_SECONDS

    def deferring(self, run_id: str, *, now: float | None = None) -> bool:
        return (run_id in self.episodes and not self.expired(run_id, now=now)
                and self.hold_remaining(run_id, now=now) > 0)

    def terminal_reason(self, run_id: str) -> str:
        episode = self.episodes.get(run_id)
        gate = (episode.gate if episode else "") or "the repository gate"
        runs = episode.gate_runs if episode else 0
        return f"{ABSENCE_TERMINAL} ({gate}, {runs} gate run(s))"

    def episode_count(self, run_id: str) -> int:
        episode = self.episodes.get(run_id)
        return episode.absences if episode else 0

    def clear(self, run_id: str) -> None:
        self.episodes.pop(run_id, None)


def read_absence(report_path) -> dict | None:
    """`repo_gate_absent` (or `repo_gate_unmeasured`, only when the report has no
    `repo_gate_absent` key) from the gate's own JSON report; None when it declares no absence."""
    if not report_path:
        return None
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    absent = data.get("repo_gate_absent") if "repo_gate_absent" in data else data.get("repo_gate_unmeasured")
    if not absent:
        return None
    gate = data.get("repo_gate") or {}
    return {"gate": str(gate.get("script") or "the repository gate")}


def candidate_rejection(report_bytes: bytes, status: str, artifact: str | None) -> str | None:
    """A JSON report may not contradict the terminal it is filed under: its status must be one a
    candidate can carry, and the artifact it names must be the one being delivered."""
    try:
        value = json.loads(report_bytes)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    allowed = {"candidate", "completed"} if status == "candidate" else {"failed"}
    if value.get("status") is not None and value["status"] not in allowed:
        return "external report status conflicts with the terminal observation"
    reported = value.get("artifact_ref", value.get("artifact"))
    if artifact is not None and reported is not None and reported != artifact:
        return "external report artifact conflicts with the terminal observation"
    return None
