"""A measured red is never recorded as "did not run".

Two shapes did exactly that on the r2 candidate (review gnr2, 2026-09-25):

  * pole 3 — the gate measured a red, carried on, and a LATER engine request
    was refused, so it exited 2 with the relay's refusal on record. The
    findings were in the report it retained, and the run read `unmeasured`.
  * pole 4 — AItelier's own pytest was red and the gate was refused. The
    report set `repo_gate_absent`, so the run parked at the absence gate and
    ended "gate did not run" with a pytest red on file.

Every test below serves the REAL harness admission code
(`tests/gate_fixture.py:HarnessRig`) and runs the REAL `run_tests`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aitelier import gate_admission
from aitelier.tools.run_tests import impl as rt
from core import gate_deferral
from tests.gate_fixture import RUN_TESTS_SH, HarnessRig, write_gate


# A gate with the game gate's report layout that can be told to measure a red
# BEFORE its engine request: a python-stage red with a findings file, or a
# /compile answered with a parse error (a stage report with `passed: false`
# and no findings file). Its /script request then meets a busy render lock.
RED_THEN_ASK_GATE = r'''
import json, os, sys, tempfile, urllib.error, urllib.request
mode = os.environ["RED_GATE_MODE"]
builder = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")
parent = os.environ.get("GATE_REPORT_DIR") or tempfile.gettempdir()
os.makedirs(parent, exist_ok=True)
d = tempfile.mkdtemp(prefix="red-gate-", dir=parent)
manifest = {"repo": os.getcwd(), "status": "incomplete", "stages": {}}
def write(name, v):
    with open(os.path.join(d, name), "w") as fh:
        json.dump(v, fh, indent=2)
def post(path, payload):
    req = urllib.request.Request(builder.rstrip("/") + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
def finish(code):
    manifest.update(exit_code=code)
    write("manifest.json", manifest)
    sys.exit(code)
base = {"project_dir": os.getcwd(), "project_id": "red", "run_id": "red",
        "operation_id": "red-gate", "scripts": ["res://t.gd"]}
if mode == "python_red":
    write("python.json", {"returncode": 1})
    write("python-findings.json", ["the python suite exited 1"])
    manifest["stages"]["python"] = {"status": "finished", "report": "python.json"}
if mode == "compile_red":
    rep = post("/compile", base)
    write("compile.json", rep)
    manifest["stages"]["compile"] = {"status": "response_received",
                                     "report": "compile.json"}
write("manifest.json", manifest)
try:
    rep = post("/script", base)
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print("godot-builder unreachable at %s: %s -- gate NOT run." % (builder, exc),
          file=sys.stderr)
    finish(2)
finish(0)
'''


@pytest.fixture
def busy(tmp_path, monkeypatch):
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    rig.hold()
    yield rig
    rig.close()


def _red_then_ask_repo(tmp_path, monkeypatch, mode):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (repo / "gate.py").write_text(RED_THEN_ASK_GATE)
    script = repo / "run_tests.sh"
    script.write_text(RUN_TESTS_SH)
    script.chmod(0o755)
    monkeypatch.setenv("RED_GATE_MODE", mode)
    return repo


def _run(repo, out):
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = json.loads((Path(out) / "test_report.json").read_text())
    return result, report


# ── pole 3: a red the gate retained beats the refusal that followed it ──────

@pytest.mark.parametrize("mode", ["python_red", "compile_red"])
def test_pole_3_a_retained_red_then_a_refusal_is_a_measured_red(
        busy, tmp_path, monkeypatch, mode):
    if mode == "compile_red":
        monkeypatch.setattr(busy.gh, "compile_project", lambda _p: {
            "passed": False, "summary": "1 error",
            "errors": [{"file": "res://a.gd", "line": 3,
                        "msg": "Parse Error: probe"}]})
    repo = _red_then_ask_repo(tmp_path, monkeypatch, mode)
    result, report = _run(repo, tmp_path / "out")
    gate = report["repo_gate"]

    # The facts that made r2 read this as "not run" are all still present.
    assert gate["returncode"] == 2
    assert gate["admission"]["state"] == gate_admission.NOT_ADMITTED
    assert gate["admission"]["requests"][-1]["status"] == 409
    # And the retained report outranks them.
    assert gate["retained_findings"] == 1
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert report["passed"] is False
    assert "repo_gate_absent" not in report
    assert result["repo_gate_absent"] is False
    assert "repo_gate_unmeasured" not in report
    assert report.get("evidence_state") != "not_run"
    assert "failure_identity_error" not in report
    (case,) = gate["failure_cases"]
    stage = "python" if mode == "python_red" else "compile"
    assert case["case_id"].startswith(stage + "/")
    assert any(f.startswith(f"repo_gate:run_tests.sh#{stage}/")
               for f in report["failures"]), report["failures"]
    assert gate_deferral.read_absence(Path(tmp_path / "out" / "test_report.json")) is None


def test_the_same_refusal_with_no_retained_red_is_still_not_run(
        busy, tmp_path, monkeypatch):
    """The other pole of the same gate: nothing measured before the refusal."""
    repo = _red_then_ask_repo(tmp_path, monkeypatch, "clean")
    result, report = _run(repo, tmp_path / "out")
    gate = report["repo_gate"]
    assert gate["returncode"] == 2
    assert gate["retained_findings"] == 0
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_absent"] is True
    assert result["repo_gate_absent"] is True
    assert report["evidence_state"] == "not_run"


@pytest.mark.parametrize("source", [
    {"unmeasured_declaration": {"state": "blocked", "reason": "x"}},
    {"timed_out": True},
    {"runner_error": True},
    {"returncode": 2, "admission": {"state": gate_admission.NOT_ADMITTED}},
    {"returncode": 2, "admission": {"state": gate_admission.UNREACHABLE}},
    {"returncode": 0},
])
def test_a_retained_finding_outranks_every_source_of_unmeasured(source):
    gate = {"returncode": 2, **source}
    assert rt._repo_gate_outcome({**gate, "retained_findings": 1}) == \
        rt.REPO_GATE_MEASURED_FAIL
    # Without the finding each of these reads as it did before.
    expected = (rt.REPO_GATE_MEASURED_PASS if source == {"returncode": 0}
                else rt.REPO_GATE_UNMEASURED)
    for none in (0, None):
        assert rt._repo_gate_outcome({**gate, "retained_findings": none}) == expected


# ── pole 4: a pytest red beside a refused gate goes back to implement ──────

def test_pole_4_a_pytest_red_beside_a_refused_gate_is_not_an_absence(
        busy, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (repo / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n")
    write_gate(repo)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "30")
    result, report = _run(repo, tmp_path / "out")
    gate = report["repo_gate"]

    assert gate["returncode"] == 2
    assert gate["admission"]["state"] == gate_admission.NOT_ADMITTED
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_unmeasured"] is True
    # The gate measured nothing, but the report is a measured red.
    assert report["repo_gate_absent"] is False
    assert result["repo_gate_absent"] is False
    assert report["passed"] is False
    assert report.get("evidence_state") != "not_run"
    assert any("test_bad" in f for f in report["failures"]), report["failures"]
    assert any("was NOT measured" in f for f in report["failures"])
    # The scheduler does not wait on it either.
    assert gate_deferral.read_absence(tmp_path / "out" / "test_report.json") is None


def test_a_pytest_killed_at_its_wall_beside_a_refused_gate_is_not_an_absence(
        busy, tmp_path, monkeypatch):
    """Any other entry in `failures[]` keeps the report out of the absence
    gate, the pytest wall included: re-running the gate does not unhang a
    suite."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(60)\n")
    write_gate(repo)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "30")
    monkeypatch.setattr(rt, "PYTEST_WALL_SECONDS", 3)
    result, report = _run(repo, tmp_path / "out")
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert any(f.startswith("pytest:timed out") for f in report["failures"])
    assert report["repo_gate_absent"] is False
    assert result["repo_gate_absent"] is False
    assert gate_deferral.read_absence(tmp_path / "out" / "test_report.json") is None


def test_a_green_pytest_beside_a_refused_gate_is_an_absence(
        busy, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    monkeypatch.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "30")
    result, report = _run(repo, tmp_path / "out")
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_absent"] is True
    assert result["repo_gate_absent"] is True
    assert gate_deferral.read_absence(tmp_path / "out" / "test_report.json") == {
        "gate": "run_tests.sh"}


@pytest.mark.parametrize("flags, absent", [
    ({"repo_gate_absent": True, "repo_gate_unmeasured": True}, True),
    ({"repo_gate_absent": False, "repo_gate_unmeasured": True}, False),
    ({"repo_gate_unmeasured": True}, True),
    ({"repo_gate_absent": False}, False),
    ({}, False),
])
def test_read_absence_follows_repo_gate_absent_when_it_is_there(
        tmp_path, flags, absent):
    path = tmp_path / "test_report.json"
    path.write_text(json.dumps({"passed": False, **flags}))
    assert (gate_deferral.read_absence(path) is not None) is absent
