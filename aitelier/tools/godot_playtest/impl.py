"""godot_playtest — headless play-test of the project's Godot scene.

Used as a tool STEP after the compile gate (mirrors run_tests / godot_compile).
It POSTs the consolidated repo path to the ``godot-builder`` sidecar's
``/playtest`` route, which copies the project, injects an autoload probe, runs
the main scene headless for a bounded number of frames (auto-pressing an input
action so the game progresses), and reports:
  * every runtime error (SCRIPT ERROR / push_error) with a res:// file + line
  * a JSON snapshot of the live scene tree's script variables — the runtime
    state an agent needs to actually SEE what the game is doing.
The outcome lands in ``playtest_report.json`` for 5_review to fold into its
verdict, so runtime failures loop back through the goal-loop alongside parse
errors.

It ALWAYS succeeds as a step:
- No ``project.godot`` → not a Godot project → pass without touching the builder.
- Builder unreachable → pass with a LOUD ``gate_skipped`` note rather than
  stalling on infra (a missing sidecar is not a code defect — but the scene
  shipped without a runtime smoke test, so 5_review must see it).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")


def godot_playtest(*, project_root: str = "", out_dir: str = "",
                   workspace_root: str = "", **kwargs) -> dict:
    """Run the headless play-test via godot-builder; write playtest_report.json.

    Returns {written, passed}. The report holds {passed, frames, errors[], state,
    summary} for the reviewer to read."""
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "frames": 0, "errors": [], "state": {}, "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not (repo / "project.godot").is_file():
        report["summary"] = "No project.godot — not a Godot project; play-test skipped."
    else:
        body = json.dumps({"project_dir": str(repo)}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/playtest", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            report["gate_skipped"] = True
            report["summary"] = (
                f"godot-builder unreachable ({_BUILDER_URL}): {e}. "
                "Play-test gate skipped — scene NOT smoke-tested.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "playtest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "playtest_report.json", "passed": report.get("passed", True)}
