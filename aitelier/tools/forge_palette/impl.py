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

## When in doubt, read the spec — the `skillflow_docs_*` tools
This cheatsheet is the common case. For ANY field, lifecycle hook, context mode,
validation tool, path variable, or end-condition type you're unsure of, use
`skillflow_docs_list` (topics) → `skillflow_docs_search` (grep a term, line-numbered
hits) → `skillflow_docs_read` (read around a hit). `schema-source` (graph.py) is the
authoritative field list; `engine-source` (core.py) documents the runtime rules below
(e.g. `skillflow_docs_search` "credit" for the loop-crediting rule).

## Gate-invisible gotchas (the 3 gates will NOT catch these — get them right)
- LOOP CREDITING: skillflow only advances a loop to its next item when an **agent**
  step returns to the loop node (credited in confirm_step). If a **tool** step is the
  loop-return, the item is never credited and the loop RE-SERVES it forever. So end a
  loop body on an agent step (`search skillflow_docs credit`).
- LOOP VARS: a loop var like `$current_x` is interpolated in a step's `context` file
  paths but NOT in `tool_params`. A tool inside a loop gets the current item via the
  engine-injected `task_name` kwarg, not via `$current_x` in tool_params.
- TERMINAL GATE: the completed-terminal gate needs `transitions: [{to: null}]`, NOT an
  empty list (`[]` → "no matching transition" → the run fails).
"""

# Curated exemplars the architect should read (via read_file) rather than inline —
# keeps grounding token-bounded while pointing at battle-tested structures.
EXEMPLARS = [
    ("configs/dpe_default.yaml", "full DPE: research->architect->PM->task_loop->verify, Green/Red pairs, manifest fan-out"),
    ("configs/subagent.yaml", "minimal Green worker -> Red reviewer -> loop-back (the canonical gated worker)"),
    ("configs/code_review.yaml", "one-shot inline diff review, synchronous verdict"),
    ("configs/fix_tests.yaml", "objective test-fix loop: fix -> run_tests gate -> loop until green"),
    ("agent_configs/dpe_default.yaml", "role table: model/template/tools/thinking per role, maker vs reviewer profiles"),
]


def _repo_root() -> Path:
    # aitelier/tools/forge_palette/impl.py -> repo root is three parents up.
    return Path(__file__).resolve().parents[3]


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

    return {"palette_markdown": "\n".join(lines), "tool_count": len(names)}
