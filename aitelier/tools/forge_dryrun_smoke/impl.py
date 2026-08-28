"""forge_dryrun_smoke — boot a generated graph with a stub runner (gate c).

Registers the generated graph in a throwaway in-memory SkillFlow (sharing the LIVE
ToolLoader so real + just-built tools resolve), drives it through the real
claim/advance loop with a no-LLM StubStepRunner, and asserts it reaches a
loop-external terminal within a step bound. See design/pipeline_forge.md §5c.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import yaml


def _drive(sf, run_id: str, runner, max_steps: int) -> dict:
    # Fully SYNCHRONOUS — this tool may execute inside the scheduler's already-
    # running event loop, so it must not create/run one of its own. The stub
    # runner, claim/advance/confirm, and approve_checkpoint are all sync.
    steps_run = 0
    trail: list[str] = []
    approvals = 0
    while steps_run < max_steps:
        next_node = sf.advance_run(run_id)
        # Drain consecutive inline tool/gate steps (advance runs one per call).
        try:
            resolver = sf._get_resolver_for_run(run_id)
            drain = 0
            while next_node is not None and drain < 100 and resolver.is_tool(next_node):
                trail.append(f"[tool]{next_node}")
                next_node = sf.advance_run(run_id)
                drain += 1
                steps_run += 1
        except Exception:
            pass

        if next_node is None:
            run = sf.get_run(run_id) or {}
            status = run.get("status")
            if status == "paused":
                # Checkpoint → auto-approve to continue toward the terminal.
                if approvals >= 20:
                    return {"status": "checkpoint_loop", "steps_run": steps_run,
                            "trail": trail}
                sf.approve_checkpoint(run_id)
                approvals += 1
                continue
            # error_reason is where skillflow puts the ONLY actionable text on a
            # failed run ("No matching transition from 'X' with flags {...}"), and
            # the smoke runs with trace_enabled=False, so nothing else records it.
            return {"status": status, "steps_run": steps_run, "trail": trail,
                    "error_reason": run.get("error_reason")}

        claimed = sf.claim_next_step(run_id)
        if claimed is None:
            run = sf.get_run(run_id) or {}
            if run.get("status") == "paused":
                sf.approve_checkpoint(run_id)
                approvals += 1
                continue
            return {"status": run.get("status"), "steps_run": steps_run,
                    "trail": trail, "error_reason": run.get("error_reason")}

        trail.append(claimed.step_id)
        try:
            res = runner.run(claimed)
            sf.confirm_step(claimed.token, res)
        except Exception as e:
            return {"status": "step_error", "steps_run": steps_run, "trail": trail,
                    "error": f"{claimed.step_id}: {e}"}
        steps_run += 1
    return {"status": "max_steps", "steps_run": steps_run, "trail": trail}


def forge_dryrun_smoke(graph_path: str = "", max_steps: int = 200,
                       verdict: bool = True, out_dir: str = "", **kwargs) -> dict:
    from aitelier.gate_report import write_gate_report

    def _fail(payload: dict) -> dict:
        write_gate_report(out_dir, "forge_dryrun_smoke", False,
                          payload.get("error", ""))
        return payload

    p = Path(graph_path)
    if not p.exists():
        return _fail({"passed": False, "status": "no_graph",
                      "error": f"graph not found: {graph_path}"})
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return _fail({"passed": False, "status": "parse_error", "error": str(e)})
    if not isinstance(data, dict) or not data.get("steps"):
        return _fail({"passed": False, "status": "invalid_graph",
                      "error": "graph has no steps"})

    # Structural smoke: strip validation (canned outputs needn't satisfy schemas)
    # and give the graph a unique name so it can't clobber a live registration.
    # Tool nodes are STUBBED (converted to stub-agents returning the success flag)
    # rather than run for real: the smoke checks graph reachability/termination, not
    # tool I/O — an input-dependent tool (e.g. a file scanner with no file) would
    # otherwise never "pass" and loop the smoke to a false-negative failure. Tool
    # IMPORTABILITY is still checked statically below (load_fn), and gates keep
    # their transitions, so a bad graph shape still fails.
    tool_names: list[str] = []
    for s in data.get("steps", []):
        if not isinstance(s, dict):
            continue
        s.pop("validation", None)
        if s.get("step_type") == "tool":
            if s.get("tool_name"):
                tool_names.append(s["tool_name"])
            s["step_type"] = "agent"
            s["agent_config"] = "_forge_stub"
            s.pop("tool_name", None)
            s.pop("tool_params", None)
    data["name"] = "smoke_" + str(data.get("name") or "gen")

    try:
        from skillflow import SkillFlow, PipelineGraph
        from core.pipeline_registry import ensure_host_agents
        from aitelier.stub_runner import StubStepRunner
        from api.dependencies import get_skillflow
    except Exception as e:  # pragma: no cover
        return _fail({"passed": False, "status": "import_error", "error": str(e)})

    live_loader = get_skillflow()._tool_loader

    # (2) Static tool-construction check — every referenced tool must import
    # (load_fn), even though the smoke stubs their execution.
    unresolved: list[str] = []
    for tname in tool_names:
        try:
            live_loader.load_fn(tname)
        except Exception as e:
            unresolved.append(f"{tname}: {e}")

    tmp = tempfile.mkdtemp(prefix="forge_smoke_")
    try:
        sf = SkillFlow(":memory:", tool_loader=live_loader,
                       workspace_base=tmp, projects_base=tmp, code_dir=tmp,
                       trace_enabled=False, artifact_history=False)
        graph = PipelineGraph._from_dict(data)
        ensure_host_agents(sf, graph)   # register invented roles as host agents
        sf.register_graph(graph)        # validates graph structure + agent refs
        run_id = sf.create_run(graph.name, project_id="forge_smoke")
        sf.start_run(run_id)
        drive = _drive(sf, run_id, StubStepRunner(verdict=verdict, graph=data),
                       int(max_steps))
    except Exception as e:
        return _fail({"passed": False, "status": "boot_error", "error": str(e),
                      "unresolved_tools": unresolved})
    finally:
        # The staging tree only ever holds canned stub output, and the :memory: DB
        # dies with `sf`. Left behind, every smoke leaks a directory — tolerable
        # when this ran once per generation, not now that a baseline replay calls
        # it on every edit.
        shutil.rmtree(tmp, ignore_errors=True)

    status = drive.get("status")
    reached_done = status == "completed"
    # Happy path must complete; adversarial path must terminate (not run to max_steps).
    if verdict:
        passed = reached_done and not unresolved
    else:
        passed = status in ("failed", "completed") and status != "max_steps"

    # `error` is what skillflow's tool-gate loop-back injects into the emitter's
    # feedback (core._inject_feedback_in_tx passes ONLY tool_result["error"]).
    # Synthesize an actionable one when the drive didn't hand us a raw error, so
    # a re-emit knows WHY the smoke failed instead of re-emitting blind.
    error = drive.get("error") or ""
    reason = (drive.get("error_reason") or "").strip()
    if not passed and not error:
        trail = drive.get("trail")
        if unresolved:
            error = ("dry-run smoke failed: these referenced tools don't load: "
                     + ", ".join(unresolved))
        elif status == "max_steps":
            error = ("dry-run smoke failed: the graph never reached its terminal "
                     "within the step budget — likely an unbounded loop or a "
                     "transition that never matches. Trail: " + str(trail))
        elif status == "checkpoint_loop":
            error = ("dry-run smoke failed: the graph re-paused at a checkpoint "
                     "without progressing. Trail: " + str(trail))
        else:
            # Without the reason this branch says only WHERE it stopped, never WHY,
            # which cost several blind re-emit rounds. skillflow's own message names
            # the step and the flags that matched nothing — quote it verbatim.
            last = (trail or [])[-1] if trail else ""
            where = f" after '{last}'" if last else ""
            detail = f" Reason: {reason}." if reason else ""
            error = (f"dry-run smoke failed{where} (status={status})."
                     f"{detail} Trail: {trail}")
    write_gate_report(out_dir, "forge_dryrun_smoke", bool(passed), error)
    return {"passed": bool(passed), "status": status,
            "steps_run": drive.get("steps_run"), "trail": drive.get("trail"),
            "unresolved_tools": unresolved, "error_reason": reason or None,
            "error": error}
