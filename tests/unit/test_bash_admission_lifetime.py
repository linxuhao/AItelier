"""A write admission must outlive the WRITE, not the coroutine that started it.

The independent review's D1: `_tool_bash` held its admission in a `with` block
and retired it in `finally`, and reaching that `finally` is not evidence the
command ended. A client disconnect raises `CancelledError` in a mid-tool await
(the meta_agent says so itself); nothing killed the subprocess; the admission
was retired and a direct run could take the checkout while the orphan was still
writing into it. The timeout path had the same hole one level down —
`proc.kill()` kills `/bin/sh`, not what the command backgrounded.

These tests drive the REAL `_tool_bash` and the REAL admission record, and they
watch what the WRITER does: a file the command writes after the tool call has
returned. The reviewer's probe asserted the defect (child survives, admission
gone); these assert the property instead — after the call, either the writers
are demonstrably ended, or the admission is still held and a direct run is still
refused. Never "the checkout is free" while something writes.

Nothing here mocks a kill and checks it was called.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core import run_isolation as ri
from core.db_manager import DBManager
from core.meta_agent import MetaAgent
from core.run_isolation import CheckoutLeased
from core.workspace_manager import WorkspaceManager

_WRITE_DELAY = 3.0          # the writer writes this long after it starts
_SETTLE = 7.0               # how long we watch for a write that must not happen


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)


def _writer(marker: Path, delay: float = _WRITE_DELAY) -> str:
    """A command that writes into the checkout AFTER a delay."""
    return (f"{sys.executable} -c \"import time,pathlib;time.sleep({delay});"
            f"pathlib.Path(r'{marker}').write_text('written by the writer')\"")


def _appeared(marker: Path, within: float) -> bool:
    deadline = time.time() + within
    while time.time() < deadline:
        if marker.exists():
            return True
        time.sleep(0.1)
    return marker.exists()


def _group_alive(pgid: int) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


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
    agent = MetaAgent(db, ws, owner_email="t@local", mode="coding")
    return {"db": db, "checkout": checkout, "agent": agent, "tmp": tmp_path}


def _direct_acquire(live, run_id="run-direct"):
    """The acquisition the exclusion exists to stop. Returns (ok, error)."""
    try:
        ri._acquire_lease(live["db"], ri.canonical_checkout(live["checkout"]),
                          run_id, "serial")
        return True, None
    except CheckoutLeased as e:
        return False, e


# ── cancellation: the ordinary client-disconnect path ────────────────

def test_a_cancelled_bash_ends_its_writer_before_it_retires(live):
    """Cancel a running `bash` the way a stream disconnect does, and require
    that no write lands afterwards — and that the tool re-raises rather than
    reporting the cancellation as a result."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    marker = checkout / "written_after_cancel.txt"
    state = {}

    async def drive():
        task = asyncio.ensure_future(
            agent._tool_bash({"project_id": "p", "command": _writer(marker),
                              "timeout": 60}))
        await asyncio.sleep(1.0)
        state["held_while_running"] = bool(ri.write_admissions(db))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())

    assert state["held_while_running"], (
        "precondition: the admission must be held while the command runs")
    assert not _appeared(marker, _SETTLE), (
        "the writer outlived the cancelled tool call and wrote into the checkout")
    assert ri.write_admissions(db) == [], (
        "the writer was ended, so the admission should have been retired")
    ok, err = _direct_acquire(live)
    assert ok, f"the checkout stayed leased after a clean cancellation: {err}"


def test_a_cancelled_bash_never_leaves_a_writer_and_a_free_checkout(live):
    """The property stated as one assertion: a write landing after the call is
    only acceptable if the admission is STILL held (and a direct run refused).
    Both-at-once is the violation."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    marker = checkout / "overlap.txt"

    async def drive():
        task = asyncio.ensure_future(
            agent._tool_bash({"project_id": "p", "command": _writer(marker),
                              "timeout": 60}))
        await asyncio.sleep(1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    acquired, err = _direct_acquire(live)
    wrote = _appeared(marker, _SETTLE)
    assert not (wrote and acquired), (
        f"a direct run took the checkout while the writer was still writing "
        f"(wrote={wrote}, acquired={acquired}, refusal={err})")


# ── timeout: the grandchild the shell leaves behind ──────────────────

def test_a_timed_out_bash_ends_the_process_it_backgrounded(live):
    """`proc.kill()` killed the shell only. The command here backgrounds a
    writer and then sleeps past the timeout."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    marker = checkout / "written_after_timeout.txt"
    cmd = f"{_writer(marker)} & sleep 30"

    res = asyncio.run(agent._tool_bash(
        {"project_id": "p", "command": cmd, "timeout": 1}))

    assert "error" in res and "timed out" in res["error"], res
    assert not _appeared(marker, _SETTLE), (
        "a process the command backgrounded outlived the timeout and wrote "
        "into the checkout")
    assert ri.write_admissions(db) == []
    ok, err = _direct_acquire(live)
    assert ok, f"the checkout stayed leased after a confirmed cleanup: {err}"


# ── normal completion is not proof the group is empty ────────────────

def test_a_completed_bash_that_left_a_background_writer_does_not_declare_free(live):
    """Exit code 0 says the shell ended. It says nothing about what the shell
    started with `&` and stdout redirected away."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    marker = checkout / "written_by_background.txt"
    cmd = f"({_writer(marker)}) >/dev/null 2>&1 & echo started"

    res = asyncio.run(agent._tool_bash(
        {"project_id": "p", "command": cmd, "timeout": 30}))
    assert res.get("exit_code") == 0, res

    wrote = _appeared(marker, _SETTLE)
    held = ri.write_admissions(db)
    acquired, err = _direct_acquire(live)
    assert not (wrote and acquired), (
        f"the checkout was declared free while a backgrounded writer was still "
        f"writing (wrote={wrote}, admissions={held}, acquired={acquired})")


def test_control_a_plain_completed_bash_retires_and_frees_the_checkout(live):
    """The control that makes the others meaningful: when the command really
    has ended, the admission goes and the checkout is available."""
    db, agent = live["db"], live["agent"]
    res = asyncio.run(agent._tool_bash(
        {"project_id": "p", "command": "echo ok", "timeout": 30}))
    assert "error" not in res and res["exit_code"] == 0, res
    assert ri.write_admissions(db) == []
    ok, err = _direct_acquire(live)
    assert ok, err


# ── when cleanup cannot confirm, the admission is RETAINED ───────────

def test_an_unconfirmable_cleanup_retains_the_admission_with_evidence(live,
                                                                     monkeypatch):
    """If the owned group cannot be confirmed gone, the honest answer is not to
    retire. The row stays, carries what happened, and a direct run is refused."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    import core.meta_agent as ma
    monkeypatch.setattr(ma, "_owned_group_is_gone", lambda pgid: False,
                        raising=False)

    res = asyncio.run(agent._tool_bash(
        {"project_id": "p", "command": "echo ok", "timeout": 30}))
    assert "error" not in res, res

    held = ri.write_admissions(db)
    assert held, "an unconfirmed cleanup must not retire the admission"
    blob = " ".join(str(r) for r in held)
    assert "PENDING" in blob.upper(), f"no operator-visible evidence: {held}"

    acquired, err = _direct_acquire(live)
    assert not acquired, "a direct run took a checkout with unconfirmed writers"
    assert "clear_write_admission" in str(err), str(err)


def test_a_lost_handle_during_spawn_retains_the_admission(live, monkeypatch):
    """The spawn/await race: a cancellation that lands after the fork and
    before the handle is ours. We cannot know whether a writer exists, so the
    admission stays and the checkout stays excluded."""
    db, agent = live["db"], live["agent"]
    import core.meta_agent as ma
    real_create = asyncio.create_subprocess_shell

    async def _create_then_cancel(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        try:
            raise asyncio.CancelledError()
        finally:
            pass

    monkeypatch.setattr(ma.asyncio, "create_subprocess_shell",
                        _create_then_cancel)

    async def drive():
        with pytest.raises(asyncio.CancelledError):
            await agent._tool_bash({"project_id": "p", "command": "sleep 5",
                                    "timeout": 30})

    asyncio.run(drive())
    held = ri.write_admissions(db)
    assert held, ("a cancellation during spawn released the checkout without "
                  "knowing whether a writer exists")
    acquired, _ = _direct_acquire(live)
    assert not acquired


def test_a_second_cancellation_during_cleanup_still_ends_the_writer(live):
    """Cleanup itself runs on a task that is already being cancelled. A second
    cancellation must not abandon the child handle."""
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    marker = checkout / "written_after_double_cancel.txt"

    async def drive():
        task = asyncio.ensure_future(
            agent._tool_bash({"project_id": "p", "command": _writer(marker),
                              "timeout": 60}))
        await asyncio.sleep(1.0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    wrote = _appeared(marker, _SETTLE)
    acquired, err = _direct_acquire(live)
    assert not (wrote and acquired), (
        f"double cancellation lost the writer (wrote={wrote}, acquired={acquired})")


# ── the rest of the surface is untouched ─────────────────────────────

def test_edit_and_create_still_hold_and_retire_their_admissions(live):
    db, checkout, agent = live["db"], live["checkout"], live["agent"]
    (checkout / "file.txt").write_text("original\n")
    agent._files_read.add(("p", str((checkout / "file.txt").resolve())))
    assert agent._tool_create_file({"project_id": "p", "path": "new.txt",
                                    "content": "x"}).get("created") == "new.txt"
    assert agent._tool_edit_file({"project_id": "p", "path": "file.txt",
                                  "old_str": "original",
                                  "new_str": "changed"}).get("edited")
    assert ri.write_admissions(db) == []
    ok, _ = _direct_acquire(live)
    assert ok
