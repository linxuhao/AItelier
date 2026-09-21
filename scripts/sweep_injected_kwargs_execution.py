"""Execute every tool in the live registry through the engine, with the host's
keyword injection switched on, and count the calls the engine REFUSED.

``scripts/audit_injected_kwargs.py`` measures what the guard and the signatures
SAY.  Its "after" number cannot be evidence for the fix: the guard's first
clause is ``keyword in named`` and the audit's refusal predicate is ``keyword
not in named``, so "guard says inject AND the engine would refuse" is
unsatisfiable for any registry, correct guard or not.  This script measures what
the ENGINE DOES instead — it calls each tool through
``AItelierSkillFlow._execute_tool_impl`` (the exact layer that binds the keyword
and the exact layer that drops it) and reads the returned error.

Two arms, so a null result is falsifiable:

* ``live``  — the real path.  The host injects per ``_tool_accepts_keyword``.
  Expected: not one refusal, for any tool.
* ``forced`` — the counterfactual.  ``project_id`` is put into the caller's
  params for EVERY tool regardless of the guard.  Expected: a refusal from every
  tool whose signature does not name it.  This arm is the instrument's control:
  it proves the harness can still see a refusal, so the ``live`` zero is an
  observation rather than an artefact of how the question is asked.

A tool that errors for its own reasons (absent path, no repo, missing argument)
is a PASS here: an error from inside the body is proof the body was entered.
Only the engine's pre-execution wording counts as a refusal.

Every call is parameterised to fail fast and touch nothing: paths point into a
throwaway directory that is never created, so repo/asset/push tools bail on
their own guards before doing work.  Nothing here runs the Godot engine.

The registry is enumerated AFTER the runtime is composed, which is what the
engine itself sees: 84 names, one (``skillflow_lint``) more than the
loader-only view ``scripts/audit_injected_kwargs.py`` reports.

Run inside the container::

    docker exec aitelier python3 /app/scripts/sweep_injected_kwargs_execution.py <run_id>
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REFUSED_MARKER = "No tool action was performed"

SCRATCH = Path(tempfile.mkdtemp(prefix="injected-kwargs-sweep-"))
ABSENT = SCRATCH / "no-such-project"        # deliberately never created
ABSENT_OUT = SCRATCH / "no-such-out-dir"    # deliberately never created

# Pinned onto every call that NAMES them, so a tool that would otherwise fall
# back to the process CWD (or to a real repository) is aimed at nothing.
PINS = {
    "project_root": str(ABSENT),
    "workspace_root": str(ABSENT),
    "out_dir": str(ABSENT_OUT),
    "config_dir": str(ABSENT),
}

LIST_ARGS = {"files", "validations", "from_repo", "from_steps"}
MAPPING_ARGS = {"inline_schema"}
PATHISH = {"path", "file", "filename", "file_path", "graph_path", "seed_path",
           "source_dir", "directory", "dest", "dir"}


def sentinel(param: str):
    if param in LIST_ARGS:
        return []
    if param in MAPPING_ARGS:
        return {}
    if param in PATHISH:
        return str(ABSENT / "missing")
    if param == "url":
        return "http://127.0.0.1:9/injected-kwargs-sweep"
    return "__injected_kwargs_sweep_probe__"


def named_parameters(fn) -> set[str]:
    return {p.name for p in inspect.signature(fn).parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY)}


def required_parameters(fn) -> list[str]:
    return [p.name for p in inspect.signature(fn).parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)]


def probe_params(named: set[str], required: list[str]) -> dict:
    params = {k: v for k, v in PINS.items() if k in named}
    for param in required:
        params.setdefault(param, sentinel(param))
    return params


def classify(outcome) -> tuple[str, str]:
    """(verdict, detail). ``refused`` is the only failing verdict."""
    if isinstance(outcome, BaseException):
        return "raised_out_of_engine", f"{type(outcome).__name__}: {outcome}"
    if isinstance(outcome, dict):
        error = str(outcome.get("error") or "")
        if REFUSED_MARKER in error:
            return "refused", error[:300]
        if error:
            return "entered_and_errored", error[:200]
        return "entered_and_returned", json.dumps(outcome, default=str)[:160]
    return "entered_and_returned", repr(outcome)[:160]


def main() -> int:
    run_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not run_id:
        print("usage: sweep_injected_kwargs_execution.py <run_id of a real run>")
        return 2

    from api.dependencies import get_skillflow, get_tool_loader
    from core.skillflow_host import AItelierSkillFlow

    sf = get_skillflow()
    loader = get_tool_loader()
    project_id = sf._get_project_id(run_id)
    if not project_id:
        print(f"run {run_id} resolves to no project; the host would inject "
              f"nothing and the sweep would prove nothing")
        return 2

    names = sorted(set(loader.list_tools()) | set(getattr(loader, "_cache", {})))
    keywords = AItelierSkillFlow.HOST_INJECTED_KEYWORDS
    print(f"run={run_id} project={project_id} tools={len(names)} "
          f"keywords={list(keywords)}")
    print(f"scratch={SCRATCH} (absent project root: {ABSENT})")

    results: dict[str, dict] = {}
    unexercised: list[tuple[str, str]] = []
    for name in names:
        try:
            fn = loader.load_fn(name)
            named, required = named_parameters(fn), required_parameters(fn)
        except Exception as exc:  # noqa: BLE001
            unexercised.append((name, f"callable would not load: "
                                      f"{type(exc).__name__}: {exc}"))
            continue
        row: dict = {"named_project_id": all(k in named for k in keywords)}
        for arm in ("live", "forced"):
            params = probe_params(named, required)
            if arm == "forced":
                for keyword in keywords:
                    params[keyword] = project_id
            try:
                outcome = sf._execute_tool_impl(
                    name, params, run_id=run_id, step_id="",
                    project_root=str(ABSENT))
            except BaseException as exc:  # noqa: BLE001
                outcome = exc
            row[arm] = classify(outcome)
        row["guard"] = {k: bool(sf._tool_accepts_keyword(name, k))
                        for k in keywords}
        results[name] = row

    # A call that never reached the signature filter answers nothing about the
    # refusal, so it is reported as inconclusive rather than counted as a pass.
    for name, row in results.items():
        if row["live"][0] == "raised_out_of_engine":
            unexercised.append((name, "the engine raised before the signature "
                                      "filter: " + row["live"][1]))
    inconclusive = {name for name, _ in unexercised}
    exercised = {n for n in results if n not in inconclusive}
    live_refused = [n for n in exercised if results[n]["live"][0] == "refused"]
    forced_refused = [n for n in exercised if results[n]["forced"][0] == "refused"]
    injected = [n for n in exercised if any(results[n]["guard"].values())]

    for name in sorted(results):
        row = results[name]
        print(f"{name}\n    guard={row['guard']} "
              f"live={row['live'][0]} forced={row['forced'][0]}")
        if row["live"][0] == "refused":
            print(f"    !! {row['live'][1]}")

    print()
    print(f"tools exercised through the engine: {len(exercised)} of {len(names)}")
    for name, why in unexercised:
        print(f"  NOT exercised: {name} — {why}")
    print(f"host injects into (guard says yes): {len(injected)}")
    print(f"LIVE arm refusals (must be 0): {len(live_refused)} {live_refused}")
    print(f"FORCED arm refusals (the instrument's control, must be > 0): "
          f"{len(forced_refused)}")
    print("SUMMARY_JSON " + json.dumps({
        "run_id": run_id,
        "tools_in_registry": len(names),
        "tools_exercised": len(exercised),
        "not_exercised": sorted(inconclusive),
        "injected_by_guard": len(injected),
        "live_refusals": live_refused,
        "forced_refusals": len(forced_refused),
    }, sort_keys=True))
    # An empty control means the harness could not have seen a refusal either
    # way, which makes the live zero worthless. Fail on that too.
    return 1 if (live_refused or not forced_refused) else 0


if __name__ == "__main__":
    raise SystemExit(main())
