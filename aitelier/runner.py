"""AgentStepRunner — implements skillflow's StepRunner protocol.

Uses skillflow's resolved agent_config from ClaimedStep.inputs to know
which agent to call.  No hardcoded step_id → agent name mapping.
"""

from __future__ import annotations

from core.workspace_manager import DPE_GRAPH_NAME

import time as _time
from pathlib import Path

from skillflow.core import ClaimedStep, StepResult

# ORPHAN-DBG (temporary diagnostic — remove after the orphaned-claim root cause
# is pinned). Shared with core/scheduler.py via core.orphan_dbg so the run_step
# ENTER/EXIT thread traces ALSO land in the durable ~/.AItelier/orphan_dbg.log
# (they were previously stdout-only → wiped on container recreation).
from core.orphan_dbg import odbg as _odbg


# Backward-compat alias
AItelierStepRunner = None  # set after class definition


class AgentStepRunner:
    """Bridges skillflow agent steps to AItelier's PipelineEngine + LLMs.

    Reads ``agent_config_name`` from skillflow's ``ClaimedStep.inputs``
    (populated by AgentRegistry).  Falls back to step_config for backward
    compat with graphs that embed agent_config directly.
    """

    def __init__(self, db_manager, workspace_manager, agent_factory=None,
                 prompt_assembler=None, event_bus=None):
        self._db = db_manager
        self._ws = workspace_manager
        self._agent_factory = agent_factory
        self._prompt_assembler = prompt_assembler
        self._event_bus = event_bus

    def _resolve_agent_name(self, step: ClaimedStep) -> str:
        """Get agent_config name from skillflow's resolved inputs."""
        # Preferred: from AgentRegistry injection in claim_next_step
        ac = step.inputs.get("_agent_config", {})
        if isinstance(ac, dict) and ac.get("name"):
            return ac["name"]
        # Fallback: from graph's step_config
        return step.step_config.get("agent_config", "")

    # ── StepRunner protocol ──────────────────────────────────────

    async def execute(self, step: ClaimedStep) -> StepResult:
        """Execute an agent step.

        Called OUTSIDE any skillflow transaction.
        """
        project_id = step.run_context.get("project_id", "unknown")
        task_id = step.run_context.get("task_id")
        step_id = step.step_id
        agent_name = self._resolve_agent_name(step)

        # ── Workspace preparation ──────────────────────────────
        repo_info = self._db.get_repo_info(project_id)
        self._ws.setup_workspace(
            project_id,
            repo_type=repo_info.get("repo_type", "new"),
            repo_path=repo_info.get("repo_path"),
            repo_url=repo_info.get("repo_url"),
        )

        # ── Context is resolved by skillflow from graph config context specs ──

        # ── Run the LLM agent ───────────────────────────────────
        from core.dpe_pipeline import PipelineEngine

        from api.dependencies import get_skillflow
        sf = get_skillflow()

        # Resolve user language from the project's owner
        user_lang = None
        try:
            proj = self._db.get_project(project_id)
            if proj and proj.get("owner_email"):
                user_lang = self._db.get_user_lang(proj["owner_email"])
        except Exception:
            pass

        engine = PipelineEngine(
            log_callback=self._make_emit_wrapper(step),
            event_bus=self._event_bus,
            registry=sf.agent_registry,
            trace_callback=self._make_trace_wrapper(step),
            user_lang=user_lang,
        )

        # skillflow surfaces reject/loop-back feedback and validation errors
        # into _resolved_context itself (inside claim_next_step), so the host
        # renders them for free — no special-casing needed here.
        resolved_context = step.inputs.get("_resolved_context")
        tool_schemas = step.inputs.get("_tool_schemas", {})
        output_dir = step.inputs.get("_output_dir", "")
        max_tool_turns = step.inputs.get("_max_tool_turns", 0)
        run_id = step.token.run_id

        try:
            # Run the LLM step in a thread-pool executor so the uvicorn event
            # loop stays free to serve /health, /api/projects, SSE, etc.
            # Safe because:
            #   (a) _has_active_claim in scheduler.py prevents re-entrant
            #       ticks on the same run (version-mismatch guard);
            #   (b) step.emit() and step.trace() are synchronous DB appends
            #       protected by skillflow's RLock + WAL mode.
            import asyncio, threading
            loop = asyncio.get_running_loop()
            # ORPHAN-DBG: log ENTER/EXIT of run_step INSIDE the executor thread.
            # ENTER with no matching EXIT (or a very late EXIT) = the thread hung
            # (zombie), distinguishing a real hang from an await-boundary cancel.
            _cid = f"inst{step.token.step_instance_id}.v{step.token.version}"

            def _run_traced():
                _tid = threading.get_ident()
                _odbg(f"{_cid} run_step ENTER thread={_tid} step={step_id}")
                _rt0 = _time.time()
                try:
                    return engine.run_step(
                        task_id=task_id or 0,
                        step_id=step_id,
                        workspace=self._ws,
                        project_id=project_id,
                        agent_config_name=agent_name,
                        resolved_context=resolved_context,
                        tool_schemas=tool_schemas,
                        output_dir=output_dir,
                        max_tool_turns=max_tool_turns,
                        run_id=run_id,
                        step_instance_id=step.token.step_instance_id,
                    )
                finally:
                    _odbg(f"{_cid} run_step EXIT thread={_tid} step={step_id} "
                          f"elapsed={_time.time() - _rt0:.1f}s")

            await loop.run_in_executor(None, _run_traced)
        except Exception:
            raise  # Let skillflow's fail_step handle retries

        # FW-3: surface the review verdict into the StepResult so it lands in
        # skillflow_steps.result_flags_json. Pipeline routing already reads the
        # file directly; this just stops DB/analytics consumers seeing {}.
        outputs, flags = self._read_review_verdict(output_dir)
        return StepResult(outputs=outputs, flags=flags)

    @staticmethod
    def _read_review_verdict(output_dir: str) -> tuple[dict, dict]:
        """Read review_verdict.json (if a review step wrote one) → (outputs, flags).

        Resilient to trailing content after the JSON object (e.g. markdown
        appended by an over-eager agent that used a now-removed append_verdict
        tool). Uses raw_decode to extract just the first JSON value.
        """
        if not output_dir:
            return {}, {}
        try:
            import json as _json
            vf = Path(output_dir) / "review_verdict.json"
            if not vf.exists():
                return {}, {}
            raw = vf.read_text(encoding="utf-8").strip()
            decoder = _json.JSONDecoder()
            data, _end = decoder.raw_decode(raw)
            if not isinstance(data, dict):
                return {}, {}
            passed = data.get("passed")
            if passed is None and "verdict" in data:
                passed = (data.get("verdict") == "passed")
            suggestions = data.get("suggestions") or []
            flags = {
                "passed": bool(passed),
                "has_suggestions": bool(suggestions),
            }
            return {"review_verdict": data}, flags
        except Exception as e:
            # A verdict file that exists but can't be parsed (e.g. an invalid
            # \escape) previously vanished into empty flags and the run died
            # on an unmatched transition with no clue — log the real reason.
            _odbg(f"review_verdict.json unreadable in {output_dir}: {e}")
            return {}, {}

    def _make_emit_wrapper(self, step: ClaimedStep):
        """Bridge PipelineEngine's log callback to skillflow's emit.

        step.emit() is wired by skillflow to NotificationBus.publish_sync(),
        which creates an async task for real-time push + durable outbox write.
        Thread-safe: publish_sync uses loop.create_task() when a running loop
        exists, which is safe from any thread.
        """
        def callback(event_type: str, data: dict):
            step.emit(event_type, data)
        return callback

    @staticmethod
    def _make_trace_wrapper(step: ClaimedStep):
        """Bridge PipelineEngine's trace callback to skillflow's durable trace.

        step.trace is a synchronous DB append (run/step/instance ids prefilled
        by claim_next_step), so unlike emit it can be called directly from the
        engine's worker thread without an event loop.
        """
        def callback(category: str, event: str, payload: dict | None = None):
            try:
                step.trace(category, event, payload)
            except Exception:
                pass
        return callback


