"""run_tests — execute the project's unit tests and write a test report.

Used as a tool STEP after the final verifier. It ALWAYS succeeds (so a failing
test never fails the run); the outcome is captured in ``test_report.json`` so the
verifier-review step can fold test failures into its change requests and loop
back to the planner (the goal-loop).

Runner resolution: prefer pytest in the current interpreter; if it is missing
(the Docker backend ships no test deps), provision a throwaway venv with
``--system-site-packages`` (so it inherits whatever IS installed) and install
the test toolchain — pytest + pytest-asyncio (REQUIRED by ``asyncio_mode=auto``
configs; without it every async test errors out) + pytest-timeout — plus the
project's declared dependencies (``requirements.txt``, or an editable install
that reads ``pyproject.toml``/``setup.py``). If the runner cannot be
provisioned at all (e.g. no network), the gate is SKIPPED (passed=True) — a
missing test runner must never masquerade as failing tests, which would spin
the goal-loop chasing a phantom failure.
"""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _kill_group(proc) -> None:
    """SIGKILL the process's whole session/group, then reap it.

    pytest spawns child processes (e.g. git subprocesses from the project's own
    test suite). subprocess timeout only kills the direct child, leaving the
    grandchildren orphaned → reparented to PID 1 → zombies. Launching pytest with
    start_new_session=True puts it in its own process group so we can take the
    whole tree down here.
    """
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _install_project_deps(venv_py: str, repo: Path) -> None:
    """Best-effort install of the project's declared deps into the venv.

    Tries ``requirements.txt``; else an editable install of the project itself
    (reads ``pyproject.toml`` ``[project.dependencies]`` / ``setup.py``). Never
    raises and never `check=True`s — the ``--system-site-packages`` base usually
    already satisfies imports, and a non-installable generated project (an app,
    not a package) must NOT fail the test gate.
    """
    try:
        if (repo / "requirements.txt").exists():
            cmd = [venv_py, "-m", "pip", "install", "-q", "-r",
                   str(repo / "requirements.txt")]
        elif any((repo / f).exists()
                 for f in ("pyproject.toml", "setup.py", "setup.cfg")):
            cmd = [venv_py, "-m", "pip", "install", "-q", "-e", str(repo)]
        else:
            return
        subprocess.run(cmd, capture_output=True, text=True,
                       timeout=300, check=False)
    except Exception:
        pass  # deps best-effort; base site-packages usually covers imports


def _pytest_timeout_args(py: str) -> list[str]:
    """Per-test timeout args, only if pytest-timeout is available for ``py``.

    Added unconditionally would make pytest error ("unrecognized arguments")
    on a host interpreter that lacks the plugin (e.g. the dev test interp).
    """
    try:
        if py == sys.executable:
            available = importlib.util.find_spec("pytest_timeout") is not None
        else:
            available = subprocess.run(
                [py, "-c", "import pytest_timeout"],
                capture_output=True, timeout=30).returncode == 0
        return ["--timeout=60", "--timeout-method=thread"] if available else []
    except Exception:
        return []


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
    #    only have to add the test toolchain, not reinstall the whole dep set).
    #    The pip install reaches PyPI, so a transient network blip would skip the
    #    gate (tests never run → false pass). Retry the whole provisioning a few
    #    times with backoff so only a PERSISTENT outage skips; a momentary blip
    #    recovers on the next attempt.
    attempts = 3
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        venv_dir = tempfile.mkdtemp(prefix="aitelier_pytest_venv_")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", venv_dir],
                capture_output=True, text=True, timeout=120, check=True,
            )
            venv_py = str(Path(venv_dir) / "bin" / "python")
            if not Path(venv_py).exists():  # windows / unusual layouts
                venv_py = str(Path(venv_dir) / "Scripts" / "python.exe")

            # Test toolchain: pytest + the plugins the project's pytest.ini
            # commonly requires. pytest-asyncio is mandatory for
            # `asyncio_mode = auto` (its absence makes every async test error);
            # pytest-timeout enables the per-test wall. pip already retries
            # individual downloads; the outer loop recovers from a blip that
            # exhausts pip's own retries.
            subprocess.run(
                [venv_py, "-m", "pip", "install", "-q",
                 "pytest", "pytest-asyncio", "pytest-timeout"],
                capture_output=True, text=True, timeout=300, check=True,
            )
            # Project's own deps — best-effort, must not skip the gate on failure.
            _install_project_deps(venv_py, repo)
            return venv_py, venv_dir
        except Exception as e:
            last_err = e
            shutil.rmtree(venv_dir, ignore_errors=True)
            if attempt < attempts:
                time.sleep(2 * attempt)  # 2s, then 4s, before retrying

    # All attempts failed → a persistent outage. SKIP (a missing runner must
    # never masquerade as failing tests, which would spin the goal-loop).
    report.update(
        passed=True, skipped=True, returncode=0,
        summary=(f"pytest unavailable and could not be provisioned after "
                 f"{attempts} attempts ({type(last_err).__name__}: "
                 f"{str(last_err)[:200]}) — test gate skipped."),
    )
    return None, None


def _find_node_project(repo: Path) -> Path | None:
    """Locate the repo's node project: package.json at the root, else the
    first one exactly one level deep (e.g. ``web/package.json`` — AItelier's
    own layout; the root-only check is how two dogfood runs verified green
    with a frontend that didn't even compile)."""
    if (repo / "package.json").exists():
        return repo
    candidates = sorted(
        p.parent for p in repo.glob("*/package.json")
        if "node_modules" not in p.parts
    )
    return candidates[0] if candidates else None


def _run_node_cmd(pkg_dir: Path, args: list[str], timeout: int) -> dict:
    """Run one npm command in its own process group; kill the tree on timeout."""
    proc = None
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(pkg_dir), start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        out = ((stdout or "") + "\n" + (stderr or "")).strip()
        return {"passed": proc.returncode == 0,
                "returncode": proc.returncode, "output": out[-2000:]}
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return {"passed": False, "returncode": -1,
                "output": f"timed out after {timeout}s: {' '.join(args)}"}
    except Exception as e:
        _kill_group(proc)
        return {"passed": False, "returncode": -1,
                "output": f"{type(e).__name__}: {e}"}


def _run_node_checks(repo: Path) -> dict | None:
    """npm install/build/test gate for the repo's node project (if any).

    Mirrors the pytest gate's skip semantics: no node project → None (no
    section in the report); npm binary unavailable → skipped=True (a missing
    runner must never masquerade as failing tests). Otherwise install deps,
    then run the build and test scripts that package.json actually declares —
    the BUILD is what catches compile-level breakage (e.g. Svelte template
    errors) that unit tests alone never see.
    """
    pkg_dir = _find_node_project(repo)
    if pkg_dir is None:
        return None

    node: dict = {"passed": True, "dir": str(pkg_dir.relative_to(repo)) or ".",
                  "checks": {}}

    if shutil.which("npm") is None:
        node.update(passed=True, skipped=True,
                    summary="npm not available — node gate skipped "
                            "(install nodejs+npm in the backend image).")
        return node

    try:
        scripts = json.loads(
            (pkg_dir / "package.json").read_text(encoding="utf-8")
        ).get("scripts", {})
    except Exception:
        scripts = {}

    # npm ci needs a lockfile; fall back to install without one.
    install_cmd = ["npm", "ci"] if (pkg_dir / "package-lock.json").exists() \
        else ["npm", "install"]
    node["checks"]["install"] = _run_node_cmd(pkg_dir, install_cmd, timeout=600)
    if node["checks"]["install"]["passed"]:
        if "build" in scripts:
            node["checks"]["build"] = _run_node_cmd(
                pkg_dir, ["npm", "run", "build"], timeout=300)
        if "test" in scripts:
            node["checks"]["test"] = _run_node_cmd(
                pkg_dir, ["npm", "test"], timeout=300)

    node["passed"] = all(c["passed"] for c in node["checks"].values())
    return node


def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", **kwargs) -> dict:
    """Run pytest over the consolidated repo; write test_report.json to out_dir.

    Returns {written, passed}. The report holds {passed, returncode, summary,
    failures[], skipped?} for the reviewer to read, plus a ``node`` section
    (npm install/build/test) when the repo contains a node project.
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
            # start_new_session=True → pytest leads its own process group so we
            # can SIGKILL the whole tree (incl. git subprocesses it spawns) on
            # timeout or any error; otherwise those grandchildren leak as zombies.
            proc = None
            try:
                # --rootdir forces pytest root to the project repo so it doesn't
                # walk up and find AItelier's pytest.ini (whose testpaths=tests
                # would cause discovery of AItelier's own test suite).
                proc = subprocess.Popen(
                    [py, "-m", "pytest", str(repo), "-q", "--tb=short",
                     "-p", "no:cacheprovider",
                     "--rootdir", str(repo), *_pytest_timeout_args(py)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    cwd=str(repo), env=env, start_new_session=True,
                )
                # Outer wall kept tight: this runs on the scheduler loop-thread
                # (under the per-project tick lock), so a long hang would stall
                # the whole run. A genuinely-passing suite finishes well under
                # this; an import/collection hang (not caught by pytest-timeout)
                # fails fast instead of blocking for minutes.
                stdout, stderr = proc.communicate(timeout=75)
                out = ((stdout or "") + "\n" + (stderr or "")).strip()
                report["returncode"] = proc.returncode
                # pytest: 0=all passed, 5=no tests collected (not a failure), 1=failures
                report["passed"] = proc.returncode in (0, 5)
                report["failures"] = [ln.strip() for ln in out.splitlines()
                                      if ln.startswith("FAILED") or " FAILED " in ln][:50]
                report["summary"] = ("No tests were collected." if proc.returncode == 5
                                     else out[-3000:])
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                report.update(passed=False, summary="pytest timed out after 75s")
            except Exception as e:  # never raise — the step must not fail
                _kill_group(proc)
                report.update(passed=False, summary=f"Error running pytest: {e}")
            finally:
                # Belt-and-suspenders: even on the success path pytest may leave
                # stray children — take the group down before cleaning up.
                if proc is not None:
                    _kill_group(proc)
                if venv_dir:
                    shutil.rmtree(venv_dir, ignore_errors=True)

    # Node gate (npm install/build/test) — folded into the same report so
    # 5_review loops frontend breakage back through the goal-loop exactly
    # like pytest failures.
    if repo.exists():
        node = _run_node_checks(repo)
        if node is not None:
            report["node"] = node
            if not node["passed"]:
                report["passed"] = False
                for name, chk in node["checks"].items():
                    if not chk["passed"]:
                        report["failures"].append(
                            f"node:{name} failed (rc={chk['returncode']}): "
                            f"{chk['output'][-500:]}")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "test_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "test_report.json", "passed": report["passed"]}
