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
  cancelled  taken out of the queue by `cancel` before it ever started; see
           `cancellation`. Not done/failed: no exit code, no verdict, no report.

`done` is about DELIVERY, not about the game passing. Read `verdict`.

CANCEL. `cancel TICKET --expected-sha SHA --reason TEXT [--actor NAME]` withdraws ONE
queued ticket that never started (nothing in it but what `submit` wrote, no report
directory). It takes mutation.lock, non-blocking, for the short read/check/write
transition (rc 4 if busy). The daemon keeps daemon.lock for single-instance ownership
but releases mutation.lock while a gate runs. Cancel never signals or stops anything,
and a refusal writes nothing. The ticket keeps every original field; the
exact pre-cancel ticket.json is kept in `cancellation.original_text` with its sha256 in
`cancellation.original_sha256`. rc: 0 cancelled or already-cancelled, 3 unknown ticket,
4 mutation lock held, 5 refused (wrong sha, not queued, started, malformed...).
A cancelled ticket is terminal; resubmit explicitly if work is wanted again.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME", Path.home()))
ROOT = Path(os.environ.get("GATE_QUEUE_DIR", HOME / ".AItelier/gate-queue"))
REPORTS = Path(os.environ.get("GATE_QUEUE_REPORTS", HOME / ".AItelier/gate-reports"))
GATE_RUN = Path(os.environ.get("GATE_RUN_SH", HOME / ".AItelier/bin/gate_run.sh"))
TICKETS = ROOT / "tickets"
LOCK = ROOT / "daemon.lock"
MUTATION_LOCK = ROOT / "mutation.lock"
PIDFILE = ROOT / "daemon.pid"
STOPFILE = ROOT / "stop"
DAEMON_LOG = ROOT / "daemon.log"
SUFFIX = "gate"  # gate_run.sh writes <SUFFIX>.gate.{log,exit,head}
POLL_SECONDS = float(os.environ.get("GATE_QUEUE_POLL", "2"))
TICKET_ID = re.compile(r"gq-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")  # what cmd_submit mints
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
# Exactly the fields cmd_submit writes. run_one() adds started_at, so any ticket the
# daemon ever picked up -- even one reconcile() put back to queued -- has more.
SUBMITTED_KEYS = frozenset(("ticket", "worktree", "sha", "label", "state", "submitted_at",
                            "report_dir", "gate_exit_code", "verdict", "error"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = "%s %s\n" % (now(), message)
    ROOT.mkdir(parents=True, exist_ok=True)
    with DAEMON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line)
    sys.stderr.write(line)


# --- tickets ----------------------------------------------------------------

@contextmanager
def mutation_lock():
    """Serialize only ticket transitions, never a gate's execution or adoption wait."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with MUTATION_LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


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
    with mutation_lock():
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
    elif record["state"] == "cancelled":
        info = record.get("cancellation") or {}
        print("cancelled: %s by %s -- %s" % (info.get("cancelled_at"), info.get("actor"),
                                             info.get("reason")))
        print("preimage: sha256 %s" % info.get("original_sha256"))
    return 0


def cmd_list(args) -> int:
    for r in all_tickets():
        print("%-8s %-28s %s %s" % (r["state"], r["ticket"], r["sha"][:12],
                                    r.get("label", "")))
    return 0


# --- cancel -----------------------------------------------------------------

def cancel_preimage_problem(record: dict) -> str | None:
    """Why a `cancelled` ticket carries no verifiable cancellation, or None.

    The ticket keeps every original field; `cancellation.original_text` is the exact
    ticket.json it replaced and `original_sha256` is that file's hash, so the
    pre-cancel bytes can be re-derived and checked from the ticket alone."""
    info = record.get("cancellation")
    if record.get("state") != "cancelled" or not isinstance(info, dict):
        return "state %r carries no cancellation record" % (record.get("state"),)
    text = info.get("original_text")
    if (not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != info.get("original_sha256")):
        return "the recorded preimage does not match its sha256"
    try:
        original = json.loads(text)
    except ValueError:
        return "the recorded preimage is not JSON"
    kept = {k: v for k, v in record.items() if k not in ("state", "cancellation")}
    if (not isinstance(original, dict) or original.get("state") != "queued"
            or kept != {k: v for k, v in original.items() if k != "state"}
            or info.get("expected_sha") != record.get("sha")):
        return "the ticket's fields differ from its recorded preimage"
    return None


def cancel_unstarted_problem(record: dict) -> str | None:
    """Why this is not a queued ticket that never started, or None.

    `cmd_submit` writes exactly SUBMITTED_KEYS with every result field null. Anything the
    daemon does adds a key or fills one, and a started ticket also has a report directory."""
    if record.get("state") != "queued":
        return "state is %r, not 'queued'" % (record.get("state"),)
    if set(record) != SUBMITTED_KEYS:
        return "fields differ from what `submit` writes: %s" % sorted(set(record) ^ SUBMITTED_KEYS)
    for key in ("report_dir", "gate_exit_code", "verdict", "error"):
        if record[key] is not None:
            return "%s is already set (%r)" % (key, record[key])
    parent = report_parent(record["ticket"])
    if os.path.lexists(parent):
        return "a report directory already exists: %s" % parent
    extra = sorted(p.name for p in ticket_path(record["ticket"]).parent.iterdir()
                   if p.name not in ("ticket.json", "ticket.tmp"))
    if extra:
        return "the ticket directory holds other files: %s" % extra
    return None


def cmd_cancel(args) -> int:
    """Withdraw ONE queued, never-started ticket, keeping its history.

    Decided under mutation.lock (flock, non-blocking; its inode is never replaced).
    Admission and finalization share this short lock. daemon.lock remains exclusively
    the daemon's lifetime lock. A refusal writes nothing; nothing is stopped."""
    def refuse(message: str, rc: int = 5) -> int:
        print("refused: %s (nothing was changed)" % message, file=sys.stderr)
        return rc

    reason = args.reason.strip()
    if not TICKET_ID.fullmatch(args.ticket):
        return refuse("not a ticket id: %r" % args.ticket)
    if not FULL_SHA.fullmatch(args.expected_sha):
        return refuse("--expected-sha must be a full lowercase 40-hex sha, got %r" % args.expected_sha)
    if not reason:
        return refuse("--reason must not be empty")
    if not ROOT.is_dir():
        print("unknown ticket: %s (no queue at %s)" % (args.ticket, ROOT), file=sys.stderr)
        return 3
    fd = os.open(MUTATION_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return refuse("another ticket transition holds %s" % MUTATION_LOCK, 4)
        # Everything below is read, checked and written while holding the lock.
        path = ticket_path(args.ticket)
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            print("unknown ticket: %s" % args.ticket, file=sys.stderr)
            return 3
        if not stat.S_ISREG(mode) or path.parent.is_symlink():
            return refuse("ticket.json is not a regular file in a real ticket directory")
        raw = path.read_bytes()
        try:
            record = json.loads(raw.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError is one
            return refuse("ticket.json is malformed")
        if not isinstance(record, dict):
            return refuse("ticket.json is not a JSON object")
        if record.get("ticket") != args.ticket:
            return refuse("the record names ticket %r, not %r" % (record.get("ticket"), args.ticket))
        if record.get("sha") != args.expected_sha:
            return refuse("ticket sha is %r, not the expected %s" % (record.get("sha"),
                                                                    args.expected_sha))
        if record.get("state") == "cancelled":
            problem = cancel_preimage_problem(record)
            if problem:
                return refuse("ticket is 'cancelled' but %s" % problem)
            info = record["cancellation"]  # someone's decision already stands: report it, write nothing
            print("already-cancelled %s by %s at %s: %s" % (args.ticket, info.get("actor"),
                                                            info.get("cancelled_at"),
                                                            info.get("reason")))
            return 0
        problem = cancel_unstarted_problem(record)
        if problem:
            return refuse(problem)
        try:
            os_user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            os_user = "uid:%d" % os.getuid()
        info = {"cancelled_at": now(), "actor": args.actor.strip() or os_user, "os_user": os_user,
                "reason": reason, "expected_sha": args.expected_sha, "original_state": "queued",
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "original_text": raw.decode("utf-8")}
        write_ticket(dict(record, state="cancelled", cancellation=info))
        log("cancelled %s @%s by %s: %s (original ticket sha256 %s)"
            % (args.ticket, args.expected_sha[:12], json.dumps(info["actor"], ensure_ascii=False),
               json.dumps(reason, ensure_ascii=False), info["original_sha256"]))
        print("cancelled %s sha=%s original_sha256=%s" % (args.ticket, args.expected_sha,
                                                          info["original_sha256"]))
        return 0
    finally:
        os.close(fd)


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


def finalize(record: dict, runner_rc: int | None = None) -> dict:
    """Finish only a fresh running record; stale selections cannot restore a cancel."""
    with mutation_lock():
        current = read_ticket(record["ticket"])
        if current["state"] != "running":
            return current
        if record.get("error"):
            current["error"] = record["error"]
        return _finalize(current, runner_rc)


def _finalize(record: dict, runner_rc: int | None = None) -> dict:
    """Read the exit FILE and flatten while the caller holds mutation.lock."""
    parent = report_parent(record["ticket"])
    exitf = parent / ("%s.gate.exit" % SUFFIX)
    raw = exitf.read_text().strip() if exitf.exists() else ""
    inner = sorted(parent.glob("wuxia-godot-gate-*"))
    if len(inner) > 1:
        # A test inside the gated tree can leave its own wuxia-godot-gate-* dirs
        # here (tests/test_repeatability_gate.py did, 2026-09-23..25: 5 green gates
        # discarded). The gate names its own report dir on its log's first line.
        logf = parent / ("%s.gate.log" % SUFFIX)
        first = logf.read_text(errors="replace").splitlines()[:1] if logf.exists() else []
        named = first[0].split("gate reports:", 1)[1].strip() if first and "gate reports:" in first[0] else ""
        inner = [d for d in inner if str(d) == named]
        if inner:
            record["stray_report_dirs"] = sorted(str(d) for d in parent.glob("wuxia-godot-gate-*") if str(d) != named)
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
        record.update(state="failed", gate_exit_code=raw or runner_rc,
                      error=record.get("error") or
                      ("no verdict: exit=%r manifest=%s"
                       % (raw, manifest.exists())))
    record["finished_at"] = now()
    write_ticket(record)
    return record


def run_one(record: dict) -> bool:
    with mutation_lock():
        # Selection may predate a cancel. Admission must read disk again.
        record = read_ticket(record["ticket"])
        if record["state"] != "queued":
            return False
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
        with mutation_lock():
            current = read_ticket(record["ticket"])
            if current["state"] == "running":
                current.update(state="failed", finished_at=now(), error=record["error"])
                write_ticket(current)
        log("refused %s: %s" % (record["ticket"], record["error"]))
        return True

    proc = subprocess.run([str(GATE_RUN), record["worktree"], str(parent), SUFFIX],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True)
    if proc.returncode in (64, 65, 66) or proc.stderr.strip():
        record["error"] = "gate_run.sh rc=%d %s" % (proc.returncode,
                                                    proc.stderr.strip()[:400])
    record = finalize(record, proc.returncode)
    log("finished %s state=%s exit=%s" % (record["ticket"], record["state"],
                                          record["gate_exit_code"]))
    return True


def reconcile() -> None:
    """A daemon restart must not lose work. Adopt or requeue in-flight tickets."""
    with mutation_lock():
        records = all_tickets()
    for selected in records:
        with mutation_lock():
            record = read_ticket(selected["ticket"])
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
            with mutation_lock():
                current = read_ticket(record["ticket"])
                if current["state"] == "running":
                    current.update(state="queued", started_at=None)
                    write_ticket(current)


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
            with mutation_lock():
                pending = [r for r in all_tickets() if r["state"] == "queued"]
            if not pending:
                if args.once:
                    break
                time.sleep(POLL_SECONDS)
                continue
            admitted = run_one(pending[0])  # FIFO: all_tickets() sorts by submitted_at
            if args.once and admitted:
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

    p = sub.add_parser("cancel", help="withdraw one queued, never-started ticket "
                                      "(takes the short mutation.lock; never stops running work)")
    p.add_argument("ticket")
    p.add_argument("--expected-sha", required=True,
                   help="the full commit sha the ticket was submitted for")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="",
                   help="who is cancelling (free text; default: the OS user, always "
                        "recorded as os_user too)")
    p.set_defaults(func=cmd_cancel)

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
