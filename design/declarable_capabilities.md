# Declarable capabilities

Status: **design, for review** — no code written yet.
Date: 2026-08-26

## The measurement that started it

`gen_image_asset` (4.4 KB of tool schema) and `gen_audio_asset` (2.7 KB) are mounted on
`task_implementer` in `agent_configs/dpe_default.yaml`, i.e. on **every DPE run's implementer**,
game or not. Across 22 workspaces and 6,996 traced tool calls:

| | |
|---|---|
| `gen_image_asset` calls | **0** |
| `gen_audio_asset` calls | **0** |
| share of the step's 13.0 KB tool-schema budget | **55%** |

The 19 runs in that window were all fixes to an already-built game, so no new art was needed.
The tools are not wrong and the schemas are not bloated — the parameter descriptions are
carrying real discipline (`transparent=true` is not optional, `subject` drives the vision check,
`cast`/`appearance` pin identity). The defect is **where the cost is mounted**: on every turn of
every implementer, for a capability used on none of them.

The same teaching is *also* in `configs/addons/game_harness/implementer.md` (11.7 KB of prompt
overlay). So today a game run pays for it twice and a non-game run pays for it once for nothing.

## What already exists

`skillflow.core.SkillFlow.register_capability(name, *, tools=(), context_provider=None)`:

- a step declares `capability: <name>` in the graph;
- the engine merges `tools` into that step's schemas, and injects
  `context_provider(config_name) -> dict` as **kwargs on every tool call the step makes**
  (all four invocation paths).
- Registered today: `stateful` (hands a tool its durable `state_dir`), `tool_creation`
  (grants write/pytest/register_tool to the forge's tool-build step).

Three gaps between that and what this design needs:

1. **`capability` is a static string on the graph node** — `_capability_of(node)` reads
   `node.capability` and nothing else. A per-task decision cannot reach it.
2. **One capability per step.** A task that needs art *and* a robot arm cannot say so.
3. **No teaching channel.** `context_provider` feeds *tool kwargs*, not prompt context. There is
   nowhere for a capability to explain itself, which is why the teaching ended up copy-pasted
   into a role template.

The loop half is already in place: `loop_item` is stamped at claim time
(`core.py`), and `_loop_item_for_step()` resolves it — so "which task card is this
step running" is answerable at exactly the moment the toolset is assembled.

## Design

### 1. Capability declaration on the graph

```yaml
t_impl:
  capability: { from_item: "capabilities" }   # read the list from this loop item's card
```

or, unchanged, the static form for steps that always have it:

```yaml
t_tool_impl:
  capability: "tool_creation"
```

`from_item` is explicit on purpose. The engine must not *infer* that a card might carry
capabilities — the same reasoning as `x-aitelier: repo_mode`: a wrong `none` is a hard runtime
failure, so the declaration is written down where a reader can see it.

`capability:` accepts a string or a list; `from_item` resolves to a list. The union of tools is
granted; `context_provider` kwargs are merged and a **key collision raises** rather than letting
one capability silently win.

### 2. Task card schema

```jsonc
{
  "id": "add_boss_sprite",
  "capabilities": ["game_assets"],     // optional; absent = none
  ...
}
```

Written by the PM (step 3), whose own context gains a palette (below). The architect already
produces an asset manifest (`configs/addons/game_harness/architect.md` requires a resource list);
the PM's job is to turn "this task needs art" into a declared capability.

### 3. Palette — the declarer must be able to see what exists

A `capability_palette` tool/context source injected into step 3, rendering the **live** registry:
name, one-line purpose, the tools it grants. Same shape and same reason as `forge_palette`: an
agent asked to name something from a table must be given the table, or it will invent a plausible
name.

### 4. Registry gate — an invented name must fail at the card, not at the step

Step 3's validation gains a rule: every string in a card's `capabilities` must resolve in the
registry. This is `forge_registry_check`'s `role_model_known` rule applied to a second namespace,
and skillflow's own code already records why it matters:

> *"a capability whose tool is missing grants nothing just as quietly"* — `_resolve_tool_schema`

Without the gate, a hallucinated capability name produces a step with no extra tools, no error,
and an implementer that quietly does the task badly.

### 5. Briefing — teaching travels with the capability

```python
sf.register_capability(
    "game_assets",
    tools=["gen_image_asset", "gen_audio_asset"],
    briefing=GAME_ASSETS_BRIEFING,          # NEW: prompt context, not tool kwargs
)
```

Injected into the prompt of **only** the steps that hold the capability. This is what lets the
asset discipline leave `implementer.md`: a run that needs no art carries neither the schemas nor
the briefing, and a run that does gets both, once, in the place that uses them.

Two constraints on a briefing, both learned the expensive way:

- **It is re-sent every turn**, exactly like a tool schema. So it is a *discipline summary*
  (hundreds of bytes: `transparent=true` is mandatory, always pass `subject`, bgm has no loop
  point), not a manual.
- **The manual goes behind a call.** `game_assets_howto()` — the `sfx_presets` pattern from
  Continuity, which exists precisely because "which fields exist" cannot be guessed and should
  not be paid for on every turn.

### 6. Escape hatch — the failure mode of least privilege is silent inability

If the PM under-declares, the implementer has no art tools and no way to say so; the observable
result is a task quietly completed with `ColorRect` placeholders and a green report. That is the
[feedback-gap](../design/) class exactly.

Two cheap counters, both proposed:

- **`capabilities_available()`** stays in every implementer's toolset (~200 B): a read-only list
  of what *could* have been declared. The agent can then fail the task legibly ("this needs
  `game_assets`; the card does not declare it") instead of substituting a placeholder.
- **3_review checks the mapping**: the architect's resource list vs the cards' declarations. A
  task that needs art with no capability declared is caught before implementation, not after.

### 7. Where capabilities are registered

- Base: `api/dependencies.py`, as today (`stateful`, `tool_creation`).
- Addon: a `capabilities:` block in the addon YAML (`configs/addons/game_harness.yaml`), so
  "which capabilities exist" composes exactly like "which steps exist". `game_harness` brings
  `game_assets`.

## Migration for the two asset tools

1. Register `game_assets` (tools + briefing).
2. Remove `gen_image_asset` / `gen_audio_asset` from `agent_configs/dpe_default.yaml`'s
   `task_implementer`.
3. Move the asset discipline out of `configs/addons/game_harness/implementer.md` into the
   briefing; leave the template's non-asset guidance alone.
4. `t_impl` declares `capability: { from_item: "capabilities" }`.
5. PM template + palette + the 3_review mapping check.

## What this does not do

It does not make a *used* capability cheaper: a task that declares `game_assets` pays the schemas
and the briefing on every one of its turns, which is correct. It removes the cost from the tasks
that never use it — which, in the measured window, was **all of them**.

## Open questions for review

1. **Card or plan?** The capability is declared on the step-3 task card. `t_plan` runs between
   step 3 and `t_impl` and could refine it. Keeping one declarer (PM) is simpler; letting the
   planner add one is more accurate. Currently proposed: PM only.
2. **Does `t_plan` need capabilities too?** It writes a plan, not code — probably never. But
   `from_item` on a planner step is free to add later.
3. **Namespacing.** `game_assets` vs `game_harness:game_assets`. Flat is friendlier to write and
   the registry gate catches collisions at registration; namespaced is unambiguous when two
   addons are composed. Currently proposed: flat, with registration refusing a duplicate name.
4. **Does a briefing belong in the system or user message?** System is cached across turns and
   would ride the byte-identical preamble — but the preamble is *project-global* and a capability
   is *per-step*, so putting it there would break the byte-identity that makes the preamble worth
   having. Currently proposed: user message, with the discipline-summary size limit above.
