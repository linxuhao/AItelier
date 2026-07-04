# core/meta_agent.py
# Autonomous meta agent — the "butler" that manages projects, tasks,
# checkpoints, and workspace inspection via tool-use over LiteLLM.
# Runs inside the backend process with direct DBManager/WorkspaceManager access.

import asyncio
import json
import os
import re
import traceback
from pathlib import Path
from typing import AsyncGenerator

import litellm
import yaml
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from core.ai_router import AIGateway, RETRYABLE_EXCEPTIONS, _read_secret

_DEFAULT_CONFIG_PATH = "dpe_roles_config.yaml"
_DEFAULT_CONFIG_PATH_V2 = "agent_configs/meta_conversation.yaml"


def _meta_dir() -> Path:
    """Meta-conversation context store — resolved via the datadir authority
    (call-time, so test isolation of AITELIER_HOME is honored)."""
    from core import datadir
    return datadir.meta_dir()

# Default line window for read_code_file when no range is given. Large enough
# that typical source files are returned whole (so the agent isn't blind to the
# tail), with a `truncated` flag signalling when there's more to page through.
_MAX_READ_LINES = 2000
# Suffixes skipped by search_code (binary / compiled artifacts).
_BINARY_SUFFIXES = {".pyc", ".pyo", ".so", ".o", ".bin", ".png", ".jpg",
                    ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff",
                    ".woff2", ".ttf", ".eot"}


def usage_stats(totals: dict | None) -> dict:
    """Display stats derived from cumulative real-usage counters.

    billed_tokens uses the DeepSeek pricing shape: a cache hit bills at
    ~1/10th of a miss, completion tokens at full price. Empty dict when no
    usage has been recorded (provider sent none, or pre-telemetry session).
    """
    t = totals or {}
    prompt = t.get("prompt_tokens", 0)
    if not prompt:
        return {}
    hit = t.get("cache_hit_tokens", 0)
    billed = t.get("cache_miss_tokens", 0) + hit / 10 + t.get("completion_tokens", 0)
    return {"hit_ratio": round(hit / prompt, 4), "billed_tokens": int(billed)}

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
approval (approve_checkpoint) it lints, emits, and AUTO-REGISTERS the generated
pipeline under a name like `gen_<slug>` (reported as `registered_config`). Once
registered you can RUN it immediately with start_config_run(config_name=<that
gen_ name>, seed_text=<input for its first step>). To UPDATE a generated pipeline,
call generate_pipeline again with the SAME name — it overwrites in place, and the
next start_config_run uses the new version.
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
            "description": "List all projects with task stats (status counts). For a large "
                           "project list, prefer search_projects to avoid pulling everything.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_projects",
            "description": "Find projects by a substring of their id/name, with an optional "
                           "status filter. Returns only the matching projects (capped) — use "
                           "this instead of list_projects when the project list is large.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Substring to match against project id or name "
                                             "(case-insensitive). Omit to list by status only."},
                    "status": {"type": "string",
                               "description": "Optional exact status filter (e.g. planning, "
                                              "running, completed, failed)."},
                    "limit": {"type": "integer",
                              "description": "Max results (default 20, capped 100)."},
                },
                "required": [],
            },
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
                    "run_id": {"type": "string", "description": "The conversation run_id "
                               "(optional — resolved automatically from the active conversation "
                               "if omitted)"},
                    "answer": {"type": "string", "description": "The user's reply, verbatim"},
                },
                "required": ["answer"],
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
                    "run_id": {"type": "string", "description": "The conversation run_id "
                               "(optional — resolved automatically from the active conversation "
                               "if omitted)"},
                },
                "required": [],
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
                           "read_workspace_file — to read existing source files. "
                           "Large files are paged: pass start_line/end_line to read "
                           "a range; check the returned 'truncated'/'total_lines' to "
                           "page through the rest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Relative path within the repo (e.g. 'server.py')"},
                    "start_line": {"type": "integer",
                                   "description": "1-based first line to read (optional)"},
                    "end_line": {"type": "integer",
                                 "description": "1-based last line to read, inclusive (optional)"},
                },
                "required": ["project_id", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search file contents (grep) in a project's actual code "
                           "repository, via get_code_path. Returns matching "
                           "{file, line, text}. Use this to locate code by content "
                           "instead of reading whole files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "pattern": {"type": "string",
                                "description": "Regex (case-insensitive) or literal "
                                               "substring to search for"},
                    "glob": {"type": "string",
                             "description": "Optional filename glob filter (e.g. '*.py')"},
                    "max_results": {"type": "integer",
                                    "description": "Max matches to return (default 100)"},
                },
                "required": ["project_id", "pattern"],
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
                    "seed_inputs": {"type": "object",
                                    "description": "Optional {filename: content} map of extra seed files "
                                                   "for configs whose first step reads several inputs."},
                    "name": {"type": "string",
                             "description": "Optional human label for this run."},
                    "against_project": {"type": "string",
                                        "description": "Run against an EXISTING project's repo — resolves "
                                                       "repo_path + repo_type=existing from it. Use to offload "
                                                       "work onto the current project (e.g. coding_impl with an "
                                                       "approved plan as seed_text)."},
                    "repo_type": {"type": "string",
                                  "description": "new | existing | clone (default new). Prefer against_project for existing repos."},
                    "repo_path": {"type": "string",
                                  "description": "Local repo path when repo_type=existing."},
                    "repo_url": {"type": "string",
                                 "description": "Git URL when repo_type=clone."},
                },
                "required": ["config_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pipelines",
            "description": "PULL the full pipeline catalog with each config's input_hint (what "
                           "seed it expects) — a compact catalog is already in your system "
                           "context; call this to see input contracts before feeding a pipeline, "
                           "or to see freshly-generated gen_* ones. Returns name, description, "
                           "input_hint, and drive mode (background = start_config_run+poll; "
                           "inline = start_config_run drives it / or runner_start per its hint).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_until_next_checkpoint_or_completion",
            "description": "Block until a pipeline run reaches its next checkpoint, completes, "
                           "or fails, then return the compact state. This is the main way to "
                           "drive a pipeline you started (instead of polling get_pipeline_status "
                           "in a loop): the wait costs no extra turns. On a checkpoint, relay it "
                           "and call approve_checkpoint / reject_checkpoint. If it returns "
                           "status 'running' it timed out still in progress — call again or do "
                           "other work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to wait on."},
                    "timeout": {"type": "number",
                                "description": "Max seconds to wait before returning 'running' "
                                               "(default 120, capped 600). Only applies to "
                                               "scheduler-owned runs like DPE."},
                },
                "required": ["run_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pipeline_result",
            "description": "Get the compact final result of a COMPLETED run — the terminal "
                           "output step's file(s), with JSON parsed into structured data. Use "
                           "after a run completes to read what it produced.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID of the completed run."},
                },
                "required": ["run_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_pipeline",
            "description": "Cancel a running or paused pipeline run. The run is marked failed and "
                           "the scheduler stops advancing it. Use for a stuck or unwanted run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to stop."},
                    "reason": {"type": "string", "description": "Optional reason to record."},
                },
                "required": ["run_id"],
            },
        },
    },
]

# ── Coding-mode tool definitions ───────────────────────────────────
# Only sent to the LLM when the session is in coding mode (user-toggled;
# there is deliberately NO tool to switch modes — prompt injection cannot
# escalate a butler session into an unrestricted coding one).

CODING_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Surgically change an EXISTING file in the project repo by replacing "
                "an exact, unique snippet. 'old_str' must appear exactly once — include "
                "surrounding context to make it unique. The rest of the file is "
                "preserved verbatim. You MUST read the file (read_code_file) in this "
                "conversation before editing it. For multiple changes, call edit_file "
                "repeatedly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Repo-relative file path"},
                    "old_str": {"type": "string",
                                "description": "Exact text to find (must appear exactly once)"},
                    "new_str": {"type": "string",
                                "description": "Replacement text"},
                },
                "required": ["project_id", "path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a NEW file in the project repo. Fails if the file already "
                "exists — use edit_file to change an existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "path": {"type": "string",
                             "description": "Repo-relative file path"},
                    "content": {"type": "string"},
                },
                "required": ["project_id", "path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the project repo (cwd = repo root). Use for "
                "running tests, git operations, ls/grep, builds. Output is capped — "
                "prefer targeted commands over dumping large files (use read_code_file "
                "for that). Long-running commands are killed at 'timeout' seconds."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "command": {"type": "string"},
                    "timeout": {"type": "integer",
                                "description": "Seconds before the command is killed (default 120, max 600)"},
                },
                "required": ["project_id", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "runner_start",
            "description": (
                "Start a PLAN-GATED runner pipeline (default graph: coding_task). "
                "Use for any non-trivial code change: the engine walks you through "
                "plan → user approval → implement, and will NOT release the "
                "implement step until the user approves the plan. Returns the "
                "first step's instruction — do the work it describes, then "
                "runner_submit. Do not edit files while a plan awaits approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "task": {"type": "string",
                             "description": "The task, verbatim from the user plus any context you gathered"},
                    "graph_name": {"type": "string",
                                   "description": "Runner graph to drive (default coding_task)"},
                },
                "required": ["project_id", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "runner_submit",
            "description": (
                "Submit the current step's outputs and advance. Pass one result "
                "key per output slot from the instruction (e.g. result={\"plan\": "
                "\"<plan.md content>\"}), or omit result if you already wrote the "
                "outputs via skillflow_tool. validation_error in the response = "
                "fix and re-submit. status='paused' = relay the checkpoint to the "
                "user and WAIT for their decision."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "step_id": {"type": "string",
                                "description": "The step you are submitting (from the last response)"},
                    "result": {"type": "object",
                               "description": "One key per output slot (e.g. {\"plan\": \"...\"})"},
                },
                "required": ["run_id", "step_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "runner_approve",
            "description": (
                "Approve a paused runner checkpoint and receive the next step. "
                "Call ONLY after the user has explicitly approved in chat — the "
                "checkpoint is for the user, never auto-approve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                },
                "required": ["run_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "runner_reject",
            "description": (
                "Reject a paused runner checkpoint with the user's feedback — the "
                "step re-runs with that feedback (e.g. you write a revised plan). "
                "Use when the user asks for changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "feedback": {"type": "string",
                                 "description": "The user's requested changes, verbatim"},
                },
                "required": ["run_id", "feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skillflow_tool",
            "description": (
                "Execute one of the CURRENT runner step's skillflow tools — the "
                "write_<slot>/read_* names listed in the step instruction (e.g. "
                "skillflow_tool(name=\"write_plan\", params={\"content\": ...})). "
                "NOT for your own tools (edit_file, bash, ...) — call those "
                "directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "name": {"type": "string",
                             "description": "Skillflow tool name from the step instruction"},
                    "params": {"type": "object",
                               "description": "Tool parameters (e.g. {\"content\": \"...\"})"},
                },
                "required": ["run_id", "step_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web (SearXNG). Use for looking up library APIs, error "
                "messages, or current documentation you are not sure about. Returns "
                "titles, URLs and snippets — follow up with web_fetch to read a "
                "result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer",
                                    "description": "1-10, default 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL and return its readable text content. Results are "
                "windowed: a truncated response names 'next_offset' — pass it as "
                "'offset' to page through a long document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "offset": {"type": "integer",
                               "description": "Character offset to continue from (default 0)"},
                },
                "required": ["url"],
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


def _load_agent_role_config(role: str,
                            config_path: str = _DEFAULT_CONFIG_PATH_V2) -> dict:
    """Load one agent role block (e.g. 'compacter') from agent_configs."""
    try:
        base = Path(__file__).resolve().parent.parent
        path = base / config_path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            return config.get(role, {}) or {}
    except Exception:
        pass
    return {}


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

    def __init__(self, db, ws, owner_email: str = "cli@local", session_id: str = None,
                 mode: str = "butler", user_lang: str | None = None):
        self.db = db
        self.ws = ws
        self.owner_email = owner_email
        self.session_id = session_id
        # butler = orchestration/inspection toolset only; coding = adds
        # edit_file/create_file/bash. Set from the session (user-toggled),
        # never by the model.
        self.mode = mode if mode in ("butler", "coding") else "butler"
        self.user_lang = user_lang
        # Set per-turn in chat(); lets the approve/answer tools resolve the run
        # by project even when the session→run link is empty (drifted session).
        self._current_project = None
        # Files read via read_code_file this request — edit_file requires a
        # prior read so the model never splices into content it hasn't seen.
        # Per-request on purpose: after a budget-pause resume the model must
        # re-read before editing (files may have changed).
        self._files_read: set = set()
        # In-memory mirror of the session's cumulative real API usage
        # (sessions.usage_json). Loaded lazily at chat start, updated on every
        # accumulate so token_usage events can carry hit_ratio/billed_tokens.
        self._usage_totals: dict | None = None

        cfg = _load_meta_agent_config()
        raw_model = cfg.get("model", "deepseek/deepseek-v4-flash")
        self._raw_model = raw_model
        self.litellm_model, self.api_base, self.api_key = _resolve_provider(raw_model)
        self.enable_thinking = cfg.get("enable_thinking", False)
        self.thinking_effort = cfg.get("thinking_effort")
        self.max_tool_turns = cfg.get("max_tool_turns", 20)
        if self.mode == "coding":
            self.max_tool_turns = cfg.get("coding_max_tool_turns", 50)
        # Token window (the LLM's advertised context limit). Default 200k.
        # Compaction triggers at 70% of this window (self.compact_at_tokens).
        self.token_window = cfg.get("token_window", 200_000)
        self.compact_at_tokens = int(self.token_window * 0.7) if self.token_window else 0

        litellm.telemetry = False
        litellm.drop_params = True

    def _build_system_prompt(self, current_project: str | None) -> str:
        from core.prompt_assembler import build_language_instruction
        lang_block = build_language_instruction(self.user_lang)
        if self.mode == "coding":
            try:
                base = Path(__file__).resolve().parent.parent
                template = (base / "templates" / "coding_mode.md").read_text(
                    encoding="utf-8")
                # .replace, not .format — the template contains literal JSON
                # braces (tool-call examples) that str.format would choke on.
                prompt = (template
                        .replace("{current_project}", current_project or "none")
                        .replace("{owner_email}", self.owner_email)
                        .replace("{pipeline_catalog}", self._pipeline_catalog_block()))
                if lang_block:
                    prompt = lang_block + "\n\n" + prompt
                return prompt
            except Exception:
                pass  # missing template → fall back to butler prompt
        prompt = SYSTEM_PROMPT.format(
            current_project=current_project or "none",
            owner_email=self.owner_email,
        )
        if lang_block:
            prompt = lang_block + "\n\n" + prompt
        return prompt

    def _pipeline_catalog_block(self) -> str:
        """Compact pipeline catalog pushed into the coding-mode system context.

        Registry-generated, so gen_* and later-added configs appear with no
        prompt maintenance. One line each (name — description [drive mode]);
        the full input contract is pulled on demand via list_pipelines."""
        try:
            from api.dependencies import get_config_registry
            entries = get_config_registry().catalog(full=False)
        except Exception:
            return "(pipeline catalog unavailable — call list_pipelines)"
        if not entries:
            return "(no pipelines registered)"
        lines = []
        for e in entries:
            desc = (e.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 100:
                desc = desc[:100] + "…"
            lines.append(f"- `{e['config_name']}` [{e['drive']}] — {desc}")
        return "\n".join(lines)

    def _build_messages(self, history: list[dict], current_project: str | None) -> list[dict]:
        messages = [{"role": "system", "content": self._build_system_prompt(current_project)}]
        messages.extend(history)
        return messages

    def _active_meta_run(self, pid_hint: str | None = None) -> dict | None:
        """Locate the live (paused/running) meta_conversation run for this turn.

        Single source of truth for run_id resolution: the relay note AND the
        approve/answer tools both call this, so the model never has to search
        for the run_id. Resolution order:
          1. the session→run link (the fast common path), then
          2. a project-scoped lookup (``get_run_by_project``) keyed on an
             explicit ``pid_hint`` or the session's active project — this
             recovers a drifted session (e.g. a reload that minted a new
             session id) where the link points at a dead session.

        Returns ``{"run_id", "project_id", "gather_state"}`` or None.
        """
        from api.dependencies import get_skillflow
        from core.meta_run import read_gather_state, META_GRAPH
        sf = get_skillflow()

        def _wrap(rid: str, pid: str) -> dict:
            return {"run_id": rid, "project_id": pid,
                    "gather_state": read_gather_state(self.ws, pid) or {}}

        # 1. Session-linked live meta run.
        if self.session_id:
            try:
                for rid in self.db.get_runs_for_session(self.session_id) or []:
                    run = sf.get_run(rid)
                    if not run or run.get("graph_name") != META_GRAPH:
                        continue
                    if run.get("status") not in ("paused", "running"):
                        continue
                    return _wrap(rid, run.get("project_id", ""))
            except Exception:
                pass

        # 2. Project-scoped fallback (newest non-completed meta run, LIMIT 1 —
        #    cannot return multiples). Recovers a drifted/empty session link via
        #    an explicit hint or the current chat project (set in chat()), since
        #    _find_active_project is itself session-bound and would also miss.
        pid = pid_hint or self._current_project or self._find_active_project()
        if pid:
            try:
                run = sf.get_run_by_project(pid, META_GRAPH)
                if run and run.get("status") in ("paused", "running"):
                    return _wrap(run["id"], pid)
            except Exception:
                pass
        return None

    def _active_conversation_note(self) -> str | None:
        """If a meta_conversation run for this session is paused, return a system
        note telling the model exactly which tool to call. State-driven relay —
        this is what keeps the butler from re-deriving / re-starting a conversation."""
        active = self._active_meta_run()
        if not active:
            return None
        rid, pid, gs = active["run_id"], active["project_id"], active["gather_state"]
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

    async def chat(
        self,
        message: str,
        history: list[dict],
        current_project: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run the agent loop. Yields SSE events."""
        self._current_project = current_project
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
        # Cumulative tokens persisted across chat() calls (survives "continue"
        # and page reload). Load the session's running total, then add each
        # turn's context-window count to it.
        total_tokens = 0
        if self.session_id:
            try:
                total_tokens = self.db.get_session_total_tokens(self.session_id) or 0
            except Exception:
                pass
        if self.session_id and self._usage_totals is None:
            try:
                self._usage_totals = self.db.get_session_usage(self.session_id)
            except Exception:
                pass
        try:
            while tool_turns < self.max_tool_turns:
                compacted = await self._maybe_compact(messages)
                if compacted is not messages:
                    messages = compacted
                    yield {"type": "compaction",
                           "message": "Older turns were summarized to stay within context."}

                turn_tokens = self._count_tokens(messages)
                total_tokens += turn_tokens
                self._persist_token_counts(turn_tokens, total_tokens)
                limit = self.token_window if self.mode == "coding" else 0
                yield {"type": "token_usage",
                       "tokens": turn_tokens,
                       "total_tokens": total_tokens,
                       "limit": limit,
                       "mode": self.mode,
                       **usage_stats(self._usage_totals)}

                full_text = ""
                tool_calls = []

                try:
                    async for event in self._stream_llm(messages):
                        if event["_type"] == "text_delta":
                            yield {"type": "text_delta", "content": event["content"]}
                        elif event["_type"] == "collected":
                            full_text = event["text"]
                            tool_calls = event["tool_calls"]
                            self._persist_usage(event.get("usage"))
                except Exception as e:
                    # Transient LLM/streaming failure (provider 5xx, rate-limit,
                    # connection dropped mid-stream). Handshake retries inside
                    # _stream_llm are already exhausted. Don't kill the session
                    # with a terminal error: the assistant turn hasn't been
                    # appended/persisted yet, so the turn is atomic — the user's
                    # message is already saved, and "continue" rebuilds the
                    # transcript and resumes cleanly. Emit a RESUMABLE pause.
                    self._log_error(f"LLM stream failed: {e}")
                    yield {
                        "type": "llm_interrupted",
                        "tool_turns": tool_turns,
                        "message": (
                            f"The model connection was interrupted "
                            f"({type(e).__name__}). Reply 'continue' to resume."
                        ),
                    }
                    return

                if not tool_calls:
                    # Final token usage (messages unchanged since we counted)
                    self._persist_token_counts(turn_tokens, total_tokens)
                    yield {"type": "token_usage",
                           "tokens": turn_tokens,
                           "total_tokens": total_tokens,
                           "limit": limit,
                           "mode": self.mode,
                           **usage_stats(self._usage_totals)}
                    yield {"type": "done", "message": {"role": "assistant", "content": full_text}}
                    return

                # Append the assistant message with tool_calls, run its tools,
                # and persist the assistant + ALL its results as ONE atomic group.
                # The persist happens in a `finally` so it runs even when a client
                # disconnect raises CancelledError/GeneratorExit at a yield or in a
                # mid-tool await. Any call cut off before producing a result is
                # completed with a synthetic "interrupted" result, so a persisted
                # assistant is NEVER left with a dangling tool_call (the cause of
                # the "tool_call_ids did not have response messages" corruption).
                # _persist_transcript is a synchronous DB write — safe to run
                # during generator unwinding (no yield/await in the finally).
                assistant_msg = self._build_assistant_msg(full_text, tool_calls)
                messages.append(assistant_msg)
                tool_msgs: list[dict] = []
                turn_errors = 0
                try:
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
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "tool_name": tc["name"],
                            "tool_args": tc["args"],
                            "content": json.dumps(result, default=str, ensure_ascii=False),
                        }
                        # Capture the result BEFORE the yield, so a disconnect at
                        # the emit can't lose it — the finally still persists it.
                        tool_msgs.append(tool_msg)
                        messages.append(tool_msg)
                        yield {"type": "tool_result", "name": tc["name"], "result": result}
                finally:
                    # Complete the group: a synthetic result for any call cut off
                    # (disconnect) before it produced one, so the persisted group
                    # is always assistant + exactly one result per tool_call.
                    answered = {m["tool_call_id"] for m in tool_msgs}
                    for tc in tool_calls:
                        if tc["id"] not in answered:
                            tool_msgs.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "tool_name": tc["name"],
                                "tool_args": tc["args"],
                                "content": json.dumps(
                                    {"error": "interrupted — stream disconnected "
                                              "before this tool completed"}),
                            })
                    # Atomic persist: assistant first, then every result in order.
                    self._persist_transcript(assistant_msg)
                    for tm in tool_msgs:
                        self._persist_transcript(tm)

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

            # Budget pause, not a failure: in coding mode the transcript is
            # persisted incrementally, so the user can reply "continue" and a
            # fresh chat() resumes from the rebuilt transcript with full state.
            yield {
                "type": "budget_exhausted",
                "tool_turns": tool_turns,
                "message": (
                    f"Tool-turn budget ({self.max_tool_turns}) reached. "
                    f"Reply 'continue' to keep going."
                ),
            }

        except Exception as e:
            yield {"type": "error", "message": f"Agent error: {e}"}

    def _persist_transcript(self, message: dict) -> None:
        """Persist one loop message (coding mode only, best-effort).

        Butler mode keeps the legacy narrative-only persistence; coding mode
        needs the full tool transcript so a budget-paused or interrupted
        session can resume with its working state intact. The row id is
        attached back onto the in-memory message so the condenser can record
        an exact compaction watermark.
        """
        if self.mode != "coding" or not self.session_id:
            return
        try:
            rid = self.db.save_chat_transcript_message(
                self.session_id, self._current_project or "", message)
            if rid:
                message["_row_id"] = rid
        except Exception as e:
            self._log_error(f"transcript persistence failed: {e}")

    def _persist_usage(self, usage: dict | None) -> None:
        """Accumulate one LLM call's real API usage onto the session (best-effort).

        Unlike _persist_token_counts (estimated context-window size), these are
        the provider-reported counters (prompt/completion/cache hit+miss), so a
        session's billed-equivalent cost is comparable with DPE skillflow_trace.
        """
        if not self.session_id or not usage:
            return
        try:
            self._usage_totals = self.db.accumulate_session_usage(
                self.session_id, usage)
        except Exception as e:
            self._log_error(f"usage persistence failed: {e}")

    def _persist_token_counts(self, turn_tokens: int, total_tokens: int) -> None:
        """Persist both per-turn window and cumulative counter (best-effort).

        Mode-agnostic: the cumulative counter must survive across turns in
        butler mode too, otherwise "cumulated" resets to 0 on every user
        message (it was previously coding-only, so butler never accrued)."""
        if not self.session_id:
            return
        try:
            self.db.set_session_token_window(self.session_id, turn_tokens)
            self.db.set_session_total_tokens(self.session_id, total_tokens)
        except Exception as e:
            self._log_error(f"token persistence failed: {e}")

    # ── Transcript condenser (coding mode) ─────────────────────────
    # When the assembled context crosses 70% of token_window, the oldest
    # ~60% of the conversation is summarized by the `compacter` agent
    # (one-shot, config-resolved model — never hardcoded) and replaced by
    # a pinned summary. The summary is persisted with a `compaction_through`
    # watermark so a resumed session rebuilds compacted instead of
    # re-summarizing.

    _COMPACT_KEEP_TAIL = 8  # most-recent messages always kept verbatim

    @staticmethod
    def _clean_msgs(messages: list[dict]) -> list[dict]:
        """Strip loop-internal bookkeeping keys before hitting the provider."""
        allowed = {"role", "content", "tool_calls", "tool_call_id", "name"}
        return [{k: v for k, v in m.items() if k in allowed} for m in messages]

    def _count_tokens(self, messages: list[dict]) -> int:
        clean = self._clean_msgs(messages)
        try:
            return litellm.token_counter(model=self.litellm_model, messages=clean)
        except Exception:
            # crude fallback: ~4 chars/token
            return sum(len(json.dumps(m, ensure_ascii=False, default=str))
                       for m in clean) // 4

    @staticmethod
    def _serialize_for_compaction(chunk: list[dict], max_chars: int = 60000) -> str:
        parts = []
        for m in chunk:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            if len(content) > 2000:
                content = content[:2000] + " …[truncated]"
            line = f"[{role}] {content}".rstrip()
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {})
                args = fn.get("arguments", "")
                if len(args) > 500:
                    args = args[:500] + " …[truncated]"
                line += f"\n  → {fn.get('name', '?')}({args})"
            parts.append(line)
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…[chunk truncated]"
        return text

    async def _summarize_chunk(self, chunk_text: str) -> str | None:
        cfg = _load_agent_role_config("compacter")
        raw_model = cfg.get("model", "deepseek/deepseek-v4-flash")
        model, api_base, api_key = _resolve_provider(raw_model)
        try:
            base = Path(__file__).resolve().parent.parent
            system = (base / "templates" / cfg.get("template", "compaction.md")
                      ).read_text(encoding="utf-8")
        except Exception as e:
            self._log_error(f"compaction template missing: {e}")
            return None
        kwargs = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": chunk_text}],
            "temperature": 0.2,
            "stream": False,
        }
        if api_base:
            kwargs["api_base"] = api_base
        if api_key:
            kwargs["api_key"] = api_key
        try:
            resp = await litellm.acompletion(**kwargs)
            self._persist_usage(AIGateway._extract_usage(resp))
            content = resp.choices[0].message.content
            return content.strip() if content else None
        except Exception as e:
            self._log_error(f"compaction LLM call failed: {e}")
            return None

    async def _maybe_compact(self, messages: list[dict]) -> list[dict]:
        """Summarize the oldest turns when the context crosses the threshold.

        Returns the original list (identity) when nothing was done, or a new
        list [system, summary, ...kept tail]. Never splits an assistant
        tool_calls group. Failure of any kind leaves the messages untouched —
        the turn proceeds uncompacted rather than dying.
        """
        if (self.mode != "coding" or not self.compact_at_tokens
                or len(messages) < 2):
            return messages
        if self._count_tokens(messages) < self.compact_at_tokens:
            return messages

        body = messages[1:]  # messages[0] is the system prompt
        cut = int(len(body) * 0.6)
        cut = min(cut, len(body) - self._COMPACT_KEEP_TAIL)
        if cut < 1:
            return messages
        # Don't orphan tool results: extend past any trailing tool messages.
        while cut < len(body) and body[cut].get("role") == "tool":
            cut += 1
        if cut >= len(body):
            return messages
        chunk = body[:cut]

        summary = await self._summarize_chunk(
            self._serialize_for_compaction(chunk))
        if not summary:
            return messages

        summary_msg = {"role": "system", "content": summary}
        through = max((m.get("_row_id") or 0) for m in chunk)
        if through and self.session_id:
            try:
                rid = self.db.save_chat_transcript_message(
                    self.session_id, self._current_project or "",
                    {"role": "system", "content": summary,
                     "compaction_through": through})
                if rid:
                    summary_msg["_row_id"] = rid
                summary_msg["compaction_through"] = through
            except Exception as e:
                self._log_error(f"compaction persistence failed: {e}")
        return [messages[0], summary_msg] + body[cut:]

    async def _stream_llm(self, messages: list[dict]) -> AsyncGenerator[dict, None]:
        """Stream LLM response. Yields text_delta events in real-time,
        then a single 'collected' event with the full text and parsed tool_calls."""
        tools = TOOL_DEFINITIONS
        if self.mode == "coding":
            tools = TOOL_DEFINITIONS + CODING_TOOL_DEFINITIONS
        kwargs = {
            "model": self.litellm_model,
            # strip loop-internal keys (_row_id, compaction_through) — some
            # providers reject unknown message fields
            "messages": self._clean_msgs(messages),
            "tools": tools,
            "stream": True,
            # Real usage telemetry: the provider attaches a usage payload to
            # the final stream chunk (empty choices) when asked.
            "stream_options": {"include_usage": True},
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

        # Retry only the handshake: transient provider errors (rate-limit,
        # 5xx, connection at request time) surface here before any token has
        # been yielded, so re-establishing the stream is safe and cannot
        # duplicate already-streamed text. A drop *during* iteration bubbles up
        # to the chat() loop, which turns it into a resumable pause. Mirrors the
        # AIGateway tenacity policy (the meta agent bypasses that gateway).
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
            reraise=True,
        )
        async def _open_stream():
            return await litellm.acompletion(**kwargs)

        response = await _open_stream()

        full_text = ""
        tool_calls_map: dict[int, dict] = {}
        usage: dict = {}

        async for chunk in response:
            if getattr(chunk, "usage", None):
                usage = AIGateway._extract_usage(chunk) or usage
            if not chunk.choices:
                continue  # usage-only final chunk
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

        yield {"_type": "collected", "text": full_text, "tool_calls": tool_calls,
               "usage": usage}

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
        # Defense in depth: coding tools are only schemas-visible in coding
        # mode, but reject them here too in case a model hallucinates the call.
        if name in _CODING_TOOL_HANDLERS and self.mode != "coding":
            return {"error": f"Tool '{name}' is only available in coding mode."}
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

    def _tool_search_projects(self, args: dict) -> dict:
        """Filter the project list by a substring query (id/name) + optional status.

        For a growing project list, prefer this over list_projects so only the
        relevant handful enters the transcript."""
        query = (args.get("query") or "").strip().lower()
        status = (args.get("status") or "").strip().lower()
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))

        def matches(p: dict) -> bool:
            if status and (p.get("status") or "").lower() != status:
                return False
            if not query:
                return True
            hay = f"{p.get('project_id', '')} {p.get('name', '')}".lower()
            return query in hay

        hits = [p for p in self.db.list_projects_with_stats(owner_email=None)
                if matches(p)]
        total = len(hits)
        return {"projects": hits[:limit], "total_matches": total,
                "truncated": total > limit}

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
        from core.run_launcher import slugify
        return slugify(text, sep="-", maxlen=40, fallback="project")

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
            from core import datadir
            default = datadir.projects_dir() / existing_pid
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

        answer = (args.get("answer") or "").strip()
        sf = get_skillflow()
        # Self-resolve the run: the model may omit/lose the run_id (it lives in a
        # transient relay note). Fall back to the active meta run for this turn so
        # the agent never has to search for it.
        run_id = args.get("run_id")
        run = sf.get_run(run_id) if run_id else None
        if not run:
            active = self._active_meta_run(pid_hint=args.get("project_id"))
            if active:
                run_id = active["run_id"]
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

        sf = get_skillflow()
        # Self-resolve the run: the model may omit/lose the run_id (it lives in a
        # transient relay note). Fall back to the active meta run for this turn so
        # approval never has to search for the run_id.
        run_id = args.get("run_id")
        run = sf.get_run(run_id) if run_id else None
        if not run:
            active = self._active_meta_run(pid_hint=args.get("project_id"))
            if active:
                run_id = active["run_id"]
                run = sf.get_run(run_id)
        if not run:
            return {"status": "error",
                    "message": f"Conversation '{run_id}' not found — "
                               "the conversation may have already ended. "
                               "Check the project list."}
        pid = run.get("project_id", "")

        # AT-1: If the run is already completed (its finalize step already ran),
        # skip approve_meta — the artifacts have been emitted.
        run_already_completed = run.get("status") == "completed"

        gs = read_gather_state(self.ws, pid) or {}
        brief = gs.get("brief") or {}
        stories = brief.get("user_stories")
        if not (isinstance(stories, list) and any(str(s).strip() for s in stories)):
            return {"status": "error",
                    "message": "The brief has no user stories yet — the conversation "
                               "has not produced a complete brief. Keep talking to "
                               "add requirements before approving."}

        # Drive the meta run through its finalize tool step — the SOLE producer of
        # the project artifacts (project_brief.md, spec.md, step1_goals.json). If
        # it fails to emit, do NOT start DPE (it would run brief-less); surface the
        # error so the user can retry. Only trigger DPE once finalize has emitted.
        if run_already_completed:
            self._log_error(f"Meta run {run_id} was already completed when approving brief — "
                           "this is expected if the checkpoint was approved via the CLI")
        else:
            try:
                approve_meta(sf, run_id)
            except Exception as e:
                self._log_error(f"approve_meta failed for {run_id}: {e}")
                return {"status": "error", "project_id": pid,
                        "message": "I couldn't finalize the brief — the requirements "
                                   "artifacts weren't produced, so I did not start the "
                                   "build. Please try approving again or adjust the brief."}

        res = seed_and_trigger(self.db, self.ws, pid, brief)
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
        """Read a file from a project's actual code repository.

        Supports line-range paging via ``start_line``/``end_line`` (1-based,
        inclusive) so large files are not silently truncated. When no range is
        given, returns up to ``_MAX_READ_LINES`` from the top. Always reports
        ``total_lines`` and a ``truncated`` flag so the agent knows when there
        is more to page through.
        """
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
        lines = content.splitlines()
        total = len(lines)
        start = args.get("start_line")
        end = args.get("end_line")
        start_idx = (start - 1) if isinstance(start, int) and start > 0 else 0
        start_idx = min(start_idx, total)
        if isinstance(end, int) and end > 0:
            end_idx = min(end, total)
        else:
            end_idx = min(start_idx + _MAX_READ_LINES, total)
        selected = lines[start_idx:end_idx]
        body = "\n".join(
            f"{start_idx + i + 1}\t{ln}" for i, ln in enumerate(selected)
        )
        # Register the read so edit_file's read-before-edit guard passes.
        self._files_read.add((pid, str(target)))
        return {
            "path": path,
            "content": body,
            "start_line": start_idx + 1,
            "end_line": end_idx,
            "total_lines": total,
            "truncated": end_idx < total,
        }

    # ── Coding-mode tools ──────────────────────────────────────────
    # Direct in-place repo editing for the interactive coding agent. The
    # surgical-edit safety core (_unique_replace) is shared with skillflow so
    # the uniqueness rule lives in one place; path handling is butler-own
    # (same jail as read_code_file — no staging dir, no AT-9 'project/' strip,
    # which would mangle repos that really have a project/ directory).

    def _resolve_code_target(self, pid: str, path: str):
        """Resolve a repo-relative path inside the project's code jail.

        Returns (base, target, None) or (None, None, error_dict).
        """
        try:
            base = self.ws.get_code_path(pid).resolve()
        except Exception as e:
            return None, None, {"error": f"Cannot resolve code path for '{pid}': {e}"}
        if not base.is_dir():
            return None, None, {"error": f"No code directory for project '{pid}'"}
        target = (base / path).resolve()
        if not str(target).startswith(str(base)):
            return None, None, {"error": "Path traversal denied"}
        return base, target, None

    def _tool_edit_file(self, args: dict) -> dict:
        from skillflow.write_tools import _unique_replace

        pid = args["project_id"]
        path = args["path"]
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        if not isinstance(old_str, str) or not old_str:
            return {"error": "edit_file: 'old_str' is required and must be non-empty"}
        base, target, err = self._resolve_code_target(pid, path)
        if err:
            return err
        if not target.is_file():
            return {"error": f"edit_file: '{path}' does not exist — use create_file for new files"}
        if (pid, str(target)) not in self._files_read:
            return {"error": (f"edit_file: read '{path}' with read_code_file before "
                              f"editing it — you must see the current content first.")}
        content = target.read_text(encoding="utf-8")
        updated, uerr = _unique_replace(content, old_str, new_str,
                                        tool="edit_file", name=path)
        if uerr:
            return uerr
        target.write_text(updated, encoding="utf-8")
        return {"edited": path}

    def _tool_create_file(self, args: dict) -> dict:
        pid = args["project_id"]
        path = args["path"]
        base, target, err = self._resolve_code_target(pid, path)
        if err:
            return err
        if target.exists():
            return {"error": (f"create_file: '{path}' already exists — use edit_file "
                              f"to change an existing file.")}
        content = args.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        # A file we just authored is by definition "seen" — allow edits.
        self._files_read.add((pid, str(target)))
        return {"created": path, "size": len(content)}

    # Output cap for bash results: head + tail, so both the command banner and
    # the (usually decisive) final lines survive in context.
    _BASH_HEAD_CHARS = 8000
    _BASH_TAIL_CHARS = 2000
    # env vars matching this never reach coding-mode subprocesses. Anchored to
    # the END of the name: secret conventions are suffixes (DEEPSEEK_API_KEY,
    # GITHUB_TOKEN, MY_PASSWORD) — an unanchored _KEY also killed
    # GIT_CONFIG_KEY_0 (the compose-wired credential helper) while leaving
    # GIT_CONFIG_COUNT, which broke every git command in the container.
    _ENV_SECRET_RE = re.compile(
        r"(_KEY|_TOKEN|_SECRET|_SECRETS|PASSWORD|_CREDENTIAL|_CREDENTIALS)$", re.I)

    async def _tool_bash(self, args: dict) -> dict:
        pid = args["project_id"]
        command = args.get("command", "")
        if not command.strip():
            return {"error": "bash: 'command' is required"}
        timeout = args.get("timeout") or 120
        timeout = max(1, min(int(timeout), 600))
        try:
            base = self.ws.get_code_path(pid).resolve()
        except Exception as e:
            return {"error": f"Cannot resolve code path for '{pid}': {e}"}
        if not base.is_dir():
            return {"error": f"No code directory for project '{pid}'"}

        env = {k: v for k, v in os.environ.items()
               if not self._ENV_SECRET_RE.search(k)}

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(base),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"error": f"bash: command timed out after {timeout}s", "command": command}

        output = out_bytes.decode("utf-8", errors="replace")
        truncated = False
        limit = self._BASH_HEAD_CHARS + self._BASH_TAIL_CHARS
        if len(output) > limit:
            output = (output[:self._BASH_HEAD_CHARS]
                      + f"\n... [{len(output) - limit} chars truncated] ...\n"
                      + output[-self._BASH_TAIL_CHARS:])
            truncated = True
        return {
            "exit_code": proc.returncode,
            "output": output,
            "truncated": truncated,
        }

    # ── Runner-mode pipelines (plan-gated coding_task & friends) ───
    # The butler drives runner-mode graphs through skillflow's RunnerService —
    # the same transport-neutral core that skillflow-mcp serves to external
    # agents (Claude Code, opencode, ...), so feature capacity is identical by
    # construction. The engine owns transitions and checkpoints (a paused run
    # will not release its next step until approval); the butler does each
    # step's work with its own context and tools. This layer adds only host
    # glue: project validation, seed-file resolution from the config manifest,
    # session↔run linking, and mode gating.

    _DEFAULT_RUNNER_GRAPH = "coding_task"

    def _runner_service(self):
        from api.dependencies import get_skillflow
        from skillflow.plugins.skill_runner import RunnerService
        return RunnerService(get_skillflow())

    def _tool_runner_start(self, args: dict) -> dict:
        pid = args["project_id"]
        task = (args.get("task") or "").strip()
        graph = (args.get("graph_name") or self._DEFAULT_RUNNER_GRAPH).strip()
        if not task:
            return {"error": "runner_start: 'task' is required"}
        if not self.db.get_project(pid):
            return {"error": f"Project '{pid}' not found — use list_projects, or "
                             f"create_project for a new one."}
        # Seed filename comes from the config manifest (x-aitelier.seed_file).
        seed_file = "task.md"
        try:
            from api.dependencies import get_config_registry
            manifest = get_config_registry().get(graph)
            if manifest and manifest.seed_file:
                seed_file = manifest.seed_file
        except Exception:
            pass
        result = self._runner_service().start(
            graph, project_id=pid, seeds={seed_file: task})
        if result.get("run_id") and self.session_id:
            try:
                self.db.link_run_to_session(self.session_id, result["run_id"])
            except Exception:
                pass
        return result

    def _tool_runner_submit(self, args: dict) -> dict:
        run_id = args.get("run_id", "")
        step_id = args.get("step_id", "")
        if not run_id or not step_id:
            return {"error": "runner_submit: 'run_id' and 'step_id' are required"}
        result = args.get("result")
        if result is not None and not isinstance(result, dict):
            return {"error": "runner_submit: 'result' must be an object with one "
                             "key per output slot (or omitted if outputs were "
                             "written via skillflow_tool)"}
        return self._runner_service().submit(run_id, step_id, result)

    def _tool_runner_approve(self, args: dict) -> dict:
        run_id = args.get("run_id", "")
        if not run_id:
            return {"error": "runner_approve: 'run_id' is required"}
        return self._runner_service().approve(run_id)

    def _tool_runner_reject(self, args: dict) -> dict:
        run_id = args.get("run_id", "")
        feedback = (args.get("feedback") or "").strip()
        if not run_id or not feedback:
            return {"error": "runner_reject: 'run_id' and 'feedback' are required"}
        return self._runner_service().reject(run_id, feedback)

    def _tool_skillflow_tool(self, args: dict) -> dict:
        run_id = args.get("run_id", "")
        step_id = args.get("step_id", "")
        name = (args.get("name") or "").strip()
        if not run_id or not step_id or not name:
            return {"error": "skillflow_tool: 'run_id', 'step_id' and 'name' "
                             "are required"}
        # Read/exploration tools receive the project's code repo as their root.
        project_root = ""
        try:
            from api.dependencies import get_skillflow
            pid = get_skillflow()._get_project_id(run_id)
            if pid:
                project_root = str(self.ws.get_code_path(pid))
        except Exception:
            pass
        return self._runner_service().execute_step_tool(
            run_id, step_id, name, args.get("params") or {},
            project_root=project_root)

    async def _tool_web_search(self, args: dict) -> dict:
        """Web search via core.web_tools (SearXNG). Sync httpx under the hood —
        run in a thread so a slow backend doesn't block the event loop."""
        from core.web_tools import WebSearchTool
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "web_search: 'query' is required"}
        try:
            max_results = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        return await asyncio.to_thread(
            WebSearchTool().search, query, max_results)

    async def _tool_web_fetch(self, args: dict) -> dict:
        """Fetch a URL's readable text via core.web_tools (SSRF-guarded,
        offset-paged). Threaded for the same reason as web_search."""
        from core.web_tools import WebFetchTool
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "web_fetch: 'url' is required"}
        try:
            offset = int(args.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        return await asyncio.to_thread(
            lambda: WebFetchTool().fetch(url, offset=offset))

    def _tool_search_code(self, args: dict) -> dict:
        """Grep file contents in a project's code repository (jailed to it).

        Returns matching ``{file, line, text}`` entries. ``pattern`` is treated
        as a regex (case-insensitive), falling back to a literal substring if it
        is not a valid regex. ``glob`` optionally filters filenames (e.g.
        ``*.py``); ``max_results`` caps matches (default 100).
        """
        pid = args["project_id"]
        pattern = args["pattern"]
        glob = args.get("glob")
        max_results = args.get("max_results") or 100
        base = self.ws.get_code_path(pid).resolve()
        if not base.exists():
            return {"error": f"Code repo not found for {pid}"}
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = None  # not a valid regex → literal substring match
        matches = []
        truncated = False
        for item in sorted(base.rglob(glob) if glob else base.rglob("*")):
            if not item.is_file() or "/.git/" in f"/{item}":
                continue
            if item.suffix.lower() in _BINARY_SUFFIXES:
                continue
            try:
                text = item.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for li, line in enumerate(text.splitlines(), 1):
                hit = regex.search(line) if regex else (pattern.lower() in line.lower())
                if hit:
                    matches.append({
                        "file": str(item.relative_to(base)),
                        "line": li,
                        "text": line.strip()[:200],
                    })
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        return {"project_id": pid, "matches": matches, "truncated": truncated}

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
        meta_dir = _meta_dir()
        meta_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(meta_dir.glob(f"{pid}_context_*.json"), reverse=True)
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
                                            run.get("project_id", ""), s["step_id"],
                                            run.get("graph_name") or "dpe_default",
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
                                    # graph-scoped: config runs (code_review,
                                    # skill_converter, gen_*) promote steps under
                                    # {pid}/{graph}/{step}; the DPE default only
                                    # matched DPE runs, so outputs came back {}.
                                    out_dir = self.ws.get_final_path(
                                        run.get("project_id", ""), s["step_id"],
                                        run.get("graph_name") or "dpe_default",
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
                        result = {
                            "status": "completed",
                            "run_id": run_id,
                            "project_id": run.get("project_id", ""),
                            "steps_completed": len([s for s in steps if s["status"] == "completed"]),
                            "outputs": outputs,
                            "message": "Pipeline completed successfully.",
                        }
                        # Configs flagged registers_generated_pipeline (skill_converter)
                        # produce a pipeline YAML on completion — persist + live-register
                        # it (gen_<slug>) so the user can run it immediately via
                        # start_config_run, and re-running with the same name updates it.
                        # Manifest-driven so this generic waiter doesn't special-case a
                        # graph name.
                        from api.dependencies import get_config_registry
                        _mf = get_config_registry().get(run.get("graph_name", ""))
                        if _mf and _mf.registers_generated_pipeline:
                            try:
                                from api.dependencies import register_pipeline_from_run
                                proj = self.db.get_project(run.get("project_id", "")) or {}
                                pname = proj.get("name") or run.get("project_id", "")
                                reg = register_pipeline_from_run(run_id, pname)
                                if reg.get("config_name"):
                                    result["registered_config"] = reg["config_name"]
                                    result["registered_action"] = reg.get("action")
                                    result["message"] = (
                                        f"Pipeline '{reg['config_name']}' is registered and "
                                        f"ready ({reg.get('action')}). Start it with "
                                        f"start_config_run(config_name='{reg['config_name']}')."
                                    )
                                elif reg.get("error"):
                                    result["registration_error"] = reg["error"]
                            except Exception as e:
                                result["registration_error"] = str(e)
                        return result
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

    # ── Layer-3 generic pipeline toolset ───────────────────────────────
    # Drive/inspect ANY registered skillflow config (dpe, code_review,
    # coding_task, gen_*, …) so the butler can offload context-heavy work to an
    # isolated run and see only compact status + checkpoints. See
    # design/context_offload_delegation.md.

    def _read_final_dir(self, project_id: str, step_id: str, graph: str,
                        cap: int = 2000) -> dict:
        """Read a completed step's promoted output dir → {relpath: content[:cap]}."""
        files: dict = {}
        try:
            out_dir = self.ws.get_final_path(project_id, step_id, graph or "dpe_default")
            if out_dir.exists():
                for f in sorted(out_dir.rglob("*")):
                    if f.is_file() and f.name != "_snapshot.json":
                        try:
                            files[str(f.relative_to(out_dir))] = \
                                f.read_text(encoding="utf-8", errors="replace")[:cap]
                        except Exception:
                            pass
        except Exception:
            pass
        return files

    def _summarize_run_state(self, run_id: str) -> dict:
        """Compact summary of a run at a terminal/paused state.

        Mirrors the terminal handling in _run_pipeline_until_checkpoint (minus the
        generated-pipeline registration side effect) so the awaited path
        (wait_until_next_checkpoint_or_completion for scheduler-owned runs) returns
        the identical shape as the inline-driven path.
        """
        from api.dependencies import get_skillflow
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"status": "error", "run_id": run_id, "message": "Run not found"}
        status = run["status"]
        graph = run.get("graph_name") or "dpe_default"
        pid = run.get("project_id", "")
        steps = sf.get_steps(run_id)

        if status == "paused":
            label = run.get("current_node", "Checkpoint")
            checkpoint_step_id = ""
            checkpoint_data = None
            try:
                resolver = sf._get_resolver(run["graph_name"])
            except Exception:
                resolver = None
            for s in reversed(steps):
                if s["status"] == "completed" and resolver:
                    node = resolver.get_node(s["step_id"])
                    if node and node.checkpoint:
                        checkpoint_step_id = s["step_id"]
                        label = node.checkpoint_label or s["step_id"]
                        checkpoint_data = self._read_final_dir(
                            pid, s["step_id"], graph, cap=3000) or None
                        break
            return {
                "status": "checkpoint",
                "run_id": run_id,
                "project_id": pid,
                "step_id": checkpoint_step_id,
                "label": label,
                "data": checkpoint_data,
                "message": f"Pipeline paused at checkpoint: {label}. Wait for user "
                           f"approval before calling approve_checkpoint or reject_checkpoint.",
            }
        if status == "completed":
            outputs = {}
            for s in steps:
                if s["status"] == "completed":
                    files = self._read_final_dir(pid, s["step_id"], graph, cap=2000)
                    if files:
                        outputs[s["step_id"]] = files
            return {
                "status": "completed",
                "run_id": run_id,
                "project_id": pid,
                "steps_completed": len([s for s in steps if s["status"] == "completed"]),
                "outputs": outputs,
                "message": "Pipeline completed successfully.",
            }
        if status == "failed":
            return {
                "status": "failed",
                "run_id": run_id,
                "project_id": pid,
                "error": run.get("error_reason", "Unknown error"),
                "message": f"Pipeline failed: {run.get('error_reason', 'Unknown error')[:200]}",
            }
        return {"status": status, "run_id": run_id, "project_id": pid,
                "message": f"Pipeline in state: {status}"}

    def _tool_list_pipelines(self, args: dict) -> dict:
        """Full pipeline catalog with input contracts (the on-demand PULL).

        A compact version is already pushed into your system context; call this
        when you need each pipeline's input_hint (what seed it expects) to
        choose and feed one, or to see freshly-generated (gen_*) pipelines.
        Drive a 'background' pipeline with start_config_run then poll; a
        plan-gated one via runner_start (see its input_hint)."""
        from api.dependencies import get_config_registry
        catalog = get_config_registry().catalog(full=True)
        return {"pipelines": catalog, "count": len(catalog)}

    def _tool_stop_pipeline(self, args: dict) -> dict:
        """Cancel a running pipeline (marks the run failed; the poller then skips it)."""
        from api.dependencies import get_skillflow
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "run_id is required."}
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}
        if run["status"] in ("completed", "failed"):
            return {"status": run["status"], "run_id": run_id,
                    "message": f"Run already {run['status']}; nothing to stop."}
        try:
            sf.fail_run(run_id, args.get("reason") or "stopped via butler")
        except Exception as e:
            return {"error": f"Failed to stop run: {e}"}
        return {"status": "stopped", "run_id": run_id, "message": "Pipeline stopped."}

    def _tool_get_pipeline_result(self, args: dict) -> dict:
        """Compact terminal result of a finished run (parsed JSON where possible)."""
        from api.dependencies import get_skillflow, get_config_registry
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "run_id is required."}
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}
        status = run["status"]
        if status != "completed":
            return {"status": status, "run_id": run_id,
                    "message": f"Run is {status}, not completed — no final result yet."}
        graph = run.get("graph_name") or "dpe_default"
        pid = run.get("project_id", "")
        manifest = get_config_registry().get(graph)
        output_step = getattr(manifest, "output_step", None) if manifest else None
        steps = sf.get_steps(run_id)
        completed = [s["step_id"] for s in steps if s["status"] == "completed"]
        target_steps = ([output_step] if output_step and output_step in completed
                        else completed)
        result = {}
        for sid in target_steps:
            for rel, content in self._read_final_dir(pid, sid, graph, cap=8000).items():
                # Parse JSON payloads so the driver gets structured data, not text.
                try:
                    result[rel] = json.loads(content)
                except (ValueError, TypeError):
                    result[rel] = content
        return {"status": "completed", "run_id": run_id, "project_id": pid,
                "output_step": output_step, "result": result}

    async def _tool_wait_until_checkpoint(self, args: dict) -> dict:
        """Block until the run hits its next checkpoint / completes / fails.

        Butler-driven configs are driven inline; scheduler-owned configs are
        advanced by the poller and this awaits (bounded by `timeout`, default
        120s) — returning {status:"running"} if still going so the driver never
        hangs. The internal wait costs zero driver turns / context.
        """
        from api.dependencies import get_skillflow, get_config_registry
        run_id = (args.get("run_id") or "").strip()
        if not run_id:
            return {"error": "run_id is required."}
        sf = get_skillflow()
        run = sf.get_run(run_id)
        if not run:
            return {"error": f"Run '{run_id}' not found"}
        manifest = get_config_registry().get(run.get("graph_name", ""))
        scheduler_owned = bool(getattr(manifest, "scheduler_owned", False)) if manifest else False

        if not scheduler_owned:
            # Butler-driven: advance inline to the next checkpoint / end.
            return await self._run_pipeline_until_checkpoint(run_id)

        # Scheduler-owned: the poller advances it; await a terminal/paused state.
        try:
            timeout = float(args.get("timeout") or 120)
        except (TypeError, ValueError):
            timeout = 120.0
        timeout = max(5.0, min(timeout, 600.0))
        interval = 2.0
        waited = 0.0
        while waited < timeout:
            run = sf.get_run(run_id)
            status = run["status"] if run else "unknown"
            if status in ("paused", "completed", "failed"):
                return self._summarize_run_state(run_id)
            await asyncio.sleep(interval)
            waited += interval
        return {"status": "running", "run_id": run_id,
                "message": f"Still running after {int(timeout)}s. Call "
                           f"wait_until_next_checkpoint_or_completion again to keep "
                           f"waiting, or do other work meanwhile."}

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
        # Unique pid per generation so re-running (an "update") always executes a
        # fresh converter run rather than reusing a prior completed one. The
        # registered config name is derived from the stable `name` (→ gen_<slug>),
        # not the pid, so update still overwrites the same config.
        import uuid as _uuid
        pid = "convert-" + self._slugify(name) + "-" + _uuid.uuid4().hex[:6]
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

        # against_project: run this pipeline against an EXISTING project's repo
        # (resolves repo_path + repo_type=existing from it). One generic param
        # for every repo-operating pipeline — e.g. offloading a coding_impl run
        # against the current project — so no per-config tool is needed.
        repo_type = args.get("repo_type") or "new"
        repo_path = args.get("repo_path")
        against = (args.get("against_project") or "").strip()
        if against:
            proj = self.db.get_project(against)
            if not proj:
                return {"error": f"against_project '{against}' not found."}
            repo_path = proj.get("repo_path")
            if not repo_path:
                try:
                    repo_path = str(self.ws.get_code_path(against))
                except Exception:
                    repo_path = None
            if not repo_path:
                return {"error": f"No repo_path for project '{against}'."}
            repo_type = "existing"

        pid = generate_run_id(config_name)
        result = start_config_run(
            self.db, self.ws, config_name, pid,
            seed_text=args.get("seed_text"),
            seed_inputs=args.get("seed_inputs") or None,
            name=args.get("name") or config_name,
            owner_email=self.owner_email,
            repo_type=repo_type,
            repo_url=args.get("repo_url"),
            repo_path=repo_path,
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
    "search_projects": MetaAgent._tool_search_projects,
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
    "search_code": MetaAgent._tool_search_code,
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
    "list_pipelines": MetaAgent._tool_list_pipelines,
    "wait_until_next_checkpoint_or_completion": MetaAgent._tool_wait_until_checkpoint,
    "get_pipeline_result": MetaAgent._tool_get_pipeline_result,
    "stop_pipeline": MetaAgent._tool_stop_pipeline,
}

# Coding-mode-only tools — schema-visible and dispatchable only when the
# session mode is "coding" (see _stream_llm / _execute_tool).
_CODING_TOOL_HANDLERS = {
    "edit_file": MetaAgent._tool_edit_file,
    "create_file": MetaAgent._tool_create_file,
    "bash": MetaAgent._tool_bash,
    "runner_start": MetaAgent._tool_runner_start,
    "runner_submit": MetaAgent._tool_runner_submit,
    "runner_approve": MetaAgent._tool_runner_approve,
    "runner_reject": MetaAgent._tool_runner_reject,
    "skillflow_tool": MetaAgent._tool_skillflow_tool,
    "web_search": MetaAgent._tool_web_search,
    "web_fetch": MetaAgent._tool_web_fetch,
}
_TOOL_HANDLERS.update(_CODING_TOOL_HANDLERS)
