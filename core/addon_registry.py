"""Pipeline addons — base-bound overlay bundles.

An addon (configs/addons/<name>.yaml) is an overlay fragment that targets a
specific base pipeline's anchors, so it declares which base it binds to::

    name: game_harness
    base: dpe_default_v2          # the base whose anchors it targets
    alias: dpe_game               # optional: friendly name for base+THIS addon
    description: "Godot compile + play-test gate for game projects."
    overlay: [ ... ops ... ]

Because an addon is written against a base's anchor vocabulary it is base-
specific, not generic — the binding lives here, not in a separate recipe file.
Compatibility is enforced anyway by anchor resolution (skillflow.compose raises
if an addon references an anchor the base lacks); `base:` just powers discovery
(list_addons) and a friendly up-front check.

Composition is a LIST: `run(base, [addon, ...])`. The result name is emergent —
`alias` for the blessed single-addon combo, else `<base>__<a>+<b>` — because a
fixed per-addon name can't compose for multi-addon runs.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_HINT_KEYS = {"label", "scheduler_owned", "has_task_loop", "seed_file",
              "output_step", "preamble_steps", "input_hint", "labels"}


def _configs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "configs"


def _load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _bases_by_name() -> dict[str, dict]:
    """Map graph `name:` → parsed config dict for every base in configs/*.yaml."""
    out: dict[str, dict] = {}
    for p in sorted(_configs_dir().glob("*.yaml")):
        try:
            d = _load_yaml(p)
        except Exception as e:  # a malformed base shouldn't sink addon discovery
            log.warning("addon_registry: skipping unreadable config %s: %s", p, e)
            continue
        if isinstance(d, dict) and d.get("name"):
            out[d["name"]] = d
    return out


def list_addons() -> list[dict]:
    """Discover addons under configs/addons/. Returns self-describing metadata
    (name, base, alias, description) for a list/search surface."""
    addons_dir = _configs_dir() / "addons"
    if not addons_dir.is_dir():
        return []
    out = []
    for p in sorted(addons_dir.glob("*.yaml")):
        try:
            d = _load_yaml(p)
        except Exception as e:
            log.warning("addon_registry: skipping unreadable addon %s: %s", p, e)
            continue
        out.append({
            "name": d.get("name", p.stem),
            "base": d.get("base", ""),
            "alias": d.get("alias", ""),
            "description": d.get("description", ""),
            "when_to_use": d.get("whenToUse", ""),
        })
    return out


def read_fragments(paths: list[str]) -> dict[str, str]:
    """Resolve addon prompt fragments (skillflow add_template stores their paths in
    a step's config.extra_templates) to a {label: content} map, read relative to
    configs/addons/. Merged into a step's resolved context by the runner, so the
    guidance reaches the prompt ONLY when the addon that added it is applied."""
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
    """Decompose a runnable config name into its base + composing addons, for
    display. An addon alias (``dpe_game``) → its base + [addon]; an emergent name
    (``base__a+b``) → parsed; a plain base → no addons."""
    for a in list_addons():
        if a.get("alias") and a["alias"] == config_name:
            return {"base": a.get("base", ""), "addons": [a["name"]]}
    if "__" in config_name:
        base, _, rest = config_name.partition("__")
        return {"base": base, "addons": [x for x in rest.split("+") if x]}
    return {"base": config_name, "addons": []}


def _addon_by_name(name: str) -> tuple[dict, dict]:
    """Return (addon_dict, base_dict) for an addon name; raise on unknown/mismatch."""
    path = _configs_dir() / "addons" / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown addon '{name}' (configs/addons/{name}.yaml missing)")
    addon = _load_yaml(path)
    bases = _bases_by_name()
    base_name = addon.get("base", "")
    if base_name and base_name not in bases:
        raise ValueError(f"addon '{name}' targets base '{base_name}' which is not registered")
    return addon, bases.get(base_name, {})


def compose_addon_graph(base_name: str, addon_names: list[str], result_name: str | None = None):
    """Compose base + addons into a named PipelineGraph. Validates that every
    addon's declared `base:` matches. Returns (name, PipelineGraph, base_hints)."""
    from skillflow import PipelineGraph
    from skillflow.compose import compose_graph

    bases = _bases_by_name()
    if base_name not in bases:
        raise ValueError(f"unknown base pipeline '{base_name}'")
    base = bases[base_name]

    overlays = []
    for a in addon_names:
        addon, _ = _addon_by_name(a)
        declared = addon.get("base", "")
        if declared and declared != base_name:
            raise ValueError(
                f"addon '{a}' binds to base '{declared}', not '{base_name}'")
        overlays.append(addon)

    name = result_name or f"{base_name}__{'+'.join(sorted(addon_names))}"
    merged = compose_graph(base, overlays)
    merged["name"] = name
    graph = PipelineGraph._from_dict(merged)
    base_hints = {k: v for k, v in (base.get("x-aitelier") or {}).items() if k in _HINT_KEYS}
    return name, graph, base_hints


def register_addon_combo(sf, registry, base_name: str, addon_names: list[str],
                         name: str | None = None) -> str:
    """Compose + register base+addons as a runnable named config (dynamic path,
    mirrors the gen_* runtime-registration). Returns the config name."""
    cfg_name, graph, base_hints = compose_addon_graph(base_name, addon_names, name)
    sf.register_graph(graph)  # re-validates reachability / cycles / agent refs
    if registry is not None:
        registry.register_one(sf, cfg_name, hint_overrides=base_hints)
    return cfg_name


def load_addon_aliases(sf, registry) -> list[str]:
    """Boot pass: register `base + THIS addon` under each addon's `alias`, so the
    blessed single-addon combos (e.g. dpe_game) are runnable by name. Non-fatal."""
    registered = []
    for meta in list_addons():
        alias, base = meta.get("alias"), meta.get("base")
        if not (alias and base):
            continue
        try:
            registered.append(register_addon_combo(sf, registry, base, [meta["name"]], name=alias))
        except Exception as e:
            log.warning("addon alias '%s' not registered: %s", alias, e)
    return registered
