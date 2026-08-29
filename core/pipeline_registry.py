"""Make converter-generated pipelines runnable in the same session.

`skill_converter` emits a pipeline YAML into its run workspace, but nothing turns
it into a *runnable* config: it is never persisted, never registered into the live
skillflow instance, and never added to the config registry (which is built once at
startup). So `generate_pipeline` could create a pipeline you couldn't then run.
This module bridges that gap.

Design (see the chat decision log):
  - **Namespaced `gen_<slug>`.** Generated configs cannot collide with core configs
    (`dpe_default_v2`, `meta_conversation`, `skill_converter`) — the keyspaces are
    disjoint by construction, so there is NO reserved-name blocklist.
  - **Persisted to `~/.AItelier/configs/`** (gitignored user data, *global* — not
    per-tenant), so they survive restart and auto-register on boot.
  - **Host agents auto-registered.** Generated graphs reference invented role names
    (e.g. `processor`) with no registered agent config; `register_graph` validates
    those refs and would reject the graph. We register each unknown role as a
    host-mode agent (`model: "host"` → `AITELIER_HOST_AGENT_MODEL`) first.
  - **Update is native.** `register_graph` overwrites by name and mints a content
    version WHEN THE CONTENT CHANGED (re-registering identical content is a
    no-op, which is what keeps a boot scan from inflating the number), and
    registry manifests read the live graph lazily, so re-generating the same name
    updates in place and `start_config_run` picks up the new version
    automatically. A run ALREADY in flight does not: it stays on the version it
    started with, and `repin_run` is how it adopts an edit.
"""

import logging
import os
from pathlib import Path

import yaml
from skillflow import PipelineGraph

GEN_PREFIX = "gen_"
# Generated graphs reference invented agent role names; we namespace them per-config
# (`<config>__<role>`) so they can never collide with a global agent (e.g. DPE's
# `researcher`). Seed input for a generated pipeline's first step is written here.
_ROLE_SEP = "__"
SEED_FILE = "seed_input.md"
# Host hints applied to every generated pipeline (keeps config_registry generic —
# it knows nothing about `gen_`): SCHEDULER-driven, and a seed file so
# `start_config_run(seed_text=...)` reaches the first step.
#
# This was `False` — "butler-driven so checkpoints relay in-chat" — which made the
# STARTER responsible for advancing the run. That is fine while the only starter is
# the chat butler, and broken the moment anything else starts one: a generated
# pipeline launched over the MCP endpoint had nobody advancing it and sat at
# `running` forever, truthfully and uselessly reported as "still running".
# Scheduler-owned makes a generated pipeline advance exactly like a DPE run
# whoever started it, and its checkpoints surface the way DPE's already do (SSE +
# the dashboard) instead of only inside one chat session.
#
# Anything that drives such a run itself must stop: the poller and an inline
# driver race for the same claim, and the inline one loses silently — see
# `core/meta_agent.py:_tool_drive_pipeline`, which now watches instead.
GEN_HINTS = {
    "scheduler_owned": True,
    "seed_file": SEED_FILE,
    # A generic input contract so a generated pipeline self-describes in the
    # butler's pipeline catalog (generated pipelines are layer-3 offload targets).
    # The graph's `description:` says WHAT it does; this says HOW to feed it.
    "input_hint": ("seed_text = the input for this pipeline's first step (the "
                   "topic / request / material it operates on), as plain text. "
                   "A generated multi-step pipeline — runs its own steps to a "
                   "result; relay any checkpoints it raises."),
}
_log = logging.getLogger(__name__)

# Tools that HARD-depend on the project's code repo existing as a git repo
# (they commit / validate / run against it). Read-type tools (read_file,
# list_tree) are deliberately NOT here — but no longer because a read is
# harmless. A run that declares `repo_mode: none` AND has no `repo_path` on its
# project row now gets no repo layer at all: `_existing_repo_code_path` answers
# False, skillflow attaches no `repo` source, the read tools cannot see one, and
# `from: repository` injects nothing. A read against a repo declared away that
# way is a real miss, not a lazy path that finds nothing.
#
# Not every `repo_mode: none` run, though. `_existing_repo_code_path` returns a
# recorded `repo_path` FIRST and only then consults `repo_type`, so the
# `against_project` shape — a repo-less config pointed at a real repository to
# read — still gets a full repo layer, which is the whole point of that shape.
# The miss above is the shape with no path recorded.
#
# They stay out of this set because of what this set is FOR. `derive_repo_mode`
# is asymmetric on purpose: any signal here means `code`, since a wrong `none`
# is a hard runtime failure while a wrong `code` only costs an unused repo. A
# read tool is the weakest possible signal — nearly every generated pipeline
# lists `read_file` whether or not it touches a repository — so admitting it
# would make the derivation answer `code` for almost everything and stop
# discriminating at all. The cost of that trade is the miss described above:
# a pipeline whose ONLY repo contact is a read is derived `none` and gets no
# repo. Do NOT change the behaviour here without re-deciding that trade.
_REPO_TOOLS = frozenset({
    "repo_apply", "draft_commit", "git_sync_pre", "repo_validate",
    "compose_validate", "pytest", "run_tests",
})


# Emitters reach for a wrapper key out of habit ("entries:", "roles:") and the
# resulting table looks empty to every consumer — which surfaces as "agent_config X
# not defined in role table" for every role, i.e. the exact opposite of the truth.
# Unwrap it in ONE place that both the registrar and forge_registry_check use, and
# only for keys that are unambiguously wrappers (never a lone real role name).
_ROLE_TABLE_WRAPPERS = ("entries", "roles", "role_table", "agents")


def normalize_role_table(data) -> tuple[dict, str]:
    """Return (roles, note). `note` is non-empty when a wrapper was unwrapped."""
    if not isinstance(data, dict) or not data:
        return {}, ""
    if len(data) == 1:
        key = next(iter(data))
        inner = data[key]
        if str(key).strip().lower() in _ROLE_TABLE_WRAPPERS and isinstance(inner, dict) \
                and all(isinstance(v, dict) for v in inner.values()) and inner:
            return inner, (f"role table was wrapped in a top-level '{key}:' key — "
                           f"roles belong at the top level; unwrapped {len(inner)} of them")
    return data, ""


def declared_output_files(step) -> tuple[list[str], bool]:
    """Files a step declares it writes, and whether that list is knowable.

    The single reader of a step's declared outputs, because getting it wrong is
    silent: a step that really does write the deliverable looks like it writes
    nothing, and callers then "helpfully" reject or mislabel a correct graph.

    Two shapes have to be tolerated, and BOTH appear in this repo's own configs:
      * a parsed StepNode flattens `output.{mode,fixed}` to `output_mode` /
        `output_fixed`; raw YAML keeps them nested under `output`;
      * a `fixed` entry is either the long form `{key: {file: "x.md", ...}}` or
        the shorthand `{key: "x.md"}` (pipeline_forge's own survey step uses the
        shorthand, its architect step mixes both).

    `knowable` is False when the step writes something we cannot enumerate
    (`mode: write`, or no `fixed` block) — callers must not conclude "writes
    nothing" from an empty list in that case.
    """
    out = getattr(step, "output", None)
    if not isinstance(out, dict):
        out = step.get("output") if isinstance(step, dict) else None
        out = out if isinstance(out, dict) else {}
    mode = getattr(step, "output_mode", None) or out.get("mode") or ""
    if mode == "write":
        return [], False
    fixed = getattr(step, "output_fixed", None)
    if not isinstance(fixed, dict):
        fixed = out.get("fixed")
    if not isinstance(fixed, dict):
        return [], False
    files = []
    for value in fixed.values():
        if isinstance(value, str) and value.strip():
            files.append(value.strip())            # shorthand: key: "file.md"
        elif isinstance(value, dict) and value.get("file"):
            files.append(str(value["file"]))       # long form: key: {file: ...}
    return files, True


def _specs(value) -> list[dict]:
    """The dict entries of a `validation:` / `context:` block, whatever its shape.

    These are LISTS of mappings in a hand-written config, but a generated graph is
    written by a model and one emitted `validation:` as a single MAPPING. Iterating
    that yields its KEYS — bare strings — and `spec.get("tool")` then raised
    `'str' object has no attribute 'get'`, which `register_forge_pipeline` caught
    as "emitted pipeline failed validation" AFTER it had already registered the
    graph. Live, run 74833837 (a forge-generated code-review pipeline): the graph
    row landed in skillflow, no files were written, and the result was a runnable
    config with no roles.json — every step silently on the generic host prompt.

    A single mapping is read as ONE spec rather than skipped, because dropping it
    would lose a repo signal, and this function is deliberately asymmetric: a
    wrong "none" is a hard runtime failure, a wrong "code" costs an unused repo.
    """
    if isinstance(value, dict):
        return [value]
    return [v for v in (value or []) if isinstance(v, dict)]


def derive_repo_mode(graph, roles: dict | None = None) -> str:
    """Does this generated graph need a code repo? ``"code"`` or ``"none"``.

    Derived from the graph itself rather than declared by the emitting agent —
    a generated pipeline has no say in its own workspace shape, and a derivation
    can't hallucinate. Deliberately ASYMMETRIC: any repo signal at all ⇒
    ``"code"``, because guessing "none" wrongly is a hard runtime failure
    (repo_apply against a nonexistent repo) while guessing "code" wrongly only
    costs an unused empty repo.
    """
    for step in getattr(graph, "steps", []) or []:
        if (getattr(step, "tool_name", "") or "") in _REPO_TOOLS:
            return "code"
        for spec in _specs(getattr(step, "validation", None)):
            if spec.get("tool") in _REPO_TOOLS:
                return "code"
        for spec in _specs(getattr(step, "context", None)):
            if spec.get("from") == "repository":
                return "code"
        # An agent step reaches the repo through its role's tool list. Look the role
        # up BOTH ways: register_forge_pipeline builds `roles` with namespaced keys
        # (`<config>__<role>`) while a role_table read straight off disk is bare, and
        # matching only one of them silently drops the whole role-tool signal.
        ac_full = getattr(step, "agent_config", "") or ""
        ac_bare = ac_full.split(_ROLE_SEP)[-1]
        role = (roles or {}).get(ac_full) or (roles or {}).get(ac_bare) or {}
        if _REPO_TOOLS & set(role.get("tools") or []):
            return "code"
    return "none"


def derive_output_step(graph) -> str | None:
    """The step that produces the run's result — what readers should show.

    Without this the manifest's output_step is None and every reader falls back to
    "the last step in the file", which for a well-formed graph is the terminal GATE
    (it writes nothing) or a give-up branch. A completed run then looks empty even
    though it produced exactly what was asked for.

    The result-producing step is the one feeding the completed terminal; among
    several, prefer one that writes something other than a review verdict.
    """
    steps = list(getattr(graph, "steps", []) or [])
    if not steps:
        return None
    by_id = {s.id: s for s in steps}

    # end_conditions is an EndConditions object on a parsed graph and a plain dict on
    # raw YAML — read both, or the lookup silently falls through to "last step in the
    # file", which is exactly the give-up branch we are trying not to point at.
    ec = getattr(graph, "end_conditions", None)
    conds = getattr(ec, "conditions", None)
    if conds is None:
        conds = (ec or {}).get("conditions", []) if isinstance(ec, dict) else []

    def _field(cond, key):
        return getattr(cond, key, None) if not isinstance(cond, dict) else cond.get(key)

    terminal = None
    for cond in conds or []:
        if _field(cond, "type") == "node_reached" and _field(cond, "result") == "completed":
            terminal = _field(cond, "node")
            break
    if terminal is None or terminal not in by_id:
        tail = [s for s in steps if getattr(s, "step_type", "") != "gate"]
        return tail[-1].id if tail else None

    preds = [s for s in steps
             if any((getattr(t, "to", None) or (t.get("to") if isinstance(t, dict) else None))
                    == terminal for t in (getattr(s, "transitions", None) or []))]
    if not preds:
        return None

    def _writes_a_result(step) -> bool:
        files, knowable = declared_output_files(step)
        if not knowable:                  # `mode: write` — writes something opaque
            return True
        return any(f != "review_verdict.json" for f in files)

    for step in preds:
        if _writes_a_result(step):
            return step.id
    return preds[0].id


def _humanize(config_name: str) -> str:
    """`gen_math_olympiad` → `Math Olympiad` — a catalog entry a human can scan."""
    stem = config_name[4:] if config_name.startswith("gen_") else config_name
    return stem.replace("_", " ").strip().title() or config_name


def _gen_hints(graph, roles: dict | None = None, config_name: str = "") -> dict:
    """GEN_HINTS plus everything derivable from this particular graph.

    Derived at REGISTRATION rather than emitted by the generator: an already-
    generated pipeline picks the metadata up on the next boot scan, and a derivation
    cannot hallucinate a step id the graph does not have.
    """
    mode = derive_repo_mode(graph, roles)
    if mode == "none":
        _log.info("generated pipeline %r has no repo signal → repo-less workspace",
                  getattr(graph, "name", "?"))
    hints = {**GEN_HINTS, "repo_mode": mode}
    out_step = derive_output_step(graph)
    if out_step:
        hints["output_step"] = out_step
    name = config_name or getattr(graph, "name", "") or ""
    if name:
        hints["label"] = _humanize(name)
    return hints


# ── Naming / storage ───────────────────────────────────────────────────────

def generated_configs_dir() -> Path:
    """Where persisted generated configs live (override via env for tests)."""
    d = os.getenv("AITELIER_GENERATED_CONFIGS_DIR")
    from core import datadir
    base = Path(d) if d else datadir.configs_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slug(text: str) -> str:
    from core.run_launcher import slugify
    return slugify(text, sep="_", maxlen=48, fallback="pipeline")


def config_name_for(name: str) -> str:
    """Deterministic config name from a human pipeline name → ``gen_<slug>``.

    Stable across re-generations of the same name, so 'update' overwrites in place.
    """
    return GEN_PREFIX + _slug(name)


def _role_prompt(role: str) -> str:
    return (
        f"You are the '{role}' step in an automated SkillFlow pipeline.\n"
        f"Your inputs are the outputs of the prior steps, provided as context.\n"
        f"Do the work the role name implies and write only the output artifact "
        f"required for this step. Be concise and precise."
    )


# ── Graph rewriting (namespacing + seeding) ─────────────────────────────────

def _namespace_agents(data: dict, config_name: str) -> None:
    """Rewrite every agent step's ``agent_config`` to ``<config_name>__<role>``.

    Generated graphs invent bare role names; left as-is they collide with global
    agents (e.g. DPE's ``researcher``) — the step would bind to that agent, or
    re-registering would clobber it. Namespacing makes both impossible. Idempotent:
    a role already prefixed with this config's namespace is left untouched.
    """
    prefix = config_name + _ROLE_SEP
    for step in data.get("steps", []):
        if not isinstance(step, dict):
            continue
        # Any step carrying an agent_config is an agent step (skillflow's
        # step_type defaults to "agent" when omitted), so key off agent_config
        # rather than step_type — else a step that omits step_type slips through
        # un-namespaced and re-introduces the collision.
        role = step.get("agent_config")
        if role and not str(role).startswith(prefix):
            step["agent_config"] = prefix + str(role)


def _rewrite_self_config_refs(data: dict, config_name: str) -> list[str]:
    """A rename must carry its OWN references. Returns the names it rewrote.

    The emitter writes self-referential context sources under the name it knows —
    the human pipeline name / slug (``{config: skill_packager, output: task.md}``).
    Registration renames the graph to ``gen_<slug>`` and used to leave those sources
    pointing at a config that will never exist: present in NINE of eleven registered
    pipelines. Benign only by luck — :func:`_inject_seed_context` inserts a correct
    source at position 0, so the dead one resolves to nothing and is skipped. It
    becomes a hard failure the moment an emitter marks it ``required: true``.

    Conservative on purpose: only a value that IS this graph's own pre-rename
    identity is rewritten. A source naming a genuinely different config (a pipeline
    that reads another pipeline's output) is left alone.
    """
    old = str(data.get("name") or "").strip()
    slug = config_name[len(GEN_PREFIX):] if config_name.startswith(GEN_PREFIX) else ""
    aliases = {n for n in (old, slug) if n and n != config_name}
    if not aliases:
        return []
    rewritten: list[str] = []
    for step in data.get("steps", []):
        if not isinstance(step, dict):
            continue
        for c in (step.get("context") or []):
            inner = c.get("source", c) if isinstance(c, dict) else None
            if isinstance(inner, dict) and inner.get("config") in aliases:
                rewritten.append(f"{step.get('id')}:{inner['config']}")
                inner["config"] = config_name
    return rewritten


def _dedupe_context_sources(data: dict) -> list[str]:
    """Drop context sources that became exact duplicates of an earlier one.

    Rewriting self-references collapses ``{config: skill_packager, output: task.md}``
    and ``{config: gen_skill_packager, output: task.md}`` onto the same source; two
    identical sources is not fatal but it feeds the agent the same file twice.
    """
    dropped: list[str] = []
    for step in data.get("steps", []):
        if not isinstance(step, dict) or not isinstance(step.get("context"), list):
            continue
        seen, kept = set(), []
        for c in step["context"]:
            key = repr(sorted((c.get("source") or c).items())) \
                if isinstance(c, dict) and isinstance(c.get("source", c), dict) else repr(c)
            if key in seen:
                dropped.append(str(step.get("id")))
                continue
            seen.add(key)
            kept.append(c)
        step["context"] = kept
    return dropped


def _inject_seed_context(data: dict, config_name: str) -> None:
    """Ensure the begin step reads the seed file (so start_config_run's seed_text
    actually reaches the generated pipeline). No-op if begin isn't an agent step or
    the seed source is already present."""
    begin = data.get("begin")
    for step in data.get("steps", []):
        if not isinstance(step, dict) or step.get("id") != begin:
            continue
        # step_type defaults to "agent" in skillflow when omitted; only a step
        # explicitly typed non-agent can't read context.
        if step.get("step_type", "agent") != "agent":
            return
        ctx = step.setdefault("context", [])
        for c in ctx:
            inner = c.get("source", c) if isinstance(c, dict) else {}
            if isinstance(inner, dict) and inner.get("config") == config_name \
                    and inner.get("output") == SEED_FILE:
                return  # already wired
        ctx.insert(0, {"source": {"config": config_name, "output": SEED_FILE}})
        return


# ── Registration ───────────────────────────────────────────────────────────

def ensure_host_agents(sf, graph) -> list[str]:
    """Register every agent role in *graph* not already known, as a host agent.

    Generated graphs invent descriptive role names with no agent config; without
    this, ``register_graph`` rejects the graph for unresolved agent_config refs and
    ``AgentFactory`` later can't build the agent. Roles are already namespaced
    (see :func:`_namespace_agents`), so this never touches a global agent. Returns
    newly added role names.
    """
    added: list[str] = []
    for node in graph.steps:
        role = getattr(node, "agent_config", "") or ""
        if role and role not in sf.agent_registry:
            sf.register_agent_config_from_dict(role, {
                "model": "host",
                "tools": ["read_file", "write"],
                "system_prompt": _role_prompt(role),
            })
            added.append(role)
    return added


def _register_text(sf, registry, config_name: str, yaml_text: str,
                   roles: dict | None = None):
    """Parse a (already-namespaced, already-seeded) generated pipeline YAML, force
    its name to *config_name*, register host agents + the graph live, and add a
    registry manifest with the generated-pipeline host hints. Raises on validation
    failure."""
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError("generated pipeline YAML is not a mapping")
    data["name"] = config_name
    graph = PipelineGraph._from_dict(data)
    # Derive the hints BEFORE registering. `_gen_hints` reads the graph's shape and
    # can raise on a malformed one (a generated `validation:` written as a mapping
    # did exactly that); deriving afterwards left the graph live with the caller
    # believing registration had failed — a config that runs, with no files and no
    # roles behind it. Nothing here mutates until everything that can fail has run.
    hints = _gen_hints(graph, roles, config_name)
    ensure_host_agents(sf, graph)
    sf.register_graph(graph)            # validates graph + agent_config refs
    registry.register_one(sf, config_name, hint_overrides=hints)
    return graph


def register_generated_pipeline(sf, registry, run_id: str, name: str) -> dict:
    """Persist + register the YAML produced by a completed skill_converter run.

    Rewrites the graph to be runnable (namespaced name + agent roles, seed wired)
    in ONE pass, then registers and persists that exact text so a boot re-scan is a
    no-op. Returns ``{config_name, path, action}`` on success, or ``{error}``.
    """
    from skillflow.plugins.skill_converter import get_output_file
    src = get_output_file(sf, run_id)
    if not src or not Path(src).exists():
        return {"error": "no generated pipeline YAML found for this run"}

    config_name = config_name_for(name)
    existed = registry.get(config_name) is not None

    try:
        data = yaml.safe_load(Path(src).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {"error": "generated pipeline YAML is not a mapping"}
        # Order matters: rewrite the graph's OWN references while `data["name"]`
        # still holds the pre-rename identity, then rename, then seed.
        _rewrite_self_config_refs(data, config_name)
        data["name"] = config_name
        _namespace_agents(data, config_name)
        _inject_seed_context(data, config_name)
        _dedupe_context_sources(data)
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError as e:
        return {"error": f"generated pipeline YAML is invalid: {e}"}

    try:
        _register_text(sf, registry, config_name, yaml_text)
    except Exception as e:
        return {"error": f"generated pipeline failed validation: {e}"}

    dest = generated_configs_dir() / f"{config_name}.yaml"
    dest.write_text(yaml_text, encoding="utf-8")
    # Persisting a config IS the intent to have it — a stale archive tombstone
    # would make it disappear at the next boot scan while the file sits on disk.
    _unarchive(config_name)
    return {"config_name": config_name, "path": str(dest),
            "action": "updated" if existed else "created"}


def _register_forge_roles(sf, config_name: str, roles: dict) -> None:
    """Register a forge-generated pipeline's roles with their REAL emitted prompts
    (namespaced), overriding the generic host-agent fallback. ``roles`` maps a
    namespaced role name → {system_prompt, tools, model, temperature, thinking}."""
    for role, cfg in (roles or {}).items():
        if not isinstance(cfg, dict):
            continue
        sf.register_agent_config_from_dict(role, {
            "model": cfg.get("model") or "host",
            "tools": cfg.get("tools") or ["read_file", "write"],
            "system_prompt": cfg.get("system_prompt") or _role_prompt(role),
            "temperature": cfg.get("temperature", 0.2),
            "thinking": cfg.get("thinking") or {"enable": True},
        })


def register_forge_pipeline(sf, registry, run_id: str, name: str) -> dict:
    """Persist + register the pipeline emitted by a completed ``pipeline_forge`` run.

    Unlike skill_converter (single YAML), pipeline_forge writes emit_graph/{
    pipeline.yaml, role_table.yaml, templates/<role>.md}. This wires the graph
    runnable (namespaced name + roles + seed) AND registers each role with its
    real emitted template as the system prompt, then persists both the graph and a
    companion ``<config>.roles.json`` so a boot re-scan restores the real prompts.
    """
    run = sf.get_run(run_id) or {}
    pid = run.get("project_id")
    if not pid:
        return {"error": "run has no project_id"}
    emit = sf._workspace.get_step_dir(pid, "pipeline_forge", "emit_graph")
    gpath = emit / "pipeline.yaml"
    if not gpath.exists():
        return {"error": f"no emitted pipeline.yaml at {emit}"}

    config_name = config_name_for(name)
    existed = registry.get(config_name) is not None
    try:
        data = yaml.safe_load(gpath.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {"error": "emitted pipeline.yaml is not a mapping"}
        # Order matters: rewrite the graph's OWN references while `data["name"]`
        # still holds the pre-rename identity, then rename, then seed.
        _rewrite_self_config_refs(data, config_name)
        data["name"] = config_name
        _namespace_agents(data, config_name)
        _inject_seed_context(data, config_name)
        _dedupe_context_sources(data)

        # Build namespaced roles from role_table.yaml + the emitted templates.
        prefix = config_name + _ROLE_SEP
        roles: dict = {}
        rt_path = emit / "role_table.yaml"
        rt = yaml.safe_load(rt_path.read_text(encoding="utf-8")) if rt_path.exists() else {}
        rt, wrap_note = normalize_role_table(rt)
        if wrap_note:
            _log.info("role table for %s: %s", config_name, wrap_note)
        for role, rcfg in (rt or {}).items():
            rcfg = rcfg if isinstance(rcfg, dict) else {}
            # Idempotent, exactly like _namespace_agents: in EDIT mode the emitter
            # echoes the baseline's already-namespaced role names, and blindly doing
            # `prefix + role` would double-prefix (`gen_x__gen_x__role`) — mismatching
            # the (idempotently single-prefixed) graph agent_config refs, so the real
            # prompt is silently dropped to the generic host fallback. Strip a leading
            # current-prefix first so both namespacing sites agree.
            bare = str(role)[len(prefix):] if str(role).startswith(prefix) else str(role)
            tmpl = rcfg.get("template") or f"templates/{bare}.md"
            tfile = emit / tmpl
            prompt = tfile.read_text(encoding="utf-8") if tfile.exists() else _role_prompt(bare)
            roles[prefix + bare] = {
                "model": "host",
                "tools": rcfg.get("tools") or ["read_file", "write"],
                "temperature": rcfg.get("temperature", 0.2),
                "thinking": rcfg.get("thinking") or {"enable": True},
                "system_prompt": prompt,
            }
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except Exception as e:
        return {"error": f"emitted pipeline is invalid: {e}"}

    # Parse and derive EVERYTHING that can fail before touching live state. The
    # order used to be roles → graph → register → hints, and a hint derivation that
    # raised (see `_specs`) left the graph registered with no files ever written:
    # a config that runs, has no `.roles.json`, and therefore drops every real
    # prompt for the generic host fallback. Deriving first makes the failure clean.
    try:
        graph = PipelineGraph._from_dict(yaml.safe_load(yaml_text))
        hints = _gen_hints(graph, roles, config_name)
    except Exception as e:
        return {"error": f"emitted pipeline failed validation: {e}"}
    try:
        _register_forge_roles(sf, config_name, roles)
        ensure_host_agents(sf, graph)   # any role not in role_table → generic fallback
        sf.register_graph(graph)
        registry.register_one(sf, config_name, hint_overrides=hints)
    except Exception as e:
        return {"error": f"emitted pipeline failed registration: {e}"}

    dest = generated_configs_dir() / f"{config_name}.yaml"
    dest.write_text(yaml_text, encoding="utf-8")
    # Persisting a config IS the intent to have it — a stale archive tombstone
    # would make it disappear at the next boot scan while the file sits on disk.
    _unarchive(config_name)
    (generated_configs_dir() / f"{config_name}.roles.json").write_text(
        _json_dumps(roles), encoding="utf-8")
    return {"config_name": config_name, "path": str(dest),
            "action": "updated" if existed else "created",
            "roles": sorted(roles.keys())}


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


def set_pipeline_capabilities(sf, registry, config_name: str,
                              add: list[str] | None = None,
                              remove: list[str] | None = None) -> dict:
    """Edit which capabilities a GENERATED pipeline offers.

    Writes where the pipeline lives — the persisted `gen_<slug>.yaml` — and
    reloads it, which is the path that already exists for editing a generated
    pipeline. A BUILT-IN config is refused: mutating a repo file at runtime is
    drift no checkout can see, and the supported way to add anything to a
    built-in base is the mechanism that already exists for it, an addon.

    This edits the OFFER LIST only. Retiring a definition is
    `capability_registry.archive`, and keeping the two apart is what makes "a
    capability a config still offers cannot be deleted" enforceable rather than
    circular.
    """
    import yaml
    from core import capability_registry as caps

    if not config_name.startswith(GEN_PREFIX):
        return {"error": f"'{config_name}' is a built-in pipeline; its offer "
                         f"list lives in the repo (configs/{config_name}.yaml). "
                         f"To give a built-in base a capability, compose it with "
                         f"an addon that offers one."}
    # The REQUEST is checked before the target: offering something nothing
    # registers means every card declaring it grants nothing, silently, and
    # naming the bad capability is more useful than naming a missing file.
    known = set(sf.capabilities())
    unknown = [c for c in (add or []) if c not in known]
    if unknown:
        return {"error": f"not registered on this deployment: {unknown}. "
                         f"Available: {sorted(known) or 'none'}."}

    f = generated_configs_dir() / f"{config_name}.yaml"
    if not f.exists():
        return {"error": f"no persisted config {config_name}.yaml"}
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:                               # noqa: BLE001
        return {"error": f"unreadable config {config_name}.yaml: {e}"}

    current = list(data.get("capabilities") or [])
    after = sorted((set(current) | set(add or [])) - set(remove or []))
    if after == sorted(current):
        return {"config_name": config_name, "capabilities": after,
                "changed": False}
    if after:
        data["capabilities"] = after
    else:
        data.pop("capabilities", None)
    # VALIDATE BEFORE WRITING. Write-then-reload leaves a rejected config on
    # disk: the live registry still holds the old graph (register_graph validates
    # before assigning), so nothing looks wrong — until the next restart, when
    # the boot scan skips the invalid file and the pipeline VANISHES from the
    # catalog. Reachable by the common case: adding an offer list to a graph that
    # already declares a static `capability:` makes that declaration illegal
    # unless it is in the list, which is exactly what the forge emits.
    try:
        from skillflow.graph import PipelineGraph
        issues = PipelineGraph._from_dict(dict(data)).validate()
    except Exception as e:                               # noqa: BLE001
        return {"error": f"the edited config would not parse: {e} — nothing written"}
    if issues:
        return {"error": f"the edited config would not register: {issues[0]} — "
                         f"nothing written. Fix the step's own `capability:` "
                         f"first, or add it to the same offer list."}

    import os as _os
    tmp = f.with_suffix(f".yaml.{_os.getpid()}.tmp")
    prev_text = f.read_text(encoding="utf-8")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    _os.replace(tmp, f)
    r = reload_generated_pipeline(sf, registry, config_name)
    if "error" in r:
        # Belt and braces: validation passed but registration did not. Put the
        # file back rather than leaving a pipeline that disappears on restart.
        f.write_text(prev_text, encoding="utf-8")
        return {"error": f"{r['error']} — the config file was restored"}
    warn = _still_declared_statically(data, set(remove or []))
    out = {"config_name": config_name, "capabilities": after, "changed": True}
    if warn:
        out["warning"] = warn
    return out


def _still_declared_statically(data: dict, removed: set) -> str:
    """Steps that still name a capability the offer list just dropped.

    `remove` has two silently different meanings. Dropping the LAST offered
    capability empties the list, and an empty list stops binding graph-declared
    names at all — so "remove X" on a graph whose step statically declares X
    RE-GRANTS it. Dropping one a task card declares revokes it correctly, but
    only as a claim-time warning: the step just quietly loses its tools. Either
    way the caller deserves to be told.
    """
    if not removed:
        return ""
    hits = []
    for st in (data.get("steps") or []):
        cap = st.get("capability") if isinstance(st, dict) else None
        names = ([cap] if isinstance(cap, str) else
                 [c for c in cap if isinstance(c, str)] if isinstance(cap, list)
                 else [])
        for n in names:
            if n in removed:
                hits.append(f"{st.get('id')}:{n}")
    if not hits:
        return ("removed from the offer list; any task card still declaring it "
                "now grants nothing (a claim-time warning, not a failure)")
    return (f"these steps still declare it in the GRAPH and keep it: "
            f"{hits} — with an empty offer list a graph-declared capability is "
            f"honoured unconditionally")


def reload_generated_pipeline(sf, registry, config_name: str) -> dict:
    """Re-read a persisted generated pipeline (``gen_<slug>.yaml`` + optional
    ``.roles.json``) from disk and re-register it live, picking up any manual edits
    the workflow agent made. Returns ``{config_name}`` or ``{error}``."""
    f = generated_configs_dir() / f"{config_name}.yaml"
    if not f.exists():
        return {"error": f"no persisted config {config_name}.yaml"}
    try:
        roles_file = f.with_suffix(".roles.json")
        roles = None
        if roles_file.exists():
            import json
            roles = json.loads(roles_file.read_text(encoding="utf-8"))
            _register_forge_roles(sf, config_name, roles)
        _register_text(sf, registry, config_name, f.read_text(encoding="utf-8"),
                       roles=roles)
        return {"config_name": config_name}
    except Exception as e:
        return {"error": f"reload failed: {e}"}


# Everything a generated pipeline owns on disk, so archive and un-archive cannot
# drift apart — the second tuple existed to delete exactly what the first moved,
# and a suffix added to one alone leaves `_archived/` lying about what is retired.
# `.baseline.json` is written by core.baseline (regression baselines).
PIPELINE_FILE_SUFFIXES = (".yaml", ".roles.json", ".baseline.json")


def archived_dir() -> Path:
    d = generated_configs_dir() / "_archived"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _exclusion_file() -> Path:
    return archived_dir() / "archived.json"


def archived_names() -> set[str]:
    """Generated configs the user has retired.

    Deleting the YAML alone leaves a ZOMBIE: `ConfigRegistry.build` enumerates
    `sf.list_graphs()`, which reads skillflow's own `skillflow_graphs` table, so a
    graph whose source file is gone is still listed and still runnable — while
    `config_read` / `reload_generated_pipeline` / `config_edit` all fail on it,
    because those go to the file. This list is what keeps the catalog honest.
    """
    import json
    f = _exclusion_file()
    if not f.exists():
        return set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return {str(n) for n in data} if isinstance(data, list) else set()
    except Exception as e:
        _log.warning("unreadable archived-pipeline list %s: %s", f, e)
        return set()


def _unarchive(config_name: str) -> bool:
    """Lift the archive tombstone for *config_name*. True if one was lifted.

    Writing a config back into the generated dir IS the intent to have it, so the
    tombstone must not outlive it. Without this, regenerating a pipeline that was
    previously archived produces a config that works for the rest of the session
    (it live-registers) and then silently VANISHES on the next restart: the file is
    on disk, the graph row exists, and both `load_generated_configs` and
    `ConfigRegistry.build` skip the name because the archive list still holds it.

    Observed exactly that way — three pipelines that had completed, registered and
    been used were absent from `/api/configs` after a restart, with their YAML
    sitting untouched in the configs dir.
    """
    import json
    names = archived_names()
    if config_name not in names:
        return False
    names.discard(config_name)
    try:
        _exclusion_file().write_text(json.dumps(sorted(names), indent=2),
                                     encoding="utf-8")
    except Exception as e:
        _log.warning("could not clear the archive entry for %s: %s", config_name, e)
        return False
    # Stale copies left by a non-purging archive would shadow nothing (the loader
    # reads the live dir) but they make the archive dir lie about what is retired.
    for suffix in PIPELINE_FILE_SUFFIXES:
        stale = archived_dir() / f"{config_name}{suffix}"
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    _log.info("un-archived %s (re-registered)", config_name)
    return True


def archive_generated_pipeline(sf, registry, config_name: str,
                               purge: bool = False) -> dict:
    """Retire a generated pipeline. Reversible unless ``purge``.

    Archive moves the source files aside and records the name, so every catalog
    build drops it. The `skillflow_graphs` row is deliberately KEPT: existing runs
    of this config resolve their graph through it, and deleting it would make their
    trace and step history unreadable. ``purge=True`` deletes that row too — a hard
    delete that reaches into skillflow's store, for when the graph must be gone.
    """
    import json
    if not config_name.startswith(GEN_PREFIX):
        return {"error": f"'{config_name}' is not a generated pipeline "
                         f"(only {GEN_PREFIX}* configs can be archived)"}

    moved: list[str] = []
    src = generated_configs_dir()
    for suffix in PIPELINE_FILE_SUFFIXES:
        f = src / f"{config_name}{suffix}"
        if f.exists():
            target = archived_dir() / f.name
            f.replace(target)
            moved.append(f.name)

    names = archived_names()
    names.add(config_name)
    try:
        _exclusion_file().write_text(
            json.dumps(sorted(names), indent=2), encoding="utf-8")
    except Exception as e:
        return {"error": f"could not record the archive: {e}"}

    # Drop it from the live process too, or it stays runnable until restart.
    try:
        registry._manifests.pop(config_name, None)
    except Exception:
        pass
    for attr in ("_graphs", "_resolvers"):
        try:
            getattr(sf, attr, {}).pop(config_name, None)
        except Exception:
            pass
    # `_pinned_resolvers` is keyed (name, version), so a name-keyed pop misses
    # it and an archived graph keeps answering from the pinned cache.
    try:
        pinned = getattr(sf, "_pinned_resolvers", None)
        if pinned is not None:
            for key in [k for k in pinned if k[0] == config_name]:
                pinned.pop(key, None)
    except Exception:
        pass

    purged = False
    if purge:
        try:
            with sf._lock:
                sf._conn.execute("DELETE FROM skillflow_graphs WHERE name = ?",
                                 (config_name,))
                # The CONTENT history too, or a hard delete leaves it behind and
                # the next pipeline registered under this name resumes numbering
                # from the dead one's last version — reported to the agent as
                # having "removed" every step of a pipeline that no longer
                # exists. `purge` means gone; a version history of nothing is
                # not history, it is a booby trap.
                sf._conn.execute(
                    "DELETE FROM skillflow_graph_versions WHERE name = ?",
                    (config_name,))
                sf._conn.commit()
            purged = True
        except Exception as e:
            return {"config_name": config_name, "archived": True, "moved": moved,
                    "purged": False,
                    "error": f"archived, but the skillflow_graphs row survived: {e}"}

    return {"config_name": config_name, "archived": True, "moved": moved,
            "purged": purged,
            "message": (f"'{config_name}' is out of the catalog. "
                        + ("Its graph row was deleted too — this cannot be undone."
                           if purged else
                           f"Files are in {archived_dir()}; restore them and remove "
                           f"the name from archived.json to bring it back."))}


def load_generated_configs(sf, registry) -> list[str]:
    """Boot-time: register every persisted ``gen_*.yaml``. Returns the names
    registered. Invalid files are skipped (logged), never fatal. A companion
    ``<name>.roles.json`` (forge-generated pipelines) restores the real role
    prompts before the graph registers."""
    out: list[str] = []
    skip = archived_names()
    for f in sorted(generated_configs_dir().glob(f"{GEN_PREFIX}*.yaml")):
        if f.stem in skip:
            continue    # a file restored by hand without clearing the archive list
        try:
            roles_file = f.with_suffix(".roles.json")
            roles = None
            if roles_file.exists():
                import json
                roles = json.loads(roles_file.read_text(encoding="utf-8"))
                _register_forge_roles(sf, f.stem, roles)
            _register_text(sf, registry, f.stem, f.read_text(encoding="utf-8"),
                           roles=roles)
            out.append(f.stem)
        except Exception as e:
            _log.warning("skipping invalid generated config %s: %s", f.name, e)
    return out
