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

    def __init__(self, verdict: bool = True, graph: dict | None = None):
        self._verdict = bool(verdict)
        # `ClaimedStep.step_config` is NOT the step's definition — it is skillflow's
        # opaque per-step `config:` key (`graph.py: config=s.get("config", {})`),
        # which every real graph leaves unset. Reading transitions/outputs from it
        # silently did nothing. Take them from the graph the smoke is booting.
        self._steps = {s.get("id"): s for s in ((graph or {}).get("steps") or [])
                       if isinstance(s, dict)}

    def _node(self, step: ClaimedStep) -> dict:
        return self._steps.get(step.step_id) or step.step_config or {}

    async def execute(self, step: ClaimedStep) -> StepResult:
        # Async wrapper for the StepRunner protocol; delegates to the sync path.
        return self.run(step)

    def run(self, step: ClaimedStep) -> StepResult:
        # Sync path — the smoke drives this directly (it may run inside an already-
        # running event loop, so it must not spin up its own).
        node = self._node(step)
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
            self._write_transition_files(node, d)
            # Touch declared fixed output files so downstream reads/loops don't crash.
            self._touch_declared_outputs(node, d)
        flags = {"passed": self._verdict, "has_suggestions": False}
        # ADD only; never override. The verdict-driven flags stay authoritative, so a
        # step branching on `passed` behaves exactly as before and the smoke keeps
        # catching a reviewer whose success edge matches the wrong value. Only keys
        # the stub cannot know (a tool's own contract: `verdict`, `synced`, …) are
        # filled in from the step's branches.
        for k, v in self._derived_flags(node).items():
            flags.setdefault(k, v)
        return StepResult(outputs={}, flags=flags)

    def _derived_flags(self, step_config: dict) -> dict:
        """Flags taken from the step's OWN transitions.

        A stubbed tool step used to return only `passed`/`has_suggestions` — flags no
        real tool emits. So a step branching on its tool's documented contract could
        never match: skillflow's native `pytest` returns `{"verdict": "passed"}`, and
        a correct `match: {verdict: passed}` failed the smoke every single time. The
        gate was punishing the convention the palette teaches ("a tool that can fail
        needs a failure edge — branch on the result").

        Only two of `_flags_match`'s four patterns are flag-based. `{from_file, field,
        value}` is fixture-backed by `_write_transition_files`, and `{from: checkpoint,
        ...}` routes on `_checkpoint_approved` — adopting either as flags would inject
        nonsense like `from="checkpoint"`. Take the happy path's flags on the happy
        run and the last branch's on the adversarial one.
        """
        eligible = []
        for t in (step_config or {}).get("transitions") or []:
            m = t.get("match") if isinstance(t, dict) else None
            if not isinstance(m, dict) or "from_file" in m or "from" in m:
                continue
            if "field" in m and "value" in m:          # indirect: flags[field] == value
                eligible.append({m["field"]: m["value"]})
            else:                                       # direct: {key: value, ...}
                pairs = {k: v for k, v in m.items() if k not in ("field", "value")}
                if pairs:
                    eligible.append(pairs)
        if not eligible:
            return {}
        return eligible[0] if self._verdict else eligible[-1]

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
