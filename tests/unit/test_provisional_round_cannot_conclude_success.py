# tests/unit/test_provisional_round_cannot_conclude_success.py
#
# A round that never measured the whole repository gate must not conclude DONE.
#
# THE DEFECT, as the producer writes it. Whenever `run_tests` cannot take a
# complete repository-gate measurement, it stamps ITS OWN report
# `purpose: provisional_round_feedback` AND `full_test_passed: false`
# (`aitelier/tools/run_tests/impl.py`, the `gate_coverage.purpose` branch) — the
# report says of itself that it is NOT full test evidence — and it says the same
# thing on the verdict dict the engine merges into the step's flags.
# `configs/coding_impl.yaml`'s `test_evidence` required only
# `passed`/`summary`/`failures` and refused only `skipped` / `node.skipped`; it
# never read `purpose` or `full_test_passed`. So a provisional round whose
# PYTEST leg was green satisfied every validator this graph has, went on to
# `test_outcome` and `done`, and concluded success on evidence its own report
# disowns — `gate_evidence.report_state` already answers that shape `partial`
# and `release_disposition` `unresolved`, a release that needs VERIFICATION, not
# a repair.
#
# WHAT IS DRIVEN, and by what:
#   * the reports and the verdicts come from the REAL `rt.run_tests` (only the
#     process boundary, `repository_executor.execute`, is served — the same seam
#     `tests/unit/test_repo_gate_early_contract_red.py` and
#     `tests/unit/test_partial_gate_cannot_reach_done.py` use). Nothing here
#     hand-builds a report the criterion is about;
#   * the form/route answers come from the SHIPPED config's own artifacts:
#     `json_schema` with the `test_evidence`/`test_outcome` step's own
#     `files` + `inline_schema` (the validator the graph actually runs) and
#     `skillflow.graph.GraphResolver` over `configs/coding_impl.yaml` (the
#     transition matcher the engine actually uses);
#   * the end-to-end answers come from the REAL graph through the REAL host and
#     the REAL tool (`tests.skillflow.test_coding_impl_gate._wire/_drive`): a
#     run's status, its `current_node` and its implement laps, not a guess.
#
# THE FOUR SHAPES THE PRODUCER WRITES, and what each must do:
#
#   shape                run_tests writes                report_state  routing
#   -------------------  ------------------------------  ------------  --------------------------
#   provisional green    passed:true, full_test_passed    partial       test_evidence_provisional
#                        false, purpose:provisional                    (failed, NOT done)
#   provisional red      passed:false, returncode:1,      failed        test_evidence -> test_outcome
#                        full_test_passed false                        -> implement (repair kept)
#   complete green       passed:true, no marker           passed        done
#   no measured coverage no_tests_collected:true          skipped       test_evidence_missing
#
# WHO ELSE READS THE PRODUCER'S FACTS. The full grep-verified inventory lives
# beside the new edge in `configs/coding_impl.yaml` (file:line, re-checkable);
# it is kept there rather than in a delivery message so the next person editing
# the edge finds it. The readers this suite's subject is most easily confused
# with, and what each gains or loses:
#   * `gate_coverage.provisional_producer` (:39) and `publication_scope_refusal`
#     (:123) — a provisional round may not authorize publication;
#   * `gate_evidence.report_state` (:320) answers the marker `partial`,
#     `release_disposition` (:359) `unresolved`, and
#     `_provisional_red_is_measured` (:260) / `_audit_one` (:614) separate a
#     `run_tests` red from `round_feedback`'s by-construction `passed: false`;
#   * `run_tests` itself refuses a provisional scope as full test evidence
#     (`_retained_coverage`, impl.py:1727), stamps the report (:3383), and keeps
#     every old gate red in its baseline pruning (:3475/:3505);
#   * `round_feedback.run_feedback` (:220) refuses a coverage scope whose
#     `purpose` is not its own — the producer of the OTHER report carrying this
#     marker, and the reader an earlier inventory of these facts missed;
#   * `godot_playtest`'s `purpose` TOOL PARAMETER (:770/:773/:775) selects that
#     feedback round, and `core/coding_impl_outcome.py:152` refuses a
#     provisional round as a passing pre-change measurement.
# `full_test_passed` is read by this edge, the tests and prose only
# (`docs/scoped-gate-coverage.md:93`, `godot_playtest/tool.yaml:48`); nothing
# under core/, api/, web_api/ or cli/ reads it. `gate_coverage` is read by
# `run_tests` (`_retained_coverage` impl.py:1722, report folding :3375, baseline
# decisions :3468/:3501) and, through `publication_scope_refusal`, by
# `release_disposition` (:467-:468) and `_audit_one` (:655-:657).
#
# The one seam, named because it is a seam: `run_tests._retained_coverage`
# refuses a provisional coverage, so a repository gate that declares one is
# `unmeasured` — an ABSENCE — rather than a measured pass, and the shipped
# acquisition cannot reach the provisional-GREEN shape on its own. That shape is
# therefore served at the acquisition (`_acquire_repo_gate`) — the same stub the
# shipped `test_repo_gate_early_contract_red._provisional_round` uses and
# documents — because the branch under test is the one `run_tests` writes when
# an acquired gate says `measured_pass` WITH a provisional coverage: `passed`
# then keeps the real pytest outcome, and the marker is stamped beside it. Every
# other shape below runs the real gate and the real pytest session end to end.
import json
import sys
from pathlib import Path

import pytest

from skillflow.graph import GraphResolver, PipelineGraph
from skillflow.tools.json_schema.impl import json_schema

from aitelier import gate_evidence as ge
from aitelier.gate_coverage import (
    PRODUCER_RUN_TESTS, PROVISIONAL_PRODUCER, PROVISIONAL_PURPOSE,
    full_coverage)
from aitelier.tools.run_tests import impl as rt
from tests.gate_fixture import embed_env, install_entry_executor

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs" / "coding_impl.yaml"
PROVISIONAL_TERMINAL = "test_evidence_provisional"
# What the config's `test` step matches to reach the provisional terminal, and
# the form it must be written in (see `test_the_provisional_edge_is_a_bare_key_
# flag_match`).
PROVISIONAL_MATCH = {"full_test_passed": False, "passed": True}

# A repository gate with the producer protocol: it retains a manifest under the
# ticket directory it is handed, and declares its own coverage. The real
# executor entry scrubs the child environment, so the fixture carries its two
# controlled settings itself.
GATE = r"""
import json, os, sys, tempfile
from pathlib import Path

parent = Path(os.environ["GATE_REPORT_DIR"])
directory = Path(tempfile.mkdtemp(prefix="provisional-", dir=parent))
manifest = {"repo": str(Path.cwd()), "status": "passed",
            "outcome": "measured_pass", "exit_code": 0, "stages": {},
            "gate_coverage": json.loads(os.environ["GATE_COVERAGE"])}
(directory / "manifest.json").write_text(json.dumps(manifest))
sys.exit(0)
"""

GREEN_PYTEST = "def test_contract():\n    assert 1 + 1 == 2\n"
RED_PYTEST = "def test_contract():\n    assert 1 + 1 == 3\n"


# ───────────────────────────── the producer's four shapes ───────────────────
def _provisional_coverage() -> dict:
    """The scope a provisional round declares: complete, but provisional."""
    coverage = full_coverage("a" * 40, {"scenarios": [{"name": "one"}]})
    coverage["purpose"] = PROVISIONAL_PURPOSE
    return coverage


def _full_coverage() -> dict:
    return full_coverage("a" * 40, {"scenarios": [{"name": "one"}]})


def _provisional_measured_pass() -> dict:
    """The acquired-gate verdict the provisional-GREEN shape is written from.

    A gate that measured its selected scenarios and says so, while the coverage
    it declares is provisional: `run_tests` keeps the real pytest outcome in
    `passed` and stamps the marker beside it.
    """
    return {"script": "run_tests.sh", "returncode": 0, "passed": True,
            "output": "a provisional round whose measured leg was green",
            "measured": rt.REPO_GATE_MEASURED_PASS,
            "gate_coverage": _provisional_coverage()}


def _repo(tmp_path, name, *, pytest_body=None, gate_coverage=None) -> Path:
    repo = tmp_path / name
    (repo / "tests").mkdir(parents=True)
    if pytest_body is not None:
        (repo / "tests" / "test_contract.py").write_text(pytest_body)
    if gate_coverage is not None:
        (repo / "gate.py").write_text(embed_env(
            GATE, GATE_COVERAGE=json.dumps(gate_coverage)))
        script = repo / "run_tests.sh"
        script.write_text(f"#!/bin/sh\nexec {sys.executable} gate.py\n")
        script.chmod(0o755)
    return repo


def _run_tests(tmp_path, monkeypatch, repo, *, measured_pass=False):
    """The REAL `run_tests`: returns (report, verdict dict, report directory).

    `measured_pass` serves the acquisition with a gate verdict of
    `measured_pass` carrying a PROVISIONAL coverage — the one shape
    `_retained_coverage` refuses to reach through a repository gate (it answers
    it `unmeasured`, an absence). See the module header.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "data"))
    install_entry_executor(monkeypatch)
    out = tmp_path / "graph" / "test"
    with monkeypatch.context() as served:
        if measured_pass:
            served.setattr(rt, "_acquire_repo_gate",
                           lambda _repo, **_kwargs: _provisional_measured_pass())
        verdict = rt.run_tests(project_root=str(repo), out_dir=str(out),
                               run_id="provisional-round",
                               evidence_cycle_start=True)
    report = json.loads((out / "test_report.json").read_text(encoding="utf-8"))
    return report, verdict, out


def _resolver() -> GraphResolver:
    return GraphResolver(PipelineGraph.from_yaml(CONFIG))


def _select(verdict, report_dir):
    """(transition, target) the ENGINE's resolver picks for `test`.

    `verdict` is `run_tests`' own return — exactly what the engine merges into
    the step's flags (`skillflow/core.py:_confirm_tool_in_tx`). The
    `file_reader` is supplied because a reader is what the engine gives the
    resolver; no `test` edge in this config reads the report through it.
    """
    def read(path: str) -> str:
        return (report_dir / path).read_text(encoding="utf-8")
    return _resolver().resolve_transition("test", verdict, {}, file_reader=read)


def _validate(step_id: str, report_dir: Path):
    """Run the SHIPPED step's own validator on the report in `report_dir`.

    The `files` and `inline_schema` are read off `configs/coding_impl.yaml`, and
    the validator is the native `json_schema` tool the step names — so this is
    the artifact check the graph itself performs, not a re-implementation.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    step = next(s for s in graph.steps if s.id == step_id)
    params = step.tool_params
    result = json_schema(files=list(params["files"]),
                         inline_schema=params["inline_schema"],
                         workspace_root=str(report_dir))
    return result["all_passed"], result["results"]


def test_a_provisional_green_round_is_refused_by_the_shipped_routing(
        tmp_path, monkeypatch):
    """The defect's exact shape: green pytest leg, provisional gate coverage.

    The producers' own facts, the classifier's answer and the config's routing
    have to agree, and the two validators are shown to be the WRONG place to
    stop it: they accept the report (that is why the routing edge exists), while
    the routing edge — reading the producer's own `full_test_passed` — refuses
    it. No `done`.
    """
    repo = _repo(tmp_path, "repo-provisional-green",
                 pytest_body=GREEN_PYTEST, gate_coverage=_provisional_coverage())
    report, verdict, out = _run_tests(tmp_path, monkeypatch, repo,
                                      measured_pass=True)

    # What the producer wrote, in its own words.
    assert report["passed"] is True, report["failures"]
    assert report["returncode"] == 0
    assert report["purpose"] == PROVISIONAL_PURPOSE
    assert report[PROVISIONAL_PRODUCER] == PRODUCER_RUN_TESTS
    assert report["full_test_passed"] is False
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_PASS
    assert verdict["full_test_passed"] is False, verdict.get("full_test_passed")

    # The classifier already answers this shape, and it does not call it a pass.
    # The word is the PROVISIONAL one (`provisional`); `partial` belongs to the
    # other fact — a node gate that could not measure every project — and these
    # two were one word until 2026-11-06.
    assert ge.report_state(report) == ge.PROVISIONAL_ROUND
    assert ge.release_disposition(report) == "unresolved"
    assert report["release_evidence"] == "unresolved" == verdict["release_evidence"]

    # The config's own validators PASS it — the mechanism of the defect. A
    # schema-only repair would have had to refuse `passed: true`, which is the
    # real outcome of a real pytest session, and would have sent it down the
    # repair loop instead.
    assert _validate("test_evidence", out)[0] is True
    assert _validate("test_outcome", out)[0] is True

    # …and the shipped routing refuses it anyway, on the producer's own fact.
    transition, target = _select(verdict, out)
    assert target == PROVISIONAL_TERMINAL, (transition, target)
    assert target != "test_evidence" and target != "done"


def test_a_provisional_red_keeps_its_repair_route(tmp_path, monkeypatch):
    """A MEASURED red under the marker is still a red: it goes back to implement.

    Real gate (declaring a provisional coverage), real failing pytest session.
    `run_tests` writes `passed: false` + `returncode: 1` + the stamp; the
    classifier answers `failed` / `known_failure`; the config sends it through
    the evidence/outcome validators to `implement` — never to the provisional
    terminal, and never to `test_evidence_missing`.
    """
    repo = _repo(tmp_path, "repo-provisional-red",
                 pytest_body=RED_PYTEST, gate_coverage=_provisional_coverage())
    report, verdict, out = _run_tests(tmp_path, monkeypatch, repo)

    assert report["passed"] is False
    assert report["returncode"] == 1
    assert report["purpose"] == PROVISIONAL_PURPOSE
    assert report["full_test_passed"] is False
    assert any(f.startswith("FAILED") for f in report["failures"]), report["failures"]
    # The gate measured nothing here, but the pytest red beside it is measured,
    # so the report is not an absence the run may wait on.
    assert report["repo_gate_absent"] is False, report.get("repo_gate_absent")

    assert ge.report_state(report) == "failed"
    assert ge.release_disposition(report) == "known_failure"
    assert verdict["release_evidence"] == "known_failure"

    # The evidence validator accepts it, and the outcome validator refuses a red
    # `passed`, which is the edge that loops back to `implement`.
    assert _validate("test_evidence", out)[0] is True
    assert _validate("test_outcome", out)[0] is False
    outcome = _resolver().resolve_transition("test_outcome", {"all_passed": False}, {})
    assert outcome[1] == "implement", outcome

    transition, target = _select(verdict, out)
    assert target == "test_evidence", (transition, target)
    assert target != PROVISIONAL_TERMINAL


def test_a_complete_green_round_still_reaches_done(tmp_path, monkeypatch):
    """The control: a full-coverage green gate and a green suite keep `done`."""
    repo = _repo(tmp_path, "repo-complete-green",
                 pytest_body=GREEN_PYTEST, gate_coverage=_full_coverage())
    report, verdict, out = _run_tests(tmp_path, monkeypatch, repo)

    assert report["passed"] is True, report["failures"]
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_PASS
    assert "purpose" not in report
    assert "full_test_passed" not in report
    assert verdict["full_test_passed"] is None

    assert ge.report_state(report) == "passed"
    assert ge.release_disposition(report) == "passed"

    assert _validate("test_evidence", out)[0] is True
    assert _validate("test_outcome", out)[0] is True
    transition, target = _select(verdict, out)
    assert target == "test_evidence", (transition, target)
    outcome = _resolver().resolve_transition("test_outcome", {"all_passed": True}, {})
    assert outcome[1] == "done", outcome


def test_a_report_with_no_measured_coverage_still_ends_at_test_evidence_missing(
        tmp_path, monkeypatch):
    """The other control: nothing measured is not a provisional round either.

    A repository with no tests at all: the producer writes `returncode: 5`,
    `no_tests_collected: true` and no marker, the classifier answers `skipped`,
    and the config's `test_evidence` schema refuses it — the shipped terminal for
    this shape, unchanged. Note `passed` is honestly True there: it is the
    probe's `no_tests_collected` term, never `passed`, that ends this run.
    """
    repo = _repo(tmp_path, "repo-no-coverage")
    report, verdict, out = _run_tests(tmp_path, monkeypatch, repo)

    assert report["passed"] is True
    assert report["returncode"] == 5
    assert report["no_tests_collected"] is True, report.get("no_tests_collected")
    assert "purpose" not in report and "full_test_passed" not in report
    assert ge.report_state(report) == "skipped"
    assert ge.release_disposition(report) == "unresolved"

    assert _validate("test_evidence", out)[0] is False
    transition, target = _select(verdict, out)
    assert target == "test_evidence", (transition, target)
    evidence = next(t for t in _resolver().get_node("test_evidence").transitions
                    if t.match == {"all_passed": True})
    assert evidence.to == "test_outcome"
    # and the step's own fall-through edge is the shipped missing-evidence
    # terminal, which the resolver takes when the validator answers false.
    fallthrough = next(t for t in _resolver().get_node("test_evidence").transitions
                       if t.match is None)
    assert fallthrough.to == "test_evidence_missing"


# ────────────────── the four shapes, one table, three readings ───────────────
# The four tests above each pin ONE shape's details. This table pins the
# property they share: for every shape the real producer writes, the producer's
# own key values, `gate_evidence`'s classification and the shipped config's
# resolved destination may not tell three different stories. Every row is
# produced by the REAL `run_tests` (the same seam and helpers the tests above
# use) and all three readings are taken off the SAME report:
#   * the producer's facts, off the report and off the returned verdict (the
#     carrier the router reads) — they may not disagree;
#   * `report_state` / `release_disposition` on that report;
#   * the route the SHIPPED config walks: `test`'s edge from `run_tests`'
#     return, then each validator step from its OWN `all_passed` on the report.
#
# The layers do NOT share a word, and the table does not ask them to: on the
# provisional-green row the producer says `full_test_passed: false` (a boolean),
# `report_state` says `partial` (a classifier word) and the router ends at
# `test_evidence_provisional` (a graph node). What is pinned is the STORY each
# layer tells, derived per layer from its own answer, so a change that makes any
# layer tell a different story for any row turns exactly that row red.
#
# MEASURED (2026-11-06), one mutant per layer, each a read-only overlay of one
# file into the container — the worktree was not edited for any of them:
#   * the edge removed from the config -> the provisional-green row red on the
#     route (`('test_evidence', 'test_outcome', 'done')` for the expected
#     `('test_evidence_provisional',)`), the other three rows green;
#   * its `passed: true` term dropped -> the provisional-RED row red on the
#     route (a red routed to the terminal), the other three green;
#   * `report_state` made to answer the marker `passed` -> the
#     provisional-green row red on the classifier pair
#     (`('passed', 'unresolved')` for `('provisional', 'unresolved')`; measured
#     when the row still expected `('partial', 'unresolved')`, i.e. before the
#     two facts were given one word each);
#   * `run_tests`' `full_test_passed` stamp removed -> BOTH provisional rows red
#     on the producer facts (`None` for `False`).
# So every one of the three readings is load-bearing, and a row names which
# layer moved.
ABSENT = object()          # the key must not be on the report at all

SHAPES = [
    pytest.param(
        {"pytest_body": GREEN_PYTEST, "gate_coverage": _provisional_coverage()},
        {"measured_pass": True},
        {"passed": True, "returncode": 0, "purpose": PROVISIONAL_PURPOSE,
         PROVISIONAL_PRODUCER: PRODUCER_RUN_TESTS, "full_test_passed": False,
         "no_tests_collected": ABSENT},
        # The PROVISIONAL word (`gate_evidence.PROVISIONAL_ROUND`). It was
        # `partial` until 2026-11-06, when that word was left to the other fact
        # it also named: a node gate that could not measure every project.
        ("provisional", "unresolved"),
        (PROVISIONAL_TERMINAL,),
        "not_full_evidence",
        id="provisional-green",
    ),
    pytest.param(
        {"pytest_body": RED_PYTEST, "gate_coverage": _provisional_coverage()},
        {},
        {"passed": False, "returncode": 1, "purpose": PROVISIONAL_PURPOSE,
         "full_test_passed": False, "repo_gate_absent": False,
         "no_tests_collected": ABSENT},
        ("failed", "known_failure"),
        ("test_evidence", "test_outcome", "implement"),
        "repair",
        id="provisional-red",
    ),
    pytest.param(
        {"pytest_body": GREEN_PYTEST, "gate_coverage": _full_coverage()},
        {},
        {"passed": True, "returncode": 0, "purpose": ABSENT,
         "full_test_passed": ABSENT, "no_tests_collected": ABSENT},
        ("passed", "passed"),
        ("test_evidence", "test_outcome", "done"),
        "full_evidence",
        id="complete-green",
    ),
    pytest.param(
        {},
        {},
        {"passed": True, "returncode": 5, "purpose": ABSENT,
         "full_test_passed": ABSENT, "no_tests_collected": True},
        ("skipped", "unresolved"),
        ("test_evidence", "test_evidence_missing"),
        "absence",
        id="no-measured-coverage",
    ),
]

# The story each layer's answer has to mean. One vocabulary per layer, and none
# of them is the other's: a state word, a graph node, and the producer's facts.
# `partial` (a node gate that could not measure every project) and
# `provisional` (a provisional diagnostics round) are two words for two facts
# since 2026-11-06; neither is a full pass, and this table keeps BOTH so that a
# state the vocabulary can answer never silently loses its story.
STORY_BY_STATE = {"passed": "full_evidence", "failed": "repair",
                  "provisional": "not_full_evidence",
                  "partial": "not_full_evidence", "skipped": "absence"}
STORY_BY_TERMINAL = {"done": "full_evidence", "implement": "repair",
                     PROVISIONAL_TERMINAL: "not_full_evidence",
                     "test_evidence_missing": "absence"}


def _producer_story(report) -> str:
    """What the producer's OWN written facts say this round is.

    Read only from the report: an absence is `no_tests_collected`, a red is a
    `passed` that is not true, and a green that carries the provisional marker
    (or the `full_test_passed: false` beside it) is not full test evidence.
    """
    if report.get("no_tests_collected") is True:
        return "absence"
    if report.get("passed") is not True:
        return "repair"
    if (report.get("full_test_passed") is False
            or report.get("purpose") == PROVISIONAL_PURPOSE):
        return "not_full_evidence"
    return "full_evidence"


def _route(verdict, out) -> tuple:
    """Every destination the SHIPPED config walks from `test` to the run's end.

    `test` is routed by the engine's resolver over `run_tests`' return; each
    validator step is routed by that resolver over its OWN validator's
    `all_passed` on the same report — exactly the flags the engine hands it.
    """
    _, target = _select(verdict, out)
    targets = [target]
    while target in ("test_evidence", "test_outcome"):
        passed, _ = _validate(target, out)
        _, target = _resolver().resolve_transition(target, {"all_passed": passed}, {})
        targets.append(target)
    return tuple(targets)


@pytest.mark.parametrize(
    "repo_kwargs,run_kwargs,producer_facts,classifier,route,story", SHAPES)
def test_the_four_shapes_tell_one_story_to_the_producer_the_classifier_and_the_router(
        repo_kwargs, run_kwargs, producer_facts, classifier, route, story,
        tmp_path, monkeypatch):
    """Criterion 3: one row per shape, three readings off the SAME real report."""
    repo = _repo(tmp_path, "repo-table", **repo_kwargs)
    report, verdict, out = _run_tests(tmp_path, monkeypatch, repo, **run_kwargs)

    # 1. The producer's own key values, on the report and on its return.
    for key, expected in producer_facts.items():
        if expected is ABSENT:
            assert key not in report, (key, report.get(key))
        else:
            assert report.get(key) == expected, (key, report.get(key), expected)
    # The RETURN is the carrier the router reads; it may not disagree.
    assert verdict.get("purpose") == report.get("purpose")
    assert verdict.get("full_test_passed") == report.get("full_test_passed")

    # 2. The classifier's answer for that same report.
    state, disposition = ge.report_state(report), ge.release_disposition(report)
    assert (state, disposition) == classifier, (state, disposition, classifier)

    # 3. The destination the shipped config resolves to, step by step.
    walked = _route(verdict, out)
    assert walked == route, (walked, route)

    # One story, three vocabularies — and never one shared word.
    assert _producer_story(report) == story, (report.get("passed"), story)
    assert STORY_BY_STATE[state] == story, (state, story)
    assert STORY_BY_TERMINAL[walked[-1]] == story, (walked, story)
    if story == "not_full_evidence":
        # The row that makes the point: the producer's boolean, the classifier's
        # state word and the graph node are three different values, so no layer
        # can be passing this table by agreeing on a spelling.
        assert len({repr(report["full_test_passed"]), state, walked[-1]}) == 3


# ─────────────────────────────────── end to end ─────────────────────────────
def _engine(tmp_path, monkeypatch, *, gate_coverage=None, pytest_body=None,
            measured_pass=False):
    """Drive the REAL config through the REAL host and the REAL `run_tests`.

    Returns (status, implement_runs, run, reports). `reports` are the
    `test_report.json` payloads the real tool wrote, read by the same wrapper the
    other `coding_impl` suites use.
    """
    from tests.skillflow.test_coding_impl_gate import _drive, _wire

    install_entry_executor(monkeypatch)
    counter = tmp_path / "gate_calls.txt"
    observations = []

    def prepare(project):
        if pytest_body is not None:
            (project / "tests").mkdir(parents=True, exist_ok=True)
            (project / "tests" / "test_contract.py").write_text(pytest_body)
        if gate_coverage is not None:
            (project / "gate.py").write_text(embed_env(
                GATE, GATE_COVERAGE=json.dumps(gate_coverage)))
            script = project / "run_tests.sh"
            script.write_text(f"#!/bin/sh\nexec {sys.executable} gate.py\n")
            script.chmod(0o755)
        (project / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        from skillflow.output_targets import git
        git(project, "add", "-A")
        git(project, "commit", "-qm", "fixture: the repository's own gate")

    sf, run_id, _calls = _wire(tmp_path, [True], real_run_tests=True,
                               repo_prepare=prepare, gate_counter=counter,
                               gate_observations=observations)
    if measured_pass:
        # Serve the acquisition ON THE TOOL THE ENGINE LOADED. The loader
        # imports `impl.py` by PATH (`spec_from_file_location`), so this is a
        # different module object from `aitelier.tools.run_tests.impl`; patching
        # that module would not reach the running step. `__wrapped__` unwraps
        # the observer `_wire` installed.
        loaded = sf._tool_loader.load_fn("run_tests")
        inner = getattr(loaded, "__wrapped__", loaded)
        inner.__globals__["_acquire_repo_gate"] = (
            lambda _repo, **_kwargs: _provisional_measured_pass())
    status, implement_runs = _drive(sf, run_id)
    reports = [row["report"] for row in observations if row["phase"] == "test"]
    return status, implement_runs, sf.get_run(run_id), reports


def test_the_graph_fails_a_provisional_green_round_instead_of_completing(
        tmp_path, monkeypatch):
    """The criterion, measured on the run: not `completed`, and it says why.

    The run must end at the provisional terminal, spend no repair cycle (there is
    nothing for the implementer to fix), and never reach `done`.
    """
    status, implement_runs, run, reports = _engine(
        tmp_path, monkeypatch, pytest_body=GREEN_PYTEST, measured_pass=True)

    assert reports, "the real tool wrote no test report"
    report = reports[-1]
    assert report["passed"] is True and report["full_test_passed"] is False

    # `current_node` stays on the step that routed (the engine ends the run on
    # the gate it reached, as it does for `test_evidence_missing`), so the
    # ending is read off the run row's reason — the same way the shipped
    # `coding_impl` suites read the absence terminal.
    assert status == "failed", (status, run)
    assert PROVISIONAL_TERMINAL in str(run.get("error_reason") or ""), run
    assert run.get("current_node") != "done", run
    assert implement_runs == 1, implement_runs


def test_the_graph_still_completes_a_complete_green_round(tmp_path, monkeypatch):
    """The control on the run: a fully measured green round still completes."""
    status, implement_runs, run, reports = _engine(
        tmp_path, monkeypatch, pytest_body=GREEN_PYTEST,
        gate_coverage=_full_coverage())

    report = reports[-1]
    assert report["passed"] is True
    assert "purpose" not in report and "full_test_passed" not in report

    assert status == "completed", (status, run)
    assert implement_runs == 1, implement_runs


def test_the_graph_still_fails_a_report_with_no_measured_coverage(
        tmp_path, monkeypatch):
    """The control that keeps the two shapes apart on the run itself."""
    status, implement_runs, run, reports = _engine(tmp_path, monkeypatch)

    report = reports[-1]
    assert report["no_tests_collected"] is True

    assert status == "failed", (status, run)
    assert "test_evidence_missing" in str(run), run
    assert PROVISIONAL_TERMINAL not in str(run), run
    assert implement_runs == 1, implement_runs


# ────────────────────────────────── the config shape ────────────────────────
def _test_step(graph):
    return next(s for s in graph.steps if s.id == "test")


def test_the_provisional_edge_is_a_bare_key_flag_match():
    """WHY the bare-key form: `field`/`value` would drop the `passed` term.

    `skillflow.graph._flags_match` RETURNS on `{field, value}` and never reads
    the rest of the dict, so `{field: full_test_passed, value: false, passed:
    true}` would match every provisional report — a provisional RED included,
    which is a repair, not a terminal. The edge is therefore written as direct
    flag keys, and both terms are asserted here.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    edges = {t.to: t.match for t in _test_step(graph).transitions}

    assert edges[PROVISIONAL_TERMINAL] == PROVISIONAL_MATCH
    match = edges[PROVISIONAL_TERMINAL]
    assert "field" not in match and "value" not in match, match
    assert "from_file" not in match, match


def test_the_provisional_edge_is_ordered_after_absence_and_before_the_written_edge():
    """Order is load-bearing (the engine takes the first match).

    * the absence edges come FIRST: a report that is an absence still parks at
      `test_gate_absent`, and a red the gate cannot attribute still ends on its
      own terminal — `full_test_passed: false` rides those shapes too;
    * the `written` edge comes after: a provisional round is decided before the
      report is handed to a validator that accepts it.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    targets = [t.to for t in _test_step(graph).transitions]
    index = targets.index(PROVISIONAL_TERMINAL)
    for before in ("test_gate_report_unattributable", "test_gate_absent",
                   "failed_candidate_evidence"):
        assert targets.index(before) < index, (before, targets)
    assert index < targets.index("test_evidence"), targets


def test_the_terminal_is_a_named_failed_gate_outside_the_loop():
    """A terminal, not a loop-back and not a park: it must END the run.

    Its only edge is `to: null`, and an end condition names it `failed` — a gate
    with no outgoing edge and no end condition would park a live run forever
    instead of failing it.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    terminal = next(s for s in graph.steps if s.id == PROVISIONAL_TERMINAL)
    assert terminal.step_type == "gate"
    assert [t.to for t in terminal.transitions] == [None]

    failed = [c for c in graph.end_conditions.conditions
              if c.type == "node_reached" and c.node == PROVISIONAL_TERMINAL]
    assert failed and all(c.result == "failed" for c in failed), failed
    # `done` stays the only success terminal among these evidence nodes.
    completed = [c.node for c in graph.end_conditions.conditions
                 if c.result == "completed"]
    assert PROVISIONAL_TERMINAL not in completed, completed


def test_the_provisional_verdict_is_not_read_from_the_report_file():
    """The carrier is the tool's RETURN, for the same two reasons as an absence.

    A `from_file` reader is resolved against the EVALUATING step's own output
    directory and the engine's `_flags_match` raises on a report that parses to
    anything but an object; the flag on `run_tests`' return needs no reader at
    all. No edge of this config may read `full_test_passed` out of a file.
    """
    graph = PipelineGraph.from_yaml(CONFIG)
    for step in graph.steps:
        for t in step.transitions:
            match = t.match or {}
            assert not (match.get("from_file")
                        and match.get("field") in ("full_test_passed",
                                                   "purpose")), (step.id, t.to, match)


def test_the_producer_states_the_fact_on_both_carriers(tmp_path, monkeypatch):
    """The config may only route on a fact the producer actually writes.

    Both carriers are the real producer's: the report file (what
    `gate_evidence` reads) and the verdict dict (what the engine routes on).
    Neither may disagree, and the complete-green control must not carry the
    flag at all — an absent flag must never be readable as "provisional".
    """
    complete_repo = _repo(tmp_path, "repo-carriers-complete",
                          pytest_body=GREEN_PYTEST,
                          gate_coverage=_full_coverage())
    complete, complete_verdict, _out = _run_tests(
        tmp_path, monkeypatch, complete_repo)
    provisional_repo = _repo(tmp_path, "repo-carriers-provisional",
                             pytest_body=GREEN_PYTEST,
                             gate_coverage=_provisional_coverage())
    provisional, provisional_verdict, _out2 = _run_tests(
        tmp_path, monkeypatch, provisional_repo, measured_pass=True)

    assert provisional["full_test_passed"] is provisional_verdict["full_test_passed"] is False
    assert provisional["purpose"] == provisional_verdict["purpose"] == PROVISIONAL_PURPOSE
    assert "full_test_passed" not in complete
    assert complete_verdict["full_test_passed"] is None
    assert complete_verdict["purpose"] is None
    assert complete["passed"] is provisional["passed"] is True
