# core/meta_agent.py
# Autonomous meta agent — the "butler" that manages projects, tasks,
# checkpoints, and workspace inspection via tool-use over LiteLLM.
# Runs inside the backend process with direct DBManager/WorkspaceManager access.

import asyncio
import json
import os
import traceback
from pathlib import Path
from typing import AsyncGenerator

import litellm
import yaml

_DEFAULT_CONFIG_PATH = "dpe_roles_config.yaml"
_DEFAULT_CONFIG_PATH_V2 = "agent_configs/meta_conversation.yaml"
_META_DIR = Path.home() / ".AItelier" / "meta"

# ── System Prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the AItelier butler — the user's single point of contact for managing \
software projects powered by the DPE (Deterministic Pipeline Engine).

You have tools to:
- List, create, update, and delete projects
- Draft and edit project briefs (save_draft_brief, edit_draft_brief)
- Draft tasks for existing projects (save_draft_task, suggest_submit_task)
- Manage tasks (list, retry)
- Inspect workspace files (source code, trace logs, architecture docs)
- Start and resume skillflow pipelines (start_pipeline, approve/reject checkpoint)

CRITICAL RULES:
- ALWAYS route work through the DPE pipeline. Never suggest bypassing the
  pipeline, writing code directly, or offering code snippets as an alternative.
  Your purpose is to feed work into the pipeline.
- NEVER implement code yourself or offer to "just write the code directly."

PROJECT WORKFLOW (new projects):
  1. Gather requirements conversationally.
  2. Call create_project to set up the workspace.
  3. Call save_draft_brief with the structured brief.
  4. Present the brief summary in chat. Say something like:
     "Here's the project brief. Shall I start the pipeline?"
  5. WAIT for the user to say "approve", "ok", "yes", "同意", "go ahead", etc.
  6. Once approved, call start_pipeline(project_id, config="dpe_default_v2").
  7. The pipeline runs until a checkpoint — present it to the user. Wait for
     approval or feedback before calling approve_checkpoint or reject_checkpoint.
- To modify a draft brief: call edit_draft_brief, present the changes, wait
  for user approval, then call start_pipeline.

TASK WORKFLOW (adding to existing projects):
- Call save_draft_task then suggest_submit_task to present for user approval.
- NEVER tell the user a task has been submitted unless suggest_submit_task
  returned a pending_confirm result.

PIPELINE ORCHESTRATION:
- start_pipeline executes steps until a checkpoint or the end.
- When start_pipeline returns status "checkpoint": present the label and
  output summary to the user. WAIT for their response.
- User says "approve"/"ok"/"yes"/"同意"/"LGTM" → call approve_checkpoint(run_id).
- User gives feedback → call reject_checkpoint(run_id, feedback).
- After approve/reject, the pipeline continues to the next checkpoint or end.
- When the pipeline completes (status: "completed"), summarize the results.
- Use get_pipeline_status(run_id) to check on a run at any time.
- NEVER call approve_checkpoint unless the user has explicitly indicated
  approval. If unsure, ask them.

Guidelines:
- Track which questions you've already asked. Don't repeat yourself.
- If save returns "no draft found", you forgot to create/save first — fix it.
- When calling a tool, be concise — one sentence. Let the result speak.
- Read workspace files when the user asks about code, progress, or errors.
- Use retrieve_previous_context for previous conversation context.

Current project: {current_project}
Owner: {owner_email}
"""

# ── Tool Definitions (LiteLLM / OpenAI function-calling schema) ────

TOOL_DEFINITIONS = [
    # ── Project CRUD ──
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List all projects with task stats (status counts).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project",
            "description": "Get details of a single project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new project. Sets up workspace and DB record.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Unique slug for the project"},
                    "name": {"type": "string", "description": "Display name"},
                    "repo_type": {"type": "string", "enum": ["new", "existing", "clone"],
                                  "description": "Repository type", "default": "new"},
                    "repo_path": {"type": "string", "description": "Local path (for existing)"},
                    "repo_url": {"type": "string", "description": "Git URL (for clone)"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": "Update project fields (name, brief, priority, status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "name": {"type": "string", "description": "New display name"},
                    "brief": {"type": "string", "description": "New project brief (markdown)"},
                    "priority": {"type": "integer", "description": "Scheduling priority"},
                    "status": {"type": "string", "description": "New status"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_project",
            "description": "Delete a project and all its tasks and workspace (destructive).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_draft_brief",
            "description": "Save a draft project brief to disk. Call this AFTER create_project "
                           "and BEFORE suggest_submit_project. "
                           "Can be called multiple times to refine the brief.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Unique slug (e.g. 'hello-world-app')"},
                    "name": {"type": "string", "description": "Display name for the project"},
                    "brief": {
                        "type": "object",
                        "description": "Structured project brief with keys: "
                                       "project_name, description, goals, non_goals, "
                                       "tech_constraints, user_stories, target_users, success_criteria",
                    },
                },
                "required": ["project_id", "brief"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_draft_brief",
            "description": "Make targeted changes to a saved draft brief without regenerating the entire thing. "
                           "Reads the existing draft, merges changes, writes back.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The project slug whose draft to edit"},
                    "changes": {
                        "type": "object",
                        "description": "Partial brief fields to merge. Any of: name, description, goals, "
                                       "non_goals, tech_constraints, user_stories, target_users, success_criteria. "
                                       "For list fields (goals, non_goals, etc.), the new value replaces the old.",
                    },
                },
                "required": ["project_id", "changes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_project",
            "description": "Retry a failed project — resets project and all failed tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_planning",
            "description": "Re-run the Researcher and Architect planning steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                },
                "required": ["project_id"],
            },
        },
    },
    # ── Task CRUD ──
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "List all tasks for a project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_draft_task",
            "description": "Save a draft task for an existing project. Call this FIRST before suggest_submit_task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID"},
                    "prompt": {"type": "string", "description": "Task description / requirements"},
                    "task_spec": {
                        "type": "object",
                        "description": "Optional structured task spec (description, acceptance_criteria, scope)",
                    },
                },
                "required": ["project_id", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_submit_task",
            "description": "Read the saved draft task and present it to the user for approval. "
                           "Does NOT create the task — the user must explicitly approve first. "
                           "Always call save_draft_task before this.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": "Get details of a single task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_task",
            "description": "Retry a failed task.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_step_output",
            "description": "Get output files from a completed pipeline step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "step_id": {"type": "string",
                                "description": "Step ID: t_plan, t_impl, t_verify, 1_5, 2, 3, 5"},
                },
                "required": ["task_id", "step_id"],
            },
        },
    },
    # ── Workspace inspection ──
    {
        "type": "function",
        "function": {
            "name": "list_workspace_tree",
            "description": "List the directory tree of a project's workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "subdir": {"type": "string",
                               "description": "Optional subdirectory to scope (e.g. 'project', '2')"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read a file from the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Relative path within workspace (e.g. 'project/main.py')"},
                },
                "required": ["project_id", "path"],
            },
        },
    },
    # ── Context ──
    {
        "type": "function",
        "function": {
            "name": "retrieve_previous_context",
            "description": "Retrieve a previously saved conversation context file. "
                           "Returns the raw messages so the user can review what was discussed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "which": {"type": "integer",
                              "description": "1=most recent, 2=second most recent, 3=third",
                              "default": 1},
                },
                "required": ["project_id"],
            },
        },
    },
    # ── Pipeline orchestration (Framework mode: session-bound step-by-step) ──
    {
        "type": "function",
        "function": {
            "name": "start_pipeline",
            "description": "Start a skillflow pipeline config for a project. "
                           "Creates a run, executes steps until a checkpoint or end. "
                           "Returns the current state: checkpoint info, completion, or error. "
                           "Binds the run to this chat session so checkpoints appear here.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Target project ID"},
                    "config": {"type": "string", "description": "Skillflow config name (default: dpe_default_v2)",
                               "default": "dpe_default_v2"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_checkpoint",
            "description": "Approve a pending checkpoint and continue the pipeline. "
                           "Only call when the user has indicated approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID from the checkpoint"},
                },
                "required": ["run_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reject_checkpoint",
            "description": "Reject a pending checkpoint with feedback. "
                           "The pipeline will redo the checkpoint step with the feedback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID from the checkpoint"},
                    "feedback": {"type": "string", "description": "User's feedback for redoing the step"},
                },
                "required": ["run_id", "feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pipeline_status",
            "description": "Get the current status of a pipeline run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID"},
                },
                "required": ["run_id"],
            },
        },
    },
]


# ── Config loading ─────────────────────────────────────────────────

def _load_meta_agent_config(config_path: str = _DEFAULT_CONFIG_PATH) -> dict:
    # Default meta_agent config (was in agent_configs/meta_conversation.yaml)
    default_config = {
        "model": "minimax/MiniMax-M3",
        "template": "meta_conversation.md",
        "tools": ["save_draft_brief", "suggest_submit_project"],
        "max_tool_turns": 20,
        "thinking": {"enable": True},
    }
    try:
        base = Path(__file__).resolve().parent.parent
        v2_path = base / _DEFAULT_CONFIG_PATH_V2
        if v2_path.exists():
            with open(v2_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            meta = config.get("meta_agent", {})
            if meta:
                return meta

        path = Path(config_path)
        if not path.exists():
            path = base / config_path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            return config.get("meta_agent", {})
    except Exception:
        pass
    return default_config


def _resolve_provider(model_name: str, config_path: str = "llm_providers.json"):
    """Resolve custom provider prefix to api_base + api_key (same as ai_router.py)."""
    api_base = None
    api_key = None
    litellm_model = model_name

    if os.path.exists(config_path) and "/" in model_name:
        provider, actual_model = model_name.split("/", 1)
        with open(config_path, "r", encoding="utf-8") as f:
            providers = json.load(f)
        if provider in providers:
            cfg = providers[provider]
            api_base = cfg.get("base_url")
            key_env = cfg.get("api_key_env")
            if key_env:
                api_key = os.getenv(key_env)
            litellm_model = f"openai/{actual_model}"

    return litellm_model, api_base, api_key


# ── MetaAgent ──────────────────────────────────────────────────────

class MetaAgent:
    """Backend meta agent: tool-use loop over LiteLLM with streaming."""

    def __init__(self, db, ws, owner_email: str = "cli@local", session_id: str = None):
        self.db = db
        self.ws = ws
        self.owner_email = owner_email
        self.session_id = session_id

        cfg = _load_meta_agent_config()
        raw_model = cfg.get("model", "minimax/MiniMax-M3")
        self._raw_model = raw_model
        self.litellm_model, self.api_base, self.api_key = _resolve_provider(raw_model)
        self.enable_thinking = cfg.get("enable_thinking", False)
        self.thinking_effort = cfg.get("thinking_effort")
        self.max_tool_turns = cfg.get("max_tool_turns", 20)

        litellm.telemetry = False
        litellm.drop_params = True

    def _build_system_prompt(self, current_project: str | None) -> str:
        return SYSTEM_PROMPT.format(
            current_project=current_project or "none",
            owner_email=self.owner_email,
        )

    def _build_messages(self, history: list[dict], current_project: str | None) -> list[dict]:
        messages = [{"role": "system", "content": self._build_system_prompt(current_project)}]
        messages.extend(history)
        return messages

    async def chat(
        self,
        message: str,
        history: list[dict],
        current_project: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run the agent loop. Yields SSE events."""
        messages = self._build_messages(history, current_project)
        messages.append({"role": "user", "content": message})

        tool_turns = 0
        consecutive_errors = 0  # AT-28: track consecutive tool errors for recovery
        try:
            while tool_turns < self.max_tool_turns:
                full_text = ""
                tool_calls = []

                async for event in self._stream_llm(messages):
                    if event["_type"] == "text_delta":
                        yield {"type": "text_delta", "content": event["content"]}
                    elif event["_type"] == "collected":
                        full_text = event["text"]
                        tool_calls = event["tool_calls"]

                if not tool_calls:
                    # Safety check: model claimed to submit but didn't call any tool
                    text_lower = full_text.lower()
                    submit_claimed = any(
                        p in text_lower
                        for p in ["task has been submitted", "task submitted", "i've submitted", "i have submitted"]
                    )
                    if submit_claimed and "suggest_submit_task" not in str(messages[-1:]).lower():
                        messages.append({"role": "assistant", "content": full_text})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You claimed to submit a task but did not call the suggest_submit_task tool. "
                                "Please call save_draft_task then suggest_submit_task now."
                            ),
                        })
                        tool_turns += 1
                        continue
                    yield {"type": "done", "message": {"role": "assistant", "content": full_text}}
                    return

                # Append assistant message with tool_calls
                messages.append(self._build_assistant_msg(full_text, tool_calls))

                turn_errors = 0
                for tc in tool_calls:
                    yield {"type": "tool_call", "name": tc["name"], "args": tc["args"]}
                    try:
                        result = await self._execute_tool(tc["name"], tc["args"])
                    except Exception as e:
                        result = {"error": str(e)}
                    # AT-28: track errors to detect unrecoverable states
                    if "error" in result:
                        turn_errors += 1
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 0  # reset on success
                    yield {"type": "tool_result", "name": tc["name"], "result": result}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, default=str, ensure_ascii=False),
                    })

                # AT-28: if all tool calls in this turn errored and we've had
                # 3+ consecutive errors, guide the LLM to exit gracefully instead
                # of looping forever in an unrecoverable state.
                if turn_errors > 0 and turn_errors == len(tool_calls) and consecutive_errors >= 3:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Several of your tool calls have returned errors. "
                            "Please stop and summarize what you were able to accomplish "
                            "or ask the user for guidance. Do NOT retry the same failing tools."
                        ),
                    })
                    # Let the LLM have one more turn to respond to this guidance.
                    # If it still errors, the max_tool_turns guard will catch it.

                tool_turns += 1

            yield {"type": "error", "message": f"Max tool turns ({self.max_tool_turns}) reached."}

        except Exception as e:
            yield {"type": "error", "message": f"Agent error: {e}"}

    async def _stream_llm(self, messages: list[dict]) -> AsyncGenerator[dict, None]:
        """Stream LLM response. Yields text_delta events in real-time,
        then a single 'collected' event with the full text and parsed tool_calls."""
        kwargs = {
            "model": self.litellm_model,
            "messages": messages,
            "tools": TOOL_DEFINITIONS,
            "stream": True,
            "temperature": 0.3,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.enable_thinking:
            kwargs.pop("temperature", None)
            if self.thinking_effort:
                kwargs["reasoning_effort"] = self.thinking_effort
            provider = self.litellm_model.split("/", 1)[-1] if "/" in self.litellm_model else ""
            extra_body = {}
            if "minimax" in (getattr(self, "_raw_model", "") or ""):
                extra_body["reasoning_split"] = True
            else:
                extra_body["thinking"] = {"type": "enabled"}
            kwargs["extra_body"] = extra_body

        response = await litellm.acompletion(**kwargs)

        full_text = ""
        tool_calls_map: dict[int, dict] = {}

        async for chunk in response:
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                full_text += delta.content
                yield {"_type": "text_delta", "content": delta.content}

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "args_str": "",
                        }
                    if tc_delta.id:
                        tool_calls_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_map[idx]["args_str"] += tc_delta.function.arguments

        tool_calls = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            try:
                args = json.loads(tc["args_str"]) if tc["args_str"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({"id": tc["id"], "name": tc["name"], "args": args})

        yield {"_type": "collected", "text": full_text, "tool_calls": tool_calls}

    def _build_assistant_msg(self, text: str, tool_calls: list[dict]) -> dict:
        msg = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"], ensure_ascii=False),
                    },
                }
                for tc in tool_calls
            ]
        return msg

    # ── Tool dispatch ──────────────────────────────────────────────

    async def _execute_tool(self, name: str, args: dict) -> dict:
        """Dispatch a tool call to the appropriate handler."""
        handler = _TOOL_HANDLERS.get(name)
        if not handler:
            return {"error": f"Unknown tool: {name}"}
        try:
            result = handler(self, args)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except Exception as e:
            return {"error": f"Tool '{name}' failed: {e}"}

    # ── Tool implementations ───────────────────────────────────────

    def _tool_list_projects(self, args: dict) -> dict:
        projects = self.db.list_projects_with_stats(owner_email=None)
        return {"projects": projects}

    def _tool_get_project(self, args: dict) -> dict:
        p = self.db.get_project(args["project_id"])
        if not p:
            return {"error": f"Project '{args['project_id']}' not found"}
        return {"project": p}

    def _tool_create_project(self, args: dict) -> dict:
        pid = args["project_id"]
        existing = self.db.get_project(pid)
        if existing:
            # AT-27: idempotent — return success so the LLM can continue
            # its flow instead of hitting an error it can't recover from.
            return {"project_id": pid, "status": "already_exists",
                    "message": f"Project '{pid}' already exists. Proceed to save_draft_brief."}
        self.db.ensure_project(
            pid, name=args.get("name"),
            owner_email=self.owner_email,
            repo_type=args.get("repo_type", "new"),
            repo_path=args.get("repo_path"),
            repo_url=args.get("repo_url"),
        )
        # Gate the scheduler: don't let it pick up this project until the
        # meta conversation finishes (brief written + checkpoint approved).
        self.db.set_project_meta_state(pid, "drafting")
        self.ws.setup_workspace(
            pid,
            repo_type=args.get("repo_type", "new"),
            repo_path=args.get("repo_path"),
            repo_url=args.get("repo_url"),
        )
        return {"project_id": pid, "status": "created"}

    def _tool_update_project(self, args: dict) -> dict:
        pid = args["project_id"]
        self.db.update_project(
            pid,
            name=args.get("name"),
            brief=args.get("brief"),
            priority=args.get("priority"),
            status=args.get("status"),
        )
        return {"project_id": pid, "status": "updated"}

    def _tool_delete_project(self, args: dict) -> dict:
        pid = args["project_id"]
        ok = self.db.delete_project_cascade(pid)
        return {"project_id": pid, "deleted": ok}

    def _tool_save_draft_brief(self, args: dict) -> dict:
        """Save a draft project brief to disk."""
        import json as _json

        pid = args["project_id"]
        brief = args["brief"]
        name = args.get("name") or brief.get("project_name", pid)

        # Ensure the brief dict includes project_name so downstream formatters
        # don't show "Untitled" when the model omits it.
        if not brief.get("project_name"):
            brief["project_name"] = name

        # Write draft to workspace
        dps_path = self.ws._get_secure_path(pid)
        dps_path.mkdir(parents=True, exist_ok=True)
        draft = {
            "project_id": pid,
            "name": name,
            "brief": brief,
        }
        (dps_path / "draft_brief.json").write_text(
            _json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return {
            "status": "draft_saved",
            "project_id": pid,
            "message": f"Draft brief saved for '{name}'. Present the brief to the user in chat. "
                       f"When the user approves, call start_pipeline.",
        }

    def _tool_edit_draft_brief(self, args: dict) -> dict:
        """Merge changes into an existing draft brief."""
        import json as _json

        pid = args["project_id"]
        changes = args.get("changes", {})

        dps_path = self.ws._get_secure_path(pid)
        draft_path = dps_path / "draft_brief.json"
        if not draft_path.exists():
            return {
                "status": "error",
                "message": f"No draft brief found for '{pid}'. Call save_draft_brief first.",
            }

        draft = _json.loads(draft_path.read_text(encoding="utf-8"))
        brief = draft.get("brief", {})

        # Merge top-level name
        if "name" in changes:
            draft["name"] = changes["name"]

        # Merge brief fields
        for key in ("description", "project_name", "goals", "non_goals",
                     "tech_constraints", "user_stories", "target_users", "success_criteria"):
            if key in changes:
                brief[key] = changes[key]

        draft["brief"] = brief
        draft_path.write_text(_json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")

        return {
            "status": "draft_updated",
            "project_id": pid,
            "message": f"Draft brief updated. Present the updated brief to the user. "
                       f"When the user approves, call start_pipeline.",
        }

    def _tool_retry_project(self, args: dict) -> dict:
        pid = args["project_id"]
        ok = self.db.retry_project(pid)
        if ok:
            self.db.set_project_meta_state(pid, None)
            # Clean workspace dirs for all task steps so retried tasks start fresh
            self.ws.clean_all_task_step_dirs(pid)
            return {"status": "retried", "project_id": pid, "_wake": True}
        return {"error": "Project not found or not in failed state"}

    def _tool_refresh_planning(self, args: dict) -> dict:
        import json as _json
        pid = args["project_id"]
        project = self.db.get_project(pid)
        if not project:
            return {"error": f"Project '{pid}' not found"}
        raw = project.get("completed_project_steps", "[]")
        completed = _json.loads(raw) if isinstance(raw, str) else raw
        for step in ("1", "2"):
            if step in completed:
                completed.remove(step)
        self.db.set_completed_project_steps(pid, completed)
        return {"status": "refreshing", "project_id": pid, "steps_to_rerun": ["1", "2"], "_wake": True}

    def _tool_list_tasks(self, args: dict) -> dict:
        tasks = self.db.list_tasks_by_project(args["project_id"])
        return {"tasks": tasks}

    def _tool_save_draft_task(self, args: dict) -> dict:
        """Save a draft task spec to disk."""
        from core.meta_conversation import format_task_spec_as_prompt
        import json as _json

        pid = args["project_id"]
        prompt = args["prompt"]
        task_spec = args.get("task_spec")
        if task_spec:
            prompt = format_task_spec_as_prompt(task_spec)

        # Dedup
        existing_tasks = self.db.list_tasks_by_project(pid)
        for t in existing_tasks:
            if t.get("status") in ("pending", "running"):
                existing_prompt = (t.get("prompt") or "").strip()
                if existing_prompt and existing_prompt == prompt.strip():
                    return {
                        "status": "already_saved",
                        "task_id": t["id"],
                        "project_id": pid,
                        "message": f"Task #{t['id']} with the same prompt is already {t['status']}.",
                    }

        dps_path = self.ws._get_secure_path(pid)
        dps_path.mkdir(parents=True, exist_ok=True)

        draft = {
            "project_id": pid,
            "prompt": prompt,
            "task_spec": task_spec,
        }
        draft_path = dps_path / "draft_task.json"
        draft_path.write_text(_json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")

        return {
            "status": "draft_saved",
            "project_id": pid,
            "message": "Draft task saved. Call suggest_submit_task to present it for approval.",
        }

    def _tool_suggest_submit_task(self, args: dict) -> dict:
        """Read the saved draft task and format it for user approval."""
        import json as _json

        pid = args["project_id"]

        dps_path = self.ws._get_secure_path(pid)
        draft_path = dps_path / "draft_task.json"
        if not draft_path.exists():
            return {
                "status": "error",
                "message": f"No draft task found for '{pid}'. Call save_draft_task first.",
            }

        draft = _json.loads(draft_path.read_text(encoding="utf-8"))
        prompt = draft.get("prompt", "")
        task_spec = draft.get("task_spec")

        # Check project exists
        project = self.db.get_project(pid)
        if not project:
            return {
                "status": "error",
                "message": f"Project '{pid}' not found. Create it first.",
            }

        task_summary = f"**Project:** {pid}\n**Task:** {prompt[:500]}"
        if task_spec:
            if task_spec.get("description"):
                task_summary += f"\n**Description:** {task_spec['description'][:500]}"
            if task_spec.get("acceptance_criteria"):
                ac = task_spec["acceptance_criteria"]
                if isinstance(ac, list):
                    task_summary += "\n**Acceptance Criteria:**\n" + "\n".join(f"  - {c}" for c in ac[:10])
                else:
                    task_summary += f"\n**Acceptance Criteria:** {str(ac)[:300]}"

        return {
            "status": "pending_confirm",
            "project_id": pid,
            "prompt": prompt,
            "task_spec": task_spec,
            "task_summary": task_summary,
            "message": "Task is ready for review. Please approve to submit.",
        }

    def _tool_get_task(self, args: dict) -> dict:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args["task_id"],)).fetchone()
            if not row:
                return {"error": f"Task #{args['task_id']} not found"}
            return {"task": dict(row)}

    def _tool_retry_task(self, args: dict) -> dict:
        task_id = args["task_id"]
        # Get project_id before retry resets the task state
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT project_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            pid = row["project_id"] if row else None
        ok = self.db.retry_task(task_id)
        if ok:
            if pid:
                self.ws.clean_all_task_step_dirs(pid)
                # Un-fail the project so scheduler picks up the retried task
                project = self.db.get_project(pid)
            return {"status": "retried", "task_id": task_id, "_wake": True}
        return {"error": "Task not found or not failed"}

    def _tool_get_step_output(self, args: dict) -> dict:
        task_id = args["task_id"]
        step_id = args["step_id"]
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT project_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if not row:
            return {"error": f"Task #{task_id} not found"}
        project_id = row["project_id"]
        final_dir = self.ws.get_final_path(project_id, step_id)
        if not final_dir.exists():
            return {"error": f"No output for step {step_id}"}
        files = {}
        for item in final_dir.rglob("*"):
            if item.is_file() and item.name != "_snapshot.json":
                rel = str(item.relative_to(final_dir))
                try:
                    files[rel] = item.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        return {"step_id": step_id, "files": files}

    def _tool_list_workspace_tree(self, args: dict) -> dict:
        pid = args["project_id"]
        base = self.ws._get_secure_path(pid)
        subdir = args.get("subdir")
        if subdir:
            base = base / subdir
        if not base.exists():
            return {"error": f"Workspace not found for {pid}"}
        tree = []
        for item in sorted(base.rglob("*")):
            if item.is_file():
                rel = str(item.relative_to(base))
                tree.append(rel)
        return {"project_id": pid, "tree": tree[:200]}

    def _tool_read_workspace_file(self, args: dict) -> dict:
        pid = args["project_id"]
        path = args["path"]
        base = self.ws._get_secure_path(pid)
        target = (base / path).resolve()
        if not str(target).startswith(str(base.resolve())):
            return {"error": "Path traversal denied"}
        if not target.exists():
            return {"error": f"File not found: {path}"}
        if not target.is_file():
            return {"error": f"Not a file: {path}"}
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": str(e)}
        return {"path": path, "content": content[:50000]}

    def _tool_retrieve_previous_context(self, args: dict) -> dict:
        pid = args["project_id"]
        which = args.get("which", 1)
        _META_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(_META_DIR.glob(f"{pid}_context_*.json"), reverse=True)
        if not files:
            return {"error": "No saved context files found"}
        if which < 1 or which > len(files):
            return {"error": f"Invalid context index {which}. Available: 1-{len(files)}"}
        chosen = files[which - 1]
        data = json.loads(chosen.read_text())
        return {"which": which, "messages": data, "saved_at": chosen.stat().st_mtime}

    # ── Pipeline orchestration (Framework mode: session-bound) ──────

    async def _run_pipeline_until_checkpoint(self, run_id: str) -> dict:
        """Execute skillflow steps until a checkpoint or the run ends.

        Advances the run, claims agent/tool steps, executes them via
        StepRunner, and confirms.  Stops when:
          - advance_run returns None (checkpoint or terminal state)
          - Execution fails
        Returns a summary dict the LLM can present to the user.
        """
        from api.dependencies import get_skillflow
        from aitelier.runner import AgentStepRunner
        from core.event_bus import event_bus

        sf = get_skillflow()

        runner = AgentStepRunner(
            db_manager=self.db,
            workspace_manager=self.ws,
            agent_factory=None,
            prompt_assembler=None,
            event_bus=event_bus,
        )

        MAX_STEPS = 50  # safety valve per tool call
        steps_run = 0

        try:
            while steps_run < MAX_STEPS:
                next_node = sf.advance_run(run_id)
                if next_node is None:
                    run = sf.get_run(run_id)
                    status = run["status"]
                    if status == "paused":
                        # Checkpoint — surface to user via the agent
                        label = run.get("current_node", "Checkpoint")
                        steps = sf.get_steps(run_id)
                        # Find the checkpoint step (last completed with checkpoint=True)
                        resolver = sf._get_resolver(run["graph_name"])
                        checkpoint_step_id = ""
                        checkpoint_data = None
                        for s in reversed(steps):
                            if s["status"] == "completed":
                                node = resolver.get_node(s["step_id"])
                                if node and node.checkpoint:
                                    checkpoint_step_id = s["step_id"]
                                    label = node.checkpoint_label or s["step_id"]
                                    # Grab step output for user review
                                    try:
                                        out_dir = self.ws.get_final_path(
                                            run.get("project_id", ""), s["step_id"]
                                        )
                                        if out_dir.exists():
                                            checkpoint_data = {}
                                            for f in sorted(out_dir.rglob("*")):
                                                if f.is_file() and f.name != "_snapshot.json":
                                                    rel = str(f.relative_to(out_dir))
                                                    try:
                                                        content = f.read_text(
                                                            encoding="utf-8", errors="replace"
                                                        )[:3000]
                                                        checkpoint_data[rel] = content
                                                    except Exception:
                                                        pass
                                    except Exception:
                                        pass
                                    break
                        return {
                            "status": "checkpoint",
                            "run_id": run_id,
                            "project_id": run.get("project_id", ""),
                            "step_id": checkpoint_step_id,
                            "label": label,
                            "data": checkpoint_data,
                            "message": f"Pipeline paused at checkpoint: {label}. "
                                       f"Wait for user approval before calling "
                                       f"approve_checkpoint or reject_checkpoint.",
                        }
                    elif status == "completed":
                        # Gather final outputs
                        outputs = {}
                        for s in steps:
                            if s["status"] == "completed":
                                try:
                                    out_dir = self.ws.get_final_path(
                                        run.get("project_id", ""), s["step_id"]
                                    )
                                    if out_dir.exists():
                                        files = {}
                                        for f in sorted(out_dir.rglob("*")):
                                            if f.is_file() and f.name != "_snapshot.json":
                                                try:
                                                    files[str(f.relative_to(out_dir))] = \
                                                        f.read_text(encoding="utf-8", errors="replace")[:2000]
                                                except Exception:
                                                    pass
                                        if files:
                                            outputs[s["step_id"]] = files
                                except Exception:
                                    pass
                        return {
                            "status": "completed",
                            "run_id": run_id,
                            "project_id": run.get("project_id", ""),
                            "steps_completed": len([s for s in steps if s["status"] == "completed"]),
                            "outputs": outputs,
                            "message": "Pipeline completed successfully.",
                        }
                    elif status == "failed":
                        return {
                            "status": "failed",
                            "run_id": run_id,
                            "error": run.get("error_reason", "Unknown error"),
                            "message": f"Pipeline failed: {run.get('error_reason', 'Unknown error')[:200]}",
                        }
                    else:
                        return {
                            "status": status,
                            "run_id": run_id,
                            "message": f"Pipeline in state: {status}",
                        }

                # Claim the next step
                try:
                    claimed = sf.claim_next_step(run_id)
                except Exception as e:
                    return {
                        "status": "error",
                        "run_id": run_id,
                        "message": f"Failed to claim step: {e}",
                    }

                if claimed is None:
                    continue  # race — retry

                # Execute the step
                try:
                    result = await runner.execute(claimed)
                    sf.confirm_step(claimed.token, result)
                    steps_run += 1
                except Exception as e:
                    error_msg = str(e)[:200]
                    try:
                        sf.fail_step(claimed.token, error_msg, retryable=True)
                    except Exception:
                        pass
                    return {
                        "status": "error",
                        "run_id": run_id,
                        "step_id": claimed.step_id,
                        "message": f"Step '{claimed.step_id}' failed: {error_msg}",
                    }

            return {
                "status": "error",
                "run_id": run_id,
                "message": f"Reached max steps ({MAX_STEPS}) without checkpoint or end. "
                           f"Possible non-converging loop.",
            }
        except Exception as e:
            return {
                "status": "error",
                "run_id": run_id,
                "message": f"Pipeline execution error: {e}",
            }

    async def _tool_start_pipeline(self, args: dict) -> dict:
        """Start a skillflow pipeline for a project, bind to session."""
        from api.dependencies import get_skillflow

        project_id = args["project_id"]
        config = args.get("config", "dpe_default_v2")

        project = self.db.get_project(project_id)
        if not project:
            return {"error": f"Project '{project_id}' not found"}

        sf = get_skillflow()

        # Clear drafting gate so the pipeline can run
        self.db.set_project_meta_state(project_id, None)

        # Get or create the skillflow run
        run_id = sf.get_or_create_run(config, project_id, {
            "project_id": project_id,
            "brief": project.get("brief", ""),
        })

        run = sf.get_run(run_id)
        if run and run["status"] == "pending":
            sf.start_run(run_id)
        elif run and run["status"] in ("running", "paused"):
            return {
                "status": "already_running",
                "run_id": run_id,
                "message": f"Pipeline is already {run['status']}. "
                           f"Use get_pipeline_status to check progress.",
            }

        # Bind run to this session
        if self.session_id:
            self.db.link_run_to_session(self.session_id, run_id)

        # Run until checkpoint or end
        result = await self._run_pipeline_until_checkpoint(run_id)

        # Sync project status
        from core.scheduler import _sync_project_status_to_db
        try:
            _sync_project_status_to_db(project_id)
        except Exception:
            pass

        # Wake the scheduler for any background work
        from core.scheduler import wake_scheduler
        wake_scheduler()

        return result

    async def _tool_approve_checkpoint(self, args: dict) -> dict:
        """Approve a checkpoint and continue the pipeline."""
        from api.dependencies import get_skillflow

        run_id = args["run_id"]
        sf = get_skillflow()

        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}

        if run["status"] == "completed":
            return {"status": "already_completed", "run_id": run_id,
                    "message": "Pipeline already completed."}

        if run["status"] != "paused":
            return {"error": f"Run is not paused (status: {run['status']}). "
                             f"Use get_pipeline_status to check."}

        # Approve and continue
        try:
            sf.approve_checkpoint(run_id)
        except Exception as e:
            return {"error": f"Failed to approve checkpoint: {e}"}

        # Clear meta_state so scheduler can also pick it up
        project_id = run.get("project_id", "")
        if project_id:
            self.db.set_project_meta_state(project_id, None)

        # Continue pipeline
        result = await self._run_pipeline_until_checkpoint(run_id)

        from core.scheduler import _sync_project_status_to_db, wake_scheduler
        try:
            _sync_project_status_to_db(project_id)
        except Exception:
            pass
        wake_scheduler()

        return result

    async def _tool_reject_checkpoint(self, args: dict) -> dict:
        """Reject a checkpoint with feedback and redo the step."""
        from api.dependencies import get_skillflow

        run_id = args["run_id"]
        feedback = args.get("feedback", "")

        sf = get_skillflow()

        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}

        if run["status"] not in ("paused", "failed"):
            return {"error": f"Run is not in a rejectable state (status: {run['status']}). "
                             f"Use get_pipeline_status to check."}

        # Find the checkpoint step
        steps = sf.get_steps(run_id)
        resolver = sf._get_resolver(run["graph_name"])
        checkpoint_step_id = ""
        for s in reversed(steps):
            if s["status"] == "completed":
                node = resolver.get_node(s["step_id"])
                if node and node.checkpoint:
                    checkpoint_step_id = s["step_id"]
                    break

        if not checkpoint_step_id:
            return {"error": "No completed checkpoint step found"}

        # Reject
        try:
            sf.reject_checkpoint(run_id, checkpoint_step_id, feedback)
        except Exception as e:
            return {"error": f"Failed to reject checkpoint: {e}"}

        # Continue pipeline (will redo the checkpoint step)
        result = await self._run_pipeline_until_checkpoint(run_id)

        from core.scheduler import _sync_project_status_to_db, wake_scheduler
        project_id = run.get("project_id", "")
        try:
            _sync_project_status_to_db(project_id)
        except Exception:
            pass
        wake_scheduler()

        return result

    def _tool_get_pipeline_status(self, args: dict) -> dict:
        """Get current status of a pipeline run."""
        from api.dependencies import get_skillflow

        run_id = args["run_id"]
        sf = get_skillflow()

        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}

        steps = sf.get_steps(run_id)
        completed = [s["step_id"] for s in steps if s["status"] == "completed"]
        current = run.get("current_node", "")

        return {
            "run_id": run_id,
            "project_id": run.get("project_id", ""),
            "graph": run.get("graph_name", ""),
            "status": run["status"],
            "current_node": current,
            "steps_completed": len(completed),
            "completed_steps": completed,
            "total_steps": len(steps),
        }

_TOOL_HANDLERS = {
    "list_projects": MetaAgent._tool_list_projects,
    "get_project": MetaAgent._tool_get_project,
    "create_project": MetaAgent._tool_create_project,
    "update_project": MetaAgent._tool_update_project,
    "delete_project": MetaAgent._tool_delete_project,
    "save_draft_brief": MetaAgent._tool_save_draft_brief,
    "edit_draft_brief": MetaAgent._tool_edit_draft_brief,
    "retry_project": MetaAgent._tool_retry_project,
    "refresh_planning": MetaAgent._tool_refresh_planning,
    "list_tasks": MetaAgent._tool_list_tasks,
    "save_draft_task": MetaAgent._tool_save_draft_task,
    "suggest_submit_task": MetaAgent._tool_suggest_submit_task,
    "get_task": MetaAgent._tool_get_task,
    "retry_task": MetaAgent._tool_retry_task,
    "get_step_output": MetaAgent._tool_get_step_output,
    "list_workspace_tree": MetaAgent._tool_list_workspace_tree,
    "read_workspace_file": MetaAgent._tool_read_workspace_file,
    "retrieve_previous_context": MetaAgent._tool_retrieve_previous_context,
    "start_pipeline": MetaAgent._tool_start_pipeline,
    "approve_checkpoint": MetaAgent._tool_approve_checkpoint,
    "reject_checkpoint": MetaAgent._tool_reject_checkpoint,
    "get_pipeline_status": MetaAgent._tool_get_pipeline_status,
}
