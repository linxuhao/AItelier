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
pytest tests/unit -v        # 360 unit tests
pytest tests/ -v            # full suite: 364 unit+integration tests
pytest tests/ -m network    # opt-in live tests (SearXNG / PyPI / httpbin), may flake

# Web SPA front-end unit tests (Vitest + jsdom) — separate toolchain, not run by pytest
cd web && npm install && npm test   # tests web/__tests__/ pure logic + component tests
```

Test config: `pytest.ini` (testpaths=tests, asyncio_mode=auto, `addopts = -m "not network"`). Suites live in `tests/{unit,integration,e2e,skillflow}`; network-dependent tests are marked `network` and deselected by default. Fixtures in `tests/conftest.py` provide isolated SQLite DB and FastAPI TestClient. The web SPA's Svelte components and utility modules are unit-tested under `web/__tests__/` and `web/src/__tests__/` (Vitest+jsdom); the full DPE pipeline runs end-to-end offline with mocked agents in `tests/integration/test_full_pipeline_real_runner.py`.

### Docker deployment & secrets

The backend + web UI run in Docker (`Dockerfile`, `docker-compose.yml`). The CLI auto-manages it: `cli/server.py:ensure_server_running` reuses the container if it is up, otherwise runs `docker compose up -d aitelier`. Docker is mandatory — there is **no host-process fallback** (running uvicorn on the host would make DPE git commits use the host developer's `~/.gitconfig` identity instead of the image's `AItelier` identity).

```bash
docker compose up -d            # build (first run) + start; the CLI does this for you
docker compose logs -f          # tail
```

- **Path-consistency:** host `~/.AItelier` is bind-mounted at the **same absolute path** inside the container, and `HOME` is set to the host home, so `Path.home()/.AItelier` and DB-stored absolute paths resolve identically on host (CLI) and in the container (server). Runs as host uid/gid so files keep host ownership.
- **External access:** the container binds `0.0.0.0`, published as `127.0.0.1:4444` (loopback-only — the public path is a Cloudflare tunnel reaching `aitelier:4444` over the shared `edge` network). `AITELIER_ALLOW_EXTERNAL=1` disables the app-level localhost guard (requests arrive from the bridge/tunnel, never 127.0.0.1).
- **Reader/writer auth** (`api/main.py:write_gate`): reads (GET) are open; mutating requests require an allowlisted **Cloudflare Access JWT** (`core/cf_access.py`, verified against `AITELIER_CF_TEAM_DOMAIN` + `AITELIER_CF_AUD`, email ∈ `AITELIER_WRITERS`) **or** the CLI's `AITELIER_ADMIN_TOKEN` (`X-AItelier-Admin-Token` header, honored only off-tunnel). The frontend read-only mode (`/api/me` → `can_write`) is UX only — the server gate is the control. Gate is inactive unless `AITELIER_CF_AUD` is set (local dev).
- **API-key secret:** `DEEPSEEK_API_KEY` is delivered as a Docker **secret file** (`~/.aitelier-secrets/DEEPSEEK_API_KEY` → `/run/secrets/DEEPSEEK_API_KEY`), NOT an env var, so test/build subprocesses that inherit `os.environ` don't receive it. `core/ai_router.py:_read_secret` resolves `/run/secrets/<name>` → `$AITELIER_SECRETS_DIR/<name>` → `os.getenv`. Keep secrets out of `.env`/git (chmod 600).
- **Git auth (clone/push/PR):** the host's `~/.ssh` / `~/.git-credentials` are **not** mounted into the container, so private-repo clone broke after containerization. Fixed with the same secret-file model: a **fine-grained GitHub PAT** at `~/.aitelier-secrets/GITHUB_TOKEN` → `/run/secrets/GITHUB_TOKEN`. `docker/git-credential-helper.sh` (wired via `GIT_CONFIG_*` in compose) feeds it to **github.com HTTPS remotes only** for clone/push; `core/git_ops.py:create_github_pr` reads the same secret for PR creation. An empty token file = "no credentials" (public clone still works). Chosen over bind-mounting `~/.git-credentials` because the container runs LLM-generated code — a scoped, revocable PAT has a far smaller blast radius than the host's whole credential store.
- **skillflow dependency — PyPI-only in the image (the vendor/wheels `--find-links` override was REMOVED from the Dockerfile):** the container runs plain `pip install -e .`, so it gets whatever `skillflow-py` version PyPI serves for the pin in `pyproject.toml`. The **host** venv uses an editable install of the skillflow checkout (`~/stepflow`), so host-side skillflow changes are live immediately — the **container** only picks them up via a PyPI release.
  - **Ship a skillflow change for real:** bump the version in the skillflow checkout's `pyproject.toml`, publish to PyPI, bump the `skillflow-py>=…` pin here, `docker compose build aitelier && up -d`.
  - **Quick dev-loop override (session-only, NOT durable):** build a wheel in the checkout, then `docker exec aitelier pip install --force-reinstall --no-deps /app/vendor/wheels/<wheel>` + `docker compose restart aitelier`. ⚠️ This lives in the container's writable layer — any container *recreation* (`up -d` after an image/config change) silently reverts to the image's PyPI version. When runner/butler tools error with ImportErrors, check `docker exec aitelier pip show skillflow-py` first.

Env reference lives in `.env.example`.

## Architecture

### Repo Separation

```
~/stepflow/  (or ~/skillflow/)  # Independent library (config-agnostic framework) — PyPI: skillflow-py
# Editable install (pip install -e <path>) — changes are live immediately (host only, NOT the container)
├── src/skillflow/
│   ├── core.py, graph.py, workspace.py, tool_loader.py, ...
│   ├── tools/               # 13 native tools (read_file, write, pytest, repo_apply, ...)
│   └── plugins/             # linter, skill_converter (skill→pipeline),
│                            # skill_runner (runner mode: SkillTool + RunnerService + skillflow-mcp)
└── {run,convert}_cli.py     # skillflow-run / -convert / -lint / -mcp console scripts

~/AItelier/                  # Host application
├── configs/                 # Skillflow graph configs (dpe_default.yaml, meta_conversation.yaml)
├── agent_configs/           # LLM agent configs by role name (model, template, tools)
├── templates/               # LLM prompt templates (*.md)
├── aitelier/tools/          # AItelier custom tools (web_search, web_fetch, run_tests, user_stories_present)
├── core/                    # Business logic (agents, scheduler, AI router, DB, workspace)
├── api/                     # CLI backend (FastAPI, localhost-only)
├── web_api/                 # Web GUI backend (multi-tenant, Cloudflare Access)
├── cli/                     # Typer CLI with Rich TUI dashboard
└── models/                  # Pydantic V2 data schemas
```

### Pipeline Flow

```
1. Meta Conversation (configs/meta_conversation.yaml)
   intent_detect → gather (Q&A loop, checkpoint) → finalize → project_brief.md + step1_goals.json

2. DPE Pipeline (configs/dpe_default.yaml)
   Researcher (1_5) → 1_5_review → Architect (2) → 2_review
   → PM (3) → 3_review → task_gate
   → [per task] t_plan → t_plan_review → t_impl → t_impl_apply (tool)
   → t_impl_validate (tool) → t_impl_review → t_verify → t_verify_review
   → task_loop → Final Verifier (5) → 5_review

3. Skill → Pipeline conversion + run (skillflow's skill_converter graph, registered at startup)
   analyze_skill → design_graph → explain_design (checkpoint) → validate_design (lint) → done
   Driven in-chat by the butler's `generate_pipeline` tool; host-mode agents → AITELIER_HOST_AGENT_MODEL.
   On completion the host BRIDGES the generated graph into a runnable config
   (`core/pipeline_registry.py`): namespaced `gen_<slug>`, persisted to `~/.AItelier/configs/`
   (gitignored, boot-scanned), live-registered (invented agent roles auto-registered as
   host agents), manifest added via `ConfigRegistry.register_one`. The butler can then
   `start_config_run(config_name="gen_<slug>")`; re-running `generate_pipeline` with the same
   name UPDATES in place (register_graph overwrites + version-bumps; manifests are lazy).

4. Butler CODING MODE (user-toggled per session: SPA toggle / `mode` field; sessions.mode column)
   The butler becomes an interactive coding agent (templates/coding_mode.md): direct repo tools
   (edit_file with read-before-edit guard, create_file, bash with secret-scrubbed env,
   web_search/web_fetch) + full-transcript persistence (chat_history.message_json; resume after
   budget_exhausted at coding_max_tool_turns or page refresh) + condenser (compacter agent
   summarizes past compact_at_tokens with a compaction_through watermark).
   Non-trivial changes go through the PLAN-GATED runner (configs/coding_task.yaml, driven via
   skillflow 1.5.0 RunnerService — the same core skillflow-mcp serves to external agents):
   runner_start → plan → ENGINE-ENFORCED checkpoint (implement not released until user approves)
   → runner_approve/reject → implement with own tools → runner_submit. skillflow_tool proxies the
   step's write_*/read_*/native tools; host tool names are bounced with a redirect error.
   Post-change review: start_config_run(config_name="code_review", seed_text=<task + verbatim
   git diff>) — butler-driven (scheduler_owned: false), verdict returns synchronously in outputs.
```

### Existing-repo support

A "fix a bug / add a feature" request on an existing codebase becomes a **new project** with `repo_type="existing"` + `repo_path`. The DPE pipeline runs normally and `repo_apply` commits changes into the real repo via skillflow's `code_path_resolver` (wired in `api/dependencies.py:_existing_repo_code_path`).

### Web UI

AItelier includes a single-page web frontend generated by AItelier itself (dogfooding).
It lives in `web/` and is served by the CLI API server.

```bash
# Start the API server (serves both API + web UI)
aitelier server

# Or start the server directly
uvicorn api.main:app --host 127.0.0.1 --port 4444

# Access the Web UI
#   Dashboard:    http://localhost:4444/
#   Chat:         http://localhost:4444/#/chat
#   Project view: http://localhost:4444/#/projects/<project_id>

# Start both Web API (multi-tenant, for real web deployment) and CLI API
AITELIER_MODE=demo uvicorn web_api.main:app --host 127.0.0.1 --port 8888
```

**Architecture:**
```
web/
├── index.html              # Svelte SPA entry (Vite)
├── package.json            # npm deps (Svelte 5, Vite, Pico CSS, Vitest)
├── vite.config.js          # Vite + Svelte plugin config
├── svelte.config.js        # Svelte compiler options
├── eslint.config.js        # ESLint + eslint-plugin-svelte
├── vitest.config.js        # Vitest + jsdom config
├── dist/                   # Vite build output (gitignored)
│   ├── index.html          # Compiled SPA entry
│   └── assets/             # Hashed JS/CSS bundles
├── src/
│   ├── main.js             # SPA mount point
│   ├── app.css             # Global styles (Pico CSS import)
│   ├── App.svelte          # Root component + router
│   ├── lib/                # Utility modules
│   │   ├── api.ts          # Typed fetch wrapper
│   │   ├── sse.ts          # EventSource manager
│   │   ├── markdown.ts     # Markdown + DOMPurify
│   │   └── format.ts       # Formatting helpers
│   ├── stores/             # Svelte stores (auth, connection, project, ...)
│   └── views/              # Svelte view components
└── __tests__/              # Vitest tests (pure logic + component)
```

**Key behaviors:**
- SPA with hash routing (`#/`, `#/projects/{id}`, `#/chat`)
- Dashboard polls `GET /api/projects` every 10s (paused during form input)
- SSE stream provides live pipeline events → notification sidebar
- Checkpoint modal auto-detects stale state and self-dismisses
- All API calls are same-origin (no CORS needed)

### Key Modules

| Module | Role |
|--------|------|
| `core/agents.py` | `AgentFactory` — reads `agent_configs/`, creates DPEAgent with model+template; model `host`/`default` → `AITELIER_HOST_AGENT_MODEL` |
| `core/prompt_assembler.py` | Assembles system/user prompts from templates + step context |
| `core/scheduler.py` | Polls skillflow: claim → execute → confirm → advance |
| `core/dpe_pipeline.py` | Legacy PipelineEngine (being phased out in favor of skillflow runner) |
| `core/workspace_manager.py` | Physical directory jail, Git operations, step staging→final directory lifecycle |
| `core/db_manager.py` | SQLite persistence (projects, tasks, settings, users) |
| `core/ai_router.py` | `AIGateway` — LiteLLM wrapper, provider registry from `llm_providers.json` |
| `core/meta_agent.py` | Autonomous CLI/WebGUI butler, DUAL-MODE (`sessions.mode`): butler = orchestration/inspection (drives meta_conversation, DPE & skill_converter runs in-chat; `generate_pipeline`; converter-completion relay); coding = interactive coding agent (edit_file/create_file/bash/web tools, runner_* + skillflow_tool over skillflow RunnerService, transcript persistence + condenser + budget pause) |
| `core/pipeline_registry.py` | Bridge that makes a converter-generated pipeline runnable: namespaced `gen_<slug>`, persist to `~/.AItelier/configs/`, live-register (auto-register invented agent roles as host agents), boot-scan; update overwrites in place |
| `core/event_bus.py` | In-process pub/sub for pipeline events |
| `api/dependencies.py` | FastAPI DI: SkillFlow, ToolLoader, AgentConfigs singletons; registers skillflow's `skill_converter` graph + boot-scans `~/.AItelier/configs/` for `gen_*` pipelines; `register_pipeline_from_run` wrapper; `code_path_resolver` for existing repos |
| `aitelier/runner.py` | `AItelierStepRunner` — bridges skillflow StepRunner protocol to PipelineEngine |
| `cli/tui/dashboard.py` | Rich TUI with project list, chat, checkpoint review |

### Configuration Files

- **`configs/dpe_default.yaml`** — v2 skillflow graph: steps, transitions, gates, tools, checkpoints
- **`configs/meta_conversation.yaml`** — Meta conversation graph (2 steps)
- **`configs/coding_task.yaml`** — Plan-gated coding runner graph (plan checkpoint → implement), butler-driven via RunnerService
- **`configs/code_review.yaml`** — One-shot diff review (butler-driven, `scheduler_owned: false`; verdict returns synchronously)
- **`agent_configs/dpe_default.yaml`** — Agent configs by role: model, template, tools list, thinking settings
- **`agent_configs/meta_conversation.yaml`** — Meta conversation agent configs + meta_agent (incl. `coding_max_tool_turns`, `compact_at_tokens`) + `compacter` (condenser)
- **`agent_configs/coding_task.yaml`** — Runner-step roles whose `system_prompt` IS the per-step prompt (the butler does the work, no LLM spawned)
- **`llm_providers.json`** — LLM provider registry (API base URLs, key env vars)
- **`AITELIER_HOST_AGENT_MODEL`** (env) — single model that skillflow `host`/`default` agents resolve to (default `deepseek/deepseek-v4-flash`); used by `skill_converter` and any generated pipeline
- **`skill_converter`** — graph + host agents live in skillflow's plugin; AItelier registers them at startup (`api/dependencies.py`), so no local config/template is duplicated
- **`templates/`** — Markdown prompt templates (step1_5_researcher.md, task_impl.md, ...)
- **`aitelier/tools/`** — AItelier custom tools: `web_search` (→ SearXNG), `web_fetch`, `run_tests`, `user_stories_present`

### Debug Tool (`debugctl.py`)

Drive the system headless — an agent or reviewer can launch the TUI, send a build request, send keys to approve checkpoints, and inspect workspace/diff/log as the pipeline runs end-to-end, no human needed:

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
