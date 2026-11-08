#!/usr/bin/env python3
"""Install the host gate FIFO from the reviewed bytes in Git, or prove they are installed.

`~/.AItelier/bin/gate_queue.py` is the single-file queue the host's gates actually
run. It used to be refreshed by copying a reviewed source file by hand, and nothing
noticed when that copy was skipped: measured 2026-11-08, the installed copy was six
commits and nineteen days stale -- installed from git 8a755972cc, sha256
95b1255073bd09bbb8f77f69ad3c8113e6e8ee8ed43cda58c1164343e9fae7e6 -- while the
reviewed `scripts/gate_queue.py` at 7e9f53a8c1 was sha256
409f0779a8b32ca929f85bfbd0045c12df2f09955e5fbb0bca0fd993fc808e43, and the
repository's own host-gate-queue suite measured 3 failed / 61 passed against the
installed copy and 64 passed against the reviewed one.

So this tool is that copy, made checkable and refusing:

* the bytes come from Git -- `git -C <repo> show <commit>:scripts/gate_queue.py` --
  never from the working tree, so what is installed is a named reviewed commit;
* it writes only when those bytes hash to an explicitly supplied `--sha256`;
* it refuses to write while the queue daemon is live or while any ticket is not
  terminal, and the refusal names the measurement that blocked it;
* `--check` never writes; it exits 0 only when the installed bytes equal the
  reviewed bytes, and otherwise prints both sha256 values and the commit it
  compared against.

HOW LIVENESS AND TICKETS ARE OBSERVED (nothing is invented here). The queue keeps
its own markers, read from `scripts/gate_queue.py`: a daemon holds an exclusive
flock on `<queue>/daemon.lock` for its whole lifetime (`cmd_daemon`) and publishes
its pid in `<queue>/daemon.pid`; every ticket is
`<queue>/tickets/<ticket>/ticket.json` with a `state` from the queue's own
vocabulary, where `done`, `failed` and `cancelled` are terminal and `queued`,
`running` and `held` are not ("held bound ticket needs explicit
disposition/recheck").

The default probe measures exactly those artifacts, corroborates the pidfile
against the real process (`/proc/<pid>`: alive, not a zombie, cmdline is this
queue) and refuses when it cannot measure -- a refusal is never a guess. During
the write it also takes the queue's own `daemon.lock` exclusively and holds it
until the new bytes are in place, so no daemon can start halfway through.

`--daemon-probe` / `--ticket-probe CMD` replace one probe with a command, for the
tests (and for a host whose queue root this tool cannot read): CMD receives the
queue directory as its only argument and exits 0 when the blocker it measures is
present, non-zero when it is absent. Its first stdout line is quoted verbatim in
the refusal.

Exit codes: 0 installed, or (with `--check`) exact match; 1 `--check` drift; 2
refusal (usage, unreadable commit, sha256 pin, unsettled queue, filesystem). The
write is a temp file in the target directory followed by `os.replace`, mode 0644.

Usage:
  tools/install_gate_queue.py --commit 7e9f53a8c1... --sha256 409f0779...
  tools/install_gate_queue.py --check --commit 7e9f53a8c1... --sha256 409f0779...
  tools/install_gate_queue.py --commit ... --sha256 ... [--repo DIR] [--queue-dir DIR]
                             [--daemon-probe CMD] [--ticket-probe CMD] [DEST]
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys

NAME = "gate_queue.py"
SOURCE = "scripts/gate_queue.py"
MODE = 0o644
INSTALL_LOCK = ".gate_queue.install.lock"
FULL_SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# The queue's own terminal states; anything else -- including a state this tool
# does not know, and a ticket.json it cannot read -- is treated as unsettled.
TERMINAL = frozenset(("done", "failed", "cancelled"))
PROBE_TIMEOUT = 30


class Refusal(Exception):
    """A measured reason not to write. The message names the blocker."""


class RefusingParser(argparse.ArgumentParser):
    """Usage errors are refusals too: same wording, same exit code, nothing written."""

    def error(self, message):
        raise Refusal(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_regular(path, *, dir_fd=None):
    """Read a regular file without following a symlink, rejecting a mid-read change."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise Refusal("not a regular file: %s" % path)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode,
                              s.st_uid, s.st_gid, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise Refusal("file changed while it was being read: %s" % path)
        return raw, identity(after)
    finally:
        os.close(fd)


# --- the reviewed bytes ------------------------------------------------------

def reviewed_bytes(repo, commit):
    """Exactly `git -C <repo> show <commit>:scripts/gate_queue.py`."""
    if not repo.is_dir():
        raise Refusal("repository %s is not a directory" % repo)
    if FULL_SHA.fullmatch(commit) is None:
        raise Refusal("--commit must be a full lowercase 40-hex commit sha, got %r "
                      "(a branch name would make --check's answer ambiguous)" % commit)
    try:
        done = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (commit, SOURCE)],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("could not run git in %s (%s)" % (repo, type(exc).__name__)) from None
    if done.returncode != 0:
        detail = (done.stderr.decode("utf-8", "replace").splitlines() or [""])[0][:400]
        raise Refusal("git could not read %s from commit %s: %s"
                      % (SOURCE, commit, detail or "exit %d" % done.returncode))
    return done.stdout


# --- what the queue itself records ------------------------------------------

def process_observation(pid):
    """(alive, zombie, cmdline) from /proc -- the real process, not the pidfile."""
    try:
        fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False, False, ""
    state = fields[0] if fields else ""
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
    except OSError:
        raw = b""
    cmdline = " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)
    return state != "Z", state == "Z", cmdline


def run_probe(command, kind, queue_dir):
    """Run one probe command: exit 0 means the blocker it measures is present."""
    try:
        done = subprocess.run([command, str(queue_dir)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                              env=dict(os.environ, GATE_QUEUE_PROBE=kind))
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("the %s probe %s could not be run (%s); refusing rather than guessing"
                      % (kind, command, type(exc).__name__)) from None
    line = (done.stdout.splitlines() or [""])[0][:400]
    return done.returncode == 0, done.returncode, line


def probe_daemon(queue_dir, override=None):
    """Measure the queue daemon: locked `daemon.lock` and/or a live pid in `daemon.pid`."""
    measured = {"probe": str(override) if override else "daemon.lock+daemon.pid+/proc",
                "queue_dir": str(queue_dir)}
    if override:
        present, rc, line = run_probe(override, "daemon", queue_dir)
        measured.update(live=present,
                        evidence="the daemon probe %s exited %d: %r" % (override, rc, line))
        return measured

    lock = queue_dir / "daemon.lock"
    pidfile = queue_dir / "daemon.pid"
    measured.update(lock=str(lock), pidfile=str(pidfile))
    if not queue_dir.is_dir():
        measured.update(live=False, evidence="there is no queue root at %s, so nothing can "
                                             "be live there" % queue_dir)
        return measured

    held = False
    if os.path.lexists(lock):
        info = os.lstat(lock)
        if not stat.S_ISREG(info.st_mode):
            raise Refusal("%s is not a regular file, so liveness cannot be measured through "
                          "the queue's own lock" % lock)
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                held = True
        finally:
            os.close(fd)
        measured["lock_present"] = True
        measured["lock_held"] = held
    else:
        measured["lock_present"] = False

    pid = None
    alive = zombie = matches = False
    cmdline = ""
    if os.path.lexists(pidfile):
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        if pid is None:
            measured["pidfile_verdict"] = "present but does not hold a pid"
        else:
            alive, zombie, cmdline = process_observation(pid)
            words = cmdline.split()
            matches = "daemon" in words and any(word.endswith(NAME) for word in words)
            measured["pidfile_verdict"] = dict(pid=pid, alive=alive, zombie=zombie,
                                               cmdline=cmdline[:400],
                                               names_this_queue=matches)
    else:
        measured["pidfile_verdict"] = "absent"

    live = held or (alive and not zombie and matches)
    if live:
        pieces = []
        if held:
            pieces.append("an exclusive flock on %s is held" % lock)
        if alive and not zombie and matches:
            pieces.append("daemon.pid names pid %d, whose /proc/%d/cmdline is %r"
                          % (pid, pid, cmdline[:200]))
        if not pieces:  # defensive; `live` is built from these two facts
            pieces.append("daemon.lock is locked")
        evidence = "; ".join(pieces)
    else:
        verdict = measured["pidfile_verdict"]
        if isinstance(verdict, dict):
            verdict = "pid %(pid)d (alive=%(alive)s, zombie=%(zombie)s, cmdline=%(cmdline)r)" % verdict
        evidence = ("%s is not locked; %s %s"
                    % (lock, pidfile, verdict))
    measured.update(live=live, evidence=evidence)
    return measured


def probe_tickets(queue_dir, override=None):
    """Collect every ticket whose state is not terminal, from the queue's own files."""
    measured = {"probe": str(override) if override else "tickets/*/ticket.json",
                "tickets_dir": str(queue_dir / "tickets")}
    if override:
        present, rc, line = run_probe(override, "ticket", queue_dir)
        measured.update(blocker=present, evidence="probe %s exited %d: %r"
                                                  % (override, rc, line))
        return measured
    tickets = queue_dir / "tickets"
    unsettled, counts = [], {}
    if tickets.is_dir():
        for entry in sorted(tickets.iterdir()):
            path = entry / "ticket.json"
            if not os.path.lexists(path):
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                unsettled.append({"ticket": entry.name, "state": None,
                                  "problem": "ticket.json is unreadable (%s)"
                                             % type(exc).__name__})
                continue
            state = record.get("state") if isinstance(record, dict) else None
            if state not in TERMINAL:
                unsettled.append({"ticket": (record.get("ticket") if isinstance(record, dict)
                                             else None) or entry.name,
                                  "state": state,
                                  "hold": record.get("hold") if isinstance(record, dict) else None})
            else:
                counts[state] = counts.get(state, 0) + 1
    measured["terminal_states"] = counts
    measured["nonterminal"] = unsettled
    measured["blocker"] = bool(unsettled)
    if unsettled:
        named = "; ".join("%s: %s" % (row["ticket"],
                                      row.get("problem") or "state %r%s"
                                      % (row["state"],
                                         " hold %r" % row["hold"] if row.get("hold") else ""))
                          for row in unsettled[:8])
        more = "" if len(unsettled) <= 8 else " (and %d more)" % (len(unsettled) - 8)
        measured["evidence"] = "%d ticket(s) are not terminal: %s%s" % (len(unsettled), named, more)
    else:
        measured["evidence"] = ("every ticket is terminal (%s)"
                                % (counts or "no tickets at all"))
    return measured


# --- installed identity ------------------------------------------------------

def installed_sha256(target):
    """(sha256 or None, note) for the installed copy; never follows a symlink."""
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None, "absent"
    if not stat.S_ISREG(info.st_mode):
        return None, "not a regular file"
    raw, _ = read_regular(target)
    return digest(raw), "regular file, mode %04o" % stat.S_IMODE(info.st_mode)


def report_check(target, commit, expected):
    installed, note = installed_sha256(target)
    print("target:    %s (%s)" % (target, note))
    print("installed: sha256 %s" % (installed or "none"))
    print("reviewed:  commit %s %s sha256 %s" % (commit, SOURCE, expected))
    match = installed == expected
    print("result:    %s" % ("match" if match else "drift"))
    return 0 if match else 1


# --- install -----------------------------------------------------------------

def install(destination, raw, expected, commit, queue_dir, daemon, tickets):
    """Replace the installed copy atomically, holding the queue's own lifetime lock."""
    if not destination.is_dir():
        raise Refusal("the target directory %s does not exist (or is not a directory)" % destination)
    target = destination / NAME
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = os.open(INSTALL_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise Refusal("the installer lock %s is not a regular file"
                              % (destination / INSTALL_LOCK))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refusal("another installer holds %s; nothing was written"
                              % (destination / INSTALL_LOCK)) from None

            # The queue's own lifetime lock: no daemon may start while the bytes change,
            # and a daemon that appeared since the probe is a measured blocker.
            daemon_lock = None
            if os.path.lexists(queue_dir / "daemon.lock"):
                daemon_fd = os.open(queue_dir / "daemon.lock",
                                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    fcntl.flock(daemon_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(daemon_fd)
                    raise Refusal("the queue daemon took its lifetime lock while installing "
                                  "(%s is held); nothing was written"
                                  % (queue_dir / "daemon.lock")) from None
                daemon_lock = daemon_fd

            try:
                try:
                    info = os.lstat(NAME, dir_fd=directory)
                except FileNotFoundError:
                    info = None
                if info is not None and not stat.S_ISREG(info.st_mode):
                    raise Refusal("the installed %s is not a regular file (mode %04o); "
                                  "refusing to replace it"
                                  % (target, stat.S_IMODE(info.st_mode)))
                if info is None:
                    old, identity = None, None
                else:
                    old, identity = read_regular(NAME, dir_fd=directory)
                temporary = "." + NAME + "." + secrets.token_hex(12) + ".pending"
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        os.fchmod(stream.fileno(), MODE)
                        stream.flush()
                        os.fsync(stream.fileno())
                        os.fstat(stream.fileno())
                    if old is not None:
                        if read_regular(NAME, dir_fd=directory) != (old, identity):
                            raise Refusal("the installed %s changed before replacement; "
                                          "nothing was written" % NAME)
                    elif os.path.lexists(destination / NAME):
                        raise Refusal("something appeared at %s before replacement; "
                                      "nothing was written" % target)
                    os.replace(temporary, NAME, src_dir_fd=directory, dst_dir_fd=directory)
                    temporary = None
                    os.fsync(directory)
                finally:
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                confirmed, after = read_regular(NAME, dir_fd=directory)
                if digest(confirmed) != expected:
                    raise Refusal("the bytes just written to %s hash to %s, not the reviewed %s"
                                  % (target, digest(confirmed), expected))
            finally:
                if daemon_lock is not None:
                    os.close(daemon_lock)
        finally:
            os.close(lock)
    finally:
        os.close(directory)

    print(json.dumps(dict(target=str(target), source="%s:%s" % (commit, SOURCE),
                          old_sha256=digest(old) if old is not None else None,
                          sha256=expected, mode=MODE,
                          uid=after[4], gid=after[5],
                          daemon=daemon, tickets=tickets), sort_keys=True))
    return 0


def main(argv=None):
    parser = RefusingParser(
        description="Install ~/.AItelier/bin/gate_queue.py from a reviewed commit, "
                    "or check that it is installed.")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                        help="git repository holding scripts/gate_queue.py "
                             "(default: this checkout)")
    parser.add_argument("--commit", required=True,
                        help="full 40-hex commit whose scripts/gate_queue.py is the reviewed source")
    parser.add_argument("--sha256", required=True,
                        help="sha256 the reviewed bytes must hash to before anything is written")
    parser.add_argument("--check", action="store_true",
                        help="never write: exit 0 only when the installed bytes equal the "
                             "reviewed bytes, else 1 while printing both sha256 values")
    parser.add_argument("--queue-dir", default=os.environ.get("GATE_QUEUE_DIR")
                        or str(Path.home() / ".AItelier" / "gate-queue"),
                        help="the queue root the daemon and tickets live in "
                             "(default: $GATE_QUEUE_DIR or ~/.AItelier/gate-queue)")
    parser.add_argument("--daemon-probe", default=None,
                        help="command replacing the daemon probe; exit 0 means a daemon is live")
    parser.add_argument("--ticket-probe", default=None,
                        help="command replacing the ticket probe; exit 0 means a ticket is "
                             "not terminal")
    parser.add_argument("dest", nargs="?", default=str(Path.home() / ".AItelier" / "bin"),
                        help="target directory (default: ~/.AItelier/bin)")
    args = parser.parse_args(argv)

    commit = args.commit
    expected = args.sha256
    if DIGEST.fullmatch(expected) is None:
        raise Refusal("--sha256 must be a full lowercase 64-hex digest, got %r" % expected)
    repo = Path(args.repo).expanduser().resolve()
    destination = Path(args.dest).expanduser().resolve()
    queue_dir = Path(args.queue_dir).expanduser().resolve()
    target = destination / NAME

    raw = reviewed_bytes(repo, commit)
    actual = digest(raw)
    if actual != expected:
        raise Refusal("%s at commit %s hashes to %s, not the supplied --sha256 %s; "
                      "nothing was written" % (SOURCE, commit, actual, expected))

    if args.check:
        return report_check(target, commit, expected)

    daemon = probe_daemon(queue_dir, args.daemon_probe)
    if daemon["live"]:
        raise Refusal("the queue daemon is live: %s -- nothing was written" % daemon["evidence"])
    tickets = probe_tickets(queue_dir, args.ticket_probe)
    if tickets["blocker"]:
        raise Refusal("the gate queue is not settled: %s -- nothing was written"
                      % tickets["evidence"])

    return install(destination, raw, expected, commit, queue_dir, daemon, tickets)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print("Installer refused: %s" % error, file=sys.stderr)
        raise SystemExit(2)
    except OSError as error:
        print("Installer refused: %s (%s)" % (type(error).__name__, error), file=sys.stderr)
        raise SystemExit(2)
