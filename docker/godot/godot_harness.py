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
# AItelier runtime probe (injected). Runs the game headless for a bounded number
# of frames, optionally auto-pressing an input action so the game progresses,
# then snapshots every node's script variables to JSON and quits.
var _frames := 0
var _max := 180
var _dumped := false
var _action := ""
func _ready() -> void:
	# Cap the framerate so a frame budget maps to stable wall-clock/game seconds —
	# headless runs uncapped otherwise, making delta tiny and the playtest advance
	# almost no game time regardless of frame count.
	Engine.max_fps = 60
	_max = int(OS.get_environment("AITELIER_PROBE_FRAMES")) if OS.get_environment("AITELIER_PROBE_FRAMES") != "" else 180
	_action = OS.get_environment("AITELIER_PROBE_INPUT")
	if _action != "" and not InputMap.has_action(_action):
		InputMap.add_action(_action)
func _process(_d: float) -> void:
	_frames += 1
	if _action != "" and _frames % 20 == 0:
		Input.action_press(_action)
	elif _action != "" and _frames % 20 == 1:
		Input.action_release(_action)
	if _frames >= _max:
		_dump()
		get_tree().quit()
func _exit_tree() -> void:
	_dump()  # fallback if the game quit itself before the frame budget
func _dump() -> void:
	if _dumped:
		return
	_dumped = true
	var out := {"frames": _frames, "nodes": {}}
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


def playtest_project(project_dir: str, frames: int = DEFAULT_PLAYTEST_FRAMES,
                     input_action: str = "ui_accept", timeout: int = 120) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "summary": "No Godot project — playtest skipped."}
    dst = _copy_project(proj)
    state_path = dst.parent / "probe_state.json"
    try:
        _inject_probe(dst)
        try:
            cp = _run(["--path", str(dst)], timeout=timeout, extra_env={
                "AITELIER_PROBE_OUT": str(state_path),
                "AITELIER_PROBE_FRAMES": str(frames),
                "AITELIER_PROBE_INPUT": input_action,
            })
            stderr, timed_out = cp.stderr, False
        except subprocess.TimeoutExpired as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            timed_out = True
        errs = [e for e in _parse_errors(stderr) if e["kind"] in ("runtime", "push_error", "parse", "load")]
        state = {}
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text())
            except json.JSONDecodeError:
                state = {}
        ran = bool(state) or not timed_out
        passed = not errs and ran
        if not ran:
            summary = "Playtest could not run the scene (no probe snapshot)."
        elif passed:
            summary = "Playtest ran %d frames cleanly, no runtime errors." % state.get("frames", frames)
        else:
            summary = "Playtest surfaced %d runtime error(s)." % len(errs)
        return {"passed": passed, "frames": state.get("frames", frames),
                "errors": errs, "state": state.get("nodes", {}), "summary": summary}
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
                    input_action=req.get("input_action", "ui_accept")))
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
