"""Tests for the godot_playtest tool's spec plumbing (aitelier/tools/godot_playtest).

The tool reads the authored play-test contract from the repo and forwards it to
the godot-builder sidecar. The contract has TWO shapes — a `playtest/` directory
with one file per scenario (preferred), and the original monolithic
`playtest_spec.yaml` (fallback, still used by every project that has not split).
These tests cover both, the precedence between them, the refusal to run a
contract that could not be read WHOLE, and that the assembled spec reaches the
sidecar payload — all without a live builder (urlopen is mocked).
"""

import json

from aitelier.tools.godot_playtest.impl import (godot_playtest, read_spec,
                                                select_scenarios)


def _write_split(root, common, scenarios):
    d = root / "playtest"
    d.mkdir(exist_ok=True)
    import yaml
    (d / "_common.yaml").write_text(yaml.safe_dump(common, sort_keys=False),
                                    encoding="utf-8")
    for sc in scenarios:
        (d / f"{sc['name']}.yaml").write_text(yaml.safe_dump(sc, sort_keys=False),
                                              encoding="utf-8")
    return d


# ── the monolith (every pre-split project) ────────────────────────────────

def test_read_spec_absent_is_none(tmp_path):
    spec, info = read_spec(tmp_path)
    assert spec is None and info["source"] == ""


def test_read_spec_valid(tmp_path):
    (tmp_path / "playtest_spec.yaml").write_text(
        "scene: res://main.tscn\nscenarios:\n  - name: s\n    timeline: []\n")
    spec, info = read_spec(tmp_path)
    assert spec and spec["scenarios"][0]["name"] == "s"
    assert info["source"] == "playtest_spec.yaml"


def test_read_spec_no_scenarios_is_none(tmp_path):
    # A spec with no scenarios can't drive anything → treat as absent (legacy path).
    (tmp_path / "playtest_spec.yaml").write_text("scene: res://main.tscn\n")
    spec, info = read_spec(tmp_path)
    assert spec is None
    # EMPTY is not MALFORMED: no contract to run is the legacy path, not an error.
    assert info["errors"] == []


def test_a_malformed_monolith_is_a_hard_error_not_a_silent_downgrade(tmp_path):
    """It used to return None, which made the gate run the legacy canned smoke
    test and report a pass — for a contract that was authored and never read.
    An unreadable contract is named instead."""
    (tmp_path / "playtest_spec.yaml").write_text("::: not yaml :::\n[unterminated")
    spec, info = read_spec(tmp_path)
    assert spec is None
    assert any("playtest_spec.yaml" in e for e in info["errors"])
    assert info["source"] == "playtest_spec.yaml"


# ── the split form ────────────────────────────────────────────────────────

def test_split_directory_assembles_the_whole_contract(tmp_path):
    """One file per scenario + a shared header must reassemble to exactly what
    the monolith held — same scenarios, same order, same shared keys."""
    _write_split(
        tmp_path,
        {"scene": "res://main.tscn", "actions": ["ui_accept"],
         "surface": {"Player": ["health"]},
         "scenario_order": ["beta", "alpha"]},
        [{"name": "alpha", "timeline": [{"at": 1, "assert": {"a": "1"}}]},
         {"name": "beta", "timeline": [{"at": 2, "assert": {"b": "2"}}]}])
    spec, info = read_spec(tmp_path)
    assert info["source"] == "playtest/"
    assert info["errors"] == []
    assert spec["scene"] == "res://main.tscn"
    assert spec["surface"] == {"Player": ["health"]}
    # scenario_order fixes the run order — NOT the alphabetical file order.
    assert [s["name"] for s in spec["scenarios"]] == ["beta", "alpha"]
    # and `scenario_order` is a loader directive, not part of the contract the
    # sidecar is handed.
    assert "scenario_order" not in spec


def test_a_scenario_file_missing_from_scenario_order_still_runs(tmp_path):
    """Adding a scenario must be "drop in a file". If an unlisted file were
    skipped, the contract would shrink by a file nobody remembered to list —
    which is the failure mode this whole split exists to prevent."""
    _write_split(tmp_path, {"scenario_order": ["alpha"]},
                 [{"name": "alpha", "timeline": []},
                  {"name": "zeta", "timeline": []}])
    spec, info = read_spec(tmp_path)
    assert [s["name"] for s in spec["scenarios"]] == ["alpha", "zeta"]
    assert info["errors"] == []


def test_split_directory_wins_over_a_leftover_monolith_and_says_so(tmp_path):
    """Both shapes on disk is a drift hazard: the monolith still looks
    authoritative, so the next round edits the file nothing reads. The directory
    wins, and the report's summary names the file that was ignored."""
    _write_split(tmp_path, {"scenario_order": ["alpha"]},
                 [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest_spec.yaml").write_text(
        "scenarios:\n  - name: stale\n    timeline: []\n")
    spec, info = read_spec(tmp_path)
    assert [s["name"] for s in spec["scenarios"]] == ["alpha"]
    assert info["notes"] and "IGNORED" in info["notes"][0]


def test_an_unreadable_scenario_file_is_an_error_not_a_silent_skip(tmp_path):
    """A play-test that quietly evaluates 25 of 26 scenarios and reports "all
    assertions passed" is a green light over an absence. The loader names the
    file instead of dropping it."""
    _write_split(tmp_path, {"scenario_order": ["alpha", "broken"]},
                 [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest" / "broken.yaml").write_text("::: not yaml :::\n[nope")
    spec, info = read_spec(tmp_path)
    assert spec is not None                      # alpha still parsed
    assert any("broken.yaml" in e for e in info["errors"])


def test_a_yaml_in_playtest_that_is_not_a_scenario_is_an_error(tmp_path):
    _write_split(tmp_path, {}, [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest" / "notes.yaml").write_text("just: a note\n")
    _spec, info = read_spec(tmp_path)
    assert any("notes.yaml" in e for e in info["errors"])


def test_an_empty_playtest_directory_falls_back_to_the_monolith(tmp_path):
    (tmp_path / "playtest").mkdir()
    (tmp_path / "playtest_spec.yaml").write_text(
        "scenarios:\n  - name: s\n    timeline: []\n")
    spec, info = read_spec(tmp_path)
    assert info["source"] == "playtest_spec.yaml"
    assert spec["scenarios"][0]["name"] == "s"


def test_select_scenarios_narrows_and_reports_unknown_names(tmp_path):
    spec = {"scene": "res://main.tscn",
            "scenarios": [{"name": "a", "timeline": []},
                          {"name": "b", "timeline": []}]}
    picked, unknown = select_scenarios(spec, ["b", "nope"])
    assert [s["name"] for s in picked["scenarios"]] == ["b"]
    assert picked["scene"] == "res://main.tscn"
    assert unknown == ["nope"]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_playtest_forwards_spec_to_builder(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "playtest_spec.yaml").write_text(
        "scenarios:\n  - name: flap\n    timeline:\n      - {at: 8, assert: []}\n")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"passed": True, "spec_used": True, "frames": 1,
                          "errors": [], "state": {}, "summary": "ok",
                          "behavior": {"all_passed": True, "scenarios": []}})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert "spec" in captured["body"]
    assert captured["body"]["spec"]["scenarios"][0]["name"] == "flap"
    assert out["passed"] is True
    # The report was persisted for the reviewer.
    assert (tmp_path / "playtest_report.json").is_file()
    report = json.loads((tmp_path / "playtest_report.json").read_text())
    assert report["spec_source"] == "playtest_spec.yaml"


def test_playtest_forwards_the_split_contract_to_builder(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    _write_split(tmp_path, {"scene": "res://main.tscn",
                            "scenario_order": ["flap", "dive"]},
                 [{"name": "flap", "timeline": [{"at": 8, "assert": []}]},
                  {"name": "dive", "timeline": [{"at": 9, "assert": []}]}])
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"passed": True, "spec_used": True, "frames": 1,
                          "errors": [], "state": {}, "summary": "ok",
                          "behavior": {"all_passed": True, "scenarios": []}})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert [s["name"] for s in captured["body"]["spec"]["scenarios"]] == ["flap", "dive"]
    assert captured["body"]["spec"]["scene"] == "res://main.tscn"
    report = json.loads((tmp_path / "playtest_report.json").read_text())
    assert report["spec_source"] == "playtest/"


def test_a_contract_that_cannot_be_read_whole_hard_fails_without_running(
        tmp_path, monkeypatch):
    """Running the readable subset and reporting on it is the pass-on-absence
    shape: the missing scenario is invisible in the results and `all(passed)`
    holds over whatever survived. Refuse instead."""
    (tmp_path / "project.godot").write_text("config_version=5\n")
    _write_split(tmp_path, {}, [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest" / "broken.yaml").write_text("::: not yaml :::\n[nope")
    called = {"n": 0}

    def fake_urlopen(req, timeout=0):
        called["n"] += 1
        return _FakeResp({"passed": True})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert out["passed"] is False
    assert called["n"] == 0, "the sidecar must not be asked to run a partial contract"
    report = json.loads((tmp_path / "playtest_report.json").read_text())
    assert "broken.yaml" in " ".join(report["spec_load_errors"])


def test_playtest_no_spec_omits_spec_key(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"passed": True, "spec_used": False, "frames": 1,
                          "errors": [], "state": {}, "summary": "ok", "behavior": None})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert "spec" not in captured["body"]


def test_playtest_non_godot_skips(tmp_path):
    # No project.godot → not a game → pass without touching the builder.
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert out["passed"] is True


# -- duplicate mapping keys ------------------------------------------------
# yaml.safe_load keeps the LAST value for a repeated key, so a timeline entry
# with two `assert:` blocks loaded as the second block alone and the gate
# counted "N/N passed" over the survivor. Measured 2026-09-05 on the game repo:
# 18 scenario files, ~30 assertions discarded before the suite started.

_DUP_SCENARIO = """name: dup
timeline:
- at: 10
  assert:
    Player.health: health == -999
  assert:
    Player.health: health == health
"""


def test_a_repeated_assert_key_is_refused_before_the_builder_is_asked(
        tmp_path, monkeypatch):
    """The exact live shape: the FIRST assert block would fail, the SECOND
    passes, and last-one-wins made the run green over a discarded assertion.
    It must hard-fail, and it must not reach the sidecar."""
    (tmp_path / "project.godot").write_text("config_version=5\n")
    _write_split(tmp_path, {"scene": "res://main.tscn"},
                 [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest" / "dup.yaml").write_text(_DUP_SCENARIO)
    called = {"n": 0}

    def fake_urlopen(req, timeout=0):
        called["n"] += 1
        return _FakeResp({"passed": True})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert out["passed"] is False
    assert called["n"] == 0, "a contract with a discarded assertion must not run"
    errs = " ".join(json.loads(
        (tmp_path / "playtest_report.json").read_text())["spec_load_errors"])
    assert "dup.yaml" in errs and "assert" in errs and "line" in errs


def test_a_duplicate_key_in_common_is_refused(tmp_path):
    d = tmp_path / "playtest"
    d.mkdir()
    (d / "_common.yaml").write_text(
        "scene: res://main.tscn\nsurface:\n  Player: [health]\nsurface:\n"
        "  Player: [mana]\n")
    (d / "alpha.yaml").write_text("name: alpha\ntimeline: []\n")
    _spec, info = read_spec(tmp_path)
    joined = " ".join(info["errors"])
    assert "_common.yaml" in joined and "surface" in joined


def test_a_duplicate_key_in_the_monolith_is_refused(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "playtest_spec.yaml").write_text(
        "scenarios:\n  - name: s\n    timeline:\n      - at: 1\n"
        "        assert: {A.b: b == 1}\n        assert: {A.c: c == 2}\n")
    called = {"n": 0}

    def fake_urlopen(req, timeout=0):
        called["n"] += 1
        return _FakeResp({"passed": True})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert out["passed"] is False
    assert called["n"] == 0, "the canned smoke test must not stand in for it"
    errs = " ".join(json.loads(
        (tmp_path / "playtest_report.json").read_text())["spec_load_errors"])
    assert "assert" in errs and "playtest_spec.yaml" in errs


def test_a_nested_duplicate_inside_a_timeline_entry_is_refused(tmp_path):
    _write_split(tmp_path, {}, [{"name": "alpha", "timeline": []}])
    (tmp_path / "playtest" / "nested.yaml").write_text(
        "name: nested\ntimeline:\n- at: 3\n  actions: [ui_accept]\n"
        "  actions: [ui_cancel]\n")
    _spec, info = read_spec(tmp_path)
    assert any("nested.yaml" in e and "actions" in e for e in info["errors"])


def test_anchors_and_merge_keys_in_the_contract_still_load(tmp_path):
    """Legal YAML must keep working: the strictness is about repeated keys, not
    about anchors, and a contract that merges a shared block is not a duplicate."""
    d = tmp_path / "playtest"
    d.mkdir()
    (d / "_common.yaml").write_text(
        "defaults: &d\n  scene: res://main.tscn\n"
        "scene: res://main.tscn\nsurface:\n  Player: [health]\n")
    (d / "alpha.yaml").write_text(
        "name: alpha\nbase: &b {at: 1}\ntimeline:\n- <<: *b\n  assert: {A.b: b == 1}\n")
    spec, info = read_spec(tmp_path)
    assert info["errors"] == []
    assert spec["scenarios"][0]["timeline"][0]["at"] == 1

