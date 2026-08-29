# Declarable capabilities

Status: **design, for review** — no code written yet.
Date: 2026-08-26

## 1. The measurement that started it

`gen_image_asset` (4.4 KB of tool schema) and `gen_audio_asset` (2.7 KB) are mounted on
`task_implementer` in `agent_configs/dpe_default.yaml`, i.e. on **every DPE run's implementer**,
game or not. Across 22 workspaces and 6,996 traced tool calls:

| | |
|---|---|
| `gen_image_asset` calls | **0** |
| `gen_audio_asset` calls | **0** |
| share of that step's 13.0 KB tool-schema budget | **55%** |
| re-sent | every turn, 3,527 turns |

The 19 runs in that window were all fixes to an already-built game, so no new art was needed.
The tools are not wrong and the schemas are not bloated — the parameter descriptions carry real
discipline (`transparent=true` is not optional, `subject` drives the vision check,
`cast`/`appearance` pin identity). The defect is **where the cost is mounted**: on every turn of
every implementer, for a capability used on none of them.

The same teaching is *also* in `configs/addons/game_harness/implementer.md` (11.7 KB of prompt
overlay). So a game run pays for it twice, and a non-game run pays for it once for nothing.

## 2. What exists today

`skillflow.core.SkillFlow.register_capability(name, *, tools=(), context_provider=None)`:

- a step declares `capability: <name>` in the graph;
- the engine merges `tools` into that step's schemas, and injects
  `context_provider(config_name) -> dict` as **kwargs on every tool call the step makes**
  (all four invocation paths);
- registered today: `stateful` (hands a tool its durable `state_dir`), `tool_creation`
  (grants write/run_tests/register_tool/register_capability to the forge's
  tool-build step).

Four gaps between that and what this design needs:

1. **`capability` is a static string on the graph node.** `_capability_of(node)` reads
   `node.capability` and nothing else, so a per-task decision cannot reach it.
2. **One capability per step.** A task needing art *and* a robot arm cannot say so.
3. **No teaching channel.** `context_provider` feeds *tool kwargs*, not prompt context. There is
   nowhere for a capability to explain itself, which is why the teaching ended up copy-pasted
   into a role template.
4. **Registration silently overwrites.** `self._capabilities[name] = {...}` — see §3.7; in a
   global registry fed by addons and generated artifacts this is the highest-risk line in the
   mechanism.

The loop half is already in place: `loop_item` is stamped at claim time and
`_loop_item_for_step()` resolves it — so "which task card is this step running" is answerable at
exactly the moment the toolset is assembled.

## 3. Design

### 3.1 One global registry, per-config offer lists

Definition and visibility are different questions. Definition stays global; visibility is
declared per config.

This repo has already made that choice twice:

| | defined where | selected where |
|---|---|---|
| tools | global `ToolLoader` (`aitelier/tools/` + `~/.AItelier/tools/`) | each agent_config's `tools:` |
| models | global `model_routes.json` | each agent_config's `model:` |
| **capabilities** | **global registry** | **`x-aitelier.capabilities` + the step's declaration** |

A per-config *registry* would mean re-registering `stateful` in DPE, novel, coding_task and every
`gen_*` pipeline — four copies to drift apart the first time a briefing improves — and a second
namespace to keep in sync with the (global) tools it grants.

A purely global registry has the hole this design must close: a PM would see capabilities its
pipeline cannot run. So the graph declares what it **offers**:

```yaml
# configs/dpe_default.yaml — TOP LEVEL, beside `steps:`, not under x-aitelier
capabilities: ["game_assets"]          # what this pipeline offers

# configs/addons/game_harness.yaml — composes the same way its steps do
capabilities: ["game_assets"]
```

**A first-class graph field, not `x-aitelier` host metadata.** The `x-` namespace is what
skillflow ignores, and the offer list has to be enforced at claim time *inside* skillflow (see
consumer 3 below) — a host-only hint cannot gate the engine's own provisioning. Composition
merges it as a **union**: base offers X, addon adds Y, the composed graph offers X ∪ Y. (Union
rather than override on purpose: every scalar hint key overrides, and an addon that silently
revoked the base's offers would be very hard to see.)

The offer list is consulted in three places, and each one's failure message improves because of it:

1. **The PM palette** = registry ∩ offer list. Invisible ⇒ undeclarable.
2. **The step-3 gate**: every name on a card must be in the *offer list*, so the error becomes
   *"`robot_arm` is registered but this pipeline does not offer it"* instead of *"unknown
   capability"* — the first sentence names the file to edit.
3. **Provisioning at claim time** also checks the offer list, not just the card. Otherwise a
   hand-edited card smuggles a capability past the gate. Same stance as `write_gate`: the
   frontend read-only mode is UX, the server gate is the control.

**Declared requirement vs deployment reality.** A global registry makes the palette
machine-dependent: a generated pipeline that offers `robot_arm` behaves differently on a machine
where nothing registered it. `x-aitelier.capabilities` is therefore the *declared requirement*,
and boot checks it the way `core/external_deps.py` checks LLM keys — a missing offer is a loud
startup warning, not a surprise three steps into a run.

### 3.2 Declaring a capability on a step

```yaml
t_impl:
  capability: { from_item: "capabilities" }   # read the list off this loop item's card
```

The static form is unchanged for steps that always hold one:

```yaml
t_tool_impl:
  capability: "tool_creation"
```

`from_item` is explicit on purpose: the engine must not *infer* that a card might carry
capabilities. Same reasoning as `x-aitelier: repo_mode` — declared, never inferred, because a
wrong default is a hard runtime failure and a reader needs to see where it came from.

`capability:` accepts a string or a list; `from_item` resolves to a list. Tools are granted as a
union. `context_provider` kwargs are merged and **a key collision raises** rather than letting one
capability silently win.

### 3.3 The task card

```jsonc
{
  "id": "add_boss_sprite",
  "capabilities": ["game_assets"],     // optional; absent = none
  ...
}
```

Written by the PM (step 3). The architect already produces a resource list
(`configs/addons/game_harness/architect.md` requires one); the PM's job is to turn "this task
needs art" into a declared capability.

**One declarer.** `t_plan` runs between step 3 and `t_impl` and could refine the list, but two
declarers means two places to look when a capability is missing. PM only; `from_item` on a
planner step stays available if that ever proves wrong.

### 3.4 Briefing — teaching travels with the capability

```python
sf.register_capability(
    "game_assets",
    tools=["gen_image_asset", "gen_audio_asset"],
    briefing=GAME_ASSETS_BRIEFING,      # NEW: prompt context, not tool kwargs
    owner="addon:game_harness",
)
```

Injected into the prompt of **only** the steps that hold the capability. This is what lets the
asset discipline leave `implementer.md`: a run needing no art carries neither the schemas nor the
briefing; a run that does gets both, once, where they are used.

Two constraints, both learned the expensive way:

- **A briefing is re-sent every turn**, exactly like a tool schema. So it is a *discipline
  summary* — hundreds of bytes: `transparent=true` is mandatory, always pass `subject`, bgm has
  no loop point — not a manual.
- **The manual goes behind a call.** `game_assets_howto()`: the `sfx_presets` pattern from
  Continuity, which exists precisely because "which fields exist" cannot be guessed and should not
  be paid for on every turn.

**User message, not system.** The system preamble is byte-identical *project-global* content
(that is what makes it cacheable); a capability is per-step. Putting a briefing there would break
the byte-identity that makes the preamble worth having — the same failure mode as
`1ed2266`, where a per-step slice landed on top of a project-global preamble.

### 3.5 Gates

| where | rule | why there |
|---|---|---|
| step 3 validation | every `capabilities` entry ∈ offer list | an invented name must fail at the card, not at the step that silently gets nothing |
| registration | every granted tool resolves | skillflow's own note: *"a capability whose tool is missing grants nothing just as quietly"* |
| claim time | offer list re-checked | a hand-edited card cannot smuggle |
| boot | offer list ⊆ registry | declared requirement vs deployment reality |

### 3.6 Escape hatch — the failure mode of least privilege is silent inability

If the PM under-declares, the implementer has no art tools and no way to say so; the observable
result is a task quietly completed with `ColorRect` placeholders and a green report.

- **`capabilities_available()`** stays in every implementer's toolset (~200 B): a read-only list
  of what *could* have been declared, so the agent fails legibly ("this needs `game_assets`; the
  card does not declare it") instead of substituting a placeholder.
- **3_review checks the mapping**: architect's resource list vs the cards' declarations. A task
  that needs art with nothing declared is caught before implementation, not after.

### 3.7 Lifecycle — update, archive, and the dangerous default

**Update is re-registration.** `register_capability(name, ...)` is idempotent by name, which is
how `register_forge_pipeline` already handles pipeline edits, and it matches the coding-mode loop
(`generate → drive → fix → drive`). No separate `update_capability`.

**But the overwrite needs an owner.** Today registration is one silent line. In a global registry
fed by base, addons and generated artifacts, a second `game_assets` would change what DPE's
implementer is granted with nothing in the log. So every capability carries an `owner`
(`host` / `addon:<name>` / `gen:<slug>`):

- same owner re-registering ⇒ update;
- **different owner ⇒ `CapabilityOwnerConflict`**, naming both sides.

**Delete is `archive_capability(name, purge=False)`**, not a file deletion — for the reason
`archive_generated_pipeline` exists: removing `~/.AItelier/capabilities/foo.json` does not
unregister it from the live registry, so behaviour would differ before and after a restart. A
zombie capability is the zombie pipeline over again.

- live-unregister **and** move the definition to `_archived/`, recorded in `archived.json` (boot
  scan and palette both consult it);
- **refuse while any config still offers it** — mirroring "a provider a route still names cannot
  be deleted". Removing the offer first is one explicit step, not an accident;
- `purge=True` deletes the definition outright, for when the forge produced junk (the
  `register_tool` mis-copy that poisoned `~/.AItelier/tools/` is the precedent).

**Not provided:** rename (it is delete + create, and configs reference by name — do the two steps
explicitly); lifecycle APIs for host-code capabilities (`stateful`, `tool_creation` are code,
their update is a deploy); briefing versioning (artifact history and the trace already answer
which briefing ran).

### 3.8 Where a definition lives

| kind | lives in | comes back via |
|---|---|---|
| host | `api/dependencies.py` | code |
| addon | the addon's `capabilities:` + host registration | addon registration |
| generated | **`~/.AItelier/capabilities/<name>.json`** | **boot scan** |

The third row is the same pattern as generated tools (`~/.AItelier/tools/`) and generated
pipelines (`~/.AItelier/configs/`). A pattern's third instance is usually a sign it is the right
one.

## 4. Pipeline generation

Not wiring the forge into this is worse than leaving it out: a forge that builds tools with no
capability to attach them to reproduces the bug `stateful` was invented to fix — a generated tool
hard-coding `~/.aitelier`, a path nothing mounts.

1. **`forge_palette` gains a Capabilities section** — name, granted tools, one line of purpose.
   It already renders tools and models; capabilities are the third table. What is not in the
   palette gets invented, which is exactly why `role_model_known` had to exist.
2. **`emit_graph` can emit `capability:`** on a step and `x-aitelier.capabilities` on the config.
3. **`forge_registry_check` gains `capability_known`** — same rule shape as `role_model_known`,
   plus: every tool a declared capability grants must resolve. Rejected at emit, not at the
   generated pipeline's first run.
4. **The forge can create capabilities**, via a `register_capability` host tool mirroring
   `register_tool`: writes `~/.AItelier/capabilities/<name>.json` with
   `owner: "gen:<slug>"`, live-registers, boot-scanned thereafter.

## 5. Management surface

One module owns the invariants — `core/capability_registry.py`, shaped like
`core/model_registry.py`, which exists for exactly this reason (the invariants live in one place
instead of being re-implemented by each caller):

1. a capability may only grant tools that resolve;
2. a config may only offer capabilities that exist;
3. a capability a config still offers cannot be deleted;
4. same-name/different-owner is a conflict, never an overwrite;
5. writes are atomic (`os.replace`) and drop the cache;
6. REST (`/api/capabilities`) and MCP both call this module — neither grows a private path, and
   a test pins that (the same test `core/model_registry.py` already carries).

Four tools:

| tool | used by | does |
|---|---|---|
| `capability_palette` | PM (context source), forge maker | offer list ∩ registry, plus a visible "declared but not registered on this machine" difference |
| `register_capability` | forge tool_loop, host | define/update a capability, owner-checked |
| `archive_capability` | host / operator | retire a capability, offer-checked, reversible unless `purge` |
| `capabilities_available()` | every implementer (~200 B) | the escape hatch of §3.6 |

### 5.1 Managing a pipeline's offer list

The four tools above manage *definitions*. A second, separate question is which pipeline offers
what — and there is nothing for it today: `list_pipelines` / `describe_pipeline` report
`input_hint`, seed shape and drive mode, and neither they nor any other tool can add or remove
anything from a pipeline.

- **`describe_pipeline` gains `capabilities`** — a pipeline's offer list is part of its contract,
  the same way `input_hint` is, and a PM palette that disagrees with `describe_pipeline` would be
  a bug nobody could see.
- **`pipeline_capability_add` / `pipeline_capability_remove`** write to *where the pipeline
  lives*, which is not the same place for the two kinds:
  - **generated** (`gen_*` in `~/.AItelier/configs/`): edit the persisted YAML and
    `reload_generated_pipeline` — the path that already exists for editing a generated pipeline.
  - **built-in** (in the repo, e.g. `dpe_default`): **refused**, naming the file to edit. A
    runtime mutation of a repo config is drift that no checkout can see; the supported way to add
    a capability to a built-in base is the mechanism that already exists for adding *anything*
    to a built-in base — compose it with an addon that offers it.
- **`remove` is offer-list-only.** It never touches a definition; `archive_capability` is the
  other operation, and keeping them separate is what makes invariant 3 ("a capability a config
  still offers cannot be deleted") enforceable rather than circular.

## 6. Migration for the two asset tools

1. Register `game_assets` (tools + briefing, `owner: addon:game_harness`).
2. Remove `gen_image_asset` / `gen_audio_asset` from `agent_configs/dpe_default.yaml`'s
   `task_implementer`.
3. Move the asset discipline out of `configs/addons/game_harness/implementer.md` into the
   briefing; leave the template's non-asset guidance alone.
4. `t_impl` declares `capability: { from_item: "capabilities" }`.
5. PM template + palette + the 3_review mapping check.

## 7. What this does not do

It does not make a *used* capability cheaper: a task declaring `game_assets` pays the schemas and
the briefing on every one of its turns, which is correct. It removes the cost from tasks that
never use it — which, in the measured window, was all of them.

## 8. Sequencing

- **Batch 1 — the mechanism.** Global registry + offer lists + `from_item` + briefing channel +
  palette + step-3 gate + escape hatch + lifecycle/invariants. `game_assets` is the first real
  user, and §6 is its migration.
- **Batch 2 — the forge.** Palette section, `emit_graph` support, `capability_known`,
  `register_capability` tool + boot scan.

Batch 2 has nothing to generate until batch 1 exists; batch 1 without batch 2 serves only
hand-written pipelines.

## 9. Deferred (not in scope for either batch)

- **Does a capability ever need to grant *context sources* as well as tools?** A robot-arm
  capability plausibly wants a calibration file in context, which is neither a tool nor a
  briefing. Deferred: nothing needs it yet, and `context_provider` + briefing may well cover it.
- **Per-item briefings.** If two tasks in one run declare the same capability, both pay the
  briefing on all their turns. That is correct but not obviously optimal; measure before
  optimising.
