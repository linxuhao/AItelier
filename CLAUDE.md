# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
Under Developpement, no backward compatbility is needed.

## Project Overview

AItelier is a multi-agent AI system that plans, architects, implements, and verifies software projects autonomously. It uses a Green (Maker) / Red (Checker) adversarial pattern defined as skillflow graph nodes — agents are stateless: each step reads its context from prior step outputs and writes results into a per-step staging directory (`{step}.tmp/`) that the engine validates and promotes to a final step directory (`{step}/`). All changes tracked via Git event sourcing.

**Pipeline execution is handled by [Skillflow](https://github.com/linxuhao/skillflow)** — a config-agnostic graph executor (PyPI: `skillflow-py`). AItelier is the host application: UI, DB, workspace management, LLM provider config, and pipeline-specific templates/tools.

## Build & Run

```bash
# Install AItelier (skillflow-py pulled from PyPI)
pip install -e .

# Run CLI
aitelier
aitelier "build me a todo app"
aitelier server

# Tests
pytest tests/unit/ -v       # 348 unit tests
pytest tests/skillflow/ -v   # 176 integration tests (use skillflow lib)
pytest tests/ -v             # all tests
```

Test config: `pytest.ini` (testpaths=tests, asyncio_mode=auto). Fixtures in `tests/conftest.py` provide isolated SQLite DB and FastAPI TestClient.

## Architecture

### Repo Separation

```
~/skillflow/                  # Independent library (config-agnostic framework)
├── src/skillflow/            # graph.py, core.py, tool_loader.py, ...
└── tools/                   # 12 native tools (read_file, write, web_search, ...)

~/AItelier/                  # Host application
├── configs/                 # Skillflow graph configs (dpe_default.yaml, meta_conversation.yaml)
├── agent_configs/           # LLM agent configs by role name (model, template, tools)
├── templates/               # LLM prompt templates (*.md)
├── aitelier/tools/          # AItelier custom tools (save_draft_brief, suggest_submit_project)
├── core/                    # Business logic (agents, scheduler, AI router, DB, workspace)
├── api/                     # CLI backend (FastAPI, localhost-only)
├── web_api/                 # Web GUI backend (multi-tenant, Cloudflare Access)
├── cli/                     # Typer CLI with Rich TUI dashboard
└── models/                  # Pydantic V2 data schemas
```

### Pipeline Flow

```
1. Meta Conversation (configs/meta_conversation.yaml)
   intent_detect → meta → project_brief.md + step1_goals.json

2. DPE Pipeline (configs/dpe_default.yaml)
   Researcher (1_5) → 1_5_review → Architect (2) → 2_review
   → PM (3) → 3_review → task_gate
   → [per task] t_plan → t_plan_review → t_impl → t_impl_apply (tool)
   → t_impl_validate (tool) → t_impl_review → t_verify → t_verify_review
   → task_loop → Final Verifier (5) → 5_review
```

### Key Modules

| Module | Role |
|--------|------|
| `core/agents.py` | `AgentFactory` — reads `agent_configs/`, creates DPEAgent with model+template |
| `core/prompt_assembler.py` | Assembles system/user prompts from templates + step context |
| `core/scheduler.py` | Polls skillflow: claim → execute → confirm → advance |
| `core/dpe_pipeline.py` | Legacy PipelineEngine (being phased out in favor of skillflow runner) |
| `core/workspace_manager.py` | Physical directory jail, Git operations, step staging→final directory lifecycle |
| `core/db_manager.py` | SQLite persistence (projects, tasks, settings, users) |
| `core/ai_router.py` | `AIGateway` — LiteLLM wrapper, provider registry from `llm_providers.json` |
| `core/meta_agent.py` | Autonomous CLI/WebGUI butler agent |
| `core/event_bus.py` | In-process pub/sub for pipeline events |
| `api/dependencies.py` | FastAPI DI: SkillFlow, ToolLoader, AgentConfigs singletons |
| `aitelier/runner.py` | `AItelierStepRunner` — bridges skillflow StepRunner protocol to PipelineEngine |
| `cli/tui/dashboard.py` | Rich TUI with project list, chat, checkpoint review |

### Configuration Files

- **`configs/dpe_default.yaml`** — v2 skillflow graph: steps, transitions, gates, tools, checkpoints
- **`configs/meta_conversation.yaml`** — Meta conversation graph (2 steps)
- **`agent_configs/dpe_default.yaml`** — Agent configs by role: model, template, tools list, thinking settings
- **`agent_configs/meta_conversation.yaml`** — Meta conversation agent configs + meta_agent
- **`llm_providers.json`** — LLM provider registry (API base URLs, key env vars)
- **`templates/`** — Markdown prompt templates (step1_5_researcher.md, task_impl.md, ...)
- **`aitelier/tools/`** — AItelier custom tools (web_search delegates to SearXNG, etc.)

### Debug Tool (`debugctl.py`)

```bash
python3 debugctl.py start              # Launch CLI in tmux
python3 debugctl.py capture            # Read TUI as text
python3 debugctl.py cmd "build me a todo app"  # Send + Enter
python3 debugctl.py key Enter          # Press a key
python3 debugctl.py stop               # Kill session
python3 debugctl.py watch <project_id> # Watch workspace changes
python3 debugctl.py inspect <project_id>  # Tree + diff + log
```

### Tech Stack

Python 3.12, Skillflow (graph executor), FastAPI, Pydantic V2, SQLite (WAL), APScheduler, LiteLLM, Typer, Rich, httpx.
