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


# ── collection errors: ALL of them, in one run ──────────────────────
# NL2Repo sweep 2026-08-18: one unimportable module INTERRUPTS the session, so
# the report was a single traceback and zero test results even where the other
# 1600 tests would have run. 5_review can only open a fix-task per defect it can
# see, and the `5_review → 3` goal loop is capped at two laps — so a report that
# reveals one defect per run cannot clear three.


def _broken_repo(tmp_path, n_broken=2, with_good=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(n_broken):
        (repo / f"test_broken_{i}.py").write_text(
            f"from missing_pkg_{i} import thing\n\ndef test_x():\n    assert thing()\n")
    if with_good:
        (repo / "test_good.py").write_text("def test_ok():\n    assert True\n")
    return repo


def test_every_collection_error_is_reported_not_just_the_first(tmp_path):
    repo = _broken_repo(tmp_path, n_broken=3)
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)

    assert res["passed"] is False
    assert len(rep["collection_errors"]) == 3, rep["collection_errors"]
    for i in range(3):
        assert any(f"test_broken_{i}.py" in e for e in rep["collection_errors"])
        # the CAUSE travels with the file — pytest's own short summary prints
        # only "ERROR test_broken_0.py", which names no defect to fix
        assert any(f"missing_pkg_{i}" in e for e in rep["collection_errors"])


def test_collection_errors_lead_the_summary_instead_of_being_tailed_off(tmp_path):
    repo = _broken_repo(tmp_path, n_broken=2)
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)

    head = rep["summary"][:400]
    assert "could not be imported" in head
    assert "test_broken_0.py" in head and "test_broken_1.py" in head
    # and they are reachable as failures, which is what 5_review iterates
    assert all(e in rep["failures"] for e in rep["collection_errors"])


def test_importable_tests_still_run_alongside_a_broken_module(tmp_path):
    """The point of --continue-on-collection-errors: one bad module no longer
    reduces the whole suite to zero results."""
    repo = _broken_repo(tmp_path, n_broken=1, with_good=True)
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert rep["collection_errors"]
    assert "1 passed" in rep["summary"]


def test_clean_repo_reports_no_collection_errors(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert res["passed"] is True
    assert rep["collection_errors"] == []
    assert "could not be imported" not in rep["summary"]


# ── test extras: the deps the project declares for its own tests ─────
# NL2Repo sweep 2026-08-18: `pip install -e .` never installs
# [project.optional-dependencies], so test-only deps (asgi_lifespan, freezegun,
# …) were missing in 6 of 12 audited runs. The gate then died on an ENVIRONMENT
# fault no DPE role can fix, six lines above the real defect.


def _pyproject_with_extras(repo: Path, groups: dict):
    body = "[project]\nname='x'\n\n[project.optional-dependencies]\n"
    for name, deps in groups.items():
        body += f"{name} = {deps!r}\n"
    (repo / "pyproject.toml").write_text(body)


def test_install_deps_adds_declared_test_extras(monkeypatch):
    calls = _capture_pip(monkeypatch)
    repo = Path(tempfile.mkdtemp())
    _pyproject_with_extras(repo, {"test": ["asgi_lifespan"], "dev": ["ruff"]})
    rt._install_project_deps("py", repo)
    assert calls and calls[0][-1] == f"{repo}[test,dev]"


def test_install_deps_only_requests_extras_the_project_declares(monkeypatch):
    """pip errors on an unknown extra — so guessing `[test]` blindly would break
    the install for every project that doesn't declare one."""
    calls = _capture_pip(monkeypatch)
    repo = Path(tempfile.mkdtemp())
    _pyproject_with_extras(repo, {"docs": ["sphinx"]})   # nothing test-shaped
    rt._install_project_deps("py", repo)
    assert calls and calls[0][-1] == str(repo)


def test_install_deps_falls_back_to_plain_editable_when_extras_fail(monkeypatch):
    """A broken extra must cost nothing: degrade to today's behaviour."""
    calls = []

    class _Proc:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", ""

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Proc(1 if "[" in cmd[-1] else 0)   # extras fail, plain succeeds

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    repo = Path(tempfile.mkdtemp())
    _pyproject_with_extras(repo, {"test": ["nope-does-not-exist"]})
    assert rt._install_project_deps("py", repo) == ""   # not an install failure
    assert len(calls) == 2 and calls[1][-1] == str(repo)


def test_install_deps_unreadable_pyproject_is_not_fatal(monkeypatch):
    calls = _capture_pip(monkeypatch)
    repo = Path(tempfile.mkdtemp())
    (repo / "pyproject.toml").write_text("this is not [ valid toml")
    rt._install_project_deps("py", repo)
    assert calls and calls[0][-1] == str(repo)   # behaves exactly as before


# ── no evidence is not a pass ───────────────────────────────────────
# `autopep8` benchmark task: the implementer never delivered the test dir, the
# gate collected nothing, reported passed:true, 5_review passed on that basis,
# and a 0.128-scoring repo shipped as `completed`. A gate that checked NOTHING
# has not passed.


def test_no_tests_collected_does_not_pass(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")   # code, no tests
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)

    assert rep["returncode"] == 5
    assert rep["no_tests_collected"] is True
    assert rep["passed"] is False and res["passed"] is False
    assert "not verified" in rep["summary"] or "verified" in rep["summary"]
    assert any("collected" in f for f in rep["failures"])


def test_no_python_sources_means_not_applicable_not_failure(tmp_path):
    """A Godot/node project has no pytest suite by construction — failing it
    would spin the goal loop on something no task can fix. Its own gate
    (node / compile section) is the real one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.gd").write_text("extends Node\n")
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)

    assert rep["returncode"] == 5
    assert rep["no_tests_collected"] is True
    assert rep["passed"] is True and res["passed"] is True
    assert "not applicable" in rep["summary"]


# ── import smoke check: does the delivered package even load? ────────
# NL2Repo `fastapi-users`: one wrong import (`SecurityBase` from
# `fastapi.security`) made conftest.py unimportable, pytest exited 4 before
# collection and all 556 cases scored zero. A prior attempt on the same task
# died on a bare NameError. Both are `python -c "import <pkg>"`.


def _pkg_repo(tmp_path, body, dist_name="broken-pkg", mod="broken_pkg"):
    repo = tmp_path / "repo"
    (repo / mod).mkdir(parents=True)
    (repo / "pyproject.toml").write_text(f"[project]\nname='{dist_name}'\n")
    (repo / mod / "__init__.py").write_text(body)
    # a green test alongside it: pytest exits 0, so ONLY the smoke check sees it
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return repo


def test_unimportable_package_fails_even_when_pytest_is_green(tmp_path):
    repo = _pkg_repo(tmp_path, "AP\n")   # NameError at import time
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)

    assert rep["returncode"] == 0          # pytest itself was happy
    assert rep["passed"] is False and res["passed"] is False
    assert "NameError" in rep["import_error"]
    assert rep["summary"].startswith("The delivered package does not import")
    assert rep["import_error"] in rep["failures"]


def test_importable_package_is_silent(tmp_path):
    repo = _pkg_repo(tmp_path, "VALUE = 1\n")
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(repo), out_dir=str(out))
    rep = _report(out)
    assert res["passed"] is True
    assert "import_error" not in rep


def test_smoke_check_skipped_when_the_module_cannot_be_identified(tmp_path):
    """A dist name that matches nothing in the tree is a name we'd only guess
    wrong with — skip rather than fabricate a failure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='nothing-here'\n")
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    assert rt._package_module(repo) is None
    out = tmp_path / "out"
    assert rt.run_tests(project_root=str(repo), out_dir=str(out))["passed"] is True


def test_package_module_normalises_the_distribution_name(tmp_path):
    """`Broken.Pkg` on the dist side is `broken_pkg` on the import side."""
    repo = _pkg_repo(tmp_path, "VALUE = 1\n", dist_name="Broken.Pkg")
    # .lower(): a case-insensitive filesystem resolves the un-lowered candidate
    # first, and either spelling imports there.
    assert rt._package_module(repo).lower() == "broken_pkg"
