"""Display one exact run's pinned graph, never the latest registry definition.

Only execution structure is exposed, not prompts, tool parameters, context,
output values, goal text or raw graph JSON. Unknown historical versions fail
closed even though the executor may offer a recovery fallback elsewhere.
"""
from skillflow.core import graph_digest
from skillflow.graph import PipelineGraph, loop_body_map


class RunGraphUnavailable(ValueError):
    pass


def _match(match):
    if not isinstance(match, dict) or not match:
        return {}
    if match.get("from") == "checkpoint" and isinstance(match.get("value"), str) and match["value"] in {"approved", "rejected"}:
        return {"from": "checkpoint", "value": match["value"]}
    # Conditional data can contain literals from private inputs. For a run
    # overview only the field names, not arbitrary string values, are needed.
    return {"condition": ", ".join(str(k)[:80] for k in list(match)[:6])}


def pinned_run_graph(sf, run_id):
    run = sf.get_run(run_id)
    if not run or run.get("id") != run_id:
        raise KeyError("exact run not found")
    version, expected = run.get("graph_version"), run.get("graph_digest")
    if type(version) is not int or version < 1 or not expected:
        raise RunGraphUnavailable("run has no historical graph pin; refusing to substitute the current graph")
    saved = sf.get_graph_version(run["graph_name"], version)
    if not saved or saved.get("digest") != expected:
        raise RunGraphUnavailable("pinned workflow graph version is missing or inconsistent")
    try:
        data = saved["graph"]
        if graph_digest(data) != expected:
            raise RunGraphUnavailable("historical graph digest mismatch")
        graph = PipelineGraph._from_dict(data)
        if graph.validate():
            raise RunGraphUnavailable("historical graph structure is invalid")
        loops = loop_body_map(graph.steps)
    except RunGraphUnavailable:
        raise
    except Exception as exc:
        raise RunGraphUnavailable("historical graph cannot be projected") from exc
    steps = []
    for node in graph.steps:
        steps.append({"id": node.id, "type": node.step_type, "checkpoint": node.checkpoint,
                      "tool_name": node.tool_name or None, "agent_config": node.agent_config or None,
                      "loop_id": next((lid for lid, body in loops.items() if node.id in body), None),
                      "is_loop": node.step_type == "loop", "from_addon": False,
                      "transitions": [{"to": t.to, "match": _match(t.match), "max_loop": t.max_loop}
                                      for t in node.transitions]})
    return {"config_name": run["graph_name"], "label": run["graph_name"], "origin": "pinned_run",
            "run_id": run_id, "graph_version": version, "graph_digest": expected,
            "base": run["graph_name"], "addons": [], "addon_steps": [],
            "begin": graph.begin, "steps": steps, "loops": {k: sorted(v) for k, v in loops.items()},
            "node_labels": {n.id: n.id for n in graph.steps},
            "provenance_note": "Run-pinned structure. Current config labels/addon attribution are intentionally not applied."}
