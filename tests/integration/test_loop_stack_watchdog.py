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
    """

    def __init__(self, watchdog: LoopStackWatchdog, *, beat_for: float = 0.25):
        self.watchdog = watchdog
        self.beat_for = beat_for
        self.pthread_id = None
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
            deadline = time.monotonic() + self.beat_for
            while time.monotonic() < deadline:
                self.watchdog.beat()
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
    from api.main import app
    app.state._test_mode = True
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
