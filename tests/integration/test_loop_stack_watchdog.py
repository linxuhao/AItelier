"""The backend's own witness for a loop parked in a syscall.

THE MEASUREMENT this answers (`iss-0f0550d7523741f2`): the backend stopped
serving its event loop for 21.52 s and 17.99 s on 2026-11-16 (39 samples >= 0.5 s
in the day's retained log) and recorded nothing about what it was doing. The
existing lag monitor (`core/event_loop_lag.py`) can answer only AFTER the loop
turns again - it runs ON that loop - and its `attribution` stayed `unknown` for
those samples. py-spy from outside is not available in this runtime: the
container's seccomp profile refuses ptrace. The retained log was silent across
EVERY thread for the biggest stalls, which is why the witness has to be in the
process and has to cover ALL threads.

WHAT IS ASSERTED HERE, against the real watchdog and real files:

  * with a thread parked in a real, GIL-releasing syscall (`os.read` on a pipe
    with no writer), the watchdog writes a dump containing EVERY live thread's
    stack, including that thread's own frame (module + function) under its own
    thread id, with a header naming the measured silence; the SAME stall with
    nothing armed writes nothing, so the green is not vacuous;
  * at >=100 live threads — where faulthandler's own dump stops at 100 blocks
    and leaves the OLDEST threads out, the watched loop thread among them
    (`iss-f1667dc30c2d41ee`: a 0.508 s stall wrote 100 blocks, no
    `Current thread` block, no base_events frame) — the file still contains the
    watched thread's own chain, written from `sys._current_frames()` BEFORE
    that dump, and the dump is labelled with the live thread count so a reader
    can tell a complete section from a capped one;
  * a healthy loop over a run longer than 3x the budget writes no file at all
    (enabling the witness cannot grow data volume when nothing is wrong);
  * one incident is bounded: at most `max_dumps_per_incident` dumps in one file
    and no growth past `max_incident_bytes`, and the retained incident files
    are pruned to `max_files`;
  * the switch defaults to OFF, and the real app factories
    (`api.state_only.create_app` and `api.main.app`) arm the watchdog when it is
    on and disarm it when the lifespan exits, with the dump directory under the
    mounted data volume (`<AITELIER_HOME>/logs/loop-stack-dumps`).

THE BOUNDARY, restated here so no reader overclaims: the watchdog is Python code
and needs the GIL. A block held INSIDE a C extension that does not release the
GIL produces NOTHING - the watchdog thread cannot run either. It covers blocks
parked in syscalls that release the GIL (fsync, subprocess pipe read, a SQLite
busy-wait that releases the GIL, a blocking socket read, `time.sleep`).

CPU-only, no network and no HTTP server: a plain thread plus the real watchdog
is enough for the stall, as the acceptance contract says.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import datadir
from core import loop_stack_watchdog as lsw
from core.loop_stack_watchdog import (
    BUDGET_ENV,
    CHECK_ENV,
    MAX_BYTES_ENV,
    MAX_DUMPS_ENV,
    MAX_FILES_ENV,
    SWITCH_ENV,
    WATCHDOG_THREAD_NAME,
    LoopStackWatchdog,
)

# Small enough that the suite stays fast, long enough that scheduler jitter on a
# loaded host cannot be mistaken for the stall under test.
_BUDGET = 0.20
_CHECK = 0.02

_SECTION_RE = re.compile(
    r"^(?:Current thread|Thread) 0x([0-9a-f]+) \(most recent call first\):$")
_HEADER_RE = re.compile(
    r"^# aitelier loop-stack-watchdog: watched loop silent for "
    r"([0-9.]+)s \(budget ([0-9.]+)s\); dump (\d+)/(\d+); pid (\d+); (.+)$")
# The section the watchdog writes from sys._current_frames() before the
# capped all-threads dump: the ident is `threading.get_ident()` of the thread
# the heartbeat comes from.
_LOOP_THREAD_RE = re.compile(
    r"^# watched loop thread (\d+) \(the thread whose heartbeat went silent; "
    r"most recent call last\):$")
# The label that says how large the process was and where the all-threads dump
# stops; without it a capped dump is indistinguishable from a complete one.
_ALL_THREADS_RE = re.compile(
    r"^# all threads below, as faulthandler prints them: (\d+) live threads, "
    r"and faulthandler stops after (\d+) thread blocks")


# ── witnesses that read the REAL thread table ────────────────────────────────

def _watchdog_threads():
    return [t for t in threading.enumerate()
            if t.name == WATCHDOG_THREAD_NAME and t.is_alive()]


_libc_pthread_self = None


def _pthread_self_hex():
    """This thread's id as faulthandler prints it (`Thread 0x...`).

    faulthandler prints the platform thread handle, not `threading`'s ident or
    native id, so the test reads the same handle from libc to say WHICH section
    belongs to which thread. Linux/glibc only, like the container.
    """
    global _libc_pthread_self
    if _libc_pthread_self is None:
        try:
            libc = ctypes.CDLL(None)
            fn = libc.pthread_self
            fn.restype = ctypes.c_void_p
            _libc_pthread_self = fn
        except (OSError, AttributeError):
            _libc_pthread_self = False
    if not _libc_pthread_self:
        return None
    return "%016x" % int(_libc_pthread_self())


def _sections(data: str):
    """[(thread_id_hex, [frame lines])] in the order faulthandler wrote them."""
    sections = []
    current = None
    for line in data.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            current = (match.group(1), [])
            sections.append(current)
        elif current is not None and line.startswith("  File "):
            current[1].append(line)
        elif not line.startswith(" "):
            current = None
    return sections


def _dump_files(root: Path):
    return sorted(root.glob("loop-stack-*.log")) if root.exists() else []


# One block of a dump is a header line plus its all-thread stacks plus a footer
# line. The watchdog OPENS the incident file with its first block and APPENDS
# later blocks to the SAME file, so "the file exists" and "the header is there"
# are both weaker than "the block is written".
_DUMP_HEADER_MARKER = b"# aitelier loop-stack-watchdog: watched loop silent for"
_DUMP_FOOTER_MARKER = b"# end of aitelier loop-stack-watchdog dump"
_DUMP_HEADER_TEXT = _DUMP_HEADER_MARKER.decode()
_DUMP_FOOTER_TEXT = _DUMP_FOOTER_MARKER.decode()


def _complete_blocks(data: bytes) -> int:
    """Dump blocks in `data` that are COMPLETE: header AND footer on disk.

    A read that lands mid-append sees a header without its footer (the stacks
    are written between them), so `min(headers, footers)` counts only blocks a
    reader may assert on.
    """
    return min(data.count(_DUMP_HEADER_MARKER), data.count(_DUMP_FOOTER_MARKER))


def _wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ── one dump block, split out of its incident file ──────────────────────────

def _dump_blocks(data: str):
    """Complete blocks (header through footer) of one incident file, in order."""
    blocks, current = [], None
    for line in data.splitlines(keepends=True):
        if line.startswith(_DUMP_HEADER_TEXT):
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
            if line.startswith(_DUMP_FOOTER_TEXT):
                current = None
    return ["".join(lines) for lines in blocks]


def _capped_block(dump_dir: Path):
    """(path, block) for a COMPLETE block whose all-thread dump is capped.

    faulthandler prints `min(live, 100)` thread blocks, so >=100 blocks in one
    block of the file says the dump was written with >=100 live threads - and
    says it from the dump itself, so the selection works on the unfixed code
    too (which has no label to read the count from).
    """
    for path in _dump_files(dump_dir):
        text = path.read_bytes().decode("utf-8", "replace")
        for block in _dump_blocks(text):
            if len(_sections(block)) >= 100:
                return path, block
    return None


def _wait_for_dump_content(dump_dir: Path, blocks: int, timeout: float = 10.0):
    """Bounded wait for a dump file whose CONTENT holds `blocks` complete blocks.

    Returns `(path, data)` or None if the timeout expires. Reading only after
    the content is complete is what removes the race against a later append to
    the same incident file.
    """
    def _ready():
        for path in _dump_files(dump_dir):
            data = path.read_bytes()
            if _complete_blocks(data) >= blocks:
                return path, data
        return None

    return _wait_for(_ready, timeout=timeout)


# ── a thread that stands in for the event loop ───────────────────────────────

class _StalledLoop:
    """Beats like the loop, then parks in a real syscall that releases the GIL.

    `os.read` on a pipe with exactly one writer (this object) blocks in the
    kernel until `release()` writes and closes; that is a real block, not a
    mocked clock and not a busy wait.

    `park_gate` holds the stand-in loop in its BEATING phase until the test
    sets it, so a test can build the process it needs (>=100 live threads)
    BEFORE the silence starts. That ordering matters: this thread must exist
    first to be the OLDEST non-main thread (faulthandler's cap drops the
    oldest), while the spare threads that have to survive the cap must be
    created after it - and the watchdog's incident admits only
    `max_dumps_per_incident` dumps, spaced by doubling silence, so a test that
    races them against the first dumps loses whenever thread creation is
    slower than that budget (measured on a loaded host: three dumps at 0.202 /
    0.500 / 0.999 s of silence, with 9 / 20 / 38 live threads).
    """

    def __init__(self, watchdog: LoopStackWatchdog, *, beat_for: float = 0.25,
                 park_gate: threading.Event | None = None):
        self.watchdog = watchdog
        self.beat_for = beat_for
        self.park_gate = park_gate
        self.pthread_id = None
        self.ident = None
        self.error = None
        self.parked = threading.Event()
        self.exited = threading.Event()
        self._read_fd, self._write_fd = os.pipe()
        self._thread = threading.Thread(
            target=self._run, name="watched-loop", daemon=True)

    def start(self) -> "_StalledLoop":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.pthread_id = _pthread_self_hex()
            self.ident = threading.get_ident()
            deadline = time.monotonic() + self.beat_for
            # Beat until the minimum beat window has passed AND, when a gate
            # was given, until the test has built the process it wants. No
            # silence accrues while this loop beats, so no dump is written
            # before the park.
            while True:
                self.watchdog.beat()
                if time.monotonic() >= deadline and (
                        self.park_gate is None or self.park_gate.is_set()):
                    break
                time.sleep(0.01)
            self.parked.set()
            self._park_in_a_real_syscall()
            # The syscall returned: the loop is turning again. Beat a little so
            # the watchdog can close the incident, then leave.
            resume_until = time.monotonic() + 0.15
            while time.monotonic() < resume_until:
                self.watchdog.beat()
                time.sleep(0.01)
        except BaseException as exc:                                # noqa: BLE001
            self.error = exc
        finally:
            self.exited.set()
            try:
                os.close(self._read_fd)
            except OSError:
                pass

    def _park_in_a_real_syscall(self) -> None:
        # The frame a dump of THIS thread must show.
        os.read(self._read_fd, 1)

    def release(self) -> None:
        if not self.exited.is_set():
            try:
                os.write(self._write_fd, b"x")
            except OSError:
                pass
        self._thread.join(timeout=5)
        try:
            os.close(self._write_fd)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _no_leaked_watchdog():
    """No test starts armed, and none leaves a witness thread behind."""
    yield
    lsw.shutdown(lsw.current_watchdog())
    assert lsw.current_watchdog() is None
    assert _watchdog_threads() == [], (
        "a watchdog thread outlived its test: "
        f"{[t.name for t in _watchdog_threads()]}")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in (SWITCH_ENV, BUDGET_ENV, CHECK_ENV, MAX_DUMPS_ENV,
                 MAX_BYTES_ENV, MAX_FILES_ENV):
        monkeypatch.delenv(name, raising=False)


# ── 1. the witness: every thread, under its own id, while the syscall blocks ─

# Under xdist, execnet adds a thread that threading.active_count() does not count
# but faulthandler dumps, so the section count is one over.
@pytest.mark.serial
def test_real_watchdog_dumps_every_thread_while_one_is_parked_in_a_syscall(tmp_path):
    dump_dir = tmp_path / "dumps"

    # VACUITY GUARD: the SAME stall, nothing armed -> nothing written. If this
    # goes green, the armed assertions below cannot be green by accident.
    unarmed = LoopStackWatchdog(budget_s=_BUDGET, check_interval_s=_CHECK,
                                dump_dir=dump_dir)
    stall = _StalledLoop(unarmed).start()
    assert stall.parked.wait(timeout=5), "the stand-in loop never parked"
    time.sleep(3 * _BUDGET)
    assert stall.parked.is_set()
    assert _dump_files(dump_dir) == []
    assert not dump_dir.exists()
    stall.release()
    assert stall.error is None

    # ARMED: the real watchdog, a real file under a tmp path. One dump per
    # incident here, so the bytes this test reads cannot grow under it.
    watchdog = LoopStackWatchdog(budget_s=_BUDGET, check_interval_s=_CHECK,
                                 dump_dir=dump_dir,
                                 max_dumps_per_incident=1)
    watchdog.start()
    try:
        stall = _StalledLoop(watchdog).start()
        assert stall.parked.wait(timeout=5), "the stand-in loop never parked"
        # Wait for a COMPLETE dump (the watchdog writes a footer after the
        # traceback), while the thread is still parked in the syscall.
        def _completed():
            for path in _dump_files(dump_dir):
                if b"# end of aitelier loop-stack-watchdog dump" in path.read_bytes():
                    return path
            return None
        path = _wait_for(_completed, timeout=8)
        assert path is not None, (
            f"no completed dump after the loop was silent > {_BUDGET}s; "
            f"files={_dump_files(dump_dir)}")
        live_threads = threading.active_count()
        data = path.read_bytes().decode("utf-8", "replace")
        sections = _sections(data)

        # The header states the measured block length and the budget.
        header = data.splitlines()[0]
        match = _HEADER_RE.match(header)
        assert match, f"unexpected dump header: {header!r}"
        blocked, budget, dump_no, dump_max, pid, _when = match.groups()
        assert float(budget) == pytest.approx(_BUDGET)
        assert _BUDGET <= float(blocked) < 60.0
        assert dump_no == "1" and dump_max == "1"
        assert int(pid) == os.getpid()

        # EVERY live thread is in the dump, keyed by the id faulthandler prints.
        ids = [thread_id for thread_id, _frames in sections]
        assert len(sections) == live_threads, (
            f"dump has {len(sections)} thread sections for {live_threads} live "
            f"threads: {ids}")
        assert stall.pthread_id is not None
        assert stall.pthread_id in ids, (
            "the parked thread's own id is not a section header: "
            f"{stall.pthread_id} not in {ids}")
        assert _pthread_self_hex() in ids

        # ... and its own module + function, under that id, with the syscall
        # frame being the top of the stack (os.read blocks inside it).
        frames = dict(sections)[stall.pthread_id]
        assert frames, f"no frames under the parked thread's id {stall.pthread_id}"
        assert any("test_loop_stack_watchdog.py" in line
                   and " in _park_in_a_real_syscall" in line for line in frames), frames
        assert all("_park_in_a_real_syscall" not in line
                   for line in dict(sections)[_pthread_self_hex()]), \
            "the parked frame must be under the parked thread's id, not main's"
    finally:
        stall.release()
        watchdog.stop()
        assert stall.exited.is_set()


# ── 1b. the watched thread survives faulthandler's 100-thread cap ───────────

def test_dump_names_the_watched_loop_thread_past_100_live_threads(tmp_path):
    """The measurement in `iss-f1667dc30c2d41ee`, as a test.

    faulthandler stops at 100 thread blocks and prints nothing to say so. With
    more threads than that, the blocks it leaves out are the ones furthest down
    its thread-state list - measurably the OLDEST, which is where the loop
    thread and the watchdog sit. The live backend therefore wrote a 0.508 s
    stall as 100 blocks with no `Current thread` block and no stack reaching
    asyncio/base_events: the one frame the file exists to name was the one the
    cap removed. A dump taken at >=100 live threads must still contain it.
    """
    dump_dir = tmp_path / "dumps"
    watchdog = LoopStackWatchdog(budget_s=_BUDGET, check_interval_s=_CHECK,
                                 dump_dir=dump_dir,
                                 max_dumps_per_incident=3)
    spares_stop = threading.Event()
    spares = []
    stall = None
    park_gate = threading.Event()
    try:
        # ORDER IS THE MEASUREMENT HERE. The watched thread starts FIRST (so it
        # is the oldest non-main thread: the one faulthandler's cap drops), but
        # it keeps BEATING until `park_gate`, so the >=100-thread process is a
        # precondition of the silence instead of a race against the first
        # dumps. Created AFTER the watched thread, they are the NEWER threads:
        # a capped dump is the one that keeps them and drops the watched loop.
        stall = _StalledLoop(watchdog, park_gate=park_gate).start()
        for index in range(120):
            spare = threading.Thread(
                target=spares_stop.wait, name=f"spare-{index}", daemon=True)
            spare.start()
            spares.append(spare)
        live = threading.active_count()
        assert live >= 100, f"only {live} live threads: the cap is not reached"

        # Armed only now: the witness sees a process that already has its 120
        # spare threads and a loop that is still turning, so the incident this
        # test reads starts at the park, not at a dump written while the
        # process was still growing.
        watchdog.start()
        park_gate.set()
        assert stall.parked.wait(timeout=10), "the stand-in loop never parked"

        # The dump is written after the budget expires. Every dump of the
        # incident now sees all `live` threads, so the first one already is
        # the capped block this test asserts on.
        ready = _wait_for(lambda: _capped_block(dump_dir), timeout=20.0)
        assert ready is not None, (
            f"no completed dump block carried 100 all-thread sections after "
            f"the loop was silent for {watchdog.silent_seconds()}s; "
            f"files={_dump_files(dump_dir)}")
        path, block = ready
        lines = block.splitlines()

        # 1. This block was written in the defective regime: faulthandler
        #    printed its full 100 blocks, and the process had at least that
        #    many live threads.
        sections = _sections(block)
        assert len(sections) == 100, (
            f"{len(sections)} all-thread sections: faulthandler's cap is not "
            f"in play in this block")

        # 2. The watched thread's own frame chain IS in the file, written
        #    before the dump that can lose it. On the unfixed code there is no
        #    such section at all, so this is the assertion that reds there.
        loop_idx = next((i for i, line in enumerate(lines)
                         if _LOOP_THREAD_RE.match(line)), None)
        assert loop_idx is not None, (
            "the dump does not name the watched loop thread: no "
            "'# watched loop thread <ident>' section, so the loop thread's own "
            "blocking frame exists only in the capped all-threads dump "
            f"({len(sections)} blocks, live threads past the cap). "
            f"Block:\n{block}")
        loop_ident = int(_LOOP_THREAD_RE.match(lines[loop_idx]).group(1))
        assert loop_ident == stall.ident, (
            f"the section names thread {loop_ident}, but the heartbeat came "
            f"from {stall.ident}")

        # 3. The capped dump is LABELLED: its own header says how large the
        #    process was and where faulthandler stops, so a reader can tell a
        #    capped section from a complete one.
        notice_idx = next((i for i, line in enumerate(lines)
                           if _ALL_THREADS_RE.match(line)), None)
        assert notice_idx is not None, (
            "the all-threads dump is not labelled with the live thread count, "
            f"so a reader cannot tell it was capped. Block:\n{block}")
        named, cap = map(int, _ALL_THREADS_RE.match(lines[notice_idx]).groups())
        assert cap == 100
        assert named >= len(sections) >= 100, (
            f"the label says {named} live threads but faulthandler printed "
            f"{len(sections)} blocks")

        first_section_idx = next(i for i, line in enumerate(lines)
                                 if _SECTION_RE.match(line))
        assert loop_idx < notice_idx < first_section_idx, (
            "the watched chain must be written BEFORE the capped dump")
        chain = "\n".join(lines[loop_idx:notice_idx])
        assert "test_loop_stack_watchdog.py" in chain, chain
        assert " in _run" in chain and "in _park_in_a_real_syscall" in chain, (
            f"the watched thread's chain is not its blocking stack:\n{chain}")

        # 4. That section is the only place the frame survives: in this block
        #    faulthandler left the parked thread out (measured - the older
        #    threads are the ones it drops).
        capped = "\n".join(lines[first_section_idx:])
        assert "_park_in_a_real_syscall" not in capped, (
            "faulthandler's capped section still named the parked thread, so "
            "this block does not reproduce the 100-block cap")
        assert _dump_files(dump_dir) == [path], "one incident must be one file"
    finally:
        spares_stop.set()
        for spare in spares:
            spare.join(timeout=5)
        if stall is not None:
            # Open the gate first: a failure before it was set left the
            # stand-in loop still beating, and `release()` alone would then
            # join a thread that has not parked yet.
            park_gate.set()
            stall.release()
        watchdog.stop()
        assert stall is not None and stall.exited.is_set()


# ── 2. silent when healthy, and silent when the switch is off ────────────────

def test_healthy_loop_longer_than_3x_budget_writes_no_file_and_no_bytes(tmp_path):
    dump_dir = tmp_path / "dumps"
    watchdog = LoopStackWatchdog(budget_s=_BUDGET, check_interval_s=_CHECK,
                                 dump_dir=dump_dir)
    watchdog.start()
    try:
        # >= 3x the budget, beating every check interval: no silence at all.
        end = time.monotonic() + 3 * _BUDGET + 0.4
        beats = 0
        while time.monotonic() < end:
            watchdog.beat()
            beats += 1
            time.sleep(_CHECK)
        assert beats >= 3 * _BUDGET / _CHECK
    finally:
        watchdog.stop()
    assert _dump_files(dump_dir) == []
    assert sum(p.stat().st_size for p in _dump_files(dump_dir)) == 0
    assert not dump_dir.exists()


def test_switch_off_starts_no_thread_and_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    # The reported default: unset means OFF.
    for value in (None, "", "0", "off", "no"):
        if value is None:
            monkeypatch.delenv(SWITCH_ENV, raising=False)
        else:
            monkeypatch.setenv(SWITCH_ENV, value)
        assert lsw.enabled_from_env() is False
        assert lsw.start_from_env() is None
        assert _watchdog_threads() == []
        assert lsw.current_watchdog() is None
    assert not (tmp_path / "logs").exists()


# ── 3. one incident is bounded ──────────────────────────────────────────────

def test_one_incident_is_bounded_in_dumps_and_bytes(tmp_path):
    dump_dir = tmp_path / "dumps"
    max_dumps, max_bytes = 2, 64 * 1024
    watchdog = LoopStackWatchdog(budget_s=0.10, check_interval_s=0.02,
                                 dump_dir=dump_dir,
                                 max_dumps_per_incident=max_dumps,
                                 max_incident_bytes=max_bytes)
    watchdog.start()
    try:
        stall = _StalledLoop(watchdog, beat_for=0.25).start()
        assert stall.parked.wait(timeout=5)
        # Uncapped, geometric spacing would dump at 0.1, 0.2, 0.4, 0.8 s; hold
        # the stall past four due times and require the cap to stop it.
        time.sleep(1.2)
        assert watchdog.silent_seconds() >= 1.0
        # The incident file APPEARS when the watchdog opens it for the first
        # block, then the capped, later block is APPENDED to the same file
        # after a geometric interval. Waiting for the path and reading once
        # races that append (one block, or half of one, lands in the read).
        # Wait (bounded) for the CONTENT to hold `max_dumps` COMPLETE blocks;
        # the cap then makes the bytes final, so the assertions are stable and
        # the wait reds instead of hanging if the content never arrives.
        ready = _wait_for_dump_content(dump_dir, max_dumps, timeout=10.0)
        assert ready is not None, (
            f"no dump file with {max_dumps} complete blocks after the loop was "
            f"silent for {watchdog.silent_seconds()}s; "
            f"files={_dump_files(dump_dir)}")
        path, data = ready
        assert _complete_blocks(data) == max_dumps
        assert len(data) <= max_bytes
        assert len(_dump_files(dump_dir)) == 1, "one incident must be one file"
    finally:
        stall.release()
        watchdog.stop()


def test_incident_files_are_pruned_to_max_files(tmp_path):
    dump_dir = tmp_path / "dumps"
    watchdog = LoopStackWatchdog(budget_s=0.05, check_interval_s=0.01,
                                 dump_dir=dump_dir, max_files=1,
                                 max_dumps_per_incident=1)
    watchdog.start()
    try:
        for _round in (1, 2):
            before = set(_dump_files(dump_dir))
            stall = _StalledLoop(watchdog, beat_for=0.15).start()
            assert stall.parked.wait(timeout=5)
            new = _wait_for(lambda: set(_dump_files(dump_dir)) - before,
                            timeout=5)
            assert new, f"incident {_round} wrote no dump"
            # Retention: only the newest incident's file survives (the old one
            # is pruned right after the new dump is complete).
            kept = _wait_for(lambda: _dump_files(dump_dir) == sorted(new),
                             timeout=5)
            assert kept, (
                f"incident {_round} kept {_dump_files(dump_dir)}, expected only "
                f"{sorted(new)}")
            stall.release()
            assert stall.exited.is_set()
            # Next beat closes this incident; wait for the incident to settle.
            assert _wait_for(lambda: watchdog.silent_seconds() < watchdog.budget_s,
                             timeout=5)
            assert _dump_files(dump_dir) == sorted(new)
    finally:
        watchdog.stop()
    assert len(_dump_files(dump_dir)) == 1


# ── 4. wired into the real app factories, bounded under the data volume ─────

@pytest.fixture
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    return tmp_path


def _state_only_app(root: Path):
    from api.state_only import create_app
    return create_app(str(root / "state.sqlite"), "t" * 40)


def _full_app(root: Path):
    # The caller's _test_mode(app) sets the flag and restores it; setting it here
    # leaked True into later files on the same worker (anonymous private reads).
    from api.main import app
    return app


@contextlib.contextmanager
def _test_mode(app):
    """Run the real app's lifespan with its test-mode short-circuit, restored."""
    had = hasattr(app.state, "_test_mode")
    previous = getattr(app.state, "_test_mode", None)
    app.state._test_mode = True
    try:
        yield app
    finally:
        if had:
            app.state._test_mode = previous
        else:
            app.state._state.pop("_test_mode", None)


@pytest.mark.parametrize("factory", [_state_only_app, _full_app],
                         ids=["state_only", "main"])
def test_app_factory_switch_off_serves_without_a_watchdog(factory, _home):
    app = factory(_home)
    with _test_mode(app):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert _watchdog_threads() == []
            assert lsw.current_watchdog() is None
        assert _watchdog_threads() == []
        assert not (_home / "logs").exists()


@pytest.mark.parametrize("factory", [_state_only_app, _full_app],
                         ids=["state_only", "main"])
def test_app_factory_switch_on_arms_under_home_logs_and_disarms(
        factory, _home, monkeypatch):
    monkeypatch.setenv(SWITCH_ENV, "1")
    # The app test does not hunt for a genuine stall: a slow first-import on the
    # loop must not be reported as one. The controlled stall is test 1's job.
    monkeypatch.setenv(BUDGET_ENV, "5.0")
    monkeypatch.setenv(CHECK_ENV, "0.1")
    app = factory(_home)
    with _test_mode(app):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            threads = _watchdog_threads()
            assert len(threads) == 1, [t.name for t in threading.enumerate()]
            watchdog = lsw.current_watchdog()
            assert watchdog is not None and watchdog.started
            assert watchdog.thread is threads[0]
            # On the mounted data volume, not the process CWD or a tempfile.
            assert watchdog.dump_dir == _home / "logs" / lsw.DUMP_DIR_NAME
            assert watchdog.budget_s == pytest.approx(5.0)
            # The loop is beating: nothing is written while it answers.
            assert _dump_files(watchdog.dump_dir) == []
        # Teardown of the lifespan disarmed it: no thread outlives the app.
        assert _watchdog_threads() == []
        assert lsw.current_watchdog() is None
        assert _dump_files(_home / "logs" / lsw.DUMP_DIR_NAME) == []
        assert not (_home / "logs" / lsw.DUMP_DIR_NAME).exists()


def test_dump_directory_is_the_mounted_data_volume(monkeypatch):
    """Production resolution, asserted without writing anything."""
    monkeypatch.delenv("AITELIER_HOME", raising=False)
    assert datadir.logs_dir() == Path.home() / ".AItelier" / "logs"
    assert lsw.dump_dir() == Path.home() / ".AItelier" / "logs" / lsw.DUMP_DIR_NAME


def test_config_defaults_are_stated_and_clamped(monkeypatch):
    for name in (BUDGET_ENV, CHECK_ENV, MAX_DUMPS_ENV, MAX_BYTES_ENV,
                 MAX_FILES_ENV):
        monkeypatch.delenv(name, raising=False)
    config = lsw.config_from_env()
    assert config["budget_s"] == 0.5
    assert config["check_interval_s"] == 0.05
    assert config["max_dumps_per_incident"] == 3
    assert config["max_incident_bytes"] == 256 * 1024
    assert config["max_files"] == 20

    # A pathological setting cannot turn the watchdog into a busy loop or an
    # unbounded writer: every number is clamped.
    monkeypatch.setenv(BUDGET_ENV, "0.0001")
    monkeypatch.setenv(CHECK_ENV, "0.0001")
    monkeypatch.setenv(MAX_DUMPS_ENV, "100000")
    monkeypatch.setenv(MAX_BYTES_ENV, "1")
    monkeypatch.setenv(MAX_FILES_ENV, "100000")
    clamped = lsw.config_from_env()
    assert clamped["budget_s"] == lsw._MIN_BUDGET_S
    assert clamped["check_interval_s"] == lsw._MIN_CHECK_S
    assert clamped["max_dumps_per_incident"] == lsw._MAX_DUMPS
    assert clamped["max_incident_bytes"] == lsw._MIN_INCIDENT_BYTES
    assert clamped["max_files"] == lsw._MAX_FILES_CAP
