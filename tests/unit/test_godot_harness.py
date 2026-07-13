"""Tests for the godot-builder harness brain (docker/godot/godot_harness.py).

The stderr parser is tested against canned Godot output (no Godot needed). A
real end-to-end compile/playtest runs only when a Godot binary is available
(GODOT_BIN set or `godot` on PATH), otherwise it is skipped.
"""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)


# ── stderr parser (Godot-free) ─────────────────────────────────────────────
def test_parse_ignores_engine_noise():
    # The editor's progress_dialog + "Condition ... is true" lines are internal
    # noise, never user diagnostics.
    stderr = (
        'ERROR: Condition "!tasks.has(p_task)" is true. Returning: canceled\n'
        "   at: task_step (editor/progress_dialog.cpp:217)\n"
    )
    assert gh._parse_errors(stderr) == []


def test_parse_gdscript_parse_error_with_location():
    stderr = (
        'SCRIPT ERROR: Parse Error: Identifier "foo" not declared in the current scope.\n'
        "          at: GDScript::reload (res://bird.gd:42)\n"
    )
    errs = gh._parse_errors(stderr)
    assert len(errs) == 1
    assert errs[0]["kind"] == "parse"
    assert errs[0]["file"] == "res://bird.gd"
    assert errs[0]["line"] == 42


def test_parse_runtime_null_call_with_location():
    stderr = (
        "SCRIPT ERROR: Cannot call method 'set_name' on a null value.\n"
        "          at: _process (res://main.gd:10)\n"
    )
    errs = gh._parse_errors(stderr)
    assert errs[0]["kind"] == "runtime"
    assert errs[0]["file"] == "res://main.gd"
    assert errs[0]["line"] == 10


def test_parse_user_push_error_kept_engine_error_dropped():
    stderr = (
        "ERROR: deliberate game error\n"
        "   at: push_error (core/variant/variant_utility.cpp:1098)\n"
        'ERROR: Condition "x" is true.\n'
        "   at: something (core/object.cpp:1)\n"
    )
    errs = gh._parse_errors(stderr)
    assert len(errs) == 1
    assert errs[0]["kind"] == "push_error"
    assert errs[0]["msg"] == "deliberate game error"


def test_parse_failed_load():
    stderr = 'ERROR: Failed to load script "res://main.gd" with error "Parse error".\n'
    errs = gh._parse_errors(stderr)
    assert errs[0]["kind"] == "load"


# ── spec-driven aggregation (Godot-free: _run_probe mocked) ────────────────
# The hard/advisory gate split lives in _playtest_spec; test it without Godot by
# faking each probe run's (probe_report, errors, timed_out).
def _mock_run_probe(monkeypatch, probe, errs, timed_out):
    monkeypatch.setattr(gh, "_run_probe",
                        lambda *a, **k: (probe, errs, timed_out))


def test_playtest_spec_all_assertions_pass(monkeypatch, tmp_path):
    _mock_run_probe(monkeypatch,
                    {"frames": 50, "asserts": [{"name": "a", "passed": True}], "nodes": {}},
                    [], False)
    spec = {"scenarios": [{"name": "flap", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "expr": "velocity.y < 0"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True and r["spec_used"] is True
    assert r["behavior"]["all_passed"] is True


def test_playtest_spec_failed_assertion_is_advisory(monkeypatch, tmp_path):
    # Game ran clean but the assertion is false → HARD passed stays True, behaviour
    # False. A wrong/flaky spec must never stall an otherwise-clean build.
    _mock_run_probe(monkeypatch,
                    {"frames": 50, "asserts": [{"name": "a", "passed": False, "actual": 5.0}], "nodes": {}},
                    [], False)
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "expr": "velocity.y < 0"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True                      # advisory, not hard fail
    assert r["behavior"]["all_passed"] is False


def test_playtest_spec_runtime_error_is_hard_fail(monkeypatch, tmp_path):
    _mock_run_probe(monkeypatch,
                    {"frames": 3, "asserts": [], "nodes": {}},
                    [{"kind": "runtime", "msg": "boom", "file": "res://x.gd", "line": 1}], False)
    spec = {"scenarios": [{"name": "s", "timeline": [{"at": 8, "assert": []}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False                     # crash → hard fail (loops)
    assert any(e["scenario"] == "s" for e in r["errors"])


def test_playtest_spec_didnt_run_is_hard_fail(monkeypatch, tmp_path):
    _mock_run_probe(monkeypatch, {}, [], True)      # timeout, no probe snapshot
    spec = {"scenarios": [{"name": "s", "timeline": [{"at": 8}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False


def test_playtest_project_dispatches_on_spec(monkeypatch, tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    monkeypatch.setattr(gh, "_copy_project", lambda p: tmp_path / "proj" / "proj")
    (tmp_path / "proj" / "proj").mkdir(parents=True)
    monkeypatch.setattr(gh, "_inject_probe", lambda d: None)
    monkeypatch.setattr(gh.shutil, "rmtree", lambda *a, **k: None)
    called = {}
    monkeypatch.setattr(gh, "_playtest_spec", lambda *a, **k: called.setdefault("spec", True) or {"passed": True})
    monkeypatch.setattr(gh, "_playtest_legacy", lambda *a, **k: called.setdefault("legacy", True) or {"passed": True})
    gh.playtest_project(str(tmp_path), spec={"scenarios": [{"name": "s", "timeline": []}]})
    assert called == {"spec": True}
    called.clear()
    gh.playtest_project(str(tmp_path), spec=None)
    assert called == {"legacy": True}


# ── real Godot (skipped if no binary) ──────────────────────────────────────
_GODOT = os.environ.get("GODOT_BIN") or shutil.which("godot")
requires_godot = pytest.mark.skipif(not _GODOT, reason="no Godot binary (set GODOT_BIN)")


@pytest.fixture
def good_project(tmp_path):
    (tmp_path / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="t"\nrun/main_scene="res://main.tscn"\n[autoload]\n')
    (tmp_path / "main.gd").write_text(
        "extends Node\nvar score := 0\nfunc _process(_d):\n\tscore += 1\n")
    (tmp_path / "main.tscn").write_text(
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://main.gd" id="1"]\n'
        '[node name="Main" type="Node"]\nscript = ExtResource("1")\n')
    return tmp_path


@requires_godot
def test_real_compile_pass(good_project):
    r = gh.compile_project(str(good_project))
    assert r["passed"] is True
    assert r["file_count"] == 1


@requires_godot
def test_real_compile_catches_parse_error(good_project):
    (good_project / "main.gd").write_text(
        "extends Node\nfunc _process(_d):\n\tundefined_function_xyz()\n")
    r = gh.compile_project(str(good_project))
    assert r["passed"] is False
    assert any(e["file"] == "res://main.gd" for e in r["errors"])


@requires_godot
def test_real_playtest_dumps_state(good_project, monkeypatch):
    monkeypatch.setenv("GODOT_PLAYTEST_FRAMES", "5")
    r = gh.playtest_project(str(good_project), frames=5)
    assert r["passed"] is True
    # The probe snapshotted the live script variable `score` off /root/Main.
    main = next((v for k, v in r["state"].items() if k.endswith("/Main")), None)
    assert main is not None and "score" in main["vars"]
    assert main["vars"]["score"] >= 1


@requires_godot
def test_real_playtest_catches_runtime_error(good_project):
    (good_project / "main.gd").write_text(
        "extends Node\nfunc _process(_d):\n\tvar n: Node = null\n\tn.set_name('x')\n")
    r = gh.playtest_project(str(good_project), frames=5)
    assert r["passed"] is False
    assert any(e["kind"] == "runtime" for e in r["errors"])


@requires_godot
def test_real_playtest_spec_evaluates_assertions(good_project):
    # main.gd increments `score` each frame. A true and an impossible assertion
    # exercise the live Expression evaluator end-to-end: both scenarios RUN clean
    # (hard passed True), but only the satisfiable one passes its assertion.
    spec = {"scenarios": [
        {"name": "score rises", "timeline": [
            {"at": 3, "assert": [{"node": "Main", "expr": "score >= 1"}]}]},
        {"name": "impossible", "timeline": [
            {"at": 3, "assert": [{"node": "Main", "expr": "score >= 999"}]}]},
    ]}
    r = gh.playtest_project(str(good_project), frames=6, spec=spec)
    assert r["passed"] is True and r["spec_used"] is True     # ran clean (hard)
    scen = {s["name"]: s for s in r["behavior"]["scenarios"]}
    assert scen["score rises"]["passed"] is True
    assert scen["impossible"]["passed"] is False
    assert r["behavior"]["all_passed"] is False


@requires_godot
def test_real_playtest_spec_reports_bad_node(good_project):
    # An assertion against a node that doesn't exist → error recorded, advisory
    # (the run itself is clean, so hard passed stays True).
    spec = {"scenarios": [{"name": "typo", "timeline": [
        {"at": 3, "assert": [{"node": "Nonexistent", "expr": "score >= 1"}]}]}]}
    r = gh.playtest_project(str(good_project), frames=6, spec=spec)
    assert r["passed"] is True
    a = r["behavior"]["scenarios"][0]["asserts"][0]
    assert a["passed"] is False and "not found" in a["error"]
