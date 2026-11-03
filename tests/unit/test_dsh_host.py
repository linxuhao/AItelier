"""Real host supervision against the backend's shared desired-state storage."""
import asyncio
import json
import sys
import time

import pytest

from core.dsh_driver import DshDrivers
from core.dsh_host import supervise
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService


async def eventually(check):
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


@pytest.fixture
def runtimes(tmp_path):
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    StateService(db, actor='fixture', project_read_trusted=True).store.create_project('alpha', 'Alpha')
    registry = DriverRegistry(db, 'host-fixture-pepper-' * 3)
    registry.seed('fixture-owner')
    root = tmp_path / 'drivers'; root.mkdir()
    (root / 'runtime.json').write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True,
                      command=[sys.executable, '-c', 'import time; time.sleep(90)'])
    return backend, host


async def test_backend_restart_preserves_host_process_and_dashboard_stop_really_stops_it(runtimes):
    backend, host = runtimes
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: backend.running('alpha'))
        proc = host.processes['alpha']
        host.update('alpha', phase='running', subagents_json='["child"]')
        assert backend.status()['projects']['alpha']['online'] == 1
        assert backend.status()['projects']['alpha']['running_subagents'] == 1
        assert backend.processes == {}
        env = host._environment(host.get('alpha'))
        assert env['DSH_PERMISSION_MODE'] == 'danger-full-access'
        assert env['AITELIER_DSH_EXECUTION'] == 'host'
        await backend.close()
        replacement = DshDrivers(backend.db, backend.registry, backend.root)
        await replacement.resume()
        assert replacement.processes == {}
        assert replacement.running('alpha') and proc.returncode is None
        assert replacement.get('alpha')['session_id'] == row['session_id']
        await replacement.stop('alpha')
        assert proc.returncode is not None
        assert not replacement.running('alpha')
        assert replacement.get('alpha')['phase'] == 'stopped'
        assert not replacement.get('alpha')['enabled']
        assert not (backend.root / row['driver_id'] / 'host-process.json').exists()
        await replacement.start('alpha', actor='fixture')
        await eventually(lambda: replacement.running('alpha'))
        assert host.processes['alpha'].pid != proc.pid
        assert replacement.get('alpha')['driver_id'] == row['driver_id']
    finally:
        stopped.set(); await task
    assert not backend.running('alpha')
    assert host.get('alpha')['enabled']  # Service restart resumes desired sessions.


async def test_stale_wrong_or_disabled_host_marker_never_counts_online(runtimes):
    backend, _ = runtimes
    row = await backend.start('alpha', actor='fixture')
    backend.update('alpha', phase='running', subagents_json='["stale"]')
    marker = backend.root / row['driver_id'] / 'host-process.json'
    def put(session, seen):
        marker.write_text(json.dumps({'pid': 123, 'session_id': session, 'seen_at': seen}))
    put(row['session_id'], time.time() - 20)
    assert backend.status()['projects']['alpha']['online'] == 0
    assert backend.status()['projects']['alpha']['running_subagents'] == 0
    put('other-session', time.time())
    assert not backend.running('alpha')
    put(row['session_id'], time.time())
    assert backend.running('alpha')
    backend.update('alpha', enabled=0)
    assert not backend.running('alpha')


async def test_second_host_controller_is_refused_without_stopping_first(runtimes):
    backend, host = runtimes
    await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: backend.running('alpha'))
        duplicate = DshDrivers(host.db, host.registry, host.root, command=host.command, host_controller=True)
        with pytest.raises(BlockingIOError):
            await supervise(duplicate, asyncio.Event(), interval=0.01)
        assert duplicate.processes == {}
        assert host.processes['alpha'].returncode is None
    finally:
        stopped.set(); await task
