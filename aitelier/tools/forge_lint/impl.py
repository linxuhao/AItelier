"""forge_lint — gate (a) of pipeline_forge validation.

Thin wrapper over skillflow's native graph linter (`skillflow_lint`) that adds an
`error` field summarizing the issues. The native tool returns {passed, issues:[...]}
with no `error`; but skillflow's tool-gate loop-back injects ONLY tool_result["error"]
into the maker's feedback (core._inject_feedback_in_tx), so a bare skillflow_lint
loop-back leaves the emitter blind to WHY the lint failed — it re-emits and
reproduces the same violation. This surfaces the issues so the re-emit is targeted.
"""
from __future__ import annotations


def forge_lint(path: str = "", out_dir: str = "", **kwargs) -> dict:
    from api.dependencies import get_skillflow
    from aitelier.gate_report import write_gate_report
    loader = get_skillflow()._tool_loader
    lint = loader.load_fn("skillflow_lint")
    # Hand the linter the live registry so it can check tool EXISTENCE — structural
    # validation cannot tell a real tool from an invented one, which is how a graph
    # naming a non-existent tool used to lint clean. (Older skillflow builds ignore
    # the kwarg; retry without it so the gate still runs.)
    try:
        res = lint(path=path, tool_loader=loader) if path else lint(tool_loader=loader, **kwargs)
    except TypeError:
        res = lint(path=path) if path else lint(**kwargs)
    if not isinstance(res, dict):
        res = {"passed": False, "error": f"skillflow_lint returned {type(res)}"}
        write_gate_report(out_dir, "forge_lint", False, res["error"])
        return res
    issues = res.get("issues") or []
    if res.get("passed"):
        res.setdefault("error", "")
        write_gate_report(out_dir, "forge_lint", True, "")
        return res
    lines = []
    for it in issues:
        if isinstance(it, dict):
            msg = it.get("message", "")
            sug = it.get("suggestion", "")
            lines.append(f"{msg}" + (f" — FIX: {sug}" if sug else ""))
        else:
            lines.append(str(it))
    res["error"] = ("Graph lint failed — fix these before re-emitting:\n- "
                    + "\n- ".join(lines)) if lines else "Graph lint failed."
    # The `error` field alone does NOT reach the emitter (see aitelier.gate_report);
    # the file does, via an ordinary context source.
    write_gate_report(out_dir, "forge_lint", False, res["error"])
    return res
