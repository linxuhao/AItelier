"""Ending the writers must not stop the process that is ending them.

The D1 fix ended the owned process group before retiring the admission, and the
independent review confirmed the writers really do end. It also found what that
cost: `_end_owned_group` polled `time.sleep(0.05)` for up to five seconds ON THE
EVENT LOOP, so every cancelled or timed-out `bash` froze SSE heartbeats, the
scheduler tick and every other request for that long — measured at 5.03 s and
5.04 s, and on the ORDINARY path, not a corner.

The previous round traded event-loop availability for uninterruptibility. Both
are required, so these tests measure the heartbeat while a real cancellation
really cleans a real process group. The bound is on the WORST GAP BETWEEN
HEARTBEATS, deliberately not on how long the cleanup takes: a SIGTERM-ignoring
child legitimately needs seconds to escalate, and during those seconds the loop
must still be serving everything else.

The other two are the review's F2 (a cancellation arriving during the reap was
swallowed and the call returned a normal result) and F3 (the code and the report
claimed a `setsid` escape is detected and retained; it is not, and the honest
statement is asserted here so the claim cannot drift back).
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import meta_agent as ma
from core import run_isolation as ri
from core.db_manager import DBManager
from core.meta_agent import MetaAgent
from core.workspace_manager import WorkspaceManager

# Generous: the heartbeat ticks every 20 ms, so anything under half a second is
# "the loop kept running". Tied to the heartbeat, never to the cleanup's
# duration — escalating past a SIGTERM-ignoring child takes seconds by design.
_MAX_LOOP_GAP_S = 0.5
_SETTLE = 7.0


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


@pytest.fixture
def live(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    db = DBManager(str(tmp_path / "aitelier.db"))
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    checkout = tmp_path / "checkout"
    _init_repo(checkout)
    db.ensure_project("p", name="p", repo_type="existing", repo_path=str(checkout))
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    return {"db": db, "checkout": checkout,
            "agent": MetaAgent(db, ws, owner_email="t@local", mode="coding")}


class _Heartbeat:
    """What every other task on this loop experiences while `bash` cleans up."""

    def __init__(self, tick=0.02):
        self.tick = tick
        self.gaps = []
        self._stop = asyncio.Event()
        self._task = None

    def start(self):
        self._task = asyncio.ensure_future(self._run())
        return self

    async def _run(self):
        last = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(self.tick)
            now = time.monotonic()
            self.gaps.append(now - last)
            last = now

    async def stop(self):
        self._stop.set()
        await self._task

    @property
    def worst(self):
        return max(self.gaps) if self.gaps else 0.0


def _stubborn(seconds: int, marker: Path | None = None) -> str:
    """A child that IGNORES SIGTERM, so cleanup has to escalate to SIGKILL."""
    body = ("import signal,time,pathlib;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"time.sleep({seconds});")
    if marker:
        body += f"pathlib.Path(r'{marker}').write_text('x');"
    return f'{sys.executable} -c "{body}"'


def _appeared(marker: Path, within: float) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if marker.exists():
            return True
        time.sleep(0.1)
    return marker.exists()


def _direct_acquire(live, run_id="run-direct"):
    from core.run_isolation import CheckoutLeased
    try:
        ri._acquire_lease(live["db"], ri.canonical_checkout(live["checkout"]),
                          run_id, "serial")
        return True, None
    except CheckoutLeased as e:
        return False, e


# ── F1 · the loop keeps running while the writers are ended ──────────

def test_cancelling_an_ordinary_command_keeps_the_loop_responsive(live):
    """A plain `sleep`, cancelled. It dies on the first SIGTERM, and the review
    measured a 5.04 s loop freeze anyway — the tool's own child stays a zombie
    that answers `killpg` until it is reaped, and the reap came last."""
    agent = live["agent"]

    async def drive():
        hb = _Heartbeat().start()
        task = asyncio.ensure_future(agent._tool_bash(
            {"project_id": "p", "command": "sleep 30", "timeout": 60}))
        await asyncio.sleep(0.6)
        t0 = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cost = time.monotonic() - t0
        await hb.stop()
        return hb.worst, cost

    worst, cost = asyncio.run(drive())
    print(f"\nordinary cancel: worst loop gap {worst:.2f}s, unwind {cost:.2f}s")
    assert worst < _MAX_LOOP_GAP_S, (
        f"the event loop was frozen for {worst:.2f}s cancelling an ordinary "
        f"command: every other task on it — SSE heartbeats, the scheduler tick, "
        f"every request — stopped for that long")


def test_escalating_to_sigkill_keeps_the_loop_responsive(live):
    """The case that legitimately takes seconds: a SIGTERM-ignoring child has
    to be escalated. The cleanup may take that long; the LOOP may not stop."""
    agent, checkout = live["agent"], live["checkout"]
    marker = checkout / "stubborn_wrote.txt"

    async def drive():
        hb = _Heartbeat().start()
        task = asyncio.ensure_future(agent._tool_bash(
            {"project_id": "p", "command": _stubborn(30, marker), "timeout": 60}))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await hb.stop()
        return hb.worst

    worst = asyncio.run(drive())
    print(f"\nSIGKILL escalation: worst loop gap {worst:.2f}s")
    assert worst < _MAX_LOOP_GAP_S, (
        f"the event loop was frozen for {worst:.2f}s while escalating")
    # …and the safety property is unchanged by making it async.
    assert not _appeared(marker, _SETTLE), "the stubborn writer survived"
    assert ri.write_admissions(live["db"]) == []
    ok, err = _direct_acquire(live)
    assert ok, err


def test_a_timed_out_command_keeps_the_loop_responsive(live):
    """The other path that reaches cleanup on every call."""
    agent, checkout = live["agent"], live["checkout"]
    marker = checkout / "timeout_wrote.txt"
    cmd = f"{_stubborn(30, marker)} & sleep 30"

    async def drive():
        hb = _Heartbeat().start()
        res = await agent._tool_bash(
            {"project_id": "p", "command": cmd, "timeout": 1})
        await hb.stop()
        return hb.worst, res

    worst, res = asyncio.run(drive())
    print(f"\ntimeout: worst loop gap {worst:.2f}s")
    assert "error" in res and "timed out" in res["error"], res
    assert worst < _MAX_LOOP_GAP_S, (
        f"the event loop was frozen for {worst:.2f}s on the timeout path")
    assert not _appeared(marker, _SETTLE)


# ── F2 · a cancellation during the reap is not a result ──────────────

def test_a_cancellation_during_the_reap_is_not_reported_as_success(live,
                                                                   monkeypatch):
    """WHITE-BOX, and labelled: the window is real — a client disconnect can
    land in any await — but far too narrow to hit by timing. So `proc.wait()`
    is made to raise exactly what a cancellation arriving there raises, on a
    command that has ALREADY COMPLETED normally. Returning its output would be
    reporting a cancelled call as a success."""
    agent = live["agent"]
    real_create = asyncio.create_subprocess_shell

    async def _create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        real_wait = proc.wait
        state = {"n": 0}

        async def _wait():
            # Call 1 is `communicate()`'s own internal wait — that path is
            # already covered. The REAP is the next one, and that is the window
            # the review found: a cancellation there was swallowed and the call
            # returned output as if nothing had happened.
            state["n"] += 1
            if state["n"] == 2:
                raise asyncio.CancelledError()
            return await real_wait()

        proc.wait = _wait
        return proc

    monkeypatch.setattr(ma.asyncio, "create_subprocess_shell", _create)

    async def drive():
        with pytest.raises(asyncio.CancelledError):
            await agent._tool_bash({"project_id": "p", "command": "echo hi",
                                    "timeout": 30})

    asyncio.run(drive())


def test_a_real_second_cancellation_during_cleanup_still_raises(live):
    """REAL boundary, no white-box: cancel, then cancel again while the cleanup
    is running. The call must still end as a cancellation."""
    agent = live["agent"]

    async def drive():
        task = asyncio.ensure_future(agent._tool_bash(
            {"project_id": "p", "command": _stubborn(30), "timeout": 60}))
        await asyncio.sleep(1.0)
        task.cancel()
        await asyncio.sleep(0.05)      # inside cleanup now
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    # The writers are ended either way; whether the admission is retired or
    # retained depends on whether cleanup could finish under repeated
    # cancellation, and both are safe. What must NOT happen is a result.
    ok, _ = _direct_acquire(live)
    print("\nafter a double cancel: admission retired =", ok)


# ── F3 · the honest scope of an owned process group ──────────────────

def test_a_setsid_escapee_is_documented_as_unseen_not_as_detected(live):
    """The review's F3. A process that calls `setsid` LEAVES the owned group;
    the group then reads as empty, which is the strongest confirmation there
    is, and the admission is retired. The tool cannot see it.

    This test asserts that reality so the claim cannot drift back to "detected
    and retained" — which is what the code comment and the report said. It is
    not an endorsement of the outcome: the ruling is that the scope is one
    owned process group, and this is what that scope costs.
    """
    agent, checkout = live["agent"], live["checkout"]
    marker = checkout / "escapee_wrote.txt"
    body = ("import os,time,pathlib;os.setsid();time.sleep(3);"
            f"pathlib.Path(r'{marker}').write_text('escaped');")
    cmd = f'{sys.executable} -c "{body}" & sleep 0.2'

    res = asyncio.run(agent._tool_bash(
        {"project_id": "p", "command": cmd, "timeout": 30}))
    assert "error" not in res, res

    wrote = _appeared(marker, _SETTLE)
    held = ri.write_admissions(live["db"])
    assert wrote, ("probe inconclusive: the escapee did not outlive the call, "
                   "so this says nothing about the boundary")
    assert held == [], (
        "the escapee was NOT detected — if this now retains, the boundary "
        "changed and the documentation has to change with it")
    # The words that must be true wherever this is described.
    doc = ma._owned_group_is_gone.__doc__ or ""
    src_note = (ma._end_owned_group_async.__doc__ or "") if hasattr(
        ma, "_end_owned_group_async") else ""
    assert "setsid" in (doc + src_note).lower(), (
        "the owned-group helpers must say, where a reader will find it, that a "
        "setsid escape leaves the group and reads as gone")


def test_the_bash_tool_contract_states_the_group_boundary(live):
    """An operator reading the tool's own description must find the boundary
    there, not only in a report."""
    schema = next(t for t in ma.CODING_TOOL_DEFINITIONS
                  if t.get("function", {}).get("name") == "bash")
    desc = schema["function"]["description"].lower()
    assert "process group" in desc or "group" in desc
    assert "setsid" in desc, (
        "the bash tool description does not tell its caller that a detached "
        "process leaves the group this call can end")
