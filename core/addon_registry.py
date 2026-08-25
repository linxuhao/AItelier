"""Pipeline addons — the HOST half of the addon story.

skillflow owns the overlay MECHANICS (``register_overlay`` / ``list_overlays`` /
``compose_config`` / ``describe_config`` — the graph half, next to its graph
registry). This module is the AItelier half:

  * **declare** addon YAML (``configs/addons/*.yaml``) to skillflow at boot,
  * layer the AItelier ``ConfigManifest`` onto composed base+addon combos
    (skillflow has no concept of label / has_task_loop / scheduler_owned),
  * serve prompt **fragments** and base **manifest hints** (host concerns).

An addon (``configs/addons/<name>.yaml``) is an overlay bound to a base::

    name: game_harness
    base: dpe_default_v2          # the base whose anchors it targets
    alias: dpe_game               # friendly name for base + THIS addon
    overlay: [ ... ops ... ]

Composition is a LIST: ``run(base, [addon, ...])``. The result name is emergent
— ``alias`` for the blessed single-addon combo, else ``<base>__<a>+<b>``.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_HINT_KEYS = {"label", "scheduler_owned", "has_task_loop", "seed_file",
              "output_step", "preamble_steps", "input_hint", "labels"}


def _configs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "configs"


def generated_addons_dir() -> Path:
    """Where addon_converter-generated overlays are persisted (gitignored user
    data under ~/.AItelier/configs/addons, boot-scanned). Mirrors
    core.pipeline_registry.generated_configs_dir for generated pipelines."""
    from core import datadir
    d = datadir.configs_dir() / "addons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def declare_addons(sf) -> list[str]:
    """Boot: register every ``configs/addons/*.yaml`` overlay spec with skillflow
    (``sf.register_overlay``). After this, skillflow is the single source of truth
    for overlays — the host reads them back via ``sf.list_overlays()``."""
    addons_dir = _configs_dir() / "addons"
    names: list[str] = []
    if not addons_dir.is_dir():
        return names
    for p in sorted(addons_dir.glob("*.yaml")):
        try:
            spec = _load_yaml(p)
            nm = spec.get("name", p.stem)
            sf.register_overlay(nm, spec)
            names.append(nm)
        except Exception as e:  # a malformed addon shouldn't sink the others
            log.warning("addon_registry: skipping unreadable addon %s: %s", p, e)
    return names


def list_addons() -> list[dict]:
    """Registered addons (delegates to skillflow's overlay registry), shaped for
    the discovery surface (``list_pipeline_addons``)."""
    from api.dependencies import get_skillflow
    return [{
        "name": o["name"], "base": o["base"], "alias": o["alias"],
        "description": o["description"], "when_to_use": o.get("whenToUse", ""),
    } for o in get_skillflow().list_overlays()]


def read_fragments(paths: list[str]) -> dict[str, str]:
    """Resolve addon prompt fragments (skillflow ``add_template`` stores their paths
    in a step's ``config.extra_templates``) to a {label: content} map, read relative
    to ``configs/addons/``. Merged into a step's resolved context by the runner, so
    the guidance reaches the prompt ONLY when its addon is applied. Host-side:
    skillflow does no prompt assembly."""
    base = _configs_dir() / "addons"
    out: dict[str, str] = {}
    for rel in paths or []:
        p = (base / rel).resolve()
        try:
            if p.is_file() and str(p).startswith(str(base.resolve())):
                out[f"Addon guidance ({rel})"] = p.read_text(encoding="utf-8")
            else:
                log.warning("addon fragment not found or outside addons dir: %s", rel)
        except OSError as e:
            log.warning("addon fragment unreadable %s: %s", rel, e)
    return out


def describe_config(config_name: str) -> dict:
    """Decompose a runnable config name into ``{base, addons}`` for display —
    delegates to skillflow's overlay-aware decomposition."""
    from api.dependencies import get_skillflow
    return get_skillflow().describe_config(config_name)


def _base_hints(base_name: str) -> dict:
    """AItelier manifest hints declared on a base config's ``x-aitelier`` block
    (label / has_task_loop / scheduler_owned / …). Read from the base YAML — these
    are host metadata skillflow neither stores nor needs."""
    for p in sorted(_configs_dir().glob("*.yaml")):
        try:
            d = _load_yaml(p)
        except Exception:
            continue
        if d.get("name") == base_name:
            return {k: v for k, v in (d.get("x-aitelier") or {}).items()
                    if k in _HINT_KEYS}
    return {}


def register_addon_combo(sf, registry, base_name: str, addon_names: list[str],
                         name: str | None = None) -> str:
    """Compose base + addons via skillflow and attach the AItelier manifest.

    skillflow composes + registers the graph (re-validating reachability / cycles
    / agent refs); the host then decorates it with a ``ConfigManifest`` seeded
    from the base's hints. Returns the composed config name."""
    cfg_name = sf.compose_config(base_name, addon_names, name=name)
    if registry is not None:
        registry.register_one(sf, cfg_name, hint_overrides=_base_hints(base_name))
    return cfg_name


def _overlay_dependency_warnings(sf, spec: dict) -> list[str]:
    """Runtime-dependency check for a (compose-valid) overlay: compose_validate
    proves the overlay COMPOSES into a valid graph, but not that the tools/
    fragments it references actually EXIST in this host. Warn (don't block) so a
    generated addon that names an unregistered tool or a missing fragment is
    surfaced instead of silently misbehaving at run time."""
    warns: list[str] = []
    try:
        known_tools = set(sf._tool_loader.list_tools())
    except Exception:
        known_tools = set()
    frag_base = _configs_dir() / "addons"
    for op in spec.get("overlay", []) or []:
        if not isinstance(op, dict):
            continue
        if "insert_after" in op:
            for st in op.get("steps", []) or []:
                tn = (st or {}).get("tool_name")
                if tn and known_tools and tn not in known_tools:
                    warns.append(
                        f"step '{(st or {}).get('id', '?')}' calls unknown tool "
                        f"'{tn}' — it composes but will fail at run time")
        if "add_template" in op:
            frag = op.get("fragment")
            if frag and not (frag_base / frag).is_file():
                warns.append(
                    f"add_template fragment '{frag}' does not exist — its guidance "
                    "will be silently skipped at run time")
    return warns


def register_addon_from_run(sf, registry, run_id: str) -> dict:
    """Bridge: persist + register the overlay a completed addon_converter run made.

    Reads the compose-validated ``overlay.yaml`` from the run workspace, declares
    it to skillflow (``register_overlay``), persists it to ``generated_addons_dir``
    (gitignored, boot-scanned), and — when the base graph is registered — composes
    its blessed alias combo so it is immediately runnable by name. Returns
    ``{addon_name, base, action, path, registered_config?}`` or ``{error}``.
    Parallel to core.pipeline_registry.register_generated_pipeline.
    """
    import yaml
    from skillflow.plugins.skill_converter import get_addon_output_file
    from core.run_launcher import slugify

    src = get_addon_output_file(sf, run_id)
    if not src or not Path(src).exists():
        return {"error": "no generated overlay YAML found for this run"}
    try:
        spec = yaml.safe_load(Path(src).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        return {"error": f"generated overlay YAML is invalid: {e}"}
    if not isinstance(spec, dict):
        return {"error": "generated overlay YAML is not a mapping"}
    name, base = spec.get("name"), spec.get("base")
    if not name or not base:
        return {"error": "overlay missing required 'name'/'base'"}

    # Sanitize the LLM-chosen name into a safe registry key / filename (stable, so
    # re-generating the same addon overwrites in place).
    name = slugify(name, sep="_", maxlen=48, fallback="addon")
    spec["name"] = name
    existed = (generated_addons_dir() / f"{name}.yaml").exists()

    sf.register_overlay(name, spec)          # mechanical half (never fails)
    result = {"addon_name": name, "base": base,
              "action": "updated" if existed else "created"}
    warns = _overlay_dependency_warnings(sf, spec)
    if warns:
        result["warnings"] = warns           # runtime deps that don't exist yet

    # Compose the blessed alias combo when the base is registered, so the addon is
    # runnable by name immediately (not just declared).
    if base in getattr(sf, "_graphs", {}):
        try:
            result["registered_config"] = register_addon_combo(
                sf, registry, base, [name], name=spec.get("alias") or None)
        except Exception as e:
            result["combo_error"] = str(e)
            log.warning("addon combo not registered for %s: %s", name, e)
    else:
        result["combo_error"] = f"base graph '{base}' not registered; combo not composed"

    dest = generated_addons_dir() / f"{name}.yaml"
    dest.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True),
                    encoding="utf-8")
    result["path"] = str(dest)
    return result


def load_generated_addons(sf, registry) -> list[str]:
    """Boot: declare + alias-register every persisted overlay in
    ``generated_addons_dir`` (~/.AItelier/configs/addons). Mirrors
    core.pipeline_registry.load_generated_configs. Non-fatal per file."""
    out: list[str] = []
    for p in sorted(generated_addons_dir().glob("*.yaml")):
        try:
            spec = _load_yaml(p)
            nm = spec.get("name", p.stem)
            sf.register_overlay(nm, spec)
            base, alias = spec.get("base"), spec.get("alias")
            if base and base in getattr(sf, "_graphs", {}):
                try:
                    register_addon_combo(sf, registry, base, [nm], name=alias or None)
                except Exception as e:
                    log.warning("generated addon combo '%s' not registered: %s", nm, e)
            out.append(nm)
        except Exception as e:
            log.warning("skipping invalid generated addon %s: %s", p, e)
    return out


def load_addon_aliases(sf, registry) -> list[str]:
    """Boot pass: declare addons to skillflow, then register each aliased single-
    addon combo (e.g. game_harness → ``dpe_game``) so the blessed base+addon combo
    is runnable by name. Ad-hoc combos use ``register_addon_combo`` at run time.
    Non-fatal."""
    declare_addons(sf)
    registered: list[str] = []
    for meta in sf.list_overlays():
        alias, base = meta.get("alias"), meta.get("base")
        if not (alias and base):
            continue
        try:
            registered.append(
                register_addon_combo(sf, registry, base, [meta["name"]], name=alias))
        except Exception as e:
            log.warning("addon alias '%s' not registered: %s", alias, e)
    return registered


def graph_view(config_name: str) -> dict | None:
    """The COMPOSED graph of a runnable config, projected for display.

    Reads ``sf._graphs`` rather than the YAML on disk because a base+addon combo
    (``dpe_game``) has no file of its own — it exists only as the composed
    product, and that product is exactly what a run executes. Rendering the base
    YAML instead would show a graph the user can never observe.

    ``addon_steps`` are the ids present here but NOT in the base graph, i.e. the
    steps the overlay SPLICED IN. It is deliberately not a full diff: an overlay
    can also rewire a base step's transitions, and that rewiring is already
    visible in the composed edges — it is only the *attribution* of a shared step
    that this cannot show. Returns None when the config is not registered.
    """
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    graph = getattr(sf, "_graphs", {}).get(config_name)
    if graph is None:
        return None
    d = graph.to_dict()
    # Loop membership, from skillflow's own reach-back computation rather than a
    # second guess at it here. Without it a fan-out renders as four ordinary
    # boxes and the picture claims each ran once, when the body actually runs
    # per item (see `loop_item` on the step rows).
    loop_of: dict[str, str] = {}
    try:
        resolver = sf._get_resolver(config_name)
        for loop_id, body in resolver.loop_bodies().items():
            for node_id in body:
                loop_of.setdefault(node_id, loop_id)
    except Exception:            # a graph with no loops, or an unloadable one
        loop_of = {}
    decomp = describe_config(config_name)
    base = decomp.get("base") or config_name
    addon_steps: list[str] = []
    if base != config_name:
        base_graph = getattr(sf, "_graphs", {}).get(base)
        if base_graph is not None:
            base_ids = {s.get("id") for s in base_graph.to_dict().get("steps") or []}
            addon_steps = [s.get("id") for s in d.get("steps") or []
                           if s.get("id") not in base_ids]
    steps = []
    for s in d.get("steps") or []:
        steps.append({
            "id": s.get("id"),
            "type": s.get("step_type"),
            "checkpoint": bool(s.get("checkpoint")),
            "tool_name": s.get("tool_name"),
            "agent_config": s.get("agent_config"),
            "loop_over": s.get("loop_over"),
            "loop_id": loop_of.get(s.get("id")) or None,
            "is_loop": s.get("step_type") == "loop",
            "from_addon": s.get("id") in addon_steps,
            # Only what an edge needs to be drawn and read: where it goes, why it
            # is taken, and whether it is a bounded retry loop.
            "transitions": [{"to": t.get("to"),
                             "match": t.get("match") or {},
                             "max_loop": t.get("max_loop")}
                            for t in (s.get("transitions") or [])],
        })
    return {"config_name": config_name, "base": base,
            "addons": decomp.get("addons") or [], "addon_steps": addon_steps,
            "begin": d.get("begin"), "description": d.get("description"),
            "end_conditions": d.get("end_conditions") or {}, "steps": steps,
            "loops": {lid: sorted(n for n, l in loop_of.items() if l == lid)
                      for lid in sorted(set(loop_of.values()))}}
