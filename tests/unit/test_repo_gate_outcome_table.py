"""The repository gate's outcome, one row per report shape.

Every row runs the REAL `run_tests` against a gate subprocess that talks to
the REAL harness admission code (`tests/gate_fixture.py:HarnessRig`), and
reads three things off what it wrote:

  * `measured`  -- `repo_gate.measured` in `test_report.json`;
  * `absent`    -- `repo_gate_absent`, on the tool's RETURN and in the report;
  * `next`      -- the node `configs/coding_impl.yaml` routes that report to,
                   computed from the graph's own transitions and schemas
                   (`_next_node`), starting at the `test` step.

The rows are the shapes review gnr3 (2026-09-25) built to turn a readable red
into `not_run` or a foreign report into `measured_fail`, the two r2 poles, the
director's two rulings (a pytest wall is not a measured failure; a missing
node runner ends at `test_evidence_missing`), and the plain passes and reds.

`ROWS` is also what `docs/repo-gate-unmeasured-protocol.md` and
`aitelier/tools/run_tests/tool.yaml` cite: every routing sentence there names
the row that drives it (`test_every_routing_sentence_cites_a_row`), and the
doc's routing table must say what this table measures
(`test_the_doc_routing_table_is_this_table`).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import RUN_TESTS_SH, HarnessRig, red_report

_ROOT = Path(__file__).resolve().parents[2]
_GRAPH = _ROOT / "configs" / "coding_impl.yaml"
_TOOL_YAML = _ROOT / "aitelier" / "tools" / "run_tests" / "tool.yaml"
_PROTOCOL = _ROOT / "docs" / "repo-gate-unmeasured-protocol.md"

# A gate with the game gate's report layout (`tools/godot_gate.py`): it
# creates its own report directory under $GATE_REPORT_DIR before anything
# else, retains `manifest.json` + `<stage>.json` + `<stage>-findings.json`
# there, and exits 2 when the engine does not answer (`... gate NOT run.`).
# `TABLE_GATE_MODE` picks what it retains before its /script request.
TABLE_GATE = r'''
import json, os, sys, tempfile, urllib.error, urllib.request
mode = os.environ["TABLE_GATE_MODE"]
builder = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")
parent = os.environ.get("GATE_REPORT_DIR") or tempfile.gettempdir()
os.makedirs(parent, exist_ok=True)
if mode == "nested":
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
    if mode != "foreign_names_repo_gate_wrote_none":
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
            "missing_stage_report", "nested"):
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

# row id -> (gate mode, AItelier-side leg, engine busy?, expected
#            (measured, absent, next node), identity error expected?)
ROWS = {
    # the r2 poles, and the clean refusal both are measured against
    "clean_refusal": ("clean", None, True,
                      (UNMEASURED, True, "test_gate_absent"), False),
    "python_red_then_refused": ("python_red", None, True,
                                (FAIL, False, "implement"), False),
    "compile_red_then_refused": ("compile_red", None, True,
                                 (FAIL, False, "implement"), False),
    # review gnr3 point 1: a readable red beside a part that is not readable
    "second_manifest_same_repo": ("second_manifest", None, True,
                                  (FAIL, False, "implement"), True),
    "foreign_unreadable_manifest": ("foreign_unreadable", None, True,
                                    (FAIL, False, "implement"), True),
    "missing_stage_report": ("missing_stage_report", None, True,
                             (FAIL, False, "implement"), True),
    "nested_two_levels": ("nested", None, True,
                          (FAIL, False, "implement"), False),
    "empty_findings_failed_stage": ("empty_findings_failed_stage", None, True,
                                    (FAIL, False, "implement"), False),
    # review gnr3 point 1, inverse: another writer names this repository
    "foreign_names_repo_gate_wrote_none": (
        "foreign_names_repo_gate_wrote_none", None, True,
        (UNMEASURED, True, "test_gate_absent"), True),
    "foreign_names_repo_beside_own": (
        "foreign_names_repo_beside_own", None, True,
        (UNMEASURED, True, "test_gate_absent"), True),
    "other_repo_reports_beside_green_gate": (
        "other_repo_reports", None, False, (PASS, False, "done"), False),
    # the sources of `unmeasured`, and what does not qualify
    "declared_absence": ("declared", None, False,
                         (UNMEASURED, True, "test_gate_absent"), False),
    "declared_failed_is_not_an_absence": (
        "declared_failed", None, False, (FAIL, False, "implement"), True),
    "gate_killed_at_its_timeout": ("sleep", "gate_timeout", False,
                                   (UNMEASURED, True, "test_gate_absent"),
                                   False),
    "exit_2_after_an_answered_request": (
        "exit2_after_answer", None, False, (FAIL, False, "implement"), True),
    "exit_1_after_a_refusal": ("exit1_after_refusal", None, True,
                               (FAIL, False, "implement"), True),
    # the rest of the report beside a refused gate (r3 pole 4, the rulings)
    "pytest_red_beside_refused_gate": ("clean", "pytest_red", True,
                                       (UNMEASURED, False, "implement"), False),
    "pytest_wall_beside_refused_gate": ("clean", "pytest_wall", True,
                                        (UNMEASURED, True, "test_gate_absent"),
                                        False),
    "node_runner_unavailable_beside_refused_gate": (
        "clean", "node_unavailable", True,
        (UNMEASURED, False, "test_evidence_missing"), False),
    "pytest_runner_unavailable_beside_refused_gate": (
        "clean", "pytest_unavailable", True,
        (UNMEASURED, False, "test_evidence_missing"), False),
    # the gate answered
    "green_gate": ("clean", None, False, (PASS, False, "done"), False),
    "answered_red": ("clean", "script_red", False,
                     (FAIL, False, "implement"), False),
}

NODE_SKIPPED = {"passed": True, "skipped": True, "dir": ".", "checks": {},
                "summary": "npm not available — node gate skipped "
                           "(install nodejs+npm in the backend image)."}


def _pytest_unavailable(_repo, report):
    # The report `_resolve_pytest_python` leaves when no runner can be
    # provisioned (its last branch), without the three provisioning attempts.
    report.update(passed=False, skipped=True, infrastructure_unavailable=True,
                  evidence_state="infrastructure_unavailable", returncode=0,
                  summary="pytest unavailable and could not be provisioned "
                          "after 3 attempts (table) — test gate skipped.")
    return None, None


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
    mode, leg, busy, _expected, _identity = ROWS[row_id]
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS", "0.3")
    monkeypatch.setenv("TABLE_GATE_MODE", mode)
    rig = HarnessRig(tmp_path / "harness", monkeypatch)
    monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
    try:
        if mode == "compile_red":
            monkeypatch.setattr(rig.gh, "compile_project", lambda _p, **_k: {
                "passed": False, "summary": "1 error",
                "errors": [{"file": "res://a.gd", "line": 3,
                            "msg": "Parse Error: table"}]})
        if leg == "script_red":
            rig.script_report = red_report()
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n")
        if leg == "pytest_red":
            (repo / "tests" / "test_bad.py").write_text(
                "def test_bad():\n    assert 1 == 2\n")
        if leg == "pytest_wall":
            (repo / "tests" / "test_slow.py").write_text(
                "import time\n\ndef test_slow():\n    time.sleep(60)\n")
            monkeypatch.setattr(rt, "PYTEST_WALL_SECONDS", 3)
        if leg == "node_unavailable":
            monkeypatch.setattr(rt, "_run_node_checks",
                                lambda _repo: dict(NODE_SKIPPED))
        if leg == "gate_timeout":
            monkeypatch.setattr(rt, "REPO_GATE_TIMEOUT", 3)
        if leg == "pytest_unavailable":
            monkeypatch.setattr(rt, "_resolve_pytest_python", _pytest_unavailable)
        (repo / "gate.py").write_text(TABLE_GATE)
        script = repo / "run_tests.sh"
        script.write_text(RUN_TESTS_SH)
        script.chmod(0o755)
        if busy:
            rig.hold()
        out = tmp_path / "out"
        result = rt.run_tests(project_root=str(repo), out_dir=str(out))
        report = json.loads((out / "test_report.json").read_text())
    finally:
        rig.close()
    gate = report.get("repo_gate") or {}
    observed = (gate.get("measured"),
                bool(result.get("repo_gate_absent")),
                _next_node(result, report))
    identity_error = bool(gate.get("retained_error")
                          or report.get("failure_identity_error"))
    print("ROW " + json.dumps({
        "row": row_id, "measured": observed[0], "repo_gate_absent": observed[1],
        "next": observed[2], "identity_error": identity_error,
        "returncode": gate.get("returncode"),
        "admission": (gate.get("admission") or {}).get("state"),
        "retained_findings": gate.get("retained_findings"),
        "retained_error": gate.get("retained_error"),
        "failures": [f[:120] for f in report.get("failures") or []]},
        ensure_ascii=False))
    return result, report, observed, identity_error


@pytest.mark.parametrize("row_id", list(ROWS))
def test_the_outcome_of_every_report_shape(tmp_path, monkeypatch, row_id):
    _mode, _leg, _busy, expected, identity_expected = ROWS[row_id]
    result, report, observed, identity_error = _drive_row(
        tmp_path, monkeypatch, row_id)
    assert observed == expected, (row_id, observed, expected)
    # The report says the same as the return.
    assert bool(report.get("repo_gate_absent")) is observed[1]
    # A part that cannot be read, or a report that is not the gate's, is
    # stated; it never hides a red (`measured`, above) and never makes one.
    assert identity_error is identity_expected, (row_id, identity_error)
    if report["repo_gate"].get("retained_findings"):
        # The reds the gate's report names are listed one by one, whatever
        # else was on the ticket.
        listed = [f for f in report["failures"]
                  if f.startswith("repo_gate:run_tests.sh#")]
        assert len(listed) == report["repo_gate"]["retained_findings"], listed


# ── the documents cite the rows ────────────────────────────────────────────

# Words that make a sentence a statement about where a report goes or what a
# gate run is worth: the graph's route targets and the outcome values.
_ROUTING_WORDS = re.compile(
    r"\b(implement|test_gate_absent|test_evidence_missing|measured_fail|"
    r"measured_pass|unmeasured|absences?|absent|not_run|did not run|not run)\b",
    re.IGNORECASE)
_CITATION = re.compile(r"\[rows?: ([a-z0-9_, ]+)\]")
# A statement about what the SCHEDULER does with an absence is driven by a
# drive of the real graph, not by a row: `[test: <file>::<function>]`.
_TEST_CITATION = re.compile(r"\[test: ([\w/.]+\.py)::(\w+)\]")


def _test_exists(path: str, name: str) -> bool:
    source = _ROOT / path
    if not source.is_file():
        return False
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return any(isinstance(node, ast.FunctionDef) and node.name == name
               for node in ast.walk(tree))


def _tool_description() -> str:
    return yaml.safe_load(_TOOL_YAML.read_text(encoding="utf-8"))["description"]


def _statements(markdown: str) -> list[str]:
    """Every sentence and every table row of a markdown text.

    Fenced and indented code blocks are quotations, not statements, and are
    left out. A table row is one statement. The rows of a table whose first
    header cell is `mutation` record an edit and the test that fails under
    it; they are left out too.
    """
    out: list[str] = []
    prose: list[str] = []
    fenced = False
    header = None
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced or line.startswith("    "):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells[0].lower()
                continue
            if set("".join(cells)) <= set("-: "):
                continue
            if header != "mutation":
                out.append(" ".join(line.split()))
            continue
        header = None
        prose.append(line)
    for paragraph in re.split(r"\n\s*\n", "\n".join(prose)):
        text = " ".join(paragraph.split())
        out.extend(s for s in re.split(r"(?<=[.;!?])\s+(?=[A-Z`*(>\"0-9\[])",
                                       text) if s)
    return out


def _routing_statements() -> list[tuple[str, str]]:
    """Every statement of tool.yaml's description and of the protocol doc,
    except the doc's routing-table rows (`test_the_doc_routing_table_is_this_table`
    checks those against the table itself)."""
    doc = _statements(_PROTOCOL.read_text(encoding="utf-8"))
    return ([("tool.yaml", s) for s in _statements(_tool_description())]
            + [(_PROTOCOL.name, s) for s in doc if not _DOC_ROW.match(s)])


def test_every_routing_sentence_cites_a_row():
    """A sentence that says where a report goes, or what a gate run is worth,
    names the rows of this table that drive it, as `[row: <id>]` or
    `[rows: <id>, <id>]` (or, for what the scheduler does with an absence,
    the drive that measures it: `[test: <file>::<function>]`); every row and
    test it names exists. A sentence that makes such a claim and cites
    nothing is not held down by any behaviour, so it fails here, however it
    is worded."""
    uncited, unknown = [], []
    for where, sentence in _routing_statements():
        if not _ROUTING_WORDS.search(sentence):
            continue
        cited = [r.strip() for m in _CITATION.finditer(sentence)
                 for r in m.group(1).split(",") if r.strip()]
        tests = _TEST_CITATION.findall(sentence)
        if not cited and not tests:
            uncited.append(f"{where}: {sentence[:200]}")
        unknown += [f"{where}: {r}" for r in cited if r not in ROWS]
        unknown += [f"{where}: {f}::{n}" for f, n in tests
                    if not _test_exists(f, n)]
    assert unknown == [], unknown
    assert uncited == [], "\n".join(uncited)


_DOC_ROW = re.compile(r"^\| `([a-z0-9_]+)` \| `(\w+)` \| `(true|false)` \| "
                      r"`(\w+)` \|")


def test_the_doc_routing_table_is_this_table():
    """The doc's "Routing, row by row" table lists every row of `ROWS` with the
    values the row asserts, and nothing else."""
    doc_rows = {}
    for line in _PROTOCOL.read_text(encoding="utf-8").splitlines():
        m = _DOC_ROW.match(line)
        if m:
            doc_rows[m.group(1)] = (m.group(2), m.group(3) == "true", m.group(4))
    assert doc_rows == {row: spec[3] for row, spec in ROWS.items()}
