"""Host-only resident DSH supervisor. The backend remains in Docker.

The existing enabled rows are the desired state. A short-lived process marker
lets the container observe host liveness without a Docker socket or another API.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import time

from core.dsh_driver import DshDrivers

# How long a child must stay up before its start counts as a successful one.
#
# `start()` returning means a process was spawned, not that it can run: a driver
# that cannot load its session dies in seconds, and every liveness signal this
# system already has is too slow to see that. `publish()` removes the process
# marker the moment the child is gone, and the container reads that marker
# through a 15 s freshness window, so a driver that dies and is respawned between
# two reads looks exactly like an idle one. Measured 2026-11-15: two drivers
# respawned for about three hours and the only trace was a repeated line in each
# driver's own runtime.log - the unit journal stayed empty.
#
# Five seconds: long enough that an ordinary start - the runtime loading itself
# and its MCP servers - is not mistaken for a death, and well under the 15 s
# floor of the retry backoff, so a fast death is decided before the retry it has
# to influence rather than after it. It is a bound on the DIAGNOSIS only: a child
# that outlives it is not thereby declared good, it is simply restarted the same
# way it always was.
STARTUP_WINDOW_SECONDS = 5.0

# How much of the driver's own log is read to find its last startup error, and
# how much of that error the record keeps. Every attempt appends to that log, so
# the log itself is unbounded; what is read from it, and what is stored, are not.
STARTUP_LOG_TAIL_BYTES = 4096
STARTUP_ERROR_LIMIT = 500

# The line that names an error, when the child wrote one. A Node crash prints
# `Error: ...` and then stack frames: the frames are not the error, and the LAST
# match in the tail is the newest attempt's error, because the log is appended to
# and the last attempt is the last thing in it.
_ERROR_LINE = re.compile(
    r"(?i)\berror\b|\bfatal\b|\btraceback\b|\bexception\b|\bfailed\b|\brefused\b|"
    r"\bcannot\b|\bENOENT\b|\bEACCES\b")

# A driver's startup error can quote its environment or a rejected header. The
# record sits on disk and the printed line stays in the journal, so the VALUES go
# and the label stays: "<redacted>" is the part an operator needs, the secret is
# the part that must not survive the process.
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{6,}")
_LABELLED_SECRET = re.compile(
    r"(?i)(\b(?:token|secret|password|passwd|passphrase|api[_-]?key|apikey|authorization)"
    r"\s*[=:]\s*[\"']?)[A-Za-z0-9._~+/:@=-]{6,}")


def publish(runtime, row):
    path = runtime.root / row["driver_id"] / "host-process.json"
    proc = runtime.processes.get(row["project_id"])
    if proc is None or proc.returncode is not None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": proc.pid, "session_id": row["session_id"],
                                     "seen_at": time.time()}))
    temporary.replace(path)


def startup_failure_path(runtime, driver_id):
    return runtime.root / driver_id / "startup-failures.json"


def last_startup_error(runtime, driver_id):
    """The first line of the driver's last startup error, from the tail of its log.

    The host already redirects both of the child's streams into the driver
    directory's runtime.log, so the diagnosis needs no second channel - and a
    child that dies before it can answer anything else still leaves it. Only the
    tail is read: the file grows with every attempt.

    Never raises for a log that is missing or unreadable. That is a diagnosis
    too, and a failed read must cost the record its detail, not the supervisor
    the loop.
    """
    path = runtime.root / driver_id / "runtime.log"
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - STARTUP_LOG_TAIL_BYTES))
            lines = log.read().decode("utf-8", "replace").splitlines()
    except OSError as exc:
        return f"runtime.log unreadable: {exc.strerror or exc}"
    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return "runtime.log has no output"
    for line in reversed(lines):
        if _ERROR_LINE.search(line):
            return line
    return lines[-1]


def _bounded(error):
    """One line, credential-free, short enough to keep the record small."""
    lines = error.strip().splitlines()
    error = lines[0] if lines else ""
    error = _BEARER.sub(r"\1<redacted>", error)
    error = _LABELLED_SECRET.sub(r"\1<redacted>", error)
    return error[:STARTUP_ERROR_LIMIT]


def record_startup_failure(runtime, row, attempts, error):
    """One small per-driver record of starts that keep failing, next to the marker.

    This is the record the liveness marker cannot be: the marker is removed as
    soon as the child is gone, so a driver that dies on every start is invisible
    to anything reading only that. Overwritten in place, never appended - what
    matters is how long the current streak is, when it began and last continued,
    and the first line of the last startup error.

    It is evidence, never a judgement: a record that declared the driver bad
    would still be declaring it after the fault was gone.
    """
    path = startup_failure_path(runtime, row["driver_id"])
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    first = stamp
    try:
        first = json.loads(path.read_text()).get("first_failed_at") or stamp
    except (OSError, ValueError, AttributeError):
        pass  # first failure of this streak, or a record that cannot be read
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "driver_id": row["driver_id"], "project_id": row["project_id"],
        "attempts": attempts, "first_failed_at": first, "last_failed_at": stamp,
        "last_error": _bounded(error)}))
    temporary.replace(path)


def clear_startup_failure(runtime, driver_id):
    """A start that stayed up ends the streak; a stale record must not outlive it."""
    startup_failure_path(runtime, driver_id).unlink(missing_ok=True)


async def supervise(runtime, stopped, *, interval=1, startup_window=STARTUP_WINDOW_SECONDS):
    """One controller per installation; systemd owns its entire process group.

    One project's failure must not stop the others. This loop is the only thing
    keeping every resident driver alive, so each row is handled inside its own
    exception boundary: `rows()` reads a multi-gigabyte SQLite database that can
    report "database is locked", and `publish()` writes a file that can fail on a
    full disk. Either used to propagate out of the loop, and the supervisor then
    exited and took every driver offline at once, not just the failing project.
    The failure record is part of that boundary for the same reason: a full disk
    or an unreadable log must cost one project's diagnosis, never the loop.

    A start counts as successful only once its child has stayed up for
    `startup_window`. A child that is gone before then is a FAILED start: it is
    counted, backed off from and reported, exactly like a `start()` that raised,
    because "a process exists" was never the question - "a driver can stay up"
    was, and a session that cannot be loaded answers no to it in seconds.
    """
    runtime.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime.root / "host-supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry_at = {}
        failures = {}
        # project -> the monotonic instant the child being watched was spawned.
        # A start is undecided until this entry has aged past `startup_window`;
        # dropping the entry is what decides it, once.
        spawned_at = {}
        try:
            while not stopped.is_set():
                try:
                    rows = runtime.rows()
                except Exception as exc:  # a locked database must not end supervision
                    print(f"DSH host: cannot read drivers: {exc}", flush=True)
                    rows = []
                for row in rows:
                    project = row["project_id"]
                    try:
                        if not row["enabled"]:
                            retry_at.pop(project, None)
                            failures.pop(project, None)
                            spawned_at.pop(project, None)
                            if project in runtime.processes:
                                await runtime.stop(project)
                        else:
                            now = time.monotonic()
                            alive = runtime.running(project)
                            spawned = spawned_at.get(project)
                            if spawned is not None and alive and now - spawned >= startup_window:
                                # It outlived the window: this start worked, so the
                                # counter and any record left by earlier starts go.
                                spawned_at.pop(project)
                                failures.pop(project, None)
                                clear_startup_failure(runtime, row["driver_id"])
                            elif spawned is not None and not alive:
                                spawned_at.pop(project)
                                if now - spawned < startup_window:
                                    failures[project] = failures.get(project, 0) + 1
                                    # One text goes to both sinks, and it passes
                                    # the same bounds: the journal is centralized
                                    # and kept, so a credential the driver echoed
                                    # must not reach it any more than the file.
                                    error = _bounded(last_startup_error(runtime, row["driver_id"]))
                                    record_startup_failure(runtime, row, failures[project], error)
                                    # One line per failed start, in the unit
                                    # journal, where somebody who is not tailing
                                    # runtime.log will see it. The healthy path
                                    # prints nothing at all.
                                    print(f"DSH host {project}: {row['driver_id']} died "
                                          f"{now - spawned:.1f}s after start, failed start "
                                          f"{failures[project]} in a row: {error}", flush=True)
                                # A child that lived past the window and left later is
                                # not a failed start; the row is simply restarted below.
                            if not alive and project not in spawned_at and now >= retry_at.get(project, 0):
                                # Back off per project: a driver that cannot start at
                                # all must not be respawned every interval forever.
                                delay = min(300, 15 * 2 ** failures.get(project, 0))
                                retry_at[project] = time.monotonic() + delay
                                try:
                                    await runtime.start(project, actor="dsh-host-recovery")
                                except Exception as exc:
                                    failures[project] = failures.get(project, 0) + 1
                                    error = _bounded(str(exc))
                                    record_startup_failure(runtime, row, failures[project], error)
                                    print(f"DSH host {project}: {error}", flush=True)
                                else:
                                    if runtime.processes.get(project) is not None:
                                        spawned_at[project] = time.monotonic()
                                    # No child to watch means this runtime did not
                                    # spawn one, so there is nothing to judge here.
                        publish(runtime, row)
                    except Exception as exc:  # one project's failure stays local
                        print(f"DSH host {project}: {exc}", flush=True)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await runtime.close()
            for row in runtime.rows():
                publish(runtime, row)


async def main():
    from core.datadir import aitelier_home, db_path
    from core.drivers import registry_for
    from core.state_database import StateDatabase
    db = StateDatabase(db_path())
    runtime = DshDrivers(db, registry_for(db), aitelier_home() / "dsh-drivers", host_controller=True)
    if json.loads((runtime.root / "runtime.json").read_text()).get("mode") != "host":
        raise ValueError("installation is not configured for the host runtime")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await supervise(runtime, stopped)


if __name__ == "__main__":
    asyncio.run(main())
