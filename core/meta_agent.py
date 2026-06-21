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

from core.ai_router import _read_secret

_DEFAULT_CONFIG_PATH = "dpe_roles_config.yaml"
_DEFAULT_CONFIG_PATH_V2 = "agent_configs/meta_conversation.yaml"
_META_DIR = Path.home() / ".AItelier" / "meta"

# ── System Prompt ──────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the AItelier butler — a helpful, general AI assistant. You can chat about \
anything, but you ALSO have the ability to build and modify software by running it \
through AItelier's deterministic pipeline.

## When the user wants to build or change software
When the user's message asks to build a new app/tool/library, or to add a feature \
or fix a bug in an existing project, you do NOT gather requirements yourself and you \
do NOT write code. Instead you START and RELAY a structured requirements \
conversation that the pipeline drives:

1. Decide which API to call:
   - NEW project (build from scratch) → start_new_project(project_id, initial_message)
   - EXISTING AItelier project (add feature / fix bug) → list_projects first, then
     start_from_aitelier_project(existing_project_id, initial_message, new_project_id=...)
     (new_project_id is optional — it auto-generates like "myapp-2")
   - EXISTING code at a known path → start_existing_project(project_id, repo_path, initial_message)
   - Clone a git URL → start_from_git_url(project_id, repo_url, initial_message)
2. Each returns either a clarifying QUESTION, a BRIEF for review, or "rejected".
3. RELAY the pipeline's question to the user verbatim — do NOT invent your own
   questions or brief. When the user replies, call
   answer_project_conversation(run_id, answer=<their reply>).
4. When a BRIEF is returned, present it and ask the user to approve. On approval
   call approve_project_brief(run_id) — this starts the build pipeline. If they
   want changes, call answer_project_conversation(run_id, answer=<their changes>).

When a conversation is already in progress you will see an [ACTIVE PROJECT
CONVERSATION] note telling you the run_id and exactly which tool to call — follow it
and do NOT start a new conversation while one is active.

CRITICAL:
- NEVER write code or offer code snippets. The pipeline implements; you relay.
- NEVER invent the brief or the clarifying questions — they come from the pipeline.
- Only call approve_project_brief after the user has clearly approved the brief.

## When the user wants to turn a SKILL or WORKFLOW into a reusable pipeline
This is different from building software. If the user describes a repeatable
multi-step *process / skill* and wants it captured as a pipeline they can re-run
(e.g. "make me a pipeline that researches a topic, drafts, then fact-checks"),
call generate_pipeline(description=<the skill, verbatim>). It runs the
skill_converter pipeline and returns a Design Review checkpoint — relay it; on
approval (approve_checkpoint) it lints and emits the generated pipeline YAML.
Use start_new_project / start_from_aitelier_project for *software* (apps/tools/bug-fixes);
generate_pipeline for *converting a skill/workflow into a pipeline graph*.

## After a pipeline starts
Pipelines run and surface their own review checkpoints. Report progress with
get_pipeline_status(run_id); when the user explicitly approves/rejects a
checkpoint in chat, call approve_checkpoint / reject_checkpoint.

## Otherwise
For anything that isn't a build/modify request (questions, chit-chat, status),
just respond normally and helpfully.

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
    # ── Four start-project APIs (one per repo source) ──
    {
        "type": "function",
        "function": {
            "name": "start_new_project",
            "description": "Start a NEW project from scratch. No existing code. "
                           "Returns {status: 'question'|'brief_review', question?, brief_markdown?, run_id}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Short unique slug (e.g. 'habit-tracker')"},
                    "initial_message": {"type": "string", "description": "The user's request, verbatim"},
                    "name": {"type": "string", "description": "Optional display name"},
                },
                "required": ["project_id", "initial_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_from_aitelier_project",
            "description": "Add features / fix bugs on an existing AItelier-built project. "
                           "Finds the original project's code repo automatically. "
                           "Pass the original project_id and a NEW project_id for this work "
                           "(or omit new_project_id to auto-generate one). "
                           "Returns {status: 'question'|'brief_review', ...}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "existing_project_id": {"type": "string", "description": "The original project's ID (from list_projects)"},
                    "initial_message": {"type": "string", "description": "The user's request, verbatim"},
                    "new_project_id": {"type": "string", "description": "Optional: new project ID for this work. If omitted, auto-generated as <existing>-2, <existing>-3, etc."},
                    "name": {"type": "string", "description": "Optional display name for the new project"},
                },
                "required": ["existing_project_id", "initial_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_existing_project",
            "description": "Work on an existing codebase at a given filesystem path. "
                           "You MUST provide repo_path. "
                           "Returns {status: 'question'|'brief_review', ...}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Short unique slug for this work"},
                    "repo_path": {"type": "string", "description": "Absolute path to the code repository"},
                    "initial_message": {"type": "string", "description": "The user's request, verbatim"},
                    "name": {"type": "string", "description": "Optional display name"},
                },
                "required": ["project_id", "repo_path", "initial_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_from_git_url",
            "description": "Clone a git repository and start working on it. "
                           "Returns {status: 'question'|'brief_review', ...}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Short unique slug"},
                    "repo_url": {"type": "string", "description": "Git URL to clone"},
                    "initial_message": {"type": "string", "description": "The user's request, verbatim"},
                    "name": {"type": "string", "description": "Optional display name"},
                },
                "required": ["project_id", "repo_url", "initial_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_project_conversation",
            "description": "Pass the user's answer (or requested brief changes) to an in-progress "
                           "project conversation and advance it. Returns the next "
                           "{status: 'question'|'brief_review'|..., question?, brief_markdown?, run_id}.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "The conversation run_id"},
                    "answer": {"type": "string", "description": "The user's reply, verbatim"},
                },
                "required": ["run_id", "answer"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_project_brief",
            "description": "Approve the reviewed project brief and start the build (DPE) pipeline. "
                           "Call ONLY after the user has clearly approved the brief.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "The conversation run_id"},
                },
                "required": ["run_id"],
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
            "name": "list_code_tree",
            "description": "List the file tree of a project's actual code repository "
                           "(the live source, via get_code_path). Use this — not "
                           "list_workspace_tree — to understand existing code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "subdir": {"type": "string",
                               "description": "Optional subdirectory to scope"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_code_file",
            "description": "Read a file from a project's actual code repository "
                           "(the live source, via get_code_path). Use this — not "
                           "read_workspace_file — to read existing source files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Relative path within the repo (e.g. 'server.py')"},
                },
                "required": ["project_id", "path"],
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
    {
        "type": "function",
        "function": {
            "name": "generate_pipeline",
            "description": "Turn a SKILL or repeatable WORKFLOW description into a reusable "
                           "SkillFlow pipeline (a YAML graph) by running the skill_converter "
                           "pipeline. Use this when the user wants to capture a multi-step "
                           "process/skill as a pipeline they can re-run — NOT for building an "
                           "app or fixing code (use start_new_project / start_from_aitelier_project for software). "
                           "Returns a Design Review checkpoint to relay; on approval it lints "
                           "and emits the generated pipeline YAML.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string",
                                    "description": "The skill / workflow to convert into a pipeline."},
                    "name": {"type": "string",
                             "description": "Optional short name for the generated pipeline."},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_config_run",
            "description": "Start a run of a registered skillflow config BY NAME (any config "
                           "other than a DPE software build — use start_new_project for apps). "
                           "Use when the user asks to run a specific registered pipeline/config. "
                           "The run appears in the dashboards like any other. Returns the run id "
                           "(and, for butler-driven configs, the first checkpoint to relay).",
            "parameters": {
                "type": "object",
                "properties": {
                    "config_name": {"type": "string",
                                    "description": "The registered config name to run."},
                    "seed_text": {"type": "string",
                                  "description": "Seed input written to the config's first-step input file."},
                    "name": {"type": "string",
                             "description": "Optional human label for this run."},
                },
                "required": ["config_name"],
            },
        },
    },
]


# ── Config loading ─────────────────────────────────────────────────

def _load_meta_agent_config(config_path: str = _DEFAULT_CONFIG_PATH) -> dict:
    # Default meta_agent config (was in agent_configs/meta_conversation.yaml)
    default_config = {
        "model": "deepseek/deepseek-v4-flash",
        "template": "meta_conversation.md",
        "tools": [],
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
                # Resolve from the mounted secret file (not just env) so the key
                # works when delivered as a Docker secret. Mirrors ai_router.
                api_key = _read_secret(key_env)
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
        raw_model = cfg.get("model", "deepseek/deepseek-v4-flash")
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

    def _active_conversation_note(self) -> str | None:
        """If a meta_conversation run for this session is paused, return a system
        note telling the model exactly which tool to call. State-driven relay —
        this is what keeps the butler from re-deriving / re-starting a conversation."""
        if not self.session_id:
            return None
        try:
            from api.dependencies import get_skillflow
            from core.meta_run import read_gather_state, META_GRAPH
            sf = get_skillflow()
            for rid in self.db.get_runs_for_session(self.session_id):
                run = sf.get_run(rid)
                if not run or run.get("graph_name") != META_GRAPH:
                    continue
                if run.get("status") not in ("paused", "running"):
                    continue
                pid = run.get("project_id", "")
                gs = read_gather_state(self.ws, pid) or {}
                if gs.get("need_input"):
                    return (f"[ACTIVE PROJECT CONVERSATION] run_id=\"{rid}\", project=\"{pid}\". "
                            f"You previously asked: \"{gs.get('question', '')}\". The user's message "
                            f"is their answer — call answer_project_conversation(run_id=\"{rid}\", "
                            f"answer=<the user's message>). Do NOT start a new conversation.")
                return (f"[ACTIVE PROJECT CONVERSATION] run_id=\"{rid}\", project=\"{pid}\" — a brief "
                        f"is ready for review. If the user approves it, call "
                        f"approve_project_brief(run_id=\"{rid}\"). Otherwise treat their message as "
                        f"requested changes and call answer_project_conversation(run_id=\"{rid}\", "
                        f"answer=<the user's message>). Do NOT start a new conversation.")
        except Exception:
            return None
        return None

    async def chat(
        self,
        message: str,
        history: list[dict],
        current_project: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run the agent loop. Yields SSE events."""
        messages = self._build_messages(history, current_project)
        # Deterministic relay: if a project conversation is paused for this
        # session, tell the model exactly which tool to call (state-driven, so it
        # cannot re-derive / re-start the conversation).
        relay = self._active_conversation_note()
        if relay:
            messages.append({"role": "system", "content": relay})
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
                    "message": f"Project '{pid}' already exists."}
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

    # ── Project-conversation tools (drive the meta_conversation pipeline) ──

    def _slugify(self, text: str) -> str:
        import re
        s = re.sub(r"[^a-z0-9-]", "-", (text or "").lower()).strip("-")[:40]
        s = re.sub(r"-+", "-", s)
        return s or "project"

    def _append_conversation(self, project_id: str, line: str) -> None:
        """Append a line to the workspace transcript the gather step reads
        (meta/conversation.md)."""
        base = self.ws._get_secure_path(project_id) / "meta"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "conversation.md", "a", encoding="utf-8") as fh:
            fh.write(line if line.endswith("\n") else line + "\n")

    def _format_brief_md(self, brief: dict) -> str:
        try:
            from core.meta_conversation import format_brief_as_markdown
            return format_brief_as_markdown(brief or {})
        except Exception:
            return json.dumps(brief or {}, indent=2, ensure_ascii=False)

    def _find_active_project(self) -> str | None:
        """Return the project_id of an active *meta conversation* in this
        session, or None.  Only considers meta_conversation runs — DPE runs
        also set meta_state='running' but are NOT conversations."""
        if not self.session_id:
            return None
        try:
            run_ids = self.db.get_runs_for_session(self.session_id)
            from api.dependencies import get_skillflow
            sf = get_skillflow()
            for rid in run_ids or []:
                run = sf.get_run(rid)
                if not run:
                    continue
                # Only meta conversations, never DPE runs
                if run.get("graph_name") != "meta_conversation":
                    continue
                pid = run.get("project_id", "")
                if not pid:
                    continue
                proj = self.db.get_project(pid)
                if not proj:
                    continue
                ms = proj.get("meta_state", "")
                if ms in ("drafting", "paused", "running"):
                    return pid
        except Exception:
            pass
        return None

    def _log_error(self, msg: str) -> None:
        """Log an error to the server log for post-mortem debugging.
        Tool-level errors are often invisible in the chat UI."""
        import logging, sys
        logger = logging.getLogger("aitelier.meta")
        logger.error(msg)
        # Also write to stderr so it appears in the server log immediately
        print(f"[meta_agent ERROR] {msg}", file=sys.stderr, flush=True)

    # ── Public API wrappers (hide repo_type/repo_path from the LLM) ──

    async def _tool_start_new_project(self, args: dict) -> dict:
        return await self._tool_start_project_conversation({
            "project_id": args["project_id"],
            "initial_message": args["initial_message"],
            "name": args.get("name", ""),
            "repo_type": "new",
        })

    async def _tool_start_from_aitelier_project(self, args: dict) -> dict:
        existing_pid = args["existing_project_id"]
        # Find the original project
        proj = self.db.get_project(existing_pid)
        if not proj:
            return {"status": "error", "message": f"Project '{existing_pid}' not found. Use list_projects first."}
        repo_path = proj.get("repo_path")
        # If no explicit repo_path, use the default code location
        if not repo_path:
            from pathlib import Path as _Path
            default = _Path.home() / ".AItelier" / "projects" / existing_pid
            if default.is_dir():
                repo_path = str(default)
            else:
                return {"status": "error",
                        "message": f"No repo_path found for '{existing_pid}' and default path does not exist. Try start_existing_project with an explicit repo_path."}

        # Auto-generate new_project_id if not given
        new_pid = args.get("new_project_id", "")
        if not new_pid:
            base = existing_pid
            # Strip trailing digits to find base name
            import re
            m = re.match(r"(.+?)-(\d+)$", base)  # already has a suffix
            if m:
                base = m.group(1)
            # Find next available suffix
            n = 2
            while self.db.get_project(f"{base}-{n}"):
                n += 1
            new_pid = f"{base}-{n}"

        return await self._tool_start_project_conversation({
            "project_id": new_pid,
            "initial_message": args["initial_message"],
            "name": args.get("name", ""),
            "repo_type": "existing",
            "repo_path": repo_path,
        })

    async def _tool_start_existing_project(self, args: dict) -> dict:
        return await self._tool_start_project_conversation({
            "project_id": args["project_id"],
            "initial_message": args["initial_message"],
            "name": args.get("name", ""),
            "repo_type": "existing",
            "repo_path": args["repo_path"],
        })

    async def _tool_start_from_git_url(self, args: dict) -> dict:
        return await self._tool_start_project_conversation({
            "project_id": args["project_id"],
            "initial_message": args["initial_message"],
            "name": args.get("name", ""),
            "repo_type": "clone",
            "repo_url": args["repo_url"],
        })

    async def _tool_start_project_conversation(self, args: dict) -> dict:
        """Create the project + workspace, start the meta_conversation run, drive
        it to its first pause, and return the question / brief to relay."""
        from api.dependencies import get_skillflow
        from core.meta_run import META_GRAPH

        initial = (args.get("initial_message") or "").strip()
        pid = args.get("project_id") or self._slugify(args.get("name") or initial)
        repo_type = args.get("repo_type", "new")
        repo_path = args.get("repo_path")
        repo_url = args.get("repo_url")

        sf = get_skillflow()

        # ── Validation (BEFORE any project creation) ──────────────────
        # Catch bad args early so we never leave an orphan project.

        # repo_type="existing" requires repo_path — validate BEFORE touching DB
        if repo_type == "existing":
            if not repo_path:
                return {
                    "status": "error",
                    "message": (
                        "repo_type='existing' requires repo_path. "
                        "Call list_projects first to find the original project, "
                        "then pass repo_path='~/.AItelier/projects/<original-id>'."
                    ),
                }
            from pathlib import Path as _Path
            code_path = _Path(repo_path).expanduser().resolve()
            if not code_path.exists():
                return {
                    "status": "error",
                    "message": (
                        f"repo_path does not exist: {repo_path}. "
                        "Call list_projects to find the correct path, "
                        "or use repo_type='new' for a fresh project."
                    ),
                }

        # Prevent overwriting a completed project
        existing_project = self.db.get_project(pid)
        if existing_project:
            dpe_runs = sf.list_runs(pid)
            has_completed_dpe = any(
                r["graph_name"] == "dpe_default_v2" and r["status"] == "completed"
                for r in dpe_runs
            )
            if has_completed_dpe:
                return {
                    "status": "error",
                    "message": (
                        f"Project '{pid}' already has a completed build pipeline. "
                        f"Do NOT reuse this project_id. Instead, pick a NEW "
                        f"project_id (e.g. '{pid}-v2') and call "
                        f"start_project_conversation with repo_type='existing' "
                        f"and repo_path='~/.AItelier/projects/{pid}'."
                    ),
                }

        # AT-2: Idempotent guard
        existing_active = self._find_active_project()
        if existing_active:
            existing_run = sf.get_run_by_project(existing_active)
            if existing_run and existing_run["status"] in ("running", "paused"):
                result = await self._run_meta_until_checkpoint(existing_run["id"])
                if result.get("status") in ("question", "brief_review"):
                    return result
            self.db.set_project_meta_state(existing_active, None)

        # ── Now safe to create ────────────────────────────────────────
        if not self.db.get_project(pid):
            self.db.ensure_project(
                pid, name=args.get("name") or pid, owner_email=self.owner_email,
                repo_type=repo_type, repo_path=repo_path, repo_url=repo_url,
            )
        self.db.set_project_meta_state(pid, "drafting")

        try:
            self.ws.setup_workspace(pid, repo_type=repo_type, repo_path=repo_path, repo_url=repo_url)
        except Exception as e:
            return {"status": "error", "project_id": pid,
                    "message": f"workspace setup failed: {e}"}

        # Seed the transcript the gather step reads as context.
        self._append_conversation(pid, f"User: {initial}")

        try:
            run_id = sf.get_or_create_run(META_GRAPH, pid, {"project_id": pid})
        except Exception as e:
            self._log_error(f"get_or_create_run failed for {pid}: {e}")
            return {"status": "error", "project_id": pid,
                    "message": f"Failed to create conversation run: {e}"}
        run = sf.get_run(run_id)
        if run and run["status"] == "pending":
            sf.start_run(run_id)
        if self.session_id:
            self.db.link_run_to_session(self.session_id, run_id)

        result = await self._run_meta_until_checkpoint(run_id)
        if result.get("status") == "question":
            self._append_conversation(pid, f"Assistant: {result.get('question', '')}")
        return result

    async def _tool_answer_project_conversation(self, args: dict) -> dict:
        """Append the user's answer to the transcript, resume the gather step,
        and return the next question / brief."""
        from api.dependencies import get_skillflow
        from core.meta_run import submit_user_answer

        run_id = args["run_id"]
        answer = (args.get("answer") or "").strip()
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"status": "error",
                    "message": f"Conversation '{run_id}' not found — "
                               "the conversation may have already finished. "
                               "Check the project list for the correct project."}
        pid = run.get("project_id", "")

        # AT-1/AT-2: If the run is already completed, the checkpoint was approved
        # via the CLI before the meta agent could handle it.  Tell the user what
        # happened instead of failing silently (which triggers a new-project loop).
        if run.get("status") == "completed":
            return {"status": "completed_already",
                    "run_id": run_id, "project_id": pid,
                    "message": "The requirements conversation has already been "
                               "finalized. Use approve_project_brief if you want "
                               "to start the build pipeline."}

        self._append_conversation(pid, f"User: {answer}")
        try:
            submit_user_answer(sf, run_id, answer)
        except Exception as e:
            self._log_error(f"submit_user_answer failed for {run_id}: {e}")
            return {"status": "error", "run_id": run_id,
                    "message": f"Could not resume the conversation: {e}. "
                               "Try starting a new conversation or check the project status."}

        result = await self._run_meta_until_checkpoint(run_id)
        if result.get("status") == "question":
            self._append_conversation(pid, f"Assistant: {result.get('question', '')}")
        return result

    async def _tool_approve_project_brief(self, args: dict) -> dict:
        """Approve the reviewed brief, complete the meta run, and trigger DPE."""
        from api.dependencies import get_skillflow
        from core.meta_run import approve_meta, read_gather_state
        from core.project_submit import seed_and_trigger

        run_id = args["run_id"]
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"status": "error",
                    "message": f"Conversation '{run_id}' not found — "
                               "the conversation may have already ended. "
                               "Check the project list."}
        pid = run.get("project_id", "")

        # AT-1: If the run is already completed (gate resolved to terminal after
        # checkpoint approval), skip the approve_meta call — it would call
        # sf.complete_run() which is idempotent but unnecessary.
        run_already_completed = run.get("status") == "completed"

        gs = read_gather_state(self.ws, pid) or {}
        brief = gs.get("brief") or {}
        stories = brief.get("user_stories")
        if not (isinstance(stories, list) and any(str(s).strip() for s in stories)):
            return {"status": "error",
                    "message": "The brief has no user stories yet — the conversation "
                               "has not produced a complete brief. Keep talking to "
                               "add requirements before approving."}

        # Seed the canonical brief + goals and wake the scheduler (starts DPE),
        # then close the meta run.
        res = seed_and_trigger(self.db, self.ws, pid, brief)
        if run_already_completed:
            self._log_error(f"Meta run {run_id} was already completed when approving brief — "
                           "this is expected if the checkpoint was approved via the CLI")
        else:
            try:
                approve_meta(sf, run_id)
            except Exception as e:
                self._log_error(f"approve_meta failed for {run_id}: {e}")
        if res.get("status") not in ("submitted", "already_planned"):
            res.setdefault("project_id", pid)
            res["message"] = res.get("message",
                f"Failed to seed the build pipeline: {res.get('status', 'unknown error')}")
            return res

        # The meta agent now starts the DPE build pipeline and drives it to its
        # first review checkpoint, surfaced in this chat (reuses the proven
        # session-bound pipeline driver).
        dpe = await self._tool_start_pipeline({"project_id": pid, "config": "dpe_default_v2"})
        dpe.setdefault("project_id", pid)
        dpe.setdefault("message", f"Brief approved — the build pipeline for '{pid}' has started.")
        return dpe

    async def _run_meta_until_checkpoint(self, run_id: str) -> dict:
        """Drive the meta_conversation run until it pauses (gather checkpoint) or
        completes. Returns {status: 'question'|'brief_review'|'rejected'|'error', ...}."""
        from api.dependencies import get_skillflow
        from aitelier.runner import AgentStepRunner
        from core.event_bus import event_bus
        from core.meta_run import read_gather_state

        sf = get_skillflow()
        runner = AgentStepRunner(
            db_manager=self.db, workspace_manager=self.ws,
            agent_factory=None, prompt_assembler=None, event_bus=event_bus,
        )
        project_id = (sf.get_run(run_id) or {}).get("project_id", "")
        MAX_STEPS = 30
        steps_run = 0
        try:
            while steps_run < MAX_STEPS:
                next_node = sf.advance_run(run_id)
                if next_node is None:
                    run = sf.get_run(run_id)
                    status = run["status"]
                    if status == "paused":
                        gs = read_gather_state(self.ws, project_id) or {}
                        if gs.get("need_input"):
                            return {"status": "question", "run_id": run_id,
                                    "project_id": project_id,
                                    "question": gs.get("question")
                                        or "Could you tell me a bit more about what you want?",
                                    "message": "Relay this question to the user VERBATIM and then "
                                               "stop — do not call any tool until the user replies."}
                        brief = gs.get("brief") or {}
                        return {"status": "brief_review", "run_id": run_id,
                                "project_id": project_id, "brief": brief,
                                "brief_markdown": self._format_brief_md(brief),
                                "message": "Present this brief to the user and ask them to approve "
                                           "it. Do NOT invent your own questions. Stop and wait for "
                                           "their response; on approval call approve_project_brief."}
                    if status == "completed":
                        # terminal without a brief = intent classified as rejected
                        return {"status": "rejected", "run_id": run_id,
                                "project_id": project_id,
                                "message": "That doesn't look like a software build request."}
                    if status == "failed":
                        return {"status": "error", "run_id": run_id,
                                "message": run.get("error_reason", "conversation failed")}
                    return {"status": status, "run_id": run_id}
                try:
                    claimed = sf.claim_next_step(run_id)
                except Exception as e:
                    return {"status": "error", "run_id": run_id, "message": f"claim failed: {e}"}
                if claimed is None:
                    continue
                try:
                    result = await runner.execute(claimed)
                    sf.confirm_step(claimed.token, result)
                    steps_run += 1
                except Exception as e:
                    try:
                        sf.fail_step(claimed.token, str(e)[:200], retryable=True)
                    except Exception:
                        pass
                    return {"status": "error", "run_id": run_id, "step_id": claimed.step_id,
                            "message": f"step '{claimed.step_id}' failed: {str(e)[:200]}"}
            return {"status": "error", "run_id": run_id,
                    "message": "conversation did not converge (max steps)."}
        except Exception as e:
            return {"status": "error", "run_id": run_id, "message": f"conversation error: {e}"}

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

    def _tool_list_code_tree(self, args: dict) -> dict:
        """List the file tree of a project's actual code repository."""
        pid = args["project_id"]
        base = self.ws.get_code_path(pid)
        subdir = args.get("subdir")
        if subdir:
            base = base / subdir
        if not base.exists():
            return {"error": f"Code repo not found for {pid}"}
        tree = []
        for item in sorted(base.rglob("*")):
            if item.is_file() and "/.git/" not in f"/{item}":
                tree.append(str(item.relative_to(base)))
        return {"project_id": pid, "tree": tree[:200]}

    def _tool_read_code_file(self, args: dict) -> dict:
        """Read a file from a project's actual code repository."""
        pid = args["project_id"]
        path = args["path"]
        base = self.ws.get_code_path(pid).resolve()
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            return {"error": "Path traversal denied"}
        if not target.is_file():
            return {"error": f"File not found: {path}"}
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": str(e)}
        return {"path": path, "content": content[:50000]}

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
                    steps = sf.get_steps(run_id)
                    if status == "paused":
                        # Checkpoint — surface to user via the agent
                        label = run.get("current_node", "Checkpoint")
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

    async def _tool_generate_pipeline(self, args: dict) -> dict:
        """Generate a reusable SkillFlow pipeline from a skill description by
        running skillflow's skill_converter pipeline in framework mode, then
        relaying its design checkpoint. The converter's host-mode agents resolve
        to HOST_AGENT_MODEL with their embedded prompts (see core/agents.py)."""
        from api.dependencies import get_skillflow

        description = (args.get("description") or "").strip()
        if not description:
            return {"error": "description is required — the skill/workflow to convert."}
        name = args.get("name") or description[:40]
        pid = "convert-" + self._slugify(name)
        sf = get_skillflow()

        # Launch the skill_converter config via the generic launcher (ensures the
        # run row tagged config_name='skill_converter', seeds skill_description.md,
        # creates + starts the run). skill_converter is butler-driven
        # (scheduler_owned=false), so the polling scheduler never grabs it.
        from core.run_launcher import start_config_run
        launch = start_config_run(
            self.db, self.ws, "skill_converter", pid,
            seed_text=description, name=name, owner_email=self.owner_email,
        )
        if launch.get("status") == "error":
            return {"error": launch.get("message")}
        run_id = launch["run_id"]
        sid = getattr(self, "session_id", None)
        if sid:
            try:
                self.db.link_run_to_session(sid, run_id)
            except Exception:
                pass

        result = await self._run_pipeline_until_checkpoint(run_id)
        result["project_id"] = pid
        result["pipeline"] = "skill_converter"
        if result.get("status") == "completed":
            try:
                from skillflow.plugins.skill_converter import get_output_file
                p = get_output_file(sf, run_id)
                if p and p.exists():
                    result["generated_pipeline_path"] = str(p)
                    result["generated_pipeline_yaml"] = p.read_text(encoding="utf-8")[:4000]
            except Exception:
                pass
        return result

    async def _tool_start_config_run(self, args: dict) -> dict:
        """Start a run of any registered skillflow config by name. Scheduler-owned
        configs are driven by the poller; butler-driven configs are driven inline
        and their first checkpoint is relayed into the chat."""
        from api.dependencies import get_config_registry
        from core.run_launcher import start_config_run, generate_run_id

        config_name = (args.get("config_name") or "").strip()
        if not config_name:
            return {"error": "config_name is required."}
        manifest = get_config_registry().get(config_name)
        if not manifest:
            avail = ", ".join(m.config_name for m in get_config_registry().list())
            return {"error": f"Unknown config '{config_name}'. Available: {avail}"}

        pid = generate_run_id(config_name)
        result = start_config_run(
            self.db, self.ws, config_name, pid,
            seed_text=args.get("seed_text"),
            name=args.get("name") or config_name,
            owner_email=self.owner_email,
        )
        if result.get("status") == "error":
            return {"error": result.get("message")}
        run_id = result.get("run_id")
        sid = getattr(self, "session_id", None)
        if sid and run_id:
            try:
                self.db.link_run_to_session(sid, run_id)
            except Exception:
                pass
        # Butler-driven config: drive to the first checkpoint and relay it.
        if run_id and not manifest.scheduler_owned:
            driven = await self._run_pipeline_until_checkpoint(run_id)
            driven["project_id"] = pid
            driven["config_name"] = config_name
            return driven
        return result


_TOOL_HANDLERS = {
    "list_projects": MetaAgent._tool_list_projects,
    "get_project": MetaAgent._tool_get_project,
    "update_project": MetaAgent._tool_update_project,
    "start_new_project": MetaAgent._tool_start_new_project,
    "start_from_aitelier_project": MetaAgent._tool_start_from_aitelier_project,
    "start_existing_project": MetaAgent._tool_start_existing_project,
    "start_from_git_url": MetaAgent._tool_start_from_git_url,
    # Legacy — hidden from LLM but kept for backward compat
    "start_project_conversation": MetaAgent._tool_start_project_conversation,
    "answer_project_conversation": MetaAgent._tool_answer_project_conversation,
    "approve_project_brief": MetaAgent._tool_approve_project_brief,
    "retry_project": MetaAgent._tool_retry_project,
    "refresh_planning": MetaAgent._tool_refresh_planning,
    "list_tasks": MetaAgent._tool_list_tasks,
    "list_code_tree": MetaAgent._tool_list_code_tree,
    "read_code_file": MetaAgent._tool_read_code_file,
    "get_task": MetaAgent._tool_get_task,
    "retry_task": MetaAgent._tool_retry_task,
    "get_step_output": MetaAgent._tool_get_step_output,
    "list_workspace_tree": MetaAgent._tool_list_workspace_tree,
    "read_workspace_file": MetaAgent._tool_read_workspace_file,
    "retrieve_previous_context": MetaAgent._tool_retrieve_previous_context,
    "approve_checkpoint": MetaAgent._tool_approve_checkpoint,
    "reject_checkpoint": MetaAgent._tool_reject_checkpoint,
    "get_pipeline_status": MetaAgent._tool_get_pipeline_status,
    "generate_pipeline": MetaAgent._tool_generate_pipeline,
    "start_config_run": MetaAgent._tool_start_config_run,
}
