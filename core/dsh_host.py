"""Host-only resident DSH supervisor. The backend remains in Docker.

The existing enabled rows are the desired state. A short-lived process marker
lets the container observe host liveness without a Docker socket or another API.
"""
from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path

from core.dsh_driver import DshDrivers, fallback_restart, media_root, role_for_project

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
# to influence rather than after it.
STARTUP_WINDOW_SECONDS = 5.0

# Whether a start WORKED is not a question about elapsed time. A driver has served
# once its row reports one of the phases only the runtime itself writes, and a real
# boot reaches its first turn in about 25 s - so "alive for 5 s" was never evidence,
# and the same child that dies 6 s after every spawn was a failed start with the
# window at 12 s and nothing at all at 5 s (measured 2026-11-15). The ceiling below
# bounds the QUESTION rather than posing a second window: a spawn that never served
# and is older than it is no longer a boot problem, and its death is the ordinary
# restart it looks like.
STARTUP_DECISION_SECONDS = 120.0

# The row phases only the runtime writes, when it reports in over /activity: a row
# showing one of them has served. `starting` is what `start()` writes and `failed`
# is a death, so neither is evidence of a working driver.
SERVING_PHASES = frozenset({"running", "waiting", "dormant"})

# How much of the driver's own log is read to find its last startup error, and
# how much of that error the record keeps. Every attempt appends to that log, so
# the log itself is unbounded; what is read from it, and what is stored, are not.
STARTUP_LOG_TAIL_BYTES = 4096
STARTUP_ERROR_LIMIT = 500

# A SERVING driver's exit record keeps what its own log last said. How much the
# child's last words may cost it is bounded twice over: forty lines, because that is
# a failure's worth of text and the newest line names the cause; read from a larger
# tail than a startup failure is, because a serving driver's log is a whole session's
# chatter and its last line can be a long way from the top; and the whole record text
# at EXIT_RECORD_LIMIT characters, cut from the OLD end. Characters bind first:
# forty lines that fit beat forty that do not.
EXIT_LOG_TAIL_BYTES = 16384
EXIT_LOG_LINES = 40
EXIT_RECORD_LIMIT = 1200

# The session log is zstd-framed JSONL, about one frame per event, and a busy
# director's grew to 178 MB (measured 2026-11-17). The exit record reads a tail
# window of frames and decompresses only what is in it, so the cost of the record
# does not grow with the session.
SESSION_LOG_TAIL_BYTES = 65536
_ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"

# How much of the session's last event line the record keeps. One event carries a
# whole payload and the record needs its identity, not its body: `seq` and `type`
# are the first keys DSH writes, so the head of the line is the part that says how
# far the session got.
SESSION_EVENT_LIMIT = 300

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


# Step 24.2b (design-steward 7.8.12a "一个写者一个子目录" 清理): the media library keeps one
# subdirectory per writer, and a director's is `<media root>/d-<driver_id>`. A running driver
# cleans its own every day; a director that is NOT running (stopped, archived, deleted) has no
# host of its own, so this supervisor runs the same cleanup on it, through the plugin's own CLI
# (integrations/dsh-director/media-gc.mjs: 30 days, then 1 GiB oldest first) - once a day per
# subdirectory, only while the project has no child, and before that project's next spawn, so
# there is still one writer at a time. A `d-<driver_id>` whose driver row is gone is removed
# whole. The steward's subdirectory (STEWARD_MEMORY_USER) is never touched here: the steward
# row is skipped and the sweep only looks at `d-` names.
MEDIA_GC_INTERVAL_SECONDS = 24 * 3600
# A cleanup is a directory listing and some unlinks; one that hangs must not hold every other
# project's supervision, so it is killed (and the spawn goes ahead) after this long.
MEDIA_GC_TIMEOUT_SECONDS = 120
MEDIA_GC_COMMAND = ("node", str(Path(__file__).resolve().parents[1]
                                / "integrations" / "dsh-director" / "media-gc.mjs"))
# The media library's own userId rule (lib/media.mjs), behind the `d-` of a director.
_DIRECTOR_SUBDIR = re.compile(r"^d-[A-Za-z0-9_.-]+$")
_ORPHAN_SWEEP = "*orphans*"  # the sweep's own entry in the once-a-day ledger


def director_media_dir(media, row):
    """`<media>/d-<driver_id>` for a director's row; None for the steward's (its library is
    `<media>/<STEWARD_MEMORY_USER>`, cleaned by its own host or the panel's clear only)."""
    if role_for_project(row["project_id"]) != "director":
        return None
    name = "d-" + row["driver_id"]
    return Path(media) / name if _DIRECTOR_SUBDIR.match(name) else None


async def run_media_gc(directory):
    """`node media-gc.mjs <directory>`, waited for. Raises with the CLI's own words when it
    fails or hangs; a hung one is killed and reaped first, so no second writer outlives it."""
    proc = await asyncio.create_subprocess_exec(
        *MEDIA_GC_COMMAND, str(directory),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=MEDIA_GC_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"media cleanup timed out after {MEDIA_GC_TIMEOUT_SECONDS}s") from None
    if proc.returncode:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"media cleanup exited {proc.returncode}: {tail[-1] if tail else ''}")


async def media_gc(runtime, row, media, ledger, now):
    """The day's cleanup of one stopped director's subdirectory, if it is due. Never for a
    project with a live child; a failure is logged and counts as the day's run, so it neither
    stops the loop nor delays the spawn it precedes."""
    if media is None or runtime.running(row["project_id"]):
        return
    directory = director_media_dir(media, row)
    if directory is None:
        return
    last = ledger.get(directory.name)
    if last is not None and now - last < MEDIA_GC_INTERVAL_SECONDS:
        return
    ledger[directory.name] = now
    try:
        if not directory.is_dir():
            return  # nothing was ever stored: nothing to clean, and nothing is created
        await run_media_gc(directory)
    except Exception as exc:
        print(f"DSH host {row['project_id']}: {directory.name}: {_bounded(str(exc))}", flush=True)


def sweep_orphan_media(runtime, media, ledger, now):
    """Once a day: remove every `d-<driver_id>` subdirectory whose driver row is gone. The rows
    are read here, fresh; when they cannot be read nothing is removed (an unreadable database
    is not an empty one)."""
    if media is None:
        return
    last = ledger.get(_ORPHAN_SWEEP)
    if last is not None and now - last < MEDIA_GC_INTERVAL_SECONDS:
        return
    ledger[_ORPHAN_SWEEP] = now
    try:
        known = {"d-" + row["driver_id"] for row in runtime.rows()}
        entries = list(os.scandir(media))
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"DSH host: media sweep skipped: {_bounded(str(exc))}", flush=True)
        return
    for entry in entries:
        if (not _DIRECTOR_SUBDIR.match(entry.name) or entry.name in known
                or not entry.is_dir(follow_symlinks=False)):
            continue
        try:
            shutil.rmtree(entry.path)
            print(f"DSH host: removed {entry.name}: its driver row is gone", flush=True)
        except OSError as exc:
            print(f"DSH host: cannot remove {entry.name}: {_bounded(str(exc))}", flush=True)


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


def _log_tail(runtime, driver_id, limit=STARTUP_LOG_TAIL_BYTES):
    """The last `limit` bytes of the driver's own log, decoded.

    Raises OSError when it cannot be read: the two readers below name that in their
    own words, because a start that never served and a driver that had been serving
    for hours are different diagnoses. Only the tail is read either way - the file
    grows with every attempt and every turn.
    """
    path = runtime.root / driver_id / "runtime.log"
    with path.open("rb") as log:
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - limit))
        return log.read().decode("utf-8", "replace")


def last_startup_error(runtime, driver_id):
    """The first line of the driver's last startup error, from the tail of its log.

    The host already redirects both of the child's streams into the driver
    directory's runtime.log, so the diagnosis needs no second channel - and a
    child that dies before it can answer anything else still leaves it.

    Never raises for a log that is missing or unreadable. That is a diagnosis
    too, and a failed read must cost the record its detail, not the supervisor
    the loop.
    """
    try:
        text = _log_tail(runtime, driver_id)
    except OSError as exc:
        return f"runtime.log unreadable: {exc.strerror or exc}"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "runtime.log has no output"
    for line in reversed(lines):
        if _ERROR_LINE.search(line):
            return line
    return lines[-1]


def _written_before(runtime, driver_id, started_after):
    """The sentence naming a log last written BEFORE this process started, or None.

    `runtime.log` is appended to, never truncated, so it outlives the child that
    wrote it: the tail of a driver that wrote nothing is its PREDECESSOR's text, and
    quoting that as this exit's cause reads as a cause this process never had
    (measured 2026-11-26: 0 bytes written in 116957 s, and the record quoted a
    process two generations back). The file's mtime is the evidence that separates
    the two, and the gap is spelled out so the reader is not left subtracting two
    timestamps.

    The number is the gap between the log's last write and THIS PROCESS'S START -
    neither "now" nor "how long ago at exit". Measured from `started_after` it is
    exact (a wall-clock read here would be a race), and the status line printed
    above the cause already carries `after <uptime>s uptime`, so the instant of the
    predecessor's last write is those two numbers together; printing the gap to the
    exit instead would mean deriving it from the uptime here, and a wording that
    claimed "ago" for a gap measured before the start would understate it by the
    whole uptime - always in the same direction.
    """
    # The read above already succeeded, so a stat that does not is no evidence of
    # staleness: it must not invent any.
    try:
        mtime = os.stat(runtime.root / driver_id / "runtime.log").st_mtime
    except OSError:
        return None
    if mtime >= started_after:
        return None
    gap = max(0.0, started_after - mtime)
    before = f"{gap / 3600:.1f}h" if gap >= 3600 else f"{max(1, round(gap / 60))}m"
    return _bounded(f"unchanged by this process (last written {before} before it started)")


def runtime_log_tail(runtime, driver_id, *, started_after=None):
    """The newest lines of a serving driver's own log, or the reason there are none.

    The same file `last_startup_error` reads - the host redirects both of the
    child's streams into it - so the exit record needs no second channel. It is a
    DIFFERENT question though: a startup reader wants the one line that names why a
    boot failed, while a driver that had been serving for hours died with its whole
    session in there, and the last thing it said is the only evidence left.

    A driver that wrote nothing is a diagnosis as much as one that wrote a
    traceback: "missing" and "no output" are named here rather than reaching the
    record as an empty field somebody has to interpret.

    `started_after` (a wall-clock instant) is passed by the exit record, which knows
    when this process started: a log whose mtime is older than that holds somebody
    else's text, and naming the fact replaces quoting it. Left None, the answers are
    exactly the ones this reader always gave - the "has no output" rows above stay
    theirs, since an empty file is a fact about this process's own writing.
    """
    try:
        lines = [line.strip() for line in
                 _log_tail(runtime, driver_id, EXIT_LOG_TAIL_BYTES).splitlines()]
    except FileNotFoundError:
        return "runtime.log missing"
    except OSError as exc:
        return f"runtime.log unreadable: {exc.strerror or exc}"
    lines = [line for line in lines if line]
    if not lines:
        return "runtime.log has no output"
    if started_after is not None:
        stale = _written_before(runtime, driver_id, started_after)
        if stale is not None:
            return stale
    # Same bounds as the startup text: one redacted, capped line each.
    return " | ".join(_bounded(line) for line in lines[-EXIT_LOG_LINES:])


def _session_log(runtime, driver_id, session_id):
    """This driver's and this session's zstd log, or None.

    The sessions root holds one directory per workspace and one per session id, so
    the session the ROW names is looked for - never merely the newest file under
    there, which can belong to a session this driver has already left.
    """
    root = runtime.root / driver_id / "home" / "sessions"
    try:
        workspaces = list(root.glob("*"))
    except OSError:
        return None
    for workspace in workspaces:
        candidate = workspace / str(session_id) / "session.v4.jsonl.zstd"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _last_zstd_line(window):
    """The last complete event line in `window`, a tail of zstd frames, or None.

    Frames are independent, so decompression can start at any frame header: the
    window's own first frame is usually cut, and the magic scan finds the first
    boundary after it. A candidate that is not a boundary decodes to nothing that
    parses as an event and the next one is tried; a frame the stopped runtime was
    still appending ends the read, and what decoded before it is what there is.

    An event is a JSON object with an integer `seq`, which is the same test DSH's
    own reader applies - a fragment of a line is not an event and must not be
    reported as the last one.
    """
    import zstandard
    starts, at = [], window.find(_ZSTD_FRAME_MAGIC)
    while at != -1:
        starts.append(at)
        at = window.find(_ZSTD_FRAME_MAGIC, at + 1)
    for start in starts:
        raw = bytearray()
        reader = zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(window[start:]), read_across_frames=True)
        try:
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                raw += chunk
        except zstandard.ZstdError:
            pass
        finally:
            reader.close()
        for line in reversed(bytes(raw).decode("utf-8", "replace").splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("seq"), int):
                return line
    return None


def last_session_event(runtime, row):
    """The last event line this driver's session log holds, bounded and redacted.

    The record of a crash needs both halves of what the process left behind: what
    its stderr said, and how far its session got. A session that ends on a
    `job/registered` with no result, or a `step/end` with no `turn/end`, is the
    difference between "the process died mid-turn" and "the process died idle".

    Never raises. A session log that is absent, unreadable or empty is named the
    same way a missing runtime.log is.
    """
    session_id = row.get("session_id")
    if not session_id:
        # A row without a session id is not a session log that is missing: saying so
        # is the difference between "this driver never got a session" and "the file
        # went away", and the record reads the absence rather than raising on it.
        return "session id missing from the driver row"
    path = _session_log(runtime, row["driver_id"], session_id)
    if path is None:
        return "session log missing"
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - SESSION_LOG_TAIL_BYTES))
            window = log.read()
    except OSError as exc:
        return f"session log unreadable: {exc.strerror or exc}"
    try:
        line = _last_zstd_line(window)
    except ImportError:  # zstandard is a declared dependency; a diagnosis if it is gone
        return "session log unreadable: zstandard is not installed"
    if line is None:
        return "session log has no events"
    return f"session {session_id} last event: {_bounded(line)[:SESSION_EVENT_LIMIT]}"


def _capped(text, limit):
    """`text` at most `limit` characters long, cut from the START with a marker.

    The END is what a diagnosis needs - the newest log line is the one that names
    the cause - so the oldest text is what goes. Nothing is cut when it fits, and a
    limit of zero is an empty field rather than an exception.
    """
    if limit <= 0:
        return ""
    return text if len(text) <= limit else "… " + text[len(text) - limit + 2:]


def exit_cause(runtime, row, *, started_at=None):
    """What a serving driver left behind when it died, as the exit record keeps it.

    Three facts and one bound. The last lines of the driver's own log, the last
    event of its session log, and - when either is absent - the absence named.
    The whole text is capped at EXIT_RECORD_LIMIT characters. The LOG gives way
    first (it is the only part that is unbounded by construction) and it is cut
    from the OLD end, because the newest line is the one that names the cause;
    the labels and the session event are what a reader needs to interpret the rest.

    `started_at` is the wall-clock instant this driver's process started, when the
    caller knows it: it goes to `runtime_log_tail` as the boundary between lines
    this process wrote and lines a predecessor left, so an appended log cannot be
    quoted as this exit's cause. None keeps the unconditional tail.

    Only called for an exit this supervisor watched, so it is not on a hot path;
    each field is read once, from the files the driver itself left.
    """
    project = _bounded(str(row["project_id"]))
    driver_id = _bounded(str(row["driver_id"]))
    head = f"DSH host {project}: {driver_id} exit cause: "
    room = EXIT_RECORD_LIMIT - len(head)
    session = last_session_event(runtime, row)
    # The readers open by the id the ROW holds, never by the bounded text that goes
    # into the record: a name too long to print is still a name a file is found by.
    log = runtime_log_tail(runtime, row["driver_id"], started_after=started_at)
    body = f"runtime.log: {log}; {session}"
    if len(body) > room:
        keep = room - len(f"runtime.log: ; {session}")
        body = (f"runtime.log: {_capped(log, keep)}; {session}" if keep > 4
                else _capped(body, room))
    return head + body


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


# What the supervisor writes into `last_error`, and the only thing it will ever
# take back out: `last_error` is a shared field, so the shape it can recognise as
# its own is what lets a recovery clear its stamp without erasing somebody else's
# diagnosis.
START_FAILURE_PREFIX = "DSH: failed start "

# The runtime's own diagnosis of the same death, written by `DshDrivers._observe`
# from a monitor thread when the child it spawned exits. It can land after the
# supervisor's stamp - both describe one death, and the row keeps the last writer.
# Recognising it is what lets the streak be put back without touching a text that
# somebody else wrote.
RUNTIME_EXIT_NOTICE = "DSH exited with code "


def _start_failure_text(attempts, error):
    """One line: how long the streak is, then the record's own bounded error."""
    return f"{START_FAILURE_PREFIX}{attempts} in a row: {_bounded(error)}"


def stamp_start_failure(runtime, row, attempts, error):
    """Put the failed start on the driver row, where the owner's page reads it.

    The record beside the marker is evidence for whoever opens the driver
    directory; the ROW is the only thing the owner's page renders - `phase`
    (with `failed` mapped to a warning label) and `last_error` under it. Without
    this, a driver that dies on every start looked merely stopped there, however
    many times the supervisor had counted it.

    Same text bounds as the record, because both are read by people and one of
    them can echo a credential.
    """
    runtime.update(row["project_id"], phase="failed",
                   last_error=_start_failure_text(attempts, error))


# Deliberately NO clear-on-recovery. A recovery needs no writer: `start()` already
# stamped `phase='starting', last_error=''` for the spawn being judged, so by the
# time a streak can end, the supervisor's own text is gone. Measured 2026-11-15 on
# this candidate: with such a helper present and then disabled, all 15 tests of
# tests/unit/test_dsh_host.py still passed - nothing depended on it, and the only
# thing it could still do is erase a diagnosis somebody else wrote.


def _overwritten_by_runtime_exit(text):
    """Did the runtime's monitor write its exit notice over the streak?"""
    return str(text or "").startswith(RUNTIME_EXIT_NOTICE)


def record_exit(runtime, row, watched, uptime, *, supervisor_stopped):
    """The record a driver's exit leaves in the unit journal, and the cause with it.

    A driver that had been SERVING used to leave no trace at all when it went:
    `DshDrivers._observe` writes `DSH exited with code N` into `last_error`, and
    the next `start()` stamps `phase='starting', last_error=''` over it, so a
    crash, an operator restart and a watchdog kill read identically afterwards
    and the in-flight turn's children are the only evidence - gone with them.

    The journal is the sink that outlives the process (the unit's StandardOutput
    is journald), and it used to be given an exit code and an uptime: measured
    2026-11-16/17, two runtimes died after 7.1 h and 7.8 h with code 1 and one
    line each, and neither death was diagnosable afterwards. So the record now
    carries the second half of the same fact - what the driver itself left behind:
    the newest lines of its own `runtime.log` and the last event of its session log
    (`exit_cause`), from the same files the startup reader already uses, redacted
    and bounded the same way, and with an absent log NAMED rather than left blank.
    The log is appended to, so the record also carries the start instant that makes
    "these are this process's lines" a fact the reader can check: a log last written
    before the child existed is named as unchanged, never quoted.

    Two physical lines: the status, then the cause. The status keeps its shape -
    the driver id, the pid, the code or - for a negative returncode - the signal,
    the uptime and whether this supervisor is why the child is gone - because that
    is what a reader greps for, and the cause is what was missing.

    Only an exit this loop watched is recorded: a spawn that never reached a
    serving phase has its own line (the failed-start one, which is untouched by
    this) and a second line for one death would read as two events. Nothing is
    claimed that was not seen - no returncode, no line, and no signal invented.
    A cause that cannot be read costs the record its detail, never its status line
    or the loop: every reader here answers an absence instead of raising.
    """
    proc = watched["proc"]
    if not watched["served"] or proc.returncode is None:
        return False
    status = (f"signal {-proc.returncode}" if proc.returncode < 0
              else f"exit code {proc.returncode}")
    print(f"DSH host {_bounded(str(row['project_id']))}: "
          f"{_bounded(str(row['driver_id']))} pid {proc.pid} exited with {status} "
          f"after {uptime:.1f}s uptime, supervisor_stopped="
          f"{'true' if supervisor_stopped else 'false'}", flush=True)
    try:
        # `uptime` is MONOTONIC and the log's mtime is WALL-clock, so the boundary
        # between this process's own lines and a predecessor's is derived here, at
        # the one place that holds both.
        cause = exit_cause(runtime, row, started_at=time.time() - uptime)
    except Exception as exc:  # a diagnosis must never be why the record is lost
        cause = (f"DSH host {_bounded(str(row['project_id']))}: "
                 f"{_bounded(str(row['driver_id']))} exit cause unavailable: {_bounded(str(exc))}")
    print(cause, flush=True)
    return True


async def supervise(runtime, stopped, *, interval=1, startup_window=STARTUP_WINDOW_SECONDS,
                    media=None):
    """One controller per installation; systemd owns its entire process group.

    One project's failure must not stop the others. This loop is the only thing
    keeping every resident driver alive, so each row is handled inside its own
    exception boundary: `rows()` reads a multi-gigabyte SQLite database that can
    report "database is locked", and `publish()` writes a file that can fail on a
    full disk. Either used to propagate out of the loop, and the supervisor then
    exited and took every driver offline at once, not just the failing project.
    The failure record is part of that boundary for the same reason: a full disk
    or an unreadable log must cost one project's diagnosis, never the loop.

    A FAILED start is a child that dies without ever having SERVED: the runtime
    writes a row's serving phase only when it reports in over /activity, so that
    phase - never the child's age - is what says whether a boot worked. Such a
    death is counted, backed off from and reported, exactly like a `start()` that
    raised, because "a process exists" was never the question: a session that
    cannot be loaded answers no to it whether it dies in 2 s or in 6 s.
    `startup_window` keeps its own, narrower job - a child alive past it has
    survived its start, which ends a streak - and `STARTUP_DECISION_SECONDS`
    bounds how long a spawn stays undecided when nothing ever reports in.

    A SERVING driver's exit is a different event with its own record: one line in
    the journal (`record_exit`), written for the child this loop spawned, naming
    the driver, the pid, the exit code or signal, the uptime, whether this
    supervisor asked for the stop - and, on the line under it, the cause the
    driver itself left (`exit_cause`: the tail of its runtime.log and the last
    event of its session log, redacted and bounded). It changes nothing about the
    failed-start machinery above - a serving death still counts nothing, writes no
    record and stamps no row text.

    `media` is the media library's root (step 24.2b): when given, a director with no
    child gets its subdirectory cleaned once a day, before its next spawn, and the
    subdirectories of drivers whose row is gone are removed (`media_gc`,
    `sweep_orphan_media`). None - every caller but `main()` - leaves the media alone.
    """
    runtime.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime.root / "host-supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry_at = {}
        failures = {}
        # project -> the bounded error line of the streak's last failed start, so
        # the row can be re-stamped with the same words it was stamped with.
        last_errors = {}
        # project -> the monotonic instant the child being watched was spawned.
        # The entry is dropped once that start is decided: it served, it died, or
        # it outlived `STARTUP_DECISION_SECONDS` without ever reporting in.
        spawned_at = {}
        # project -> this spawn has served: the runtime reported one of the serving
        # phases for it, which is the only evidence that a start worked.
        served = set()
        # project -> {"proc": child, "started": monotonic instant, "served": bool}
        # for each child THIS loop spawned whose exit it has not yet recorded.
        # Deliberately separate from `spawned_at`/`served` above: those answer
        # whether a START worked and are dropped the moment that is decided, so
        # they stop watching the child entirely - and the death worth a record is
        # the one after that point, of a driver that had been serving for minutes.
        children = {}
        # subdirectory name (or the orphan sweep's key) -> the monotonic instant of
        # its last media cleanup: at most one a day each.
        media_ledger = {}
        try:
            while not stopped.is_set():
                try:
                    rows = runtime.rows()
                except Exception as exc:  # a locked database must not end supervision
                    print(f"DSH host: cannot read drivers: {exc}", flush=True)
                    rows = []
                try:
                    sweep_orphan_media(runtime, media, media_ledger, time.monotonic())
                except Exception as exc:  # the media must never cost supervision
                    print(f"DSH host: media sweep failed: {_bounded(str(exc))}", flush=True)
                for row in rows:
                    project = row["project_id"]
                    try:
                        watched = children.get(project)
                        if watched is not None and watched["proc"].returncode is not None:
                            # The child this loop spawned is gone, whether or not
                            # its death was a start problem. Recorded first, while
                            # the state that describes it is still here, and
                            # exactly once: the entry goes with the record.
                            # Nothing below is changed by it.
                            children.pop(project, None)
                            record_exit(runtime, row, watched,
                                        time.monotonic() - watched["started"],
                                        supervisor_stopped=False)
                        if not row["enabled"]:
                            retry_at.pop(project, None)
                            failures.pop(project, None)
                            last_errors.pop(project, None)
                            spawned_at.pop(project, None)
                            served.discard(project)
                            if project in runtime.processes:
                                await runtime.stop(project)
                                # The exit above is this supervisor's own decision,
                                # not a crash: the line says so, and a driver
                                # stopped here is not a failed start either.
                                child = children.pop(project, None)
                                if child is not None:
                                    record_exit(runtime, row, child,
                                                time.monotonic() - child["started"],
                                                supervisor_stopped=True)
                            # A stopped director: its own host cleans nothing now.
                            await media_gc(runtime, row, media, media_ledger, time.monotonic())
                        else:
                            # Step 24.1a (design-steward 7.8.10a): a due fallback-restart
                            # marker is executed here, by the spawner. The stop is this
                            # supervisor's own (not a crash, not a failed start); the row
                            # stays enabled, so the recovery start below brings the
                            # driver back when its backoff allows.
                            if await fallback_restart(runtime, row, then_start=False):
                                spawned_at.pop(project, None)
                                served.discard(project)
                                child = children.pop(project, None)
                                if child is not None:
                                    record_exit(runtime, row, child,
                                                time.monotonic() - child["started"],
                                                supervisor_stopped=True)
                                publish(runtime, row)
                                continue
                            now = time.monotonic()
                            watching = children.get(project)
                            if watching is not None and row.get("phase") in SERVING_PHASES:
                                # The runtime reported in over /activity for THIS
                                # child, so whatever ends it from here is the exit
                                # of a driver that had been serving.
                                watching["served"] = True
                            alive = runtime.running(project)
                            spawned = spawned_at.get(project)
                            if spawned is not None and row.get("phase") in SERVING_PHASES:
                                # The runtime reported in over /activity: this
                                # spawn served, whatever it does next.
                                served.add(project)
                            if spawned is not None and alive and now - spawned >= startup_window:
                                # It outlived the window: this start worked, so the
                                # counter and any record left by earlier starts go.
                                failures.pop(project, None)
                                last_errors.pop(project, None)
                                # The row needs no clearing here: this spawn's own
                                # `start()` wrote `phase='starting', last_error=''`
                                # before the streak could end (see the note above).
                                clear_startup_failure(runtime, row["driver_id"])
                                if project in served or now - spawned >= STARTUP_DECISION_SECONDS:
                                    # Served, or too old to be a start problem any
                                    # more: nothing is left to judge here.
                                    spawned_at.pop(project)
                                    served.discard(project)
                            elif spawned is not None and not alive:
                                spawned_at.pop(project)
                                did_serve = project in served
                                served.discard(project)
                                if did_serve:
                                    # A session that ran and was killed is an
                                    # ordinary restart: the boot question is not why
                                    # it is gone, and its delay must not grow.
                                    failures.pop(project, None)
                                    last_errors.pop(project, None)
                                elif now - spawned < STARTUP_DECISION_SECONDS:
                                    failures[project] = failures.get(project, 0) + 1
                                    # One text goes to both sinks, and it passes
                                    # the same bounds: the journal is centralized
                                    # and kept, so a credential the driver echoed
                                    # must not reach it any more than the file.
                                    error = _bounded(last_startup_error(runtime, row["driver_id"]))
                                    last_errors[project] = error
                                    record_startup_failure(runtime, row, failures[project], error)
                                    # One line per failed start, in the unit
                                    # journal, where somebody who is not tailing
                                    # runtime.log will see it. The healthy path
                                    # prints nothing at all.
                                    print(f"DSH host {project}: {row['driver_id']} died "
                                          f"{now - spawned:.1f}s after start, failed start "
                                          f"{failures[project]} in a row: {error}", flush=True)
                                    try:
                                        stamp_start_failure(runtime, row, failures[project], error)
                                    except Exception as exc:
                                        # A row this cannot be written must cost the
                                        # project's visibility, never its retry, its
                                        # marker or the rest of the loop.
                                        print(f"DSH host {project}: cannot stamp the failure "
                                              f"on its row: {exc}", flush=True)
                                # Otherwise the death is not a boot problem: no
                                # record, no line, just the ordinary retry below.
                            elif spawned is not None and now - spawned >= STARTUP_DECISION_SECONDS:
                                # Alive far past any boot without ever reporting in:
                                # a real defect, but not this spawn's start outcome.
                                spawned_at.pop(project)
                                served.discard(project)
                            if (failures.get(project) and spawned is None and not alive
                                    and _overwritten_by_runtime_exit(row["last_error"])):
                                # `DshDrivers._observe` diagnoses the same death
                                # from a monitor thread, and its write can land
                                # after the stamp above. The row is what the
                                # owner's page renders, so the streak is put back -
                                # and only over that notice, never over a text
                                # somebody else wrote.
                                try:
                                    stamp_start_failure(runtime, row, failures[project],
                                                        last_errors[project])
                                except Exception as exc:
                                    print(f"DSH host {project}: cannot stamp the failure "
                                          f"on its row: {exc}", flush=True)
                            if not alive and project not in spawned_at and now >= retry_at.get(project, 0):
                                # Back off per project: a driver that cannot start at
                                # all must not be respawned every interval forever.
                                delay = min(300, 15 * 2 ** failures.get(project, 0))
                                retry_at[project] = time.monotonic() + delay
                                # The day's cleanup of a director's media goes FIRST and
                                # is waited for, so the child never shares its library
                                # with it; a failed cleanup is logged and the spawn goes on.
                                await media_gc(runtime, row, media, media_ledger, time.monotonic())
                                try:
                                    await runtime.start(project, actor="dsh-host-recovery")
                                except Exception as exc:
                                    failures[project] = failures.get(project, 0) + 1
                                    error = _bounded(str(exc))
                                    last_errors[project] = error
                                    record_startup_failure(runtime, row, failures[project], error)
                                    print(f"DSH host {project}: {error}", flush=True)
                                    try:
                                        stamp_start_failure(runtime, row, failures[project], error)
                                    except Exception as stamp_error:
                                        print(f"DSH host {project}: cannot stamp the failure "
                                              f"on its row: {stamp_error}", flush=True)
                                else:
                                    if runtime.processes.get(project) is not None:
                                        spawned_at[project] = time.monotonic()
                                        children[project] = {
                                            "proc": runtime.processes[project],
                                            "started": spawned_at[project],
                                            "served": False}
                                        served.discard(project)  # a new spawn, nothing served yet
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
            # Every child still ALIVE here is one this supervisor is about to
            # stop: `close()` asks each of them, so their exits are this loop's
            # decision and the line says so. One that died on its own just before
            # the shutdown is not - it gets the exit it actually was.
            stopping = {project for project, child in children.items()
                        if child["proc"].returncode is None}
            await runtime.close()
            for row in runtime.rows():
                publish(runtime, row)
                child = children.pop(row["project_id"], None)
                if child is not None:
                    record_exit(runtime, row, child, time.monotonic() - child["started"],
                                supervisor_stopped=row["project_id"] in stopping)


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
    await supervise(runtime, stopped, media=Path(media_root()))


if __name__ == "__main__":
    asyncio.run(main())
