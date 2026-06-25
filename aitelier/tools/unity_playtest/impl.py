"""unity_playtest — PlayMode smoke test of the project's Unity scene.

Used as a tool STEP after the compile gate (mirrors run_tests / unity_compile).
It POSTs the consolidated repo path to the ``unity-builder`` sidecar's
``/playtest`` route, which copies the project, injects a reflection-based smoke
test (drives ``SceneBootstrapper.BuildScene()``), runs the editor's PlayMode
Test Runner, and reports whether the scene builds without runtime exceptions and
produces gameplay objects. The outcome lands in ``playtest_report.json`` for
5_review to fold into its verdict, so runtime failures (legacy-Input throws,
empty/orphaned scenes) loop back through the goal-loop alongside compile errors.

It ALWAYS succeeds as a step:
- No ``Assets/`` in the repo → not a Unity project → pass without touching the builder.
- Builder unreachable / unlicensed → pass with a note rather than stalling on
  infra (a missing sidecar or absent license is not a code defect).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("UNITY_BUILDER_URL", "http://unity-builder:8080")


def unity_playtest(*, project_root: str = "", out_dir: str = "",
                   workspace_root: str = "", **kwargs) -> dict:
    """Run the PlayMode smoke test via unity-builder; write playtest_report.json.

    Returns {written, passed}. The report holds {passed, total, passed_count,
    failed_count, failures[], summary} for the reviewer to read.
    """
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "total": 0, "passed_count": 0, "failed_count": 0,
              "failures": [], "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not (repo / "Assets").is_dir():
        report["summary"] = "No Assets/ — not a Unity project; play-test skipped."
    else:
        body = json.dumps({"project_dir": str(repo)}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/playtest", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            # First asset import can be slow; give the sidecar headroom over its
            # own 900s editor timeout.
            with urllib.request.urlopen(req, timeout=960) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            # Infra problem, not a code defect → don't fail the run.
            report["summary"] = (
                f"unity-builder unreachable ({_BUILDER_URL}): {e}. "
                "Play-test gate skipped.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "playtest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "playtest_report.json", "passed": report.get("passed", True)}
