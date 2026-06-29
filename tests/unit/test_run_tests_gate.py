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
    """pytest not importable AND venv provisioning fails → SKIP, not fail."""
    monkeypatch.setattr(rt.importlib.util, "find_spec", lambda name: None)

    def boom(*a, **k):
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
