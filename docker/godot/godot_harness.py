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
import fcntl
import json
import os
import re
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GODOT_BIN = os.environ.get("GODOT_BIN", "godot")
DEFAULT_PLAYTEST_FRAMES = int(os.environ.get("GODOT_PLAYTEST_FRAMES", "180"))
# How many frames to photograph per playtest run. It decides PHOTOGRAPHY ONLY.
# It used to decide RENDERING too (`render = bool(capture_at)`), which meant
# turning the pictures off silently moved the whole gate onto --headless's dummy
# driver — measured 2026-09-17, that alone turned 5 of 8 scenarios red because
# nothing was laid out to click. Whether the engine draws is now each call
# site's own explicit argument to `_run_probe`.
#
# DEFAULT 0 since 2026-09-17 (owner ruling). At 4/scenario the PNGs were 99.7%
# of a gate's playtest.json — 442,870,140 of 444,172,652 bytes across 656
# fields — and NOTHING on the gate path read them: the only reader is
# aitelier/tools/godot_playtest/impl.py:218, reachable only from :408, while
# tools/godot_gate.py imports read_spec alone and the game repo's
# `git grep png_b64` exits 1. This is a size-and-reviewability cut, not a speed
# one: capture + serialize measured 73.4041 s of a 3373.1978 s run (2.18%).
# A red scenario is re-photographed ON DEMAND: POST /playtest with
# {"captures": 4} (or set GODOT_PLAYTEST_CAPTURES) re-runs it in render mode and
# the PNGs come back exactly as before — the capability is intact, only the
# default is off.
PLAYTEST_CAPTURES = int(os.environ.get("GODOT_PLAYTEST_CAPTURES", "0"))
RENDER_RES = os.environ.get("GODOT_PLAYTEST_RES", "1280x720")
# Frames per second the probe's game clock advances at, as a FIXED DELTA rather
# than a real-time throttle. `--fixed-fps N` makes the engine hand _process
# exactly 1/N as delta and "disables real-time synchronization" (godot --help,
# 4.7.2), so a frame budget still maps to a determined slice of game time — the
# property the old `Engine.max_fps = 60` bought — without SLEEPING for it.
# Measured 2026-09-17 in this image, 600 frames of a trivial scene:
#   Engine.max_fps = 60   game_time 9.9478 s  wall 9.9504 s  mean delta 0.016580
#   --fixed-fps 60        game_time 10.0000 s wall 0.0017 s  mean delta 0.016667
#   neither               game_time  4.1241 s wall 4.1201 s  mean delta 0.006874
# The fixed delta is the one that is EXACTLY 1/60; the cap only approximated it.
# Set to 0 to fall back to the old real-time cap (for measuring the difference).
PLAYTEST_FIXED_FPS = int(os.environ.get("GODOT_PLAYTEST_FIXED_FPS", "60"))
PORT = int(os.environ.get("PORT", "8080"))
LIFECYCLE_DB = os.environ.get(
    "GODOT_LIFECYCLE_DB",
    str(Path.home() / ".AItelier" / "godot-control" / "owners.sqlite3"),
)
DEPLOYMENT_LOCK = os.environ.get(
    "GODOT_DEPLOYMENT_LOCK",
    str(Path(LIFECYCLE_DB).with_name("deployment-admission.lock")),
)
RENDER_EFFECT_LOCK = os.environ.get(
    "GODOT_RENDER_EFFECT_LOCK",
    str(Path(LIFECYCLE_DB).with_name("render-effect.lock")),
)
_MAX_CAPTURES = 8       # hard ceiling: every PNG rides home inside the JSON body
# A render owner refreshes heartbeat_at every RENDER_OWNER_HEARTBEAT_INTERVAL_SEC
# for as long as its render runs (_start_owner_heartbeat). An `active` owner
# whose heartbeat is older than RENDER_OWNER_HEARTBEAT_STALE_SEC has a process
# that died or wedged with its render effect possibly unsettled: a request
# refuses on it (it needs reconciliation) instead of queuing behind it.
RENDER_OWNER_HEARTBEAT_INTERVAL_SEC = 20.0
RENDER_OWNER_HEARTBEAT_STALE_SEC = 120.0
# How often a queued request re-reads the durable owner table and checks that
# its caller is still connected.
RENDER_OWNER_WAIT_POLL_SEC = 0.25


def _lifecycle_connection() -> sqlite3.Connection:
    path = Path(LIFECYCLE_DB)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS render_owners (
        owner_id TEXT PRIMARY KEY,
        resource TEXT NOT NULL,
        project_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        status TEXT NOT NULL,
        actor TEXT NOT NULL,
        started_at REAL NOT NULL,
        heartbeat_at REAL NOT NULL,
        ended_at REAL,
        reason TEXT,
        UNIQUE(resource, generation)
    )""")
    conn.commit()
    return conn


@contextmanager
def _operation_admission_fence():
    path = Path(DEPLOYMENT_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _acquire_render_effect_lock(*, blocking: bool = True):
    path = Path(RENDER_EFFECT_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = path.open("a+b")
    os.chmod(path, 0o600)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(stream.fileno(), flags)
    except BaseException:
        stream.close()
        raise
    return stream


def _release_render_effect_lock(stream) -> None:
    if stream is None:
        return
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def acquire_render_owner(project_id: str, run_id: str, operation_id: str) -> dict:
    with _operation_admission_fence():
        return _acquire_render_owner_under_fence(project_id, run_id, operation_id)


def _acquire_render_owner_under_fence(project_id: str, run_id: str,
                                      operation_id: str) -> dict:
    """Durably reserve render ownership; stale owners stay blocking."""
    now = time.time()
    owner_id = uuid.uuid4().hex
    with _lifecycle_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM render_owners WHERE resource='render' "
            "AND status IN ('active','owner_lost') ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if existing is not None:
            raise RenderOwnerConflict(_render_owner_conflict(dict(existing)))
        generation = (conn.execute(
            "SELECT COALESCE(MAX(generation), 0) + 1 FROM render_owners "
            "WHERE resource='render'").fetchone()[0])
        row = {"owner_id": owner_id, "resource": "render",
               "project_id": str(project_id), "run_id": str(run_id),
               "operation_id": str(operation_id), "generation": generation,
               "status": "active", "actor": f"godot-harness:{os.getpid()}",
               "started_at": now, "heartbeat_at": now}
        conn.execute(
            "INSERT INTO render_owners (owner_id,resource,project_id,run_id,"
            "operation_id,generation,status,actor,started_at,heartbeat_at) "
            "VALUES (:owner_id,:resource,:project_id,:run_id,:operation_id,"
            ":generation,:status,:actor,:started_at,:heartbeat_at)", row)
        conn.commit()
    return row


class RenderOwnerConflict(RuntimeError):
    """A render owner is in the way; `payload` names it and says whether to wait."""

    def __init__(self, payload: dict):
        super().__init__(json.dumps(payload, sort_keys=True))
        self.payload = payload


class RenderWaitAbandoned(RuntimeError):
    """The caller disconnected while queued, so its render must never start."""


def _render_owner_conflict(row: dict) -> dict:
    """Classify the holder in the way: a live render to queue behind, or a stop.

    `active` with a heartbeat younger than RENDER_OWNER_HEARTBEAT_STALE_SEC is a
    live render: the request waits for it. `owner_lost`, and `active` with an
    older heartbeat, need reconciliation: the process that held the render may
    have left its effect unsettled, so no request may start past it until an
    operator reconciles the row (POST /lifecycle/owner-lost, then
    /lifecycle/reconcile).
    """
    heartbeat_age = time.time() - float(row.get("heartbeat_at") or 0.0)
    if row.get("status") == "owner_lost":
        kind = "owner_lost"
        detail = (f"render owner {row.get('owner_id')} is marked owner_lost "
                  f"({row.get('reason') or 'no reason recorded'}): its render "
                  f"effect may be unsettled, so it needs reconciliation "
                  f"(POST /lifecycle/reconcile) before any render can start")
    elif heartbeat_age > RENDER_OWNER_HEARTBEAT_STALE_SEC:
        kind = "active_stale"
        detail = (f"render owner {row.get('owner_id')} is marked active but its "
                  f"heartbeat is {heartbeat_age:.0f}s old (threshold "
                  f"{RENDER_OWNER_HEARTBEAT_STALE_SEC:.0f}s): its process died "
                  f"or wedged with the render effect possibly unsettled, so it "
                  f"needs reconciliation (POST /lifecycle/owner-lost, then "
                  f"/lifecycle/reconcile) before any render can start")
    else:
        kind = "active"
        detail = (f"render owner {row.get('owner_id')} is rendering (heartbeat "
                  f"{heartbeat_age:.0f}s old); a request queues until it releases")
    return {"error": "render owner exists", "owner_kind": kind,
            "owner_id": row.get("owner_id"),
            "needs_reconciliation": kind != "active",
            "detail": detail, "owner": row}


def acquire_render_owner_waiting(project_id: str, run_id: str, operation_id: str, *,
                                 wait_timeout: float | None = None,
                                 should_abort=None) -> dict:
    """Take render ownership, queuing behind a live owner until it releases.

    Raises RenderOwnerConflict at once for a holder that needs reconciliation,
    RenderOwnerConflict ("render owner wait timed out") when the request's own
    `wait_timeout` runs out, and RenderWaitAbandoned when `should_abort()`
    turns true. The last two leave no owner row behind. The returned row
    carries `waited_sec` and `waited_for_owner_ids` (every live holder this
    request queued behind, in order).
    """
    started = time.monotonic()
    waited_for: list[str] = []
    while True:
        if should_abort is not None and should_abort():
            raise RenderWaitAbandoned(
                f"caller disconnected after {time.monotonic() - started:.2f}s "
                f"queued behind render owner(s) {waited_for}")
        try:
            row = acquire_render_owner(project_id, run_id, operation_id)
        except RenderOwnerConflict as exc:
            if exc.payload["needs_reconciliation"]:
                raise
            if exc.payload["owner_id"] not in waited_for:
                waited_for.append(exc.payload["owner_id"])
        else:
            row = dict(row)
            row["waited_sec"] = round(time.monotonic() - started, 4)
            row["waited_for_owner_ids"] = waited_for
            return row
        elapsed = time.monotonic() - started
        if wait_timeout is not None and elapsed >= wait_timeout:
            raise RenderOwnerConflict({
                "error": "render owner wait timed out", "owner_kind": "active",
                "owner_id": waited_for[-1], "waited_for_owner_ids": waited_for,
                "needs_reconciliation": False,
                "waited_sec": round(elapsed, 4), "wait_timeout_sec": wait_timeout,
                "detail": (f"queued {elapsed:.2f}s behind live render owner "
                           f"{waited_for[-1]}; the request's own "
                           f"render_wait_timeout_sec ({wait_timeout}) ran out "
                           f"and nothing was rendered")})
        time.sleep(RENDER_OWNER_WAIT_POLL_SEC)


def _start_owner_heartbeat(owner_id: str, generation: int):
    """Refresh the owner's heartbeat_at until the returned event is set."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(RENDER_OWNER_HEARTBEAT_INTERVAL_SEC):
            try:
                heartbeat_render_owner(owner_id, generation)
            except Exception as exc:
                print(f"[harness] render owner heartbeat failed: {exc}", flush=True)
                return

    thread = threading.Thread(target=beat, name="render-owner-heartbeat", daemon=True)
    thread.start()
    return stop, thread


def _stop_owner_heartbeat(heartbeat) -> None:
    if heartbeat is None:
        return
    stop, thread = heartbeat
    stop.set()
    thread.join(timeout=5)


def _with_owner_wait(report, owner_wait: dict):
    """Put how long the request queued, and behind whom, on its report."""
    if isinstance(report, dict):
        report.update(owner_wait)
    return report


def heartbeat_render_owner(owner_id: str, generation: int) -> None:
    with _lifecycle_connection() as conn:
        updated = conn.execute(
            "UPDATE render_owners SET heartbeat_at=? WHERE owner_id=? "
            "AND generation=? AND status='active'",
            (time.time(), owner_id, generation)).rowcount
        if updated != 1:
            raise RuntimeError("render owner is no longer active")
        conn.commit()


def release_render_owner(owner_id: str, generation: int, reason: str = "completed") -> None:
    with _lifecycle_connection() as conn:
        updated = conn.execute(
            "UPDATE render_owners SET status='released', ended_at=?, reason=? "
            "WHERE owner_id=? AND generation=? AND status='active'",
            (time.time(), str(reason)[:500], owner_id, generation)).rowcount
        if updated != 1:
            raise RuntimeError("render owner cannot be released")
        conn.commit()


def mark_render_owner_lost(owner_id: str, generation: int, reason: str) -> dict:
    with _lifecycle_connection() as conn:
        updated = conn.execute(
            "UPDATE render_owners SET status='owner_lost', ended_at=?, reason=? "
            "WHERE owner_id=? AND generation=? AND status='active'",
            (time.time(), str(reason)[:500], owner_id, generation)).rowcount
        if updated != 1:
            raise RuntimeError("render owner is not active")
        row = conn.execute("SELECT * FROM render_owners WHERE owner_id=?", (owner_id,)).fetchone()
        conn.commit()
    return dict(row)


def reconcile_render_owner(owner_id: str, generation: int, actor: str, reason: str) -> dict:
    if not str(actor).strip() or not str(reason).strip():
        raise ValueError("actor and reason are required")
    with _lifecycle_connection() as conn:
        owner = conn.execute(
            "SELECT * FROM render_owners WHERE owner_id=? AND generation=?",
            (owner_id, generation)).fetchone()
        if owner is None or owner["status"] != "owner_lost":
            raise RuntimeError("only an owner_lost render may be reconciled")
        if _Handler._RENDER_LOCK.locked():
            raise RuntimeError("render effect is not settled: process render lock is held")
        owner_actor = str(owner["actor"] or "")
        if owner_actor.startswith("godot-harness:"):
            try:
                owner_pid = int(owner_actor.rsplit(":", 1)[1])
            except ValueError:
                raise RuntimeError("render owner process identity is malformed")
            if owner_pid != os.getpid():
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise RuntimeError("render owner process settlement is unknown") from exc
                else:
                    raise RuntimeError("render owner process is still alive")
        try:
            effect_lock = _acquire_render_effect_lock(blocking=False)
        except BlockingIOError as exc:
            raise RuntimeError("render effect is not settled: durable effect lock is held") from exc
        _release_render_effect_lock(effect_lock)
        updated = conn.execute(
            "UPDATE render_owners SET status='reconciled', ended_at=?, actor=?, reason=? "
            "WHERE owner_id=? AND generation=? AND status='owner_lost'",
            (time.time(), str(actor)[:200], str(reason)[:500], owner_id, generation)).rowcount
        if updated != 1:
            raise RuntimeError("only an owner_lost render may be reconciled")
        row = conn.execute("SELECT * FROM render_owners WHERE owner_id=?", (owner_id,)).fetchone()
        conn.commit()
    return dict(row)


def render_owner_snapshot() -> list[dict]:
    with _lifecycle_connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM render_owners ORDER BY generation, owner_id")]

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


# ── native (engine-side) errors ────────────────────────────────────────────
# `_parse_errors` drops every plain ERROR line that is not a user push_error,
# because engine internals are not the game's business. That blanket drop hid a
# real, actionable defect for a whole wave. Measured on the wave-4 script sweep
# (director/reports/m1-runtime-wave3/raw/scripts-wave4-22d9b64-script.json):
# `res://tests/test_encounter.gd` exited 0, printed no FAIL marker and was
# reported `passed: true`, while its stderr held
#     ERROR: Error calling deferred method: 'Node2D(battlefield.gd)::_wire_hud':
#            Cannot convert argument 2 from Array to Array.
#        at: _call_function (core/object/message_queue.cpp:222)
# — a call the game makes that never ran. The engine said so; the harness threw
# it away; the report vouched for the run.
#
# The fix is NOT "fail on every ERROR", and the same sweep shows why: the unit
# suite's test_user_storage DELIBERATELY feeds malformed JSON, a truncated
# ConfigFile and an impossible copy, and the engine prints one ERROR per
# negative case. Those lines are the test working. So every native ERROR is
# CLASSIFIED and REPORTED (`native_errors[]`), and only the classes that mean
# "the game asked the engine for something it cannot do" decide pass/fail:
#
#   class           blocking  observed evidence
#   deferred_call   yes       Error calling deferred method / message_queue.cpp
#   type_conversion yes       "Cannot convert argument N from X to Y"
#   image_format    yes       Condition "format != p_src->format" @ image.cpp
#                             blit_rect — the atlas-format errors, also missed
#   io_input        no        json.cpp / config_file.cpp / dir_access.cpp —
#                             the engine rejecting INPUT it was handed. A
#                             negative test provokes exactly this, but THE
#                             SOURCE FILE CANNOT PROVE IT WAS MEANT TO: an
#                             io_input line is REVIEWED DEBT, never "expected",
#                             never evidence the run was clean
#   exit_leak       no        "... at exit" — resources still in use, leaked
#                             RIDs; long-standing debt, recorded not gated,
#                             and never a claim the leak is gone or harmless
#   unclassified    no        everything else: VISIBLE debt, never dropped
#
# Non-blocking is not "exempt" and this is not a core/io exemption: image.cpp
# and resource.cpp are core/io too and are classified on their own evidence.
# Every entry rides home in the report with its repeat count, so a leak, an
# io_input line or an unclassified one can never be read as absent — and a
# report with none of the three blocking classes is not a release-readiness
# claim about anything.
_IO_INPUT_SRC = ("core/io/json.cpp", "core/io/config_file.cpp",
                 "core/io/dir_access.cpp")
_BLOCKING_NATIVE = ("deferred_call", "type_conversion", "image_format")


def _classify_native(msg: str, at_func: str, at_src: str) -> str:
    """Bucket one native ERROR line. See the table above for the evidence."""
    if "Error calling deferred method" in msg or at_src == "core/object/message_queue.cpp":
        return "deferred_call"
    if "Cannot convert argument" in msg:
        return "type_conversion"
    if at_src == "core/io/image.cpp" and ("format" in msg or at_func in ("blit_rect", "blend_rect")):
        return "image_format"
    if "at exit" in msg:
        return "exit_leak"
    if at_src in _IO_INPUT_SRC:
        return "io_input"
    return "unclassified"


def _native_errors(stderr: str) -> list[dict]:
    """Classify the engine-side ERROR lines `_parse_errors` deliberately drops.

    One entry per DISTINCT (class, message, location) with a `count`: the
    wave-4 encounter run printed the same blit_rect line eight times, and a
    report that repeats it eight times is a report nobody finishes reading.
    Lines already claimed by `_parse_errors` are left to it, so no diagnostic
    is counted twice and the existing `errors[]` keeps its meaning exactly.
    What it claims is exactly: SCRIPT ERROR lines, `Failed to load script`, and
    an ERROR whose location is a `push_error` frame. A res:// location is NOT
    one of those tests — it only decorates a diagnostic the parser already kept
    — so a plain ERROR pointing INTO user code belongs here, with its file and
    line. Treating res:// as "owned" dropped that shape from both fields; no
    stored report shows one (1254 report files scanned 2026-09-05, zero hits),
    which makes it a latent hole rather than a measured loss, and a lossless
    union has to hold by construction rather than by luck.
    """
    lines = stderr.splitlines()
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    i = 0
    while i < len(lines):
        m = _USER_ERR.match(lines[i])
        if not m or _FAILED_LOAD.match(lines[i]):
            i += 1
            continue
        msg = m.group(1).strip()
        at = _AT.match(lines[i + 1]) if i + 1 < len(lines) else None
        at_func, at_src = (at.group(1).strip(), at.group(2)) if at else ("", "")
        if at:
            i += 1
        if at_func.startswith("push_error"):     # `_parse_errors` kept it
            i += 1
            continue
        in_user_code = at_src.startswith("res://")
        cls = _classify_native(msg, at_func, at_src)
        key = (cls, msg, at_func, at_src)
        if key in seen:
            seen[key]["count"] += 1
        else:
            entry = {"kind": "native", "native_class": cls,
                     "blocking": cls in _BLOCKING_NATIVE, "msg": msg,
                     "at": "%s (%s:%s)" % (at_func, at_src, at.group(3)) if at else None,
                     "file": at_src if in_user_code else None,
                     "line": int(at.group(3)) if in_user_code else None,
                     "count": 1}
            seen[key] = entry
            out.append(entry)
        i += 1
    return out


def _split_diagnostics(errs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split diagnostics into (gating, debt). Everything is KEPT; only the
    verdict is scoped. Entries without a `blocking` key are the pre-existing
    kinds and gate exactly as they always did.

    This runs in the CALLER, not at the probe, on purpose. stderr exists
    whether or not the run produced a snapshot, and the first version of this
    hung the debt off the snapshot dict: a timed-out scenario then reported
    "scene did not run" and threw the engine's account of that run away with
    the empty probe. Splitting here keeps the evidence on the failing path,
    where it is worth most.
    """
    gating = [e for e in errs if e.get("blocking", True)]
    debt = [e for e in errs if not e.get("blocking", True)]
    return gating, debt


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
# ── WHERE THE TIME WENT ────────────────────────────────────────────────────
# The engine side of the per-scenario clock. Three of the four classes can only
# be seen from in here: the python side only ever sees one opaque subprocess.
#   _t_first_process_usec  engine start -> first _process  == boot/scene load
#   _t_step_end_usec       first _process -> _finish       == frame stepping
#   _capture_usec          viewport grab + save_png, charged out of stepping
#   (serialize)            _walk + JSON.stringify, measured inside _finish
var _t_first_process_usec := -1
var _t_step_end_usec := -1
var _capture_usec := 0
# GAME TIME: the sum of the deltas the engine handed _process. This is the
# quantity the frame budget is supposed to buy -- under --fixed-fps N every
# delta is 1/N, so this must come out at frames/N, and the python side CHECKS
# that it does for every scenario (determined_game_time_findings). Before this
# line the ledger could only report WALL clock, which under a fixed delta is
# unrelated to what the game experienced: "a frame budget maps to a determined
# slice of game time" was a claim with no observation point anywhere.
var _game_usec := 0.0
# MEASUREMENT ONLY, never set on the gate path: extra real microseconds spent at
# the end of every frame. It exists so "does this scenario's verdict depend on
# what a frame costs in real time?" can be ASKED, instead of being discovered by
# accident. Under `Engine.max_fps = N` the cap only sleeps for the REMAINDER of
# 1/N, so a frame that costs more than that hands the game a bigger delta and
# the scenario silently gets more game time; under `--fixed-fps N` the delta is
# 1/N no matter what this is set to, which is the property worth proving.
# (Measured 2026-09-17: four captured frames per run were worth 1.2 s of extra
# game time in a 230-frame scenario, and that was the whole reason four
# scenarios were green.)
var _frame_load_usec := 0
var _watch := []         # [{node, attr}] whose frame-0 value a delta assert needs
var _baselines := {}     # "node|attr" -> frame-0 value
func _ready() -> void:
    # Keep ticking even when the game calls get_tree().paused = true — otherwise
    # the probe freezes with the game and can neither un-pause nor assert, so a
    # pause feature would be untestable.
    process_mode = Node.PROCESS_MODE_ALWAYS
    # A frame budget must map to a determined slice of game time — uncapped,
    # delta goes tiny (0.0069 measured) and the game barely advances. That used
    # to be bought with `Engine.max_fps = 60`, which SLEEPS: 82,675 frames over
    # a gate ÷ 60 = 1,378 s of the hour spent waiting for a clock. The engine's
    # own `--fixed-fps` hands _process exactly 1/N without the sleep, so the
    # python side passes it and this stays out of the way. AITELIER_PROBE_MAX_FPS
    # is the fallback for a run that deliberately keeps the old throttle.
    var cap := OS.get_environment("AITELIER_PROBE_MAX_FPS")
    if cap != "" and int(cap) > 0:
        Engine.max_fps = int(cap)
    var envl := OS.get_environment("AITELIER_PROBE_FRAME_LOAD_USEC")
    if envl != "":
        _frame_load_usec = int(envl)
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
    # Timed around the WHOLE grab, every exit included: an early return here is
    # still capture work that happened, and letting it fall into stepping is
    # exactly the mis-attribution this instrument exists to remove.
    var t0 := Time.get_ticks_usec()
    _grab(drawn)
    _capture_usec += Time.get_ticks_usec() - t0
func _grab(drawn: int) -> void:
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
    # The first _process is the boundary between "booting" and "stepping": the
    # main scene is instantiated and in the tree by now, and nothing has been
    # driven yet.
    if _t_first_process_usec < 0:
        _t_first_process_usec = Time.get_ticks_usec()
    # Whatever the engine hands out IS this frame's game time. Summing the
    # ARGUMENT is the only reading that cannot disagree with what the game saw:
    # any independent clock would be measuring something else.
    _game_usec += _d * 1000000.0
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
    if _frame_load_usec > 0:
        OS.delay_usec(_frame_load_usec)
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
    _t_step_end_usec = Time.get_ticks_usec()
    var out := {"frames": _frame, "asserts": _results, "nodes": {}, "captures": _captures}
    var t_walk := Time.get_ticks_usec()
    _walk(get_tree().get_root(), out["nodes"])
    var walk_usec := Time.get_ticks_usec() - t_walk
    # boot: engine start -> first _process. If the game quit before a single
    # frame ran there is no stepping to separate, and saying so beats inventing
    # a split: boot swallows the whole run and step reads 0.
    var boot_usec := _t_first_process_usec
    var step_usec := 0
    if _t_first_process_usec < 0:
        boot_usec = _t_step_end_usec
    else:
        step_usec = (_t_step_end_usec - _t_first_process_usec) - _capture_usec
    # The two zeros below are PLACEHOLDERS patched into the serialized text: a
    # number can neither contain the cost of writing itself nor the clock read
    # that follows it. Both are spliced after stringify returns, so the cost
    # reported is the real one and the document is serialized exactly once.
    out["timing"] = {"boot_usec": boot_usec, "step_usec": step_usec,
                     "capture_usec": _capture_usec, "walk_usec": walk_usec,
                     "serialize_usec": 0, "engine_usec": 0,
                     "game_usec": int(_game_usec),
                     "frames_stepped": _frame, "captures_taken": _captures.size()}
    var path := OS.get_environment("AITELIER_PROBE_OUT")
    if path == "":
        path = "user://probe_state.json"
    var t_str := Time.get_ticks_usec()
    var text := JSON.stringify(out, "  ")
    var str_usec := (Time.get_ticks_usec() - t_str) + walk_usec
    text = text.replace("\"serialize_usec\": 0", "\"serialize_usec\": %d" % str_usec)
    text = text.replace("\"engine_usec\": 0", "\"engine_usec\": %d" % Time.get_ticks_usec())
    var f := FileAccess.open(path, FileAccess.WRITE)
    if f != null:
        f.store_string(text)
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


def _capture_frames(total: int, timeline: list | None = None,
                    limit: int | None = None) -> list[int]:
    """Which frames to photograph. Assert frames have PRIORITY over the stride
    (a PNG earns its bandwidth by showing the very state an assertion judged),
    and when there are more of them than there is budget they are sampled
    EVENLY ACROSS the scenario rather than taken from its head — see below. The
    stride only spends whatever budget is left, so a run with no asserts still
    comes back with a filmstrip instead of nothing.

    Never schedules the last frame: the probe calls _finish() and quit() from
    _process once _frame >= _max, so that frame's post-draw never fires and the
    JSON would name a PNG that was never written."""
    limit = min(PLAYTEST_CAPTURES if limit is None else limit, _MAX_CAPTURES)
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
                render: bool, timing: dict | None = None) -> tuple[dict, list, bool]:
    if state_path.exists():
        state_path.unlink()
    t_proc = time.monotonic()
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
    proc_sec = time.monotonic() - t_proc
    errs = [e for e in _parse_errors(stderr) if e["kind"] in ("runtime", "push_error", "parse", "load")]
    # A deferred call that never ran, or an atlas blit the engine refused, is a
    # runtime error of the game's own making — it just has no res:// frame. It
    # comes back with the rest; `_split_diagnostics` in the caller decides which
    # ones gate. Nothing is dropped here, so an empty or unparseable snapshot
    # cannot take the evidence down with it.
    errs += _native_errors(stderr)
    probe = {}
    t_parse = time.monotonic()
    if state_path.is_file():
        try:
            probe = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            probe = {}
    parse_sec = time.monotonic() - t_parse
    if timing is not None:
        timing["proc_sec"] = timing.get("proc_sec", 0.0) + proc_sec
        timing["snapshot_parse_sec"] = timing.get("snapshot_parse_sec", 0.0) + parse_sec
        timing["passes"] = timing.get("passes", 0) + 1
        eng = probe.get("timing") if isinstance(probe, dict) else None
        if isinstance(eng, dict):
            for key in ("boot_usec", "step_usec", "capture_usec", "serialize_usec",
                        "engine_usec", "game_usec", "frames_stepped"):
                timing[key] = timing.get(key, 0) + int(eng.get(key, 0) or 0)
    return probe, errs, timed_out


def _attach_pngs(captures: list, cap_dir: Path, timing: dict | None = None) -> list:
    """Inline each captured PNG as base64 and keep only its basename: the sidecar
    mounts the workspace read-only, so the bytes have to ride home in the JSON
    body, and the container-local path means nothing to the caller."""
    out = []
    t0 = time.monotonic()
    png_bytes = 0
    for c in captures:
        png = cap_dir / Path(str(c.get("file", ""))).name
        if png.is_file():
            raw = png.read_bytes()
            png_bytes += len(raw)
            out.append({"frame": c.get("frame"), "file": png.name,
                        "png_b64": base64.b64encode(raw).decode()})
    if timing is not None:
        timing["png_b64_sec"] = timing.get("png_b64_sec", 0.0) + (time.monotonic() - t0)
        timing["png_bytes"] = timing.get("png_bytes", 0) + png_bytes
    return out


def _run_probe(dst: Path, state_path: Path, frames: int, timeout: int,
               extra: dict, scene: str = "",
               capture_at: list[int] | None = None,
               timing: dict | None = None,
               render: bool = True) -> tuple[dict, list, bool]:
    """One probe run. Returns (probe_report, errors, timed_out) — the captures
    ride inside probe_report, because callers (and the unit tests that fake this)
    depend on the 3-tuple."""
    args = ["--path", str(dst)]
    if PLAYTEST_FIXED_FPS > 0:
        # Ahead of the scene argument: this is an engine flag, not a scene.
        args += ["--fixed-fps", str(PLAYTEST_FIXED_FPS)]
    if scene:
        args.append(scene)              # run a specific scene instead of main
    env = {"AITELIER_PROBE_OUT": str(state_path), "AITELIER_PROBE_FRAMES": str(frames)}
    # Exactly one of the two mechanisms is ever live: the flag (fixed delta, no
    # sleep) or the in-probe cap (real-time throttle, the pre-2026-09-17 path).
    if PLAYTEST_FIXED_FPS <= 0:
        env["AITELIER_PROBE_MAX_FPS"] = "60"
    env.update(extra)
    cap_dir = dst.parent / "captures"
    if render and capture_at:
        shutil.rmtree(cap_dir, ignore_errors=True)
        cap_dir.mkdir(parents=True, exist_ok=True)
        env["AITELIER_PROBE_CAPTURE"] = str(cap_dir)
        env["AITELIER_PROBE_CAPTURE_AT"] = ",".join(str(f) for f in capture_at)
    probe, errs, timed_out = _probe_once(args, env, state_path, timeout, render,
                                        timing=timing)
    if render and not probe:
        # A broken X/GL setup must degrade to yesterday's behaviour, not take the
        # whole playtest gate down: retry once, headless, with capture off.
        env.pop("AITELIER_PROBE_CAPTURE", None)
        env.pop("AITELIER_PROBE_CAPTURE_AT", None)
        render = False
        if timing is not None:
            timing["headless_retry"] = True
        probe, errs, timed_out = _probe_once(args, env, state_path, timeout, False,
                                             timing=timing)
    if probe:
        # Report which mode actually produced this, so a silent fallback to the
        # pixel-blind path is visible rather than looking like "no captures".
        probe["render_mode"] = "render" if render else "headless"
        probe["captures"] = (_attach_pngs(probe.get("captures", []), cap_dir,
                                          timing=timing)
                             if render and capture_at else [])
    return probe, errs, timed_out


def _playtest_legacy(dst: Path, frames: int, input_action: str, timeout: int,
                     ledger: dict | None = None,
                     cap_limit: int | None = None) -> dict:
    """The old canned smoke test: run the main scene auto-pressing one action,
    snapshot the end state. HARD-fails only on crash / didn't-run."""
    state_path = dst.parent / "probe_state.json"
    t_legacy: dict = {}
    t_legacy_start = time.monotonic()
    probe, errs, timed_out = _run_probe(
        dst, state_path, frames, timeout, {"AITELIER_PROBE_INPUT": input_action},
        capture_at=_capture_frames(frames, limit=cap_limit), timing=t_legacy)
    if ledger is not None:
        t_legacy["frames_stepped"] = (probe.get("timing") or {}).get(
            "frames_stepped", probe.get("frames", 0))
        ledger["scenarios"] = [_scenario_ledger(
            "(legacy smoke test)", "", time.monotonic() - t_legacy_start, t_legacy)]
        ledger["controls"] = []
    errs, debt = _split_diagnostics(errs)
    ran = bool(probe) or not timed_out
    passed = not errs and ran
    if not ran:
        summary = "Playtest could not run the scene (no probe snapshot)."
    elif passed:
        summary = "Playtest ran %d frames cleanly, no runtime errors." % probe.get("frames", frames)
    else:
        summary = "Playtest surfaced %d runtime error(s)." % len(errs)
    return {"passed": passed, "frames": probe.get("frames", frames), "errors": errs,
            "native_debt": debt,
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


def _scenario_ledger(name: str, scene: str, wall_sec: float, t: dict) -> dict:
    """One scenario's line in the ledger.

    Four classes, each measured at its own clock, plus the residue:

      boot       engine start -> first _process (engine init, autoloads, the
                 main scene instantiated and in the tree)
      step       first _process -> _finish, MINUS the in-frame capture cost
      capture    viewport grab + save_png (engine) + base64 (python)
      serialize  the node-tree walk + JSON.stringify (engine) + the python-side
                 parse of that snapshot

      process    python's subprocess wall MINUS everything the engine clock
                 saw == exec of xvfb-run/godot, engine teardown, the store of
                 the snapshot file
      engine_res what the engine clock saw that none of the four classes
                 claimed
      other      python wall outside the subprocess that is not already charged
                 to capture or serialize == the temp user:// dir, the spec
                 write, the digest

    The three residues are named for what they are, and none of them may absorb
    a measured class. That is an arithmetic property, not a promise, so the line
    carries `sum_check_sec` — wall minus all six — and it is ~0 or the ledger is
    lying. MEASURED, 2026-09-17: an earlier version defined `other` as
    wall-minus-subprocess, and because the base64 encode runs outside the
    subprocess, a 0.5s/PNG delay injected into it landed in BOTH capture
    (+6.030s, correct) and other (+6.008s, a residue eating a measured class).
    Subtracting the out-of-subprocess charges here is what makes the polarity
    test mean something."""
    usec = lambda k: int(t.get(k, 0) or 0) / 1e6
    proc_sec = float(t.get("proc_sec", 0.0))
    boot, step = usec("boot_usec"), usec("step_usec")
    capture = usec("capture_usec") + float(t.get("png_b64_sec", 0.0))
    serialize = usec("serialize_usec") + float(t.get("snapshot_parse_sec", 0.0))
    engine = usec("engine_usec")
    # The engine clock starts at engine startup, so everything before and after
    # it belongs to the process, not to any of the four classes.
    process = proc_sec - engine
    engine_res = engine - (boot + step + usec("capture_usec")
                           + usec("serialize_usec"))
    # Charged to capture and serialize above, and NOT inside the subprocess —
    # so they must come out of the python-side residue or they are counted
    # twice. This is the line the variant-B polarity run was added to hold.
    outside = float(t.get("png_b64_sec", 0.0)) + float(t.get("snapshot_parse_sec", 0.0))
    other = wall_sec - proc_sec - outside
    total = boot + step + capture + serialize + process + engine_res + other
    return {"name": name, "scene": scene,
            "wall_sec": round(wall_sec, 4),
            "boot_sec": round(boot, 4), "step_sec": round(step, 4),
            "capture_sec": round(capture, 4), "serialize_sec": round(serialize, 4),
            "process_sec": round(process, 4),
            "engine_residual_sec": round(engine_res, 4),
            "other_sec": round(other, 4),
            "sum_check_sec": round(wall_sec - total, 6),
            "subprocess_sec": round(proc_sec, 4),
            "engine_sec": round(engine, 4),
            "passes": int(t.get("passes", 0)),
            "headless_retry": bool(t.get("headless_retry", False)),
            "frames_stepped": int(t.get("frames_stepped", 0) or 0),
            # The property, and what it is supposed to be, on the same line —
            # so nobody has to divide by hand to find out whether it held.
            "game_time_sec": round(usec("game_usec"), 6),
            "expected_game_time_sec": (
                round(int(t.get("frames_stepped", 0) or 0) / PLAYTEST_FIXED_FPS, 6)
                if PLAYTEST_FIXED_FPS > 0 else None),
            "png_bytes": int(t.get("png_bytes", 0) or 0)}


# How far a scenario's game time may sit from frames/N before the run is called
# a lie. ABSOLUTE, and deliberately not scaled by the run length: the only drift
# this should ever see is the engine's own float accumulation plus the
# microsecond truncation on the way out, both fixed-size, while a proportional
# band would grow until a long scenario could lose whole frames inside it.
# 0.002 s is under an eighth of a frame at 60 fps; the regression it exists to
# catch — the real-time cap coming back — was measured at +32%, 5.05 s of game
# time where 3.83 s was due.
GAME_TIME_TOL = float(os.environ.get("GODOT_PLAYTEST_GAME_TIME_TOL", "0.002"))


def determined_game_time_findings(rows: list, fixed_fps: int,
                                  tol: float = GAME_TIME_TOL) -> list:
    """THE OBSERVATION POINT for "a frame budget maps to a determined slice of
    game time". Returns one finding per scenario whose measured game time is not
    frames/N; an empty list means the property held for every row.

    This exists because the property was bought and then went UNWATCHED. r2
    replaced the probe's old real-time frame cap (which bought it by sleeping)
    with `--fixed-fps N` (which buys it by decree), measured it once by hand, wrote
    the numbers in a report, and shipped tests that mock the engine out — so
    nothing that runs would have noticed if a later change quietly took the
    property away again. It is checked here, on the production path, for all 164
    scenarios of every gate, rather than in a test that is allowed to pretend.

    A finding is HARD (it joins spec_errors): a run whose frames no longer buy a
    known amount of game time has not measured the game the author wrote, and a
    green verdict over it would mean nothing. With fixed_fps <= 0 the harness is
    deliberately back on the real-time cap and there is no expectation to check,
    so the list is empty and says nothing either way."""
    if fixed_fps <= 0:
        return []
    out = []
    for r in rows:
        frames = int(r.get("frames_stepped", 0) or 0)
        if frames <= 0:
            continue
        got = float(r.get("game_time_sec", 0.0) or 0.0)
        want = frames / fixed_fps
        if got <= 0.0:
            # Frames were stepped and no game time came back at all. That is the
            # property UNOBSERVED rather than violated, and a run nobody can
            # attest is not a run to pass: this gate exists in a codebase whose
            # recurring defect is a verdict delivered over missing evidence.
            out.append(
                "scenario %r: stepped %d frames and reported NO game time. The "
                "probe's reading is missing, so nothing can say whether the "
                "frame budget still buys a determined slice of game time."
                % (r.get("name", "?"), frames))
        elif abs(got - want) > tol:
            out.append(
                "scenario %r: %d frames at --fixed-fps %d must be %.6f s of game "
                "time, measured %.6f s (off by %+.6f s). The frame budget no "
                "longer buys a determined slice of game time, so every `at:` in "
                "the spec means something different than it did."
                % (r.get("name", "?"), frames, fixed_fps, want, got, got - want))
    return out


def _playtest_spec(dst: Path, spec: dict, frames: int, timeout: int,
                   ledger: dict | None = None,
                   cap_limit: int | None = None) -> dict:
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
    scen_timing: list[dict] = []
    ctrl_timing: list[dict] = []
    all_debt: list[dict] = []
    scen_nodes: list[dict] = []
    scen_frames: list[int] = []
    scen_scenes: list[str] = []
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
        t_scenario: dict = {}
        t_scen_start = time.monotonic()
        try:
            probe, errs, timed_out = _run_probe(
                dst, state_path, sframes, timeout,
                {"AITELIER_PROBE_SPEC": str(spec_path), "HOME": sc_home},
                scene=sc_scene,
                capture_at=_capture_frames(sframes, timeline, limit=cap_limit),
                timing=t_scenario)
        finally:
            shutil.rmtree(sc_home, ignore_errors=True)
        t_scenario["frames_stepped"] = (probe.get("timing") or {}).get(
            "frames_stepped", probe.get("frames", 0))
        scen_timing.append(_scenario_ledger(
            name, sc_scene, time.monotonic() - t_scen_start, t_scenario))
        errs, debt = _split_diagnostics(errs)
        ran = bool(probe) or not timed_out
        ran_any = ran_any or ran
        if errs:
            crashed = True
        all_errors.extend({**e, "scenario": name} for e in errs)
        all_debt.extend({**e, "scenario": name} for e in debt)
        asserts = probe.get("asserts", [])
        scen_passed = ran and not errs and bool(asserts) and all(a.get("passed") for a in asserts)
        scen_results.append({"name": name, "ran": ran, "errors": errs,
                             "native_debt": debt,
                             "asserts": asserts, "passed": scen_passed,
                             # "Did this scenario drive ANY input?" -- derived
                             # from the normalised entries themselves: after
                             # _normalize_timeline every key is either the
                             # schedule (`at`), an assertion, or an input the
                             # probe delivers (press/release/click/hover, and
                             # whatever is added to _TIMELINE_KEYS next). This
                             # used to read only `press`, so the 10 click-only
                             # scenarios never entered L0 at all -- the same
                             # two-lists-out-of-sync shape as the actions:/press:
                             # incident, one level up.
                             "pressed": any(set(e) - {"at", "assert"} for e in timeline),
                             "input_dead": False})
        scen_nodes.append(_digest(probe.get("nodes", {})))
        scen_frames.append(sframes)
        scen_scenes.append(sc_scene)
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
    # Keyed by (scene, frame budget): the control must boot the SAME scene the
    # scenario booted. It was keyed by frame budget alone and always booted the
    # spec-level scene, so every scenario with its own `scene:` override (86 of
    # 172 on the wuxia tree) was compared against a different node tree --
    # digests that can never be equal, so input_dead could never fire for them.
    controls: dict[tuple[str, int], dict] = {}
    if driven and not crashed:
        for i in driven:
            n = scen_frames[i]
            key = (scen_scenes[i], n)
            if key not in controls:
                spec_path.write_text(json.dumps({"frames": n, "timeline": []}))
                # The control is a RUN, so it needs the same throwaway user://
                # every scenario gets. It was the one probe call still on the
                # container's HOME: measured 2026-09-05 on the wuxia tree, the
                # game's user:// logs landed in the sidecar's shared home at the
                # end of each request, and only the control passes were there.
                # A control that boots into a save an earlier control left is
                # not the no-input baseline this comparison claims to be.
                ctrl_home = tempfile.mkdtemp(prefix="godot_home_")
                t_ctrl: dict = {}
                t_ctrl_start = time.monotonic()
                try:
                    ctrl, _e, _t = _run_probe(dst, state_path, n, timeout,
                                              {"AITELIER_PROBE_SPEC": str(spec_path),
                                               "HOME": ctrl_home},
                                              scene=scen_scenes[i], timing=t_ctrl,
                                              render=False)
                finally:
                    shutil.rmtree(ctrl_home, ignore_errors=True)
                ctrl_timing.append(_scenario_ledger(
                    "control:%s@%d" % (scen_scenes[i] or "(main)", n),
                    scen_scenes[i], time.monotonic() - t_ctrl_start, t_ctrl))
                controls[key] = _digest(ctrl.get("nodes", {}))
            # An empty control means the control pass itself failed to report --
            # stay quiet rather than accuse the game on missing evidence.
            if controls[key] and scen_nodes[i] == controls[key]:
                scen_results[i]["input_dead"] = True
                scen_results[i]["passed"] = False

    # The determined-game-time check runs over the scenario rows, not the
    # controls: a control has no `at:` and nothing rides on its budget.
    spec_errors.extend(determined_game_time_findings(scen_timing, PLAYTEST_FIXED_FPS))

    dead = [r["name"] for r in scen_results if r["input_dead"]]
    behavior_passed = bool(scen_results) and all(s["passed"] for s in scen_results)
    hard_passed = ran_any and not crashed and not spec_errors and not dead
    n_fail = sum(1 for s in scen_results if not s["passed"])
    if spec_errors:
        # `spec_errors` used to hold exactly one kind of thing, so the
        # summary named it. It now also holds determined-game-time findings,
        # and a summary that calls those "malformed timeline entries" would
        # send the next reader to the wrong file.
        summary = ("Playtest HARD-failed: %d spec violation%s -- %s"
                   % (len(spec_errors), "" if len(spec_errors) == 1 else "s",
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
    if ledger is not None:
        ledger["scenarios"] = scen_timing
        ledger["controls"] = ctrl_timing
    return {"passed": hard_passed, "frames": default_frames, "errors": all_errors,
            "native_debt": all_debt,
            "state": last_state, "spec_used": True, "spec_errors": spec_errors,
            "captures": captures, "render_mode": render_mode,
            "behavior": {"all_passed": behavior_passed, "scenarios": scen_results},
            "summary": summary}


def _assemble_ledger(ledger: dict, started_at: str, t_start: float,
                     copy_sec: float, import_sec: float) -> dict:
    """The playtest-level ledger: every part, AND the remainder.

    A report of parts without a remainder is the same unaccountability it is
    meant to remove, so `unattributed_sec` is not optional and not a bucket
    anything is poured into — it is what is LEFT once every measured part is
    subtracted from this call's own wall clock, and it is 0 only if nothing is
    missing. `harness_finished_at`/`wall_sec` are the outer endpoints: the
    difference between them and the gate manifest's playtest stage is the HTTP
    transport plus the caller's own re-serialization of this document, which
    this process cannot see and does not claim to have measured."""
    scenarios = ledger.get("scenarios") or []
    controls = ledger.get("controls") or []
    scen_sum = sum(float(s.get("wall_sec", 0.0)) for s in scenarios)
    ctrl_sum = sum(float(s.get("wall_sec", 0.0)) for s in controls)
    wall = time.monotonic() - t_start

    def klass(key):
        return round(sum(float(s.get(key, 0.0)) for s in scenarios + controls), 4)

    return {
        "schema": "playtest-timing/1",
        "harness_started_at": started_at,
        "harness_finished_at": datetime.now(timezone.utc).isoformat(),
        "wall_sec": round(wall, 4),
        "copy_project_sec": round(copy_sec, 4),
        "import_resources_sec": round(import_sec, 4),
        "scenario_count": len(scenarios),
        "scenario_wall_sum_sec": round(scen_sum, 4),
        "control_count": len(controls),
        "control_wall_sum_sec": round(ctrl_sum, 4),
        "unattributed_sec": round(
            wall - scen_sum - ctrl_sum - copy_sec - import_sec, 4),
        "unattributed_means": (
            "this call's wall clock minus every scenario, every L0 control, the "
            "project copy and the resource import: inter-scenario scheduling, "
            "spec assembly, the aggregation below, and this ledger itself. It "
            "does NOT include the HTTP transport or the caller writing the "
            "response to disk — those lie between harness_finished_at and the "
            "gate manifest's playtest finished_at."),
        "by_class_sec": {"boot": klass("boot_sec"), "step": klass("step_sec"),
                         "capture": klass("capture_sec"),
                         "serialize": klass("serialize_sec"),
                         "process": klass("process_sec"),
                         "engine_residual": klass("engine_residual_sec"),
                         "scenario_other": klass("other_sec")},
        "worst_sum_check_sec": max(
            (abs(float(s.get("sum_check_sec", 0.0)))
             for s in scenarios + controls), default=0.0),
        "top10_scenarios": sorted(
            ({"name": s["name"], "wall_sec": s["wall_sec"]} for s in scenarios),
            key=lambda s: -s["wall_sec"])[:10],
        "top10_share": (round(sum(sorted((float(s.get("wall_sec", 0.0))
                                          for s in scenarios), reverse=True)[:10])
                              / scen_sum, 4) if scen_sum else 0.0),
        "report_serialize_sec": 0.0,
        "report_serialize_means": (
            "seconds spent turning this whole document into the JSON response "
            "body, spliced in after the serialization it measures; 0.0 means "
            "the response was not produced by the HTTP route (CLI, or a unit "
            "test calling playtest_project directly)."),
        "scenarios": scenarios,
        "controls": controls,
    }


def playtest_project(project_dir: str, frames: int = DEFAULT_PLAYTEST_FRAMES,
                     input_action: str = "ui_accept", spec: dict | None = None,
                     timeout: int = 120, captures: int | None = None) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "behavior": None, "spec_used": False, "no_project": True,
                "summary": "No Godot project — playtest skipped."}
    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    t_copy = time.monotonic()
    dst = _copy_project(proj)
    copy_sec = time.monotonic() - t_copy
    ledger: dict = {}
    try:
        _inject_probe(dst)
        t_import = time.monotonic()
        _import_resources(dst, timeout)
        import_sec = time.monotonic() - t_import
        if spec and isinstance(spec.get("scenarios"), list) and spec["scenarios"]:
            result = _playtest_spec(dst, spec, frames, timeout, ledger=ledger,
                                    cap_limit=captures)
        else:
            result = _playtest_legacy(dst, frames, input_action, timeout,
                                      ledger=ledger, cap_limit=captures)
        if isinstance(result, dict):
            result["timing"] = _assemble_ledger(ledger, started_at, t_start,
                                                copy_sec, import_sec)
        return result
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
            # rc and the FAIL marker are what the SUITE says about itself.
            # They are silent about what the ENGINE said: test_encounter exited
            # 0, printed PASS, and left a deferred call that never ran in its
            # stderr. Classified native errors close that hole — and only the
            # blocking classes do, so a suite whose malformed JSON is the
            # POINT stays green — with those lines on the record as debt, not
            # as a finding that the input was meant to be malformed.
            natives = _native_errors(err)
            blocking = [e for e in natives if e["blocking"]]
            failed = rc != 0 or _has_failure_marker(out, err) or bool(blocking)
            results.append({"script": rel, "returncode": rc, "passed": not failed,
                            "stdout": _script_log_excerpt(out),
                            "stderr": _script_log_excerpt(err),
                            "stdout_truncated": len(out) > 4000,
                            "stderr_truncated": len(err) > 4000,
                            "errors": _parse_errors(err),
                            "native_errors": natives,
                            "native_blocking": sorted({e["native_class"] for e in blocking})})
        ok = all(r["passed"] for r in results)
        bad = [r["script"] for r in results if not r["passed"]]
        summary = ("%d script(s) ran, all passed." % len(results) if ok
                   else "%d/%d script(s) failed: %s"
                        % (len(bad), len(results), ", ".join(bad)))
        blocked = ["%s (%s)" % (r["script"], ", ".join(r["native_blocking"]))
                   for r in results if r["native_blocking"]]
        if blocked:
            summary += ("  Native engine error(s) the game caused: %s."
                        % "; ".join(blocked))
        debt = sorted({e["native_class"] for r in results
                       for e in r["native_errors"] if not e["blocking"]})
        if debt:
            summary += ("  Native engine errors recorded, NOT gating (reviewed "
                        "debt, not proof of intent): %s (see native_errors[])."
                        % ", ".join(debt))
        return {"passed": ok, "results": results, "discovered": scripts,
                "summary": summary}
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

    def _send_timed(self, code: int, payload: dict) -> None:
        """Send a report whose ledger includes the cost of serializing itself.

        A document cannot contain a number describing the act that produces it,
        so the placeholder written by `_assemble_ledger` is spliced AFTER
        json.dumps returns. The payload is serialized exactly once — measuring
        by serializing twice would both double the cost and report the wrong
        one. If the placeholder is not there (an older ledger, or a report with
        no timing at all) the body is sent untouched: a missing number must
        never cost a caller its 444MB of evidence."""
        t0 = time.monotonic()
        text = json.dumps(payload)
        took = time.monotonic() - t0
        placeholder = '"report_serialize_sec": 0.0'
        if text.count(placeholder) == 1:
            text = text.replace(placeholder,
                                '"report_serialize_sec": %.4f' % took, 1)
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def _render_client_abandoned(self) -> bool:
        """True once the socket that asked for this render has been closed.

        The request body is already read, so the socket turns readable only
        when the peer closes it (EOF) or resets it.
        """
        sock = getattr(self, "connection", None)
        if sock is None:
            return False
        try:
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return True
        if not readable:
            return False
        try:
            return sock.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "engine": "godot", "bin": GODOT_BIN})
        elif self.path == "/lifecycle":
            try:
                self._send(200, {"resource": "render",
                                 "owners": render_owner_snapshot()})
            except Exception as exc:  # fail closed for the deployment observer
                self._send(503, {"error": str(exc)})
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
        if self.path == "/lifecycle/owner-lost":
            try:
                row = mark_render_owner_lost(
                    str(req["owner_id"]), int(req["generation"]), str(req["reason"]))
                return self._send(200, row)
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                return self._send(409, {"error": str(exc)})
        if self.path == "/lifecycle/reconcile":
            try:
                row = reconcile_render_owner(
                    str(req["owner_id"]), int(req["generation"]),
                    str(req["actor"]), str(req["reason"]))
                return self._send(200, row)
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                return self._send(409, {"error": str(exc)})
        proj = req.get("project_dir", "")
        # Queue behind any render in flight: a request that finds a live render
        # owner waits for it to release, then owns the render. The wait has no
        # harness timeout; it ends when the caller disconnects (its own HTTP
        # timeout) or when the request's own {"render_wait_timeout_sec": N} runs
        # out, and in both cases nothing is rendered and no owner row is made.
        # A holder that needs reconciliation (owner_lost, or active with a stale
        # heartbeat) is refused at once with a 409 that names its kind.
        held = self.path in self._RENDER_ROUTES
        # The matching finally releases the process lock and durable owner.
        # finally: _RENDER_LOCK.release()
        owner = None
        effect_lock = None
        heartbeat = None
        lock_wait = 0.0
        owner_wait = {}
        if held:
            project_id = req.get("project_id") or Path(proj).name or "unknown-project"
            run_id = req.get("run_id") or os.environ.get("AITELIER_RUN_ID") or "unknown-run"
            operation_id = (req.get("operation_id")
                            or self.headers.get("X-AItelier-Operation")
                            or uuid.uuid4().hex)
            wait_timeout = req.get("render_wait_timeout_sec")
            try:
                wait_timeout = None if wait_timeout is None else max(0.0, float(wait_timeout))
            except (TypeError, ValueError):
                return self._send(400, {"error": "render_wait_timeout_sec must be a number"})
            try:
                owner = acquire_render_owner_waiting(
                    project_id, run_id, operation_id, wait_timeout=wait_timeout,
                    should_abort=self._render_client_abandoned)
            except RenderWaitAbandoned as exc:
                print(f"[harness] {self.path} {operation_id}: {exc}", flush=True)
                return
            except RenderOwnerConflict as exc:
                return self._send(409, exc.payload)
            except RuntimeError as exc:
                return self._send(409, {"error": str(exc)})
            owner_wait = {"render_owner_wait_sec": owner.pop("waited_sec"),
                          "render_owner_waited_for_owner_ids": owner.pop("waited_for_owner_ids")}
            if owner_wait["render_owner_wait_sec"] > 1.0:
                print(f"[harness] {self.path} waited "
                      f"{owner_wait['render_owner_wait_sec']:.0f}s for render owner(s) "
                      f"{owner_wait['render_owner_waited_for_owner_ids']}", flush=True)
            heartbeat = _start_owner_heartbeat(owner["owner_id"], owner["generation"])
            waited = time.time()
            try:
                self._RENDER_LOCK.acquire()
                effect_lock = _acquire_render_effect_lock()
            except Exception as exc:
                if self._RENDER_LOCK.locked():
                    self._RENDER_LOCK.release()
                _stop_owner_heartbeat(heartbeat)
                try:
                    release_render_owner(owner["owner_id"], owner["generation"],
                                         "render effect lock acquisition failed")
                except Exception as release_exc:
                    print(f"[harness] render owner release failed: {release_exc}", flush=True)
                return self._send(500, {"error": str(exc)})
            delay = time.time() - waited
            lock_wait = delay
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
                self._send(200, _with_owner_wait(run_script(
                    proj, req.get("scripts") or [],
                    timeout=req.get("timeout", 600)), owner_wait))
            elif self.path == "/x11_input_smoke":
                self._send(200, _with_owner_wait(x11_input_smoke(
                    proj, timeout=int(req.get("timeout", 180))), owner_wait))
            elif self.path == "/playtest":
                report = playtest_project(
                    proj, frames=req.get("frames", DEFAULT_PLAYTEST_FRAMES),
                    input_action=req.get("input_action", "ui_accept"),
                    spec=req.get("spec"),
                    # On-demand re-photography of a red scenario: the gate runs
                    # with 0 captures, a reviewer re-runs that one scenario with
                    # {"captures": 4} and gets the PNGs back.
                    captures=req.get("captures"))
                if isinstance(report.get("timing"), dict):
                    report["timing"]["render_lock_wait_sec"] = round(lock_wait, 4)
                    report["timing"].update(owner_wait)
                self._send_timed(200, _with_owner_wait(report, owner_wait))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never crash the service on one bad project
            self._send(500, {"error": str(e)})
        finally:
            if held:
                _stop_owner_heartbeat(heartbeat)
                _release_render_effect_lock(effect_lock)
                self._RENDER_LOCK.release()
                try:
                    release_render_owner(owner["owner_id"], owner["generation"])
                except Exception as exc:  # preserve owner-loss evidence for recovery
                    print(f"[harness] render owner release failed: {exc}", flush=True)


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
