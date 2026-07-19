"""StubStepRunner — a no-LLM StepRunner for the pipeline_forge dry-run smoke.

Drives a generated graph through skillflow's real claim/advance loop while every
`agent` step returns a canned, schema-shaped result (no LLM call). skillflow still
auto-runs inline tool/gate nodes and evaluates every transition, so the smoke
exercises the real engine — proving the graph boots, its tools/roles construct,
and it reaches a loop-external terminal without running away — deterministically
and ~free. See design/pipeline_forge.md §5c / §7.
"""
from __future__ import annotations

import json
from pathlib import Path

from skillflow.core import ClaimedStep, StepResult


class StubStepRunner:
    """Return canned outputs for agent steps. `verdict` drives review branches:
    True → happy path (should reach `done`); False → adversarial (reject loops must
    stay max_loop-bounded and end FAILED, never run to max_steps)."""

    def __init__(self, verdict: bool = True):
        self._verdict = bool(verdict)

    async def execute(self, step: ClaimedStep) -> StepResult:
        # Async wrapper for the StepRunner protocol; delegates to the sync path.
        return self.run(step)

    def run(self, step: ClaimedStep) -> StepResult:
        # Sync path — the smoke drives this directly (it may run inside an already-
        # running event loop, so it must not spin up its own).
        output_dir = step.inputs.get("_output_dir", "") if step.inputs else ""
        if output_dir:
            d = Path(output_dir)
            d.mkdir(parents=True, exist_ok=True)
            # A verdict file so `from_file: review_verdict.json` transitions resolve.
            (d / "review_verdict.json").write_text(
                json.dumps({"passed": self._verdict, "feedback": "stub",
                            "suggestions": []}),
                encoding="utf-8")
            # Write any file a transition matches on via `from_file` (e.g. a tool
            # gate's test_report.json) with the success flag, so branch resolution
            # follows the happy path regardless of which file the real step used.
            self._write_transition_files(step.step_config, d)
            # Touch declared fixed output files so downstream reads/loops don't crash.
            self._touch_declared_outputs(step.step_config, d)
        return StepResult(outputs={},
                          flags={"passed": self._verdict, "has_suggestions": False})

    def _write_transition_files(self, step_config: dict, d: Path) -> None:
        for t in (step_config or {}).get("transitions") or []:
            match = t.get("match") if isinstance(t, dict) else None
            if not isinstance(match, dict):
                continue
            fname = match.get("from_file")
            if not fname:
                continue
            field = match.get("field", "passed")
            fp = d / fname
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(json.dumps({field: self._verdict, "stub": True}),
                              encoding="utf-8")
            except Exception:
                pass

    @staticmethod
    def _touch_declared_outputs(step_config: dict, d: Path) -> None:
        out = (step_config or {}).get("output") or {}
        fixed = out.get("fixed") if isinstance(out, dict) else None
        if not isinstance(fixed, dict):
            return
        for _slot, spec in fixed.items():
            fname = spec.get("file") if isinstance(spec, dict) else spec
            if not fname or "*" in str(fname):
                continue
            fp = d / fname
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                if not fp.exists():
                    fp.write_text("{}" if str(fname).endswith(".json") else "stub\n",
                                  encoding="utf-8")
            except Exception:
                pass
