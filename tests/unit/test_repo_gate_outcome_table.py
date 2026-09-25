"""The repository gate's outcome, one row per report shape.

Every row runs the REAL `run_tests` against a gate subprocess that talks to
the REAL harness admission code (`tests/gate_fixture.py:HarnessRig`), and
reads off what it wrote:

  * `attribution` -- `repo_gate.report_attribution.state`: `own` when the
                     gate's report was identified under its ticket,
                     `unattributable` when it was not;
  * `measured`    -- `repo_gate.measured` in `test_report.json`;
  * `absent`      -- `repo_gate_absent`, on the tool's RETURN and in the report;
  * `next`        -- the node `configs/coding_impl.yaml` routes that report to,
                     computed from the graph's own transitions and schemas
                     (`_next_node`), starting at the `test` step;
  * `identity_error` -- whether `repo_gate.retained_error` or
                     `failure_identity_error` is set.

`ROWS` is the only statement of where a report goes. The routing table in
`docs/repo-gate-unmeasured-protocol.md` is rendered from it
(`render_routing_table`) and must be byte-identical to that rendering
(`test_the_doc_routing_table_is_rendered_from_rows`). After editing `ROWS`,
rewrite the doc's table with `python -m tests.unit.test_repo_gate_outcome_table`.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

import jsonschema
import pytest
import yaml

from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import RUN_TESTS_SH, HarnessRig, red_report

_ROOT = Path(__file__).resolve().parents[2]
_GRAPH = _ROOT / "configs" / "coding_impl.yaml"
_PROTOCOL = _ROOT / "docs" / "repo-gate-unmeasured-protocol.md"

# A gate with the game gate's report layout (`tools/godot_gate.py`): it
# creates its own report directory under $GATE_REPORT_DIR, retains
# `manifest.json` + `<stage>.json` + `<stage>-findings.json` there, and exits
# 2 when the engine does not answer (`... gate NOT run.`).
# `TABLE_GATE_MODE` picks what it retains before its /script request, and
# `TABLE_GATE_PRE` what it creates under $GATE_REPORT_DIR BEFORE its report
# directory (the protocol requires nothing to come before it):
#   lockfile      a file `.gate.lock`
#   cachedir      a directory `cache`
#   file_first    no report directory at all: the report goes straight into
#                 $GATE_REPORT_DIR
#   foreign_same  a directory whose manifest names this repository with a red
#   foreign_other a directory whose manifest names another repository with a red
TABLE_GATE = r'''
import json, os, sys, tempfile, urllib.error, urllib.request
mode = os.environ["TABLE_GATE_MODE"]
pre = os.environ.get("TABLE_GATE_PRE", "")
builder = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")
parent = os.environ.get("GATE_REPORT_DIR") or tempfile.gettempdir()
os.makedirs(parent, exist_ok=True)
def pre_writer(repo_claim):
    f = tempfile.mkdtemp(prefix="aaa-first-", dir=parent)
    for name, v in (("python.json", {"returncode": 1}),
                    ("python-findings.json", ["first writer: the python suite exited 1"]),
                    ("manifest.json", {"repo": repo_claim, "status": "failed", "exit_code": 1,
                                       "stages": {"python": {"status": "finished",
                                                             "report": "python.json"}}})):
        with open(os.path.join(f, name), "w") as fh:
            json.dump(v, fh)
if pre == "lockfile":
    with open(os.path.join(parent, ".gate.lock"), "w") as fh:
        fh.write("pid\n")
elif pre == "cachedir":
    os.makedirs(os.path.join(parent, "cache"), exist_ok=True)
elif pre == "foreign_same":
    pre_writer(os.getcwd())
elif pre == "foreign_other":
    pre_writer("/tmp/some-other-repo")
if pre == "file_first":
    d = parent
elif mode == "nested":
    d = tempfile.mkdtemp(prefix="gate-", dir=tempfile.mkdtemp(prefix="outer-", dir=parent))
else:
    d = tempfile.mkdtemp(prefix="gate-", dir=parent)
cwd = os.getcwd()
manifest = {"repo": cwd, "status": "incomplete", "stages": {}}
def write(name, v, where=None):
    with open(os.path.join(where or d, name), "w") as fh:
        json.dump(v, fh, indent=2)
def post(path, payload):
    req = urllib.request.Request(builder.rstrip("/") + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
def save_manifest():
    if mode not in ("foreign_names_repo_gate_wrote_none", "findings_no_manifest"):
        write("manifest.json", manifest)
def finish(code):
    manifest.update(exit_code=code, status={0: "passed", 1: "failed"}.get(code, "incomplete"))
    save_manifest()
    sys.exit(code)
def other_writer(repo_claim, findings, broken=False):
    f = tempfile.mkdtemp(prefix="other-", dir=parent)
    if broken:
        with open(os.path.join(f, "manifest.json"), "w") as fh:
            fh.write('{"repo": "/tmp/other-repo", "stag')
        return
    write("python.json", {"returncode": 1}, f)
    write("python-findings.json", findings, f)
    write("manifest.json", {"repo": repo_claim, "status": "failed", "exit_code": 1,
          "stages": {"python": {"status": "finished", "report": "python.json"}}}, f)
save_manifest()
if mode in ("declared", "declared_failed"):
    state = "blocked" if mode == "declared" else "failed"
    print('AITELIER_REPO_GATE_UNMEASURED={"state": "%s", "reason": "table"}' % state)
    finish(3)
if mode == "sleep":
    import time
    time.sleep(60)
base = {"project_dir": cwd, "project_id": "table", "run_id": "table",
        "scripts": ["res://t.gd"], "timeout": 60}
if mode in ("python_red", "second_manifest", "foreign_unreadable",
            "missing_stage_report", "nested", "findings_no_manifest"):
    write("python.json", {"returncode": 1})
    write("python-findings.json", ["the python suite exited 1"])
    manifest["stages"]["python"] = {"status": "finished", "report": "python.json"}
    save_manifest()
if mode == "compile_red":
    write("compile.json", post("/compile", base))
    manifest["stages"]["compile"] = {"status": "response_received", "report": "compile.json"}
    save_manifest()
if mode == "second_manifest":
    write("manifest.json", {"repo": cwd, "status": "incomplete", "stages": {}},
          tempfile.mkdtemp(prefix="gate-retry-", dir=parent))
if mode == "foreign_unreadable":
    other_writer(None, None, broken=True)
if mode == "missing_stage_report":
    manifest["stages"]["compile"] = {"status": "response_received", "report": "compile.json"}
    save_manifest()
if mode == "empty_findings_failed_stage":
    write("compile.json", {"passed": False, "summary": "1 error",
                           "errors": [{"file": "res://a.gd", "line": 3, "msg": "Parse Error: table"}]})
    write("compile-findings.json", [])
    manifest["stages"]["compile"] = {"status": "response_received", "report": "compile.json"}
    save_manifest()
if mode in ("foreign_names_repo_gate_wrote_none", "foreign_names_repo_beside_own"):
    other_writer(cwd, ["other writer: the python suite exited 1"])
if mode == "other_repo_reports":
    other_writer("/repo", ["fixture: the python suite exited 1"])
try:
    rep = post("/script", base)
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print("godot-builder unreachable at %s: %s -- gate NOT run." % (builder, exc), file=sys.stderr)
    finish(1 if mode == "exit1_after_refusal" else 2)
write("script.json", rep)
manifest["stages"]["script"] = {"status": "response_received", "report": "script.json"}
found = ["script %s FAILED" % r.get("script") for r in rep.get("results") or [] if not r.get("passed")]
write("script-findings.json", found)
if mode == "exit2_after_answer":
    finish(2)
finish(1 if found else 0)
'''

UNMEASURED, FAIL, PASS = "unmeasured", "measured_fail", "measured_pass"
UNATTRIBUTABLE = "unattributable"
OWN = "own"
TERMINAL = "test_gate_report_unattributable"


class Row(NamedTuple):
    mode: str            # TABLE_GATE_MODE
    pre: str             # TABLE_GATE_PRE ("" = the report directory comes first)
    leg: str | None      # what else the run meets (AItelier side, or the engine)
    busy: bool           # the engine is held by another owner
    attribution: str
    measured: str
    absent: bool
    next: str
    identity_error: bool
    shape: str           # one line for the rendered table


ROWS = {
    # the r2 poles, and the clean refusal both are measured against
    "clean_refusal": Row(
        "clean", "", None, True, OWN, UNMEASURED, True, "test_gate_absent", False,
        "the engine refused the gate's only request; exit 2"),
    "python_red_then_refused": Row(
        "python_red", "", None, True, OWN, FAIL, False, "implement", False,
        "a python-stage red retained, then a refused `/script`; exit 2"),
    "compile_red_then_refused": Row(
        "compile_red", "", None, True, OWN, FAIL, False, "implement", False,
        "an answered `/compile` red, then a refused `/script`; exit 2"),
    # review gnr3 point 1: a readable red beside a part that is not readable
    "second_manifest_same_repo": Row(
        "second_manifest", "", None, True, OWN, FAIL, False, "implement", True,
        "a python red, plus a second manifest naming the same repository, "
        "created after the gate's report directory"),
    "foreign_unreadable_manifest": Row(
        "foreign_unreadable", "", None, True, OWN, FAIL, False, "implement", True,
        "a python red, plus a truncated manifest elsewhere under the ticket"),
    "missing_stage_report": Row(
        "missing_stage_report", "", None, True, OWN, FAIL, False, "implement", True,
        "a python red, plus a manifest stage whose report file is missing"),
    "nested_two_levels": Row(
        "nested", "", None, True, OWN, FAIL, False, "implement", False,
        "a python red retained two directories below the ticket"),
    "empty_findings_failed_stage": Row(
        "empty_findings_failed_stage", "", None, True, OWN, FAIL, False,
        "implement", False,
        "`compile.json` says `passed: false`, `compile-findings.json` is `[]`"),
    # another writer names this repository
    "foreign_names_repo_gate_wrote_none": Row(
        "foreign_names_repo_gate_wrote_none", "", None, True, UNATTRIBUTABLE,
        UNATTRIBUTABLE, False, TERMINAL, True,
        "another writer's report names this repository with a red; the gate's "
        "first entry holds no manifest; refused"),
    "foreign_names_repo_beside_own": Row(
        "foreign_names_repo_beside_own", "", None, True, OWN, UNMEASURED, True,
        "test_gate_absent", True,
        "the same, beside the gate's own clean report; refused"),
    "other_repo_reports_beside_green_gate": Row(
        "other_repo_reports", "", None, False, OWN, PASS, False, "done", False,
        "reports for another repository beside a green gate"),
    # the sources of `unmeasured`, and what does not qualify
    "declared_absence": Row(
        "declared", "", None, False, OWN, UNMEASURED, True, "test_gate_absent",
        False, '`AITELIER_REPO_GATE_UNMEASURED={"state": "blocked"}`, exit 3'),
    "declared_failed_is_not_an_absence": Row(
        "declared_failed", "", None, False, OWN, FAIL, False, "implement", True,
        'the same line with `"state": "failed"`, exit 3'),
    "gate_killed_at_its_timeout": Row(
        "sleep", "", "gate_timeout", False, OWN, UNMEASURED, True,
        "test_gate_absent", False,
        "the harness killed the gate at `REPO_GATE_TIMEOUT`"),
    "exit_2_after_an_answered_request": Row(
        "exit2_after_answer", "", None, False, OWN, FAIL, False, "implement", True,
        "the engine answered, then the gate exited 2"),
    "exit_1_after_a_refusal": Row(
        "exit1_after_refusal", "", None, True, OWN, FAIL, False, "implement", True,
        "the engine refused, then the gate exited 1"),
    # the rest of the report beside a refused gate (r3 pole 4, the rulings)
    "pytest_red_beside_refused_gate": Row(
        "clean", "", "pytest_red", True, OWN, UNMEASURED, False, "implement", False,
        "AItelier's pytest red; the gate refused"),
    "pytest_wall_beside_refused_gate": Row(
        "clean", "", "pytest_wall", True, OWN, UNMEASURED, True,
        "test_gate_absent", False,
        "AItelier's pytest killed at its wall; the gate refused (director "
        "ruling rev 4: a pytest killed at its wall measured nothing)"),
    "node_runner_unavailable_beside_refused_gate": Row(
        "clean", "", "node_unavailable", True, OWN, UNMEASURED, False,
        "test_evidence_missing", False,
        "no npm (`node.skipped`); the gate refused (director ruling rev 5: a "
        "missing runner, npm or pytest, ends at `test_evidence_missing`)"),
    "pytest_runner_unavailable_beside_refused_gate": Row(
        "clean", "", "pytest_unavailable", True, OWN, UNMEASURED, False,
        "test_evidence_missing", False,
        "no pytest could be provisioned; the gate refused (the same ruling)"),
    # the gate answered
    "green_gate": Row(
        "clean", "", None, False, OWN, PASS, False, "done", False,
        "the engine answered green"),
    "answered_red": Row(
        "clean", "", "script_red", False, OWN, FAIL, False, "implement", False,
        "the engine answered red; exit 1"),
    # rev 5: the gate's first entry is not its report directory
    "lockfile_first_red_refused": Row(
        "python_red", "lockfile", None, True, UNATTRIBUTABLE, UNATTRIBUTABLE,
        False, TERMINAL, True,
        "the gate creates `.gate.lock` first, then its report with a python "
        "red; refused"),
    "cachedir_first_red_refused": Row(
        "python_red", "cachedir", None, True, UNATTRIBUTABLE, UNATTRIBUTABLE,
        False, TERMINAL, True,
        "the gate creates a `cache` directory first, then its report with a "
        "python red; refused"),
    "file_first_red_refused": Row(
        "python_red", "file_first", None, True, UNATTRIBUTABLE, UNATTRIBUTABLE,
        False, TERMINAL, True,
        "the gate writes `manifest.json` and its python red straight into the "
        "ticket; refused"),
    "first_entry_names_other_repo_red_refused": Row(
        "python_red", "foreign_other", None, True, UNATTRIBUTABLE,
        UNATTRIBUTABLE, False, TERMINAL, True,
        "a writer that names another repository creates its report before the "
        "gate's; the gate retains a python red; refused"),
    "first_entry_names_this_repo_clean_refused": Row(
        "clean", "foreign_same", None, True, OWN, FAIL, False, "implement", True,
        "a writer that names this repository with a red creates its report "
        "before the gate's; the gate is clean; refused"),
    "report_without_manifest_red_refused": Row(
        "findings_no_manifest", "", None, True, UNATTRIBUTABLE, UNATTRIBUTABLE,
        False, TERMINAL, True,
        "the gate's report directory holds a python red and no "
        "`manifest.json`; refused"),
    "lockfile_first_green_gate": Row(
        "clean", "lockfile", None, False, UNATTRIBUTABLE, PASS, False, "done",
        False, "the gate creates `.gate.lock` first; no red anywhere; the "
        "engine answered green"),
    "pytest_runner_unavailable_beside_unattributable_red": Row(
        "python_red", "lockfile", "pytest_unavailable", True, UNATTRIBUTABLE,
        UNATTRIBUTABLE, False, "test_evidence_missing", True,
        "no pytest could be provisioned, beside the `lockfile_first` red "
        "(director ruling rev 5)"),
    # rev 5: the order of creation could not be observed (`inotify_init1`
    # fails with EMFILE, as when the uid's inotify instances are all taken)
    "inotify_unavailable_own_red": Row(
        "python_red", "", "inotify_unavailable", True, UNATTRIBUTABLE,
        UNATTRIBUTABLE, False, TERMINAL, True,
        "`inotify_init1` fails; the gate's own python red; refused"),
    "inotify_unavailable_foreign_red": Row(
        "foreign_names_repo_beside_own", "", "inotify_unavailable", True,
        UNATTRIBUTABLE, UNATTRIBUTABLE, False, TERMINAL, True,
        "`inotify_init1` fails; another writer's red naming this repository, "
        "beside the gate's clean report; refused"),
    "inotify_unavailable_no_red": Row(
        "clean", "", "inotify_unavailable", True, UNATTRIBUTABLE, UNMEASURED,
        True, "test_gate_absent", False,
        "`inotify_init1` fails; no red anywhere; refused"),
}

NODE_SKIPPED = {"passed": True, "skipped": True, "dir": ".", "checks": {},
                "summary": "npm not available — node gate skipped "
                           "(install nodejs+npm in the backend image)."}

# Set by a caller that has itself used up this uid's inotify instances
# (`fs.inotify.max_user_instances`): the `inotify_unavailable` rows then run
# with the real libc, and `inotify_init1` fails for real.
REAL_EXHAUSTION_ENV = "AITELIER_TABLE_INOTIFY_EXHAUSTED"


def _pytest_unavailable(_repo, report):
    # The report `_resolve_pytest_python` leaves when no runner can be
    # provisioned (its last branch), without the three provisioning attempts.
    report.update(passed=False, skipped=True, infrastructure_unavailable=True,
                  evidence_state="infrastructure_unavailable", returncode=0,
                  summary="pytest unavailable and could not be provisioned "
                          "after 3 attempts (table) — test gate skipped.")
    return None, None


class _LibcWithoutInotify:
    """libc as `ctypes.CDLL(None)` returns it, except `inotify_init1`, which
    fails the way the kernel fails it once the uid's inotify instances are
    all taken: it returns -1 with errno EMFILE. Only the system call is
    replaced; `_FirstEntry` runs unchanged."""

    def __init__(self, lib):
        self._lib = lib

    def __getattr__(self, name):
        return getattr(self._lib, name)

    def inotify_init1(self, _flags):
        ctypes.set_errno(errno.EMFILE)
        return -1


def _without_inotify(monkeypatch) -> None:
    real = ctypes.CDLL

    def cdll(name, *args, **kwargs):
        lib = real(name, *args, **kwargs)
        return _LibcWithoutInotify(lib) if name is None else lib

    monkeypatch.setattr(rt.ctypes, "CDLL", cdll)


def _next_node(result: dict, report: dict) -> str:
    """Where `configs/coding_impl.yaml` sends this `test` step's outcome.

    The `test` step's flags are the tool's return (the engine merges it into
    the step's flags). A `json_schema` step's flag is `all_passed`: whether
    `test_report.json` validates against its `inline_schema`. Transitions are
    taken in order; the first whose `match` holds wins, and one with no
    `match` always holds. The walk stops at the first node that is not a
    `json_schema` step.
    """
    steps = {s["id"]: s for s in yaml.safe_load(_GRAPH.read_text())["steps"]}
    node, flags = "test", dict(result)
    for _ in range(len(steps)):
        for transition in steps[node]["transitions"]:
            match = transition.get("match")
            if match is None:
                break
            if "field" in match:
                if flags.get(match["field"]) == match["value"]:
                    break
            elif all(flags.get(k) == v for k, v in match.items()):
                break
        else:
            raise AssertionError(f"no transition out of {node} matches {flags}")
        node = transition["to"]
        step = steps.get(node) or {}
        if step.get("tool_name") != "json_schema":
            return str(node)
        try:
            jsonschema.validate(report, step["tool_params"]["inline_schema"])
            flags = {"all_passed": True}
        except jsonschema.ValidationError:
            flags = {"all_passed": False}
    raise AssertionError("the route did not leave the schema steps")


def _drive_row(tmp_path, monkeypatch, row_id):
    row = ROWS[row_id]
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("TABLE_GATE_MODE", row.mode)
    monkeypatch.setenv("TABLE_GATE_PRE", row.pre)
    inotify = "real"
    if row.leg == "inotify_unavailable":
        if os.environ.get(REAL_EXHAUSTION_ENV) == "1":
            inotify = "exhausted by the caller"
        else:
            _without_inotify(monkeypatch)
            inotify = "inotify_init1 stubbed to EMFILE"
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    try:
        if row.mode == "compile_red":
            monkeypatch.setattr(rig.gh, "compile_project", lambda _p, **_k: {
                "passed": False, "summary": "1 error",
                "errors": [{"file": "res://a.gd", "line": 3,
                            "msg": "Parse Error: table"}]})
        if row.leg == "script_red":
            rig.script_report = red_report()
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n")
        if row.leg == "pytest_red":
            (repo / "tests" / "test_bad.py").write_text(
                "def test_bad():\n    assert 1 == 2\n")
        if row.leg == "pytest_wall":
            (repo / "tests" / "test_slow.py").write_text(
                "import time\n\ndef test_slow():\n    time.sleep(60)\n")
            monkeypatch.setattr(rt, "PYTEST_WALL_SECONDS", 3)
        if row.leg == "node_unavailable":
            monkeypatch.setattr(rt, "_run_node_checks",
                                lambda _repo: dict(NODE_SKIPPED))
        if row.leg == "gate_timeout":
            monkeypatch.setattr(rt, "REPO_GATE_TIMEOUT", 3)
        if row.leg == "pytest_unavailable":
            monkeypatch.setattr(rt, "_resolve_pytest_python", _pytest_unavailable)
        (repo / "gate.py").write_text(TABLE_GATE)
        script = repo / "run_tests.sh"
        script.write_text(RUN_TESTS_SH)
        script.chmod(0o755)
        if row.busy:
            rig.hold()
        out = tmp_path / "out"
        result = rt.run_tests(project_root=str(repo), out_dir=str(out))
        report = json.loads((out / "test_report.json").read_text())
    finally:
        rig.close()
    gate = report.get("repo_gate") or {}
    attribution = gate.get("report_attribution") or {}
    observed = (gate.get("measured"),
                bool(result.get("repo_gate_absent")),
                _next_node(result, report))
    identity_error = bool(gate.get("retained_error")
                          or report.get("failure_identity_error"))
    print("ROW " + json.dumps({
        "row": row_id, "attribution": attribution.get("state"),
        "measured": observed[0], "repo_gate_absent": observed[1],
        "next": observed[2], "identity_error": identity_error,
        "inotify": inotify, "observed": attribution.get("observed"),
        "first_entry": attribution.get("first_entry"),
        "why": attribution.get("why"),
        "returncode": gate.get("returncode"),
        "admission": (gate.get("admission") or {}).get("state"),
        "retained_findings": gate.get("retained_findings"),
        "unattributed_reds": gate.get("unattributed_reds"),
        "retained_error": gate.get("retained_error"),
        "release_evidence": result.get("release_evidence"),
        "failures": [f[:160] for f in report.get("failures") or []]},
        ensure_ascii=False))
    return result, report, observed, identity_error


_DRIVEN: dict = {}


def _driven(row_id, tmp_path_factory):
    """Each row is driven once per session, whichever test asks first."""
    if row_id not in _DRIVEN:
        with pytest.MonkeyPatch.context() as mp:
            _DRIVEN[row_id] = _drive_row(tmp_path_factory.mktemp(row_id), mp,
                                         row_id)
    return _DRIVEN[row_id]


@pytest.mark.parametrize("row_id", list(ROWS))
def test_the_outcome_of_every_report_shape(tmp_path_factory, row_id):
    row = ROWS[row_id]
    result, report, observed, identity_error = _driven(row_id, tmp_path_factory)
    assert observed == (row.measured, row.absent, row.next), (row_id, observed)
    # The report says the same as the return.
    assert bool(report.get("repo_gate_absent")) is observed[1]
    # A part that cannot be read, a report that is not the gate's, or reds
    # that belong to no identified report, are stated.
    assert identity_error is row.identity_error, (row_id, identity_error)
    gate = report["repo_gate"]
    if gate.get("retained_findings"):
        # The reds the gate's report names are listed one by one, whatever
        # else was on the ticket.
        listed = [f for f in report["failures"]
                  if f.startswith("repo_gate:run_tests.sh#")]
        assert len(listed) == gate["retained_findings"], listed
    if row.measured == UNATTRIBUTABLE:
        assert gate["retained_findings"] is None
        assert gate["unattributed_reds"], gate
        assert report["repo_gate_unattributable"] is (row.next == TERMINAL)
        assert result["release_evidence"] == "unresolved"


@pytest.mark.parametrize("row_id", list(ROWS))
def test_the_attribution_of_every_report_shape(tmp_path_factory, row_id):
    row = ROWS[row_id]
    _result, report, _observed, _identity = _driven(row_id, tmp_path_factory)
    attribution = report["repo_gate"]["report_attribution"]
    assert attribution["state"] == row.attribution, (row_id, attribution)
    if row.leg == "inotify_unavailable":
        assert attribution["observed"] is False, attribution
        assert "inotify_init1 failed (errno 24" in attribution["why"], attribution
    else:
        assert attribution["observed"] is True, attribution


# The protocol requires the gate to create its own report directory under
# GATE_REPORT_DIR before it creates anything else there. These rows break it.
_BREAKS_THE_REQUIREMENT = ("lockfile_first_red_refused",
                           "cachedir_first_red_refused",
                           "file_first_red_refused",
                           "first_entry_names_other_repo_red_refused")


@pytest.mark.parametrize("row_id", _BREAKS_THE_REQUIREMENT)
def test_the_gate_must_create_its_report_directory_first(tmp_path_factory,
                                                         row_id):
    """A gate that creates anything else under its ticket first leaves its
    report unattributable: its own red is listed, with where it was read,
    and is neither dropped nor counted as the gate's."""
    result, report, observed, _identity = _driven(row_id, tmp_path_factory)
    gate = report["repo_gate"]
    assert gate["report_attribution"]["state"] == UNATTRIBUTABLE
    assert gate["retained_findings"] is None
    own = [r for r in gate["unattributed_reds"]
           if r.endswith("[python]: the python suite exited 1")]
    assert len(own) == 1, gate["unattributed_reds"]
    assert gate["measured"] == UNATTRIBUTABLE
    assert observed[2] == TERMINAL
    assert any(own[0] in f for f in report["failures"]), report["failures"]


# ── the doc's routing table is rendered from ROWS ──────────────────────────

TABLE_BEGIN = ("<!-- routing table: rendered from ROWS in "
               "tests/unit/test_repo_gate_outcome_table.py by "
               "render_routing_table(); edit ROWS and run "
               "`python -m tests.unit.test_repo_gate_outcome_table` -->")
TABLE_END = "<!-- routing table: end -->"
_HEADER = ("| row | attribution | measured | repo_gate_absent | next "
           "| identity error | the shape |")


def render_routing_table(rows: dict | None = None) -> str:
    """The routing table as the doc carries it, markers included."""
    rows = ROWS if rows is None else rows
    lines = [TABLE_BEGIN, "", _HEADER, "|---|---|---|---|---|---|---|"]
    for row_id, row in rows.items():
        lines.append(
            f"| `{row_id}` | `{row.attribution}` | `{row.measured}` "
            f"| `{str(row.absent).lower()}` | `{row.next}` "
            f"| `{str(row.identity_error).lower()}` | {row.shape} |")
    lines += ["", TABLE_END]
    return "\n".join(lines) + "\n"


def _doc_table(text: str) -> str:
    assert text.count(TABLE_BEGIN) == 1 and text.count(TABLE_END) == 1
    begin = text.index(TABLE_BEGIN)
    end = text.index(TABLE_END) + len(TABLE_END) + 1
    return text[begin:end]


def test_the_doc_routing_table_is_rendered_from_rows():
    """The doc's routing table, between its two markers, is byte for byte
    what `render_routing_table` makes of `ROWS`."""
    assert all("|" not in row.shape and "\n" not in row.shape
               for row in ROWS.values())
    doc = _PROTOCOL.read_bytes().decode("utf-8")
    assert _doc_table(doc).encode("utf-8") == render_routing_table().encode("utf-8")


def write_routing_table() -> None:
    doc = _PROTOCOL.read_text(encoding="utf-8")
    old = _doc_table(doc)
    _PROTOCOL.write_text(doc.replace(old, render_routing_table()),
                         encoding="utf-8")


if __name__ == "__main__":
    write_routing_table()
    sys.exit(0)
