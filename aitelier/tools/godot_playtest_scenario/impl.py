"""godot_playtest_scenario — run ONE play-test scenario, not all 26.

WHY THIS EXISTS. The 5_compile gate runs the whole contract, and on
jinyong-assets that is 26 scenarios and about ten minutes. An implementer
repairing one scenario therefore had no way to ask "is THIS one green yet?"
short of ending its step and waiting for the full gate — so it did not ask. It
reasoned from the source instead, guessed at the runtime, and spent a whole task
slot on a wrong guess. jinyong-usable, 2026-08-24: five enemies failed
`turns_taken == 1` with `actual: false`, four rounds of static diagnosis missed
why, and one 90-second single-scenario probe (`/tmp/probe_turns.py`, the
prototype this tool generalises) produced the number — 2, they had acted twice —
that settled it immediately.

WHAT IT IS NOT. It is not a gate and it is not wired into any graph edge. The
full 26-scenario run at 5_compile stays exactly as it is, because it is the only
thing that catches "fixing X broke Y". This is a probe an agent calls while it
works.

STAGED EDITS. A t_impl agent's changes live in its staging directory until the
step's on_deliver repo_apply; the consolidated repo still holds the OLD code.
Play-testing `project_root` directly would therefore test the code the agent is
in the middle of replacing and report on it as if it were the fix — a wrong
answer delivered with a straight face. So when the step has staged files, this
tool assembles repo+staging into a throwaway tree (under ~/.AItelier/scratch, on
the mount the sidecar can read) and tests THAT, and it says in its result which
files were overlaid so the reader knows what ran.
"""

import json
import shutil
import sys
from pathlib import Path

_SKIP_NAMES = {".gitkeep", "_snapshot.json", "_deletions.json"}
_MAX_ASSERT_LINES = 200


def _scratch_root(step_id: str, run_id: str) -> Path:
    project_root = Path(__file__).parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from core import datadir
    import re
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(step_id or run_id or "adhoc"))[:80]
    return datadir.scratch_dir() / "playtest_scenario" / key


def _staged_files(step_tmp_dir: str) -> list[Path]:
    """The files this step has written but not yet delivered to the repo."""
    d = Path(step_tmp_dir) if step_tmp_dir else None
    if not d or not d.is_dir():
        return []
    return [f for f in sorted(d.rglob("*"))
            if f.is_file() and f.name not in _SKIP_NAMES and ".git/" not in str(f)]


def _overlay_tree(repo: Path, staged: list[Path], src_root: Path,
                  dest: Path) -> list[str]:
    """repo + staged files → a throwaway project tree at ``dest``."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(repo, dest,
                    ignore=shutil.ignore_patterns(".git", ".godot", "frames"))
    applied = []
    for f in staged:
        rel = f.relative_to(src_root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        applied.append(str(rel))
    return applied


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


def godot_playtest_scenario(*, scenario: str = "", project_root: str = "",
                            workspace_root: str = "", step_tmp_dir: str = "",
                            step_id: str = "", run_id: str = "",
                            use_staged: bool = True, **kwargs) -> dict:
    """Play-test the named scenario(s) only. ``scenario`` is one name or a
    comma-separated list. Returns the per-assertion outcome as text."""
    from aitelier.tools.godot_playtest.impl import (post_playtest, read_spec,
                                                    select_scenarios)

    repo = Path(project_root or workspace_root).resolve()
    if not (repo / "project.godot").is_file():
        return {"error": f"No project.godot at {repo} — not a Godot project."}

    spec, info = read_spec(repo)
    if info["errors"]:
        return {"error": "The play-test contract could not be read whole: "
                         + " | ".join(info["errors"])}
    if not spec:
        return {"error": f"No play-test contract in {repo} (expected a "
                         f"playtest/ directory or playtest_spec.yaml)."}

    names = [n.strip() for n in str(scenario).split(",") if n.strip()]
    available = sorted(str(s.get("name")) for s in spec["scenarios"])
    if not names:
        return {"error": "scenario is required — name one (or several, comma-"
                         f"separated) of: {', '.join(available)}"}
    picked, unknown = select_scenarios(spec, names)
    if unknown:
        # Never run the recognised subset and report on it: a typo would then
        # read as "the scenario I asked about is green".
        return {"error": f"unknown scenario(s) {unknown}. Available: "
                         f"{', '.join(available)}"}

    staged = _staged_files(step_tmp_dir) if use_staged else []
    scratch = _scratch_root(step_id, run_id)
    target = repo
    overlaid: list[str] = []
    try:
        if staged:
            target = scratch / "project"
            overlaid = _overlay_tree(repo, staged, Path(step_tmp_dir), target)

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
        header = [f"ran {len(results)} scenario(s) against "
                  + ("repo + %d staged file(s): %s"
                     % (len(overlaid), ", ".join(overlaid[:20]))
                     if overlaid else "the consolidated repo (no staged edits)"),
                  f"spec source: {info['source']}",
                  f"hard gate passed: {report.get('passed')} — "
                  f"{report.get('summary', '')}",
                  ""]
        return {
            "scenarios": results,
            "all_passed": bool(results) and all(r["passed"] for r in results),
            "hard_passed": bool(report.get("passed")),
            "staged_files_applied": overlaid,
            "report": "\n".join(header + _render(scen)),
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
