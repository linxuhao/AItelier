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

  playtest -> copy the project, inject an autoload probe, run the game headless
              for N frames with a dummy renderer, then return:
                * every runtime error (SCRIPT ERROR / push_error) with file+line
                * a JSON snapshot of the live scene tree's script variables
                  (score, velocity, game_state, ...) — the thing that makes an
                  agent actually SEE runtime state, which Unity could never give.

The gate_skipped fail-open->observable contract is enforced on the *tool* side
(aitelier/tools/godot_compile), not here; this service just reports facts.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GODOT_BIN = os.environ.get("GODOT_BIN", "godot")
DEFAULT_PLAYTEST_FRAMES = int(os.environ.get("GODOT_PLAYTEST_FRAMES", "180"))
PORT = int(os.environ.get("PORT", "8080"))

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


def _run(args: list[str], timeout: int, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [GODOT_BIN, "--headless", *args],
        capture_output=True, text=True, timeout=timeout, env=env,
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


def compile_project(project_dir: str, timeout: int = 120) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "returncode": 0, "file_count": 0,
                "errors": [], "warning_count": 0,
                "summary": "No Godot project (project.godot absent) — nothing to compile."}
    gd_files = [p for p in proj.rglob("*.gd") if ".godot/" not in str(p)]
    dst = _copy_project(proj)
    try:
        cp = _run(["--path", str(dst), "--import"], timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "file_count": len(gd_files),
                "errors": [{"kind": "timeout", "msg": f"Import timed out after {timeout}s",
                            "file": None, "line": None}],
                "warning_count": 0, "summary": "Godot import timed out."}
    finally:
        shutil.rmtree(dst.parent, ignore_errors=True)
    errs = [e for e in _parse_errors(cp.stderr) if e["kind"] in ("parse", "load")]
    passed = not errs
    summary = ("GDScript parse OK (%d scripts)." % len(gd_files) if passed
               else "GDScript parse FAILED — %d error(s)." % len(errs))
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
# Always writes {frames, asserts[], nodes{}} to AITELIER_PROBE_OUT and quits.
# Indented with spaces (GDScript accepts consistent spaces or tabs).
var _frame := 0
var _max := 180
var _dumped := false
var _legacy_action := ""
var _spec_mode := false
var _timeline := []      # SPEC: [{at:int, press?:String, release?:String, assert?:[{name,node,expr}]}]
var _releases := {}      # frame -> [action, ...] auto-release schedule
var _results := []       # [{name, node, expr, passed, actual, error, frame}]
func _ready() -> void:
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
func _process(_d: float) -> void:
    # 0-based frames: apply this frame's scheduled releases + timeline entries,
    # THEN advance. Incrementing first would make `at: 0` unreachable.
    if _releases.has(_frame):
        for act in _releases[_frame]:
            if InputMap.has_action(act):
                Input.action_release(act)
        _releases.erase(_frame)
    if _spec_mode:
        for e in _timeline:
            if int(e.get("at", -1)) == _frame:
                _apply_entry(e)
    elif _legacy_action != "":
        if _frame % 20 == 0:
            Input.action_press(_legacy_action)
        elif _frame % 20 == 1:
            Input.action_release(_legacy_action)
    _frame += 1
    if _frame >= _max:
        _finish()
        get_tree().quit()
func _apply_entry(e: Dictionary) -> void:
    var pr = e.get("press", "")
    if pr != "":
        Input.action_press(pr)
        var rf := _frame + 2      # hold ~2 frames, then auto-release
        _releases[rf] = _releases.get(rf, [])
        _releases[rf].append(pr)
    var rl = e.get("release", "")
    if rl != "" and InputMap.has_action(rl):
        Input.action_release(rl)
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
    _results.append(res)
func _resolve(name: String) -> Node:
    if name == "":
        return get_tree().current_scene
    if name.begins_with("/") or name.begins_with("res:"):
        return get_node_or_null(NodePath(name))
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
    var out := {"frames": _frame, "asserts": _results, "nodes": {}}
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


def _run_probe(dst: Path, state_path: Path, frames: int, timeout: int,
               extra: dict, scene: str = "") -> tuple[dict, list, bool]:
    """One headless probe run. Returns (probe_report, errors, timed_out)."""
    if state_path.exists():
        state_path.unlink()
    args = ["--path", str(dst)]
    if scene:
        args.append(scene)              # run a specific scene instead of main
    env = {"AITELIER_PROBE_OUT": str(state_path), "AITELIER_PROBE_FRAMES": str(frames)}
    env.update(extra)
    try:
        cp = _run(args, timeout=timeout, extra_env=env)
        stderr, timed_out = cp.stderr, False
    except subprocess.TimeoutExpired as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        timed_out = True
    errs = [e for e in _parse_errors(stderr) if e["kind"] in ("runtime", "push_error", "parse", "load")]
    probe = {}
    if state_path.is_file():
        try:
            probe = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            probe = {}
    return probe, errs, timed_out


def _playtest_legacy(dst: Path, frames: int, input_action: str, timeout: int) -> dict:
    """The old canned smoke test: run the main scene auto-pressing one action,
    snapshot the end state. HARD-fails only on crash / didn't-run."""
    state_path = dst.parent / "probe_state.json"
    probe, errs, timed_out = _run_probe(
        dst, state_path, frames, timeout, {"AITELIER_PROBE_INPUT": input_action})
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
            "spec_used": False, "summary": summary}


def _playtest_spec(dst: Path, spec: dict, frames: int, timeout: int) -> dict:
    """Authored-spec playtest: run ONE isolated headless pass per scenario, driving
    its input timeline and evaluating its Expression assertions against live nodes.

    Gate split: ``passed`` (HARD, loops the goal-loop) covers only crash / didn't-
    run; per-scenario assertion outcomes are ADVISORY (``behavior``) so a wrong or
    flaky spec can never stall a build that otherwise runs clean."""
    scene = str(spec.get("scene", "") or "")
    default_frames = int(spec.get("frames", frames) or frames)
    scenarios = spec.get("scenarios") or []
    state_path = dst.parent / "probe_state.json"
    spec_path = dst.parent / "scenario_spec.json"

    scen_results, all_errors = [], []
    ran_any = crashed = False
    last_state: dict = {}
    for sc in scenarios:
        name = str(sc.get("name", "scenario"))
        timeline = sc.get("timeline") or []
        max_at = max([int(e.get("at", 0)) for e in timeline], default=0)
        sframes = min(default_frames, max_at + 30) if timeline else default_frames
        spec_path.write_text(json.dumps({"frames": sframes, "timeline": timeline}))
        probe, errs, timed_out = _run_probe(
            dst, state_path, sframes, timeout,
            {"AITELIER_PROBE_SPEC": str(spec_path)}, scene=scene)
        ran = bool(probe) or not timed_out
        ran_any = ran_any or ran
        if errs:
            crashed = True
        all_errors.extend({**e, "scenario": name} for e in errs)
        asserts = probe.get("asserts", [])
        scen_passed = ran and not errs and bool(asserts) and all(a.get("passed") for a in asserts)
        scen_results.append({"name": name, "ran": ran, "errors": errs,
                             "asserts": asserts, "passed": scen_passed})
        last_state = probe.get("nodes", last_state)

    behavior_passed = bool(scen_results) and all(s["passed"] for s in scen_results)
    hard_passed = ran_any and not crashed      # HARD gate = crash / didn't-run only
    n_fail = sum(1 for s in scen_results if not s["passed"])
    if not hard_passed:
        summary = ("Playtest HARD-failed: %s."
                   % ("runtime error(s)" if crashed else "scene did not run"))
    elif behavior_passed:
        summary = "Playtest ran %d scenario(s); all assertions passed." % len(scen_results)
    else:
        summary = ("Playtest ran clean but %d/%d scenario(s) failed assertions (advisory)."
                   % (n_fail, len(scen_results)))
    return {"passed": hard_passed, "frames": default_frames, "errors": all_errors,
            "state": last_state, "spec_used": True,
            "behavior": {"all_passed": behavior_passed, "scenarios": scen_results},
            "summary": summary}


def playtest_project(project_dir: str, frames: int = DEFAULT_PLAYTEST_FRAMES,
                     input_action: str = "ui_accept", spec: dict | None = None,
                     timeout: int = 120) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "behavior": None, "spec_used": False,
                "summary": "No Godot project — playtest skipped."}
    dst = _copy_project(proj)
    try:
        _inject_probe(dst)
        if spec and isinstance(spec.get("scenarios"), list) and spec["scenarios"]:
            return _playtest_spec(dst, spec, frames, timeout)
        return _playtest_legacy(dst, frames, input_action, timeout)
    finally:
        shutil.rmtree(dst.parent, ignore_errors=True)


# ── HTTP transport (mirrors the Unity sidecar) ─────────────────────────────
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

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        proj = req.get("project_dir", "")
        try:
            if self.path == "/compile":
                self._send(200, compile_project(proj))
            elif self.path == "/playtest":
                self._send(200, playtest_project(
                    proj, frames=req.get("frames", DEFAULT_PLAYTEST_FRAMES),
                    input_action=req.get("input_action", "ui_accept"),
                    spec=req.get("spec")))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never crash the service on one bad project
            self._send(500, {"error": str(e)})


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
