"""Run selected/inline scenarios against the current run worktree.

Code and the playtest contract are read from the same root, including uncommitted
changes. No staging overlay or temporary copy of the project is constructed.
Legacy staged callers fail explicitly; they never get a misleading baseline pass.
"""

import json
from pathlib import Path

_MAX_ASSERT_LINES = 200


def _render(scenarios: list[dict]) -> list[str]:
    """The failing-assert view an implementer actually needs: the expression,
    and the value the property ACTUALLY held. `actual` on a comparison assert is
    just `false` — it says the assert did not hold and nothing about why."""
    lines: list[str] = []
    budget = _MAX_ASSERT_LINES
    for sc in scenarios:
        asserts = sc.get("asserts") or []
        ok = sum(1 for a in asserts if a.get("passed"))
        mark = "PASS" if sc.get("passed") else "FAIL"
        lines.append(f"[{mark}] {sc.get('name')}  {ok}/{len(asserts)}")
        for e in sc.get("errors") or []:
            lines.append(f"    RUNTIME ERROR: {json.dumps(e, ensure_ascii=False)[:200]}")
        if sc.get("input_dead"):
            lines.append("    INPUT DEAD: this scenario pressed keys and ended in "
                         "exactly the state a no-input run reaches.")
        for a in asserts:
            if a.get("passed"):
                continue
            if budget <= 0:
                lines.append("    … (assertion list truncated)")
                break
            budget -= 1
            line = (f"    FAIL f{a.get('frame')} {a.get('name')}: "
                    f"{str(a.get('expr'))[:100]}")
            if "observed" in a:
                line += (f"\n         observed="
                         f"{json.dumps(a.get('observed'), ensure_ascii=False)[:200]}")
            if a.get("error"):
                line += f"\n         error={str(a.get('error'))[:160]}"
            lines.append(line)
    return lines


def godot_playtest_scenario(*, scenario: str = "", inline_scenario: str = "",
                            project_root: str = "",
                            workspace_root: str = "",
                            step_id: str = "", run_id: str = "",
                            legacy_code_staging: bool = False, **kwargs) -> dict:
    """Play-test one scenario and report every failing assertion's ``observed``.

    ``scenario`` names one (or several, comma-separated) from the project's
    contract. ``inline_scenario`` instead supplies a scenario as YAML text and
    never touches the repo — that is the way to force values out of a running
    build without writing a throwaway file into the deliverable.
    """
    from aitelier.tools.godot_playtest.impl import (post_playtest, read_spec,
                                                    select_scenarios)

    if not project_root or not Path(project_root).is_absolute():
        return {"error": "godot_playtest_scenario requires an injected absolute project_root"}
    repo = Path(project_root).resolve()
    if not (repo / "project.godot").is_file():
        return {"error": f"No project.godot at {repo} — not a Godot project."}

    names = [n.strip() for n in str(scenario).split(",") if n.strip()]
    inline_doc = None
    if str(inline_scenario).strip():
        # Strict on duplicate mapping keys: an inline probe that repeats
        # `assert:` would otherwise run only its last block, silently.
        from aitelier.strict_yaml import load_yaml_strict
        try:
            doc = load_yaml_strict(inline_scenario, source="inline_scenario")
        except Exception as exc:
            return {"error": f"inline_scenario is not valid YAML: {exc}"}
        if isinstance(doc, dict) and isinstance(doc.get("scenarios"), list):
            inline_doc = doc["scenarios"]
        elif isinstance(doc, dict) and doc.get("timeline") is not None:
            inline_doc = [doc]
        else:
            return {"error": "inline_scenario must be one scenario mapping "
                             "(with `timeline:`) or {scenarios: [...]}."}
        for i, sc in enumerate(inline_doc):
            if not isinstance(sc, dict) or sc.get("timeline") is None:
                return {"error": f"inline_scenario[{i}] has no `timeline:`."}
            sc.setdefault("name", f"inline_probe_{i}" if i else "inline_probe")
    if not names and inline_doc is None:
        return {"error": "give either `scenario` (a name from the contract) or "
                         "`inline_scenario` (scenario YAML, never written to "
                         "the repo)."}
    if names and inline_doc is not None:
        return {"error": "give `scenario` or `inline_scenario`, not both."}

    # This runtime no longer overlays code drafts. An old pinned graph must
    # be completed with its old runtime or explicitly migrated, not measured
    # against the wrong tree after a restart.
    if legacy_code_staging:
        return {"error": "Legacy code-staging graph. Code playtests require direct "
                         "worktree outputs; explicitly recover or finish the old run first."}
    if "use_staged" in kwargs:
        return {"error": "use_staged is no longer supported; tests use the current run worktree."}
    # Artifact drafts (a plan, verdict, report) are NOT source staging and
    # must not prevent a reviewer/planner from probing the actual code.
    target = repo
    spec, info = read_spec(target)
    if info["errors"]:
        return {"error": "The play-test contract could not be read whole: "
                         + " | ".join(info["errors"])}
    if not spec:
        return {"error": f"No play-test contract in {target} (expected a "
                         f"playtest/ directory or playtest_spec.yaml)."}

    if inline_doc is not None:
        # The contract still supplies the shared header (scene / actions /
        # surface from _common.yaml) — only the scenario list is replaced.
        # Nothing is written to playtest/, which is the whole point: the
        # tool used to take a NAME only, so forcing `observed` values out
        # meant writing a throwaway scenario into the deliverable directory
        # and remembering to delete it. jinyong-endgame 2026-08-24: four of
        # six cards shipped or re-shipped probe scaffolding that way, one of
        # them across three rejections, and the loader runs unlisted
        # scenario files — so a forgotten probe reddens the WHOLE gate, not
        # just itself.
        picked = dict(spec)
        picked["scenarios"] = inline_doc
    else:
        available = sorted(str(s.get("name")) for s in spec["scenarios"])
        picked, unknown = select_scenarios(spec, names)
        if unknown:
            # Never run the recognised subset and report on it: a typo would
            # then read as "the scenario I asked about is green".
            return {"error": f"unknown scenario(s) {unknown}. Available: "
                             f"{', '.join(available)}"}

    report = post_playtest({"project_dir": str(target), "spec": picked},
                           timeout=900)
    if report.get("gate_skipped"):
        return {"error": report.get("summary", "godot-builder unreachable")}
    if report.get("no_project"):
        return {"error": f"godot-builder cannot see {target} — its workspace "
                         f"mount is stale; recreate the container."}

    scen = (report.get("behavior") or {}).get("scenarios") or []
    results = [{"name": s.get("name"),
                "passed": bool(s.get("passed")),
                "ok": sum(1 for a in (s.get("asserts") or []) if a.get("passed")),
                "total": len(s.get("asserts") or [])} for s in scen]
    header = [f"ran {len(results)} scenario(s) against the current run worktree: {target}",
              f"spec source: {info['source']}",
              f"hard gate passed: {report.get('passed')} — "
              f"{report.get('summary', '')}",
              ""]
    return {
        "scenarios": results,
        "all_passed": bool(results) and all(r["passed"] for r in results),
        "hard_passed": bool(report.get("passed")),
        "code_root": str(target),
        "report": "\n".join(header + _render(scen)),
    }
