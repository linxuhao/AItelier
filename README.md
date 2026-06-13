# AItelier

Multi-agent AI system for autonomous software project planning, implementation, and verification. Powered by [SkillFlow](https://github.com/linxuhao/SkillFlow) — a deterministic, config-agnostic LLM pipeline graph executor.

## Why AItelier

Most "AI builds software for you" tools are non-deterministic black boxes: you can't reproduce a run, audit why the agent did what it did, or insert a human decision where it matters. AItelier is built on the opposite premise — that an autonomous pipeline should be **trustworthy by construction**:

- **Deterministic** — the pipeline is a graph traversed by the engine, not a control flow improvised by an LLM. The same config follows the same path.
- **Fully traceable** — every run keeps an append-only audit trace: each step, prompt, model response, and tool call. "Why did this run do that?" is one query, not forensic archaeology.
- **Human-in-the-loop** — approval/reject checkpoints are first-class between stages; you can review and send work back with feedback.
- **Adversarial quality** — every step is produced by a Green (Maker) agent and reviewed by a Red (Checker) agent before it advances.
- **Adaptable** — pipelines, agents, and prompts are plain config. Nothing about the engine is hardcoded to one workflow.

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

## How it works

AItelier defines its workflow as a **SkillFlow graph** of stateless agent steps. The SkillFlow engine owns traversal, tool execution, checkpoints, and the durable trace; AItelier supplies the agents, templates, tools, and UI.

Agents never hold state in memory. Each step receives its context from the outputs of prior steps, writes its results into a per-step staging directory that the engine validates and then promotes, and every promoted change is committed to **Git (event sourcing)** — so any run can be replayed or inspected after the fact. A scheduler drives the loop one step at a time: `advance → claim → execute → confirm`. The default DPE pipeline applies this to software delivery (research → architect → plan → implement → verify), but because a pipeline is just config, the same engine runs any auditable multi-agent workflow.

## Architecture

AItelier is a **host application** on top of the SkillFlow framework:

- **Configs** (`configs/`, `agent_configs/`) — pipeline graph and LLM agent definitions
- **Templates** (`templates/`) — per-step LLM system prompts
- **Tools** (`aitelier/tools/`) — AItelier custom tools + SkillFlow native tools
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

The underlying pipeline engine, [SkillFlow](https://github.com/linxuhao/SkillFlow), is fully open source under the MIT license.
