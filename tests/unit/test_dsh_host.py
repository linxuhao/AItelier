"""Real host supervision against the backend's shared desired-state storage."""
import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from core.dsh_driver import DshDrivers
from core.dsh_host import (EXIT_LOG_LINES, EXIT_RECORD_LIMIT, SERVING_PHASES, STARTUP_ERROR_LIMIT,
                           exit_cause, last_startup_error, record_startup_failure, runtime_log_tail,
                           supervise)
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService
from tests.support import state_template


async def eventually(check):
    # A preparation wait, never an assertion: it holds until a supervised child dies or the
    # host writes its journal, which is scheduled work rather than a fixed delay. 20 s is the
    # same tolerance `journal()` below uses (2026-11-24: one full-suite arm red at the old 5 s
    # bound while the same nodeid passed 3/3 alone in under 3 s on the same tree).
    async with asyncio.timeout(20):
        while not check():
            await asyncio.sleep(0.01)


async def for_at_most(seconds, check):
    """`eventually`, for the waits that are a real backoff rather than one tick."""
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.05)


async def journal(capsys, condition, seconds=20):
    """What the supervisor printed so far, drained until `condition` holds.

    `capsys.readouterr()` CLEARS the buffer, so one read at the end misses every
    line printed before it - and the line under test is printed exactly once, by
    the loop, never by anything the test calls.
    """
    seen = []

    def drain():
        chunk = capsys.readouterr().out
        if chunk:
            seen.append(chunk)
        return condition(''.join(seen))

    try:
        async with asyncio.timeout(seconds):
            while not drain():
                await asyncio.sleep(0.05)
    except TimeoutError:
        pass  # the assertion that follows reports what was actually printed
    return ''.join(seen)


@pytest.fixture
def runtimes(tmp_path, state_db_copy):
    db = StateDatabase(state_db_copy(state_template.state_registry_dsh, tmp_path / 'state.sqlite'))
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(record.exists)
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
    finally:
        stopped.set(); await task
    foreign = 'quota exhausted; the driver retries after 12:00'
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01))
    try:
        await eventually(lambda: recovered.running('alpha'))
        # The foreign text lands while this start is still undecided: the
        # serving row ends the streak without writing `last_error`, so a
        # diagnosis the supervisor did not make survives the clearing.
        backend.update('alpha', last_error=foreign)
        backend.update('alpha', phase='running')  # what the runtime reports when it serves
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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


async def test_a_start_that_serves_is_never_stamped(crashing):
    """The healthy path stays silent: a serving start needs no failure text."""
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(record.exists)  # alpha's one death is spent on this run
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
    finally:
        stopped.set(); await task
    # A fresh supervisor whose FIRST start of alpha reports a serving phase: it
    # must end with no failure text and no failed phase of the supervisor's making.
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01))
    try:
        await eventually(lambda: recovered.running('alpha'))
        backend.update('alpha', phase='running')  # what the runtime reports when it serves
        await eventually(lambda: not record.exists())  # that phase decided the start
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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


async def test_a_start_that_serves_clears_the_record(crashing):
    """A later start that reports a serving phase ends the streak: no stale failure."""
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(record.exists)
        # The failed start was stamped on the row too; a recovery has to undo
        # both halves of it, or the page keeps a warning nobody can clear.
        await eventually(lambda: backend.get('alpha')['phase'] == 'failed')
    finally:
        stopped.set(); await task
    # A new supervisor process, the way the fault gets fixed: the driver it
    # respawns now reports in, and the record of the old streak must go with it.
    recovered = DshDrivers(host.db, host.registry, host.root, host_controller=True,
                           command=host.command)
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(recovered, stopped, interval=0.01))
    try:
        await eventually(lambda: recovered.running('alpha'))
        backend.update('alpha', phase='running')  # what the runtime reports when it serves
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))

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

    Six seconds is long enough that an age-keyed rule called this a working
    start; the child never reports a serving phase, because it cannot load its
    session at all, so every death here is a failed start. Each spawn records its
    own wall-clock instant, so a test can measure the retry spacing the supervisor
    chose: 15 s for a start that has no counted failure behind it, 30 s after one.
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
    """Alive for 6 s is not "served": the age is the wrong question.

    An age rule at 12 s never decided this 6 s child: it died before the rule's
    threshold, so the death grew the streak. The shipped 5 s rule cleared the
    streak first, and the death that followed started a new streak at 1 again.
    """
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: len(spawn_times(spawns)) >= 1)
        counted_base = len(spawn_times(spawns))  # the spawn whose death is counted
        await for_at_most(20, lambda: record.exists())
        state = json.loads(record.read_text())
        assert state['attempts'] == 1, state
        assert state['driver_id'] == row['driver_id'] and state['project_id'] == 'alpha'
        assert 'session log is corrupt' in state['last_error'], state
        # `phase == 'failed'` is written by BOTH writers of this death: the runtime's
        # monitor stamps it with `DSH exited with code 1`, and the supervisor's streak
        # stamp can land a tick later (the loop puts the streak back over that notice,
        # see test_the_runtime_exit_notice_does_not_hide_the_streak). Waiting on the
        # phase therefore samples the row in between the two: on the full suite at
        # 381c227e20 this line read 'DSH exited with code 1; see runtime.log'. Wait
        # for the content this assertion is about, the way its sibling above does.
        await eventually(
            lambda: 'DSH: failed start 1 in a row: ' in (backend.get('alpha')['last_error'] or ''))
        driver_row = backend.get('alpha')
        assert driver_row['phase'] == 'failed', driver_row
        assert driver_row['last_error'].startswith('DSH: failed start 1 in a row: '), driver_row
        reported = [line for line in capsys.readouterr().out.splitlines()
                    if 'alpha' in line and 'died' in line]
        assert len(reported) == 1, reported
        assert 'failed start 1 in a row' in reported[0]
        # Anchor the pair to the counted death, not to a fixed index: whatever
        # the first spawn cost under load, the TWO spawns after it must be
        # separated by the backoff a counted failure buys (30 s, not 15 s).
        await for_at_most(90, lambda: len(spawn_times(spawns)) >= counted_base + 2)
        times = spawn_times(spawns)
        # Anchored to the counted death (the spawn at counted_base - 1) and read as a
        # DIFFERENCE of two gaps: the retry after one counted failure is 30 s, the one
        # before it was 15 s. A load shift moves both gaps by about the same amount,
        # while a streak that never grows schedules both at 15 s - that is the shape
        # this has to see, and one absolute interval cannot tell it from a slow tick.
        death, first_after, second_after = times[counted_base - 1:counted_base + 2]
        first_gap, second_gap = first_after - death, second_after - first_after
        assert second_gap - first_gap > 8, (first_gap, second_gap)
    finally:
        stopped.set(); await task


async def test_two_deaths_that_never_served_grow_one_streak(never_serves, capsys):
    """Two children that outlive 5 s without serving are starts 1 and 2, not two 1s.

    The journal witness had 46 lines with uptimes 33-119 s, every one reading
    "1 in a row": an age-keyed clear threw the streak away before the death that
    should have grown it. Here the child dies at 6 s, so the same shape fits in
    one test.
    """
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        assert backend.get('alpha')['phase'] not in SERVING_PHASES  # alive, not served
        await for_at_most(20, lambda: record.exists())
        first = json.loads(record.read_text())
        assert first['attempts'] == 1, first
        first_failed_at = first['first_failed_at']
        first_lines = [line for line in capsys.readouterr().out.splitlines()
                       if 'alpha' in line and 'died' in line]
        assert len(first_lines) == 1, first_lines
        uptime = re.search(r'died ([0-9.]+)s after start', first_lines[0])
        assert uptime and 5 < float(uptime.group(1)) < 120, first_lines[0]
        assert 'failed start 1 in a row' in first_lines[0], first_lines[0]
        # The second death comes after the backoff the first counted failure buys.
        text = await journal(capsys, lambda out: 'failed start 2 in a row' in out, seconds=45)
        second_lines = [line for line in text.splitlines()
                        if 'alpha' in line and 'died' in line]
        assert len(second_lines) == 1, text
        assert second_lines[0].startswith(
            f'DSH host alpha: {row["driver_id"]} died '), second_lines[0]
        assert 'failed start 2 in a row' in second_lines[0], second_lines[0]
        assert backend.get('alpha')['last_error'].startswith(
            'DSH: failed start 2 in a row: '), backend.get('alpha')
        state = json.loads(record.read_text())
        assert state['attempts'] == 2, state
        assert state['first_failed_at'] == first_failed_at, (first, state)
        # These two deaths are ~15 s apart (the first retry was scheduled at the
        # first spawn); the one-second stamps must still differ, so an unchanged
        # first_failed_at here can only mean the streak continued.
        assert state['last_failed_at'] > first['last_failed_at'], (first, state)
    finally:
        stopped.set(); await task


async def test_a_serving_phase_ends_the_streak_and_its_death_is_not_counted(
        never_serves, capsys):
    """One supervisor: service resets the COUNTER, not merely the failure file."""
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: len(spawn_times(spawns)) >= 1)
        await for_at_most(20, lambda: record.exists())
        assert json.loads(record.read_text())['attempts'] == 1
        await eventually(lambda: (backend.get('alpha')['last_error'] or '').startswith(
            'DSH: failed start 1 in a row: '))
        capsys.readouterr()  # the first death's line belongs to the old streak

        # The recovery spawn is made by THIS supervisor, so its `failures` still
        # holds the counted death; deleting the reset would change what follows.
        await eventually(lambda: len(spawn_times(spawns)) >= 2)
        proc = host.processes['alpha']
        assert record.exists()  # the streak is live when service is reported
        backend.update('alpha', phase='running')  # the runtime says THIS spawn served
        await eventually(lambda: not record.exists())
        # The file was still there when the phase was stamped: five seconds of
        # uptime cannot explain this clearing.
        assert time.time() - spawn_times(spawns)[1] < 4.5, time.time() - spawn_times(spawns)[1]
        assert not (backend.get('alpha')['last_error'] or '').startswith('DSH: failed start ')

        # The served child's exit must really have been recorded, not merely
        # inferred from an absence of "died" lines.
        text = await journal(capsys, lambda out: f'pid {proc.pid}' in out)
        lines = exit_lines(text, row['driver_id'])
        assert len(lines) == 1, text
        assert 'supervisor_stopped=false' in lines[0], lines[0]
        assert not [line for line in text.splitlines()
                    if 'alpha' in line and 'died' in line], text
        assert not record.exists()

        # The NEXT child dies with no serving phase: a reset counter makes this
        # "1 in a row"; a stale one would say "2 in a row".
        await for_at_most(60, lambda: len(spawn_times(spawns)) >= 3)
        assert backend.get('alpha')['phase'] not in SERVING_PHASES
        text = await journal(capsys, lambda out: 'failed start 1 in a row' in out, seconds=60)
        died = [line for line in text.splitlines() if 'alpha' in line and 'died' in line]
        assert len(died) == 1, text
        assert 'failed start 1 in a row' in died[0], died[0]
        assert json.loads(record.read_text())['attempts'] == 1
        await eventually(lambda: (backend.get('alpha')['last_error'] or '').startswith(
            'DSH: failed start 1 in a row: '))
    finally:
        stopped.set(); await task


async def test_a_serving_report_after_the_deadline_clears_the_record(crashing, monkeypatch):
    """A late /activity report is still the platform's evidence that the start worked."""
    # The real ceiling is 120 s; shortening it here keeps the late report in one
    # test while leaving the shipped constant untouched.
    monkeypatch.setattr('core.dsh_host.STARTUP_DECISION_SECONDS', 1.5)
    backend, host = crashing
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(record.exists)  # alpha's immediate death was counted
        assert json.loads(record.read_text())['attempts'] == 1
        await eventually(lambda: host.running('alpha'))  # this supervisor's recovery spawn
        await asyncio.sleep(2.0)  # past the deadline, with no serving phase reported
        assert host.processes['alpha'].returncode is None
        assert record.exists()  # the old streak is still on disk and on the page
        backend.update('alpha', last_error='DSH: failed start 1 in a row: late witness')
        # The report goes through the REAL `POST /activity` handler, because it is
        # that handler which sets the phase and `last_error` in one statement: a row
        # written here by hand would stay green with the route's text write deleted,
        # and could not say whether a late report still clears the stamp. Only the
        # route's inputs are replaced, the way test_dsh_driver_activity_stamp.py does
        # it: `manager`, `require_writer` and `request_identity` (which `owned` calls
        # directly, so it cannot be replaced as a dependency).
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from api import authz, dsh_driver_routers
        from core.drivers import Identity
        app = FastAPI()
        app.include_router(dsh_driver_routers.router)
        app.dependency_overrides[dsh_driver_routers.manager] = lambda: host
        app.dependency_overrides[authz.require_writer] = lambda: None
        monkeypatch.setattr(authz, 'request_identity',
                            lambda request: Identity('driver', row['driver_id'],
                                                     driver_id=row['driver_id']))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/dsh-drivers/alpha/activity',
                                         json={'phase': 'running', 'error': ''})
        assert response.status_code == 200, response.text
        await eventually(lambda: not record.exists())
        assert (backend.get('alpha')['last_error'] or '') == ''
        assert backend.get('alpha')['phase'] in SERVING_PHASES
    finally:
        stopped.set(); await task


async def test_a_death_after_the_driver_served_is_an_ordinary_restart(never_serves, capsys):
    """The same child, the same exit: served is what separates a boot failure from a kill.

    The child never reports a serving phase on its own, so the test writes the
    phase the runtime would have written for a real driver; it is that phase,
    not the child's age, that makes the later death an ordinary restart.
    """
    backend, host, spawns = never_serves
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
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


@pytest.fixture
def serves_then_exits(tmp_path):
    """`alpha`'s child serves, then exits on its own with code 7.

    The row's serving phase is written by the runtime, never by the child, so the
    test stamps it the way /activity would. Two seconds is long enough for the
    supervisor to read that phase, which DECIDES the start: it is the state a
    driver that had been serving for hours is in when it dies, with nothing about
    its start pending any more and nothing watching it.
    """
    script = tmp_path / 'child.py'
    script.write_text("import sys, time\ntime.sleep(2)\nsys.exit(7)\n")
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    StateService(db, actor='fixture', project_read_trusted=True).store.create_project(
        'alpha', 'Alpha')
    registry = DriverRegistry(db, 'host-fixture-pepper-' * 3)
    registry.seed('fixture-owner')
    root = tmp_path / 'drivers'; root.mkdir()
    (root / 'runtime.json').write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True,
                      command=[sys.executable, str(script)])
    return backend, host


def exit_lines(text, driver_id):
    """The lines about this driver's exit, as a reader of the journal finds them."""
    return [line for line in text.splitlines()
            if driver_id in line and 'supervisor_stopped=' in line]


async def test_a_serving_driver_that_exits_on_its_own_is_recorded(serves_then_exits, capsys):
    """A crash, an operator restart and a watchdog kill used to look identical later.

    The line in the journal is the artifact: it names the driver, the pid it had,
    how the child ended, how long it had been up and whether this supervisor is
    why it is gone. Here nobody asked, so it must say false - and the failed-start
    machinery must stay exactly where it was: no record file, no streak text.
    """
    backend, host = serves_then_exits
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        proc = host.processes['alpha']
        backend.update('alpha', phase='running')  # what the runtime reports when it serves
        text = await journal(capsys, lambda out: f'pid {proc.pid}' in out)
        lines = exit_lines(text, row['driver_id'])
        assert len(lines) == 1, text  # ONE line, once, for one exit
        line = lines[0]
        assert f'pid {proc.pid}' in line, line
        assert 'exited with exit code 7' in line, line
        assert 'supervisor_stopped=false' in line, line
        assert 'supervisor_stopped=true' not in line, line
        assert len(line) < 1000, line  # one bounded physical line for the journal
        uptime = re.search(r'after ([0-9.]+)s uptime', line)
        assert uptime and 1.0 <= float(uptime.group(1)) < 60, line
        assert proc.returncode == 7, proc.returncode
        # Nothing about a failed start moved: the same death is still an ordinary
        # restart, with no record, no streak and no supervisor text on the row.
        assert not record.exists()
        current = backend.get('alpha')
        assert not (current['last_error'] or '').startswith('DSH: failed start '), current
    finally:
        stopped.set(); await task
    # The shutdown of the loop itself adds no second line for this exit.
    text += capsys.readouterr().out
    assert len(exit_lines(text, row['driver_id'])) == 1, text


async def test_a_driver_this_supervisor_stopped_is_recorded_as_stopped(runtimes, capsys):
    """`stop()` for a disabled row is the one exit this loop can call its own.

    Same sink, same line, opposite flag: without it a shutdown the operator asked
    for would be indistinguishable from a crash in the journal, which is the whole
    reason the line exists.
    """
    backend, host = runtimes
    row = await backend.start('alpha', actor='fixture')
    record = host.root / row['driver_id'] / 'startup-failures.json'
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        proc = host.processes['alpha']
        backend.update('alpha', phase='running')  # it had served before the stop
        await asyncio.sleep(0.2)  # the loop reads that phase on its next pass
        backend.update('alpha', enabled=0)  # the dashboard's Stop
        text = await journal(capsys, lambda out: 'supervisor_stopped=true' in out)
        lines = exit_lines(text, row['driver_id'])
        assert len(lines) == 1, text
        line = lines[0]
        assert f'pid {proc.pid}' in line, line
        assert 'exited with signal 15' in line, line
        assert 'supervisor_stopped=true' in line, line
        assert 'supervisor_stopped=false' not in line, line
        assert len(line) < 1000, line
        uptime = re.search(r'after ([0-9.]+)s uptime', line)
        assert uptime and 0 <= float(uptime.group(1)) < 60, line
        await eventually(lambda: not host.running('alpha'))
        assert proc.returncode == -15, proc.returncode  # SIGTERM, not a crash
        stopped_row = backend.get('alpha')
        assert stopped_row['enabled'] == 0, stopped_row
        assert not (stopped_row['last_error'] or '').startswith('DSH: failed start '), stopped_row
        assert not record.exists()
    finally:
        stopped.set(); await task
    text += capsys.readouterr().out
    assert len(exit_lines(text, row['driver_id'])) == 1, text


@pytest.fixture
def serving_exit(tmp_path):
    """`alpha`'s child serves, then dies with exit code 1, leaving its own log.

    The same shape as `serves_then_exits`, with the code the two runtimes actually
    died with on 2026-11-16/17 (1, after 7.1 h and 7.8 h). Two seconds is long
    enough for the supervisor to read the serving phase - and for a test to write
    into, or remove, the runtime.log the dying driver leaves behind before the
    exit it records.
    """
    script = tmp_path / 'child.py'
    script.write_text("import sys, time\ntime.sleep(2)\nsys.exit(1)\n")
    db = StateDatabase(str(tmp_path / 'state.sqlite'))
    StateService(db, actor='fixture', project_read_trusted=True).store.create_project(
        'alpha', 'Alpha')
    registry = DriverRegistry(db, 'host-fixture-pepper-' * 3)
    registry.seed('fixture-owner')
    root = tmp_path / 'drivers'; root.mkdir()
    (root / 'runtime.json').write_text('{"mode":"host"}')
    backend = DshDrivers(db, registry, root)
    host = DshDrivers(db, registry, root, host_controller=True,
                      command=[sys.executable, str(script)])
    return backend, host


async def serving_cause(capsys, row, *, seconds=20):
    """The exit record's cause line for this driver, drained as it is printed."""
    marker = f'DSH host alpha: {row["driver_id"]} exit cause: '
    text = await journal(capsys, lambda out: marker in out, seconds)
    causes = [line for line in text.splitlines() if marker in line]
    return causes, text


async def test_a_serving_exit_record_carries_what_the_driver_said_before_dying(
        serving_exit, capsys):
    """The exit code was the whole record; the cause was on disk and never read.

    Measured 2026-11-16/17: two runtimes died with code 1 after 7.1 h and 7.8 h,
    each leaving one journal line naming nothing, so neither death was
    diagnosable afterwards. The host already redirects the child's streams into
    runtime.log, so the record now carries the tail of it - the child's own last
    words, through the same redaction and the same bound as the startup record.
    """
    backend, host = serving_exit
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        backend.update('alpha', phase='running')  # what the runtime reports when it serves
        await asyncio.sleep(0.1)  # the loop reads that phase before the child dies
        (host.root / row['driver_id'] / 'runtime.log').write_text(
            'loading the driver profile\n'
            'AItelier DSH exit: uncaught exception: Error: session log is corrupt '
            'token=deadbeef1234\n')
        causes, text = await serving_cause(capsys, row)
        assert len(causes) == 1, text  # ONE cause, once, for one exit
        cause = causes[0]
        assert 'uncaught exception: Error: session log is corrupt' in cause, cause
        # The same bounds as the startup text: the value goes, the label stays.
        assert 'deadbeef1234' not in cause, cause
        assert '<redacted>' in cause, cause
        # The status line is unchanged, and still there: the cause is added, not swapped in.
        assert 'exited with exit code 1' in text, text
        assert 'supervisor_stopped=false' in text, text
    finally:
        stopped.set(); await task


async def test_a_serving_exit_record_names_an_absent_log_instead_of_an_empty_field(
        serving_exit, capsys):
    """A driver that wrote nothing is a diagnosis, not a blank.

    The crashed incarnation on 2026-11-17 appended 0 bytes, which is exactly the
    case a record must spell out: "missing" says the file is gone (or was never
    written), "no output" says it exists and is empty, and neither is silence.
    The fixture has no session log either, so that absence is named the same way.
    """
    backend, host = serving_exit
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        backend.update('alpha', phase='running')
        await asyncio.sleep(0.1)
        (host.root / row['driver_id'] / 'runtime.log').unlink()  # the 0-byte death
        causes, text = await serving_cause(capsys, row)
        assert len(causes) == 1, text
        cause = causes[0]
        assert 'runtime.log missing' in cause, cause
        assert 'session log missing' in cause, cause
        assert 'exited with exit code 1' in text, text
    finally:
        stopped.set(); await task


async def test_a_serving_exit_record_bounds_a_log_far_longer_than_the_cap(
        serving_exit, capsys):
    """A journal entry is read by a person: the record is capped, not the log.

    The newest line is the one that names the cause, so the cut takes the OLD end:
    a log thousands of characters long still yields the marker the driver died on.
    """
    backend, host = serving_exit
    row = await backend.start('alpha', actor='fixture')
    stopped = asyncio.Event()
    task = asyncio.create_task(supervise(host, stopped, interval=0.01))
    try:
        await eventually(lambda: host.running('alpha'))
        backend.update('alpha', phase='running')
        await asyncio.sleep(0.1)
        (host.root / row['driver_id'] / 'runtime.log').write_text(
            ''.join(f'line {number} ' + 'x' * 120 + '\n' for number in range(400))
            + 'AItelier DSH exit: unhandled rejection: Error: the marker\n')
        causes, text = await serving_cause(capsys, row)
        assert len(causes) == 1, text
        cause = causes[0]
        assert 'the marker' in cause, cause  # the newest line survives the cut
        assert len(cause) <= EXIT_RECORD_LIMIT, len(cause)
        assert 'line 0 ' not in cause, cause  # the oldest end is what goes
        assert 'exited with exit code 1' in text, text
    finally:
        stopped.set(); await task


def test_the_exit_log_tail_keeps_the_newest_lines_and_names_an_absent_log(tmp_path):
    """Bounded twice: the last EXIT_LOG_LINES lines, each redacted and capped."""
    runtime = SimpleNamespace(root=tmp_path)
    directory = tmp_path / 'dsh-tail'; directory.mkdir()
    log = directory / 'runtime.log'
    assert runtime_log_tail(runtime, 'dsh-tail') == 'runtime.log missing'
    log.write_text('')
    assert runtime_log_tail(runtime, 'dsh-tail') == 'runtime.log has no output'
    log.write_text(''.join(f'line {number}\n' for number in range(100)))
    tail = runtime_log_tail(runtime, 'dsh-tail')
    lines = tail.split(' | ')
    assert len(lines) == EXIT_LOG_LINES, lines
    assert lines[-1] == 'line 99' and lines[0] == f'line {100 - EXIT_LOG_LINES}', lines
    # A log line longer than the record's own bound is still one bounded line.
    log.write_text('Error: ' + 'y' * 5000 + '\n')
    assert len(runtime_log_tail(runtime, 'dsh-tail')) <= STARTUP_ERROR_LIMIT


def test_the_exit_cause_names_a_log_written_before_this_process_started(tmp_path):
    """An appended log outlives its writer, so a stale one is named, not quoted.

    Measured 2026-11-26T09:32:16Z: the driver whose record was printed had written
    0 bytes in 116957 s, and the "exit cause" quoted a process two generations back
    as if those lines caused THIS exit - and writing nothing is the normal case for
    a director driver. The record says so in words instead, naming the gap between
    the predecessor's last write and this process's start - the status line above it
    carries the uptime, so the two numbers date the write and the wording claims
    only what `exit_cause` measured - and it quotes nothing from the file's body.

    Both branches are asserted, because "even the log this process did write is now
    called stale" is the same defect from the other side. Deterministic: the mtime
    is set and the start instant is derived from it, so there is no sleep and no
    dependence on the clock's granularity.
    """
    runtime = SimpleNamespace(root=tmp_path)
    row = {'driver_id': 'dsh-stale', 'project_id': 'alpha', 'session_id': 'session-1'}
    directory = tmp_path / 'dsh-stale'; directory.mkdir()
    log = directory / 'runtime.log'
    log.write_text('predecessor: loading the driver profile\n'
                   'predecessor: AItelier DSH exit: exit code 7\n')
    written = time.time() - 3600
    os.utime(log, (written, written))
    # A minute between the log's last write and this process's start: minutes, exact.
    cause = exit_cause(runtime, row, started_at=written + 60)
    assert 'unchanged by this process (last written 1m before it started)' in cause, cause
    assert 'predecessor: loading the driver profile' not in cause, cause
    assert 'predecessor: AItelier DSH exit: exit code 7' not in cause, cause
    assert len(cause) <= EXIT_RECORD_LIMIT, len(cause)
    # Hours once the gap is an hour or more, so a host restart can be read at a glance.
    assert 'unchanged by this process (last written 32.5h before it started)' in exit_cause(
        runtime, row, started_at=written + 32.5 * 3600)
    # The fresh branch: a log THIS process wrote is still quoted, and the stale
    # sentence is absent - the two answers must not be able to trade places.
    started_at = time.time() - 3600
    log.write_text('this process: started serving\n'
                   'this process: Error: the newest marker\n')
    cause = exit_cause(runtime, row, started_at=started_at)
    assert 'unchanged by this process' not in cause, cause
    assert 'this process: Error: the newest marker' in cause, cause
    assert len(cause) <= EXIT_RECORD_LIMIT, len(cause)


def test_the_exit_cause_carries_the_last_event_of_the_session_log(tmp_path):
    """The other half of what a dying runtime leaves: how far its session got.

    A session whose last event is a `job/registered` with no result, or a
    `step/end` with no `turn/end`, is the difference between "died mid-turn" and
    "died idle" - and it is the only thing a later reader can measure the lost
    work by. The log is zstd-framed JSONL, about one frame per event, so only the
    tail is decompressed: the 178 MB session measured on 2026-11-17 costs a
    record 0.05 s.
    """
    # Imported, not skipped: zstandard is a declared dependency (pyproject) and the
    # suite runs where it is installed, so its absence is a RED row, not a blind one.
    import zstandard
    runtime = SimpleNamespace(root=tmp_path)
    row = {'driver_id': 'dsh-session', 'project_id': 'alpha', 'session_id': 'session-1'}
    directory = tmp_path / 'dsh-session' / 'home' / 'sessions' / 'workspace' / 'session-1'
    directory.mkdir(parents=True)
    frame = zstandard.ZstdCompressor().compress
    (directory / 'session.v4.jsonl.zstd').write_bytes(
        frame(b'{"seq":1,"type":"turn/start"}\n')
        + frame(b'{"seq":2,"type":"job/registered","data":{"id":"b9ad"}}\n'))
    cause = exit_cause(runtime, row)
    assert 'runtime.log missing' in cause, cause
    assert 'session session-1 last event:' in cause, cause
    assert '"type":"job/registered"' in cause, cause
    assert 'turn/start' not in cause, cause  # the LAST event, not the first
    # The session the row names is the one read, never merely the newest one there.
    other = tmp_path / 'dsh-session' / 'home' / 'sessions' / 'workspace' / 'session-2'
    other.mkdir()
    (other / 'session.v4.jsonl.zstd').write_bytes(frame(b'{"seq":9,"type":"other"}\n'))
    assert '"type":"other"' not in exit_cause(runtime, row)
    # An empty log is named, not reported as an event.
    (directory / 'session.v4.jsonl.zstd').write_bytes(b'')
    assert 'session log has no events' in exit_cause(runtime, row)


# The runtime half of the same fix (`runtime.mjs`): the exit record can only carry
# the cause if the runtime prints one. Its module cannot be imported in isolation -
# it is a DSH plugin - so the witness spawns it the way the driver's node suite
# does, from `integrations/dsh-driver`, and reads what it left in one file:
# the host spawns it with `stdout=log, stderr=log` (core/dsh_driver.py), and a PIPE
# would misorder the report node writes through the stream against the diagnosis,
# which is written with writeSync.
DRIVER = pathlib.Path(__file__).resolve().parents[2] / 'integrations' / 'dsh-driver'


def runtime_exit(tmp_path, body):
    """Run the runtime's own `apply()` in a child node; return (exit code, its log)."""
    node = shutil.which('node')
    if node is None:
        pytest.fail(
            'missing capability: `node` is not on PATH, so the runtime\'s own exit line '
            'cannot be witnessed. This row expects the shipped image aitelier:latest '
            f'(node at /usr/local/bin/node); PATH={os.environ.get("PATH", "")!r}.')
    script = tmp_path / 'exit.mjs'
    script.write_text(
        f'import {{ apply }} from {json.dumps((DRIVER / "runtime.mjs").as_uri())};\n'
        'apply({ provide() {} });\n' + body + '\n')
    log = tmp_path / 'exit.log'
    with log.open('ab') as handle:
        result = subprocess.run([node, str(script)], cwd=DRIVER, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=handle, timeout=120)
    return result.returncode, log.read_text()


@pytest.mark.parametrize(('body', 'code', 'expected', 'stack'), [
    # An unhandled rejection is the shape node ends as an uncaught exception.
    ('Promise.reject(new Error("the session log is corrupt"));', 1,
     'AItelier DSH exit: unhandled rejection: Error: the session log is corrupt', True),
    ('setTimeout(() => { throw new Error("kaboom"); }, 0);', 1,
     'AItelier DSH exit: uncaught exception: Error: kaboom', True),
    # The shape that left 0 bytes in runtime.log on 2026-11-17: it says "stop"
    # and nothing else, so the line is the only trace the death could have.
    ('process.exit(5);', 5, 'AItelier DSH exit: exit code 5', False),
    ('setTimeout(() => {}, 0);', 0, None, False),  # a clean exit says nothing
])
def test_the_runtime_names_its_own_death_before_it_exits(tmp_path, body, code, expected,
                                                         stack):
    """Exit 1 with no cause anywhere is what this change exists to end.

    Two director runtimes died that way on 2026-11-16/17 (iss-10bb7c78d636411d) and
    the crashing incarnation appended nothing to runtime.log, so no traceback
    survived. Each of node's three non-zero endings now names itself in one line -
    and observing them changes nothing about how they end: the status stays what it
    was and node still prints its own stack.
    """
    status, text = runtime_exit(tmp_path, body)
    assert status == code, text
    named = [line for line in text.splitlines() if line.startswith('AItelier DSH exit: ')]
    assert named == ([] if expected is None else [expected]), text
    if stack:  # the monitor only observes: node's own report is still printed
        assert 'Node.js v' in text, text


def test_the_runtime_exit_line_is_bounded_and_credential_free(tmp_path):
    """One line, capped, with the value gone and the label kept - as the record is."""
    status, text = runtime_exit(
        tmp_path,
        'Promise.reject(new Error("token=deadbeef1234 " + "x".repeat(5000)'
        ' + "\\nsecond line"));')
    assert status == 1, text
    named = [line for line in text.splitlines() if line.startswith('AItelier DSH exit: ')]
    assert len(named) == 1, text
    line = named[0]
    assert len(line) <= 300, line
    assert 'token=<redacted>' in line, line
    assert 'deadbeef1234' not in line, line
    assert 'second line' not in line, line
