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
  - **Update is native.** `register_graph` overwrites by name + version-bumps, and
    registry manifests read the live graph lazily, so re-generating the same name
    updates in place and `start_config_run` picks up the new version automatically.
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
# it knows nothing about `gen_`): butler-driven so checkpoints relay in-chat, and a
# seed file so `start_config_run(seed_text=...)` reaches the first step.
GEN_HINTS = {
    "scheduler_owned": False,
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
# list_tree) are deliberately NOT here: skillflow's get_project_code_path is
# lazy and happily returns a path that doesn't exist, so a read just finds
# nothing — only the git-touching tools actually break without a repo.
_REPO_TOOLS = frozenset({
    "repo_apply", "draft_commit", "git_sync_pre", "repo_validate",
    "compose_validate", "pytest", "run_tests",
})


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
        for spec in getattr(step, "validation", []) or []:
            if (spec or {}).get("tool") in _REPO_TOOLS:
                return "code"
        for spec in getattr(step, "context", []) or []:
            if (spec or {}).get("from") == "repository":
                return "code"
        # An agent step reaches the repo through its role's tool list. Graph
        # agent_configs are namespaced (`<config>__<role>`); role_table keys are bare.
        ac = (getattr(step, "agent_config", "") or "").split(_ROLE_SEP)[-1]
        role = (roles or {}).get(ac) or {}
        if _REPO_TOOLS & set(role.get("tools") or []):
            return "code"
    return "none"


def _gen_hints(graph, roles: dict | None = None) -> dict:
    """GEN_HINTS plus the repo_mode derived from this particular graph."""
    mode = derive_repo_mode(graph, roles)
    if mode == "none":
        _log.info("generated pipeline %r has no repo signal → repo-less workspace",
                  getattr(graph, "name", "?"))
    return {**GEN_HINTS, "repo_mode": mode}


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
    ensure_host_agents(sf, graph)
    sf.register_graph(graph)            # validates graph + agent_config refs
    registry.register_one(sf, config_name,
                          hint_overrides=_gen_hints(graph, roles))
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
        data["name"] = config_name
        _namespace_agents(data, config_name)
        _inject_seed_context(data, config_name)
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError as e:
        return {"error": f"generated pipeline YAML is invalid: {e}"}

    try:
        _register_text(sf, registry, config_name, yaml_text)
    except Exception as e:
        return {"error": f"generated pipeline failed validation: {e}"}

    dest = generated_configs_dir() / f"{config_name}.yaml"
    dest.write_text(yaml_text, encoding="utf-8")
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
        data["name"] = config_name
        _namespace_agents(data, config_name)
        _inject_seed_context(data, config_name)

        # Build namespaced roles from role_table.yaml + the emitted templates.
        prefix = config_name + _ROLE_SEP
        roles: dict = {}
        rt_path = emit / "role_table.yaml"
        rt = yaml.safe_load(rt_path.read_text(encoding="utf-8")) if rt_path.exists() else {}
        for role, rcfg in (rt or {}).items():
            rcfg = rcfg if isinstance(rcfg, dict) else {}
            tmpl = rcfg.get("template") or f"templates/{role}.md"
            tfile = emit / tmpl
            prompt = tfile.read_text(encoding="utf-8") if tfile.exists() else _role_prompt(role)
            roles[prefix + role] = {
                "model": "host",
                "tools": rcfg.get("tools") or ["read_file", "write"],
                "temperature": rcfg.get("temperature", 0.2),
                "thinking": rcfg.get("thinking") or {"enable": True},
                "system_prompt": prompt,
            }
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except Exception as e:
        return {"error": f"emitted pipeline is invalid: {e}"}

    try:
        _register_forge_roles(sf, config_name, roles)
        graph = PipelineGraph._from_dict(yaml.safe_load(yaml_text))
        ensure_host_agents(sf, graph)   # any role not in role_table → generic fallback
        sf.register_graph(graph)
        registry.register_one(sf, config_name,
                              hint_overrides=_gen_hints(graph, roles))
    except Exception as e:
        return {"error": f"emitted pipeline failed validation: {e}"}

    dest = generated_configs_dir() / f"{config_name}.yaml"
    dest.write_text(yaml_text, encoding="utf-8")
    (generated_configs_dir() / f"{config_name}.roles.json").write_text(
        _json_dumps(roles), encoding="utf-8")
    return {"config_name": config_name, "path": str(dest),
            "action": "updated" if existed else "created",
            "roles": sorted(roles.keys())}


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=2)


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


def load_generated_configs(sf, registry) -> list[str]:
    """Boot-time: register every persisted ``gen_*.yaml``. Returns the names
    registered. Invalid files are skipped (logged), never fatal. A companion
    ``<name>.roles.json`` (forge-generated pipelines) restores the real role
    prompts before the graph registers."""
    out: list[str] = []
    for f in sorted(generated_configs_dir().glob(f"{GEN_PREFIX}*.yaml")):
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
