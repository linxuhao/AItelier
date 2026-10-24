"""Input delivery and cooperative acceptance through existing entrypoints.

Offline probes below deliberately supply NO producer evidence. Native tests use
real Godot, retain its returned reports/raw streams in pytest's private temp
area, and independently check gameplay. They must be selected with
AITELIER_REQUIRE_NATIVE=1 for acceptance; an unavailable engine then FAILS.
"""

import copy
import importlib.util
import json
import os
import re
import shutil
import textwrap
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness_input_observation", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)
_FIXTURE = Path(__file__).resolve().parent / "fixtures/godot_input_acceptance_controls"
_GODOT = os.environ.get("GODOT_BIN") or shutil.which("godot")
_ACK = '        input_acceptance["decisions"].append({"run_id": token["run_id"], "seq": token["seq"], "decision": decision})\n'


def _case(name="runtime3_actualgame2accepted"):
    spec = json.loads((_FIXTURE / "spec.json").read_text(encoding="utf-8"))
    spec["scenarios"] = [s for s in spec["scenarios"] if s["name"] == name]
    assert len(spec["scenarios"]) == 1
    return spec


def _scenario(report):
    rows = report["behavior"]["scenarios"]
    assert len(rows) == 1
    return rows[0]


def _observation(report):
    obs = _scenario(report)["input_observation"]
    assert obs["version"] == 1
    assert obs["scope"] == "timeline_action_press"
    return obs


def _decode(value):
    """Decode only the public tagged value format; never infer decisions."""
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("__t")
    if tag == "Array":
        return [_decode(v) for v in value["__v"]]
    if tag == "Dictionary":
        pairs = value["__v"]
        out = {_decode(k): _decode(v) for k, v in pairs}
        assert len(out) == len(pairs)
        return out
    if tag == "Float":
        return float(value["__v"])
    raise AssertionError("unexpected public observation tag: %r" % value)


def _raw_assert(spec):
    spec["scenarios"][0]["timeline"].append({"at": 59, "assert": [
        {"name": "raw received", "node": "Player", "expr": "true", "attr": "raw_received"}
    ]})
    return spec


def _received(report):
    a = next(a for a in _scenario(report)["asserts"] if a["name"] == "raw received")
    assert a["measurement"] == "ok" and a["passed"] is True
    return _decode(a["observed"])


def _position(report, y):
    player = report["state"]["/root/Player"]
    assert player["vars"]["grid_y"] == y
    assert player["pos"][1] == y
    # Neither application-owned observation Dictionary becomes L0 state.
    assert "input_acceptance" not in player["vars"]
    assert "raw_received" not in player["vars"]


def _assert_dispatch(report, presses, events, tagged):
    raw = _received(report)
    assert raw["presses"] == presses
    assert raw["events"] == events
    assert raw["tagged"] == tagged
    assert raw["untagged"] == presses - tagged
    assert raw["bad_metadata"] == 0
    return raw


def _assert_unobserved(obs):
    assert obs["status"] == "unobserved"
    for key in ("runtime_injected", "runtime_events_injected", "accepted", "rejected", "unknown", "acceptance_pass", "receipts", "witness"):
        assert obs[key] is None, (key, obs)


def _assert_invalid(report, presses=3, events=6):
    row, obs = _scenario(report), _observation(report)
    assert obs["runtime_injected"] == presses
    assert obs["runtime_events_injected"] == events
    assert obs["accepted"] is None and obs["rejected"] is None
    assert obs["unknown"] == presses
    assert obs["complete"] is False and obs["acceptance_pass"] is None
    assert obs["errors"]
    assert row["passed"] is False and report["passed"] is False
    assert any("input" in e.lower() and "observ" in e.lower() for e in report["spec_errors"])


def _replace(project, old, new):
    path = project / "player.gd"
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _append(project, source):
    path = project / "player.gd"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + source, encoding="utf-8")


def _value(project, body, witness=False):
    _replace(project, "extends Node2D\n", "extends Node2D\nvar boundary_value\n")
    if witness:
        body += '\ninput_acceptance["padding"] = boundary_value'
    _append(project, "func _ready() -> void:\n" + textwrap.indent(body, "    ") + "\n")


def _probe_without_producer(monkeypatch, prior_error=None):
    """An old/omitted producer is a transport control, never native evidence."""
    calls = []

    def fake(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
             timing=None, render=True, **kwargs):
        payload = json.loads(Path(extra["AITELIER_PROBE_SPEC"]).read_text())
        calls.append((frames, payload))
        if timing is not None:
            timing["game_usec"] = int(frames * 1_000_000 / 60)
        asserts = [{"name": "transport assertion", "passed": True}
                   for e in payload["timeline"] for _ in e.get("assert", [])]
        driven = any("press" in e for e in payload["timeline"])
        return {"frames": frames, "complete": True, "asserts": asserts,
                "nodes": {"/root/Player": {"vars": {"grid_y": -2 if driven else 0}}},
                "spec_errors": [prior_error] if prior_error else []}, [], False

    monkeypatch.setattr(gh, "_run_probe", fake)
    return calls


@pytest.mark.parametrize("declaration", [
    None, [], {}, {"node": "Player"},
    {"node": "Player", "property": "input_acceptance", "extra": 1},
    {"node": 1, "property": "input_acceptance"},
    {"node": "", "property": "input_acceptance"},
    {"node": "Player", "property": True},
    {"node": "Player", "property": "input_acceptance.decisions"},
    {"node": "Player", "property": "get_instance_id()"},
])
def test_declaration_is_an_exact_scenario_mapping(monkeypatch, tmp_path, declaration):
    calls = _probe_without_producer(monkeypatch)
    spec = _case()
    spec["scenarios"][0]["input_observation"] = declaration
    report = gh._playtest_spec(tmp_path / "proj", spec, 60, 120)
    assert calls == []
    assert report["passed"] is False and _scenario(report)["ran"] is False
    assert any("input_observation" in e for e in report["spec_errors"])


@pytest.mark.parametrize("where", ["header", "timeline"])
def test_declaration_has_only_one_scope(monkeypatch, tmp_path, where):
    calls = _probe_without_producer(monkeypatch)
    spec = _case()
    declaration = spec["scenarios"][0].pop("input_observation")
    target = spec if where == "header" else spec["scenarios"][0]["timeline"][0]
    target["input_observation"] = declaration
    report = gh._playtest_spec(tmp_path / "proj", spec, 60, 120)
    assert calls == []
    assert report["passed"] is False
    assert any("unknown" in e and "input_observation" in e for e in report["spec_errors"])


def test_admission_preserves_89_frames_and_missing_producer_is_not_success(monkeypatch, tmp_path):
    calls = _probe_without_producer(monkeypatch)
    spec = _case()
    report = gh._playtest_spec(tmp_path / "proj", spec, 60, 120)
    assert calls, report
    frames, payload = calls[0]
    assert frames == payload["frames"] == 89
    assert payload["input_observation"] == spec["scenarios"][0]["input_observation"]
    assert [e["at"] for e in payload["timeline"]] == [20, 30, 40, 59]
    obs = _observation(report)
    assert obs["scheduled_presses"] == 3
    assert obs["delivery_complete"] is not True
    assert obs["complete"] is False and obs["acceptance_pass"] is None
    assert obs["status"] != "all_accepted"
    assert report["passed"] is False and report["spec_errors"]


def test_original_failure_precedes_missing_producer_diagnostic(monkeypatch, tmp_path):
    first = "frame 59: original path refusal sentinel"
    _probe_without_producer(monkeypatch, first)
    report = gh._playtest_spec(tmp_path / "proj", _case(), 60, 120)
    assert report["spec_errors"][0] == "scenario 'runtime3_actualgame2accepted': " + first
    assert len(report["spec_errors"]) >= 2
    assert report["passed"] is False


def test_undeclared_transport_keeps_the_existing_hard_and_advisory_result(monkeypatch, tmp_path):
    calls = _probe_without_producer(monkeypatch)
    spec = _case()
    spec["scenarios"][0].pop("input_observation")
    report = gh._playtest_spec(tmp_path / "proj", spec, 60, 120)
    assert len(calls) == 2  # driven pass and its independently isolated control
    assert all("input_observation" not in payload for _, payload in calls)
    assert report["passed"] is True and report["spec_errors"] == []
    assert _scenario(report)["passed"] is True
    _assert_unobserved(_observation(report))


def test_consumer_zero_assertion_guard_survives_without_producer(monkeypatch, tmp_path):
    import aitelier.tools.godot_playtest.impl as pt
    repo, output = tmp_path / "proj", tmp_path / "out"
    repo.mkdir()
    (repo / "project.godot").write_text("config_version=5\n")
    (repo / "playtest_spec.yaml").write_text(json.dumps(_case()))
    stub = {"passed": True, "spec_used": True, "summary": "transport sentinel",
            "behavior": {"scenarios": [{"name": "s", "asserts": []}]}}
    monkeypatch.setattr(pt, "post_playtest", lambda *a, **k: copy.deepcopy(stub))
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(output))
    report = json.loads((output / "playtest_report.json").read_text())
    assert result["passed"] is False and report["blind_builder"] is True
    assert "ZERO assertions" in report["summary"]
    assert "transport sentinel" in report["summary"]


@pytest.fixture
def native(tmp_path, monkeypatch):
    if not _GODOT or not shutil.which(str(_GODOT)):
        if os.environ.get("AITELIER_REQUIRE_NATIVE") == "1":
            pytest.fail("native acceptance requires a real Godot binary; set GODOT_BIN")
        pytest.skip("no Godot binary; this is NOT native acceptance")
    monkeypatch.setattr(gh, "GODOT_BIN", str(_GODOT))
    project = tmp_path / "project"
    shutil.copytree(_FIXTURE, project)
    original = gh._run_probe
    passes, raw_logs, active_fault = [], [], [None]

    def capture(dst, state_path, frames, timeout, extra, **kwargs):
        path = Path(extra["AITELIER_PROBE_SPEC"])
        payload = json.loads(path.read_text())
        if active_fault[0] is not None and payload.get("timeline"):
            active_fault[0](payload)
            path.write_text(json.dumps(payload), encoding="utf-8")
        # This still runs the real probe and binary, with the existing CPU-only
        # headless entrypoint. No render owner, X server or GPU is requested.
        kwargs["render"] = False
        kwargs["raw"] = raw_logs
        try:
            result = original(dst, state_path, frames, timeout, extra, **kwargs)
            passes.append({"home": extra["HOME"], "requested_frames": frames,
                           "probe": copy.deepcopy(result[0]),
                           "errors": copy.deepcopy(result[1]), "timed_out": result[2]})
            return result
        finally:
            (tmp_path / "native-trace.json").write_text(
                json.dumps({"passes": passes, "raw": raw_logs}, indent=2), encoding="utf-8")

    monkeypatch.setattr(gh, "_run_probe", capture)

    def run(spec, fault=None):
        before = len(passes)
        active_fault[0] = fault
        report = gh.playtest_project(str(project), frames=60, timeout=120,
                                     captures=0, spec=copy.deepcopy(spec))
        (tmp_path / ("native-report-%d.json" % before)).write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        observed = passes[before:]
        assert observed, report
        assert report.get("gate_skipped") is not True
        assert report.get("no_project") is not True
        assert all(p["timed_out"] is False for p in observed)
        homes = [p["home"] for p in passes]
        assert len(set(homes)) == len(homes)
        assert all(not Path(home).exists() for home in homes)
        return report, observed[0]["probe"]

    return project, run


@pytest.mark.parametrize("name,y,accepted,rejected,status,acceptance,dead", [
    ("runtime3_actualgame2accepted", -2, 2, 1, "partial", False, False),
    ("runtime3_actualgame3accepted", -3, 3, 0, "all_accepted", True, False),
    ("no_input", 0, None, None, "not_applicable", None, False),
    ("witness_only", 0, 3, 0, "l0_failed", False, True),
])
def test_real_literal_causal_controls(native, name, y, accepted, rejected, status, acceptance, dead):
    project, run = native
    spec = _raw_assert(_case(name))
    report, probe = run(spec)
    row, obs = _scenario(report), _observation(report)
    presses = 0 if name == "no_input" else 3
    assert probe["frames"] == 89 and probe["complete"] is True
    assert report["timing"]["scenarios"][0]["frames_stepped"] == 89
    assert row["complete"] is True
    assert row["pressed"] is bool(presses) and row["input_dead"] is dead
    assert all(a["passed"] is True for a in row["asserts"])
    _position(report, y)
    raw = _assert_dispatch(report, presses, 2 * presses, presses)
    assert obs["scheduled_presses"] == obs["runtime_injected"] == presses
    assert obs["runtime_events_injected"] == 2 * presses
    assert (obs["accepted"], obs["rejected"], obs["unknown"]) == (accepted, rejected, 0)
    assert obs["status"] == status and obs["acceptance_pass"] is acceptance
    assert obs["delivery_complete"] is True and obs["complete"] is True
    assert obs["errors"] == [] and report["spec_errors"] == []
    assert report["passed"] is (not dead)
    receipts = _decode(obs["receipts"])
    decisions = _decode(obs["witness"])["decisions"]
    assert len(receipts) == 2 * presses
    assert len(decisions) == presses
    assert all(d["run_id"] == obs["run_id"] for d in decisions)
    if presses:
        assert isinstance(obs["run_id"], str) and obs["run_id"]
        assert [r[0] for r in receipts] == [1, 2, 3, 4, 5, 6]
        assert [r[1] for r in receipts] == [20, 22, 30, 32, 40, 42]
        assert [r[3] for r in receipts] == ["timeline", "auto_release"] * 3
        assert all(r[4] == "action" and r[5] == "move_up" and r[7] == "returned" for r in receipts)
        assert [r[6] for r in receipts] == [True, False] * 3
        assert [r[2] for r in receipts[::2]] == [0, 1, 2]
        assert [d["seq"] for d in decisions] == [1, 3, 5]
        frames = raw["engine_press_frames"]
        assert [f - frames[0] for f in frames] == [0, 10, 20]
    if name == "runtime3_actualgame2accepted":
        assert [d["decision"] for d in decisions] == ["accepted", "rejected", "accepted"]
        # Complete measured rejection remains an observation, not a hard error.
        assert row["passed"] is True and report["passed"] is True
    if name == "runtime3_actualgame3accepted":
        again, _ = run(spec)
        assert _observation(again)["run_id"] != obs["run_id"]
        _position(again, -3)


@pytest.mark.parametrize("fault", [
    "missing", "constant", "scalar", "version", "bool_version", "decisions_type",
    "duplicate", "conflict", "foreign", "release_id", "unknown_id", "bool_id", "float_id", "decision",
])
def test_real_invalid_terminal_witness_is_unknown_after_all_dispatch(native, fault):
    project, run = native
    spec = _raw_assert(_case())
    declaration = spec["scenarios"][0]["input_observation"]
    if fault in ("missing", "constant", "scalar"):
        declaration["property"] = {"missing": "nonexistent_property", "constant": "PI", "scalar": "busy_frames"}[fault]
    else:
        replacement = {
            "version": _ACK + '        input_acceptance["version"] = 2\n',
            "bool_version": _ACK + '        input_acceptance["version"] = true\n',
            "decisions_type": '        input_acceptance["decisions"] = {}\n',
            "duplicate": _ACK + _ACK,
            "conflict": _ACK + _ACK.replace('"decision": decision', '"decision": "rejected" if decision == "accepted" else "accepted"'),
            "foreign": _ACK.replace('token["run_id"]', '"foreign-run"'),
            "release_id": _ACK.replace('token["seq"]', 'token["seq"] + 1'),
            "unknown_id": _ACK.replace('token["seq"]', 'token["seq"] + 1000'),
            "bool_id": _ACK.replace('token["seq"]', 'true'),
            "float_id": _ACK.replace('token["seq"]', 'float(token["seq"])'),
            "decision": _ACK.replace('"decision": decision', '"decision": "consumed"'),
        }[fault]
        _replace(project, _ACK, replacement)
    report, probe = run(spec)
    assert probe["frames"] == 89
    _position(report, -2)
    _assert_dispatch(report, 3, 6, 3)
    _assert_invalid(report)


@pytest.mark.parametrize("delay,accepted,rejected,unknown,complete", [
    (None, 2, 0, 1, False), (50, 2, 1, 0, True), (70, 2, 0, 1, False),
])
def test_real_absent_and_deferred_decisions_use_only_the_existing_window(native, delay, accepted, rejected, unknown, complete):
    project, run = native
    if delay is None:
        _replace(project, _ACK, '        if decision != "rejected":\n    ' + _ACK)
    else:
        _replace(project, _ACK,
                 '        if decision == "rejected":\n'
                 '            _later(token.duplicate(true), decision)\n'
                 '        else:\n    ' + _ACK)
        _append(project,
                'func _later(token: Dictionary, decision: String) -> void:\n'
                '    for _i in range(%d):\n' % delay +
                '        await get_tree().process_frame\n'
                '    input_acceptance["decisions"].append({"run_id": token["run_id"], "seq": token["seq"], "decision": decision})\n')
    report, probe = run(_raw_assert(_case()))
    obs = _observation(report)
    assert probe["frames"] == 89
    _position(report, -2)
    _assert_dispatch(report, 3, 6, 3)
    assert (obs["accepted"], obs["rejected"], obs["unknown"]) == (accepted, rejected, unknown)
    assert obs["complete"] is complete and obs["delivery_complete"] is True
    assert obs["acceptance_pass"] is (False if complete else None)
    assert report["passed"] is complete
    if not complete:
        assert _scenario(report)["passed"] is False and report["spec_errors"]


def test_real_terminal_read_does_not_validate_an_earlier_invalid_prefix(native):
    project, run = native
    _append(project,
            'func _ready() -> void:\n'
            '    input_acceptance["version"] = 0\n'
            '    for _i in range(70):\n'
            '        await get_tree().process_frame\n'
            '    input_acceptance["version"] = 1\n')
    report, probe = run(_raw_assert(_case()))
    assert probe["frames"] == 89
    _position(report, -2)
    _assert_dispatch(report, 3, 6, 3)
    assert _observation(report)["status"] == "partial"
    assert _observation(report)["complete"] is True
    assert report["passed"] is True and report["spec_errors"] == []


def test_real_early_exit_cannot_be_complete_acceptance(native):
    project, run = native
    _append(project,
            'func _ready() -> void:\n'
            '    for _i in range(50):\n'
            '        await get_tree().process_frame\n'
            '    get_tree().quit()\n')
    report, probe = run(_case("runtime3_actualgame3accepted"))
    obs = _observation(report)
    assert 42 < probe["frames"] < 59
    assert probe["complete"] is False and _scenario(report)["complete"] is False
    assert obs["runtime_injected"] == 3 and obs["runtime_events_injected"] == 6
    assert obs["complete"] is False and obs["acceptance_pass"] is None
    assert report["passed"] is False and report["spec_errors"]
    row = _scenario(report)
    assert row["asserts"] == []
    assert row["expected_asserts"] == row["asserts_missing"] == 2
    assert "probe did not reach its frame budget" in report["spec_errors"][0]
    # During scene teardown get_path() need not retain /root/Player. Identify
    # the fixture's sole gameplay snapshot by class and script-property shape,
    # require uniqueness, then check actual state independently of the witness.
    players = [node for node in report["state"].values()
               if node.get("class") == "Node2D"
               and {"grid_y", "busy_frames", "busy_until", "witness_only"}
               <= set(node.get("vars", {}))]
    assert len(players) == 1, report["state"]
    player = players[0]
    assert player["vars"]["busy_frames"] == 5
    assert player["vars"]["witness_only"] is False
    assert player["vars"]["grid_y"] == -3
    assert player["pos"][1] == -3
    assert "input_acceptance" not in player["vars"]
    assert "raw_received" not in player["vars"]


def test_real_skipped_producer_is_detected_against_the_host_schedule(native):
    project, run = native
    spec = _raw_assert(_case("runtime3_actualgame3accepted"))
    spec["scenarios"][0]["timeline"][3]["assert"] = {"Player.grid_y": -2, "Player.position.y": -2}

    def omit_middle_press(payload):
        payload["timeline"] = [e for e in payload["timeline"]
                               if not (e.get("at") == 30 and e.get("press") == "move_up")]

    # Fault only the outgoing runtime payload. The original host schedule still
    # requires three presses; the actual engine receives and commits only two.
    report, probe = run(spec, omit_middle_press)
    obs = _observation(report)
    assert probe["frames"] == 89
    _position(report, -2)
    _assert_dispatch(report, 2, 4, 2)
    assert obs["scheduled_presses"] == 3
    assert obs["runtime_injected"] == 2 and obs["runtime_events_injected"] == 4
    assert obs["delivery_complete"] is False
    assert obs["complete"] is False and obs["acceptance_pass"] is not True
    assert obs["status"] != "all_accepted"
    assert report["passed"] is False and report["spec_errors"]


def test_real_receipts_preserve_release_mouse_and_entry_order(native):
    project, run = native
    spec = _case("runtime3_actualgame3accepted")
    spec["scenarios"][0]["timeline"] = [
        {"at": 20, "press": "move_up"},
        {"at": 22, "hover": "Player +32,32"},
        {"at": 22, "click": "Player +32,32"},
        {"at": 22, "press": "move_up"},
        {"at": 22, "release": "move_up"},
        {"at": 59, "assert": {"Player.grid_y": -1, "Player.position.y": -1}},
    ]
    report, _ = run(_raw_assert(spec))
    obs = _observation(report)
    _position(report, -1)
    _assert_dispatch(report, 2, 9, 2)
    rows = _decode(obs["receipts"])
    assert [r[0] for r in rows] == list(range(1, 10))
    assert [r[1] for r in rows] == [20] + [22] * 7 + [24]
    assert [r[4] for r in rows] == ["action", "action", "mouse_motion", "mouse_motion", "mouse_button", "mouse_button", "action", "action", "action"]
    assert [r[3] for r in rows] == ["timeline", "auto_release"] + ["timeline"] * 6 + ["auto_release"]
    assert [r[2] for r in rows if r[3] == "timeline"] == [0, 1, 2, 2, 2, 3, 4]
    assert all(r[7] == "returned" for r in rows)
    assert [d["seq"] for d in _decode(obs["witness"])["decisions"]] == [1, 7]
    assert (obs["runtime_injected"], obs["runtime_events_injected"], obs["accepted"], obs["rejected"]) == (2, 9, 1, 1)
    assert obs["delivery_complete"] is True and obs["complete"] is True
    assert report["passed"] is True


@pytest.mark.parametrize("entries,events", [
    ([{"at": 20, "release": "move_up"}], 1),
    ([{"at": 20, "hover": "Player"}, {"at": 30, "click": "Player"}], 4),
])
def test_real_unsupported_only_input_does_not_claim_semantic_acceptance(native, entries, events):
    project, run = native
    spec = _case("no_input")
    spec["scenarios"][0]["timeline"] = entries + spec["scenarios"][0]["timeline"]
    report, _ = run(_raw_assert(spec))
    obs = _observation(report)
    _position(report, 0)
    _assert_dispatch(report, 0, events, 0)
    assert obs["runtime_injected"] == 0 and obs["runtime_events_injected"] == events
    assert obs["status"] == "unsupported" and obs["acceptance_pass"] is None
    assert _scenario(report)["pressed"] is True
    assert _scenario(report)["input_dead"] is True and report["passed"] is False


def test_real_polling_without_a_retained_token_remains_unknown(native):
    project, run = native
    path = project / "player.gd"
    source = path.read_text()
    source = source[:source.index("func _unhandled_input(")] + (
        'func _unhandled_input(event: InputEvent) -> void:\n'
        '    _count_received(event)\n\n'
        'func _process(_delta: float) -> void:\n'
        '    if Input.is_action_just_pressed("move_up"):\n'
        '        grid_y -= 1\n'
        '        position.y = float(grid_y)\n')
    path.write_text(source, encoding="utf-8")
    report, probe = run(_raw_assert(_case("runtime3_actualgame3accepted")))
    obs = _observation(report)
    assert probe["frames"] == 89
    _position(report, -3)
    _assert_dispatch(report, 3, 6, 3)
    assert (obs["accepted"], obs["rejected"], obs["unknown"]) == (0, 0, 3)
    assert obs["delivery_complete"] is True
    assert obs["complete"] is False and obs["acceptance_pass"] is None
    assert report["passed"] is False


@pytest.mark.parametrize("declared", [False, True])
def test_real_513_presses_keep_all_1026_injections_after_refusal(native, declared):
    project, run = native
    path = project / "all.tscn"
    path.write_text(path.read_text().replace("busy_frames = 5", "busy_frames = 0"))
    spec = _case("runtime3_actualgame3accepted")
    sc = spec["scenarios"][0]
    if not declared:
        sc.pop("input_observation")
    sc["timeline"] = [{"at": 4 * i, "press": "move_up"} for i in range(513)] + [
        {"at": 2060, "assert": {"Player.grid_y": -513, "Player.position.y": -513}},
        {"at": 2060, "assert": [{"name": "raw received", "node": "Player", "expr": "true", "attr": "raw_received"}]},
    ]
    report, probe = run(spec)
    assert probe["frames"] == 2090 and probe["complete"] is True
    _position(report, -513)
    _assert_dispatch(report, 513, 1026, 513 if declared else 0)
    obs = _observation(report)
    if declared:
        _assert_invalid(report, 513, 1026)
        assert obs["scheduled_presses"] == 513
        assert obs["receipts"] is None and obs["delivery_complete"] is None
        assert any("receipt" in e.lower() for e in obs["errors"])
    else:
        _assert_unobserved(obs)
        assert report["passed"] is True and report["spec_errors"] == []
        assert _scenario(report)["passed"] is True and _scenario(report)["input_dead"] is False


@pytest.mark.parametrize("presses", [227, 228])
def test_real_receipt_node_boundary_also_respects_the_actual_byte_cap(native, presses):
    project, run = native
    path = project / "all.tscn"
    path.write_text(path.read_text().replace("busy_frames = 5", "busy_frames = 0"))
    # Keep the witness small so the receipt boundary is independently visible.
    _replace(project, _ACK, "        pass\n")
    spec = _case("runtime3_actualgame3accepted")
    at = 4 * presses + 12
    spec["scenarios"][0]["timeline"] = [{"at": 4 * i, "press": "move_up"} for i in range(presses)] + [
        {"at": at, "assert": {"Player.grid_y": -presses, "Player.position.y": -presses}},
        {"at": at, "assert": [{"name": "raw received", "node": "Player", "expr": "true", "attr": "raw_received"}]},
    ]
    report, probe = run(spec)
    obs = _observation(report)
    assert probe["frames"] == at + 30
    _position(report, -presses)
    _assert_dispatch(report, presses, 2 * presses, presses)
    assert obs["runtime_injected"] == presses and obs["runtime_events_injected"] == 2 * presses
    assert obs["unknown"] == presses and obs["acceptance_pass"] is None
    if presses == 228:
        assert obs["receipts"] is None and obs["delivery_complete"] is None
        assert any("receipt" in e.lower() for e in obs["errors"])
    elif obs["receipts"] is None:
        # 4087 raw nodes fit; tagged envelopes/escaping may still exceed bytes.
        assert obs["delivery_complete"] is None
        assert any("receipt" in e.lower() and ("byte" in e.lower() or "65536" in e) for e in obs["errors"])
    else:
        assert len(_decode(obs["receipts"])) == 454
        encoded = json.dumps(obs["receipts"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(encoded) <= 65536
        assert obs["delivery_complete"] is True
    assert obs["complete"] is False and report["passed"] is False


@pytest.mark.parametrize("action", ["x" * 40000, "\\" * 17000], ids=["aggregate-raw-strings", "actual-escaped-tagged-bytes"])
def test_real_receipt_byte_refusal_preserves_engine_dispatch(native, action):
    project, run = native
    spec = _case("no_input")
    spec["scenarios"][0]["timeline"].insert(0, {"at": 20, "press": action})
    report, probe = run(_raw_assert(spec))
    obs = _observation(report)
    assert probe["frames"] == 89
    _position(report, 0)
    _assert_dispatch(report, 1, 2, 1)
    _assert_invalid(report, 1, 2)
    assert obs["receipts"] is None and obs["delivery_complete"] is None
    assert any("receipt" in e.lower() and "byte" in e.lower() for e in obs["errors"])


@pytest.mark.parametrize("body,admitted,needle,bytes_expected", [
    ("boundary_value = 0\nfor _i in range(8):\n    boundary_value = [boundary_value]", True, "", None),
    ("boundary_value = 0\nfor _i in range(9):\n    boundary_value = [boundary_value]", False, "depth", None),
    ("boundary_value = []\nboundary_value.resize(4095)", True, "", None),
    ("boundary_value = []\nboundary_value.resize(4096)", False, "4096", None),
    ('boundary_value = "x".repeat(65534)', True, "", 65536),
    ('boundary_value = "x".repeat(65535)', False, "byte", None),
    ('boundary_value = String.chr(92).repeat(32767)', True, "", 65536),
    ('boundary_value = String.chr(92).repeat(32767) + "x"', False, "byte", None),
    ('boundary_value = String.chr(0x1F600).repeat(16383) + "xx"', True, "", 65536),
    ('boundary_value = String.chr(0x1F600).repeat(16383) + "xxx"', False, "byte", None),
], ids=["depth8", "depth9", "nodes4096", "nodes4097", "bytes65536", "bytes65537", "escaped65536", "escaped65537", "utf8-65536", "utf8-65537"])
def test_real_existing_observer_boundaries_through_normal_assertions(native, body, admitted, needle, bytes_expected):
    project, run = native
    _value(project, body)
    spec = _case("no_input")
    spec["scenarios"][0].pop("input_observation")
    spec["scenarios"][0]["timeline"].append({"at": 59, "assert": [
        {"name": "boundary", "node": "Player", "expr": "true", "attr": "boundary_value"}
    ]})
    report, _ = run(spec)
    row = _scenario(report)
    a = next(a for a in row["asserts"] if a["name"] == "boundary")
    assert row["complete"] is admitted and report["passed"] is admitted
    if admitted:
        assert a["measurement"] == "ok" and a["passed"] is True
        if bytes_expected is not None:
            encoded = json.dumps(a["observed"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            assert len(encoded) == bytes_expected
    else:
        assert a["measurement"] == "incomplete" and a["observed"] is None
        assert needle in a["observed_error"].lower()
        assert row["incomplete_asserts"] == 1 and report["spec_errors"]


@pytest.mark.parametrize("body,needle", [
    ("boundary_value = 0\nfor _i in range(9):\n    boundary_value = [boundary_value]", "depth"),
    ("boundary_value = []\nboundary_value.resize(4096)", "4096"),
    ("boundary_value = String.chr(92).repeat(32767)", "byte"),
])
def test_real_terminal_witness_reuses_observer_refusals(native, body, needle):
    project, run = native
    _value(project, body, witness=True)
    report, probe = run(_raw_assert(_case()))
    assert probe["frames"] == 89
    _position(report, -2)
    _assert_dispatch(report, 3, 6, 3)
    _assert_invalid(report)
    obs = _observation(report)
    assert obs["witness"] is None
    assert any("witness" in e.lower() and needle in e.lower() for e in obs["errors"])


def test_real_original_path_failure_remains_first_when_witness_is_missing(native):
    project, run = native
    spec = _raw_assert(_case())
    spec["scenarios"][0]["input_observation"]["property"] = "nonexistent_property"
    spec["scenarios"][0]["timeline"].append({"at": 59, "assert": {"Missing/Player.grid_y": -2}})
    report, _ = run(spec)
    _position(report, -2)
    _assert_dispatch(report, 3, 6, 3)
    _assert_invalid(report)
    assert "Missing/Player" in report["spec_errors"][0]
    assert "path" in report["spec_errors"][0]
    assert len(report["spec_errors"]) >= 2


def test_real_false_authored_assertion_remains_advisory(native):
    project, run = native
    spec = _raw_assert(_case("runtime3_actualgame3accepted"))
    spec["scenarios"][0]["timeline"][3]["assert"]["Player.grid_y"] = 99
    report, _ = run(spec)
    _position(report, -3)
    _assert_dispatch(report, 3, 6, 3)
    assert _scenario(report)["passed"] is False
    assert report["passed"] is True and report["spec_errors"] == []
    assert _observation(report)["acceptance_pass"] is True


@pytest.mark.parametrize("name", ["runtime3_actualgame2accepted", "witness_only"])
def test_real_report_survives_normal_consumer_serialization_and_summary(native, monkeypatch, tmp_path, name):
    import aitelier.tools.godot_playtest.impl as pt
    project, run = native
    spec = _raw_assert(_case(name))
    native_report, _ = run(spec)
    retained = json.loads(json.dumps(native_report))
    (project / "playtest_spec.yaml").write_text(json.dumps(spec), encoding="utf-8")
    sent = []

    def replay(payload, timeout=0):
        sent.append(copy.deepcopy(payload))
        return copy.deepcopy(retained)

    # A real native report tests the ordinary consumer. This replay is NOT a
    # second native run and cannot replace the producer assertions above.
    monkeypatch.setattr(pt, "post_playtest", replay)
    output = tmp_path / "consumer"
    result = pt.godot_playtest(project_root=str(project), out_dir=str(output))
    saved = json.loads((output / "playtest_report.json").read_text())
    assert sent[0]["spec"] == spec
    assert _observation(saved) == _observation(retained)
    assert saved["spec_errors"] == retained["spec_errors"]
    assert saved["errors"] == retained["errors"]
    assert result["passed"] is retained["passed"]
    assert saved["passed"] is retained["passed"]
    assert retained["summary"] in saved["summary"]
    summary = saved["summary"].lower()
    assert "accept" in summary
    if name == "runtime3_actualgame2accepted":
        assert re.search(r"(?:accept\w*[^.;|]*2|2[^.;|]*accept)", summary)
        assert re.search(r"(?:reject\w*[^.;|]*1|1[^.;|]*reject)", summary)
        assert _observation(saved)["acceptance_pass"] is False
    else:
        assert _scenario(saved)["input_dead"] is True
        assert saved["passed"] is False and _observation(saved)["acceptance_pass"] is False
