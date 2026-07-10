"""godot_compile — parse-check the project's GDScript over the whole repo.

Used as a tool STEP after the final verifier (mirrors run_tests / the old
unity_compile). Godot has no ahead-of-time compile, but importing the project
parse-checks EVERY script and .tscn together and surfaces parse errors / broken
resource references — the whole-repo check scripts cross-reference each other
need. Runs via the license-free ``godot-builder`` sidecar (docker/godot/
godot_harness.py). The outcome lands in ``compile_report.json`` for 5_review to
fold into its verdict, so parse errors loop back through the goal-loop alongside
the verifier's semantic issues.

It ALWAYS succeeds as a step:
- No ``project.godot`` in the repo → not a Godot project → pass without touching
  the builder (Python/web projects never need it).
- Builder unreachable → pass with a LOUD ``gate_skipped`` note rather than
  stalling the pipeline on an infra problem (a missing sidecar is not a code
  defect — but the code shipped UNVERIFIED, so 5_review must see it).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")


def _is_godot(repo: Path) -> bool:
    return (repo / "project.godot").is_file()


def godot_compile(*, project_root: str = "", out_dir: str = "",
                  workspace_root: str = "", **kwargs) -> dict:
    """Parse-check the repo's GDScript via godot-builder, then (if it passed)
    play-test it. Writes compile_report.json always, and playtest_report.json
    always. Returns {written, passed}."""
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "returncode": 0, "file_count": 0,
              "errors": [], "warning_count": 0, "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not _is_godot(repo):
        report["summary"] = "No project.godot — not a Godot project; compile skipped."
    else:
        body = json.dumps({"project_dir": str(repo)}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/compile", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            # Infra problem, not a code defect → don't fail the run, but flag it
            # LOUDLY: this branch only runs when the repo IS a Godot project, so a
            # skip here means real GDScript shipped UNVERIFIED. gate_skipped lets
            # 5_review surface that instead of reading a bare passed:true as clean.
            report["gate_skipped"] = True
            report["summary"] = (
                f"godot-builder unreachable ({_BUILDER_URL}): {e}. "
                "Compile gate skipped — GDScript NOT verified.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "compile_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    # ── Chain the headless play-test (compile → if passed → playtest) ──
    # Play-testing code that didn't parse is pointless — the scene load would
    # fail and pile a redundant failure on top of the parse errors.
    if report.get("passed", True) and _is_godot(repo):
        from aitelier.tools.godot_playtest.impl import godot_playtest
        pt = godot_playtest(project_root=str(repo), out_dir=str(target_dir))
        pt_passed = pt.get("passed", True)
    else:
        reason = ("Parse failed — play-test skipped (fix parse errors first)."
                  if not report.get("passed", True)
                  else "No project.godot — not a Godot project; play-test skipped.")
        (target_dir / "playtest_report.json").write_text(json.dumps(
            {"passed": True, "frames": 0, "errors": [], "state": {},
             "summary": reason}, indent=2), encoding="utf-8")
        pt_passed = True

    return {"written": ["compile_report.json", "playtest_report.json"],
            "passed": report.get("passed", True) and pt_passed}
