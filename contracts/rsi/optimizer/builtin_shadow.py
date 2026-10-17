"""Builtin pilot export to a private generated shadow, and its equivalence control.

Approved design §1: the builtin ``investigate`` is exported into a PRIVATE
generated shadow that preserves behaviour (template resolved into embedded role
text); both arms then go through the same native conversion, and the reference
shadow's semantic equivalence is a prerequisite proved before evaluation.

``register_builtin`` performs the host's own boot registration for one builtin
(``register_agent_config_from_dict`` per role of its agent_configs file, then
``register_graph`` of its configs file) from the exact Source tree.
``export_shadow`` freezes the served builtin through the accepted native
``capture`` (registered graph, served roles, actual template bytes, served tool
identities) and writes it as an ordinary generated target, rewriting only what
the explicit ``mapping`` records: the graph and its self-seed reference name,
generated role namespacing, template -> inline prompt embedding, and an end
condition proved unreachable for this graph. Model names stay as declared
(``flash`` is resolved by the frozen route table, never replaced); every
setting, tool, context source, output and limit is copied.

``equivalence`` is the observable control: the builtin's and the shadow's
logical contents (accepted versioning projection, which removes exactly the
physical naming and embeds prompt bytes) must be equal after the recorded
mapping, their served tool identities equal, and the effective gateway profile
each role would actually be served with (internal/concrete model, effort,
temperature, output and turn limits) equal. Any other difference fails it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

_SOURCES = ("configs/{name}.yaml", "agent_configs/{name}.yaml")


class ExportRefused(ValueError):
    """The builtin cannot be exported without changing its behaviour."""


def builtin_paths(source_root: Path, name: str) -> dict:
    root = Path(source_root)
    paths = {rel.format(name=name): root / rel.format(name=name) for rel in _SOURCES}
    roles = yaml.safe_load(paths[f"agent_configs/{name}.yaml"].read_text(encoding="utf-8"))
    for cfg in roles.values():
        if isinstance(cfg, dict) and cfg.get("template"):
            paths[f"templates/{cfg['template']}"] = root / "templates" / cfg["template"]
    return paths


def register_builtin(sf, source_root: Path, name: str) -> None:
    """The host's boot registration of ONE builtin config from exact Source."""
    from skillflow.graph import PipelineGraph
    root = Path(source_root)
    roles = yaml.safe_load((root / "agent_configs" / f"{name}.yaml").read_text(encoding="utf-8"))
    for role, cfg in roles.items():
        if isinstance(cfg, dict):
            sf.register_agent_config_from_dict(role, cfg)
    sf.register_graph(PipelineGraph.from_yaml(root / "configs" / f"{name}.yaml"))


def capture_builtin(sf, registry, source_root: Path, name: str) -> dict:
    from core import pipeline_frozen_bundle as fb
    root = Path(source_root)
    return fb.capture(sf, registry, name, template_base=root / "templates",
                      configs_dirs=[root / "configs"])


def _redundant_end_conditions(graph: dict) -> list:
    """End conditions that can never decide a run of THIS graph, with the proof.

    ``max_total_steps`` counts the run's step rows in completed/failed status
    (SkillFlow ``_evaluate_end_conditions``). ``create_run`` inserts one row per
    node; retries and stale recovery reopen that same row; a new instance is
    opened only by re-entering a node through a transition. A single-step graph
    whose only transitions end the run therefore holds exactly one row, so a
    limit above 1 is unreachable and the remaining ``node_reached`` condition
    alone decides the run."""
    ec = graph.get("end_conditions") or {}
    conds = ec.get("conditions") or []
    limits = [c for c in conds if c.get("type") == "max_total_steps"]
    if not limits:
        return []
    steps = graph.get("steps") or []
    terminal_only = len(steps) == 1 and all(
        isinstance(t, dict) and t.get("to") is None for t in steps[0].get("transitions") or [])
    reached = any(c.get("type") == "node_reached" and c.get("node") == steps[0].get("id")
                  for c in conds) if steps else False
    bound = 1
    if not (ec.get("combinator") == "or" and terminal_only and reached
            and all(isinstance(c.get("limit"), int) and c["limit"] > bound for c in limits)):
        raise ExportRefused(f"end conditions {limits} are not provably redundant for this graph")
    return [{"condition": c, "proof": "single step, terminal transitions only: at most "
             f"{bound} step row per run (< limit {c['limit']}); node_reached decides"} for c in limits]


def export_shadow(sf, registry, config_dir: Path, *, source_root: Path, builtin: str,
                  shadow: str) -> dict:
    """Write and register the private generated shadow; return the mapping."""
    from core import pipeline_registry as pr
    bundle = capture_builtin(sf, registry, source_root, builtin)
    raw = yaml.safe_load((Path(source_root) / "configs" / f"{builtin}.yaml").read_text(encoding="utf-8"))
    graph = copy.deepcopy(bundle["graph"])
    dropped = _redundant_end_conditions(graph)
    if dropped:
        graph["end_conditions"]["conditions"] = [
            c for c in graph["end_conditions"]["conditions"] if c.get("type") != "max_total_steps"]
    graph["name"] = shadow
    self_refs = []
    for step in graph.get("steps") or []:
        for ctx in step.get("context") or []:
            src = ctx.get("source") if isinstance(ctx, dict) else None
            if isinstance(src, dict) and src.get("config") == builtin:
                src["config"] = shadow
                self_refs.append({"step": step.get("id"), "from": builtin, "to": shadow})
    roles, role_map, prompt_map = {}, {}, {}
    for step in graph.get("steps") or []:
        role = step.get("agent_config")
        if not role:
            continue
        physical = f"{shadow}{pr._ROLE_SEP}{role}"
        step["agent_config"] = physical
        if role in role_map:
            continue
        frozen = bundle["roles"][role]
        entry = {**copy.deepcopy(frozen["config"]), "model": frozen["model"],
                 "tools": list(frozen["tools"])}
        if frozen["prompt"] is not None:
            entry["system_prompt"] = bundle["prompts"][frozen["prompt"]]
            prompt_map[frozen["prompt"]] = f"inline:{physical}"
        roles[physical], role_map[role] = entry, physical
    graph["x-aitelier"] = copy.deepcopy(raw.get("x-aitelier") or {})
    gen = Path(config_dir)
    (gen / f"{shadow}.yaml").write_text(yaml.safe_dump(graph, sort_keys=False, allow_unicode=True),
                                        encoding="utf-8")
    (gen / f"{shadow}.roles.json").write_text(json.dumps(roles, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    out = pr.reload_generated_pipeline(sf, registry, shadow)
    if "error" in out:
        raise ExportRefused(f"shadow registration refused: {out['error']}")
    return {"schema": "rsi.optimizer.builtin_shadow.v1", "builtin": builtin, "shadow": shadow,
            "builtin_bundle_identity": bundle["identity"], "graph_name": {builtin: shadow},
            "self_seed_references": self_refs, "roles": role_map, "prompts": prompt_map,
            "dropped_redundant_end_conditions": dropped,
            "x_aitelier": "copied from the builtin config (inert host hints for a generated config)"}


def _gateway(evaluator, role: str) -> dict:
    try:
        gateway, cfg = evaluator._gateway_profile(role)
    except Exception as exc:                                        # noqa: BLE001
        return {"refused": f"{type(exc).__name__}: {exc}"}
    return {"internal_model": gateway.internal_model, "active_model": gateway.active_model,
            "effort": gateway._binding_effort() if gateway.enable_thinking else "disabled",
            "temperature": cfg.get("temperature", 0.2),
            "max_output_tokens": int(gateway.max_output_tokens),
            "max_tool_turns": int(cfg.get("max_tool_turns", 10)),
            "native_tool_calling": cfg.get("native_tool_calling")}


def equivalence(sf, registry, evaluator, config_dir: Path, *, source_root: Path, builtin: str,
                shadow: str, mapping: dict | None) -> dict:
    """Observable control: is ``shadow`` the builtin under the recorded mapping?"""
    from contracts.rsi.pipeline_versioning.native_provider import _captured_role, project, project_capture
    from core import pipeline_frozen_bundle as fb
    b = capture_builtin(sf, registry, source_root, builtin)
    s = fb.capture(sf, registry, shadow, configs_dirs=[Path(config_dir)])
    graph = copy.deepcopy(b["graph"])
    dropped = [d["condition"] for d in (mapping or {}).get("dropped_redundant_end_conditions") or ()]
    if dropped:
        if [d["condition"] for d in _redundant_end_conditions(graph)] != dropped:
            raise ExportRefused("recorded end-condition mapping is not the proved one")
        graph["end_conditions"]["conditions"] = [
            c for c in graph["end_conditions"]["conditions"] if c not in dropped]
    expected, expected_vid = project(b["logical_name"], graph, _captured_role(b), b["tools"])
    actual, actual_vid = project_capture(s)
    differences = []
    for key in ("graph", "roles", "tools"):
        if expected[key] != actual[key]:
            differences.append({"field": key, "builtin": expected[key], "shadow": actual[key]})
    if b["capabilities"] != s["capabilities"]:
        differences.append({"field": "capabilities", "builtin": b["capabilities"], "shadow": s["capabilities"]})
    profiles = {}
    for logical in expected["roles"]:
        physical = next(r for _, r in fb.agent_refs(s["graph"]))
        profiles[logical] = {"builtin": _gateway(evaluator, logical), "shadow": _gateway(evaluator, physical)}
        if profiles[logical]["builtin"] != profiles[logical]["shadow"] or "refused" in profiles[logical]["builtin"]:
            differences.append({"field": f"effective_gateway:{logical}", **profiles[logical]})
    return {"builtin": builtin, "shadow": shadow, "equivalent": not differences,
            "expected_version": expected_vid, "shadow_version": actual_vid,
            "builtin_bundle_identity": b["identity"], "mapping": mapping,
            "effective_profiles": profiles, "differences": differences}
