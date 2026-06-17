# core/dpe_pipeline.py
# [修复说明] 修正了 Gate 物理拦截时的文件相对路径，并加入了 [DPE Debug] 控制台转播。
# [变更] step_id 从 int 改为 str；支持多 action 产出；新增 subtask 循环逻辑；
#        使用 commit_all_drafts 批量封卷；移除 Step 4.5，审核由 Step 4 Red Agent 承担。
#        升级为混合读写模型：Agent 通过工具软读取 project/，硬写入 Outbox_Draft，
#        DPE 在审查通过后回写 project/。
#        重构为三路分发：content-only / read+content / full tool，基于 StepProfile。

import json
import re
import time
from pathlib import Path
from typing import Any, Optional
from core.agents import AgentFactory
from core.workspace_manager import WorkspaceManager, DPE_GRAPH_NAME
from core.prompt_assembler import PromptAssembler


def _repair_json_content(raw: str) -> str | None:
    """Attempt to repair common LLM JSON malformations."""
    if not raw:
        return None
    repaired = raw.strip()
    for fence_prefix in ("```json", "```"):
        if repaired.startswith(fence_prefix):
            inner = repaired[len(fence_prefix):].lstrip()
            if inner.endswith("```"):
                inner = inner[:-3].rstrip()
            repaired = inner
            break
    repaired = repaired.replace("\\'", "'")
    repaired = re.sub(r',(\s*[}\]])', r'\1', repaired)
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        return None


class MaxRetriesExceeded(Exception):
    """达到最大重试次数熔断异常"""
    pass


class PipelineEngine:
    def __init__(self, log_callback=None,
                 repo_type: str = "new", event_bus=None, *, registry=None,
                 trace_callback=None):
        self.factory = AgentFactory(registry=registry)
        self.assembler = PromptAssembler(repo_type=repo_type)
        self._log = log_callback or (lambda *a, **kw: None)
        self._trace_cb = trace_callback or (lambda *a, **kw: None)
        self._event_bus = event_bus
        self._project_id = None
        self._current_step = None
        self._pipeline_start = None
        self._step_start = None
        self._repo_type = repo_type
        self._resolved_context: dict | None = None

    @staticmethod
    def _extract_json(text: str, try_multiple: bool = False) -> dict | None:
        """Extract JSON from LLM response. Only strips outermost code fences.

        CRITICAL: Previous implementation stripped ALL ``` markers, which corrupted
        JSON containing embedded markdown code blocks. This version uses regex to
        match only the outermost fence, preserving embedded code blocks.

        When try_multiple=True (used by content-mode steps), returns the FIRST JSON
        that has non-empty 'actions' OR contains a 'files' key. This handles cases
        where the LLM outputs multiple JSON objects like:
          {"thoughts": "...", "actions": []}
          {"files": {"step2_design.md": "..."}}
        """
        import re

        text = text.strip()

        # Step 1: Remove outermost code fence (```json or ```)
        # The fence is always at the very start and very end. We strip it directionally
        # rather than using a non-greedy regex, because the JSON content may contain
        # embedded markdown code blocks (```python, etc.) that confuse .+? matching.
        stripped_fence = False
        for prefix in ("```json", "```JSON", "```"):
            if text.startswith(prefix):
                # Find the LAST occurrence of ``` as closing fence
                # (content may contain embedded triple backticks in code blocks)
                last_fence = text.rfind("```")
                if last_fence > len(prefix):
                    # Cut off the opening fence + any whitespace/newline
                    after_prefix = text[len(prefix):last_fence].strip("\n\r \t")
                    stripped_fence = True
                    try:
                        return json.loads(after_prefix)
                    except json.JSONDecodeError:
                        text = after_prefix
                break

        # Step 2: Try direct parse (no outer fence, or fence content invalid)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Step 3: Brace matching for multiple/embedded JSON objects
        depth = 0
        start = None
        results = []
        for i, ch in enumerate(text):
            if ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start:i + 1])
                        results.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = None
                    continue

        if not results:
            return None

        if not try_multiple:
            return results[0]

        # For content-mode steps with multiple JSONs: merge all into one unified object
        # This handles cases where model outputs multiple JSONs (e.g., actions then files)
        if len(results) > 1:
            merged = {"thoughts": "", "actions": [], "files": {}}
            for obj in results:
                # Merge thoughts (take last non-empty)
                if obj.get("thoughts"):
                    merged["thoughts"] = obj["thoughts"]
                # Merge actions (accumulate all)
                if "actions" in obj and isinstance(obj["actions"], list):
                    merged["actions"].extend(obj["actions"])
                # Merge files (accumulate all)
                if "files" in obj and isinstance(obj["files"], dict):
                    merged["files"].update(obj["files"])

            # If we have files, return the merged object (files take priority)
            if merged["files"]:
                return merged
            # If we only have actions, return merged actions
            if merged["actions"]:
                return merged
            # Otherwise return first result
            return results[0]

        # Single JSON: prefer 'files' over 'actions'
        obj = results[0]
        if "files" in obj and isinstance(obj["files"], dict) and obj["files"]:
            return obj
        return obj

    @staticmethod
    def _detect_truncated_json(text: str) -> bool:
        """Detect if JSON response was truncated (unmatched braces at depth > 0).

        Fix 18: Detect model output truncation and enable recovery strategies.
        """
        depth = 0
        for ch in text:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
        return depth > 0  # Positive depth means unmatched open braces (truncated)

    @staticmethod
    def _repair_truncated_json(text: str) -> str | None:
        """Attempt to repair truncated JSON by adding missing closing braces.

        Fix 18: Simple repair strategy for truncated model outputs.
        """
        depth = 0
        for ch in text:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1

        if depth <= 0:
            return text  # Not truncated or over-closed

        # Add missing closing braces
        repaired = text.rstrip()
        # Remove trailing incomplete content (partial strings, etc.)
        # Find last valid structure point
        repaired = repaired.rstrip('"').rstrip(',').rstrip()
        # Add missing braces
        repaired += '\n' + ('}' * depth)
        return repaired

    def _make_feedback_example(self) -> str:
        """Build a step-aware JSON example for feedback messages.

        Uses the step's tool_schemas (from skillflow output.fixed) so the
        example shows the ACTUAL expected output files, not a hardcoded
        'task_verify_report.json' that misleads non-verifier steps.
        """
        ts = getattr(self, '_tool_schemas', {}) or {}
        # Collect expected output files from write tool descriptions
        example_files: dict[str, str] = {}
        for name, schema in ts.items():
            if name.startswith("write_"):
                desc = schema.get("description", "")
                # Extract filename from description like "Replace step1_sota.md with..."
                import re as _re
                m = _re.search(r'([\w][\w./-]*\.\w+)', desc)
                if m:
                    fname = m.group(1)
                    example_files[fname] = "<content here>"
        if not example_files:
            # Fallback: generic example
            example_files["output.md"] = "<content here>"
        files_example = ", ".join(
            f"'{k}': '<content here>'" for k in list(example_files.keys())[:3]
        )
        return (
            f"{{'thoughts': str, "
            f"'actions': [{{'tool': str, 'params': {{'content': str}}}}], "
            f"'files': {{{files_example}}}}}"
        )

    @staticmethod
    def _ensure_valid_json_content(filename: str, content: str) -> str:
        """Deterministically repair JSON content before writing to disk.
        Only acts on .json files. Returns repaired content or original if unrepairable."""
        # Check if this file should be JSON (by name or by sanitization result)
        safe_name = WorkspaceManager._sanitize_filename(filename, content)
        if not safe_name.endswith('.json'):
            return content
        try:
            json.loads(content)
            return content  # Already valid
        except json.JSONDecodeError:
            pass
        repaired = _repair_json_content(content)
        return repaired if repaired is not None else content

    def _emit(self, event_type: str, data: dict):
        """Emit structured event through skillflow notification bus (via step.emit)
        and print for local debugging."""
        payload = {**data}
        if self._current_step and "step_id" not in payload:
            payload["step_id"] = self._current_step
        if self._project_id and "project_id" not in payload:
            payload["project_id"] = self._project_id
        # Routes through _make_emit_wrapper → step.emit() → skillflow NotificationBus
        self._log(event_type, payload)

        preview = payload.get("preview", "")
        tag = f" {preview}" if preview else ""
        print(f"[DPE Debug] {event_type}{tag}")

    def _trace(self, category: str, event: str, payload: dict | None = None):
        """Append to skillflow's durable run trace (full prompts/responses).

        Framework records tool calls/results/lifecycle/steps; the host records
        what only it sees — the assembled prompts and raw model responses,
        keyed (by skillflow) on step_instance_id so loop iterations never
        overwrite one another.
        """
        try:
            self._trace_cb(category, event, payload or {})
        except Exception:
            pass

    def _get_project_path(self, workspace: Any, project_id: str) -> Path:
        """获取 DPS workspace 路径 (Inbox/Outbox/Trace)"""
        return workspace._get_secure_path(project_id)

    def _get_code_path(self, workspace: Any, project_id: str) -> Path:
        """获取 project 代码仓库路径"""
        return workspace.get_code_path(project_id)

    def _exec_tool(self, action: dict) -> dict:
        """Execute a tool action via skillflow. All tool execution is delegated.

        Host-level step-control tools (ask_more_turns) are handled here as
        no-ops; the runner detects them in the turn loop and acts accordingly.
        """
        tool_name = action.get("tool", "")
        if tool_name == "ask_more_turns":
            return {"status": "granted", "turns": action.get("params", {}).get("turns", 3)}
        from api.dependencies import get_skillflow
        sf = get_skillflow()
        return sf.execute_tool(
            tool_name, action.get("params", {}),
            run_id=getattr(self, '_run_id', ''),
            step_id=self._current_step or '',
            step_instance_id=getattr(self, '_step_instance_id', None),
            project_root=str(self._code_path) if hasattr(self, '_code_path') else '',
        )


    @staticmethod
    def _is_review_step(step_id: str) -> bool:
        return step_id.endswith("_review")

    @staticmethod
    def _agent_role(step_id: str) -> str:
        return "red" if step_id.endswith("_review") else "green"

    # ── Category A: Content step (write tools, no read tools) ──────────

    def _run_content_step(self, task_id: int, step_id: str, workspace: Any,
                          project_id: str, subtask_id: str | None = None,
                          agent_config_name: str = "") -> bool:
        """Single call with skillflow-generated write tools via actions."""
        agent = self.factory.get_agent(agent_config_name)
        role = self._agent_role(step_id)
        role_label = "Red Agent" if role == "red" else "Green Agent"
        project_path = self._get_project_path(workspace, project_id)
        code_path = self._get_code_path(workspace, project_id)
        self._code_path = code_path

        self._current_step = step_id
        self._step_start = time.time()
        prompt = self.assembler.assemble(
            step_id, project_path, "", "", task_id=task_id, code_path=code_path,
            resolved_context=self._resolved_context,
            tool_schemas=self._tool_schemas,
        )

        self._trace("prompt", "user_prompt", {"mode": "content", "role": role, "user": prompt})

        self._emit("agent_call", {"agent_role": role, "model": agent.gateway.litellm_model,
                                  "preview": f"{role_label} (content)"})
        t0 = time.time()
        response = agent.run(prompt)
        elapsed = time.time() - t0
        self._emit("agent_response", {"agent_role": role, "elapsed_s": round(elapsed, 1),
                                      "chars": len(response), "preview": response[:300]})
        self._trace("response", "agent_response", {"mode": "content", "role": role, "text": response})


        payload = self._extract_json(response)
        if payload is None:
            raise MaxRetriesExceeded(
                f"Step {step_id}: Failed to parse JSON. Response: {response[:200]}"
            )

        if "thoughts" in payload and payload["thoughts"]:
            self._emit("agent_message", {
                "content": str(payload["thoughts"])[:500],
                "level": "info",
            })

        # Execute skillflow-generated write tools from actions
        written_files: list[str] = []

        # Fallback: legacy "files" format → convert to generic write action per file
        actions = payload.get("actions", [])
        if not actions and "files" in payload and isinstance(payload["files"], dict):
            for fname, fcontent in payload["files"].items():
                actions.append({"tool": "write", "params": {"file": fname, "content": str(fcontent)}})

        for action in actions:
            tool_name = action.get("tool", "")
            if tool_name.startswith("write") or tool_name == "write":
                result = self._exec_tool(action)
                if "error" in result:
                    raise MaxRetriesExceeded(
                        f"Step {step_id}: write tool '{tool_name}' failed: {result['error']}"
                    )
                wf = result.get("written", "")
                if wf:
                    written_files.append(wf)

        if not written_files:
            raise MaxRetriesExceeded(
                f"Step {step_id}: No output. Use write_* tools in actions. "
                f"Response: {response[:300]}"
            )

        self._emit("files_written", {"files": written_files,
                     "preview": f"Written {len(written_files)} file(s) to Draft"})
        return True
    # ── Category B: Read tools + content output ──────────────────────

    def _run_tool_content_step(self, task_id: int, step_id: str, workspace: Any,
                               project_id: str, subtask_id: str | None = None,
                               agent_config_name: str = "") -> bool:
        """Multi-turn read loop, then content output with fixed filenames.
        Red review is handled by skillflow-level _review steps."""
        agent = self.factory.get_agent(agent_config_name)
        role = self._agent_role(step_id)
        role_label = "Red Agent" if role == "red" else "Green Agent"
        project_path = self._get_project_path(workspace, project_id)
        code_path = self._get_code_path(workspace, project_id)
        self._code_path = code_path  # for _exec_tool delegation

        feedback = ""
        rejection_history = []
        cached_exploration = []
        self._current_step = step_id
        self._step_start = time.time()
        max_retries = self.factory.get_max_retries(step_id)
        for attempt in range(1, max_retries + 1):
    # Priority: step config > agent config > default
            max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(step_id)

            # Pre-compute step-aware feedback templates so error messages
            # reference the actual expected output files, not a hardcoded
            # "task_verify_report.json" that misleads non-verifier steps.
            example_shape = self._make_feedback_example()
            write_names = sorted(
                k for k in (self._tool_schemas or {})
                if k.startswith(("write_", "create_", "append_"))
            ) or ["write_*"]
            tool_hint = ", ".join(write_names[:5])

            self._emit("step_attempt", {"step_id": step_id, "attempt": attempt, "max_attempts": max_retries,
                                        "preview": f"Step {step_id} Attempt {attempt}/{max_retries} (read+content)"})


            tool_results = []
            written_files = []
            current_max_turns = max_turns
            tool_turn = 0
            ended_early = False

            while tool_turn < current_max_turns:
                remaining = current_max_turns - tool_turn
                prompt = self.assembler.assemble(
                    step_id, project_path, "", feedback, task_id=task_id, code_path=code_path,
                    resolved_context=self._resolved_context,
                    tool_schemas=self._tool_schemas,
                )
                # Inject turn budget and step-control instructions.
                # finish_step is now a native tool in every step's schema;
                # remind the agent it's available.
                prompt += (
                    f"\n\n[Turn Budget: {remaining} remaining]\n"
                    "Step-control tools available in your tool list:\n"
                    "- finish_step(summary=\"...\") — signal all outputs written, complete the step\n"
                    "- ask_more_turns(turns=N, reason=\"...\") — request extra turns\n"
                    "Write files incrementally across turns. They accumulate.\n"
                    "When all required outputs are ready, call finish_step."
                )
                if cached_exploration and tool_turn == 0:
                    # Deduplicate: same tool call+result can appear multiple times
                    seen = set()
                    deduped = []
                    for entry in cached_exploration:
                        if entry not in seen:
                            seen.add(entry)
                            deduped.append(entry)
                    prompt += "\n\n[Cached Exploration Results from Previous Attempt]\n" + "\n".join(deduped)
                if tool_results:
                    prompt += "\n\n[Previous Tool Results]\n" + "\n".join(tool_results)
                if rejection_history:
                    prompt += "\n\n[Previous Rejection History]\n" + "\n---\n".join(rejection_history)


                self._emit("agent_call", {"agent_role": role, "model": agent.gateway.litellm_model,
                                          "turn": tool_turn + 1, "preview": f"{role_label} Turn {tool_turn + 1}"})
                self._trace("prompt", "user_prompt", {
                    "mode": "json", "role": role, "attempt": attempt,
                    "turn": tool_turn + 1, "user": prompt})
                t0 = time.time()
                response = agent.run(prompt)
                elapsed = time.time() - t0
                self._emit("agent_response", {"agent_role": role, "elapsed_s": round(elapsed, 1),
                                              "chars": len(response), "preview": response[:300]})
                self._trace("response", "agent_response", {
                    "mode": "json", "role": role, "attempt": attempt,
                    "turn": tool_turn + 1, "text": response})


                payload = self._extract_json(response, try_multiple=True)
                if payload is None:
                    # Fix 18: Detect and repair truncated JSON output
                    if self._detect_truncated_json(response):
                        self._emit("truncation_detected", {"preview": "JSON appears truncated, attempting repair"})
                        repaired_text = self._repair_truncated_json(response)
                        payload = self._extract_json(repaired_text, try_multiple=True)
                        if payload is not None:
                            self._emit("truncation_repaired", {"preview": "Successfully repaired truncated JSON"})
                            # Continue to process payload below
                            if "files" in payload and isinstance(payload["files"], dict):
                                for filename, content in payload["files"].items():
                                    if not filename or not content:
                                        continue
                                    safe_content = self._ensure_valid_json_content(filename, str(content))
                                    workspace.write_draft(project_id, step_id, filename, safe_content, graph_name=self._draft_graph_name())
                                    written_files.append(WorkspaceManager._sanitize_filename(filename, safe_content))
                                self._emit("files_written", {"files": written_files,
                                            "preview": f"Written {len(written_files)} file(s) (repaired)"})
                                break
                    if payload is None:
                        # Treat free-text response as a message from the agent.
                        # Stream it via SSE and feed it back as conversation context
                        # so the agent can continue in the next turn.
                        self._emit("agent_message", {
                            "agent_role": role,
                            "turn": tool_turn + 1,
                            "preview": response[:300],
                            "chars": len(response),
                        })
                        # A7 fix #2: inline concrete JSON schema + stuck-run guidance
                        # so the agent can recover from a parse failure.
                        feedback = (
                            f"[Your previous response was not valid JSON. "
                            f"Here is what you said]:\n\n{response}\n\n"
                            f"Now respond with valid JSON. REQUIRED SHAPE: "
                            f"{example_shape}. "
                            f"Available write tools: {tool_hint}. "
                            f"On tool error, do NOT retry the same call - list the error in your final report."
                        )
                        tool_turn += 1
                        continue

                # ── Output normalizer ──────────────────────────────────────
                # LLMs produce varied JSON shapes. Normalize common patterns into
                # the standard {actions, files} shape BEFORE the switch below so
                # the existing dispatch logic handles them without duplication.
                # Pattern: {"filename.md": "content", ...} → wrap in files dict
                if not payload.get("files") and not payload.get("actions"):
                    # Keys that look like output filenames (not metadata keys)
                    meta_keys = {"thoughts", "actions", "files", "message"}
                    file_keys = {
                        k: v for k, v in payload.items()
                        if k not in meta_keys
                        and isinstance(v, str)
                        and ("." in k or v.strip().startswith(("#", "{", "[", "<")))
                    }
                    if file_keys:
                        payload["files"] = file_keys
                        self._emit("payload_normalized", {
                            "pattern": "bare-filename-keys",
                            "files": list(file_keys.keys()),
                            "preview": f"Normalized {len(file_keys)} bare key(s) → files dict",
                        })

                # Check for final output (files dict or list)
                files_data = payload.get("files")
                if files_data:
                    if isinstance(files_data, dict):
                        for filename, content in files_data.items():
                            if not filename or not content:
                                continue
                            safe_content = self._ensure_valid_json_content(filename, str(content))
                            if isinstance(content, str) and content.startswith("{"):
                                try:
                                    parsed = json.loads(content)
                                    if isinstance(parsed, dict) and "content" in parsed:
                                        safe_content = parsed["content"]
                                except json.JSONDecodeError:
                                    pass
                            workspace.write_draft(project_id, step_id, filename, safe_content, graph_name=self._draft_graph_name())
                            written_files.append(WorkspaceManager._sanitize_filename(filename, safe_content))
                    elif isinstance(files_data, list):
                        for entry in files_data:
                            if not isinstance(entry, dict):
                                continue
                            filename = entry.get("path") or entry.get("file") or ""
                            content = entry.get("content") or ""
                            if not filename or not content:
                                continue
                            safe_content = self._ensure_valid_json_content(filename, str(content))
                            workspace.write_draft(project_id, step_id, filename, safe_content, graph_name=self._draft_graph_name())
                            written_files.append(WorkspaceManager._sanitize_filename(filename, safe_content))
                    if written_files:
                        self._emit("files_written", {"files": written_files,
                                                     "preview": f"Written {len(written_files)} file(s)"})
                        break

                # Check for tool exploration (actions)
                actions = payload.get("actions", [])
                if not actions:
                    # The agent returned valid JSON with thoughts but no actions/files.
                    # Treat this as a message turn — stream the thoughts, feed back
                    # as conversation context, and give another turn.
                    thoughts = payload.get("thoughts", "")
                    self._emit("agent_message", {
                        "agent_role": role,
                        "turn": tool_turn + 1,
                        "preview": (thoughts or "(no thoughts)")[:300],
                        "chars": len(response),
                    })
                    # A7 fix #2: inline concrete JSON schema + stuck-run guidance
                    # so the verifier escapes the parse-failure feedback loop.
                    feedback = (
                        f"[Your previous response contained thoughts but no actions or files. "
                        f"Here is what you thought]:\n\n{thoughts or response}\n\n"
                        f"Now respond with valid JSON. REQUIRED SHAPE: "
                        f"{example_shape}. "
                        f"Available write tools: {tool_hint}. "
                        f"On tool error, do NOT retry the same call - list in final report. "
                        f"If 7+ tool turns used, stop exploring and emit the report now."
                    )
                    tool_turn += 1
                    continue

                # Step-control pseudo-tools: finish_step/end_step, ask_more_turn.
                # Detected AFTER processing all other tool calls in this turn so
                # that multi-tool responses (e.g. write + finish) work correctly.
                end_step_call = next((a for a in actions if a.get("tool") in ("end_step", "finish_step")), None)
                ask_more_call = next((a for a in actions if a.get("tool") in ("ask_more_turn", "ask_more_turns")), None)

                # ask_more_turns: defer budget extension until after all tool calls
                # in this turn are executed, so write calls in the same response
                # aren't lost.
                if ask_more_call:
                    extra = int(ask_more_call.get("params", {}).get("turns", 3))
                    reason = ask_more_call.get("params", {}).get("reason", "")
                    self._emit("agent_turn_request", {
                        "extra_turns": extra, "reason": reason,
                        "remaining": current_max_turns - tool_turn - 1,
                        "preview": f"Agent asked for +{extra} turns ({reason[:80]})",
                    })

                tool_calls = [a for a in actions if a.get("tool") in ("read_file", "list_tree", "web_search", "web_fetch")]
                message_calls = [a for a in actions if a.get("tool") == "message"]

                # Resolve ALL allowed write/create/append tools from tool_schemas.
                # Must include create_* and append_* — the unknown-write check
                # below also matches these prefixes, so constraining to only
                # write_* would falsely flag create_verdict etc. as unknown.
                constrained_writes = {
                    k for k in self._tool_schemas
                    if k.startswith(("write_", "create_", "append_"))
                }
                if constrained_writes:
                    write_calls = [a for a in actions if a.get("tool") in constrained_writes]
                    generic_writes = [a for a in actions if a.get("tool") == "write"]
                    # Detect write-like tools that aren't in the allowed set
                    # (e.g. LLM invents "write_file" instead of "write_sota").
                    # Without this, unknown write tools are silently ignored →
                    # no output → retry loop with no feedback.
                    unknown_writes = [
                        a for a in actions
                        if (a.get("tool", "").startswith("write")
                            or a.get("tool", "").startswith("create_")
                            or a.get("tool", "").startswith("append_"))
                        and a.get("tool") not in constrained_writes
                        and a.get("tool") != "write"
                    ]
                    if generic_writes:
                        allowed_names = ", ".join(sorted(constrained_writes))
                        feedback = (
                            f"ERROR: You used the generic 'write' tool, but this step only allows "
                            f"constrained write tools: {allowed_names}. "
                            f"Each write_* tool produces a specific output file. "
                            f"Do NOT write code or arbitrary files — produce the plan/design output only."
                        )
                        self._emit("parse_error", {"error": feedback, "preview": "Wrong write tool"})
                        tool_results.append(feedback)
                        tool_turn += 1
                        continue
                    if unknown_writes:
                        allowed_names = ", ".join(sorted(constrained_writes))
                        bad_names = ", ".join(
                            f"'{a.get('tool','')}'" for a in unknown_writes
                        )
                        feedback = (
                            f"ERROR: Unknown write tool(s): {bad_names}. "
                            f"This step ONLY allows: {allowed_names}. "
                            f"Each tool writes a specific output file — use the "
                            f"exact tool names listed."
                        )
                        self._emit("parse_error", {"error": feedback, "preview": "Unknown write tool"})
                        tool_results.append(feedback)
                        tool_turn += 1
                        continue
                else:
                    write_calls = [a for a in actions if a.get("tool", "").startswith("write_") or a.get("tool") == "write"]

                if message_calls:
                    for action in message_calls:
                        content = action.get("params", {}).get("content", "")[:500]
                        level = action.get("params", {}).get("level", "info")
                        self._emit("agent_message", {
                            "content": content, "level": level,
                            "preview": content[:200]
                        })

                if tool_calls:
                    turn_results = []
                    for action in tool_calls:
                        result = self._exec_tool(action)
                        result_str = json.dumps(result, ensure_ascii=False)
                        params_str = json.dumps(action.get("params", {}), ensure_ascii=False)
                        entry = f"Tool: {action['tool']}({params_str})\nResult: {result_str}"
                        turn_results.append(entry)
                        # C3: Cache all exploration results
                        cached_exploration.append(entry)
                    tool_results.extend(turn_results)
                    self._emit("tool_calls", {"count": len(tool_calls), "preview": f"Executed {len(tool_calls)} tool call(s)"})

                if write_calls:
                    for action in write_calls:
                        result = self._exec_tool(action)
                        if "error" in result:
                            tool_results.append(f"Write error: {result['error']}")
                            continue
                        written_file = result.get("written", "")
                        if written_file:
                            written_files.append(written_file)
                    self._emit("files_written", {"files": written_files,
                                                 "preview": f"Written {len(written_files)} file(s)"})
                    # end_step or step 3 (multi-file): accumulate, don't break
                    if end_step_call:
                        summary = end_step_call.get("params", {}).get("summary", "Task split complete")
                        self._emit("agent_message", {
                            "content": f"end_step: {summary}", "level": "milestone",
                            "preview": f"Agent ended step: {summary[:150]}"
                        })
                        ended_early = True
                        break
                    if step_id == "3":
                        pass
                    else:
                        break

                # end_step without write calls — use previously accumulated files
                if end_step_call and written_files and not write_calls:
                    summary = end_step_call.get("params", {}).get("summary", "Task split complete")
                    self._emit("agent_message", {
                        "content": f"end_step: {summary}", "level": "milestone",
                        "preview": f"Agent ended step: {summary[:150]}"
                    })
                    ended_early = True
                    break

                # No write calls and no tool calls after message — agent signals work
                # is already done (no-op). Copy existing project files to draft if any.
                if not tool_calls and not written_files:
                    project_files = list(code_path.rglob("*")) if code_path and code_path.exists() else []
                    if project_files:
                        for f in project_files:
                            if f.is_file() and ".git" not in f.relative_to(code_path).parts:
                                rel = str(f.relative_to(code_path))
                                content = f.read_text(encoding="utf-8", errors="replace")
                                workspace.write_draft(project_id, step_id, rel, content, graph_name=self._draft_graph_name())
                                written_files.append(
                                    WorkspaceManager._sanitize_filename(rel, content))
                        if written_files:
                            self._emit("files_written", {
                                "files": written_files,
                                "preview": f"No-op: copied {len(written_files)} existing file(s)"
                            })
                    break

                # Apply ask_more_turns budget extension after all tool calls in
                # this turn have been processed (deferred from detection above).
                if ask_more_call:
                    extra = int(ask_more_call.get("params", {}).get("turns", 3))
                    reason = ask_more_call.get("params", {}).get("reason", "")
                    current_max_turns += extra
                    turn_entry = (
                        f"ask_more_turns: +{extra} turns granted. "
                        f"Reason: {reason}. Remaining: {current_max_turns - tool_turn - 1}"
                    )
                    tool_results.append(turn_entry)

                tool_turn += 1
                self._emit("exploration", {"turn": tool_turn, "preview": f"Exploration turn {tool_turn}"})
            else:
                # Max tool turns exceeded without producing files
                feedback = f"Max tool exploration turns ({max_turns}) exceeded."
                self._emit("tool_turns_exceeded", {"max_turns": max_turns, "preview": "Max tool turns exceeded"})
                # If no files were produced across ALL turns, fail immediately
                if not written_files:
                    raise MaxRetriesExceeded(
                        f"Step {step_id}: Agent exhausted {max_turns} tool exploration turns without producing any write actions. "
                        "The agent must produce at least one 'write' action to complete this step."
                    )
                continue

            if not written_files:
                feedback = feedback or "System Error: No files were produced."
                continue

            # Validation and draft→final promotion are handled by skillflow
            # lifecycle hooks (after_validate → draft_promote) in confirm_step().
            self._emit("step_done", {"step_id": step_id, "files": written_files,
                                     "preview": f"All Green! {len(written_files)} file(s) written"})
            return True

        raise MaxRetriesExceeded(
            f"Task {task_id} Step {step_id} aborted: Max retries ({max_retries}) exceeded. "
            f"Last feedback: {feedback}"
        )

    # ── Category C: Full tool step (read + write, dynamic filenames) ──

    def _run_tool_step(self, task_id: int, step_id: str, workspace: Any,
                       project_id: str, subtask_id: str | None = None,
                       agent_config_name: str = "") -> bool:
        """Full multi-turn tool loop with read_file/list_tree/write.
        Red review is handled by skillflow-level _review steps."""
        agent = self.factory.get_agent(agent_config_name)
        role = self._agent_role(step_id)
        role_label = "Red Agent" if role == "red" else "Green Agent"
        project_path = self._get_project_path(workspace, project_id)
        code_path = self._get_code_path(workspace, project_id)
        self._code_path = code_path  # for _exec_tool delegation

        feedback = ""
        rejection_history = []
        cached_exploration = []

        self._current_step = step_id
        self._step_start = time.time()
        max_retries = self.factory.get_max_retries(step_id)
        previously_passed_files = {}  # filename -> content from successful previous attempt
        message_count = 0
        MAX_MESSAGES_PER_STEP = 3

        for attempt in range(1, max_retries + 1):
            max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(step_id)

            self._emit("step_attempt", {"step_id": step_id, "attempt": attempt, "max_attempts": max_retries,
                                        "preview": f"Step {step_id} Attempt {attempt}/{max_retries}"})


            tool_results = []
            written_files = []

            # C2: Re-inject previously passed files so agent only fixes failing ones
            if previously_passed_files and attempt > 1:
                prev_files_section = "[Previously Written Files — DO NOT REWRITE THESE]\n"
                prev_files_section += "These files have already passed validation. Only fix the failing files.\n"
                for fname, fcontent in previously_passed_files.items():
                    prev_files_section += f"\n--- {fname} (already passed) ---\n```\n{fcontent}\n```\n"
                tool_results.insert(0, prev_files_section)

            for tool_turn in range(max_turns):
                prompt = self.assembler.assemble(
                    step_id, project_path, "", feedback, task_id=task_id, code_path=code_path,
                    resolved_context=self._resolved_context,
                    tool_schemas=self._tool_schemas,
                )
                if cached_exploration and tool_turn == 0:
                    # Deduplicate: same tool call+result can appear multiple times
                    seen = set()
                    deduped = []
                    for entry in cached_exploration:
                        if entry not in seen:
                            seen.add(entry)
                            deduped.append(entry)
                    prompt += "\n\n[Cached Exploration Results from Previous Attempt]\n" + "\n".join(deduped)
                if tool_results:
                    prompt += "\n\n[Previous Tool Results]\n" + "\n".join(tool_results)
                if rejection_history:
                    prompt += "\n\n[Previous Rejection History]\n" + "\n---\n".join(rejection_history)


                self._emit("agent_call", {"agent_role": role, "model": agent.gateway.litellm_model,
                                          "turn": tool_turn + 1, "preview": f"{role_label} Turn {tool_turn + 1}"})
                self._trace("prompt", "user_prompt", {
                    "mode": "json", "role": role, "attempt": attempt,
                    "turn": tool_turn + 1, "user": prompt})
                t0 = time.time()
                response = agent.run(prompt)
                elapsed = time.time() - t0
                self._emit("agent_response", {"agent_role": role, "elapsed_s": round(elapsed, 1),
                                              "chars": len(response), "preview": response[:300]})
                self._trace("response", "agent_response", {
                    "mode": "json", "role": role, "attempt": attempt,
                    "turn": tool_turn + 1, "text": response})


                payload = self._extract_json(response)
                if payload is None:
                    # Prose fallback: auto-convert non-JSON output to user-visible message
                    message_count += 1
                    if message_count <= MAX_MESSAGES_PER_STEP:
                        self._emit("agent_message", {
                            "content": response[:500],
                            "level": "info",
                            "auto_converted": True,
                            "preview": f"[auto] {response[:200]}"
                        })
                    feedback = (
                        "System Error: Failed to parse JSON. "
                        "You MUST respond with ONLY a JSON object like: "
                        '{\"thoughts\": \"...\", \"actions\": [{\"tool\": \"write\", \"params\": {\"file\": \"path\", \"content\": \"...\"}}]}. '
                        "Do NOT add any text before or after the JSON."
                    )
                    self._emit("parse_error", {"error": feedback, "preview": "JSON Parse Error"})
                    break

                if isinstance(payload, list):
                    payload = {"thoughts": "", "actions": payload}

                actions = payload.get("actions", [])
                if not actions:
                    feedback = "System Error: No actions found in response."
                    self._emit("parse_error", {"error": feedback, "preview": "No actions in response"})
                    break

                tool_calls = [a for a in actions if a.get("tool") in ("read_file", "list_tree", "web_search", "web_fetch")]
                write_calls = [a for a in actions if a.get("tool", "").startswith("write_") or a.get("tool") == "write"]
                message_calls = [a for a in actions if a.get("tool") == "message"]

                # Handle message actions — emit to user, count toward budget
                if message_calls:
                    for action in message_calls:
                        message_count += 1
                        if message_count <= MAX_MESSAGES_PER_STEP:
                            content = action.get("params", {}).get("content", "")[:500]
                            level = action.get("params", {}).get("level", "info")
                            self._emit("agent_message", {
                                "content": content, "level": level,
                                "auto_converted": False,
                                "preview": content[:200]
                            })
                    # Message-only turn: no tool or write calls — continue exploring
                    if not tool_calls and not write_calls:
                        self._emit("exploration", {"turn": tool_turn + 1,
                                                    "preview": f"Agent sent message, turn {tool_turn + 1}"})
                        continue

                if tool_calls:
                    turn_results = []
                    for action in tool_calls:
                        result = self._exec_tool(action)
                        result_str = json.dumps(result, ensure_ascii=False)
                        params_str = json.dumps(action.get("params", {}), ensure_ascii=False)
                        entry = f"Tool: {action['tool']}({params_str})\nResult: {result_str}"
                        turn_results.append(entry)
                        # C3: Cache all exploration results
                        cached_exploration.append(entry)
                    tool_results.extend(turn_results)
                    self._emit("tool_calls", {"count": len(tool_calls), "tools": [a.get("tool") for a in tool_calls],
                                              "preview": f"Executed {len(tool_calls)} tool call(s)"})

                if write_calls:
                    for action in write_calls:
                        result = self._exec_tool(action)
                        if "error" in result:
                            tool_results.append(f"Write error: {result['error']}")
                            continue
                        written_file = result.get("written", "")
                        if written_file:
                            written_files.append(written_file)

                    self._emit("files_written", {"files": written_files,
                                                 "preview": f"Written {len(written_files)} file(s)"})
                    break

                # No write calls and no tool calls — agent signals no-op completion.
                # Copy existing project files to draft so validation can proceed.
                if not tool_calls and not written_files:
                    project_files = list(code_path.rglob("*")) if code_path and code_path.exists() else []
                    if project_files:
                        for f in project_files:
                            if f.is_file() and ".git" not in f.relative_to(code_path).parts:
                                rel = str(f.relative_to(code_path))
                                content = f.read_text(encoding="utf-8", errors="replace")
                                workspace.write_draft(project_id, step_id, rel, content, graph_name=self._draft_graph_name())
                                written_files.append(
                                    WorkspaceManager._sanitize_filename(rel, content))
                        if written_files:
                            self._emit("files_written", {
                                "files": written_files,
                                "preview": f"No-op: copied {len(written_files)} existing file(s)"
                            })
                    break

                self._emit("exploration", {"turn": tool_turn + 1, "preview": f"Exploration turn {tool_turn + 1}, continuing..."})
            else:
                self._emit("tool_turns_exceeded", {"max_turns": max_turns, "preview": "Max tool turns exceeded"})
                if not written_files:
                    self._emit("no_files_written", {"max_turns": max_turns})
                    raise MaxRetriesExceeded(
                        f"Task {task_id} Step {step_id}: No file writes produced after {max_turns} tool exploration turn(s). "
                        "The agent must produce at least one 'write' action to complete this step. "
                        "Sending messages or making read/list calls is not sufficient — you MUST write files."
                    )
                feedback = (f"Max tool exploration turns ({max_turns}) exceeded. "
                            "You MUST produce write actions now.")
                continue

            # Inject previously_passed_files into tmp dir so they get committed together
            if previously_passed_files:
                draft_dir = project_path / DPE_GRAPH_NAME / f"{step_id}.tmp"
                draft_dir.mkdir(parents=True, exist_ok=True)
                for fname, fcontent in previously_passed_files.items():
                    if fname not in written_files:
                        fpath = draft_dir / fname
                        fpath.parent.mkdir(parents=True, exist_ok=True)
                        fpath.write_text(fcontent, encoding="utf-8")
                        written_files.append(fname)

            # Validation and draft→final promotion are handled by skillflow
            # lifecycle hooks (after_validate → draft_promote) in confirm_step().
            self._emit("step_done", {"step_id": step_id, "files": written_files,
                                     "preview": f"All Green! {len(written_files)} file(s) written"})
            return True

        raise MaxRetriesExceeded(
            f"Task {task_id} Step {step_id} aborted: Max retries ({max_retries}) exceeded. "
            f"Last feedback: {feedback}"
        )

    # ── Step dispatch ─────────────────────────────────────────────────

    # ── Native tool calling ─────────────────────────────────────────

    @staticmethod
    def _to_openai_tools(tool_schemas: dict) -> list[dict]:
        """Convert skillflow write tool schemas to OpenAI function format.

        Each schema: {name: {description, parameters: {param: {type, required, description}}}}
        Output: [{type: "function", function: {name, description, parameters: {type, properties, required}}}]
        """
        tools = []
        for name, schema in tool_schemas.items():
            params_spec = schema.get("parameters", {})
            properties = {}
            required: list[str] = []
            for pname, pspec in params_spec.items():
                if not isinstance(pspec, dict):
                    continue
                properties[pname] = {
                    "type": pspec.get("type", "string"),
                    "description": pspec.get("description", ""),
                }
                if pspec.get("required"):
                    required.append(pname)

            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return tools

    def _run_native_step(self, task_id: int, step_id: str, workspace: Any,
                         project_id: str, agent_config_name: str = "",
                         subtask_id: str | None = None) -> bool:
        """Native tool-calling conversation loop.

        Uses litellm's native tools parameter. Tool results are injected
        as role:"tool" messages, building up a multi-turn conversation
        until the model signals completion (no more tool_calls).
        """
        agent = self.factory.get_native_agent(agent_config_name)
        role = self._agent_role(step_id)
        role_label = "Red Agent" if role == "red" else "Green Agent"
        project_path = self._get_project_path(workspace, project_id)
        code_path = self._get_code_path(workspace, project_id)
        self._code_path = code_path

        feedback = ""
        self._current_step = step_id
        self._step_start = time.time()
        # Tool schemas → OpenAI format.
        # Inject ask_more_turns (host-level step-control tool) alongside
        # skillflow-generated tools so the agent can request extra turns.
        if "ask_more_turns" not in self._tool_schemas:
            self._tool_schemas["ask_more_turns"] = {
                "name": "ask_more_turns",
                "description": (
                    "Request extra tool-calling turns before the step's turn "
                    "budget is exhausted. Use this when you need more iterations "
                    "to complete all required outputs."
                ),
                "parameters": {
                    "turns": {"type": "integer", "required": True,
                             "description": "Number of extra turns to request"},
                    "reason": {"type": "string", "required": False,
                              "description": "Why extra turns are needed"},
                },
            }
        write_tool_names = {k for k in self._tool_schemas if k.startswith("write_") or k.startswith("create_") or k.startswith("append_") or k == "write"}
        native_tools = self._to_openai_tools(self._tool_schemas)

        max_retries = self.factory.get_max_retries(step_id)
        max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(step_id)

        for attempt in range(1, max_retries + 1):
            self._emit("step_attempt", {
                "step_id": step_id, "attempt": attempt,
                "max_attempts": max_retries,
                "mode": "native",
                "preview": f"Step {step_id} Attempt {attempt}/{max_retries} (native)",
            })

            # Build initial messages
            user_prompt = self.assembler.assemble(
                step_id, project_path, "", feedback,
                task_id=task_id, code_path=code_path,
                resolved_context=self._resolved_context,
                tool_schemas=self._tool_schemas,
                native=True,
            )

            # Inject turn budget so the agent can pace exploration
            user_prompt += (
                f"\n\n[Turn Budget: {max_turns} turns total, then forced output]\n"
                "Plan your exploration accordingly. After gathering enough context, "
                "call write_*/create_* to produce the required output, then "
                "finish_step to complete. Do not exhaust all turns on exploration — "
                "leave at least 1 turn for writing."
            )

            messages: list[dict] = [
                {"role": "system", "content": agent.system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            self._trace("prompt", "user_prompt", {
                "attempt": attempt, "mode": "native",
                "system": agent.system_prompt, "user": user_prompt,
            })

            written_files: list[str] = []
            turn_count = 0
            last_reasoning = ""  # cached for deepseek: replay on tool-only turns

            for turn_count in range(max_turns):
                remaining = max_turns - turn_count
                if remaining > 1:
                    tool_choice = "auto"
                elif not written_files and write_tool_names:
                    # Final turn and the step still has no output: FORCE the
                    # write tool rather than forbidding tools. Exploration-heavy
                    # models (e.g. deepseek) otherwise burn the whole budget on
                    # read/search tools and reach the last turn with nothing
                    # written; tool_choice="none" then makes writing impossible,
                    # so the step ends empty and fails validation. Forcing the
                    # write tool guarantees the step produces its output file.
                    forced = sorted(write_tool_names)[0]
                    tool_choice = {"type": "function",
                                   "function": {"name": forced}}
                else:
                    tool_choice = "none"

                self._emit("agent_call", {
                    "agent_role": role, "turn": turn_count + 1,
                    "mode": "native",
                    "preview": f"{role_label} Turn {turn_count + 1} (native)",
                })
                t0 = time.time()

                try:
                    result = agent.turn(
                        messages=messages, tools=native_tools,
                        tool_choice=tool_choice,
                    )
                except Exception as e:
                    self._emit("native_error", {"error": str(e)[:200]})
                    feedback = f"Native tool calling error: {e}. Response truncated."
                    break

                elapsed = time.time() - t0
                self._emit("agent_response", {
                    "agent_role": role, "elapsed_s": round(elapsed, 1),
                    "chars": len(result.text),
                    "tool_calls": len(result.tool_calls),
                    "preview": result.text[:300] if result.text else f"[{len(result.tool_calls)} tool call(s)]",
                })

                # Record trace
                self._trace("response", "agent_response", {
                    "attempt": attempt, "turn": turn_count + 1,
                    "text": result.text or "",
                    "tool_calls": [tc["function"]["name"] for tc in result.tool_calls],
                    "tool_args": [tc["function"].get("arguments", "")[:500] for tc in result.tool_calls],
                })

                if result.text:
                    self._emit("agent_message", {
                        "content": result.text[:500],
                        "level": "info",
                        "preview": result.text[:200],
                    })

                if not result.tool_calls:
                    # Agent signalled completion (no more tool calls)
                    break

                # Execute tool calls
                assistant_msg: dict = {"role": "assistant", "content": result.text or None}
                if result.tool_calls:
                    assistant_msg["tool_calls"] = result.tool_calls
                # DeepSeek thinking + tools: reasoning_content MUST appear on every
                # subsequent turn (absent or empty → 400).  Cache and replay.
                if result.reasoning_content:
                    last_reasoning = result.reasoning_content
                if last_reasoning:
                    assistant_msg["reasoning_content"] = last_reasoning
                messages.append(assistant_msg)

                called_finish = False
                ask_more_extra = 0
                ask_more_reason = ""
                for tc in result.tool_calls:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    if tool_name == "finish_step":
                        called_finish = True
                    elif tool_name == "ask_more_turns":
                        try:
                            params = json.loads(fn["arguments"])
                            ask_more_extra = int(params.get("turns", 3))
                            ask_more_reason = params.get("reason", "")
                        except (json.JSONDecodeError, ValueError, KeyError):
                            ask_more_extra = 3
                    try:
                        params = json.loads(fn["arguments"])
                    except json.JSONDecodeError:
                        params = {}

                    tool_result = self._exec_tool({"tool": tool_name, "params": params})
                    result_str = json.dumps(tool_result, ensure_ascii=False)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })

                    # Track written files
                    wf = tool_result.get("written", "")
                    if wf:
                        written_files.append(wf)


                # Apply ask_more_turns budget extension after all tool calls
                # in this turn have been processed.
                if ask_more_extra > 0:
                    max_turns += ask_more_extra
                    self._emit("agent_turn_request", {
                        "extra_turns": ask_more_extra,
                        "reason": ask_more_reason,
                        "remaining": max_turns - turn_count - 1,
                        "preview": f"Agent asked for +{ask_more_extra} turns ({ask_more_reason[:80]})",
                    })

                # finish_step detected after ALL tool calls are processed.
                # Break the turn loop so the step proceeds to validation.
                if called_finish:
                    break

                self._emit("exploration", {
                    "turn": turn_count + 1, "mode": "native",
                    "preview": f"Executed {len(result.tool_calls)} tool call(s)",
                })

            if not written_files:
                feedback = (
                    f"Step {step_id}: No output produced. "
                    f"Use write_*/create_*/append_* tools to write output files."
                )
                if turn_count >= max_turns - 1:
                    feedback = f"Max turns ({max_turns}) exceeded. " + feedback
                self._emit("step_retry", {"attempt": attempt, "error": feedback[:200]})
                continue

            self._emit("files_written", {
                "files": written_files,
                "preview": f"Written {len(written_files)} file(s) (native)",
            })
            return True

        raise MaxRetriesExceeded(
            f"Step {step_id}: Max retries ({max_retries}) exceeded in native mode."
        )

    def _draft_graph_name(self) -> str:
        """Graph config for draft writes, derived from skillflow's output_dir
        (workspaces/<pid>/<graph>/<step>.tmp) so outputs land in the RUN's own
        config dir — not the hardcoded DPE default. For DPE runs this equals
        dpe_default_v2 (unchanged); for meta_conversation it is meta_conversation."""
        od = getattr(self, "_output_dir", "")
        if od:
            from pathlib import Path
            return Path(od).parent.name
        from core.workspace_manager import DPE_GRAPH_NAME
        return DPE_GRAPH_NAME

    # ── Dispatch ─────────────────────────────────────────────────────

    def run_step(self, task_id: int, step_id: str, workspace: Any,
                 project_id: str = "default", subtask_id: str | None = None,
                 agent_config_name: str = "",
                 resolved_context: dict | None = None,
                 tool_schemas: dict | None = None,
                 output_dir: str = "",
                 max_tool_turns: int = 0,
                 run_id: str = "",
                 step_instance_id: int | None = None) -> bool:
        """
        Dispatch to the appropriate step execution path.

        agent_config_name, tool_schemas, output_dir, run_id come from
        skillflow's ClaimedStep.inputs.
        max_tool_turns overrides agent config default when > 0.
        Red review is handled by skillflow-level _review steps.
        """
        self._project_id = project_id
        self._resolved_context = resolved_context
        self._tool_schemas = tool_schemas or {}
        self._output_dir = output_dir
        self._max_tool_turns = max_tool_turns
        self._run_id = run_id
        self._step_instance_id = step_instance_id

        # Prefer native tool calling if agent config enables it
        if self.factory.is_native(agent_config_name):
            try:
                return self._run_native_step(
                    task_id, step_id, workspace, project_id,
                    agent_config_name, subtask_id,
                )
            except Exception as e:
                import logging
                logging.getLogger("aitelier.dpe").warning(
                    f"Native step '{step_id}' failed: {type(e).__name__}: {e}",
                    exc_info=True,
                )
                if not self.factory.get_fallback_to_json(agent_config_name):
                    raise
                # Fall through to JSON mode dispatch below
                self._emit("native_fallback", {
                    "step_id": step_id,
                    "preview": f"Native failed ({type(e).__name__}), falling back to JSON mode",
                })

        # Dispatch based on skillflow-provided tool_schemas
        ts = self._tool_schemas
        has_read_tools = any(
            not k.startswith("write") and k != "write"
            for k in ts
        )
        has_write_tools = any(
            k.startswith("write") for k in ts
        )
        has_generic_write = "write" in ts

        if has_read_tools and has_write_tools:
            return self._run_tool_content_step(task_id, step_id, workspace,
                                               project_id, subtask_id,
                                               agent_config_name)
        elif has_write_tools and not has_read_tools:
            return self._run_content_step(task_id, step_id, workspace,
                                          project_id, subtask_id,
                                          agent_config_name)
        else:
            return self._run_tool_step(task_id, step_id, workspace,
                                       project_id, subtask_id,
                                       agent_config_name)

    # ── Manifest helpers ──────────────────────────────────────────────

    # 支持的 manifest 文件名前缀（LLM 可能输出不同命名）
    _MANIFEST_PREFIXES = ("tasks_manifest", "task_manifest", "subtasks_manifest")
