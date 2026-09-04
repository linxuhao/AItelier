# core/dpe_pipeline.py
# [修复说明] 修正了 Gate 物理拦截时的文件相对路径，并加入了 [DPE Debug] 控制台转播。
# [变更] step_id 从 int 改为 str；支持多 action 产出；新增 subtask 循环逻辑；
#        使用 commit_all_drafts 批量封卷；移除 Step 4.5，审核由 Step 4 Red Agent 承担。
#        升级为混合读写模型：Agent 通过工具软读取 project/，硬写入 Outbox_Draft，
#        DPE 在审查通过后回写 project/。
#        重构为三路分发：content-only / read+content / full tool，基于 StepProfile。

import logging
import os
import json
import threading
import re
import time
from pathlib import Path
from typing import Any, Optional
from core.agents import AgentFactory
from core.workspace_manager import WorkspaceManager, DPE_GRAPH_NAME
from core.prompt_assembler import (PromptAssembler, build_language_instruction,
                                   is_mutation_tool)


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


# How many turns before the cap the agent is warned. One turn is too
# late to finish anything; the existing final-turn nudge already covers
# the nothing-written cliff.
_LOW_TURN_BUDGET = 3
# ask_more_turns: how many grants one step may take and how long each may be.
# Unbounded before 2026-09-04; bounded now so a step that keeps asking cannot
# loop forever, and named in the low-budget warning so a step with real work
# left asks instead of being cut off (R5: 15 exhaustions, 0 asks).
_MAX_TURN_GRANTS = 2
_GRANT_TURNS_MAX = 6


def _grant_turns(turn_grants: int, asked: int) -> tuple[int, int, str]:
    """Decide one ask_more_turns call: (extra, new_turn_grants, message).

    ONE place for both turn loops. The JSON-actions loop capped grants at
    4d4638d; the native tool-calling loop — the one every real run takes —
    kept applying the raw request, so a t_impl went 30 -> 54 turns in four
    asks (R5, instance 3598) while the warning text promised "at most 2 grants
    of up to 6 turns".
    """
    if turn_grants >= _MAX_TURN_GRANTS:
        return 0, turn_grants, (
            f"ask_more_turns: DENIED — {_MAX_TURN_GRANTS} grants already used "
            f"this step. Finish now: deliver what exists and list what is "
            f"missing in the delivery notes.")
    extra = max(0, min(int(asked), _GRANT_TURNS_MAX))
    turn_grants += 1
    return extra, turn_grants, (
        f"ask_more_turns: +{extra} turns granted ({turn_grants}/{_MAX_TURN_GRANTS}).")

# Argument names an agent may never set on a tool call: the host injects them.
_AGENT_RESERVED_ARGS = ("project_root", "workspace_root", "step_dir", "out_dir")


def _strip_agent_roots(params) -> dict:
    """Drop host-owned root arguments from an agent-supplied tool call."""
    if not isinstance(params, dict):
        return {}
    dropped = [k for k in params if k in _AGENT_RESERVED_ARGS]
    if dropped:
        logging.getLogger("aitelier.dpe").warning(
            "tool call supplied host-owned argument(s) %s — ignored", dropped)
    return {k: v for k, v in params.items() if k not in _AGENT_RESERVED_ARGS}

class PipelineEngine:
    def __init__(self, log_callback=None,
                 repo_type: str = "new", event_bus=None, *, registry=None,
                 trace_callback=None, user_lang: str | None = None,
                 llm_progress=None):
        # llm_progress: liveness hook handed down to every gateway the factory
        # builds (see AIGateway.on_progress): ticks every few seconds while a
        # completion streams, so a watcher can tell a long response from a
        # wedged one.
        self.factory = AgentFactory(registry=registry, on_progress=llm_progress)
        self.assembler = PromptAssembler(repo_type=repo_type, user_lang=user_lang)
        # Kept on the engine too: tool-phase ticks ("running run_tests") come
        # from the tool-execution loop, not from a gateway.
        self._llm_progress = llm_progress
        self._log = log_callback or (lambda *a, **kw: None)
        self._trace_cb = trace_callback or (lambda *a, **kw: None)
        self._event_bus = event_bus
        self._project_id = None
        self._current_step = None
        self._pipeline_start = None
        self._step_start = None
        self._repo_type = repo_type
        self._resolved_context: dict | None = None
        self._validation_error: str | None = None
        self._user_lang = user_lang
        # See _note_feedback: how many times each distinct failure message has
        # been handed to a step.
        self._feedback_seen: dict[tuple[str, str], int] = {}
        self._feedback_repeats: dict[str, int] = {}
        # True while the pending feedback came from an exploratory turn (thoughts
        # with no actions) rather than a rejection. See _note_feedback.
        self._feedback_exploratory = False

    # Delivered three times unchanged: a recovering agent essentially never sees
    # this, and every one of the twelve harness defects did.
    _FEEDBACK_REPEAT_ALARM = 3
    # Process-wide, keyed by (run_id, step_id, message) — see _note_feedback for
    # why it cannot live on the instance. FIFO-evicted at the cap.
    #
    # GUARDED, because agent steps run in a thread pool (scheduler.py's tick
    # notes, runner.py's `loop.run_in_executor`) and 42 same-run overlapping step
    # executions are on record in this host's own DB. Unsynchronised, the eviction
    # sweep below races: `list()` against a concurrent insert raises RuntimeError,
    # and two threads passing the size check both delete the same keys. Either
    # would abort the step — a diagnostic must never be able to kill the thing it
    # observes.
    _FEEDBACK_SEEN: dict[tuple[str, str, str], int] = {}
    _FEEDBACK_SEEN_CAP = 2000
    _FEEDBACK_LOCK = threading.Lock()

    def _note_phase(self, phase: str, tool_name: str = "") -> None:
        """Emit a liveness phase tick ("tool"/"tool_done") — never raises."""
        if not self._llm_progress:
            return
        try:
            self._llm_progress({"phase": phase, "tool": tool_name})
        except Exception:
            pass

    def _note_feedback(self, step_id: str, feedback: str,
                       *, exploratory: bool = False) -> int:
        """Count how often this step has been handed the SAME failure text.

        This is a detector for the defect CLASS rather than for any one site.
        Twelve times over six drives the harness computed the fact that explained
        a failure and dropped it before the agent could act on it; the agent then
        retried and was handed the identical, cause-free sentence again. That
        repetition is the observable runtime signature, and it is the same
        whichever of the seven syntactic forms produced it — a swallowed
        exception, an uninformative `.get` default, an if/elif chain with no else,
        a name-prefix test, `check=False`, an empty filter, or a `break` before
        the reason was rendered. Nothing in the system looked at it, so each
        instance cost a drive to find.

        A message that does not name a cause cannot be acted on, so it comes back
        verbatim. When it does, say so: `_FEEDBACK_REPEAT_ALARM` deliveries raise
        an event, and the count rides along on the step's final error, where an
        operator reads it without opening a trace.

        An EXPLORATORY turn does not count. A response carrying thoughts but no
        actions is the agent using its message path, which the loop deliberately
        allows — not the harness failing to explain itself. Replaying this detector
        over 108 runs / 60k trace rows: counting every feedback-bearing prompt
        fires 16 times with 10 false alarms (38% precision), and 9 of those 10
        carry the thinking-turn message. Skipping it leaves 7 alarms, 1 false,
        and all 6 genuinely-stuck steps still caught — 86%.

        (Measured, not assumed: my first idea was to count per ASSIGNMENT instead
        of per turn. Replayed, that scored 12% — worse — because the write-failure
        branches are exactly the true positives and they cannot be reconstructed
        from an agent response alone. The turn is the right unit; the exploratory
        branch was the wrong input.)

        Detection only — the prompt is not altered. What to do about an
        unactionable message is a judgement about that message.
        """
        if not feedback or exploratory or self._feedback_exploratory:
            return 0
        key = (getattr(self, "_run_id", "") or "", step_id,
               " ".join(feedback.split()))
        # Counted across STEP CLAIMS, not just across the retries inside one.
        # A PipelineEngine is built per claimed step, so per-instance counting
        # would see only the in-step retry loop — and the repetition that
        # actually cost drives spans FIX LAPS, where a graph returns to the same
        # maker again and again with the same complaint. Keyed by run so two
        # runs never pool, and bounded so a long-lived server cannot grow it
        # without limit.
        with self._FEEDBACK_LOCK:
            n = self._FEEDBACK_SEEN.get(key, 0) + 1
            if len(self._FEEDBACK_SEEN) >= self._FEEDBACK_SEEN_CAP:
                for stale in list(self._FEEDBACK_SEEN)[:self._FEEDBACK_SEEN_CAP // 4]:
                    self._FEEDBACK_SEEN.pop(stale, None)
            self._FEEDBACK_SEEN[key] = n
        self._feedback_seen[key] = n
        self._feedback_repeats[step_id] = max(
            self._feedback_repeats.get(step_id, 0), n)
        if n == self._FEEDBACK_REPEAT_ALARM:
            payload = {"step_id": step_id, "count": n,
                       "feedback": feedback[:500],
                       "preview": (f"Same failure message handed to {step_id} "
                                   f"{n}x — it may not name a cause")}
            self._emit("feedback_repeated", payload)
            self._trace("diagnostic", "feedback_repeated", payload)
        return n

    @staticmethod
    def _unresolved_note(agent_config_name: str) -> str:
        """Name the role's tool grants that do not resolve, on the step that failed.

        skillflow records these (`SkillFlow.unresolved_tools()`, whose own
        docstring says "a host can surface this after registration") and no host
        ever did — the fact was produced and left sitting, which is the same shape
        as the defect it was added to fix. A role granted a tool that does not
        exist registers clean and runs WITHOUT it, so the only symptom is a step
        that mysteriously produces nothing. That is exactly the step raising here,
        so this is where the answer belongs.
        """
        if not agent_config_name:
            return ""
        try:
            from api.dependencies import get_skillflow
            missing = get_skillflow().unresolved_tools().get(
                f"agent_config:{agent_config_name}")
        except Exception:
            import logging
            logging.getLogger("aitelier.dpe").warning(
                "could not read unresolved tool grants", exc_info=True)
            return ""
        if not missing:
            return ""
        return (f" NOTE: role '{agent_config_name}' is granted tool(s) that do "
                f"not exist and were silently dropped at registration: "
                f"{', '.join(sorted(missing))}. The step ran without them.")

    def _repeat_note(self, step_id: str) -> str:
        """Tail for a step's final error, when its feedback never changed."""
        n = self._feedback_repeats.get(step_id, 0)
        if n < self._FEEDBACK_REPEAT_ALARM:
            return ""
        return (f" (the same message was delivered {n}x unchanged — if it does "
                f"not name a cause, the gap is in the harness, not the agent)")

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

    def _get_code_path(self, workspace: Any, project_id: str) -> Path | None:
        """获取 project 代码仓库路径; None = 这个 run 声明它没有代码仓库。

        Passed straight to skillflow as `project_root` (see `_exec_tool`) and to
        the prompt assembler, so the host must not invent one here: skillflow's
        own code-path resolver answers the same question and the two have to
        agree about the same run.
        """
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
        # The roots are the HOST's to inject, never the agent's to choose.
        # skillflow's call site does `kwargs.setdefault("project_root", …)`, so
        # an agent that passes `project_root` in its arguments would win over
        # the injected one and point a root-resolving tool (semantic_search,
        # run_tests, …) at any path the container can see. Strip them here.
        params = _strip_agent_roots(action.get("params", {}))
        return sf.execute_tool(
            tool_name, params,
            run_id=getattr(self, '_run_id', ''),
            step_id=self._current_step or '',
            step_instance_id=getattr(self, '_step_instance_id', None),
            # Fencing token: an executor that was reclaimed mid-step is refused
            # here instead of invoking tools alongside its replacement. 0 means
            # unfenced, and skillflow skips the check when either side is 0, so
            # a claim already in flight across the upgrade is never rejected.
            claim_epoch=getattr(self, '_claim_epoch', 0),
            # "" — never `str(None)`, which is the RELATIVE path "None".
            #
            # What "" MEANS to skillflow depends on the version installed, and
            # this comment used to assert the version we wanted rather than the
            # one that runs: from 1.5.52 `execute_tool` treats "" as "no opinion"
            # and asks its own code_path_resolver (which answers False for a
            # repo-less run, and the argument is then OMITTED); every earlier
            # release — including the 1.5.46 the container installs from PyPI —
            # forwards "" straight into `project_root` AND `workspace_root`, and
            # `Path("").resolve()` is the process CWD, i.e. the server's own
            # checkout.
            #
            # So the host does NOT rely on the engine here. What keeps a
            # repo-less run off the CWD on BOTH engine versions is the tool's own
            # guard — and which tools have one is NOT listed here. It was, and
            # the list was wrong: it claimed to cover "every AItelier tool that
            # resolves a root" and omitted four (capability_declarations_known,
            # gdscript_check, user_stories_present, and tasks_manifest_complete —
            # the first three `Path(workspace_root or ".")`, the last
            # `Path(workspace_root or step_dir or … or ".")`). A prose list of 27
            # tools cannot stay true across a release, and a reader who guards
            # the tools it names inherits its omissions.
            #
            # Two inventories carry it instead, both COMPLETE BY CONSTRUCTION:
            #
            #   * tests/unit/test_tool_root_guard_inventory.py enumerates every
            #     tool under aitelier/tools/ whose entry point declares
            #     `project_root`/`workspace_root` and fails until each is
            #     classified guarded / unguarded / does-not-resolve;
            #   * tests/unit/test_a_capability_grant_survives_the_deployed_engine.py
            #     covers the half the first one cannot see. A NATIVE skillflow
            #     tool's guard ships with the PyPI package, not with this repo,
            #     so this host cannot assert anything about it — the container
            #     runs whatever version `pip install` resolved (1.5.46 today,
            #     whose `pytest` has no guard). That test therefore forbids
            #     granting a native root-resolving tool at all, rather than
            #     trusting a guard it cannot deploy.
            #
            # The conclusion that matters here: no repo-less pipeline reaches an
            # unguarded root-resolver today. The only repo-less shape granting
            # root-resolving tools is `tool_creation`
            # (write/run_tests/register_tool/register_capability): `write` never
            # reaches ToolLoader (the engine's write path claims the name first),
            # `run_tests` refuses a non-absolute project_root, and the two
            # register_* tools declare no root. skillflow's own
            # `read_file`/`list_tree` skip an empty root rather than resolving
            # it. A generated pipeline that lists an unguarded tool WOULD reach
            # one — guard the tool before offering it to a `repo_mode: none`
            # config.
            project_root=str(getattr(self, '_code_path', '') or ''),
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
            user_lang=self._user_lang,
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
        if payload is not None:
            payload, _norm = self._normalize_payload(payload, self._tool_schemas)
            if _norm:
                self._emit("payload_normalized", _norm)
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

    # The mutation vocabulary skillflow actually injects for a step, from its
    # `output.mode`: `mode: write` gives generic `create` / `edit` / `write`,
    # `mode: content` gives per-slot `create_<slot>` / `write_<slot>` / `edit_<slot>`.
    _GENERIC_MUTATORS = ("write", "create", "edit")
    _SLOT_MUTATOR_PREFIXES = ("write_", "create_", "append_", "edit_")

    # Read tools the engine executes itself, plus the step-control pseudo-tools.
    # Everything an agent may legitimately ask for is one of: a read, a write
    # (see `_is_mutation_tool`), a message, or one of these controls.
    _READ_TOOLS = ("read_file", "list_tree", "web_search", "web_fetch")
    _CONTROL_TOOLS = ("finish_step", "end_step", "ask_more_turn", "ask_more_turns")

    @classmethod
    def _classify_actions(cls, actions: list, tool_schemas: dict | None,
                          write_calls: list) -> tuple[list, list, list, list]:
        """Partition a turn's actions into (reads, messages, controls, unclaimed).

        This is turn accounting, and it exists because the same defect has now been
        found thirteen times: the engine receives a complete delivery, fails to
        recognise how it was spelled, and drops it without a word. Each earlier
        instance was fixed by teaching the parser one more spelling; the partition
        is what makes the NEXT unrecognised spelling report itself.

        The identity every handler holds: writes + reads + messages + controls +
        unclaimed == actions. Nothing an agent asked for may vanish. `write_calls`
        is passed in already computed because the two write vocabularies differ
        (constrained `write_<slot>` vs generic `create`/`edit`); everything else is
        classified here, once.

        A read is anything the step was GRANTED, not a hardcoded name list. The
        list was `read_file`/`list_tree`/`web_search`/`web_fetch`, so an agent
        calling `read`, `search` or `list` — the unified read surface skillflow
        injects into every step, tools the step demonstrably HAS — had the call
        silently dropped. Asking the grant instead of the spelling is the same
        correction `_is_mutation_tool` already made for writes.
        """
        claimed = {id(a) for a in write_calls}
        reads: list = []
        messages: list = []
        controls: list = []
        unclaimed: list = []
        for a in actions:
            if id(a) in claimed:
                continue
            if not isinstance(a, dict):
                unclaimed.append(a)
                continue
            name = a.get("tool") or ""
            if name in cls._CONTROL_TOOLS:
                controls.append(a)
            elif name == "message":
                messages.append(a)
            elif name in cls._READ_TOOLS or name in (tool_schemas or {}):
                reads.append(a)
            else:
                unclaimed.append(a)
        return reads, messages, controls, unclaimed

    @staticmethod
    def _unclaimed_feedback(unclaimed: list, tool_schemas: dict | None) -> str:
        """Answer a discarded action instead of swallowing it.

        Names what was not understood AND what the step can actually call — an
        agent told only "that didn't work" re-guesses, which is exactly how one
        step produced eight different envelope shapes across four drives.
        """
        names = []
        for a in unclaimed:
            if not isinstance(a, dict):
                names.append(f"(not an object: {type(a).__name__})")
            else:
                names.append(f"'{a.get('tool') or a.get('name') or '(unnamed)'}'")
        available = ", ".join(sorted(tool_schemas or {})) or "(none)"
        return (
            f"ERROR: {len(unclaimed)} action(s) in your last response were not "
            f"executed because this step has no such tool: {', '.join(names)}. "
            f"Nothing was written for them. This step can ONLY call: {available}. "
            f"Re-send the same work using one of those exact names."
        )

    @staticmethod
    def _is_mutation_tool(name: str, tool_schemas: dict | None) -> bool:
        """Would calling `name` write a file in THIS step?

        Classification used to be by name prefix alone — `write_*`, `create_*`,
        `append_*`, plus the bare `write`. That silently excluded `create` and
        `edit`, which are exactly the two tools skillflow injects into every
        `mode: write` step and exactly the two the forge palette teaches agents to
        use. An agent calling `create(file, content)` had its call classified as
        neither a write, nor a read, nor an unknown write — so it was dropped
        without a word, the step produced nothing, and the retry feedback said
        "Nothing was written". Four attempts, four complete deliveries discarded.

        Membership in the step's own schemas is required, so an invented name is
        still not executed. It is not dropped either: `_classify_actions` leaves it
        unclaimed and both handlers answer it with the names the step really has.
        (This docstring used to assert that hand-off as already true. It was not —
        the branch it named existed only on the constrained-slot path — and an
        unverified claim about a downstream owner is precisely how the rest of this
        family of defects survived. Turn accounting is what makes it true.)
        """
        return is_mutation_tool(name, tool_schemas or {})

    @staticmethod
    def _normalize_payload(payload: dict, tool_schemas: dict | None) -> tuple[dict, dict | None]:
        """Coerce a model's JSON into the canonical {files, actions} shape.

        Extracted from the tool loop so it can be tested directly — the shapes it
        absorbs were each found by reading a trace of a step that had genuinely done
        the work and had it thrown away.

        Returns (payload, emit-payload-or-None). The payload is mutated in place and
        also returned, so callers read naturally.
        """
        # `actions` and its per-call keys have as many spellings as the file
        # envelope does. Observed live, all carrying complete work:
        #   {"tools": [{"tool": "write_file", "args": {...}}]}
        #   {"command": "create", "path": ..., "content": ...}
        # Canonicalise the container and the per-call keys FIRST, so everything
        # below (and the unknown-write branch, which teaches the agent what it may
        # actually call) sees one shape.
        _known_names = set(tool_schemas or {}) | {
            "read_file", "list_tree", "web_search", "web_fetch",
            "finish_step", "end_step", "ask_more_turns", "message"}

        def _looks_like_calls(entries) -> bool:
            """Guard against a CONTENT key that happens to be named `tools`.

            A spec-writing step legitimately returns
            `{"tools": [{"name": "word_frequency", "description": ...}]}` as its
            OUTPUT. Converting that to actions would invent a call to a tool named
            `word_frequency`, produce no writes, and fail the step — trading one
            discard for another. Only convert when an entry actually names a tool
            this step can call.
            """
            for e in entries:
                if not isinstance(e, dict):
                    continue
                # A CALL carries arguments; a content record carries prose.
                if any(isinstance(e.get(k), dict)
                       for k in ("args", "params", "arguments", "parameters", "input")):
                    return True
                # Or it names a tool this step can actually call (a no-arg call).
                # `isinstance(..., str)` is load-bearing: OpenAI's own envelope
                # puts a DICT under `function` ({"name":…, "arguments":…}), and
                # `dict in set` raises `TypeError: unhashable type: 'dict'`, which
                # propagates out of the turn loop and fails the step with a
                # framework traceback. A real C1 response in this host's traces
                # (gen-mcp-server-builder-c8168ed4, seq 227) is exactly that shape;
                # it predates this function by eleven hours, which is the only
                # reason it has never fired.
                if any(isinstance(e.get(k), str) and e.get(k) in _known_names
                       for k in ("tool", "name", "command", "function")):
                    return True
                # The OpenAI tool-call shape itself. `_run_native_step` already
                # understands it; guarding without ABSORBING it would just trade a
                # crash for a silent discard, which is the defect this whole file
                # is about.
                fn = e.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    return True
            return False

        for alias in ("tools", "tool_calls", "calls"):
            entries = payload.get(alias)
            if (not payload.get("actions") and isinstance(entries, list)
                    and _looks_like_calls(entries)):
                payload["actions"] = payload.pop(alias)
                break
        if isinstance(payload.get("actions"), list):
            for a in payload["actions"]:
                if not isinstance(a, dict):
                    continue
                for k in ("args", "arguments", "parameters", "input"):
                    if "params" not in a and isinstance(a.get(k), dict):
                        a["params"] = a.pop(k)
                        break
                # OpenAI's nested form: {"function": {"name": …, "arguments": …}}.
                # `arguments` is a JSON *string* there, so parse it — a raw string
                # under `params` is not a call the dispatcher can make.
                fn = a.get("function")
                if isinstance(fn, dict):
                    if not a.get("tool") and isinstance(fn.get("name"), str):
                        a["tool"] = fn["name"]
                    if "params" not in a:
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, ValueError):
                                args = None
                        if isinstance(args, dict):
                            a["params"] = args
                for k in ("name", "command", "function"):
                    if not a.get("tool") and isinstance(a.get(k), str):
                        a["tool"] = a[k]
                        break

        _PATH_KEYS = ("path", "file", "file_path", "filename", "filepath")

        if payload.get("actions"):
            return payload, None

        files = payload.get("files")
        if files:
            # `files` is the CANONICAL, documented delivery shape — and
            # `_run_tool_step`, the handler every `mode: write` step uses, reads
            # only `actions`, so a payload that arrives already in the right shape
            # was answered "No actions found in your response". Seen live: after
            # `create` refused ("already exists — use 'edit'"), the step delivered
            # via `files` and was told it had sent nothing, then spent its
            # remaining turns reading. AA8 fixed this for the {path, content}
            # envelope by emitting an equivalent action and left the shape the
            # prompt actually documents unhandled.
            # Handlers that read `files` write from it and break before actions
            # run, so emitting both cannot double-write.
            mutator = next((t for t in ("create", "write", "edit")
                            if t in (tool_schemas or {})), None)
            # All three shapes `_run_tool_content_step` accepts, so the two
            # handlers agree on what a delivery is. A JSON-object body is the
            # natural shape for a `.json` deliverable and a str-only filter drops
            # it; the list form is rarer but is on record in this host's traces.
            pairs: list = []
            if isinstance(files, dict):
                for k, v in files.items():
                    if not isinstance(k, str) or not k.strip():
                        continue
                    pairs.append((k, v if isinstance(v, str)
                                  else json.dumps(v, ensure_ascii=False, indent=2)))
            elif isinstance(files, list):
                for entry in files:
                    if not isinstance(entry, dict):
                        continue
                    name_ = next((entry[x] for x in _PATH_KEYS
                                  if isinstance(entry.get(x), str) and entry[x].strip()), None)
                    if not name_ or "content" not in entry:
                        continue
                    body_ = entry["content"]
                    pairs.append((name_, body_ if isinstance(body_, str)
                                  else json.dumps(body_, ensure_ascii=False, indent=2)))
            if mutator and pairs:
                payload["actions"] = [
                    {"tool": mutator, "params": {"file": name, "content": body}}
                    for name, body in pairs]
                return payload, {"pattern": "files-envelope-to-actions",
                                 "files": [n for n, _ in pairs],
                                 "preview": (f"Mirrored files -> {len(pairs)} "
                                             f"{mutator} action(s)")}
            return payload, None


        # {"path": "x.py", "content": "..."} — the shape a model reaches for when it
        # has ONE file to deliver. A step wrote a complete pytest suite as
        # {"file": "tests/test_tools.py", "content": ...} and then, next attempt, as
        # {"file_path": ..., "content": ...}; both were discarded and both answered
        # with "Nothing was written", until the step failed. The work existed — only
        # the envelope was unrecognised.
        name = next((payload[k] for k in _PATH_KEYS
                     if isinstance(payload.get(k), str) and payload[k].strip()), None)
        body = payload.get("content")
        if name and isinstance(body, str):
            payload["files"] = {name: body}
            # Emit the equivalent ACTION too. `_run_tool_step` — the handler every
            # `mode: write` step in this repo uses — reads only `actions` and
            # ignores `files` entirely, so normalising to `files` alone left the
            # delivery in a dead end there. Handlers that read `files` take it
            # first and stop, so this cannot double-write.
            # An explicitly named tool WINS. Live failure: after `create` correctly
            # refused ("already exists — use 'edit'"), the agent came back with
            # {"tool": "edit", "file_path": …, "content": …} — the right call — and
            # this rule rewrote it back to `create`, which failed identically. The
            # agent had already corrected itself and was overruled. If `edit` is then
            # the wrong shape for a full rewrite, `edit`'s OWN error says so, and the
            # agent gets a turn to act on it.
            # Restricted to MUTATORS: this envelope carries a file body, so a named
            # `read_file` here would emit a read call with a `content` param — a call
            # that cannot do what the payload plainly intends.
            named = next((payload[k] for k in ("tool", "action", "command")
                          if isinstance(payload.get(k), str)
                          and PipelineEngine._is_mutation_tool(payload[k], tool_schemas)),
                         None)
            mutator = named or next((t for t in ("create", "write", "edit")
                                     if t in (tool_schemas or {})), None)
            if mutator:
                payload["actions"] = [{"tool": mutator,
                                       "params": {"file": name, "content": body}}]
            return payload, {"pattern": "single-file-envelope", "files": [name],
                             "preview": f"Normalized {{path, content}} -> files ({name})"}

        # {"filename.md": "content", ...} — bare filename keys. `content` and the
        # path aliases are excluded because they are the envelope above; treating
        # `content` as a FILENAME writes a file literally called "content".
        meta_keys = {"thoughts", "thought", "actions", "action", "files", "message",
                     "content", "reasoning", "summary", *_PATH_KEYS}
        file_keys = {k: v for k, v in payload.items()
                     if k not in meta_keys and isinstance(v, str)
                     and ("." in k or v.strip().startswith(("#", "{", "[", "<")))}
        if file_keys:
            payload["files"] = file_keys
            return payload, {"pattern": "bare-filename-keys",
                             "files": list(file_keys.keys()),
                             "preview": f"Normalized {len(file_keys)} bare key(s) -> files"}

        # {"<tool_name>": {params}} or {"action": "<tool>", ...} — a tool call that
        # never reached the dispatcher. A model asking to READ before writing emitted
        # {"read_file": {"file_path": "src/..."}} and, next attempt,
        # {"thought": "...", "action": "read_file", "path": "src/..."}. The read never
        # happened, the step ended empty, and the retry feedback ("you wrote nothing")
        # never answered the question the agent was actually asking.
        known = set(tool_schemas or {}) | {
            "read_file", "list_tree", "web_search", "web_fetch",
            "finish_step", "end_step", "ask_more_turns", "message"}
        # The flat form names its tool under any of these. `tool` was missing here
        # and it is the most natural spelling of all: an agent that had been told
        # "use 'edit'", read the file to obtain `old_str`, and returned
        # {"tool": "edit", "path": …, "old_str": …, "new_str": …} — a perfectly
        # formed call — had it dropped, because only `action` was recognised.
        _NAME_KEYS = ("action", "tool", "command", "function")
        act = None
        named = next((k for k in _NAME_KEYS
                      if isinstance(payload.get(k), str) and payload[k] in known), None)
        if named:
            act = {"tool": payload[named],
                   "params": {k: v for k, v in payload.items()
                              if k not in _NAME_KEYS + ("thought", "thoughts")}}
        else:
            hit = next((k for k in payload
                        if k in known and isinstance(payload[k], dict)), None)
            if hit:
                act = {"tool": hit, "params": payload[hit]}
        if act:
            payload["actions"] = [act]
            return payload, {"pattern": "bare-tool-call", "files": [],
                             "preview": f"Normalized bare tool call -> actions ({act['tool']})"}

        return payload, None

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
    # Priority: step config > agent config > default. Resolve by
            # agent_config_name (role) — the registry is keyed by role, not
            # step_id, so passing step_id silently fell back to DEFAULT.
            max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(agent_config_name)

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
            effects: list[str] = []   # non-file output: see _effect_name
            current_max_turns = max_turns
            turn_grants = 0
            tool_turn = 0
            ended_early = False

            while tool_turn < current_max_turns:
                remaining = current_max_turns - tool_turn
                self._note_feedback(step_id, feedback)
                prompt = self.assembler.assemble(
                    step_id, project_path, "", feedback, task_id=task_id, code_path=code_path,
                    resolved_context=self._resolved_context,
                    tool_schemas=self._tool_schemas,
                    user_lang=self._user_lang,
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
                # [Language] — injected ONCE, as the genuinely last block (after
                # tool results / rejection history) for maximal recency override.
                lang_instruction = build_language_instruction(self._user_lang)
                if lang_instruction:
                    prompt += "\n\n" + lang_instruction


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
                        self._feedback_exploratory = False
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
                # LLMs produce varied JSON shapes for the same intent. Normalize
                # them into the standard {actions, files} shape BEFORE the dispatch
                # below, so the existing logic handles them without duplication.
                payload, _norm = self._normalize_payload(payload, self._tool_schemas)
                if _norm:
                    self._emit("payload_normalized", _norm)

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
                    self._feedback_exploratory = True
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


                # Resolve ALL allowed write/create/append tools from tool_schemas.
                # Must include create_* and append_* — the unknown-write check
                # below also matches these prefixes, so constraining to only
                # write_* would falsely flag create_verdict etc. as unknown.
                # `edit_` belongs here. skillflow emits write_/create_/edit_ per
                # slot together (write_tools.py), and its own description marks
                # `edit_<slot>` PREFERRED on a revision round — "a full rewrite
                # from memory silently corrupts unflagged parts". Omitting the
                # prefix put every granted `edit_<slot>` into `unknown_writes`,
                # which told the agent a tool it HAS does not exist and pushed it
                # onto a full rewrite. The three prefixes always appear together,
                # so this cannot change which branch a step takes.
                constrained_writes = {
                    k for k in self._tool_schemas
                    if k.startswith(("write_", "create_", "append_", "edit_"))
                }
                if constrained_writes:
                    _dicts = [a for a in actions if isinstance(a, dict)]
                    write_calls = [a for a in _dicts if a.get("tool") in constrained_writes]
                    generic_writes = [a for a in _dicts if a.get("tool") == "write"]
                    # Detect write-like tools that aren't in the allowed set
                    # (e.g. LLM invents "write_file" instead of "write_sota").
                    # Without this, unknown write tools are silently ignored →
                    # no output → retry loop with no feedback.
                    unknown_writes = [
                        a for a in _dicts
                        if (a.get("tool", "").startswith(
                                ("write", "create", "append", "edit"))
                            or a.get("tool", "") in self._GENERIC_MUTATORS)
                        and a.get("tool") not in constrained_writes
                        and a.get("tool") != "write"
                    ]
                    if generic_writes:
                        allowed_names = ", ".join(sorted(constrained_writes))
                        self._feedback_exploratory = False
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
                        self._feedback_exploratory = False
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
                    # No fixed slots → `mode: write`, whose vocabulary is the
                    # generic create/edit/write. See _is_mutation_tool.
                    write_calls = [a for a in actions if isinstance(a, dict)
                                   and self._is_mutation_tool(a.get("tool", ""),
                                                              self._tool_schemas)]

                # Turn accounting — see `_classify_actions`. The two branches above
                # answer an unknown WRITE-shaped name on the constrained-slot path;
                # nothing answered anything else. An action naming `bash`,
                # `str_replace` or `apply_patch` — or any name at all on the
                # `mode: write` path — was claimed by no bucket and fell through to
                # the no-op branch below, which returns SUCCESS with zero files.
                (tool_calls, message_calls, _control_calls,
                 unclaimed) = self._classify_actions(
                     actions, self._tool_schemas, write_calls)
                if unclaimed:
                    self._feedback_exploratory = False
                    feedback = self._unclaimed_feedback(unclaimed, self._tool_schemas)
                    self._emit("unknown_tool", {
                        "error": feedback,
                        "tools": [a.get("tool") if isinstance(a, dict) else None
                                  for a in unclaimed],
                        "preview": f"{len(unclaimed)} action(s) named an unavailable tool"})
                    tool_results.append(feedback)
                    tool_turn += 1
                    continue

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
                        # A granted tool that CHANGED something counts as output
                        # even though it left no file in staging (repo_remove_file, a
                        # durable state write). See _effect_name.
                        wf = self._written_name(result)
                        if wf:
                            written_files.append(wf)
                        else:
                            eff = self._effect_name(result)
                            if eff:
                                effects.append(eff)
                    tool_results.extend(turn_results)
                    self._emit("tool_calls", {"count": len(tool_calls), "preview": f"Executed {len(tool_calls)} tool call(s)"})

                if write_calls:
                    for action in write_calls:
                        result = self._exec_tool(action)
                        if "error" in result:
                            tool_results.append(f"Write error: {result['error']}")
                            continue
                        written_file = self._written_name(result)
                        if written_file:
                            written_files.append(written_file)
                    # Only stop when a write actually LANDED. `break` used to fire
                    # unconditionally, so a turn whose every write errored ended the
                    # loop with the reason captured in `tool_results` and never
                    # shown to anyone. Live example: on a fix-loop step's second
                    # visit, `create` returned "'tests/test_tools.py' already exists
                    # — use 'edit'" (the file was in the repo from the first pass,
                    # though this step's staging was empty). The agent was never
                    # given the turn in which it could have switched to `edit`; the
                    # step reported writing nothing and failed validation.
                    if not written_files:
                        self._feedback_exploratory = False
                        feedback = ("Every write in your last response failed:\n"
                                    + "\n".join(tool_results[-len(write_calls):]))
                        self._emit("write_failed", {
                            "error": feedback,
                            "preview": f"All {len(write_calls)} write(s) failed"})
                        tool_turn += 1
                        continue
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

                # No write calls and no tool calls after message — agent signals
                # the work is already done (no-op). Complete cleanly with empty
                # output instead of copying the whole repo into the draft (which
                # produced wholesale commits and corrupted binaries).
                # The agent signalled completion and the step DID something, just
                # not to a file in staging — a queued deletion, a durable state
                # write. Counting only files made that look like a no-op: the engine
                # performed the change, then spent the rest of the budget before
                # failing for "no file writes produced".
                if _control_calls and effects and not written_files:
                    self._emit("step_done", {
                        "step_id": step_id, "files": [], "effects": effects,
                        "preview": f"No file written; {len(effects)} state change(s)",
                    })
                    return True

                if not tool_calls and not written_files:
                    self._emit("step_done", {
                        "step_id": step_id, "files": [],
                        "preview": "No change needed (no writes)",
                    })
                    return True

                # Apply ask_more_turns budget extension after all tool calls in
                # this turn have been processed (deferred from detection above).
                if ask_more_call:
                    reason = ask_more_call.get("params", {}).get("reason", "")
                    extra, turn_grants, msg = _grant_turns(
                        turn_grants, ask_more_call.get("params", {}).get("turns", 3))
                    current_max_turns += extra
                    tool_results.append(
                        f"{msg} Reason: {reason}. Remaining: {current_max_turns - tool_turn - 1}")

                tool_turn += 1
                self._emit("exploration", {"turn": tool_turn, "preview": f"Exploration turn {tool_turn}"})
            else:
                # Max tool turns exceeded without producing files
                self._feedback_exploratory = False
                feedback = f"Max tool exploration turns ({max_turns}) exceeded."
                self._emit("tool_turns_exceeded", {"max_turns": max_turns, "preview": "Max tool turns exceeded"})
                # If no files were produced across ALL turns, fail immediately
                if not written_files and not effects:
                    raise MaxRetriesExceeded(
                        f"Step {step_id}: Agent exhausted {max_turns} tool exploration turns without producing any write actions. "
                        "The agent must produce at least one 'write' action to complete this step."
                        + self._repeat_note(step_id) + self._unresolved_note(agent_config_name)
                    )
                continue

            if not written_files and not effects:
                self._feedback_exploratory = False
                feedback = feedback or "System Error: No files were produced."
                continue

            # Validation and draft→final promotion are handled by skillflow
            # lifecycle hooks (after_validate → draft_promote) in confirm_step().
            self._emit("step_done", {"step_id": step_id, "files": written_files,
                                     "preview": f"All Green! {len(written_files)} file(s) written"})
            return True

        raise MaxRetriesExceeded(
            f"Task {task_id} Step {step_id} aborted: Max retries ({max_retries}) exceeded. "
            f"Last feedback: {feedback}" + self._repeat_note(step_id) + self._unresolved_note(agent_config_name)
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
            # Resolve budget by role (agent_config_name); step_id is not a
            # registry key so it silently fell back to DEFAULT_MAX_TOOL_TURNS.
            max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(agent_config_name)

            self._emit("step_attempt", {"step_id": step_id, "attempt": attempt, "max_attempts": max_retries,
                                        "preview": f"Step {step_id} Attempt {attempt}/{max_retries}"})


            tool_results = []
            written_files = []
            effects: list[str] = []   # non-file output: see _effect_name

            # C2: Re-inject previously passed files so agent only fixes failing ones
            if previously_passed_files and attempt > 1:
                prev_files_section = "[Previously Written Files — DO NOT REWRITE THESE]\n"
                prev_files_section += "These files have already passed validation. Only fix the failing files.\n"
                for fname, fcontent in previously_passed_files.items():
                    prev_files_section += f"\n--- {fname} (already passed) ---\n```\n{fcontent}\n```\n"
                tool_results.insert(0, prev_files_section)

            for tool_turn in range(max_turns):
                self._note_feedback(step_id, feedback)
                prompt = self.assembler.assemble(
                    step_id, project_path, "", feedback, task_id=task_id, code_path=code_path,
                    resolved_context=self._resolved_context,
                    tool_schemas=self._tool_schemas,
                    user_lang=self._user_lang,
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
                if payload is not None:
                    payload, _norm = self._normalize_payload(
                        payload, self._tool_schemas)
                    if _norm:
                        self._emit("payload_normalized", _norm)
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
                    self._feedback_exploratory = False
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
                    # Costs a TURN, not the step. This used to `break`, so one
                    # unrecognised shape ended the step outright — the agent asked
                    # a question, got no answer, and the step reported having
                    # written nothing. The turn budget still bounds the loop.
                    self._feedback_exploratory = False
                    feedback = (
                        "System Error: No actions found in your response. Reply with "
                        '{"thoughts": "...", "actions": [{"tool": "<name>", '
                        '"params": {...}}]} — `actions` is a list, each entry needs '
                        "`tool` and `params`. Available tools: "
                        f"{', '.join(sorted(self._tool_schemas or {})) or '(none)'}.")
                    self._emit("parse_error", {"error": feedback,
                                               "preview": "No actions in response"})
                    tool_turn += 1
                    continue

                write_calls = [a for a in actions if isinstance(a, dict)
                               and self._is_mutation_tool(a.get("tool", ""),
                                                          self._tool_schemas)]
                (tool_calls, message_calls, _control_calls,
                 unclaimed) = self._classify_actions(
                     actions, self._tool_schemas, write_calls)

                # Turn accounting. Anything no bucket claimed is a delivery about
                # to be dropped: it used to fall through to the no-op branch at the
                # bottom of this loop, which returns SUCCESS with zero files. So an
                # agent that named a tool this step does not have — `write_file`
                # where the step has `create` — had its file discarded, was given
                # no turn to correct itself, and the step reported "No change
                # needed (no writes)". Verified before the fix: one turn, no
                # second turn, run_step True, nothing written. The docstring of
                # `_is_mutation_tool` claimed an invented name "falls through to
                # the unknown-write branch"; that branch exists only on the
                # constrained-slot path in `_run_tool_content_step`, never here.
                if unclaimed:
                    self._feedback_exploratory = False
                    feedback = self._unclaimed_feedback(unclaimed, self._tool_schemas)
                    self._emit("unknown_tool", {
                        "error": feedback,
                        "tools": [a.get("tool") if isinstance(a, dict) else None
                                  for a in unclaimed],
                        "preview": f"{len(unclaimed)} action(s) named an unavailable tool"})
                    continue

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
                        # A granted tool that CHANGED something counts as output
                        # even though it left no file in staging (repo_remove_file, a
                        # durable state write). See _effect_name.
                        wf = self._written_name(result)
                        if wf:
                            written_files.append(wf)
                        else:
                            eff = self._effect_name(result)
                            if eff:
                                effects.append(eff)
                    tool_results.extend(turn_results)
                    self._emit("tool_calls", {"count": len(tool_calls), "tools": [a.get("tool") for a in tool_calls],
                                              "preview": f"Executed {len(tool_calls)} tool call(s)"})

                if write_calls:
                    for action in write_calls:
                        result = self._exec_tool(action)
                        if "error" in result:
                            tool_results.append(f"Write error: {result['error']}")
                            continue
                        written_file = self._written_name(result)
                        if written_file:
                            written_files.append(written_file)

                    # Only stop when a write actually LANDED. `break` used to fire
                    # unconditionally, so a turn whose every write errored ended the
                    # loop with the reason captured in `tool_results` and never
                    # shown to anyone. Live example: on a fix-loop step's second
                    # visit, `create` returned "'tests/test_tools.py' already exists
                    # — use 'edit'" (the file was in the repo from the first pass,
                    # though this step's staging was empty). The agent was never
                    # given the turn in which it could have switched to `edit`; the
                    # step reported writing nothing and failed validation.
                    if not written_files:
                        self._feedback_exploratory = False
                        feedback = ("Every write in your last response failed:\n"
                                    + "\n".join(tool_results[-len(write_calls):]))
                        self._emit("write_failed", {
                            "error": feedback,
                            "preview": f"All {len(write_calls)} write(s) failed"})
                        tool_turn += 1
                        continue
                    self._emit("files_written", {"files": written_files,
                                                 "preview": f"Written {len(written_files)} file(s)"})
                    break

                # No write calls and no tool calls — agent signals no-op
                # completion. Complete cleanly with empty output instead of
                # copying the whole repo into the draft (which produced wholesale
                # commits and corrupted binaries).
                # The agent signalled completion and the step DID something, just
                # not to a file in staging — a queued deletion, a durable state
                # write. Counting only files made that look like a no-op: the engine
                # performed the change, then spent the rest of the budget before
                # failing for "no file writes produced".
                if _control_calls and effects and not written_files:
                    self._emit("step_done", {
                        "step_id": step_id, "files": [], "effects": effects,
                        "preview": f"No file written; {len(effects)} state change(s)",
                    })
                    return True

                if not tool_calls and not written_files:
                    self._emit("step_done", {
                        "step_id": step_id, "files": [],
                        "preview": "No change needed (no writes)",
                    })
                    return True

                self._emit("exploration", {"turn": tool_turn + 1, "preview": f"Exploration turn {tool_turn + 1}, continuing..."})
            else:
                self._emit("tool_turns_exceeded", {"max_turns": max_turns, "preview": "Max tool turns exceeded"})
                if not written_files and not effects:
                    self._emit("no_files_written", {"max_turns": max_turns})
                    raise MaxRetriesExceeded(
                        f"Task {task_id} Step {step_id}: No file writes produced after {max_turns} tool exploration turn(s). "
                        "The agent must produce at least one 'write' action to complete this step. "
                        "Sending messages or making read/list calls is not sufficient — you MUST write files."
                        + self._repeat_note(step_id) + self._unresolved_note(agent_config_name)
                    )
                self._feedback_exploratory = False
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
            f"Last feedback: {feedback}" + self._repeat_note(step_id) + self._unresolved_note(agent_config_name)
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
                prop = {
                    "type": pspec.get("type", "string"),
                    "description": pspec.get("description", ""),
                }
                # Structured .json slots carry nested schemas (array items,
                # object properties) — pass them through or providers see a
                # bare "array"/"object" with no shape.
                for key in ("items", "properties", "enum"):
                    if key in pspec:
                        prop[key] = pspec[key]
                properties[pname] = prop
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

    @staticmethod
    def _written_name(result: dict) -> str:
        """The file a write/edit/create tool reported.

        The generic tools use different result keys — write→'written',
        edit→'edited', create→'created'. Count all three: a single successful
        edit MUST register as output. Otherwise the step looks like a no-op,
        and because retries inherit the message history, the agent re-applies
        its edit on the next attempt (the duplicated/triplicated-code bug) and
        the empty written_files trips the whole-repo no-op floor.
        """
        return (result.get("written") or result.get("edited")
                or result.get("created") or "")

    # Keys by which a tool reports that it CHANGED something without leaving a
    # file in this step's staging. A deletion is real output; so is a durable
    # state write. Counting only files made a correct delete-only turn look like
    # a no-op — the engine executed the deletion and then failed the step for
    # "no file writes produced".
    _EFFECT_KEYS = ("queued_for_deletion", "deleted", "removed",
                    "applied", "state_written", "state_updated", "committed")

    @classmethod
    def _effect_name(cls, result: dict) -> str:
        """What this tool changed, if it changed something but wrote no file.

        Separate from `_written_name` on purpose: a step's staging can be empty
        and the step still have done its job. `t_impl` is granted `repo_remove_file`,
        whose success is `{"queued_for_deletion": …}` — no file, a real effect.
        """
        if not isinstance(result, dict) or result.get("error"):
            return ""
        for k in cls._EFFECT_KEYS:
            v = result.get(k)
            if v:
                return f"{k}: {v}" if not isinstance(v, bool) else k
        return ""

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

        # Reset this step's draft staging at the start of the run, aligned with
        # the fresh `messages` history below — both reset together. A new task
        # (or a skillflow-level retry that re-invokes this method with a clean
        # history) must not inherit a prior run's staged files, which the shared,
        # never-cleared {step_id}.tmp would otherwise carry forward and commit
        # wholesale. In-attempt retries stay inside the loop below and do NOT
        # re-clear, so they keep staging consistent with the inherited history.
        workspace.clean_draft_dir(project_id, step_id, self._draft_graph_name())

        feedback = ""
        # Carryover across attempts (parity with JSON mode) done the cache-optimal
        # way: a retry CONTINUES the prior attempt's message list (byte-identical
        # prefix → KV-cache hit) and just appends a corrective nudge, rather than
        # rebuilding a fresh prompt with the exploration re-summarised as novel
        # (cache-missing) text. `messages` and `last_reasoning` therefore persist
        # across attempts.
        messages: list[dict] = []
        last_reasoning = ""  # cached for deepseek: replay on tool-only turns
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
        # Resolve budget by role (agent_config_name); the registry is keyed by
        # role, not step_id, so passing step_id silently capped every native
        # step at DEFAULT_MAX_TOOL_TURNS regardless of its configured budget.
        max_turns = self._max_tool_turns or self.factory.get_max_tool_turns(agent_config_name)

        # Config-driven shared preamble. A config opts in via
        # x-aitelier.preamble_steps; only then is project-global stable content
        # hoisted into a byte-identical system preamble that caches across every
        # step of the run. That declaration IS the opt-in — the old
        # AITELIER_HOIST_DESIGN kill switch is gone. It was a rollout hatch from
        # when this shipped, was never set anywhere, and by 2026-08-26 its only
        # live effect was to make the configs lie: `{step: "2", mode:
        # "interfaces"}` reads like a size control, but the entry is DROPPED
        # wholesale while the preamble carries it (drop_preamble_steps), so the
        # mode never arms. Flipping the switch off silently swapped that for the
        # FULL design doc in every t_plan / t_impl / t_impl_review user message.
        # A hatch nobody pulls, which changes behaviour in a way the config does
        # not describe, is a trap rather than an option.
        #
        # Measured 2026-08-26, and the reason it stays on: the first reviewer of
        # a run gets 0% cache hit on its opening turn; a LATER reviewer's first
        # turn gets 34.2% — that difference is the preamble.
        graph_name = self._draft_graph_name()
        preamble_steps = self._preamble_steps(graph_name)
        use_preamble = bool(preamble_steps)

        # When the preamble carries the design docs of preamble_steps, drop their
        # resolved_context copies so design isn't duplicated in the prompt.
        # Conditional: F2-off leaves resolved_context untouched.
        resolved_ctx = self._resolved_context
        if use_preamble:
            resolved_ctx = self.assembler.drop_preamble_steps(
                resolved_ctx, preamble_steps)
        # skillflow ALSO puts the validation error in _resolved_context under its
        # own label. It is rendered as an instruction below, so drop the context
        # copy — matched by VALUE, never by the label string, which is a private
        # skillflow constant that would silently stop matching if reworded.
        resolved_ctx = self._drop_context_value(resolved_ctx, self._validation_error)

        for attempt in range(1, max_retries + 1):
            self._emit("step_attempt", {
                "step_id": step_id, "attempt": attempt,
                "max_attempts": max_retries,
                "mode": "native",
                "preview": f"Step {step_id} Attempt {attempt}/{max_retries} (native)",
            })

            if attempt == 1:
                # Build initial messages. F1: project-global stable content
                # (workspace layout + brief [+ design when F2]) goes in a
                # byte-identical system preamble so the provider KV cache reuses
                # it across every step; assemble() omits those blocks from the
                # user message (hoist_*).
                user_prompt = self.assembler.assemble(
                    step_id, project_path, "", feedback,
                    task_id=task_id, code_path=code_path,
                    resolved_context=resolved_ctx,
                    tool_schemas=self._tool_schemas,
                    native=True,
                    hoist_globals=use_preamble,
                    hoist_design=use_preamble,
                    user_lang=self._user_lang,
                )

                # Inject turn budget so the agent can pace exploration.
                # NOTE: appended AFTER assemble()'s volatile tail (resolved
                # context / tree / feedback), past the cache-prefix boundary —
                # editing it does NOT perturb prefix caching. Kept native-only.
                # THE LAST ATTEMPT'S VALIDATION FAILURE IS AN INSTRUCTION,
                # NOT BACKGROUND. Delivered via _resolved_context it rendered as
                # the final `### <label>` entry of [Pre-resolved Context] —
                # measured at line 1517/1519, right behind a 1484-line clipped
                # design bundle. Same words, same prompt, no salience. Put it in
                # the recency slot instead, where the turn budget and language
                # override already live.
                user_prompt += self._validation_error_block(self._validation_error)
                user_prompt += (
                    f"\n\n[Turn Budget: {max_turns} turns total, then forced output]\n"
                    "You are a workflow automation step, not a chat assistant: your "
                    "visible reply text is never shown to anyone and is discarded — "
                    "only tool calls have any effect, and a reply with no tool call "
                    "ends the step immediately.\n"
                    "Plan your exploration, then call write_*/create_* to produce the "
                    "required output. The moment all required files are written, call "
                    "finish_step immediately. Do NOT re-read, re-list, search, or "
                    "otherwise re-verify files you just wrote — writes are trusted and "
                    "already staged, so re-verifying them only burns turns. Never end "
                    "a turn with a plain-text 'done' / 'written successfully' note; "
                    "your final action must be a tool call (the write tool, then "
                    "finish_step). Do not exhaust all turns on exploration — leave at "
                    "least 1 turn for writing.\n"
                    # Context, not just turns. The role template already says not
                    # to read whole files, but as the 6th bullet of a section about
                    # WRITING, 78k chars into the system message — so it is restated
                    # here, at the end of the user message, for the same recency
                    # reason the [Language] block is placed here.
                    #
                    # Its effect is UNPROVEN, and the first version of this comment
                    # overstated it ("ignored 11/11 -> 10/13"). That was turn-1-only
                    # replay data; models orient broadly and narrow later. Across all
                    # turns of production, localqwen already scopes 64.6% of 676 read
                    # calls unaided (qwen/qwen3.8-flash 72.3%, the DeepSeek endpoints
                    # ~50%). Kept because it costs a few hundred characters and cannot
                    # hurt — measure the spill rate, not tool calls.
                    "Read narrowly: `search` with a `glob` and `context_lines` to "
                    "FIND, `read` with `start_line`/`end_line` to read a known "
                    "region. Reading a large file whole spends the context you need "
                    "for the edit itself."
                )

                # [Language] — injected ONCE, here, as the absolute last block of
                # the user message (after [Turn Budget]). It is the last content
                # the model reads (system message precedes the user message), so
                # this single placement gives maximal recency-weighted override
                # while keeping the cached system prefix language-independent.
                lang_instruction = build_language_instruction(self._user_lang)
                if lang_instruction:
                    user_prompt += "\n\n" + lang_instruction

                if use_preamble:
                    preamble = self.assembler.build_shared_preamble(
                        project_path, code_path, graph_name=graph_name,
                        preamble_steps=preamble_steps, include_design=True,
                    )
                    system_content = f"{preamble}\n\n{agent.system_prompt}"
                else:
                    system_content = agent.system_prompt
                messages = [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_prompt},
                ]
                self._trace("prompt", "user_prompt", {
                    "attempt": attempt, "mode": "native",
                    # Trace the ACTUAL system message sent (incl. the shared
                    # preamble), not just the role template.
                    "system": system_content, "user": user_prompt,
                })
            else:
                # Retry: CONTINUE the prior attempt's conversation. Everything
                # already in `messages` is byte-identical to what the provider
                # cached during the previous attempt, so the whole exploration
                # prefix is a KV-cache HIT — far cheaper than rebuilding the
                # prompt with the exploration re-summarised as novel tokens.
                # Just append a corrective nudge.
                nudge = (
                    (f"{feedback}\n\n" if feedback else "")
                    + "[Retry] Your previous attempt produced NO output file. "
                    "Do not re-read or re-explore — you already have everything "
                    "above. Your VERY NEXT action MUST be write_/create_ tool "
                    "call(s) to produce the required file(s), then finish_step."
                )
                messages.append({"role": "user", "content": nudge})
                self._trace("prompt", "user_prompt", {
                    "attempt": attempt, "mode": "native", "user": nudge,
                })

            written_files: list[str] = []
            turn_count = 0
            # Set when the agent explicitly calls finish_step — its "I am done"
            # signal. Used below to distinguish a deliberate no-op completion
            # (the fix is already present, nothing to write) from a step that
            # simply failed to produce output.
            agent_signaled_done = False

            # The turn budget is MUTABLE — two separate mechanisms below raise
            # it mid-step — but `for … in range(max_turns)` freezes the bound at
            # loop entry, so both raises were discarded and the loop stopped at
            # the original count. Hoist the bound into a variable the loop
            # actually re-reads. `continue` appears throughout this body, so the
            # counter is incremented at the TOP: exactly range()'s semantics.
            current_max_turns = max_turns
            turn_grants = 0
            turn_count = -1
            while True:
                turn_count += 1
                if turn_count >= current_max_turns:
                    # Ending AT the cap and ending because the work is done are
                    # two different outcomes, and they used to leave identical
                    # traces. Downstream (validation, the reviewer, the human at
                    # the checkpoint) then reads an incomplete deliverable with
                    # no hint that it was cut off mid-flight rather than judged
                    # complete by its author.
                    self._trace("step", "turn_budget_exhausted", {
                        "step_id": step_id, "turns": turn_count,
                        "max_turns": current_max_turns,
                        "written_files": sorted(written_files or []),
                    })
                    self._emit("turn_budget_exhausted", {
                        "step_id": step_id, "turns": turn_count,
                        "preview": f"Step {step_id} stopped at its {turn_count}-turn cap",
                    })
                    break
                remaining = current_max_turns - turn_count
                # A LOW BUDGET IS NEWS EVEN WHEN OUTPUT EXISTS.
                # The nudge below fires only when NOTHING is written, so a step
                # that has produced SOME of what it owes looks identical to one
                # that is finished: the loop just breaks at the cap, silently.
                # Live, jinyong-numbers 2026-09-01 step "3": the PM declared 9
                # task cards in tasks_manifest.json, wrote 5, and hit turn 20
                # twice (its reasoning had already eaten the whole 32768-token
                # output cap on turn 12, so several turns produced no tool call
                # at all). It was never told it was running out, and the
                # incomplete breakdown was then promoted and put to a human for
                # approval. Announce the remaining budget once, early enough to
                # act on, so "finish what you owe" is a decision the agent can
                # make instead of a cliff it walks off.
                if self._should_warn_low_budget(remaining, current_max_turns):
                    messages.append({
                        "role": "user",
                        "content": self._low_budget_message(
                            remaining, current_max_turns),
                    })
                    self._trace("step", "turn_budget_low", {
                        "step_id": step_id, "remaining": remaining,
                        "max_turns": current_max_turns,
                        "written_files": sorted(written_files or []),
                    })
                if remaining > 1:
                    tool_choice = "auto"
                elif not written_files and write_tool_names:
                    # Final turn and the step still has no output. Exploration-
                    # heavy models (e.g. deepseek) burn the whole budget on
                    # read/search tools and reach the last turn with nothing
                    # written. Do NOT force a specific function via tool_choice
                    # — forcing a named function crashes native tool calling for
                    # some providers (the call raises, the loop breaks, and the
                    # step dies empty). Keep "auto" and inject a hard nudge so
                    # the model writes on its own initiative.
                    tool_choice = "auto"
                    messages.append({
                        "role": "user",
                        "content": (
                            "[Turn budget nearly exhausted] You have not "
                            "written any output yet. Your VERY NEXT action MUST "
                            "be a write_/create_ tool call to produce the "
                            "required file(s), then finish_step. Do not read, "
                            "search, or list."
                        ),
                    })
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
                    # A SPENT QUOTA is not feedback for the agent — it is an
                    # infrastructure condition, and the only correct response is
                    # to stop asking. Swallowing it here is what defeated the
                    # scheduler's quota hold: every DPE role is
                    # native_tool_calling, so every LLM call arrives at this
                    # handler, the RateLimitError became prose, `feedback` was
                    # overwritten three lines later by the "No output produced"
                    # message, and the loop re-called the spent endpoint once
                    # per attempt until MaxRetriesExceeded — which the scheduler
                    # catches BEFORE its quota check, and which carries none of
                    # the provider's reset-time prose. A byte-for-byte replay of
                    # the 2026-08-26 outage the hold was written to stop.
                    #
                    # With routing in place this only fires once EVERY candidate
                    # for the model is spent, so it is genuinely the last resort.
                    from core.llm_quota import is_quota_exhausted
                    if is_quota_exhausted(e):
                        raise
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

                # Phase 0 cache telemetry: record per-turn token + prompt-cache
                # usage so a run's cache hit-ratio can be aggregated from traces.
                usage = getattr(agent.gateway, "last_usage", {}) or {}
                if usage:
                    self._trace("usage", "token_usage", {
                        "step_id": step_id, "attempt": attempt,
                        "turn": turn_count + 1, **usage,
                    })

                # Record trace — store the full response (free text + every
                # tool call with untruncated args + reasoning) so the trace is
                # a faithful copy of what the model produced, not a lossy digest.
                self._trace("response", "agent_response", {
                    "attempt": attempt, "turn": turn_count + 1,
                    "text": result.text or "",
                    "reasoning_content": result.reasoning_content or "",
                    "tool_calls": [
                        {"name": tc["function"]["name"],
                         "arguments": tc["function"].get("arguments", "")}
                        for tc in result.tool_calls
                    ],
                })

                # The turn hit max_output_tokens and produced neither text nor a
                # tool call: on DeepSeek that cap covers reasoning + visible
                # output together, so an over-long chain of thought can consume
                # the entire budget and the step's write/verdict is never
                # emitted. Untagged this looks exactly like a well-behaved
                # no-op, which is how a reviewer step "passed" without ever
                # reviewing anything. Say so loudly, in the event stream and in
                # the durable trace, so the role's budget can be re-sized.
                starved_turn = bool(result.truncated and not result.tool_calls
                                    and not result.text)
                if starved_turn:
                    starved = {
                        "step_id": step_id, "agent_role": role,
                        "attempt": attempt, "turn": turn_count + 1,
                        "reasoning_chars": len(result.reasoning_content or ""),
                        "completion_tokens": usage.get("completion_tokens"),
                        "reasoning_tokens": usage.get("reasoning_tokens"),
                        "max_output_tokens": agent.gateway.max_output_tokens,
                    }
                    self._emit("output_cap_starved", {
                        **starved, "level": "warning",
                        "preview": (f"{role_label} turn {turn_count + 1}: output cap "
                                    f"({agent.gateway.max_output_tokens}) consumed by "
                                    f"reasoning — no text, no tool call"),
                    })
                    self._trace("response", "output_cap_starved", starved)

                    # Detection on its own changes nothing: the next turn (and
                    # the next attempt) reissues a byte-identical call — same
                    # model, prompt, effort and cap — which necessarily starves
                    # again. Observed live: task_implementer burned two full
                    # 32768-token budgets on pure reasoning back to back. So
                    # raise the one setting that produced the truncation and let
                    # the existing retry path make the call again.
                    #
                    # This needs no limit of its own, and deliberately has none.
                    # Magnitude is bounded by OUTPUT_CAP_CEILING, past which
                    # escalate_output_cap() declines. Frequency is bounded by
                    # the step's existing turn budget, because the escalated
                    # retry IS the next ordinary turn rather than an inner loop
                    # — and a starved turn emits no tool call, so it can never
                    # reach ask_more_turns to extend that budget. The raised cap
                    # rides on the gateway, which get_native_agent() builds once
                    # per step, so it persists across this step's remaining
                    # turns and attempts (the condition that starved turn 1 is
                    # still there on turn 2) and resets for the next step.
                    previous_cap = starved["max_output_tokens"]
                    escalated = agent.gateway.escalate_output_cap()
                    detail = {**starved, "previous_cap": previous_cap,
                              "new_cap": escalated}
                    if escalated:
                        # Carry it into the next claim of this role, so the
                        # ladder is climbed once per process, not once per card.
                        try:
                            from core.agents import remember_output_cap
                            remember_output_cap(agent_config_name, escalated)
                        except Exception:  # noqa: BLE001 — telemetry must not break a turn
                            logging.getLogger("aitelier.pipeline").warning(
                                "could not remember output cap", exc_info=True)
                        self._emit("output_cap_escalated", {
                            **detail, "level": "warning",
                            "preview": (f"{role_label} turn {turn_count + 1}: raising "
                                        f"output cap {previous_cap} → {escalated} "
                                        f"for the retry"),
                        })
                        self._trace("response", "output_cap_escalated", detail)
                        # The cap is raised "for the retry" — and on the LAST
                        # turn there is no retry: the no-tool-call branch below
                        # completes the step EMPTY and the freshly-raised
                        # gateway is thrown away. So the escalation could only
                        # ever help a starve that happened early, and the loop
                        # head aims the biggest write demand at the final turn
                        # ("Your VERY NEXT action MUST be a write_ call"),
                        # making the final turn the likeliest to starve.
                        # Live, jinyong-usable 2026-08-23: nine consecutive
                        # t_plan executions each starved on turn 6 of 6, each
                        # logged "16384 → 32768 for the retry", and each
                        # returned a 0-byte task_plan.md. The second escalation
                        # never once appeared in the log — no turn ever ran at
                        # the raised cap. The empty plans were then confirmed as
                        # "no change needed", and the reviewer's (correct)
                        # rejections burned the run's plan-loop budget to its
                        # limit. Buy back the turn.
                        # Bounded without a counter of its own: a grant requires
                        # a SUCCESSFUL escalation, and escalate_output_cap()
                        # returns None at OUTPUT_CAP_CEILING — so a role
                        # starting at 16384 gets at most two.
                        if not written_files and turn_count >= current_max_turns - 1:
                            current_max_turns += 1
                            self._emit("turn_granted_for_escalation", {
                                **detail, "level": "warning",
                                "preview": (f"{role_label}: last turn starved — granting "
                                            f"turn {current_max_turns} so the raised cap "
                                            f"({escalated}) is actually used"),
                            })
                    else:
                        # Already at the ceiling — doubling again would only buy
                        # an API error. Say so, so a role that keeps landing
                        # here is visible as needing a smaller prompt or less
                        # reasoning rather than a bigger budget.
                        self._emit("output_cap_ceiling", {
                            **detail, "level": "warning",
                            "preview": (f"{role_label} turn {turn_count + 1}: output cap "
                                        f"already at the ceiling ({previous_cap}) — "
                                        f"cannot escalate"),
                        })
                        self._trace("response", "output_cap_ceiling", detail)

                if result.text:
                    self._emit("agent_message", {
                        "content": result.text[:500],
                        "level": "info",
                        "preview": result.text[:200],
                    })

                if not result.tool_calls:
                    # A reply with no tool call produces no output. This is the
                    # only "the agent is done / has nothing more" signal, and is
                    # handled in parity with JSON mode:
                    if not written_files and write_tool_names:
                        if turn_count < current_max_turns - 1:
                            # Budget remains: nudge to WRITE rather than ending
                            # empty (the step likely has real output to produce).
                            salvage_msg: dict = {"role": "assistant",
                                                 "content": result.text or None}
                            # A starved turn's reasoning is a chain of thought
                            # cut off mid-sentence, and on these prompts it is
                            # the full cap's worth of tokens. Replaying it into
                            # every later turn would grow the prompt by exactly
                            # the budget we just doubled, pushing the request
                            # toward the context window the raised cap has to
                            # share — so keep the last COMPLETE reasoning
                            # instead. Same state a turn that reasoned not at
                            # all would leave behind, which this path already
                            # handles.
                            if result.reasoning_content and not starved_turn:
                                last_reasoning = result.reasoning_content
                            if last_reasoning:
                                salvage_msg["reasoning_content"] = last_reasoning
                            messages.append(salvage_msg)
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Your reply contained no tool call, so it "
                                    "produced no output and was discarded. You "
                                    "MUST call write_/create_ tools now to "
                                    "produce the required file(s), then "
                                    "finish_step."
                                ),
                            })
                            self._emit("exploration", {
                                "turn": turn_count + 1, "mode": "native",
                                "preview": "No tool call — nudging agent to write",
                            })
                            continue
                        # Budget exhausted and the agent stopped calling tools
                        # without writing — a genuine no-op signal. Do NOT floor
                        # the whole repo into the draft (that produced wholesale
                        # commits + corrupted binaries), and do NOT fall through
                        # to the "No output produced" retry: retries inherit the
                        # message history, so re-prompting an agent that already
                        # decided "no change" makes it fabricate/re-edit. Complete
                        # with EMPTY output; repo_apply no-ops on it and
                        # t_impl_review still verifies a change wasn't required.
                        self._emit("step_done", {
                            "step_id": step_id, "files": [],
                            "preview": "No change needed (no writes, budget reached)",
                        })
                        return True
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
                        agent_signaled_done = True
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

                    # Phase ticks around the tool run: a gate tool (run_tests,
                    # godot_compile/_playtest) can hold this thread for
                    # minutes, and without these the liveness line either
                    # lingers on a stale "generating" or shows nothing.
                    self._note_phase("tool", tool_name)
                    try:
                        tool_result = self._exec_tool({"tool": tool_name, "params": params})
                    finally:
                        self._note_phase("tool_done", tool_name)
                    if tool_name == "ask_more_turns":
                        # _exec_tool answers "granted" unconditionally; the
                        # budget decision is the loop's. Overwrite the result
                        # so the model reads the real grant (or the denial).
                        ask_more_extra, turn_grants, grant_msg = _grant_turns(turn_grants, ask_more_extra)
                        tool_result = {"status": "granted" if ask_more_extra else "denied",
                                       "turns": ask_more_extra, "note": grant_msg}
                    result_str = json.dumps(tool_result, ensure_ascii=False)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })

                    # Track written files (write→'written', edit→'edited',
                    # create→'created'); see _written_name.
                    wf = self._written_name(tool_result)
                    if wf:
                        written_files.append(wf)


                # Apply ask_more_turns budget extension after all tool calls
                # in this turn have been processed.
                if ask_more_extra > 0:
                    # Was `max_turns += …`, which the frozen range() ignored:
                    # the agent asked for more turns, was told it had them, and
                    # the loop still stopped at the original bound.
                    current_max_turns += ask_more_extra
                    self._emit("agent_turn_request", {
                        "extra_turns": ask_more_extra,
                        "reason": ask_more_reason,
                        "remaining": current_max_turns - turn_count - 1,
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

            if not written_files and agent_signaled_done:
                # Agent explicitly called finish_step without writing — a
                # legitimate "no change needed" outcome. Complete the step with
                # EMPTY output. This used to copy the entire code_path into the
                # draft, which produced wholesale repo commits AND corrupted
                # binaries (read_text(errors="replace") mangles non-UTF-8 files).
                # It must also NOT fall through to the "No output produced" retry
                # below: retries inherit the message history, so re-prompting an
                # agent that correctly decided "no change" makes it re-apply its
                # edits (the triplicated-import bug). Empty staging promotes as a
                # no-op and repo_apply (empty source) reports success.
                self._emit("step_done", {
                    "step_id": step_id, "files": [],
                    "preview": "No change needed (finish_step, no writes)",
                })
                return True

            if not written_files:
                feedback = (
                    f"Step {step_id}: No output produced. "
                    f"Use write_*/create_*/append_* tools to write output files."
                )
                if turn_count >= current_max_turns - 1:
                    feedback = f"Max turns ({current_max_turns}) exceeded. " + feedback
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

    def _preamble_steps(self, graph_name: str) -> list[str]:
        """Stable step ids to hoist into the shared preamble, declared by the
        running config's host metadata (x-aitelier.preamble_steps). Empty for
        configs that don't opt in — config-agnostic, no pipeline hardcoding."""
        try:
            from api.dependencies import get_config_registry
            manifest = get_config_registry().get(graph_name)
            return list(manifest.preamble_steps) if manifest else []
        except Exception:
            return []

    # ── Dispatch ─────────────────────────────────────────────────────

    @staticmethod
    def _should_warn_low_budget(remaining: int, max_turns: int) -> bool:
        """Warn once, `_LOW_TURN_BUDGET` turns before the cap.

        Deliberately independent of whether anything has been written: the
        final-turn nudge already covers the nothing-written cliff, and the case
        it does NOT cover is the expensive one — a step that has produced SOME
        of what it owes looks finished to the loop and is cut off mid-flight.
        Silent on a budget that never had room to warn (cap <= the threshold),
        where every turn is already the last few.
        """
        return remaining == _LOW_TURN_BUDGET and max_turns > _LOW_TURN_BUDGET

    @staticmethod
    def _low_budget_message(remaining: int, max_turns: int) -> str:
        """The warning. Names the count, the obligation, and the honest way out.

        The third sentence matters as much as the first: without it the agent's
        only options are to finish or to be cut off, and being cut off is
        indistinguishable from finishing. Saying "I could not fit them all" is
        a deliverable; silently missing items is not.
        """
        return (
            f"[Turn Budget: {remaining} of {max_turns} turns remain] Stop "
            "exploring. Finish EVERY output this step owes — if you declared a "
            "manifest, index or list, every item it names must exist before you "
            "call finish_step. If real work remains (files or scenarios you have "
            "not written yet), call ask_more_turns(turns=N, reason=\"what remains\") "
            f"NOW — at most {_MAX_TURN_GRANTS} grants of up to {_GRANT_TURNS_MAX} "
            "turns per step. Only when that is exhausted, say so explicitly in "
            "the output you do write rather than leaving items silently missing."
        )

    @staticmethod
    def _validation_error_block(validation_error: str | None) -> str:
        """The previous attempt's validation failure, as an INSTRUCTION.

        Empty string when there is none, so the caller appends unconditionally.
        skillflow also exposes this through _resolved_context, where it renders
        as the last `### <label>` entry among the graph's context sources —
        measured at line 1517 of 1519, behind a 1484-line clipped design bundle
        (jinyong-numbers 2026-09-01, step "3"). Same words, no salience.
        """
        if not validation_error:
            return ""
        return (
            "\n\n[Previous Attempt Failed Validation — MUST FIX]\n"
            f"{validation_error}\n"
            "This is not background context: the step you are running now IS "
            "that retry. Fix exactly this before producing anything else, and "
            "re-emit every file the step owes — a file you do not write this "
            "attempt is not carried over."
        )

    @staticmethod
    def _drop_context_value(resolved_ctx: dict | None, value) -> dict:
        """Drop entries whose CONTENT equals ``value`` (not whose label matches).

        Used to de-duplicate the validation error, which the host renders as its
        own instruction block. Matching skillflow's label string would be the
        obvious way and the wrong one: it is a private constant, so a rewording
        there would silently reinstate the duplicate with nothing failing.
        """
        if not resolved_ctx or value is None:
            return resolved_ctx or {}
        return {k: v for k, v in resolved_ctx.items() if v != value}

    def run_step(self, task_id: int, step_id: str, workspace: Any,
                 project_id: str = "default", subtask_id: str | None = None,
                 agent_config_name: str = "",
                 resolved_context: dict | None = None,
                 validation_error: str | None = None,
                 tool_schemas: dict | None = None,
                 output_dir: str = "",
                 max_tool_turns: int = 0,
                 run_id: str = "",
                 step_instance_id: int | None = None,
                 claim_epoch: int = 0) -> bool:
        """
        Dispatch to the appropriate step execution path.

        agent_config_name, tool_schemas, output_dir, run_id come from
        skillflow's ClaimedStep.inputs.
        max_tool_turns overrides agent config default when > 0.
        Red review is handled by skillflow-level _review steps.
        """
        self._project_id = project_id
        self._resolved_context = resolved_context
        self._validation_error = validation_error
        self._tool_schemas = tool_schemas or {}
        self._output_dir = output_dir
        self._max_tool_turns = max_tool_turns
        self._run_id = run_id
        self._step_instance_id = step_instance_id
        self._claim_epoch = claim_epoch

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
                # A spent quota is not a native-tool-calling failure, and this
                # `except` was quietly undoing the re-raise `_run_native_step`
                # performs on purpose (see the `is_quota_exhausted` guard there
                # and the outage it names). EVERY role sets
                # `fallback_to_json_mode: true`, so the re-raise never once
                # reached the scheduler through this path.
                #
                # Falling through costs more than a mislabel: the JSON path
                # re-walks the same candidate list, and `_next_usable`
                # deliberately degrades to "try it anyway" when every endpoint
                # is parked — so it re-pays one real 429 per candidate against
                # endpoints already known to be spent, on top of the walk the
                # native path just finished. The scheduler's quota hold exists
                # to stop exactly that.
                from core.llm_quota import is_quota_exhausted
                if is_quota_exhausted(e):
                    raise
                if not self.factory.get_fallback_to_json(agent_config_name):
                    raise
                # Fall through to JSON mode dispatch below
                self._emit("native_fallback", {
                    "step_id": step_id,
                    "preview": f"Native failed ({type(e).__name__}), falling back to JSON mode",
                })

        # Dispatch based on skillflow-provided tool_schemas
        ts = self._tool_schemas
        # NOTE: these two predicates are deliberately left as prefix tests.
        # `create`/`edit` are invisible to them, so a `mode: write` step without
        # `allow_full_write` lands in the `_run_tool_step` branch below — which is
        # where EVERY write-mode step in this repo has always landed (26 of them,
        # including dpe_default's `t_impl` and pipeline_forge's own `emit_graph`),
        # and that handler writes perfectly well. Making them mutation-aware would
        # re-route all 26 onto a different handler to fix a defect that lives in
        # `_run_tool_step`'s own write-call classification, which is fixed there
        # instead. Changing the routing of the entire system is not the smaller fix.
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
