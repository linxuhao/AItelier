#!/usr/bin/env python3
"""Host-side FIFO queue in front of the Godot gate's globally exclusive render lock.

WHY. A full gate run on the wuxia game takes ~55 minutes and the sidecar's render
lock is globally exclusive: exactly one engine run at a time. Today every subagent
starts its own gate with `gate_run.sh` and then SITS THERE waiting, so N agents on
one lock burn N full LLM contexts per wake and buy nothing.

So: `submit` writes a ticket to disk and returns IMMEDIATELY without touching the
engine — the calling process can exit and the gate still runs to completion — and a
single host-side daemon drains the queue FIFO, one engine run at a time.

NOT a pipeline. A pipeline run is scheduler-owned and core/scheduler.py advances one
project per tick, so the gate would queue behind every other run; and a tool step
writing into the code tree deadlocks against skillflow's clean-worktree guard.

This WRAPS `~/.AItelier/bin/gate_run.sh` (battle-tested: rc 64 not a git repo,
rc 65 dirty tree, rc 66 tree changed under the gate, throwaway container so a
container recreation cannot kill the gate). It does not replace it.

Durability: the queue IS the filesystem. A queued ticket survives a daemon restart
and a host-side crash (the daemon requeues on boot); a ticket whose engine run is in
flight is adopted if its throwaway container is still alive, and requeued otherwise.

States:
  queued   ticket on disk, engine not yet touched
  running  holding the render lock
  done     the gate delivered a verdict; see `verdict` (passed/failed) + report_dir
  failed   no verdict was produced (not a repo, dirty tree, poisoned tree, infra)

`done` is about DELIVERY, not about the game passing. Read `verdict`.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME", Path.home()))
ROOT = Path(os.environ.get("GATE_QUEUE_DIR", HOME / ".AItelier/gate-queue"))
REPORTS = Path(os.environ.get("GATE_QUEUE_REPORTS", HOME / ".AItelier/gate-reports"))
GATE_RUN = Path(os.environ.get("GATE_RUN_SH", HOME / ".AItelier/bin/gate_run.sh"))
TICKETS = ROOT / "tickets"
LOCK = ROOT / "daemon.lock"
PIDFILE = ROOT / "daemon.pid"
STOPFILE = ROOT / "stop"
DAEMON_LOG = ROOT / "daemon.log"
SUFFIX = "gate"  # gate_run.sh writes <SUFFIX>.gate.{log,exit,head}
POLL_SECONDS = float(os.environ.get("GATE_QUEUE_POLL", "2"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = "%s %s\n" % (now(), message)
    ROOT.mkdir(parents=True, exist_ok=True)
    with DAEMON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    sys.stderr.write(line)


# --- tickets ----------------------------------------------------------------

def ticket_path(ticket: str) -> Path:
    return TICKETS / ticket / "ticket.json"


def read_ticket(ticket: str) -> dict:
    return json.loads(ticket_path(ticket).read_text(encoding="utf-8"))


def write_ticket(record: dict) -> None:
    path = ticket_path(record["ticket"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)  # atomic: a reader never sees half a ticket


def all_tickets() -> list[dict]:
    out = []
    for d in sorted(TICKETS.glob("*/ticket.json")):
        try:
            out.append(json.loads(d.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("submitted_at", ""))


def report_parent(ticket: str) -> Path:
    return REPORTS / ticket


# --- submit -----------------------------------------------------------------

def cmd_submit(args) -> int:
    worktree = Path(args.worktree).resolve()
    ticket = "gq-%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                           uuid.uuid4().hex[:8])
    record = {
        "ticket": ticket,
        "worktree": str(worktree),
        "sha": args.sha,
        "label": args.label or "",
        "state": "queued",
        "submitted_at": now(),
        "report_dir": None,
        "gate_exit_code": None,
        "verdict": None,
        "error": None,
    }
    write_ticket(record)
    print(ticket)
    if not daemon_alive():
        print("warning: no gate-queue daemon is running — this ticket stays "
              "queued until one starts (%s daemon)" % Path(__file__).name,
              file=sys.stderr)
    return 0


# --- status -----------------------------------------------------------------

def cmd_status(args) -> int:
    try:
        record = read_ticket(args.ticket)
    except OSError:
        print("unknown ticket: %s" % args.ticket, file=sys.stderr)
        return 3
    print("ticket:   %s" % record["ticket"])
    print("state:    %s" % record["state"])
    print("worktree: %s" % record["worktree"])
    print("sha:      %s" % record["sha"])
    print("submitted:%s" % record["submitted_at"])
    for key in ("started_at", "finished_at", "gate_exit_code", "verdict", "error"):
        if record.get(key) not in (None, ""):
            print("%-10s%s" % (key + ":", record[key]))
    if record["state"] == "done":
        print("report_dir: %s" % record["report_dir"])
    elif record["state"] == "queued":
        ahead = [r for r in all_tickets()
                 if r["state"] == "queued"
                 and r["submitted_at"] < record["submitted_at"]]
        print("ahead_in_queue: %d" % len(ahead))
    return 0


def cmd_list(args) -> int:
    for r in all_tickets():
        print("%-8s %-28s %s %s" % (r["state"], r["ticket"], r["sha"][:12],
                                    r.get("label", "")))
    return 0


# --- daemon -----------------------------------------------------------------

def daemon_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def daemon_alive() -> bool:
    return daemon_pid() is not None


def container_alive(ticket: str) -> bool:
    """Is a throwaway gate container still running? (the queue serializes, so
    at most one exists, and it belongs to the one running ticket.)"""
    name = "wuxia-gate-%s-" % SUFFIX
    out = subprocess.run(["docker", "ps", "--filter", "name=" + name,
                          "--format", "{{.Names}}"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def finalize(record: dict) -> dict:
    """Read the exit FILE (never a pipe — a pipe eats the exit code) and flatten."""
    parent = report_parent(record["ticket"])
    exitf = parent / ("%s.gate.exit" % SUFFIX)
    raw = exitf.read_text().strip() if exitf.exists() else ""
    inner = sorted(parent.glob("wuxia-godot-gate-*"))
    if len(inner) == 1:
        for item in inner[0].iterdir():  # promote the report to <ticket>/
            shutil.move(str(item), str(parent / item.name))
        inner[0].rmdir()
    manifest = parent / "manifest.json"
    if raw in ("0", "1") and manifest.exists():
        record.update(state="done", gate_exit_code=int(raw),
                      verdict={"0": "passed", "1": "failed"}[raw],
                      report_dir=str(parent))
    else:
        record.update(state="failed", gate_exit_code=raw or None,
                      error=record.get("error") or
                      ("no verdict: exit=%r manifest=%s"
                       % (raw, manifest.exists())))
    record["finished_at"] = now()
    write_ticket(record)
    return record


def run_one(record: dict) -> None:
    parent = report_parent(record["ticket"])
    parent.mkdir(parents=True, exist_ok=True)
    record.update(state="running", started_at=now(), error=None)
    write_ticket(record)
    log("running %s %s @%s" % (record["ticket"], record["worktree"],
                               record["sha"][:12]))

    head = subprocess.run(["git", "-C", record["worktree"], "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head != record["sha"]:
        record["error"] = ("HEAD is %s, ticket asked for %s — refusing to gate a "
                           "tree that is not the submitted commit"
                           % (head or "<unreadable>", record["sha"]))
        record.update(state="failed", finished_at=now())
        write_ticket(record)
        log("refused %s: %s" % (record["ticket"], record["error"]))
        return

    proc = subprocess.run([str(GATE_RUN), record["worktree"], str(parent), SUFFIX],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True)
    if proc.returncode in (64, 65, 66) or proc.stderr.strip():
        record["error"] = "gate_run.sh rc=%d %s" % (proc.returncode,
                                                    proc.stderr.strip()[:400])
    finalize(record)
    log("finished %s state=%s exit=%s" % (record["ticket"], record["state"],
                                          record["gate_exit_code"]))


def reconcile() -> None:
    """A daemon restart must not lose work. Adopt or requeue in-flight tickets."""
    for record in all_tickets():
        if record["state"] != "running":
            continue
        exitf = report_parent(record["ticket"]) / ("%s.gate.exit" % SUFFIX)
        if exitf.exists():
            log("adopting finished %s" % record["ticket"])
            finalize(record)
        elif container_alive(record["ticket"]):
            log("adopting in-flight %s — waiting for its exit file"
                % record["ticket"])
            while container_alive(record["ticket"]) and not exitf.exists():
                time.sleep(POLL_SECONDS)
            finalize(record)
        else:
            log("requeueing %s — nothing is running it" % record["ticket"])
            record.update(state="queued", started_at=None)
            write_ticket(record)


def cmd_daemon(args) -> int:
    ROOT.mkdir(parents=True, exist_ok=True)
    TICKETS.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another gate-queue daemon holds %s (pid %s)"
              % (LOCK, daemon_pid()), file=sys.stderr)
        return 4
    STOPFILE.unlink(missing_ok=True)
    PIDFILE.write_text("%d\n" % os.getpid())
    stopping = {"now": False}

    def on_signal(signum, frame):
        stopping["now"] = True
        log("signal %d — will stop after the current gate" % signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log("daemon up pid=%d queue=%s reports=%s" % (os.getpid(), ROOT, REPORTS))
    try:
        reconcile()
        while not stopping["now"] and not STOPFILE.exists():
            pending = [r for r in all_tickets() if r["state"] == "queued"]
            if not pending:
                if args.once:
                    break
                time.sleep(POLL_SECONDS)
                continue
            run_one(pending[0])  # FIFO: all_tickets() sorts by submitted_at
            if args.once:
                break
    finally:
        log("daemon down pid=%d" % os.getpid())
        PIDFILE.unlink(missing_ok=True)
        STOPFILE.unlink(missing_ok=True)
    return 0


def cmd_stop(args) -> int:
    """Stop via the daemon's own control path — never pkill."""
    ROOT.mkdir(parents=True, exist_ok=True)
    STOPFILE.write_text(now() + "\n")
    pid = daemon_pid()
    if pid is None:
        print("no daemon running; stop flag left for the next one")
        return 0
    os.kill(pid, signal.SIGTERM)
    print("asked daemon pid %d to stop after the current gate" % pid)
    return 0


def cmd_daemon_status(args) -> int:
    pid = daemon_pid()
    print("daemon: %s" % ("running pid %d" % pid if pid else "not running"))
    counts: dict[str, int] = {}
    for r in all_tickets():
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    print("tickets: %s" % (counts or "none"))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit", help="queue a gate; returns a ticket id at once")
    p.add_argument("worktree")
    p.add_argument("sha")
    p.add_argument("--label", default="")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("status", help="queued / running / done / failed")
    p.add_argument("ticket")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("daemon", help="drain the queue, one engine run at a time")
    p.add_argument("--once", action="store_true",
                   help="run at most one ticket, then exit")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("stop", help="ask the daemon to stop after the current gate")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("daemon-status")
    p.set_defaults(func=cmd_daemon_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
