"""forge_palette — grounding context for pipeline_forge agents.

Surfaces the LIVE tool registry + exemplar config paths + the AItelier graph-idiom
cheatsheet, so the designer references only REAL primitives (and declares missing
ones as tool-creation tasks) instead of hallucinating tool names.
"""
from __future__ import annotations

from pathlib import Path


# The AItelier skillflow-graph idiom & trap cheatsheet — the encoded knowledge the
# built-in skill_converter lacks. Sourced from the project's hard-won memories.
CHEATSHEET = """\
## AItelier skillflow-graph conventions (MUST follow)

1. Maker != checker, both AGENTS. Every creative `agent` step is immediately
   followed by a real `agent` reviewer that emits review_verdict.json
   {"passed": bool, "feedback": str, "suggestions": [str]}. NEVER fake a review
   with a boolean tool. The reviewer defaults to fail-on-uncertainty. Format
   issues are NOT blocking reasons.
2. Native max_loop, NEVER hand-rolled counters. Bound every cycle with `max_loop`
   on a transition edge. Do NOT invent increment/check counter tool steps or
   counter files.
3. Loop-external `done` gate. The ONLY terminal carrying the
   node_reached...completed end-condition must be a `step_type: gate` with
   `to: null` (or empty transitions), reached only on a real pass. Give-up paths
   must end FAILED — never share the success terminal (else exhausting a fix
   budget falsely reports `completed`).
4. Objective gate BEFORE semantic review where a suite/build exists (run_tests,
   pytest, a compile tool). A reviewer with no execution tool cannot catch a
   broken build.
5. Staged write + promotion. Steps that mutate use output.mode: write into
   $STEP_DIR; validation gates promotion; repo mutation goes through repo_apply
   lifecycle. Surgical edit/create, never whole-file overwrite.
6. Manifest -> loop fan-out for per-item work: a step emits a manifest
   {"execution_order": [[id,...],...]}; a `step_type: loop` node consumes it with
   loop.source + item_as + $var interpolation in later steps' context paths.
   A loop node is NOT self-bounding to the lint: put max_loop: N (=max_iterations)
   on the loop->body edge AND the body->loop return edge, or the lint fails the
   cycle as unbounded. End the loop body on an AGENT step (loop crediting).
7. Verdict routing: transitions branch on the verdict file, e.g.
   {to: next, match: {from_file: review_verdict.json, field: passed, value: true}}
   and the reject edge loops back to the maker with max_loop: 3.

## Step types
- agent : executed by an LLM role (needs agent_config). Reads `context`, writes
  `output`. Reviewers are agent steps.
- tool  : auto-executed inline by the engine (needs tool_name; its return dict
  becomes the transition flags).
- loop  : iterates a workspace-file manifest list (loop.source + item_as).
- gate  : pure flag routing, no execution; use for the loop-external terminal.

{CAPABILITIES_SECTION}

## When in doubt, read the spec — the `skillflow_docs_*` tools
This cheatsheet is the common case. For ANY field, lifecycle hook, context mode,
validation tool, path variable, or end-condition type you're unsure of, use
`skillflow_docs_list` (topics) → `skillflow_docs_search` (grep a term, line-numbered
hits) → `skillflow_docs_read` (read around a hit). `schema-source` (graph.py) is the
authoritative field list; `engine-source` (core.py) documents the runtime rules below
(e.g. `skillflow_docs_search` "credit" for the loop-crediting rule).

## Gotchas the gates CANNOT see (nothing will catch these for you)
- LOOP CREDITING: skillflow only advances a loop to its next item when an **agent**
  step returns to the loop node (credited in confirm_step). If a **tool** step is the
  loop-return, the item is never credited and the loop RE-SERVES it forever. So end a
  loop body on an agent step (`search skillflow_docs credit`).
- LOOP VARS: a loop var like `$current_x` is interpolated in a step's `context` file
  paths but NOT in `tool_params`. A tool inside a loop gets the current item via the
  engine-injected `task_name` kwarg, not via `$current_x` in tool_params.
- TERMINAL GATE: the completed-terminal gate needs `transitions: [{to: null}]`, NOT an
  empty list (`[]` → "no matching transition" → the run fails).
- `draft_commit` COMMITS ITS OWN STEP DIR. With no `source_dir` it commits what THIS
  step staged — a step that writes nothing commits nothing and returns
  `{"error": "Source dir not found: .../<step>"}`. To commit another step's output,
  pass `source_dir` explicitly in `tool_params`.
- TOOL NAMES ARE GLOBAL. Generated tools live in one flat namespace shared by every
  pipeline, and registering a name replaces whatever held it. Prefer
  `<domain>_<verb>`; never claim a generic name like `edit_file` or `fetch_data`.
"""


def _enforced_rules_section() -> str:
    """Render the registry gate's OWN rule table.

    Rendered, not restated. A gate rule the emitter is never taught costs one
    guaranteed rework round on every generation forever — which is exactly what
    happened when the write-mode `validation` rule shipped as a check while the
    palette mentioned validation only in passing. One table, two consumers: the
    gate runs the checks, this renders them.
    """
    try:
        from aitelier.tools.forge_registry_check.impl import RULES
    except Exception as e:  # pragma: no cover - defensive
        return (f"## Rules the registry gate enforces\n\n> WARNING: could not load "
                f"the gate's rule table ({e}) — check `forge_registry_check`.\n")
    out = ["## Rules the registry gate enforces — fix these BEFORE emitting\n",
           "Each one is an automatic rejection that costs a full rework round.\n"]
    for r in RULES:
        mark = "" if r.enforced else "  *(not auto-checked — but every surface shows it)*\n"
        out.append(f"- **{r.id}** — {r.teaches}\n{mark}")
    return "\n".join(out)

# Curated exemplars the architect should read (via read_file) rather than inline —
# keeps grounding token-bounded while pointing at battle-tested structures.
EXEMPLARS = [
    ("configs/dpe_default.yaml", "full DPE: research->architect->PM->task_loop->verify, Green/Red pairs, manifest fan-out"),
    ("configs/subagent.yaml", "minimal Green worker -> Red reviewer -> loop-back (the canonical gated worker)"),
    ("configs/code_review.yaml", "one-shot inline diff review, synchronous verdict"),
    ("configs/fix_tests.yaml", "objective test-fix loop: fix -> run_tests gate -> loop until green"),
    ("agent_configs/dpe_default.yaml", "role table: model/template/tools/thinking per role, maker vs reviewer profiles"),
]


_CAPABILITY_DECLARATION_HELP = """## Capabilities — let the FRAMEWORK provision a step's tools + context
A step declares a capability and the ENGINE hands it that capability's tools plus
its briefing, so neither you nor the agent picks a toolset or a write folder
(least privilege). Three declaration forms:

```yaml
  - id: persist_positions
    step_type: tool
    tool_name: persist_positions
    capability: stateful            # one name
    tool_params: { source_path: "$STEP_DIR/positions.json" }
    transitions: [ { to: done } ]

  - id: build
    step_type: agent
    capability: [stateful, tool_creation]     # several

  - id: t_impl                                # per LOOP ITEM: the planning step
    step_type: agent                          # writes `capabilities: [...]` on a
    capability:                               # task card and only THAT task gets
      from_item: capabilities                 # the tools
      card: "3/tasks/$current_task.json"
```

And the graph declares what it OFFERS, at the top level beside `steps:`:

```yaml
capabilities: ["stateful"]        # bounds what a task CARD may grant
```

The offer list binds DATA, not you: a name you write into a step is honoured
even with no offer list, but a name arriving from a task card is refused unless
the graph advertises it. Declaring an offer list also makes a step's own
capability checked at registration.

**A tool granted by a capability must never compute its own path.** It writes
RELATIVE to the `state_dir` (or other kwarg) the capability injects — a
hardcoded absolute path under the user's home directory escapes the mount and is
lost on the next container rebuild."""


def _capabilities_section() -> str:
    """The capabilities this deployment can actually provision.

    Rendered LIVE, like the model list and for the same reason: this section was
    a hand-written list of two, so a capability added anywhere else (an addon
    shipping one, the forge authoring one) was invisible to the next generation
    — and a maker that cannot see a capability writes the tool grant by hand,
    which is precisely what capabilities exist to stop.
    """
    try:
        from api.dependencies import get_skillflow
        caps = getattr(get_skillflow(), "_capabilities", {}) or {}
    except Exception as e:                               # noqa: BLE001
        return (_CAPABILITY_DECLARATION_HELP
                + f"\n\n> WARNING: could not read the capability registry ({e}). "
                  "Declare no capability rather than guessing a name.\n")
    if not caps:
        return (_CAPABILITY_DECLARATION_HELP
                + "\n\nThis deployment registers NO capabilities — do not "
                  "declare one.\n")
    rows = ["\nAvailable on this deployment (a name outside this list grants "
            "NOTHING, and the registry gate rejects it):\n"]
    for name in sorted(caps):
        cap = caps[name] or {}
        tools = ", ".join(f"`{t}`" for t in (cap.get("tools") or ()))
        if not tools:
            # A capability may grant no tools and still do the important half:
            # inject framework-chosen kwargs into the step's tool calls. Saying
            # "no extra tools" reads as "does nothing" and is why a maker would
            # skip it and let a tool pick its own directory.
            tools = ("no extra tools — it INJECTS framework kwargs into this "
                     "step's tool calls" if cap.get("context_provider")
                     else "no extra tools")
        first = next((ln.strip() for ln in (cap.get("briefing") or "").splitlines()
                      if ln.strip() and not ln.startswith("#")), "")
        purpose = f" — {first[:110]}" if first else ""
        rows.append(f"- `{name}` → {tools}{purpose}")
    return _CAPABILITY_DECLARATION_HELP + "\n" + "\n".join(rows) + "\n"


def _repo_root() -> Path:
    # aitelier/tools/forge_palette/impl.py -> repo root is three parents up.
    return Path(__file__).resolve().parents[3]


# One line per internal model, for a maker choosing a role's brain. Keyed on the
# route NAME, so a deployment that renames or adds one still gets its own list —
# what is described here is the JOB each name is for, which is host knowledge,
# not something the route table can carry.
_MODEL_GUIDANCE = {
    "flash": "the default. Cheap and fast; every maker/reviewer role unless "
             "you can say why not.",
    "pro":   "a stronger generalist. For a role whose output the rest of the "
             "run is built on — a plan, a task breakdown.",
    "glm":   "an alternative strong generalist. Long-form authored documents.",
    "smart": "strongest at one-shot reasoning and algorithmic code, and WEAK "
             "at long agentic tool loops. Good for a judge or an architect; "
             "bad for a 20-turn implementer.",
    "vision": "image input. Only for a step that looks at rendered frames.",
}


def _models_section() -> str:
    """The internal model names this deployment can actually serve.

    Before the routing layer every generated role got `model: "host"` — one
    model for the whole pipeline, because a maker that invented a concrete
    `provider/model` string would hallucinate an endpoint nobody has. Internal
    names removed that: the set is small, stable, and every entry is guaranteed
    to resolve here, so a maker can pick per role from a list rather than
    accepting one size for everything.
    """
    try:
        from core.model_routes import ModelRoutes, config_or_example
        names = ModelRoutes(config_or_example("model_routes.json")).names()
    except Exception as e:                               # noqa: BLE001
        return (f"## Models\n\n> WARNING: could not read the model routes "
                f"({e}). Use `model: \"host\"` for every role.\n")
    if not names:
        return ('## Models\n\nNo routes configured — use `model: "host"`.\n')
    out = ["## Models — a role's `model:` must be ONE of these names\n",
           "These are INTERNAL names. Never write a `provider/model` string: "
           "which vendor serves each name is this deployment's config, and a "
           "name you invent will not resolve.\n"]
    for n in names:
        out.append(f"- `{n}` — {_MODEL_GUIDANCE.get(n, 'configured on this host.')}")
    out.append("\n`host` also still works and means the host default.\n")
    return "\n".join(out)


def forge_palette(include_signatures: bool = True, **kwargs) -> dict:
    """Return the live palette as a single markdown blob for prompt injection."""
    lines: list[str] = ["# pipeline_forge palette (grounding)\n"]

    # ── Live tool registry ────────────────────────────────────────────────
    try:
        from api.dependencies import get_skillflow
        loader = get_skillflow()._tool_loader
        names = sorted(loader.list_tools())
    except Exception as e:  # pragma: no cover - defensive
        loader = None
        names = []
        lines.append(f"> WARNING: could not read live tool registry: {e}\n")

    lines.append(f"## Available tools ({len(names)}) — reference ONLY these; "
                 "declare anything else as a tool-creation task\n")
    for name in names:
        sig = ""
        if include_signatures and loader is not None:
            try:
                schema = loader.load_schema(name) or {}
                desc = (schema.get("description") or "").strip().replace("\n", " ")
                params = schema.get("parameters") or {}
                pnames = ", ".join(sorted(params.keys())) if isinstance(params, dict) else ""
                sig = f" — ({pnames}) — {desc[:140]}"
            except Exception:
                sig = ""
        lines.append(f"- `{name}`{sig}")
    lines.append("")

    # ── Models the deployment actually has ────────────────────────────────
    lines.append(_models_section())

    # ── Exemplars ─────────────────────────────────────────────────────────
    root = _repo_root()
    lines.append("## Exemplar configs (read with read_file before designing)\n")
    for rel, why in EXEMPLARS:
        exists = (root / rel).exists()
        mark = "" if exists else " (not found on this host)"
        lines.append(f"- `{rel}`{mark} — {why}")
    lines.append("")

    # ── Cheatsheet ────────────────────────────────────────────────────────
    lines.append(CHEATSHEET)

    # ── What the gate will reject (rendered from the gate's own table) ────
    lines.append(_enforced_rules_section())

    # Capabilities are rendered LIVE from the registry (see

    # _capabilities_section); the cheatsheet only carries the placeholder,

    # so the substitution has to happen after every block is in.

    _md = ("\n".join(lines)).replace("{CAPABILITIES_SECTION}",

                           _capabilities_section())

    return {"palette_markdown": _md, "tool_count": len(names)}
