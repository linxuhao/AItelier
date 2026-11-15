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


async def for_at_most(seconds, check):
    """`eventually`, for the waits that are a real backoff rather than one tick."""
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.05)


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
        # A start that raised is the same failed start as a child that died, and
        # it reaches the row the owner reads the same way.
        failed_row = backend.get('alpha')
        assert failed_row['phase'] == 'failed', failed_row
        assert failed_row['last_error'].startswith('DSH: failed start 1 in a row: '), failed_row
        assert 'database is locked' in failed_row['last_error'], failed_row
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


async def test_a_foreign_last_error_survives_a_recovery(crashing):
    """A recovery writes nothing on the row, so a foreign diagnosis survives it.

    `last_error` is shared: the runtime writes exit codes there, and an operator
    can too. The supervisor's own failed-start text is dropped by the next
    `start()` (which stamps `phase='starting', last_error=''`), so the loop has
    nothing to clear on recovery - and a write here is exactly the write that
    would erase a diagnosis the supervisor did not make.
    """
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
    finally:
        stopped.set(); await task
    foreign = 'quota exhausted; the driver retries after 12:00'
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(lambda: recovered.running('alpha'))
        # The foreign text lands while this start is still inside the window,
        # so the recovery branch is what reads it.
        backend.update('alpha', last_error=foreign)
        await eventually(lambda: not record.exists())
        await asyncio.sleep(0.2)
        current = backend.get('alpha')
        assert current['last_error'] == foreign, current
        assert current['phase'] != 'failed', current
        assert recovered.processes['alpha'].returncode is None
    finally:
        stopped.set(); await task


async def test_a_row_that_cannot_be_stamped_does_not_end_supervision(crashing, capsys, monkeypatch):
    """A row write that raises costs visibility, never the record, the line or the loop."""
    backend, host = crashing
    alpha = await backend.start('alpha', actor='fixture')
    beta = await backend.start('beta', actor='fixture')
    record = host.root / alpha['driver_id'] / 'startup-failures.json'

    def locked(project_id, **fields):
        # Only the supervisor's own stamp is unwritable; anything else on the row
        # (the runtime's exit-code diagnosis, a start) still writes normally.
        if project_id == 'alpha' and str(fields.get('last_error') or '').startswith(
                'DSH: failed start '):
            raise RuntimeError('database is locked')
        return real_update(project_id, **fields)

    real_update = host.update
    monkeypatch.setattr(host, 'update', locked)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)  # the on-disk record is still written
        assert json.loads(record.read_text())['attempts'] >= 1
        out = capsys.readouterr().out
        assert 'died' in out and 'failed start 1 in a row' in out, out  # the line is printed
        assert 'cannot stamp the failure' in out, out
        assert not task.done()
        # The retry and the marker still happened, and the other project's row was
        # still handled in the same iteration.
        await eventually(lambda: host.running('beta'))
        assert host.processes['beta'].returncode is None
        assert (host.root / beta['driver_id'] / 'host-process.json').exists()
    finally:
        stopped.set(); await task


async def test_a_start_that_outlives_the_window_is_never_stamped(crashing):
    """The healthy path stays silent: a good start needs no failure text."""
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)  # alpha's one death is spent on this run
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
    finally:
        stopped.set(); await task
    # A fresh supervisor whose FIRST start of alpha outlives the window: it must
    # end with no failure text and no failed phase of the supervisor's making.
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01, startup_window=0.3))
    try:
        await eventually(lambda: not record.exists())  # the window passed, survived
        await asyncio.sleep(0.4)
        current = backend.get('alpha')
        assert (current['last_error'] or '') == '', current
        assert current['phase'] != 'failed', current
        assert recovered.processes['alpha'].returncode is None
    finally:
        stopped.set(); await task


async def test_the_runtime_exit_notice_does_not_hide_the_streak(crashing):
    """`_observe` diagnoses the same death from a thread; the notice can land last.

    When it does, the row would show `DSH exited with code 1` instead of the
    streak, so the supervisor puts its own text back - over that notice only,
    never over a text somebody else wrote (the foreign test above).
    """
    backend, host = crashing
    alpha = await backend.start('alpha', actor='fixture')
    record = host.root / alpha['driver_id'] / 'startup-failures.json'
    streak = 'DSH: failed start 1 in a row: '
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=1))
    try:
        await eventually(record.exists)
        await eventually(lambda: (backend.get('alpha')['last_error'] or '').startswith(streak))
        # The monitor won the race this time; the sampler reads the row next tick.
        backend.update('alpha', last_error='DSH exited with code 1; see runtime.log')
        await eventually(lambda: (backend.get('alpha')['last_error'] or '').startswith(streak))
        current = backend.get('alpha')
        assert current['phase'] == 'failed', current
        assert 'session log is corrupt' in current['last_error'], current
    finally:
        stopped.set(); await task


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
        # The record is not the only sink, and not the one the owner reads: the
        # driver ROW is what the project page renders (`phase` + `last_error`),
        # so the same failed start has to land there or a driver that dies on
        # every start still looks merely stopped.
        await eventually(
            lambda: 'DSH: failed start 1 in a row: ' in (backend.get('alpha')['last_error'] or ''))
        await asyncio.sleep(0.2)  # it stays there, not just for a moment
        driver_row = backend.get('alpha')
        assert driver_row['phase'] == 'failed', driver_row
        assert driver_row['last_error'].startswith('DSH: failed start 1 in a row: '), driver_row
        assert 'session log is corrupt' in driver_row['last_error'], driver_row
        assert 'deadbeef1234' not in driver_row['last_error'], driver_row
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
        # The failed start was stamped on the row too; a recovery has to undo
        # both halves of it, or the page keeps a warning nobody can clear.
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
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
        recovered_row = backend.get('alpha')
        assert not (recovered_row['last_error'] or '').startswith('DSH: failed start '), recovered_row
        assert recovered_row['phase'] != 'failed', recovered_row
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


@pytest.fixture
def never_serves(tmp_path):
    """`alpha`'s child dies 6 s after EVERY spawn; `beta`'s stays up.

    Six seconds is past the 5 s startup window, so an age-keyed rule diagnosed
    nothing here; the child also never reports a serving phase, because it cannot
    load its session at all. Each spawn records its own wall-clock instant, so a
    test can measure the retry spacing the supervisor chose: 15 s for a start that
    has no counted failure behind it, 30 s after one.
    """
    spawns = tmp_path / 'alpha-spawns'
    script = tmp_path / 'child.py'
    script.write_text(
        "import os, sys, time\n"
        f"spawns = {str(spawns)!r}\n"
        "if os.environ['AITELIER_DSH_PROJECT'] == 'alpha':\n"
        "    with open(spawns, 'a') as handle:\n"
        "        handle.write(str(time.time()) + '\\n')\n"
        "    time.sleep(6)\n"
        "    print('Error: session log is corrupt: cannot load the session',"
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
    return backend, host, spawns


def spawn_times(path):
    if not path.exists():
        return []
    return [float(value) for value in path.read_text().split()]


async def test_a_death_without_ever_serving_is_a_failed_start_at_any_age(never_serves, capsys):
    """Alive past the 5 s window is not "served": the age is the wrong question.

    The same child that is a failed start with the window at 12 s produced nothing
    at the shipped 5 s, because surviving the window was treated as proof that the
    start worked and the death that followed was judged by nobody.
    """
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await for_at_most(20, lambda: record.exists())
        state = json.loads(record.read_text())
        assert state['attempts'] == 1, state
        assert state['driver_id'] == row['driver_id'] and state['project_id'] == 'alpha'
        assert 'session log is corrupt' in state['last_error'], state
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
        driver_row = backend.get('alpha')
        assert driver_row['last_error'].startswith('DSH: failed start 1 in a row: '), driver_row
        reported = [line for line in capsys.readouterr().out.splitlines()
                    if 'alpha' in line and 'died' in line]
        assert len(reported) == 1, reported
        assert 'failed start 1 in a row' in reported[0]
        # The retry a counted failure buys is the one after it: a spawn's first
        # retry is already scheduled when it starts, so the growth has to be read
        # between the second and the third spawn (30 s instead of 15 s).
        await for_at_most(90, lambda: len(spawn_times(spawns)) >= 3)
        second, third = spawn_times(spawns)[1:3]
        assert third - second > 25, third - second
    finally:
        stopped.set(); await task


async def test_a_death_after_the_driver_served_is_an_ordinary_restart(never_serves, capsys):
    """The same child, the same exit: served is what separates a boot failure from a kill.

    The window is longer than the child lives, so the death itself is what the
    supervisor judges - and the row reported a serving phase before it, which is
    exactly what the runtime writes when a driver really reports in.
    """
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01, startup_window=10))
    try:
        await eventually(lambda: host.running('alpha'))
        # Every spawn reports in before it dies, so every death here is a served
        # one: none may be counted and the retry stays at the first 15 s.
        for expected in (1, 2):
            backend.update('alpha', phase='running')
            await for_at_most(60, lambda: len(spawn_times(spawns)) > expected)
    finally:
        stopped.set(); await task
    assert not record.exists(), 'a served driver that was killed is not a failed start'
    out = capsys.readouterr().out
    assert not [line for line in out.splitlines() if 'alpha' in line and 'died' in line], out
    current = backend.get('alpha')
    assert not (current['last_error'] or '').startswith('DSH: failed start '), current
    second, third = spawn_times(spawns)[1:3]
    # Unchanged: a served death must not grow the retry past its first 15 s.
    assert third - second < 25, third - second
