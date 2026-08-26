# dsh-plugin-aitelier

Use **AItelier** as a subagent from [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness): design a pipeline, edit its graph / roles / prompts / tools, run it, and carry it to another machine.

The plugin is a Profile Bundle that mounts one `@deepseek-ai/dsh-mcp-client` row against AItelier's MCP endpoint. The model then sees the surface as native tools under `mcp__aitelier__*`.

> ### No `mcp__aitelier__*` tools? Read this first.
>
> A connection failure here is **silent**. `dsh-mcp-client` has `failOnStartupError: false`, so an unreachable endpoint does not stop `dsh` booting — the tools simply never appear, and nothing says why. An agent in that state can only report "no such tools" and guess; it cannot diagnose it from the inside. Check, in order:
>
> 1. **Is AItelier running?** `curl -s localhost:4444/health` should answer `{"status":"ok",…}`. If not, start it — see [Prerequisite](#prerequisite-aitelier-itself).
> 2. **Is the URL right for where `dsh` runs?** The default is `http://127.0.0.1:4444/mcp` (host). Use `http://aitelier:4444/mcp` **only** if `dsh` is itself a container on the same docker network.
> 3. **Does the endpoint answer?** `curl -s -X POST $AITELIER_MCP_URL -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'` — a `421` means the Host header is not in AItelier's allow-list (set `AITELIER_MCP_ALLOWED_HOSTS` on the AItelier side).
> 4. **Is the row actually mounted?** `dsh --profile <name> --dump-config | grep -A3 mcp-aitelier`.

## Prerequisite: AItelier itself

This plugin is a *client*. It does not install or start AItelier — you need one running and reachable first. AItelier ships as a container:

```bash
git clone https://github.com/linxuhao/AItelier && cd AItelier
mkdir -p ~/.aitelier-secrets && chmod 700 ~/.aitelier-secrets
printf '%s' "sk-your-deepseek-key" > ~/.aitelier-secrets/DEEPSEEK_API_KEY
chmod 600 ~/.aitelier-secrets/DEEPSEEK_API_KEY
docker compose up -d          # serves the API + MCP endpoint on 127.0.0.1:4444
```

The LLM key stays on the AItelier side and never travels through this plugin — see [Which API key goes where](#which-api-key-goes-where).

## Install

```bash
corepack enable pnpm     # `dsh plugin` drives pnpm; on a node-only box it refuses
dsh plugin --profile headless add dsh-plugin-aitelier
```

That one command installs the package **and** appends it to the profile's `dsh.profile.bundles`. Then restart the profile. Configure it in the Harness home's env layer (`~/.dsh/.env`):

```sh
AITELIER_MCP_URL=http://127.0.0.1:4444/mcp   # the default; set it only to override
AITELIER_ADMIN_TOKEN=…                       # only needed for the write tools
```

Reads work with no credentials. Writes need the token — see [Authorization](#authorization).

Verify the install by asking the agent to call `mcp__aitelier__list_pipelines`; it should come back with the registered pipelines.

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
| `generate_pipeline` | write | Write a NEW pipeline from a description (runs AItelier's grounded generator). `edit_target=` re-generates an existing one with a change. |
| `archive_pipeline` | write | Retire a generated pipeline. Deleting its files alone leaves a runnable zombie. |
| `run_pipeline` | write | Start a run; returns a `run_id` immediately. |
| `wait_for_run` | read | Block until the run pauses at a checkpoint or finishes. Use this, not a poll loop. |
| `answer_checkpoint` | write | Approve or reject a paused run. Rejecting sends work back with feedback. |
| `stop_pipeline` | write | Cancel a run that is going nowhere. |
| `get_run_status` | read | A single non-blocking look. |
| `get_run_summary` | read | What the run did: per-step status, the FIRST failure with its error, final outputs. Inside a loop each entry names the **item** it ran for. |
| `get_step_output` | read | The files ONE step produced. Each is capped at 20000 chars; a file that was cut says so in the text and in a `truncated` map, and `file=<name>` reads one file at a 200000-char cap. |
| `list_runs` | read | Recent runs, newest first — the entry point when you hold no id. |
| `trace_list` / `trace_search` / `trace_read` | read | The durable trace: find where it broke, then read the actual prompt / response / tool result. |

| `get_available_models` | read | The INTERNAL model names this deployment serves (`flash`, `pro`, …), their ordered endpoint candidates, and whether each can serve right now. Roles reference these names, never a `provider/model` string — start here before `edit_role`. |
| `list_providers` | read | Registered endpoints: base URL, the NAME of the secret each reads, and which models it serves. |
| `add_provider` / `update_provider` / `delete_provider` | write | Manage endpoints. `api_key_env` is the NAME of a secret file, never the key. Deleting one a model still uses is refused. |
| `add_model` / `delete_model` | write | Create or remove an internal model name. **Order is policy**: calls bind to the first candidate and the rest are failover, so put a pay-as-you-go endpoint LAST. Deleting one something references is refused. |
| `map_model` / `unmap_model` | write | Point an internal model at one more endpoint, or take one away. Removing the last candidate is refused — a model resolving to nothing fails at its first call. |
| `skillflow_docs_list` / `skillflow_docs_search` / `skillflow_docs_read` | read | Skillflow's own spec for the graph YAML `edit_pipeline` accepts. Read it before inventing a field. |

Every run-taking tool names its argument `run_id` and accepts either a run id or a project id (the newest run of that project is used, and the reply names which one). Before 2026-08-26 four of them called it `run` and only some accepted a project id — a call written against the old shape fails validation with the key it wanted, so it is a retry, not a wrong answer.


### Editing needs something to edit

Only **generated** (`gen_*`) pipelines are editable and exportable — a built-in config lives in the AItelier repo and travels with it. **A fresh AItelier has no generated pipelines at all**, so on a new install every `edit_*` and `export_pipeline` call correctly refuses, and `list_pipelines` shows only built-ins. Make one with `generate_pipeline`.

## The skill

The package ships one skill, `aitelier-pipelines`, at
`skills/aitelier-pipelines/SKILL.md`. It teaches the loop below, which tool
answers which question when a drive fails, and the failure shapes that pass all
three of AItelier's structural gates and only show up on a real run. Install it
into a skill root DSH already scans:

```bash
mkdir -p ~/.dsh/skills
cp -r ~/.dsh/profiles/*/node_modules/dsh-plugin-aitelier/skills/aitelier-pipelines ~/.dsh/skills/
```

`~/.dsh/skills` (`$DSH_HOME/skills`) is the `user-dsh` root — scanned for every
project, no git root required. **The package lives in the PROFILE's
`node_modules`, not your project's**: `dsh plugin add` installs into
`$DSH_HOME/profiles/<name>`, so a `cp` run from a project directory finds
nothing. For one project only, `<projectRoot>/.agents/skills/` works too — the
project root being the nearest ancestor with a `.git`.

**Why this is a copy and not automatic.** A Cordis patch targets a row by id and
replaces its *whole* config. Mounting the skill by patching the shared
`skill-filesystem` row would therefore overwrite whatever skill roots, watch
settings and custom directories you already had. Inserting an isolated provider
row instead would need this patch to resolve its own installed directory, and
this bundle ships no code to do that with. One `cp` you can see beats a config
edit that silently drops your other skills.

## The loop: generate → drive → observe → fix

The whole point of the surface. AItelier's own three structural gates check that a generated pipeline is *shaped* right; only running it shows whether it *works*, and that is a judgment loop, not a fixed DAG:

1. **`generate_pipeline("…")`** → a `run_id`. The generator is scheduler-driven, so AItelier advances it; you do not step it.
2. **`wait_for_run`** → it pauses at a design review. Read it, then **`answer_checkpoint`** — approve, or reject with feedback and it revises. On completion the pipeline appears in `list_pipelines` as `gen_<slug>`.
3. **`run_pipeline(gen_<slug>, seed_text=…)`** — a test drive. Checkpoints are answered for you by default (see below).
4. **`wait_for_run`** → **`get_run_summary`**. A step failed, or the outputs are wrong? **`trace_list(run_id, errors_only=true)`** finds where, **`trace_read(seq)`** shows the actual prompt and response, **`get_step_output`** shows what a middle step wrote.
   Inside a fan-out, `get_run_summary` names the loop **item** each instance ran for (`{step: t_impl, status: failed, item: health_bar}`) — a loop body runs once per item, plus retries, so without it a failure names a step that ran nine times and you are guessing which task broke.
5. **Fix and go again.** `edit_template` for a prompt (usually the answer), `edit_pipeline` for the graph — consult `skillflow_docs_search` for the schema rather than guessing — `edit_tool` for a tool's code. Or `generate_pipeline(edit_target=gen_<slug>, description="the change")` for a surgical regeneration. Then back to 3.
6. **`stop_pipeline`** any drive that is going nowhere, and **`archive_pipeline`** the attempts you abandon.

Nothing in that loop steps the pipeline by hand: AItelier's scheduler runs it, and the agent decides at checkpoints and between runs.

## Runs do not block, but waiting does

An AItelier run is long and may pause for human approval, so `run_pipeline` returns a `run_id` and nothing else. Then call **`wait_for_run`**: it is push-based and returns the instant the run settles — at a checkpoint OR at a failure, because a watcher that matches only the happy ending sits silently through a crash.

It waits at most `timeout_seconds` (default 45) and then returns `status: "waiting"`, `timed_out: true`. That is not a failure — call it again.

**The ceiling is your client's, not ours.** A wait longer than `toolCallTimeoutMs` does not wait longer: the client hangs up first and the model sees a transport error instead of "still running". This plugin therefore raises `toolCallTimeoutMs` to 10 minutes (override with `AITELIER_MCP_TIMEOUT_MS`), well above `wait_for_run`'s own default, so the two cannot fight. Use `timeout_seconds: 0` for one look with no wait.

A `paused` run is waiting to be answered — **`answer_checkpoint(run_id, decision, feedback)`**, right here. The UI is the other way in, not the only one. (This line used to say DSH could not approve, while the table above listed `answer_checkpoint`; the tool always worked.)

An approval carries **no** feedback channel: AItelier refuses `decision: "approve"` with non-empty `feedback` rather than accept text it cannot deliver. To make a demand stick, reject with it — that sends the step back to redo the work against it.

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
