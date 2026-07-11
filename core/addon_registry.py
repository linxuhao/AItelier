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
