"""forge_registry_check — validate a generated graph against the LIVE registry.

Gate (b): every tool_name / agent_config / context source in the emitted graph
must resolve to a real primitive (the tools were built + registered upstream), and
the graph must obey the AItelier conventions a structural linter can't see. This is
what catches the `gen_game_subagent.yaml` class of defect (7 hallucinated tools).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


# ── The rule table: what this gate enforces IS what the palette teaches ────────
#
# Every rule the gate enforces has to be taught to the emitter, or the emitter
# learns it the expensive way — one rework round per generation, forever. That
# drifted once already: C1 (`write_steps_without_validation`) shipped as a gate
# rule while the palette said only "validation gates promotion" in passing, so
# every single generation burned a guaranteed round rediscovering it from the
# violation message.
#
# One table, two consumers: this module runs the checks, `forge_palette` renders
# `teaches` into the emitter's grounding context. Adding a rule therefore teaches
# it automatically. `tests/unit/test_forge_rule_table.py` asserts the binding —
# a new `_check` function with no RULES entry fails the suite.

class Rule:
    """A plain class on purpose — NOT a dataclass.

    This module is loaded via `spec_from_file_location` (ToolLoader), which leaves
    `sys.modules[__name__]` unset; `dataclasses` then dereferences that None while
    resolving annotations and the whole tool fails to import. Same trap the custom
    lint backends hit.
    """

    __slots__ = ("id", "teaches", "enforced")

    def __init__(self, id, teaches, enforced=True):
        self.id = id                 # matches the check function name (minus the _)
        self.teaches = teaches       # addressed to the emitter: the failure mode
        self.enforced = enforced     # False = taught only, not a hard failure

    def __repr__(self):
        return f"Rule({self.id!r})"


RULES: tuple[Rule, ...] = (
    Rule("tool_exists",
         "Every `tool_name` must already be in the live registry. Tools you asked "
         "for in the tool plan were built before emit; anything else is invented "
         "and rewinds the run."),
    Rule("role_defined",
         "Every `agent_config` must have a top-level entry in role_table.yaml, "
         "keyed by the exact role name the step uses. Do not nest the table under "
         "an `entries:` key — it is accepted but you will be told about it, and the "
         "wrong shape ships in the artifact for the next reader."),
    Rule("role_model_known",
         "A role's `model:` must be an INTERNAL name from the palette's Models "
         "section (`flash`, `pro`, …) or `host` — never a `provider/model` "
         "string. Which vendor serves each name is the deployment's config, so "
         "a concrete endpoint written here is one you guessed at, and an "
         "unknown name fails at the first LLM call rather than at emit."),
    Rule("capability_known",
         "A step's `capability:` must name one the palette lists, and a name a "
         "TASK CARD may declare must also be in the graph's top-level "
         "`capabilities:` offer list. An unregistered name grants nothing — "
         "silently, since a missing tool looks to the agent like a world where "
         "that work is impossible — and a card-declared name outside the offer "
         "list is refused by the engine at claim time, long after emit. "
         "`capabilities:` is a LIST, not a string. A `{from_item: ...}` "
         "declaration also needs `card:` — a loop item is a NAME, so the card "
         "path is the only way to reach its fields — and that path is resolved "
         "against the CONFIG directory, so it needs the folder holding the card "
         "(e.g. `3/tasks/$current_task.json`), not a bare filename."),
    Rule("counter_smell",
         "No hand-rolled counter tools (`increment_*`/`check_*_counter`, or any "
         "name containing 'counter'). Bound cycles with `max_loop` on an edge."),
    Rule("reviewer_is_agent",
         "A step whose id names a review must be `step_type: agent` emitting "
         "review_verdict.json — never a boolean tool."),
    Rule("reviewer_reads_maker",
         "A reviewer that loops back to its maker on `passed: false` MUST read "
         "that maker's output via a `{step: <maker>}` context source. Without it "
         "the reviewer judges blind, rejects every round, and the loop churns to "
         "failure."),
    Rule("context_refs_resolve",
         "Every context `source.step` must name a real step and every "
         "`source.tool` a real tool."),
    Rule("scope_declaration",
         "`scope:` is 'task' or 'all'. Omit it and the engine routes by position "
         "(same-loop reader → its own item, outside reader → all items); write "
         "`scope: all` on an out-of-loop reader of a loop-body AGENT producer."),
    Rule("completed_terminal_is_gate",
         "The node named by the `completed` end-condition must be a loop-external "
         "`step_type: gate` whose only transition is `to: null`. A tool or agent "
         "carrying the completed end-condition is a false green."),
    Rule("deliverable_before",
         "THE DELIVERABLE GOES ON THE SUCCESS PATH. The step that writes the "
         "user-visible result belongs between the last check and the terminal gate. "
         "If the success path is `... -> review -> done_gate`, a successful run "
         "produces only a verdict, and the answer-producing step usually ends up "
         "stranded on the give-up branch — the one path that reports FAILED."),
    Rule("role_tools_unknown", (
         "WRITE TOOLS ARE INJECTED BY THE FRAMEWORK — NEVER LIST THEM IN A ROLE'S "
         "`tools:`. A step's mutation vocabulary comes from its `output.mode`:\n"
         "    · `mode: write` (no fixed slots) → `create(file, content)`, "
         "`edit(file, old_str, new_str)`, `finish_step` — plus `write(file, content)` "
         "ONLY with `allow_full_write: true`.\n"
         "    · `mode: content` with fixed slots → `write_<slot>` / `create_<slot>` / "
         "`edit_<slot>` per slot (e.g. `create_verdict`), plus `finish_step`.\n"
         "  A role's `tools:` list is for REGISTRY tools only (web_search, run_tests, "
         "read_file…). A name in that list that does not resolve is DROPPED SILENTLY "
         "— the step still runs, just without it. There is no `write_file`, "
         "`create_file` or `edit_file`: those are a different application's coding "
         "tools, and a maker told to use them writes nothing at all while its step "
         "still reports success.")),
    Rule("template_names_absent_tools",
         "A role's template may only name tools that role actually has. The agent "
         "follows its PROMPT over its toolset: a template promising "
         "`create_file(path, content)` produced a maker that emitted its files as "
         "prose, wrote zero of them, and passed — until a reviewer rejected it four "
         "times and the loop died. Name the injected tools above."),
    Rule("write_steps_without_validation",
         "EVERY `mode: write` step must declare a `validation`. Without one, a step "
         "that writes nothing at all completes green — the same silent no-op as a "
         "maker that writes prose. With one, an empty step becomes a validation "
         "failure that retries in place WITH the reason attached. Use "
         "`validation: {type: file_exists, files: [\"*\"]}` when the filenames are "
         "not known ahead of time."),
    Rule("routing_file_unguaranteed",
         "IF A STEP'S TRANSITIONS ROUTE ON A FILE, THE STEP MUST GUARANTEE THAT "
         "FILE. An agent step whose edges read `match: {from_file: verdict.json, "
         "...}` has to either declare that file as a `mode: content` fixed slot, or "
         "name it in a `file_exists` validation. `files: [\"*\"]` does NOT count — "
         "it is satisfied by any file at all. This is not theoretical: a step wrote "
         "`final_answer.md`, passed its `[\"*\"]` validation, produced no "
         "`final_verdict.json`, and the run died on 'No matching transition from "
         "final_answer'. The whole proof was written and thrown away."),
    Rule("fallible_tools_unrouted",
         "A TOOL THAT CAN FAIL NEEDS A FAILURE EDGE. The engine confirms a tool step "
         "from its result dict WITHOUT inspecting `error`, so an unconditional "
         "`transitions: [{to: x}]` advances the run even when the tool returned "
         "`{\"error\": ...}`. Branch on the result: `- {to: next, match: {passed: "
         "true}}` plus a failure edge. Exactly ONE conditional edge is the same "
         "defect from the other side — a failure then matches nothing and kills the "
         "run with 'no matching transition'."),
    Rule("failure_rejoins_success",
         "THE FAILURE EDGE MUST GO SOMEWHERE THAT HANDLES THE FAILURE — back to the "
         "maker with the error, or a terminal that ends the run failed. Routing it "
         "into a gate that forwards to the same place success goes is a no-op wearing "
         "a branch's clothes: the gate does no work, so a failed `repo_apply` (the "
         "code never landed) walks into the next phase exactly as if it had worked."),
    Rule("duplicate_max_loop_edges",
         "ONE `max_loop` EDGE PER (from, to) PAIR. skillflow keys its loop counter on "
         "(run, from_step, to_step), so two bounded edges between the same two steps "
         "make the graph impossible to START — the run dies on a UNIQUE constraint "
         "before step one, and the linter will not warn you. Keep one bounded edge "
         "and distinguish the cases with `match`. (Parallel edges are fine as long as "
         "at most one carries `max_loop`.)"),
    Rule("unreachable_terminals",
         "EVERY TERMINAL NEEDS AN END CONDITION. A step with `to: null` (or no "
         "transitions) ends the graph, and if no `node_reached` condition names it "
         "the run reaches it, does its work, and then dies with 'no matching "
         "transition' — the output is written and thrown away. This is how an ABSTAIN "
         "outcome ('no confident answer') silently became a failure: the give-up "
         "branch produced the answer and had nowhere to go. Give the give-up path its "
         "own terminal gate with its own end condition (`result: failed` is fine — "
         "the deliverable still lands)."),
    Rule("validation_is_a_spec_list",
         "`validation:` IS A LIST OF SPECS KEYED BY `tool:`, NOT A MAPPING. "
         "skillflow iterates what you give it and calls `.get()` on each element, "
         "so a mapping yields its string KEYS and the step dies with \"'str' object "
         "has no attribute 'get'\" — after the agent has already produced its "
         "output, so every retry re-runs the whole step. The key is `tool`, not "
         "`type`. Shape: `validation:` / `  - files: [\"verdict.json\"]` / "
         "`    tool: file_exists`. `gen_dsh_code_review` shipped the mapping form "
         "on four steps through all three gates and burned two full LLM reviews "
         "before failing on it."),
    Rule("the_seed_actually_reaches_the_first_step", enforced=False, teaches=(
         "A PIPELINE THAT TAKES RUNTIME INPUT MUST WIRE ITS OWN SEED, AND SAY SO. "
         "The host writes `seed_text` to `$CONFIG_DIR/_seed/<seed_file>` — that "
         "literal path is the whole contract, and nothing hands it to a step for "
         "you. An agent step reads it with a context source "
         "`{config: <this pipeline name>, output: <seed_file>}`; a TOOL step takes "
         "it as a tool_param, e.g. `diff_path: $CONFIG_DIR/_seed/seed_input.md`. "
         "Also declare it: `x-aitelier: {seed_file: <name>, input_hint: <what to "
         "send>}` — without `input_hint` a caller has to guess what seed_text is, "
         "and `list_pipelines` shows the pipeline with nothing to say about its "
         "input. `gen_dsh_code_review` shipped all three gates green and failed on "
         "its FIRST drive: its capture step got only `project_root`, so it ran "
         "`git diff HEAD` on an empty throwaway repo and routed to `input_failed` "
         "while the caller's diff sat unread in `_seed/`. Structure was fine; the "
         "input was never connected.")),
    Rule("tools_do_not_read_meaning_from_framework_paths", enforced=False, teaches=(
         "A TOOL MUST NOT DERIVE MEANING FROM A PATH THE FRAMEWORK CHOSE. A step's "
         "staging/output directory is named after the STEP ID (`$STEP_DIR`, "
         "`$CONFIG_DIR/<step>`), not after the thing being built, and no agent can "
         "rename it. A generated validator that asserted `basename(skill_dir) == "
         "frontmatter.name` was UNSATISFIABLE: the directory was called `draft`, so "
         "writing SKILL.md at its top was rejected for the name, and writing it "
         "under `<skill-name>/` instead was rejected as 'SKILL.md not found'. The "
         "maker oscillated between the two until the loop exhausted — on every run. "
         "Take the expected name as an explicit parameter, or from the content.")),
    Rule("prompt_build_backend_is_real",
         "A CODE TEMPLATE YOU PASTE INTO A ROLE PROMPT IS EXECUTED VERBATIM ON "
         "EVERY RUN, FOREVER. `gen_mcp_server_builder`'s scaffold role shipped "
         "`build-backend = \"setuptools.backends._legacy:_Backend\"` — a symbol that "
         "does not exist — so `pip install -e .` died with `BackendUnavailable` on "
         "the first attempt of every run, and a fix lap was spent re-deriving the "
         "same correction each time. setuptools' backend is "
         "`setuptools.build_meta` (or `setuptools.build_meta:__legacy__`). More "
         "generally: a template is not a suggestion the maker will sanity-check, "
         "so do not paste an API you are not certain of — describe what the file "
         "must contain and let the maker write it against the error it gets."),
    Rule("a_tool_gate_declares_that_an_error_is_its_verdict", enforced=False,
         teaches=(
         "A TOOL STEP WHOSE RESULT CARRIES `error` FAILS THE STEP AND THE RUN, "
         "UNLESS THE NODE SAYS `tool_error: \"route\"`. That default is what stops "
         "a plumbing tool (a repo apply, a git sync) that refused to do anything "
         "from being recorded `completed` and the run reporting success. But a "
         "GATE tool step is the opposite case: its `error` is its VERDICT, meant "
         "to be routed (and injected as `feedback:`) back to the maker. Any tool "
         "step that can answer `{passed: false, error: <what is wrong>}` — a lint, "
         "a test, a check, a capture step that reports an unusable input — must "
         "carry `tool_error: \"route\"` next to its `tool_name`, or its first red "
         "verdict kills the run instead of looping back. Unenforceable here: "
         "whether a tool ever sets `error` is a fact about its code, not about the "
         "graph.")),
    Rule("step_ids_are_legible", enforced=False, teaches=(
         "Name step ids for what they DO (`interview`, `draft`, `validate`, "
         "`package`) — not `A1`/`B2`/`C1`. Step ids are what every surface shows: "
         "the dashboard, the trace, checkpoint labels, and these violation "
         "messages.")),
)


# Known agent roles that are resolved by the host even without a role-table entry
# (host/default agents, and the base converter/coding roles). Kept small on purpose.
_KNOWN_HOST_ROLES: set[str] = set()

# Counter-tool smell: these names (or any containing "counter") mean a hand-rolled
# loop bound where a native max_loop edge belongs.
_COUNTER_SMELL = {"increment_fix_counter", "check_fix_counter", "increment_counter"}


def _load_yaml(path: str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _live_tools() -> set[str]:
    try:
        from api.dependencies import get_skillflow
        return set(get_skillflow()._tool_loader.list_tools())
    except Exception:
        return set()


def _loop_bodies(graph: dict) -> dict:
    """Map each loop node id → set of its body step ids, using SKILLFLOW'S OWN
    topology (graph.loop_body_map, reach-back semantics: a body node must be
    able to return to the loop — a give-up/drain target is NOT body). Parsing
    via PipelineGraph keeps this gate byte-identical to the engine's runtime
    routing; a hand-rolled mirror here once drifted (forward-only reachability
    exempted drain-edge aggregators from the scope rule). Returns {} when the
    graph doesn't parse — the lint gate before this one reports the parse error.
    A loop-body AGENT step's output is per-item ({step}/{item}/), which changes
    how it must be read; tool body steps stay flat and are exempt.
    """
    try:
        from skillflow.graph import PipelineGraph, loop_body_map
        g = PipelineGraph._from_dict(dict(graph))
        return {lid: set(body) for lid, body in loop_body_map(g.steps).items()}
    except Exception:
        return {}


# ── "Can this tool FAIL?" — ask the tool, not a list ──────────────────────────
#
# A tool whose result carries `passed`/`error` needs its failure routed, or the
# engine takes the single unconditional edge and a failure reads as success
# (skillflow confirms a tool step from its result dict without inspecting `error`).
#
# This used to be a hardcoded allowlist of built-in names, which by construction
# cannot see the tools the forge itself GENERATES — precisely the ones a generated
# graph routes. `skill_package_zip` returns `{"passed": False, "error": ...}` on
# three paths, was emitted with one unconditional edge into the COMPLETED terminal,
# and shipped: a failed zip reported a successful run. (The allowlist had also
# rotted: three of its seventeen names no longer resolve to any tool, and one was a
# GENERATED tool somebody had already hand-added — the maintenance model failing in
# both directions at once.)
#
# The answer now lives with the tool, in its `tool.yaml`: `x-fallible: true`.
# `register_tool` derives and stamps it for every generated tool.
_FALLIBLE_SCHEMA_KEY = "x-fallible"

# skillflow's OWN tools ship from PyPI, so their tool.yaml cannot carry the stamp
# until the next release. Explicit, short, and deletable — not a heuristic.
_FALLIBLE_UPSTREAM = {"pytest", "repo_apply", "repo_validate", "draft_commit",
                      "compose_validate"}

# Prefixes that name a CHECK — kept as a cheap belt for a hand-written tool that
# was never stamped. Deliberately not "test_": the registry has a real `test_write`
# tool that writes a test file, and flagging it would fail a correct graph for not
# routing a failure it does not have.
_FALLIBLE_PREFIXES = ("verify_", "check_", "validate_", "lint_")


def _fallible_names(live_tools: set[str]) -> set[str]:
    """Live tools that DECLARE they can fail, plus the not-yet-stampable upstream set.

    Computed once per gate run — `load_schema` is loader-cached, but the set is also
    what the violation messages reason about. The upstream names are NOT intersected
    with the live registry: when the loader is unavailable (a unit test, a boot-order
    edge) an empty intersection would silently switch the whole rule off, which is
    exactly the fail-open shape this rule exists to catch.
    """
    names = set(_FALLIBLE_UPSTREAM)
    try:
        from api.dependencies import get_skillflow
        loader = get_skillflow()._tool_loader
    except Exception:
        return names
    for t in live_tools:
        try:
            if (loader.load_schema(t) or {}).get(_FALLIBLE_SCHEMA_KEY) is True:
                names.add(t)
        except Exception:
            continue          # unresolvable name — the tool_exists rule reports it
    return names


def _is_fallible(tool_name: str, declared: set[str]) -> bool:
    """Judge the name the STEP uses, not only names that resolve today.

    The prefix belt is applied per-name rather than filtered through the registry: a
    graph naming `verify_claims` still needs its failure routed, and whether that
    tool is registered yet is a different violation with its own message.
    """
    return bool(tool_name) and (tool_name in declared
                                or tool_name.startswith(_FALLIBLE_PREFIXES))


def _declared_outputs(step: dict) -> tuple[list[str], bool]:
    """Files a step declares it writes — see core.pipeline_registry for the rules.

    Shared with the registrar rather than reimplemented: both the long form
    (`{key: {file: "x.md"}}`) and the shorthand (`{key: "x.md"}`) are legal and
    both appear in this repo's configs, and a reader that knows only one of them
    silently concludes a deliverable-writing step writes nothing.
    """
    from core.pipeline_registry import declared_output_files
    return declared_output_files(step)


def _deliverable_before(terminal: str, steps: list, by_id: dict) -> list[str]:
    """The success terminal must be reached from something that wrote the result.

    A pipeline whose success path runs review → done_gate reports `completed` while
    having produced only a verdict: the answer step ends up stranded on the give-up
    branch, so the ONLY run that emits the deliverable is the failed one.
    """
    preds = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        for t in s.get("transitions") or []:
            if isinstance(t, dict) and t.get("to") == terminal:
                preds.append(s)
                break
    if not preds:
        return []
    for p in preds:
        files, knowable = _declared_outputs(p)
        if not knowable:                       # writes something we can't enumerate
            return []
        if any(f != "review_verdict.json" for f in files):
            return []
    names = ", ".join(str(p.get("id")) for p in preds)
    return [f"success terminal '{terminal}' is reached only from [{names}], which write "
            f"nothing but review_verdict.json — no step on the success path produces the "
            f"run's deliverable. Put the result-producing step between the last check and "
            f"the terminal gate (a give-up branch is not where the answer belongs)."]


def _unreachable_terminals(steps: list, ends: list) -> list[str]:
    """A step that ends the graph must be a step the graph knows how to end on.

    `to: null` (or no transitions at all) makes a step terminal. If no end
    condition names it, the run reaches it, does its work, and then dies — there
    is no next node and no end condition to fire. This is how a give-up branch
    that DOES produce the answer still reports failure and returns nothing: the
    abstain outcome the user asked for is written to disk and then thrown away.
    """
    named = {c.get("node") for c in ends
             if isinstance(c, dict) and c.get("type") == "node_reached"}
    out = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        trans = [t for t in (s.get("transitions") or []) if isinstance(t, dict)]
        if any(t.get("to") for t in trans):
            continue                       # has a real outgoing edge
        sid = s.get("id")
        if sid not in named:
            out.append(
                f"step '{sid}' is terminal (no outgoing edge) but no end condition "
                f"names it — a run that reaches it dies with no matching transition, "
                f"discarding whatever it produced. Route it to a terminal `gate`, or "
                f"add a node_reached end condition for it.")
    return out


# The framework injects a step's mutation tools from its `output.mode` (see
# skillflow.write_tools.generate_write_tool_schemas). Naming one in a role's `tools:`
# is redundant but harmless — and it is what nine of the ten pipelines generated so far
# do, all of which work. So this set is EXEMPT from the unknown-tool rule rather than a
# violation of its own: flagging the working convention would fail correct graphs.
_INJECTED_WRITE_TOOLS = {"create", "edit", "write", "finish_step"}


def _role_model_known(rt) -> list[str]:
    """A role's `model:` must be a name this deployment can actually serve.

    Before internal names the emitter had one safe answer (`host`) and any
    attempt to be more specific meant inventing a `provider/model` for an
    endpoint nobody has. Now the palette hands it a short, live list, so being
    specific is safe — and this is what keeps it honest: a name outside the
    table resolves to nothing and the generated pipeline dies at its first LLM
    call, long after the emit step that could have caught it.
    """
    out = []
    if not isinstance(rt, dict):
        return out
    try:
        from core.model_routes import ModelRoutes, config_or_example
        known = set(ModelRoutes(config_or_example("model_routes.json")).names())
    except Exception:                                    # noqa: BLE001
        return out          # no table to check against; not the emitter's fault
    allowed = known | {"host", "default"}
    for role, cfg in rt.items():
        if not isinstance(cfg, dict):
            continue
        model = cfg.get("model")
        if not isinstance(model, str) or not model or model in allowed:
            continue
        hint = ("write an internal name, not a provider endpoint"
                if "/" in model else "not a configured model")
        out.append(
            f"role_model_known: role '{role}' declares model '{model}' — {hint}. "
            f"Available: {sorted(known)} (or 'host').")
    return out


def _capability_known(graph, steps) -> list[str]:
    """Every declared capability must exist, and a card-declared one be offered.

    Same shape and the same reason as `_role_model_known`: the palette hands the
    maker a live list, and a name outside it fails at RUNTIME as silence — the
    step simply runs without the tools it asked for, which no gate downstream
    can see. The offer list is checked too because it is what bounds a task
    card: `{from_item: ...}` with nothing offered grants nothing, every time.
    """
    out: list[str] = []
    try:
        from api.dependencies import get_skillflow
        known = set(get_skillflow().capabilities())
    except Exception:                                    # noqa: BLE001
        return out          # no registry to check against; not the emitter's fault
    declared_offers = (graph.get("capabilities") if isinstance(graph, dict) else None)
    if declared_offers is not None and not isinstance(declared_offers, list):
        # `capabilities: "stateful"` makes a set of CHARACTERS, in the gate and
        # in the engine alike — every name then looks unoffered.
        return [f"capability_known: graph `capabilities:` must be a LIST of "
                f"names, got {type(declared_offers).__name__}."]
    offers = set(declared_offers or ())
    step_ids = {st.get("id") for st in steps if isinstance(st, dict)}
    for name in sorted(offers - known):
        out.append(
            f"capability_known: graph offers capability '{name}', which this "
            f"deployment does not register — every task card declaring it would "
            f"grant nothing. Available: {sorted(known) or 'none'}.")
    for st in steps:
        if not isinstance(st, dict):
            continue
        cap = st.get("capability")
        if not cap:
            continue
        sid = st.get("id", "?")
        if isinstance(cap, str):
            names = [cap]
        elif isinstance(cap, list):
            names = [c for c in cap if isinstance(c, str)]
        elif isinstance(cap, dict):
            if not cap.get("from_item"):
                out.append(
                    f"capability_known: step '{sid}' declares a capability "
                    f"object without `from_item` — it grants nothing. Use a "
                    f"name, a list, or {{from_item: <card field>, card: <path>}}.")
                continue
            card = cap.get("card")
            if not card:
                out.append(
                    f"capability_known: step '{sid}' declares `from_item` with "
                    f"no `card:` — a loop item is a NAME, so the card path is "
                    f"the only way to read its fields, and without it nothing "
                    f"is granted.")
            elif not isinstance(card, str):
                out.append(
                    f"capability_known: step '{sid}' has a non-string `card:` "
                    f"({type(card).__name__}) — it must be a path relative to "
                    f"the config directory, e.g. '3/tasks/$current_task.json'.")
            elif "/" not in card.strip("/"):
                # Only the unambiguous error is enforced. The engine resolves a
                # card against the CONFIG directory (not a step dir) and
                # interpolates $vars BEFORE splitting, so '/3/x.json',
                # '$plan_step/x.json' and a real non-step folder like
                # 'Outbox_Final_3/x.json' are all VALID — an earlier version of
                # this rule rejected all three, and a gate that blocks correct
                # output is worse than one that misses.
                out.append(
                    f"capability_known: step '{sid}' has card '{card}' with no "
                    f"directory part — it is resolved against the config "
                    f"directory, so it needs the folder that holds the card, "
                    f"e.g. '3/tasks/$current_task.json'.")
            elif (head := card.strip("/").split("/")[0]) not in step_ids \
                    and "$" not in head and "_" not in head and not head[:1].isupper():
                out.append(
                    f"capability_known: step '{sid}' reads its card from "
                    f"'{head}/', which is neither a step in this graph nor an "
                    f"obvious data folder. Steps: "
                    f"{sorted(x for x in step_ids if x)}.")
            if not offers:
                out.append(
                    f"capability_known: step '{sid}' reads capabilities from a "
                    f"task card, but the graph offers none — the engine refuses "
                    f"every card-declared name. Add a top-level "
                    f"`capabilities: [...]` list.")
            continue
        else:
            out.append(f"capability_known: step '{sid}' has an unusable "
                       f"`capability:` value of type {type(cap).__name__}.")
            continue
        for n in names:
            if n not in known:
                out.append(
                    f"capability_known: step '{sid}' declares capability "
                    f"'{n}', which this deployment does not register — it would "
                    f"grant nothing. Available: {sorted(known) or 'none'}.")
            elif offers and n not in offers:
                out.append(
                    f"capability_known: step '{sid}' declares capability "
                    f"'{n}', absent from the graph's own `capabilities:` offer "
                    f"list {sorted(offers)} — registration will reject the graph.")
    return out


def _role_tools_unknown(rt, live_tools: set) -> list[str]:
    """Every tool a ROLE is granted must exist.

    skillflow DROPS an unresolvable name — the role registers, runs, and quietly lacks
    the tool. Since 1.5.29 it at least records the miss (`SkillFlow.unresolved_tools()`,
    surfaced by the engine on a step that produced nothing), but recording it is
    after the fact: at emit time the role table is still a file on disk, skillflow has
    not seen it, and this is the only place the mistake can be caught BEFORE it ships.
    """
    out = []
    if not isinstance(rt, dict):
        return out
    for role, cfg in rt.items():
        if not isinstance(cfg, dict):
            continue
        tools = cfg.get("tools")
        if not isinstance(tools, list):
            continue
        unknown = [t for t in tools
                   if isinstance(t, str) and t not in live_tools
                   and t not in _INJECTED_WRITE_TOOLS]
        if unknown:
            out.append(
                f"role '{role}': tools {unknown} do not exist in the registry. A name "
                f"that does not resolve is dropped SILENTLY, so the role runs without "
                f"it. (There is no write_file/create_file/edit_file — write tools are "
                f"injected from the step's output.mode: `create`/`edit` for mode:write, "
                f"`create_<slot>` for mode:content.)")
    return out


_CALL_SHAPED = re.compile(r"`([a-z_][a-z0-9_]{2,})\(")
# Read tools the engine derives from a step's context, plus the ones every agent gets.
_ALWAYS_AVAILABLE = {"read_file", "read", "search", "list", "list_tree", "finish_step"}


def _effective_tools(role_cfg: dict, step: dict, live_tools: set) -> set:
    """Everything the agent for this step can actually call."""
    tools = {t for t in (role_cfg.get("tools") or [])
             if isinstance(t, str) and t in live_tools}
    out = step.get("output") or {}
    try:
        from skillflow.write_tools import generate_write_tool_schemas
        tools |= {w["name"] for w in generate_write_tool_schemas(
            out.get("mode", ""), out.get("fixed") or {},
            allow_full_write=bool(out.get("allow_full_write")))}
    except Exception:
        pass
    return tools | _ALWAYS_AVAILABLE


def _template_names_absent_tools(rt, steps: list, role_table_path: str,
                                 live_tools: set) -> list[str]:
    """A role template must only name tools that role actually has.

    The agent follows its PROMPT over its toolset. A template promising
    `create_file(path, content)` produced a maker that emitted its files as prose,
    wrote zero of them, and whose step still reported success — four rounds, until the
    reviewer's bounded loop burned out. The role's `tools:` list was a symptom; the
    template was the cause.

    Only call-shaped mentions (`` `name(`` ) count, so ordinary prose that merely
    mentions a tool is not flagged. Audited across the ten generated pipelines: 12 hits,
    all in the one broken pipeline, all genuinely non-existent tools.
    """
    if not isinstance(rt, dict):
        return []
    base = Path(role_table_path).parent if role_table_path else None
    by_role = {s.get("agent_config"): s for s in steps
               if isinstance(s, dict) and s.get("step_type") == "agent"
               and s.get("agent_config")}
    out = []
    for role, cfg in rt.items():
        step = by_role.get(role)
        if not isinstance(cfg, dict) or step is None:
            continue
        text = cfg.get("system_prompt") or ""
        if not text and base is not None and cfg.get("template"):
            try:
                text = (base / str(cfg["template"])).read_text(encoding="utf-8")
            except Exception:
                continue
        if not text:
            continue
        eff = _effective_tools(cfg, step, live_tools)
        bogus = sorted({n for n in _CALL_SHAPED.findall(text) if n not in eff})
        if bogus:
            real = [n for n in bogus if n in live_tools]
            fake = [n for n in bogus if n not in live_tools]
            detail = []
            if fake:
                detail.append(f"{fake} do not exist at all")
            if real:
                detail.append(f"{real} exist but are not granted to this role")
            out.append(
                f"role '{role}': its template tells the agent to call "
                f"{', '.join(detail)}. The agent follows the prompt over its toolset, so "
                f"it will produce nothing while the step still reports success. This "
                f"step's actual tools are: {sorted(eff)}.")
    return out


def _write_steps_without_validation(steps: list) -> list[str]:
    """A `mode: write` step that writes nothing must not pass silently.

    Observed: a maker emitted its files as prose, the lifecycle hook logged
    `0 file(s)`, and the step completed green — four rounds running, until the
    reviewer's bounded reject loop exhausted and the run died with no indication that
    the maker had produced nothing. A declared `validation` turns that into a
    validation failure, which retries the step in place WITH the reason attached.
    """
    out = []
    for s in steps:
        if not isinstance(s, dict) or s.get("step_type") != "agent":
            continue
        if ((s.get("output") or {}).get("mode")) != "write":
            continue
        if s.get("validation") or []:
            continue
        # Prefer naming the files the GRAPH already depends on. Suggesting the bare
        # `["*"]` escape hatch unconditionally is how a step ended up passing a
        # vacuous validation while never writing the verdict its own edges route on
        # — see `_routing_file_unguaranteed`. `["*"]` stays the answer only when
        # there is genuinely nothing specific to name.
        routed = sorted({t["match"]["from_file"]
                         for t in (s.get("transitions") or [])
                         if isinstance(t, dict) and isinstance(t.get("match"), dict)
                         and t["match"].get("from_file")})
        remedy = (f"      validation:\n"
                  f"        - files: {routed}\n"
                  f"          tool: file_exists"
                  if routed else
                  f"      validation:\n"
                  f"        - files: [\"*\"]\n"
                  f"          tool: file_exists")
        why = (f" Its own transitions route on {routed}, so name "
               f"{'that file' if len(routed) == 1 else 'those files'} — not `[\"*\"]`, "
               f"which any file at all satisfies."
               if routed else
               " Name the files you expect, or — when they are not known ahead of "
               "time — assert that SOMETHING was written.")
        out.append(
            f"step '{s.get('id')}': writes free-form files (`mode: write`) but "
            f"declares no `validation`, so a run in which it writes NOTHING still "
            f"completes successfully and hands an empty result downstream.{why}\n"
            f"{remedy}")
    return out


def _validation_is_a_spec_list(steps: list) -> list[str]:
    """`validation:` is a LIST of specs keyed by `tool:`, never a mapping.

    skillflow's ``StepValidator.validate(specs)`` iterates what it is given and
    calls ``spec.get(...)`` on each element. Hand it a mapping and iteration
    yields its string KEYS, so the step dies with
    ``'str' object has no attribute 'get'`` — AFTER the agent has produced its
    output, so the whole review re-runs on every retry until they are exhausted.

    All three forge gates passed `gen_dsh_code_review` with
    `validation: {type: file_exists, files: [...]}` on four steps, and the run
    burned two full LLM reviews before dying on it. `_write_steps_without_validation`
    could not catch it either: a mapping is truthy, so the step looked validated.
    The `type:`/`tool:` mix-up rides along — the key is `tool`.
    """
    out = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        v = s.get("validation")
        if v is None or isinstance(v, list):
            if isinstance(v, list):
                for spec in v:
                    if not isinstance(spec, dict):
                        out.append(
                            f"step '{s.get('id')}': `validation` entry {spec!r} is not "
                            f"a mapping. Each entry is `- files: [...]` + `tool: <name>`.")
                    elif not spec.get("tool"):
                        named = spec.get("type")
                        out.append(
                            f"step '{s.get('id')}': `validation` entry names no `tool`"
                            + (f" (it says `type: {named}` — the key is `tool`)."
                               if named else ".")
                            + "\n      validation:\n"
                            f"        - files: {spec.get('files', [])}\n"
                            f"          tool: {named or 'file_exists'}")
            continue
        files = v.get("files", []) if isinstance(v, dict) else []
        tool = (v.get("tool") or v.get("type") or "file_exists") if isinstance(v, dict) else "file_exists"
        out.append(
            f"step '{s.get('id')}': `validation` is a mapping. skillflow iterates it "
            f"and calls .get() on each element, so a mapping yields its string keys "
            f"and the step dies with \"'str' object has no attribute 'get'\" — after "
            f"the agent already ran. It is a LIST of specs:\n"
            f"      validation:\n"
            f"        - files: {files}\n"
            f"          tool: {tool}")
    return out


def _passthrough_target(start, by_id: dict, depth: int = 5):
    """Follow a chain of do-nothing `gate` hops to whatever it actually reaches.

    A `gate` executes nothing — it only routes. So a chain of gates joined by
    unconditional edges is, behaviourally, a direct edge to wherever it lands.
    """
    node_id, seen = start, set()
    while depth > 0 and node_id not in seen:
        seen.add(node_id)
        node = by_id.get(node_id)
        if not node or node.get("step_type") != "gate":
            return node_id
        edges = [t for t in (node.get("transitions") or [])
                 if isinstance(t, dict) and t.get("to") and not t.get("match")]
        if len(edges) != 1:
            return node_id
        node_id, depth = edges[0]["to"], depth - 1
    return node_id


def _edge_polarity(t: dict) -> str | None:
    """Does this transition assert SUCCESS, assert FAILURE, or catch everything?

    Judged from the match VALUES, never from the flag names or the edge's position.
    A tool signals success under whatever key it likes — `passed`, `applied`, `ok` —
    so a name list would go stale on the first tool that picks a new one, and
    position is not a contract at all: skillflow evaluates matches in order, and
    writing the failure edge first is the correct idiom for a tool that returns no
    `passed` on success.
    """
    m = t.get("match")
    if not isinstance(m, dict) or not m:
        return None                      # unconditional catch-all
    vals = list(m.values())
    if all(v is True for v in vals):
        return "success"
    if any(v is False for v in vals):
        return "failure"
    return None                          # matching on a non-boolean: undecidable


def _failure_rejoins_success(steps: list, by_id: dict, fallible: set) -> list[str]:
    """A failure branch that lands back on the success target is still fail-open.

    Observed in a generated graph: `spec_apply --{applied:true}--> scaffold_maker`
    plus `spec_apply --(unconditional)--> spec_apply_fallback [gate] --> scaffold_maker`.
    The failure IS routed, so the "needs a failure edge" rule is satisfied to the
    letter — and a `repo_apply` that never landed the code advances into the next
    phase exactly as if it had succeeded, which is the entire defect. Only a human
    caught it. Routing a failure into a gate that rejoins the success path is a
    no-op dressed as a branch.

    Which edge is which is decided by POLARITY. This used to take the first matched
    edge as the success edge and everything after it as failures. That is right for
    the ordering the ten generated graphs happen to use and inverts for the other
    one — write the failure edge first, which is the correct idiom for a tool with
    no `passed` on success, and the rule silently checks the wrong direction: a real
    fail-open goes unreported, and two failure edges sharing a target get reported
    as one "rejoining the SUCCESS target" that is in fact the fix step. Being right
    on today's inputs for a reason that is not the rule is how a gate rots.
    """
    out = []
    for s in steps:
        if not isinstance(s, dict) or s.get("step_type") != "tool":
            continue
        tname = str(s.get("tool_name") or "")
        if not _is_fallible(tname, fallible):
            continue
        trans = [t for t in (s.get("transitions") or [])
                 if isinstance(t, dict) and t.get("to")]
        if len(trans) < 2:
            continue                      # covered by _fallible_tools_unrouted
        succeeds = [t for t in trans if _edge_polarity(t) == "success"]
        fails = [t for t in trans if _edge_polarity(t) == "failure"]
        catch_all = [t for t in trans if _edge_polarity(t) is None]
        # Exactly one side stated explicitly → the catch-all is the other side.
        if succeeds and not fails:
            fails = catch_all
        elif fails and not succeeds:
            succeeds = catch_all
        if not succeeds or not fails:
            continue                      # nothing to compare, or undecidable
        success_targets = {_passthrough_target(t["to"], by_id) for t in succeeds}
        for t in fails:
            landing = _passthrough_target(t["to"], by_id)
            if landing in success_targets:
                out.append(
                    f"step '{s.get('id')}': the failure branch → '{t['to']}' rejoins the "
                    f"SUCCESS target '{landing}', so a failed '{tname}' advances exactly "
                    f"as if it had succeeded. A gate that forwards to the success step "
                    f"is not handling the failure. Send it somewhere that does: back to "
                    f"the maker with the error, or a terminal that ends the run failed.")
                break
    return out


# The only build backends that actually exist. `setuptools.backends._legacy:_Backend`
# — hallucinated, and pasted into a shipped role prompt — is the one that cost a lap
# on every run. Membership is checked, not the spelling of the module path, because a
# near-miss is exactly the failure mode.
_REAL_BUILD_BACKENDS = {
    "setuptools.build_meta", "setuptools.build_meta:__legacy__",
    "hatchling.build", "flit_core.buildapi", "poetry.core.masonry.api",
    "pdm.backend", "maturin", "scikit_build_core.build", "mesonpy",
}
_BUILD_BACKEND_RE = re.compile(r"""build-backend\s*=\s*["']([^"']+)["']""")


def _prompt_build_backend_is_real(role_table) -> list[str]:
    """A role prompt's pasted `pyproject.toml` must name a build backend that exists.

    Checked here rather than at runtime because the container has no setuptools and
    none of the generated project's dependencies, so nothing downstream can resolve
    an arbitrary symbol — the general "does this API exist" check would be blind. A
    build backend is different: it is a short, closed set of published constants, so
    membership is decidable anywhere.

    This is the one defect that recurred with perfect reliability: the emitter wrote
    the bad constant INTO the role prompt, so the maker was following instructions,
    and `run_tests` correctly named the failure on every single run while the wrong
    line sat in the artifact producing it again next time. A fix loop that converges
    every run and starts from scratch the next one is a fix the generator never made.
    """
    out = []
    for role, cfg in (role_table or {}).items():
        if not isinstance(cfg, dict):
            continue
        text = cfg.get("system_prompt") or cfg.get("template") or ""
        if not isinstance(text, str):
            continue
        for backend in set(_BUILD_BACKEND_RE.findall(text)):
            if backend not in _REAL_BUILD_BACKENDS:
                out.append(
                    f"role '{role}': the pyproject template in its prompt sets "
                    f"build-backend = '{backend}', which does not exist. Every run "
                    f"of this pipeline will fail `pip install -e .` with "
                    f"BackendUnavailable and spend a fix lap rediscovering it. Use "
                    f"'setuptools.build_meta'.")
    return out


def _duplicate_max_loop_edges(steps: list) -> list[str]:
    """Two `max_loop` edges on one (from, to) pair make the graph UN-RUNNABLE.

    `create_run` inserts one `skillflow_edge_counts` row per max_loop edge and that
    table is UNIQUE on (run_id, from_step, to_step), so the run dies with an
    IntegrityError before its first step. The YAML parses and `forge_lint` passes it
    clean; only the dry-run smoke catches it, as an opaque SQL error at boot. Our own
    configs get this from `tests/unit/test_config_graph_integrity.py` — a generated
    graph deserves the same rule, with a message that names the pair.
    """
    out = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        counts: dict = {}
        for t in (s.get("transitions") or []):
            if isinstance(t, dict) and t.get("to") and t.get("max_loop") is not None:
                counts[t["to"]] = counts.get(t["to"], 0) + 1
        for target, n in counts.items():
            if n > 1:
                out.append(
                    f"step '{s.get('id')}': {n} transitions to '{target}' both carry "
                    f"`max_loop` — skillflow keys its loop counter on (run, from, to), "
                    f"so this graph cannot even start (UNIQUE constraint on "
                    f"skillflow_edge_counts). Keep ONE bounded edge for this pair and "
                    f"distinguish the cases with `match`.")
    return out


def _guaranteed_files(step: dict) -> tuple[set, bool]:
    """Files this step is GUARANTEED to leave behind, and whether `*` was used.

    Two mechanisms count, and only these two: a `mode: content` fixed slot (the
    engine gives the agent a per-slot write tool and promotes the named file), and
    a `file_exists` validation naming the file (a miss becomes a retry-with-reason).
    A `mode: write` step with no validation guarantees nothing at all.
    """
    files, knowable = _declared_outputs(step)
    out = set(files) if knowable else set()
    star = False
    for v in (step.get("validation") or []):
        if not isinstance(v, dict) or v.get("tool") != "file_exists":
            continue
        for f in (v.get("files") or []):
            if str(f) == "*":
                star = True
            else:
                out.add(str(f))
    return out, star


def _routing_file_unguaranteed(steps: list) -> list[str]:
    """A step that ROUTES on a file must guarantee that file exists.

    The failure this exists for, from a live run: `final_answer` was `mode: write`
    with `validation: {file_exists, files: ["*"]}` and two edges, both matching on
    `final_verdict.json`. It wrote `final_answer.md` — which satisfies `["*"]` —
    never wrote the verdict, and the run died with "No matching transition from
    'final_answer' with flags {'wrote_files': True}". A complete proof, produced and
    then discarded, because the routing contract was never enforced.

    `["*"]` was the remediation this gate's OWN write-mode message suggested. It is
    the right escape hatch for a step whose filenames are not knowable — and exactly
    the wrong one for a step whose graph depends on a specific filename.

    Agent steps only: a gate step routes on a file some earlier step wrote, and a
    tool step's file is the tool's contract rather than the graph's. Measured across
    the 28 configs on this host: two hits, both the steps that actually broke.
    """
    out = []
    for s in steps:
        if not isinstance(s, dict) or s.get("step_type", "agent") != "agent":
            continue
        routed = {t["match"]["from_file"]
                  for t in (s.get("transitions") or [])
                  if isinstance(t, dict) and isinstance(t.get("match"), dict)
                  and t["match"].get("from_file")}
        if not routed:
            continue
        guaranteed, star = _guaranteed_files(s)
        missing = sorted(routed - guaranteed)
        if not missing:
            continue
        star_note = (' A `files: ["*"]` validation does not cover it — any file at '
                     'all satisfies that.' if star else "")
        out.append(
            f"step '{s.get('id')}': its transitions route on {missing}, but the step "
            f"does not guarantee {'that file' if len(missing) == 1 else 'those files'}."
            f"{star_note} A step that does not write what its own edges read matches "
            f"NO transition and kills the run with 'no matching transition' — after "
            f"doing all the work. Fix it one of two ways: declare the file as a "
            f"`mode: content` fixed slot, or name it explicitly in a validation, e.g. "
            f"`validation: [{{tool: file_exists, files: {missing}}}]`.")
    return out


def _fallible_tools_unrouted(steps: list, fallible: set) -> list[str]:
    """A tool that can fail must have its failure routed, not fall through."""
    out = []
    for s in steps:
        if not isinstance(s, dict) or s.get("step_type") != "tool":
            continue
        tname = str(s.get("tool_name") or "")
        if not _is_fallible(tname, fallible):
            continue
        trans = [t for t in s.get("transitions") or [] if isinstance(t, dict)]
        if trans and not any(t.get("match") for t in trans):
            out.append(
                f"step '{s.get('id')}': '{tname}' can fail, but its only transition is "
                f"unconditional — a failed run would advance as if it had succeeded. "
                f"Branch on its result, e.g. match: {{passed: true}} plus a failure edge.")
        elif len(trans) == 1 and trans[0].get("match"):
            # One CONDITIONAL edge is the other half of the same defect: the success
            # case is routed and the failure case matches nothing, so a failed check
            # kills the run with "no matching transition" instead of being handled.
            # (The dry-run smoke used to catch this by accident, because its stub
            # emitted flags no tool returns — it can't any more, so it is checked
            # here, where the message can actually say what to do.)
            out.append(
                f"step '{s.get('id')}': '{tname}' can fail and has exactly ONE "
                f"conditional transition — nothing routes its failure, so a failed "
                f"check ends the run with 'no matching transition'. Add the other "
                f"branch (a fix/give-up edge), or an unconditional fallback after it.")
    return out


def forge_registry_check(graph_path: str = "", role_table: str = "",
                         out_dir: str = "", **kwargs) -> dict:
    from aitelier.gate_report import write_gate_report
    graph = _load_yaml(graph_path)
    if graph is None:
        # failure_class is part of the contract on EVERY failing return: the graph
        # routes on it, and an unclassified failure would match no edge at all.
        err = f"graph not found or failed to parse: {graph_path}"
        write_gate_report(out_dir, "forge_registry_check", False, err)
        return {"passed": False, "failure_class": "emit_fixable", "notes": [],
                "error": err,
                "violations": [f"graph not found/parse-failed: {graph_path}"],
                "unknown_tools": [], "unknown_roles": []}

    roles: set[str] = set(_KNOWN_HOST_ROLES)
    rt = _load_yaml(role_table) if role_table else None
    role_note = ""
    if isinstance(rt, dict):
        # Share the registrar's normalizer: a table wrapped in `entries:` is
        # accepted here and there, and the note says so instead of reporting every
        # role as undefined (which reads as "you forgot them", not "you nested them").
        try:
            from core.pipeline_registry import normalize_role_table
            rt, role_note = normalize_role_table(rt)
        except Exception:
            pass
        roles |= set(rt.keys())

    live_tools = _live_tools()
    steps = graph.get("steps") or []
    step_ids = {s.get("id") for s in steps if isinstance(s, dict)}
    by_id = {s.get("id"): s for s in steps if isinstance(s, dict)}
    # Loop-body AGENT producers write per-item folders ({step}/{item}/); the
    # engine routes readers by position (same-loop → own item, outside → all).
    bodies = _loop_bodies(graph)

    violations: list[str] = []
    unknown_tools: list[str] = []
    unknown_roles: list[str] = []

    for s in steps:
        if not isinstance(s, dict):
            continue
        sid = s.get("id", "?")
        stype = s.get("step_type")

        if stype == "tool":
            tname = s.get("tool_name")
            if not tname:
                violations.append(f"step '{sid}': tool step with no tool_name")
            elif tname not in live_tools:
                unknown_tools.append(tname)
                violations.append(f"step '{sid}': tool_name '{tname}' not in live registry "
                                  f"(hallucinated or not-yet-built)")
            if tname in _COUNTER_SMELL or (tname and "counter" in tname.lower()):
                violations.append(f"step '{sid}': hand-rolled counter tool '{tname}' — "
                                  f"use a native max_loop edge instead")

        if stype == "agent":
            role = s.get("agent_config")
            if not role:
                violations.append(f"step '{sid}': agent step with no agent_config")
            elif role not in roles and rt is not None:
                unknown_roles.append(role)
                violations.append(f"step '{sid}': agent_config '{role}' not defined in role table")

        # Reviewer-is-an-agent convention: a step whose id names a review must be an agent.
        if "review" in str(sid).lower() and stype == "tool":
            violations.append(f"step '{sid}': looks like a reviewer but is a tool — a review "
                              f"must be an `agent` emitting review_verdict.json")

        # Reviewer-reads-its-maker: the #1 behavioral defect that passes lint/smoke
        # but fails at runtime. A reviewer loops back to its maker on `passed:false`;
        # if it doesn't READ that maker's output via {step:<maker>} context, it judges
        # blind, rejects every round, and the loop churns until max_loop kills the run.
        if stype == "agent" and "review" in str(sid).lower():
            maker = None
            for t in (s.get("transitions") or []):
                if not isinstance(t, dict):
                    continue
                m = t.get("match") or {}
                # reject edge: passed==false loops back to the maker
                if m.get("field") == "passed" and m.get("value") in (False, "false") and t.get("to"):
                    maker = t["to"]
                    break
            if maker and maker in step_ids:
                ctx_steps = {(c.get("source") or {}).get("step")
                             for c in (s.get("context") or []) if isinstance(c, dict)}
                if maker not in ctx_steps:
                    violations.append(
                        f"step '{sid}': reviewer loops back to maker '{maker}' on reject but "
                        f"does NOT read its output — add a context source {{step: {maker}}} or "
                        f"it judges blind (rejects every round → loop churns to failure)")

        # Context source references resolve.
        for c in (s.get("context") or []):
            src = c.get("source") if isinstance(c, dict) else None
            if not isinstance(src, dict):
                continue
            ref_step = src.get("step")
            if ref_step and ref_step not in step_ids:
                violations.append(f"step '{sid}': context references unknown step '{ref_step}'")
            # Scope sanity for loop fan-out reads. The engine (skillflow >=1.5.24)
            # routes by position — a same-loop reader gets its own item, any
            # outside reader gets ALL items — so a missing scope is safe. Flag
            # only real mistakes: an invalid value, or an explicit `scope: task`
            # written on an OUT-of-loop reader of an AGENT body producer (the
            # engine silently overrides it to all-items; the declaration lies).
            raw_scope = src.get("scope")
            if raw_scope not in (None, "task", "all"):
                violations.append(
                    f"step '{sid}': invalid scope '{raw_scope}' — must be 'task' "
                    f"or 'all' (registration will reject it)")
            elif ref_step and raw_scope == "task":
                producer = by_id.get(ref_step) or {}
                for lid, body in bodies.items():
                    if (ref_step in body and sid not in body and sid != lid
                            and producer.get("step_type") == "agent"):
                        violations.append(
                            f"step '{sid}': explicit `scope: task` on out-of-loop "
                            f"reader of loop-body producer '{ref_step}' — the engine "
                            f"reads ALL items there; use `scope: all` (or omit) so "
                            f"the graph says what actually happens.")
                        break
            ref_tool = src.get("tool")
            if ref_tool and ref_tool not in live_tools:
                unknown_tools.append(ref_tool)
                violations.append(f"step '{sid}': context tool '{ref_tool}' not in live registry")

    # Loop-external done gate: the node the completed end-condition names must be a
    # gate with no outgoing transitions (else success + give-up can share a terminal).
    ends = ((graph.get("end_conditions") or {}).get("conditions")) or []
    by_id = {s.get("id"): s for s in steps if isinstance(s, dict)}
    for cond in ends:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "node_reached" and cond.get("result") == "completed":
            term = cond.get("node")
            node = by_id.get(term)
            if node is None:
                violations.append(f"end-condition names unknown node '{term}'")
            else:
                trans = node.get("transitions") or []
                # The success terminal must be a loop-external `gate` whose only
                # transition is `to: null` (a real outgoing edge = not terminal;
                # a non-gate carrying the completed end-condition = fail-open false
                # green, e.g. gen_game_subagent's output_result tool).
                real_edges = [t for t in trans if isinstance(t, dict) and t.get("to")]
                if node.get("step_type") != "gate" or real_edges:
                    violations.append(
                        f"completed-terminal '{term}' is a "
                        f"{node.get('step_type')} with {len(real_edges)} outgoing edge(s) — "
                        f"the success terminal must be a loop-external `gate` whose only "
                        f"transition is `to: null` (fail-open false-green risk)")
                else:
                    violations.extend(_deliverable_before(term, steps, by_id))

    violations.extend(_role_tools_unknown(rt, live_tools))
    violations.extend(_role_model_known(rt))
    violations.extend(_capability_known(graph, steps))
    violations.extend(_template_names_absent_tools(rt, steps, role_table, live_tools))
    violations.extend(_validation_is_a_spec_list(steps))
    violations.extend(_write_steps_without_validation(steps))
    violations.extend(_routing_file_unguaranteed(steps))
    fallible = _fallible_names(live_tools)
    violations.extend(_fallible_tools_unrouted(steps, fallible))
    violations.extend(_failure_rejoins_success(steps, by_id, fallible))
    violations.extend(_duplicate_max_loop_edges(steps))
    violations.extend(_prompt_build_backend_is_real(rt if isinstance(rt, dict) else {}))
    violations.extend(_unreachable_terminals(steps, ends))

    passed = not violations
    # `error` carries the actionable detail back to the emitter: skillflow's
    # tool-gate loop-back injects ONLY tool_result["error"] into the maker's
    # feedback (core._inject_feedback_in_tx), so without it a re-emit is blind.
    prefix = ("Registry check failed — fix these before re-emitting:\n- "
              if not unknown_tools else
              "Registry check failed — the graph references tools that do not exist, "
              "so the tool plan needs revisiting:\n- ")
    error = "" if passed else prefix + "\n- ".join(violations)
    # The wrapper note must survive a PASS. It is not blocking — the table was
    # normalized and the graph is fine — but if it is only ever attached to an
    # error, an emitter that wraps its role table is never told, and the wrong
    # shape ships in the artifact for the next reader that does not normalize.
    notes = [role_note] if role_note else []
    if role_note and error:
        error = f"{error}\n(note: {role_note})"

    # Failure CLASS drives where the run goes back to. An unknown tool means the
    # plan is wrong and tools must be (re)built — that is worth a rewind. Everything
    # else is a defect in the emitted files themselves, repairable in place; sending
    # those back to the architect re-runs the whole planning chain and throws away a
    # graph that was already reviewed and accepted.
    failure_class = ("unknown_tool" if unknown_tools
                     else ("" if passed else "emit_fixable"))
    write_gate_report(out_dir, "forge_registry_check", passed, error)
    return {"passed": passed, "error": error, "violations": violations,
            "failure_class": failure_class, "notes": notes,
            "unknown_tools": sorted(set(unknown_tools)),
            "unknown_roles": sorted(set(unknown_roles))}
