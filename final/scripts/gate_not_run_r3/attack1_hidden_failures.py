"""Copy of the gnr2 reviewer's probe
(~/.AItelier/director/reports/gnr2-review-20260925/probes/attack1_hidden_failures.py),
with four more fields printed: `repo_gate.retained_findings`,
`repo_gate_unmeasured`, and per request `keepalives` / `admitted_after_sec`.
Poles: 3 = red_then_refused and answered_red_then_refused, 4 = pytest_red_refused.

Reviewer probe (gnr2 review, attack 1 + 2): can a gate that RAN and found
failures end up `unmeasured` / `repo_gate_absent`? Runs the REAL run_tests of
whichever tree is first on PYTHONPATH against the REAL harness admission code
(tests/gate_fixture.py:HarnessRig loaded by file path from the candidate tree;
the harness file is identical on base and candidate).

usage: python attack1_hidden_failures.py <fixture_py> <scenario>
"""
import importlib.util, json, os, sys, tempfile
from pathlib import Path
import pytest

fixture_path, scenario = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("gate_fixture_probe", fixture_path)
gf = importlib.util.module_from_spec(spec); spec.loader.exec_module(gf)
from aitelier.tools.run_tests import impl as rt

GATE = r'''
import json, os, sys, tempfile, urllib.error, urllib.request
mode = os.environ["PROBE_GATE_MODE"]
builder = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")
client_timeout = float(os.environ.get("PROBE_CLIENT_TIMEOUT", "60"))
parent = os.environ.get("GATE_REPORT_DIR") or tempfile.gettempdir()
os.makedirs(parent, exist_ok=True)
d = tempfile.mkdtemp(prefix="probe-gate-", dir=parent)
manifest = {"repo": os.getcwd(), "status": "incomplete", "stages": {}}
def write(name, v):
    open(os.path.join(d, name), "w").write(json.dumps(v, indent=2))
def post(path, payload):
    req = urllib.request.Request(builder.rstrip("/") + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=client_timeout) as r:
        return json.loads(r.read())
def finish(code):
    manifest.update(exit_code=code, status={0: "passed", 1: "failed"}.get(code, "incomplete"))
    write("manifest.json", manifest); sys.exit(code)
base = {"project_dir": os.getcwd(), "project_id": "probe", "run_id": "probe",
        "operation_id": "probe-gate", "scripts": ["res://t.gd"], "timeout": 60}
red = []
if mode == "red_then_refused":
    # a measured python-stage red FIRST, then an engine request that is refused
    write("python.json", {"returncode": 1}); write("python-findings.json", ["the python suite exited 1"])
    manifest["stages"]["python"] = {"status": "finished", "report": "python.json"}; write("manifest.json", manifest)
    print("python gate FAILED (continuing to the engine stages)"); red.append("python")
if mode == "answered_red_then_refused":
    rep = post("/compile", base); write("compile.json", rep)
    manifest["stages"]["compile"] = {"status": "response_received", "report": "compile.json"}; write("manifest.json", manifest)
    if not rep.get("passed"):
        print("compile gate FAILED (continuing)"); red.append("compile")
try:
    rep = post("/script", base)
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print("godot-builder unreachable at %s: %s -- gate NOT run." % (builder, exc), file=sys.stderr)
    if mode == "refused_then_crash":
        raise RuntimeError("probe: gate crashed while handling the refusal")
    if mode == "refused_then_sigkill":
        os.kill(os.getpid(), 9)
    finish(2)
write("script.json", rep)
manifest["stages"]["script"] = {"status": "response_received", "report": "script.json"}
f = ["script %s FAILED" % r.get("script") for r in rep.get("results") or [] if not r.get("passed")]
write("script-findings.json", f)
finish(1 if (f or red) else 0)
'''

def fields(result, report, rig):
    gate = report.get("repo_gate") or {}
    adm = gate.get("admission") or {}
    last = (adm.get("requests") or [{}])[-1]
    return {"tree_impl": rt.__file__, "scenario": scenario,
            "repo_gate.returncode": gate.get("returncode"), "repo_gate.measured": gate.get("measured"),
            "repo_gate.retained_findings": gate.get("retained_findings"),
            "repo_gate_unmeasured": report.get("repo_gate_unmeasured"),
            "admission.state": adm.get("state"),
            "admission.requests": [{k: r.get(k) for k in ("route", "status", "outcome", "owner_kind",
                                   "render_wait_timeout_sec", "waited_sec", "wait_timeout_sec", "error",
                                   "keepalives", "admitted_after_sec")}
                                   for r in adm.get("requests") or []],
            "repo_gate_absent(return)": result.get("repo_gate_absent"),
            "repo_gate_absent(report)": report.get("repo_gate_absent"),
            "evidence_state": report.get("evidence_state"), "passed": report.get("passed"),
            "release_evidence": result.get("release_evidence"),
            "failure_identity_error": report.get("failure_identity_error"),
            "failures": [x[:160] for x in report.get("failures") or []],
            "new_failures": [x[:160] for x in report.get("new_failures") or []],
            "harness_rendered_for": list(rig.rendered)}

with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    mp.setenv("AITELIER_HOME", str(work / "home"))
    rig = gf.HarnessRig(work / "harness", mp, render_seconds=4.0 if scenario == "client_timeout_during_red_render" else 0.0)
    mp.setenv("GODOT_BUILDER_URL", rig.base)
    mp.setenv("PROBE_GATE_MODE", scenario)
    mp.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "1.0")
    mp.setenv("PROBE_CLIENT_TIMEOUT", "3")
    repo = work / "repo"; (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    if scenario == "pytest_red_refused":
        (repo / "tests" / "test_bad.py").write_text("def test_bad():\n    assert 1 == 2\n")
    if scenario in ("pytest_red_refused", "wait_expiry_payload"):
        gf.write_gate(repo)
        mp.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", "60" if scenario == "wait_expiry_payload" else "3")
    else:
        (repo / "gate.py").write_text(GATE)
        s = repo / "run_tests.sh"; s.write_text(gf.RUN_TESTS_SH); s.chmod(0o755)
    if scenario == "answered_red_then_refused":
        mp.setattr(rig.gh, "compile_project", lambda _p: {"passed": False, "summary": "1 error",
                   "errors": [{"file": "res://a.gd", "line": 3, "msg": "Parse Error: probe"}]})
    if scenario == "client_timeout_during_red_render":
        rig.script_report = gf.red_report(); mp.setenv("PROBE_CLIENT_TIMEOUT", "1")
    else:
        rig.hold()
    state = work / "state"
    bdir = Path(rt._baseline_dir(str(state), repo.resolve())); bdir.mkdir(parents=True, exist_ok=True)
    (bdir / rt.BASELINE_FILE).write_text(json.dumps({"failures": []}))
    out = work / "out"
    try:
        result = rt.run_tests(project_root=str(repo), out_dir=str(out), state_dir=str(state))
        report = json.loads((out / "test_report.json").read_text())
        print("PROBE " + json.dumps(fields(result, report, rig), indent=1))
    finally:
        rig.close()
