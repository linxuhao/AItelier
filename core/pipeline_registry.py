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
import re
from pathlib import Path

import yaml
from skillflow import PipelineGraph

GEN_PREFIX = "gen_"
_log = logging.getLogger(__name__)


# ── Naming / storage ───────────────────────────────────────────────────────

def generated_configs_dir() -> Path:
    """Where persisted generated configs live (override via env for tests)."""
    d = os.getenv("AITELIER_GENERATED_CONFIGS_DIR")
    base = Path(d) if d else (Path.home() / ".AItelier" / "configs")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:48]
    s = re.sub(r"_+", "_", s)
    return s or "pipeline"


def config_name_for(name: str) -> str:
    """Deterministic config name from a human pipeline name → ``gen_<slug>``.

    Stable across re-generations of the same name, so 'update' overwrites in place.
    """
    return GEN_PREFIX + _slug(name)


def _role_prompt(role: str, step_id: str) -> str:
    return (
        f"You are the '{role}' step in an automated SkillFlow pipeline.\n"
        f"Your inputs are the outputs of the prior steps, provided as context.\n"
        f"Do the work the role name implies and write only the output artifact "
        f"required for this step. Be concise and precise."
    )


# ── Registration ───────────────────────────────────────────────────────────

def ensure_host_agents(sf, graph) -> list[str]:
    """Register every agent role in *graph* not already known, as a host agent.

    Generated graphs invent descriptive role names with no agent config; without
    this, ``register_graph`` rejects the graph for unresolved agent_config refs and
    ``AgentFactory`` later can't build the agent. Returns newly added role names.
    """
    added: list[str] = []
    for node in graph.steps:
        role = getattr(node, "agent_config", "") or ""
        if role and role not in sf.agent_registry:
            sf.register_agent_config_from_dict(role, {
                "model": "host",
                "tools": ["read_file", "write"],
                "system_prompt": _role_prompt(role, node.id),
            })
            added.append(role)
    return added


def _register_text(sf, registry, config_name: str, yaml_text: str):
    """Parse *yaml_text*, force its name to *config_name*, register host agents +
    the graph live, and add a registry manifest. Raises on validation failure."""
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError("generated pipeline YAML is not a mapping")
    data["name"] = config_name
    graph = PipelineGraph._from_dict(data)
    ensure_host_agents(sf, graph)
    sf.register_graph(graph)            # validates graph + agent_config refs
    registry.register_one(sf, config_name)
    return graph


def register_generated_pipeline(sf, registry, run_id: str, name: str) -> dict:
    """Persist + register the YAML produced by a completed skill_converter run.

    Returns ``{config_name, path, action}`` on success, or ``{error}``.
    """
    from skillflow.plugins.skill_converter import get_output_file
    src = get_output_file(sf, run_id)
    if not src or not Path(src).exists():
        return {"error": "no generated pipeline YAML found for this run"}

    config_name = config_name_for(name)
    existed = registry.get(config_name) is not None

    # Normalize the persisted name so the file and the registered graph agree
    # (and a boot-time re-scan registers under the same name).
    raw = Path(src).read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
        data["name"] = config_name
        yaml_text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError:
        yaml_text = raw

    try:
        _register_text(sf, registry, config_name, yaml_text)
    except Exception as e:
        return {"error": f"generated pipeline failed validation: {e}"}

    dest = generated_configs_dir() / f"{config_name}.yaml"
    dest.write_text(yaml_text, encoding="utf-8")
    return {"config_name": config_name, "path": str(dest),
            "action": "updated" if existed else "created"}


def load_generated_configs(sf, registry) -> list[str]:
    """Boot-time: register every persisted ``gen_*.yaml``. Returns the names
    registered. Invalid files are skipped (logged), never fatal."""
    out: list[str] = []
    for f in sorted(generated_configs_dir().glob(f"{GEN_PREFIX}*.yaml")):
        try:
            _register_text(sf, registry, f.stem, f.read_text(encoding="utf-8"))
            out.append(f.stem)
        except Exception as e:
            _log.warning("skipping invalid generated config %s: %s", f.name, e)
    return out
