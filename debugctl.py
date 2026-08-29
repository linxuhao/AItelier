#!/usr/bin/env python3
"""debugctl — AItelier CLI debug controller.

Wraps the `aitelier` CLI in a tmux session so Claude Code can:
  - Capture the rendered Rich TUI as text
  - Send input and keystrokes
  - Watch workspace file changes
  - Inspect workspace state

Composite commands (save tool calls):
  cmd <text>       — type text + Enter (saves 2 calls: send + key)
  snapshot [pid]   — capture screen + workspace tree/log (saves 3 calls)
  inspect <pid>    — tree + diff + log combined (saves 2 calls)

Blocking command (do NOT poll for a checkpoint — this pushes):
  await <pid>      — block on the server's SSE stream until the run pauses at a
                     checkpoint, fails, or completes; print one line and exit.
"""

import argparse
import os
import re
import subprocess
import sys
import time

# ── Constants ──────────────────────────────────────────────────────────
DEFAULT_SESSION = "aitelier"
RUN_DIR = os.path.expanduser("~/.AItelier")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")

# Resolve the venv python / aitelier entry point relative to this script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_VENV_PYTHON = os.path.join(_SCRIPT_DIR, ".venv", "bin", "python")
_AITELIER_CMD = os.path.join(_SCRIPT_DIR, ".venv", "bin", "aitelier")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _session_exists(name: str) -> bool:
    r = _run(["tmux", "has-session", "-t", name])
    return r.returncode == 0


def _die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _require_session(name: str):
    if not _session_exists(name):
        _die(f"No tmux session '{name}' running.")


# ── CLI commands ───────────────────────────────────────────────────────

def cmd_start(args):
    """Launch `aitelier` in a detached tmux session."""
    name = args.session
    if _session_exists(name):
        _die(f"tmux session '{name}' already running. Use 'stop' first or 'capture' to read it.")

    aitelier_bin = _AITELIER_CMD if os.path.isfile(_AITELIER_CMD) else "aitelier"

    cli_args = args.args or []
    cmd = ["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
           aitelier_bin] + cli_args
    r = _run(cmd)
    if r.returncode != 0:
        _die(f"Failed to start tmux session: {r.stderr.strip()}")
    time.sleep(1)
    print(f"Session '{name}' started. Use 'capture' to read the screen.")


def cmd_stop(args):
    """Kill the tmux session."""
    name = args.session
    _require_session(name)
    _run(["tmux", "kill-session", "-t", name])
    print(f"Session '{name}' stopped.")


def cmd_capture(args):
    """Capture the current tmux pane content as plain text."""
    name = args.session
    _require_session(name)

    r = _run(["tmux", "capture-pane", "-t", name, "-p", "-S", "-", "-E", "-"])
    if r.returncode != 0:
        _die(f"capture failed: {r.stderr.strip()}")

    text = r.stdout
    if not args.ansi:
        text = ANSI_RE.sub("", text)

    lines = text.rstrip("\n").split("\n")
    print("\n".join(lines))


def cmd_send(args):
    """Send text to the CLI's input."""
    name = args.session
    _require_session(name)
    _run(["tmux", "send-keys", "-t", name, "-l", args.text])
    print(f"Sent: {args.text!r}")


def cmd_key(args):
    """Send a special key (Enter, Escape, Up, Down, Tab, etc.)."""
    name = args.session
    _require_session(name)
    _run(["tmux", "send-keys", "-t", name, args.key])
    print(f"Key: {args.key}")


def cmd_cmd(args):
    """Type text and press Enter — combines send + key Enter."""
    name = args.session
    _require_session(name)
    text = args.text
    _run(["tmux", "send-keys", "-t", name, "-l", text])
    _run(["tmux", "send-keys", "-t", name, "Enter"])
    print(f"Sent: {text!r} + Enter")


def cmd_snapshot(args):
    """Capture screen + optional workspace tree/log — one call instead of 3."""
    name = args.session
    _require_session(name)

    # Capture screen
    r = _run(["tmux", "capture-pane", "-t", name, "-p", "-S", "-", "-E", "-"])
    if r.returncode == 0:
        text = ANSI_RE.sub("", r.stdout)
        lines = text.rstrip("\n").split("\n")
        print("=== SCREEN ===")
        print("\n".join(lines))

    # Optional workspace info
    pid = args.project_id
    if pid:
        ws_dir = os.path.join(RUN_DIR, "workspaces", pid)
        if os.path.isdir(ws_dir):
            print("\n=== TREE ===")
            _print_tree(ws_dir)
            print("\n=== LOG ===")
            r = _run(["git", "log", "--oneline", "-5"], cwd=ws_dir)
            print(r.stdout)


def cmd_inspect(args):
    """Inspect workspace: tree + diff + log combined."""
    pid = args.project_id
    ws_dir = os.path.join(RUN_DIR, "workspaces", pid)
    if not os.path.isdir(ws_dir):
        _die(f"Workspace not found: {ws_dir}")

    print("=== WORKSPACE TREE (planning artifacts + brief staging) ===")
    _print_tree(ws_dir)

    # The generated CODE lives in the separate code repo (projects/<pid>/), NOT
    # under workspaces/<pid>/project/ (which only holds the brief). Surface it
    # explicitly so "where's the code?" is never ambiguous again (AT-11).
    code_dir = os.path.join(RUN_DIR, "projects", pid)
    if os.path.isdir(code_dir):
        print(f"\n=== CODE REPO ({code_dir}) ===")
        _print_tree(code_dir)
        print("\n--- CODE REPO LOG ---")
        n = args.lines or 10
        r = _run(["git", "log", "--oneline", f"-{n}"], cwd=code_dir)
        print(r.stdout or "(no commits)")
    else:
        print(f"\n=== CODE REPO === (none yet at {code_dir})")

    print("\n=== WORKSPACE DIFF (stat) ===")
    r = _run(["git", "diff", "--stat"], cwd=ws_dir)
    print(r.stdout or "(clean)")

    print("\n=== WORKSPACE LOG ===")
    n = args.lines or 10
    r = _run(["git", "log", "--oneline", f"-{n}"], cwd=ws_dir)
    print(r.stdout)


def _print_tree(ws_dir: str):
    """Print workspace directory tree."""
    try:
        r = _run(["tree", "--noreport", "-L", "3", ws_dir])
        if r.returncode == 0:
            print(r.stdout.rstrip())
            return
    except FileNotFoundError:
        pass
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        level = root.replace(ws_dir, "").count(os.sep)
        indent = "  " * level
        basename = os.path.basename(root)
        if basename:
            print(f"{indent}{basename}/")
        subindent = "  " * (level + 1)
        for f in sorted(files):
            print(f"{subindent}{f}")


def cmd_watch(args):
    """Watch workspace files for changes. Emits one line per event."""
    project_id = args.project_id
    ws_dir = os.path.join(RUN_DIR, "workspaces", project_id)
    proj_dir = os.path.join(RUN_DIR, "projects", project_id)

    dirs = [ws_dir]
    if args.include_project:
        dirs.append(proj_dir)

    for d in dirs:
        if not os.path.isdir(d):
            _die(f"Directory not found: {d}")

    inotify_cmd = ["inotifywait", "-m", "-r", "--format", "%T %e %w%f",
                   "--timefmt", "%H:%M:%S"] + dirs

    try:
        proc = subprocess.Popen(inotify_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        _poll_watch(dirs)
        return

    for line in proc.stdout:
        line = line.strip()
        if line:
            print(line, flush=True)


def _poll_watch(dirs: list[str], interval: float = 1.0):
    """Fallback polling-based file watcher when inotifywait is not available."""
    snapshots = {}
    for d in dirs:
        snapshots[d] = _snapshot_dir(d)

    try:
        while True:
            time.sleep(interval)
            for d in dirs:
                current = _snapshot_dir(d)
                prev = snapshots[d]
                for path, mtime in current.items():
                    if path not in prev:
                        print(f"{time.strftime('%H:%M:%S')} CREATE {path}", flush=True)
                    elif prev[path] != mtime:
                        print(f"{time.strftime('%H:%M:%S')} MODIFY {path}", flush=True)
                for path in prev:
                    if path not in current:
                        print(f"{time.strftime('%H:%M:%S')} DELETE {path}", flush=True)
                snapshots[d] = current
    except KeyboardInterrupt:
        pass


def _snapshot_dir(d: str) -> dict[str, float]:
    """Take a snapshot of file paths and their modification times."""
    result = {}
    for root, _, files in os.walk(d):
        if "/.git/" in root or root.endswith("/.git"):
            continue
        for f in files:
            path = os.path.join(root, f)
            try:
                result[path] = os.path.getmtime(path)
            except OSError:
                pass
    return result


def cmd_tree(args):
    """Print workspace directory tree."""
    ws_dir = os.path.join(RUN_DIR, "workspaces", args.project_id)
    if not os.path.isdir(ws_dir):
        _die(f"Workspace not found: {ws_dir}")
    _print_tree(ws_dir)


def cmd_cat(args):
    """Print a file from the workspace."""
    path = os.path.join(RUN_DIR, "workspaces", args.project_id, args.path)
    path = os.path.realpath(path)
    ws_root = os.path.realpath(os.path.join(RUN_DIR, "workspaces", args.project_id))
    if not path.startswith(ws_root):
        _die("Path traversal outside workspace denied.")

    if not os.path.isfile(path):
        _die(f"File not found: {path}")

    with open(path) as f:
        print(f.read(), end="")


def cmd_diff(args):
    """Show git diff for the workspace."""
    ws_dir = os.path.join(RUN_DIR, "workspaces", args.project_id)
    if not os.path.isdir(ws_dir):
        _die(f"Workspace not found: {ws_dir}")

    r = _run(["git", "diff", "--stat"], cwd=ws_dir)
    print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    r = _run(["git", "diff"], cwd=ws_dir)
    print(r.stdout)


def cmd_log(args):
    """Show git log for the workspace."""
    ws_dir = os.path.join(RUN_DIR, "workspaces", args.project_id)
    if not os.path.isdir(ws_dir):
        _die(f"Workspace not found: {ws_dir}")

    n = args.lines or 20
    r = _run(["git", "log", "--oneline", f"-{n}"], cwd=ws_dir)
    print(r.stdout)


def _run_id_for_project(conn, pid: str):
    row = conn.execute(
        "SELECT id FROM skillflow_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
        (pid,),
    ).fetchone()
    return row[0] if row else None


def cmd_await(args):
    """Block until a run reaches a checkpoint / terminal state, then exit.

    The server already PUSHES these — `GET /api/events/stream` carries a
    `checkpoint_paused` event carrying {step_id, label, next_node, project_id}.
    Polling `/api/runs` in a sleep loop burns a request every few seconds and
    still reports the pause late; this blocks on the stream and returns the
    instant it fires.

    Exits 0 on a checkpoint, 0 on completion, 1 on run failure, 2 on timeout —
    so a caller can `debugctl.py await <pid> && do-the-next-thing`, and an agent
    can run it in the background and be woken by its exit.

    Terminal events are matched as well as checkpoints on purpose: a watcher
    that only greps for the happy path stays silent through a failure, and
    silence is indistinguishable from "still running".

    `--follow` keeps the stream open PAST a checkpoint and prints one line per
    event, exiting only when the run reaches a terminal state. Without it this
    is single-shot, and single-shot leaves a hole exactly where the driver is
    busiest: the watcher exits at the checkpoint, the driver reads the plan,
    approves, and re-arms — and anything that happens in between is unwitnessed.

    Live, jinyong-encounter 2026-08-23: checkpoint at 16:12:16, approved at
    16:12:57, run dead at 16:13:18 on an exhausted plan loop. The `await` had
    already exited at the checkpoint and the replacement watcher was a log grep
    whose filter did not include the terminal outcome, so nothing reported the
    death — the driver found out when the user said so. One `--follow` watcher
    armed once covers the whole run: every checkpoint is a line, and so is the
    ending.

    Pair it with a per-line consumer (each line is an event) rather than an
    exit-code consumer, since with `--follow` the exit comes only at the end.
    """
    import json as _json
    import urllib.request

    pid = args.project_id
    url = args.url.rstrip("/") + "/api/events/stream"
    deadline = time.time() + args.timeout
    want = {"checkpoint_paused", "run_completed", "run_failed", "pipeline_failed"}

    # The stream is REOPENED on close instead of ending the watch. `--follow`
    # promises to witness the run to a terminal state, and an SSE connection
    # dropping is a TRANSPORT event, not a pipeline event: the run is still
    # going. Live, jinyong-aim 2026-08-28 — the stream closed about an hour in,
    # await ended, and the run advanced two more cards with nobody watching.
    # Worse, the give-up path exited 2, the SAME CODE as a real TIMEOUT, so a
    # watcher that had silently stopped watching looked exactly like one that
    # waited out its full deadline. Exhausted reconnects now exit 4, which no
    # other outcome uses.
    resp = None
    backoff = 1.0
    fails = 0

    def _reopen():
        nonlocal resp, backoff, fails
        while time.time() < deadline:
            try:
                resp = urllib.request.urlopen(url, timeout=min(args.timeout, 300))
                if fails:
                    print(f"RECONNECTED to {url}", flush=True)
                backoff, fails = 1.0, 0
                return True
            except Exception as e:
                fails += 1
                if fails == 1 or fails % 10 == 0:
                    print(f"RECONNECT failed ({fails}x): {e}", flush=True)
                if fails >= 60:
                    return False
                time.sleep(min(backoff, 30.0))
                backoff *= 2
        return False

    if not _reopen():
        _die(f"cannot open the event stream at {url}")

    while True:
        for raw in resp:
            if time.time() > deadline:
                print(f"TIMEOUT after {args.timeout}s waiting on {pid}")
                sys.exit(2)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                # The stream wraps each event as {"log": "<json string>"}.
                outer = _json.loads(line[5:].strip())
                ev = _json.loads(outer["log"]) if isinstance(outer.get("log"), str) else outer
            except Exception:
                continue
            if ev.get("project_id") != pid:
                continue
            kind = ev.get("type", "")
            if kind not in want:
                continue
            if kind == "checkpoint_paused":
                print(f"CHECKPOINT {pid} step={ev.get('step_id')} "
                      f"label={ev.get('label')!r} next={ev.get('next_node')}",
                      flush=True)
                if not args.follow:
                    sys.exit(0)
                continue
            if kind == "run_completed":
                print(f"COMPLETED {pid}", flush=True)
                sys.exit(0)
            # Terminal failure ends the watch even under --follow: there is nothing
            # further to witness, and a watcher that kept waiting here would go
            # quiet forever on the one event that most needs reporting.
            print(f"FAILED {pid} {ev.get('error_reason') or ev.get('reason') or ev.get('detail') or ''}".rstrip(),
                  flush=True)
            sys.exit(1)

        # Fell out of the read loop: the server closed the stream. The run is
        # almost certainly still alive, so reconnect and keep watching.
        if time.time() >= deadline:
            print(f"TIMEOUT after {args.timeout}s waiting on {pid}", flush=True)
            sys.exit(2)
        print(f"STREAM CLOSED, reconnecting (still watching {pid})", flush=True)
        if not _reopen():
            print(f"STREAM LOST for {pid}: reconnects exhausted, NOT WATCHING ANY MORE",
                  flush=True)
            sys.exit(4)


def cmd_trace(args):
    """Dump the durable skillflow run trace chronologically.

    The trace is append-only and keyed by step_instance_id, so loop
    iterations don't overwrite — this replaces stitching Trace_*/ dirs +
    live SSE during investigation.  When per-project trace DBs are active
    (trace.db in the workspace), queries that instead of the shared DB.
    """
    import sqlite3
    import json as _json

    pid = args.project_id
    # Per-project trace DB takes priority.
    trace_db = os.path.join(RUN_DIR, "workspaces", pid, "trace.db")
    if os.path.isfile(trace_db):
        conn = sqlite3.connect(trace_db)
    else:
        db = os.path.join(RUN_DIR, "skillflow.db")
        if not os.path.isfile(db):
            _die(f"Neither trace.db nor skillflow.db found for '{pid}'")
        conn = sqlite3.connect(db)
    try:
        run_id = args.run_id or _run_id_for_project(conn, pid)
        if not run_id:
            _die(f"No skillflow run found for project '{pid}'")

        q = ("SELECT seq, step_id, step_instance_id, category, event, payload_json, created_at "
             "FROM skillflow_trace WHERE run_id = ?")
        params = [run_id]
        if args.category:
            q += " AND category = ?"
            params.append(args.category)
        if args.step:
            q += " AND step_id = ?"
            params.append(args.step)
        q += " ORDER BY seq ASC"
        try:
            rows = conn.execute(q, params).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                _die("skillflow_trace table not present yet — restart the server "
                     "(table is created on SkillFlow init) and run a fresh project.")
            raise
    finally:
        conn.close()

    if not rows:
        print(f"(no trace records for run {run_id})")
        return

    print(f"=== TRACE run={run_id} project={pid} ({len(rows)} records) ===")
    # Compact glyphs per category for fast scanning.
    glyph = {"step": "▶", "prompt": "→", "response": "←", "tool_call": "🔧",
             "tool_result": "✓", "lifecycle": "⚙", "event": "•"}
    full = args.full
    for seq, step_id, inst, cat, event, payload_json, ts in rows:
        try:
            payload = _json.loads(payload_json)
        except Exception:
            payload = {}
        g = glyph.get(cat, " ")
        head = f"{seq:>4} {g} [{cat}/{event}] {step_id or '-'}#{inst if inst is not None else '-'}"
        # One-line salient summary per category.
        summary = ""
        if cat == "tool_call":
            summary = _json.dumps(payload.get("params", {}))[:160]
        elif cat == "tool_result":
            summary = ", ".join(f"{k}={payload[k]}" for k in ("written", "error", "applied") if k in payload)
        elif cat == "lifecycle":
            summary = f"{payload.get('status','')} {payload.get('detail','')}".strip()
        elif cat == "response":
            tcs = payload.get("tool_calls") or []
            summary = (f"tools={tcs} " if tcs else "") + (payload.get("text", "")[:120].replace("\n", " "))
        elif cat == "prompt":
            summary = (payload.get("user", "")[:120].replace("\n", " "))
        elif cat == "step":
            summary = _json.dumps({k: v for k, v in payload.items() if v})[:160]
        print(f"{head}  {summary}")
        if full and cat in ("prompt", "response"):
            body = payload.get("user") or payload.get("text") or ""
            if body:
                for line in body.splitlines():
                    print(f"        | {line}")


# ── Argument parser ────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="debugctl",
        description="AItelier CLI debug controller for Claude Code",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ── start ──
    s = sub.add_parser("start", help="Launch aitelier CLI in a tmux session")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("args", nargs="*", help="Extra args passed to 'aitelier'")
    s.set_defaults(func=cmd_start)

    # ── stop ──
    s = sub.add_parser("stop", help="Kill the tmux session")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.set_defaults(func=cmd_stop)

    # ── capture ──
    s = sub.add_parser("capture", help="Capture the current screen content")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("--ansi", action="store_true", help="Keep ANSI escape codes")
    s.set_defaults(func=cmd_capture)

    # ── send ──
    s = sub.add_parser("send", help="Send text to the CLI")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("text", help="Text to type into the CLI")
    s.set_defaults(func=cmd_send)

    # ── key ──
    s = sub.add_parser("key", help="Send a special key")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("key", help="Key name (Enter, Escape, Up, Down, Tab, C-c, etc.)")
    s.set_defaults(func=cmd_key)

    # ── cmd (composite: send + Enter) ──
    s = sub.add_parser("cmd", help="Type text and press Enter (saves 1 tool call)")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("text", help="Text to type + Enter")
    s.set_defaults(func=cmd_cmd)

    # ── snapshot (composite: capture + tree + log) ──
    s = sub.add_parser("snapshot", help="Capture screen + workspace tree/log (saves 2-3 tool calls)")
    s.add_argument("--session", default=DEFAULT_SESSION)
    s.add_argument("project_id", nargs="?", help="Optional project ID to include workspace info")
    s.set_defaults(func=cmd_snapshot)

    # ── inspect (composite: tree + diff + log) ──
    s = sub.add_parser("inspect", help="Workspace tree + diff + log combined (saves 2 tool calls)")
    s.add_argument("project_id")
    s.add_argument("--lines", type=int, default=10)
    s.set_defaults(func=cmd_inspect)

    # ── watch ──
    s = sub.add_parser("watch", help="Watch workspace files for changes")
    s.add_argument("project_id")
    s.add_argument("--include-project", action="store_true",
                   help="Also watch the project workspace")
    s.set_defaults(func=cmd_watch)

    # ── tree ──
    s = sub.add_parser("tree", help="Show workspace directory tree")
    s.add_argument("project_id")
    s.set_defaults(func=cmd_tree)

    # ── cat ──
    s = sub.add_parser("cat", help="Print a workspace file")
    s.add_argument("project_id")
    s.add_argument("path", help="Relative path inside the workspace")
    s.set_defaults(func=cmd_cat)

    # ── diff ──
    s = sub.add_parser("diff", help="Show git diff for the workspace")
    s.add_argument("project_id")
    s.set_defaults(func=cmd_diff)

    # ── log ──
    s = sub.add_parser("log", help="Show git log for the workspace")
    s.add_argument("project_id")
    s.add_argument("--lines", type=int, default=20)
    s.set_defaults(func=cmd_log)

    # ── await ──
    s = sub.add_parser("await", help="Block on the SSE stream until a checkpoint / terminal state")
    s.add_argument("project_id")
    s.add_argument("--url", default="http://localhost:4444", help="API base URL")
    s.add_argument("--timeout", type=int, default=3600, help="Give up after N seconds (exit 2)")
    s.add_argument("--follow", action="store_true",
                   help="Keep watching past each checkpoint; exit only on a "
                        "terminal state (one line per event)")
    s.set_defaults(func=cmd_await)

    # ── trace ──
    s = sub.add_parser("trace", help="Dump durable skillflow run trace (events/prompts/actions)")
    s.add_argument("project_id")
    s.add_argument("--run-id", default="", help="Specific run id (default: latest for project)")
    s.add_argument("--category", default="",
                   help="Filter: step|prompt|response|tool_call|tool_result|lifecycle|event")
    s.add_argument("--step", default="", help="Filter by step_id (e.g. t_impl)")
    s.add_argument("--full", action="store_true", help="Print full prompt/response bodies")
    s.set_defaults(func=cmd_trace)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
