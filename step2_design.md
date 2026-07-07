# Technical Architecture Design

## Overview

Add a "Path A — Pipeline Offload" decision path to the butler agent's SYSTEM_PROMPT in `core/meta_agent.py`, allowing small bug fixes and small features (~5 files or fewer) on existing projects to be offloaded directly to subagent/fix_tests/investigate/code_review pipelines without the DPE requirements conversation. Keep the existing DPE flow as Path B (the default for everything else — new projects and non-trivial changes).

This is purely a **prompt string edit** to one file. No new tool definitions, no runtime logic changes, no pipeline config changes.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│              butler SYSTEM_PROMPT                        │
│              (core/meta_agent.py:62-127)                 │
│                                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │ IDENTITY / ROLE (unchanged)                       │  │
│  │ "You are the AItelier butler..."                  │  │
│  └──────────────────────────────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │ PATH DECISION (NEW)                               │  │
│  │ Decision table + routing logic:                   │  │
│  │   - Existing project + small (~5 files) → Path A  │  │
│  │   - Everything else → Path B (default)             │  │
│  └──────────────────────────────────────────────────┘  │
│                    │              │                     │
│                    ▼              ▼                     │
│  ┌──────────────────────┐ ┌────────────────────────┐  │
│  │ PATH A (NEW)          │ │ PATH B (EXISTING)       │  │
│  │ Pipeline Offload      │ │ DPE                     │  │
│  │                        │ │                         │  │
│  │ 1. Explore codebase   │ │ 1. start_new_project /  │  │
│  │ 2. Pick pipeline      │ │    start_from_aitelier  │  │
│  │    (describe_pipeline)│ │    / start_existing     │  │
│  │ 3. Offload             │ │    / start_from_git_url│  │
│  │    (start_config_run) │ │ 2. Relay questions      │  │
│  │ 4. Drive to completion│ │ 3. Relay brief          │  │
│  │    (wait→approve/     │ │ 4. Approve → build      │  │
│  │     reject loop)      │ │                         │  │
│  │ 5. Report              │ │                         │  │
│  │    (get_pipeline_     │ │                         │  │
│  │     result)           │ │                         │  │
│  └──────────────────────┘ └────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌──────────────────────────────────────────────────┐  │
│  │ SHARED SECTIONS (preserved/lightly adjusted)      │  │
│  │ - [ACTIVE PROJECT CONVERSATION] relay            │  │
│  │ - Skill/workflow → generate_pipeline             │  │
│  │ - After a pipeline starts (checkpoint handling)  │  │
│  │ - CRITICAL rules                                 │  │
│  │ - Otherwise (chit-chat)                          │  │
│  │ - Placeholder variables                          │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Component List

### Component 1: Butler SYSTEM_PROMPT string

- **File:** `./core/meta_agent.py`
- **Lines:** 62–127 (triple-quoted Python string assigned to `SYSTEM_PROMPT`)
- **Responsibility:** Instructs the butler LLM on how to route user requests and orchestrate pipelines
- **Interface:** This is a string injected as the system message into the butler's LLM conversation. It has no programmatic interface — it drives LLM behavior purely through instruction.
- **Change scope:** ~65 lines of Python string (adding Path A section + decision table; reorganizing existing Path B text)

### Component 2: (No changes) Existing pipeline configs

The five offload pipelines are already registered and operational:
- `subagent` — gated generalist worker (default for small fixes/features)
- `fix_tests` — test-fix loop (when tests are failing)
- `investigate` — read-only exploration
- `code_review` — adversarial diff review
- `coding_impl` — offloaded implementation from approved plan

### Component 3: (No changes) Existing orchestration tools

All tools needed for Path A already exist in `TOOL_DEFINITIONS`:
- `describe_pipeline(name)` — check pipeline input contracts
- `start_config_run(config_name, seed_text, against_project)` — launch pipeline
- `wait_until_next_checkpoint_or_completion(run_id)` — block until checkpoint/completion
- `approve_checkpoint(run_id)` / `reject_checkpoint(run_id, feedback)` — handle checkpoints
- `get_pipeline_result(run_id)` — get final output
- `stop_pipeline(run_id)` — cancel a run
- `list_code_tree(path)`, `read_code_file(path)`, `search_code(pattern)` — explore codebase

## Prompt Structure Design

### Section ordering (final SYSTEM_PROMPT)

1. **Identity/role** (1 line) — unchanged
2. **Path decision guide** (NEW) — how to choose between Paths A and B, includes the decision table
3. **Path A — Pipeline Offload** (NEW) — explore → pick → offload → drive → report workflow
4. **Path B — DPE** (MODIFIED) — existing DPE flow, recontextualized as "for everything else"
5. **Active conversation relay** — unchanged
6. **Skill/workflow → pipeline** — unchanged
7. **After a pipeline starts** — lightly expanded to cover both Path A and Path B checkpoint handling
8. **CRITICAL rules** — preserved with minor additions for Path A
9. **Otherwise** — unchanged
10. **Placeholder variables** — unchanged

### Path A workflow (5 steps)

| Step | Action | Tool(s) |
|------|--------|---------|
| 1. Explore | Understand the codebase before acting | `list_code_tree`, `read_code_file`, `search_code` |
| 2. Pick pipeline | Choose the right offload target; verify its contract | `describe_pipeline(name)` |
| 3. Offload | Launch the pipeline against the existing project | `start_config_run(config_name, seed_text, against_project=<project_id>)` |
| 4. Drive | Block→relay→approve/reject loop until completion | `wait_until_next_checkpoint_or_completion`, `approve_checkpoint`, `reject_checkpoint` |
| 5. Report | Summarize the result | `get_pipeline_result` |

### Decision table (embedded in prompt)

| Situation | Path | Pipeline |
|-----------|------|----------|
| "Fix the login redirect bug" on existing project | A | subagent |
| "Add a search bar" on existing project | A | subagent |
| "Find everywhere we use the old API" on existing project | A | investigate |
| "The tests are failing — fix them" on existing project | A | fix_tests |
| "Build a new habit-tracking app" (from scratch) | B | DPE |
| "Add real-time collaboration" (touches architecture) | B | DPE |
| "Migrate the database schema and all queries" (many files) | B | DPE |

### Path A gates (when NOT to use Path A)

- No existing project (new app from scratch)
- Non-trivial scope (~5+ files, architectural implications)
- Cross-cutting concerns (shared design, data model changes)
- User explicitly requests DPE/architecture planning
- Ambiguous scope that reveals cross-cutting implications after initial exploration

When in doubt: **Path A for existing projects with clearly small scope; Path B (safer default) for everything else.**

### Extended CRITICAL rules

Existing rules preserved; adds:
- Path A still uses pipelines — NEVER write code yourself
- Path A requires an existing project with a wired repo
- If exploration reveals the task is larger than ~5 files, escalate to Path B

## Technical Stack

- **Language:** Python 3.x (string literal edit only)
- **No dependencies changed**
- **No tests changed** (existing tests for `core/meta_agent.py` continue to pass — they test tool definitions and runtime behavior, not the SYSTEM_PROMPT string content)

## Extensibility Considerations

- **New offload pipelines:** If new pipeline configs are registered (e.g., a `gen_*` skill), the butler discovers them via `describe_pipeline` — no prompt change needed. The prompt says "pick the right pipeline" generically.
- **Threshold tuning:** The "~5 files" heuristic is in the prompt string, not in code. Adjusting it is a one-line edit.
- **Path C (future):** The prompt structure cleanly separates path selection from path execution. A future Path C would slot in next to A and B with its own decision-table row.

## Rollback Plan

The change is a single-file string edit. Rollback:
1. Revert `core/meta_agent.py` to the previous version via `git checkout`
2. Restart the butler agent service

No database migrations, no schema changes, no config changes. The SYSTEM_PROMPT is read at agent initialization time.

## Edge Cases Addressed

1. **No project exists:** Path A gates require an existing project → falls through to Path B
2. **Ambiguous scope:** Prompt instructs butler to explore first; if scope exceeds ~5 files or reveals architectural implications, escalate to Path B
3. **Coherence trigger:** Multiple independent small fixes can each be Path A; a single change touching many files with shared design → Path B (same rule as coding_mode.md)
4. **Unknown pipeline:** `describe_pipeline` may return an error → butler instructed to fall back to `list_pipelines` or pick a safe default (subagent)
5. **Missing repo:** `start_config_run` may fail if project has no repo → butler instructed to explain and suggest Path B
6. **Mixed request:** Prompt instructs butler to prefer Path B for safety when a request mixes small fixes with architectural changes
7. **Existing project without prior DPE run:** "Existing project" is the gate, not DPE history — projects started via `start_existing_project` qualify for Path A
