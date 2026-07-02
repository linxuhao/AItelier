# tests/unit/test_run_tests_gate.py
# run_tests gate: real run when pytest is present; graceful SKIP (not fail) when
# the runner can't be provisioned — so a missing test runner never masquerades
# as failing tests and spins the goal-loop.
import json
import tempfile
from pathlib import Path

import aitelier.tools.run_tests.impl as rt


def _report(out_dir):
    return json.loads((Path(out_dir) / "test_report.json").read_text())


def test_runs_when_pytest_present():
    repo = Path(tempfile.mkdtemp())
    (repo / "test_x.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    out = Path(tempfile.mkdtemp())
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert res["passed"] is True
    assert rep["returncode"] == 0
    assert not rep.get("skipped")


def test_skips_when_runner_unavailable(monkeypatch):
    """pytest not importable AND provisioning fails on EVERY retry → SKIP, not fail."""
    monkeypatch.setattr(rt.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(rt.time, "sleep", lambda *_: None)  # no real backoff in tests

    calls = []
    def boom(*a, **k):
        calls.append(a)
        raise OSError("no network / venv blocked")
    monkeypatch.setattr(rt.subprocess, "run", boom)

    repo = Path(tempfile.mkdtemp())
    (repo / "test_x.py").write_text("def test_ok():\n    assert True\n")
    out = Path(tempfile.mkdtemp())
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert res["passed"] is True          # gate does NOT fail the run
    assert rep["skipped"] is True
    assert "skipped" in rep["summary"].lower()
    assert "3 attempts" in rep["summary"]  # retried before giving up
    assert len(calls) == 3                 # one provisioning attempt per retry


def test_provisioning_retries_on_transient_failure(monkeypatch):
    """A transient blip on the first attempt is retried; the next attempt's
    success provisions the runner instead of skipping the gate."""
    monkeypatch.setattr(rt.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(rt.time, "sleep", lambda *_: None)

    n = {"calls": 0}
    def flaky(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:               # first attempt's venv-create blips
            raise OSError("transient network blip")
        class _R:                          # everything after succeeds
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()
    monkeypatch.setattr(rt.subprocess, "run", flaky)

    report = {"passed": True, "returncode": 0, "summary": "", "failures": []}
    py, venv_dir = rt._resolve_pytest_python(Path(tempfile.mkdtemp()), report)
    assert py is not None                  # recovered on retry — did NOT skip
    assert not report.get("skipped")
    assert n["calls"] >= 2                 # first attempt failed, then retried


def test_missing_repo_fails():
    out = Path(tempfile.mkdtemp())
    res = rt.run_tests(project_root="/nonexistent/repo/path", out_dir=str(out))
    assert res["passed"] is False


# ── provisioning: install the project's declared deps (B) ──────────────────

def _capture_pip(monkeypatch):
    """Capture the pip command _install_project_deps issues (or None)."""
    calls = []
    monkeypatch.setattr(rt.subprocess, "run",
                        lambda cmd, *a, **k: calls.append(cmd))
    return calls


def test_install_deps_prefers_requirements(monkeypatch):
    calls = _capture_pip(monkeypatch)
    repo = Path(tempfile.mkdtemp())
    (repo / "requirements.txt").write_text("pytest\n")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")  # ignored
    rt._install_project_deps("py", repo)
    assert calls and "-r" in calls[0] and str(repo / "requirements.txt") in calls[0]


def test_install_deps_falls_back_to_editable_pyproject(monkeypatch):
    calls = _capture_pip(monkeypatch)
    repo = Path(tempfile.mkdtemp())
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    rt._install_project_deps("py", repo)
    assert calls and "-e" in calls[0] and str(repo) in calls[0]


def test_install_deps_noop_without_dep_files(monkeypatch):
    calls = _capture_pip(monkeypatch)
    rt._install_project_deps("py", Path(tempfile.mkdtemp()))  # empty repo
    assert calls == []


def test_install_deps_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("pip blew up")
    monkeypatch.setattr(rt.subprocess, "run", boom)
    repo = Path(tempfile.mkdtemp())
    (repo / "requirements.txt").write_text("pytest\n")
    rt._install_project_deps("py", repo)  # must not raise


def test_timeout_args_gated_on_plugin(monkeypatch):
    # current interpreter path: keyed on find_spec
    monkeypatch.setattr(rt.importlib.util, "find_spec",
                        lambda name: object() if name == "pytest_timeout" else None)
    assert rt._pytest_timeout_args(rt.sys.executable) == [
        "--timeout=60", "--timeout-method=thread"]
    monkeypatch.setattr(rt.importlib.util, "find_spec", lambda name: None)
    assert rt._pytest_timeout_args(rt.sys.executable) == []


# ── Node gate (npm install/build/test) ──────────────────────────────
# The old pipeline ran ONLY pytest — two dogfood runs verified green with a
# frontend that didn't compile. The node gate finds package.json (root or one
# level deep, e.g. web/) and folds npm failures into the same report.


def _node_repo(tmp_path, subdir="web", scripts=None, lockfile=True):
    repo = tmp_path / "repo"
    pkg_dir = repo / subdir if subdir else repo
    pkg_dir.mkdir(parents=True)
    pkg = {"name": "x", "version": "0.0.0"}
    if scripts is not None:
        pkg["scripts"] = scripts
    (pkg_dir / "package.json").write_text(json.dumps(pkg))
    if lockfile:
        (pkg_dir / "package-lock.json").write_text("{}")
    return repo, pkg_dir


def _fake_npm(tmp_path, monkeypatch, exit_codes=None):
    """Put a fake `npm` on PATH that logs its args and exits per-command."""
    import os
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "npm_calls.log"
    codes = exit_codes or {}
    script = ["#!/bin/bash", f'echo "$@" >> "{log}"']
    for key, code in codes.items():
        script.append(f'[[ "$*" == "{key}"* ]] && exit {code}')
    script.append("exit 0")
    npm = bin_dir / "npm"
    npm.write_text("\n".join(script) + "\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return log


def test_node_gate_finds_subdir_package(tmp_path, monkeypatch):
    repo, _ = _node_repo(tmp_path, subdir="web",
                         scripts={"build": "x", "test": "y"})
    log = _fake_npm(tmp_path, monkeypatch)
    node = rt._run_node_checks(repo)
    assert node is not None and node["passed"] is True
    assert node["dir"] == "web"
    calls = log.read_text()
    assert "ci" in calls and "run build" in calls and "test" in calls


def test_node_gate_absent_without_package_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")
    assert rt._run_node_checks(repo) is None


def test_node_gate_skips_without_npm(tmp_path, monkeypatch):
    repo, _ = _node_repo(tmp_path)
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    node = rt._run_node_checks(repo)
    assert node["skipped"] is True
    assert node["passed"] is True  # missing runner must not fail the gate


def test_node_build_failure_fails_report(tmp_path, monkeypatch):
    repo, _ = _node_repo(tmp_path, scripts={"build": "x", "test": "y"})
    _fake_npm(tmp_path, monkeypatch, exit_codes={"run build": 1})
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert rep["node"]["checks"]["build"]["passed"] is False
    assert rep["passed"] is False
    assert res["passed"] is False
    assert any(f.startswith("node:build") for f in rep["failures"])


def test_node_skips_scripts_it_does_not_have(tmp_path, monkeypatch):
    repo, _ = _node_repo(tmp_path, scripts={})  # no build/test scripts
    log = _fake_npm(tmp_path, monkeypatch)
    node = rt._run_node_checks(repo)
    assert node["passed"] is True
    assert set(node["checks"]) == {"install"}
    assert "run build" not in log.read_text()
