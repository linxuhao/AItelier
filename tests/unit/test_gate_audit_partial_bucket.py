# tests/unit/test_gate_audit_partial_bucket.py
# The audit is the second place a partial gate must not read as a pass, and it
# is the place a reviewer's mutant survived 116 tests: `_audit_one` could be
# changed to treat `partial` as passed, and to file it under `failed_gates`
# instead of `incomplete_coverage_gates`, without reddening anything.
#
# These two probes pin the two facts `configs/addons/game_harness.yaml` relies
# on when it routes a partial report to `5_release_wait`: the audit fails the
# run, and it says which kind of failure it is.
import json
from pathlib import Path

from aitelier.gate_evidence import CYCLE_MANIFEST, audit_evidence, report_state


def _graph(tmp_path, report):
    """A graph dir with a current cycle manifest and one stamped report."""
    run_id, cycle = "run-partial", "cycle-7"
    graph = tmp_path / "graph"
    (graph / "5_final_test").mkdir(parents=True)
    (graph / CYCLE_MANIFEST).write_text(json.dumps({
        "version": 1, "run_id": run_id, "evidence_cycle_id": cycle,
        "evidence_generated_at": "2026-11-06T00:00:00Z",
    }))
    report = dict(report, run_id=run_id, evidence_cycle_id=cycle)
    (graph / "5_final_test" / "test_report.json").write_text(json.dumps(report))
    return graph, run_id


PARTIAL = {
    "passed": True,
    "summary": ("8 of 9 node project(s) measured; 1 not measured: "
                "packages/p7: not visited: more than 8 node projects"),
    "failures": [],
    "node_unmeasured": [{"dir": "packages/p7", "kind": "capped",
                         "reason": "not visited: more than 8 node projects"}],
}

GREEN = {"passed": True, "summary": "9 of 9 node project(s) measured",
         "failures": []}


def test_a_partial_report_fails_the_audit_and_is_named_as_incomplete(tmp_path):
    """`partial` is not `passed` here, and the failure keeps its own bucket."""
    assert report_state(PARTIAL) == "partial"
    graph, run_id = _graph(tmp_path, PARTIAL)

    verdict = audit_evidence(graph, run_id, [["5_final_test", "test_report.json"]])

    assert verdict["passed"] is False, verdict
    # The bucket's aggregate name is `coverage_incomplete` for a node-gate
    # absence as well as for a publication-scope refusal. That conflates two
    # different questions, and it is tolerable only because nothing routes on
    # it: `release_disposition` reads `report_state` directly, and both states
    # park. Pinned here so the conflation is visible rather than discovered.
    assert verdict["state"] == "coverage_incomplete", verdict["state"]
    entry = verdict["incomplete_coverage_gates"][0]
    assert entry["state"] == "partial"
    assert entry["passed"] is False
    # Not filed as a product failure: an absence must not charge the code.
    assert verdict["failed_gates"] == [], verdict["failed_gates"]


def test_a_fully_measured_green_report_still_audits_as_passed(tmp_path):
    """The other pole: the same probe, one report away."""
    graph, run_id = _graph(tmp_path, GREEN)

    verdict = audit_evidence(graph, run_id, [["5_final_test", "test_report.json"]])

    assert verdict["passed"] is True, verdict
    assert verdict["state"] == "fresh", verdict["state"]
    assert verdict["incomplete_coverage_gates"] == []
