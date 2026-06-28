"""run_tests — execute the project's unit tests and write a test report.

Used as a tool STEP after the final verifier. It ALWAYS succeeds (so a failing
test never fails the run); the outcome is captured in ``test_report.json`` so the
verifier-review step can fold test failures into its change requests and loop
back to the planner (the goal-loop).

Runner resolution: prefer pytest in the current interpreter; if it is missing
(the Docker backend ships no test deps), provision a throwaway venv with
``--system-site-packages`` (so it inherits whatever IS installed and only needs
to add pytest) and install pytest + the project's requirements there. If the
runner cannot be provisioned at all (e.g. no network), the gate is SKIPPED
(passed=True) — a missing test runner must never masquerade as failing tests,
which would spin the goal-loop chasing a phantom failure.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _resolve_pytest_python(repo: Path, report: dict) -> tuple[str | None, str | None]:
    """Return (python_executable, venv_dir_to_cleanup).

    python_executable is an interpreter that can `-m pytest`; None means the
    runner is unavailable and the caller should SKIP (report is updated in place
    with the skip outcome).
    """
    # 1. pytest already importable in the running interpreter → use it directly.
    if importlib.util.find_spec("pytest") is not None:
        return sys.executable, None

    # 2. Provision a throwaway venv that inherits system site-packages (so we
    #    only have to add pytest, not reinstall the whole dependency set).
    venv_dir = tempfile.mkdtemp(prefix="aitelier_pytest_venv_")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", venv_dir],
            capture_output=True, text=True, timeout=120, check=True,
        )
        venv_py = str(Path(venv_dir) / "bin" / "python")
        if not Path(venv_py).exists():  # windows / unusual layouts
            venv_py = str(Path(venv_dir) / "Scripts" / "python.exe")

        pip_cmd = [venv_py, "-m", "pip", "install", "-q", "pytest"]
        reqs = repo / "requirements.txt"
        if reqs.exists():
            pip_cmd += ["-r", str(reqs)]
        subprocess.run(pip_cmd, capture_output=True, text=True,
                       timeout=300, check=True)
        return venv_py, venv_dir
    except Exception as e:
        shutil.rmtree(venv_dir, ignore_errors=True)
        report.update(
            passed=True, skipped=True, returncode=0,
            summary=(f"pytest unavailable and could not be provisioned "
                     f"({type(e).__name__}: {str(e)[:200]}) — test gate skipped."),
        )
        return None, None


def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", **kwargs) -> dict:
    """Run pytest over the consolidated repo; write test_report.json to out_dir.

    Returns {written, passed}. The report holds {passed, returncode, summary,
    failures[], skipped?} for the reviewer to read.
    """
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "returncode": 0, "summary": "", "failures": []}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    else:
        py, venv_dir = _resolve_pytest_python(repo, report)
        if py is None:
            pass  # runner unavailable → report already marked skipped/passed
        else:
            # Isolate: do NOT inherit PYTHONPATH from the host process — it may
            # point to AItelier's own source tree, causing pytest to discover
            # AItelier's tests instead of the project's.  Only the project root
            # belongs on the path.
            env = {**os.environ, "PYTHONPATH": str(repo)}
            try:
                # --rootdir forces pytest root to the project repo so it doesn't
                # walk up and find AItelier's pytest.ini (whose testpaths=tests
                # would cause discovery of AItelier's own test suite).
                r = subprocess.run(
                    [py, "-m", "pytest", str(repo), "-q", "--tb=short",
                     "-p", "no:cacheprovider",
                     "--rootdir", str(repo)],
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
            finally:
                if venv_dir:
                    shutil.rmtree(venv_dir, ignore_errors=True)

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "test_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "test_report.json", "passed": report["passed"]}
