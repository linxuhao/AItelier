"""Tests for the godot-builder harness brain (docker/godot/godot_harness.py).

The stderr parser is tested against canned Godot output (no Godot needed). A
real end-to-end compile/playtest runs only when a Godot binary is available
(GODOT_BIN set or `godot` on PATH), otherwise it is skipped.
"""

import importlib.util
import json
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
    def fake(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
             timing=None, render=True):
        # The real probe reports the game time its frames bought, and the
        # harness HARD-fails a run that stepped frames and reported none — a
        # stand-in that leaves it out is standing in for a broken engine, not
        # for this one. Report what a healthy fixed-delta run would.
        if timing is not None:
            timing["game_usec"] = int(probe.get("frames", 0) * 1_000_000 / 60)
        return probe, errs, timed_out

    monkeypatch.setattr(gh, "_run_probe", fake)


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

    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
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


def test_normalize_timeline_keeps_a_click_that_shares_a_frame_with_actions():
    """`click:` survives on an entry that also carries `actions:`.

    The tail of _normalize_timeline only emitted `base` when it carried a
    press/release/assert, so an entry with BOTH `actions:` and `click:` dropped
    the click on the floor. That is the same silent-skip this function exists to
    prevent -- every shipped `actions:` entry once vanished the same way and the
    scenarios still passed, because an input the spec asked for and the probe
    never delivered looks exactly like a game that ignored it.
    """
    out, errs = gh._normalize_timeline([
        {"at": 40, "actions": ["move_up"], "click": "MenuEntry0"},
    ])
    assert errs == []
    assert {"at": 40, "press": "move_up"} in out
    assert any(e.get("click") == "MenuEntry0" for e in out)


def test_normalize_timeline_expands_clicks_into_one_entry_each():
    """`clicks:` is the plural of `click:`, as `actions:` is of `press:`."""
    out, errs = gh._normalize_timeline([
        {"at": 12, "clicks": ["AttrPlus0", "ConfirmButton"]},
    ])
    assert errs == []
    assert [e["click"] for e in out if "click" in e] == ["AttrPlus0", "ConfirmButton"]


def test_normalize_timeline_expands_hovers_into_one_entry_each():
    """`hovers:` is the plural of `hover:`, as `clicks:` is of `click:`."""
    out, errs = gh._normalize_timeline([
        {"at": 12, "hovers": ["TraitToggle0", "TraitToggle5"]},
    ])
    assert errs == []
    assert [e["hover"] for e in out if "hover" in e] == [
        "TraitToggle0", "TraitToggle5"]


def test_normalize_timeline_keeps_a_hover_that_shares_a_frame_with_actions():
    """The `click` lesson applied to `hover`: an entry carrying both `actions:`
    and `hover:` must not drop the hover on the floor. A pointer move the spec
    asked for and the probe never sent is indistinguishable from a game that
    ignores hovering."""
    out, errs = gh._normalize_timeline([
        {"at": 7, "actions": ["ui_accept"], "hover": "TraitToggle3"},
    ])
    assert errs == []
    assert [e["press"] for e in out if "press" in e] == ["ui_accept"]
    assert [e["hover"] for e in out if "hover" in e] == ["TraitToggle3"]


def test_normalize_timeline_keeps_an_assert_that_shares_a_frame_with_hovers():
    out, errs = gh._normalize_timeline([
        {"at": 9, "hovers": ["TraitToggle1"],
         "assert": {"CreationScreen.trait_hover_index": 1}},
    ])
    assert errs == []
    assert [e["hover"] for e in out if "hover" in e] == ["TraitToggle1"]
    kept = [e for e in out if "assert" in e]
    assert len(kept) == 1
    assert kept[0]["assert"][0]["name"] == "CreationScreen.trait_hover_index"


def test_hover_and_click_are_both_accepted_timeline_keys():
    assert {"hover", "hovers"} <= gh._TIMELINE_KEYS
    out, errors = gh._normalize_timeline([{"at": 3, "hovers": ["X"]}])
    assert errors == []
    assert out


def test_probe_hover_sends_motion_and_no_button():
    """The GDScript half: `_hover` moves the pointer and presses NOTHING. That
    is the whole reason it exists — `clicks:` already implies a hover, so a
    hover-only affordance could be observed by a click but never told apart
    from what the click selected."""
    src = _HARNESS.read_text(encoding="utf-8")
    start = src.index("func _hover(spec: String) -> void:")
    end = src.index("\nfunc ", start + 1)
    body = src[start:end]
    assert "InputEventMouseMotion" in body
    assert "InputEventMouseButton" not in body
    assert "_point_of(" in body
    # a button token in a hover spec is refused, never silently ignored
    assert "push_error" in body and "hover takes no button" in body


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


def test_probe_reads_the_value_back_for_both_polarities():
    """The observed value is retained for passing AND failing asserts.

    The capture used to sit behind `if not res["passed"]`, so a green assert
    said nothing about what it had seen: a passing expression could not be
    audited (right value, or vacuous truth?) and the report threw away an
    observation the probe had already paid for. The condition now keys on the
    attr alone, so both polarities keep their observed value. The read is
    TAGGED, so a failed read is reported as an observed error instead of being
    smuggled in as a null value.
    """
    src = gh.PROBE_GD if hasattr(gh, "PROBE_GD") else Path(gh.__file__).read_text(
        encoding="utf-8")
    assert 'if a.has("attr"):' in src
    assert 'res["observed"] = obs["value"]' in src
    assert 'res["observed_error"] = obs["error"]' in src
    assert 'if not res["passed"] and a.has("attr"):' not in src



@pytest.mark.parametrize("times_out", [False, True])
def test_script_long_logs_keep_first_error_and_summary(monkeypatch, tmp_path, times_out):
    import subprocess

    (tmp_path / "project.godot").write_text("[application]\n")
    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    stdout = "FIRST TEST STARTED\n" + "progress noise\n" * 1000 + "FINAL TEST SUMMARY\n"
    stderr = ('SCRIPT ERROR: Parse Error: First diagnostic.\n'
              '          at: GDScript::reload (res://first.gd:7)\n'
              + "engine noise\n" * 500
              + 'SCRIPT ERROR: Parse Error: Middle diagnostic.\n'
                '          at: GDScript::reload (res://middle.gd:9)\n'
              + "engine noise\n" * 500 + "FINAL ERROR SUMMARY\n")

    def run(*args, **kwargs):
        if times_out:
            raise subprocess.TimeoutExpired("godot", 140, output=stdout.encode(),
                                            stderr=stderr.encode())
        return subprocess.CompletedProcess("godot", 1, stdout, stderr)

    monkeypatch.setattr(gh, "_run", run)
    result = gh.run_script(str(tmp_path), ["res://tests/run.gd"], timeout=140)["results"][0]
    assert result["passed"] is False
    assert result["stdout"].startswith("FIRST TEST STARTED")
    assert result["stdout"].endswith("FINAL TEST SUMMARY\n")
    assert "first.gd:7" in result["stderr"]
    assert "FINAL ERROR SUMMARY" in result["stderr"]
    for stream in ("stdout", "stderr"):
        assert result[stream + "_truncated"] is True
        assert "[middle truncated]" in result[stream]
        assert len(result[stream]) <= 4000
    # Error parsing must still see the unabridged stream, including a diagnostic
    # omitted from the display excerpt. The timeout suffix must also survive.
    assert "middle.gd:9" not in result["stderr"]
    assert any(e["file"] == "res://middle.gd" for e in result["errors"])
    if times_out:
        assert result["returncode"] == 124
        assert result["stderr"].endswith("timed out after 140s")


# ── per-invocation user:// isolation (script gate) ─────────────────────────
#
# Godot derives user:// from $HOME. The script gate ran every entry point with
# the container's HOME, so all of them — and every later request, since the
# sidecar container outlives one — shared one app_userdata/<project>/. A suite
# that saves therefore decided what the next suite booted into. That is the
# order-dependence the play-test already fixed per scenario; these tests hold
# the script gate to the same property, with subprocess stubbed (no Godot).

def _script_project(tmp_path, monkeypatch, entries=("t_a.gd", "t_b.gd")):
    """A project whose entry points are discovered, with the copy step stubbed.

    `_copy_project` hands back a dir NESTED in tmp_path so run_script's closing
    `rmtree(dst.parent)` stays inside it — returning the project itself would
    delete the whole pytest run directory (learned the hard way in 62a5a27).

    HOME is redirected for the same reason: these tests are meaningful only if
    they can be run against an UNFIXED harness, and an unfixed harness hands
    the run the ambient HOME — the developer's own, whose user:// the fake
    would then write its sentinel into.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "container_home"))
    (tmp_path / "container_home").mkdir()
    (tmp_path / "project.godot").write_text("[application]\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in entries:
        (tests / name).write_text("extends SceneTree\n")
    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    return work


def _record_homes(monkeypatch, seen, behaviour=None):
    """Stub `subprocess.run` — NOT `_run` — so the real env assembly is tested.

    Each fake invocation writes the save file a suite that saves would leave in
    user://, and records what it found there on entry: the sentinel is how a
    later invocation would betray that it inherited an earlier one's HOME.
    """
    import subprocess

    def fake_run(cmd, **kw):
        env = kw["env"]
        home = Path(env["HOME"])
        seen.append({"home": home,
                     "found": sorted(p.name for p in home.iterdir()),
                     "token": env.get("AITELIER_HARNESS_TOKEN"),
                     "existed": home.is_dir()})
        (home / "save_1.json").write_text('{"gold": 1}')
        if behaviour is not None:
            return behaviour(cmd)
        return subprocess.CompletedProcess(cmd, 0, "PASS all\n", "")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    return seen


def test_each_script_entry_point_runs_in_its_own_home(monkeypatch, tmp_path):
    _script_project(tmp_path, monkeypatch)
    container_home = tmp_path / "container_home"
    monkeypatch.setenv("AITELIER_HARNESS_TOKEN", "inherited")
    seen = _record_homes(monkeypatch, [])

    r = gh.run_script(str(tmp_path), [], timeout=30)

    assert r["passed"] is True
    assert [x["script"] for x in r["results"]] == ["res://tests/t_a.gd",
                                                   "res://tests/t_b.gd"]
    assert len(seen) == 2
    first, second = seen
    # Distinct homes, neither of them the container's.
    assert first["home"] != second["home"]
    assert container_home not in (first["home"], second["home"])
    assert container_home not in first["home"].parents
    # The sentinel the first suite saved is invisible to the second: it did not
    # start in a directory anyone else had written to.
    assert first["found"] == [] and second["found"] == []
    assert (container_home / "save_1.json").exists() is False
    # Every other inherited variable still reaches the run.
    assert first["token"] == second["token"] == "inherited"
    # Nothing left behind on the happy path.
    assert not first["home"].exists() and not second["home"].exists()


def test_a_second_request_does_not_inherit_the_first_requests_home(monkeypatch, tmp_path):
    """The sidecar container outlives a request, so cross-REQUEST leakage was
    the same defect one call further out."""
    _script_project(tmp_path, monkeypatch, entries=("t_only.gd",))
    seen = _record_homes(monkeypatch, [])

    gh.run_script(str(tmp_path), [], timeout=30)
    gh.run_script(str(tmp_path), [], timeout=30)

    assert len(seen) == 2
    assert seen[0]["home"] != seen[1]["home"]
    assert seen[1]["found"] == []           # the first request's save is gone
    assert not seen[0]["home"].exists()


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_a_red_script_still_reports_and_still_cleans_its_home(monkeypatch, tmp_path, outcome):
    """Cleanup must not cost the gate its evidence: a red run keeps its own
    account of the failure, and its home goes anyway."""
    import subprocess
    _script_project(tmp_path, monkeypatch, entries=("t_red.gd",))
    stdout = "PASS test_a\nFAILED: test_b\n"
    stderr = ('SCRIPT ERROR: Invalid access to property "hp"\n'
              "          at: _run (res://tests/t_red.gd:12)\n")

    def behaviour(cmd):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, 30, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(cmd, 1, stdout, stderr)

    seen = _record_homes(monkeypatch, [], behaviour)

    r = gh.run_script(str(tmp_path), [], timeout=30)

    res = r["results"][0]
    assert r["passed"] is False and res["passed"] is False
    assert res["returncode"] == (124 if outcome == "timeout" else 1)
    assert "FAILED: test_b" in res["stdout"]            # the run's own account
    assert "t_red.gd:12" in res["stderr"]               # ...and where it died
    assert any(e["file"] == "res://tests/t_red.gd" for e in res["errors"])
    if outcome == "timeout":
        assert res["stderr"].endswith("timed out after 30s")
    assert len(seen) == 1 and not seen[0]["home"].exists()


def test_an_unexpected_error_surfaces_and_leaves_no_home_behind(monkeypatch, tmp_path):
    """An error nobody predicted must still reach the caller — swallowing it
    here would turn a broken sidecar into a silent pass — and must not leak the
    home it was holding."""
    _script_project(tmp_path, monkeypatch, entries=("t_boom.gd",))

    def behaviour(cmd):
        raise OSError("cannot fork")

    seen = _record_homes(monkeypatch, [], behaviour)

    with pytest.raises(OSError, match="cannot fork"):
        gh.run_script(str(tmp_path), [], timeout=30)

    assert len(seen) == 1 and not seen[0]["home"].exists()


# ── the no-input CONTROL pass is a run too ──────────────────────────────────
#
# Each scenario got its own throwaway user:// (2026-09-04); the control pass
# that decides `input_dead` did not, so every control in every request shared
# the container's HOME. Measured on the wuxia tree 2026-09-05: the game's
# user:// logs appeared in the sidecar's shared home at the END of each
# play-test request — the control passes, and nothing else. A control that
# boots into what an earlier control saved is not a no-input BASELINE.

def _spec(names_pressed, at=5):
    """A spec whose scenarios all PRESS input, so each one earns a control pass."""
    return {"scenarios": [
        {"name": n, "timeline": [{"at": at, "press": "ui_accept"},
                                 {"at": at, "assert": [{"node": "N", "expr": "x > 0"}]}]}
        for n in names_pressed]}


def _home_recording_probe(monkeypatch, seen, control_nodes=None, on_control=None):
    """Record the HOME each probe run receives, and what it finds already there.

    The sentinel is the point: a control that can see the previous run's file is
    a control that inherited its user://.
    """
    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
        home = env.get("HOME")
        is_control = "AITELIER_PROBE_SPEC" in env and capture_at is None
        rec = {"home": home, "is_control": is_control, "existed": False, "found": []}
        if home:
            hp = Path(home)
            rec["existed"] = hp.is_dir()
            rec["found"] = sorted(p.name for p in hp.iterdir()) if hp.is_dir() else []
            (hp / "save_1.json").write_text("{}")
        seen.append(rec)
        # Same reason as in _mock_run_probe: a probe that steps frames and
        # reports no game time is a broken engine, and the harness says so.
        if timing is not None:
            timing["game_usec"] = int(frames * 1_000_000 / 60)
        if is_control:
            if on_control is not None:
                return on_control()
            return ({"frames": frames, "asserts": [], "nodes": control_nodes or {}}, [], False)
        return ({"frames": frames, "asserts": [{"name": "a", "passed": True}],
                 "nodes": {"Bird": {"vars": {"x": 1}}}}, [], False)

    monkeypatch.setattr(gh, "_run_probe", fake)
    return seen


def test_the_control_pass_runs_in_its_own_home_and_leaves_none_behind(monkeypatch, tmp_path):
    seen = _home_recording_probe(monkeypatch, [])
    r = gh._playtest_spec(tmp_path / "proj", _spec(["a", "b"]), 60, 120)

    controls = [s for s in seen if s["is_control"]]
    assert len(seen) == 3 and len(controls) == 1        # 2 scenarios + 1 control
    homes = [s["home"] for s in seen]
    assert all(h for h in homes), "every probe run must be handed a HOME"
    assert len(set(homes)) == 3                          # scenarios AND control differ
    assert all(s["existed"] and s["found"] == [] for s in seen)   # each one fresh
    assert not any(Path(h).exists() for h in homes)      # and all removed
    assert r["passed"] is True                           # nodes differ -> input alive


def test_a_second_request_does_not_hand_the_control_an_old_home(monkeypatch, tmp_path):
    seen = _home_recording_probe(monkeypatch, [])
    gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)
    gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)

    controls = [s for s in seen if s["is_control"]]
    assert len(controls) == 2
    assert controls[0]["home"] != controls[1]["home"]
    assert controls[1]["found"] == []                    # request 1 left nothing
    assert not Path(controls[0]["home"]).exists()


def test_a_dead_input_is_still_called_dead_from_a_fresh_control(monkeypatch, tmp_path):
    """Isolating the control must not soften the verdict it exists to deliver.

    The scenario presses a key and ends in EXACTLY the control's state; that is
    still a HARD failure, and the control that proved it still ran in its own
    home.
    """
    same = {"Bird": {"vars": {"x": 1}}}
    seen = _home_recording_probe(monkeypatch, [], control_nodes=same)
    r = gh._playtest_spec(tmp_path / "proj", _spec(["ghost_press"]), 60, 120)

    assert r["passed"] is False
    assert "input" in r["summary"] and "ghost_press" in r["summary"]
    assert r["behavior"]["scenarios"][0]["input_dead"] is True
    control = [s for s in seen if s["is_control"]][0]
    assert control["home"] and control["found"] == []
    assert not Path(control["home"]).exists()


def test_a_control_that_timed_out_accuses_nobody_and_still_cleans_up(monkeypatch, tmp_path):
    """No control evidence => no input_dead claim (the pre-existing rule), and
    the home goes anyway."""
    seen = _home_recording_probe(monkeypatch, [], on_control=lambda: ({}, [], True))
    r = gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)

    assert r["behavior"]["scenarios"][0]["input_dead"] is False
    assert r["passed"] is True
    control = [s for s in seen if s["is_control"]][0]
    assert not Path(control["home"]).exists()


def test_an_unexpected_control_error_surfaces_and_leaves_no_home(monkeypatch, tmp_path):
    def boom():
        raise OSError("cannot fork")

    seen = _home_recording_probe(monkeypatch, [], on_control=boom)
    with pytest.raises(OSError, match="cannot fork"):
        gh._playtest_spec(tmp_path / "proj", _spec(["a"]), 60, 120)

    control = [s for s in seen if s["is_control"]][0]
    assert control["home"] and not Path(control["home"]).exists()


# ── L0 (input-dead) blind spots, 2026-09-10 ────────────────────────────────
# L0 compares a driven scenario's end state with a no-input control run. Two
# ways it silently did not apply on the wuxia tree (98 of 172 scenarios):
#   * the control always booted the SPEC-level scene, so a scenario with its
#     own `scene:` was compared against a different node tree (never equal);
#   * "drove input" meant `press` only, so click-only scenarios never entered.
def _l0_probe_recorder(seen):
    """Fake _run_probe that records (scene, frames, has_timeline) per call and
    returns a state that depends on the scene and on whether input was driven,
    so a control on the wrong scene can never accidentally match."""
    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
        import json as _json
        spec = _json.loads(open(env["AITELIER_PROBE_SPEC"]).read())
        driven = bool(spec.get("timeline"))
        seen.append((scene, frames, driven))
        return ({"frames": frames, "asserts": [{"name": "a", "passed": True}],
                 "nodes": {"Root": {"scene": scene, "driven": driven}}},
                [], False)
    return fake


def test_the_control_run_boots_the_scenarios_own_scene(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(gh, "_run_probe", _l0_probe_recorder(seen))
    spec = {"scene": "res://scenes/main.tscn", "scenarios": [
        {"name": "menu_thing", "scene": "res://scenes/menu.tscn",
         "timeline": [{"at": 5, "press": "ui_accept"},
                      {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]},
    ]}
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    controls = [(sc, n) for sc, n, driven in seen if not driven]
    assert controls == [("res://scenes/menu.tscn", 300)], (
        "the no-input control must boot the scene the scenario booted; a "
        "control on main.tscn compares two different node trees and input_dead "
        "can never fire: %r" % (seen,))


def test_controls_are_shared_per_scene_and_frame_budget(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(gh, "_run_probe", _l0_probe_recorder(seen))
    press = [{"at": 5, "press": "ui_accept"}]
    spec = {"scene": "res://scenes/main.tscn", "scenarios": [
        {"name": "a_main", "timeline": press},
        {"name": "b_main_same_budget", "timeline": press},
        {"name": "c_menu", "scene": "res://scenes/menu.tscn", "timeline": press},
        {"name": "d_menu_longer", "scene": "res://scenes/menu.tscn",
         "timeline": [{"at": 400, "press": "ui_accept"}]},
    ]}
    gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    controls = sorted((sc, n) for sc, n, driven in seen if not driven)
    assert controls == [("res://scenes/main.tscn", 300),
                        ("res://scenes/menu.tscn", 300),
                        ("res://scenes/menu.tscn", 430)]


def test_a_click_only_scenario_enters_l0(monkeypatch, tmp_path):
    """`clicks:` is input the probe delivers; a scenario made only of clicks
    that ends in the no-input state tested nothing, exactly like a press."""
    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
        return ({"frames": frames, "asserts": [{"name": "a", "passed": True}],
                 "nodes": {"Root": {"x": 1}}}, [], False)   # identical every run
    monkeypatch.setattr(gh, "_run_probe", fake)
    spec = {"scenarios": [
        {"name": "clicks_only", "timeline": [
            {"at": 5, "clicks": ["Button"]},
            {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]},
        {"name": "hover_only", "timeline": [
            {"at": 5, "hovers": ["Button"]},
            {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]},
    ]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    dead = {s["name"] for s in r["behavior"]["scenarios"] if s["input_dead"]}
    assert dead == {"clicks_only", "hover_only"}
    assert r["passed"] is False


def test_every_input_key_the_probe_delivers_counts_as_driving(monkeypatch, tmp_path):
    """Derived from _TIMELINE_KEYS, not a second hand-written list: whatever
    the normaliser lets through besides `at`/`assert` is input."""
    input_keys = sorted(gh._TIMELINE_KEYS - {"at", "assert", "actions", "clicks", "hovers"})
    assert input_keys == ["click", "hover", "press", "release"]
    for k in input_keys:
        def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
            return ({"frames": frames, "asserts": [{"name": "a", "passed": True}],
                     "nodes": {"Root": {"x": 1}}}, [], False)
        monkeypatch.setattr(gh, "_run_probe", fake)
        spec = {"scenarios": [{"name": "s", "timeline": [
            {"at": 5, k: "ui_accept"},
            {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]}]}
        r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
        assert r["behavior"]["scenarios"][0]["input_dead"] is True, k


def test_a_scenario_with_no_input_at_all_is_not_judged_by_l0(monkeypatch, tmp_path):
    """`actions: []` plus asserts drives nothing -- there is no input whose
    arrival L0 could check, and no control run is spent on it."""
    seen = []
    monkeypatch.setattr(gh, "_run_probe", _l0_probe_recorder(seen))
    spec = {"scenarios": [{"name": "self_running_gate", "timeline": [
        {"at": 30, "actions": [], "assert": [{"node": "N", "expr": "x > 0"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert len(seen) == 1 and r["behavior"]["scenarios"][0]["input_dead"] is False


def test_input_dead_still_fires_on_a_scene_override_that_ignores_input(monkeypatch, tmp_path):
    """The comparison is not loosened: same scene, same budget, identical end
    state => input_dead, and a scenario whose state DID move stays alive."""
    def fake(dst, state_path, frames, timeout, env, scene="", capture_at=None, timing=None, render=True):
        import json as _json
        spec = _json.loads(open(env["AITELIER_PROBE_SPEC"]).read())
        driven = bool(spec.get("timeline"))
        moved = driven and scene == "res://scenes/map.tscn"
        return ({"frames": frames, "asserts": [{"name": "a", "passed": True}],
                 "nodes": {"Root": {"scene": scene, "moved": moved}}}, [], False)
    monkeypatch.setattr(gh, "_run_probe", fake)
    tl = [{"at": 5, "press": "ui_accept"},
          {"at": 10, "assert": [{"node": "N", "expr": "x > 0"}]}]
    spec = {"scene": "res://scenes/main.tscn", "scenarios": [
        {"name": "menu_ignores", "scene": "res://scenes/menu.tscn", "timeline": tl},
        {"name": "map_reacts", "scene": "res://scenes/map.tscn", "timeline": tl},
    ]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    by = {s["name"]: s["input_dead"] for s in r["behavior"]["scenarios"]}
    assert by == {"menu_ignores": True, "map_reacts": False}
    assert r["passed"] is False and "menu_ignores" in r["summary"]


# ── native assertion-value controls (real Godot; UNRUN by the source worker) ─
# Meaningful additions beyond the four original native cases: a String
# observation, a real null baseline, a parse error and a missing baseline, all
# driven through the live Expression evaluator and the probe's report shape.
# This is a TRACKED fixture/spec asset, not an invented mirror framework; the
# root-controlled native slot runs it. The source worker never invokes Godot.
_VALUE_CONTROLS = (Path(__file__).resolve().parent / "fixtures"
                   / "godot_assertion_value_controls")


@requires_godot
def test_real_playtest_assertion_value_controls():
    import json
    spec = json.loads((_VALUE_CONTROLS / "spec.json").read_text(encoding="utf-8"))
    r = gh.playtest_project(str(_VALUE_CONTROLS), frames=8, spec=spec)
    scen = {s["name"]: s for s in r["behavior"]["scenarios"]}

    # A String expression evaluates as a VALUE and its observed value is kept.
    s = scen["string_observation_positive"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"] == "ready"

    # A DIRECT String observation (expr == "label", not a comparison): the value
    # is observed and kept, and its truth is the non-empty String.
    s = scen["string_value_direct"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"] == "ready"
    assert s["asserts"][0]["actual"] == "ready"

    # The EMPTY String is measured, not vacuous: it is a value that decides
    # advisory-false, and it never crashes via bool(String).
    s = scen["empty_string_advisory_false"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is False
    assert s["asserts"][0]["observed"] == ""

    # An UNSUPPORTED observed value (a live Object and a Callable) and a delta on
    # one are explicit incomplete measurements, never str(v) that could compare
    # equal and pass. Color/Dictionary/Array are now safely representable, so the
    # false-green control deliberately uses a live Object/Callable instead.
    for name in ("unsupported_observed_attr", "unsupported_callable_attr",
                 "unsupported_delta"):
        row = scen[name]
        assert row["complete"] is False and row["incomplete_asserts"] == 1, name

    # A delta whose node did not exist at frame 0 but whose LATER read is valid
    # is a genuinely ABSENT frame-0 baseline -- reported as baseline_missing, not
    # as a current-read error.
    s = scen["absent_frame0_baseline_later_valid"]
    assert s["complete"] is False and s["incomplete_asserts"] == 1
    assert s["asserts"][0]["actual"]["baseline_missing"] is True
    assert "current_error" not in s["asserts"][0]["actual"]

    assert s["asserts"][0]["actual"]["current"] is not None

    # Numeric both polarities: one holds, one does not; both were measured.
    s = scen["numeric_both_polarities"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert [a["passed"] for a in s["asserts"]] == [True, False]
    assert all(a["observed"] is not None for a in s["asserts"])

    # A real null captured at frame 0 is a baseline, not a missing one.
    s = scen["legit_null_baseline"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True

    # Safely representable structured values carry their TYPE and key/value
    # distinctions: an unchanged Vector2i/Rect2/nested Array is a measured
    # complete observation, and a MOVED Vector2i/Color/Dictionary is a measured
    # `changed`. The tagged envelope keeps Vector2i(3,4) distinct from [3, 4].
    for name in ("vector2i_unchanged", "rect2_unchanged", "nested_array_unchanged"):
        row = scen[name]
        assert row["complete"] is True and row["incomplete_asserts"] == 0, name
        assert row["asserts"][0]["passed"] is True, name
    for name in ("vector2i_changed", "color_changed", "dictionary_value_changed"):
        row = scen[name]
        assert row["complete"] is True and row["incomplete_asserts"] == 0, name
        assert row["asserts"][0]["passed"] is True, name
    # Insertion order alone is NOT a value change: the same Dictionary entries in
    # a different order are an unchanged observation.
    s = scen["dictionary_reorder_is_not_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    # The tagged envelope survives into the report, so the type is observable.
    assert scen["vector2i_unchanged"]["asserts"][0]["actual"]["current"]["__t"] == "Vector2i"

    # A Float observation is exported LOSSLESSLY in the engine's SCIENTIFIC form
    # (`String.num_scientific`), so close and tiny finite values are DISTINCT
    # wire tokens and dictionary key sorting is total. Unchanged on 1.0 passes; a
    # single close finite step is a real `changed` and NOT an `unchanged`.
    s = scen["float_lossless_unchanged"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    cur = s["asserts"][0]["actual"]["current"]
    assert isinstance(cur, dict) and cur["__t"] == "Float"
    s = scen["float_close_step_is_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    close = s["asserts"][0]["actual"]["current"]
    assert isinstance(close, dict) and close["__t"] == "Float"
    assert close["__v"] != cur["__v"]
    # ...and the unchanged polarity on the moved value is a measured advisory
    # miss, never a vacuous green.
    s = scen["float_close_step_unchanged_is_false"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is False
    # Two close finite Float KEYS stay distinct under the canonical key sort:
    # reordering them alone is not a value change.
    s = scen["float_key_reorder_is_not_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True

    # TINY magnitude, subnormal, min-normal and huge finite doubles each keep
    # their exact value under the scientific token (a fixed-decimal formatter
    # would flatten the tiny ones to 0.000000). A 1e-20 -> 2e-20 step is a real
    # change; the unchanged polarity on it is a measured advisory miss.
    for name in ("tiny_float_unchanged", "subnormal_float_unchanged",
                 "minnormal_float_unchanged", "huge_float_unchanged"):
        row = scen[name]
        assert row["complete"] is True and row["incomplete_asserts"] == 0, name
        assert row["asserts"][0]["passed"] is True, name
        assert row["asserts"][0]["actual"]["current"]["__t"] == "Float", name
    s = scen["tiny_float_step_is_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["actual"]["baseline"]["__v"] != s["asserts"][0]["actual"]["current"]["__v"]
    s = scen["tiny_float_step_unchanged_is_false"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is False
    # +0.0 and -0.0 compare equal in ordinary Godot numeric equality: a raw sign
    # difference is NOT a legacy delta, so an `unchanged` on -0.0 passes.
    s = scen["signed_zero_is_not_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    # A TINY Float KEY change, an int->float TYPE change and a bool flip are each
    # a real value change per the actual engine, not Python number equality.
    for name in ("tiny_typed_key_change_is_a_change",
                 "int_to_float_type_change_is_a_change",
                 "bool_type_change_is_a_change"):
        row = scen[name]
        assert row["complete"] is True and row["incomplete_asserts"] == 0, name
        assert row["asserts"][0]["passed"] is True, name
    # Typed dictionary keys (int/bool/float) sort canonically and are stable; a
    # value flip under a stable key set is a change.
    s = scen["typed_keys_unchanged"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    s = scen["typed_keys_value_change_is_a_change"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True

    # The cap is on the ACTUAL compact JSON encoding of the finished
    # observation, so a value genuinely under the byte cap is ADMITTED (the
    # below-cap twins), while its over-cap twin is a HARD incomplete refusal --
    # never a truncation and never a fabricated null. This distinguishes a real
    # payload cap from an arbitrary character/scan-pattern estimate.
    for name in ("byte_budget_string_refused", "byte_budget_string_key_refused",
                 "byte_cap_emoji_refused", "byte_cap_escape_refused",
                 "byte_cap_float_tag_refused", "byte_cap_vector_refused"):
        row = scen[name]
        assert row["complete"] is False and row["incomplete_asserts"] == 1, name
    for name in ("byte_cap_string_below_admitted",
                 "byte_cap_string_key_below_admitted",
                 "byte_cap_emoji_below_admitted",
                 "byte_cap_escape_below_admitted",
                 "byte_cap_float_tag_below_admitted",
                 "byte_cap_vector_below_admitted"):
        row = scen[name]
    for name in ("byte_cap_string_below_admitted",
                 "byte_cap_string_key_below_admitted",
                 "byte_cap_emoji_below_admitted",
                 "byte_cap_escape_below_admitted",
                 "byte_cap_float_tag_below_admitted",
                 "byte_cap_vector_below_admitted"):
        row = scen[name]
        assert row["complete"] is True and row["incomplete_asserts"] == 0, name
        assert row["asserts"][0]["passed"] is True, name

    # An unrepresentable observed value is a HARD incomplete refusal, never a
    # stringification or a fabricated null: a Float that became non-finite
    # (inf/nan), a Color or Vector component carrying NaN, and a self-referential
    # (cyclic) container are each exactly one unmeasured assertion.
    for name in ("nonfinite_float_refused", "nonfinite_color_refused",
                 "nonfinite_vector_refused", "cycle_refused"):
        row = scen[name]
        assert row["complete"] is False and row["incomplete_asserts"] == 1, name




    # An unparseable expression and a missing frame-0 baseline are HARD
    # incomplete measurements: the run cannot read as a shorter success.
    assert r["passed"] is False
    for name in ("parse_error_expression", "missing_baseline"):
        row = scen[name]
        assert row["complete"] is False and row["incomplete_asserts"] == 1, name
    assert any("incomplete measurement" in e for e in r["spec_errors"])

    # SCIENTIFIC staging fidelity: the token cases above prove an UNCHANGED
    # observation keeps its wire token, which a staged ZERO would also satisfy
    # vacuously. These controls read the raw pre-encoder value back at frame 3 and
    # assert it is genuinely positive -- so an unchanged-zero green can never stand
    # in for a fidelity measurement. The admitted native batch showed the source
    # `5e-324` / `2.2250738585072014e-308` literals stage as 0.0 here, so the fixture
    # supplies these raw values from GENUINE IEEE-754 bit patterns decoded at
    # runtime (PackedByteArray.decode_double), shared with the delta fields; the
    # `subnormal_literal_staged_nonzero` ID is retained for evidence
    # continuity and now means "the fixture-supplied raw value is nonzero".
    s = scen["subnormal_literal_staged_nonzero"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"]["__t"] == "Float"
    assert s["asserts"][0]["observed"]["__v"] != "0"
    s = scen["ieee_subnormal_runtime_staged_nonzero"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"]["__v"] != "0"
    s = scen["minnormal_staged_nonzero"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"]["__v"] != "0"
    s = scen["huge_staged_finite_positive"]
    assert s["complete"] is True and s["incomplete_asserts"] == 0
    assert s["asserts"][0]["passed"] is True
    assert s["asserts"][0]["observed"]["__t"] == "Float"

# ── same-frame text/geometry observation (Godot-free halves) ───────────────
# The probe's collector is GDScript and runs inside the engine; these tests
# cover the Python half (_attach_pngs forwarding) with a real temp PNG, and the
# collector/refusal bookkeeping by reading the emitted source. The native pilot
# that drives the real built-in Controls is @requires_godot and UNRUN here.
import base64 as _b64


def _tiny_png(path):
    # A tiny opaque byte sequence; the harness only base64s it, so it need not be
    # a decodable image.
    data = b"\x89PNG\r\n\x1a\n" + bytes(range(24))
    path.write_bytes(data)
    return data


def test_attach_pngs_forwards_the_text_observation_verbatim(tmp_path):
    """The same-frame text/geometry observation rides home with its PNG.

    _attach_pngs used to keep ONLY frame/file/png_b64, stripping everything
    else the probe attached. A capture's observation was therefore thrown away
    before the caller could see it. The observation is now forwarded verbatim,
    and the exact PNG bytes still ride alongside it (PNG correspondence).
    """
    obs = {"frame": 0, "locale": "en", "controls": [
        {"path": "/root/TextRoot/VisibleIdLabel", "class": "Label",
         "source_text": "VISIBLE_TABLE_ID", "displayed_text": "VISIBLE_TABLE_ID",
         "complete": True}], "complete": True}
    raw = _tiny_png(tmp_path / "frame_0000.png")
    caps = [{"frame": 0, "file": "/container/local/frame_0000.png",
             "text_observation": obs}]
    out = gh._attach_pngs(caps, tmp_path)
    assert len(out) == 1 and out[0]["file"] == "frame_0000.png"
    assert out[0]["text_observation"] is obs
    assert out[0]["png_b64"] == _b64.b64encode(raw).decode()


def test_attach_pngs_reports_a_missing_observation_never_drops_it(tmp_path):
    """A capture row without an observation is an ABSENT observation, not a
    silently complete empty corpus: it is kept on the row with complete=false."""
    _tiny_png(tmp_path / "frame_0003.png")
    caps = [{"frame": 3, "file": "/container/local/frame_0003.png"}]
    out = gh._attach_pngs(caps, tmp_path)
    assert out[0]["frame"] == 3
    assert out[0]["text_observation"]["complete"] is False
    assert "no text observation" in out[0]["text_observation"]["error"]
    # The PNG still rode home; only the observation is reported absent.
    assert out[0].get("png_b64")


def test_attach_pngs_reports_a_missing_png_visibly(tmp_path):
    """A scheduled capture whose PNG is not on disk is an explicit png_error on
    the row; it is never presented as a successful capture of an empty frame."""
    caps = [{"frame": 2, "file": "/container/local/frame_0002.png",
             "text_observation": {"frame": 2, "complete": False}}]
    timing = {}
    out = gh._attach_pngs(caps, tmp_path, timing=timing)
    assert out[0]["png_error"] == "missing PNG file"
    assert "png_b64" not in out[0]
    assert timing["png_missing"] == ["frame_0002.png"]
    # The observation is still forwarded, so a missing PNG is distinguishable
    # from a missing observation.
    assert out[0]["text_observation"]["frame"] == 2


def test_observe_text_source_observes_supported_and_marks_unsupported():
    """Source-level contract of the emitted GDScript collector.

    The collector must: walk the live tree (so script-less Controls are seen),
    support the ordinary Label/Button text classes, mark the visibly
    text-bearing but unsupported classes explicitly, report the engine locale,
    and refuse a whole over-budget observation rather than truncating it. A
    source-census classifier must NOT gate what gets captured.
    """
    src = _HARNESS.read_text(encoding="utf-8")
    assert "func _observe_text(" in src
    assert "_observe_text_walk(root, 0, obs)" in src
    assert "get_tree().get_root()" in src
    assert 'const _TEXT_SUPPORTED = ["Label", "Button"]' in src
    for cls in ("RichTextLabel", "LineEdit", "OptionButton", "ItemList", "Tree"):
        assert cls in src, cls
    # The engine's OWN locale is observed, never inferred from a requested lang.
    assert "TranslationServer.get_locale()" in src
    assert "TranslationServer.get_loaded_locales()" in src
    assert "TranslationServer.translate(" in src
    # Whole-observation refusal on the byte ceiling, never truncation.
    assert "_TEXT_OBSERVE_BYTES_MAX" in src
    assert "the whole observation is refused rather than truncated" in src
    # Rendering-chain clip + viewport intersection for the effective rect.
    assert "func _render_chain(c: Control) -> Dictionary:" in src
    assert "get_visible_rect()" in src
    assert "clip_contents" in src
    # The walk's root is declared inside the probe (the generated probe has
    # no `root` member; an undeclared identifier would not even parse).
    assert "var root: Node = get_tree().get_root()" in src
    # Presence only: the ceiling is checked on the returned dictionary itself.
    assert "if _text_envelope_bytes(obs) > _TEXT_OBSERVE_BYTES_MAX:" in src


def test_observe_text_records_geometry_as_rects_not_ink_bounds():
    """Geometry is reported as Control/clip/viewport rects only. The observer
    must not claim a glyph ink bound or proof of occlusion anywhere."""
    src = _HARNESS.read_text(encoding="utf-8")
    for key in ("global_rect", "screen_rect", "clip_rect", "viewport_rect",
                "effective_rect"):
        assert '"%s"' % key in src
    # Honest coverage fields: unsupported surfaces and unresolved text are
    # named as incomplete reasons, never silently dropped.
    assert "incomplete_reasons" in src
    assert "displayed_text_resolved" in src
    assert "unsupported visible text surface" in src


# ── planned native fixture controls (real Godot; UNRUN by source worker) ─────
# The root-controlled native slot drives the fixture's two live built-in
# Controls (a script-less Label with a visible id and a Button whose text is a
# missing-catalog translation key), plus the real ancestor clip/viewport
# intersection. The source worker never invokes Godot.
_TEXT_OBSERVE_CONTROLS = (Path(__file__).resolve().parent / "fixtures"
                          / "godot_text_observation_controls")


def test_native_fixture_declares_visible_id_and_missing_catalog_key():
    """The literal captured corpus the native pilot observes carries BOTH
    injected strings: a visible id label and a missing-catalog translation key.
    These are present in the fixture SOURCE, not produced by any classifier."""
    main = (_TEXT_OBSERVE_CONTROLS / "main.gd").read_text(encoding="utf-8")
    assert "VISIBLE_TABLE_ID" in main
    assert "missing_catalog.key" in main
    # Both surfaces are script-less built-in Controls.
    assert "Label.new()" in main and "Button.new()" in main
    assert (_TEXT_OBSERVE_CONTROLS / "project.godot").is_file()
    assert (_TEXT_OBSERVE_CONTROLS / "main.tscn").is_file()


@requires_godot
def test_real_playtest_text_observation_same_frame_as_png(tmp_path):
    """Native: at a captured frame the observation describes that SAME frame.

    The observer sees the script-less Label by walking the live tree, records
    its global/clip/viewport rects and the engine locale, and the missing-
    catalog key stays the literal displayed string (translate() returns it
    unchanged). This is the criterion the independent native pilot confirms;
    it is UNRUN by the source worker.
    """
    import shutil as _shutil
    proj = tmp_path / "textobs"
    _shutil.copytree(_TEXT_OBSERVE_CONTROLS, proj)
    r = gh.playtest_project(str(proj), frames=8, captures=2)
    assert r["render_mode"] in ("render", "headless")
    caps = r["captures"]
    assert caps, "expected at least one captured frame"
    obs = caps[0]["text_observation"]
    assert "locale" in obs
    # The nested-viewport surface leaves the observation explicitly
    # INCOMPLETE: its geometry is named as unresolved, never guessed.
    assert obs["complete"] is False
    assert any("NestedViewportLabel" in r for r in obs["incomplete_reasons"])
    label = next(c for c in obs["controls"]
                 if c["source_text"] == "VISIBLE_TABLE_ID")
    assert label["source_text"] == "VISIBLE_TABLE_ID"
    assert label["displayed_text"] == "VISIBLE_TABLE_ID"
    assert label["displayed_text_resolved"] is True
    for key in ("global_rect", "clip_rect", "viewport_rect", "effective_rect"):
        assert isinstance(label[key], list) and len(label[key]) == 4, key
    button = next(c for c in obs["controls"] if c["class"] == "Button")
    assert button["source_text"] == "missing_catalog.key"
    assert button["displayed_text_resolved"] is True
    assert button["displayed_text"] == "missing_catalog.key"
    # Known-hidden surfaces (own alpha 0, ancestor modulate 0, clipped out,
    # hidden scene-tree parent of a top_level Label) are reported hidden.
    hidden = " ".join(h["path"] for h in obs["hidden_surfaces"])
    for name in ("OwnSelfModulateZeroLabel", "FadedLabel", "ClippedOutLabel",
                 "HiddenLabel", "TopLevelUnderHiddenLabel"):
        assert name in hidden, name
    # The rendering chain ends at a plain Node and at top_level: modulate,
    # visibility (plain Node only) and clip do not cross it, and the chain
    # end is resolved, not unresolved.
    for name in ("ParentSelfModulateChildLabel", "UnicodeLocalizedLabel",
                 "TopLevelLabel"):
        row = next(c for c in obs["controls"] if c["path"].endswith(name))
        assert row["clip_resolved"] is True, name
        assert row["text_possibly_visible"] is True, name
        assert name not in hidden, name
    uni = next(c for c in obs["controls"]
               if c["path"].endswith("UnicodeLocalizedLabel"))
    assert uni["displayed_text"] == "战况表 · localized_display"
    # Label._shape transforms: uppercase and VC_CHARS_BEFORE_SHAPING.
    up = next(c for c in obs["controls"] if c["path"].endswith("UppercaseLabel"))
    assert up["source_text"] == "upper_case_text"
    assert up["displayed_text"] == "UPPER_CASE_TEXT"
    vc = next(c for c in obs["controls"]
              if c["path"].endswith("VisibleCharactersLabel"))
    assert vc["displayed_text"] == "visi"
    # A sub-half-pixel clip_contents rect is dropped by the renderer.
    assert "SubPixelClippedLabel" in hidden
    assert obs["coordinate_space"].startswith("screen_rect/clip_rect/effective_rect")


def test_grab_preserves_an_explicit_incomplete_observation_per_failure():
    """Source contract: a requested rendered frame whose grab fails (null
    viewport/texture/image or a failed save_png) is kept as an explicit
    incomplete row -- it never vanishes and never reads as a complete empty
    corpus. Frame 0 is subject to the same accounting.
    """
    src = _HARNESS.read_text(encoding="utf-8")
    assert "func _capture_failure(drawn: int, reason: String) -> Dictionary" in src
    for reason in ("viewport is null at frame_post_draw", "viewport texture is null",
                   "viewport texture image is null", "save_png failed for"):
        assert reason in src, reason
    # Every early exit appends the failure row; the success path is the only
    # one that appends a complete observation.
    assert src.count("_captures.append(_capture_failure(drawn") == 4
    assert '"text_observation": _observe_text(drawn, img.get_size())})' in src


def test_collector_follows_the_rendering_chain_not_whole_node_ancestry():
    """Source contract (string presence only; the engine behavior is UNRUN).

    Official 4.7.2 CanvasItem::get_parent_item() is not exposed to scripts,
    so the collector re-derives the rendering parent: the direct CanvasItem
    parent unless the item is top_level; a non-CanvasItem parent or top_level
    ends the chain at the canvas, which is resolved. Modulate, cull mask and
    clip_contents follow that chain; visibility is the engine's own
    is_visible_in_tree(). Nested viewports, rotated/skewed transforms and
    pixel-mask clipping are refused as unresolved.
    """
    src = _HARNESS.read_text(encoding="utf-8")
    probe = src.split("_PROBE_GD = r\'\'\'", 1)[1].split("\'\'\'", 1)[0]
    # Named once in a comment; never called (it is not bound for scripts).
    assert probe.count("get_parent_item(") == 1
    assert "CanvasItem::get_parent_item()" in probe
    assert "func _render_chain(c: Control) -> Dictionary:" in src
    assert "if item.top_level:" in src
    assert "if p is CanvasItem:" in src
    assert 'out["chain_end"] = "canvas"' in src
    assert "c.get_global_transform_with_canvas()" in src
    assert "vp.canvas_cull_mask" in src
    assert "CanvasItem.CLIP_CHILDREN_DISABLED" in src
    assert "custom_viewport" in src
    assert "c.self_modulate.a" in src
    assert "c.is_visible_in_tree()" in src
    assert "nested viewport/window whose screen geometry is not resolvable" in src
    assert "rotated or skewed canvas transform" in src
    assert '"text_possibly_visible": possibly_visible' in src
    assert "chain is broken" not in src


def test_collector_traversal_is_finitely_bounded():
    """Source contract: node count and depth are bounded BEFORE the walk, and a
    bound stop is an incomplete reason -- never a silently partial corpus.
    """
    src = _HARNESS.read_text(encoding="utf-8")
    assert "const _TEXT_OBSERVE_MAX_NODES := 4096" in src
    assert "const _TEXT_OBSERVE_MAX_DEPTH := 64" in src
    assert "_text_walk_nodes = 0" in src
    assert 'obs["traversal_bounded"] = true' in src
    assert "the frame's corpus is not complete" in src
    # A bound stop ends the WHOLE walk: no remaining sibling or subtree
    # continues past it (only the current child used to be skipped while
    # every remaining child kept appending the bound reason).
    assert ("func _observe_text_walk(node: Node, depth: int, obs: Dictionary)"
            " -> bool:") in src
    # Children are visited by index, never via a whole get_children() array.
    assert "for i in node.get_child_count():" in src
    assert "if _observe_text_walk(node.get_child(i), depth + 1, obs):" in src


def test_byte_ceiling_is_checked_on_the_returned_observation():
    """Source contract (string presence only; the engine behavior is UNRUN).

    Every row and reason goes through _obs_add, which charges its JSON size
    and stops allocating once the ceiling cannot be met; text/path longer
    than the ceiling stop the walk before a row is built. The summary reasons
    are added BEFORE the returned dictionary is measured, the measurement
    includes its own "bytes" field, and a refusal is a fixed small shape with
    no rows, no locale list and a length-capped locale."""
    src = _HARNESS.read_text(encoding="utf-8")
    assert "func _obs_add(obs: Dictionary, key: String, row) -> void:" in src
    assert "var _text_bytes_spent := 0" in src
    probe = src.split("_PROBE_GD = r\'\'\'", 1)[1]
    body = probe[probe.index("func _observe_text(drawn: int"):]
    body = body[:body.index("\nfunc ")]
    # No direct append bypasses the accounting inside the observer.
    observer = probe[probe.index("func _render_chain("):probe.index("func _load_spec(")]
    assert ".append(" not in observer.replace("chain.append(", "").replace(
        "hidden_by.append(", "").replace("obs[key].append(row)", "")
    # Summary reasons are charged before the final whole-envelope check.
    assert body.index("unresolved displayed text") < body.index(
        "if _text_envelope_bytes(obs) > _TEXT_OBSERVE_BYTES_MAX:")
    assert "while int(obs.get(\"bytes\", -1)) != n:" in src
    assert '"loaded_locales_count": locales_total' in src
    assert "_TEXT_LOCALE_MAX_CHARS" in src
    assert "the whole observation is refused rather than truncated" in src


def test_probe_gdscript_declares_every_underscore_identifier_it_uses():
    """CPU check over the generated probe text: every `_name` identifier the
    probe reads or assigns is declared (member var/const, func, local var,
    loop variable or parameter). cb424 assigned an undeclared
    `_text_bytes_spent`, which makes the whole probe fail to parse -- every
    input, assertion and capture with it. This is a lexical check, not a
    GDScript compile; the native parse remains UNRUN."""
    import re
    probe = re.search(r"_PROBE_GD = r\'\'\'(.*?)\'\'\'",
                      _HARNESS.read_text(encoding="utf-8"), re.S).group(1)
    s = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', probe)
    s = re.sub(r"#[^\n]*", "", s)
    declared = set(re.findall(r"\b(?:var|const|func)\s+(_\w+)", s))
    declared |= set(re.findall(r"\bfor\s+(_\w+)\s+in\b", s))
    for params in re.findall(r"^func\s+\w+\(([^)]*)\)", s, re.M):
        declared |= set(re.findall(r"(_\w+)\s*(?::|=|,|$)", params))
    used = set(re.findall(r"(?<![.\w])(_\w+)\b(?!\s*\()", s))
    assert sorted(used - declared) == []
    assert "_text_bytes_spent" in used


def test_requested_capture_frames_never_observed_are_retained():
    """Source contract: a requested capture frame (frame 0 included) that
    never reached frame_post_draw is kept as an explicit incomplete row and
    named in the report -- it never vanishes from the corpus."""
    src = _HARNESS.read_text(encoding="utf-8")
    assert "was never observed at frame_post_draw" in src
    assert 'out["captures_unobserved"] = unobserved' in src


def test_attach_pngs_invalidates_complete_observation_when_png_is_missing(tmp_path):
    """A missing PNG means there is NO frame the observation could describe,
    so a COMPLETE same-frame observation is invalidated (complete=false with
    an explicit reason) while the row and the raw png_error are retained."""
    caps = [{"frame": 1, "file": "/container/local/frame_0001.png",
             "text_observation": {"frame": 1, "complete": True,
                                  "incomplete_reasons": []}}]
    out = gh._attach_pngs(caps, tmp_path)
    assert out[0]["png_error"] == "missing PNG file"
    obs = out[0]["text_observation"]
    assert obs["complete"] is False
    assert any("same-frame observation invalidated" in r for r in obs["incomplete_reasons"])
    assert obs["png_error"] == "missing PNG file"


def test_attach_pngs_invalidates_complete_observation_when_png_is_unreadable(
        tmp_path, monkeypatch):
    """Same accounting for a PNG that exists but cannot be read. An already-
    incomplete observation simply stays false; the row is never dropped."""
    png = tmp_path / "frame_0002.png"
    png.write_bytes(b"x")
    real_read = Path.read_bytes

    def broken_read(self):
        if self == png:
            raise OSError("permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", broken_read)
    caps = [{"frame": 2, "file": "/container/local/frame_0002.png",
             "text_observation": {"frame": 2, "complete": True}}]
    out = gh._attach_pngs(caps, tmp_path)
    assert "read error" in out[0]["png_error"]
    assert out[0]["text_observation"]["complete"] is False
    caps2 = [{"frame": 3, "file": "missing.png",
              "text_observation": {"frame": 3, "complete": False}}]
    out2 = gh._attach_pngs(caps2, tmp_path)
    assert out2[0]["text_observation"]["complete"] is False


def test_fixture_declares_hidden_clipped_alpha_and_intermediary_surfaces():
    """The native pilot's fixture carries the meaningful minimal edge cases in
    its SOURCE: a non-Control intermediary, a hidden Label, a Label clipped
    fully out of its ancestor, and a Label with effective alpha 0. Hidden
    surfaces are never fabricated as visible text."""
    main = (_TEXT_OBSERVE_CONTROLS / "main.gd").read_text(encoding="utf-8")
    assert "NonControlIntermediary" in main
    assert "localized_display" in main and "战况表" in main
    assert "hidden_label.visible = false" in main
    assert "clip_panel.clip_contents = true" in main
    assert "Color(1, 1, 1, 0.0)" in main
    assert "fully_transparent_text" in main
    # Minimal positive/negative CanvasItem-semantics cases: own self_modulate
    # zero vs PARENT-only self_modulate zero (which does NOT inherit), the
    # top_level flag, and a nested-viewport surface.
    assert "own_faded.self_modulate = Color(1, 1, 1, 0.0)" in main
    assert "parent_faded.self_modulate = Color(1, 1, 1, 0.0)" in main
    assert "parent_self_modulate_does_not_inherit" in main
    assert "top_label.top_level = true" in main
    assert "tl_clip.add_child(top_label)" in main
    assert "veiled.add_child(intermediary)" in main
    assert "TopLevelUnderHiddenLabel" in main
    assert "SubViewport" in main and "nested_viewport_text" in main


# ── final observation seal under the actual response serializer (CPU) ───────
def _wire_body(payload: dict) -> bytes:
    """The bytes the real _Handler._send_timed writes for `payload`."""
    import io
    h = object.__new__(gh._Handler)
    h.wfile = io.BytesIO()
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h._send_timed(200, payload)
    return h.wfile.getvalue()


def _wire_observation(out: list) -> tuple[dict, bytes]:
    body = _wire_body({"captures": out, "timing": {"report_serialize_sec": 0.0}})
    obs = json.loads(body)["captures"][0]["text_observation"]
    return obs, body


def _assert_sealed(obs: dict, body: bytes) -> None:
    # "bytes" is the observation subtree exactly as it rides in the body.
    sub = json.dumps(obs).encode()
    assert sub in body
    assert obs["bytes"] == len(sub) <= gh._TEXT_OBS_MAX_BYTES
    assert obs["bytes_encoding"] == "json.dumps"


def _complete_obs(frame: int, text: str, rows: int) -> dict:
    return {"frame": frame, "locale": "en", "complete": True,
            "incomplete_reasons": [], "refused": [], "byte_capped": False,
            "controls": [{"path": "/root/T/L%d" % i, "class": "Label",
                          "source_text": text, "displayed_text": text,
                          "displayed_text_resolved": True} for i in range(rows)]}


def test_seal_refuses_unicode_that_fits_compact_utf8_but_not_the_wire(tmp_path):
    """The probe measures compact UTF-8; the response escapes non-ASCII
    (3 UTF-8 bytes -> 6 escaped bytes per BMP char). An observation the probe
    admitted is refused as a whole once its wire form exceeds the ceiling."""
    _tiny_png(tmp_path / "frame_0001.png")
    obs = _complete_obs(1, "战" * 8000, 1)
    compact = json.dumps(obs, ensure_ascii=False, separators=(",", ":"))
    assert len(compact.encode("utf-8")) < gh._TEXT_OBS_MAX_BYTES
    out = gh._attach_pngs([{"frame": 1, "file": "x/frame_0001.png",
                            "text_observation": obs}], tmp_path)
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["complete"] is False and wire["byte_capped"] is True
    assert wire["controls"] == [] and wire["incomplete_reasons"] == []
    assert "refused rather than truncated" in wire["refused"][0]
    assert wire["refused_bytes"] > gh._TEXT_OBS_MAX_BYTES
    assert wire["frame"] == 1 and wire["locale"] == "en"
    assert b"\\u6218" not in body
    # The PNG still rode home on the row: only the observation is refused.
    assert out[0]["png_b64"]


def test_seal_keeps_a_near_cap_unicode_observation_that_fits_the_wire(tmp_path):
    _tiny_png(tmp_path / "frame_0002.png")
    obs = _complete_obs(2, "战况表", 1)
    while len(json.dumps(obs)) < gh._TEXT_OBS_MAX_BYTES - 400:
        obs["controls"].append(dict(obs["controls"][0]))
    out = gh._attach_pngs([{"frame": 2, "file": "x/frame_0002.png",
                            "text_observation": obs}], tmp_path)
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["complete"] is True
    assert wire["controls"][0]["displayed_text"] == "战况表"
    assert len(wire["controls"]) == len(obs["controls"])


def test_png_invalidation_is_resealed_and_refused_over_the_wire_cap(tmp_path):
    """A complete ASCII observation just under the ceiling loses its PNG: the
    invalidation reason and png_error are added FIRST, then the final seal
    re-measures and refuses the whole observation rather than truncating."""
    import copy
    obs = _complete_obs(3, "a" * 64, 200)
    obs["controls"][0]["note"] = ""
    pad = gh._TEXT_OBS_MAX_BYTES - 120 - len(json.dumps(obs))
    obs["controls"][0]["note"] = "n" * pad
    # With its PNG, the same observation fits the ceiling and stays complete.
    (tmp_path / "ok").mkdir()
    _tiny_png(tmp_path / "ok" / "frame_0003.png")
    kept = gh._attach_pngs([{"frame": 3, "file": "x/frame_0003.png",
                             "text_observation": copy.deepcopy(obs)}], tmp_path / "ok")
    assert kept[0]["text_observation"]["complete"] is True
    out = gh._attach_pngs([{"frame": 3, "file": "x/frame_0003.png",
                            "text_observation": obs}], tmp_path)
    assert out[0]["png_error"] == "missing PNG file"
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["complete"] is False and wire["byte_capped"] is True
    assert wire["png_error"] == "missing PNG file"
    assert wire["controls"] == []


def test_png_invalidation_under_the_cap_is_sealed_with_its_reason(tmp_path, monkeypatch):
    png = tmp_path / "frame_0004.png"
    png.write_bytes(b"x")
    real_read = Path.read_bytes

    def broken_read(self):
        if self == png:
            raise OSError("permission denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", broken_read)
    out = gh._attach_pngs([{"frame": 4, "file": "x/frame_0004.png",
                            "text_observation": _complete_obs(4, "VISIBLE_TABLE_ID", 1)}],
                          tmp_path)
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["complete"] is False
    assert "read error" in wire["png_error"]
    assert any("same-frame observation invalidated" in r
               for r in wire["incomplete_reasons"])
    assert wire["controls"][0]["source_text"] == "VISIBLE_TABLE_ID"
    # An already-incomplete observation is invalidated the same way.
    out2 = gh._attach_pngs([{"frame": 5, "file": "x/frame_0005.png",
                             "text_observation": {"frame": 5, "complete": False,
                                                  "incomplete_reasons": ["u"]}}],
                           tmp_path)
    w2, b2 = _wire_observation(out2)
    _assert_sealed(w2, b2)
    assert w2["png_error"] == "missing PNG file"
    assert w2["incomplete_reasons"][0] == "u" and len(w2["incomplete_reasons"]) == 2


def test_refusal_is_small_and_drops_long_locale_error_and_reason_fields(tmp_path):
    """Over the cap, long locale/error/reason/path data are NOT retained: the
    refusal is a fixed small shape, never a truncated copy of them."""
    obs = {"frame": 6, "complete": False, "locale": "x" * 70000,
           "error": "e" * 70000, "incomplete_reasons": ["r" * 1000] * 80,
           "loaded_locales": ["l" * 500] * 200}
    out = gh._attach_pngs([{"frame": 6, "file": "x/frame_0006.png",
                            "text_observation": obs}], tmp_path)
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["byte_capped"] is True and wire["complete"] is False
    assert wire["locale"] is None and wire["error"] is None
    assert wire["png_error"] == "missing PNG file"
    assert "loaded_locales" not in wire and wire["incomplete_reasons"] == []
    assert wire["bytes"] < 1024
    # The row still names the PNG failure outside the capped subtree.
    assert out[0]["png_error"] == "missing PNG file"


def test_absent_or_non_object_observation_is_sealed_explicitly(tmp_path):
    _tiny_png(tmp_path / "frame_0007.png")
    out = gh._attach_pngs([{"frame": 7, "file": "x/frame_0007.png"},
                           {"frame": 7, "file": "x/frame_0007.png",
                            "text_observation": None}], tmp_path)
    for row in out:
        obs = row["text_observation"]
        assert obs["complete"] is False and "no text observation" in obs["error"]
        assert obs["bytes"] == len(json.dumps(obs).encode())
        assert row["png_b64"]


def test_normal_capture_stays_complete_and_png_corresponds(tmp_path):
    raw = _tiny_png(tmp_path / "frame_0000.png")
    obs = _complete_obs(0, "VISIBLE_TABLE_ID", 2)
    out = gh._attach_pngs([{"frame": 0, "file": "x/frame_0000.png",
                            "text_observation": obs}], tmp_path)
    wire, body = _wire_observation(out)
    _assert_sealed(wire, body)
    assert wire["complete"] is True and wire["frame"] == 0
    assert json.loads(body)["captures"][0]["png_b64"] == _b64.b64encode(raw).decode()
    assert "png_error" not in wire


def test_label_display_transform_follows_label_shape_or_is_unresolved():
    """Source contract (native UNRUN): the drawn string reproduces
    Label._shape's uppercase + VC_CHARS_BEFORE_SHAPING substr through the same
    public TextServer call and the node's own atr(); glyph/line selection after
    shaping and locale-dependent case mapping are unresolved, never guessed."""
    src = _HARNESS.read_text(encoding="utf-8")
    assert "func _label_display_transform(c: Control, txt: String) -> Dictionary:" in src
    assert "TextServerManager.get_primary_interface()" in src
    assert "ts.string_to_upper(txt, lang)" in src
    assert "TextServer.VC_CHARS_BEFORE_SHAPING" in src
    assert "txt = txt.substr(0, vc)" in src
    assert "TextServer.OVERRUN_NO_TRIMMING" in src
    assert "lines_skipped" in src and "max_lines_visible" in src
    assert "displayed = str(c.atr(source_text))" in src
    assert "var shaped := _label_display_transform(c, str(displayed))" in src


def test_clip_follows_renderer_half_pixel_and_rounding_in_png_space():
    """Source contract (native UNRUN): clips are accumulated canvas-down in
    the captured PNG's pixel space, an under-0.5 px clip drops the item and a
    kept clip is rounded, as renderer_canvas_cull.cpp does; stretch is applied
    via the viewport's final transform or the geometry is refused."""
    src = _HARNESS.read_text(encoding="utf-8")
    assert "if clip.size.x < 0.5 or clip.size.y < 0.5:" in src
    assert "clip = Rect2(clip.position.round(), clip.size.round())" in src
    assert "for i in range(chain.size() - 1, -1, -1):" in src
    assert "_png_xform = vp.get_final_transform()" in src
    assert "var t := _png_xform * c.get_global_transform_with_canvas()" in src
    assert "vp.get_stretch_transform() * vp.get_visible_rect()" in src
    assert '"coordinate_space":' in src and '"png_from_viewport_2d":' in src
    assert "under 0.5 px, so the renderer does not draw it" in src
