"""run_tests — execute the project's unit tests and write a test report.

Used as a tool STEP after the final verifier. It ALWAYS succeeds (so a failing
test never fails the run); the outcome is captured in ``test_report.json`` so the
verifier-review step can fold test failures into its change requests and loop
back to the planner (the goal-loop).
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", **kwargs) -> dict:
    """Run pytest over the consolidated repo; write test_report.json to out_dir.

    Returns {written, passed}. The report holds {passed, returncode, summary,
    failures[]} for the reviewer to read.
    """
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "returncode": 0, "summary": "", "failures": []}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    else:
        env = {**os.environ,
               "PYTHONPATH": os.pathsep.join(
                   [str(repo), os.environ.get("PYTHONPATH", "")])}
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", str(repo), "-q", "--tb=short",
                 "-p", "no:cacheprovider"],
                capture_output=True, text=True, timeout=180, cwd=str(repo), env=env,
            )
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            report["returncode"] = r.returncode
            # pytest: 0=all passed, 5=no tests collected (not a failure), 1=failures
            report["passed"] = r.returncode in (0, 5)
            report["failures"] = [ln.strip() for ln in out.splitlines()
                                  if ln.startswith("FAILED") or " FAILED " in ln][:50]
            report["summary"] = ("No tests were collected." if r.returncode == 5
                                 else out[-3000:])
        except subprocess.TimeoutExpired:
            report.update(passed=False, summary="pytest timed out after 180s")
        except Exception as e:  # never raise — the step must not fail
            report.update(passed=False, summary=f"Error running pytest: {e}")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "test_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "test_report.json", "passed": report["passed"]}
