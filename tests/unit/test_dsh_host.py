"""Real host supervision against the backend's shared desired-state storage."""
import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from core.dsh_driver import DshDrivers
from core.dsh_host import last_startup_error, record_startup_failure, supervise
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


async def test_one_project_failure_does_not_end_supervision(runtimes):
    """A raising start()/publish() must stay local, not take every driver down.

    The supervisor is the only thing keeping resident drivers alive: before this
    boundary, one project's exception propagated out of the loop and the process
    exited, marking EVERY driver offline at once.
    """
    backend, host = runtimes
    await backend.start('alpha', actor='fixture')
    calls = {'alpha': 0}
    real_start = host.start

    async def flaky_start(project_id, **kwargs):
        if project_id == 'alpha':
            calls['alpha'] += 1
            raise RuntimeError('database is locked')
        return await real_start(project_id, **kwargs)

    host.start = flaky_start
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: calls['alpha'] >= 1)
        # The loop is still running and still publishing, not exited.
        assert not task.done()
        # Backoff grows instead of hammering every interval.
        await asyncio.sleep(0.2)
        assert calls['alpha'] == 1, f'retried {calls["alpha"]} times despite backoff'
    finally:
        stopped.set()
        await task


@pytest.fixture
def crashing(tmp_path):
    """`alpha`'s child dies seconds after spawn (once); `beta`'s stays up.

    Both projects run the SAME command, as they do in a real installation: the
    child reads the project id the host already puts in its environment, so the
    failing driver is the same binary a healthy one runs. The error line is long
    and carries a token-shaped value, so a record made from it can be checked for
    both of the things that must not survive: unbounded text and a credential.
    """
    died = tmp_path / 'alpha-died-once'
    script = tmp_path / 'child.py'
    script.write_text(
        "import os, sys, time\n"
        f"flag = {str(died)!r}\n"
        "if os.environ['AITELIER_DSH_PROJECT'] == 'alpha' and not os.path.exists(flag):\n"
        "    open(flag, 'w').close()\n"
        "    print('Error: session log is corrupt token=deadbeef1234 ' + 'x' * 600,"
        " file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "time.sleep(90)\n")
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    service = StateService(db, actor='fixture', project_read_trusted=True)
    service.store.create_project('alpha', 'Alpha')
    service.store.create_project('beta', 'Beta')
    registry = DriverRegistry(db, 'host-fixture-pepper-' * 3)
    registry.seed('fixture-owner')
    root = tmp_path / 'drivers'; root.mkdir()
    (root / 'runtime.json').write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True,
                      command=[sys.executable, str(script)])
    return backend, host


def test_last_startup_error_is_the_first_line_of_the_last_attempt(tmp_path):
    """The log is appended to by every attempt; only the newest one is reported."""
    runtime = SimpleNamespace(root=tmp_path)
    directory = tmp_path / 'dsh-log'; directory.mkdir()
    log = directory / 'runtime.log'
    log.write_text('Error: an older attempt failed\n    at old\n' + 'starting\n' * 4000
                   + 'Error: session log is corrupt\n    at load\n')
    assert last_startup_error(runtime, 'dsh-log') == 'Error: session log is corrupt'
    # No error marker at all: the last thing the child said is all there is.
    log.write_text('starting\nsomething odd\n')
    assert last_startup_error(runtime, 'dsh-log') == 'something odd'
    # A log that cannot be read is a diagnosis, not an exception.
    log.unlink()
    assert last_startup_error(runtime, 'dsh-log').startswith('runtime.log unreadable')


def test_the_record_keeps_the_first_failure_of_the_streak_and_one_line(tmp_path):
    """The streak's start is not overwritten by its latest attempt."""
    runtime = SimpleNamespace(root=tmp_path)
    row = {'driver_id': 'dsh-record', 'project_id': 'alpha'}
    (tmp_path / 'dsh-record').mkdir()
    record = tmp_path / 'dsh-record' / 'startup-failures.json'
    record.write_text(json.dumps({'first_failed_at': '2026-01-01T00:00:00Z', 'attempts': 7}))
    record_startup_failure(runtime, row, 8, 'Error: first line\n    at a frame')
    state = json.loads(record.read_text())
    assert state['attempts'] == 8
    assert state['first_failed_at'] == '2026-01-01T00:00:00Z'
    assert state['last_failed_at'] > state['first_failed_at']
    assert state['last_error'] == 'Error: first line'  # one line, even from a dump
    assert len(state['last_error']) <= 500
    # A record that cannot be read is replaced, not a reason to stop counting.
    record.write_text('not json')
    record_startup_failure(runtime, row, 1, 'Error: again')
    assert json.loads(record.read_text())['attempts'] == 1


async def test_a_child_that_dies_after_spawn_is_a_failed_start(crashing):
    """`start()` returning is not success: the child must stay up to count."""
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)
        state = json.loads(record.read_text())
        assert state['driver_id'] == row['driver_id'] and state['project_id'] == 'alpha'
        assert state['attempts'] >= 1
        assert state['first_failed_at'] and state['last_failed_at']
        assert 'session log is corrupt' in state['last_error']
        assert 'deadbeef1234' not in state['last_error'], state['last_error']
        assert len(state['last_error']) <= 500
        assert 'healthy' not in record.read_text().lower()
    finally:
        stopped.set(); await task


async def test_a_start_that_stays_up_clears_the_record(crashing):
    """A later start that outlives the window ends the streak: no stale failure."""
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)
    finally:
        stopped.set(); await task
    # A new supervisor process, the way the fault gets fixed: the driver it
    # respawns now stays up, and the record of the old streak must go with it.
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(lambda: recovered.running('alpha'))
        await eventually(lambda: not record.exists())
        await asyncio.sleep(0.2)
        assert not record.exists()
        assert recovered.processes['alpha'].returncode is None
    finally:
        stopped.set(); await task


async def test_each_failed_start_is_printed_once_and_a_healthy_driver_prints_nothing(
        crashing, capsys):
    """The unit journal carries the failure; a running driver writes no line."""
    backend, host = crashing
    alpha = await backend.start('alpha', actor='fixture')
    await backend.start('beta', actor='fixture')
    record = host.root / alpha['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)
        # Many more iterations over a child that is already counted: the failure
        # is reported once per failed start, not once per pass of the loop.
        await asyncio.sleep(0.3)
        out = capsys.readouterr().out
        reported = [line for line in out.splitlines() if 'alpha' in line and 'died' in line]
        assert len(reported) == 1, out
        assert alpha['driver_id'] in reported[0]
        assert 'failed start 1 in a row' in reported[0]
        assert 'session log is corrupt' in reported[0]
        # The journal keeps what it is given: the same bounds as the record.
        assert 'deadbeef1234' not in reported[0]
        assert '<redacted>' in reported[0]
        assert len(reported[0]) < 1000
        assert not [line for line in out.splitlines() if 'beta' in line], out
        assert host.running('beta')
    finally:
        stopped.set(); await task


async def test_the_other_driver_keeps_being_supervised_while_one_fails(crashing):
    """One project's fast death must not cost the rest of the loop a pass."""
    backend, host = crashing
    alpha = await backend.start('alpha', actor='fixture')
    beta = await backend.start('beta', actor='fixture')
    record = host.root / alpha['driver_id'] / 'startup-failures.json'
    marker = host.root / beta['driver_id'] / 'host-process.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(lambda: record.exists() and marker.exists())
        pid = host.processes['beta'].pid
        await asyncio.sleep(0.3)
        assert not task.done()
        assert host.processes['beta'].pid == pid
        assert host.processes['beta'].returncode is None
        assert json.loads(marker.read_text())['pid'] == pid
        assert backend.running('beta')  # the marker the container reads
    finally:
        stopped.set(); await task


async def test_a_failure_record_that_cannot_be_written_does_not_end_supervision(
        crashing, capsys, monkeypatch):
    """A full disk costs one project's diagnosis, never the loop."""
    backend, host = crashing
    alpha = await backend.start('alpha', actor='fixture')
    beta = await backend.start('beta', actor='fixture')

    def full_disk(*args, **kwargs):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr('core.dsh_host.record_startup_failure', full_disk)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))

    def refusal_reported():
        return 'No space left on device' in capsys.readouterr().out

    try:
        await eventually(lambda: host.running('beta'))
        await eventually(refusal_reported)  # the failing write was reached
        assert not task.done()
        assert not (host.root / alpha['driver_id'] / 'startup-failures.json').exists()
        assert host.processes['beta'].returncode is None
        assert (host.root / beta['driver_id'] / 'host-process.json').exists()
    finally:
        stopped.set(); await task
