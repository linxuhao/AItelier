"""Step 24.2b (design-steward 7.8.12a "一个写者一个子目录" 清理, 7.8.18 "导演子目录的清理"):
the DSH host's supervise loop cleans a NOT running director's media subdirectory once a day,
before that project's next spawn, removes `d-<driver_id>/` whose driver row is gone, and never
touches a running driver's subdirectory or the steward's.

A fake runtime stands in for DshDrivers, and the cleanup command is a recorder script, so the
order of cleanup and spawn is visible; one test runs the real media-gc.mjs when node exists.
"""
import asyncio
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import core.dsh_host as dsh_host
from core.dsh_host import MEDIA_GC_INTERVAL_SECONDS, media_gc, supervise, sweep_orphan_media

ALPHA = "dsh-" + "a" * 20
BETA = "dsh-" + "b" * 20
STEWARD = "dsh-" + "c" * 20

RECORDER = r'''
import os, pathlib, sys, time
d = pathlib.Path(sys.argv[1])
def note(word):
    with open(os.environ["GC_LOG"], "a") as f:
        f.write(f"{word} {d.name}\n")
note("gc-begin")
if (d / ".slow").exists():
    time.sleep(float((d / ".slow").read_text()))
if (d / ".fail").exists():
    sys.stderr.write("media-gc: cleanup failed: EACCES\n")
    sys.exit(1)
note("gc-end")
'''


class FakeProc:
    pid = 4242
    returncode = None


class FakeRuntime:
    """What supervise() reads of DshDrivers: rows, processes, running, start, stop, close."""

    def __init__(self, root, rows, log):
        self.root, self._rows, self.log = root, rows, log
        self.processes = {}
        self.rows_fail = False
        for row in rows:
            (root / row["driver_id"]).mkdir(parents=True, exist_ok=True)

    def rows(self):
        if self.rows_fail:
            raise RuntimeError("database is locked")
        return [dict(row) for row in self._rows]

    def running(self, project):
        proc = self.processes.get(project)
        return proc is not None and proc.returncode is None

    async def start(self, project, *, actor):
        with open(self.log, "a") as f:
            f.write(f"start {project}\n")
        self.processes[project] = FakeProc()

    async def stop(self, project):
        self.processes.pop(project, None)

    async def close(self):
        pass


def row(project, driver_id, enabled=1):
    return {"project_id": project, "driver_id": driver_id, "enabled": enabled, "phase": "stopped",
            "session_id": "s-" + project, "last_error": ""}


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "events.log"
    log.write_text("")
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER)
    monkeypatch.setenv("GC_LOG", str(log))
    monkeypatch.setattr(dsh_host, "MEDIA_GC_COMMAND", (sys.executable, str(recorder)))

    async def no_fallback(runtime, row, *, then_start):
        return False
    monkeypatch.setattr(dsh_host, "fallback_restart", no_fallback)
    media = tmp_path / "media"
    media.mkdir(mode=0o700)
    root = tmp_path / "drivers"
    root.mkdir()
    return {"log": log, "media": media, "root": root}


def events(env):
    return env["log"].read_text().splitlines()


def subdir(env, name, files=("2026-10/f0.pdf",)):
    path = env["media"] / name
    for file in files:
        (path / file).parent.mkdir(parents=True, exist_ok=True)
        (path / file).write_text("%PDF-1.7 " + name)
    (path / "index.jsonl").write_text('{"ref":"m_' + "0" * 16 + '"}\n')
    return path


def snapshot(path):
    return {str(p.relative_to(path)): (p.read_text() if p.is_file() else "dir")
            for p in sorted(path.rglob("*"))}


async def run_until(runtime, env, check, *, media="default", ticks_after=0):
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(runtime, stopped, interval=0.01,
                                         media=env["media"] if media == "default" else media))
    try:
        async with asyncio.timeout(10):
            while not check():
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.01 * ticks_after)
    finally:
        stopped.set()
        await task


async def test_a_stopped_director_is_cleaned_before_its_spawn_and_the_spawn_waits(env):
    path = subdir(env, "d-" + ALPHA)
    (path / ".slow").write_text("0.4")
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA)], env["log"])
    await run_until(runtime, env, lambda: "start alpha" in events(env))
    assert events(env)[:3] == [f"gc-begin d-{ALPHA}", f"gc-end d-{ALPHA}", "start alpha"]


async def test_a_disabled_director_is_cleaned_and_a_running_one_never_is(env):
    running = subdir(env, "d-" + BETA)
    before = snapshot(running)
    subdir(env, "d-" + ALPHA)
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA, enabled=0), row("beta", BETA)], env["log"])
    runtime.processes["beta"] = FakeProc()  # beta's own host is running: it cleans itself
    await run_until(runtime, env, lambda: f"gc-end d-{ALPHA}" in events(env), ticks_after=20)
    assert events(env) == [f"gc-begin d-{ALPHA}", f"gc-end d-{ALPHA}"]
    assert snapshot(running) == before, "the host never touched the running driver's subdirectory"
    # Asked directly, for a disabled row whose child is still alive: still nothing.
    await media_gc(runtime, row("beta", BETA, enabled=0), env["media"], {}, 0.0)
    assert events(env) == [f"gc-begin d-{ALPHA}", f"gc-end d-{ALPHA}"]


async def test_a_subdirectory_without_a_driver_row_is_removed_and_the_steward_is_never_touched(env, tmp_path):
    gone = subdir(env, "d-dsh-" + "9" * 20)
    owner = subdir(env, "owner")
    steward_like = subdir(env, "d-" + STEWARD)  # the steward row's driver id, under d-
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.pdf").write_text("%PDF")
    (env["media"] / "d-link").symlink_to(outside, target_is_directory=True)
    (env["media"] / "d-file").write_text("not a directory")
    other = subdir(env, "unrelated")
    keep = {p: snapshot(p) for p in (owner, steward_like, other)}
    runtime = FakeRuntime(env["root"], [row("aitelier-home", STEWARD, enabled=0),
                                        row("alpha", ALPHA, enabled=0)], env["log"])
    await run_until(runtime, env, lambda: not gone.exists(), ticks_after=20)
    assert not gone.exists()
    for path, before in keep.items():
        assert snapshot(path) == before, path
    assert (outside / "keep.pdf").exists(), "a symlink is never followed"
    assert (env["media"] / "d-link").is_symlink() and (env["media"] / "d-file").is_file()
    assert events(env) == [], "no cleanup ran: the steward row is skipped, alpha has no subdirectory"
    assert not (env["media"] / ("d-" + ALPHA)).exists(), "the cleanup creates no directory"


async def test_unreadable_rows_remove_nothing(env):
    orphan = subdir(env, "d-dsh-" + "9" * 20)
    runtime = FakeRuntime(env["root"], [], env["log"])
    runtime.rows_fail = True
    ledger = {}
    sweep_orphan_media(runtime, env["media"], ledger, 1000.0)
    assert orphan.exists(), "a locked database is not an empty one"


async def test_at_most_once_a_day_per_subdirectory(env):
    subdir(env, "d-" + ALPHA)
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA, enabled=0)], env["log"])
    await run_until(runtime, env, lambda: f"gc-end d-{ALPHA}" in events(env), ticks_after=30)
    assert events(env) == [f"gc-begin d-{ALPHA}", f"gc-end d-{ALPHA}"], "many ticks, one cleanup"

    env["log"].write_text("")
    ledger, alpha = {}, row("alpha", ALPHA, enabled=0)
    await media_gc(runtime, alpha, env["media"], ledger, 1000.0)
    await media_gc(runtime, alpha, env["media"], ledger, 1000.0 + MEDIA_GC_INTERVAL_SECONDS - 1)
    assert events(env) == [f"gc-begin d-{ALPHA}", f"gc-end d-{ALPHA}"]
    await media_gc(runtime, alpha, env["media"], ledger, 1000.0 + MEDIA_GC_INTERVAL_SECONDS)
    assert events(env).count(f"gc-end d-{ALPHA}") == 2, "the next day it runs again"

    orphan = subdir(env, "d-dsh-" + "9" * 20)
    sweep_orphan_media(runtime, env["media"], ledger, 1000.0)
    assert not orphan.exists()
    orphan = subdir(env, "d-dsh-" + "9" * 20)
    sweep_orphan_media(runtime, env["media"], ledger, 1000.0 + MEDIA_GC_INTERVAL_SECONDS - 1)
    assert orphan.exists(), "the sweep too runs once a day"
    sweep_orphan_media(runtime, env["media"], ledger, 1000.0 + MEDIA_GC_INTERVAL_SECONDS)
    assert not orphan.exists()


async def test_a_failing_cleanup_is_logged_and_stops_neither_the_loop_nor_the_spawn(env, capsys):
    (subdir(env, "d-" + ALPHA) / ".fail").write_text("")
    subdir(env, "d-" + BETA)
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA), row("beta", BETA)], env["log"])
    await run_until(runtime, env, lambda: {"start alpha", "start beta"} <= set(events(env)))
    assert events(env) == [f"gc-begin d-{ALPHA}", "start alpha",
                           f"gc-begin d-{BETA}", f"gc-end d-{BETA}", "start beta"]
    out = capsys.readouterr().out
    assert f"DSH host alpha: d-{ALPHA}: media cleanup exited 1: media-gc: cleanup failed: EACCES" in out


async def test_a_hung_cleanup_is_killed_and_the_spawn_goes_on(env, monkeypatch, capsys):
    monkeypatch.setattr(dsh_host, "MEDIA_GC_TIMEOUT_SECONDS", 0.3)
    (subdir(env, "d-" + ALPHA) / ".slow").write_text("3")
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA)], env["log"])
    await run_until(runtime, env, lambda: "start alpha" in events(env))
    await asyncio.sleep(3.5)
    assert events(env) == [f"gc-begin d-{ALPHA}", "start alpha"], "killed: it never finished"
    assert "media cleanup timed out" in capsys.readouterr().out


async def test_without_a_media_root_the_media_is_left_alone(env):
    orphan = subdir(env, "d-dsh-" + "9" * 20)
    subdir(env, "d-" + ALPHA)
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA)], env["log"])
    await run_until(runtime, env, lambda: "start alpha" in events(env), media=None, ticks_after=10)
    assert events(env) == ["start alpha"]
    assert orphan.exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node for the real media-gc.mjs")
async def test_the_real_media_gc_removes_a_stopped_directors_expired_files(env, monkeypatch):
    monkeypatch.undo()  # the real command, the real fallback_restart is not reached
    name = "d-" + ALPHA
    home = env["media"] / name
    (home / "2026-10").mkdir(parents=True)
    rows = []
    for i, age in enumerate((31, 1)):
        file = f"{name}/2026-10/f{i}.pdf"
        (env["media"] / file).write_text(f"%PDF-1.7 {i}")
        at = (datetime.now(timezone.utc) - timedelta(days=age)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        rows.append({"ref": f"m_{str(i) * 16}", "sha256": str(i) * 64, "mime": "application/pdf",
                     "size": 10, "audience": "project:alpha", "source": "generated",
                     "channel": "director", "messageRef": f"req-{i}", "index": 0, "file": file, "at": at})
    (home / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    runtime = FakeRuntime(env["root"], [row("alpha", ALPHA, enabled=0)], env["log"])
    await media_gc(runtime, row("alpha", ALPHA, enabled=0), env["media"], {}, time.monotonic())
    assert not (env["media"] / rows[0]["file"]).exists()
    assert (env["media"] / rows[1]["file"]).exists()
    kept = [json.loads(line)["ref"] for line in (home / "index.jsonl").read_text().splitlines()]
    assert kept == [rows[1]["ref"]]
