---
name: aitelier-pipelines
description: Use when building, test-driving or repairing an AItelier pipeline through the mcp__aitelier__* tools — covers the generate → drive → observe → fix loop, what the structural gates do and do not prove, and the failure shapes that only a real run exposes
---

# Driving an AItelier pipeline

AItelier turns a description into a runnable SkillFlow graph: steps, agent roles,
prompts and tools. You do not step it. Its scheduler runs the steps; your job is
to decide at checkpoints, read what a run actually did, and fix what it shows.

**The generator's three gates check that a pipeline is SHAPED right. Only running
it shows whether it WORKS.** Treat a green generation as an untested draft.

## The loop

1. `generate_pipeline("<what it should do>")` → a `run_id`. Scheduler-driven.
2. `wait_for_run` → it pauses at a design review. Read it, then
   `answer_checkpoint` — approve, or reject with feedback and it revises. On
   completion the pipeline appears in `list_pipelines` as `gen_<slug>`.
3. `run_pipeline(gen_<slug>, seed_text=…)` — the test drive. **Do this before
   you believe anything.**
4. `wait_for_run` → `get_run_summary`. Then fix. Then 3 again.
5. `stop_pipeline` a drive going nowhere; `archive_pipeline` an attempt you
   abandon (deleting its files alone leaves a runnable zombie).

`wait_for_run` returns the instant the run settles — at a checkpoint OR at a
failure. It waits at most `timeout_seconds` (default 45) and then returns
`status: "waiting", timed_out: true`. That is not a failure; call it again.

## Reading a failed run

Work outside-in. Each tool answers a different question, and reaching for the
trace first buries you.

| Question | Tool |
|---|---|
| Which step broke, and why | `get_run_summary` → `first_failure` |
| Where exactly, across the whole run | `trace_list(run, errors_only=true)` |
| What the agent was actually told / said | `trace_read(seq)` |
| What a middle step wrote | `get_step_output(run, step)` |

**Quote, do not recount.** Report the tool's own strings — step ids, item names,
the error text. Observed twice on the same task: the model read a
`list_pipelines` result holding 13 entries and reported 14, while the names it
gave from the same call were exactly right. A count you recompute is a claim
about the data; a name you copy is the data.

Two things `get_run_summary` alone will not tell you:

- **A step that ROUTED to a failure gate did not "fail".** `first_failure` is
  `null` and the run's error is `Node 'input_failed' reached`. The reason lives
  in the routing step's own result — `trace_list(errors_only=true)` finds it.
- **Inside a fan-out, the step id is not enough.** A loop body runs once per
  item plus retries, so `t_impl` can appear nine times for six tasks.
  `get_run_summary` names the **item** each instance ran for
  (`{step: t_impl, status: failed, item: health_bar}`); without it you are
  guessing which task broke.

## What the gates do not catch

Every failure below shipped through `skillflow_lint` + `forge_registry_check` +
`forge_dryrun_smoke`, all green. Check these by hand before the first drive.

**The seed is not wired.** A pipeline's runtime input is `seed_text`, written to
`$CONFIG_DIR/_seed/<seed_file>`. Nothing hands that path to a step. An agent step
reads it with `{config: <this pipeline>, output: <seed_file>}`; a tool step takes
it as a `tool_param` (`diff_path: $CONFIG_DIR/_seed/seed_input.md`). Observed: a
review pipeline whose capture step got only `project_root`, so it ran
`git diff HEAD` against an empty throwaway repo and routed to `input_failed`
while the caller's diff sat unread. Confirm with `get_pipeline` that something
names `_seed/`, and that `x-aitelier` declares `seed_file` + `input_hint`.

**A generated tool ignores the input it was given.** The tool was built to a
contract; the graph may never pass it. Read the tool with `get_tool` and the step
that calls it with `get_pipeline`, together — a mismatch is invisible in either
one alone.

**A tool falls back instead of failing.** A tool that quietly tries something
else when its input is missing reports an accurate error about a question nobody
asked, and sends you to look in the wrong place. If an error names something you
did not configure, suspect a fallback.

**The pipeline is structurally fine and the ROLE is wrong.** An empty output, a
verdict in the wrong shape, a reviewer demanding more than one turn can deliver.
`trace_read` on the step shows the prompt it got; `edit_template` is usually the
answer, and it is cheaper than regenerating.

## Fixing

- `edit_template` — a role's prompt. The most common fix by a wide margin.
- `edit_pipeline` — the graph. Read the real schema with `skillflow_docs_search`
  first; do not invent a field.
- `edit_tool` — a generated tool's source.
- `generate_pipeline(edit_target=gen_<slug>, description="the change")` — a
  surgical regeneration when the shape itself is wrong.

After any edit, drive again. A fix you have not re-driven is a guess.

## Before you start

- `list_pipelines` first. Each entry carries an `input_hint` saying what
  `seed_text` it expects; a pipeline fed the wrong seed fails in ways that look
  like bugs.
- Only generated (`gen_*`) pipelines are editable and exportable. Built-ins live
  in the AItelier repo. **A fresh AItelier has no generated pipelines at all**,
  so every `edit_*` correctly refuses until you make one.
- Reads need no credentials. Writes need `AITELIER_ADMIN_TOKEN`; without it the
  write tools answer `denied: …` and change nothing, which is a legitimate
  read-only installation, not a bug.
- No `mcp__aitelier__*` tools at all? The MCP client fails **silently** by
  design. See the plugin README's first section — an agent in that state cannot
  diagnose it from the inside.
