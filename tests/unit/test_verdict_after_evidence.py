"""The final verdict is downstream of fresh evidence from this verification cycle."""
import json
from pathlib import Path

from unittest.mock import ANY
import yaml

from aitelier.gate_evidence import (
    audit_evidence,
    stamp_report,
)
from aitelier.tools.verify_evidence.impl import verify_evidence

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGS = _ROOT / "configs"
RUN = "run-current"


def _composed():
    from skillflow.compose import compose_graph
    return compose_graph(
        yaml.safe_load((_CONFIGS / "dpe_default.yaml").read_text()),
        [yaml.safe_load((_CONFIGS / "addons" / "game_harness.yaml").read_text())],
    )


def _write(root, step, filename, *, cycle, run_id=RUN, passed=True, **extra):
    d = root / step
    d.mkdir(parents=True, exist_ok=True)
    body = {"passed": passed, "run_id": run_id,
            "evidence_cycle_id": cycle, "summary": step}
    body.update(extra)
    (d / filename).write_text(json.dumps(body), encoding="utf-8")


def test_start_stamp_creates_a_new_cycle_each_time(tmp_path):
    one = stamp_report({}, run_id=RUN, out_dir=str(tmp_path / "5_test"),
                       start_cycle=True)
    two = stamp_report({}, run_id=RUN, out_dir=str(tmp_path / "5_test"),
                       start_cycle=True)
    assert one["run_id"] == RUN
    assert one["evidence_cycle_id"]
    assert one["evidence_cycle_id"] != two["evidence_cycle_id"]


def test_negative_control_same_run_previous_cycle_is_stale(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new")
    _write(tmp_path, "5_compile", "compile_report.json", cycle="cycle-old")
    verdict = audit_evidence(
        tmp_path, RUN,
        [["5_test", "test_report.json"],
         ["5_compile", "compile_report.json"]],
    )
    assert verdict["passed"] is False
    assert verdict["state"] == "stale"
    assert verdict["stale_reports"][0]["evidence_cycle_id"] == "cycle-old"


def test_missing_gate_is_explicit_non_passing_evidence(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new")
    verdict = audit_evidence(
        tmp_path, RUN,
        [["5_test", "test_report.json"],
         ["5_compile", "compile_report.json"]],
    )
    assert verdict["passed"] is False
    assert verdict["state"] == "missing"
    assert verdict["missing_reports"] == [{
        "step": "5_compile", "file": "compile_report.json",
        "state": "missing", "passed": False,
        "skipped_because": "report_missing",
        "summary": ANY,
    }]


def test_current_cycle_skip_is_explicit_and_non_passing(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new")
    _write(tmp_path, "5_compile", "compile_report.json", cycle="cycle-new",
           gate_skipped=True, skipped_because="upstream_failed")
    verdict = audit_evidence(
        tmp_path, RUN,
        [["5_test", "test_report.json"],
         ["5_compile", "compile_report.json"]],
    )
    assert verdict["passed"] is False
    assert verdict["state"] == "skipped"
    assert verdict["skipped_gates"][0]["skipped_because"] == "upstream_failed"



def test_plain_skipped_report_is_non_passing(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new",
           skipped=True)
    verdict = audit_evidence(
        tmp_path, RUN, [["5_test", "test_report.json"]],
    )
    assert verdict["passed"] is False
    assert verdict["state"] == "skipped"


def test_not_applicable_skip_is_still_non_passing(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new",
           gate_skipped=True, skipped_because="not_applicable")
    verdict = audit_evidence(tmp_path, RUN, [["5_test", "test_report.json"]])
    assert verdict["passed"] is False
    assert verdict["state"] == "skipped"


def test_previous_run_report_is_stale(tmp_path):
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new",
           run_id="run-previous")
    verdict = audit_evidence(
        tmp_path, RUN, [["5_test", "test_report.json"]],
    )
    assert verdict["passed"] is False
    assert verdict["state"] == "stale"

def test_fresh_complete_cycle_passes(tmp_path):
    for step, filename in (
        ("5_test", "test_report.json"),
        ("5_compile", "compile_report.json"),
        ("5_compile", "playtest_report.json"),
        ("5_vision", "vision_report.json"),
    ):
        _write(tmp_path, step, filename, cycle="cycle-new")
    verdict = audit_evidence(
        tmp_path, RUN,
        [["5_test", "test_report.json"],
         ["5_compile", "compile_report.json"],
         ["5_compile", "playtest_report.json"],
         ["5_vision", "vision_report.json"]],
    )
    assert verdict["passed"] is True
    assert verdict["state"] == "fresh"


def test_verify_evidence_writes_current_cycle_report(tmp_path):
    step = tmp_path / "5_evidence"
    step.mkdir()
    _write(tmp_path, "5_test", "test_report.json", cycle="cycle-new")
    result = verify_evidence(
        out_dir=str(step), run_id=RUN,
        gates=json.dumps([["5_test", "test_report.json"]]),
    )
    assert result["written"] == "evidence_report.json"
    report = json.loads((step / "evidence_report.json").read_text())
    assert report["passed"] is True
    assert report["evidence_cycle_id"] == "cycle-new"


def test_composed_graph_has_no_verdict_before_evidence_or_verdict_cycle():
    graph = _composed()
    steps = {s["id"]: s for s in graph["steps"]}
    assert [t["to"] for t in steps["task_loop"]["transitions"]] == ["t_plan", "5_test"]
    assert [t["to"] for t in steps["5_evidence"]["transitions"]] == ["5_design"]
    assert [t["to"] for t in steps["5_game_evidence"]["transitions"]] == ["5"]
    assert [t["to"] for t in steps["5"]["transitions"]] == ["5_knowledge"]
    assert all(t.get("to") != "5_test" for t in steps["5"]["transitions"])

    incoming = [s["id"] for s in graph["steps"]
                if any(t.get("to") == "5" for t in s.get("transitions", []))]
    assert incoming == ["5_game_evidence"]


def test_game_verifier_declares_every_report_it_judges():
    graph = _composed()
    verifier = next(s for s in graph["steps"] if s["id"] == "5")
    pairs = {(c.get("source", c).get("step"), c.get("source", c).get("output"))
             for c in verifier["context"]}
    assert {
        ("5_test", "test_report.json"),
        ("5_compile", "compile_report.json"),
        ("5_compile", "playtest_report.json"),
        ("5_vision", "vision_report.json"),
        ("5_final_test", "test_report.json"),
        ("5_game_evidence", "evidence_report.json"),
        ("5_evidence", "evidence_report.json"),
    } <= pairs


def test_game_reviewer_receives_both_current_cycle_audits():
    graph = _composed()
    reviewer = next(s for s in graph["steps"] if s["id"] == "5_review")
    pairs = {(c.get("source", c).get("step"), c.get("source", c).get("output"))
             for c in reviewer["context"]}
    assert ("5_evidence", "evidence_report.json") in pairs
    assert ("5_game_evidence", "evidence_report.json") in pairs
