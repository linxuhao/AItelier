# AItelier

Multi-agent AI system for autonomous software project planning, implementation, and verification. Powered by [Skillflow](https://github.com/linxuhao/skillflow) — a config-agnostic LLM pipeline graph executor.

## Install

```bash
# Install AItelier (the skillflow-py framework is pulled from PyPI automatically)
pip install -e .
```

## Quick Start

First, set up your API keys:

```bash
# Copy the template and fill in your real keys
cp .env.example .env
# edit .env to add your provider keys, then load them
source .env
```

Then run:

```bash
# Interactive CLI dashboard
aitelier

# One-shot pipeline
aitelier "build me a todo app"

# Start backend server
aitelier server
```

## Configuration

To change which models or agents the pipeline uses, edit the config files directly:

- **`llm_providers.json`** — LLM providers (base URLs, API-key env var names)
- **`agent_configs/`** — per-role model, template, tools, and thinking settings
- **`templates/`** — the LLM prompt templates each step uses

## Architecture

AItelier is a **host application** on top of the Skillflow framework:

- **Configs** (`configs/`, `agent_configs/`) — pipeline graph and LLM agent definitions
- **Templates** (`templates/`) — per-step LLM system prompts
- **Tools** (`aitelier/tools/`) — AItelier custom tools + Skillflow native tools
- **Core** (`core/`) — agents, scheduler, AI router, DB, workspace
- **API** (`api/`, `web_api/`) — CLI and Web backend servers
- **CLI** (`cli/`) — Rich TUI dashboard

## Pipeline

```
Meta Conversation (gather requirements)
  → DPE Pipeline:
    Research → Architect → PM → [per task: Plan → Implement → Verify]
    → Final Verification
```

## Tests

```bash
pytest tests/unit/ -v      # 348 unit tests
pytest tests/skillflow/ -v  # 176 integration tests
```

## License

AItelier is **source-available** under the [Functional Source License (FSL-1.1-MIT)](LICENSE).

You may **use, modify, and self-host AItelier freely** — for internal use, education, research, and professional services. The only restriction is a **Competing Use**: you may not offer AItelier (or a substantially similar substitute) to others as a commercial product or service. Two years after each release, that version automatically converts to the **MIT license**.

The underlying pipeline engine, [SkillFlow](https://github.com/linxuhao/skillflow), is fully open source under the MIT license.
