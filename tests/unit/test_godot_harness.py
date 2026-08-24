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


def test_scenario_may_override_the_boot_scene(monkeypatch, tmp_path):
    """A scenario naming its own `scene:` boots THAT scene; others keep the
    spec-level one.

    Every scenario already runs in its own fresh Godot process, and run_godot has
    always accepted a scene argument -- but only the spec-level scene was ever
    passed, so all 27 scenarios booted main.tscn and each paid the full boot
    preamble before it could assert anything about a later screen. This asserts
    on the value each probe RECEIVES, not merely that the key is readable: the
    old shape read the override from nowhere and silently used main.tscn, which
    is indistinguishable from a passing test until you check which scene
    actually rendered.
    """
    seen = []

    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None):
        seen.append(scene)
        return ({"frames": 5, "asserts": [{"name": "a", "passed": True}], "nodes": {}},
                [], False)

    monkeypatch.setattr(gh, "_run_probe", fake)
    spec = {"scene": "res://scenes/main.tscn", "scenarios": [
        {"name": "whole_game", "timeline": [
            {"at": 5, "assert": [{"node": "N", "expr": "x > 0"}]}]},
        {"name": "just_creation", "scene": "res://scenes/segments/creation.tscn",
         "timeline": [{"at": 5, "assert": [{"node": "N", "expr": "x > 0"}]}]},
    ]}
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert seen == ["res://scenes/main.tscn",
                    "res://scenes/segments/creation.tscn"]


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


def test_normalize_asserts_dict_form():
    # The ergonomic dict form authored by the DPE agents → the probe's {node,expr}.
    out = gh._normalize_asserts({
        "GameManager.paused": True,
        "HUD/PausedLabel.visible": True,       # node name contains '/', attr after 1st dot
        "Bird.velocity.y": "velocity.y != 0",  # comparison string → expr verbatim
        "GameManager.state": 0,                # number → equality
        "GameManager.score": "score == 0",
    })
    by = {a["name"]: a for a in out}
    assert by["GameManager.paused"]["node"] == "GameManager"
    assert by["GameManager.paused"]["expr"] == "paused == true"
    assert by["HUD/PausedLabel.visible"]["node"] == "HUD/PausedLabel"
    assert by["HUD/PausedLabel.visible"]["expr"] == "visible == true"
    assert by["Bird.velocity.y"] == {"node": "Bird", "attr": "velocity.y",
                                     "expr": "velocity.y != 0", "name": "Bird.velocity.y"}
    assert by["GameManager.state"]["expr"] == "state == 0"
    assert by["GameManager.score"]["expr"] == "score == 0"


def test_normalize_asserts_string_literal_equality():
    # A plain string (no comparison operator) → string-literal equality.
    out = gh._normalize_asserts({"HUD/MessageLabel.text": "Game Over"})
    assert out[0]["node"] == "HUD/MessageLabel"
    assert out[0]["expr"] == 'text == "Game Over"'


def test_normalize_asserts_list_passthrough():
    lst = [{"node": "Bird", "expr": "velocity.y < 0"}]
    assert gh._normalize_asserts(lst) is lst


def test_normalize_timeline_only_touches_assert_entries():
    tl = [{"at": 0, "press": "flap"},
          {"at": 5, "assert": {"Bird.velocity.y": "velocity.y < 0"}}]
    out, errors = gh._normalize_timeline(tl)
    assert errors == []
    assert out[0] == {"at": 0, "press": "flap"}
    assert out[1]["assert"] == [{"node": "Bird", "attr": "velocity.y",
                                "expr": "velocity.y < 0", "name": "Bird.velocity.y"}]


def test_normalize_timeline_expands_actions_into_presses():
    # `actions:` is what every LLM-authored spec reaches for; it used to be
    # dropped on the floor because the probe only reads `press:`.
    out, errors = gh._normalize_timeline([{"at": 7, "actions": ["move_right", "skill_1"]}])
    assert errors == []
    assert out == [{"at": 7, "press": "move_right"}, {"at": 7, "press": "skill_1"}]


def test_normalize_timeline_keeps_the_assert_when_actions_share_the_frame():
    out, errors = gh._normalize_timeline(
        [{"at": 7, "actions": ["move_right"], "assert": {"Bird.alive": True}}])
    assert errors == []
    assert {"at": 7, "press": "move_right"} in out
    assert any("assert" in e for e in out)


def test_normalize_timeline_rejects_an_unknown_key():
    out, errors = gh._normalize_timeline([{"at": 3, "keys": ["ui_accept"]}])
    assert out == []
    assert len(errors) == 1 and "keys" in errors[0] and "at: 3" in errors[0]


def test_normalize_asserts_understands_changed_and_unchanged():
    out = gh._normalize_asserts({"Player.grid_pos": "changed",
                                 "Score.value": "UNCHANGED"})
    assert out[0] == {"node": "Player", "attr": "grid_pos",
                      "name": "Player.grid_pos", "mode": "changed"}
    assert out[1]["mode"] == "unchanged"


class _CP:
    def __init__(self, rc, err=""):
        self.returncode, self.stderr, self.stdout = rc, err, ""


def test_check_gdscript_passes_a_clean_file(monkeypatch, tmp_path):
    f = tmp_path / "ok.gd"
    f.write_text("extends Node\n")
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(0))
    r = gh.check_gdscript([str(f)])
    assert r["all_passed"] is True
    assert r["results"][0]["passed"] is True


def test_check_gdscript_fails_a_syntax_error(monkeypatch, tmp_path):
    f = tmp_path / "bad.gd"
    f.write_text("extends Node\n")
    err = ('SCRIPT ERROR: Parse Error: Expected statement, found "Indent" instead.\n'
           "          at: GDScript::reload (res://bad.gd:4)\n")
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(1, err))
    r = gh.check_gdscript([str(f)])
    assert r["all_passed"] is False
    assert "Expected statement" in r["results"][0]["error_message"]


def test_check_gdscript_ignores_missing_project_context(monkeypatch, tmp_path):
    # One file, no project.godot: autoloads, res:// paths and sibling classes are
    # all unresolvable. 17 of 21 files in a WORKING repo reported exactly these,
    # so treating them as defects would fail almost every task.
    f = tmp_path / "ai.gd"
    f.write_text("extends RefCounted\n")
    err = ('SCRIPT ERROR: Parse Error: Identifier "GridManager" not declared in '
           "the current scope.\n"
           'SCRIPT ERROR: Parse Error: Could not resolve super class path "res://a.gd".\n'
           'SCRIPT ERROR: Parse Error: Preload file "res://b.gd" does not exist.\n')
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(1, err))
    r = gh.check_gdscript([str(f)])
    assert r["all_passed"] is True


def test_check_gdscript_reports_only_the_real_error_when_mixed(monkeypatch, tmp_path):
    f = tmp_path / "mixed.gd"
    f.write_text("extends Node\n")
    err = ('SCRIPT ERROR: Parse Error: Identifier "GridManager" not declared in '
           "the current scope.\n"
           'SCRIPT ERROR: Parse Error: Unexpected "Indent" in class body.\n')
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(1, err))
    r = gh.check_gdscript([str(f)])
    assert r["all_passed"] is False
    msg = r["results"][0]["error_message"]
    assert "Unexpected" in msg and "GridManager" not in msg


def test_spec_frame_cap_is_declared_and_generous():
    # The cap only bites on scenarios longer than ~50s at 60fps; _playtest_spec
    # turns an assertion scheduled past it into a spec_error rather than letting
    # it silently vanish from the results.
    assert gh._MAX_SPEC_FRAMES == 3000


def test_digest_drops_the_probes_own_bookkeeping():
    nodes = {"/root/_AItelierProbe": {"vars": {"_frame": 400}},
             "/root/Main/Bird": {"vars": {"alive": True}}}
    assert gh._digest(nodes) == {"/root/Main/Bird": {"vars": {"alive": True}}}


def test_playtest_project_dispatches_on_spec(monkeypatch, tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    monkeypatch.setattr(gh, "_copy_project", lambda p: tmp_path / "proj" / "proj")
    (tmp_path / "proj" / "proj").mkdir(parents=True)
    monkeypatch.setattr(gh, "_inject_probe", lambda d: None)
    # Stubbed for the same reason as _copy_project: this test is about WHICH
    # play-test path runs, and the real one would shell out to Godot.
    monkeypatch.setattr(gh, "_import_resources", lambda d, t: None)
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
def test_real_playtest_spec_input_timeline(good_project):
    # The game reacts to a 'flap' action; a press at frame 0 must reach it.
    # Regression: frames are 0-based, so an `at: 0` press is not swallowed.
    (good_project / "main.gd").write_text(
        "extends Node\nvar lift := 0.0\n"
        "func _process(_d):\n"
        "\tif Input.is_action_pressed('flap'):\n\t\tlift = -1.0\n")
    spec = {"scenarios": [{"name": "flap", "timeline": [
        {"at": 0, "press": "flap"},
        {"at": 5, "assert": [{"node": "Main", "expr": "lift < 0"}]}]}]}
    r = gh.playtest_project(str(good_project), frames=10, spec=spec)
    assert r["passed"] is True
    assert r["behavior"]["scenarios"][0]["passed"] is True


@requires_godot
def test_real_playtest_spec_dict_assert_form(good_project):
    # The ergonomic dict form (what the DPE agents actually author) must evaluate
    # identically to the list form end-to-end.
    spec = {"scenarios": [{"name": "score", "timeline": [
        {"at": 3, "assert": {"Main.score": "score >= 1"}}]}]}
    r = gh.playtest_project(str(good_project), frames=6, spec=spec)
    assert r["passed"] is True
    assert r["behavior"]["scenarios"][0]["passed"] is True


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


# A hanging GDScript suite is the case where the output matters MOST: the
# SceneTree spins forever precisely because a runtime error aborted the function
# holding the final quit(), and that error — plus every PASS/FAIL printed before
# it — is already in the buffer when the wall-clock kill lands.

def test_script_timeout_keeps_the_output_the_run_already_produced(monkeypatch, tmp_path):
    """The timeout branch used to report `out=""`, so the report said only "it
    hung" about a run that had already said where and why."""
    import subprocess
    (tmp_path / "project.godot").write_text("[application]\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_fsm.gd").write_text("extends SceneTree\n")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(
            cmd="godot", timeout=140,
            output="PASS test_a\nPASS test_b\n",
            stderr="SCRIPT ERROR: Invalid access to property 'state_changed'\n"
                   "          at: _run (res://tests/t_fsm.gd:46)\n")

    # run_script ends with `shutil.rmtree(dst.parent)`. Returning `proj` itself
    # from the _copy_project stub therefore deletes tmp_path.PARENT — the whole
    # pytest-of-<user>/pytest-N run directory — and every later test in the
    # session dies with FileNotFoundError on it. (Done exactly that way in
    # 62a5a27; 558 errors on the next full-suite run.) Hand back a nested dir so
    # the cleanup stays inside tmp_path, and stub rmtree as well, the same
    # belt-and-braces the playtest_project tests above use.
    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    monkeypatch.setattr(gh.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(gh, "_run", _boom)

    r = gh.run_script(str(tmp_path), [], timeout=140)
    assert r["passed"] is False
    res = r["results"][0]
    assert res["returncode"] == 124
    assert "PASS test_a" in res["stdout"]                 # the run's own account
    assert "t_fsm.gd:46" in res["stderr"]                 # ...and where it died
    assert "timed out after 140s" in res["stderr"]        # ...without losing why


def test_normalize_carries_attr_so_a_failed_assert_can_report_the_value():
    """Every normalised assert must carry `attr`, not just `expr`.

    The probe uses it to read the value back when a comparison fails. Without
    it a failing `turns_taken == 1` reports `actual: false` and nothing else —
    the report says the assert did not hold but not what was there instead, and
    the next implementer has to re-derive runtime behaviour from the source.
    jinyong-usable 2026-08-23 spent a task card doing exactly that, and got it
    wrong.
    """
    out = gh._normalize_asserts({
        "East_Heretic.turns_taken": 1,                        # number form
        "CombatManager.phase": "IDLE",                        # string form
        "CombatManager.current_round": "current_round >= 4",  # expression form
        "HUD/PausedLabel.visible": True,                      # bool + path node
    })
    by_name = {a["name"]: a for a in out}
    assert by_name["East_Heretic.turns_taken"]["attr"] == "turns_taken"
    assert by_name["CombatManager.phase"]["attr"] == "phase"
    assert by_name["CombatManager.current_round"]["attr"] == "current_round"
    assert by_name["HUD/PausedLabel.visible"]["node"] == "HUD/PausedLabel"
    assert by_name["HUD/PausedLabel.visible"]["attr"] == "visible"
    # The expression form is still passed through verbatim.
    assert by_name["CombatManager.current_round"]["expr"] == "current_round >= 4"


def test_probe_reads_the_value_back_only_when_the_assert_failed():
    """The capture is on the failure path only — a passing assert stays small."""
    src = gh.PROBE_GD if hasattr(gh, "PROBE_GD") else Path(gh.__file__).read_text(
        encoding="utf-8")
    assert 'if not res["passed"] and a.has("attr"):' in src
    assert 'res["observed"] = _jsonable(_read_attr(target, str(a["attr"])))' in src
