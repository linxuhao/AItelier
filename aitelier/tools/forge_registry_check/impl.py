"""forge_registry_check — validate a generated graph against the LIVE registry.

Gate (b): every tool_name / agent_config / context source in the emitted graph
must resolve to a real primitive (the tools were built + registered upstream), and
the graph must obey the AItelier conventions a structural linter can't see. This is
what catches the `gen_game_subagent.yaml` class of defect (7 hallucinated tools).
"""
from __future__ import annotations

from pathlib import Path

import yaml

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


def forge_registry_check(graph_path: str = "", role_table: str = "", **kwargs) -> dict:
    graph = _load_yaml(graph_path)
    if graph is None:
        return {"passed": False, "violations": [f"graph not found/parse-failed: {graph_path}"],
                "unknown_tools": [], "unknown_roles": []}

    roles: set[str] = set(_KNOWN_HOST_ROLES)
    rt = _load_yaml(role_table) if role_table else None
    if isinstance(rt, dict):
        roles |= set(rt.keys())

    live_tools = _live_tools()
    steps = graph.get("steps") or []
    step_ids = {s.get("id") for s in steps if isinstance(s, dict)}

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

        # Context source references resolve.
        for c in (s.get("context") or []):
            src = c.get("source") if isinstance(c, dict) else None
            if not isinstance(src, dict):
                continue
            ref_step = src.get("step")
            if ref_step and ref_step not in step_ids:
                violations.append(f"step '{sid}': context references unknown step '{ref_step}'")
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

    passed = not violations
    return {"passed": passed, "violations": violations,
            "unknown_tools": sorted(set(unknown_tools)),
            "unknown_roles": sorted(set(unknown_roles))}
