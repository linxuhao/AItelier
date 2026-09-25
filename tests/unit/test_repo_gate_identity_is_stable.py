"""A failure keeps its identity from one run of the gate to the next.

The known-red baseline can only follow a failure whose id is the same string
on every run. On the r2 candidate the id was a hash of the finding's TEXT, and
the game gate writes the value the run observed into that text
(`... -> actual false; observed 6`): one assertion got 4 ids over 4 real runs,
and the same assertion failing at two frames collapsed into one id
(review gnr2, 2026-09-25: 19 findings -> 14 ids).

The findings below are written the way the game gate writes them
(`tools/godot_gate.py:verify_playtest` / `verify_repeatability`), next to the
stage report they were written from.
"""
from __future__ import annotations

import json
from pathlib import Path

from aitelier.tools.run_tests import impl as rt

REPO = Path("/work/repo")


def _assert_line(scenario, row):
    """One failing-assertion finding, in the game gate's own format."""
    return "  %s / %s: %s -> actual %s%s%s" % (
        scenario, row.get("name"), row.get("expr"),
        json.dumps(row.get("actual"), ensure_ascii=False),
        ("; observed %s" % json.dumps(row["observed"], ensure_ascii=False))
        if "observed" in row else "",
        (" [%s]" % row["error"]) if row.get("error") else "")


def _row(name, expr, frame, observed, passed=False):
    return {"actual": passed, "error": "", "expr": expr, "frame": frame,
            "name": name, "node": name.split(".")[0], "observed": observed,
            "passed": passed}


def _playtest(observed_a, observed_b, *, summary="Playtest HARD-failed: 2 red"):
    rows = [
        _row("CultivationScreen.phase", 'phase == "CARD_PICK"', 830, observed_a),
        _row("CultivationScreen.month", "month == 3", 900, 1, passed=True),
        _row("CultivationScreen.phase", 'phase == "CARD_PICK"', 1210, observed_b),
        _row("MotionProbe.failures", "failures == 0", 40, observed_a),
    ]
    scenarios = [{"name": "event_travel", "ran": True, "passed": False,
                  "asserts": rows[:3]},
                 {"name": "body_motion_probe", "ran": True, "passed": False,
                  "asserts": rows[3:]}]
    # Top-level member order as the sidecar writes it: captures, then
    # behavior, then summary.
    report = {"passed": False, "captures": ["x" * 64],
              "behavior": {"all_passed": False, "scenarios": scenarios},
              "summary": summary}
    same_error = "errors: " + json.dumps({"kind": "push_error", "msg": "node not found"})
    findings = ["hard gate failed: %s" % summary, same_error, same_error,
                "scenario 'body_motion_probe' reports passed=False"]
    findings += [_assert_line("body_motion_probe", r) for r in rows[3:]]
    findings += [_assert_line("event_travel", r) for r in rows[:3] if not r["passed"]]
    left = _row("Player.grid_pos", "changed", 12, observed_a)
    right = dict(left, observed=observed_b)
    findings.append("repeatability some_scenario: %s != %s" % (
        json.dumps(left, ensure_ascii=False), json.dumps(right, ensure_ascii=False)))
    return report, findings


def _ticket(root, report, findings, *, raw_report=None):
    ticket = root / "ticket"
    gate = ticket / "wuxia-godot-gate-x"
    gate.mkdir(parents=True)
    (gate / "manifest.json").write_text(json.dumps({"repo": str(REPO), "stages": {
        "compile": {"report": "compile.json"},
        "playtest": {"report": "playtest.json"}}}))
    (gate / "compile.json").write_text(json.dumps({"passed": True}))
    (gate / "playtest.json").write_text(
        raw_report if raw_report is not None
        else json.dumps(report, ensure_ascii=False, indent=2))
    (gate / "playtest-findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2))
    return ticket


def _ids(root, report, findings, **kw):
    cases, error = rt._report_dir_failure_cases(
        _ticket(root, report, findings, **kw), REPO,
        {"name": "wuxia-godot-gate-x", "is_dir": True})
    assert error is None, error
    assert len(cases) == len(findings)
    return [c["case_id"] for c in cases]


def test_the_same_failure_twice_has_the_same_identities(tmp_path):
    first = _ids(tmp_path / "1", *_playtest("PLAN_RUN", "PLAN_RUN"))
    second = _ids(tmp_path / "2", *_playtest("PLAN_RUN", "PLAN_RUN"))
    assert first == second


def test_different_observed_values_keep_the_same_identities(tmp_path):
    first = _ids(tmp_path / "1", *_playtest(6, 7, summary="Playtest HARD-failed: 2 red"))
    second = _ids(tmp_path / "2", *_playtest(53, "MENU", summary="Playtest HARD-failed: 5 red"))
    assert first == second
    report, findings = _playtest(6, 7)
    report2, findings2 = _playtest(53, "MENU")
    assert findings != findings2


def test_an_assertion_is_named_by_scenario_name_and_frame(tmp_path):
    ids = _ids(tmp_path, *_playtest("PLAN_RUN", "MENU"))
    assert "playtest/event_travel/CultivationScreen.phase@830" in ids
    assert "playtest/event_travel/CultivationScreen.phase@1210" in ids
    assert "playtest/body_motion_probe/MotionProbe.failures@40" in ids
    assert "playtest/repeatability/some_scenario/Player.grid_pos@12" in ids
    assert len(set(ids)) == len(ids)


def test_findings_with_identical_text_are_both_kept(tmp_path):
    ids = _ids(tmp_path, *_playtest(1, 2))
    errors = [i for i in ids if i.endswith("~2")]
    assert len(errors) == 1
    assert errors[0][:-2] in ids


def test_a_report_too_large_to_parse_whole_is_read_from_its_tail(
        tmp_path, monkeypatch):
    """The sidecar's report carries its captures BEFORE `behavior` (442 MB on
    a real run). Above the whole-parse bound only the members from the last
    top-level `"behavior":` on are parsed; the ids are the same."""
    report, findings = _playtest("PLAN_RUN", "MENU")
    whole = _ids(tmp_path / "whole", report, findings)
    monkeypatch.setattr(rt, "STAGE_REPORT_FULL_PARSE_MAX", 256)
    report["captures"] = ["y" * 4096, {"behavior": {"scenarios": []}}]
    assert len(json.dumps(report)) > 256
    assert _ids(tmp_path / "tail", report, findings) == whole


def test_a_behavior_key_nested_after_the_real_one_is_not_read(
        tmp_path, monkeypatch):
    """The LAST `"behavior":` in the file is the one the tail read starts
    from; when it sits inside a nested object, the suffix does not parse, and
    no frame is taken from the nested rows."""
    report, findings = _playtest("PLAN_RUN", "MENU")
    decoy = {"behavior": {"scenarios": [{"name": "event_travel", "asserts": [
        _row("CultivationScreen.phase", 'phase == "CARD_PICK"', 7, "X")]}]}}
    text = json.dumps(dict(report, trailer=decoy), indent=2)
    monkeypatch.setattr(rt, "STAGE_REPORT_FULL_PARSE_MAX", 256)
    ids = _ids(tmp_path, report, findings, raw_report=text)
    assert not any(i.endswith("@7") for i in ids), ids


def test_two_real_gate_runs_whose_summary_differs_get_the_same_ids(
        tmp_path, monkeypatch):
    """Two runs of the fixture gate against the real harness: its findings
    carry the report's summary, and the summary differs between the runs."""
    from tests.gate_fixture import HarnessRig, red_report, write_gate
    ids = []
    for n, summary in enumerate(("0 passed, 2 failed", "0 passed, 2 failed (run 2)")):
        rig = HarnessRig(tmp_path / f"harness-{n}", monkeypatch)
        rig.script_report = dict(red_report(), summary=summary)
        monkeypatch.setenv("GODOT_BUILDER_URL", rig.base)
        try:
            repo = tmp_path / f"repo-{n}"
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
            write_gate(repo)
            rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / f"out-{n}"))
        finally:
            rig.close()
        report = json.loads((tmp_path / f"out-{n}" / "test_report.json").read_text())
        ids.append([c["case_id"] for c in report["repo_gate"]["failure_cases"]])
    assert ids[0] == ids[1] and len(ids[0]) == 3
