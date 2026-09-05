#!/usr/bin/env python3
"""Godot game-harness — the brain of the aitelier-godot builder sidecar.

Runs inside a container that has the Godot 4 headless binary, but is also
directly runnable on any host with `godot` available (set GODOT_BIN). It is the
Godot analogue of docker/unity/unity_compile.py, but far simpler: Godot is free,
needs no license activation, and its headless binary can both parse-check scripts
(compile gate) and run the game with a dummy renderer (playtest gate).

Two capabilities, exposed over HTTP and CLI:

  compile  -> `godot --headless --path <proj> --import`, parse stderr for
              GDScript parse errors / failed script loads. Returns CS####-style
              diagnostics with res:// file + line.

  playtest -> copy the project, inject an autoload probe, run the game for N
              frames on a virtual X display (Xvfb + software GL), then return:
                * every runtime error (SCRIPT ERROR / push_error) with file+line
                * a JSON snapshot of the live scene tree's script variables
                  (score, velocity, game_state, ...) — the thing that makes an
                  agent actually SEE runtime state, which Unity could never give.
                * PNGs of real rendered frames, base64'd home over HTTP — the
                  thing that makes an agent actually SEE the game.

The gate_skipped fail-open->observable contract is enforced on the *tool* side
(aitelier/tools/godot_compile), not here; this service just reports facts.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GODOT_BIN = os.environ.get("GODOT_BIN", "godot")
DEFAULT_PLAYTEST_FRAMES = int(os.environ.get("GODOT_PLAYTEST_FRAMES", "180"))
# How many frames to photograph per playtest run. 0 disables rendering entirely
# and falls back to the pure-headless run — the opt-out for anyone who only wants
# the state snapshot (or whose host has no working software GL).
PLAYTEST_CAPTURES = int(os.environ.get("GODOT_PLAYTEST_CAPTURES", "4"))
RENDER_RES = os.environ.get("GODOT_PLAYTEST_RES", "1280x720")
PORT = int(os.environ.get("PORT", "8080"))
_MAX_CAPTURES = 8       # hard ceiling: every PNG rides home inside the JSON body

# ── error parsing ──────────────────────────────────────────────────────────
# Godot always exits 0 even on script errors, so correctness lives in stderr.
# A SCRIPT ERROR is always user-relevant (parse errors, null calls, bad method).
# A plain ERROR line is engine-internal noise UNLESS it is a user push_error.
_SCRIPT_ERR = re.compile(r"^SCRIPT ERROR:\s*(.*)")
_USER_ERR = re.compile(r"^(?:USER )?ERROR:\s*(.*)")
_FAILED_LOAD = re.compile(r'^ERROR: Failed to load script "(res://[^"]+)"')
# `   at: <where> (<file>:<line>)`  — res:// => user code, otherwise engine C++.
_AT = re.compile(r"^\s*at:\s*(.*?)\s*\((.+?):(\d+)\)")


def _parse_errors(stderr: str) -> list[dict]:
    """Extract user-relevant diagnostics from Godot stderr.

    Each diagnostic: {kind, msg, file, line}. `file` is a res:// path when the
    error is locatable in user code, else None. Engine-internal ERROR lines
    (Condition "..." is true, editor/progress_dialog.cpp, ...) are dropped.
    """
    lines = stderr.splitlines()
    out: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _SCRIPT_ERR.match(line)
        failed = _FAILED_LOAD.match(line)
        kind = None
        msg = None
        if m:
            kind = "parse" if "Parse Error" in m.group(1) else "runtime"
            msg = m.group(1)
        elif failed:
            kind = "load"
            msg = f'Failed to load script "{failed.group(1)}"'
        elif _USER_ERR.match(line):
            # Only keep it if the following `at:` points at a user push_error,
            # i.e. the game deliberately signalled a problem. Engine internals
            # (progress_dialog.cpp, "Condition ... is true") are ignored.
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            at = _AT.match(nxt)
            if at and at.group(1).strip().startswith("push_error"):
                kind, msg = "push_error", _USER_ERR.match(line).group(1)
        if kind is None:
            i += 1
            continue
        # Look ahead one line for the location.
        file = None
        loc_line = None
        if i + 1 < len(lines):
            at = _AT.match(lines[i + 1])
            if at and at.group(2).startswith("res://"):
                file, loc_line = at.group(2), int(at.group(3))
                i += 1
        out.append({"kind": kind, "msg": msg.strip(), "file": file, "line": loc_line})
        i += 1
    return out


def _run(args: list[str], timeout: int, extra_env: dict | None = None,
         render: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    if render:
        # --headless FORCES the dummy rendering driver, which draws nothing, so a
        # viewport capture comes back empty. To photograph frames Godot needs a
        # real display: Xvfb gives it one, and llvmpipe gives it a GL stack (this
        # container has no GPU). Compile stays on --headless — it needs no display
        # and skipping the display is faster.
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        cmd = ["xvfb-run", "-a", "-s", f"-screen 0 {RENDER_RES}x24", GODOT_BIN,
               "--display-driver", "x11", "--rendering-driver", "opengl3", *args]
    else:
        cmd = [GODOT_BIN, "--headless", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=env,
    )


# ── compile gate ───────────────────────────────────────────────────────────
def _copy_project(proj: Path) -> Path:
    """Copy a project to a writable temp dir. `--import` and play-test runs write
    a `.godot/` cache, but the sidecar mounts the workspace read-only, so we never
    touch the source. Caller must rmtree the returned dir's parent."""
    work = Path(tempfile.mkdtemp(prefix="godot_"))
    dst = work / "proj"
    shutil.copytree(proj, dst, ignore=shutil.ignore_patterns(".godot", ".git"))
    return dst


# A THIRD pass, because the first two do not parse every script. `--import`
# parses what it can REACH from resources: scenes, autoloads, their preload
# chains as resources. A .gd that is only ever `preload()`ed by another script
# is enumerated by rglob but never compiled — so the summary said
# "GDScript parse OK (68 scripts)" while one of those 68 could not parse at all.
#
# Measured 2026-08-25 on jinyong-assets, same type error injected into two
# scripts: `health_bar.gd` (attached via health_bar.tscn) was caught; the
# identical error in `visibility_probe.gd` (preload-only) was NOT reported.
# That gap cost a real diagnosis: the runtime said "Nonexistent function
# first_fail_layer in base GDScript", the gate said 0 errors, and the correct
# hypothesis (the class does not compile) was ruled out because the gate was
# believed. A gate that vouches for what it never looked at is worse than one
# that stays silent.
_PARSE_ALL_GD = """extends SceneTree
func _walk(dir_path: String, out: Array) -> void:
	var d := DirAccess.open(dir_path)
	if d == null:
		return
	d.list_dir_begin()
	var n := d.get_next()
	while n != "":
		if d.current_is_dir():
			if not n.begins_with("."):
				_walk(dir_path.path_join(n), out)
		elif n.ends_with(".gd"):
			out.append(dir_path.path_join(n))
		n = d.get_next()
	d.list_dir_end()

func _init() -> void:
	var files: Array = []
	_walk("res://", files)
	var loaded := 0
	for f in files:
		if f.ends_with("__parse_all.gd"):
			continue
		var r = ResourceLoader.load(f)
		if r == null:
			push_error("Failed to load script \\"%s\\"" % f)
		else:
			loaded += 1
	print("PARSE_ALL_LOADED=%d/%d" % [loaded, files.size() - 1])
	quit()
"""


def _parse_every_script(dst: Path, timeout: int) -> tuple[str, int]:
    """Load every .gd explicitly. Returns (stderr, scripts_loaded_ok).

    Errors are pushed, never raised, and quit() is the last statement on the
    only path — an `extends SceneTree` entry point that can miss quit() spins
    the tree until the wall (recorded lesson, design/90_decisions.md).

    LIMITATION, measured 2026-08-25, stated because a gate must not imply more
    than it checked: this pass is not exhaustive when SEVERAL scripts are
    broken. With one error in an attached script and another in a preload-only
    script, only the attached one was reported — the first failure can mask a
    later one. With the error in the preload-only script ALONE it is reported
    exactly (file + line), which is the case the import pass could never see.
    So: fix what it reports, then RUN IT AGAIN. "0 errors" means "nothing left
    that this pass can reach", and after a clean run that is the whole tree —
    68 of 68 explicitly re-parsed on jinyong-assets."""
    probe = dst / "__parse_all.gd"
    try:
        probe.write_text(_PARSE_ALL_GD, encoding="utf-8")
        cp = _run(["--path", str(dst), "--script", "res://__parse_all.gd"],
                  timeout=timeout)
    except subprocess.TimeoutExpired:
        return ("", -1)
    finally:
        probe.unlink(missing_ok=True)
    n = -1
    for line in (cp.stdout or "").splitlines():
        if line.startswith("PARSE_ALL_LOADED="):
            n = int(line.split("=", 1)[1].split("/")[0])
    return (cp.stderr or "", n)


def compile_project(project_dir: str, timeout: int = 120) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        # `no_project` is the machine-readable half of this answer, and the
        # caller needs it: "I cannot see a project here" is a PASS when the repo
        # really is a Python one, and a hard FAILURE when the caller is looking
        # at project.godot as it asks. Only the caller can tell those apart, and
        # it cannot tell them apart from prose.
        return {"passed": True, "returncode": 0, "file_count": 0,
                "errors": [], "warning_count": 0, "no_project": True,
                "summary": "No Godot project (project.godot absent) — nothing to compile."}
    gd_files = [p for p in proj.rglob("*.gd") if ".godot/" not in str(p)]
    dst = _copy_project(proj)
    try:
        # TWO passes, and the second one is the authoritative diagnosis. Godot
        # imports resources and parses scripts in the SAME pass, so on a cold
        # cache every `preload("res://…​.wav")` is read before its importer has
        # run and reports "no resource loaders (unrecognized file extension)" —
        # a phantom. Measured on a project with six sound effects: pass one
        # reports 18 errors, pass two reports the 6 that are real. Blaming the
        # agent for the other 12 makes the goal loop burn iterations chasing
        # errors that do not exist.
        _run(["--path", str(dst), "--import"], timeout=timeout)
        cp = _run(["--path", str(dst), "--import"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "file_count": len(gd_files),
                "errors": [{"kind": "timeout", "msg": f"Import timed out after {timeout}s",
                            "file": None, "line": None}],
                "warning_count": 0, "summary": "Godot import timed out."}
    all_stderr, loaded_ok = _parse_every_script(dst, timeout)
    # The explicit pass contributes CAUSES only. Its `load` failures are not
    # trustworthy: `--script` runs a bare SceneTree with NO autoloads, so every
    # script that names GameManager / CombatManager / GridManager fails to
    # resolve them and "fails to load" while being perfectly fine in the game.
    # Measured on a clean tree: 0 parse errors and 37 such load failures. A
    # parse error, by contrast, is a fault in the file itself and holds either
    # way — that is the half that caught the preload-only `Canvas` error the
    # import pass never looked at.
    errs = [e for e in _parse_errors(cp.stderr) if e["kind"] in ("parse", "load")]
    errs += [e for e in _parse_errors(all_stderr) if e["kind"] == "parse"]
    seen, deduped = set(), []
    for e in errs:
        key = (e.get("kind"), e.get("file"), e.get("line"), e.get("msg"))
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    errs = deduped
    passed = not errs
    # Report what was PARSED, not what was found on disk. The old summary took
    # its count from rglob and its verdict from --import — two different sets.
    # Say what was actually looked at. The old summary took its COUNT from
    # rglob and its VERDICT from --import — two different sets, and the gap
    # between them is exactly where the missed error lived.
    parsed_note = ("%d of %d scripts explicitly re-parsed" % (loaded_ok, len(gd_files))
                   if loaded_ok >= 0
                   else "explicit re-parse TIMED OUT — coverage unverified")
    # Parse errors are CAUSES; the load failures of everything that preloads a
    # broken script are consequences. One injected error produced 3 parse lines
    # and 40 dependent load lines — burying the cause under its own fallout is
    # how a report stops being actionable. Causes first, and the summary says
    # which is which.
    n_parse = sum(1 for e in errs if e["kind"] == "parse")
    errs.sort(key=lambda e: (e["kind"] != "parse", str(e.get("file") or "")))
    summary = ("GDScript parse OK (%s)." % parsed_note
               if passed
               else "GDScript parse FAILED — %d parse error(s), %d dependent "
                    "load failure(s). Fix the parse errors; the load failures "
                    "are their fallout." % (n_parse, len(errs) - n_parse))
    shutil.rmtree(dst.parent, ignore_errors=True)
    return {"passed": passed, "returncode": cp.returncode, "file_count": len(gd_files),
            "errors": errs, "warning_count": 0, "summary": summary}


# ── playtest gate ──────────────────────────────────────────────────────────
_PROBE_GD = r'''extends Node
# AItelier runtime probe (injected). Two modes:
#   * SPEC mode  (AITELIER_PROBE_SPEC set): drive an AUTHORED input timeline and,
#     at each assert frame, evaluate a GDScript Expression against a live node —
#     objective, per-game behavioural checks (the TDD oracle).
#   * LEGACY mode (no spec): run N frames auto-pressing one action every 20
#     frames, then snapshot — the old canned smoke test (still the fallback).
# Orthogonally, if AITELIER_PROBE_CAPTURE names a directory it saves the viewport
# as a PNG at each frame listed in AITELIER_PROBE_CAPTURE_AT.
# Always writes {frames, asserts[], nodes{}, captures[]} to AITELIER_PROBE_OUT.
# Indented with spaces (GDScript accepts consistent spaces or tabs).
var _frame := 0
var _max := 180
var _dumped := false
var _legacy_action := ""
var _spec_mode := false
var _timeline := []      # SPEC: [{at:int, press?:String, release?:String, assert?:[{name,node,expr}]}]
var _releases := {}      # frame -> [action, ...] auto-release schedule
var _results := []       # [{name, node, expr, passed, actual, error, frame}]
var _capture_dir := ""
var _capture_at := {}    # frame -> true, consumed as each one is photographed
var _captures := []      # [{frame, file}]
var _watch := []         # [{node, attr}] whose frame-0 value a delta assert needs
var _baselines := {}     # "node|attr" -> frame-0 value
func _ready() -> void:
    # Keep ticking even when the game calls get_tree().paused = true — otherwise
    # the probe freezes with the game and can neither un-pause nor assert, so a
    # pause feature would be untestable.
    process_mode = Node.PROCESS_MODE_ALWAYS
    # Cap the framerate so a frame budget maps to stable game time — headless
    # runs uncapped otherwise, making delta tiny so the game barely advances.
    Engine.max_fps = 60
    var envf := OS.get_environment("AITELIER_PROBE_FRAMES")
    _max = int(envf) if envf != "" else 180
    var spec_path := OS.get_environment("AITELIER_PROBE_SPEC")
    if spec_path != "":
        _load_spec(spec_path)
    else:
        _legacy_action = OS.get_environment("AITELIER_PROBE_INPUT")
        if _legacy_action != "" and not InputMap.has_action(_legacy_action):
            InputMap.add_action(_legacy_action)
    _capture_dir = OS.get_environment("AITELIER_PROBE_CAPTURE")
    if _capture_dir != "":
        for s in OS.get_environment("AITELIER_PROBE_CAPTURE_AT").split(",", false):
            _capture_at[int(s)] = true
        # frame_post_draw is the ONLY moment the viewport texture holds the frame
        # that was just drawn; reading it from _process yields the PREVIOUS one.
        RenderingServer.frame_post_draw.connect(_on_post_draw)
func _on_post_draw() -> void:
    # _process bumps _frame at its END and post-draw fires after _process, so the
    # frame just drawn is _frame - 1, not _frame.
    var drawn := _frame - 1
    if not _capture_at.has(drawn):
        return
    _capture_at.erase(drawn)
    var vp := get_viewport()
    if vp == null:
        return
    var tex := vp.get_texture()
    if tex == null:
        return
    var img := tex.get_image()
    if img == null:
        return
    var path := _capture_dir.path_join("frame_%04d.png" % drawn)
    if img.save_png(path) == OK:
        _captures.append({"frame": drawn, "file": path})
func _load_spec(path: String) -> void:
    var f := FileAccess.open(path, FileAccess.READ)
    if f == null:
        return
    var data = JSON.parse_string(f.get_as_text())
    f.close()
    if typeof(data) != TYPE_DICTIONARY:
        return
    _spec_mode = true
    if data.has("frames"):
        _max = int(data["frames"])
    var tl = data.get("timeline", [])
    if typeof(tl) == TYPE_ARRAY:
        for e in tl:
            _timeline.append(e)
    # Register every action the timeline presses so the input actually fires.
    for e in _timeline:
        for key in ["press", "release"]:
            var act = e.get(key, "")
            if act != "" and not InputMap.has_action(act):
                InputMap.add_action(act)
    # A "changed"/"unchanged" assertion needs the value it is changing FROM.
    for e in _timeline:
        var asserts = e.get("assert", [])
        if typeof(asserts) == TYPE_ARRAY:
            for a in asserts:
                if a.has("mode"):
                    _watch.append({"node": str(a.get("node", "")), "attr": str(a.get("attr", ""))})
func _process(_d: float) -> void:
    # 0-based frames: apply this frame's scheduled releases + timeline entries,
    # THEN advance. Incrementing first would make `at: 0` unreachable.
    if _frame == 0:
        _capture_baselines()
    if _releases.has(_frame):
        for act in _releases[_frame]:
            if InputMap.has_action(act):
                _act(act, false)
        _releases.erase(_frame)
    if _spec_mode:
        for e in _timeline:
            if int(e.get("at", -1)) == _frame:
                _apply_entry(e)
    elif _legacy_action != "":
        if _frame % 20 == 0:
            _act(_legacy_action, true)
        elif _frame % 20 == 1:
            _act(_legacy_action, false)
    _frame += 1
    if _frame >= _max:
        _finish()
        get_tree().quit()
func _act(action: String, pressed: bool) -> void:
    # Feed a real InputEventAction through the input system: this reaches BOTH
    # polling (Input.is_action_pressed) AND event handlers (_input /
    # _unhandled_input + event.is_action_pressed). Input.action_press only
    # updates polling state, so event-driven input (a common pause handler)
    # would never fire.
    var ev := InputEventAction.new()
    ev.action = action
    ev.pressed = pressed
    Input.parse_input_event(ev)
# LIMITATION, MEASURED 2026-08-24 -- read this before writing a mouse scenario.
#
# `click:` reliably drives CONTROLS: Godot routes GUI input by the event's own
# `position`, so a synthesized button event hits the Control under that point.
# Verified end-to-end against the real menu (click MenuEntry0 -> the state
# actually becomes CHARACTER_CREATION), and both negative controls fail loudly.
#
# It does NOT drive world-space picking that reads `get_global_mouse_position()`.
# jinyong-assets' player.gd:460 does exactly that, and clicking an enemy Node2D
# produced NO error and NO effect -- twice, with and without a preceding
# InputEventMouseMotion. The same timeline with `attack_confirm` works, so the
# setup was sound; only the click was inert. What is PROVEN is the inertness;
# what is NOT proven is the mechanism (most likely the viewport's cached pointer
# is owned by the windowing system and cannot be moved headless, but that was
# not measured -- do not repeat it as fact).
#
# Consequence: a scenario whose handler re-queries the global mouse position
# CANNOT be tested with `click:` today, and would pass vacuously if someone
# asserted only "no error". Either make the handler use the event position it
# was already handed (the more robust shape anyway, and it makes the path
# testable), or drive that path with its keyboard action instead.
func _click(spec: String) -> void:
    # `spec` is "<Node>[ +dx,dy][ left|right|middle]" -- see _click_at.
    var node_name := spec
    var offset := Vector2.ZERO
    var button := MOUSE_BUTTON_LEFT
    var toks := spec.split(" ", false)
    if toks.size() > 0:
        node_name = toks[0]
        for i in range(1, toks.size()):
            var t: String = toks[i]
            if t.begins_with("+") or t.begins_with("-") or ("," in t):
                var xy := t.split(",", false)
                if xy.size() != 2 or not xy[0].is_valid_float() or not xy[1].is_valid_float():
                    push_error("click: malformed offset %s in spec: %s" % [t, spec])
                    return
                offset = Vector2(float(xy[0]), float(xy[1]))
            elif t == "left":
                button = MOUSE_BUTTON_LEFT
            elif t == "right":
                button = MOUSE_BUTTON_RIGHT
            elif t == "middle":
                button = MOUSE_BUTTON_MIDDLE
            else:
                push_error("click: unknown token %s in spec: %s" % [t, spec])
                return
    _click_at(node_name, offset, button, spec)


## Resolve a spec's node to the on-SCREEN point a real pointer would sit at,
## or Vector2(NAN, NAN) when it cannot be delivered (every refusal is a
## push_error -- an input the spec asked for and the probe never sent is
## indistinguishable from a game that ignored it). Shared by `click`/`clicks`
## and by `hover`/`hovers`, so both address a node exactly the same way.
func _point_of(node_name: String, offset: Vector2, spec: String) -> Vector2:
    var nan_pt := Vector2(NAN, NAN)
    var n := _resolve(node_name)
    if n == null:
        push_error("aim: node not found: " + node_name + " (spec: " + spec + ")")
        return nan_pt
    var pos: Vector2
    if n is Control:
        var c := n as Control
        if not c.is_visible_in_tree():
            push_error("aim: node is not visible in tree: " + node_name)
            return nan_pt
        if c.mouse_filter == Control.MOUSE_FILTER_IGNORE:
            push_error("aim: node has mouse_filter=IGNORE (cannot be hit): " + node_name)
            return nan_pt
        var r := c.get_global_rect()
        if r.size.x <= 0.0 or r.size.y <= 0.0:
            push_error("aim: node has a zero-size rect: " + node_name)
            return nan_pt
        pos = r.position + r.size * 0.5
    elif n is Node2D:
        var n2 := n as Node2D
        if not n2.is_visible_in_tree():
            push_error("aim: node is not visible in tree: " + node_name)
            return nan_pt
        pos = n2.get_global_transform_with_canvas().origin
    else:
        push_error("aim: node is neither a Control nor a Node2D (cannot be aimed at): " + node_name)
        return nan_pt
    pos += offset
    var vp_rect := get_viewport().get_visible_rect()
    if not vp_rect.has_point(pos):
        push_error("aim: point %s is outside the viewport %s (spec: %s)" % [pos, vp_rect.size, spec])
        return nan_pt
    return pos


## MOVE THE POINTER, PRESS NOTHING. `clicks:` already moves the pointer before
## its button event, so a click has always IMPLIED a hover -- which means a
## hover-only affordance (a tooltip, a description preview) could be observed
## by a click but never told apart from what the click itself selected.
## `hovers:` is that missing half: it fires mouse_entered / mouse_exited and
## nothing else, so a scenario can assert "pointing at it previews" separately
## from "pressing it selects". Same spec grammar as `clicks:` minus the button
## token -- "<Node>[ +dx,dy]"; a button token is refused, not ignored.
func _hover(spec: String) -> void:
    var node_name := spec
    var offset := Vector2.ZERO
    var toks := spec.split(" ", false)
    if toks.size() > 0:
        node_name = toks[0]
        for i in range(1, toks.size()):
            var t: String = toks[i]
            if t.begins_with("+") or t.begins_with("-") or ("," in t):
                var xy := t.split(",", false)
                if xy.size() != 2 or not xy[0].is_valid_float() or not xy[1].is_valid_float():
                    push_error("hover: malformed offset %s in spec: %s" % [t, spec])
                    return
                offset = Vector2(float(xy[0]), float(xy[1]))
            else:
                push_error("hover: unknown token %s in spec: %s (hover takes no button)" % [t, spec])
                return
    var pos := _point_of(node_name, offset, spec)
    if is_nan(pos.x):
        return
    var mm := InputEventMouseMotion.new()
    mm.position = pos
    mm.global_position = pos
    Input.parse_input_event(mm)


## Click a resolved node's on-screen point, optionally displaced by `offset`
## screen pixels and with a chosen mouse button.
##
## The OFFSET exists because not every clickable thing is a node. The battle
## grid is painted by _draw() -- an empty tile has no node to name -- so a
## click-to-move scenario cannot address its destination by name. Absolute
## screen coordinates would work but rot the moment the camera, the tile size
## or the layout moves. Anchoring to a live node instead ("Player +64,0" = one
## 64px tile to the player's right) keeps the scenario expressed in the terms
## the DESIGN uses, and it stays correct as long as the anchor is where the
## game says it is -- which the surrounding assertions already check.
func _click_at(node_name: String, offset: Vector2, button: int, spec: String) -> void:
    # Click a NAMED NODE, not a raw coordinate: resolve it, take the centre of
    # its on-screen rect, and send a real InputEventMouseButton there. That is
    # what makes this a HIT TEST rather than a handler call -- a button that is
    # covered by another Control, has mouse_filter IGNORE, is zero-sized or has
    # drifted off-screen will simply not receive the event, which is exactly the
    # failure a `debug_click_*` action that calls the handler directly cannot see.
    #
    # Every failure here is push_error, never a silent skip: a click the probe
    # could not deliver means the scenario did not do what it says it does, and
    # a scenario that quietly skips its own input is how this harness once
    # graded a game nobody played.
    var pos := _point_of(node_name, offset, spec)
    if is_nan(pos.x):
        return
    var mm := InputEventMouseMotion.new()
    mm.position = pos
    mm.global_position = pos
    Input.parse_input_event(mm)
    for is_down in [true, false]:
        var ev := InputEventMouseButton.new()
        ev.button_index = button
        ev.pressed = is_down
        ev.position = pos
        ev.global_position = pos
        Input.parse_input_event(ev)


func _apply_entry(e: Dictionary) -> void:
    # Hover BEFORE click on the same frame: a scenario that writes both means
    # "point here, then press", which is the order a real pointer does it in.
    var hv = e.get("hover", "")
    if hv != "":
        _hover(str(hv))
    var ck = e.get("click", "")
    if ck != "":
        _click(str(ck))
    var pr = e.get("press", "")
    if pr != "":
        _act(pr, true)
        var rf := _frame + 2      # hold ~2 frames, then auto-release
        _releases[rf] = _releases.get(rf, [])
        _releases[rf].append(pr)
    var rl = e.get("release", "")
    if rl != "" and InputMap.has_action(rl):
        _act(rl, false)
    var asserts = e.get("assert", [])
    if typeof(asserts) == TYPE_ARRAY:
        for a in asserts:
            _eval_assert(a)
func _eval_assert(a: Dictionary) -> void:
    var node_name = str(a.get("node", ""))
    var expr_str = str(a.get("expr", ""))
    var res := {"name": str(a.get("name", expr_str)), "node": node_name,
        "expr": expr_str, "passed": false, "actual": null, "error": "", "frame": _frame}
    var target := _resolve(node_name)
    if target == null:
        res["error"] = "node not found: " + node_name
        _results.append(res)
        return
    if a.has("mode"):
        _eval_delta(a, target, res)
        return
    var expr := Expression.new()
    if expr.parse(expr_str) != OK:
        res["error"] = "parse error: " + expr.get_error_text()
        _results.append(res)
        return
    # Evaluate against the node as base instance (so "velocity.y < 0" resolves the
    # node's own properties). show_error=false keeps a failed assert OUT of stderr
    # so it stays advisory and never trips the hard runtime-error gate.
    var val = expr.execute([], target, false)
    if expr.has_execute_failed():
        res["error"] = "execute failed: " + expr.get_error_text()
        _results.append(res)
        return
    res["actual"] = _jsonable(val)
    res["passed"] = bool(val)
    # A FAILING comparison reports `false` and nothing else — which says the
    # assert did not hold, but not what was there instead. Read the asserted
    # attribute back and record it, so the report can say "turns_taken == 1
    # failed, it was 3" rather than "failed".
    # jinyong-usable 2026-08-23: a whole task card was spent re-timing sample
    # frames, derived from a tween budget read out of the source, because the
    # report could not say what current_round actually was at the frame it
    # sampled. The re-timed frames failed too. The probe had the number the
    # entire time and threw it away.
    if not res["passed"] and a.has("attr"):
        res["observed"] = _jsonable(_read_attr(target, str(a["attr"])))
    _results.append(res)
func _capture_baselines() -> void:
    for w in _watch:
        var t := _resolve(str(w["node"]))
        if t != null:
            _baselines[str(w["node"]) + "|" + str(w["attr"])] = _read_attr(t, str(w["attr"]))
func _read_attr(target: Object, attr: String):
    # Same Expression machinery the assertions use, so "velocity.y" reads as
    # naturally as "grid_pos".
    var e := Expression.new()
    if e.parse(attr) != OK:
        return null
    var v = e.execute([], target, false)
    if e.has_execute_failed():
        return null
    return _jsonable(v)
func _eval_delta(a: Dictionary, target: Node, res: Dictionary) -> void:
    var attr := str(a.get("attr", ""))
    var mode := str(a.get("mode", "changed"))
    var key := str(a.get("node", "")) + "|" + attr
    var cur = _read_attr(target, attr)
    var base = _baselines.get(key, null)
    res["expr"] = attr + " " + mode + " since frame 0"
    res["actual"] = {"baseline": base, "current": cur}
    res["passed"] = (cur != base) if mode == "changed" else (cur == base)
    _results.append(res)
func _resolve(name: String) -> Node:
    if name == "":
        return get_tree().current_scene
    if name.begins_with("/") or name.begins_with("res:"):
        return get_node_or_null(NodePath(name))
    var scene := get_tree().current_scene
    # A "/"-separated name is a PATH (e.g. "HUD/PausedLabel"), not a node name —
    # resolve it relative to the current scene; fall back to matching the leaf
    # name anywhere in the tree if the exact path doesn't line up.
    if "/" in name and scene != null:
        var n := scene.get_node_or_null(NodePath(name))
        if n != null:
            return n
        var parts := name.split("/")
        return get_tree().get_root().find_child(parts[parts.size() - 1], true, false)
    return get_tree().get_root().find_child(name, true, false)
func _jsonable(v):
    match typeof(v):
        TYPE_VECTOR2:
            return [v.x, v.y]
        TYPE_VECTOR3:
            return [v.x, v.y, v.z]
        TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_STRING:
            return v
        _:
            return str(v)
func _exit_tree() -> void:
    _finish()  # fallback if the game quit itself before the frame budget
func _finish() -> void:
    if _dumped:
        return
    _dumped = true
    var out := {"frames": _frame, "asserts": _results, "nodes": {}, "captures": _captures}
    _walk(get_tree().get_root(), out["nodes"])
    var path := OS.get_environment("AITELIER_PROBE_OUT")
    if path == "":
        path = "user://probe_state.json"
    var f := FileAccess.open(path, FileAccess.WRITE)
    if f != null:
        f.store_string(JSON.stringify(out, "  "))
        f.close()
        print("AITELIER_PROBE_WROTE ", path)
func _walk(node: Node, acc: Dictionary) -> void:
    # Only snapshot script-bearing nodes (the gameplay logic), but for those also
    # capture transform so the agent sees WHERE things are, not just their vars.
    if node.get_script() != null:
        var vars := {}
        for p in node.get_property_list():
            if p.usage & PROPERTY_USAGE_SCRIPT_VARIABLE:
                var v = node.get(p.name)
                match typeof(v):
                    TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_STRING:
                        vars[p.name] = v
                    TYPE_VECTOR2:
                        vars[p.name] = [v.x, v.y]
        var entry := {"class": node.get_class(), "vars": vars}
        if node is Node2D:
            entry["pos"] = [node.global_position.x, node.global_position.y]
            entry["visible"] = node.visible
        elif node is Node3D:
            entry["pos"] = [node.global_position.x, node.global_position.y, node.global_position.z]
            entry["visible"] = node.visible
        acc[str(node.get_path())] = entry
    for c in node.get_children():
        _walk(c, acc)
'''


def _inject_probe(dst: Path) -> None:
    (dst / "_aitelier_probe.gd").write_text(_PROBE_GD)
    pg = dst / "project.godot"
    text = pg.read_text() if pg.is_file() else "config_version=5\n"
    autoload = '_AItelierProbe="*res://_aitelier_probe.gd"'
    if "[autoload]" in text:
        text = text.replace("[autoload]", "[autoload]\n" + autoload, 1)
    else:
        text += "\n[autoload]\n" + autoload + "\n"
    pg.write_text(text)


def _capture_frames(total: int, timeline: list | None = None) -> list[int]:
    """Which frames to photograph. Assert frames have PRIORITY over the stride
    (a PNG earns its bandwidth by showing the very state an assertion judged),
    and when there are more of them than there is budget they are sampled
    EVENLY ACROSS the scenario rather than taken from its head — see below. The
    stride only spends whatever budget is left, so a run with no asserts still
    comes back with a filmstrip instead of nothing.

    Never schedules the last frame: the probe calls _finish() and quit() from
    _process once _frame >= _max, so that frame's post-draw never fires and the
    JSON would name a PNG that was never written."""
    limit = min(PLAYTEST_CAPTURES, _MAX_CAPTURES)
    last = total - 2
    if limit <= 0 or last < 0:
        return []
    asserted = [int(e.get("at", 0)) for e in (timeline or []) if e.get("assert")]
    # SPREAD them, do not take the head. The loop below stops at `limit`, so
    # taking assert frames in timeline order photographs a scenario's OPENING
    # and nothing else — and the more assertions a scenario carries, the smaller
    # the fraction of it the vision gate can see. That is backwards: a scenario
    # is almost always "do X, then verify the result", so its subject is at the
    # END.
    #
    # Live, jinyong-facility 2026-08-29: `facility_use_reusable` carries 16
    # assert frames spanning 400..810. The facility — the entire deliverable of
    # that round — is used at 530..810. The four captured frames were
    # [400, 440, 460, 500]: two map screens and an event. The vision judge was
    # shown a walk to the node and asked whether the round's feature was
    # readable. Worse, `map_node_event_shaolin` shares that prologue and so has
    # the same first four assert frames, and the two scenarios came back with
    # BYTE-IDENTICAL frame sets — the gate spent its budget judging one picture
    # twice while the thing under test was never photographed.
    #
    # Sampling evenly across the assert range keeps both endpoints, so the last
    # assertion — the one that says the feature finally did the thing — always
    # gets its picture.
    if limit == 1:
        asserted = asserted[-1:]
    elif len(asserted) > limit:
        asserted = [asserted[round(i * (len(asserted) - 1) / (limit - 1))]
                    for i in range(limit)]
    picked = asserted
    stride = max(1, total // (limit + 1))
    picked += [i * stride for i in range(1, limit + 1)]
    out: list[int] = []
    for f in picked:
        f = min(max(f, 0), last)
        if f not in out:
            out.append(f)
        if len(out) == limit:
            break
    return sorted(out)


def _probe_once(args: list[str], env: dict, state_path: Path, timeout: int,
                render: bool) -> tuple[dict, list, bool]:
    if state_path.exists():
        state_path.unlink()
    try:
        cp = _run(args, timeout=timeout, extra_env=env, render=render)
        stderr, timed_out = cp.stderr, False
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        timed_out = True
    except FileNotFoundError as e:
        # Render mode shells out to xvfb-run; if the image lacks it there is no
        # run at all. The caller's headless retry is what keeps the gate alive.
        if not render:
            raise
        stderr, timed_out = str(e), False
    errs = [e for e in _parse_errors(stderr) if e["kind"] in ("runtime", "push_error", "parse", "load")]
    probe = {}
    if state_path.is_file():
        try:
            probe = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            probe = {}
    return probe, errs, timed_out


def _attach_pngs(captures: list, cap_dir: Path) -> list:
    """Inline each captured PNG as base64 and keep only its basename: the sidecar
    mounts the workspace read-only, so the bytes have to ride home in the JSON
    body, and the container-local path means nothing to the caller."""
    out = []
    for c in captures:
        png = cap_dir / Path(str(c.get("file", ""))).name
        if png.is_file():
            out.append({"frame": c.get("frame"), "file": png.name,
                        "png_b64": base64.b64encode(png.read_bytes()).decode()})
    return out


def _run_probe(dst: Path, state_path: Path, frames: int, timeout: int,
               extra: dict, scene: str = "",
               capture_at: list[int] | None = None) -> tuple[dict, list, bool]:
    """One probe run. Returns (probe_report, errors, timed_out) — the captures
    ride inside probe_report, because callers (and the unit tests that fake this)
    depend on the 3-tuple."""
    args = ["--path", str(dst)]
    if scene:
        args.append(scene)              # run a specific scene instead of main
    env = {"AITELIER_PROBE_OUT": str(state_path), "AITELIER_PROBE_FRAMES": str(frames)}
    env.update(extra)
    render = bool(capture_at)
    cap_dir = dst.parent / "captures"
    if render:
        shutil.rmtree(cap_dir, ignore_errors=True)
        cap_dir.mkdir(parents=True, exist_ok=True)
        env["AITELIER_PROBE_CAPTURE"] = str(cap_dir)
        env["AITELIER_PROBE_CAPTURE_AT"] = ",".join(str(f) for f in capture_at)
    probe, errs, timed_out = _probe_once(args, env, state_path, timeout, render)
    if render and not probe:
        # A broken X/GL setup must degrade to yesterday's behaviour, not take the
        # whole playtest gate down: retry once, headless, with capture off.
        env.pop("AITELIER_PROBE_CAPTURE")
        env.pop("AITELIER_PROBE_CAPTURE_AT")
        render = False
        probe, errs, timed_out = _probe_once(args, env, state_path, timeout, False)
    if probe:
        # Report which mode actually produced this, so a silent fallback to the
        # pixel-blind path is visible rather than looking like "no captures".
        probe["render_mode"] = "render" if render else "headless"
        probe["captures"] = _attach_pngs(probe.get("captures", []), cap_dir) if render else []
    return probe, errs, timed_out


def _playtest_legacy(dst: Path, frames: int, input_action: str, timeout: int) -> dict:
    """The old canned smoke test: run the main scene auto-pressing one action,
    snapshot the end state. HARD-fails only on crash / didn't-run."""
    state_path = dst.parent / "probe_state.json"
    probe, errs, timed_out = _run_probe(
        dst, state_path, frames, timeout, {"AITELIER_PROBE_INPUT": input_action},
        capture_at=_capture_frames(frames))
    ran = bool(probe) or not timed_out
    passed = not errs and ran
    if not ran:
        summary = "Playtest could not run the scene (no probe snapshot)."
    elif passed:
        summary = "Playtest ran %d frames cleanly, no runtime errors." % probe.get("frames", frames)
    else:
        summary = "Playtest surfaced %d runtime error(s)." % len(errs)
    return {"passed": passed, "frames": probe.get("frames", frames), "errors": errs,
            "state": probe.get("nodes", {}), "behavior": None,
            "captures": probe.get("captures", []),
            "render_mode": probe.get("render_mode", "headless"),
            "spec_used": False, "summary": summary}


_CMP_OPS = ("==", "!=", "<=", ">=", "<", ">", " and ", " or ", " in ", " not ")


_DELTA_MODES = ("changed", "unchanged")


def _normalize_asserts(raw) -> list:
    """Accept BOTH assertion shapes and return the probe's ``[{node, expr, name}]``.

    LLM-authored specs use an ergonomic DICT — ``"Node.attr.path": <value>`` —
    which we normalise here (the probe stays a simple {node, expr} evaluator):
      * value is a bool/number      → equality on the attr path (``attr == value``)
      * value is a comparison string → used verbatim as the expression
      * value is a plain string      → string-literal equality (``attr == "value"``)
    The node is the key up to the FIRST dot (node names use ``/`` for scene paths,
    so ``HUD/PausedLabel.visible`` splits cleanly into node + ``visible``). A LIST
    already in ``{node, expr}`` form passes through unchanged."""
    if isinstance(raw, list):
        return raw
    out = []
    if isinstance(raw, dict):
        for key, val in raw.items():
            node, _, attr = str(key).partition(".")
            if isinstance(val, str) and val.strip().lower() in _DELTA_MODES:
                # Differential assertion: did this value MOVE since the scenario
                # started? A presence check ("visible == true") passes on a game
                # that ignores every keypress. This one cannot.
                out.append({"node": node, "attr": attr, "name": str(key),
                            "mode": val.strip().lower()})
                continue
            if isinstance(val, bool):
                expr = f"{attr} == {'true' if val else 'false'}"
            elif isinstance(val, (int, float)):
                expr = f"{attr} == {val}"
            elif isinstance(val, str) and any(op in val for op in _CMP_OPS):
                expr = val                       # already a boolean expression
            else:
                expr = f'{attr} == "{val}"'      # string-literal equality
            # Carry `attr` on the expression form too (the delta form already
            # does): the probe needs it to read the value back when the
            # comparison fails. See _eval_assert.
            out.append({"node": node, "attr": attr, "expr": expr,
                        "name": str(key)})
    return out


_TIMELINE_KEYS = {"at", "press", "release", "actions", "assert", "click", "clicks",
                  "hover", "hovers"}
_MAX_SPEC_FRAMES = 3000   # safety cap on how long one scenario may run


def _normalize_timeline(timeline: list) -> tuple[list, list]:
    """Normalise one scenario's timeline and REJECT anything unrecognised.

    Returns ``(entries, errors)``. Input comes in two accepted shapes:
    ``press: <action>`` (one action) and ``actions: [<a>, <b>]`` (several on the
    same frame, expanded here into one ``press`` entry each).

    An unknown key is an ERROR, never a silent skip. Both shipped game specs put
    ``actions:`` INSIDE timeline entries while the probe only ever read
    ``press:``; every entry was dropped, no input was ever delivered, and each
    scenario still passed because its assertions only checked that UI nodes
    existed. Ignoring a key we do not understand is how a gate ends up grading a
    game nobody played."""
    out, errors = [], []
    for i, e in enumerate(timeline or []):
        if not isinstance(e, dict):
            errors.append("timeline entry %d is %s, expected a mapping"
                          % (i, type(e).__name__))
            continue
        unknown = sorted(set(e) - _TIMELINE_KEYS)
        if unknown:
            errors.append("timeline entry %d (at: %s) has unknown key(s) %s - allowed: %s"
                          % (i, e.get("at", "?"), ", ".join(unknown),
                             ", ".join(sorted(_TIMELINE_KEYS))))
            continue
        # `at` must be an int the whole pipeline can do arithmetic on. It was
        # not checked here, so a shorthand copied out of a notes table --
        # `at: 3..15`, `at: 20/25/30` -- reached int() deep inside the run and
        # raised an unhandled ValueError, which the HTTP layer turned into a
        # bare 500. On 2026-08-25 that cost a round its measurement: nine 500s
        # in a row (including a trivial connectivity probe that reused the same
        # broken prologue) read as "the builder service is down", so the probe
        # was recorded as BLOCKED and the defect it existed to measure went
        # unmeasured. A malformed scenario must say it is malformed.
        at_raw = e.get("at", 0)
        if isinstance(at_raw, bool) or not isinstance(at_raw, (int, float)):
            errors.append(
                "timeline entry %d has a non-numeric `at`: %r. Frames are single "
                "integers -- a range or a list is not supported, write one entry "
                "per frame (`- {at: 3, ...}` … `- {at: 15, ...}`)." % (i, at_raw))
            continue
        at = int(at_raw)
        if at < 0:
            errors.append("timeline entry %d has a negative `at`: %r" % (i, at_raw))
            continue
        acts = e.get("actions") or []
        if isinstance(acts, str):
            acts = [acts]
        clicks = e.get("clicks") or []
        if isinstance(clicks, str):
            clicks = [clicks]
        hovers = e.get("hovers") or []
        if isinstance(hovers, str):
            hovers = [hovers]
        base = {k: v for k, v in e.items()
                if k not in ("actions", "clicks", "hovers")}
        if "assert" in base:
            base["assert"] = _normalize_asserts(base["assert"])
        # The probe fires every entry whose `at` matches the frame, so several
        # presses on one frame are simply several entries. `clicks:` is the
        # plural of `click:` exactly as `actions:` is the plural of `press:`.
        for a in acts:
            out.append({"at": at, "press": a})
        for c in clicks:
            out.append({"at": at, "click": c})
        for h in hovers:
            out.append({"at": at, "hover": h})
        # `click` MUST be in this condition. Without it an entry carrying both
        # `actions:` and `click:` would drop the click on the floor -- the same
        # silent-skip that made every shipped `actions:` entry vanish before
        # this function existed. An input the spec asked for and the probe never
        # delivered is indistinguishable from a game that ignored it.
        if (base.get("press") or base.get("release") or base.get("click")
                or base.get("hover") or base.get("assert")
                or not (acts or clicks or hovers)):
            out.append(base)
    return out, errors


def _digest(nodes: dict) -> dict:
    """Node state minus the probe's own bookkeeping - its frame counter and
    capture paths differ between any two runs, which would defeat the
    no-input comparison in _playtest_spec."""
    return {k: v for k, v in (nodes or {}).items() if "_AItelierProbe" not in k}


def _playtest_spec(dst: Path, spec: dict, frames: int, timeout: int) -> dict:
    """Authored-spec playtest: run ONE isolated headless pass per scenario, driving
    its input timeline and evaluating its Expression assertions against live nodes.

    Gate split: ``passed`` (HARD, loops the goal-loop) covers crash / didn't-run,
    plus the two ways a scenario can look green without testing anything -- a
    malformed timeline, and input that never reached the game. Per-scenario
    assertion outcomes stay ADVISORY (``behavior``) so a wrong or flaky assertion
    can never stall a build that otherwise runs clean."""
    scene = str(spec.get("scene", "") or "")
    default_frames = int(spec.get("frames", frames) or frames)
    scenarios = spec.get("scenarios") or []
    state_path = dst.parent / "probe_state.json"
    spec_path = dst.parent / "scenario_spec.json"

    scen_results, all_errors, captures, spec_errors = [], [], [], []
    scen_nodes: list[dict] = []
    scen_frames: list[int] = []
    ran_any = crashed = False
    last_state: dict = {}
    render_mode = "headless"
    for i, sc in enumerate(scenarios):
        name = str(sc.get("name", "scenario"))
        timeline, terrs = _normalize_timeline(sc.get("timeline") or [])
        spec_errors.extend("scenario %r: %s" % (name, m) for m in terrs)
        max_at = max([int(e.get("at", 0)) for e in timeline], default=0)
        # Run long enough to REACH the last timeline event (+margin) -- default_frames
        # is a floor, not a ceiling. Capped for safety. Truncating here would drop a
        # scenario's late assertions (e.g. one that checks at frame 300).
        want = max(max_at + 30, default_frames) if timeline else default_frames
        sframes = min(want, _MAX_SPEC_FRAMES)
        if want > _MAX_SPEC_FRAMES:
            # The cap is a safety limit, not a truncation the author consented
            # to: an assertion scheduled past it simply never fires, vanishes
            # from asserts[], and `all(a.passed)` then holds over whatever did
            # run. A scenario losing its terminal assertion must not read as a
            # scenario that passed it.
            dropped = sorted({int(e.get("at", 0)) for e in timeline
                              if e.get("assert") and int(e.get("at", 0)) >= _MAX_SPEC_FRAMES})
            if dropped:
                spec_errors.append(
                    "scenario %r: assertion(s) scheduled at frame(s) %s, past the "
                    "%d-frame cap - they would never be evaluated. Reach the same "
                    "state sooner, or assert earlier."
                    % (name, ", ".join(str(d) for d in dropped), _MAX_SPEC_FRAMES))
        spec_path.write_text(json.dumps({"frames": sframes, "timeline": timeline}))
        # Per-scenario scene override. `run_godot` has always been able to boot
        # a specific scene instead of main; only the SPEC-level scene was ever
        # wired to it, so all 27 scenarios booted main.tscn and each one paid
        # the full boot preamble (7x ui_accept to clear the tutorial dialogs)
        # before it could assert anything about, say, the creation screen.
        # Since every scenario already gets its OWN fresh Godot process, letting
        # it name its own scene is what turns this harness into unit-level
        # testing: boot creation.tscn, inject, assert, done in tens of frames.
        # It also decouples a scenario from the BOOT FLOW — a scenario that
        # boots its own scene does not shift when a menu is inserted ahead of
        # the tutorial, which is otherwise a 27-file rewrite, and this repo has
        # already lost assertions to one of those.
        sc_scene = str(sc.get("scene") or scene)
        # ── EVERY SCENARIO GETS ITS OWN user:// ────────────────────────────
        # Godot derives user:// from $HOME, and $HOME was the container's, so
        # every scenario in every sweep on every tree shared one save
        # directory: app_userdata/<project>/{save_1.json, settings.cfg, ...}.
        # A scenario that saves therefore changed what the NEXT one booted
        # into — and what the next SWEEP booted into.
        #
        # That is the "flake" (measured 2026-09-04): the same unchanged tree
        # gave 0 red, then 1 red, then 6 red, with disjoint red sets, and
        # `menu_load_continues` failed its `load_available: changed` assert
        # with baseline true / current true — the frame-0 baseline had a save
        # left over from an earlier scenario. Order-dependence, not chance.
        sc_home = tempfile.mkdtemp(prefix="godot_home_")
        try:
            probe, errs, timed_out = _run_probe(
                dst, state_path, sframes, timeout,
                {"AITELIER_PROBE_SPEC": str(spec_path), "HOME": sc_home},
                scene=sc_scene, capture_at=_capture_frames(sframes, timeline))
        finally:
            shutil.rmtree(sc_home, ignore_errors=True)
        ran = bool(probe) or not timed_out
        ran_any = ran_any or ran
        if errs:
            crashed = True
        all_errors.extend({**e, "scenario": name} for e in errs)
        asserts = probe.get("asserts", [])
        scen_passed = ran and not errs and bool(asserts) and all(a.get("passed") for a in asserts)
        scen_results.append({"name": name, "ran": ran, "errors": errs,
                             "asserts": asserts, "passed": scen_passed,
                             "pressed": any(e.get("press") for e in timeline),
                             "input_dead": False})
        scen_nodes.append(_digest(probe.get("nodes", {})))
        scen_frames.append(sframes)
        last_state = probe.get("nodes", last_state)
        render_mode = probe.get("render_mode", render_mode)
        # Every scenario re-runs from frame 0, so basenames collide across them --
        # prefix with the scenario index and tag with its name.
        captures.extend({**c, "scenario": name, "file": "s%d_%s" % (i, c["file"])}
                        for c in probe.get("captures", []))

    # -- L0: did the input reach the game at all? ----------------------------
    # A scenario that presses keys and ends in EXACTLY the state an untouched run
    # reaches tested nothing, however green its assertions look. One extra probe
    # pass per distinct frame budget buys the answer -- headless, no captures, so
    # it is the cheapest run in the file. This is the gate that would have caught
    # the `actions:`-vs-`press:` mismatch on day one instead of two games later.
    driven = [i for i, r in enumerate(scen_results) if r["pressed"] and r["ran"]]
    controls: dict[int, dict] = {}
    if driven and not crashed:
        for i in driven:
            n = scen_frames[i]
            if n not in controls:
                spec_path.write_text(json.dumps({"frames": n, "timeline": []}))
                # The control is a RUN, so it needs the same throwaway user://
                # every scenario gets. It was the one probe call still on the
                # container's HOME: measured 2026-09-05 on the wuxia tree, the
                # game's user:// logs landed in the sidecar's shared home at the
                # end of each request, and only the control passes were there.
                # A control that boots into a save an earlier control left is
                # not the no-input baseline this comparison claims to be.
                ctrl_home = tempfile.mkdtemp(prefix="godot_home_")
                try:
                    ctrl, _e, _t = _run_probe(dst, state_path, n, timeout,
                                              {"AITELIER_PROBE_SPEC": str(spec_path),
                                               "HOME": ctrl_home},
                                              scene=scene)
                finally:
                    shutil.rmtree(ctrl_home, ignore_errors=True)
                controls[n] = _digest(ctrl.get("nodes", {}))
            # An empty control means the control pass itself failed to report --
            # stay quiet rather than accuse the game on missing evidence.
            if controls[n] and scen_nodes[i] == controls[n]:
                scen_results[i]["input_dead"] = True
                scen_results[i]["passed"] = False

    dead = [r["name"] for r in scen_results if r["input_dead"]]
    behavior_passed = bool(scen_results) and all(s["passed"] for s in scen_results)
    hard_passed = ran_any and not crashed and not spec_errors and not dead
    n_fail = sum(1 for s in scen_results if not s["passed"])
    if spec_errors:
        summary = ("Playtest HARD-failed: %d malformed timeline entr%s -- %s"
                   % (len(spec_errors), "y" if len(spec_errors) == 1 else "ies",
                      spec_errors[0]))
    elif not ran_any or crashed:
        summary = ("Playtest HARD-failed: %s."
                   % ("runtime error(s)" if crashed else "scene did not run"))
    elif dead:
        summary = ("Playtest HARD-failed: scenario(s) %s pressed input and ended in "
                   "EXACTLY the state a no-input control run reaches -- the game "
                   "never received it. Check the action names against "
                   "project.godot [input] and how the game reads them."
                   % ", ".join(repr(d) for d in dead))
    elif behavior_passed:
        summary = "Playtest ran %d scenario(s); all assertions passed." % len(scen_results)
    else:
        summary = ("Playtest ran clean but %d/%d scenario(s) failed assertions (advisory)."
                   % (n_fail, len(scen_results)))
    return {"passed": hard_passed, "frames": default_frames, "errors": all_errors,
            "state": last_state, "spec_used": True, "spec_errors": spec_errors,
            "captures": captures, "render_mode": render_mode,
            "behavior": {"all_passed": behavior_passed, "scenarios": scen_results},
            "summary": summary}


def playtest_project(project_dir: str, frames: int = DEFAULT_PLAYTEST_FRAMES,
                     input_action: str = "ui_accept", spec: dict | None = None,
                     timeout: int = 120) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "behavior": None, "spec_used": False, "no_project": True,
                "summary": "No Godot project — playtest skipped."}
    dst = _copy_project(proj)
    try:
        _inject_probe(dst)
        _import_resources(dst, timeout)
        if spec and isinstance(spec.get("scenarios"), list) and spec["scenarios"]:
            return _playtest_spec(dst, spec, frames, timeout)
        return _playtest_legacy(dst, frames, input_action, timeout)
    finally:
        shutil.rmtree(dst.parent, ignore_errors=True)


def _import_resources(dst: Path, timeout: int) -> None:
    """Build the import cache before running the scene.

    `_copy_project` strips `.godot/`, and Godot resolves a texture or a sound
    through that cache — an un-imported PNG makes `ExtResource("bgtex")` resolve
    to nothing, so the Sprite2D draws NOTHING and the run still exits cleanly
    with zero errors. A game whose art had been replaced by real files therefore
    play-tested as a flat grey screen and passed. The compile gate already
    imports, but on its own temp copy, which it then deletes; this path needs its
    own. Best-effort: a project with no importable resources (the primitives-only
    case this harness was written for) is unaffected either way."""
    try:
        _run(["--path", str(dst), "--import"], timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


# A single-file --check-only run has no project.godot, so it cannot see
# autoloads, res:// paths or sibling classes. Every one of these diagnostics
# means "I cannot see the rest of the project" — NOT a defect in the file.
# Measured on a working 21-script repo: 17 of 21 files "failed", every failure
# one of these three. Ignoring them makes this a SYNTAX gate, which is exactly
# the defect class it exists for (structure, indentation, unbalanced blocks).
# Cross-file resolution is 5_compile's job — it imports the whole project once,
# with autoloads live, and it is the gate that catches a typo'd identifier.
_RESOLUTION_ERRORS = (
    "not declared in the current scope",
    "Could not resolve super class path",
    "Preload file",
    "Could not find type",
    "Could not resolve external class member",
)


def _is_resolution_error(line: str) -> bool:
    return any(frag in line for frag in _RESOLUTION_ERRORS)


def check_gdscript(files: list[str], timeout: int = 120) -> dict:
    """Parse-check GDScript files one at a time — the PER-TASK syntax gate.

    ``--check-only --script`` parses ONE file with no project import, which is
    why this can run after every implementation step where the full
    ``compile_project`` (copy the project, import every resource, boot the
    engine) is far too heavy. Without it a GDScript syntax error survives the
    whole task loop and only surfaces at the end, because the base pipeline's
    importability validation globs ``*.py`` and a Godot project has none: run
    jinyong-play shipped a corrupted BFS loop (tab depths 3/4/5/7 plus a
    duplicated guard) that no gate caught — only a reviewer reading the
    indentation by eye rejected it.

    Returns the shape StepValidator reads: ``{all_passed, results: [{file,
    passed, error_message}]}``."""
    results, unseen = [], []
    for f in files or []:
        fp = Path(f)
        if not fp.is_file():
            # The caller globbed this path and could stat it; if this process
            # cannot, its view of the workspace is broken (a bind mount whose
            # source was replaced keeps resolving to the old, unlinked inode).
            # Dropping it silently turns "checked nothing" into a clean pass.
            unseen.append(str(fp))
            continue
        try:
            cp = _run(["--check-only", "--script", str(fp)], timeout=timeout)
        except subprocess.TimeoutExpired:
            results.append({"file": str(fp), "passed": False,
                            "error_message": "godot --check-only timed out"})
            continue
        if cp.returncode == 0:
            results.append({"file": str(fp), "passed": True, "error_message": ""})
            continue
        # Keep the parse diagnosis and the res:// line number, drop the engine
        # banner and the generic "Failed to load script" tail.
        lines = [ln.strip() for ln in cp.stderr.splitlines() if "Parse Error" in ln]
        real = [ln for ln in lines if not _is_resolution_error(ln)]
        if not real:
            # Every complaint was "I cannot see the rest of the project", which
            # is true and expected: one file, no project.godot. Not a defect.
            results.append({"file": str(fp), "passed": True, "error_message": ""})
            continue
        results.append({"file": str(fp), "passed": False,
                        "error_message": " ".join(real)[:800]})
    if unseen:
        return {"all_passed": False, "results": results, "unseen": unseen,
                "error_message": (
                    "%d of %d file(s) are not visible to the godot sidecar "
                    "(first: %s) — its workspace mount is stale; recreate the "
                    "container." % (len(unseen), len(files or []), unseen[0]))}
    return {"all_passed": all(r["passed"] for r in results), "results": results}


# ── script gate (GDScript unit suite) ──────────────────────────
# Godot's own SCRIPT ERROR / stack-dump lines, plus the conventional markers a
# hand-rolled GDScript runner prints. The exit code alone is NOT enough: Godot
# exits 0 on an uncaught script error, so a suite that printed FAILED and died
# would otherwise be recorded as a pass.
_FAILURE_MARKERS = ("SCRIPT ERROR", "Stack frames", "--- Debugging parse error",
                    "FAILED:", "FAIL:", "ASSERT FAILED", "Assertion failed")


def _has_failure_marker(out: str, err: str) -> bool:
    blob = (out or "") + (err or "")
    return any(m in blob for m in _FAILURE_MARKERS)


def _discover_entry_points(proj: Path) -> list:
    """Every `tests/*.gd` that `extends SceneTree` — the scripts `-s` can run.

    Discovered, not configured, because a hard-coded list goes stale silently:
    the caller keeps passing five names while the project grows a sixth suite,
    and the new one is never run by anything. `extends SceneTree` is the exact
    property `-s` requires, so it is also the exact right filter — a plain
    `test_*.gd` glob would sweep in the 12 static test files that the runner
    script collects, and running one of those directly is an error, not a test
    failure.
    """
    tests = proj / "tests"
    if not tests.is_dir():
        return []
    found = []
    for f in sorted(tests.glob("*.gd")):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "extends SceneTree" in head:
            found.append("res://tests/" + f.name)
    return found


def _script_log_excerpt(text: str) -> str:
    """Keep the first diagnostic and final summary within the 4000-char budget."""
    if len(text) <= 4000:
        return text
    marker = "\n... [middle truncated] ...\n"
    remaining = 4000 - len(marker)
    head = remaining // 2
    return text[:head] + marker + text[-(remaining - head):]


def run_script(project_dir: str, scripts: list, timeout: int = 600) -> dict:
    """Run ``godot --headless --path <proj> -s <res://...>`` for each script.

    The GDScript unit suite is the project's fastest, most targeted feedback,
    and it was DEAD: ``run_tests.sh`` shells out to a bare ``godot``, and there
    is no godot binary in the aitelier container -- only in this sidecar. Every
    round the unit gate failed with "godot binary not found", 5_review blocked
    on it, and the PM planned a repair the implementer could not possibly make:
    no amount of PATH resolution finds a binary that is not in the filesystem.
    Give the suite the same HTTP route /compile and /playtest already use.
    """
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "no_project": True, "results": [],
                "summary": "No project.godot -- not a Godot project; script gate skipped."}
    scripts = list(scripts or []) or _discover_entry_points(proj)
    if not scripts:
        return {"passed": True, "results": [], "discovered": [],
                "summary": "No `extends SceneTree` entry point under tests/."}

    dst = _copy_project(proj)
    try:
        # The suite loads scenes and resources exactly like the game does, so it
        # needs the same import cache the play-test builds.
        _import_resources(dst, timeout=min(timeout, 300))
        results = []
        for rel in scripts:
            # ── EVERY ENTRY POINT GETS ITS OWN user:// ────────────────────
            # Godot derives user:// from $HOME, and $HOME was the container's,
            # so every entry point in every request wrote its saves and
            # settings into one app_userdata/<project>/: a suite that saves
            # changed what the NEXT suite booted into, and — because the
            # container outlives a request — what the next REQUEST booted
            # into. Same order-dependence the play-test fixed per scenario
            # above; the fix is the same, one throwaway HOME per invocation.
            sc_home = tempfile.mkdtemp(prefix="godot_home_")
            try:
                cp = _run(["--path", str(dst), "-s", rel], timeout=timeout,
                          extra_env={"HOME": sc_home})
                rc, out, err = cp.returncode, cp.stdout, cp.stderr
            except subprocess.TimeoutExpired as e:
                # TimeoutExpired CARRIES the output produced before the kill —
                # discarding it blinded the gate at the one moment its output
                # matters most. A GDScript suite that hangs does so because a
                # runtime error aborted the function holding the final quit():
                # the SceneTree keeps spinning and the process runs to the wall.
                # The error, and every PASS/FAIL printed before it, are already
                # in that buffer. Live, jinyong-usable 2026-08-23:
                # test_game_manager_fsm.gd reported rc=124 with an EMPTY stdout,
                # so the report said only "it hung" about a run that had already
                # said where and why.
                def _s(v):
                    return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
                rc = 124
                out = _s(e.stdout)
                err = (_s(e.stderr) + "\ntimed out after %ss" % timeout).lstrip()
            finally:
                # Pass, fail, timeout or an error nobody predicted: the home
                # goes, or the "throwaway" one accumulates in the sidecar.
                shutil.rmtree(sc_home, ignore_errors=True)
            failed = rc != 0 or _has_failure_marker(out, err)
            results.append({"script": rel, "returncode": rc, "passed": not failed,
                            "stdout": _script_log_excerpt(out),
                            "stderr": _script_log_excerpt(err),
                            "stdout_truncated": len(out) > 4000,
                            "stderr_truncated": len(err) > 4000,
                            "errors": _parse_errors(err)})
        ok = all(r["passed"] for r in results)
        bad = [r["script"] for r in results if not r["passed"]]
        return {"passed": ok, "results": results, "discovered": scripts,
                "summary": ("%d script(s) ran, all passed." % len(results) if ok
                            else "%d/%d script(s) failed: %s"
                                 % (len(bad), len(results), ", ".join(bad)))}
    finally:
        shutil.rmtree(dst.parent, ignore_errors=True)


# ── HTTP transport (mirrors the Unity sidecar) ─────────────────────────────


# ── The windowed input gate ────────────────────────────────────────────────
#
# WHY THIS EXISTS. Everything else in this file delivers input with Godot's
# `Input.parse_input_event()`. That injects below the window layer and never
# reaches the GUI phase, where a `mouse_filter = STOP` Control decides whether
# an event survives to `_unhandled_input`. So no `clicks:` scenario can fail
# the way a real click fails.
#
# On 2026-08-27 that blind spot cost jinyong-assets its primary interaction:
# `menu.tscn`'s full-rect `SegmentHost` was missing `mouse_filter = 2`, sat
# over the board for the whole session, and swallowed every mouse press that
# did not land on a Button. Left-click movement was dead for every player, on
# web and on desktop, while the 57-scenario suite reported 57/57. It was the
# second time — the same node in the sibling scene had the same bug — and the
# contract could not see either, because the contract boots the sibling.
#
# This gate runs the game in a REAL X11 window on Xvfb and drives it with REAL
# events via xdotool. It is the only path here that exercises OS event ->
# window -> engine -> GUI phase -> handler -> state change.
#
# WHAT THE GAME MUST PROVIDE. The gate is deliberately dumb: it does not know
# how to navigate the game, and it must not, because navigation is not the
# layer under test. The project supplies an autoload that, when
# `AITELIER_INPUT_GATE_REPORT` is set, drives itself to the state under test by
# its own internal calls (never by synthesizing input) and rewrites that path
# with a JSON object:
#
#   {"ready": true,               # the state under test has been reached
#    "player_world": [x, y],      # where to aim, in VIEWPORT pixels
#    "grid": "(7, 5)", "moves_left": 4,
#    "raw_left": 0, "handled_left": 0,     # presses reaching _input / the handler
#    "raw_right": 0, "handled_right": 0,
#    "eater": ""}                 # topmost non-IGNORE Control under the last press
#
# A report that never turns ready is a SKIP with a reason, never a pass: the
# caller records it as an open coverage gap. Everything this gate asserts is a
# differential (a counter moved, a tile changed), so it does not care what the
# board looks like.



def _free_display() -> str:
    """A display number nobody is using.

    Not a fixed one: killing whatever holds a hard-coded display would abort a
    concurrent gate run and report its half-finished counters as a verdict. The
    playtest path uses , which allocates the same way.
    """
    for n in range(90, 100):
        if not Path("/tmp/.X%d-lock" % n).exists():
            return ":%d" % n
    raise RuntimeError("no free X display in :90-:99")


def _xvfb_up(display: str, size: str = "960x704x24"):
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", size],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    return proc


def _read_gate_report(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _await_ready(path: Path, deadline: float) -> dict:
    while time.time() < deadline:
        rep = _read_gate_report(path)
        if rep.get("ready"):
            return rep
        time.sleep(0.5)
    return _read_gate_report(path)


def _xdo(display: str, *args) -> None:
    env = dict(os.environ, DISPLAY=display)
    subprocess.run(["xdotool", *args], env=env, capture_output=True, timeout=20)


def x11_input_smoke(project_dir: str, timeout: int = 180) -> dict:
    """Drive the game in a real window with real X11 events.

    Returns {passed, skipped, reason, steps[]} — `skipped` is NOT a pass; the
    caller must surface it as an open coverage gap.
    """
    out: dict = {"passed": False, "skipped": False, "reason": "", "steps": []}
    src = Path(project_dir)
    if not (src / "project.godot").is_file():
        out.update(skipped=True, reason="no project.godot at %s" % src)
        return out
    if shutil.which("xdotool") is None:
        out.update(skipped=True, reason=(
            "xdotool is not in this image — the windowed gate cannot inject "
            "real events. Rebuild the sidecar (Dockerfile.godot installs it); "
            "installing it into the running container does not survive."))
        return out

    # The workspace mount is read-only in this container and `--import` writes
    # res://.godot, so the project has to be copied somewhere writable first.
    work = Path(tempfile.mkdtemp(prefix="x11gate-"))
    proj = work / "proj"
    shutil.copytree(src, proj, ignore=shutil.ignore_patterns(".git"))
    report = work / "gate.json"

    # Two import passes. Without them every resource fails to load, `preload()`
    # turns into a parse error, and the autoloads never come up — which reads
    # as "the game is broken" rather than "the project was not imported".
    for _ in range(2):
        subprocess.run([GODOT_BIN, "--headless", "--path", str(proj), "--import"],
                       capture_output=True, timeout=180)

    display = _free_display()
    xvfb = _xvfb_up(display)
    env = dict(os.environ, DISPLAY=display,
               AITELIER_INPUT_GATE_REPORT=str(report))
    game = subprocess.Popen(
        [GODOT_BIN, "--path", str(proj), "--resolution", "960x704", "--position", "0,0"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        deadline = time.time() + timeout
        rep = _await_ready(report, min(deadline, time.time() + 90))
        if not rep.get("ready"):
            out.update(skipped=True, reason=(
                "the project published no input-gate report (looked for "
                "AITELIER_INPUT_GATE_REPORT). The game side of the gate is not "
                "wired, so the real input path is NOT covered."))
            return out

        px, py = (int(v) for v in rep.get("player_world", [0, 0]))
        out["steps"].append({"stage": "ready", **rep})

        # LEFT click on the empty tile one above the actor: a real press, at a
        # real screen coordinate, through the real window.
        _xdo(display, "mousemove", str(px), str(py - 64))
        time.sleep(0.5)
        _xdo(display, "click", "1")
        time.sleep(2.5)
        after_left = _read_gate_report(report)
        out["steps"].append({"stage": "after_left_click", **after_left})

        moved = after_left.get("grid") != rep.get("grid")
        arrived = after_left.get("raw_left", 0) > rep.get("raw_left", 0)
        handled = after_left.get("handled_left", 0) > rep.get("handled_left", 0)

        # RIGHT click on the actor's own tile: the undo path, and the exact
        # point a floating health bar used to swallow.
        _xdo(display, "mousemove", str(px), str(py))
        time.sleep(0.5)
        _xdo(display, "click", "3")
        time.sleep(2.5)
        after_right = _read_gate_report(report)
        out["steps"].append({"stage": "after_right_click", **after_right})

        undone = after_right.get("grid") == rep.get("grid")
        r_arrived = after_right.get("raw_right", 0) > after_left.get("raw_right", 0)
        r_handled = after_right.get("handled_right", 0) > after_left.get("handled_right", 0)

        fails = []
        if not arrived:
            fails.append("the left press never reached the actor node (raw_left "
                         "did not move) — it died before the engine")
        elif not handled:
            fails.append("the left press reached the actor but never reached the "
                         "click handler (handled_left did not move) — the GUI "
                         "phase ate it; eater=%r" % after_left.get("eater", ""))
        elif not moved:
            fails.append("the click was handled but the actor did not move "
                         "(grid %s -> %s) — a gate or the coordinate transform"
                         % (rep.get("grid"), after_left.get("grid")))
        if not r_arrived:
            fails.append("the right press never reached the actor node")
        elif not r_handled:
            fails.append("the right press reached the actor but never reached "
                         "the undo handler; eater=%r" % after_right.get("eater", ""))
        elif not undone:
            fails.append("undo did not restore the tile (%s, expected %s)"
                         % (after_right.get("grid"), rep.get("grid")))

        out["passed"] = not fails
        out["reason"] = ("a real window, real events: click moved and "
                         "right-click undid it" if not fails else " | ".join(fails))
        return out
    finally:
        for pr in (game, xvfb):
            try:
                pr.terminate()
                pr.wait(timeout=5)
            except Exception:
                try:
                    pr.kill()
                except Exception:
                    pass
        shutil.rmtree(work, ignore_errors=True)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "engine": "godot", "bin": GODOT_BIN})
        else:
            self._send(404, {"error": "not found"})

    # ── ONE RENDER AT A TIME ────────────────────────────────────────────
    # ThreadingHTTPServer answers concurrent requests, and the render routes
    # (/playtest, /script, /x11_input_smoke) each start their own Xvfb and
    # drive Godot on software GL. Two of them on one box do not fail — they
    # SLOW EACH OTHER DOWN, and a timing-sensitive scenario then reports a
    # red that has nothing to do with the game.
    #
    # Measured 2026-09-04 on one unchanged tree: 118 scenarios / 0 red with
    # the machine to itself; 6 red + 2 runtime errors while a second sweep
    # ran; and the two runs' red sets were DISJOINT. A gate that answers
    # differently depending on who else is on the box is not a gate.
    #
    # So the render routes serialise. /compile and /checkgd stay concurrent:
    # they are CPU-cheap, headless, and nothing about them is timed.
    _RENDER_LOCK = threading.Lock()
    _RENDER_ROUTES = ("/playtest", "/script", "/x11_input_smoke")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        proj = req.get("project_dir", "")
        # Queue behind any render in flight. No timeout: a caller that waited
        # is strictly better off than a caller that got a fast wrong answer,
        # and the tool side already carries its own HTTP timeout.
        held = self.path in self._RENDER_ROUTES
        if held:
            waited = time.time()
            self._RENDER_LOCK.acquire()
            delay = time.time() - waited
            if delay > 1.0:
                print(f"[harness] {self.path} waited {delay:.0f}s for the render lock",
                      flush=True)
        try:
            if self.path == "/compile":
                self._send(200, compile_project(proj))
            elif self.path == "/checkgd":
                self._send(200, check_gdscript(
                    req.get("files") or [], timeout=req.get("timeout", 120)))
            elif self.path == "/script":
                self._send(200, run_script(
                    proj, req.get("scripts") or [],
                    timeout=req.get("timeout", 600)))
            elif self.path == "/x11_input_smoke":
                self._send(200, x11_input_smoke(
                    proj, timeout=int(req.get("timeout", 180))))
            elif self.path == "/playtest":
                self._send(200, playtest_project(
                    proj, frames=req.get("frames", DEFAULT_PLAYTEST_FRAMES),
                    input_action=req.get("input_action", "ui_accept"),
                    spec=req.get("spec")))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never crash the service on one bad project
            self._send(500, {"error": str(e)})
        finally:
            if held:
                self._RENDER_LOCK.release()


def _serve():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    print(f"godot-harness serving on :{PORT} (bin={GODOT_BIN})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve":
        _serve()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--compile":
        print(json.dumps(compile_project(sys.argv[2]), indent=2))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--playtest":
        print(json.dumps(playtest_project(sys.argv[2]), indent=2))
    else:
        print("usage: godot_harness.py --serve | --compile <dir> | --playtest <dir>", file=sys.stderr)
        sys.exit(2)
