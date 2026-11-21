"""Step 24.1c (design-steward 7.8.10a "起不来", director 434): a start the RUNTIME makes on
its own (crash recovery under the host supervisor, boot recovery, a fallback restart's
respawn) that cannot spawn is a failed start, never the owner's stop: the row keeps
enabled=1, shows phase=failed with the error, and the supervisor retries it with its
backoff. A user's own start that cannot spawn still answers that request with enabled=0.

The witness is a NON-steward driver first (the rule covers every driver the host loop
supervises), then the steward with the same injections."""
import asyncio
import json
import os
import sys
import time
import types

import pytest

from core import dsh_host
from core.drivers import DriverRegistry
from core.dsh_driver import DriverStampBusy, DshDrivers
from core.dsh_host import startup_failure_path, supervise
from core.state_database import StateDatabase
from core.state_service import StateService

HOME = "aitelier-home"


async def eventually(check, seconds=5):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture
def hosted(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "beta", HOME):
        service.store.create_project(project, project.title())
    registry = DriverRegistry(db, "recovery-start-pepper-" * 3)
    registry.seed("fixture-owner")
    root = tmp_path / "drivers"
    root.mkdir()
    (root / "runtime.json").write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True,
                      command=[sys.executable, "-c", "import time; time.sleep(90)"])
    return backend, host


class Clock:
    """The supervisor's monotonic clock, moved by the test (the event loop keeps the real one)."""

    def __init__(self):
        self.now = 1000.0

    def install(self, monkeypatch):
        proxy = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith("_")})
        proxy.monotonic = lambda: self.now
        monkeypatch.setattr(dsh_host, "time", proxy)


def inject(monkeypatch, host, kind, failures):
    """The next `failures` spawns of `host` fail: `_environment` raising ValueError, or
    opening runtime.log raising OSError (both inside start()'s except (OSError, ValueError))."""
    left = {"n": failures}
    if kind == "environment":
        real = host._environment

        def environment(row):
            if left["n"] > 0:
                left["n"] -= 1
                raise ValueError("profile could not be written")
            return real(row)
        monkeypatch.setattr(host, "_environment", environment)
    else:
        real_open = os.open

        def opener(path, *args, **kwargs):
            if str(path).endswith("runtime.log") and left["n"] > 0:
                left["n"] -= 1
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)
        monkeypatch.setattr("core.dsh_driver.os.open", opener)
    return left


def attempts(host, row):
    path = startup_failure_path(host, row["driver_id"])
    return json.loads(path.read_text())["attempts"] if path.exists() else 0


@pytest.mark.parametrize("project", ["alpha", HOME])
@pytest.mark.parametrize("kind", ["environment", "open"])
async def test_a_recovery_start_that_cannot_spawn_keeps_the_driver_enabled_and_retries(hosted, monkeypatch,
                                                                                      project, kind):
    backend, host = hosted
    clock = Clock()
    clock.install(monkeypatch)
    row = await backend.start(project, actor="fixture")
    calls = []
    real_start = host.start

    async def counted(project_id, **kwargs):
        if project_id == project:
            calls.append(kwargs.get("actor"))
        return await real_start(project_id, **kwargs)
    monkeypatch.setattr(host, "start", counted)
    left = inject(monkeypatch, host, kind, 2)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: len(calls) == 1 and attempts(host, row) == 1)
        failed = backend.get(project)
        assert failed["enabled"] == 1 and failed["phase"] == "failed" and failed["last_error"], failed
        assert calls == ["dsh-host-recovery"]
        # The backoff: nothing before 15 s, one start at 15 s.
        clock.now += 14
        await asyncio.sleep(0.2)
        assert len(calls) == 1
        clock.now += 1
        await eventually(lambda: len(calls) == 2 and attempts(host, row) == 2)
        assert backend.get(project)["enabled"] == 1
        # Failed again: the next is 30 s later.
        clock.now += 29
        await asyncio.sleep(0.2)
        assert len(calls) == 2
        clock.now += 1
        await eventually(lambda: len(calls) == 3 and host.running(project))
        assert left["n"] == 0
        assert backend.get(project)["enabled"] == 1 and backend.get(project)["phase"] == "starting"
    finally:
        stopped.set()
        await task


async def test_the_other_driver_starts_while_one_cannot(hosted, monkeypatch):
    backend, host = hosted
    Clock().install(monkeypatch)
    await backend.start("alpha", actor="fixture")
    await backend.start("beta", actor="fixture")
    real = host._environment
    monkeypatch.setattr(host, "_environment", lambda row: (_ for _ in ()).throw(ValueError("broken"))
                        if row["project_id"] == "alpha" else real(row))
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running("beta") and backend.get("alpha")["phase"] == "failed")
        assert backend.get("alpha")["enabled"] == 1
    finally:
        stopped.set()
        await task


async def test_a_stamp_lock_refusal_is_raised_and_writes_nothing(hosted, monkeypatch):
    backend, host = hosted
    await backend.start("alpha", actor="fixture")
    before = dict(host.get("alpha"))
    real_update = host.update

    def busy(project_id, **fields):
        if fields.get("phase") == "starting":
            raise DriverStampBusy("state_write_busy: held", 2)
        return real_update(project_id, **fields)
    monkeypatch.setattr(host, "update", busy)
    with pytest.raises(DriverStampBusy):
        await host.start("alpha", actor="dsh-host-recovery")
    after = host.get("alpha")
    assert (after["enabled"], after["phase"], after["last_error"]) == (before["enabled"], before["phase"],
                                                                       before["last_error"])


async def test_a_users_own_start_that_cannot_spawn_still_answers_enabled_0(hosted, monkeypatch):
    _backend, host = hosted
    host._provision("alpha", "aitelier", "driver", "fixture")
    monkeypatch.setattr(host, "_environment", lambda row: (_ for _ in ()).throw(ValueError("broken")))
    with pytest.raises(ValueError):
        await host.start("alpha", actor="owner:o@example.com")
    row = host.get("alpha")
    assert row["enabled"] == 0 and row["phase"] == "failed" and "broken" in row["last_error"]


async def test_non_host_resume_keeps_a_failed_recovery_enabled_and_tries_again_next_resume(tmp_path, monkeypatch):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    service = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", "beta"):
        service.store.create_project(project, project.title())
    registry = DriverRegistry(db, "recovery-start-pepper-" * 3)
    registry.seed("fixture-owner")
    command = [sys.executable, "-c", "import time; time.sleep(90)"]
    first = DshDrivers(db, registry, tmp_path / "drivers", command=command)
    for project in ("alpha", "beta"):
        first._provision(project, "aitelier", "driver", "fixture")
        first.update(project, enabled=1, phase="running")
    real = first._environment
    monkeypatch.setattr(first, "_environment", lambda row: (_ for _ in ()).throw(ValueError("broken"))
                        if row["project_id"] == "alpha" else real(row))
    try:
        await first.resume()
        alpha = first.get("alpha")
        assert alpha["enabled"] == 1 and alpha["phase"] == "failed" and "broken" in alpha["last_error"]
        assert first.running("beta")
        monkeypatch.setattr(first, "_environment", real)
        await first.resume()
        assert first.running("alpha")
    finally:
        await first.stop("alpha")
        await first.stop("beta")
