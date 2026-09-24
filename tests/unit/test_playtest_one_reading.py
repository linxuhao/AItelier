"""A play-test scenario has one reading: what the harness runs is what the
author wrote and what a static rule reads.

The three shapes below come from the p48 r10 review
(~/.AItelier/director/reports/p48-r10-review-20260924/review.md, section 4).
Each YAML block is copied from the on-disk edit that review applied to the game
repo's playtest/plan_route_event_reroll_budget.yaml (wuxia master 73f01262):

  BY1   logs/03_ondisk_BY1-cand_edit.log   geometry asserts moved under `reviewer_notes:`
  BY2b  logs/03_ondisk_BY2b-cand_edit.log  an `at: 195` block written before `at: 185`
  BY3   logs/03_ondisk_BY3-cand_edit.log   a click on `X/PlanEditKind1 +0,20`
  Y4b   logs/03_ondisk_Y4b-cand_edit.log   the f200 click moved to `at: 185.5`
           (playtest/event_travel_plan_effects.yaml)

All three ran green on the harness before this change. BY1 and BY2b are refused
at parse time by the real `_playtest_spec` / `_normalize_timeline`; BY3 is
refused by the engine-side probe, whose report this file checks is lifted into
the response's `spec_errors` (the engine run itself is in the delivery note).

The second half takes every shape from the r1 review of this node
(~/.AItelier/director/reports/onereading-r1-review-20260924/review.md, section
"Two-reading attempts"), copied from the script that produced each row:

  K10-K14, K17, K18, A10, A12-A15   R/battery.py        (in-process shapes)
  L1-L3, monolith L1/L2             R/dispatch.py       (spec dispatch, reader)
  E08/E08b, E10, E12                R/engine/drive.py   (engine battery)
  G1, G2                            R/mutate.py         (probe mutants)

R = ~/.AItelier/director/reports/onereading-r1-review-20260924.
"""

import importlib.util
import textwrap
from pathlib import Path

import pytest
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
    assert gh._SCENARIO_KEYS == {"name", "timeline", "scene", "repeatability",
                                 "description"}


# ── `description` is allowed only as a plain string ──────────────────────

def test_a_plain_string_description_is_allowed_and_runs(monkeypatch, tmp_path):
    ran = _probe_recorder(monkeypatch)
    spec = {"scenarios": [{"name": "s", "description": "RENDER REQUIRED. Real keyboard menu.",
                           "timeline": [{"at": 3, "assert": {"HUD.visible": True}}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["spec_errors"] == []
    assert ran == [""]
    assert r["passed"] is True


_BY1_NOTES = yaml.safe_load(_BY1)["reviewer_notes"]


@pytest.mark.parametrize("value", [
    _BY1_NOTES,                    # a list: BY1's notes block moved under `description:`
    _BY1_NOTES[0],                 # a mapping: one BY1 assert block
    3, 185.5, True, None,
], ids=["list", "mapping", "int", "float", "bool", "null"])
def test_a_description_that_is_not_a_string_is_refused(monkeypatch, tmp_path, value):
    ran = _probe_recorder(monkeypatch)
    by1 = yaml.safe_load(_BY1)
    by1.pop("reviewer_notes")
    by1["description"] = value
    r = gh._playtest_spec(tmp_path / "proj", {"scenarios": [by1]}, 300, 120)
    assert ran == [], "a refused scenario must not reach the engine"
    assert r["passed"] is False
    assert len(r["spec_errors"]) == 1, r["spec_errors"]
    msg = r["spec_errors"][0]
    assert "'plan_route_event_reroll_budget'" in msg
    assert "key description of type %s" % type(value).__name__ in msg
    assert [s["ran"] for s in r["behavior"]["scenarios"]] == [False]


# ── a frame is a whole number ─────────────────────────────────────────────

# Y4b: the real f185 block of event_travel_plan_effects.yaml, then the review's
# edit moving the f200 aim to `at: 185.5`, which the harness ran at frame 185.
_Y4B = """\
- at: 185
  actions: []
  assert:
    CultivationScreen.phase: phase == "PLAN_EDIT"
    PlanEditKind1.get_popup().visible: get_popup().visible == true
    PlanEditKind1.get_popup().item_count: get_popup().item_count == 4
    PlanEditKind1.get_popup().get_item_text(3): get_popup().get_item_text(3) == "游历"
    PlanEditKind1.get_popup().position.y: get_popup().position.y == int(get_global_rect().end.y)
    PlanEditKind1.aim_90_hits_item_3: int((get_global_rect().get_center().y + 90.0 - get_popup().position.y) / (get_popup().size.y / get_popup().item_count)) == 3
- at: 185.5
  actions: []
  clicks:
  - PlanEditKind1 +0,90
"""


def test_y4b_a_fractional_frame_is_refused():
    out, errors = gh._normalize_timeline(yaml.safe_load(_Y4B))
    assert len(errors) == 1, errors
    assert "timeline entry 1 has a non-integer `at`: 185.5" in errors[0]
    assert not any("click" in e for e in out), "the refused click must not be scheduled"


def test_y4b_through_playtest_spec_is_a_spec_error(monkeypatch, tmp_path):
    _probe_recorder(monkeypatch)
    spec = {"scenarios": [{"name": "event_travel_plan_effects",
                           "timeline": yaml.safe_load(_Y4B)}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False
    assert any("'event_travel_plan_effects'" in e and "entry 1" in e and "185.5" in e
               for e in r["spec_errors"]), r["spec_errors"]


def test_whole_frames_still_run_as_written():
    out, errors = gh._normalize_timeline([
        {"at": 185, "assert": {"HUD.visible": True}},
        {"at": 200.0, "clicks": ["PlanEditKind1 +0,90"]}])
    assert errors == []
    assert [(e["at"], type(e["at"])) for e in out] == [(185, int), (200, int)]


@pytest.mark.parametrize("value", [True, "185", "3..15"])
def test_a_bool_or_string_frame_is_still_refused(value):
    out, errors = gh._normalize_timeline([{"at": value, "clicks": ["A"]}])
    assert out == []
    assert len(errors) == 1 and "non-numeric `at`" in errors[0]


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
               "resolve in the scene tree (spec: X/PlanEditKind1 +0,20)")
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


# ── the probe source: what a mutant has to change ────────────────────────
# The probe is GDScript and runs only inside Godot, which this suite does not
# have. These tests therefore read the probe source. A leaf fallback needs a
# by-name tree lookup, so the first test lists every lookup call in the probe:
# the review's G1 adds `find_child(name.get_file(), ...)` in a helper and is a
# new entry in that list. G2 edits the one line that decides what is a path.

_LOOKUP = ("find_child(", "find_children(", "get_node_or_null(", "get_node(",
           "has_node(", "get_children(", "get_child(")


def _probe_lookups():
    import re
    out, func = [], None
    for line in gh._PROBE_GD.splitlines():
        m = re.match(r"func (\w+)\(", line)
        if m:
            func = m.group(1)
        code = line.strip()
        if code.startswith("#"):
            continue
        if any(re.search(r"\b" + re.escape(k), code) for k in _LOOKUP):
            out.append((func, code))
    return out


def test_every_tree_lookup_in_the_probe_is_one_of_these():
    assert _probe_lookups() == [
        ("_resolve", "return get_node_or_null(NodePath(name))"),
        ("_resolve", "return scene.get_node_or_null(NodePath(name))"),
        ("_resolve", "return get_tree().get_root().find_child(name, true, false)"),
        ("_walk", "for c in node.get_children():"),
    ]


def test_the_path_branch_returns_the_path_lookup_and_nothing_else():
    body = _probe_func("_resolve")
    branch = body[body.index('if "/" in name:'):body.rindex("return get_tree()")]
    code = [ln.strip() for ln in branch.splitlines()[1:] if ln.strip()]
    assert code == ["if scene == null:", "return null",
                    "return scene.get_node_or_null(NodePath(name))"]


def test_any_name_with_a_slash_is_a_path_the_probe_refuses():
    """E08 / E08b: an absolute `/root/X/...` that does not resolve is refused
    like a relative one. G2 (`return false`) changes this line."""
    body = _probe_func("_is_path")
    code = [ln.strip() for ln in body.splitlines()[1:] if ln.strip()]
    assert code == ['return "/" in name']
    assert "_is_path(node_name)" in _probe_func("_point_of")
    assert "_is_path(node_name)" in _probe_func("_eval_assert")


# ── shapes from the r1 review (R/battery.py, verbatim) ───────────────────

from aitelier.strict_yaml import load_yaml_strict  # noqa: E402


def Y(text):
    return load_yaml_strict(textwrap.dedent(text), source="battery")


GEOM = """\
    PlanEditKind1.get_popup().visible: get_popup().visible == true
    PlanEditKind1.aim_90_hits_item_3: int((get_global_rect().get_center().y + 90.0 - get_popup().position.y) / (get_popup().size.y / get_popup().item_count)) == 3
"""


def sc_yaml(extra_top="", timeline=None):
    tl = timeline or """\
- at: 185
  assert:
    CultivationScreen.phase: phase == "PLAN_EDIT"
- at: 200
  clicks:
  - PlanEditKind1 +0,90
"""
    return "name: s\n" + extra_top + "timeline:\n" + tl


def spec1(sc, **top):
    d = {"scene": "res://main.tscn"}
    d.update(top)
    d["scenarios"] = [sc]
    return d


def TL(s):
    return spec1(Y(sc_yaml(timeline=s)))


def _refused(monkeypatch, tmp_path, spec, *needles):
    """Run the real _playtest_spec; the shape must come back as a spec error
    carrying every needle, fail the gate, and (for a key or type refusal)
    never reach the engine."""
    ran = _probe_recorder(monkeypatch)
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    hits = [e for e in r["spec_errors"] if all(n in e for n in needles)]
    assert hits, r["spec_errors"]
    assert r["passed"] is False
    assert r["errors"] == []
    return r, ran


# (c) each key has one type: an assert block parked under it is refused

def test_k10_repeatability_as_a_mapping_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path,
                      spec1(Y(sc_yaml("repeatability:\n  at: 1\n  assert:\n" + GEOM))),
                      "scenario 's'", "key repeatability of type dict", "true or false")
    assert ran == []


def test_k11_repeatability_yes_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path, spec1(Y(sc_yaml("repeatability: 'yes'\n"))),
                      "key repeatability of type str", "true or false")
    assert ran == []


def test_repeatability_true_and_false_both_run(monkeypatch, tmp_path):
    for value in ("true", "false"):
        ran = _probe_recorder(monkeypatch)
        r = gh._playtest_spec(tmp_path / "proj",
                              spec1(Y(sc_yaml("repeatability: %s\n" % value))), 300, 120)
        # the scenario, then its no-input control
        assert r["spec_errors"] == [] and ran == ["res://main.tscn"] * 2, (value, r["spec_errors"])
        assert r["behavior"]["scenarios"][0]["ran"] is True


def test_k12_name_as_a_mapping_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path,
                      spec1(dict(Y(sc_yaml()), name={"at": 1, "assert": {"A.b": 1}})),
                      "key name of type dict", "a string")
    assert ran == []


def test_k13_spec_actions_as_a_mapping_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path,
                      spec1(Y(sc_yaml()), actions={"at": 1, "assert": {"A.b": 1}}),
                      "spec has key actions of type dict", "a list of strings")
    assert ran == []
    assert [s["ran"] for s in r["behavior"]["scenarios"]] == [False]


def test_k14_spec_surface_carrying_a_timeline_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path,
                      spec1(Y(sc_yaml()), surface={"timeline": [{"at": 1, "assert": {"A.b": 1}}]}),
                      "spec has key surface of type dict",
                      "a mapping of node name to a list of strings")
    assert ran == []


@pytest.mark.parametrize("where,key,value,needle", [
    ("spec", "scene", {"at": 1}, "spec has key scene of type dict"),
    ("spec", "frames", "400", "spec has key frames of type str"),
    ("spec", "frames", True, "spec has key frames of type bool"),
    ("scenario", "scene", ["res://a.tscn"], "scenario 's' has key scene of type list"),
    ("scenario", "timeline", {"at": 1, "assert": {"A.b": 1}},
     "scenario 's' has key timeline of type dict"),
], ids=["spec-scene", "spec-frames-str", "spec-frames-bool", "scenario-scene",
        "scenario-timeline"])
def test_the_other_keys_have_one_type_too(monkeypatch, tmp_path, where, key, value, needle):
    spec = spec1(Y(sc_yaml()))
    (spec if where == "spec" else spec["scenarios"][0])[key] = value
    r, ran = _refused(monkeypatch, tmp_path, spec, needle)
    assert ran == []


def test_a_scenario_that_is_not_a_mapping_is_refused(monkeypatch, tmp_path):
    r, ran = _refused(monkeypatch, tmp_path, {"scenarios": ["plan_route_event_reroll_budget"]},
                      "scenario 0 is a str")
    assert ran == []


def test_p7_a_non_string_top_level_key_is_refused(monkeypatch, tmp_path):
    spec = dict(spec1(Y(sc_yaml())))
    spec[1] = "x"
    r, ran = _refused(monkeypatch, tmp_path, spec, "spec has unknown top-level key(s) 1")
    assert ran == []


# K17 / K18: a list-form assert item has the probe's keys and one reading

def test_k17_an_unknown_key_in_a_list_form_assert_item_is_refused(monkeypatch, tmp_path):
    spec = spec1(Y(sc_yaml(timeline="- at: 185\n  assert:\n  - {node: PlanEditKind1, expr: 'true', precondition: 'get_popup().visible == true'}\n")))
    _refused(monkeypatch, tmp_path, spec, "scenario 's'", "timeline entry 0 (at: 185)",
             "assert item 0 has unknown key(s) precondition",
             "allowed: attr, expr, mode, name, node")


def test_k18_mode_and_expr_in_one_assert_item_is_refused(monkeypatch, tmp_path):
    spec = spec1(Y(sc_yaml(timeline="- at: 185\n  assert:\n  - {node: PlanEditKind1, attr: presses, mode: unchanged, expr: 'presses == 3'}\n")))
    _refused(monkeypatch, tmp_path, spec, "timeline entry 0 (at: 185)",
             "assert item 0 has both `mode` and `expr`")


def test_list_form_items_the_probe_reads_still_run():
    out, errors = gh._normalize_timeline([{"at": 5, "assert": [
        {"node": "HUD", "expr": "visible == true", "name": "hud", "attr": "visible"},
        {"node": "HUD", "attr": "visible", "mode": "unchanged"}]}])
    assert errors == []
    assert len(out[0]["assert"]) == 2


@pytest.mark.parametrize("raw,needle", [
    ("HUD.visible == true", "`assert` is a str"),
    (["HUD.visible == true"], "assert item 0 is a str"),
], ids=["assert-string", "assert-list-of-strings"])
def test_an_assert_the_probe_cannot_read_is_refused(raw, needle):
    out, errors = gh._normalize_timeline([{"at": 5, "assert": raw}])
    assert out == [] and len(errors) == 1 and needle in errors[0], errors


# (a) / (b) an entry with no `at`

def test_a12_an_assert_entry_with_no_at_is_refused(monkeypatch, tmp_path):
    _refused(monkeypatch, tmp_path, TL("- assert: {A.b: 99}\n- at: 5\n  assert: {A.c: 1}\n"),
             "scenario 's'", "timeline entry 0 has no `at`")


def test_e10_the_engine_shape_is_refused_before_the_engine(monkeypatch, tmp_path):
    k = "Panel/PlanEditKind1"
    spec = {"scenarios": [{"name": "E10_atless_assert_first", "timeline": [
        {"assert": {k + ".presses": "presses == 99"}},
        {"at": 5, "assert": {k + ".presses": "presses == 0"}}]}]}
    _refused(monkeypatch, tmp_path, spec, "'E10_atless_assert_first'",
             "timeline entry 0 has no `at`")


def test_a13_an_entry_with_no_at_is_not_called_frame_0(monkeypatch, tmp_path):
    r, _ = _refused(monkeypatch, tmp_path, TL("- at: 185\n  assert: {A.b: 1}\n- assert: {A.c: 1}\n"),
                    "timeline entry 1 has no `at`")
    assert not any("(at: 0)" in e for e in r["spec_errors"]), r["spec_errors"]


@pytest.mark.parametrize("first", ["- press: ui_accept\n", "- actions: [ui_accept]\n"],
                         ids=["a14-press", "a15-actions"])
def test_a14_a15_press_and_actions_with_no_at_are_refused_alike(first):
    out, errors = gh._normalize_timeline(Y(first + "- at: 5\n  assert: {A.c: 1}\n"))
    assert errors == ["timeline entry 0 has no `at`. Every entry runs on the frame "
                      "its `at` names; write it (`at: 0` is the first frame)."]
    assert [e["at"] for e in out] == [5], "nothing may be scheduled for the refused entry"


# (d) E12: one aim, one offset

def test_e12_a_click_with_two_offsets_is_refused():
    k = "Panel/PlanEditKind1"
    out, errors = gh._normalize_timeline([
        {"at": 10, "clicks": [k + " +0,500 +0,0"]},
        {"at": 20, "assert": {k + ".presses": "presses == 1"}}])
    assert errors == ["timeline entry 0 (at: 10): clicks 'Panel/PlanEditKind1 +0,500 +0,0' "
                      "has 2 offsets (+0,500, +0,0); an aim takes one offset"]
    assert not any("click" in e for e in out)


@pytest.mark.parametrize("key,aim,needle", [
    ("click", "PlanEditKind1 -4,0 +0,90", "click 'PlanEditKind1 -4,0 +0,90' has 2 offsets"),
    ("hovers", ["PlanEditKind1 +0,90 +0,0"], "hovers 'PlanEditKind1 +0,90 +0,0' has 2 offsets"),
    ("clicks", ["PlanEditKind1 left right"], "has 2 buttons (left, right)"),
], ids=["click", "hovers", "two-buttons"])
def test_every_aim_takes_one_offset_and_one_button(key, aim, needle):
    out, errors = gh._normalize_timeline([{"at": 10, key: aim}])
    assert out == [] and len(errors) == 1 and needle in errors[0], errors


def test_one_offset_and_one_button_still_run():
    out, errors = gh._normalize_timeline([{"at": 10, "clicks": ["PlanEditKind1 +0,90 right"],
                                           "hovers": ["PlanEditKind1 -3,4"]}])
    assert errors == []
    assert {e.get("click") or e.get("hover") for e in out} == {
        "PlanEditKind1 +0,90 right", "PlanEditKind1 -3,4"}


# (f) A10: an entry past the frame cap never runs

def test_a10_a_click_past_the_frame_cap_is_refused(monkeypatch, tmp_path):
    _refused(monkeypatch, tmp_path,
             TL("- at: 10\n  assert: {A.b: 1}\n- at: 999999\n  clicks: [PlanEditKind1]\n"),
             "scenario 's'", "frame(s) 999999", "past the 3000-frame cap")


# ── a spec with keys never reaches the canned smoke test ─────────────────
# R/dispatch.py: L1-L3 went to _playtest_legacy and came back passed=True.

_SC = {"name": "s", "timeline": [{"at": 5, "assert": {"A.b": 1}}],
       "reviewer_notes": [{"at": 1}]}


def _dispatch(monkeypatch, tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "copy" / "proj").mkdir(parents=True)
    monkeypatch.setattr(gh, "_copy_project", lambda p: tmp_path / "copy" / "proj")
    monkeypatch.setattr(gh, "_inject_probe", lambda d: None)
    monkeypatch.setattr(gh, "_import_resources", lambda d, t: None)
    took = []
    monkeypatch.setattr(gh, "_playtest_legacy", lambda *a, **k: (
        took.append("legacy"), {"passed": True, "spec_used": False,
                                "spec_errors": None, "summary": "legacy smoke"})[1])
    return took


@pytest.mark.parametrize("spec,needles", [
    ({"scene": "res://main.tscn", "scenario": [_SC]},
     ["spec has no `scenarios` key (top-level keys: scenario, scene)",
      "spec has unknown top-level key(s) scenario"]),
    ({"scenarios": {"s": _SC}}, ["spec has key scenarios of type dict", "a non-empty list"]),
    ({"scenarios": [], "reviewer_notes": [_SC]},
     ["spec has key scenarios of type list", "spec has unknown top-level key(s) reviewer_notes"]),
    (["not", "a", "mapping"], ["spec is a list"]),
], ids=["L1-scenario-typo", "L2-scenarios-mapping", "L3-empty-scenarios-plus-notes",
        "spec-not-a-mapping"])
def test_a_keyed_spec_is_read_as_a_spec_or_refused(monkeypatch, tmp_path, spec, needles):
    took = _dispatch(monkeypatch, tmp_path)
    ran = _probe_recorder(monkeypatch)
    r = gh.playtest_project(str(tmp_path), spec=spec)
    assert took == [] and ran == [], "a spec with keys must never reach the smoke test"
    assert r["spec_used"] is True and r["passed"] is False
    for n in needles:
        assert any(n in e for e in r["spec_errors"]), (n, r["spec_errors"])


@pytest.mark.parametrize("spec", [None, {}], ids=["none", "empty"])
def test_only_a_request_with_no_spec_runs_the_smoke_test(monkeypatch, tmp_path, spec):
    took = _dispatch(monkeypatch, tmp_path)
    gh.playtest_project(str(tmp_path), spec=spec)
    assert took == ["legacy"]


def test_the_monolith_reader_names_a_scenario_typo_and_a_scenarios_mapping(tmp_path):
    from aitelier.tools.godot_playtest.impl import read_spec
    repo = tmp_path / "typo"
    repo.mkdir()
    (repo / "playtest_spec.yaml").write_text("scene: res://main.tscn\nscenario:\n- name: s\n  timeline:\n  - at: 5\n    assert: {A.b: 1}\n")
    spec, info = read_spec(repo)
    assert spec is None
    assert info["errors"] == ["playtest_spec.yaml has no non-empty `scenarios` list "
                              "(`scenarios` is absent; top-level keys: scenario, scene)"]
    repo2 = tmp_path / "mapping"
    repo2.mkdir()
    (repo2 / "playtest_spec.yaml").write_text("scenarios:\n  s:\n    name: s\n    timeline:\n    - at: 5\n      assert: {A.b: 1}\n")
    spec2, info2 = read_spec(repo2)
    assert spec2 is None
    assert info2["errors"] == ["playtest_spec.yaml has no non-empty `scenarios` list "
                               "(`scenarios` is dict; top-level keys: scenarios)"]


def test_the_monolith_reader_names_a_file_that_is_not_a_mapping(tmp_path):
    from aitelier.tools.godot_playtest.impl import read_spec
    (tmp_path / "playtest_spec.yaml").write_text("- name: s\n  timeline: []\n")
    spec, info = read_spec(tmp_path)
    assert spec is None
    assert info["errors"] == ["playtest_spec.yaml is a list, not a YAML mapping"]


# (e) godot_playtest_scenario: the inline wrapper's other keys reach the harness

def test_inline_wrapper_keys_reach_the_harness(monkeypatch, tmp_path):
    import aitelier.tools.godot_playtest.impl as pt
    from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario
    repo = tmp_path / "repo"
    (repo / "playtest").mkdir(parents=True)
    (repo / "project.godot").write_text("config_version=5\n")
    (repo / "playtest" / "_common.yaml").write_text("scene: res://main.tscn\n")
    (repo / "playtest" / "alpha.yaml").write_text(
        "name: alpha\ntimeline:\n- at: 10\n  assert: {x: '1'}\n")
    sent = []
    ran = _probe_recorder(monkeypatch)

    def fake_post(payload, timeout=0):
        sent.append(payload["spec"])
        return gh._playtest_spec(tmp_path / "proj", payload["spec"], 300, 120)

    monkeypatch.setattr(pt, "post_playtest", fake_post)
    body = "scenarios:\n- name: probe_a\n  timeline:\n  - at: 1\n    assert: {A.b: 'b == -1'}\n"
    out = godot_playtest_scenario(project_root=str(repo),
                                  inline_scenario="scene: res://probe.tscn\n" + body)
    assert sent[-1]["scene"] == "res://probe.tscn" and ran == ["res://probe.tscn"], out
    assert out["spec_errors"] == []
    out = godot_playtest_scenario(project_root=str(repo),
                                  inline_scenario="reviewer_notes:\n- at: 1\n" + body)
    assert sent[-1]["reviewer_notes"] == [{"at": 1}]
    assert ran == ["res://probe.tscn"], "the refused inline doc must not reach the engine"
    assert out["hard_passed"] is False
    assert any("unknown top-level key(s) reviewer_notes" in e for e in out["spec_errors"]), out
