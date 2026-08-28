"""Regression baselines for generated pipelines and addons.

The three forge gates (`skillflow_lint`, `forge_registry_check`,
`forge_dryrun_smoke`) all run ONCE, at generation. After that a `gen_*` pipeline
stays editable (`_tool_config_edit`, `edit_pipeline`, `edit_role`) and nothing
proves that an edit kept whatever a real test-drive already verified. A baseline
is that proof: a recorded shape + stub-drive result that a later edit is replayed
against, deterministically and without an LLM call.

Two halves, because only one of them can always be collected:

  * ``shape`` + ``smoke`` — static read of the graph plus a stub drive. Available
    for anything registered, which is why addons get a baseline at all: a composed
    addon config (`dpe_game`) has no file of its own and its base is a whole DPE
    run, so it can never be test-driven for real. Recomposing it IS the check —
    `compose_graph` raises when an overlay's target step id no longer exists, and
    an addon may target raw base ids (game_harness hangs off `5_knowledge`,
    `t_plan`, `t_impl`), so a base rename silently breaks it today.
  * ``observed`` — what a real drive actually produced, per step. Only `gen_*`
    pipelines can earn this; `drive_pipeline` is their entry point.

The stub drive is not always available: a graph whose first step requires context
from ANOTHER config's run cannot boot in the stub's empty workspace, which is true
of `dpe_default_v2` and therefore of every addon composed onto it. `smoke.usable`
says whether it ran, so a replay reports "structure only" instead of comparing two
identical failures and calling it a match.

The comparison is deliberately coarse where a fine one would be noise: the stub
drive is compared as a SET of reached step ids, never as a sequence. A real
drive's loop iterates once per real task and the stub's iterates over canned
(usually empty) output, so the sequences differ for reasons that have nothing to
do with a regression.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

KIND_PIPELINE = "pipeline"
KIND_ADDON = "addon"

BASELINE_SUFFIX = ".baseline.json"

# The stub drive is structural: a graph that cannot terminate is the finding, so
# the bound only has to be generous enough that a healthy graph never hits it.
_SMOKE_MAX_STEPS = 200

# Outcomes that mean the drive never STARTED, as opposed to started and ended
# somewhere. A graph whose first step needs context from another config's run
# cannot boot in the stub's empty workspace — every DPE-based graph is in this
# class ("Required context source resolved to no content: finalize"), and so is
# every addon composed onto one. Recorded as an ordinary result, a later replay
# would compare boot_error to boot_error and report "matches the baseline" when
# the reachability half had never run at all.
_SMOKE_DID_NOT_RUN = {"boot_error", "no_graph", "parse_error", "invalid_graph",
                      "import_error", "unavailable"}


# --------------------------------------------------------------------------- #
# location
# --------------------------------------------------------------------------- #

def path_for(kind: str, target: str) -> Path:
    """Where *target*'s baseline lives, next to the thing it describes.

    Both boot scans are suffix-specific — `load_generated_configs` globs
    `gen_*.yaml` and `load_generated_addons` globs `*.yaml` — so a `.baseline.json`
    sibling is invisible to them.
    """
    if kind == KIND_ADDON:
        from core.addon_registry import generated_addons_dir
        return generated_addons_dir() / f"{target}{BASELINE_SUFFIX}"
    from core.pipeline_registry import generated_configs_dir
    return generated_configs_dir() / f"{target}{BASELINE_SUFFIX}"


def resolve_kind(target: str) -> str | None:
    """`pipeline` for a registered `gen_*`, `addon` for a declared overlay name.

    Returns None when the name is neither, so the caller can say so rather than
    guessing and failing later with a confusing error.
    """
    from api.dependencies import get_skillflow
    from core.pipeline_registry import GEN_PREFIX
    if target.startswith(GEN_PREFIX):
        return KIND_PIPELINE
    try:
        if any(o.get("name") == target for o in get_skillflow().list_overlays()):
            return KIND_ADDON
    except Exception:                                            # noqa: BLE001
        pass
    return None


def read(kind: str, target: str) -> dict | None:
    p = path_for(kind, target)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                       # noqa: BLE001
        log.warning("unreadable baseline %s: %s", p, e)
        return None


def write(kind: str, target: str, data: dict) -> Path:
    """Atomic — a half-written baseline reads as a pile of false regressions."""
    p = path_for(kind, target)
    tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def delete(kind: str, target: str) -> bool:
    p = path_for(kind, target)
    try:
        p.unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# resolving the graph a baseline describes
# --------------------------------------------------------------------------- #

def _addon_spec(name: str) -> dict:
    """The overlay spec as skillflow currently holds it.

    Read from the registry rather than the file so a spec registered live (a
    just-generated addon) resolves too; the boot scan and `register_addon_from_run`
    both put it there.
    """
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    spec = getattr(sf, "_overlays", {}).get(name)
    if not spec:
        raise ValueError(f"no registered addon '{name}'")
    return spec


def resolve_graph(kind: str, target: str) -> dict:
    """The graph dict a baseline is taken against.

    For an addon this RECOMPOSES against the live base instead of reading the
    combo out of the registry: the composed graph registered at boot is frozen at
    whatever the base looked like then, and a base edit since is exactly the
    regression this is meant to catch. `compose_graph` raises on an unresolvable
    anchor or step id, which is the finding itself.
    """
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    if kind == KIND_ADDON:
        from skillflow.compose import compose_graph
        spec = _addon_spec(target)
        base = spec.get("base") or ""
        base_graph = getattr(sf, "_graphs", {}).get(base)
        if base_graph is None:
            raise ValueError(f"addon '{target}' binds to base '{base}', "
                             f"which is not registered")
        composed = compose_graph(base_graph.to_dict(), [spec])
        composed["name"] = spec.get("alias") or f"{base}__{target}"
        return composed
    graph = getattr(sf, "_graphs", {}).get(target)
    if graph is None:
        raise ValueError(f"no registered config '{target}'")
    return graph.to_dict()


def _base_step_ids(kind: str, target: str) -> set[str]:
    """Step ids the addon's BASE already had — everything else is spliced in."""
    if kind != KIND_ADDON:
        return set()
    from api.dependencies import get_skillflow
    try:
        spec = _addon_spec(target)
        base_graph = getattr(get_skillflow(), "_graphs", {}).get(spec.get("base"))
        if base_graph is None:
            return set()
        return {s.get("id") for s in (base_graph.to_dict().get("steps") or [])}
    except Exception:                                            # noqa: BLE001
        return set()


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

def _declared_outputs(step: dict) -> list[str]:
    """File names a step promises in ``output.fixed``.

    Globs are skipped: a `*` pattern names no specific file, so it can neither be
    compared nor go missing. Same rule the stub runner applies when it touches
    declared outputs (`aitelier/stub_runner.py:_touch_declared_outputs`).
    """
    out = (step.get("output") or {}) if isinstance(step.get("output"), dict) else {}
    fixed = out.get("fixed")
    if not isinstance(fixed, dict):
        return []
    names = []
    for spec in fixed.values():
        fname = spec.get("file") if isinstance(spec, dict) else spec
        if fname and "*" not in str(fname):
            names.append(str(fname))
    return sorted(set(names))


def capture_shape(graph: dict, *, base_ids: set[str] | None = None) -> dict:
    steps = {}
    for s in graph.get("steps") or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        steps[s["id"]] = {
            "step_type": s.get("step_type"),
            "tool_name": s.get("tool_name"),
            "agent_config": s.get("agent_config"),
            "capability": s.get("capability"),
            "declared_outputs": _declared_outputs(s),
        }
    shape = {
        "begin": graph.get("begin"),
        "capabilities": sorted(graph.get("capabilities") or []),
        "steps": steps,
    }
    if base_ids is not None:
        shape["addon_steps"] = sorted(i for i in steps if i not in base_ids)
    return shape


def capture_smoke(graph: dict) -> dict:
    """Drive *graph* with the stub runner and record where it got to.

    Goes through `forge_dryrun_smoke` rather than reimplementing the drive so the
    replay and the generation gate can never disagree about what "boots and
    terminates" means. It takes a path, so the graph is dumped to a temp file that
    is removed with the directory.
    """
    import yaml
    from api.dependencies import get_skillflow

    try:
        fn = get_skillflow()._tool_loader.load_fn("forge_dryrun_smoke")
    except Exception as e:                                       # noqa: BLE001
        return {"status": "unavailable", "reached": [], "usable": False,
                "passed": False, "error": str(e)}

    with tempfile.TemporaryDirectory(prefix="baseline_smoke_") as d:
        p = Path(d) / "graph.yaml"
        p.write_text(yaml.safe_dump(graph, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        # out_dir="" — the gate report is feedback for a forge re-emit round; a
        # replay has a caller to return findings to.
        res = fn(graph_path=str(p), max_steps=_SMOKE_MAX_STEPS, verdict=True,
                 out_dir="") or {}

    trail = res.get("trail") or []
    reached = sorted({t[len("[tool]"):] if t.startswith("[tool]") else t
                      for t in trail})
    status = res.get("status")
    return {"status": status, "passed": bool(res.get("passed")),
            "reached": reached,
            "usable": status not in _SMOKE_DID_NOT_RUN,
            "error": res.get("error") or res.get("error_reason") or ""}


def capture_observed(sf, ws, run_id: str, project_id: str, config_name: str,
                     test_seed: str = "") -> dict:
    """Per-step file names a real drive actually promoted.

    Only BASENAMES. `get_final_path` returns the `{step}/` parent for a loop-body
    step and `rglob` descends into the per-item subdirectories, whose names carry
    a hash suffix that varies with the item value (`skillflow/workspace.py`
    `_sanitize_item`) — full relative paths would differ between two healthy runs
    of the same graph.
    """
    seen: dict[str, dict] = {}
    try:
        rows = sf.get_steps(run_id)
    except Exception as e:                                       # noqa: BLE001
        log.warning("baseline: cannot read steps of run %s: %s", run_id, e)
        rows = []
    for r in rows:
        step_id = r.get("step_id")
        if not step_id:
            continue
        entry = seen.get(step_id)
        if entry is None:
            entry = {"step": step_id, "loop": False, "files": []}
            seen[step_id] = entry
            try:
                od = ws.get_final_path(project_id, step_id, config_name)
                if od.exists():
                    entry["files"] = sorted(
                        {f.name for f in od.rglob("*")
                         if f.is_file() and f.name != "_snapshot.json"})
            except Exception:                                    # noqa: BLE001
                pass          # a missing output dir is a fact about the run
        if r.get("loop_item"):
            entry["loop"] = True
    return {"test_seed": test_seed, "steps": list(seen.values())}


def graph_digest(graph: dict) -> str:
    return hashlib.sha256(
        json.dumps(graph, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def capture(kind: str, target: str, *, observed: dict | None = None) -> dict:
    """A full baseline for *target*. Raises on an unresolvable graph."""
    graph = resolve_graph(kind, target)
    base_ids = _base_step_ids(kind, target) if kind == KIND_ADDON else None
    data = {
        "kind": kind,
        "target": target,
        "graph_digest": graph_digest(graph),
        "shape": capture_shape(graph, base_ids=base_ids),
        "smoke": capture_smoke(graph),
    }
    if kind == KIND_ADDON:
        spec = _addon_spec(target)
        data["base"] = spec.get("base")
        data["alias"] = spec.get("alias")
    if observed:
        data["observed"] = observed
    return data


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #

def _f(name: str, detail: str, **extra) -> dict:
    return {"finding": name, "detail": detail, **extra}


def diff(old: dict, new: dict) -> list[dict]:
    """Structured differences from the recorded baseline to a fresh capture.

    Each difference is reported ONCE. A step that vanished is `step_removed`, not
    also `unreachable` — reachability is only asked about steps both captures
    still have, or a rename would produce three findings describing one edit.
    """
    out: list[dict] = []
    o_shape = (old.get("shape") or {})
    n_shape = (new.get("shape") or {})
    o_steps: dict = o_shape.get("steps") or {}
    n_steps: dict = n_shape.get("steps") or {}

    for sid in sorted(set(o_steps) - set(n_steps)):
        out.append(_f("step_removed",
                      f"step '{sid}' was in the baseline and is gone", step=sid))
    for sid in sorted(set(n_steps) - set(o_steps)):
        out.append(_f("step_added", f"step '{sid}' is new since the baseline",
                      step=sid))

    for sid in sorted(set(o_steps) & set(n_steps)):
        o, n = o_steps[sid], n_steps[sid]
        for field in ("step_type", "tool_name", "agent_config", "capability"):
            if o.get(field) != n.get(field):
                out.append(_f("step_changed",
                              f"step '{sid}' {field}: {o.get(field)!r} → "
                              f"{n.get(field)!r}", step=sid, field=field))
        gone = sorted(set(o.get("declared_outputs") or [])
                      - set(n.get("declared_outputs") or []))
        if gone:
            out.append(_f("output_undeclared",
                          f"step '{sid}' no longer declares {gone}",
                          step=sid, files=gone))

    dropped_caps = sorted(set(o_shape.get("capabilities") or [])
                          - set(n_shape.get("capabilities") or []))
    if dropped_caps:
        out.append(_f("capability_dropped",
                      f"the graph no longer offers {dropped_caps}",
                      capabilities=dropped_caps))

    o_smoke, n_smoke = (old.get("smoke") or {}), (new.get("smoke") or {})
    if o_smoke.get("status") != n_smoke.get("status"):
        out.append(_f("smoke_status_changed",
                      f"stub drive ended {o_smoke.get('status')!r} at baseline, "
                      f"{n_smoke.get('status')!r} now"
                      + (f" — {n_smoke['error']}" if n_smoke.get("error") else "")))
    still_present = set(o_steps) & set(n_steps)
    unreachable = sorted((set(o_smoke.get("reached") or [])
                          - set(n_smoke.get("reached") or [])) & still_present)
    if unreachable:
        out.append(_f("unreachable",
                      f"the stub drive no longer reaches {unreachable} — "
                      f"a transition was rewired or never matches",
                      steps=unreachable))

    # What a real drive PRODUCED, against what the graph still PROMISES. Only for
    # steps that declare fixed outputs at all: an agent step with no `output.fixed`
    # writes freely, so "produced but undeclared" is its normal state, not a
    # regression.
    for entry in ((old.get("observed") or {}).get("steps") or []):
        sid = entry.get("step")
        cur = n_steps.get(sid)
        if not cur:
            continue
        declared = set(cur.get("declared_outputs") or [])
        if not declared:
            continue
        produced = set(entry.get("files") or [])
        promised_then = set((o_steps.get(sid) or {}).get("declared_outputs") or [])
        lost = sorted((produced & promised_then) - declared)
        if lost:
            out.append(_f("observed_output_undeclared",
                          f"step '{sid}' produced {lost} on the recorded drive "
                          f"but no longer declares it", step=sid, files=lost))
    return out
