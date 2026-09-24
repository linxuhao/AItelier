"""A play-test scenario has one reading: what the harness runs is what the
author wrote and what a static rule reads.

The three shapes below come from the p48 r10 review
(~/.AItelier/director/reports/p48-r10-review-20260924/review.md, section 4).
Each YAML block is copied from the on-disk edit that review applied to the game
repo's playtest/plan_route_event_reroll_budget.yaml (wuxia master 73f01262):

  BY1   logs/03_ondisk_BY1-cand_edit.log   geometry asserts moved under `reviewer_notes:`
  BY2b  logs/03_ondisk_BY2b-cand_edit.log  an `at: 195` block written before `at: 185`
  BY3   logs/03_ondisk_BY3-cand_edit.log   a click on `X/PlanEditKind1 +0,20`

All three ran green on the harness before this change. BY1 and BY2b are refused
at parse time by the real `_playtest_spec` / `_normalize_timeline`; BY3 is
refused by the engine-side probe, whose report this file checks is lifted into
the response's `spec_errors` (the engine run itself is in the delivery note).
"""

import importlib.util
from pathlib import Path

import yaml

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness_one_reading", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)


def _probe_recorder(monkeypatch, probe=None):
    """Stand in for the engine and record every scenario it was asked to run."""
    ran = []

    def fake(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
             timing=None, render=True):
        ran.append(scene)
        report = dict(probe or {"frames": frames, "nodes": {},
                                "asserts": [{"name": "a", "passed": True}]})
        if timing is not None:
            timing["game_usec"] = int(report.get("frames", 0) * 1_000_000 / 60)
        return report, [], False

    monkeypatch.setattr(gh, "_run_probe", fake)
    return ran


# The f185 block and the f200 aim as they stand in the game repo, then BY1's
# edit: the five geometry lines leave the timeline and reappear, byte for byte,
# under a top-level `reviewer_notes:` key.
_BY1 = """\
name: plan_route_event_reroll_budget
timeline:
- at: 185
  actions: []
  assert:
    CultivationScreen.phase: phase == "PLAN_EDIT"
- at: 200
  actions: []
  clicks:
  - PlanEditKind1 +0,90
reviewer_notes:
- at: 1
  assert:
    PlanEditKind1.get_popup().visible: get_popup().visible == true
    PlanEditKind1.get_popup().item_count: get_popup().item_count == 4
    PlanEditKind1.get_popup().get_item_text(3): get_popup().get_item_text(3) == "游历"
    PlanEditKind1.get_popup().position.y: get_popup().position.y == int(get_global_rect().end.y)
    PlanEditKind1.aim_90_hits_item_3: int((get_global_rect().get_center().y + 90.0 - get_popup().position.y) / (get_popup().size.y / get_popup().item_count)) == 3
"""

# BY2b: the review's `at: 195` block written ABOVE the real f185 entry.
_BY2B = """\
- at: 170
  actions: []
  clicks:
  - PlanEditKind1
- at: 195
  assert:
    CultivationScreen.phase: phase == "PLAN_EDIT"
- at: 185
  actions: []
  assert:
    CultivationScreen.phase: phase == "PLAN_EDIT"
    PlanEditKind1.get_popup().visible: get_popup().visible == true
    PlanEditKind1.get_popup().item_count: get_popup().item_count == 4
    PlanEditKind1.get_popup().get_item_text(3): get_popup().get_item_text(3) == "游历"
    PlanEditKind1.get_popup().position.y: get_popup().position.y == int(get_global_rect().end.y)
    PlanEditKind1.aim_90_hits_item_3: int((get_global_rect().get_center().y + 90.0 - get_popup().position.y) / (get_popup().size.y / get_popup().item_count)) == 3
- at: 200
  actions: []
  clicks:
  - PlanEditKind1 +0,90
"""


# ── an unknown key at any level is an error ───────────────────────────────

def test_by1_reviewer_notes_in_a_scenario_is_refused_and_the_scenario_does_not_run(
        monkeypatch, tmp_path):
    ran = _probe_recorder(monkeypatch)
    spec = {"scenarios": [yaml.safe_load(_BY1)]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert ran == [], "a refused scenario must not reach the engine"
    assert r["passed"] is False
    errs = [e for e in r["spec_errors"] if "unknown key" in e]
    assert len(errs) == 1, r["spec_errors"]
    msg = errs[0]
    assert "'plan_route_event_reroll_budget'" in msg          # scenario name
    assert "reviewer_notes" in msg                              # key name
    assert "allowed: " + ", ".join(sorted(gh._SCENARIO_KEYS)) in msg  # allowed set
    assert r["summary"].startswith("Playtest HARD-failed: 1 spec violation")
    row = r["behavior"]["scenarios"][0]
    assert row["name"] == "plan_route_event_reroll_budget"
    assert row["ran"] is False and row["passed"] is False and row["asserts"] == []


def test_an_unknown_top_level_spec_key_is_refused_and_nothing_runs(monkeypatch, tmp_path):
    ran = _probe_recorder(monkeypatch)
    by1 = yaml.safe_load(_BY1)
    notes = by1.pop("reviewer_notes")
    spec = {"scene": "res://main.tscn", "reviewer_notes": notes, "scenarios": [by1]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert ran == []
    assert r["passed"] is False
    assert len(r["spec_errors"]) == 1, r["spec_errors"]
    msg = r["spec_errors"][0]
    assert "top-level key(s) reviewer_notes" in msg
    assert "allowed: " + ", ".join(sorted(gh._SPEC_KEYS)) in msg
    assert [s["ran"] for s in r["behavior"]["scenarios"]] == [False]


def test_every_key_the_game_contract_uses_is_allowed_and_runs(monkeypatch, tmp_path):
    """The shared header (_common.yaml: scene/actions/surface) and the
    scenario keys the corpus carries with a reader behind them."""
    ran = _probe_recorder(monkeypatch)
    spec = {"scene": "res://main.tscn", "frames": 120, "actions": ["ui_accept"],
            "surface": {"HUD": ["visible"]},
            "scenarios": [{"name": "s", "scene": "res://x.tscn", "repeatability": True,
                           "timeline": [{"at": 3, "assert": {"HUD.visible": True}}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["spec_errors"] == []
    assert ran == ["res://x.tscn"]
    assert r["passed"] is True


def test_the_allowed_sets_are_exactly_these():
    assert gh._SPEC_KEYS == {"scene", "frames", "scenarios", "actions", "surface"}
    assert gh._SCENARIO_KEYS == {"name", "timeline", "scene", "repeatability"}


# ── the timeline runs in the order it is written ─────────────────────────

def test_by2b_a_frame_written_after_a_later_frame_is_refused():
    out, errors = gh._normalize_timeline(yaml.safe_load(_BY2B))
    assert len(errors) == 1, errors
    msg = errors[0]
    # the two entries, by index and by frame
    assert "timeline entry 2 (at: 185)" in msg
    assert "entry 1 (at: 195)" in msg


def test_by2b_through_playtest_spec_is_a_spec_error(monkeypatch, tmp_path):
    _probe_recorder(monkeypatch)
    spec = {"scenarios": [{"name": "plan_route_event_reroll_budget",
                           "timeline": yaml.safe_load(_BY2B)}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False
    assert any("'plan_route_event_reroll_budget'" in e and "entry 2 (at: 185)" in e
               and "entry 1 (at: 195)" in e for e in r["spec_errors"]), r["spec_errors"]


def test_the_same_frame_written_twice_is_legal():
    out, errors = gh._normalize_timeline([
        {"at": 5, "actions": ["ui_accept"]},
        {"at": 5, "assert": {"HUD.visible": True}},
        {"at": 9, "clicks": ["A"]},
        {"at": 9, "clicks": ["B"]},
    ])
    assert errors == []
    assert [e["at"] for e in out] == [5, 5, 9, 9]


def test_a_drop_below_an_earlier_maximum_names_the_maximum():
    out, errors = gh._normalize_timeline([
        {"at": 10, "actions": ["a"]}, {"at": 30, "actions": ["b"]},
        {"at": 30, "actions": ["c"]}, {"at": 20, "actions": ["d"]}])
    assert len(errors) == 1, errors
    assert "timeline entry 3 (at: 20)" in errors[0] and "entry 1 (at: 30)" in errors[0]


# ── a path that does not resolve never falls back to a leaf ───────────────

def test_a_path_the_probe_refused_comes_back_as_a_spec_error(monkeypatch, tmp_path):
    """BY3's click as the probe now reports it. The refusal travels in the
    probe's own `spec_errors` and is carried into the response's `spec_errors`
    with the scenario name: a spec error, not a runtime error."""
    refused = ("frame 201: aim target X/PlanEditKind1 is a path and does not "
               "resolve under the current scene (spec: X/PlanEditKind1 +0,20)")
    _probe_recorder(monkeypatch, {"frames": 231, "nodes": {}, "spec_errors": [refused],
                                  "asserts": [{"name": "a", "passed": True}]})
    spec = {"scenarios": [{"name": "plan_route_event_reroll_budget", "timeline": [
        {"at": 201, "actions": [], "clicks": ["X/PlanEditKind1 +0,20"]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False
    assert r["errors"] == []
    assert r["spec_errors"] == ["scenario 'plan_route_event_reroll_budget': " + refused]
    assert r["summary"].startswith("Playtest HARD-failed: 1 spec violation")


def _probe_func(name: str) -> str:
    src = _HARNESS.read_text(encoding="utf-8")
    start = src.index("func %s(" % name)
    ends = [i for i in (src.find("\nfunc ", start + 1), src.find("\n## ", start + 1))
            if i >= 0]
    return src[start:min(ends)]


def test_the_probe_resolves_a_path_by_path_only():
    body = _probe_func("_resolve")
    path_branch = body[body.index('if "/" in name:'):body.rindex("return get_tree().get_root().find_child(name")]
    assert "find_child" not in path_branch
    assert "scene.get_node_or_null(NodePath(name))" in path_branch
    # a bare name is still searched by name anywhere in the tree
    assert body.rstrip().endswith("return get_tree().get_root().find_child(name, true, false)")


def test_the_probe_reports_an_unresolved_path_as_a_spec_error():
    assert "_refuse_path(" in _probe_func("_point_of")
    assert "_refuse_path(" in _probe_func("_eval_assert")
    assert '"spec_errors": _spec_errors' in _probe_func("_finish")
