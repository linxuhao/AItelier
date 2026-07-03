# Butler execution layers & the generic pipeline toolset

**Status:** design of record · **Date:** 2026-07-03

## 1. Problem & goal

The coding-mode butler is a single long-lived ReAct loop. Everything it does —
files read, command output, diffs digested — is appended verbatim to *its own*
transcript. Context-heavy work (audit a subsystem, implement + review a task,
build a module) bloats the driver session: the condenser then has to summarize,
tokens/turn climb, and the session eventually hits the tool-turn budget.

**Goal:** give the butler a clean, explicit ladder of *where work runs*, so that
the heaviest work (implementation, review, multi-agent tooling — millions of
tokens) happens in **isolated skillflow runs** whose transcripts are discarded,
and the driver only ever sees compact status + checkpoints. Combined with the
existing condenser, this keeps the driver session slim and long-lived.

## 2. The three execution layers

The butler chooses one of three layers per subtask. This is the whole model —
there is no async/background layer; a run is either driven inline or awaited.

| Layer | What | Mechanism | When |
|-------|------|-----------|------|
| **1 — Inline** | Classic ReAct: the butler reads/edits/runs directly | `edit_file`, `create_file`, `bash`, `read_code_file`, `search_code`, `web_*` | Discovered-as-you-go, small, exploratory. |
| **2 — Hybrid (plan→task)** | Plan-gated coding runner: the butler does the work but through a gated MCP runner | `runner_start / runner_submit / runner_approve / runner_reject` + `skillflow_tool` over `configs/coding_task.yaml` | Non-trivial multi-file change the user should sign off on. |
| **3 — Offload to a skillflow config** | Hand a whole unit of work to a registered pipeline whose agents burn their *own* context | the **generic pipeline toolset** (§4), driving any config: `dpe_default`, `code_review`, `coding_task`, `gen_*`, and new ones | Context-heavy, self-contained work whose *result* is what the driver needs, not the reasoning. |

Layer 3 is the context saver: the run's implementation/review/tool-calling never
touches the driver transcript. Checkpoint payloads *do* re-enter the driver, but a
checkpoint is a few KB against the millions of tokens spent inside the run — the
gate cost is negligible, so **gated pipelines (DPE, plan→task) are first-class
layer-3 citizens**, not just the checkpoint-free `code_review`.

## 3. What already exists

Layer 3 is ~70% built; it's a *consolidation + two new verbs*, not new machinery.

- **Generic start.** `start_config_run(config_name, seed_text)` launches any
  registered config (`core/run_launcher.py`, `meta_agent.py:2836`).
- **Inline drive-to-checkpoint.** `_run_pipeline_until_checkpoint`
  (`meta_agent.py:2415`) advances a **butler-driven** (`scheduler_owned:false`)
  run until it pauses/completes and returns a compact
  `{status, step_id, label, data, outputs}`. This *is* the wait primitive for that
  class of config.
- **Async drive.** Scheduler-owned configs (DPE) are advanced by the APScheduler
  poller; the `event_bus` emits `CHECKPOINT_REACHED` / `PIPELINE_END` /
  `PIPELINE_ERROR` and skillflow's notification bus emits `checkpoint_paused` /
  `run_completed` (`scheduler.py:692`).
- **Status / outputs.** `get_pipeline_status(run_id)` is already generic;
  `get_step_output`, `read_workspace_file`, `list_workspace_tree` read artifacts.
- **Checkpoint control.** `approve_checkpoint` / `reject_checkpoint` exist.
- **Registry.** `get_config_registry().list()` knows every config — but no tool
  surfaces it to the butler.

## 4. The generic pipeline toolset (layer 3)

Seven tools; the butler drives *any* config with the same loop regardless of
whether it's scheduler-owned or butler-driven.

| Tool | State | Behaviour |
|------|-------|-----------|
| `list_pipelines` | **new** | Return the registry: `{config_name, label, description, scheduler_owned, takes_seed}`. The discovery primitive — layer 3 is self-serve only with this. |
| `start_pipeline` | generalize `start_config_run` | Start any config. Adds `seed_inputs` (multi-file seeds) + repo params (`repo_type/url/path`) so it can start existing-repo/DPE-style runs too. |
| `wait_until_next_checkpoint_or_completion` | **new (half exists)** | Block until the run hits its next checkpoint / completes / fails, then return the compact `_run_pipeline_until_checkpoint` shape. **Bounded**: on `timeout` returns `{status:"running"}` so the driver never hangs. |
| `approve_checkpoint` / `reject_checkpoint` | exist | Decide a pending gate. |
| `get_pipeline_result` | **new** | Compact terminal output of a finished run (parsed JSON if the config's `output_step` produced one). One call, no per-step fetching. |
| `stop_pipeline` | **new** | Cancel a run (`SkillFlow.fail_run(run_id, reason)`); the poller then ignores it. |
| `get_pipeline_status` | exists | Optional mid-run peek; demoted from "the mechanism". |

### The `wait_until_next_checkpoint_or_completion` primitive

This is the one with substance. It unifies both drive models behind one call:

- **Butler-driven config** (`scheduler_owned:false`) → call
  `_run_pipeline_until_checkpoint(run_id)` inline (exists) and return its result.
- **Scheduler-owned config** (`scheduler_owned:true`, DPE) → the poller is
  advancing it in the background; this tool **awaits** (internal bounded loop on
  `sf.get_run(run_id).status`, or an `event_bus` subscription) until status ∈
  {paused, completed, failed} or `timeout`, then reuses the *same* checkpoint-data
  / outputs-gathering code to return the identical shape.

The internal wait lives inside one tool call — it costs **zero driver turns and
zero driver context**. That is the difference from a naive `get_pipeline_status`
polling loop, which would spend a driver turn (and grow the transcript) on every
poll.

### The layer-3 driver loop

The whole of layer 3 collapses to this — no status-polling turns:

```
run_id = start_pipeline(config, seed[, inputs, repo])
while True:
    ev = wait_until_next_checkpoint_or_completion(run_id)   # ONE blocking call
    match ev.status:
        "completed" | "failed" -> return get_pipeline_result(run_id)
        "checkpoint"           -> decide -> approve_checkpoint(run_id)
                                          |  reject_checkpoint(run_id, feedback)
        "running"              -> (timed out) wait again, or go do other work
```

## 5. Routing prompt (templates/coding_mode.md)

Replace the current "Choosing between the loop and a pipeline" section with the
explicit 3-layer ladder:

> - **Layer 1 — inline.** Exploratory / small / discovered-as-you-go: use your
>   direct tools (`read_code_file`, `edit_file`, `bash`, …).
> - **Layer 2 — plan→task runner.** A non-trivial multi-file change the user
>   should approve: `runner_start` → plan → user gate → implement → `runner_submit`.
> - **Layer 3 — offload to a pipeline.** Context-heavy, self-contained work whose
>   *result* is what you need (build an app, audit a subsystem, review a diff, run
>   a captured skill). `list_pipelines` to see what's available, `start_pipeline`,
>   then loop on `wait_until_next_checkpoint_or_completion` — approving/rejecting
>   checkpoints — until it completes, then `get_pipeline_result`. The run's heavy
>   reasoning never enters this session; you only pay for checkpoints (cheap) and
>   the final result.

## 6. Guardrails

- **Compact results.** `get_pipeline_result` reads the config's `output_step`
  terminal file; configs meant for offload should emit a bounded, schema-validated
  JSON (like `code_review`), so the driver gets a map, not a payload. Human-facing
  truncation (2000 chars) does not apply to `get_pipeline_result`'s parse path.
- **Bounded wait.** `wait_until_…` always returns within `timeout` (default ~120s)
  with `status:"running"` rather than hanging on a multi-minute run.
- **Stop is real.** `stop_pipeline` → `fail_run`, after which the poller's
  `get_run_by_project` skips it; an in-flight step is abandoned, which is the
  intended semantics of a cancel.
- **No new async surface.** A run is inline-driven or awaited — there is no
  fire-and-forget handle store to reason about.

## 7. Build order

1. `list_pipelines`, `get_pipeline_result`, `stop_pipeline` — small, independent.
2. `start_pipeline` — generalize `start_config_run` (`seed_inputs` + repo params).
3. `wait_until_next_checkpoint_or_completion` — wrap `_run_pipeline_until_checkpoint`
   for butler-driven; add the awaited path for scheduler-owned.
4. Routing prompt (§5).
5. Tests: registry listing; result parse/compactness; stop→failed; wait returns
   checkpoint for a gated run and completed for a finished one; wait honours timeout.
```
