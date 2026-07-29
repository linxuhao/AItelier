# Pipeline-generation UX test — 2026-07-26

Driven end-to-end as a user through the CLI TUI (`debugctl`), butler in `/mode coding`, against the
running container (image at `0a23262`). No code was changed during the test.

## What was run

| # | Request (skill → pipeline) | Forge run | Outcome |
|---|---|---|---|
| R1 | `math_olympiad` — solve IMO/Putnam with a fresh-context adversarial verifier, revise ≤2 rounds, emit calibrated confidence | `forge-math-olympiad-636930` | **completed** in 23 min, incl. one full rewind to `architect`; `gen_math_olympiad` registered |
| R1-drive | test-drive on a real inequality problem | `drive-gen-math-olympiad-f91006` | **completed** — but produced no final answer (S4) |
| R2 | `skill_packager` — expand idea → draft SKILL.md → **deterministic** validation tool → zip packager | `forge-skill-packager-73b840` | **failed** after 25 min ("Cycle limit exceeded") |
| R2b | butler's own retry of the same request | `forge-skill-packager-ea7621` | **failed** identically after 20 min |
| R2-drive | butler registered `gen_skill_packager` by hand and drove it | `drive-gen-skill-packager-ebf506` | **failed**, no reason surfaced |
| R3 | `mcp_server_builder` — spec → scaffold → write tests → **actually run pytest** → README + commit (a code/`repo_mode: code` pipeline) | `forge-mcp-server-builder-2a9ea7` | **failed** after 38 min — 6 identical `v_registry` failures on a role-table format error (S16), each rewinding the whole planning chain |

| R3-recovery | butler, unprompted, hand-registered the failed run's graph and iterated `drive_pipeline` | `drive-gen-mcp-server-builder-{9feaf6,8c7741,a3d963,b50b15}` | **worked** — 4 drives (2 test failures, 1 scaffold failure, then green): 9 passing pytest tests, real `src/`+`tests/`+`README.md` repo |

| R4 | *"edit `gen_math_olympiad` so the success path writes the answer"* — the natural user follow-up to S4 | (no forge run — butler hand-edited) | **worked**: added a `success_answer` step before `done_gate`, kept `final_answer` on the give-up branch, added an end-condition mapping it to `failed`; re-drive `drive-gen-math-olympiad-10d04b` **completed** and wrote `success_answer/final_answer.md` with `Confidence: HIGH` |

**Net: 1 of 4 forge runs completed.** The one that did produced a pipeline that silently drops its own
deliverable. Roughly two hours of wall clock went into three failures, each caused by a small,
deterministic defect that the pipeline could not diagnose or route around.

---

## Severity 1 — correctness

### S1. `register_tool` copies the WRONG tool for every loop item after the first
`~/.AItelier/tools/skill_package/` contains **skill_validate's** code: `tool.yaml` says
`name: skill_validate`, `impl.py` exports `skill_validate`, the test file is `test_skill_validate.py`.
The workspace is correct — `t_tool_impl/skill_package/skill_package/{tool.yaml,impl.py,test_skill_package.py}`.

Cause: `configs/pipeline_forge.yaml:247` passes `source_dir: "$CONFIG_DIR/t_tool_impl"` (no loop item —
loop vars aren't interpolated in `tool_params`), and `_resolve_tool_src`
(`aitelier/tools/register_tool/impl.py:29`) then

1. tries `source_dir/<tool_name>/tool.yaml` — misses, because the implementer writes
   `t_tool_impl/<item>/<tool>/tool.yaml` (item dir **and** tool dir), then
2. falls back to `source_dir.rglob("tool.yaml")`, accepting the first hit where
   `cand.parent.name == tool_name` **or** `(cand.parent / "impl.py").exists()` — the `or` clause matches
   *any* built tool, so tool #1 wins for every subsequent item.

It returns `{"registered": true}` for the bad copy. Detected only two steps later by `v_smoke`:
`skill_package: impl.py must export function 'skill_package'`.

**Fix:** restrict the search to the `source_dir/<tool_name>` subtree and match on the tool.yaml's own
`name:` field rather than "any directory containing impl.py". Then have `register_tool` actually load the
copied tool (import + entrypoint present) and return `registered: false` with the load error if it
doesn't — registering something unloadable is the whole failure.

### S2. A broken registered tool poisons every future run — no self-heal
The retry (`forge-skill-packager-ea7621`) surveyed the live registry, saw `skill_package` already
registered (the bad copy counts), emitted `missing_tools.json = {"tools": []}` and
`tool_plan.execution_order = []` — **skipping the tool-build loop entirely** — then failed at `v_smoke`
with the identical error. No number of retries can fix it — no pipeline path rebuilds an
already-registered name. Recovery is manual: the coding-mode butler eventually repaired
`~/.AItelier/tools/skill_package/` by hand at 09:36, ~45 minutes and two dead runs after the first
failure. Nothing automated would have.

**Fix:** falls out of S1 (never persist an unloadable tool). Additionally, survey/`forge_palette` should
report a registered-but-unloadable tool as *missing* so the loop rebuilds it.

### S3. Gate failures rewind to the wrong step
* `v_registry` failure → **`architect`** (`configs/pipeline_forge.yaml:353`), intended for "unknown tool
  ⇒ may need (re)building". But the checker also emits pure graph-shape violations (terminal-gate rules),
  and those cost a full `architect → architect_review → tool_plan → tool_plan_review → tool_loop →
  emit_graph → emit_review → gates` redo — ~6 agent steps, ~10 min — and throw away an already-reviewed
  graph. **R1 lost 10 minutes to exactly this.**
* `v_smoke` failure → **`emit_graph`** (`configs/pipeline_forge.yaml:366`). But "referenced tool doesn't
  load" is a tool-build defect the emitter cannot fix; R2 burned all 5 retries re-emitting the same graph
  and then died.

**Fix:** route by failure *class*, not by gate. `forge_registry_check` / `forge_dryrun_smoke` should
return a `kind` (`unknown_tool` | `tool_unloadable` | `graph_shape`) and the transitions match on it:
`graph_shape → emit_graph`, `unknown_tool → architect`, `tool_unloadable → tool_loop`.

### S4. A completed generated pipeline can produce no deliverable at all
`gen_math_olympiad`'s success path is `verify(passed) → done_gate (gate, to: null)` — the run ends there.
`final_answer`, the step that writes the answer *and* the calibrated confidence the user asked for, is
reachable **only** from the give-up branch after the revision budget is exhausted. The test-drive confirms
it: run `completed`, outputs = `parse/problem_analysis.md`, `solve/full_solution.md`,
`strip/cleaned_proof.md`, `verify/review_verdict.json` — **no answer, no confidence rating**.

Induced by the terminal-gate convention (S5): pressure to make the completed terminal a bare `gate` with
`to: null` pushed the answer-producing agent off the success path. Nothing checks that the success
terminal is reached *after* the deliverable step.

**Fix:** add a check to `forge_registry_check` — the completed-terminal gate must be immediately preceded
by a step that writes at least one output file. Say it explicitly in `forge_palette` ("the gate
terminates; put the deliverable *before* it"); the convention currently reads as "the gate is the end",
which the maker took literally.

---

## Severity 1 (cont.) — S15. Generated tools live in a flat, global namespace and silently overwrite

`register_tool` writes to `~/.AItelier/tools/<tool_name>/` with `if dest.exists(): shutil.rmtree(dest)`.
R3's architect needed a surgical-edit tool and asked to build one called **`edit_file`** — a legitimate
request (the 49-tool registry has no surgical-edit tool), but a very generic name. That pipeline now owns
`edit_file` system-wide, and the next pipeline that generates its own `edit_file` will silently replace it
— every earlier pipeline referencing the name then runs the new code, with no version, no owner, no
warning. Generated *configs* are namespaced `gen_<slug>` on purpose; generated *tools* are not.

**Fix:** namespace generated tools per pipeline (`<slug>__<tool>`), or record an owner in `tool.yaml` and
refuse to overwrite a tool owned by a different pipeline unless a rebuild is explicitly requested.

---

## Severity 1 (cont.) — S16. Nothing validates the emitted `role_table.yaml`, and the error misleads

R3's `emit_graph` wrote the role table wrapped in a top-level `entries:` key:

```yaml
entries:
  spec_maker: {model: host, template: templates/spec_maker.md, tools: [write, read_file]}
  ...
```

`forge_registry_check` expects roles at the top level, so it reported:

```
Registry check failed — step 'spec': agent_config 'spec_maker' not defined in role table   (×5 roles)
```

All five roles **are** present, one level too deep. The message tells the maker it *forgot* the roles, so
the obvious repair is to write what already exists. With `feedback: true` injecting exactly that message,
every re-emit produced the **identical** `entries:` structure — six times, verified by hand at each
iteration — and because `v_registry` rewinds to `architect` (S3), each attempt re-ran the entire planning
chain. The run died after 38 minutes having never changed the one line that mattered.

`emit_review` passed this graph (`passed: true`, empty feedback) despite its own checklist claiming to
verify "all agent_configs are defined in role_table.yaml" — S5 again, in a second run.

*"Missing `role_table.yaml` validation" was already recorded as a finding in the 2026-07-23 cac40 session.
It is recurring and still unfixed.*

**Fix:** attach a `json_schema` validation to the `emit_graph` step for `role_table.yaml`; accept (or
normalize) an `entries:` wrapper in the loader; and make the registry error say what it actually saw —
"role table defines 0 roles at top level (found key 'entries' with 5 children)".

---

## What the recovery proved (the most important result of the day)

After R3's forge run died, the butler — still in coding mode, without being asked — registered the failed
run's emitted graph by hand, fixed the `entries:` wrapper and the scaffold template (the emitted one used
a stale MCP SDK API; it web-searched and switched to `FastMCP`), and then ran the
**drive → observe → fix → drive** loop four times: two `run_tests` failures, one `scaffold` failure, then
green. Final state: `gen_mcp_server_builder` registered, and a drive run that carried a real repo through
`spec → scaffold → test → run_tests(9 passed) → readme → commit`. It then wrote the user a complete,
accurate summary of the pipeline it had repaired — the best-quality output of the whole session.

**The emitted design was sound.** What killed the forge run was a one-key format error the gates could
neither name nor route around. This is the strongest argument for the S3 + S16 fixes: the creative work
was already correct, and the pipeline had a working repair loop available — it just never got to use it.

---

## Severity 1 (cont.) — S18. A tool step that returns an error still advances to the success terminal

The recovered pipeline's last step is `commit` (`draft_commit`), transitioning unconditionally
`[{to: done_gate}]`. Its trace:

```
commit|draft_commit|{"error": "Source dir not found: .../gen_mcp_server_builder/commit", "files": 0}
```

Nothing was committed — yet the transition carries no `match`, so the engine advanced straight to the
terminal gate and the run reported **completed**. The user's explicit requirement ("write a README and
commit") silently did not happen, and neither the run status, the step list, nor `final_outputs` shows it.
(The commits that *do* exist in the repo are the framework's own per-step promotions.)

**This is a defect in the generated graph, not in the framework.** The engine is right to
hand a tool's result to the transition matcher untouched:

- **Agent-invoked tools must tolerate errors.** In a ReAct loop the agent calls a tool, sees
  `{"error": ...}`, and adapts — that is the loop working. `core/dpe_pipeline.py:718` appends the
  error to `tool_results` and feeds it into the next prompt. Turning that into a step failure
  would break every agent's ability to recover from a bad path or a missing file.
- **A failing tool STEP is a normal, routable outcome too.** `configs/fix_tests.yaml` routes
  `run_tests` → `{passed: true} → done` and `{passed: false} → fix, max_loop: 3`. If the engine
  hard-failed the step on error, that loop could never run: the entire "objective gate, then loop
  back to the maker" pattern — DPE's test gate, every Green/Red pair — depends on a failure being
  something the *graph* decides about.

So the fix belongs where the defect is: the emitted graph gave a fallible tool a single
unconditional edge, and `forge_registry_check` now rejects that shape. Plus the palette entry for
`draft_commit`'s `source_dir` contract, since misreading it is what produced the empty commit.

The only thing arguably worth upstreaming is *observability*, not semantics: trace a warning when a
tool step returns a non-empty `error` and the edge that fired carried no `match`, i.e. the error was
structurally ignored. That cannot break an existing pipeline. The host-side gate already prevents the
shape from being generated, so it is optional.

---

## Severity 1 (cont.) — S19. A butler turn can die server-side without closing the SSE stream, wedging the session

Testing the last untested path (edit mode: *"edit `gen_math_olympiad` so the success path writes the final
answer"*), the session wedged:

| time | evidence |
|---|---|
| 15:06:33 | `POST /api/agent/chat` → 200, stream opened; user message persisted (`chat_history` 5383) |
| 15:06–15:15 | **no** assistant row, **no** tool call, **no** run created, **no** log line |
| 15:14 | container has **0** established outbound :443 connections (checked via `/proc/net/tcp*`) — it is not waiting on the LLM |
| any retry | CLI answers `Agent is still responding, please wait...` and drops the message |

So the turn died server-side, the SSE stream never closed, and `_stream_agent_response`
(`cli/tui/chat.py:1812`, `@work(exclusive=True)` + the `_agent_streaming` guard) waits forever. The
session is permanently unusable with no error anywhere — not in the UI, not in the DB, not in the logs.
Context worth noting: this session's live `token_window` is **106,012 tokens**, i.e. at/over the
coding-mode compaction threshold, which is the most likely place for the turn to have died.

**Fix:**
1. The chat stream must emit a terminal `error`/`done` event from a `finally` so a dead turn always
   releases the client.
2. Log the exception — right now the failure leaves no trace at all in any of the three places a user
   would look.
3. Client-side watchdog: if no event arrives for N seconds, surface "no response — retry?" and re-enable
   input instead of silently swallowing messages.
4. Emit a `status: compacting` event, so the one legitimately long silent phase is visible as progress.

---

## Severity 1 (cont.) — S22. `tool_plan` is the one creative step with no view of the tool registry

`forge_palette` does surface the **global** registry — `loader.list_tools()` over all three roots the
`ToolLoader` scans (`api/dependencies.py:292-305`: skillflow's native tools, `aitelier/tools`, and the
generated `~/.AItelier/tools`) — rendered as ``name — (params) — description[:140]``. Seven of the eight
creative forge steps receive it as a context source.

The exception is the step that decides what to build:

| step | palette? | context |
|---|---|---|
| survey / survey_review | yes | |
| architect / architect_review | yes | |
| **tool_plan** | **no** | `architect/missing_tools.json` **only** |
| **tool_plan_review** | **no** | `missing_tools.json` + `tool_plan` output |
| t_tool_impl, emit_graph, emit_review | yes | |

So once a name lands in `missing_tools.json`, **nothing downstream ever re-checks it against the live
registry**. The planner writes the tool cards, its reviewer judges them against the same blind input, and
`t_tool_impl` (which does hold the palette) is tasked with *building the named tool*, not with questioning
whether it should exist. The only guard is the architect, whose actual job is graph shape.

Two compounding factors:
- Descriptions are truncated to 140 chars in the palette, so "does an existing tool already cover this?"
  is often not answerable from the rendering even when the step can see it.
- `forge_registry_check` verifies that referenced tools **exist**; nothing checks that a newly created one
  **duplicates** an existing one. Combined with S15 (flat namespace + `rmtree` overwrite), a duplicate name
  does not merely waste a build — it silently replaces another pipeline's tool.

**Fix:** add `source: {tool: forge_palette}` to `tool_plan` and `tool_plan_review`, require the tool card to
carry a `why_not_existing` field naming the closest registry match and why it does not fit, and add a
mechanical duplicate check (exact name, then fuzzy name/purpose match against `list_tools()` + schemas) to
`forge_registry_check` so it is a gate, not a matter of prose.

---

## Severity 2 — reviewer vs. gate disagreement

### S5. The Red reviewer treats a hard gate rule as a soft suggestion
R1's `emit_review` round 2 returned `passed: true` with a *suggestion*: "success terminal `final_answer`
is `step_type: agent`, but palette convention 3 recommends the completed terminal be a gate with
`to: null`". `forge_registry_check` then failed on precisely that, as a blocking error ("fail-open
false-green risk"). One full cycle wasted on a rule both sides know.

**Fix:** the reviewer template must mirror the registry-checker's rule list verbatim and mark those rules
BLOCKING. Better: have `emit_review` *call* `forge_registry_check` before writing its verdict, so the two
cannot diverge.

---

## Severity 2 (cont.) — S21. The documented "edit the generated pipeline" workflow is blocked by the file tools

`drive_pipeline`'s own hint tells the agent exactly what to do when a generated pipeline is wrong:

> *"edit the generated config (`~/.AItelier/configs/<config_name>.yaml`), its `.roles.json`, or a
> template, then drive_pipeline again."*

In R4 the butler followed that instruction literally and was refused, twice:

```
read_code_file(project_id="drive-gen-math-olympiad-f91006",
               path="/home/linxuhao/.AItelier/configs/gen_math_olympiad.yaml")   → Path traversal denied
read_code_file(... "/home/linxuhao/.AItelier/configs/gen_math_olympiad.roles.json") → Path traversal denied
```

The coding-mode file tools are jailed to a project's code dir, and generated configs live outside every
project. So the one workflow the system advertises for repairing a generated pipeline cannot be performed
with the file tools at all — the agent has to fall back to `bash`, and only if it thinks of it. The error
message makes this worse: "Path traversal denied" reads as *an attack was blocked*, not *this file is
outside the jail; generated configs live at ~/.AItelier/configs/*.

**Fix:** give the coding-mode file tools a second allowed root for `~/.AItelier/configs` and
`~/.AItelier/tools` (read+write, no traversal above them), or add a dedicated
`edit_generated_pipeline(config_name, file, old, new)` tool. And make the denial name the allowed roots.

---

## Severity 3 — diagnosability (what turned bugs into wasted hours)

### S6. `Cycle limit exceeded` is the only failure reason the user gets
`get_project(forge-skill-packager-73b840)` → `failed:Cycle limit exceeded`. The actionable error
("dry-run smoke failed: … `skill_package`: impl.py must export function 'skill_package'") lives only in
the per-run `trace.db`. Consequence, verbatim from the transcript: the butler concluded *"The forge had a
transient emit failure. Let me retry."* and launched a fresh 20-minute run that failed identically.

Same shape in the drive path: `drive_pipeline` on `gen_skill_packager` returned
`{"drive_status":"failed","verdict":"failed","first_failure":null,"run_error":null,"final_outputs":{}}` —
a "failed" verdict with three nulls.

**Fix:** when a run dies on loop/cycle exhaustion, persist the **last failing gate's error** as the run's
failure reason; have `drive_pipeline` fall back to "last non-passing tool result from the trace" when
`first_failure` is null.

### S7. Completed runs show their terminal gate as `pending`
For the completed `drive-gen-math-olympiad-f91006`:
`parse|completed solve|completed strip|completed verify|completed done_gate|PENDING final_answer|PENDING`.
Gate nodes never get a step-row update, so `drive_pipeline`'s per-step summary (and the Web UI step list)
shows a healthy run as stuck. The butler read this as *"the pipeline's routing is broken: done_gate and
final_answer both show pending"* and spent ~8 tool turns reading skillflow's `core.py` chasing a non-bug.
Combined with S4 it is genuinely ambiguous: "success but no deliverable" and "routing broken" look
identical.

**Fix:** mark the end-condition node `completed`/`skipped` when the run terminates on it, or render gate
nodes distinctly ("terminal gate — reached") instead of `pending`.

### S8. Generated configs carry no `x-aitelier` block
`gen_math_olympiad.yaml` — and all 9 `gen_*` configs — have no `x-aitelier` at all → manifest `label` =
raw config name, `input_hint` empty, `output_step: null`. Consequences: `drive_pipeline`'s `final_outputs`
falls back to `steps[-1]` (which on the success path never runs) and reports empty outputs for a run that
wrote four files; the pipeline catalog lists nine unlabeled, undescribed entries.

**Fix:** emit `x-aitelier: {label, input_hint, output_step}` from `emit_graph`, or synthesize it in
`register_forge_pipeline` (`output_step` = last file-writing step on the success path).

### S9. `derive_repo_mode`'s role-tool signal is dead code
`core/pipeline_registry.py:85-88` looks up `roles[ac.split("__")[-1]]` (bare role name), but
`register_forge_pipeline` builds `roles` with **namespaced** keys (`roles[prefix + bare]`, ~line 315).
The lookup always misses, so a generated pipeline whose agents reach `pytest`/`run_tests`/`repo_apply`
only through their role tool list is classified `repo_mode: none` → repo-less workspace → hard runtime
failure. Precisely the case the deliberately asymmetric derivation exists to prevent. (Latent — today's
two pipelines are genuinely repo-less.)

**Fix:** look up both `roles[ac]` and `roles[bare]`.

---

## Severity 4 — CLI / butler ergonomics

### S10. The CLI silently drops a message typed while the agent is streaming
`cli/tui/chat.py:1020-1025` echoes the message (`_add_message("user", …)`) **and** appends it to
`self.history`, then `_stream_agent_response` returns early at `:1814` with "Agent is still responding,
please wait...". The message is never POSTed. It sits in the transcript looking delivered; the agent never
sees it. My second pipeline request was lost this way.

**Fix:** queue the text and send it when the stream ends (or don't echo it).

### S11. The CLI never surfaces the coding-mode budget pause
`core/meta_agent.py:1621` yields `{"type":"budget_exhausted", message:"Tool-turn budget (50) reached.
Reply 'continue' to keep going."}`. `web/src/views/Chat.svelte:659` handles it; the CLI SSE loop handles
only `tool_call`/`tool_result`/`done`/`error`. Observed: the butler stopped mid-sentence at 08:41:57
("let me check skillflow's match syntax…") with no message at all.

**S10 + S11 together are a trap:** the turn dies silently, the user types the next request into what looks
like an idle prompt, and it vanishes. That is exactly the sequence that happened here.

### S12. Butler run-management tools want `run_id`; the butler only holds `project_id`
* `drive_pipeline` returns `project_id` but no `run_id` → the butler called
  `get_pipeline_result(run_id=<project_id>)` → `Run '…' not found` → 6 tool turns of groping with
  `list_workspace_tree` + `read_workspace_file`.
* `retry_project(project_id=…)` → `Tool 'retry_project' failed: Run not found: forge-skill-packager-73b840`.
* `describe_pipeline(name="skill_packager")` → `No pipeline matches 'skill_packager'` (it registers as
  `gen_skill_packager`; nothing tells the agent the mapping).

**Fix:** return `run_id` from `drive_pipeline`/`generate_pipeline`; accept either id in the `*_project` /
`*_pipeline` tools; resolve a bare name to `gen_<slug>`.

### S13. The design explanation's "How to run it" is invented
`explain/design_explanation.md` tells the user to "place the problem in `task.md` at the pipeline root"
and run `pipeline_forge run math_olympiad --input task.md`. No such CLI exists; the real entry point is
`start_config_run(config_name="gen_math_olympiad", seed_text=…)`, and the seed goes to the config's
`seed_file`.

**Fix:** ground that section of `templates/forge_explain.md` in the actual invocation, templated from the
registered config name.

---

---

## Severity 2 (cont.) — S14. The generated validator and the generated drafter disagreed on the schema

*(listed here after the ergonomics section; same severity as S5/S21)*

`gen_skill_packager`'s `validate` step recorded `passed: false` on all four iterations, exhausting
`max_loop: 3` on `validate → draft` and killing the run. Cause (found by the butler at 09:39, which then
edited the tool): the drafter template writes a `trigger:` frontmatter field, while the generated
`skill_validate` rejects unknown frontmatter fields and did not list `trigger` among the known ones. Two
artifacts produced by the *same* forge run carried contradictory contracts, and `feedback: true` on the
loop edge was not enough for the drafter to converge — it kept writing the field the validator forbade.

(Note: `skill_validate` now passes on that draft only because the butler added `trigger` to the known
fields at 09:39 — the run itself never got there.)

**Fix:** when the tool plan includes a validator for an artifact another step drafts, the emit step must
derive both from one written schema (put the field list in the tool plan and have the drafter template
reference it), and `emit_review` should check drafter-template ↔ validator-tool agreement.

## Coverage gap worth naming

The forge's **edit mode** (`generate_pipeline(edit_target=gen_<slug>)`) is still untested. Asked to fix a
generated pipeline, the butler did not reach for it — it hand-edited the YAML with `bash` and re-drove,
which took about two minutes and worked on the first try. That is a reasonable choice by the agent, but it
means the advertised edit path gets no exercise in practice. Either make the butler prefer it for
structural changes, or accept that hand-edit + `drive_pipeline` *is* the real edit workflow and invest
there (starting with S21, which currently blocks the file tools from touching those files at all).

---

## What worked well (don't regress it)

* Grounding in the live registry: R1's survey plan was a faithful, well-structured translation of the
  skill, including the fresh-context isolation of the verifier.
* The tool-build loop genuinely builds real tools with tests — `skill_validate` works correctly when
  invoked directly.
* Green/Red caught real defects (R1's first emit had both a non-terminal gate and a give-up path sharing
  the success terminal).
* `drive_pipeline` context-isolation worked; the butler recovered from several dead ends on its own.

## Where each finding lives: skillflow framework vs AItelier business logic

| # | Finding | Home | File |
|---|---|---|---|
| S1 | `register_tool` copies the wrong tool for loop item ≥2 | AItelier | `aitelier/tools/register_tool/impl.py` |
| S2 | A broken registered tool poisons every future run | AItelier | same + `forge_palette` |
| S3 | `v_registry` failure rewinds to `architect` | AItelier | `configs/pipeline_forge.yaml` |
| S4 | Completed pipeline produces no deliverable | AItelier | `forge_registry_check` + forge palette/templates |
| S5 | Red reviewer treats a hard gate rule as a suggestion | AItelier | `templates/forge_review_red.md` |
| **S6** | **`Cycle limit exceeded` is the only failure reason** | **skillflow** | `core.py:2950,3407` — `_fail_run_in_tx(run_id, "Cycle limit exceeded")` discards the `CycleLimitExceeded` detail (line 1518 keeps it — inconsistent within the framework) |
| **S7** | Terminal gate shows `pending` on a completed run | **shared** | gates emit no step/trace rows at all (skillflow); rendering is AItelier |
| S8 | Generated configs carry no `x-aitelier` block | AItelier | `core/pipeline_registry.py` + `emit_graph` templates |
| S9 | `derive_repo_mode` role-tool lookup is dead code | AItelier | `core/pipeline_registry.py` |
| S10 | CLI drops a message typed while streaming | AItelier | `cli/tui/chat.py` |
| S11 | CLI never surfaces the budget pause | AItelier | `cli/tui/chat.py` + `core/meta_agent.py` |
| S12 | Butler tools want `run_id`, butler holds `project_id` | AItelier | `core/meta_agent.py` |
| S13 | "How to run it" in the design explanation is invented | AItelier | `templates/forge_explain.md` |
| S14 | Generated validator and drafter disagreed on the schema | AItelier | forge emit templates |
| S15 | Generated tools share a flat global namespace | AItelier | `aitelier/tools/register_tool/impl.py` |
| S16 | No validation of the emitted `role_table.yaml` | AItelier | `forge_registry_check`, `core/pipeline_registry.py`, emit templates |
| S18 | A fallible tool step given an unconditional edge — the engine is right to route rather than fail | AItelier | emitted graph + `forge_registry_check` |
| S18b | `draft_commit`'s `source_dir` contract undocumented | shared | the tool is skillflow's; the palette that should teach it is AItelier's |
| S19 | Dead butler turn never closes the SSE stream | AItelier | chat stream endpoint + `cli/tui/chat.py` |
| S20 | `bash` raises a bare `KeyError` on missing `project_id` | AItelier | `core/meta_agent.py` |
| S21 | File tools jailed out of `~/.AItelier/configs` | AItelier | `core/meta_agent.py` file tools |
| S22 | `tool_plan` cannot see the tool registry | AItelier | `configs/pipeline_forge.yaml` + `forge_registry_check` |

**19 of 22 are AItelier business logic.** skillflow owns **one** outright — S6, where the framework knows
which cycle blew its budget and reports a constant string — plus half of two more (S7's missing gate
status, S18b's undocumented tool contract). Everything else is AItelier's generator design, its host
tools, or its CLI.

An earlier draft of this document also charged the framework with "treating a failed tool as a success".
That was wrong: tolerating a tool's error is correct at BOTH call sites — an agent must see the error and
adapt, and a failing tool *step* is a routable outcome the graph decides about (`fix_tests.yaml` loops a
red `run_tests` back to the fixer). The defect was the generated graph's unconditional edge. See S18.


## Proposed fix order

1. **S1 + S2** — `register_tool` resolution + verify-on-register. One small file; unblocks all multi-tool
   generation and stops registry poisoning. Ship with a test that builds two tools in one loop.
   (The bad `~/.AItelier/tools/skill_package` was already repaired by hand by the butler during the test.)
2. **S6** — carry the last gate error into the run's failure reason. Cheap, and it turns the remaining
   bugs from "mystery" into "one line to read".
3. **S19** — always close the chat stream and log the failure. A dead turn currently costs the user the
   whole session with no error anywhere.

4. **S18** — require a `match` on tool steps that can fail (stops silent false-greens), and document
   `draft_commit`'s source_dir in the palette.

5. **S3** — classify gate failures and route by class; removes both the over-rewind and the
   unfixable-loop dead end.
6. **S5 + S4** — reviewer mirrors the checker; checker requires a deliverable before the terminal gate.
   Fixes the "successful pipeline with no output" class.
7. **S10 + S11** — CLI queue-on-busy and `budget_exhausted` handling. Small, high daily annoyance.
8. **S16** — schema-validate the emitted `role_table.yaml` and make the registry error describe what it
   saw. Cheap, and it is a *recurring* failure (also hit on 2026-07-23).
9. **S15** — namespace or take ownership of generated tools so a generic name can't be silently clobbered.
10. **S14** — one written schema shared by a generated drafter template and its generated validator
   tool; `emit_review` checks they agree.
11. **S8, S12, S13, S7** — metadata, ids, grounded instructions, gate step status. Each independently small.
12. **S9** — one-line lookup fix; do it while touching `pipeline_registry.py` for S8.

---

# Fix plan (verified impacts + regression risks)

## 0. What already exists (checked, not assumed)

| Capability | Backend | HTTP API | Butler tool |
|---|---|---|---|
| Trace read | `sf.get_trace(run_id, step_instance_id=, category=, after_seq=, before_seq=, order=, limit=)` and `sf.trace_query(run_id, sql, params)` | **yes** — `GET /api/runs/{run_id}/trace`, keyset-paginated; `_resolve_run` (`api/run_routers.py:181`) already accepts **either** a skillflow run id **or** a `project_id` (falls back to `list_runs(project_id=…)[0]`, ordered `created_at DESC`) | **none** |
| Config metadata | `ConfigRegistry.describe()` | `GET /api/configs` | `list_pipelines`, `describe_pipeline` |
| Config **content** (YAML / `.roles.json` / templates) | files on disk | **none** | **none** — and `read_code_file`/`edit_file` refuse the paths (S21) |
| Registered tools | `ToolLoader.list_tools()` in-process | **none** | **none** (`forge_palette` exists but only as a pipeline step tool) |

So trace is a *plumbing* gap (API exists, no tool); config-content and tools are *real* gaps at every layer.

## 1. Proposed toolset

Three small read-first families. Every one is read-only except `config_edit`.

### Trace (requires `run` = run_id **or** project_id — reuse `_resolve_run`)
- `trace_list(run, step=None, category=None, errors_only=False, order="desc", limit=50)`
  → compact rows `{seq, step_id, category, event, summary}` with `summary` hard-capped at ~200 chars.
- `trace_search(run, query, step=None, limit=30)` → same shape, rows whose payload matches.
- `trace_read(run, seq, seq_end=None)` → full payloads for a small explicit range.

Implementation notes: `trace_list` maps onto `sf.get_trace` directly. Filtering by **`step_id`** (a string
the agent actually knows) and text search are **not** supported by `get_trace` — it only takes
`step_instance_id: int` — so both go through `sf.trace_query` with a **server-built parameterized**
statement. Never accept model-supplied SQL.

### Config
- `config_read(config_name, file=None)` — `file` defaults to the YAML; also serves `<slug>.roles.json`
  and `templates/<name>.md`. Resolved through the registry, then jailed to the two known roots.
- `config_search(query)` — grep across config YAMLs; answers "which pipeline uses tool X / has step Y".
- `config_edit(config_name, file, old_str, new_str)` — the missing half of S21. It should call
  `reload_generated_pipeline` on success, so the edit→drive loop can't silently run stale bytes.

### Tools — resolve through the loader, never a hardcoded directory
- `tool_list(filter=None)` — every registered tool: name, params, purpose, **root** (skillflow-native /
  aitelier / generated) and **owner** (which pipeline generated it).
- `tool_read(name, file=None)` — `tool.yaml` / `impl.py` / tests of any registered tool, resolved by name
  across all roots.
- `tool_search(query)` — match over name, description and params.

**All three resolve via `loader.list_tools()` / `loader.load_schema()` / the loader's own path resolution**,
which already spans the three roots wired in `api/dependencies.py:292-305` — skillflow's native
`skillflow/tools`, `aitelier/tools`, and the generated `~/.AItelier/tools`. Nothing here may hardcode a
directory: a fourth root added later must light up in the reader tools for free.

**Coherence with the writer is a hard requirement, not a nicety.** There are three views of the same
registry — `register_tool` (writes), `forge_palette` (shows it to forge agents), and these reader tools
(show it to the butler). They must share **one** resolution helper and **one** ownership field
(`x-generated-by` in `tool.yaml`, per fix 1.1), or they drift — and every drift between "what exists",
"what the generator thinks exists", and "what the fixer can see" is a duplicate tool or a silent
overwrite (S15, S22). Concretely: extract the lookup into one module that all three import, and have
`forge_palette` render from `tool_list`'s output rather than re-deriving it.

## 2. Phases

### Phase 1 — stop the bleeding (AItelier only, no skillflow release)

**1.1 `register_tool` resolves by declared name (S1, S2, S15)** — `aitelier/tools/register_tool/impl.py`
- `_resolve_tool_src`: keep `source_dir/<tool_name>` first; for the flat and `rglob` fallbacks, read each
  candidate's `tool.yaml` and require `name == tool_name`. Drop the
  `or (cand.parent / "impl.py").exists()` disjunct — that clause is what makes item ≥2 match item 1.
- After `copytree`, load the fn; on failure `rmtree` the dest and return an error (never leave an
  unloadable tool behind).
- Ownership: write `x-generated-by: <config_name>` into the copied `tool.yaml`; refuse to overwrite a tool
  owned by a *different* pipeline unless `force=true`.
- **Regression risks:** (a) a `tool.yaml` with no `name` field, or `name` ≠ dir name, would newly fail —
  mitigate by accepting a *single unambiguous* candidate when `name` is absent, and only hard-failing when
  ≥2 candidates exist and none matches; (b) the single-tool path **works today** (R3 registered `edit_file`
  correctly) and must stay working; (c) verify-on-import will now reject tools that import optional deps at
  module scope — that is the intent, but it turns a late failure into an early one, so the loop's
  `t_tool_review` must be able to see the error and retry.
- **Coverage:** there is **no `tests/unit/test_register_tool.py` at all**. Add one, and make its first case
  the two-item loop.

**1.2 Trace toolset (S6 in practice, S12)** — `core/meta_agent.py`
- Add the three trace tools above; reuse `_resolve_run` so `project_id` works everywhere.
- Also switch `get_pipeline_result` / `stop_pipeline` to that resolver (they call `sf.get_run(run_id)`
  directly today, which is why passing a `project_id` returns "Run not found"), and return `run_id` from
  `drive_pipeline`/`generate_pipeline`.
- **Regression risks:** context blowup — trace payloads carry whole prompts/responses. Default to
  `order=desc, limit=50`, cap each row's summary, and require an explicit `trace_read` for full text.
  `trace_search` is a `LIKE` scan over a per-project SQLite table (a few MB) — acceptable with a hard limit.

**1.3 Failure reason survives the run (S6)** — host-side, no framework change
- On a failed run, the completion hook reads the last error-bearing trace row and persists it as the
  project's failure reason. skillflow's `core.py:2950,3407` mint a bare `"Cycle limit exceeded"`; fixing it
  there needs a PyPI release + pin bump + image rebuild (the container installs skillflow from PyPI only),
  so compensate host-side first and upstream the detail separately.

**1.4 `bash` argument handling (S20)** — one line, `core/meta_agent.py:2671`: `.get()` + a real message.

### Phase 2 — make the forge converge

**2.1 Route registry failures by class (S3)** — `aitelier/tools/forge_registry_check`, `configs/pipeline_forge.yaml`
- Return a `class` field: `unknown_tool` → `architect`; `graph_shape` / `role_table` → `emit_graph`.
- **Regression risks:** splitting one edge into three gives each its **own** `max_loop` budget, so the
  worst-case attempt count rises — keep `max_total_steps: 200` as the backstop. Edge order matters
  (`passed: true` first, then class edges, then a catch-all to `architect`). `match` on a tool step reads
  the tool's own result dict (skillflow `core.py:2940-2955` sets `step_flags = tool_result`), so a new key
  is matchable without framework changes. `tests/unit/test_forge_registry_check.py` asserts on **substrings
  of `error`/`violations`**, never exact dicts, so adding a field is additive.

**2.2 Validate the emitted `role_table.yaml` (S16)**
- Attach a `json_schema` validation to `emit_graph` for `role_table.yaml`; accept/normalize an `entries:`
  wrapper in the loader; change the registry error to describe what it saw ("0 roles at top level; found
  key 'entries' with 5 children") instead of "agent_config X not defined".
- **Regression risk:** none for existing configs — today an `entries:`-wrapped table is simply broken.

**2.3 Deliverable-before-terminal + fallible tool steps (S4, S18b)** — `forge_registry_check`
- Require that the completed-terminal gate is immediately preceded by a step that writes an output.
- Require a `match` on any tool step whose tool can return `error`/`passed` (that is exactly the `commit`
  step that "succeeded" while committing nothing), and put `draft_commit`'s `source_dir` contract in
  `forge_palette`.
- **Regression risk:** stricter gate ⇒ some previously-passing graphs now fail. That is the point, but it
  makes 2.1 a prerequisite — otherwise every new violation costs a full rewind to `architect`.

**2.35 Ground `tool_plan` and add a duplicate gate (S22)** — `configs/pipeline_forge.yaml`, `forge_registry_check`
- Add `source: {tool: forge_palette}` to `tool_plan` **and** `tool_plan_review` — they are the only creative
  steps without it, and they are the ones deciding what gets built.
- Require each tool card to carry `why_not_existing`: the closest registry match and why it does not fit.
- Add a mechanical duplicate check to `forge_registry_check` (exact name, then fuzzy name/purpose against
  `list_tools()` + schemas) so "don't duplicate" is a gate rather than prose in a prompt.
- **Regression risks:** (a) the palette is a sizeable blob — adding it to two more steps raises prompt cost
  on every forge run; it is already on seven steps, so the marginal cost is known and small relative to a
  wasted tool build. (b) A fuzzy duplicate check will produce false positives; make the failure *advisory
  with a required justification* (the card's `why_not_existing`) rather than a hard block, so a genuinely
  new tool with an unlucky name can still be built. (c) Descriptions are truncated to 140 chars in the
  palette — raise that cap for the tool-planning steps, or the grounding is present but unreadable.

**2.4 Reviewer mirrors the checker (S5, S14)** — `templates/forge_emit_review.md`: state that the terminal
  and role-table rules are **blocking**, not advisory, and have `emit_review` run the same checklist the
  gate enforces.

### Phase 3 — ergonomics and metadata

- **S21** — allowlist `~/.AItelier/configs` and `~/.AItelier/tools` as extra roots for the file tools, plus
  `config_edit`. **Security-relevant**: this container runs LLM-generated code, so implement it as an
  explicit second root with `realpath` containment (no symlink escape, no traversal above the root), not by
  loosening the existing check — `tests/unit/test_meta_agent.py::test_read_workspace_file_traversal` and
  `::test_read_code_file_traversal` must stay green.
- **S8, S9** — synthesize `x-aitelier` (label/input_hint/output_step) at **registration**, so already-
  generated configs get it too; merge rather than replace an emitted block. Fix `derive_repo_mode` to look
  up both `roles[ac]` and `roles[bare]`.
  **Verified regression trap:** `tests/unit/test_pipeline_registry.py::test_agent_reaching_the_repo_through_its_role_tools_forces_code_mode`
  **passes today while the production path is dead** — its `_graph_with` helper builds a graph with a
  *bare* `agent_config: processor` and a *bare* roles key, whereas `register_forge_pipeline` namespaces
  both (`gen_x__processor`) and `derive_repo_mode` strips the prefix before the lookup. The test must be
  made faithful (namespaced graph **and** namespaced role keys) or the bug regresses invisibly.
- **S19** — emit a terminal `error`/`done` SSE event from a `finally`, log the exception, and add a client
  watchdog. **Regression risk:** double-terminal events — make the client's handler idempotent; and the
  watchdog should *warn plus offer cancel*, never auto-abort a legitimately long turn.
- **S10, S11, S13, S7, S12** — queue-on-busy, surface the budget pause, ground the "how to run it" text,
  render terminal gates as completed.

### Phase 4 — upstream to skillflow (needs a release + pin bump + image rebuild)

- **S6** — carry the `CycleLimitExceeded` detail into the run error at `core.py:2950,3407` (line 1518
  already does; the framework is inconsistent with itself).
- **Optional, observability only** — trace a warning when a tool step returns a non-empty `error` and the
  edge that fired carried no `match` (the error was structurally ignored). NOT a semantics change: see
  S18 for why tolerating tool errors is correct in both the agent loop and the graph.
- **S7** — record a status for end-condition/gate nodes so a completed run stops showing its terminal as
  `pending`.


---

# Applied — 2026-07-26 (branch `fix/pipeline-generation-ux`)

Phases 1–3 are implemented. Phase 4 (skillflow) is not: it needs a PyPI release, a pin bump and an image
rebuild, and after review it amounts to one real item (S6's discarded cycle detail) plus an optional trace
warning — the tool-error semantics change originally proposed there was wrong and is withdrawn (see S18).

| Fix | Where | Verified by |
|---|---|---|
| **S1, S2, S15** — resolve a tool by its own identity; stage → verify → swap; provenance + loud replace | `aitelier/tools/register_tool/impl.py` | new `tests/unit/test_register_tool.py` (15 tests); the two-item loop case fails without the fix |
| **S6, S12** — trace/registry/config tools for the butler; `_resolve_run_row` accepts run **or** project id; `run_id` returned from drive/generate | `core/meta_agent.py` | `tests/unit/test_meta_agent_diagnosis.py` (31 tests) + live: `trace_list(errors_only)` on the real failed run returns the gate error |
| **S6** — a failed run's status carries the last failing gate's error, not "Cycle limit exceeded" | `core/scheduler.py` | `tests/unit/test_scheduler_failure_reason.py` (7) + live: *"Cycle limit exceeded — v_registry: … 'spec_maker' not defined in role table"* |
| **S3** — registry failures route by class (`unknown_tool` → architect, `emit_fixable` → emit_graph) | `forge_registry_check`, `configs/pipeline_forge.yaml` | gate tests; forge_lint clean; live re-run against R3's files classes `emit_fixable` |
| **S16** — one shared `normalize_role_table`; the error describes what it saw | `core/pipeline_registry.py`, `forge_registry_check` | registry + gate tests; R3's real `entries:`-wrapped table now resolves |
| **S4, S18b** — deliverable-before-terminal + fallible tool steps must route failure; `draft_commit`'s contract in the palette | `forge_registry_check`, `forge_palette` | gate tests; the new gate catches R3's unrouted `commit` that the old one passed |
| **S22** — palette on `tool_plan` + `tool_plan_review`; `why_not_existing` required per tool card | `configs/pipeline_forge.yaml`, `templates/forge_tool_plan.md` | config parses + lints; both steps now carry the palette |
| **S5, S14** — the four gate rules are declared BLOCKING for the Red reviewer | `templates/forge_review_red.md` | — |
| **S8** — `output_step` + human `label` derived at registration | `core/pipeline_registry.py` | tests + live: all 11 existing `gen_*` pipelines gained both on the boot rescan, no regeneration |
| **S9** — role lookup tries namespaced **and** bare | `core/pipeline_registry.py` | new faithful test fails without the fix (the old one passed while production was dead) |
| **S13** — the explain template is grounded in `start_config_run`, with the invented CLI called out | `templates/forge_explain.md` | — |
| **S10, S11** — a message typed mid-turn is queued, not dropped; the budget pause is surfaced | `cli/tui/chat.py` | — |
| **S19** — per-event idle guard closes a hung stream; client watchdog warns then releases input | `api/agent_routers.py`, `cli/tui/chat.py` | — |
| **S20** — `bash` names its missing argument | `core/meta_agent.py` | — |
| **S21** — `config_read` / `config_search` / `config_edit` (edit reloads the pipeline) | `core/meta_agent.py` | diagnosis tests incl. refusing built-ins and ambiguous matches |

**Suite: 1094 passed, 9 skipped** (from 985 before; +53 unit tests added).

### Deliberately not done
- **S7** (terminal gates render as `pending`) — the status belongs to skillflow's step table; the host-side
  render-only workaround would paper over it.
- **S18a as originally written** — "make the framework treat a tool's `error` as a step failure" —
  is **withdrawn**. It would break agent ReAct loops (which must see and adapt to tool errors) and every
  gated fix-loop (`fix_tests.yaml` routes a red `run_tests` back to the fixer; hard-failing would kill
  the run instead). The real defect was the generated graph's unconditional edge, and that is fixed.
- Widening `edit_file`'s jail. `config_edit` covers the documented workflow with a narrower blast radius,
  and the two existing traversal tests stay meaningful.


---

## Self-review follow-up (same day)

A careful pass over the applied diff found 8 defects; all are fixed, with tests.

| # | Defect | Fix |
|---|---|---|
| R1 | `_declared_outputs` read only the long `fixed: {k: {file: …}}` form, so the shorthand `fixed: {k: "x.md"}` — used by pipeline_forge's own `survey` step — looked like "writes nothing" and a **correct** graph was rejected for having no deliverable | one canonical `declared_output_files()` in `core/pipeline_registry.py`, accepting both forms and both graph shapes; the gate imports it |
| R2 | The queued chat message was started and then immediately cancelled: `_reload_session_history` is also `@work(exclusive=True)` on the **default** group | the stream worker gets its own `group="agent-stream"`; the flush runs from a timer, not from a possibly-cancelled task |
| R3 | `_failure_reason` ran an unindexed `LIKE` scan of the whole trace table on **every** poll tick for an already-failed project | memoized per run id (a failed run is terminal), bounded cache |
| R4 | Same shorthand blind spot in `_writes_a_result` → `output_step` pointed at the reviewer instead of the writer | shares `declared_output_files()` |
| R5 | The stall watchdog flipped `_agent_streaming` behind the worker's back, permanently disarming itself | it now cancels the worker group; the worker's `finally` is the single owner of the flag |
| R6 | `_FALLIBLE_PREFIXES` contained `test_`, matching the real `test_write` tool (a writer, not a check) | prefix removed, with the reason recorded |
| R7 | The role-table wrapper note was discarded whenever the check passed | returned as a non-blocking `notes` field |
| R8 | `config_edit` on a generated pipeline's template answered "<name> is a built-in config" | the message now distinguishes the two cases and points at `.roles.json` |

Also fixed while re-reading, though it predates this branch: typing a second message mid-turn **cancelled the
running turn**, because `_stream_agent_response` is exclusive and starting it cancels the group before the
in-body guard can run. The guard now lives in `on_input_submitted`, so the running turn survives and the
message really is queued.

**Suite: 1114 passed, 9 skipped.** Re-verified on real data: the shorthand graph now passes; R3's actual
emitted files still fail — for the one real reason (`draft_commit` unrouted), classed `emit_fixable`, with
the wrapper note attached; `output_step` still resolves to `success_answer` / `commit` / `persist_positions`
on the live generated configs.

The common cause of R1 and R4: both output-inspecting helpers were written against the long form only, and
the tests used the long form too — so the suite stayed green over a real defect. The parametrized tests now
cover shorthand, long form and mixed.


---

# Round 2 — same three skills, re-run after the fixes (2026-07-26 17:15–18:55)

Clean-slate protocol: the three generated configs and the three tools they had created were moved to a
backup dir, their rows deleted from `skillflow_graphs`, and the backend restarted. The user's eight
pre-existing `gen_*` pipelines were left untouched. Same prompts, same policy (act as a user; intervene
only on a hard block).

## Result

| Skill | Round 1 | Round 2 |
|---|---|---|
| `math_olympiad` | completed, **1 full rewind to `architect`**, 23 min; the answer step stranded on the give-up branch | **completed, ZERO rework, 13 min**; answer on the success path |
| `skill_packager` | **failed twice**, ~45 min, no pipeline produced | **completed + registered**, 1 cheap rework hop, ~23 min |
| `mcp_server_builder` | **failed**, 6 identical phantom rewinds on the `entries:` role table, 38 min | **still failed** (55 min) — but on `v_smoke` after `v_lint`+`v_registry` passed every round, with **zero rewinds to `architect`** and each round attacking a genuinely different real defect. The round-1 blocker (S16) is gone; what remains is T8, an unactionable smoke error |

### What the fixes demonstrably did

* **S1 (`register_tool`)** — the decisive test. The architect again asked for the same two tools
  (`skill_validate`, `skill_package`) in a single fan-out wave. Both registered with their **own** code
  (132 and 58 lines, each exporting its own function) and their own `x-generated-by` provenance. In round 1
  this exact pair produced the mis-copy that failed the run twice.
* **S16 (role table)** — `mcp_server_builder`'s emitted `role_table.yaml` came out flat and
  **`v_registry` passed**. That single defect caused all six of round 1's rewinds.
* **S3 (routing by class)** — across both round-2 runs, every rework stayed local (`emit_graph` ↔
  `emit_review`, `v_smoke` → `emit_graph`). Not one rewind to `architect`.
* **S5 + S18 (reviewer mirrors the checker)** — `emit_review` blocked twice on unrouted fallible tools,
  once on a success path that never committed its deliverable, and when it finally passed it enumerated
  the four blocking checks by name. In round 1 this defect class sailed through review into production.
* **S22 (grounded `tool_plan`)** — every tool card carried a real `why_not_existing` naming actual palette
  tools. `mcp_server_builder`'s architect asked for **no** new tools at all this round (round 1 wanted to
  build a generically-named `edit_file`, claiming that name globally).
* **S13** — the explain now prints the real `start_config_run(...)` invocation, not the invented CLI.
* **S8** — both new pipelines registered with human labels and a correct `output_step`
  (`final_answer`, `skill_package`).

## New issues found in round 2

* **T1 — a generated pipeline cannot be deleted.** skillflow exposes `register_graph`/`list_graphs` but no
  delete, and AItelier's registry has no remove either. Deleting the persisted YAML leaves a **zombie**:
  still listed in `/api/configs` and still runnable after a restart, but with no source file, so
  `config_read` / `reload_generated_pipeline` / `config_edit` all fail on it. A clean slate required
  `DELETE FROM skillflow_graphs` by hand. The catalog can only grow.
* **T2 — one request, two forge runs.** A single `generate_pipeline` produced two runs 6s apart.
* **T3 — a run that dies in `create_run` is invisible.** It sits at `status: planning` forever; the
  exception is only in the container log, and there is no trace to read because the run never started.
  The S6 fix does not cover this path — it enriches *failed* runs, and this run is never marked failed.
* **T4 — two `max_loop` edges on the same (from, to) pair make a config un-runnable** (my own regression,
  fixed). `create_run` inserts one `skillflow_edge_counts` row per max_loop edge, UNIQUE on
  (run_id, from_step, to_step). The YAML parses and `forge_lint` passes clean. Note the invariant is
  precisely about **max_loop** edges: `meta_conversation` has had two `intent_detect → gather` edges for
  20 runs. Guard added: `tests/unit/test_config_graph_integrity.py`.
* **T5 — the requested abstain outcome is unreachable.** The user asked for an answer "allowed to say
  'no confident solution'". The generated graph loops `verify → solve` (max_loop 2) and, when that budget
  is exhausted, has no edge left: the run dies with "no matching transition" and produces nothing. Round 1
  had the mirror-image bug. Nothing checks that a bounded loop's exhaustion path produces an output.
* **T6 — the Design Review checkpoint has no working rejection path.** `explain` sets `checkpoint: true`
  but no `checkpoint_reject_to`, and its only transition matches `approved`. A rejection therefore re-runs
  `explain` — which only writes prose — so "Request Changes" rewrites the *explanation* while the graph,
  roles and templates stay exactly as they were. `novel_init`/`novel_chapter` do this correctly. Fix:
  `checkpoint_reject_to: emit_graph`.
* **T7 — `tool_plan_review` can falsely reject a legitimately EMPTY tool plan**, claiming "No preceding
  maker step output found in context" when `tool_tasks_manifest.json` says `{"execution_order": []}`.
  Possibly caused by my own S22 change, which put the large palette blob FIRST in that reviewer's context,
  ahead of the maker's 27-byte output. Intermittent (an identical empty plan passed in the other run).
  Mitigation: put the palette after the maker output, and state in the template that an empty plan is valid.
* **T9 — the S6 enrichment is written correctly and then discarded by the API read path.**
  `mcp_server_builder` exhausted its smoke-loop budget and failed. The write side is fine:
  `scheduler.py:954` stores `status = f"failed:{_failure_reason(run)[:160]}"`, and the DB really holds
  `failed:Cycle limit exceeded — v_smoke: dry-run smoke failed (status=failed). Trail: [...]`
  (verified by reading `runs.status` directly). The dashboard still shows a bare `failed` because
  `api/dependencies.py:363` (`enrich_project_status`) overwrites it on every read:

  ```python
  run_status = run["status"]                      # skillflow's raw status: "failed"
  if run_status == "running" and run.get("current_node"):
      project["status"] = f"running:{run['current_node']}"
  else:
      project["status"] = run_status              # ← clobbers "failed:<reason>"
  ```

  Only the `running` case preserves a refined status, though the AT-15 comment directly above states the
  intent generally ("preserve the DB's enriched status … only fall back to `run["status"]` if the DB
  column hasn't been synced yet"). Fix: keep the DB value when it is a refinement of the raw status
  (`db_status.split(":", 1)[0] == run_status`), otherwise adopt `run_status`.
  *(An earlier reading of this — "no sync runs after a run is marked failed, so the enrichment never gets
  written" — was wrong; the enriched value is in the DB. The scheduler needs no change here.)*

* **T8 — `forge_dryrun_smoke`'s failure message is not actionable.** The whole payload is
  `{"passed": false, "error": "dry-run smoke failed (status=failed). Trail: [<step ids>]"}` — where it
  stopped, never why. No unmatched transition, no missing flag. `mcp_server_builder` burned several emit
  rounds on it, while `v_registry` failures (which name the exact violation) get fixed in one hop. Same
  lesson as S6, one layer down.


---

# Round-2 fix plan (mechanisms verified, not inferred)

Ordered by user impact per unit of work. T4 is already fixed and guarded.

## Phase 1 — make failures legible (small, high leverage)

### 1.1 T9 — the API discards the enriched failure status *(corrected diagnosis)*
`api/dependencies.py:363` in `enrich_project_status`:

```python
run_status = run["status"]
if run_status == "running" and run.get("current_node"):
    project["status"] = f"running:{run['current_node']}"
else:
    project["status"] = run_status          # ← clobbers "failed:<reason>"
```

The scheduler's write is correct and **is** in the DB (verified:
`failed:Cycle limit exceeded — v_smoke: dry-run smoke failed…`). The read path throws it away for every
status except `running`, contradicting its own AT-15 comment ("preserve the DB's enriched status").

**Fix:** keep the DB value whenever it is a refinement of the raw status — i.e. when
`project["status"].split(":", 1)[0] == run_status`, leave it alone; otherwise adopt `run_status`.
**Regression risk:** a *stale* DB status could now survive (e.g. DB says `failed:old` while skillflow has
been reactivated to `running`). The prefix check prevents exactly that: the prefixes differ, so the raw
status wins. Add a test for both directions.

### 1.2 T8 — `forge_dryrun_smoke` throws away the reason it already has
`aitelier/tools/forge_dryrun_smoke/impl.py:57` returns `{"status": run.get("status"), …}` and drops
`run["error_reason"]`; the assembly at :166 then falls back to
`f"dry-run smoke failed (status={status}). Trail: {trail}"`. The tool already writes good messages for
`max_steps`, `checkpoint_loop` and unresolved tools — the `failed` branch is the one that says nothing,
and it is the branch `mcp_server_builder` hit every round.

**Fix:** carry `run.get("error_reason")` (and the last step id) out of `_drive`, and include it in the
message: *"dry-run smoke failed at `repo_apply_code`: no transition matched flags {applied: true}"*.
**Regression risk:** none — additive to a message. **Impact:** this is the single change most likely to
turn round-2's `mcp_server_builder` failure into a pass.

### 1.3 T3 — a run that dies in `create_run` is invisible
The scheduler's tick lets the exception escape to APScheduler; the project stays at
`status: planning, step: 1` forever, with nothing in the trace (the run never started).

**Fix:** wrap `_get_or_create_skillflow_run` in the tick; on exception write
`status = f"failed:could not start run — {e}"` and log it. **Regression risk:** low; it only adds a
terminal state where today there is an infinite "planning".

## Phase 2 — forge convergence

### 2.1 T7 — palette ordering in `tool_plan_review` *(likely my own regression)*
S22 put `- source: {tool: forge_palette}` **first** in `tool_plan_review`'s context, ahead of the maker's
27-byte manifest; the reviewer then claimed "No preceding maker step output found". Intermittent — an
identical empty plan passed in the other run.

**Fix:** move the palette *after* the maker output in `tool_plan_review` (keep it first in `tool_plan`,
which needs it to decide), and state in `templates/forge_review_red.md` that
`{"execution_order": []}` is a VALID plan meaning "no new tools needed".
**Regression risk:** none structurally; it is a context-ordering change.

### 2.2 T5 — a bounded loop's give-up path must still produce something
The user asked for an answer "allowed to say 'no confident solution'". The emitted graph loops
`verify → solve` (max_loop 2) with no edge left when the budget is exhausted, so the run dies with
"no matching transition" and produces nothing. Round 1 had the mirror-image bug.

**Fix:** add a `forge_registry_check` rule — for every edge carrying `max_loop`, the source step must have
at least one *other* outgoing edge (the give-up path), and that path must reach a step that writes an
output before its terminal. Add the rule to `forge_palette` and to the reviewer's blocking list.
**Regression risk:** stricter gate ⇒ more emit rounds. Mitigated because 2.1/1.2 make each round cheaper
and because the routing fix (S3) keeps rework local. Ship it *after* 1.2, not before.

### 2.3 T6 — the Design Review checkpoint cannot actually reject
`explain` sets `checkpoint: true` with no `checkpoint_reject_to`, and its only transition matches
`approved`. Rejection therefore re-runs `explain`, which only writes prose — so "Request Changes" rewrites
the *explanation* and leaves the graph untouched.

**Fix:** `checkpoint_reject_to: "emit_graph"` on `explain` (the emitter is what the feedback is about),
and add a `{to: emit_graph, match: {from: checkpoint, value: rejected}}` edge.
**Regression risk:** a rejection now re-enters the emit→gates cycle, consuming its `max_loop` budget —
acceptable, and the alternative is an affordance that does nothing. `novel_init`/`novel_chapter` are the
working precedent.

## Phase 3 — lifecycle

### 3.1 T2 — one request, two forge runs
Verified: the butler issued two `generate_pipeline` calls 6s apart.
**Fix:** make `_tool_generate_pipeline` idempotent — before launching, look for a non-terminal run of
`pipeline_forge` whose project id matches `forge-<slug>-*` and return that one with a note.
**Regression risk:** a deliberate re-generation while one is in flight would be refused; return the
existing run id and let the caller stop it explicitly.

### 3.2 T1 — generated pipelines cannot be deleted
`ConfigRegistry` builds `_manifests` from `sf.list_graphs()` (`core/config_registry.py:249`), so deleting
the YAML leaves a runnable zombie whose source is gone. skillflow exposes no delete.

**Fix (two tiers):**
* `archive_generated_pipeline(name)` — AItelier-side, reversible: move the YAML/roles to
  `~/.AItelier/configs/_archived/` and record the name in a persisted exclusion list that
  `load_generated_configs`/`catalog()` consult. The graph stays in skillflow but disappears from the
  catalog and from `list_pipelines`.
* `purge=true` — additionally delete the `skillflow_graphs` row. Documented as the hard delete, since it
  is reaching into another component's store.

**Regression risk:** the exclusion list must be consulted everywhere the catalog is built, or an archived
pipeline reappears on the next boot scan. Add a test that boots a registry with an archived name present
on disk and asserts it stays out.

---

**Suggested order:** 1.2 → 1.1 → 2.1 → 2.3 → 1.3 → 2.2 → 3.1 → 3.2. The first two make every remaining
failure self-explaining, which is what made round 1's debugging expensive; 2.2 (a stricter gate) goes last
among the forge changes so it lands on top of cheaper rework, not before it.

---

# Applied — round-2 plan, 2026-07-27 (branch `fix/pipeline-generation-ux`)

All eight items shipped, in the planned order. Full suite: **1188 passed, 9 skipped**.

| # | Change | Where |
|---|--------|-------|
| 1.2 T8 | `_drive` carries `run["error_reason"]` out; the `failed` branch quotes it verbatim and names the last step. Message went from `dry-run smoke failed (status=failed). Trail: [...]` to `dry-run smoke failed after 'check' (status=failed). Reason: No matching transition from 'check' with flags {'passed': True, ...}. Trail: [...]` | `aitelier/tools/forge_dryrun_smoke/impl.py` (+ `tool.yaml`), `tests/unit/test_forge_dryrun_smoke.py` (new — the tool had **no** tests) |
| 1.1 T9 | Keep the DB status when its prefix matches skillflow's raw status; a prefix mismatch means stale, so skillflow wins | `api/dependencies.py`, `tests/unit/test_enrich_project_status.py` (new, 6 cases incl. both stale directions) |
| 2.1 T7 | Maker output moved ahead of the palette in `tool_plan_review`; reviewer told an empty `execution_order` is valid | `configs/pipeline_forge.yaml`, `templates/forge_review_red.md` |
| 2.3 T6 | `checkpoint_reject_to: emit_graph` on `explain`; label says what reject does | `configs/pipeline_forge.yaml` |
| 1.3 T3 | The tick catches a `create_run` exception and writes `failed:could not start run — …` | `core/scheduler.py`, `tests/unit/test_scheduler_failure_reason.py` (+2) |
| 2.2 T5 | New gate rule: a terminal step no end condition names | `aitelier/tools/forge_registry_check/impl.py`, `forge_palette`, `forge_review_red.md`, `tests/unit/test_forge_registry_check.py` (+3) |
| 3.1 T2 | `generate_pipeline` relays an in-flight `forge-<slug>-<hex>` run instead of launching a second | `core/meta_agent.py`, `tests/unit/test_generate_pipeline_idempotent.py` (new, 8) |
| 3.2 T1 | `archive_generated_pipeline(…, purge=)` + exclusion list honored in `ConfigRegistry.build`; `archive_pipeline` butler tool (coding-mode) | `core/pipeline_registry.py`, `core/config_registry.py`, `core/meta_agent.py`, `tests/unit/test_archive_generated_pipeline.py` (new, 9) |

## Where the plan's own reasoning changed under verification

* **T5's rule is narrower and sharper than planned.** The plan said "every `max_loop`
  edge's source must have another outgoing edge (the give-up path) reaching a step that
  writes an output". Reading the actual round-1 artifact killed that formulation: the
  give-up edge **did** exist (`verify → final_answer`, unconditional) and `final_answer`
  **did** write `final_answer.md`. The defect was one step further on — `final_answer`
  declared `to: null` while no end condition named it, so the run reached it, wrote the
  answer, and died with no matching transition. The shipped rule is therefore *a terminal
  step must be named by an end condition*, which is mechanical and, audited across all 4
  shipped configs + 6 live `gen_*` + 4 round-1 artifacts, flags **exactly** the one real
  defect and nothing else.
* **T6 needs no `rejected` edge.** `reject_checkpoint` writes `current_node = redirect_to`
  directly — it is not a transition. Verified too that the user's wording still reaches the
  emitter: `_append_feedback_log` writes the redirect target's log and `claim` prefers that
  log over the scalar `_feedback`, so the `status = 'pending'` filter on the inputs_json
  injection (which `emit_graph`, being completed, would miss) does not lose it.
* **T2's prefix test was wrong on the first try** — a test caught it. `forge-todo-` also
  prefixes `forge-todo-app-abc123`, so slug `todo` would have claimed an unrelated
  `todo-app` generation. Now matched against the full `forge-<slug>-[0-9a-f]{6}` shape.
* **T1 keeps the `skillflow_graphs` row on a plain archive.** Deleting it (what I did by
  hand between rounds) makes existing runs of that config unresolvable — the zombie-run
  trap already recorded in memory. Archive is reversible and leaves runs readable; `purge`
  is the documented hard delete.

## Known limits (not defects, but say them out loud)

* A T6 rejection re-enters `emit_graph → gates`, and the `max_loop` counters from the first
  pass are **not** reset — several rejections in a row can exhaust them and fail the run.
* Archive drops the pipeline from the catalog and from the live process, but the graph row
  stays, so a caller holding the config name could still `create_run` it directly. That is
  deliberate (existing runs), and `purge=true` closes it.
* The container still runs the PyPI `skillflow-py`; none of these changes need a skillflow
  release, but `forge_dryrun_smoke`'s new message depends on `error_reason` being populated,
  which every `_fail_run_in_tx` path does.

---

# Round 3 — `mcp_server_builder` re-drive after the round-2 fixes (2026-07-27 03:48–04:10)

Same verbatim request (recovered from the round-2 run's own `_seed/skill_description.md`, not retyped).
Slate was already clean: no `gen_mcp_server_builder` in `~/.AItelier/configs/`, in `skillflow_graphs`, or
in the live registry, and none of its tools in `~/.AItelier/tools/`.

**Outcome: still failed — but in 22 min instead of 55, with a cause anyone can read.**

## Fixes verified in the live run

| Fix | Evidence |
|---|---|
| **T8** | `v_smoke` reported `dry-run smoke failed after 'pytest_run' (status=failed). Reason: No matching transition from 'pytest_run' with flags {'passed': True, 'has_suggestions': False}. Trail: [...]`. Round 2's version of the identical failure was `dry-run smoke failed (status=failed). Trail: [...]`. This single message turned a 55-minute mystery into a one-read diagnosis (U1 below). |
| **T9** | The API returned `failed:Output validation failed: Validation failed: {'file': 'pipeline.yaml', ...}` — the enriched status reached the client instead of a bare `failed`. |
| **T7** | `tool_plan_review` passed an empty plan on the first attempt and went straight to `emit_graph` — no loop back to `tool_plan`. |
| **T2** | One forge run (`forge-mcp-server-builder-95991a`), not two. |

Not exercised this round: T5 (the emitted graph had no unreachable terminal), T6 (no rejection), T3 (no
`create_run` failure), T1 (nothing needed archiving).

`v_lint` and `v_registry` passed on **every** emit round — round 2's remaining rewind pressure is gone.

## U1 — the dry-run smoke rejects CORRECT graphs (root cause of R3 in rounds 2 AND 3)

The emitted step was right:

```yaml
id: pytest_run
step_type: tool
tool_name: pytest
transitions:
  - {to: readme_writer, match: {verdict: passed}}
  - {to: fixer, match: {verdict: failed}, max_loop: 5, feedback: true}
```

skillflow's native `pytest` really returns `{"verdict": "passed"|"failed"}`
(`skillflow/tools/pytest/impl.py`). But `forge_dryrun_smoke` converts every tool step into a stub agent
returning `{"passed": True, "has_suggestions": False}` — flags no real tool emits.
`StubStepRunner._write_transition_files` synthesizes fixtures for `from_file:` matches and does nothing
for **plain flag matches**, so a step branching on its tool's documented contract can never match.

Reproduced in isolation with a 4-step graph: `passed: False`, same message. Three emit rounds produced
the identical failure, because there was nothing to fix — the graph was correct and the gate was wrong.

This is a false negative that hits **every** generated pipeline running a real fallible tool — which is
exactly what `forge_palette` tells makers to do ("a tool that can fail needs a failure edge… branch on
the result"). The gate is currently punishing the convention it teaches.

**Proposed fix:** when the smoke stubs a tool step, derive the stub's flags from that step's own
transitions — the same idea already used for `from_file` fixtures. `verdict=True` adopts the first plain
`match` dict, `verdict=False` the last. **Trade-off to state:** the smoke then can no longer detect a tool
step whose branches are internally unroutable. That loss is acceptable — `forge_registry_check`'s
`_fallible_tools_unrouted` already covers the unconditional-edge case, and a gate that fails every
correct graph is strictly worse than one that misses a rare shape.

## U2 — a re-emit skips rewriting files it thinks already exist, and the run dies on validation

After the third smoke rejection, `emit_graph` was claimed four more times and produced
`validation_failed` every time; the run ended on retry exhaustion. The agent's own narration says why:

> All required files are in place: `pipeline.yaml` — existing, passed review ✅ · `role_table.yaml` —
> existing, passed review ✅ … *(then it wrote only the 4 missing templates)*

It was reading the **promoted** `emit_graph/` directory, where the previous round's files really do sit,
while validation checks the **fresh** `emit_graph.tmp/` staging dir, which starts empty on every attempt.
So the emitter "correctly" concluded there was nothing to rewrite and shipped a staging dir with only
templates in it. Same staging-vs-final confusion class as the earlier subagent read-thrash.

The user-facing error then names the *symptom* (`File not found: pipeline.yaml`) and never the cause (the
smoke rejection that triggered the re-emit) — the exact S6/T8 pattern, one layer up.

**Proposed fix:** say it in `templates/forge_emit.md` — a re-emit must write the FULL file set into its
own staging dir every attempt, because prior output is visible but not inherited. Cheaper and more
general than a code change; the validation error itself could also name the staging dir as "yours, and
empty" rather than just "not found".

## U3 — the CLI turn dies at 7 min while `generate_pipeline` polls a 22-min run

> `Still waiting on the agent (191s with no activity)` → `Error: No response from the agent for 7 minutes
> — the turn looks dead. Input is enabled again; your message was saved.`

The forge run continued in the background and finished normally, but the chat had already given up. A
generation that legitimately takes 20–40 minutes cannot be relayed by a turn with a 7-minute idle bound.
The run-completion relay exists precisely for this; the poll-to-checkpoint call should hand off to it
rather than block the turn.

---

# Round-3 fix plan (trace-verified)

Reading the trace changed the picture: **U1 was not the reason the run failed to converge.** The emitter
never saw a single gate error. Two skillflow-side defects silently drop the feedback channel, and they
outrank everything else.

## The evidence

`forge-mcp-server-builder-95991a`, step `emit_graph`, 7 attempts (4 instances × up to 3 validation
retries). The emitter re-emitted **blind, six times** — three identical smoke failures, then four
identical partial writes.

> **Correction (2026-07-27).** This section first cited "every prompt is 20022–20023 chars and none
> contains the gate error". **That evidence was unsound and is withdrawn**: the trace clips a
> `user_prompt` payload at ~20k with a `…[clipped N chars]` marker (attempt #452 of the next run clipped
> 24 841 chars), so a substring test over that field proves nothing about what the model saw. The
> findings below rest on the STEP ROWS instead, which are not truncated — and on the regression tests,
> which fail against the unfixed code.

### F1 — the claim path reads the feedback fields from the WRONG step instance

`core.py` ~1059, inside `claim_next_step`:

```python
existing = conn.execute(
    "SELECT inputs_json FROM skillflow_steps WHERE run_id = ? AND step_id = ?",
    (run_id, run["current_node"]),          # no ORDER BY, no LIMIT
).fetchone()
```

Run that exact query against the live DB and it returns instance **3768** (the first, `validation_retry_count=0`)
while the instance being claimed is **3785** (`validation_retry_count=3`) — the one
`_handle_validation_failure` actually wrote `_validation_error` into.

Controlled comparison **inside the same run**: `architect` has ONE instance, and its resolved context
carries `⚠️ Previous attempt failed validation — MUST FIX`. `emit_graph` has FOUR, and never gets the
label despite three validation retries. Single-instance steps work; multi-instance steps — i.e. every
maker in every Green/Red loop after the first rejection — silently don't.

The codebase already knows the idiom: `_confirm_tool_in_tx` and the pending-instance INSERT both use
`ORDER BY id DESC LIMIT 1`. This read is the outlier — and it does not even need ordering, because
`step_row["id"]` (the instance just claimed) is in scope two statements earlier.

**Fix:** `SELECT inputs_json FROM skillflow_steps WHERE id = ?` with `step_row["id"]`.
**Regression risk:** none — it narrows an ambiguous read to the row the writer targeted. Add a test with
two instances where only the newest carries `_validation_error`.

### F2 — `feedback: true` on a tool-gate edge silently no-ops on a backward loop-back

```python
def _inject_feedback_in_tx(self, conn, run_id, target_step_id, feedback):
    conn.execute("UPDATE skillflow_steps SET inputs_json = json_set(inputs_json, '$._feedback', ?) "
                 "WHERE run_id = ? AND step_id = ? AND status = 'pending'", ...)
```

When `v_smoke` fails and routes back to `emit_graph`, the previous `emit_graph` instance is `completed`
and the next one does not exist yet — the claim path INSERTs it later, with a fresh `inputs_json`. The
UPDATE matches **zero rows**. Confirmed: 0 of 4 `emit_graph` instances ever carried `_feedback`.

It lands only when the target already has a pending row (a forward edge, or a step instantiated in the
first traversal and not yet claimed) — which is why the mechanism looks alive in aggregate (10 rows) while
being dead for exactly the case that matters.

**Blast radius:** `pipeline_forge` (all 4 gates), `novel_chapter` (1), and **8 sites across 6 generated
pipelines**. DPE is unaffected — it uses the reviewer-reads-the-verdict-file pattern, which works.

**Fix:** also `_append_feedback_log(...)`. That log is read by the claim path unconditionally and keyed by
(project, graph, step_id), so it survives re-instantiation — it is why checkpoint-rejection feedback has
always worked. **Decision to make:** the log accumulates ALL rounds and OVERRIDES the scalar, so gate
errors would stack each round. Either cap it or have the emit template say "fix the LAST one".

Both F1 and F2 need a skillflow release + pin bump + image rebuild.

## Phase 1 — make the feedback arrive TODAY (host-side, no skillflow release)

**1.1** Gate tools write their failure to `$STEP_DIR/gate_error.md`; `emit_graph` (and `architect`) read
`{source: {step: v_lint | v_registry | v_smoke, required: false}}`. This is the **same rule the palette
already mandates for agent reviewers** — "the maker MUST read the reviewer's verdict via context, no
`feedback:` flag needed". Tool gates were the exception, and the exception is what broke.
**Regression risk:** a stale `gate_error.md` from a previous round would mislead the next one — each gate
must delete/truncate it on pass. Test both directions.

## Phase 2 — the smoke gate (U1)

**2.1** Derive the stub's flags from the step's own transitions. `_flags_match` recognises four patterns;
only two are flag-based — `{field, value}` and the direct `{key: val}`. Skip `{from_file, field, value}`
(already fixture-backed) and `{from: checkpoint, ...}` (routed via `_checkpoint_approved`); adopting those
as flags would inject garbage, e.g. `from=checkpoint`. `verdict=True` → first eligible transition;
`verdict=False` → last.
**Regression risk:** the smoke can then no longer catch a tool step whose branches are internally
unsatisfiable — covered by 2.2.

**2.2** `forge_registry_check`: a fallible tool step whose every edge carries a `match` needs ≥2 edges,
else an unmatched result strands the run. Audited across 4 shipped + 6 generated configs: **zero** false
positives.

## Phase 3 — the re-emit contract (U2)

**3.1** `templates/forge_emit.md`: promotion **rmtree's the step dir and renames the staging dir onto it**
(`core.py:1898-1914`) — a re-emit that writes only the changed files DESTROYS the rest. The config hands
the emitter its own prior output as context by design (`{source: {step: emit_graph}}`), and nothing tells
it that output is not inherited. Say it, and scope the EDIT-MODE "only for roles you added or changed"
sentence so it cannot be read as "skip files".
**Regression risk:** none (prose). It is prompt-dependent, which is why Phase 1 matters more.

## Phase 4 — the three disagreeing timeouts (U3)

**4.1** TUI release 420s (`chat.py:1829`) < server SSE idle 600s (`agent_routers.py:28`) <
`_poll_pipeline_until_checkpoint` 1800s (`meta_agent.py:4383`). The turn is killed while the tool is
legitimately still polling. Bound the poll below the TUI's release and return
`{status: "running", run_id: …}`; the checkpoint modal and the SSE sidebar already surface progress
independently of the chat turn.
**Regression risk:** the butler no longer relays the checkpoint inline — it must say "I'll tell you when
it's ready" and the user relies on the modal. Acceptable; today they get an error message instead.

## Order

**1.1 → 2.1 → 2.2 → 3.1 → 4.1 → (release) F1 → F2.**
Phase 1 unblocks without a release and is worth keeping even after F1/F2 land — an explicit context source
is more legible than a framework-injected field. F1 is the highest-value upstream fix: it silently
disables validation feedback for **every** multi-instance step in **every** pipeline.

---

# Applied — round-3 plan, 2026-07-27

AItelier: **1212 passed, 9 skipped**. skillflow: **538 passed** (up from 531).

| # | Change | Where |
|---|--------|-------|
| 1.1 | The three gates write `gate_error.md` into their own step dir on failure (removed on pass); `emit_graph` reads all three as ordinary context. `architect` already declared `{step: v_registry}`/`{step: v_smoke}` — those sources resolved to nothing because tool steps wrote no files, so this makes an existing, intended wiring actually work. | `aitelier/gate_report.py` (new), the 3 gate tools + their `tool.yaml`, `configs/pipeline_forge.yaml`, `tests/unit/test_gate_report.py` (new, 11) |
| 2.1 | `StubStepRunner` derives flags from the step's own transitions — **add-only**, never overriding `passed` | `aitelier/stub_runner.py`, `forge_dryrun_smoke/impl.py`, `tests/unit/test_forge_dryrun_smoke.py` (+6) |
| 2.2 | A fallible tool with exactly ONE conditional edge is now a violation | `forge_registry_check/impl.py`, `tests/unit/test_forge_registry_check.py` (+4) |
| 3.1 | The staging-dir contract, stated | `templates/forge_emit.md` |
| 4.1 | `_POLL_BUDGET_S = 240`, under both watchdogs; a still-running run returns instead of blocking | `core/meta_agent.py`, `tests/unit/test_generate_pipeline_idempotent.py` (+3) |
| F1 | The claim path reads `inputs_json` from the instance it just claimed | `skillflow/core.py`, `tests/test_feedback_reaches_the_rerun.py` (new, 7) |
| F2 | `_inject_feedback_in_tx` targets the newest instance; the re-instantiating INSERT carries `_feedback` forward (NOT `_validation_error`) | `skillflow/core.py` |

**The F1/F2 tests were verified to fail without the fix** — all 7, then all 7 pass with it.

## Two things the plan got wrong, found while implementing

* **The stub was reading the wrong object entirely.** `ClaimedStep.step_config` is not
  the step's definition — it is skillflow's opaque per-step `config:` key
  (`graph.py: config=s.get("config", {})`), which every real graph leaves unset. So
  `_write_transition_files` and `_touch_declared_outputs` have **never run**, not once,
  since the smoke was written. The runner now takes the step definition from the graph
  it is booting. The `from_file` fixture support the code claims to have is therefore
  live for the first time.
* **"Adopt the first branch's flags" was too blunt.** It would also satisfy a step
  matching `{passed: false}`, so a reviewer whose success edge matches the wrong value
  would be routed by its own mistake — destroying detection the smoke did have. The
  shipped rule is ADD-ONLY: the verdict-driven `passed`/`has_suggestions` stay
  authoritative and only keys the stub cannot know (`verdict`, `synced`, …) are filled
  in. Both properties are now tested.

Audit of the new 2.2 rule across 4 shipped + 6 generated configs: **zero** new
findings (only the two pre-existing unconditional-edge cases, unchanged).

## NOT deployed — the container still runs the PyPI build

F1 and F2 live in the host's editable `~/stepflow` checkout. Per `CLAUDE.md` the image
installs `skillflow-py` from PyPI, so **the container does not have them**. Shipping
them needs a version bump, a PyPI publish, a pin bump here, and
`docker compose build aitelier && up -d`. Until then a re-drive exercises only the
host-side fixes (1.1, 2.1, 2.2, 3.1, 4.1) — which is most of the value, since 1.1
delivers the gate errors without any framework change.

---

# Round 4 — `mcp_server_builder` after the round-3 fixes (2026-07-27 06:15–07:17)

Host-side fixes only: F1/F2 live in the editable `~/stepflow` checkout and the container runs the
PyPI build, so this run is a test of **Phase 1 alone**. Verified before starting: the container
imports `aitelier.gate_report`, `StubStepRunner._derived_flags` exists, `v_smoke` carries
`out_dir: $STEP_DIR`; zero `gen_mcp_server_builder` rows anywhere. Same verbatim request.

## Result: PASSED — the first time this skill has ever produced a pipeline

| Round | R1 | R2 | R3 | **R4** |
|---|---|---|---|---|
| outcome | failed, 38 min | failed, 55 min | failed, 22 min | **passed, 37 min** |

```
emit 1:  v_lint ✅  v_registry ❌ (fix_apply unrouted + final_commit single conditional edge)
emit 2:  v_lint ✅  v_registry ✅  v_smoke ❌ (UNIQUE constraint: skillflow_edge_counts)
emit 3:  v_lint ✅  v_registry ❌ (fix_apply — REGRESSION, see V2)
emit 4:  v_lint ✅  v_registry ✅  v_smoke ✅  → explain → Design Review
   [user rejects with substantive feedback]
emit 5:  v_lint ✅  v_registry ✅  v_smoke ✅  → explain → Design Review
```

**Every gate failure was acted on.** Across R1–R3 the same failure repeated verbatim until the loop
died; here each one produced a targeted repair.

## What each fix did, from untruncated evidence

* **1.1 — decisive, and it worked WITHOUT F1/F2.** The emitter's second instance resolved
  `Step v_registry — gate_error.md` in its context, and emit 2 fixed exactly what emit 1's report
  named. Phase 1 was therefore sufficient on its own: the skillflow fixes can ride a normal release
  instead of a rushed one.
* **2.2** — caught its first live defect on emit 1 (`final_commit`'s single conditional edge).
* **T8** — the smoke handed the emitter skillflow's exact `UNIQUE constraint failed` text, which it
  then fixed.
* **T5** — the re-emit's new `apply_failed` terminal was correctly registered in `end_conditions`;
  without the rule it would have been a dead end.
* **T6** — rejection redirected `current_node` to `emit_graph` (before, it re-ran `explain`, which
  only writes prose) and the feedback landed in `_feedback/emit_graph.md`. The next instance carried
  `_feedback` plus the context label `⚠️ Reviewer / User Feedback — MUST ADDRESS`. Note this channel
  works WITHOUT F1/F2 because `_read_feedback_log` is keyed by (project, graph, step) rather than by
  instance — the same asymmetry that let checkpoint rejection work while tool-gate feedback died.
* **T2** — one forge run, not two, in both drives.

The rejection produced a real fix: all five `*_apply_fallback` no-op gates deleted, every
`repo_apply` failure routed to a new `apply_failed` terminal, the redundant `run_tests_fallback`
removed, and `fix_apply`'s failure looped back to `fix_maker`.

## New findings

* **V1 — `forge_registry_check` does not know the duplicate-`max_loop`-edge rule.** Emit 2 shipped
  two `max_loop` edges on one (from, to) pair; `forge_lint` passed it and only the smoke caught it,
  as a boot error. Our own configs get this from a pytest guard
  (`tests/unit/test_config_graph_integrity.py`) written after I caused the same defect. The gate
  should own it — the message would then name the pair instead of quoting a SQL constraint.
* **V2 — gate reports must ACCUMULATE, not reset (a defect in my own 1.1).** I chose
  write-on-fail/delete-on-pass to avoid stale reports. Emit 3 then fixed the smoke's complaint and
  **reintroduced** the registry defect emit 2 had already fixed, because `v_registry`'s report had
  been deleted when it passed. skillflow's own feedback log accumulates for precisely this reason —
  its fixture says *"a revision that silently reverts an earlier round's fix gets rejected instead
  of passing blind"*. I picked the opposite and reproduced the failure mode that design prevents.
  Fix: append each round with a marker, never delete; let the reviewer judge staleness.
* **V3 — the CLI cannot reject a checkpoint AT ALL.** In `CheckpointModal` the only focusable widget
  is the `VerticalScroll` content pane, which consumes ↑↓ to scroll (observed: the content moved
  while the cursor stayed on Approve). Textual consults screen `BINDINGS` only after the focused
  widget declines the key, and `Tab` does not help because the feedback `Input` is built with
  `can_focus = False`. "Request Changes" is unreachable; the rejection had to go through the API.
  Every rejection path in the system is dead from the CLI — including the T6 path just added.
* **V4 — the 2.2 rule is gameable, and the emitter gamed it.** Told to route a fallible tool's
  failure, it added an edge to a gate that unconditionally rejoins the SUCCESS target
  (`spec_apply --fail--> spec_apply_fallback --> scaffold_maker`, the same node success goes to).
  The letter of the rule is satisfied and the fail-open is preserved: a `repo_apply` that never
  landed the code advances exactly as if it had. Only the human review caught it. The rule needs to
  check that a failure branch does not reconverge on the success target before something handles it.

---

# Round 4 (cont.) — approval, registration, and the first real test-drive

After the rejection round the graph was clean, so it was **approved through the TUI** (the default
cursor sits on Approve, which is why approval works from the CLI and rejection does not — see V3).

* Run `completed`; `gen_mcp_server_builder` registered with `gen_mcp_server_builder.yaml` +
  `.roles.json` (10 namespaced roles, ~1.4k-char real prompts each).
* `derive_repo_mode` → **`code`** (correct: repo_apply / run_tests / draft_commit),
  `derive_output_step` → `final_commit`, catalog label and description both sensible.
* **The full generate → review → reject → re-emit → approve → register lifecycle now works.**

Then `drive_pipeline` test-drove it on a fresh brief (a reading-list MCP server). It got through
`spec_maker → spec_reviewer → spec_maker` (the Green/Red loop the generator built works) and then
**died**:

```
error_reason: read_file() missing 1 required positional argument: 'path'
```

## W1 — a malformed tool CALL is fatal; a tool that RETURNS an error is tolerated

`skillflow/core.py:_execute_tool_impl` ends:

```python
sig = _inspect.signature(fn)
kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}   # drops unknown kwargs
...
result = fn(**kwargs)                                              # UNGUARDED
```

The signature filter silently drops a misnamed argument (`file=` instead of `path=`), which turns a
recoverable "unexpected keyword" into a guaranteed `TypeError: missing 1 required positional
argument`. Nothing catches it: it propagates through `execute_tool` → `_exec_tool` → the agent turn
loop → step failure → run failed.

This is the principle already stated for this codebase — *a ReAct agent should tolerate tool errors
in the loop* — with one path where it does not hold. The agent cannot recover from its own argument
typo, because the typo never comes back to it as a tool result. A `{"error": "..."}` RETURN is fed
back and survivable; a raised `TypeError` is not.

**Fix direction:** wrap the call so an exception becomes
`{"error": "<tool>(...) failed: <msg>. Expected params: <signature>"}` — the agent then sees its
mistake and the correct parameter names in the same turn. Do NOT stop filtering unknown kwargs
(that protects tools from junk), but DO name the dropped keys in the error, since dropping them is
what produced the missing-argument error in the first place.

## W2 — the emitter grants `read_file` to a reviewer, which the emit template forbids by name

`gen_mcp_server_builder__spec_reviewer` has `tools: ['read_file']`. `templates/forge_emit.md` says:

> Give a role `read_file` in its tools ONLY when it must fetch something NOT in its context (rare);
> default to no `read_file`, **like the DPE reviewers**.

A reviewer is the exact counter-example the template names, and the emitter granted it anyway — then
the role used it, on context it had already been handed, with the wrong parameter name. Prose in the
emit template is not holding. `forge_registry_check` can check this mechanically: a step with
context sources whose role's tool list includes `read_file` is at best redundant and, per W1, a live
crash risk.

## W3 — a butler-driven run's status never reaches the DB

skillflow says `failed`; `aitelier.db` still says **`planning`**. The T9 fix works for
scheduler-owned runs because `_sync_project_status_to_db` runs on the poller's tick — but a
generated pipeline is `scheduler_owned: false`, so nothing ever syncs it. The dashboard shows a
failed test-drive as still planning, forever. Same class as T3/T9, third distinct path.

## Applied — W1 / W2 / W3 (2026-07-27)

AItelier **1227 passed**, skillflow **545 passed**. The 7 W1/W2 tests were checked against the
unfixed code first: 6 of 7 fail, all pass after.

* **W1** — `fn(**kwargs)` in `_execute_tool_impl` is wrapped. A raised exception becomes
  `{"error": "<tool>() failed: <type>: <msg>. Accepted parameters: …. These arguments were not
  recognised and were ignored: …"}`, so the ReAct loop sees the mistake AND the right parameter
  names in the same turn. Framework-injected params (`workspace_root`, `project_root`, `step_id`,
  `run_id`) are excluded from the advertised list — naming them would invite the agent to pass them.
* **W2 (runtime half)** — `_rebind_unambiguous_param`: when exactly ONE argument was dropped as
  unrecognised and exactly ONE required parameter is unfilled, bind them. With more than one on
  either side it is a guess, so it reports instead. This makes `read_file(file=…)` — the exact
  mistake `write(file=…)` teaches — just work.
  *Bug found while writing the tests:* `dropped` initially counted the engine's own
  `setdefault`-injected kwargs, so it was never 1 and the rebinding could never fire; it now counts
  only the caller's arguments, which also stops the error blaming the agent for arguments it never
  sent.
* **W2 (review half)** — `register_tool` now returns a `param_naming` advisory when a new tool uses
  a variant spelling (`file`/`filename`/`file_path` → `path`, `files`/`file_list` → `paths`).
  **Deliberately not a rename**: renaming `write(file=…)` or `pytest(file=…)` would break every
  config's `tool_params` and every role prompt that names them. This stops the divergence growing;
  the runtime rebinding absorbs what already exists.
* **W3** — `drive_pipeline` calls `_sync_project_status_to_db(pid)` at its exit. It was the only one
  of the butler's run-launching tools that did not; a guard test now asserts both it and
  `generate_pipeline` do.

### Why not standardise the names outright
The registry has six spellings for one concept and the two most-used tools disagree. A mass rename
is the "correct" fix and the wrong move here: `tool_params` in every shipped and generated config,
plus every role prompt that mentions a parameter, would need to change together, and generated tools
under `~/.AItelier/tools/` are user data. Alias-at-the-boundary plus a registration-time advisory
gets the same safety with none of the blast radius.

---

# X-series — why a fully-gated pipeline produced nothing (trace-verified)

`drive-gen-mcp-server-builder-c5493e`, step `scaffold_maker`, four rounds. Two of my earlier
statements were wrong and are corrected here.

## What the trace actually shows

```
tools ACTUALLY advertised:  ['read_file','create','edit','finish_step','read','search','list']
role's declared tools:      ['read_file','write_file','create_file','edit_file']
tool_call events:            NONE, in any of the four rounds
after_validate:              "0 file(s)"  ×4
step outcome:                completed, flags {}, next_node scaffold_reviewer   ×4
```

* **Correction 1** — I said the makers "had exactly one real tool: `read_file`, read-only". Wrong.
  skillflow injects the write-mode toolset (`create`/`edit`) from the step's `output.mode`
  regardless of the role's list, and silently drops names that do not resolve. The agent could write.
* **Correction 2** — the bad `tools:` list was therefore **not** the operative cause. The operative
  cause is the role TEMPLATE prose: *"You have access to `create_file(path, content)`,
  `write_file(path, content)`, and `edit_file(path, old_str, new_str)`"* — none of which were in its
  toolset. The agent followed its prompt rather than its toolset and emitted the files as a JSON
  blob of prose.

## The four defects

**X1 — skillflow checks tool existence and throws the answer away.** `agent_registry.py:117`:

```python
for tool_name in cfg.tools:
    try:
        cfg.tool_schemas[tool_name] = tool_loader.load_schema(tool_name)
    except ImportError:
        pass  # tool not found — graph validation will catch
```

`load_schema` IS the existence check. The comment is false: `graph.validate()` sees only the YAML,
and role tool lists are not in the graph, so nothing downstream catches it. Registration is the
right boundary — skillflow owns the registry (native + custom); it does not own, and must not
judge, WHICH tools a role should be granted (that is the config author's call).

**X2 — a role template names tools that are not in that role's toolset.** This is what actually
misled the agent. Nothing checks a template's prose against the step's effective vocabulary.

**X3 — a `mode: write` step that writes zero files completes green.** The lifecycle hook already
reports `0 file(s)`; nothing acts on it. The generated step carried `validation: null`, so there was
no retry either. Four silent no-ops, four correct rejections, then cycle exhaustion.

**X4 — no forge surface tells the emitter the real write vocabulary.** The palette never names
`create` / `edit` / `write` / `finish_step` or the content-mode `create_<slot>` family, and never
says the role's `tools:` list must NOT include them because the framework injects them from
`output.mode`. Asked to write a role table for "an agent that writes files", the emitter supplied
plausible names from the coding-agent genre. (Checked: the 9 generated pipelines that predate this
one are all clean, and 11 write-mode steps across 6 of them all got `write` — so this is
domain-driven invention on the only code-writing pipeline, not a prompt regression.)

---

# Fix plan (X-series)

## Phase A — existence, in skillflow, where the registry lives

**A1. Stop swallowing unknown tools** — `resolve_tool_schemas` records the unresolved names per
config, logs a warning naming the role and the tools, and exposes
`AgentRegistry.unknown_tools() -> {role: [names]}`.
*Impact:* nothing changes for a correct config; the information stops being destroyed.
*Regression risk:* a host that registers agents BEFORE tools would see transient warnings.
Contained: `resolve_tool_schemas` re-resolves EVERY config on each `register_agent_config*` call
(core.py:483/489), so the unknown set is recomputed and clears itself once the tool appears. Do NOT
raise — raising would break exactly that ordering.

**A2. Let the linter check existence** — `lint_config` / `lint_content` take an optional
`tool_loader`; when supplied, every `tool_name` in the graph is resolved and unknown ones become
lint errors. `skillflow_lint` forwards a loader when the host provides one.
*Impact:* the linter stops being purely structural, which is what makes it unable to catch this
class today. *Regression risk:* none — omitting the loader preserves current behaviour exactly.

Both need a skillflow release + pin bump + image rebuild.

## Phase B — the gate, before anything is registered

At emit time `role_table.yaml` is a file on disk; skillflow has not seen it, so A1 cannot fire yet.
`forge_registry_check` already loads it and already has `_live_tools()`.

**B1. Validate every role's `tools:` against the live registry.** `failure_class: emit_fixable`.
*Regression risk:* a role naming a tool built later in the same run — impossible, `tool_loop` runs
before `emit_graph`.

**B2. Flag a template that names a tool outside its role's effective vocabulary.** Effective set =
(declared ∩ real) ∪ `generate_write_tool_schemas(mode, fixed, allow_full_write)` ∪ the context-derived
read tools ∪ `finish_step`. Scan templates for `` `name(`` call-shaped mentions.
*Regression risk:* false positives on prose that mentions a tool it does not call. Mitigate by
matching only call-shaped occurrences, and measure against the 10 existing generated pipelines
before shipping — same bar the V-rules were held to (0 false positives across 28 configs).

## Phase C — a step that produces nothing must not pass

**C1 (host, now).** `forge_registry_check`: a `mode: write` step must declare a `validation`.
A missing output then becomes a validation failure, which retries the step in place WITH the reason
attached — a channel that works now that F1 delivers `_validation_error` to the right instance.
*Regression risk:* stricter emit; mitigated because the failure is local and self-explaining.

**C2 (skillflow, upstream).** A write-mode step that promotes zero files should surface it — set a
`wrote_files: false` flag so a graph CAN route on it, and log it. The generic fix; C1 is the one
that would have caught this run on round 1.

## Phase D — stop the invention at the source

**D1.** `forge_palette` + `templates/forge_emit.md` state the mutation vocabulary explicitly: write
mode gives `create` / `edit` (+ `write` only with `allow_full_write`), content mode gives
`create_<slot>` / `write_<slot>` / `edit_<slot>` per fixed slot, plus `finish_step`; and a role's
`tools:` list must NOT name them — the framework injects them from `output.mode`, and a name that
does not resolve is dropped silently. Also: a template must only reference tools the role actually
has.
*Regression risk:* none (prose). This is prevention, not enforcement — B1/B2 are the enforcement.

## Order

**D1 → B1 → C1 → B2 → (release) A1 → A2 → C2.**

D1 first because it is free and removes the cause. B1/C1 are small gate rules that convert the
remaining cases into legible, self-repairing failures. B2 last among the host changes because it is
the only heuristic one and needs a false-positive audit. The skillflow items ride the next release
along with F1/F2/W1/W2 — none of them is on the critical path now that the host-side gate covers
the emit-time case.

## Applied — X-series (2026-07-27)

AItelier **1242 passed**, skillflow **553 passed**. The A-phase tests were checked against the
unfixed code first: 6 of 8 fail, all pass after.

| # | Change | Where |
|---|--------|-------|
| D1 | The real mutation vocabulary, stated: `mode: write` → `create`/`edit`/`finish_step` (+`write` with `allow_full_write`); `mode: content` → `create_<slot>` etc. A role's `tools:` is for REGISTRY tools only, and a name that does not resolve is dropped silently. Plus: a template must only name tools its role has. | `forge_palette`, `templates/forge_emit.md` |
| B1 | Every role's `tools:` validated against the live registry | `forge_registry_check` (+4 tests) |
| B2 | A template naming a tool outside its role's effective vocabulary is a violation | `forge_registry_check` (+6 tests) |
| C1 | A `mode: write` step must declare a `validation` | `forge_registry_check` (+4 tests) |
| A1 | `resolve_tool_schemas` records unknown tools + warns; `AgentRegistry.unknown_tools()` | `skillflow/agent_registry.py` (+5 tests) |
| A2 | `lint_config`/`lint_content` take an optional `tool_loader` and check `tool_name` existence; `forge_lint` passes the live loader | `skillflow/plugins/linter`, `forge_lint` (+3 tests) |
| C2 | A write-mode step promoting zero files sets `wrote_files: false` and logs | `skillflow/core.py` |

### Two rules were wrong on first draft; the audits caught both

* **"A role must not list injected write tools" — deleted.** The audit showed **nine of ten**
  generated pipelines list `write` in a role's tools, and all nine work (`write` is a real registry
  tool; listing it is redundant, not wrong). The rule flagged the working convention. Only the
  *unknown*-tool half survives, and it now flags exactly one pipeline: the broken one.
* **B2's false-positive rate was measured before shipping**, to the same bar as the V-rules: 12 hits
  across the ten pipelines, **all in `gen_mcp_server_builder`, all genuinely non-existent tools**,
  zero elsewhere. Matching only call-shaped mentions (`` `name(`` ) is what keeps it quiet — prose
  that merely names a tool is not flagged, which is covered by a test.

### One deliberate strictness increase
C1 fires on write-mode steps in 6 of the 10 existing pipelines. Those are **latent** risks, not false
positives: `gen_skill_packager__draft` would fail exactly as `scaffold_maker` did if its model ever
emitted prose instead of calling `create`. Remediation is one line, and `file_exists` globs, so
`files: ["*"]` fits a step whose filenames are not known ahead of time — the violation message
carries that snippet verbatim. An existing test that asserted a bare write-mode step passes was
updated: its subject (the deliverable check must not second-guess write mode) is preserved and now
asserted directly instead of via the global verdict.

### Not deployed
A1/A2/C2 and the earlier F1/F2/W1/W2 all live in the editable `~/stepflow` checkout. The container
runs the PyPI build, so shipping them needs a version bump, a PyPI publish, a pin bump, and an image
rebuild — a decision that is the user's, not mine. The host-side items (D1/B1/B2/C1) are live on
restart.

---

# Round 5 — all three skills, clean slate (2026-07-27 18:18–)

Clean-slate protocol used the **new `archive_generated_pipeline(purge=true)` tool** rather than hand
SQL — the T1 fix doing the job it was built for. The three test pipelines were archived + purged and
their two generated tools moved aside; the user's other eight pipelines were untouched. Every fix
(host + the skillflow wheel) verified live in the container before starting. Round 2's exact prompts
for math_olympiad/skill_packager, rounds 1–4's for mcp_server_builder.

All three were driven **concurrently**, which round 2 never did.

## Verified working in this round

* **T2** — one forge run per request, three times over. (Round 2's math_olympiad produced two runs
  30s apart; that is the finding's original evidence.)
* **4.1** — the butler ended its turn with *"This takes 10–40 minutes. The checkpoint will surface on
  its own"* instead of the CLI killing it at 7 minutes with "the turn looks dead".
* **T7** — *"The manifest is empty; no tool cards"* accepted without a rework hop.
* **V2** — the accumulating findings log, in `math_olympiad`'s `v_lint`:
  `## round 1 · PASSED — nothing to fix. Everything flagged above is resolved; keep it that way.`
  A pass is now a recorded round, so a later emit cannot silently regress it — the exact amnesia
  that cost round 4 a wasted cycle.
* **C1** — first live catches, in TWO different pipelines: `math_olympiad`'s `parse_plan` and
  `mcp_server_builder`'s `B1`, both `mode: write` with no `validation`.
* **S1** — the decisive re-test. `skill_packager` built and registered TWO tools in one fan-out:
  `skill_frontmatter_validate` (157 lines, its own function + 2 helpers) and `skill_package_zip`
  (49 lines, its own function), each stamped `x-generated-by: forge-skill-packager-a54811`. In
  round 1 the second tool received the first one's code.
* **B1/A1** — `gen_math_olympiad` registered with all five roles on `['read_file', 'write']`:
  zero hallucinated tools.

## math_olympiad — completed and registered

```
emit 1:  emit_review ❌  give-up path shares the success terminal (reviewer quoted the palette)
emit 2:  v_lint ✅  v_registry ❌  C1: parse_plan writes with no validation
emit 3:  v_lint ✅  v_registry ✅  v_smoke ✅  → explain → approved → registered
```

Three rounds, each fixing a genuinely different real defect. **Round 2 generated this same skill
with ZERO rework in 13 minutes** — round 5 took more rounds because the gates are stricter, and the
resulting pipeline is better: round 2's version shipped the silent-empty risk C1 now catches. More
rounds, better output.

The graph is faithful to the brief: `verify` reads ONLY `strip` (the cleaned proof, no reasoning —
the fresh-context adversarial verification that was asked for), `solve` reads the verifier's
objections, and "no confident solution" is a real outcome — `final_answer` routes to `done`
(completed) or `give_up` (failed) on its own confidence verdict, with both terminals declared.

## New issues

* **Z1 — concurrent runs serialize behind the slowest agent step.** `poll_and_execute` picks ONE
  project per tick and `await`s the whole tick, and the interval job carries `max_instances=1`, so
  three runs take the SUM of their step times, not the max. There is already a per-project
  `_get_tick_lock` guarding the same-run version races `max_instances=1` was added for (SF-5), so the
  global serialization is belt-and-braces that costs all cross-project concurrency — and
  `scheduler.py:59` already notes the intent was finer-grained. Throughput, not correctness; it
  inflates every wall-clock number in this round.
* **Z2 — a rename that does not carry its own references.** The emitter writes a self-referential
  seed source (`{config: <graph's own name>, output: task.md}`); registration renames the graph to
  `gen_<slug>` and does NOT rewrite it, leaving a source pointing at a config that will never exist.
  Present in **6 of 8** registered pipelines (`gen_deepsearch`, `gen_refverify`,
  `gen_reference_verify_e2e`, `gen_cac40_v2`, `gen_mdlink_pipeline`, `gen_deepsearch_verify2`).
  Benign today only because `_inject_seed_context` inserts a correct source at position 0 — it would
  be FATAL if the emitter ever marked the dead one `required: true`. Fix belongs in registration.
* **Y1 (mine, fixed during setup)** — the A1 unknown-tool warning was quadratic:
  `resolve_tool_schemas` re-resolves every config on every registration, so 5 bad roles emitted ~90
  identical lines while archiving. Now warns only when a config's picture changes; test asserts one
  line across five resolutions.

## Round 5 result: 3 of 3 completed and registered — a first

| Skill | R1 | R2 | R3 | R4 | **R5** |
|---|---|---|---|---|---|
| `math_olympiad` | completed, 1 rewind, 23m | completed, 0 rework, 13m | — | — | **completed, 3 emit rounds, 31m** |
| `skill_packager` | failed ×2, ~45m | completed, 1 hop, ~23m | — | — | **completed, 2 emit rounds, 42m** |
| `mcp_server_builder` | failed, 38m | failed, 55m | failed, 22m | completed, 5 rounds + a reject, 37m | **completed, 2 emit rounds, 36m** |

**All three in one session, concurrently, with no intervention.** Every previous round needed at
least one failure or a manual unblock. Wall-clock is inflated by Z1 (three runs sharing one poller),
so the minutes are not comparable to earlier single-run rounds; the ROUND counts are.

`mcp_server_builder` is the headline: 1→3 failed, R4 needed five emit rounds and a rejection, R5
took **two**, and its role tools came out `['read_file']` with **zero** hallucinated names — the
X-series defect that made R4's version constitutionally unable to write is gone.

### The gate did the work
C1 fired in **all three** pipelines (`parse_plan`, `draft`, `B1`) — the same latent silent-empty
defect, caught before it shipped, three times. Every previous round shipped it.

## Remaining issues found this round

* **Z3 — C1 enforces a rule the emitter is never taught.** D1 added the write VOCABULARY to the
  palette and emit template, but neither says a `mode: write` step must declare a `validation`. Every
  generation now burns one guaranteed rework round learning it from the gate. A paragraph in
  `forge_palette` + `forge_emit.md` removes that round for every future run. **Cheapest remaining
  win.**
* **Z5 — the fallible-tool rules are blind to GENERATED tools.** `skill_package_zip` documents and
  returns `{"passed": False, "error": ...}` on three paths, and the emitted graph routes it with a
  single unconditional edge to the COMPLETED terminal: a failed zip reports success. V4/2.2 missed
  it because `_FALLIBLE_TOOLS` is a hardcoded allowlist of BUILT-IN names and `_FALLIBLE_PREFIXES`
  does not match. The forge's own generated tools are precisely the ones those rules cannot see.
  Fix: derive fallibility from the tool's contract — `register_tool` can stamp `x-fallible: true`
  when the impl returns an `error` key, and the gate reads it.
* **Z4 — opaque step ids.** `mcp_server_builder` emitted `A1, A2, B1, B2, B3, C1…` while naming its
  ROLES well. Step ids are what every surface shows: dashboard step, trace, checkpoint labels, gate
  messages — this round's own violation read *"step 'B1': writes free-form files"*. `skill_packager`
  used `interview/draft/validate/package`, so this is per-emission variance, not systematic. Worth a
  line in the emit template.
* **Z1 / Z2 / Y1** — as recorded above (poller serialization; the rename that drops its own
  references; the quadratic warning, fixed during setup).

### Suggested order
**Z3 → Z5 → Z2 → Z4 → Z1.** Z3 is prose and saves a round every time. Z5 is a real fail-open in
shipped output. Z2 is latent-but-systemic (6 of 8 pipelines). Z4 is legibility. Z1 is throughput and
the largest change.

---

# Structural fix plan — applied 2026-07-28 (branch `fix/pipeline-generation-ux`)

The round-5 findings were fixed as three *patterns* rather than five holes. What each
subsumes is stated so the next reader can tell whether a new symptom is covered.

## S3 — one rule table: the gate teaches what it enforces

`forge_registry_check` had ~16 rules as private functions; `forge_palette` had ~10
conventions in a hand-written string; nothing tied them. Z3 was the visible cost — C1
shipped as a check while the palette mentioned validation only in passing, so **every
generation burned a guaranteed rework round** rediscovering it from the violation text.

* `RULES: tuple[Rule, ...]` in `forge_registry_check/impl.py` — one entry per rule,
  each carrying a `teaches` string addressed to the emitter.
* `forge_palette` RENDERS that table ("Rules the registry gate enforces").
* The six cheatsheet bullets that duplicated rules moved INTO `teaches`; the section
  that survived is honestly retitled "Gotchas the gates CANNOT see".
* `tests/unit/test_forge_rule_table.py` is the binding: it parses which checks
  `forge_registry_check` actually invokes and fails if one has no rule, asserts every
  rule reaches the rendered palette, and asserts the deleted bullets' detail survived.
* Z3 and Z4 both close as `teaches` entries. **A future rule cannot repeat Z3.**

`Rule` is a plain class, NOT a dataclass — this module is loaded by
`spec_from_file_location`, which leaves `sys.modules[__name__]` unset, and
`dataclasses` dereferences that None while resolving annotations. Same trap as the
custom lint backends. Caught by the tests within a minute of writing it.

## S2 — the tool declares its own failure contract; the allowlist is gone

Z5's root cause was `_FALLIBLE_TOOLS`, a hardcoded set of built-in names that is
*structurally* blind to the tools the forge generates — precisely the ones a generated
graph routes. The set had also rotted in both directions: **three of its seventeen
names resolved to no tool at all**, and one (`md_link_check`) was a GENERATED tool
somebody had already hand-added. The maintenance model was failing visibly.

* `x-fallible: true` in `tool.yaml`. `register_tool` DERIVES it (AST: does the
  exported function return a dict literal carrying `passed`/`error`?) and stamps it;
  an explicit declaration in the yaml always wins.
* The 8 AItelier built-ins are stamped; the 7 already-generated tools were back-filled.
* `forge_registry_check` reads the schema. `_FALLIBLE_UPSTREAM` (5 skillflow-native
  names that ship from PyPI and cannot carry the stamp until a release) is explicit,
  short and deletable — and deliberately NOT intersected with the live registry, since
  an empty intersection would switch the whole rule off silently.
* Measured across all 28 configs: three NEW true positives (`skill_package_zip` = Z5,
  plus two cac40 bookkeeping steps), zero new false positives.

## S4 — a rename must carry its own references

`_rewrite_self_config_refs` + `_dedupe_context_sources`, run before the rename in both
registration paths. Only the graph's own pre-rename identity (declared name or slug) is
rewritten; a source naming a genuinely different config is left alone.

Z2 was worse than recorded: **9 of 11** registered pipelines carried a dead
self-reference, not 6 of 8. Back-filling the historical files only recovers the 3 whose
dead name equals the slug — for the rest the emitter's original graph name is not
recoverable from the file. Registration handles them correctly from now on.

## Z6 — an archive tombstone that no registration lifts *(found while setting up the drive)*

`gen_math_olympiad`, `gen_skill_packager` and `gen_mcp_server_builder` had all
completed, registered and been used in round 5 — and were **absent from `/api/configs`**
after a restart, with their YAML sitting untouched in `~/.AItelier/configs/`.

They had been archived during round 5's clean-slate setup and re-generated afterwards.
`register_forge_pipeline` persisted + live-registered them but never cleared the archive
entry, so `load_generated_configs` and `ConfigRegistry.build` both skipped the name.
Works for the rest of the session, then silently gone. Fixed: `_unarchive(config_name)`
on both persist paths — writing a config IS the intent to have it.

Same shape as the whole produce-then-discard family: the registration produced a live
config and the tombstone discarded it at the next boot.

---

# Round 6 — the first DRIVE of generated pipelines (2026-07-28 03:33)

Rounds 1–5 tested **generation** only. Round 5 registered three pipelines and drove
none, so the whole execution half — W1/W2/W3, F1/F2, A2/C2 — was unproven. This round
ran all three end to end. `gen_math_olympiad`, `gen_skill_packager`,
`gen_mcp_server_builder`, driven by the butler through the TUI.

**Result: 1 completed, 2 failed — and both failures were OUR defects, not the agents'.**

## Verified working, live, for the first time

* **F1** — `gen_mcp_server_builder`'s `B1` retried three times and `_validation_error`
  was present on the claimed row every time (`inputs keys=[... '_validation_error']`,
  trace `claimed {"validation_error": "Validation failed: ..."}`). Before F1 that field
  landed on the wrong instance and the agent retried blind.
* **The maker–checker loop with real feedback**, in a GENERATED pipeline:
  `gen_skill_packager` ran `draft → validate(passed:false) → draft → validate(passed:false)
  → draft → validate(passed:true) → package`, and the two rejections carried real
  content ("directory name 'draft' does not match frontmatter 'name'").
* **C1** — the write-mode validation rule turned what used to be a silent no-op into a
  loud, legible, retried failure. In round 4 the same class of step completed green four
  times having written nothing.
* **S6/T9** — every failure arrived with its reason attached
  (`failed:Cycle limit exceeded — validate: check failed`).
* **Z5's shape did not bite this time**: `skill_package_zip` returned a `zip_path`, not
  a failure. The fail-open is still real; S2 now rejects it at emit.

## AA1 — a step must guarantee the file its own transitions route on

`gen_math_olympiad` died with:

```
Run failed: No matching transition from 'final_answer' with flags {'wrote_files': True}
```

`final_answer` was `mode: write` with `validation: [{tool: file_exists, files: ["*"]}]`
and two edges, both matching `from_file: final_verdict.json`. It wrote
`final_answer.md` — which satisfies `["*"]` — never wrote the verdict, and matched no
edge. **A complete proof, produced and thrown away.**

`["*"]` is the remediation *this gate's own C1 message* suggested. The gate taught the
emitter the shape that broke it.

* New rule `routing_file_unguaranteed`: an AGENT step whose transitions read
  `from_file: X` must guarantee X — via a `mode: content` fixed slot naming it, or a
  `file_exists` validation naming it. `["*"]` does not count.
* Gate steps are exempt (they route on an earlier step's file); tool steps are exempt
  (the file is the tool's contract, not the graph's).
* **Calibration: 2 hits across the 28 configs on this host — exactly the two steps that
  broke.** Same zero-false-positive bar as the V-rules and B2.
* C1's own message now names the routing file instead of suggesting `["*"]` blindly;
  the `["*"]` hatch survives only for a step that routes on nothing.

## AA2 — `file_exists` was blind to directories, and its error message hid them

The round's most damaging finding, and the one that had been failing *correct* work.

`gen_mcp_server_builder`'s `B1` was asked for a Python package and **produced it,
correctly**, on all four attempts. The trace shows the agent returning valid JSON with
all four files each time, and the files are on disk:

```
B1.tmp/pyproject.toml
B1.tmp/src/word_freq/{__init__,__main__,server}.py
```

Its validation declared `files: [src, src/word_freq, …]`. Every one existed. The check
failed anyway — `passed = f.exists() and f.is_file()` — and the message read:

```
File not found: src (expected in .../B1.tmp). Files present: pyproject.toml
```

…because the sibling listing filtered out directories too. **The agent was told its
directory was missing while it was sitting right there**, rewrote the same tree four
times, and the run failed blaming the agent.

The `["*"]` form had the same defect from the other side: `rglob("*")` yields
directories, each was tested with `is_file()`, so the canonical "assert this step wrote
SOMETHING" validation **failed for any step that created a subdirectory** — the exact
validation C1 tells every write-mode step to add.

Fixed in `skillflow/tools/file_exists/impl.py` (+11 tests):
* a declared path that EXISTS satisfies the check; a directory counts, an *empty*
  directory does not (that is the wrote-nothing case wearing a disguise);
* a glob is judged as a whole over FILES only — matched ⇒ pass, nothing ⇒ fail;
* the listing shows directories, marked `src/`, so the agent can see what is there.

Two skillflow tests asserted the literal string "File not found"; the phrase is now
"Not found", because calling a missing *directory* a missing *file* is precisely how
this cost four rounds. No code depends on the phrase (checked both repos).

Deployed to the container as a dev wheel and verified in-container before re-driving.

### Why this was invisible until now
Every prior round tested generation, where steps write flat files into a step dir.
`B1` is the first step in this project's history asked to produce a **directory tree**.
C1 — which I added — is what made `file_exists` load-bearing on every write-mode step,
so the blindness went from latent to run-killing in the same change that was supposed
to make silent failures loud.

## AA3 — a step delivered its work four times and every envelope was discarded

`gen_mcp_server_builder`'s test-authoring step, four validation attempts, full trace:

```
1  {"read_file": {"file_path": "src/wordfreq/server.py"}}
2  {"thought": "I need to read server.py ... The previous attempt failed because it
    didn't write anything.", "action": "read_file", "path": "src/wordfreq/server.py"}
3  {"file": "tests/test_tools.py",      "content": "\"\"\"Tests for all tools...\"\"\" ..."}
4  {"file_path": "tests/test_tools.py", "content": "..."}
```

Attempts **3 and 4 contained the entire pytest suite**. Both were discarded, because
the dispatcher accepts only `{"files": {...}}` or `{"actions": [...]}` and the existing
normalizer's "bare filename keys" rule matches neither (`file`/`content` have no dot in
the KEY). Attempts 1 and 2 were the agent asking to read the module before writing
tests — a reasonable request that was never executed, so the retry feedback ("Nothing
was written") answered a question the agent had not asked. It could not escape: it
asked, got no answer, and was told it had produced nothing.

Fix: `PipelineEngine._normalize_payload`, extracted from the tool loop so it is
testable at all, now also absorbs
* `{path|file|file_path|filename, content}` → one file, and
* `{"<tool>": {params}}` / `{"action": "<tool>", ...}` → one action, restricted to
  tools the step actually has (plus the step-control pseudo-tools), so an invented
  name is still not conjured into an action.

`content` and the path aliases were added to `meta_keys` in the same pass: the bare-key
rule would otherwise write a file literally called `content`.

14 tests, one per shape observed in the trace, plus the negatives that matter (a pure
`{"thoughts": ...}` turn must stay a thinking turn, an unknown tool name must not
become an action, canonical payloads must pass through untouched).

**Not resolved:** the trace shows exactly ONE turn per validation attempt even though
the role's budget resolves to 10, and no `agent_message` event. The multi-turn
exploration loop should have given the read-requesting agent another turn regardless of
shape. The normalizer fix removes the need in this case but does not explain the turn
count, and I am not going to guess at it — it is written down here as open.

## S1 — one owner for "a declared tool does not exist" *(skillflow)*

The same swallow was written twice under the same false comment. A1 fixed
`AgentRegistry.resolve_tool_schemas`; `core.py:1142` — the CAPABILITY grant path — kept
discarding, and nothing downstream covers it: `graph.validate()` sees only the YAML, a
capability's tool list is not in the YAML at all, and A2's linter checks `tool_name`
fields. So a capability whose tool is missing grants nothing, silently, on exactly the
path `pipeline_forge`'s tool-build step uses (`capability: tool_creation`).

* `SkillFlow._resolve_tool_schema(name, owner=...)` — the ONE place a tool name becomes
  a schema. Records the miss, warns once per (owner, name), returns None rather than
  raising (hosts legitimately register capabilities before tools; the record clears on
  the next resolve).
* `SkillFlow.unresolved_tools()` → `{owner: [names]}` across both grant paths, owners
  keyed `agent_config:<name>` / `capability:<name>`.
* The invariant is now checkable: no `load_schema` call outside the resolver.

Not yet observed failing — this is the one item in this round fixed BEFORE it cost a
run rather than after.

## AA4 — `create` and `edit` were not recognised as write tools *(the deepest one)*

AA3's normalizer fix landed and the same step failed again, with four NEW envelopes —
each still carrying the complete file:

```
{"create": {"file_name": "tests/test_tools.py", "content": "\"\"\"Tests for all tools…"}}
{"action": "read_file", "file": "src/word_frequency/server.py"}
{"tool_use": "write", "path": "tests/test_tools.py", "content": "…"}
{"action": "create", "arguments": {"path": "tests/test_tools.py", "content": "…"}}
```

The first one is a *correctly formed call to a tool the step actually has*. It was
still dropped. `PipelineEngine` classified write calls by NAME PREFIX:

```python
constrained_writes = {k for k in self._tool_schemas
                      if k.startswith(("write_", "create_", "append_"))}
...
else:
    write_calls = [a for a in actions
                   if a.get("tool","").startswith("write_") or a.get("tool") == "write"]
```

A `mode: write` step's schemas are `create` / `edit` / `write` / `finish_step` +
read tools. None of `create` / `edit` starts with `create_` / `write_`, and
`constrained_writes` is therefore EMPTY, so the else branch runs — and it accepts only
`write_*` and the bare `write`.

**So `create` and `edit` — the two tools skillflow injects into every `mode: write`
step, and the two the palette explicitly teaches agents to call (D1) — were classified
as neither a write, nor a read, nor an unknown write. Silently discarded.** The
unknown-write branch, which exists precisely to say "you may only call X", also missed
them: it tested `startswith("create_")`, and `create` has no underscore.

Only an agent that happened to pick the bare `write` could produce output at all.

Fixed with one classifier, `PipelineEngine._is_mutation_tool(name, tool_schemas)`:
a mutation is a name the step ACTUALLY HAS that is `write`/`create`/`edit` or carries a
slot prefix. Membership in the step's own schemas is required, so an invented name still
falls through to the unknown-write branch and the agent gets told what it may call. The
unknown-write detection now also catches a bare `create`/`edit` used in a content-mode
step that has no such tool.

### This is very likely the X-series' real root cause
Round 4's `scaffold_maker` "wrote nothing four times" was attributed to its template
naming `create_file`/`write_file` (X2). That was true and worth fixing — but D1 then
taught every emitter to use `create`, and `create` was a tool the engine could not
execute. The X-series fix moved agents from one unusable vocabulary onto another.

### Pattern
This is produce-then-discard again, and the most expensive instance yet: not a
diagnostic thrown away, but the **work product itself**. Three separate layers each
discarded a complete deliverable — `file_exists` (AA2), the payload envelope (AA3), and
the tool classifier (AA4) — and every one of them reported the same thing to the agent:
"you wrote nothing".

## AA5 — the handler DISPATCHER also classified by name prefix *(the actual bottom)*

AA4 landed and the same step failed a third time, with four more envelopes. One of them
was `{"file_path": "tests/test_tools.py", "content": "..."}` — a shape AA3's normalizer
handles *and has a passing test for*. So the normalizer was not running at all.

It wasn't, because a different handler owns that step:

```python
has_read_tools  = any(not k.startswith("write") and k != "write" for k in ts)
has_write_tools = any(k.startswith("write") for k in ts)
```

`C1`'s schemas are `['read_file', 'create', 'edit', 'finish_step', 'read', 'search',
'list']` — **no `write` at all**, because `write` is injected only with
`allow_full_write`. So `has_write_tools` is False, and a `mode: write` step is routed to
the handler for steps that have no write path. It could not have written anything by any
means, in any envelope. `B1`, two steps earlier in the same pipeline, DID have `write`
(different `output` block) and took the correct handler — which is why one step in the
pipeline worked and its sibling was unfixable.

Fixed by routing the dispatcher through the same `_is_mutation_tool` classifier, and by
running the normalizer at all three payload-parsing sites rather than one.

### The shape of this bug family
Three layers classified the same thing by spelling, and each discarded finished work
while reporting "you wrote nothing":

| layer | test | what it missed |
|---|---|---|
| handler dispatch | `k.startswith("write")` | `create`, `edit` → wrong handler entirely |
| write-call classify | `startswith("write_")` or `== "write"` | `create`, `edit` → call dropped |
| payload parse | `{files}` / `{actions}` only | every other envelope a model produces |
| validation | `f.exists() and f.is_file()` | directories, and `["*"]` under any subdir |

Four independent places, one root: **deciding what something IS from how its name is
spelled, instead of from the registry that defines it.** That is the same root as S2's
`_FALLIBLE_TOOLS` allowlist and A1's swallowed `load_schema` — the system repeatedly
answers questions about tools without asking the tool.

## Round 6 outcome so far

| Pipeline | drive 1 | drive 2 | drive 3 |
|---|---|---|---|
| `gen_math_olympiad` | failed (AA1) | **completed, zero revisions** | — |
| `gen_skill_packager` | completed | failed (unsatisfiable generated tool) | tool fixed, pending |
| `gen_mcp_server_builder` | failed (AA2, B1) | failed (AA3/AA4/AA5, C1) | pending |

`gen_skill_packager`'s round-2 failure was NOT a framework defect: its generated
`skill_frontmatter_validate` asserted `basename(skill_dir) == frontmatter.name`, and
`skill_dir` is `$CONFIG_DIR/draft` — a directory the ENGINE named after the step. Write
SKILL.md at the top of `draft/` and the name check rejects it; write it under
`bibliography-to-bibtex/` and the tool reports "SKILL.md not found". Unsatisfiable, so
the maker oscillated between the two until the loop exhausted, on every run. The check
was removed (the frontmatter is the authority for the skill's name; the staging
directory is an implementation detail) and the lesson added to the rule table as
`tools_do_not_read_meaning_from_framework_paths`.

### AA5, corrected — the dispatcher change was reverted after measuring it

I first made the DISPATCHER mutation-aware, and described the blast radius as "a
content-mode reviewer with no read sources". Then I measured it instead of asserting it:
the change re-routed **26 write-mode steps across 26 configs** — every `mode: write`
agent step in the repo, including `dpe_default`'s `t_impl`, `subagent`'s `work`, and
`pipeline_forge`'s own `emit_graph` and `t_tool_impl` — from `_run_tool_step` onto
`_run_tool_content_step`.

Those 26 steps have always gone to `_run_tool_step`, and that handler writes perfectly
well. The defect was never the routing: it was `_run_tool_step`'s OWN `write_calls`
filter, which used the same prefix test and dropped `create`. Fixing the filter fixes
the defect and leaves all 26 steps exactly where they were.

**Reverted.** The dispatcher predicates are back to the prefix form, with a comment
saying why they stay that way, and a test asserts the routing is unchanged. Re-routing
the entire system to fix one handler is not the smaller change — and I had already
mis-stated its scope once.

### Rule-set calibration after all of this
Running the FULL rule set over all 28 configs on this host: `routing_file_unguaranteed`
now reports **zero** violations (its two true positives are fixed), and the fallible
rule reports exactly the 5 expected — 3 new true positives from S2 plus the 2 the old
allowlist already flagged. No new rule produces noise. The remaining 50 violations are
pre-existing findings in hand-written configs, which have never been subject to this
gate.

## Round 6 — what the drive proved

The final drive of `gen_mcp_server_builder` executed the pipeline's real logic for the
first time in its history:

```
A1 spec → A2 review ✓ → B1 scaffold (wrote_files) → B2 review ✗ → B1 → B2 ✗ → B1 → B2 ✓
   → B3 repo_apply ✓ (pyproject.toml + src/word_frequency/*)
   → C1 tests (wrote_files, FIRST ATTEMPT, zero validation retries)
   → C2 review ✓ → C3 repo_apply ✓ (tests/test_tools.py, committed)
   → D1 run_tests {passed: false} → back into the fix loop
```

Every mechanism the earlier rounds could not reach is now visibly working: a maker
writing files, a reviewer rejecting with feedback and the maker converging, `repo_apply`
landing a nested source tree, a real pytest run producing a real verdict, and the graph
routing on it. `C1` — the step that failed 12 times across three drives — passed on its
first attempt with no validation retries.

**Six framework defects were found and fixed by driving, none of which generation could
have surfaced:** AA1 (routing file unguaranteed), AA2 (`file_exists` blind to
directories), AA3 (payload envelope), AA4 (`create`/`edit` not classified as writes),
AA5 (handler dispatch by name prefix), Z6 (archive tombstone). Four of the six were
discarding *finished work* while telling the agent it had produced nothing.

Suites after all of it: **AItelier 1308 passed / 9 skipped**, **skillflow 571 passed**.

### AA6 — `run_tests` and the `src/` layout *(recorded, deliberately NOT patched)*

`D1` produced a real, correct verdict: `passed: false`, because
`from word_frequency.server import word_frequency` fails — the generated project uses a
`src/` layout and pytest runs from the repo root with nothing putting `src` on the path.

This is the generated PROJECT's problem, not the framework's: a `src/`-layout project is
responsible for its own test discovery (`[tool.pytest.ini_options] pythonpath = ["src"]`
or an editable install), and the pipeline has a `D1 → B1` fix loop with the full pytest
output as feedback — this is precisely the situation that loop exists for. Patching
`run_tests` to guess a `PYTHONPATH` would paper over a real defect in the generated
artifact and make the objective gate lie.

Left running. Recorded so the next reader does not re-diagnose it as a framework bug.

## AA7 — the tool-gate feedback banner said "Tool failed" and nothing else

`gen_mcp_server_builder` ran its `D1 (run_tests) → B1` fix loop three times and never
fixed anything. Reading the maker's actual inputs settled why — and corrected a first
guess of mine along the way.

**Not** a context failure: the maker DID receive the full report as ordinary context —
`Step D1: ### test_report.json {"passed": false, "returncode": 2, "summary": "ERRORS …
ModuleNotFoundError: No module named 'word_frequency'"}`, 1027 characters, every round.
(An earlier read of mine looked only at the top-level `D1` key, which holds the tool's
result flags; the file content is in `_resolved_context`. The graph is correct: B1 reads
`{step: D1}` and the failing edge carries `feedback: true, max_loop: 3`.)

The defect is the BANNER. `core.py` injected `tool_result.get("error", "Tool failed")`,
and `run_tests` has no `error` key — it returns `{"written": "test_report.json",
"passed": false}` and puts pytest's output in the file. So the most prominent line in
the retried prompt, headed **"⚠️ Reviewer / User Feedback — MUST ADDRESS before
resubmitting"**, read exactly:

```
Tool failed
```

Two harms, not one: it carried no information, and it misdescribed the failure — the
agent was told a *tool* had failed when what had failed was the *tests*. It competed
with the real report for the agent's attention and won.

Fixed with `_describe_tool_failure(tool_result)`: prefer `error`; otherwise lead with a
`summary`/`message` field, state the flags the gate actually returned, and point at the
artifact (`"The full detail is in 'test_report.json' — read it before retrying."`).
Nested values are excluded so the banner stays a pointer rather than a dump. 7 tests.

**Verified live** — on the confirming drive, `B1`'s `_feedback` reads:

```
The gate returned passed=False, written='test_report.json'.
The full detail is in 'test_report.json' — read it before retrying.
```

One intermediate run did still show `"Tool failed"` after the wheel was installed and
after `inspect.getsource` in the running container showed the new call site, which I
recorded here as unexplained rather than guess at it. The next restart cleared it, so
the most likely explanation is simply that the `pip install` and the `docker compose
restart` in that one command raced — the server process came up against the old module
while a fresh `docker exec` saw the new one. Worth remembering as a deployment trap:
**verifying a container fix with `docker exec` proves the FILE is right, not that the
running server loaded it.** Check a live artifact, not an exec'd import.

Same family as T8/F2 and the rest: the information existed, and the channel that was
supposed to carry it substituted a placeholder.

### The run's own failure was real
Three fix rounds later the run failed at `D1` with the tests still red. The remaining
defect is AA6 — a `src/` layout with no `pythonpath` — which belongs to the generated
project, and is what the fix loop should now have a fair chance at with a banner that
points somewhere.

## Confirming drive — the narrower fix is sufficient

With the dispatcher reverted (the `create`/`edit` fix living ONLY in
`_run_tool_step`'s write-call filter, where the defect actually was):

```
A1 ✓ → A2 ✓ (first try) → B1 wrote_files → B2 ✓ (first try)
   → B3 repo_apply ✓ → C1 wrote_files (1 validation retry) → C2 …
```

`C1` — the step that failed 12 times across three earlier drives, in four different
envelopes each carrying the complete file — now writes. All 26 write-mode steps in the
repo keep the handler they have always used.

## AA8 — `actions` has as many spellings as the file envelope, and `_run_tool_step` reads only `actions`

The confirming drive got through the ENTIRE first pass
(`A1 → A2 → B1 → B2 → B3 → C1 → C2 → C3 → D1`) and then failed on C1's *second* visit.
The trace shows real progress and two remaining gaps:

```
117  agent_response  {"action": "read_file", "file": "src/word_frequency/server.py"}
118  read_file       ← EXECUTED. AA3 working: the read-before-write path now runs
119  read_file
120  user_prompt     turn=2      ← and the agent got its second turn
121  agent_response  {"tools": [{"tool": "write_file", "args": {"file": …, "content": …}}]}
123  validation_failed   "Nothing matching '*' was written"
126  agent_response  {"command": "create", "path": "tests/test_tools.py", "content": …}
128  validation_failed   "Nothing matching '*' was written"
```

**Two defects:**

1. **Container/key aliases.** `tools` (and `tool_calls`) for `actions`, `args` for
   `params`, `name`/`command` for `tool`. This matters even when the tool NAME is wrong
   — `write_file` does not exist — because only once a call reaches `actions` does the
   unknown-write branch fire and tell the agent what it may actually call. Dropped
   silently, the agent learns nothing and guesses again.

2. **`_run_tool_step` reads only `actions` and ignores `files` entirely**, and it is the
   handler every `mode: write` step in this repo uses. So AA3's `{path, content}` →
   `files` normalisation landed in a dead end there. The envelope now also emits the
   equivalent ACTION, using a mutator the step actually has (`create`, else `write`).
   Handlers that read `files` take it first and stop, so there is no double write.

**And a third, worse than either:** `if not actions: … break` — one unrecognised shape
ended the whole STEP rather than costing a turn. That is why every failing attempt shows
exactly one turn: the agent asked a question, the handler ended the step, and the step
reported having written nothing. Now it emits actionable feedback (naming the tools the
step actually has), spends a turn, and continues — bounded by the same turn budget.

This is the same root as AA4/AA5 one level up: the system decides what a payload IS from
the exact key it was spelled with, and discards everything else — including, four times
over, a complete pytest suite.

### AA8's guard — a regression I introduced and caught before it shipped
`tools` is also a perfectly ordinary CONTENT key. The spec-writing step in this very
pipeline emits `{"tools": [{"name": "word_frequency", "description": …}]}` as its
output; converting that to actions would invent a call to a tool named
`word_frequency`, produce no writes, and fail the step — trading one silent discard for
another.

First guard ("an entry must name a KNOWN tool") was too strict and re-broke the live
case, where the name is `write_file` and does not exist. The discriminator that
separates them correctly is **arguments**: a call carries `args`/`params`; a content
record carries `description`. So an entry converts if it has a params-shaped key, or if
it names a tool the step can call (the no-arg case). Both directions are pinned by
tests, including the exact spec payload this pipeline produces.

## AA9 — a turn whose every write FAILED ended the step before the reason could be shown

The drive after AA8 got through the whole first pass again and failed on C1's second
visit. The trace is unambiguous, and this time the agent did everything right:

```
117  agent_response  {"thoughts": "...", "actions": [{"tool": "create", "params": {...}}]}
118  tool_call    create   {"source": "agent", "params": {"path": "tests/test_tools.py", …}}
119  tool_result  create   {"error": "create: 'tests/test_tools.py' already exists —
                            use 'edit' to change an existing file"}
121  validation_failed    "Nothing matching '*' was written. Directory is empty: …/C1.tmp"
```

The canonical envelope. A real tool. A real, actionable error — `create` refused because
the file exists **in the repo**, committed by `C3` on the first pass, even though this
step's own staging was empty. W1 delivered that error into `tool_results`.

And then:

```python
if write_calls:
    for action in write_calls:
        result = self._exec_tool(action)
        if "error" in result:
            tool_results.append(f"Write error: {result['error']}")
            continue
    self._emit("files_written", ...)
    break          # ← unconditional, even when written_files is EMPTY
```

**The loop ended.** `tool_results` is only ever rendered into the NEXT turn's prompt, and
there was no next turn. The agent was never given the turn in which it could have
switched to `edit`. The step reported writing nothing and failed validation — for the
third time in this investigation, a step that had done its job correctly was recorded as
having produced nothing.

Fixed in both handlers: `break` only when a write actually landed. If every write
errored, the errors are fed back and a turn is spent, bounded by the same budget.

### The chain, end to end
Nine defects, one root, and the last four are literally the same sentence:

| | the system had | and discarded it by |
|---|---|---|
| AA2 | the files on disk | asking `is_file()` of a directory |
| AA3 | a complete delivery | not recognising its envelope |
| AA4 | a correct `create` call | classifying tools by name prefix |
| AA5 | a working handler | dispatching by name prefix |
| AA8a | a tool call in `tools:` | insisting on the key `actions` |
| AA8b | a `{path, content}` delivery | a handler that reads only `actions` |
| AA8c | an unreadable shape | `break` instead of a turn |
| AA9 | an actionable tool error | `break` instead of a turn |
| AA7 | the failure's reason | substituting the string "Tool failed" |

Every one reported the same thing to the agent: **"you wrote nothing."**

## Z5 closed — `gen_skill_packager`'s shipped fail-open, fixed via the gate that found it

Re-gating all three test pipelines after the rule changes:

```
gen_math_olympiad          passed=True
gen_mcp_server_builder     passed=True
gen_skill_packager         passed=False — package: 'skill_package_zip' can fail, but its
                                          only transition is unconditional
```

That is S2 doing exactly what it was built for: the rule now sees a GENERATED tool's
failure contract, and the fail-open Z5 recorded is still in the shipped artifact.

Fixing it also exercised V4 usefully. My first attempt added
`[{done_gate, passed:true}, {done_gate}, {fail_gate, passed:false}]` and the gate
rejected it — the unconditional fallback to `done_gate` makes the failure edge a no-op.
Correct shape, given that the tool returns `{"zip_path": …}` on success (no `passed`
key) and `{"passed": false, "error": …}` on failure, is the failure as the MATCHED case
and success as the fallback:

```yaml
transitions:
  - {to: fail_gate, match: {passed: false}}
  - {to: done_gate}
```

All three now gate clean.

*(One latent weakness noted, not fixed: `_failure_rejoins_success` assumes `matched[0]`
is the success edge. With the failure edge written first — the correct idiom for a tool
that returns no `passed` on success — that assumption is inverted, and the rule happens
not to fire rather than reasoning correctly. It is right on every case measured here,
but it is right for a slightly wrong reason.)*

## AA10 — my own normalizer overruled the agent's explicit tool choice

AA8b/AA9 both verified live in one trace, and then exposed the next link:

```
88  agent_response  {"file_path": "tests/test_tools.py", "content": …}
89  tool_call  create        ← AA8b: envelope became a real action and RAN
90  tool_result create       "already exists — use 'edit' to change an existing file"
91  user_prompt  turn=2      ← AA9: the failure cost a TURN, not the step
92  agent_response  {"tool": "edit", "file_path": …, "content": …}   ← self-corrected
93  tool_call  create        ← …and my rule rewrote `edit` back to `create`
94  tool_result create       identical error
```

The agent read the error and did the right thing. The single-file-envelope rule — added
this session to stop deliveries being discarded — picked the mutator itself and ignored
the `"tool": "edit"` the agent had written. **Produce-then-discard, introduced by the fix
for produce-then-discard.**

Fixed: an explicitly named tool the step actually has wins; the `create`/`write`/`edit`
default applies only when the payload names none. A named tool the step LACKS still
falls back to a real one, so no uncallable action is emitted.

`create`'s refusal itself is correct and was left alone — its docstring is explicit that
whole-file rewriting from a model's partial view silently drops regions it did not
reproduce, which is why `edit` exists. If `edit` with `content` is then the wrong shape,
`edit`'s own error says so, and AA9 now guarantees a turn to act on it.

---

# Round 6 — final tally

**Ten framework defects, all found by DRIVING**, none of which generation could have
surfaced. Rounds 1–5 tested generation only; this round ran the pipelines.

| # | defect | where |
|---|---|---|
| Z6 | an archive tombstone no registration lifts | `core/pipeline_registry.py` |
| AA1 | a step must guarantee the file its transitions route on | `forge_registry_check` (new rule) |
| AA2 | `file_exists` blind to directories; `["*"]` fails under any subdir | `skillflow/tools/file_exists` |
| AA3 | payload envelopes other than `{files}`/`{actions}` discarded | `PipelineEngine._normalize_payload` |
| AA4 | `create`/`edit` not classified as write calls | `PipelineEngine._is_mutation_tool` |
| AA5 | *(reverted)* the dispatcher's prefix test — measured, not the smaller fix | — |
| AA6 | `run_tests` vs a `src/` layout — the generated PROJECT's defect | recorded, not patched |
| AA7 | the tool-gate banner said only `"Tool failed"` | `skillflow/core._describe_tool_failure` |
| AA8 | `tools`/`args` aliases; `_run_tool_step` reads only `actions`; `break` on an unreadable shape | `PipelineEngine` |
| AA9 | a turn whose every write FAILED ended the step before the reason could be shown | `PipelineEngine` (both handlers) |
| AA10 | the envelope normalizer overruled the agent's explicit `"tool": "edit"` | mine, from AA8 |

Plus the four structural items (S1–S4) and Z5's closure.

**Three were mine**, introduced this session or earlier: C1's `["*"]` remediation caused
AA1; AA5's over-broad first fix; AA10 was introduced by AA8's fix. All three were caught
by measuring rather than asserting, and all three are recorded here with the evidence
that overturned my first read.

**The root, once:** the system HAD the thing — the files, the delivery, the call, the
handler, the error — and discarded it because of how it was spelled, or because a loop
ended before the reason could be delivered. Every one reported the same sentence to the
agent: *"you wrote nothing."* When an agent looks incompetent across several rounds,
check what the framework did with what it produced before concluding anything about the
agent.

**Progression of `gen_mcp_server_builder`, which had never passed its second step:**

| drive | reached |
|---|---|
| 1 | `B1` — failed (AA2) |
| 2 | `C1` — failed (AA3/AA4/AA5) |
| 3 | `D1` — full first pass, fix loop, failed (AA7 banner) |
| 4 | `C1` 2nd visit — failed (AA8) |
| 5 | `C1` 2nd visit — failed (AA9) |
| 6 | **three full laps of the fix loop**, every review passing first try, multi-turn
      read-then-write working, ending on the declared give-up terminal |

The remaining failure is AA6 — the generated project's `src/` layout — which is the
artifact's own defect, correctly surfaced by an objective gate, and the loop now ends
cleanly through a declared `failed` terminal rather than an unmatched transition.

### What a working step now looks like

C1's third-lap trace, after all of the above, is the clearest evidence that the chain is
fixed — the step is having an actual conversation instead of being cut off:

```
turn 5  {"file_path": …, "content": …}   → create → "already exists — use 'edit'"
turn 6  edit(...)                        → "edit: 'old_str' is required and must be non-empty"
        read_file("tests/test_tools.py") → (to obtain the exact text for old_str)
turn 7  …
```

Try → precise error → gather what the error asked for → retry. Every one of those hops
existed as a capability before today and none of them was reachable: the envelope was
discarded (AA3/AA8), the tool call was not classified as a write (AA4), and the first
failure ended the step (AA8c/AA9). The step got exactly ONE turn and reported writing
nothing.

## AA11 — the flat call form recognised `action` but not `tool`

Watching the conversation from the previous section continue, turn 7:

```
{"tool": "edit", "path": "tests/test_tools.py", "old_str": "…", "new_str": "…"}
```

A perfectly formed `edit` — the agent had been told to use `edit`, had been told
`old_str` was required, and had just read the file to obtain it. It did everything
asked. **Dropped**, because the flat call form recognised only `action`, `command` and
`function` — not `tool`, which is the most natural spelling of the four. No `content`
key, so the single-file envelope did not match either; the payload fell through every
rule and the turn was spent on nothing.

Fixed: all four name keys are accepted for the flat form, and the name key itself is no
longer passed through as a parameter. An unknown name still becomes no action, so
invented tools continue to route to the branch that teaches.

This is the eleventh instance of one root, and the third one *inside the fix for it* —
the normalizer's job is to stop discarding deliveries, and it was still discarding this
one on a key it did not happen to list.

## Round 6 close-out — the drive ended exactly as designed

`gen_mcp_server_builder`, final drive:

```
A1 A2 B1 B2 B3 C1 C2 C3 D1        ← full first pass
   B1 B2 B3 C1 C2 C3 D1           ← fix lap 1
   B1 B2 B3 C1 C2 C3 D1           ← fix lap 2
   B1 B2 B3 C1 C2 C3 D1           ← fix lap 3, D1->B1 budget spent
status: failed    error_reason: Node 'give_up_gate' reached
```

**Thirty steps, not one of which failed.** Every review passed, every `repo_apply`
landed, every `run_tests` produced a real verdict, and when the fix budget ran out the
graph took its declared give-up edge and ended `failed` with a legible reason — not
"no matching transition", not "cycle limit exceeded". That is the T5/AA1 machinery, the
V-rules, and this round's ten fixes all working together.

*(A correction to my own reading during the run: I first said it had ended AT `D1`
rather than at `give_up_gate`, because `give_up_gate` is absent from the completed-step
list. Gate steps are pure routing and do not get a completed step row — the run's
`error_reason` is the authority, not the step list.)*

The one thing still red is AA6, the generated project's `src/` layout: its own tests
cannot import it, and its maker did not add `pythonpath` in three attempts. That is the
artifact's defect, surfaced by an objective gate exactly as intended, and deliberately
not papered over.

---

# AA6 re-examined with the traces — and the real root found

The user asked which of prompt / tool / context was missing. Checking all three
against `gen_mcp_server_builder`'s final drive settles it, and overturns my own AA6
write-up.

## Context: present and complete
Every fix lap, `B1` received `Step D1` with the full pytest report (1250 chars,
traceback included), plus `Step A1` (the spec), `Step B2` (the review), and AA7's
banner. Nothing was truncated or missing.

## Tools: present, and used well
`['read_file', 'create', 'edit', 'write', 'finish_step', 'read', 'search', 'list']`.
The trace shows the agent using them competently — reading five files before writing
five, diagnosing correctly each lap:

* lap 2 → *"tests expect a list of tuples, the function is async"* → rewrote the server
* lap 3 → *"`src` is not on the Python path"* → wrote a `tests/conftest.py` that adds it
  **and fixed the layout problem** (the later laps import `word_frequency_server` fine)
* lap 4 → *"`InitializationCapabilities` no longer exists in mcp.server"* → removed it

**AA6 needed a two-part correction, and my first correction over-shot.** The
`src/`-layout failure is real and RECURRING — it is the first thing the tests hit on
every drive, including the verification drive after the prompt fix
(`ModuleNotFoundError: No module named 'word_frequency'`). What I got wrong the first
time was calling it the FINAL failure: the agent fixes it (lap 3 of the previous drive,
by writing a `tests/conftest.py` that puts `src` on the path) and then hits a second,
deeper one:

```
src/word_frequency_server/server.py:2: from mcp.server import Server, InitializationCapabilities
E   ImportError: cannot import name 'InitializationCapabilities' from 'mcp.server'
```

So there are TWO defects in the generated artifact, in sequence: the `src/` layout with
no `pythonpath` (which the agent can and does fix from the error text), and then a
guessed third-party symbol — the real name is **`InitializationOptions`** — which it
cannot fix, because it cannot check: the read tools are closures over the project root / step staging
/ step output ("the agent never sees or guesses paths"), so `site-packages/mcp/server/`
is unreachable by design. It can only guess again. *That* is a genuine capability gap,
and it is the honest content of AA6.

## Prompt: THIS is what was missing — and it is the root of the whole family

`core/prompt_assembler.py`, both the native and JSON branches:

```python
write_tools = sorted(n for n in (tool_schemas or {})
                     if n.startswith(("write_", "create_", "append_")) or n == "write")
```

The same name-prefix classification as AA4 and AA5 — a fourth instance, and this one
sits *upstream of all of them*. It excludes `create` and `edit`. For a `mode: write`
step **without** `allow_full_write` the schemas are
`[read_file, create, edit, finish_step, read, search, list]`, so the selection is
**empty** and the entire `[Output Delivery — REQUIRED]` section is skipped.

Verified directly against the two steps of this run:

```
B1 (allow_full_write: true)  → Output Delivery present, lists: `write(file, content)`
C1 (no allow_full_write)     → Output Delivery ABSENT — no write vocabulary at all
```

**`C1` is the step that failed twelve times across this investigation.** It was never
told it could write, never shown a tool name, never shown a parameter list. It was
guessing at a contract nobody had stated — which is precisely why it produced eight
different envelopes (`{"files"}`, `{"file","content"}`, `{"read_file":{…}}`,
`{"tools":[…]}`, `{"command":"create"}`, `{"tool":"edit"}`, …), every one answered with
"you wrote nothing".

And the one tool a luckier step *was* shown carries the advice *"Prefer 'edit' for
existing files"* — recommending a tool the prompt does not document, which is exactly
why the agent later called `edit(file, content)` and got `'old_str' is required`.

**Fixed:** one `is_mutation_tool(name, tool_schemas)` in `core/prompt_assembler.py`, used
by the prompt to decide what to ADVERTISE and imported by `PipelineEngine` to decide
what to EXECUTE. Previously those were two separate prefix tests that disagreed with the
framework's actual injection.

### What this reframes
Every fix from AA3 onward — accept more envelope shapes, classify more tool names,
honour the agent's named tool — was work on the RECEIVING end. They were all real, and
they all mattered. But the reason so many shapes arrived in the first place is that the
SENDING end was never told the shape. I spent the session teaching the parser to
understand guesses, when the deeper fix was to stop the guessing.

The X-series ("a maker that produced nothing four times") is very likely this same
defect, seen from the far end.

## Verified — the prompt was the root cause

Same step (`C1`), same pipeline, same seed, with the write vocabulary now stated:

```
turn 1  {"actions":[{"tool":"read_file","params":{"file":"src/word_frequency/server.py"}}]}
turn 2  {"actions":[{"tool":"create","params":{"file":"tests/test_tools.py","content":…}}]}
        create → tests/test_tools.py → file_exists ✓ → completed
```

Canonical envelope, correct tool, correct parameter names, **first attempt — zero
validation retries, two turns**. And its prompt now carries:

```
Available write tools:
  - `create(file, content)` — Create a NEW file… Fails if the file already exists —
    use 'edit' to change an existing file.
  - `edit(file, old_str, new_str)` — Surgical…
```

Against the same step's history in this investigation: **twelve failures across four
drives, eight different envelope shapes**, `edit(file, content)` called because `edit`
had never been documented, and a `create` that could not be recovered from because the
error arrived after the loop had ended.

The normalizer work (AA3, AA8, AA10, AA11) was all real and stays — a model will still
vary its output shape, and those paths are now covered and tested. But it was treating
the symptom. **A step that is told what it may call, with signatures, produces the
canonical shape on the first turn.**

### Answer to "prompt, tool, or context?"
* **context** — complete, every lap, never the problem
* **tools** — all present, and used competently once known
* **prompt** — the whole of it. And it failed by the same name-prefix classification
  bug as AA4/AA5, one layer further upstream, where it was invisible because its
  symptom looked like an incompetent agent.

### The verification drive's first pass, for the record

```
A1 ✓  A2 ✓  B1 ✓  B2 ✓  B3 ✓  C1 ✓  C2 ✓  C3 ✓  D1(tests) → red
```

Nine steps, **every one `vretry=0`**, no reviewer rejection anywhere, straight to the
objective gate — the cleanest pass of the whole investigation. The only red is the
generated project's own `src/`-layout import, which is exactly what an objective gate is
for. Framework noise is gone; what is left is the artifact's real quality.

## AA6, finally diagnosed — `run_tests` swallowed the install failure

Watching the verification drive's fix loop settled AA6 properly. The maker rewrote
`pyproject.toml` and its write landed byte-for-byte in the repo — the framework
delivered it correctly. But the fix was irrelevant to the problem, and the trace shows
why it could not have been anything else.

`run_tests` DOES install the project (`_install_project_deps` → `pip install -e .`).
Running that install by hand against the generated project:

```
pip._vendor.pyproject_hooks._impl.BackendUnavailable:
    Cannot import 'setuptools.backends._legacy'
```

The generated `pyproject.toml` declares

```toml
build-backend = "setuptools.backends._legacy:_Backend"     # does not exist
```

— a hallucinated symbol, the same class of error as `InitializationCapabilities`. The
install therefore fails with a message naming the exact cause, and
`_install_project_deps` discarded it **twice over**: `check=False`, then
`except Exception: pass`.

So pytest reports `ModuleNotFoundError: No module named 'word_frequency'`, the maker
reads a symptom with its cause removed, concludes "package discovery", and rewrites
`[tool.setuptools.packages.find]` — which was never wrong. Same loop every lap, until
the budget ran out. Across two separate drives.

**This is the session's root pattern in the objective gate itself**: the system ran the
command, received the explanation, and threw it away.

Fixed: `_install_project_deps` RETURNS the failure instead of swallowing it; the report
carries `install_error`, and when the suite is red the summary LEADS with it —

> The project could not be installed into the test environment, so its own package may
> be unimportable. Fix this FIRST — a ModuleNotFoundError below is most likely a
> consequence of it, not a packaging-discovery problem: …

The install still cannot fail the gate: a generated project may legitimately be an app
rather than an installable package. That reasoning was right; only the silence was wrong.

### AA6's final form
Not "`run_tests` doesn't handle `src/` layouts" (my first write-up) and not "the agent
fixed it and hit something else" (my second). It is: **the agent wrote a bogus build
backend, and the gate hid the error that said so.** Two of the three write-ups were
wrong because I reasoned from the pytest output instead of running the install.

### The message has to be the useful line, not the tail

First cut of the fix returned `" | ".join(tail[-4:])`, which on a pip failure is caret
rulers and vendored frames — information-free. `_install_failure_reason` now prefers the
exception line. Verified in the container against the exact generated `pyproject.toml`:

```
pip._vendor.pyproject_hooks._impl.BackendUnavailable:
    Cannot import 'setuptools.backends._legacy'
```

That is the sentence the maker needed and never received, across two drives and six fix
laps. Surfacing a reason is only half the fix; surfacing a *readable* one is the rest.

### Operational note
Two verification drives in this session were killed by my own `docker compose restart`
mid-run — the butler drives these inline, so a restart kills the driver. Restart only
between drives.

## AA6 — the actual defect, found by watching the fix land and still fail

The `install_error` fix surfaced nothing on the next drive (`install_error: None`) while
the import still failed. That ruled out the build-backend theory for this run and pointed
at the setup itself.

`_resolve_pytest_python` step 1:

```python
if importlib.util.find_spec("pytest") is not None:
    return sys.executable, None        # ← returns HERE, always, in the container
```

`_install_project_deps` is only called on the venv-provisioning path below it. pytest is
always importable in the server's interpreter, so **the project under test is never
installed**. And the pytest subprocess ran with:

```python
env = {**os.environ, "PYTHONPATH": str(repo)}    # repo ROOT only
```

So a FLAT layout imports fine and the standard `src/` layout **cannot import its own
package at all** — `ModuleNotFoundError` on every test module, every attempt, with no
edit to the project that could fix it. The maker was being asked to repair something
outside the project.

**That is AA6, and it is a framework defect after all** — not the artifact's, and not the
"agent can't inspect its dependencies" gap I settled on last time (that one is real too,
but it is a different failure that only appears once this one is out of the way).

Fixed with `_pythonpath_for(repo)`: repo root first (so root-level conftest/packages keep
winning), then `src/` when it is a directory, then any inherited `PYTHONPATH`. Deliberately
NOT `pip install -e .` on this path — installing a generated project into the SERVER's own
interpreter would mutate the container's site-packages with LLM-authored package metadata
on every test run. This is what pytest's own `pythonpath = ["src"]` does, and it touches
nothing.

### AA6 was diagnosed wrong three times before this
1. *"`run_tests` doesn't handle `src/` layouts — the project's own problem"* — right
   symptom, wrong owner, and I declined to fix it on that basis.
2. *"the agent fixed the layout itself; the real issue is a guessed `mcp` symbol"* —
   true of one drive, not the general case.
3. *"the install failed on a bogus build-backend and the error was swallowed"* — real,
   fixed, and worth fixing, but not why THIS import fails.
4. The install never runs on this path, and `src/` was never on `PYTHONPATH`.

Each wrong answer came from reasoning about the pytest output. Each correction came from
running the thing myself — the install by hand, then the resolver's actual branch. The
last one only surfaced because a fix that should have explained the failure came back
saying nothing was wrong.

## Verified live — the maker is finally told the truth

Drive after both fixes, `D1`'s report:

```
The project could not be installed into the test environment, so its own package may be
unimportable. Fix this FIRST — a ModuleNotFoundError below is most likely a consequence
of it, not a packaging-discovery problem:
    pip._vendor.pyproject_hooks._impl.BackendUnavailable:
        Cannot import 'setuptools.backends._legacy'

==================================== ERRORS ====================================
ImportError while importing test module 'tests/test_tools.py'…
```

The cause, named, above the symptom. Six fix laps across three earlier drives were spent
rewriting `[tool.setuptools.packages.find]` — metadata that was never wrong — because
this sentence existed inside a `subprocess.run(..., check=False)` and was thrown away.
The bogus `build-backend = "setuptools.backends._legacy:_Backend"` is now the first
thing the maker reads.

Both paths are covered: the venv path surfaces the install failure (this run), and the
fast path — where `_install_project_deps` is never called at all — now puts `src/` on
`PYTHONPATH` so a standard layout can import itself.

First pass of this drive: `A1 A2 B1 B2 B3 C1 C2 C3 D1`, **every step first-try, zero
validation retries.**

### The loop closed, end to end

Checked against the live project mid-drive:

```
backend:             build-backend = "setuptools.build_meta:__legacy__"
install now says:    (SUCCESS)
PYTHONPATH:          <repo>:<repo>/src
```

1. the gate surfaced the real cause instead of discarding it,
2. the maker read it and changed the exact line that was wrong —
   `setuptools.backends._legacy:_Backend` → `setuptools.build_meta:__legacy__`,
3. and the change actually resolves the blocker: the editable install now succeeds.

Three earlier drives spent their entire fix budget rewriting
`[tool.setuptools.packages.find]`, because the sentence naming the real fault was
produced by `pip` and thrown away by `check=False` + `except: pass`. This is the first
time the objective gate has told the truth and the maker has acted on it.

## AA12 — an ImportError knows the answer and does not say it

With packaging fixed, the run's second `D1` shows the package importing correctly and
failing one line later:

```
src/word_frequency/server.py:4: from mcp.server.models import InitializationCapabilities
E   ImportError: cannot import name 'InitializationCapabilities' from 'mcp.server.models'
```

`InitializationCapabilities` does not exist; the real name is `InitializationOptions`.
The agent guessed it on three separate drives and cannot check — the read tools are
closures over project root / step staging / step output, so `site-packages` is
unreachable by design.

But **"cannot import name X from Y" means Y imported fine.** The answer is sitting in
the interpreter that just raised, and the message throws it away. Exactly the shape of
AA2, where `file_exists` reported a directory missing while listing only files and the
directory sat right there.

`_explain_missing_names` now appends, in the same interpreter pytest used:

```
[what those modules really export]
'mcp.server.models' has no 'InitializationCapabilities'. It actually exports: …
  Closest by name: InitializationOptions
```

Best-effort, silent on failure, bounded output, each module reported once — it can only
ever add information. This closes the capability gap without granting the agent
filesystem access to site-packages: the gate answers the question instead.

**Twelve defects, one root, start to finish.** The system had the files, the delivery,
the call, the handler, the error, the agent's corrected tool choice, the install's
explanation, and the module's real exports — and discarded every one of them, each time
reporting some version of "that didn't work" with the reason removed.

## Both gate fixes verified live, in sequence, on one drive

Lap 1 → `D1`:
```
The project could not be installed into the test environment… Fix this FIRST:
    BackendUnavailable: Cannot import 'setuptools.backends._legacy'
```
The maker read it and wrote `build-backend = "setuptools.build_meta"` — the canonical
value, and the correct fix. (Same behaviour on the previous drive, which produced
`setuptools.build_meta:__legacy__`. Two independent runs.)

Lap 2 → `D1`: `install_error: None` — the install now succeeds, the package imports, and
the failure moves one line deeper, carrying its own answer:
```
E   ImportError: cannot import name 'InitializationCapabilities' from 'mcp.server'
'mcp.server' has no 'InitializationCapabilities'. It actually exports: FastMCP,
InitializationOptions, NotificationOptions, Server, auth, …
  Closest by name: InitializationOptions
```

That is the symbol the agent guessed wrong on three separate drives, with no way to
check. It is now the first thing it reads. Each fix moves the failure strictly forward:
packaging → import → the actual API call.

### The complete chain, in one lap

Lap 3's maker, after reading the enriched ImportError:

```
tool_call    edit   {'file': 'src/word_frequency/server.py',
                     'old_str': '            InitializationCapabilitie…'}
tool_result  edit   (no error)
```

A surgical `edit` with a correct `old_str`, landed first try. Three things had to be
true for that single call to happen, and none of them was true this morning:

1. the prompt documents `edit(file, old_str, new_str)` — it advertised no write tools at
   all for this step shape (the root),
2. the gate told it WHICH symbol was wrong and what the module really exports (AA12),
3. `edit` is recognised as a write call and executes (AA4), in a handler that gives a
   turn rather than ending the step on the first error (AA9).

Prompt → gate → execution. The agent was never the problem.

---

# The feedback gap, reviewed as a class (2026-07-29)

Twelve instances over six drives were fixed one at a time. This pass asks what
*generates* them, on the premise that a thirteenth was already in the tree.

## The census was aimed at the wrong syntax

The 2026-07-08 audit (`silent-swallow-audit`) AST-scanned for
`except Exception|BaseException|bare:` whose body discards the error, classified
172 sites, and fixed ~14 on "wiring/registration/execution" paths. It is recorded
as having missed `resolve_tool_schemas` despite that being a registration path.

It did not miss it. **It could not have seen it.** The original source was

```python
except ImportError:
    pass  # tool not found — graph validation will catch
```

a NARROW handler, outside the census's own stated filter. And that is the smaller
half of the problem: **ten of the twelve defects contain no exception handler at
all.** Their syntactic forms:

| form | instance |
|---|---|
| narrow `except X: pass` with a justifying comment | `resolve_tool_schemas`, capability grant |
| `check=False`, result discarded | `_install_project_deps` |
| `.get(key, <uninformative default>)` | `"Tool failed"` banner |
| if/elif chain with no `else` | payload normalizer |
| name-prefix classification instead of asking the registry | `create`/`edit`, `write_*`, `_is_mutation_tool` |
| a predicate that is simply wrong | `file_exists` and `is_file()` |
| empty filter ⇒ skip the whole section | `prompt_assembler` |
| `break` before the reason is rendered | the turn loop, twice |
| reading the field off the wrong instance | `_validation_error` |

A second census of `except: pass` would have found at most two of twelve. Counting
it is not the exercise.

**Reproduced today** (AST, excluding `.venv` and worktree copies): AItelier 125
broad-discarding handlers + 44 narrow; skillflow 39 + 14 in `src/`. The "629"
figure counts the same source five times — `.claude/worktrees/` holds four
additional checkouts (202 `.py` files in the main tree, ~1030 across all five).

## The mechanism: an unverified hand-off

What every instance shares is not a syntax. It is a **belief about a downstream
owner that nobody checked.**

| discarded fact | the belief | the reality |
|---|---|---|
| unresolvable tool name | "graph validation will catch" | `graph.validate()` sees only YAML; a role's `tools:` is not in it |
| same, on the capability path | same comment, copy-pasted | the linter cannot see capabilities at all |
| `pip install` stderr | pytest's error will explain | `ModuleNotFoundError`, cause removed |
| tool failure detail | every tool sets `error` | `run_tests` reports through `passed` |
| an invented tool name | "falls through to the unknown-write branch" | that branch existed only on the constrained-slot path |
| `SkillFlow.unresolved_tools()` | "a host can surface this" | no host did |
| `files` envelope | the handler reads it | `_run_tool_step` reads only `actions` |

The last three were live in the tree at the start of this pass — two of them
*inside the fixes for earlier instances*. The class is generative, not a finished
list, which is why site-hunting cannot end it.

## What was built

**1. Turn accounting** (`PipelineEngine._classify_actions`). One partition —
writes + reads + messages + controls + unclaimed == actions — used by both JSON
handlers. `unclaimed` is answered by naming the tools the step really has, at the
cost of a turn.

Two things it made visible immediately:

* An action naming a tool the step lacks used to reach `if not tool_calls and not
  written_files: return True` — **the step reported SUCCESS with zero files**,
  preview `"No change needed (no writes)"`. Reproduced before the fix: one turn,
  no second turn, `run_step` True, nothing written.
* `tests/integration/test_full_pipeline_real_runner.py` — the repo's own offline
  end-to-end proof — was passing on exactly that. Its mock called `write`;
  `t_impl` is `mode: write` without `allow_full_write`, so skillflow grants
  `create, delete_file, edit, finish_step, list, list_tree, read,
  read_test_written, search, test_write` and **no `write`**. The call was
  discarded, the step "succeeded", the pipeline "completed". Verified by printing
  the live schemas, not inferred.

The read bucket was a hardcoded four-name list, so `read`/`search`/`list` —
skillflow's unified read surface, injected into every step — were dropped too.
Reads now route by the step's own grant.

**2. A detector for the class** (`_note_feedback`). The runtime signature is the
same whichever syntax caused it: the step is handed the identical failure text
again. Three unchanged deliveries raise `feedback_repeated`, and the count rides
on the step's final error, where an operator reads it without opening a trace.
Detection only; the prompt is untouched. It would have flagged ~10 of the 12
within one drive. It does not see AA1 (the run ends without a retry).

**3. The recorded fact reaches the step that needs it** (`_unresolved_note`).
`SkillFlow.unresolved_tools()` is read on the failure path of a step that
produced nothing — which is the only symptom a dropped tool grant has.

**4. `files` mirrored into actions.** Found by reading the previous session's
live trace: after `create` refused ("already exists — use 'edit'"), the step
delivered via `files` and was answered *"No actions found in your response"*.
AA8 fixed this for `{path, content}` and left the shape the prompt documents
unhandled.

## Open items

* **`enrich_project_status`** — the run's `error_reason` sat unread in the row
  skillflow had just returned. Attached. Then verified against a live response:
  still absent, because `response_model=ProjectWithStats` filtered it out. **The
  fix for produce-then-discard was itself produce-then-discard**, one layer
  further along, and only reading the live artifact caught it (trap 1 earns its
  keep).
* **`_failure_rejoins_success`** — success/failure is now decided by match
  POLARITY, not edge position. Measured over all 22 configs on this host: no
  behaviour change. On synthetic inputs the old rule missed a real fail-open when
  the failure edge was written first and **falsely accused** a correct graph with
  two failure edges to one fix step, calling the fix step "the SUCCESS target".
* **The hallucinated build-backend** — not a maker hallucination. It is
  hardcoded in the *shipped role prompt*
  (`gen_mcp_server_builder.roles.json:scaffold_maker`), together with
  `from mcp.server.models import InitializationCapabilities` (real name:
  `InitializationOptions`) and `@server.tool()` (`Server` has no such decorator).
  AA12's "the agent guessed on three drives" was the emitter's paste, replayed
  verbatim every run. New enforced rule `prompt_build_backend_is_real` — limited
  to build backends on purpose: the gate runs in a container with neither
  setuptools nor any generated project's dependencies, so a general "does this
  API exist" check would be blind there, while a build backend is a short closed
  set decidable anywhere. The stored prompt was corrected and its pasted API
  replaced with a description.
* **`mcp` is not installed in the container.** `gen_mcp_server_builder`'s suite
  cannot go green there regardless of what its maker writes.

## What none of this covers

Turn accounting covers the agent-turn boundary in `PipelineEngine` only — not
skillflow's runner-mode proxy, not the butler's coding loop, not discards inside
a tool. `_unresolved_note` covers tool-name resolution only. The detector reports
a repeat; it is blind to a failure that gets a *different* uninformative message
each time, and it prevents nothing. And none of the three would have caught
`file_exists` being blind to directories — a wrong predicate returning a
confident wrong answer, with no discarded fact anywhere. That still needs a drive.

## The verification drive (2026-07-29)

`gen_mcp_server_builder`, fresh run `de25b3f1`, with all of the above deployed.

```
A1 A2 B1 B2  B1 B2  B1 B2  B1 B2
status: failed   error_reason: Cycle limit exceeded:
  All transitions from 'B2' are exhausted: 'B2' -> 'B1' (max_loop=3 …)
```

**What the harness did right.** Trace of the whole run: 8 prompts, 8 responses,
**8 `create` tool_calls and 8 `create` tool_results**. Zero `parse_error`, zero
`unknown_tool`, zero `write_failed`, zero discarded envelopes. Every turn an agent
took produced an executed write. That is the chain the twelve defects used to break.

**What the prompt fix did.** With the pasted `Server` template replaced by a
description, the maker wrote the REAL low-level API — `@server.list_tools()` and
`@server.call_tool()`, both correct — instead of the hallucinated `@server.tool()`
it had copied on every previous drive. The failure moved strictly forward, to
`server.run(transport='stdio')`: a FastMCP signature on a low-level `Server`,
whose `run` is a coroutine taking two streams and an options object.

**What is actually blocking this pipeline, and it is not the feedback gap.**
`mcp` is not installed in the container. Neither the maker nor the reviewer can
inspect the API they are arguing about — the read tools are closures over project
root / staging / step output, so `site-packages` is unreachable by design. The
reviewer flagged the sync/async symptom correctly and its *suggested fix* was
wrong ("call `main()` directly, remove `asyncio.run`" — the maker had already done
exactly that), so the maker complied and was rejected again. Three laps, budget
spent. This is AA12's capability gap, unresolved and now clearly the binding
constraint on this pipeline.

### A rule I measured and did NOT add

B2's only failure edge is `max_loop: 3` with no unconditional fallback, so an
unsatisfied reviewer produces `Cycle limit exceeded` rather than a declared
give-up terminal. `forge_registry_check` passes the graph. The obvious rule —
"a `max_loop` edge needs an exit for when the budget is spent" — was calibrated
first: it fails **17 of 22 configs on this host**, including `dpe_default`
(all six review loops), `pipeline_forge` (all eight), `novel_chapter`, `subagent`,
`coding_impl` and `fix_tests`. The shape is the system's normal way to end an
unconvergeable loop. Not a rule.

The real defect is the MESSAGE. `Cycle limit exceeded: All transitions from 'B2'
are exhausted` names the edge and not the reason the loop never converged — the
reviewer's last verdict, which is sitting in `review_verdict.json` in the step
output the engine just promoted. Same shape as everything above, one level up, at
the run's own terminal. **It belongs in skillflow** (the host does not compose
that string), which is why it is recorded here rather than fixed: a skillflow
change needs a PyPI release before the container sees it, and an unverified fix is
what this whole document is about.

## The checkpoint rejection that looked like it failed (2026-07-29)

Reported as "I rejected once, but after reject the modal is still on the web UI
and the UI is not updated, so I approved". Traced on the live novel run
`9d9d1c5f` / `novel-chapter-98264c92`:

```
07:05:02  outline_gate  checkpoint_paused
07:07:51  POST /checkpoint/reject -> 200 OK
07:07:51  outline       claimed  {"attempt_feedback": true}   <- the reject WORKED
07:09:17  outline_gate  checkpoint_paused  (same gate, same label)
07:10:29  POST /checkpoint/approve -> 200 OK
```

The rejection was never broken. The feedback was persisted by skillflow and
delivered to the outliner, which acted on it (an `edit_outline` removing the
section the user complained about). Exactly one reject POST exists in the log,
and it succeeded synchronously.

**What failed is the only surface that could have told the user any of that.**
The modal's "Revised N time(s) / Last feedback: …" banner reads
`stepOutput.rejection_history`, which the API fills from
`{step_dir}/user_rejection_history.json`. That file has **three readers in this
repo — `api/meta_routers.py`, `core/prompt_assembler.py`, and `restage`'s
skip-list — and no writer anywhere**; `find` over a live workspace turns up none,
and `git log -S` shows one was never removed. It has never existed.

skillflow is what persists the feedback (`_append_feedback_log`, since 1.5.15),
and two things make it easy to look in the wrong place — the old code got both
wrong. The log lives beside the config dir, not inside the step's output, and it
is keyed by the step the reject REWINDS to (`checkpoint_reject_to` → `outline`),
not by the gate. The user's actual words were on disk the whole time:

```
~/.AItelier/workspaces/novel-chapter-98264c92/novel_chapter/_feedback/outline.md
## 反馈轮 #1 · 2026-07-29 07:07 UTC
王超哪来的问题，是beat出错了吗，本来不就是设计出来用来死的新人吗？
```

So the gate re-paused with the same label and no banner, and the only rational
reading from the UI was "nothing happened". `rejection_count` was hardcoded `0`
in the same response, and the modal does not read it anyway.

Fixed by resolving the path through skillflow's own `feedback_log_path` helper,
following `checkpoint_reject_to`, and parsing the rounds out of the log.
Verified against the running server: `rejection_count: 1` and the user's verbatim
text now reach the client. No frontend change was needed — the modal already
renders exactly that shape.

**Not a regression.** It is the oldest instance of the class in this document:
the system had the fact, in the user's own words, timestamped, and the surface
read a different file. It is also the first instance whose victim was the USER
rather than an agent — the same defect shape, one audience further out.

*(Two hypotheses discarded on evidence before this one: that my container
restarts had interfered — they were 04:01–04:07, the session was 07:05–07:10 —
and that the deployed SPA was stale relative to source. A fresh `npm run build`
produced byte-identical content hashes to the bundle in the image, so the
deployed UI was current and the code I was reading is what ran.)*

## Two rules that could not both be satisfied (2026-07-29)

`novel-chapter-98264c92` chapter 5 died `Cycle limit exceeded` at `continuity`.
It is NOT a feedback gap — the reason reached the agent every lap:

| lap | humanize output | continuity verdict |
|---|---|---|
| 1 | 5739 字 | 字数超限 5739（上限 4500） |
| 2 | 4656 字 | 字数超限 4656 **and** 润色字数漂移 -19%（初稿 5743 → 终稿 4656），超出 ±10% |
| 3 | 5662 字 | 字数超限 5662 |

From a 5743-字 draft the ±10% fidelity rule permits 5169–6317; the 4500 ceiling
demands ≤4500, which is -22% drift. **No output satisfies both.** The agent read
the feedback correctly each lap and oscillated between two impossible demands
until the loop exhausted and the chapter was lost.

The ceiling was enforced on the wrong step. Length is set by `draft`; `humanize`
only polishes language, and the very next rule forbids it from adding or removing
plot. Fixed per the author's call — **the hard gate is the FLOOR, the ceiling is
advisory**:

* `字数不足` stays a violation (a short chapter is genuinely unfinished).
* `字数超限` becomes an advisory naming why it is not humanize's to fix.
* `templates/novel_{humanize,draft,design}.md` updated in the same change — a
  gate rule the maker is taught wrongly costs a rework round on every run
  (see the RULES/`teaches` binding above).

The final draft was 5662 字 at **-1% drift** — a textbook humanize result,
rejected purely for the ceiling. Under the new rule it passes.

*(Metric note: everything here is 字数 = non-whitespace CHARACTER count,
`novel_state.char_count`, the CJK prose convention — not words.)*

### The tool result that was hidden one layer up

`skillflow_runs.error_reason` for that run is the bare `Cycle limit exceeded`.
The scheduler already resolves the real cause (`_failure_reason` →
`_last_trace_error`, which finds the tool's own `error` string), and the project
`status` carried it. But `enrich_project_status`'s new `error_reason` field
served the RAW column — so the API handed clients the framework artifact while
the host had already dug out the cause. Fixed to serve the resolved reason;
verified live, the field now reads
`Cycle limit exceeded — continuity: continuity_check 未通过: - 字数超限: 5662 字（上限 4500）`.

That is the same defect as everything else in this document, committed by me,
in the fix for it, two turns after writing the section that warns about it.

## `apply_state` made all-or-nothing (2026-07-29)

The partial write that cost chapter 5 of `novel-chapter-98264c92`. The sequence
was: write `ch0005/` → post the events → rebuild the derived state. Event 1
(林凌漆) posted; event 2 (王超, no bible card) was refused. What was left on
disk was a chapter directory, a modified character card, and an `index.yaml`
still saying `chapters_written: 4`.

**The second failure is the expensive one.** `next_chapter_number()` derives
from the dirs on disk, so it now answered 6 while `chapter_events.json` still
declared 5 — every retry failed with a *different* error than the first, and
skillflow reported only `Tool step 'apply_state' crashed 3 times — likely a bug
in the tool`. The three distinct causes were in container stdout and nowhere
else. Recovery was `git checkout` the card, `rm -rf` the unbooked chapter, fix
the flag, retry — a human who knew the internals.

Two changes, both in the host:

* **`novel_state.validate_events`** — every refusal `apply_events` can make
  (unknown `entity_type`, missing `entity_name`, a character with no card and no
  `create: true`, an unknown faction) now runs over the WHOLE list before the
  first write. Entities created earlier in the same list count as known.
  `apply_events` calls it first, so the guarantee holds for direct callers too.
  The refusal message from `9cdfdfe` is unchanged — it moved into
  `_no_card_error`, one definition, still teaching.
* **`novel_state.state_transaction`** — the write phase is wrapped: `bible/` and
  `state/` are snapshotted, new entries under `chapters/` are remembered, and any
  exception restores the snapshot and removes what the failed run created.
  `chapters/` is append-only so it is never copied, only pruned. If the rollback
  itself fails, the error names both causes and says the tree needs a human —
  the one thing it must not do is claim a clean tree it did not restore.

The guarantee is checkable rather than asserted: the novel tree is a git repo,
so `tests/unit/test_apply_state_atomic.py` runs the live failure and asserts
`git status --porcelain -- novel` is **empty** — then that the corrected retry
just works, which is the recovery that used to need a human.

**Validation alone would not have been enough.** The refusal fired between two
writes in *different* functions (`ch_dir.mkdir` in the tool, the raise in
`apply_events`); pre-validating makes the common failure cost nothing, and the
transaction is what makes any *other* failure — a bad rebuild, a disk error, a
bug — cost nothing too. Both, because they cover different halves.

**The related gap this exposed, not yet closed:** `apply_state` is a terminal
tool step with no failure edge back to `finalize`, so a refusal still ends the
run after 3 identical crashes — cleanly now, but the finalizer never sees the
error it could act on. That is the feedback gap again, one step further out.
Mitigated where the fact actually lives: `state_probe` now lists the characters
who are in `by_character` but have no card (live: 李默, 周小雨 — on stage since
chapter 3), with the `create: true` remedy, in the bundle the finalizer reads.
Nobody was computing that difference, and it is only discoverable at the
chapter's last step.
