# pipeline_forge — a grounded, self-provisioning skillflow config generator

**Status:** SHIPPED + verified live (2026-07-18). A `pipeline_forge` run seeded with
"lint a Markdown file for broken relative links…" grounded correctly, BUILT +
registered a real `md_link_check` tool in-graph, emitted a valid `markdown_link_fix`
graph, passed all three gates (lint + registry-check + dry-run smoke), reached the
Design Review checkpoint, and — on approval — registered as a runnable `gen_<slug>`
with its real role prompts (boot-durable). See §11 for the lessons the live runs taught.
**Replaces:** skillflow's built-in `skill_converter` as the backend for the butler's `generate_pipeline`.
**Modeled on:** `configs/dpe_default.yaml` — Green/Red maker–checker, manifest→loop fan-out, staged
promotion, deterministic gates, loop-external `done`.

---

## 1. Problem

AItelier generates pipeline configs by running skillflow's built-in `skill_converter`
(`analyze_skill → design_graph → explain_design (checkpoint) → validate_design (lint) → done`). That
graph is **ReAct-shaped**: a single `graph_designer` emits free-form YAML from imagination, checked
only by a static linter. The one artifact it produced on this machine,
`~/.AItelier/configs/gen_game_subagent.yaml`, exhibits every failure mode. The seed asked for
AItelier's real `subagent` pattern (Green worker **agent** → Red reviewer **agent** → loop) plus a
Godot gate; what came out:

| Defect | Root cause |
|---|---|
| 7 hallucinated tools (`review_tool`, `godot_cli_tool`, `output_tool`…) — none exist | **No grounding** — `_tool_generate_pipeline` seeds only `skill_description.md`; the designer never sees the real registry. |
| Zero reviewer agents; maker/checker loop faked with boolean tools → the "fix" path has **no LLM in it** | **Lint-only checking** — `skillflow_lint` sees a structurally-valid graph, can't see the semantics are hollow. |
| Hand-rolled `fix_counter.txt` instead of native `max_loop`; process-global, races | Designer has **no knowledge of skillflow/AItelier idioms**. |
| Success + give-up share one terminal → exhausting the fix budget reports `completed` (**fail-open**) | Violates the loop-external `done`-gate rule (`skillflow-loop-bound-traps`). |
| `design_explanation.md` self-diagnosed most of this — and shipped anyway | Nothing downstream **acts** on the concerns. |

Root cause, one line: *the designer generates from imagination and is checked only for syntax.*

## 2. Design thesis

Two inversions, plus a third capability that makes them hold for genuinely-novel workflows:

1. **Ground the maker in the real palette.** A `forge_palette` context tool feeds the designer the
   live tool registry (names + signatures via `tool_loader`), curated config exemplars, and an
   AItelier idiom/trap cheatsheet (§6). (The **addon** path already does this —
   `generate_addon`/`meta_agent.py:3660` injects `list_tools()` "so the design agent references REAL
   tools instead of inventing one that fails at run time." pipeline_forge generalizes it.)
2. **Check semantics adversarially, then execute.** Green/Red agent pairs at each stage
   (reviewer defaults fail-on-uncertainty), then a **3-part deterministic gate**: `skillflow_lint`
   (structure) → `forge_registry_check` (every `tool_name`/`agent_config`/context ref resolves +
   convention linters) → `forge_dryrun_smoke` (the graph actually boots to `done` and terminates).
3. **Self-provision what's missing — in-graph.** A genuinely new workflow may need tools that don't
   exist yet (a Godot compile tool, a domain validator). Rather than hallucinate them (defect #1) or
   defer them to a human, pipeline_forge **builds and registers the missing tools as steps in its own
   graph, before the gate runs.** By the time `forge_registry_check` runs, the registry is *real* —
   so the gate stays strict with **zero exceptions**. This is the property that lets us keep the
   anti-hallucination gate while still supporting new capabilities.

**Locked decisions** (from design conversation):
- Real DPE-style LLM agents (scheduler-owned, autonomous), not `model: host` ReAct — *except* the
  survey/provisioning front-end, which is a bounded agentic loop (its exit predicate is the
  objective check "every tool the target graph references now resolves in `list_tools()`", not the
  agent's self-judgment).
- Direct YAML output, backed by the strong gate (no intermediate IR).
- Gate = lint + registry-check + dry-run smoke.
- **No pipeline-calls-pipeline recursion.** Tool-building is expressed **in-graph** (a `tool_loop`,
  DPE-style), *not* by `start_config_run` spawning a sub-pipeline per tool. One run, one trace, one
  `max_total_steps`/`max_iterations` envelope; structurally cannot recurse into itself. (The only
  thing given up — black-box reuse of an existing standalone pipeline — is cheap to inline.)
- **Sidecars/containers are out of scope** for auto-provisioning (a container can't be safely spawned
  from inside the AItelier container without granting docker-control — see §9). Tools only.

## 3. Pipeline graph — `configs/pipeline_forge.yaml`

Structurally DPE, retargeted from "build an app" to "build a workflow + its missing tools." The
1:1 mapping is the whole argument for reuse-the-shape-not-the-config:

| DPE | pipeline_forge |
|---|---|
| researcher (1) — read codebase | **survey** — grounding: read `forge_palette`, decide phases + which tools are missing |
| architect (2) — design | **architect** — design the target graph *shape* + name required-but-missing tools |
| PM (3) — `tasks_manifest` | **tool_plan** — emit `tool_tasks_manifest` (dependency waves) + tool cards |
| task_loop → t_plan/t_impl/t_review | **tool_loop** → build one tool per item + **register** it live |
| final_verifier (5) + gates | **emit_graph** (author YAML, tools now real) → **validate** (3-part gate) |
| goal-loop `5_review → 3` | validate-fail → loop to `architect` (redesign) or `emit_graph` (re-render) |

Repo-less authoring run (`registers_generated_pipeline: true` → `run_launcher` resolves
`repo_type="none"`). `scheduler_owned: true` (like `dpe_default`); the butler's `generate_pipeline`
becomes start + run-until-checkpoint (the helper `generate_addon` already uses).

```
begin: survey

survey            agent  research_designer   → survey_review
  ctx: forge_palette (real tools+sigs, exemplars, cheatsheet), skill seed.
  out: forge_plan.md — phases; for each, the REAL tools it uses; a list of
       MISSING tools it will need built {name, purpose, one-line contract}.

survey_review     agent  reviewer   verdict → survey (max_loop 3) | architect
  Red: does the plan cite only real tools OR list a missing-tool to build?
       (no silent unknowns) coherent vs. the request?

architect         agent  graph_architect   → architect_review
  ctx: forge_plan + palette + cheatsheet.
  out: graph_spec.md — the target graph's node/edge shape in prose+skeleton,
       plus missing_tools.json {tools:[{name,purpose,interface}]} (authoritative).

architect_review  agent  reviewer   verdict → architect (max_loop 3) | tool_plan
  Red enforces the §6 convention checklist that lint cannot see.

tool_plan         agent  tool_planner   → tool_plan_review
  ctx: missing_tools.json.
  out: tool_tasks_manifest.json {execution_order:[[name,...],...]} + one
       tool card per tool tools/<name>.json {name, purpose, interface_contract,
       params_schema}. (DPE's PM, scoped to tools. Empty manifest = no new
       tools needed → loop drains immediately → emit_graph.)

tool_plan_review  agent  reviewer   verdict → tool_plan (max_loop 3) | tool_loop

tool_loop         loop   source: tool_plan/tool_tasks_manifest.json:execution_order
                         item_as: current_tool ; max_iterations: 30
  → t_tool_impl  (per item) ; → emit_graph (drained)

t_tool_impl       agent  tool_implementer   output.mode: write
  ctx: tool_plan/tools/$current_tool.json, forge_palette.
  writes into $STEP_DIR: <name>/tool.yaml, <name>/impl.py, <name>/test_<name>.py
  → t_tool_register

t_tool_register   tool   register_tool  (persist to ~/.AItelier/tools/<name>,
                         live-register so list_tools() sees it NOW)
  → t_tool_review
  NOTE: register runs BEFORE review and the AGENT review returns to the loop,
  because skillflow only credits a loop item as done when an AGENT step returns to
  the loop (confirm_step → _credit_loop_current_item); an inline TOOL step
  returning to the loop (_complete_tool_step) does NOT credit, so the loop would
  re-serve the same item forever. Also: a loop var ($current_tool) is only
  interpolated in CONTEXT paths, NOT in tool_params — register_tool takes the name
  from the engine-injected `task_name` (the loop's current_item). Both learned the
  hard way in the first live run.

t_tool_review     agent  reviewer   verdict → tool_loop (pass, credits item) |
                         t_tool_impl (fail, max 3; rebuild re-registers/overwrites)
  (Hardening TODO: an objective pytest gate on test_<name>.py before register.)

emit_graph        agent  graph_emitter   output.mode: write
  ctx: graph_spec + palette (tools now include the just-built ones).
  writes: pipeline.yaml (the generated graph), role_table.yaml (agent_configs),
          templates/<role>.md (paired Green/Red per maker role).
  → emit_review

emit_review       agent  reviewer   verdict → emit_graph (max 3) | validate

validate          tool   3-part gate (§5): skillflow_lint → forge_registry_check
                         → forge_dryrun_smoke
  → explain {passed:true} | architect {passed:false} (max_loop 3, feedback:true)

explain           agent  design_explainer   checkpoint:true, reject_to: architect
  human-readable walkthrough + provisioned-tools summary + approval gate.
  → done

done              gate   to: null   loop-external terminal — no false-green.
```

`end_conditions`: `node_reached done → completed` OR `max_total_steps` OR `flag_match {fatal_error}`.

Persistence/bridge unchanged: `core/pipeline_registry.py` namespaces the emitted YAML as `gen_<slug>`,
persists to `~/.AItelier/configs/`, auto-registers invented agent roles as host agents, adds the
manifest. The built tools persist to `~/.AItelier/tools/` (§7).

## 4. Agent roles — `agent_configs/pipeline_forge.yaml`

DPE's maker/reviewer profile split (capable+thinking makers; cheap, cold, read-only reviewers that
default-fail-on-uncertainty). One shared `reviewer` role serves every Red slot (its per-step template
is injected via context — same as DPE's practice of one small reviewer profile).

| role | model | template | tools | thinking |
|---|---|---|---|---|
| `research_designer` | pro | `forge_survey.md` | `forge_palette`, `list_tree`, `read_file` | max |
| `graph_architect` | pro | `forge_architect.md` | `forge_palette`, `read_file`, `write` | max |
| `tool_planner` | flash | `forge_tool_plan.md` | `read_file`, `write` | on |
| `tool_implementer` | pro | `forge_tool_impl.md` | `forge_palette`, `read_file` (+write via mode) | on |
| `graph_emitter` | pro | `forge_emit.md` | `forge_palette`, `read_file` (+write via mode) | max |
| `reviewer` | flash | `forge_review_red.md` | `read_file` | on |
| `design_explainer` | flash | `forge_explain.md` | `read_file` | on |

Reviewer verdict = DPE's JSON contract, machine-validated by `json_schema`:
`{"passed": bool, "feedback": str, "suggestions": [str]}`. Format issues are **not** blocking.

## 5. The three-part validation gate

`validate` runs three checks in sequence; first failure short-circuits back to `architect` with the
report as `feedback`. Because `tool_loop` already built + registered the missing tools, all three
see a real registry — the gate needs **no unknown-tool exception**.

- **(a) `skillflow_lint`** — existing skillflow tool. Structure: begin, reachability, dup ids,
  transition targets, every cycle has `max_loop`.
- **(b) `forge_registry_check`** — *new*. Loads the emitted YAML; asserts every `step.tool_name` ∈
  `tool_loader.list_tools()`, every `step.agent_config` ∈ emitted `role_table.yaml`, every
  `context.source.{step,config,tool}` resolves; plus **convention linters** encoding §6: only a
  `gate` with `to: null` may carry the `node_reached…completed` end-condition; no file-based counter
  where `max_loop` fits; a maker step's reviewer is an `agent`, not a tool. → `{passed, violations}`.
- **(c) `forge_dryrun_smoke`** — *new*. Boots the generated graph once in an ephemeral workspace with
  a **stub `StepRunner`** (§7) driving the claim loop in place of `AgentStepRunner`. skillflow
  auto-runs inline tool/gate nodes and evaluates transitions; only `agent` steps hit the runner
  (`core/scheduler.py:684-694`), so real tool/gate/loop structure executes while agents return canned
  schema-conformant outputs. Asserts: begin resolves; every tool/role constructs (`load_fn`/role
  lookup) without `ImportError`; a path reaches `done`; terminates **before** `max_total_steps`.
  A second pass forcing verdicts `{passed:false}` proves reject loops are bounded and end FAILED.
  → `{passed, reached_terminal, steps_run, error}`.

## 6. The AItelier convention checklist (encoded knowledge)

The value-add over the built-in converter. `survey` **reads** this via `forge_palette`; Red +
`forge_registry_check` **enforce** it. Sourced from the project memories.

- **Maker ≠ checker, both agents.** Every creative step → real `agent` maker + real `agent` reviewer
  emitting `{passed,feedback,suggestions}`, default-fail-on-uncertainty. Never a boolean-tool review.
- **Native `max_loop`, never hand-rolled counters.**
- **Loop-external `done` gate** (`step_type: gate`, `to: null`) is the only `completed` terminal;
  give-up paths end FAILED.
- **Objective gate before semantic review** where a suite/build exists (`run_tests`, `pytest`,
  compile) — a reviewer with no execution tool can't catch a broken build.
- **Staged write + `repo_apply`/register** for steps that mutate; validation gates promotion.
- **Manifest → loop fan-out** for per-item work (`loop.source` + `item_as` + `$var`).
- **completion_seq / position awareness** on looped nodes (skillflow ≥1.5.18) → hence the external gate.

## 7. New tools & the runtime-registration mechanism

All new tools under `aitelier/tools/`. **Runtime tool registration needs no skillflow change** — the
hooks exist:
- `ToolLoader.register_dynamic_tool(name, schema, fn)` (`tool_loader.py:61`) — in-memory, injects the
  cache; live for the session.
- `ToolLoader.add_tools_dir(path)` + the all-dirs scan in `list_tools`/`_find_tool_dir` — a tool dir
  dropped in a scanned location is discoverable; add `~/.AItelier/tools/` to the loader at construction
  (`api/dependencies.py`), mirroring how `~/.AItelier/configs/` is boot-scanned for `gen_*` graphs.
- Prior art: `core/asset_registry.py:register_tool` already persists a pipeline-produced tool to disk
  with a manifest.

| tool | purpose |
|---|---|
| `forge_palette` | Context provider: `list_tools()` + each tool's `load_schema()` signature + curated exemplar configs + the §6 cheatsheet. Read-only; the grounding surface. |
| `register_tool` | Persist `$STEP_DIR/<name>/{tool.yaml,impl.py}` → `~/.AItelier/tools/<name>` **and** live-register into the running `ToolLoader` so `list_tools()` sees it immediately. Namespaced `gen_<slug>__<name>` to avoid collisions; overwrite on re-gen. |
| `forge_registry_check` | Gate (b): validate emitted YAML against the live registry + §6 convention linters. |
| `forge_dryrun_smoke` | Gate (c): stub-`StepRunner` boot of the generated graph to terminal. |

**Stub runner** — a second `StepRunner` impl next to `aitelier/runner.py:AgentStepRunner`. `execute`
inspects the claimed agent step's `output.fixed` spec and writes minimal schema-conformant canned
files (verdicts → `{"passed": true}` for the happy path); returns a `StepResult` that passes the
step's `validation`. Injected only inside `forge_dryrun_smoke`'s ephemeral claim loop.

## 8. Host wiring

- `configs/pipeline_forge.yaml` + `agent_configs/pipeline_forge.yaml` registered at boot alongside
  the other AItelier configs (`api/dependencies.py`). `x-aitelier` block: `input_hint`,
  `registers_generated_pipeline: true`, `scheduler_owned: true`, seed = `skill_description.md`,
  output step = `emit_graph`, labels per step.
- `config_registry._EXTERNAL_HINTS["pipeline_forge"]` → `registers_generated_pipeline: True`
  (repo-less authoring run via `run_launcher`).
- `ToolLoader` gets `~/.AItelier/tools/` as a scanned dir + a boot-scan so persisted generated tools
  survive restart.
- `meta_agent._tool_generate_pipeline` (`:3560`): swap `config_name="skill_converter"` →
  `"pipeline_forge"`; because it's scheduler-owned, drive via `start_config_run` +
  `_run_pipeline_until_checkpoint` (same as `generate_addon`). Design-review checkpoint relay
  unchanged; re-run same name overwrites in place.
- `skill_converter`/`addon_converter` registration stays (addons still use `addon_converter`); only
  `generate_pipeline`'s backend changes. Deprecate `skill_converter` once verified live.

## 9. Risks / decisions

1. **Trust boundary (the real one).** A registered tool's `impl.py` runs with **server-process
   privilege** (scheduler/butler access), unlike DPE project code which runs sandboxed in a workspace
   via pytest. LLM-authored in-process tools are a privilege escalation. *Interim stance for this
   cut:* `t_tool_test` runs the tool's unit test in the same sandboxed `run_tests`/pytest subprocess
   DPE uses (not in-process), and registration persists the file but the tool only actually executes
   later inside `forge_dryrun_smoke` (stubbed) or a real run. A hardened sandbox (subprocess/container
   tool-runner) is a follow-up. **Flagged for explicit sign-off before enabling live generated-tool
   execution outside the smoke.**
2. **Scope/cost + surprise.** A single request can fan into several tool builds. `tool_loop`
   `max_iterations: 30` + `max_total_steps` bound it; the `explain` checkpoint surfaces the built
   tools. (A pre-`tool_loop` provision-plan checkpoint is a cheap add if runs feel runaway.)
3. **Lifecycle/cleanup.** Namespaced `gen_<slug>__tool`, overwrite on re-gen, delete tool dirs when
   the pipeline is deleted (the zombie discipline from `authoring-runs-repoless`).
4. **Sidecars — deferred.** A Godot/Unity sidecar is Docker orchestration; auto-spawning it from the
   AItelier container needs docker-socket/compose control (large blast radius, esp. running
   LLM-authored code). Out of scope; such workflows declare the sidecar as a manual prerequisite.

## 10. Prevents the negative example

| `gen_game_subagent` defect | prevented by |
|---|---|
| 7 hallucinated tools | grounding (`forge_palette`) + **self-provision** (build the real ones) + registry-check (b) |
| faked reviewer / no-LLM fix path | §6 Green/Red convention + `*_review` Red + smoke (c) |
| hand-rolled fix counter | §6 native-`max_loop` + registry-check convention linter |
| fail-open false-green terminal | §6 loop-external `done` gate + registry-check + smoke terminal assertion |
| self-diagnosed but shipped | machine gates run before the human checkpoint; reject is actionable |

---

## 11. Hybrid architecture — DAG generation + ReAct drive/fix (the real shape)

The 3 gates verify **structure** (valid graph, real tools, reachable terminal) — NOT
**runtime behavior**. Proven: a generated pipeline passed lint+registry+smoke but at
real runtime (a) its reviewer role misused `read_file` and (b) its tool's `file_path`
pointed at the wrong step dir — both invisible to the gates (the smoke *stubs* agents
and tools). Generation has bounded objective oracles (lint/registry/smoke); judging
"does the generated pipeline actually work" is open-ended and needs judgment.

So the orchestrator split at the **oracle boundary**:
- **DAG (`pipeline_forge`)** owns generation — grounded design, tool-building, and the
  bounded gates. Runs context-isolated (scheduler-owned) so its huge token cost stays
  OUT of the agent's window.
- **ReAct (butler CODING mode)** owns runtime validation + repair — it calls
  `generate_pipeline` (→ pipeline_forge), then **`drive_pipeline`** (test-drives the
  generated pipeline context-isolated on a synthesized input, returns a compact
  per-step summary + first failure + outputs), JUDGES the result, and FIXES the
  generated config (`~/.AItelier/configs/gen_<slug>.yaml` / `.roles.json` via bash) —
  `drive_pipeline` reloads from disk each call. Loop until it behaves, then present.

`drive_pipeline` (`meta_agent._tool_drive_pipeline`) + `reload_generated_pipeline`
(`pipeline_registry`) are the new pieces; the loop guidance is in `templates/coding_mode.md`.
**Verified end-to-end:** generate → drive (fails) → fix roles → drive (loops) → fix
tool file_path → drive → **completed**. The DAG couldn't self-correct these; the ReAct
loop caught + fixed both in three iterations.

## 12. Lessons from the live runs (all fixed)

1. **Loop-var interpolation is context-only.** `$current_tool` is substituted in a
   step's `context` file paths but NOT in `tool_params`. `register_tool` takes the
   name from the engine-injected `task_name` (the loop's current_item) instead.
2. **Only an AGENT step credits a loop item.** skillflow credits the loop's
   current_item on the agent `confirm_step` path (`_credit_loop_current_item`);
   an inline TOOL step returning to the loop (`_complete_tool_step`) does NOT — the
   loop re-serves the same item forever. So the loop body ends on an agent
   (`t_tool_impl → t_tool_register(tool) → t_tool_review(agent) → tool_loop`).
3. **A terminal gate needs `transitions: [{to: null}]`, not `[]`.** An empty
   transitions list makes gate resolution report "no matching transition" → the run
   fails. (`forge_registry_check` enforces this.)
4. **The dry-run smoke must be fully synchronous.** It runs inside the scheduler's
   event loop; `asyncio.run_until_complete` throws "event loop already running". The
   drive + stub runner are sync.
5. **The smoke stubs tool nodes** (converts them to stub-agents returning the
   success flag) rather than running them for real — an input-dependent tool (a file
   scanner with no file) would never "pass" and loop the smoke to a false negative.
   Tool importability is still checked statically (`load_fn`).
6. **The emit template must embed the EXACT skillflow schema.** The first live emit
   invented a plausible-but-wrong YAML dialect (`end_conditions` as a list,
   `run_input` context, list `output`); a terse "copy the exemplars" prompt wasn't
   enough. The gate caught it; the fix was a prescriptive `forge_emit.md`.
7. **Registration is generator-specific.** `pipeline_forge` emits graph +
   role_table + templates (not one file), so `register_forge_pipeline` registers the
   roles with their real emitted prompts and persists a `<config>.roles.json` for
   boot restore. A scheduler-owned generator needed a completion hook in the
   scheduler (`_registered_gen_runs`) since the butler doesn't drive it.

---

**Delivered:** the load-bearing tools (`forge_palette`, `forge_registry_check`,
`forge_dryrun_smoke`, `register_tool`) + stub runner; `configs/pipeline_forge.yaml`
+ `agent_configs/pipeline_forge.yaml` + `templates/forge_*.md`; host wiring
(`~/.AItelier/tools` boot-scan, `register_forge_pipeline` + dispatch, scheduler
completion hook, `generate_pipeline` → `pipeline_forge`); EDIT MODE
(`edit_target` seeds a baseline for surgical changes) + request-framing; the ReAct
drive/fix loop (`drive_pipeline` in coding mode); and deprecating `skill_converter`.

## Phase 2 (not built)

- **Objective pytest gate** per built tool (run `test_<name>.py` before `register_tool`).
- **Addon-output mode:** let `architect` choose *standalone graph* vs *addon overlay on a
  base* (a Godot compile gate on DPE is genuinely an addon); `validate` branches to
  `compose`-validate + smoke-on-composed-graph. Reuses the `addon_converter` surface.
- **Hardened sandbox** for generated-tool execution (§9 risk #1).
- **Auto-sidecar provisioning** behind an explicit docker-control grant (§9 risk #4).
