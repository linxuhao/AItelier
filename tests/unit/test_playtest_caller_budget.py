"""Actual urllib payloads and native admission/planning, with only probe execution faked."""
import copy
import importlib.util
import io
import json
import math
from pathlib import Path

import pytest
import yaml

from aitelier.tools.godot_playtest import impl as pt
from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario

HARNESS = Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"
_binding = importlib.util.spec_from_file_location("gh_caller_budget", HARNESS)
native = importlib.util.module_from_spec(_binding)
_binding.loader.exec_module(native)


def contract():
    return {"scene": "res://main.tscn", "scenarios": [
        {"name": "long", "execution_budget": {"max_frames": 6030, "timeout_seconds": 180},
         "timeline": [{"at": 1, "actions": ["advance"]},
                      {"at": 6000, "assert": {"N.x": "x == 1"}}]},
        {"name": "short", "timeline": [{"at": 10, "assert": {"N.x": "x == 1"}}]},
    ]}


def project(tmp_path, spec, layout="split"):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("[application]\n")
    if spec is None:
        return repo
    if layout == "monolith":
        (repo / pt.SPEC_FILE).write_text(yaml.safe_dump(spec))
    else:
        folder = repo / pt.SPEC_DIR
        folder.mkdir()
        header = {k: v for k, v in spec.items() if k != "scenarios"}
        header["scenario_order"] = [s["name"] for s in spec["scenarios"]]
        (folder / "_common.yaml").write_text(yaml.safe_dump(header))
        for scenario in spec["scenarios"]:
            (folder / (scenario["name"] + ".yaml")).write_text(yaml.safe_dump(scenario))
    return repo


@pytest.fixture
def bridge(monkeypatch):
    observed = {"requests": [], "reports": [], "probes": [], "effects": [],
                "mode": "responsive"}

    def forbidden(*args, **kwargs):
        observed["effects"].append("copy/import")
        pytest.fail("native refused work reached copy/import")

    monkeypatch.setattr(native, "_copy_project", forbidden)
    monkeypatch.setattr(native, "_import_resources", forbidden)

    def probe(dst, state_path, frames, timeout, extra, **kwargs):
        data = json.loads(Path(extra["AITELIER_PROBE_SPEC"]).read_text())
        observed["probes"].append((frames, timeout, copy.deepcopy(data)))
        driven = bool(data["timeline"])
        mode = observed["mode"]
        rows = [{**assertion, "frame": entry["at"], "passed": mode != "negative",
                 "measurement": "ok"}
                for entry in data["timeline"] for assertion in entry.get("assert", [])]
        usec = int(frames / 60 * 1000000)
        kwargs.get("timing", {}).update(game_usec=usec)
        timed_out = ((mode == "scenario_timeout" and driven)
                     or (mode == "control_timeout" and not driven))
        return {"frames": frames, "complete": True, "asserts": rows,
                "nodes": {"N": {"x": driven or mode == "input_dead"}},
                "timing": {"frames_stepped": frames, "game_usec": usec}}, [], timed_out

    monkeypatch.setattr(native, "_run_probe", probe)

    def urlopen(request, timeout):
        payload = json.loads(request.data)
        observed["requests"].append((payload, timeout))
        spec = payload.get("spec")
        frames, caller = payload.get("frames", 180), payload.get("timeout", 120)
        if spec:
            _, errors = native._authored_execution_plan(spec, frames, caller)
            if errors:
                # Real native preflight must refuse before copy/import or probe.
                report = native.playtest_project(payload["project_dir"], spec=spec,
                                                 frames=frames, timeout=caller)
            else:
                report = native._playtest_spec(Path(payload["project_dir"]) / "native",
                                               spec, frames, caller)
        else:
            report = {"passed": True, "spec_used": False, "summary": "legacy"}
        observed["reports"].append(copy.deepcopy(report))
        return io.BytesIO(json.dumps(report).encode())

    monkeypatch.setattr(pt.urllib.request, "urlopen", urlopen)
    return observed


@pytest.mark.parametrize("layout", ["split", "monolith"])
def test_ordinary_request_grants_180_without_rewriting_contract(tmp_path, bridge, layout):
    authored = contract()
    repo = project(tmp_path, authored, layout)
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"),
                               project_id="p", run_id="r", step_id="s", operation_id="o")
    report = bridge["reports"][0]
    assert result["passed"], report
    payload, socket_timeout = bridge["requests"][0]
    assert payload["spec"] == authored and payload["timeout"] == 180
    assert {k: payload[k] for k in ("project_id", "run_id", "operation_id")} == {
        "project_id": "p", "run_id": "r", "operation_id": "o"}
    assert socket_timeout == 3600 and payload["render_wait_timeout_sec"] == 1800
    assert payload["captures"] == pt._DPE_CAPTURES
    assert [(n, seconds) for n, seconds, _ in bridge["probes"]] == [
        (6030, 180), (180, 120), (6030, 180)]
    normal, errors = native._normalize_timeline(authored["scenarios"][0]["timeline"])
    assert not errors and bridge["probes"][0][2]["timeline"] == normal
    assert bridge["probes"][-1][2]["timeline"] == []
    assert not bridge["effects"] and not report["spec_errors"]
    loaded, _ = pt.read_spec(repo)
    assert loaded == authored


@pytest.mark.parametrize("spec", [None, {"scenarios": [contract()["scenarios"][1]]}])
def test_no_authored_budget_retains_120(tmp_path, bridge, spec):
    repo = project(tmp_path, spec)
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"))
    assert result["passed"], bridge["reports"]
    assert bridge["requests"][0][0].get("timeout", 120) == 120
    assert all(seconds == 120 for _, seconds, _ in bridge["probes"])


@pytest.mark.parametrize("selection,expected", [
    ("long", 180), ("short", 120), ("short,long", 180), ("inline", 180),
])
def test_scenario_wrapper_uses_only_selected_budgets(tmp_path, bridge, selection, expected):
    authored = contract()
    authored["scenarios"].append({"name": "unselected", "timeline": [],
                                  "execution_budget": {"max_frames": 3000, "timeout_seconds": 600}})
    repo = project(tmp_path, authored)
    options = ({"inline_scenario": yaml.safe_dump(authored["scenarios"][0])}
               if selection == "inline" else {"scenario": selection})
    result = godot_playtest_scenario(project_root=str(repo), operation_id="probe-owner", **options)
    assert result["hard_passed"], result
    payload, socket_timeout = bridge["requests"][0]
    names = ["long"] if selection == "inline" else selection.split(",")
    chosen, unknown = pt.select_scenarios(authored, names)
    assert not unknown and payload["spec"] == chosen
    assert payload["timeout"] == expected and socket_timeout == 900
    assert payload["operation_id"] == "probe-owner"


@pytest.mark.parametrize("budget", [
    None, {}, {"max_frames": 6030},
    {"max_frames": 6030, "timeout_seconds": 180, "extra": 1},
    {"max_frames": True, "timeout_seconds": 180},
    {"max_frames": 0, "timeout_seconds": 180},
    {"max_frames": (1 << 53), "timeout_seconds": 180},
    *[{"max_frames": 6030, "timeout_seconds": value}
      for value in (True, False, 0, -1, "180", math.inf, math.nan,
                    native._MAX_POLL_MILLISECONDS / 1000 + 1, 10 ** 400)],
])
def test_invalid_authored_budgets_still_refuse_before_effects(tmp_path, bridge, budget):
    authored = contract()
    authored["scenarios"][0]["execution_budget"] = budget
    repo = project(tmp_path, authored)
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"))
    assert not result["passed"]
    assert bridge["reports"][0]["spec_errors"]
    assert not bridge["probes"] and not bridge["effects"]
    assert bridge["requests"][0][0].get("timeout", 120) == 120


@pytest.mark.parametrize("mutation", ["omitted", "undersized", "floor", "bad_timeline"])
def test_carry_cannot_bypass_native_frame_or_timeline_admission(tmp_path, bridge, mutation):
    authored = contract()
    scenario = authored["scenarios"][0]
    if mutation == "omitted":
        scenario.pop("execution_budget")
    elif mutation == "undersized":
        scenario["execution_budget"]["max_frames"] = 6029
    elif mutation == "floor":
        authored["frames"] = 6031
    else:
        scenario["timeline"] = True
    repo = project(tmp_path, authored)
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"))
    assert not result["passed"] and bridge["reports"][0]["spec_errors"]
    assert not bridge["effects"] and not bridge["probes"]


@pytest.mark.parametrize("seconds,valid,admitted", [
    (180, True, True), (3600, True, True), (3601, True, False),
    (native._MAX_POLL_MILLISECONDS / 1000, True, False),
    (math.nextafter(native._MAX_POLL_MILLISECONDS / 1000, math.inf), False, False),
])
def test_carried_ceiling_respects_native_and_actual_transport_limits(
        tmp_path, bridge, seconds, valid, admitted):
    authored = contract()
    authored["scenarios"] = authored["scenarios"][:1]
    authored["scenarios"][0]["execution_budget"]["timeout_seconds"] = seconds
    repo = project(tmp_path, authored)
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"))
    assert native._valid_process_timeout(seconds) is valid
    assert result["passed"] is admitted, bridge["reports"]
    assert bridge["requests"][0][0].get("timeout", 120) == (min(seconds, 3600) if valid else 120)
    if not admitted:
        assert bridge["reports"][0]["spec_errors"]
        assert not bridge["effects"] and not bridge["probes"]


def test_actual_shorter_transport_refuses_an_otherwise_valid_scenario(tmp_path, bridge):
    repo = project(tmp_path, None)
    report = pt.post_playtest({"project_dir": str(repo), "spec": contract()}, timeout=150)
    assert not report["passed"] and report["spec_errors"]
    assert bridge["requests"][0][0]["timeout"] == 150
    assert bridge["requests"][0][1] == 150
    assert not bridge["effects"] and not bridge["probes"]


def test_explicit_request_cap_is_not_overridden(tmp_path, bridge):
    repo = project(tmp_path, None)
    report = pt.post_playtest({"project_dir": str(repo), "spec": contract(), "timeout": 120})
    assert not report["passed"] and any("<= caller timeout" in e for e in report["spec_errors"])
    assert bridge["requests"][0][0]["timeout"] == 120
    assert not bridge["effects"] and not bridge["probes"]


@pytest.mark.parametrize("mode,hard,behavior", [
    ("responsive", True, True), ("negative", True, False),
    ("input_dead", False, False), ("scenario_timeout", False, False),
    ("control_timeout", False, True),
])
def test_180_budget_preserves_useful_negative_and_input_dead_controls(
        tmp_path, bridge, mode, hard, behavior):
    authored = contract()
    authored["scenarios"] = authored["scenarios"][:1]
    repo = project(tmp_path, authored)
    bridge["mode"] = mode
    result = pt.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"))
    report = bridge["reports"][0]
    assert bridge["probes"], report
    assert result["passed"] is hard and report["behavior"]["all_passed"] is behavior
    row = report["behavior"]["scenarios"][0]
    assert row["input_dead"] is (mode == "input_dead")
    assert all(n == 6030 and seconds == 180 for n, seconds, _ in bridge["probes"])
    if mode in ("scenario_timeout", "control_timeout"):
        assert report["spec_errors"]
    if mode == "negative":
        assert row["asserts"] and not row["asserts"][0]["passed"]


def test_undeclared_budget_keeps_a_stricter_native_caller():
    spec = {"scenarios": [contract()["scenarios"][1]]}
    plans, errors = native._authored_execution_plan(spec, 180, 0.5)
    assert not errors and plans == [(180, 0.5)]
