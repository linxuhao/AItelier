"""Step 24.1a: executing a due fallback-restart marker (design-steward 7.8.10a, tests
7.8.18 "后备重启的执行"): by the DSH host's supervision loop, or by the backend itself
in non-host mode; a spawn deletes the markers; a restart never writes `enabled`."""
import asyncio
import json
import sys
from datetime import timedelta

import pytest

from core import state_steward_connections as sc
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers, fallback_restart
from core.dsh_host import supervise
from core.state_database import StateDatabase
from core.state_service import StateService

SLEEPER = [sys.executable, '-c', 'import time; time.sleep(90)']


async def eventually(check, seconds=5):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture
def db(tmp_path):
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    StateService(db, actor='fixture', project_read_trusted=True).store.create_project('alpha', 'Alpha')
    return db


def _registry(db):
    registry = DriverRegistry(db, 'restart-fixture-pepper-' * 3)
    registry.seed('fixture-owner')
    return registry


def mark(db, driver_id, reason='tools_stream_down', due_in=timedelta(seconds=-1), digest='h' * 64):
    sc.ensure_schema(db)
    with db.get_connection() as conn:
        conn.execute('INSERT INTO state_steward_restart_due VALUES(?,?,?,?,?)',
                     (driver_id, reason, digest, sc._iso(sc._utcnow() + due_in), sc._iso(sc._utcnow())))
        conn.commit()


def markers(db):
    with db.get_connection() as conn:
        return [tuple(r) for r in conn.execute('SELECT driver_id, reason FROM state_steward_restart_due')]


def done_rows(db):
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM state_steward_restart_done ORDER BY id')]


def presence_rows(db, driver_id):
    with db.get_connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'driver_presence_declarations' not in tables:
            return []
        return [dict(r) for r in conn.execute('SELECT * FROM driver_presence_declarations WHERE driver_id=?', (driver_id,))]


@pytest.fixture
def host(db, tmp_path):
    registry = _registry(db)
    root = tmp_path / 'drivers'; root.mkdir()
    (root / 'runtime.json').write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True, command=SLEEPER)
    return backend, host


async def test_the_supervisor_restarts_once_and_keeps_the_row_enabled(host, db):
    backend, runtime = host
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(runtime, stopped, interval=0.01))
    try:
        await eventually(lambda: 'alpha' in runtime.processes and runtime.running('alpha'))
        first = runtime.processes['alpha']
        runtime.update('alpha', phase='waiting')
        mark(db, row['driver_id'])
        mark(db, row['driver_id'], reason='tools_resync:' + 'h' * 64, due_in=timedelta(seconds=300))
        await eventually(lambda: first.returncode is not None, 10)
        await eventually(lambda: runtime.processes.get('alpha') is not None
                         and runtime.processes['alpha'] is not first, 20)
        assert markers(db) == []  # every marker went, the not-yet-due one too
        [done] = done_rows(db)
        assert {r['reason'] for r in json.loads(done['reasons_json'])} == {'tools_stream_down', 'tools_resync:' + 'h' * 64}
        assert done['error'] is None
        current = runtime.get('alpha')
        assert current['enabled'] == 1
        assert not (runtime.root / row['driver_id'] / 'startup-failures.json').exists()
        assert presence_rows(db, row['driver_id']) == []  # a restart is no offline declaration
    finally:
        stopped.set(); await task


async def test_a_turn_holds_the_restart_back_at_most_five_minutes(host, db):
    backend, runtime = host
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(runtime, stopped, interval=0.01))
    try:
        await eventually(lambda: runtime.running('alpha'))
        first = runtime.processes['alpha']
        runtime.update('alpha', phase='running')
        mark(db, row['driver_id'])
        await asyncio.sleep(0.3)
        assert first.returncode is None and markers(db)  # a turn is running: it waits
        with db.get_connection() as conn:
            conn.execute('UPDATE state_steward_restart_due SET due_at=?', (sc._iso(sc._utcnow() - timedelta(minutes=6)),))
            conn.commit()
        await eventually(lambda: first.returncode is not None, 10)
        assert markers(db) == [] and len(done_rows(db)) == 1
    finally:
        stopped.set(); await task


class FakeRuntime:
    """The executor's view of a runtime: a live child or not, a stop and a start."""

    def __init__(self, db, row, alive=True, stop_error=None, start_error=None):
        self.db, self.row, self.alive = db, row, alive
        self.stop_error, self.start_error = stop_error, start_error
        self.calls = []

    def running(self, project):
        return self.alive

    async def stop(self, project, *, preserve_enabled=False):
        self.calls.append(('stop', preserve_enabled))
        if self.stop_error:
            raise self.stop_error

    async def start(self, project, *, actor):
        self.calls.append(('start', actor))
        if self.start_error:
            raise self.start_error


@pytest.fixture
def driver(db, tmp_path):
    runtime = DshDrivers(db, _registry(db), tmp_path / 'drivers', command=SLEEPER)
    return runtime._provision('alpha', 'aitelier', 'driver', 'fixture')


def _row(db, project='alpha'):
    with db.get_connection() as conn:
        return dict(conn.execute('SELECT * FROM dsh_drivers WHERE project_id=?', (project,)).fetchone())


async def test_no_live_child_no_restart(db, driver):
    mark(db, driver['driver_id'])
    fake = FakeRuntime(db, driver, alive=False)
    assert await fallback_restart(fake, {**_row(db), 'enabled': 1}, then_start=True) is False
    assert fake.calls == [] and markers(db) and done_rows(db) == []


async def test_nothing_due_nothing_done(db, driver):
    mark(db, driver['driver_id'], due_in=timedelta(seconds=60))
    fake = FakeRuntime(db, driver)
    assert await fallback_restart(fake, _row(db), then_start=True) is False
    assert fake.calls == [] and markers(db)


async def test_an_owner_stop_before_the_transaction_only_drops_the_markers(db, driver):
    mark(db, driver['driver_id'])
    with db.get_connection() as conn:
        conn.execute('UPDATE dsh_drivers SET enabled=0 WHERE project_id=?', ('alpha',))
        conn.commit()
    fake = FakeRuntime(db, driver)
    assert await fallback_restart(fake, {**_row(db), 'enabled': 1}, then_start=True) is False
    assert fake.calls == [] and markers(db) == [] and done_rows(db) == []


async def test_non_host_mode_stops_then_starts_as_recovery(db, driver):
    mark(db, driver['driver_id'])
    with db.get_connection() as conn:
        conn.execute('UPDATE dsh_drivers SET enabled=1, phase=? WHERE project_id=?', ('failed', 'alpha'))
        conn.commit()
    fake = FakeRuntime(db, driver)
    assert await fallback_restart(fake, _row(db), then_start=True) is True  # phase failed with a live child: restarted
    assert fake.calls == [('stop', True), ('start', 'dsh-runtime-recovery')]
    assert markers(db) == [] and len(done_rows(db)) == 1


async def test_a_failed_stop_or_start_is_recorded_as_a_code(db, driver):
    with db.get_connection() as conn:
        conn.execute('UPDATE dsh_drivers SET enabled=1 WHERE project_id=?', ('alpha',))
        conn.commit()
    mark(db, driver['driver_id'])
    fake = FakeRuntime(db, driver, stop_error=OSError('disk on fire /secret/path'))
    assert await fallback_restart(fake, _row(db), then_start=True) is False
    assert done_rows(db)[-1]['error'] == 'OSError'
    mark(db, driver['driver_id'], digest='g' * 64)
    fake = FakeRuntime(db, driver, start_error=ValueError('nope'))
    assert await fallback_restart(fake, _row(db), then_start=True) is True
    assert done_rows(db)[-1]['error'] == 'ValueError'
    # The gateway's check tells the owner once per failed restart.
    with StateService(db, actor='fixture', project_read_trusted=True).store.transaction(write=True) as conn:
        drivers = [{'driver_id': driver['driver_id'], 'enabled': True, 'running': True, 'phase': 'waiting',
                    'has_stream': True}]
        sc.check_stream_markers(conn, drivers)
        sc.check_stream_markers(conn, drivers)
    assert all(r['noticed_at'] for r in done_rows(db))


async def test_a_spawn_deletes_the_markers_in_its_starting_write(db, tmp_path):
    runtime = DshDrivers(db, _registry(db), tmp_path / 'drivers', command=SLEEPER)
    # A fresh database without the tables (the host before the backend): no error.
    await runtime.start('alpha', actor='fixture')
    await runtime.stop('alpha')
    row = runtime.get('alpha')
    mark(db, row['driver_id'])
    mark(db, 'someone-else')
    await runtime.start('alpha', actor='fixture')
    try:
        assert markers(db) == [('someone-else', 'tools_stream_down')]
    finally:
        await runtime.stop('alpha')


async def test_a_restart_stop_never_writes_enabled(db, tmp_path):
    runtime = DshDrivers(db, _registry(db), tmp_path / 'drivers', command=SLEEPER)
    await runtime.start('alpha', actor='fixture')
    # The owner's stop lands between the executor's transaction and its stop.
    with db.get_connection() as conn:
        conn.execute('UPDATE dsh_drivers SET enabled=0 WHERE project_id=?', ('alpha',))
        conn.commit()
    await runtime.stop('alpha', preserve_enabled=True)
    assert runtime.get('alpha')['enabled'] == 0 and runtime.get('alpha')['phase'] == 'stopped'
    # A restart of an enabled driver keeps its 1; an owner stop writes 0.
    await runtime.start('alpha', actor='fixture')
    await runtime.stop('alpha', preserve_enabled=True)
    assert runtime.get('alpha')['enabled'] == 1
    await runtime.stop('alpha')
    assert runtime.get('alpha')['enabled'] == 0
