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

  script   -> run each discovered `extends SceneTree` entry point under tests/
              with `-s`, one throwaway $HOME each. Headless by default. An
              explicit {"render": true} request runs the SAME admitted entry
              points — the default discovery or an explicitly named selection —
              through _run(render=True) (Xvfb + software GL) so a suite can
              assert on real pixels. An explicitly named selection is validated
              against the project's REAL admitted `extends SceneTree` entries
              (types, relative paths, existence, SceneTree) before owner
              admission, project copy, import or any engine/output work, and a
              rendered request with no real project is refused, never answered
              with a green render. Every render still passes the unchanged
              render owner / effect-lock / admission / client-abort / release
              fences.

OWNED INVOCATION EVIDENCE. /script and /playtest both accept an optional,
 bounded {"retain": {"files": [...], "patterns": [...]}} request. `files` are
 literal relative user:// JSON/PNG paths; `patterns` are narrow relative path
 patterns for artifacts whose directory or name the invocation derives at run
 time (for example "monthly-journey-actual-*/runtime.json"). A pattern must
 start with a literal first-directory prefix, may wildcard only within one path
 component (`*`/`?`, never `**` or a root-wide `*`), keeps a fixed component
 depth, and may never select the saves/profile subtrees. Before the throwaway
 $HOME is removed, only the DECLARED (literal or pattern-matched) JSON/PNG
 artifacts that this invocation actually generated under user:// are copied —
 together with the full, untruncated raw stdout/stderr of every pass — into a
 server-chosen, unique directory under the durable godot-control state root
 (the owner ledger's own mount). An explicit request with an empty `files` list
 ({"retain": {"files": []}}) is a raw-only request: it retains the full streams
 and no artifacts, while an OMITTED `retain` key changes nothing. The caller
 cannot choose the destination, overwrite a previous invocation, reach a
 foreign absolute/traversal/symlink/hardlink path, or get a silent green when a
 declared artifact (or a pattern that matched nothing) is missing or a limit is
 hit: the invocation writes a manifest.json beside the files with each retained
 path, size, SHA256 and its project/run/operation/invocation correlation.


The gate_skipped fail-open->observable contract is enforced on the *tool* side
(aitelier/tools/godot_compile), not here; this service just reports facts.
"""
from __future__ import annotations

import base64
import hashlib
import fcntl
import json
import os
import re
import select
import shutil
import socket
import sqlite3
import stat
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


@contextmanager
def _lifecycle_connection():
    """Keep transaction semantics and close the owned connection on every exit."""
    path = Path(LIFECYCLE_DB)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=5)
    try:
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
        # Preserve SQLite commit/rollback at scope exit, then release the FD.
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def _operation_admission_fence(*, blocking: bool = True):
    """Shared hold on the deployment-admission fence.

    With blocking=False it raises BlockingIOError at once while a deployment
    holds the fence exclusively.
    """
    path = Path(DEPLOYMENT_LOCK)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB))
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


def acquire_render_owner(project_id: str, run_id: str, operation_id: str, *,
                         blocking_fence: bool = True) -> dict:
    with _operation_admission_fence(blocking=blocking_fence):
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
    turns true. The last two leave no owner row behind. Both are checked on
    every poll, including polls that find the deployment-admission fence held
    (the fence is tried without blocking) or the owner table locked. The
    returned row carries `waited_sec` and `waited_for_owner_ids` (every live
    holder this request queued behind, in order). do_POST checks both
    conditions again once it owns the render, before the render starts.
    """
    started = time.monotonic()
    waited_for: list[str] = []
    fence_held = False
    while True:
        if should_abort is not None and should_abort():
            raise RenderWaitAbandoned(
                f"caller disconnected after {time.monotonic() - started:.2f}s "
                f"queued behind render owner(s) {waited_for}"
                + (" and the deployment-admission fence" if fence_held else ""))
        try:
            row = acquire_render_owner(project_id, run_id, operation_id,
                                       blocking_fence=False)
        except BlockingIOError:
            fence_held = True
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
        except RenderOwnerConflict as exc:
            fence_held = False
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
            raise RenderOwnerConflict(_wait_timed_out(
                elapsed, wait_timeout, waited_for, fence_held=fence_held))
        time.sleep(RENDER_OWNER_WAIT_POLL_SEC)


def _wait_timed_out(elapsed: float, wait_timeout: float, waited_for: list,
                    *, fence_held: bool = False, owned: bool = False) -> dict:
    """The refusal for a request whose own render_wait_timeout_sec ran out."""
    behind = []
    if waited_for:
        behind.append(f"live render owner {waited_for[-1]}")
    if fence_held:
        behind.append("the deployment-admission fence")
    where = " and ".join(behind) or "the render lock"
    tail = ("it took ownership only after the limit, so its owner row was "
            "released and nothing was rendered" if owned
            else "nothing was rendered and no owner row was made")
    return {"error": "render owner wait timed out", "owner_kind": "active",
            "owner_id": waited_for[-1] if waited_for else None,
            "waited_for_owner_ids": waited_for, "deployment_fence_held": fence_held,
            "needs_reconciliation": False,
            "waited_sec": round(elapsed, 4), "wait_timeout_sec": wait_timeout,
            "detail": (f"queued {elapsed:.2f}s behind {where}; the request's own "
                       f"render_wait_timeout_sec ({wait_timeout}) ran out: {tail}")}


def _start_owner_heartbeat(owner_id: str, generation: int):
    """Refresh the owner's heartbeat_at until the returned event is set.

    A beat that fails because the owner table is locked is retried at the next
    interval, for as long as the render runs. The thread ends when the event is
    set, or when the row is no longer this render's active owner.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(RENDER_OWNER_HEARTBEAT_INTERVAL_SEC):
            try:
                heartbeat_render_owner(owner_id, generation)
            except sqlite3.OperationalError as exc:
                print(f"[harness] render owner heartbeat failed, retrying: {exc}",
                      flush=True)
            except Exception as exc:
                print(f"[harness] render owner heartbeat stopped: {exc}", flush=True)
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
def _raise_walk_error(error: OSError) -> None:
    raise error


def _boundary_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _excluded_roots(proj: Path) -> list:
    """Directories under `proj` that are NOT part of the parent project.

    Two boundary kinds, both derived from the real filesystem — never from a
    hard-coded name like `contracts/`:
      * a nested directory holding its own project.godot is a CHILD PROJECT:
        Godot would resolve its `res://` paths under the PARENT's namespace
        (its `preload("res://fake.gd")` accidentally reads the parent root,
        and its `class_name`s collide with the parent's global class list),
        so it must not be staged into the parent's import or parse passes;
      * a directory holding a regular .gdignore uses Godot's exclusion marker.

    Returns [{"path": <posix relpath>, "reason": str}, ...]. The top of the
    tree is never excluded, symlinks are never followed (a link out of the
    project is not a project boundary we can vouch for). Missing or invalid
    marker files leave the subtree included. An unreadable directory raises
    rather than returning an incomplete inventory.
    """
    excluded = []
    for dirpath, dirnames, filenames in os.walk(
            proj, followlinks=False, onerror=_raise_walk_error):
        here = Path(dirpath)
        rel = here.relative_to(proj).as_posix()
        if here != proj:
            if here.is_symlink():
                dirnames[:] = []
                continue
            if "project.godot" in filenames and _boundary_file(here / "project.godot"):
                excluded.append({"path": rel,
                                 "reason": "nested project (own project.godot)"})
                dirnames[:] = []
                continue
            if ".gdignore" in filenames and _boundary_file(here / ".gdignore"):
                excluded.append({"path": rel, "reason": ".gdignore"})
                dirnames[:] = []
                continue
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".godot", ".git") and not (here / d).is_symlink())
    excluded.sort(key=lambda e: e["path"])
    return excluded


def _under_excluded(p: Path, proj: Path, excluded: list) -> bool:
    rel = p.relative_to(proj).as_posix()
    return any(rel == e["path"] or rel.startswith(e["path"] + "/")
               for e in excluded)


def _parent_scripts(proj: Path, excluded: list) -> list[Path]:
    scripts = []
    for dirpath, dirnames, filenames in os.walk(
            proj, followlinks=False, onerror=_raise_walk_error):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".godot", ".git")
                             and not (here / d).is_symlink()
                             and not _under_excluded(here / d, proj, excluded))
        scripts.extend(here / name for name in sorted(filenames)
                       if name.endswith(".gd") and _boundary_file(here / name))
    return scripts


def _copy_project(proj: Path, *, excluded: list | None = None) -> Path:
    """Copy a project to a writable temp dir. `--import` and play-test runs write
    a `.godot/` cache, but the sidecar mounts the workspace read-only, so we never
    touch the source. Caller must rmtree the returned dir's parent.

    Child-project roots and .gdignore subtrees stay on disk in the copy (their
    bytes are the user's), but each nested CHILD PROJECT root gets a .gdignore
    written into the COPY so the parent engine never imports or resolves it:
    the child is a project of its own, not a folder of the parent's res://
    namespace. A .gdignore the user already wrote is copied as-is and needs no
    marker. Child contract projects require separate validation under their own
    project root. Symlinks are omitted from the copy; the parent's own regular
    root .gdignore file is omitted so it cannot hide the parent from import (a
    directory of that name is parent source and is copied)."""
    config = proj / "project.godot"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise ValueError("project.godot must be a regular non-symlink file")
    work = Path(tempfile.mkdtemp(prefix="godot_"))
    dst = work / "proj"

    def ignore(directory, names):
        here = Path(directory)
        return [name for name in names
                if name in (".godot", ".git") or (here / name).is_symlink()
                or (here == proj and name == ".gdignore" and _boundary_file(here / name))]

    try:
        shutil.copytree(proj, dst, ignore=ignore)
        for e in _excluded_roots(proj) if excluded is None else excluded:
            if e["reason"].startswith("nested project"):
                marker = dst / e["path"] / ".gdignore"
                if not marker.is_file():
                    marker.write_text("", encoding="utf-8")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
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
	d.include_hidden = true
	d.list_dir_begin()
	var n := d.get_next()
	while n != "":
		if d.is_link(n):
			n = d.get_next()
			continue
		if d.current_is_dir():
			var sub := dir_path.path_join(n)
			if n != ".godot" and n != ".git" and not FileAccess.file_exists(sub.path_join(".gdignore")) and not FileAccess.file_exists(sub.path_join("project.godot")):
				_walk(sub, out)
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
    config = proj / "project.godot"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        return {"passed": False, "returncode": -1, "file_count": 0,
                "errors": [{"kind": "boundary", "file": "project.godot", "line": None,
                            "msg": "Parent project.godot must be a regular non-symlink file."}],
                "warning_count": 0, "excluded_child_roots": [],
                "summary": "Invalid parent project boundary; validation did not run."}
    if not (proj / "project.godot").is_file():
        # `no_project` is the machine-readable half of this answer, and the
        # caller needs it: "I cannot see a project here" is a PASS when the repo
        # really is a Python one, and a hard FAILURE when the caller is looking
        # at project.godot as it asks. Only the caller can tell those apart, and
        # it cannot tell them apart from prose.
        return {"passed": True, "returncode": 0, "file_count": 0,
                "errors": [], "warning_count": 0, "no_project": True,
                "summary": "No Godot project (project.godot absent) — nothing to compile."}
    # Boundaries come from the filesystem, once, and gate BOTH the count and
    # the engine passes (staging marks nested child projects; the GDScript walk
    # honors .gdignore). Excluded child roots are REPORTED, never silent: a
    # whole-repository pass that quietly swallowed a nested project would vouch
    # for scripts it never looked at.
    excluded = _excluded_roots(proj)
    gd_files = _parent_scripts(proj, excluded)
    dst = _copy_project(proj, excluded=excluded)
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
        shutil.rmtree(dst.parent, ignore_errors=True)
        return {"passed": False, "returncode": -1, "file_count": len(gd_files),
                "errors": [{"kind": "timeout", "msg": f"Import timed out after {timeout}s",
                            "file": None, "line": None}],
                "warning_count": 0, "summary": "Godot import timed out.",
                "excluded_child_roots": excluded}
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
    if excluded:
        summary += " Excluded non-parent roots (outside this parent validation): " + "; ".join(
                       "%s (%s)" % (e["path"], e["reason"]) for e in excluded)
    shutil.rmtree(dst.parent, ignore_errors=True)
    return {"passed": passed, "returncode": cp.returncode, "file_count": len(gd_files),
            "errors": errs, "warning_count": 0, "summary": summary,
            "excluded_child_roots": excluded}


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
var _spec_errors := []   # author errors found while running: a target path that does not resolve
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
var _baseline_missing := []  # "node|attr" the frame-0 walk could not read at all
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
func _capture_failure(drawn: int, reason: String) -> Dictionary:
    # One explicit incomplete observation per REQUESTED rendered frame. A null
    # viewport/texture/image or a failed save_png must never make the frame
    # VANISH from the report: the row is kept with complete=false and the exact
    # reason, so a lost capture can never read as a complete empty corpus.
    return {"frame": drawn, "file": "", "complete": false, "error": reason,
        "text_observation": {"frame": drawn, "complete": false,
            "error": reason,
            "incomplete_reasons": ["capture failure: %s" % reason]}}
func _grab(drawn: int) -> void:
    var vp := get_viewport()
    if vp == null:
        _captures.append(_capture_failure(drawn,
            "viewport is null at frame_post_draw"))
        return
    var tex := vp.get_texture()
    if tex == null:
        _captures.append(_capture_failure(drawn, "viewport texture is null"))
        return
    var img := tex.get_image()
    if img == null:
        _captures.append(_capture_failure(drawn,
            "viewport texture image is null"))
        return
    var path := _capture_dir.path_join("frame_%04d.png" % drawn)
    if img.save_png(path) != OK:
        _captures.append(_capture_failure(drawn,
            "save_png failed for %s" % path))
        return
    # Same-frame text/geometry observation: the PNG and the observation are
    # taken at the SAME _on_post_draw instant, so both describe one frame.
    _captures.append({"frame": drawn, "file": path,
        "text_observation": _observe_text(drawn, img.get_size())})

func _rect_json(r: Rect2) -> Array:
    return [r.position.x, r.position.y, r.size.x, r.size.y]
func _prop_or_null(obj: Object, name: String):
    # Object.get() on an ABSENT property returns null indistinguishably from a
    # real null value, so the property LIST is what decides existence.
    for p in obj.get_property_list():
        if str(p.name) == name:
            return obj.get(name)
    return null
func _autotranslate_disabled(mode):
    # `auto_translate_mode` is the Godot 4 enum (ALWAYS/DISABLED/INHERIT); the
    # legacy `auto_translate` bool is true == translate. Either shape is handled,
    # and an absent mode is never guessed.
    if mode == null:
        return false
    if typeof(mode) == TYPE_BOOL:
        return not mode
    return mode == Control.AUTO_TRANSLATE_MODE_DISABLED
func _autotranslate_inherit(mode):
    if mode == null or typeof(mode) == TYPE_BOOL:
        return false
    return mode == Control.AUTO_TRANSLATE_MODE_INHERIT
# Rendering parent, per official 4.7.2 CanvasItem::get_parent_item() (which is
# NOT exposed to scripts): the direct parent when it is a CanvasItem and this
# item is not top_level; otherwise the item is parented straight to its canvas
# (the nearest CanvasLayer's canvas, else the viewport's world canvas) and its
# rendering chain ENDS there. That end is the ordinary root of every 2D/GUI
# tree, so it is resolved. The renderer (renderer_canvas_cull.cpp) applies
# modulate, the visibility_layer/canvas_cull_mask test and clip_contents clips
# down THIS chain only; visibility (is_visible_in_tree) follows the scene-tree
# CanvasItem parents instead, including across top_level, and is read from the
# engine rather than re-derived.
# screen_rect/clip_rect/effective_rect are in the CAPTURED PNG's pixel space:
# the renderer draws every canvas through the viewport's stretch *
# global_canvas transform (get_final_transform), which
# get_global_transform_with_canvas does not include, and it rounds each
# clip_contents rect in that space and drops the item when the clip is under
# 0.5 px. global_rect/viewport_rect stay in the logical viewport 2D space.
const _CANVAS_CULL_ALPHA := 0.007  # renderer: modulate.a below this is not drawn
var _png_xform := Transform2D.IDENTITY
var _png_rect := Rect2()
var _png_unresolved := ""
func _axis_aligned(t: Transform2D) -> bool:
    return is_zero_approx(t.x.y) and is_zero_approx(t.y.x)
func _render_chain(c: Control) -> Dictionary:
    var out := {"resolved": false, "reason": "", "clip": Rect2(),
        "screen_rect": Rect2(), "chain_alpha": 0.0, "layer_culled": false,
        "clip_culled": false, "chain_end": ""}
    var vp := get_viewport()
    if c.get_viewport() != vp:
        out["reason"] = "lives in a nested viewport/window whose screen geometry is not resolvable against the captured viewport"
        return out
    if _png_unresolved != "":
        out["reason"] = _png_unresolved
        return out
    var t := _png_xform * c.get_global_transform_with_canvas()
    if not _axis_aligned(t):
        out["reason"] = "rotated or skewed canvas transform; the drawn quad is not an axis-aligned rect"
        return out
    out["screen_rect"] = t * Rect2(Vector2.ZERO, c.size)
    var chain := []
    var item: CanvasItem = c
    while true:
        chain.append(item)
        if item.clip_children != CanvasItem.CLIP_CHILDREN_DISABLED or item is CanvasGroup:
            out["reason"] = "%s uses clip_children/CanvasGroup: a drawn-pixel mask, not a rect" % str(item.get_path())
            return out
        if item.top_level:
            out["chain_end"] = "top_level"
            break
        var p := item.get_parent()
        if p is CanvasItem:
            item = p as CanvasItem
        else:
            out["chain_end"] = "canvas"
            break
    # The canvas the chain is parented to: the engine walks from the chain's
    # top item to the nearest CanvasLayer or Viewport.
    var n: Node = item
    while n != null and not (n is Viewport):
        if n is CanvasLayer:
            if (n as CanvasLayer).custom_viewport != null:
                out["reason"] = "CanvasLayer %s renders into a custom_viewport" % str(n.get_path())
                return out
            if (n as CanvasLayer).follow_viewport_enabled and (n as CanvasLayer).follow_viewport_scale != 1.0:
                out["reason"] = "CanvasLayer %s follows the viewport at nonunit scale; PNG pixel geometry is unsupported" % str(n.get_path())
                return out
            break
        n = n.get_parent()
    # Accumulated from the canvas down, as the renderer does: an alpha prefix
    # below the cull threshold, or a clip under 0.5 px, stops that item and its
    # children; a kept clip is rounded before it clips the next one.
    var clip := _png_rect
    var acc := 1.0
    var culled := false
    for i in range(chain.size() - 1, -1, -1):
        var ci: CanvasItem = chain[i]
        acc *= ci.modulate.a
        if acc < _CANVAS_CULL_ALPHA:
            culled = true
        if (ci.visibility_layer & vp.canvas_cull_mask) == 0:
            out["layer_culled"] = true
        if ci is Control and (ci as Control).clip_contents and not out["clip_culled"]:
            var it := _png_xform * ci.get_global_transform_with_canvas()
            if not _axis_aligned(it):
                out["reason"] = "clip_contents ancestor %s has a rotated or skewed canvas transform" % str(ci.get_path())
                return out
            clip = clip.intersection(it * Rect2(Vector2.ZERO, (ci as Control).size))
            if clip.size.x < 0.5 or clip.size.y < 0.5:
                out["clip_culled"] = true
                clip = Rect2(clip.position, Vector2.ZERO)
            else:
                clip = Rect2(clip.position.round(), clip.size.round())
    out["chain_alpha"] = 0.0 if culled else acc
    out["clip"] = clip
    out["resolved"] = true
    return out
const _TEXT_SUPPORTED = ["Label", "Button"]
# Classes that visibly bear text but whose DISPLAYED string is not an ordinary
# text property: RichTextLabel markup, LineEdit/TextEdit masking + secret mode,
# the item-model OptionButton/ItemList/Tree/PopupMenu, and the shader/custom
# draw surfaces. They are reported explicitly, never silently dropped and never
# counted as covered.
const _TEXT_UNSUPPORTED := ["RichTextLabel", "LineEdit", "TextEdit", "CodeEdit",
    "OptionButton", "ItemList", "Tree", "PopupMenu", "TabBar", "MenuButton",
    "LinkButton", "CheckBox", "CheckButton", "ColorPickerButton", "FileDialog",
    "ProgressBar", "SpinBox"]
# Byte ceiling on the JSON of ONE frame's returned observation. Over it, the
# whole observation is replaced by a small refusal; rows are never truncated.
# This is the probe's early bound; the harness re-seals the final observation
# as the response serializes it (_seal_text_observation).
const _TEXT_OBSERVE_BYTES_MAX = 65536
const _TEXT_LOCALE_MAX_CHARS := 64
var _text_bytes_spent := 0
# Appends one row/reason and charges its JSON size to the running total, so
# the walk stops allocating once the ceiling cannot be met. The returned
# envelope is still measured as a whole in _observe_text.
func _obs_add(obs: Dictionary, key: String, row) -> void:
    if obs["byte_capped"]:
        return
    var n := JSON.stringify(row).to_utf8_buffer().size() + 1
    if _text_bytes_spent + n > _TEXT_OBSERVE_BYTES_MAX:
        obs["byte_capped"] = true
        return
    _text_bytes_spent += n
    obs[key].append(row)
# Label._shape (official 4.7.2 label.cpp) draws
#   txt = uppercase ? TS.string_to_upper(xl_text, lang) : xl_text
#   then txt.substr(0, visible_characters) for VC_CHARS_BEFORE_SHAPING.
# Those two are reproduced with the same public TextServer call. The other
# visible-character behaviours, text_overrun trimming and line skipping/limits
# select glyphs or lines AFTER shaping, so the drawn string is reported as
# unresolved instead of guessed. Case mapping that depends on a locale the
# script cannot read (Control._get_locale is not exposed) is also unresolved.
const _CASE_LOCALES := ["", "tr", "az", "lt", "el"]
func _label_display_transform(c: Control, txt: String) -> Dictionary:
    var upper = _prop_or_null(c, "uppercase")
    if typeof(upper) == TYPE_BOOL and upper:
        var ts := TextServerManager.get_primary_interface()
        var lang = _prop_or_null(c, "language")
        if typeof(lang) == TYPE_STRING and lang != "":
            txt = ts.string_to_upper(txt, lang)
        else:
            var up := ts.string_to_upper(txt, TranslationServer.get_locale())
            for l in _CASE_LOCALES:
                if ts.string_to_upper(txt, l) != up:
                    return {"text": null, "note": "uppercase mapping of this text depends on the control's locale, which is not exposed to scripts; the displayed string is unresolved"}
            txt = up
    var vc = _prop_or_null(c, "visible_characters")
    if typeof(vc) == TYPE_INT and vc >= 0:
        if _prop_or_null(c, "visible_characters_behavior") != TextServer.VC_CHARS_BEFORE_SHAPING:
            return {"text": null, "note": "visible_characters trims glyphs after shaping; the displayed string is unresolved"}
        txt = txt.substr(0, vc)
    var ob = _prop_or_null(c, "text_overrun_behavior")
    if ob != null and ob != TextServer.OVERRUN_NO_TRIMMING:
        return {"text": null, "note": "text_overrun_behavior may trim/ellipsize after shaping; the displayed string is unresolved"}
    var skip = _prop_or_null(c, "lines_skipped")
    var maxl = _prop_or_null(c, "max_lines_visible")
    if (skip != null and skip > 0) or (maxl != null and maxl >= 0):
        return {"text": null, "note": "lines_skipped/max_lines_visible select shaped lines; the displayed string is unresolved"}
    return {"text": txt, "note": ""}
func _observe_control_text(c: Control, cls: String, path: String, obs: Dictionary) -> Dictionary:
    var source_text := str(c.get("text"))
    var global_rect := c.get_global_rect()
    var chain := _render_chain(c)
    var clip_rect: Rect2 = chain["clip"]
    var clip_resolved: bool = chain["resolved"]
    var screen_rect: Rect2 = chain["screen_rect"]
    var effective_rect = null
    var intersects := false
    var viewport_rect := get_viewport().get_visible_rect()
    var alpha = null
    var possibly_visible = null
    if clip_resolved:
        var eff := screen_rect.intersection(clip_rect)
        effective_rect = Rect2(eff.position,
            Vector2(max(0.0, eff.size.x), max(0.0, eff.size.y)))
        intersects = effective_rect.size.x > 0.0 and effective_rect.size.y > 0.0
        # self_modulate applies to this item only, after the chain.
        alpha = float(chain["chain_alpha"]) * c.self_modulate.a
        var hidden_by := PackedStringArray()
        if not c.is_visible_in_tree():
            hidden_by.append("not visible in tree")
        if chain["layer_culled"]:
            hidden_by.append("visibility_layer outside the viewport canvas_cull_mask on the rendering chain")
        if chain["clip_culled"]:
            hidden_by.append("a clip_contents rect on the rendering chain is under 0.5 px, so the renderer does not draw it")
        if alpha <= 0.0:
            hidden_by.append("effective alpha 0 (rendering-chain modulate below the engine cull threshold, or own self_modulate 0)")
        if not intersects and not chain["clip_culled"]:
            hidden_by.append("no intersection with the clip/viewport rect")
        possibly_visible = hidden_by.is_empty()
        if not possibly_visible:
            _obs_add(obs, "hidden_surfaces", {
                "path": path,
                "class": cls,
                "visible_in_tree": c.is_visible_in_tree(),
                "effective_alpha": alpha,
                "intersects_viewport": intersects,
                "reason": "; ".join(hidden_by),
            })
    else:
        _obs_add(obs, "incomplete_reasons",
            "geometry/visibility of control %s (%s) is unresolved: %s" % [path, cls, str(chain["reason"])])
    var auto_mode = _prop_or_null(c, "auto_translate_mode")
    if auto_mode == null:
        auto_mode = _prop_or_null(c, "auto_translate")
    var autotranslate_known := auto_mode != null
    # TranslationServer.translate() returns the message UNCHANGED when no
    # translation resolves, so a source property alone is not always the
    # displayed string. The engine's own CURRENT locale is what we report -- we
    # never infer a locale from a requested language and never force one.
    var translated := str(TranslationServer.translate(source_text))
    var differs := translated != source_text
    var resolved := true
    var displayed = source_text
    var note := ""
    if c.has_method("atr"):
        # Node.atr() is exactly what Label/Button draw (xl_text = atr(text)):
        # it applies this node's resolved auto_translate_mode (INHERIT
        # included) and translation domain.
        displayed = str(c.atr(source_text))
    elif not differs:
        # No translation resolves for this message, so the source property IS
        # the displayed string. The missing-catalog key is exactly this case.
        displayed = source_text
    elif not autotranslate_known:
        # A translation EXISTS and whether it is applied depends on an
        # autotranslate mode this engine does not expose: the displayed string
        # is genuinely unresolved, so we refuse to name one.
        resolved = false
        displayed = null
        note = "a translation resolves for this message but the autotranslate mode is not exposed by this engine; the displayed string is unresolved"
    elif _autotranslate_disabled(auto_mode):
        # The control has autotranslation DISABLED, so the raw source property is
        # what is drawn even though a translation exists.
        displayed = source_text
        note = "autotranslate is disabled on this control; the source property is displayed"
    elif _autotranslate_inherit(auto_mode):
        # INHERIT defers to an ancestor / the engine default this function cannot
        # see from one Control, so the displayed string is unresolved.
        resolved = false
        displayed = null
        note = "autotranslate mode is INHERIT; the displayed string depends on an ancestor/engine default that is not resolved here"
    else:
        # ALWAYS (or auto_translate == true): the translated string is displayed.
        displayed = translated
    if resolved:
        var shaped := _label_display_transform(c, str(displayed))
        displayed = shaped["text"]
        if shaped["note"] != "":
            note = shaped["note"]
        if displayed == null:
            resolved = false
    return {
        "path": path,
        "class": cls,
        "visible_in_tree": c.is_visible_in_tree(),
        "effective_alpha": alpha,
        "text_possibly_visible": possibly_visible,
        "global_rect": _rect_json(global_rect),
        "screen_rect": _rect_json(screen_rect) if clip_resolved else null,
        "clip_rect": _rect_json(clip_rect),
        "viewport_rect": _rect_json(viewport_rect),
        "effective_rect": _rect_json(effective_rect) if effective_rect != null else null,
        "intersects_viewport": intersects,
        "clip_resolved": clip_resolved,
        "clip_unresolved_reason": str(chain["reason"]),
        "top_level": c.top_level,
        "rendering_chain_end": chain["chain_end"],
        "auto_translate_mode": auto_mode,
        "autotranslate_known": autotranslate_known,
        "source_text": source_text,
        "engine_translated_text": translated,
        "translation_differs": differs,
        "displayed_text": displayed,
        "displayed_text_resolved": resolved,
        "note": note,
    }
# Finite traversal bounds: the walk stops at a fixed node count and depth and
# records the stop as an incomplete reason. A bounded frame corpus is reported
# as incomplete; it is never passed off as the whole tree.
const _TEXT_OBSERVE_MAX_NODES := 4096
const _TEXT_OBSERVE_MAX_DEPTH := 64
var _text_walk_nodes := 0
# Returns TRUE to stop the whole remaining walk (a bound was reached).
func _observe_text_walk(node: Node, depth: int, obs: Dictionary) -> bool:
    if obs["byte_capped"]:
        return true
    _text_walk_nodes += 1
    if _text_walk_nodes > _TEXT_OBSERVE_MAX_NODES or depth > _TEXT_OBSERVE_MAX_DEPTH:
        obs["traversal_bounded"] = true
        _obs_add(obs, "incomplete_reasons",
            "text-observation traversal stopped at the finite node/depth bound (%d nodes, depth %d); the frame's corpus is not complete" % [_TEXT_OBSERVE_MAX_NODES, _TEXT_OBSERVE_MAX_DEPTH])
        return true
    if node is CanvasModulate and (node as CanvasModulate).is_visible_in_tree() \
            and (node as CanvasModulate).color.a != 1.0:
        _obs_add(obs, "incomplete_reasons",
            "CanvasModulate %s changes canvas-wide alpha, which effective_alpha does not include" % str(node.get_path()))
    if node is Control:
        var c := node as Control
        var cls := c.get_class()
        var supported := _TEXT_SUPPORTED.has(cls)
        var unsupported := _TEXT_UNSUPPORTED.has(cls)
        var t = null
        if not supported and not unsupported:
            # Property LIST probe: Object.get on an absent property would push
            # an engine error.
            t = _prop_or_null(c, "text")
        if supported or unsupported or (typeof(t) == TYPE_STRING and str(t) != "" and c.is_visible_in_tree()):
            # Pre-row bound: a path or text with more characters than the
            # ceiling has bytes cannot fit in any returned observation.
            var path := str(c.get_path())
            if path.length() > _TEXT_OBSERVE_BYTES_MAX or (supported
                    and str(c.get("text")).length() > _TEXT_OBSERVE_BYTES_MAX):
                obs["byte_capped"] = true
                return true
            if supported:
                _obs_add(obs, "controls", _observe_control_text(c, cls, path, obs))
            elif unsupported:
                _obs_add(obs, "unsupported", {
                    "path": path,
                    "class": cls,
                    "visible_in_tree": c.is_visible_in_tree(),
                    "reason": "displayed string is not an ordinary text property for this class (markup/masking/item model/shader surface); not observed",
                })
            else:
                _obs_add(obs, "unsupported", {
                    "path": path,
                    "class": cls,
                    "visible_in_tree": true,
                    "reason": "class is outside the supported text-surface set; its text property is not claimed to be the displayed string",
                })
    if obs["byte_capped"]:
        return true
    # Indexed children: no array of every child is allocated up front.
    for i in node.get_child_count():
        if _observe_text_walk(node.get_child(i), depth + 1, obs):
            return true
    return false
# Writes the envelope's own size into "bytes" until it is self-consistent and
# returns it: the number covers every returned field, "bytes" included.
func _text_envelope_bytes(obs: Dictionary) -> int:
    var n := JSON.stringify(obs).to_utf8_buffer().size()
    while int(obs.get("bytes", -1)) != n:
        obs["bytes"] = n
        n = JSON.stringify(obs).to_utf8_buffer().size()
    return n
func _text_refusal(drawn: int, obs: Dictionary, locales_total: int) -> Dictionary:
    # Fixed small shape: no rows, no reasons list, no locale list, and the
    # locale only when it is short.
    var locale := str(obs["locale"])
    var out := {
        "frame": drawn,
        "complete": false,
        "byte_capped": true,
        "traversal_bounded": obs["traversal_bounded"],
        "locale": locale if locale.length() <= _TEXT_LOCALE_MAX_CHARS else null,
        "loaded_locales_count": locales_total,
        "controls": [],
        "unsupported": [],
        "hidden_surfaces": [],
        "incomplete_reasons": [],
        "refused": ["text observation for frame %d exceeds the %d-byte ceiling; the whole observation is refused rather than truncated" % [drawn, _TEXT_OBSERVE_BYTES_MAX]],
    }
    _text_envelope_bytes(out)
    return out
func _observe_text(drawn: int, image_size: Vector2i) -> Dictionary:
    # Finite, literal observation of the built-in text surfaces present in the
    # frame that was just drawn. It only OBSERVES: it does not classify where a
    # string came from, does not change assertion truth and does not gate.
    var locale := str(TranslationServer.get_locale())
    var vp := get_viewport()
    _png_xform = vp.get_final_transform()
    _png_rect = Rect2(Vector2.ZERO, Vector2(image_size))
    _png_unresolved = ""
    if not (vp.get_stretch_transform() * vp.get_visible_rect()).is_equal_approx(_png_rect):
        _png_unresolved = "the stretched viewport rect does not match the captured image size, so PNG pixel geometry is unresolved"
    elif vp.snap_2d_transforms_to_pixel:
        _png_unresolved = "2D transform pixel snapping is enabled; the renderer's snapped positions are not reproduced"
    var obs := {
        "frame": drawn,
        "locale": locale,
        "loaded_locales": [],
        "supported_classes": _TEXT_SUPPORTED.duplicate(),
        "unsupported_classes": _TEXT_UNSUPPORTED.duplicate(),
        "viewport_rect": _rect_json(get_viewport().get_visible_rect()),
        "image_size": [image_size.x, image_size.y],
        "coordinate_space": "screen_rect/clip_rect/effective_rect: captured PNG pixels; global_rect/viewport_rect: logical viewport 2D",
        "png_from_viewport_2d": [_png_xform.x.x, _png_xform.x.y, _png_xform.y.x, _png_xform.y.y, _png_xform.origin.x, _png_xform.origin.y],
        "controls": [],
        "unsupported": [],
        "hidden_surfaces": [],
        "refused": [],
        "traversal_bounded": false,
        "byte_capped": false,
        "incomplete_reasons": [],
        "complete": false,
    }
    _text_walk_nodes = 0
    _text_bytes_spent = JSON.stringify(obs).to_utf8_buffer().size()
    var locales := TranslationServer.get_loaded_locales()
    if locale.length() > _TEXT_LOCALE_MAX_CHARS:
        obs["byte_capped"] = true
    for loc in locales:
        _obs_add(obs, "loaded_locales", str(loc))
        if obs["byte_capped"]:
            break
    var root: Node = get_tree().get_root()
    if root != null:
        _observe_text_walk(root, 0, obs)
    for u in obs["unsupported"]:
        if bool(u.get("visible_in_tree", false)):
            _obs_add(obs, "incomplete_reasons", "unsupported visible text surface %s (%s)" % [u["path"], u["class"]])
    for c in obs["controls"]:
        if not bool(c.get("displayed_text_resolved", false)):
            _obs_add(obs, "incomplete_reasons", "unresolved displayed text for %s (%s)" % [c["path"], c["class"]])
    if obs["byte_capped"]:
        return _text_refusal(drawn, obs, locales.size())
    obs["complete"] = obs["incomplete_reasons"].is_empty()
    if _text_envelope_bytes(obs) > _TEXT_OBSERVE_BYTES_MAX:
        return _text_refusal(drawn, obs, locales.size())
    return obs
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
        if _is_path(node_name):
            _refuse_path("aim", node_name, spec)
            return nan_pt
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
        "expr": expr_str, "passed": false, "actual": null, "error": "", "frame": _frame,
        "measurement": "ok"}
    var target := _resolve(node_name)
    if target == null:
        # A target that never resolved is the AUTHOR's spec error, reported as
        # such. It stays an advisory behaviour failure (the existing contract);
        # it is not an incomplete measurement of a value that was there.
        res["measurement"] = ""
        res["error"] = "node not found: " + node_name
        if _is_path(node_name):
            res["error"] = "path does not resolve: " + node_name
            _refuse_path("assert", node_name, str(a.get("name", expr_str)))
        _results.append(res)
        return
    if a.has("mode"):
        _eval_delta(a, target, res)
        return
    var expr := Expression.new()
    if expr.parse(expr_str) != OK:
        # A comparison that could not be parsed never observed anything: an
        # INCOMPLETE measurement, not a false one.
        res["error"] = "parse error: " + expr.get_error_text()
        res["measurement"] = "incomplete"
        _results.append(res)
        return
    # Evaluate against the node as base instance (so "velocity.y < 0" resolves the
    # node's own properties). show_error=false keeps a failed assert OUT of stderr
    # so it stays advisory and never trips the hard runtime-error gate.
    var val = expr.execute([], target, false)
    if expr.has_execute_failed():
        res["error"] = "execute failed: " + expr.get_error_text()
        res["measurement"] = "incomplete"
        _results.append(res)
        return
    var vt := typeof(val)
    res["actual"] = _jsonable(val)
    # Observation and truth are separate: `actual` is the value the expression
    # produced, `passed` is whether it holds. Truth is decided ONLY for the
    # supported kinds -- Boolean as-is, a non-empty String, a number, and null.
    # Any other kind has no assertion truth of its own, and feeding it through
    # bool() would let an unsupported value pass vacuously; it is an INCOMPLETE
    # observation instead. A String observation is a value like any other -- it
    # is never fed through bool(String) to decide truth.
    var supported := (vt == TYPE_BOOL or vt == TYPE_STRING or vt == TYPE_INT
        or vt == TYPE_FLOAT or vt == TYPE_NIL)
    if supported:
        res["passed"] = _truthy(val)
    else:
        res["measurement"] = "incomplete"
        res["error"] = "unsupported observation type %d cannot decide assertion truth" % vt
    # A FAILING comparison reports `false` and nothing else — which says the
    # assert did not hold, but not what was there instead. Read the asserted
    # attribute back and record it, so the report can say "turns_taken == 1
    # failed, it was 3" rather than "failed". The read is retained for BOTH
    # polarities; a read that did not happen is reported as a read error rather
    # than smuggled in as a null observation.
    if a.has("attr"):
        var obs := _read_attr(target, str(a["attr"]))
        if obs["ok"]:
            res["observed"] = obs["value"]
        else:
            res["observed"] = null
            res["observed_error"] = obs["error"]
            res["measurement"] = "incomplete"
            if res["error"] == "":
                res["error"] = "observed read failed: " + obs["error"]
    _results.append(res)

func _capture_baselines() -> void:
    for w in _watch:
        var key := str(w["node"]) + "|" + str(w["attr"])
        var t := _resolve(str(w["node"]))
        if t == null:
            # ABSENT, not null: this key never got a frame-0 reading, so a
            # later delta against it is an incomplete measurement, not a
            # comparison against a legitimately captured null value.
            _baseline_missing.append(key)
            continue
        # A FAILED frame-0 read (parse/execute error, unsupported value) is not
        # a captured null: only an ok read becomes a baseline. Anything else is
        # recorded as MISSING, so a later `changed` assert cannot compare
        # against a fabricated null and pass vacuously.
        var r := _read_attr(t, str(w["attr"]))
        if r["ok"]:
            _baselines[key] = r["value"]
        else:
            _baseline_missing.append(key)
## The raw Godot types a probe read may OBSERVE, and the gate checked on the RAW
## value BEFORE any conversion. Anything else (Object, RID, Callable, ...) has no
## observation of its own and is a tagged read failure -- never str(v). `_jsonable`
## keeps its str(v) catch-all for the PUBLIC dumps, so the observation conversion
## below is a SEPARATE, type-preserving walk (`_observe_value`), not `_jsonable`.
func _is_observable(v) -> bool:
    match typeof(v):
        TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING, TYPE_VECTOR2, TYPE_VECTOR3:
            return true
        # A structured value is observable only if every member is: the recursive
        # walk in `_observe_value` refuses an unsafe member, a cycle (depth bound)
        # or excessive size/count, rather than str(v) or a fabricated null.
        TYPE_VECTOR2I, TYPE_RECT2, TYPE_COLOR, TYPE_ARRAY, TYPE_DICTIONARY:
            return true
        _:
            return false


func _read_attr(target: Object, attr: String) -> Dictionary:
    # A tagged read: a legitimate null value and a FAILED read are DIFFERENT
    # facts, and the caller must be able to tell them apart. Returning a bare
    # null for a parse/execute failure collapsed both onto "the attribute was
    # null", so a delta could silently compare against the wrong thing.
    #   {"ok": true,  "value": <jsonable>}
    #   {"ok": false, "error": "<why the read did not happen>"}
    if attr == "":
        return {"ok": false, "error": "empty attribute path"}
    var e := Expression.new()
    if e.parse(attr) != OK:
        return {"ok": false, "error": "read parse error: " + e.get_error_text()}
    var v = e.execute([], target, false)
    if e.has_execute_failed():
        return {"ok": false, "error": "read execute failed: " + e.get_error_text()}
    # Gate on the RAW type BEFORE converting. The conversion is `_observe_value`,
    # a type-preserving bounded walk that REFUSES an unsafe member (Object/RID/
    # Callable), a cycle or an over-large value instead of falling back to
    # `_jsonable`'s public str(v) catch-all: converting first would turn an
    # unsupported value into a String and let a delta pass vacuously. An
    # unsupported value has no observation of its own, so both an attribute read
    # and a frame-0 baseline refuse it.
    if not _is_observable(v):
        return {"ok": false, "error": "attribute is not a supported observable type (%d)" % typeof(v)}
    var obs := _observe_value(v)
    if not obs["ok"]:
        return {"ok": false, "error": "attribute is not a supported observable type (%d): %s" % [typeof(v), obs["error"]]}
    return {"ok": true, "value": obs["value"]}

## Bounded, type-preserving conversion of a safely representable observation.
## Returns {"ok": true, "value": <tagged>} or {"ok": false, "error": <why>}.
## Primitives keep their JSON value EXCEPT Float, which is exported losslessly in
## the engine's SCIENTIFIC form as {"__t": "Float", "__v": <token>}: an
## observation must keep 1.0, 1.000000000000001 and 1e-20 distinct, where the
## report's default JSON precision (6 decimals) would collapse them. Structured
## values are wrapped in a tagged envelope so their TYPE and KEYS survive
## comparison: Vector2i(3,4) is NOT the same observation as Array [3,4] or
## Vector2(3,4). A Dictionary is emitted as a canonically ORDERED list of
## [key, value] pairs, sorted by the OBSERVED key representation (already
## lossless), so insertion order alone is never a value change. A cycle, an unsafe
## member (Object/RID/Callable), a NON-FINITE Float or vector/Rect2/Color
## component (NaN/INF have no legitimate observation -- the official JSON path
## would turn them into a fabricated null or an overflow), or a value whose
## COMPLETE compact encoding exceeds the byte cap is refused -- never str(v),
## never truncated, never a fabricated null.
const _OBS_MAX_DEPTH := 8
const _OBS_MAX_NODES := 4096
## The finite cap on the WHOLE returned observation payload, measured over the
## ACTUAL compact JSON encoding of the finished observation (`JSON.stringify`
## then `.to_utf8_buffer().size()`), not an internal character-count estimate. It
## is a POSTCONDITION, so it honestly counts nested tags, Array brackets,
## Dictionary key/value pairs, Vector/Rect2/Color component tags and JSON
## escaping (a backslash or a multi-byte glyph is charged what it really encodes
## to). An over-cap observation is REFUSED -- never truncated into a smaller,
## equal-looking value and never replaced by a fabricated null.
const _OBS_MAX_BYTES := 65536


func _observe_value(v) -> Dictionary:
    if not _is_observable(v):
        return {"ok": false, "error": "unsupported observation type %d" % typeof(v)}
    var r := _observe_at(v, 0, {"n": 0})
    if not r["ok"]:
        return r
    # The complete byte postcondition, applied to the ACTUAL compact encoding of
    # the finished observation BEFORE it can return ok or be stored as a
    # baseline. The depth/node guards above bound only its SHAPE; this is what
    # bounds the whole payload, and a raw-String preflight inside the walk only
    # avoids constructing an already-doomed giant leaf.
    var encoded := JSON.stringify(r["value"])
    var nbytes := encoded.to_utf8_buffer().size()
    if nbytes > _OBS_MAX_BYTES:
        return {"ok": false, "error": "observation exceeds the %d byte cap (%d encoded bytes)" % [_OBS_MAX_BYTES, nbytes]}
    return r

## The lossless export of a FINITE Float, using the engine's scientific formatter
## (`String.num_scientific`), which emits a shortest round-trippable form rather
## than fixed decimal places: tiny magnitudes (1e-20), subnormals, min-normal and
## huge powers of ten all keep their exact double and never collapse under the
## report's default 6-decimal JSON precision. +0.0 and -0.0 share ONE canonical
## token because Godot's numeric equality treats them as equal -- exporting the
## raw sign bit would invent a `changed` delta the legacy comparison never saw.
## Non-finite values never reach here -- the caller refuses them first.
func _observe_float(f: float) -> Dictionary:
    var zero := 0.0
    if f == 0.0:
        f = zero
    return {"__t": "Float", "__v": String.num_scientific(f)}


func _observe_at(v, depth: int, budget: Dictionary) -> Dictionary:
    budget["n"] = int(budget["n"]) + 1
    if int(budget["n"]) > _OBS_MAX_NODES:
        return {"ok": false, "error": "observation exceeds the %d node bound" % _OBS_MAX_NODES}
    var vt := typeof(v)
    if vt == TYPE_STRING:
        # Minimal PRE-CONSTRUCTION guard: a raw String whose own UTF-8 payload
        # already exceeds the cap can never sit inside an admissible observation,
        # so refuse it before the walk builds the huge tagged structure. The
        # complete postcondition in `_observe_value` still owns the final verdict,
        # because escaping and tags can push an under-cap leaf over.
        if v.to_utf8_buffer().size() > _OBS_MAX_BYTES:
            return {"ok": false, "error": "observation exceeds the %d byte cap" % _OBS_MAX_BYTES}
    if vt == TYPE_FLOAT:
        # A finite Float is exported LOSSLESSLY as a tagged value; a non-finite
        # one has no legitimate observation at all. This guard sits above the
        # match so the shared primitive arm below never sees a Float.
        if not is_finite(v):
            return {"ok": false, "error": "non-finite Float is not an observation"}
        return {"ok": true, "value": _observe_float(v)}

    match vt:
        TYPE_NIL:
            return {"ok": true, "value": null}
        TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
            # A finite Float returned above; Bool/Int/String keep their plain JSON
            # value. TYPE_FLOAT is listed so the primitives share one arm.
            return {"ok": true, "value": v}
        TYPE_VECTOR2:
            if not (is_finite(v.x) and is_finite(v.y)):
                return {"ok": false, "error": "non-finite Vector2 component is not an observation"}
            return {"ok": true, "value": [_observe_float(v.x), _observe_float(v.y)]}
        TYPE_VECTOR3:
            if not (is_finite(v.x) and is_finite(v.y) and is_finite(v.z)):
                return {"ok": false, "error": "non-finite Vector3 component is not an observation"}
            return {"ok": true, "value": [_observe_float(v.x), _observe_float(v.y), _observe_float(v.z)]}
        TYPE_VECTOR2I:
            return {"ok": true, "value": {"__t": "Vector2i", "__v": [v.x, v.y]}}
        TYPE_RECT2:
            if not (is_finite(v.position.x) and is_finite(v.position.y)
                    and is_finite(v.size.x) and is_finite(v.size.y)):
                return {"ok": false, "error": "non-finite Rect2 component is not an observation"}
            return {"ok": true, "value": {"__t": "Rect2", "__v": [
                _observe_float(v.position.x), _observe_float(v.position.y),
                _observe_float(v.size.x), _observe_float(v.size.y)]}}
        TYPE_COLOR:
            if not (is_finite(v.r) and is_finite(v.g) and is_finite(v.b) and is_finite(v.a)):
                return {"ok": false, "error": "non-finite Color component is not an observation"}
            return {"ok": true, "value": {"__t": "Color", "__v": [
                _observe_float(v.r), _observe_float(v.g),
                _observe_float(v.b), _observe_float(v.a)]}}
        TYPE_ARRAY:
            if depth >= _OBS_MAX_DEPTH:
                return {"ok": false, "error": "observation exceeds the %d level depth bound" % _OBS_MAX_DEPTH}
            var items := []
            for e in v:
                var r := _observe_at(e, depth + 1, budget)
                if not r["ok"]:
                    return r
                items.append(r["value"])
            return {"ok": true, "value": {"__t": "Array", "__v": items}}
        TYPE_DICTIONARY:
            if depth >= _OBS_MAX_DEPTH:
                return {"ok": false, "error": "observation exceeds the %d level depth bound" % _OBS_MAX_DEPTH}
            var pairs := []
            for k in v.keys():
                var kr := _observe_at(k, depth + 1, budget)
                if not kr["ok"]:
                    return kr
                var vr := _observe_at(v[k], depth + 1, budget)
                if not vr["ok"]:
                    return vr
                pairs.append([kr["value"], vr["value"]])
            # Canonical order by the OBSERVED key representation, which is already
            # lossless (a Float key is a scientific token), so close but distinct
            # keys never collapse into one sort token and the order is total and
            # deterministic. The same entries in any insertion order compare equal.
            pairs.sort_custom(func(a, b): return JSON.stringify(a[0]) < JSON.stringify(b[0]))
            return {"ok": true, "value": {"__t": "Dictionary", "__v": pairs}}
        _:
            return {"ok": false, "error": "unsupported observation type %d" % vt}


## Type-aware equality over the SUPPORTED observed representation. Both sides of
## a delta went through the SAME `_observe_value` walk, so a supported observation
## is canonical and carries its type: plain JSON scalars (null/bool/int/string), a
## tagged Float envelope and tagged structured envelopes (Vector2i/Rect2/Color/
## Array/Dictionary). A delta must NEVER feed these to Godot's native `==`/`!=`:
## an int baseline against a tagged-Float current is `int != Dictionary`, an
## engine "Invalid operands" error that stopped the whole probe (strictcontroller
## RC1) instead of deciding the assert. This walks the VALUES instead:
##   * two envelopes must share `__t` AND compare equal on `__v`, so
##     Vector2i(3,4) is not Array [3,4] and a Dictionary is not an Array;
##   * an Array compares element-wise and in ORDER (a nested difference is real);
##   * a Dictionary's `__v` is already the canonically sorted pair list, so entry
##     insertion order alone is never a change while a key/value/type difference
##     is;
##   * a scalar TYPE difference is a real change: int 1 and tagged Float 1.0 are
##     DIFFERENT observations;
##   * a legitimately captured null baseline compares equal to a null current.
## Bounded and total: it recurses only over shapes `_observe_value` already
## admitted, so it can never see a cycle, an over-cap payload or a live Object.
func _observe_equal(a, b) -> bool:
    var ta := typeof(a)
    var tb := typeof(b)
    var at := ""
    var bt := ""
    if ta == TYPE_DICTIONARY and a.has("__t"):
        at = str(a.get("__t", ""))
    if tb == TYPE_DICTIONARY and b.has("__t"):
        bt = str(b.get("__t", ""))
    if at != "" or bt != "":
        # At least one side is a TAGGED envelope. Both must be tagged with the
        # SAME type, or they are different observations -- an envelope never
        # equals a bare scalar/Array of the same shape.
        if at == "" or bt == "" or at != bt:
            return false
        if at == "Dictionary":
            # `__v` is the sorted pair list; recurse as an Array so entry order
            # is canonical and each key/value pair is itself walked.
            return _observe_equal(a.get("__v", []), b.get("__v", []))
        return _observe_equal(a.get("__v", null), b.get("__v", null))
    if ta == TYPE_DICTIONARY or tb == TYPE_DICTIONARY:
        # No UNTAGGED Dictionary can come out of `_observe_value`; if one does
        # (hand-built report data), compare its keys rather than native `==`.
        if ta != TYPE_DICTIONARY or tb != TYPE_DICTIONARY:
            return false
        return str(a) == str(b)
    if ta == TYPE_ARRAY or tb == TYPE_ARRAY:
        if ta != TYPE_ARRAY or tb != TYPE_ARRAY:
            return false
        if a.size() != b.size():
            return false
        for i in a.size():
            if not _observe_equal(a[i], b[i]):
                return false
        return true
    if ta != tb:
        # A scalar TYPE difference (int vs bool vs String vs tagged Float) is a
        # real value change: 1 and 1.0 are not the same observation.
        return false
    return a == b


func _eval_delta(a: Dictionary, target: Node, res: Dictionary) -> void:
    var attr := str(a.get("attr", ""))
    var mode := str(a.get("mode", "changed"))
    var key := str(a.get("node", "")) + "|" + attr
    res["expr"] = attr + " " + mode + " since frame 0"
    var cur_read := _read_attr(target, attr)
    if not cur_read["ok"]:
        # The CURRENT read did not happen (parse/execute failure, or no
        # JSONable value). That is an incomplete measurement, never a
        # comparison: the delta has no current side to compare.
        res["error"] = "delta read failed for %s on %s: %s" % [attr, str(a.get("node", "")), cur_read["error"]]
        res["measurement"] = "incomplete"
        res["actual"] = {"baseline": null, "current": null,
                         "current_error": cur_read["error"]}
        res["passed"] = false
        _results.append(res)
        return
    var cur = cur_read["value"]
    if not _baselines.has(key):
        # No frame-0 baseline was ever captured for this attribute (its node
        # did not resolve at frame 0, or its frame-0 read failed). A missing
        # baseline is an INCOMPLETE measurement, not a comparison that happens
        # to have null on one side: `changed` against a fabricated null would
        # pass vacuously. A legitimately captured null value keeps flowing
        # through below.
        res["error"] = ("missing frame-0 baseline for %s on %s -- the node was not resolvable at frame 0, or its frame-0 read failed"
                        % [attr, str(a.get("node", ""))])
        res["measurement"] = "incomplete"
        res["actual"] = {"baseline": null, "current": cur, "baseline_missing": true}
        res["passed"] = false
        _results.append(res)
        return
    var base = _baselines.get(key, null)
    res["actual"] = {"baseline": base, "current": cur}
    # Both polarities decide through the TYPE-AWARE walk, never native `==`/`!=`:
    # a heterogeneous pair (int vs tagged Float) would otherwise be an engine
    # error rather than an assert. `changed` is the negation of the same walk.
    var same := _observe_equal(cur, base)
    res["passed"] = (not same) if mode == "changed" else same
    _results.append(res)

func _truthy(v) -> bool:
    # Assertion truth is decided ONLY from a value that has a truth of its own:
    # a Boolean as-is, a non-empty String, and bool() for the rest (numbers,
    # null, objects). A String observation rides in `actual` untouched either
    # way -- this only decides the assert.
    var t := typeof(v)
    if t == TYPE_BOOL:
        return v
    if t == TYPE_STRING:
        return v != ""
    return bool(v)
func _resolve(name: String) -> Node:
    if name == "":
        return get_tree().current_scene
    if name.begins_with("/") or name.begins_with("res:"):
        return get_node_or_null(NodePath(name))
    var scene := get_tree().current_scene
    # A "/"-separated name is a PATH (e.g. "HUD/PausedLabel"), not a node name:
    # it resolves relative to the current scene or not at all. It used to fall
    # back to find_child(leaf) anywhere in the tree, so `X/PlanEditKind1`
    # clicked a node the written path never named. The caller reports a path
    # that does not resolve as a spec error (_is_path / _refuse_path).
    if "/" in name:
        if scene == null:
            return null
        return scene.get_node_or_null(NodePath(name))
    return get_tree().get_root().find_child(name, true, false)
## A PATH target -- scene-relative (`HUD/Label`) or absolute (`/root/...`) --
## as opposed to a bare node name, which is searched by name. A node name
## cannot contain "/", so the slash alone decides it.
func _is_path(name: String) -> bool:
    return "/" in name
## The spec named a path that is not in the tree. That is the author's error,
## so it goes to the spec_errors the python side reports, not to push_error.
func _refuse_path(kind: String, name: String, spec: String) -> void:
    _spec_errors.append("frame %d: %s target %s is a path and does not resolve in the scene tree (spec: %s)" % [_frame, kind, name, spec])
func _jsonable(v):
    match typeof(v):
        TYPE_NIL:
            return null
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
    var out := {"frames": _frame, "asserts": _results, "nodes": {}, "captures": _captures,
        "spec_errors": _spec_errors}
    # Completeness accounting: a report that lost a scheduled assertion, or
    # that never reached its frame budget, must not be readable as a shorter
    # but successful measurement. `complete` is false whenever the game quit
    # (or died) before the frame budget was spent; `asserts_missing` is the
    # scheduled assertions that produced no result row at all.
    var scheduled := 0
    var omitted := 0
    for e in _timeline:
        var a = e.get("assert", [])
        if typeof(a) == TYPE_ARRAY:
            scheduled += a.size()
            if int(e.get("at", -1)) >= _frame:
                omitted += a.size()
    out["scheduled_asserts"] = scheduled
    out["asserts_omitted"] = omitted
    out["asserts_missing"] = max(0, scheduled - _results.size())
    out["complete"] = _frame >= _max
    out["baseline_missing"] = _baseline_missing
    var incomplete_count := 0
    for r in _results:
        if str(r.get("measurement", "")) == "incomplete":
            incomplete_count += 1
    out["asserts_incomplete"] = incomplete_count

    # Requested-but-unobserved capture accounting: any frame listed in
    # AITELIER_PROBE_CAPTURE_AT that never produced a capture row (the run
    # ended first, or frame_post_draw never fired for it -- frame 0 included)
    # is kept as an explicit incomplete row and named. It never vanishes and
    # never reads as a shorter but complete corpus.
    var unobserved := []
    for f in _capture_at.keys():
        _captures.append(_capture_failure(int(f),
            "requested capture frame %d was never observed at frame_post_draw" % int(f)))
        unobserved.append(int(f))
    unobserved.sort()
    out["captures_unobserved"] = unobserved

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
                render: bool, timing: dict | None = None,
                raw: list | None = None,
                raw_label: str | None = None) -> tuple[dict, list, bool]:
    if state_path.exists():
        state_path.unlink()
    t_proc = time.monotonic()
    try:
        cp = _run(args, timeout=timeout, extra_env=env, render=render)
        stdout, stderr = cp.stdout, cp.stderr
        returncode, timed_out = cp.returncode, False
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired carries both streams as bytes; when a caller asked for
        # the raw log the killed run's own account must survive the kill.
        def _s(v):
            return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
        stdout, stderr = _s(e.stdout), _s(e.stderr)
        returncode, timed_out = 124, True
    except FileNotFoundError as e:
        # Render mode shells out to xvfb-run; if the image lacks it there is no
        # run at all. The caller's headless retry is what keeps the gate alive.
        if not render:
            raise
        stdout, stderr = "", str(e)
        returncode, timed_out = 127, False
    proc_sec = time.monotonic() - t_proc
    if raw is not None:
        # The label is the pass identity the manifest carries for this raw
        # stream (import / scenario:<name> / control:<scene>@<frames> / the
        # script's own name), so per-pass grouping survives the copy.
        raw.append({"label": raw_label, "stdout": stdout, "stderr": stderr,
                    "returncode": returncode, "timed_out": timed_out})
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


def _invalidate_observation_for_png(entry: dict, why: str) -> None:
    """A missing/unreadable PNG means there is NO frame the observation could
    describe, so the same-frame observation is invalidated in place
    (complete=false + explicit reason). The row and the raw png_error stay."""
    obs = entry.get("text_observation")
    if isinstance(obs, dict):
        obs["complete"] = False
        reasons = obs.get("incomplete_reasons")
        if not isinstance(reasons, list):
            reasons = obs["incomplete_reasons"] = []
        reasons.append("same-frame observation invalidated: %s (%s)" % (why, entry.get("file")))
        obs["png_error"] = why


# The same ceiling the probe applies, enforced here on the observation as the
# outgoing serializer (_Handler: plain json.dumps, ASCII-escaped) encodes it.
_TEXT_OBS_MAX_BYTES = 65536
_TEXT_OBS_SHORT_CHARS = 256


def _text_obs_wire_bytes(obs: dict) -> int:
    """Writes the observation's own wire size into "bytes" until it is
    self-consistent: the number covers every field, "bytes" included."""
    n = len(json.dumps(obs).encode())
    while obs.get("bytes") != n:
        obs["bytes"] = n
        n = len(json.dumps(obs).encode())
    return n


def _seal_text_observation(obs) -> dict:
    """The ONE final step for a capture row's observation, run after every
    mutation (absent observation, PNG invalidation). Over the ceiling, the whole
    observation becomes a small fixed refusal: no rows, no lists, and a string
    field only when it is short -- never a truncated corpus."""
    if not isinstance(obs, dict):
        obs = {"complete": False,
               "error": "probe reported no text observation object for this frame"}
    obs["bytes_encoding"] = "json.dumps"
    n = _text_obs_wire_bytes(obs)
    if n <= _TEXT_OBS_MAX_BYTES:
        return obs

    def short(v):
        return v if isinstance(v, str) and len(v) <= _TEXT_OBS_SHORT_CHARS else None

    frame = obs.get("frame")
    out = {"frame": frame if isinstance(frame, int) else None,
           "complete": False, "byte_capped": True,
           "locale": short(obs.get("locale")),
           "error": short(obs.get("error")),
           "png_error": short(obs.get("png_error")),
           "refused_bytes": n,
           "controls": [], "unsupported": [], "hidden_surfaces": [],
           "incomplete_reasons": [],
           "refused": ["text observation exceeds the %d-byte ceiling as serialized for "
                       "the response; the whole observation is refused rather than "
                       "truncated" % _TEXT_OBS_MAX_BYTES],
           "bytes_encoding": "json.dumps"}
    _text_obs_wire_bytes(out)
    return out


def _attach_pngs(captures: list, cap_dir: Path, timing: dict | None = None) -> list:
    """Inline each captured PNG as base64 and keep only its basename: the sidecar
    mounts the workspace read-only, so the bytes have to ride home in the JSON
    body, and the container-local path means nothing to the caller."""
    out = []
    t0 = time.monotonic()
    png_bytes = 0
    png_missing = []
    for c in captures:
        png = cap_dir / Path(str(c.get("file", ""))).name
        entry = {"frame": c.get("frame"), "file": png.name}
        # The probe's same-frame text/geometry observation is forwarded verbatim.
        # A capture row with NO observation is reported as an ABSENT observation,
        # never dropped and never turned into a complete empty corpus.
        if "text_observation" in c:
            entry["text_observation"] = c["text_observation"]
        else:
            entry["text_observation"] = {
                "complete": False,
                "error": "probe reported no text observation for this frame"}
        if png.is_file():
            try:
                raw = png.read_bytes()
            except OSError as e:
                # A read error is VISIBLE on the row; the PNG is not silently
                # treated as absent, and the observation still rides home.
                entry["png_error"] = "read error: %s" % e
                png_missing.append(png.name)
                _invalidate_observation_for_png(entry, entry["png_error"])
            else:
                png_bytes += len(raw)
                entry["png_b64"] = base64.b64encode(raw).decode()
        else:
            entry["png_error"] = "missing PNG file"
            png_missing.append(png.name)
            _invalidate_observation_for_png(entry, entry["png_error"])
        entry["text_observation"] = _seal_text_observation(entry["text_observation"])
        out.append(entry)
    if timing is not None:
        timing["png_b64_sec"] = timing.get("png_b64_sec", 0.0) + (time.monotonic() - t0)
        timing["png_bytes"] = timing.get("png_bytes", 0) + png_bytes
        if png_missing:
            timing["png_missing"] = png_missing
    return out


def _run_probe(dst: Path, state_path: Path, frames: int, timeout: int,
               extra: dict, scene: str = "",
               capture_at: list[int] | None = None,
               timing: dict | None = None,
               render: bool = True,
               raw: list | None = None,
               raw_label: str | None = None) -> tuple[dict, list, bool]:
    """One probe run. Returns (probe_report, errors, timed_out) — the captures
    ride inside probe_report, because callers (and the unit tests that fake this)
    depend on the 3-tuple. When `raw` is given, each pass's full untruncated
    stdout/stderr is appended to it for the invocation's owned evidence."""
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
                                        timing=timing, raw=raw,
                                        raw_label=raw_label)
    if render and not probe:
        # A broken X/GL setup must degrade to yesterday's behaviour, not take the
        # whole playtest gate down: retry once, headless, with capture off.
        env.pop("AITELIER_PROBE_CAPTURE", None)
        env.pop("AITELIER_PROBE_CAPTURE_AT", None)
        render = False
        if timing is not None:
            timing["headless_retry"] = True
        probe, errs, timed_out = _probe_once(args, env, state_path, timeout, False,
                                             timing=timing, raw=raw,
                                             raw_label=raw_label)
    if probe:
        # Report which mode actually produced this, so a silent fallback to the
        # pixel-blind path is visible rather than looking like "no captures".
        probe["render_mode"] = "render" if render else "headless"
        probe["captures"] = (_attach_pngs(probe.get("captures", []), cap_dir,
                                          timing=timing)
                             if render and capture_at else [])
    return probe, errs, timed_out


class _InvocationLogs(list):
    """Raw streams plus the caller's exact owned HOME/pass-label registries.

    This remains a list for existing helper/probe callers. Only invocation
    retention callers transfer HOME cleanup ownership through these registries.
    """
    def __init__(self, homes: list, labels: list):
        super().__init__()
        self.homes = homes
        self.labels = labels


def _playtest_legacy(dst: Path, frames: int, input_action: str, timeout: int,
                     ledger: dict | None = None,
                     cap_limit: int | None = None,
                     retain: list | None = None,
                     retain_errors: list | None = None,
                     retain_patterns: list | None = None,
                     retain_requested: bool = False,
                     corr: dict | None = None,
                     raw_logs: list | None = None,
                     user_dir_name: str | None = None) -> dict:
    """Canned smoke test with owned user:// retention and total cleanup."""
    home = Path(tempfile.mkdtemp(prefix="godot_home_"))
    raw_logs = raw_logs if raw_logs is not None else []
    homes, labels = [home], [{"scenario": "(legacy smoke test)"}]
    if isinstance(raw_logs, _InvocationLogs):
        homes, labels = raw_logs.homes, raw_logs.labels
        homes.append(home)
        labels.append({"scenario": "(legacy smoke test)"})
    try:
        return _playtest_legacy_inner(
            dst, frames, input_action, timeout, ledger=ledger, cap_limit=cap_limit,
            retain=retain, retain_errors=retain_errors, corr=corr,
            retain_patterns=retain_patterns, retain_requested=retain_requested,
            raw_logs=raw_logs, user_dir_name=user_dir_name, home=home)
    except BaseException as exc:
        if retain or retain_errors or retain_patterns or retain_requested:
            try:
                _retain_copy(retain, homes, raw_logs,
                             {**dict(corr or {}), "mode": "render"},
                             extra_errors=list(retain_errors or [])
                             + ["unexpected smoke error: %r" % exc],
                             pass_labels=labels, user_dir_name=user_dir_name,
                             patterns=retain_patterns, requested=True)
            except Exception:
                pass
        raise
    finally:
        for h in homes:
            shutil.rmtree(h, ignore_errors=True)


def _playtest_legacy_inner(dst: Path, frames: int, input_action: str, timeout: int,
                     ledger: dict | None = None,
                     cap_limit: int | None = None,
                     retain: list | None = None,
                     retain_errors: list | None = None,
                     retain_patterns: list | None = None,
                     retain_requested: bool = False,
                     corr: dict | None = None,
                     raw_logs: list | None = None,
                     user_dir_name: str | None = None,
                     home: Path | None = None) -> dict:
    """Run the canned smoke test in its registered disposable HOME."""
    state_path = dst.parent / "probe_state.json"
    raw_logs = raw_logs if raw_logs is not None else []
    t_legacy: dict = {}
    t_legacy_start = time.monotonic()
    # `raw` is passed only when a retention request needs the full streams; the
    # existing probe fakes (and the plain path) keep the exact signature when no
    # evidence was asked for.
    legacy_kwargs = ({"raw": raw_logs, "raw_label": "(legacy smoke test)"}
                     if (retain or retain_errors or retain_patterns
                         or retain_requested) else {})
    probe, errs, timed_out = _run_probe(
        dst, state_path, frames, timeout, {"AITELIER_PROBE_INPUT": input_action, **_home_env(str(home))},
        capture_at=_capture_frames(frames, limit=cap_limit), timing=t_legacy,
        **legacy_kwargs)
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
    report = {"passed": passed, "frames": probe.get("frames", frames), "errors": errs,
              "native_debt": debt,
              "state": probe.get("nodes", {}), "behavior": None,
              "captures": probe.get("captures", []),
              "render_mode": probe.get("render_mode", "headless"),
              "spec_used": False, "summary": summary}
    if retain or retain_errors or retain_patterns or retain_requested:
        retention = _retain_copy(retain, raw_logs.homes if isinstance(raw_logs, _InvocationLogs) else [home], raw_logs,
                                 {**dict(corr or {}), "mode": report["render_mode"]},
                                 extra_errors=retain_errors,
                                 pass_labels=raw_logs.labels if isinstance(raw_logs, _InvocationLogs) else [{"scenario": "(legacy smoke test)"}],
                                 user_dir_name=user_dir_name,
                                 patterns=retain_patterns,
                                 requested=bool(retain or retain_errors
                                                or retain_patterns or retain_requested))
        report["retention"] = retention
        if not retention["ok"]:
            report["passed"] = False
            report["summary"] = summary + (
                "  Invocation evidence NOT fully retained; manifest: %s."
                % retention["manifest"])
    return report


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
# The two levels above a timeline entry are checked the same way: a key outside
# these sets is a spec error and the scenario does not run. A key nothing reads
# is a second reading of the file -- assert blocks parked under
# `reviewer_notes:` read as preconditions to the author and to a static rule,
# and as nothing to this harness. Every key here has a reader:
#   scene / frames / scenarios, name / timeline / scene -- _playtest_spec below;
#   actions / surface -- the game repo's contract tests read _common.yaml;
#   repeatability -- the game repo's gate launcher replays the scenario;
#   description -- read by the type check in _playtest_spec: a plain string
#     only, because a mapping or a list under it could carry assert blocks.
# Each key also has ONE type. A value of another type is a spec error: a
# mapping parked under `repeatability:` or `name:` carries assert blocks no
# reader evaluates, and the launcher reads `repeatability: 'yes'` as off.
# The shapes are the ones the readers above consume (wuxia _common.yaml:
# actions is a list of action names, surface maps a node to attribute names).
def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


_SPEC_KEY_TYPES = {
    "scene": (lambda v: isinstance(v, str), "a string"),
    "frames": (lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer"),
    "scenarios": (lambda v: isinstance(v, list) and len(v) > 0, "a non-empty list"),
    "actions": (_is_str_list, "a list of strings"),
    "surface": (lambda v: isinstance(v, dict) and all(
        isinstance(k, str) and _is_str_list(x) for k, x in v.items()),
        "a mapping of node name to a list of strings"),
}
_SCENARIO_KEY_TYPES = {
    "name": (lambda v: isinstance(v, str), "a string"),
    "timeline": (lambda v: isinstance(v, list), "a list"),
    "scene": (lambda v: isinstance(v, str), "a string"),
    "repeatability": (lambda v: isinstance(v, bool), "true or false"),
    "description": (lambda v: isinstance(v, str), "a plain string"),
}
_SPEC_KEYS = set(_SPEC_KEY_TYPES)
_SCENARIO_KEYS = set(_SCENARIO_KEY_TYPES)


def _key_type_errors(where: str, mapping: dict, types: dict) -> list:
    return ["%s has key %s of type %s - it must be %s."
            % (where, k, type(mapping[k]).__name__, what)
            for k, (ok, what) in types.items() if k in mapping and not ok(mapping[k])]


# A list-form assert item is read by the probe's _eval_assert: node, expr and
# name, or node, attr and mode for a delta assert, which never reads expr.
_ASSERT_ITEM_KEYS = {"name", "node", "expr", "mode", "attr"}


def _assert_errors(raw) -> list:
    if isinstance(raw, dict):
        return []
    if not isinstance(raw, list):
        return ["`assert` is a %s - it must be a mapping or a list" % type(raw).__name__]
    errors = []
    for j, a in enumerate(raw):
        if not isinstance(a, dict):
            errors.append("assert item %d is a %s - it must be a mapping"
                          % (j, type(a).__name__))
            continue
        unknown = sorted(str(k) for k in a if k not in _ASSERT_ITEM_KEYS)
        if unknown:
            errors.append("assert item %d has unknown key(s) %s - allowed: %s"
                          % (j, ", ".join(unknown), ", ".join(sorted(_ASSERT_ITEM_KEYS))))
        if "mode" in a and "expr" in a:
            errors.append("assert item %d has both `mode` and `expr`; a `mode` "
                          "assert compares `attr` with frame 0 and never reads "
                          "`expr`. Write two items." % j)
    return errors


_AIM_BUTTONS = ("left", "right", "middle")


def _aim_errors(key: str, aim) -> list:
    """The probe parses an aim token by token and a later offset or button
    overwrites an earlier one, so `X +0,500 +0,0` clicks at +0,0."""
    toks = str(aim).split(" ")[1:]
    offsets = [t for t in toks if t.startswith(("+", "-")) or "," in t]
    buttons = [t for t in toks if t in _AIM_BUTTONS]
    errors = []
    if len(offsets) > 1:
        errors.append("%s %r has %d offsets (%s); an aim takes one offset"
                      % (key, aim, len(offsets), ", ".join(offsets)))
    if len(buttons) > 1:
        errors.append("%s %r has %d buttons (%s); a click takes one button"
                      % (key, aim, len(buttons), ", ".join(buttons)))
    return errors


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
    latest = None   # (index, at) of the first entry holding the highest `at` so far
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
        # An entry with no `at` had two readings: rebuilt `actions:`/`clicks:`/
        # `hovers:` entries ran at frame 0, while `press:`/`click:`/`assert:`
        # kept no `at` and the probe never ran them -- an assert that is never
        # evaluated, in a scenario that stays green.
        if "at" not in e:
            errors.append("timeline entry %d has no `at`. Every entry runs on the "
                          "frame its `at` names; write it (`at: 0` is the first "
                          "frame)." % i)
            continue
        at_raw = e["at"]
        if isinstance(at_raw, bool) or not isinstance(at_raw, (int, float)):
            errors.append(
                "timeline entry %d has a non-numeric `at`: %r. Frames are single "
                "integers -- a range or a list is not supported, write one entry "
                "per frame (`- {at: 3, ...}` … `- {at: 15, ...}`)." % (i, at_raw))
            continue
        # A fractional frame has two readings: int() below runs `at: 185.5` at
        # frame 185, while anything comparing the written value sees 185.5.
        # A float that is a whole number (185.0) has one reading and stays.
        if isinstance(at_raw, float) and not at_raw.is_integer():
            errors.append(
                "timeline entry %d has a non-integer `at`: %r. Frames are whole "
                "numbers; write the frame the entry should run on." % (i, at_raw))
            continue
        at = int(at_raw)
        if at < 0:
            errors.append("timeline entry %d has a negative `at`: %r" % (i, at_raw))
            continue
        # The probe fires entries by frame, not by their place in the file, so
        # an entry written after a later frame runs before it. File order must
        # therefore not decrease (equal frames are fine): otherwise the order a
        # reader sees and the order the probe runs are two readings of one file.
        if latest is not None and at < latest[1]:
            errors.append(
                "timeline entry %d (at: %d) is written after entry %d (at: %d); "
                "entries run in frame order, so `at` must not decrease in file "
                "order. Move entry %d above entry %d, or change its frame."
                % (i, at, latest[0], latest[1], i, latest[0]))
        elif latest is None or at > latest[1]:
            latest = (i, at)
        shape_errors = _assert_errors(e["assert"]) if "assert" in e else []
        for key in ("click", "hover", "clicks", "hovers"):
            aims = e.get(key) or []
            for aim in ([aims] if isinstance(aims, str) else aims):
                shape_errors.extend(_aim_errors(key, aim))
        if shape_errors:
            errors.extend("timeline entry %d (at: %d): %s" % (i, at, m)
                          for m in shape_errors)
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
                   cap_limit: int | None = None,
                   retain: list | None = None,
                   retain_errors: list | None = None,
                   retain_patterns: list | None = None,
                   retain_requested: bool = False,
                   corr: dict | None = None,
                   raw_logs: list | None = None,
                   user_dir_name: str | None = None) -> dict:
    """Authored-spec playtest with a TOTAL owned-home cleanup guarantee.

    Every scenario/control throwaway HOME is REGISTERED the moment it is
    created (never after its pass), and the finally below removes every
    registered home on success, failure, timeout AND an unexpected exception —
    but only after a truthful bounded retention attempt has written its
    manifest when the request declared retention. The original error is always
    re-raised unchanged, never replaced by a green or hidden behind a
    retention error."""
    scen_homes = raw_logs.homes if isinstance(raw_logs, _InvocationLogs) else []
    scen_labels = raw_logs.labels if isinstance(raw_logs, _InvocationLogs) else []
    retention_cell: dict = {}
    try:
        return _playtest_spec_inner(
            dst, spec, frames, timeout, ledger=ledger, cap_limit=cap_limit,
            retain=retain, retain_errors=retain_errors, corr=corr,
            retain_patterns=retain_patterns, retain_requested=retain_requested,
            raw_logs=raw_logs, user_dir_name=user_dir_name,
            scen_homes=scen_homes, scen_labels=scen_labels,
            retention_cell=retention_cell)
    except BaseException as exc:
        if ((retain or retain_errors or retain_patterns or retain_requested)
                and "value" not in retention_cell):
            # An error nobody predicted must not erase the generated evidence:
            # attempt the same bounded retention (the manifest records the
            # unexpected error), then re-raise the ORIGINAL failure.
            try:
                retention_cell["value"] = _retain_copy(
                    retain, scen_homes, list(raw_logs or []),
                    {**dict(corr or {}), "mode": "headless"},
                    extra_errors=list(retain_errors or [])
                    + ["unexpected error before retention: %r" % exc],
                    pass_labels=scen_labels, user_dir_name=user_dir_name,
                    patterns=retain_patterns, requested=True)
            except Exception:
                pass
        raise
    finally:
        # Owned roots only: exactly the homes this invocation registered.
        for h in scen_homes:
            shutil.rmtree(h, ignore_errors=True)


def _playtest_spec_inner(dst: Path, spec: dict, frames: int, timeout: int,
                         ledger: dict | None = None,
                         cap_limit: int | None = None,
                         retain: list | None = None,
                         retain_errors: list | None = None,
                         retain_patterns: list | None = None,
                         retain_requested: bool = False,
                         corr: dict | None = None,
                         raw_logs: list | None = None,
                         user_dir_name: str | None = None,
                         scen_homes: list | None = None,
                         scen_labels: list | None = None,
                         retention_cell: dict | None = None) -> dict:
    """Authored-spec playtest: run ONE isolated headless pass per scenario, driving
    its input timeline and evaluating its Expression assertions against live nodes.

    Gate split: ``passed`` (HARD, loops the goal-loop) covers crash / didn't-run,
    plus the two ways a scenario can look green without testing anything -- a
    malformed timeline, and input that never reached the game. Per-scenario
    assertion outcomes stay ADVISORY (``behavior``) so a wrong or flaky assertion
    can never stall a build that otherwise runs clean."""
    # A spec with keys is read as a spec or refused; the canned smoke test is
    # only for a request that carries no spec (see playtest_project).
    spec_errors = []
    if not isinstance(spec, dict):
        spec_errors.append("spec is a %s - it must be a mapping with a `scenarios` "
                           "list. No scenario was run." % type(spec).__name__)
        spec = {}
    elif "scenarios" not in spec:
        spec_errors.append("spec has no `scenarios` key (top-level keys: %s). No "
                           "scenario was run." % ", ".join(sorted(str(k) for k in spec)))
    # corr is OPTIONAL in the signature and MUST stay so: internal/CLI callers
    # pass None. Normalise it to the empty mapping once, here, so every
    # `{**corr, ...}` expansion below works without each call site re-deciding.
    # The HTTP handler always passes the server-mandated correlation mapping,
    # which survives this copy unchanged — the mandatory correlation is a
    # server property, not a reason to crash a None caller.
    corr = dict(corr or {})
    header_errors = _key_type_errors("spec", spec, _SPEC_KEY_TYPES)
    spec_errors.extend(m + " No scenario was run." for m in header_errors)
    scene = str(spec.get("scene", "") or "")
    default_frames = int(spec.get("frames") or frames) if not header_errors else frames
    scenarios = spec.get("scenarios") if isinstance(spec.get("scenarios"), list) else []
    state_path = dst.parent / "probe_state.json"
    spec_path = dst.parent / "scenario_spec.json"

    scen_results, all_errors, captures = [], [], []
    scen_timing: list[dict] = []
    ctrl_timing: list[dict] = []
    all_debt: list[dict] = []
    scen_nodes: list[dict] = []
    scen_frames: list[int] = []
    scen_scenes: list[str] = []
    # Every scenario's (and control's) throwaway $HOME, kept until the owned
    # retention below has copied the declared artifacts out of them.
    scen_homes = scen_homes if scen_homes is not None else []
    scen_labels = scen_labels if scen_labels is not None else []
    raw_logs = raw_logs if raw_logs is not None else []
    ran_any = crashed = False
    last_state: dict = {}
    render_mode = "headless"
    unknown_spec = sorted(str(k) for k in spec if k not in _SPEC_KEYS)
    if unknown_spec:
        spec_errors.append(
            "spec has unknown top-level key(s) %s - allowed: %s. No scenario was run."
            % (", ".join(unknown_spec), ", ".join(sorted(_SPEC_KEYS))))
    header_bad = bool(spec_errors)
    for i, sc in enumerate(scenarios):
        refused = header_bad
        if not isinstance(sc, dict):
            spec_errors.append("scenario %d is a %s - it must be a mapping. The "
                               "scenario was not run." % (i, type(sc).__name__))
            sc, refused = {}, True
        name = str(sc.get("name", "scenario"))
        tl_raw = sc.get("timeline")
        timeline, terrs = _normalize_timeline(tl_raw if isinstance(tl_raw, list) else [])
        spec_errors.extend("scenario %r: %s" % (name, m) for m in terrs)
        unknown = sorted(str(k) for k in sc if k not in _SCENARIO_KEYS)
        if unknown:
            spec_errors.append(
                "scenario %r has unknown key(s) %s - allowed: %s. The scenario was not run."
                % (name, ", ".join(unknown), ", ".join(sorted(_SCENARIO_KEYS))))
        bad_types = _key_type_errors("scenario %r" % name, sc, _SCENARIO_KEY_TYPES)
        spec_errors.extend(m + " The scenario was not run." for m in bad_types)
        if refused or unknown or bad_types:
            scen_results.append({"name": name, "ran": False, "errors": [],
                                 "native_debt": [], "asserts": [], "passed": False,
                                 "pressed": False, "input_dead": False})
            scen_nodes.append({})
            scen_frames.append(0)
            scen_scenes.append("")
            continue
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
            # scenario that passed it. An input past the cap never fires either,
            # so it is refused the same way.
            dropped = sorted({int(e.get("at", 0)) for e in timeline
                              if int(e.get("at", 0)) >= _MAX_SPEC_FRAMES})
            if dropped:
                spec_errors.append(
                    "scenario %r: timeline entry(ies) scheduled at frame(s) %s, past "
                    "the %d-frame cap - they would never run. Reach the same "
                    "state sooner, or act and assert earlier."
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
        scen_homes.append(Path(sc_home))
        # Registered the moment it exists; the label carries the pass identity
        # the manifest rows report (and marks this home a scenario, not a
        # control, for retention).
        scen_labels.append({"scenario": name})
        # Only a retention request needs the full raw streams, and the existing
        # probe fakes (and the plain path) keep their exact signature when none
        # is asked for.
        sc_kwargs = ({"raw": raw_logs, "raw_label": "scenario:%s" % name}
                     if (retain or retain_errors or retain_patterns
                         or retain_requested) else {})
        t_scenario: dict = {}
        t_scen_start = time.monotonic()
        try:
            probe, errs, timed_out = _run_probe(
                dst, state_path, sframes, timeout,
                {"AITELIER_PROBE_SPEC": str(spec_path), **_home_env(sc_home)},
                scene=sc_scene,
                capture_at=_capture_frames(sframes, timeline, limit=cap_limit),
                timing=t_scenario, **sc_kwargs)
        finally:
            # When this invocation declared retention the home must outlive the
            # run so its artifacts can be copied out below; otherwise it goes at
            # once, exactly as before. Either way nothing survives the call.
            if not (retain or retain_errors or retain_patterns or retain_requested):
                shutil.rmtree(sc_home, ignore_errors=True)
        t_scenario["frames_stepped"] = (probe.get("timing") or {}).get(
            "frames_stepped", probe.get("frames", 0))
        scen_timing.append(_scenario_ledger(
            name, sc_scene, time.monotonic() - t_scen_start, t_scenario))
        spec_errors.extend("scenario %r: %s" % (name, m)
                           for m in probe.get("spec_errors") or [])
        errs, debt = _split_diagnostics(errs)
        ran = bool(probe) or not timed_out
        ran_any = ran_any or ran
        if errs:
            crashed = True
        all_errors.extend({**e, "scenario": name} for e in errs)
        all_debt.extend({**e, "scenario": name} for e in debt)
        asserts = probe.get("asserts", [])
        # A reported assertion can still be an INCOMPLETE measurement: a read
        # that did not happen (parse/execute failure, unsupported value type),
        # a delta with no frame-0 baseline, or an observation that could not be
        # recorded. Such rows carry measurement == "incomplete" (or, from an
        # older probe shape, actual.baseline_missing). A merely FALSE comparison
        # is NOT incomplete -- it was measured, and stays advisory.
        incomplete = [
            a for a in asserts
            if a.get("measurement") == "incomplete"
            or (isinstance(a.get("actual"), dict)
                and a["actual"].get("baseline_missing"))]
        # Completeness accounting: the normalised timeline is the set of
        # assertions the author SCHEDULED; the probe report is what actually
        # came back. A scheduled assertion with no result row (crash, early
        # quit, dropped frame budget) is an incomplete measurement, never a
        # shorter successful one, and a probe that reports it never reached
        # its frame budget is the same. Reports from an older probe shape
        # without `complete` are judged only on whether every scheduled
        # assertion arrived.
        expected_asserts = sum(len(e.get("assert") or []) for e in timeline)
        missing_asserts = max(0, expected_asserts - len(asserts))
        frame_budget_reached = bool(
            probe.get("complete", len(asserts) >= expected_asserts))
        complete = frame_budget_reached and not incomplete and not missing_asserts
        if not complete:
            why = []
            if not frame_budget_reached:
                why.append("probe did not reach its frame budget")
            if missing_asserts:
                why.append("%d scheduled assertion(s) produced no result row"
                           % missing_asserts)
            if incomplete:
                why.append("%d reported assertion(s) could not be observed (%s)"
                           % (len(incomplete),
                              "; ".join(str(a.get("error") or a.get("name", "?"))
                                        for a in incomplete[:3])))
            spec_errors.append(
                "scenario %r: incomplete measurement -- %s; %d of %d scheduled "
                "assertions reported. The scenario did not produce a complete "
                "result, so it cannot count as a passing or advisory outcome."
                % (name, "; ".join(why) or "incomplete", len(asserts),
                   expected_asserts))
        scen_passed = (ran and not errs and complete and not missing_asserts
                       and not incomplete
                       and bool(asserts) and all(a.get("passed") for a in asserts))

        scen_results.append({"name": name, "ran": ran, "errors": errs,
                             "native_debt": debt,
                             "asserts": asserts, "passed": scen_passed,
                             # Measurement-completeness rows: what was scheduled
                             # vs what the probe actually reported.
                             "expected_asserts": expected_asserts,
                             "asserts_missing": missing_asserts,
                             "complete": complete,
                             "incomplete_asserts": len(incomplete),

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
                ctrl_label = "control:%s@%d" % (scen_scenes[i] or "(main)", n)
                scen_homes.append(Path(ctrl_home))
                scen_labels.append({"control": ctrl_label})
                t_ctrl: dict = {}
                t_ctrl_start = time.monotonic()
                try:
                    ctrl_kwargs = ({"raw": raw_logs, "raw_label": ctrl_label}
                                   if (retain or retain_errors or retain_patterns
                                       or retain_requested) else {})
                    ctrl, _e, _t = _run_probe(dst, state_path, n, timeout,
                                              {"AITELIER_PROBE_SPEC": str(spec_path),
                                               **_home_env(ctrl_home)},
                                              scene=scen_scenes[i], timing=t_ctrl,
                                              render=False, **ctrl_kwargs)
                finally:
                    if not (retain or retain_errors or retain_patterns or retain_requested):
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
    # Owned invocation evidence: copy the declared user:// artifacts out of the
    # scenario/control homes while they are still on disk, then remove every one
    # of them. A retained artifact that never appeared or was refused makes the
    # HARD verdict False, so a partial retention is never a silent green.
    retention = None
    if retain or retain_errors or retain_patterns or retain_requested:
        retention = _retain_copy(retain, scen_homes, raw_logs,
                                 {**corr, "mode": render_mode},
                                 extra_errors=retain_errors,
                                 pass_labels=scen_labels,
                                 user_dir_name=user_dir_name,
                                 patterns=retain_patterns,
                                 requested=bool(retain or retain_errors
                                                or retain_patterns or retain_requested))
        retention_cell["value"] = retention
        if not retention["ok"]:
            hard_passed = False
            why = []
            if retention["refused"]:
                why.append("refused: %s" % "; ".join(retention["refused"]))
            if retention["missing"]:
                why.append("declared but missing: %s" % ", ".join(retention["missing"]))
            if retention["limit_hit"]:
                why.append("retention limit hit: %s" % retention["limit_hit"])
            summary += ("  Invocation evidence NOT fully retained (%s); manifest: %s."
                        % (" | ".join(why), retention["manifest"]))
    # The homes are removed by the _playtest_spec wrapper's finally — on a
    # pass, a failure, a timeout or an unexpected error — after retention has
    # copied what the call declared.
    if ledger is not None:
        ledger["scenarios"] = scen_timing
        ledger["controls"] = ctrl_timing
    report = {"passed": hard_passed, "frames": default_frames, "errors": all_errors,
              "native_debt": all_debt,
              "state": last_state, "spec_used": True, "spec_errors": spec_errors,
              "captures": captures, "render_mode": render_mode,
              "behavior": {"all_passed": behavior_passed, "scenarios": scen_results},
              "summary": summary}
    if retention is not None:
        report["retention"] = retention
    return report


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
                     input_action: str = "ui_accept",
                     spec: dict | None = None, timeout: int = 120,
                     captures: int | None = None,
                     retain: list | None = None,
                     retain_errors: list | None = None,
                     retain_patterns: list | None = None,
                     retain_requested: bool = False,
                     corr: dict | None = None) -> dict:
    proj = Path(project_dir)
    if not (proj / "project.godot").is_file():
        return {"passed": True, "frames": 0, "errors": [], "state": {},
                "behavior": None, "spec_used": False, "no_project": True,
                "summary": "No Godot project — playtest skipped."}
    # The real Godot user:// root name for THIS project — retention searches
    # the invocation-owned app_userdata/<name> (or custom user dir), never a
    # guessed HOME root and never the whole HOME.
    user_dir_name = _project_user_dir_name(proj)
    t_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    t_copy = time.monotonic()
    dst = _copy_project(proj)
    copy_sec = time.monotonic() - t_copy
    ledger: dict = {}
    homes, labels = [], []
    want_raw = bool(retain or retain_errors or retain_patterns or retain_requested)
    raw_logs = _InvocationLogs(homes, labels) if want_raw else []
    try:
        _inject_probe(dst)
        t_import = time.monotonic()
        try:
            if want_raw:
                _import_resources(dst, timeout, raw_logs=raw_logs)
            else:
                _import_resources(dst, timeout)
        except BaseException as exc:
            if want_raw:
                try:
                    _retain_copy(retain, homes, raw_logs, dict(corr or {}),
                                 extra_errors=list(retain_errors or [])
                                 + ["unexpected import error: %r" % exc],
                                 pass_labels=labels, user_dir_name=user_dir_name,
                                 patterns=retain_patterns, requested=True)
                except Exception:
                    pass
            raise
        import_sec = time.monotonic() - t_import
        # Any spec with keys is read as a spec (and refused there if it has
        # no scenario list); only a request with no spec runs the smoke test.
        if spec:
            result = _playtest_spec(dst, spec, frames, timeout, ledger=ledger,
                                    cap_limit=captures, retain=retain,
                                    retain_errors=retain_errors, corr=corr,
                                    retain_patterns=retain_patterns,
                                    retain_requested=retain_requested,
                                    raw_logs=raw_logs,
                                    user_dir_name=user_dir_name)
        else:
            result = _playtest_legacy(dst, frames, input_action, timeout,
                                      ledger=ledger, cap_limit=captures,
                                      retain=retain, retain_errors=retain_errors,
                                      retain_patterns=retain_patterns,
                                      retain_requested=retain_requested,
                                      corr=corr, raw_logs=raw_logs,
                                      user_dir_name=user_dir_name)
        if isinstance(result, dict):
            result["timing"] = _assemble_ledger(ledger, started_at, t_start,
                                                copy_sec, import_sec)
        return result
    finally:
        for h in homes:
            shutil.rmtree(h, ignore_errors=True)
        shutil.rmtree(dst.parent, ignore_errors=True)


def _home_env(home: str) -> dict:
    """Keep Godot's user data and config inside this pass's disposable HOME."""
    return {"HOME": home, "XDG_DATA_HOME": str(Path(home) / ".local/share"),
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "XDG_CACHE_HOME": str(Path(home) / ".cache")}


def _import_resources(dst: Path, timeout: int,
                      raw_logs: list | None = None) -> None:
    """Build this copy's import cache with isolated HOME/XDG and full raw logs.

    Import remains best-effort for timeouts, as before. Unexpected errors keep
    their original exception. Retention invocations register this HOME with
    the caller before any effect and keep it until outer retention/cleanup;
    direct helper calls and default requests clean it locally on every outcome.
    """
    home = tempfile.mkdtemp(prefix="godot_import_home_")
    caller_owned = isinstance(raw_logs, _InvocationLogs)
    try:
        if caller_owned:
            raw_logs.homes.append(Path(home))
            raw_logs.labels.append({"import": "resources"})
        try:
            cp = _run(["--path", str(dst), "--import"], timeout=timeout,
                      extra_env=_home_env(home))
            if raw_logs is not None:
                raw_logs.append({"label": "import", "stdout": cp.stdout,
                                 "stderr": cp.stderr, "returncode": cp.returncode,
                                 "timed_out": False})
        except subprocess.TimeoutExpired as e:
            if raw_logs is not None:
                def _s(v):
                    return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
                raw_logs.append({"label": "import", "stdout": _s(e.stdout),
                                 "stderr": _s(e.stderr) + "\nimport timed out",
                                 "returncode": 124, "timed_out": True})
        except BaseException as exc:
            if raw_logs is not None:
                raw_logs.append({"label": "import", "stdout": "",
                                 "stderr": "unexpected import error: %r" % exc,
                                 "returncode": None, "timed_out": False})
            raise
    finally:
        if not caller_owned:
            shutil.rmtree(home, ignore_errors=True)


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


# ── owned invocation evidence (generated user:// artifacts that survive HOME) ─
#
# WHY THIS EXISTS. Every run gets a throwaway $HOME and that HOME is deleted the
# moment the run ends — which is correct for isolation and fatal for a gate meant
# to produce AUDITABLE pixels and durable reports. A /script render that draws a
# PNG into user:// and a /playtest that writes save_1.json both lose the evidence
# to their own cleanup. This is the bounded, server-owned way to keep it.
#
# THE CONTRACT the caller may state (and only this):
#
#   "retain": {"files": ["reports/summary.json", "frames/f2.png"]}
#
# Each entry is a RELATIVE user:// path under the invocation's throwaway HOME.
# A leading "user://" / "res://" is stripped; anything absolute, containing a
# ".." segment, resolving outside that HOME, or reached through a symlink or a
# hard link is REFUSED (listed in `retention.refused`) and never copied. The
# caller names WHAT to keep. It never names WHERE: the destination is a unique
# server-chosen directory under the durable godot-control state root, so one
# invocation can neither choose a foreign path nor overwrite a previous one.
#
# The destination holds each retained artifact under its declared relative path,
# plus `raw/<n>-<label>.stdout.log` / `.stderr.log` with the FULL untruncated
# streams (the response keeps excerpts for compatibility; the raw logs do not
# truncate), plus `manifest.json`: one row per retained file and raw log with
# path, size, SHA256, source, the user:// path it came from, and the
# project/run/operation/invocation correlation the request carried. A declared
# artifact that is not there is a truthfully recorded `missing`, and a missing
# artifact or an exceeded limit makes the invocation say so — never a silent
# green. On a pass, fail, timeout or an unexpected error the owned state
# directory is still written, and only the invocation's own temps are removed.

# The whole-gate sizing these defaults must be raised to (server-side env, no
# new quota framework): a normal 69-script gate emits >= 140 raw streams (69
# entry points + 1 import pass, each with stdout+stderr), and a normal
# 221-scenario playtest emits >= 442 raw streams before any on-demand captures
# or input-dead control passes. GODOT_RETAIN_MAX_FILES must be set at least that
# high (with headroom) or the retention limit is a truthful HARD failure, never
# a silent truncation.
_RETAIN_MAX_FILES = int(os.environ.get("GODOT_RETAIN_MAX_FILES", "64"))
_RETAIN_MAX_BYTES = int(os.environ.get("GODOT_RETAIN_MAX_BYTES", str(64 * 1024 * 1024)))
_RETAIN_ALLOWED_SUFFIXES = (".json", ".png")
# Dynamic-name selection (see `patterns` below): a bounded selector count and a
# bounded directory-entry search budget so a hostile pattern cannot walk a wide
# tree. Both are server-configured, like the file/byte budgets above.
_RETAIN_MAX_PATTERNS = int(os.environ.get("GODOT_RETAIN_MAX_PATTERNS", "16"))
_RETAIN_MAX_SEARCH_ENTRIES = int(os.environ.get(
    "GODOT_RETAIN_MAX_SEARCH_ENTRIES", "4096"))
# Subtree names a pattern may never select: the game's saved games and player
# profiles are not invocation-generated evidence and stay out of scope.
_RETAIN_EXCLUDED_DIRS = ("saves", "saved_games", "profile", "profiles")


def _retain_root() -> Path:
    """The durable, server-owned state root the owner ledger already uses.

    It is the `godot-control` directory's parent (the mounted
    /var/lib/aitelier-godot), so a retained artifact lives on the volume that
    survives the container and the request, beside the render-owner ledger that
    a deployment observer already reads."""
    override = os.environ.get("GODOT_EVIDENCE_ROOT")
    if override:
        return Path(override)
    return Path(LIFECYCLE_DB).parent


def _retain_declared(req: dict) -> tuple[list, list]:
    """Normalise the request's `retain` block into (files, errors).

    Shape errors are returned, never raised: a malformed retention request must
    not cost the caller its whole report. `files` is the declared literal list
    exactly as authored (still possibly hostile); _retain_copy validates each
    entry. The `patterns` selector is normalised by _retain_selectors and its
    syntax checked by _retain_patterns."""
    raw = (req or {}).get("retain")
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        return [], ["retain must be a mapping with a `files` list"]
    unknown = sorted(str(k) for k in raw if k not in ("files", "patterns"))
    if unknown:
        return [], ["retain has unknown key(s) %s - allowed: files, patterns"
                    % ", ".join(unknown)]
    files = raw.get("files")
    if files is None:
        return [], []
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return [], ["retain.files must be a list of relative path strings"]
    return files, []


def _retain_relpaths(declared: list) -> tuple[list, list]:
    """(accepted relative paths, refusals) — SYNTACTIC validation, no I/O.

    A leading "user://" / "res://" is stripped. Anything absolute, with a
    ".." segment, empty after normalisation, a duplicate, a saves/saved_games/
    profile/profiles subtree, or not ending in a permitted suffix is refused
    here, BEFORE any home is touched, so a hostile declaration can never reach
    a filesystem call. Containment and symlink checks need the home and happen
    in _retain_copy.
    """
    accepted, refused, seen = [], [], set()
    for raw in declared:
        text = str(raw).strip()
        rel = text.replace("\\", "/")
        for prefix in ("user://", "res://"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or os.path.isabs(text):
            refused.append("%s: not a relative user:// path" % raw)
            continue
        rel = "/".join(parts)
        if any(p.lower() in _RETAIN_EXCLUDED_DIRS for p in parts):
            refused.append("%s: saves/profile subtrees may not be selected" % raw)
            continue
        if rel in seen:
            continue
        if not rel.endswith(_RETAIN_ALLOWED_SUFFIXES):
            refused.append("%s: only %s artifacts may be retained"
                           % (raw, ", ".join(_RETAIN_ALLOWED_SUFFIXES)))
            continue
        seen.add(rel)
        accepted.append(rel)
    return accepted, refused


def _retain_selectors(req: dict) -> tuple[list, list]:
    """Normalise the request's `retain.patterns` selector into (patterns, errors).

    Shape only: a non-list (or one holding non-strings) is an error returned to
    the caller before any effect. Pattern SYNTAX is checked by _retain_patterns."""
    raw = (req or {}).get("retain")
    if not isinstance(raw, dict):
        return [], []
    patterns = raw.get("patterns")
    if patterns is None:
        return [], []
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        return [], ["retain.patterns must be a list of relative path patterns"]
    return list(patterns), []


def _retain_requested(req: dict) -> bool:
    """True when the caller EXPLICITLY asked for retention (even with no files).

    {"retain": {"files": []}} and the explicit empty mapping {"retain": {}} are
    raw-only requests: they retain the full streams. An omitted/non-mapping
    `retain` leaves the historic behaviour —
    no retention at all — unchanged."""
    raw = (req or {}).get("retain")
    return isinstance(raw, dict)


def _retain_component_regex(component: str):
    """One path component glob: `*`/`?` match WITHIN the component, never `/`."""
    return re.compile("".join(
        "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
        for ch in component))


def _retain_patterns(declared: list) -> tuple[list, list]:
    """(accepted patterns, refusals) — SYNTACTIC validation, no I/O.

    A pattern is a relative user:// path whose FIRST component starts with a
    non-empty literal prefix (so it can never be a root-wide match), whose
    wildcards (`*`/`?`) stay inside a single component, and which names at least
    a directory and then a file. `**`, absolute paths, `..` traversal, a bad
    suffix, a saves/profile head and anything past the bounded pattern count are
    refused BEFORE any home is touched."""
    accepted, refused, seen = [], [], set()
    for raw in declared:
        if len(accepted) >= _RETAIN_MAX_PATTERNS:
            refused.append("%s: more than %d retention patterns are not accepted"
                           % (raw, _RETAIN_MAX_PATTERNS))
            continue
        text = str(raw).strip()
        rel = text.replace("\\", "/")
        for prefix in ("user://", "res://"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        parts = rel.split("/")
        if not parts or not all(parts) or ".." in parts or os.path.isabs(text):
            refused.append("%s: not a relative user:// pattern" % raw)
            continue
        if "**" in rel:
            refused.append("%s: a recursive '**' wildcard is not allowed" % raw)
            continue
        if len(parts) < 2:
            refused.append("%s: a pattern must name a directory and then a file"
                           % raw)
            continue
        head = parts[0]
        literal_prefix = head.split("*")[0].split("?")[0]
        if not literal_prefix:
            refused.append("%s: the first directory component must start with a "
                           "literal prefix (no root-wide match)" % raw)
            continue
        if head in _RETAIN_EXCLUDED_DIRS or literal_prefix.lower() in _RETAIN_EXCLUDED_DIRS:
            refused.append("%s: saves/profile subtrees may not be selected" % raw)
            continue
        if not rel.endswith(_RETAIN_ALLOWED_SUFFIXES):
            refused.append("%s: only %s artifacts may be retained"
                           % (raw, ", ".join(_RETAIN_ALLOWED_SUFFIXES)))
            continue
        if rel in seen:
            continue
        seen.add(rel)
        accepted.append(rel)
    return accepted, refused


def _retain_walk_pattern(root_fd: int, pattern: str, budget: dict) -> tuple[list, list]:
    """Resolve one accepted pattern under an opened, alias-free user root.

    Enumerates exactly the pattern's fixed component depth through pinned,
    no-follow descriptors; every matched name is opened without following
    aliases (a symlink, FIFO or non-regular match is refused, never followed)
    and matches are returned sorted and de-duplicated. `budget` is the shared
    bounded directory-entry search budget, so a hostile pattern cannot walk a
    wide tree."""
    comps = pattern.split("/")
    matches: list = []
    refused: list = []
    # Transfer each selected descriptor to _retain_copy on success. Keeping
    # the inode live until consumption prevents unlink/recreate from recycling
    # its numeric identity. A failed walk still owns and closes every pin.
    selected = budget["selected"] = {}


    def walk(fd: int, idx: int, chosen: list) -> None:
        comp = comps[idx]
        last = idx == len(comps) - 1
        regex = _retain_component_regex(comp)
        try:
            scanner = os.scandir(fd)
        except OSError as exc:
            refused.append("%s: %s" % ("/".join(chosen) or pattern, exc))
            return
        names: list = []
        # Enumerate lazily and count EACH entry against the declared search
        # budget before it is collected and sorted: a wide directory must cost
        # at most the budget, never one allocation of every foreign name in it.
        with scanner:
            for entry in scanner:
                budget["entries"] += 1
                if budget["entries"] > _RETAIN_MAX_SEARCH_ENTRIES:
                    raise OSError(
                        "retention pattern search exceeded %d directory entries"
                        % _RETAIN_MAX_SEARCH_ENTRIES)
                names.append(entry.name)
        names.sort()
        for name in names:
            if name in ("", ".", "..") or "/" in name or not regex.fullmatch(name):
                continue
            # Excluded subtrees are matched case-insensitively at EVERY
            # traversal depth, consistently with the literal-path check, and
            # BEFORE the name is opened: a wildcard pattern must not traverse
            # "Saves"/"Profiles" any more than "saves"/"profiles".
            if name.lower() in _RETAIN_EXCLUDED_DIRS:
                continue  # saves/profile subtrees stay out of scope
            rel = "/".join(chosen + [name])
            flags = os.O_RDONLY | os.O_NOFOLLOW
            flags |= os.O_NONBLOCK if last else os.O_DIRECTORY
            child_fd = None
            try:
                child_fd = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                refused.append("%s: %s" % (rel, exc))
                continue
            try:
                if last:
                    fst = os.fstat(child_fd)
                    if not stat.S_ISREG(fst.st_mode):
                        refused.append("%s: not a regular generated artifact" % rel)
                        continue
                    if len(matches) >= _RETAIN_MAX_FILES:
                        raise OSError("retention pattern matched more than %d "
                                      "files" % _RETAIN_MAX_FILES)
                    selected[rel] = child_fd
                    child_fd = None  # ownership transfers through selected
                    matches.append(rel)

                else:
                    walk(child_fd, idx + 1, chosen + [name])
            finally:
                if child_fd is not None:
                    os.close(child_fd)

    try:
        walk(root_fd, 0, [])
    except BaseException:
        for fd in selected.values():
            os.close(fd)
        budget.pop("selected", None)
        raise
    return sorted(dict.fromkeys(matches)), refused


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _project_user_dir_name(proj: Path) -> str:
    """Read Godot's typed application user-directory settings.

    A custom directory is marked by its relative .local/share path so it
    cannot be confused with app_userdata/<project name> by retention.
    """
    try:
        text = (proj / "project.godot").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    section = re.search(r"(?ms)^\[application\]\s*$(.*?)(?=^\[|\Z)", text)
    text = section.group(1) if section else text
    custom = re.search(r"(?m)^config/use_custom_user_dir\s*=\s*(true|false)\s*$", text)
    key = "config/custom_user_dir_name" if custom and custom.group(1) == "true" else "config/name"
    name = re.search(r"(?m)^" + re.escape(key) + r'\s*=\s*("(?:[^"\\]|\\.)*")\s*$', text)
    try:
        value = json.loads(name.group(1)) if name else ""
    except ValueError:
        value = ""
    if not value and custom and custom.group(1) == "true":
        name = re.search(r'(?m)^config/name\s*=\s*("(?:[^"\\]|\\.)*")\s*$', text)
        try:
            value = json.loads(name.group(1)) if name else ""
        except ValueError:
            value = ""
    # Project metadata is never authority to select an absolute/alias root.
    if (not value or value.startswith(("/", "\\")) or ":" in value
            or any(x in ("", ".", "..") for x in value.replace("\\", "/").split("/"))):
        return ""
    if custom and custom.group(1) == "true":
        return ".local/share/" + value
    return value


def _user_data_roots(home: Path, user_dir_name: str | None) -> list:
    """Only roots physically below this invocation's HOME are eligible."""
    if not user_dir_name:
        return [home]  # explicit helper fixtures with HOME-root user://
    if user_dir_name.startswith(".local/share/"):
        return [home / user_dir_name]
    parts = user_dir_name.replace("\\", "/").split("/")
    if (os.path.isabs(user_dir_name) or ":" in user_dir_name
            or any(x in ("", ".", "..") for x in parts)):
        return []
    return [home / ".local/share/godot/app_userdata" / user_dir_name,
            home]  # older callers' explicitly HOME-root generated files


def _evidence_dir(path: Path, create: bool = False, parent_fd: int | None = None) -> int:
    """Open every directory component without aliases; return a pinned fd."""
    if parent_fd is None:
        path = Path(os.path.abspath(path))
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        parts = path.parts[1:]
    else:
        fd = os.dup(parent_fd)
        parts = path.parts
    nxt = None
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise OSError("invalid evidence directory component")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            after = os.fstat(nxt)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise OSError("evidence directory changed during open")
            old_fd = fd
            fd, nxt = nxt, None
            os.close(old_fd)
        return fd
    except BaseException:
        if nxt is not None:
            os.close(nxt)
        os.close(fd)
        raise


class _EvidenceFile:
    """This copy's diagnostic path and pinned source/destination descriptor."""
    def __init__(self, path, read_fd=None, parent_fd=None):
        self.path = Path(path)
        self.read_fd = read_fd
        self.parent_fd = parent_fd

    @property
    def parent(self):
        return self.path.parent

    @property
    def name(self):
        return self.path.name

    def __fspath__(self):
        return os.fspath(self.path)


def _safe_copy_file(src: Path, dst: Path) -> int:
    """Copy from a pinned regular single-link inode to an exclusive owned file."""
    fd = os.dup(src.read_fd)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
            raise OSError("not a regular, single-link generated artifact")
        dfd = os.open(dst.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=dst.parent_fd)
        try:
            with os.fdopen(fd, "rb") as inp, os.fdopen(dfd, "wb") as out:
                fd = None
                copied = 0
                while chunk := inp.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > fst.st_size:
                        raise OSError("generated artifact grew during copy")
                    out.write(chunk)
                if copied != fst.st_size or os.fstat(inp.fileno()).st_nlink != 1:
                    raise OSError("generated artifact changed during copy")
        except BaseException:
            os.unlink(dst.name, dir_fd=dst.parent_fd)
            raise
        return fst.st_size
    finally:
        if fd is not None:
            os.close(fd)



def _retain_new_invocation_dir(base: Path) -> tuple[Path, str, int]:
    """Allocate exclusively through pinned no-follow destination ancestors."""
    evidence = base / "evidence"
    fd = _evidence_dir(evidence, create=True)
    try:
        for _ in range(8):
            inv = uuid.uuid4().hex
            try:
                os.mkdir(inv, 0o700, dir_fd=fd)
                root_fd = os.open(inv, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                return evidence / inv, inv, root_fd
            except FileExistsError:
                continue
        raise OSError("could not allocate a unique retention invocation directory")
    finally:
        os.close(fd)


def _evidence_bytes(root_fd: int, rel: str, body: bytes | None = None) -> tuple[int, str]:
    """Read/hash or exclusively write one file beneath the pinned invocation."""
    path = Path(rel)
    fd = _evidence_dir(path.parent, create=body is not None, parent_fd=root_fd) if str(path.parent) != "." else os.dup(root_fd)
    try:
        flags = os.O_RDONLY if body is None else os.O_WRONLY | os.O_CREAT | os.O_EXCL
        out = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, "rb" if body is None else "wb") as fh:
            if body is None:
                data = fh.read()
            else:
                fh.write(body)
                data = body
        return len(data), hashlib.sha256(data).hexdigest()
    finally:
        os.close(fd)


def _retain_take(cand: Path, home: Path, rel: str, pi: int, label: dict,
                 root: Path, root_fd: int, used_paths: set, files: list,
                 raws: list, refused: list, state: dict,
                 expected: tuple | None = None) -> str:

    """Copy one declared/pattern-matched rel out of one pass's user root.

    All source and destination ancestry is opened without following aliases;
    the validated source inode and destination parent stay pinned through copy,
    hashing and manifest row. Returns 'taken' | 'absent' | 'replaced' | 'error'
    | 'limit'. When `expected` is given (the identity of a still-live pattern
    discovery descriptor), the object under `cand.name` must BE that object: a
    different inode is 'replaced', a hard failure with no retry or path fallback.

    """
    source_fd = parent_fd = dest_fd = None
    try:
        home_fd = _evidence_dir(Path(home))
        try:
            parent_fd = (_evidence_dir(cand.parent.relative_to(home), parent_fd=home_fd)
                         if cand.parent != Path(home) else os.dup(home_fd))
        finally:
            os.close(home_fd)
        before = os.stat(cand.name, dir_fd=parent_fd, follow_symlinks=False)
        source_fd = os.open(cand.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent_fd)
        st = os.fstat(source_fd)
        if expected is not None and (st.st_dev, st.st_ino) != expected:
            # The discovery walk SELECTED a DIFFERENT object under this exact
            # name. Copying the replacement would launder a swapped artifact
            # into the invocation's evidence, so the selected identity is
            # enforced here and the caller records a hard failure. There is no
            # fresh-stat retry and no path fallback: the SELECTED object is
            # simply no longer deliverable.
            return "replaced"
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                or (before.st_dev, before.st_ino) != (st.st_dev, st.st_ino)):
            raise OSError("not an unchanged regular single-link generated artifact")

        if len(files) + len(raws) >= _RETAIN_MAX_FILES:
            state["limit_hit"] = "file_count"
            return "limit"
        if state["total"] + st.st_size > _RETAIN_MAX_BYTES:
            state["limit_hit"] = "total_bytes"
            return "limit"
        dst_rel = rel
        if rel in used_paths:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                          str(sorted(label.values())[0]) if label else "pass")
            dst_rel = "%02d-%s/%s" % (pi, safe, rel)
        dest_path = root / dst_rel
        dest_fd = (_evidence_dir(Path(dst_rel).parent, create=True, parent_fd=root_fd)
                   if str(Path(dst_rel).parent) != "." else os.dup(root_fd))
        copied = _safe_copy_file(_EvidenceFile(cand, read_fd=source_fd),
                                 _EvidenceFile(dest_path, parent_fd=dest_fd))
        size, digest = _evidence_bytes(root_fd, dst_rel)
        # A file growing during copy cannot silently exceed the agreed byte
        # quota or claim its old size.
        if size != copied or state["total"] + size > _RETAIN_MAX_BYTES:
            os.unlink(dest_path.name, dir_fd=dest_fd)
            refused.append("%s: changed size during copy" % rel)
            return "error"
        state["total"] += size
        used_paths.add(dst_rel)
        files.append({"path": dst_rel, "source": "user://",
                      "user_path": "user://" + rel, "size": size,
                      "sha256": digest, "pass": pi, **label})
        return "taken"
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        refused.append("%s: %s" % (rel, exc))
        return "error"
    finally:
        for fd in (source_fd, parent_fd, dest_fd):
            if fd is not None:
                os.close(fd)


def _open_user_root(home: Path, base: Path) -> int:
    """Open an already-validated user root through pinned no-follow ancestors."""
    home_fd = _evidence_dir(Path(home))
    try:
        if base == Path(home):
            return os.dup(home_fd)
        return _evidence_dir(base.relative_to(home), parent_fd=home_fd)
    finally:
        os.close(home_fd)


def _retain_copy(declared: list, homes: list, raw_logs: list, corr: dict,
                 extra_errors: list | None = None,
                 pass_labels: list | None = None,
                 user_dir_name: str | None = None,
                 patterns: list | None = None,
                 requested: bool = False) -> dict:
    """Retain every matching pass and complete raw streams within one budget.

    ``declared`` are literal relative JSON/PNG paths; ``patterns`` are the
    bounded dynamic-name selectors (see _retain_patterns). ``requested`` marks an
    EXPLICIT retention request so an empty selector still allocates the owned
    destination and writes a raw-only manifest. All source and destination
    ancestry is opened without following aliases; the validated source inode and
    destination parent remain pinned through copying, hashing and manifest
    writing. Refusal leaves foreign state alone.

    Two truthful outcomes are pinned here. A pattern that SELECTED a regular
    artifact, but whose artifact then vanished (or changed inode) before the
    pinned copy could open it, is a hard failure: the selected rel is named in
    ``missing``, exactly like a literal, and is never folded back into the
    "this pattern matched nothing" outcome; there is no retry, reopen or path
    fallback. A pattern matched by NO entry under any user root stays missing.
    If the owned destination itself cannot be allocated, no raw row is
    reported — a fabricated manifest row pointing at bytes that were never
    written would be a false green — while the allocation failure in
    ``refused`` already makes ``ok`` False.
    """
    declared = list(declared or [])
    pattern_strs = list(patterns or [])
    rels, refused = _retain_relpaths(declared)
    pats, pat_refused = _retain_patterns(pattern_strs)
    refused = list(extra_errors or []) + refused + pat_refused
    labels = list(pass_labels or [])
    files, raws, missing = [], [], []
    root = inv = root_fd = None
    state = {"total": 0, "limit_hit": None}
    if rels or pats or raw_logs or requested or refused:
        try:
            root, inv, root_fd = _retain_new_invocation_dir(_retain_root())
        except OSError as exc:
            refused.append("retention destination: %s" % exc)
    used_paths = set()
    # A (pass index, selected rel) is taken at most once: a literal and a
    # pattern that select the SAME artifact of the SAME pass must not both copy
    # it (a duplicate row and a double byte count), while the same rel from
    # DISTINCT passes stays distinct evidence.
    taken: set = set()
    entry_budget = {"entries": 0}
    try:
        if root_fd is not None:
            for rel in rels:
                hits = 0
                for pi, h in enumerate(homes or []):
                    if state["limit_hit"]:
                        break
                    if (pi, rel) in taken:
                        hits += 1
                        continue
                    label = labels[pi] if pi < len(labels) else {}
                    for base in _user_data_roots(Path(h), user_dir_name):
                        status = _retain_take(base / rel, Path(h), rel, pi, label,
                                              root, root_fd, used_paths, files,
                                              raws, refused, state)
                        if status == "absent":
                            continue
                        hits += 1
                        if status == "taken":
                            taken.add((pi, rel))
                        break  # only alternate roots for THIS pass
                if hits == 0:
                    missing.append(rel)
            for pat in pats:
                selected = False
                for pi, h in enumerate(homes or []):
                    if state["limit_hit"]:
                        break
                    label = labels[pi] if pi < len(labels) else {}
                    for base in _user_data_roots(Path(h), user_dir_name):
                        try:
                            base_fd = _open_user_root(Path(h), base)
                        except (OSError, ValueError):
                            continue
                        selected_here = {}
                        try:
                            try:
                                matches, walk_refused = _retain_walk_pattern(
                                    base_fd, pat, entry_budget)
                                selected_here = entry_budget.pop("selected", {})
                            except OSError as exc:
                                refused.append("%s: %s" % (pat, exc))
                                break
                            refused.extend(walk_refused)
                            for rel in matches:
                                selected = True
                                if (pi, rel) in taken:
                                    continue
                                # Identity comes from the live discovery pin,
                                # never a fresh name or an expired inode number.
                                pin = os.fstat(selected_here[rel])
                                status = _retain_take(base / rel, Path(h), rel, pi, label,
                                                      root, root_fd, used_paths, files,
                                                      raws, refused, state,
                                                      expected=(pin.st_dev, pin.st_ino))
                                if status == "taken":
                                    taken.add((pi, rel))
                                elif status in ("absent", "replaced"):
                                    if rel not in missing:
                                        missing.append(rel)
                                if state["limit_hit"]:
                                    break
                        finally:
                            # Includes duplicate, quota, missing/replaced and
                            # copy-error exits, or failure just after discovery.
                            for fd in selected_here.values():
                                os.close(fd)
                            for fd in entry_budget.pop("selected", {}).values():
                                os.close(fd)
                            os.close(base_fd)
                        if state["limit_hit"]:
                            break
                if (not selected and not state["limit_hit"]
                        and pat not in missing):
                    missing.append(pat)
            for n, log in enumerate(raw_logs or []):
                label = re.sub(r"[^A-Za-z0-9._-]", "_", str(log.get("label", "pass%d" % n)))
                for stream in ("stdout", "stderr"):
                    if state["limit_hit"]:
                        break
                    if len(files) + len(raws) >= _RETAIN_MAX_FILES:
                        state["limit_hit"] = "file_count"
                        break
                    body = str(log.get(stream, "")).encode("utf-8", errors="replace")
                    if state["total"] + len(body) > _RETAIN_MAX_BYTES:
                        state["limit_hit"] = "total_bytes"
                        break
                    name = "raw/%d-%s.%s.log" % (n, label, stream)
                    try:
                        size, digest = _evidence_bytes(root_fd, name, body)
                    except OSError as exc:
                        refused.append("raw %s: %s" % (name, exc))
                        break
                    state["total"] += size
                    row = {"path": name, "source": "raw_%s" % stream, "size": size,
                           "sha256": digest, "pass": n, "label": log.get("label", "pass%d" % n)}
                    row.update({k: log[k] for k in ("returncode", "timed_out") if k in log})
                    raws.append(row)
        limit_hit = state["limit_hit"]
        ok = not refused and not missing and limit_hit is None
        manifest = {"schema": "godot-invocation-evidence/1", "invocation_id": inv,
                    "retained_dir": str(root) if root is not None else "",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    **{k: corr.get(k) for k in ("project_id", "run_id", "operation_id", "owner_id", "generation", "mode")},
                    "declared": declared, "patterns": pattern_strs, "files": files,
                    "raw_logs": raws, "refused": refused, "missing": missing,
                    "limit_hit": limit_hit, "ok": ok}
        manifest_path = ""
        if root_fd is not None:
            try:
                _evidence_bytes(root_fd, "manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
                manifest_path = str(root / "manifest.json")
            except OSError as exc:
                refused.append("retention manifest: %s" % exc)
                ok = False
        return {"ok": ok, "invocation_id": inv,
                "retained_dir": str(root) if root is not None else "",
                "files": files, "raw_logs": raws, "refused": refused,
                "missing": missing, "limit_hit": limit_hit, "manifest": manifest_path}
    finally:
        if root_fd is not None:
            os.close(root_fd)




def _script_log_excerpt(text: str) -> str:
    """Keep the first diagnostic and final summary within the 4000-char budget."""
    if len(text) <= 4000:
        return text
    marker = "\n... [middle truncated] ...\n"
    remaining = 4000 - len(marker)
    head = remaining // 2
    return text[:head] + marker + text[-(remaining - head):]


def _validate_script_selection(project_dir: str, scripts: list,
                               render: bool) -> list:
    """Refuse an unsupported /script selection BEFORE anything happens.

    Runs before render-owner admission, the project copy, the import pass, any
    engine run and any output write, so an invalid request changes nothing on
    this box. Each named entry must be a relative `res://` path naming one of
    the project's ACTUAL admitted `extends SceneTree` entry points (the same
    discovery the default runs use). A rendered request with no real project
    is refused here too — it can never be answered with a green render; a
    headless request with an omitted selection keeps the full default
    discovery untouched.
    """
    errors: list = []
    requested: list = []
    if scripts is not None and not isinstance(scripts, list):
        return ["scripts must be a list of relative res:// entry paths or null"]
    for raw in scripts or []:
        if not isinstance(raw, str) or not raw.strip():
            errors.append("%r: a script entry must be a relative res:// path to "
                          "an admitted `extends SceneTree` test file" % (raw,))
            continue
        rel = raw.strip()
        if rel.startswith("res://"):
            rel = rel[len("res://"):]
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts or os.path.isabs(raw.strip()):
            errors.append("%s: not a relative res:// path inside the project" % raw)
            continue
        requested.append("res://" + "/".join(parts))
    if errors:
        return errors
    proj = Path(project_dir or "")
    if not (proj / "project.godot").is_file():
        if render:
            errors.append("render=true requires a real Godot project — there is "
                          "no project.godot at %s" % (proj or "."))
        return errors
    if not requested:
        return errors  # default discovery (headless or rendered) is preserved
    admitted = set(_discover_entry_points(proj))
    for entry in requested:
        if entry not in admitted:
            errors.append("%s: not an admitted `extends SceneTree` entry point "
                          "under tests/ (discovered: %s)"
                          % (entry, ", ".join(sorted(admitted)) or "none"))
    return errors


def run_script(project_dir: str, scripts: list, timeout: int = 600,
               render: bool = False, retain: list | None = None,
               retain_errors: list | None = None,
               retain_patterns: list | None = None,
               retain_requested: bool = False,
               corr: dict | None = None) -> dict:
    """Run ``godot --path <proj> -s <res://...>`` for each script.

    The GDScript unit suite is the project's fastest, most targeted feedback,
    and it was DEAD: ``run_tests.sh`` shells out to a bare ``godot``, and there
    is no godot binary in the aitelier container -- only in this sidecar. Every
    round the unit gate failed with "godot binary not found", 5_review blocked
    on it, and the PM planned a repair the implementer could not possibly make:
    no amount of PATH resolution finds a binary that is not in the filesystem.
    Give the suite the same HTTP route /compile and /playtest already use.

    ``render=True`` (the caller's validated opt-in) runs each admitted entry
    point through ``_run(render=True)`` — Xvfb plus software GL — so a suite
    can render real pixels. The entries are the default discovery or an
    explicitly validated selection; headless (default) and render runs are
    otherwise identical, and the render owner / effect-lock / admission /
    client-abort / release fences are enforced by the HTTP route for both.
    A render failure is reported as a failure — it is never retried headless
    into a silent green.

    ``retain`` is the request's declared relative user:// artifacts and
    ``retain_patterns`` its bounded dynamic-name selectors; each entry point's
    throwaway HOME is copied from before it is removed. A refused or missing
    declaration (or a pattern that matched nothing) makes `passed` False — never
    a silent green for evidence the caller asked for and did not get. An explicit
    request with neither files nor patterns (``retain_requested``) still retains
    the full raw streams. An explicit retention request on a valid project whose
    discovery found NO admitted entry still allocates its owned destination and
    writes a bounded manifest (a zero-pass raw-only success, or a hard failure
    naming a declaration no pass can satisfy); the omitted-``retain`` no-entry
    case keeps the historic skip. On ANY exit path — pass, failure, timeout or an
    error nobody predicted — every owned HOME is removed in the outer finally,
    after a truthful retention attempt has written its manifest; the original
    error is never replaced by a green.
    """
    proj = Path(project_dir)
    retain = list(retain or [])
    retain_patterns = list(retain_patterns or [])
    corr = dict(corr or {})
    retain_errors = list(retain_errors or [])
    want_raw = bool(retain or retain_patterns or retain_errors or retain_requested)
    homes: list = []
    pass_labels: list = []
    raw_logs = _InvocationLogs(homes, pass_labels) if want_raw else []
    retention_cell: dict = {}
    render_mode = "render" if render else "headless"
    if not (proj / "project.godot").is_file():
        # A plain headless request stays the pre-existing skip it always was;
        # an opt-in render with no project must never read as a green render.
        return {"passed": not render, "no_project": True, "results": [],
                "render_mode": render_mode, "render_requested": bool(render),
                "summary": ("render=true requires a real Godot project — no "
                            "project.godot here." if render else
                            "No project.godot -- not a Godot project; script gate skipped.")}
    scripts = list(scripts or []) or _discover_entry_points(proj)
    if not scripts:
        # Opt-in render with nothing admitted must not be answered by the
        # pixel-blind path: there is no render to do, so this is a HARD failure
        # rather than a silent headless pass. A plain (headless) request with no
        # admitted entry point stays the pre-existing skip it always was.
        if not want_raw:
            return {"passed": not render, "results": [], "discovered": [],
                    "render_mode": render_mode, "render_requested": bool(render),
                    "summary": ("No admitted `extends SceneTree` entry point under tests/ "
                                "-- a rendered /script has nothing to render.") if render
                               else "No `extends SceneTree` entry point under tests/."}
        # An EXPLICIT retention request does not get to skip silently just
        # because discovery found nothing: the caller asked for evidence, so the
        # owned destination is allocated and a bounded, truthful manifest is
        # written (raw-only, zero passes). An empty valid project is therefore an
        # explicit raw-only success; a declaration no pass can satisfy is a
        # truthful hard failure. Omitted retention keeps the historic skip above.
        retention = _retain_copy(retain, [], raw_logs,
                                 {**corr, "mode": render_mode},
                                 extra_errors=retain_errors,
                                 pass_labels=pass_labels,
                                 user_dir_name=_project_user_dir_name(proj),
                                 patterns=retain_patterns, requested=True)
        if render:
            passed = False
            summary = ("No admitted `extends SceneTree` entry point under tests/ "
                       "-- a rendered /script has nothing to render.")
        else:
            passed = bool(retention["ok"])
            if retention["ok"]:
                summary = ("No admitted `extends SceneTree` entry point under tests/; "
                           "the explicit retention request wrote a bounded raw-only "
                           "manifest at %s." % retention["manifest"])
            else:
                why = []
                if retention["refused"]:
                    why.append("refused: %s" % "; ".join(retention["refused"]))
                if retention["missing"]:
                    why.append("declared but missing: %s"
                               % ", ".join(retention["missing"]))
                if retention["limit_hit"]:
                    why.append("retention limit hit: %s" % retention["limit_hit"])
                summary = ("No admitted `extends SceneTree` entry point under tests/, "
                           "and the explicit retention request could not be fully "
                           "satisfied (%s)." % " | ".join(why))
        return {"passed": passed, "results": [], "discovered": [],
                "render_mode": render_mode, "render_requested": bool(render),
                "retention": retention, "summary": summary}

    # The real Godot user:// root name for THIS project — retention searches
    # the invocation-owned app_userdata/<name> (or custom user dir), never a
    # guessed HOME root and never the whole HOME.
    user_dir_name = _project_user_dir_name(proj)
    dst = _copy_project(proj)
    try:
        # The suite loads scenes and resources exactly like the game does, so it
        # needs the same import cache the play-test builds.
        if want_raw:
            _import_resources(dst, timeout=min(timeout, 300), raw_logs=raw_logs)
        else:
            _import_resources(dst, timeout=min(timeout, 300))
        results = []
        # A rendered run names the SAME admitted entries as the headless one; the
        # mode is reported per invocation, never inferred from the output.
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
            homes.append(Path(sc_home))
            # Registered the moment it exists; the label carries the pass
            # identity the manifest rows and raw logs report.
            pass_labels.append({"script": rel})
            try:
                cp = _run(["--path", str(dst), "-s", rel], timeout=timeout,
                          extra_env=_home_env(sc_home), render=render)
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
            except Exception as exc:
                # An error nobody predicted (a missing Xvfb, a harness bug)
                # must not take the pass's evidence down with it: the pass is
                # recorded, retention still runs in the handler below, and the
                # ORIGINAL error is re-raised — never a green, never a silent
                # headless retry.
                raw_logs.append({"label": Path(str(rel)).name, "stdout": "",
                                 "stderr": "unexpected engine/harness error: %r"
                                           % exc,
                                 "returncode": None, "timed_out": False})
                raise
            # The home is NOT removed here: retention copies its declared
            # artifacts below, and only then are all the homes cleaned up. The
            # full streams are kept for the manifest regardless of truncation.
            raw_logs.append({"label": Path(str(rel)).name, "stdout": out,
                             "stderr": err, "returncode": rc, "timed_out": rc == 124})
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
        # Retention runs while the entry points' homes are still on disk: a
        # declared relative path is taken from whichever owned user-data root
        # holds it, and the full raw streams recorded above go into the
        # manifest. Copying now is what lets the cleanup below be total.
        retention = None

        if want_raw:
            retention = _retain_copy(retain, homes,
                                     raw_logs, {**corr, "mode": render_mode},
                                     extra_errors=retain_errors,
                                     pass_labels=pass_labels,
                                     user_dir_name=user_dir_name,
                                     patterns=retain_patterns,
                                     requested=bool(retain or retain_patterns
                                                   or retain_errors or retain_requested))
        retention_cell["value"] = retention
        ok = all(r["passed"] for r in results)
        if retention is not None and not retention["ok"]:
            ok = False
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
        if retention is not None and not retention["ok"]:
            why = []
            if retention["refused"]:
                why.append("refused: %s" % "; ".join(retention["refused"]))
            if retention["missing"]:
                why.append("declared but missing: %s" % ", ".join(retention["missing"]))
            if retention["limit_hit"]:
                why.append("retention limit hit: %s" % retention["limit_hit"])
            summary += ("  Invocation evidence NOT fully retained (%s); manifest: %s."
                        % (" | ".join(why), retention["manifest"]))
        report = {"passed": ok, "results": results, "discovered": scripts,
                  "render_mode": render_mode, "render_requested": bool(render),
                  "summary": summary}
        if retention is not None:
            report["retention"] = retention
        return report
    except Exception as exc:
        if want_raw and "value" not in retention_cell:
            # Unexpected error before the normal retention point: attempt the
            # same bounded retention (the manifest records the unexpected
            # error) so the generated evidence survives the cleanup below,
            # then re-raise the ORIGINAL failure.
            try:
                retention_cell["value"] = _retain_copy(
                    retain, homes, raw_logs, {**corr, "mode": render_mode},
                    extra_errors=retain_errors
                    + ["unexpected error before retention: %r" % exc],
                    pass_labels=pass_labels, user_dir_name=user_dir_name,
                    patterns=retain_patterns, requested=True)
            except Exception:
                pass
        raise
    finally:
        # Pass, fail, timeout or an error nobody predicted: every throwaway home
        # goes, or the "throwaway" ones accumulate in the sidecar. Retention has
        # already copied what the call declared.
        for h in homes:
            shutil.rmtree(h, ignore_errors=True)
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


_LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def health_snapshot():
    """Named effective limits and actual loaded source; no environment dump."""
    return {"ok": True, "engine": "godot", "bin": GODOT_BIN,
            "source_identity": {"path": str(Path(__file__).resolve()),
                                "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                "loaded_sha256": _LOADED_SOURCE_SHA256,
                                "pid": os.getpid()},
            "retention_limits": {"files": _RETAIN_MAX_FILES, "bytes": _RETAIN_MAX_BYTES,
                                 "patterns": _RETAIN_MAX_PATTERNS,
                                 "search_entries": _RETAIN_MAX_SEARCH_ENTRIES}}


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
            self._send(200, health_snapshot())
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
        # Owned-evidence and render flags are validated BEFORE any copy, import,
        # admission or effect work, so a request that asks for something invalid
        # changes nothing on this box. `render` must be a real boolean and the
        # retention declaration must have a valid shape; both refusals name what
        # is wrong and neither starts a render owner.
        if self.path in ("/script", "/playtest"):
            render_flag = req.get("render", False)
            if not isinstance(render_flag, bool):
                return self._send(400, {"error": "render must be true or false"})
            retain_files, retain_errors = _retain_declared(req)
            retain_patterns, pattern_errors = _retain_selectors(req)
            retain_errors = list(retain_errors) + list(pattern_errors)
            if not retain_errors and retain_files:
                # The LITERAL declaration is semantically validated here too,
                # BEFORE any owner admission, copy, import or run — the same
                # pre-effect fence the pattern selectors already get. A bad
                # literal returns 400 and changes nothing on this box.
                _, literal_errors = _retain_relpaths(retain_files)
                retain_errors = list(literal_errors)
            if not retain_errors and retain_patterns:
                _, pattern_errors = _retain_patterns(retain_patterns)
                retain_errors = list(pattern_errors)
            if retain_errors:
                return self._send(400, {"error": "; ".join(retain_errors)})
            retain_requested = _retain_requested(req)
        else:
            render_flag, retain_files, retain_patterns = False, [], []
            retain_errors, retain_requested = [], False
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
        if self.path == "/script":
            # The render flag and retention shape are already validated; the
            # script SELECTION is validated here too, against the project's
            # real admitted SceneTree entries, BEFORE the owner/admission work
            # below — so an invalid entry creates no owner row, copies
            # nothing, imports nothing, runs nothing and writes nothing.
            selection_errors = _validate_script_selection(
                proj, req.get("scripts"), render_flag)
            if selection_errors:
                return self._send(400, {"error": "; ".join(selection_errors)})
        # Queue behind any render in flight: a request that finds a live render
        # owner waits for it to release, then owns the render. The wait has no
        # harness timeout; it ends when the caller disconnects (its own HTTP
        # timeout) or when the request's own {"render_wait_timeout_sec": N} runs
        # out. Both are checked on every poll, and again after the request owns
        # the render and holds both locks, just before the render starts. In
        # every case nothing is rendered: a request stopped while queued makes no
        # owner row, and one stopped after taking ownership releases its row
        # with a "not rendered" reason. A holder that needs reconciliation
        # (owner_lost, or active with a stale heartbeat) is refused at once with
        # a 409 that names its kind.
        held = self.path in self._RENDER_ROUTES
        # The matching finally releases the process lock and durable owner.
        # finally: _RENDER_LOCK.release()
        owner = None
        effect_lock = None
        heartbeat = None
        lock_wait = 0.0
        owner_wait = {}
        wait_timeout = None
        release_reason = "completed"
        if held:
            # One server-resolved identity for admission AND correlation: the
            # request carries it, the X-AItelier-Operation header carries it,
            # or the server mints it — and the retained manifest binds to
            # exactly what was admitted, never req.get(None).
            project_id = (req.get("project_id") or Path(proj).name
                          or "unknown-project")
            run_id = (req.get("run_id") or os.environ.get("AITELIER_RUN_ID")
                      or "unknown-run")
            operation_id = (req.get("operation_id")
                            or self.headers.get("X-AItelier-Operation")
                            or uuid.uuid4().hex)
            wait_timeout = req.get("render_wait_timeout_sec")
            try:
                wait_timeout = None if wait_timeout is None else max(0.0, float(wait_timeout))
            except (TypeError, ValueError):
                return self._send(400, {"error": "render_wait_timeout_sec must be a number"})
            admission_started = time.monotonic()
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
            if held:
                elapsed = time.monotonic() - admission_started
                if self._render_client_abandoned():
                    release_reason = ("not rendered: the caller disconnected "
                                      "before the render started")
                    print(f"[harness] {self.path} {operation_id}: {release_reason}",
                          flush=True)
                    return
                if wait_timeout is not None and elapsed > wait_timeout:
                    release_reason = ("not rendered: render_wait_timeout_sec ran "
                                      "out before the render started")
                    return self._send(409, _wait_timed_out(
                        elapsed, wait_timeout,
                        owner_wait["render_owner_waited_for_owner_ids"], owned=True))
            if self.path == "/compile":
                self._send(200, compile_project(proj))
            elif self.path == "/checkgd":
                self._send(200, check_gdscript(
                    req.get("files") or [], timeout=req.get("timeout", 120)))
            elif self.path == "/script":
                # The SERVER-RESOLVED identity — the same project/run/operation
                # the owner row was admitted under, plus the acquired owner id
                # and generation — is the correlation every retained manifest
                corr = {"project_id": project_id, "run_id": run_id,
                        "operation_id": operation_id,
                        "owner_id": owner.get("owner_id") if owner else None,
                        "generation": owner.get("generation") if owner else None}
                # Only the kwargs the request actually used are passed, so the
                # existing render-body stubs (`run_script(proj, scripts,
                # timeout=...)`) keep working for a plain request.
                script_kwargs = {}
                if render_flag:
                    script_kwargs["render"] = True
                if retain_files or retain_patterns or retain_errors or retain_requested:
                    script_kwargs.update(retain=retain_files,
                                         retain_patterns=retain_patterns,
                                         retain_requested=retain_requested,
                                         retain_errors=retain_errors, corr=corr)
                self._send(200, _with_owner_wait(run_script(
                    proj, req.get("scripts") or [],
                    timeout=req.get("timeout", 600), **script_kwargs), owner_wait))
            elif self.path == "/x11_input_smoke":
                self._send(200, _with_owner_wait(x11_input_smoke(
                    proj, timeout=int(req.get("timeout", 180))), owner_wait))
            elif self.path == "/playtest":
                corr = {"project_id": project_id, "run_id": run_id,
                        "operation_id": operation_id,
                        "owner_id": owner.get("owner_id") if owner else None,
                        "generation": owner.get("generation") if owner else None}
                playtest_kwargs = {}
                if retain_files or retain_patterns or retain_errors or retain_requested:
                    playtest_kwargs.update(retain=retain_files,
                                           retain_patterns=retain_patterns,
                                           retain_requested=retain_requested,
                                           retain_errors=retain_errors, corr=corr)
                report = playtest_project(
                    proj, frames=req.get("frames", DEFAULT_PLAYTEST_FRAMES),
                    input_action=req.get("input_action", "ui_accept"),
                    timeout=req.get("timeout", 120),
                    spec=req.get("spec"),
                    # On-demand re-photography of a red scenario: the gate runs
                    # with 0 captures, a reviewer re-runs that one scenario with
                    # {"captures": 4} and gets the PNGs back.
                    captures=req.get("captures"), **playtest_kwargs)
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
                    release_render_owner(owner["owner_id"], owner["generation"],
                                         release_reason)
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
