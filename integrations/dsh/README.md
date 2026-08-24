# dsh-plugin-aitelier

Use **AItelier** as a subagent from [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness): design a pipeline, edit its graph / roles / prompts / tools, run it, and carry it to another machine.

The plugin is a Profile Bundle that mounts one `@deepseek-ai/dsh-mcp-client` row against AItelier's MCP endpoint. The model then sees the surface as native tools under `mcp__aitelier__*`.

## Install

```sh
npm install dsh-plugin-aitelier
```

Then add it to the profile and restart it. Point it at the running AItelier:

```sh
export AITELIER_MCP_URL=http://aitelier:4444/mcp     # docker service name
export AITELIER_ADMIN_TOKEN=…                        # only needed for writes
```

Reads work with no credentials. Writes need the token — see **Authorization**.

## The surface

| Tool | Kind | What it is for |
|---|---|---|
| `list_pipelines` | read | Start here. Names + `input_hint` for every registered pipeline. |
| `get_pipeline` | read | One pipeline's graph YAML and step list. |
| `edit_pipeline` | write | Replace the graph. Validated before anything is written. |
| `list_roles` / `get_role` | read | The agent roles a pipeline's steps use. |
| `edit_role` | write | Model, tools, temperature, thinking. |
| `list_templates` / `get_template` | read | Each role's prompt. |
| `edit_template` | write | Replace a role's prompt — the main way to change behaviour. |
| `list_tools` / `get_tool` | read | Host tools; which are generated (editable) vs built-in. |
| `edit_tool` | write | Write a generated tool. The source must import and define its own name. |
| `export_pipeline` | read | The whole closure — graph, roles with prompts, custom tools — as one JSON bundle. |
| `import_pipeline` | write | Install a bundle, optionally under a new name. |
| `run_pipeline` | write | Start a run; returns a `run_id` immediately. |
| `wait_for_run` | read | Block until the run pauses at a checkpoint or finishes. Use this, not a poll loop. |
| `get_run_status` | read | A single non-blocking look. |

Only **generated** (`gen_*`) pipelines are editable and exportable. A built-in config lives in the AItelier repo and travels with it.

## Runs do not block, but waiting does

An AItelier run is long and may pause for human approval, so `run_pipeline` returns a `run_id` and nothing else. Then call **`wait_for_run`**: it is push-based and returns the instant the run settles — at a checkpoint OR at a failure, because a watcher that matches only the happy ending sits silently through a crash.

It waits at most `timeout_seconds` (default 45) and then returns `status: "waiting"`, `timed_out: true`. That is not a failure — call it again.

**The ceiling is your client's, not ours.** `toolCallTimeoutMs` defaults to 60s in `dsh-mcp-client`, and a wait longer than that does not wait longer: the client hangs up first and the model sees a transport error instead of "still running". Raise `toolCallTimeoutMs` before raising `timeout_seconds`. Use `timeout_seconds: 0` for one look with no wait.

A `paused` run is waiting for a person in the AItelier UI — DSH cannot approve it.

## Authorization

Reads are open. Writes require `AITELIER_ADMIN_TOKEN`, checked per tool by AItelier itself.

The reason it is per tool rather than per path: MCP posts every call, read or write, to the same URL, so AItelier's normal method-based write gate cannot tell them apart. Exempting the path would have left `edit_pipeline` unauthenticated. See `api/mcp_router.py`.

Without the token, write tools answer `denied: …` and change nothing. That is a legitimate read-only installation.

## Which API key goes where

Two different credentials, two different owners. They do not mix:

- **AItelier's LLM key** (`DEEPSEEK_API_KEY`) belongs to AItelier and never leaves it. Its agents run inside its own container and call the model themselves; DSH is only telling them what to do. AItelier reads it from a mounted secret file, deliberately not from the environment, so subprocesses cannot inherit it.
- **The credential in THIS plugin's config** is only for reaching AItelier: `AITELIER_ADMIN_TOKEN`. That is the one DSH owns.

Both sides follow the same rule — configuration carries a *reference* to a secret, never the secret. `cordis.patch.yml` holds `process.env.AITELIER_ADMIN_TOKEN`, not a token.

## Not included

A native `SubagentProvider` (the seat `subagent-codex` and `subagent-claude-code` occupy) is not part of this version. It would let `ctx.subagents.start('aitelier', …)` delegate a whole task and make DSH's own `tool-subagent-control` / `-report` work against it. The blocker is not effort but contract: a one-shot subagent is request → result, while an AItelier run stops at human checkpoints. DSH's *continuable* children (`prepareContinuable` + `followup`) are the right shape for that, and it is worth doing separately.
