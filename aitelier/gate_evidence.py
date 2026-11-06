"""Current-cycle identity and fail-closed audit for deterministic gate reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
from aitelier.gate_coverage import (
    PRODUCER_RUN_TESTS, PROVISIONAL_PRODUCER, PROVISIONAL_PURPOSE,
    provisional_producer, publication_scope_refusal)

CYCLE_FIELD = "evidence_cycle_id"
RUN_FIELD = "run_id"
CYCLE_MANIFEST = ".evidence_cycle.json"
_SKIP_MARKERS = ("gate_skipped", "skipped", "skipped_because", "blind",
                 "no_tests_collected")
_UNRUN_STATUSES = {"not_run", "not-run", "not run", "unrun", "unexecuted"}
_PENDING_STATUSES = {"pending", "queued", "running", "in_progress", "in-progress"}
_INFRA_STATUSES = {"infrastructure_unavailable", "infrastructure-unavailable",
                   "infra_unavailable", "runner_unavailable"}
_KNOWN_FAILURE_STATES = {"failed", "known_failure"}
# A red whose report could not be attributed to the gate run (run_tests
# `repo_gate.report_attribution`): not a confirmed product failure.
_UNATTRIBUTABLE_STATUSES = {"unattributable"}
# `passed_relative: true` is a CLAIM that the failures were already in the repo.
# It is only readable beside a `baseline_state` that says a baseline was
# actually taken or read — see `run_tests.impl.BASELINE_MEASURED`. Any other
# value, and the absence of the field, mean nothing was compared.
_BASELINE_MEASURED = {"seeded", "compared"}
# `unmeasured` is the fourth value `run_tests._apply_baseline` can write: an
# absence whose run found no baseline to compare against. It is NOT a
# measurement in either direction — it may not seed a baseline and it may not
# report `passed_relative: true` — so it is absent from `_BASELINE_MEASURED`
# above on purpose, and a reader that sees it must not read the red as old.

# WHY a project was not measured, in the producer's own words. `run_tests`
# writes this `kind` on every entry of `node["unmeasured"]` and
# `node_unmeasured`, so that no reader has to parse the sentence beside it to
# know what to do about it. It does NOT change the disposition: see
# `release_disposition` for why every absence still parks, and where the
# permanence was actually removed instead.
ABSENCE_CAPPED = "capped"                      # past NODE_PROJECT_CAP, no declaration
ABSENCE_UNDECLARED = "undeclared"             # the repo declared a set; this was not in it
ABSENCE_DECLARATION = "declaration"           # the repo's own declaration is unusable
ABSENCE_UNREADABLE = "unreadable"             # package.json/lockfile not a plain file
# Reserved: nothing emits this today. An install or fetch that fails is a
# MEASURED red today (`_run_node_project` returns it as a failed check), so this
# word exists for the producer that learns to tell "this cache cannot satisfy
# this lockfile" from "this project is broken" — that difference should park a
# run, not charge the implementer.
ABSENCE_UNPROVISIONABLE = "unprovisionable"   # install/fetch cannot be satisfied here
ABSENCE_ISOLATION = "isolation"               # the executor refused (or lost) isolation
ABSENCE_TICKET = "ticket"                     # run-owned ticket/snapshot not prepared
ABSENCE_RAISED = "raised"                     # the project's own run raised


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("report is not a JSON object")
    return data


def _manifest_path(graph_dir: Path) -> Path:
    return Path(graph_dir) / CYCLE_MANIFEST


def _write_cycle_manifest(graph_dir: Path, *, run_id: str, cycle: str,
                          generated_at: str) -> None:
    """Atomically publish the current cycle outside every audited report."""
    path = _manifest_path(graph_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    tmp.write_text(json.dumps({
        "version": 1,
        RUN_FIELD: run_id,
        CYCLE_FIELD: cycle,
        "evidence_generated_at": generated_at,
    }, indent=2), encoding="utf-8")
    tmp.replace(path)


def _current_cycle(graph_dir: Path, run_id: str) -> str:
    if not run_id:
        raise ValueError("current run_id is empty")
    manifest = _read_json(_manifest_path(graph_dir))
    cycle = manifest.get(CYCLE_FIELD)
    if manifest.get(RUN_FIELD) != run_id or not isinstance(cycle, str) or not cycle:
        raise ValueError("cycle manifest is not from this run/current cycle")
    return cycle


def stamp_report(report: dict, *, run_id: str, out_dir: str,
                 start_cycle: bool = False, cycle_from: str = "5_test",
                 cycle_file: str = "test_report.json") -> dict:
    """Stamp a report from an independent run-scoped cycle manifest."""
    if not isinstance(report, dict):
        return report
    generated_at = datetime.now(timezone.utc).isoformat()
    report[RUN_FIELD] = run_id
    report["evidence_generated_at"] = generated_at
    graph_dir = Path(out_dir).parent

    def stamp() -> None:
        # BOTH answers are read from the report AS THE PRODUCER WROTE IT, and
        # only then written back. `report_state`/`release_disposition` read
        # `evidence_state` (it is how a producer says "not_run", "unattributable",
        # "infrastructure_unavailable"), so annotating the report with the first
        # answer before asking the second question made the second answer read
        # the annotation instead of the evidence: measured on a provisional
        # report whose gate measured nothing, `report_state` said `partial`,
        # that overwrote `not_run`, and `release_disposition` then read `partial`
        # as a measured red and released it as `known_failure` — an ABSENCE
        # reading as a FAILURE, which is the opposite error to the one this
        # module exists to prevent, and worse for a release than the `unresolved`
        # it replaced.
        state = report_state(report)
        disposition = release_disposition(report)
        report["evidence_state"] = state
        report["release_evidence"] = disposition

    if start_cycle:
        cycle = f"{run_id}:{uuid4().hex}"
        report[CYCLE_FIELD] = cycle
        stamp()
        _write_cycle_manifest(graph_dir, run_id=run_id, cycle=cycle,
                              generated_at=generated_at)
        return report

    # Kept in the signature for graph/tool compatibility. The old implementation
    # read cycle_from/cycle_file as evidence and thereby trusted an audited report
    # as its own freshness oracle. The manifest is the only current-cycle source.
    del cycle_from, cycle_file
    try:
        report[CYCLE_FIELD] = _current_cycle(graph_dir, run_id)
    except Exception as exc:
        report[CYCLE_FIELD] = ""
        report["passed"] = False
        report["skipped_because"] = "evidence_cycle_missing"
        report["evidence_stamp_error"] = f"{_manifest_path(graph_dir)}: {exc}"
    stamp()
    return report


def stamp_file(path: Path, *, run_id: str, out_dir: str,
               cycle_from: str = "5_test") -> None:
    """Stamp a report written by a chained tool. Never hides a gate failure."""
    path = Path(path)
    try:
        report = _read_json(path)
    except Exception as exc:
        report = {"passed": False, "skipped_because": "report_unreadable",
                  "summary": f"{path.name} could not be read after the gate: {exc}"}
    stamp_report(report, run_id=run_id, out_dir=out_dir,
                 cycle_from=cycle_from)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def _absence_state(report: dict, raw_passed) -> str | None:
    """The state of a report that measured nothing, or None when it measured.

    Split out of `report_state` so that the provisional marker's branch can ask
    the SAME question — "is this red a measurement, or an absence wearing
    `passed: false`?" — with the same predicates, instead of a second copy of
    them that could drift from the classifier. Every branch below is the one
    `report_state` had inline; the order is load-bearing (a report that is both
    pending and red is pending, not failed).
    """
    status = str(report.get("evidence_state") or report.get("status") or "").strip().lower()
    if report.get("pending") is True or status in _PENDING_STATUSES:
        return "pending"
    if (report.get("infrastructure_unavailable") is True
            or status in _INFRA_STATUSES):
        return "infrastructure_unavailable"
    if report.get("blind") is True or report.get("blind_builder") is True:
        return "blind"
    if status in _UNATTRIBUTABLE_STATUSES:
        return "unattributable"
    # A node gate that measured SOME projects and could not measure others is
    # not a pass: for every consumer that exists, an absence nobody renders is
    # no absence at all. `passed` is honestly true there (the measured projects
    # passed), so the report needs a state of its own rather than the word
    # "passed". Anything that DID fail is handled below, unchanged.
    if report.get("node_unmeasured") and raw_passed is True:
        return "partial"
    markers = [name for name in _SKIP_MARKERS if report.get(name)]
    unrun = (report.get("unrun") is True or report.get("ran") is False
             or report.get("executed") is False or status in _UNRUN_STATUSES)
    if markers or unrun:
        return "skipped"
    return None


def _provisional_red_is_measured(report: dict) -> bool:
    """Is this provisional report's `passed: false` a RED that must be repaired?

    Both producers write `purpose: provisional_round_feedback` and only the
    field beside it says which one did (`gate_coverage.PROVISIONAL_PRODUCER`):

      * `round_feedback.run_feedback` builds its report with `passed: False` BY
        CONSTRUCTION. A feedback round is not a gate result, there is nothing
        for an implementer to repair in it, and `partial` is its shipped,
        pinned answer — so it must never take this branch.
      * `run_tests` stamps the same marker on ITS report when the round's
        repository-gate coverage was provisional, and there `passed` is the
        real pytest outcome. A red there was answered `partial` by the marker
        alone, i.e. softened into "not a red", and never reached the repair
        path that a red is for.

    So three conditions, and all three are needed: the marker names
    `run_tests`, the report says `passed: false`, and that false is a
    MEASUREMENT (`_absence_state` finds no absence behind it). A gate that
    measured nothing is not a red — it parks exactly as it did before, and the
    green control beside a red keeps its `partial` too.
    """
    if (report.get("passed", report.get("all_passed")) is not False
            or report.get("purpose") != PROVISIONAL_PURPOSE
            or provisional_producer(report) != PRODUCER_RUN_TESTS):
        return False
    # `repo_gate_absent` is the producer's OWN verdict on its own gate: the
    # gate measured nothing and nothing else in this report measured red
    # either. It is read here — in a branch that exists only for this producer
    # — because `stamp_report` annotates `evidence_state` with THIS function's
    # answer, so a test that looked only at the state vocabulary would re-read
    # its own annotation on the second pass and charge an absence as a
    # failure. The vocabulary still decides the pytest-side absences
    # (`infrastructure_unavailable`, `skipped_because`, `no_tests_collected`…),
    # which this flag knows nothing about.
    if report.get("repo_gate_absent") is True:
        return False
    return _absence_state(report, False) is None


def report_state(report: dict) -> str:
    """Return one release state without collapsing distinct non-pass outcomes."""
    raw_passed = report.get("passed", report.get("all_passed"))
    # The provisional marker is answered `partial` BEFORE any classification,
    # because a feedback round has no gate verdict to classify — with ONE
    # exception, the reason this branch is not a bare string compare: a
    # `run_tests` report whose own pytest leg failed is a red, and `passed` is
    # the real outcome there. See `_provisional_red_is_measured`.
    if (report.get("purpose") == PROVISIONAL_PURPOSE
            and not _provisional_red_is_measured(report)):
        return "partial"
    if raw_passed is not True and raw_passed is not False:
        return "unreadable"
    absent = _absence_state(report, raw_passed)
    if absent:
        return absent
    if raw_passed is False:
        if report.get("passed_relative") is True:
            # THIS is the line that reads the distinction. `known_failure`
            # RELEASES the run past the hold (game_harness: 5_compile ->
            # 5_vision); `unattributed` collapses to `unresolved` below, which
            # routes to 5_release_wait instead. A report that never compared
            # against a baseline has not earned the first one: it is telling us
            # the failures are old while holding no record of what was old.
            if str(report.get("baseline_state") or "") in _BASELINE_MEASURED:
                return "known_failure"
            return "unattributed"
        return "failed"
    return "passed"


def release_disposition(report: dict) -> str:
    """Collapse a report only as far as release routing safely allows.

    A confirmed product failure may suppress expensive downstream work and
    re-enter planning. Evidence that is absent, stale, pending, blind, skipped,
    unreadable, UNATTRIBUTED (a relative pass with no baseline behind it), or
    blocked by infrastructure needs verification instead.

    WHAT A PARTIALLY MEASURED NODE GATE MEANS TO THE RELEASE (decided
    2026-11-06, `design.gate-absence-has-a-disposition-20261106`).

    Three states exist and stay apart: every project measured and green
    (`passed`), a measured project failed (`failed`, decided below), and some
    project not measured (`partial` — `report_state` refuses to call that
    "passed" for exactly this reason).

    THE DECISION: a partial gate stays `unresolved`. It parks, and it parks at
    the human checkpoint. That is not a compromise forced by caution; releasing
    on a recorded gap was the candidate answer and it is wrong, for one reason
    that no wording can fix: the projects that were not measured include the
    one that would have failed, and a release record nobody reads is not
    evidence. "Absence never reads as a pass" is only true if absence does not
    release.

    THE DEFECT WAS NOT THE DISPOSITION. It was that the third state had no
    remedy: a repository could not do anything to stop being truncated. A park
    is a request for evidence, and a request nobody can answer is not a gate,
    it is a tax — it teaches the person clicking it to click through it, and
    that lesson transfers to the parks that matter. So the fix belongs in the
    producer, not here: `run_tests._find_node_projects` now takes the
    repository's own coverage declaration (`node_projects.json`) and holds it
    against discovery, so a repository that states its node projects is
    measured in full up to the declaration ceiling, and a project that the
    declaration omits — or a declaration the gate will not honour — is reported
    as an absence that NAMES the one edit which removes it.

    The disposition is unchanged here on purpose — but "unchanged here" is not
    "unchanged everywhere", and an earlier version of this paragraph claimed the
    stronger thing and was measured false. Two of this repository's own
    pipelines (`configs/fix_tests.yaml`, `configs/subagent.yaml`) WERE reading a
    partial report as success through its raw `passed: true`, and had to be
    taught a new way: `run_tests` now writes `node_gate_complete`, and both
    route it to a terminal that fails the run. `configs/coding_impl.yaml`'s
    `test_evidence` was already refusing such a report on `node.skipped`. So the
    honest scope of the claim is: this release layer (`report_state`,
    `audit_evidence`, `release_disposition`) plus every pipeline this
    repository ships as a config. What stays outside that scope is named rather
    than implied — generated pipeline records in the live deployment that branch
    on the raw `passed`.

    THE PROVISIONAL MARKER IS NOT ONE SHAPE (measured 2026-11-06). Two
    producers stamp `purpose: provisional_round_feedback`, and this function
    used to answer both `unresolved` on the string alone. `round_feedback`'s
    report IS a feedback round — not a gate result, nothing to repair — and
    `unresolved` is right for it, unchanged. `run_tests` stamps the same marker
    on a TEST report whose repository-gate coverage was provisional, where
    `passed` is the real pytest outcome; answering THAT red `unresolved`
    swapped a repair for a park and kept a real breakage out of the
    implementer's hands. So the marker now names its producer
    (`gate_coverage.provisional_producer`) and only a MEASURED red from
    `run_tests` is classified as the red it is — `failed` above, therefore
    `known_failure` below. An unlabelled marker keeps the older, stricter
    answer.

    Rejected, and why:
    * (a) "a distinct bounded state that lets the release proceed and records
      what was not measured" — see above: it converts an absence into a
      release, and the record does not fail a test, so it changes nothing about
      the code that ships.
    * (b) "keep parking, raise the cap" — raising a bound moves the cliff.
      Whatever number is chosen, the state comes back at N+1, and the
      disposition would again depend on the size of the repository rather than
      on whether anything is wrong with it. The declaration removes the
      dependency instead of shifting it.
    * counting the absence as a red — measured as "worse than the defect being
      fixed": every run of the repository red for a reason no code change can
      touch.

    Two properties are restated here because both are load-bearing and both are
    preserved by this decision: an absence never reads as a pass to any
    consumer (this function returns "unresolved", never "passed"), and a
    measured red is not softened beside it (`partial` is reachable only when
    `passed` is honestly True, so a failed project arrives here as `failed`).

    What "not softened" means exactly, because an earlier version of this
    paragraph overstated it and a reviewer measured the difference: `failed`
    and `known_failure` are both in `_KNOWN_FAILURE_STATES`, so a red leaves
    this function as `known_failure`, which is the value the pipelines route to
    their repair/replanning steps. It never becomes `passed`, and it never
    becomes `unresolved` on the strength of an absence beside it. The naming
    conflates "a failure we know about" with "a failure that was already
    there" — a separate, inherited defect, recorded rather than renamed here,
    because renaming a release value is a contract change of its own.
    """
    if (report.get("purpose") == PROVISIONAL_PURPOSE
            and not _provisional_red_is_measured(report)):
        return "unresolved"
    state = str(report.get("upstream_state") or report_state(report))
    if (not report.get("upstream_state")
            and report.get("skipped_because") == "upstream_failed"):
        state = "failed"
    if state == "passed":
        if "repo_gate" in report or "gate_coverage" in report:
            if publication_scope_refusal(report.get("gate_coverage")):
                return "unresolved"
        return "passed"
    if state in _KNOWN_FAILURE_STATES:
        return "known_failure"
    return "unresolved"


def first_upstream_blocker(graph_dir: Path, run_id: str, gates) -> dict | None:
    """Return the first current-cycle gate that is not a clean pass.

    This is the fail-fast decision shared by expensive downstream gates. A
    missing manifest/report and a stale report are blockers too; running a new
    gate cannot turn absent evidence into release evidence.
    """
    graph_dir = Path(graph_dir)
    if isinstance(gates, str):
        try:
            gates = json.loads(gates)
        except (TypeError, json.JSONDecodeError) as exc:
            return {"step": "", "file": "", "state": "unreadable",
                    "passed": False, "skipped_because": "invalid_gate_declaration",
                    "summary": f"Fail-fast gate declaration is unreadable: {exc}"}
    if not gates:
        return None
    try:
        expected_cycle = _current_cycle(graph_dir, run_id)
    except Exception as exc:
        return {"step": "", "file": CYCLE_MANIFEST, "state": "stale",
                "passed": False, "skipped_because": "evidence_cycle_missing",
                "summary": f"Cannot establish current evidence cycle: {exc}"}
    for pair in gates:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return {"step": str(pair), "file": "", "state": "unreadable",
                    "passed": False, "skipped_because": "invalid_gate_declaration",
                    "summary": f"Invalid gate declaration: {pair!r}"}
        step_id, filename = map(str, pair)
        entry = _audit_one(graph_dir, step_id, filename, run_id, expected_cycle)
        if not entry["passed"]:
            try:
                source = _read_json(graph_dir / step_id / filename)
            except Exception:
                source = {}
            if source.get("upstream_state"):
                entry["upstream_state"] = source["upstream_state"]
            return entry
    return None


def upstream_failed_report(blocker: dict, gate: str) -> dict:
    """Build a fresh marker without conflating failure and uncertainty."""
    upstream = f"{blocker.get('step')}/{blocker.get('file')}".strip("/")
    state = str(blocker.get("upstream_state") or blocker.get("state") or "failed")
    if (not blocker.get("upstream_state") and state == "skipped"
            and blocker.get("skipped_because") == "upstream_failed"):
        disposition = "known_failure"
    else:
        disposition = release_disposition({"passed": False,
                                           "upstream_state": state})
    because = ("upstream_failed" if disposition == "known_failure"
               else "upstream_unresolved")
    return {
        "passed": False,
        "gate_skipped": True,
        "skipped_because": because,
        "upstream_state": state,
        "upstream_gate": upstream,
        "release_evidence": disposition,
        "summary": (f"{gate} not run because upstream release evidence "
                    f"{upstream or '<unknown>'} is {state}."),
    }


def audit_evidence(graph_dir: Path, run_id: str, gates: list) -> dict:
    """Audit gate reports against an independent current-cycle identity."""
    graph_dir = Path(graph_dir)
    verdict = {
        "run_id": run_id,
        CYCLE_FIELD: "",
        "state": "fresh",
        "passed": True,
        "stale_reports": [],
        "missing_reports": [],
        "skipped_gates": [],
        "failed_gates": [],
        "known_failures": [],
        "pending_gates": [],
        "blind_gates": [],
        "infrastructure_unavailable_gates": [],
        "incomplete_coverage_gates": [],
        "gates_audited": [],
    }
    if not gates:
        verdict.update(state="audit_error", passed=False,
                       summary="No gate reports were declared; zero evidence cannot pass.")
        return verdict

    try:
        expected_cycle = _current_cycle(graph_dir, run_id)
        if not isinstance(expected_cycle, str) or not expected_cycle:
            raise ValueError("expected evidence cycle is empty")
        verdict[CYCLE_FIELD] = expected_cycle
    except Exception as exc:
        expected_cycle = ""
        verdict.update(state="stale", passed=False,
                       summary=f"Cannot establish current evidence cycle: {exc}")

    for pair in gates:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            entry = {"step": str(pair), "file": "", "state": "unreadable",
                     "passed": False, "skipped_because": "invalid_gate_declaration",
                     "summary": f"Invalid gate declaration: {pair!r}"}
        else:
            step_id, filename = map(str, pair)
            entry = _audit_one(graph_dir, step_id, filename, run_id,
                               expected_cycle)
        verdict["gates_audited"].append(entry)
        if entry["passed"]:
            continue
        verdict["passed"] = False
        bucket = {
            "stale": "stale_reports", "unreadable": "stale_reports",
            "missing": "missing_reports", "skipped": "skipped_gates",
            "failed": "failed_gates", "known_failure": "known_failures",
            "pending": "pending_gates", "blind": "blind_gates",
            "infrastructure_unavailable": "infrastructure_unavailable_gates",
            "coverage_incomplete": "incomplete_coverage_gates",
            "partial": "incomplete_coverage_gates",
        }.get(entry["state"], "failed_gates")
        verdict[bucket].append(entry)

    if not verdict["passed"]:
        for state, bucket in (
            ("stale", "stale_reports"), ("missing", "missing_reports"),
            ("infrastructure_unavailable", "infrastructure_unavailable_gates"),
            ("blind", "blind_gates"), ("pending", "pending_gates"),
            ("known_failure", "known_failures"), ("failed", "failed_gates"),
            ("coverage_incomplete", "incomplete_coverage_gates"),
            ("skipped", "skipped_gates"),
        ):
            if verdict[bucket]:
                verdict["state"] = state
                break
    return verdict


def _audit_one(graph_dir: Path, step_id: str, filename: str, run_id: str,
               expected_cycle: str) -> dict:
    path = graph_dir / step_id / filename
    entry = {"step": step_id, "file": filename, "state": "fresh",
             "passed": True, "skipped_because": None}
    if not path.is_file():
        entry.update(state="missing", passed=False,
                     skipped_because="report_missing",
                     summary=f"{step_id}/{filename} is absent; the gate did not produce current evidence")
        return entry
    try:
        report = _read_json(path)
    except Exception as exc:
        entry.update(state="unreadable", passed=False,
                     skipped_because="report_unreadable",
                     summary=f"{step_id}/{filename} is unreadable: {exc}")
        return entry

    entry[RUN_FIELD] = report.get(RUN_FIELD)
    entry[CYCLE_FIELD] = report.get(CYCLE_FIELD)
    if (not expected_cycle or report.get(RUN_FIELD) != run_id
            or report.get(CYCLE_FIELD) != expected_cycle):
        entry.update(state="stale", passed=False,
                     summary=(f"{step_id}/{filename} belongs to run/cycle "
                              f"{report.get(RUN_FIELD)!r}/{report.get(CYCLE_FIELD)!r}, "
                              f"expected {run_id!r}/{expected_cycle!r}"))
        return entry

    state = report_state(report)
    if report.get("purpose") == PROVISIONAL_PURPOSE:
        entry["purpose"] = report["purpose"]
        entry["selected_state"] = report.get("selected_state")
        # WHICH producer stamped it. The audit entry has to say this because
        # `state` no longer identifies the shape on its own: a feedback round
        # arrives here as `partial` (it is not a gate result), while a
        # `run_tests` report whose pytest leg failed arrives as `failed`.
        # Carried only when the report names one, so an older report cannot
        # acquire a producer it never had.
        producer = provisional_producer(report)
        if producer:
            entry[PROVISIONAL_PRODUCER] = producer
    if "repo_gate" in report or "gate_coverage" in report:
        entry["gate_coverage"] = report.get("gate_coverage")
        coverage_error = publication_scope_refusal(entry["gate_coverage"])
        if coverage_error:
            entry["gate_coverage_error"] = coverage_error
            if state == "passed":
                entry.update(state="coverage_incomplete", passed=False,
                             skipped_because="coverage_incomplete", summary=coverage_error)
                return entry
    if state == "unreadable":
        raw_passed = report.get("passed", report.get("all_passed", False))
        entry.update(state="unreadable", passed=False,
                     skipped_because="invalid_passed_type",
                     summary=(f"{step_id}/{filename} has non-boolean pass status "
                              f"{raw_passed!r}"))
        return entry
    passed = state == "passed"
    because = report.get("skipped_because")
    if state == "skipped" and not because:
        status = str(report.get("status", "")).strip().lower()
        unrun = (report.get("unrun") is True or report.get("ran") is False
                 or report.get("executed") is False or status in _UNRUN_STATUSES)
        because = "gate_unrun" if unrun else "gate_skipped"
    entry.update(passed=passed, skipped_because=because,
                 state=state,
                 gate_summary=str(report.get("summary", ""))[:400])
    return entry
