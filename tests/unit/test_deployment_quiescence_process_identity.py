"""Synthetic process identities must preserve unknown measurement blockers."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import deployment_quiescence as dq


def _probe(line):
    def runner(command):
        return SimpleNamespace(returncode=0, stdout='' if command[0] == 'docker' else line + '\n', stderr='')
    return runner


def _proc(tmp_path, monkeypatch, *, argv, executable='/usr/bin/tail'):
    root = tmp_path / 'proc'
    entry = root / '4321'
    entry.mkdir(parents=True)
    (entry / 'cmdline').write_bytes(b'\0'.join(word.encode() for word in argv) + b'\0')
    (entry / 'exe').symlink_to(executable)
    (entry / 'comm').write_text(Path(argv[0]).name + '\n')
    monkeypatch.setattr(dq, 'PROC_ROOT', root)
    return entry


def test_proven_native_tail_log_paths_are_data(tmp_path, monkeypatch):
    argv = ['tail', '-F', '/var/log/example/metrics/err.log', '/var/log/example/worker/out.log']
    _proc(tmp_path, monkeypatch, argv=argv)
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(argv)))
    assert owners == []
    assert errors == []


@pytest.mark.parametrize('failure', ['missing_comm', 'unreadable_comm', 'different_argv', 'partial_argv', 'invalid_utf8', 'foreign_comm'])
def test_unproven_tail_identity_stays_unknown(tmp_path, monkeypatch, failure):
    argv = ['tail', '-F', '/var/log/example/metrics/err.log']
    entry = _proc(tmp_path, monkeypatch, argv=argv)
    if failure in {'missing_comm', 'unreadable_comm', 'foreign_comm'}:
        (entry / 'comm').unlink()
    if failure == 'unreadable_comm':
        (entry / 'comm').mkdir()
    elif failure == 'foreign_comm':
        (entry / 'comm').write_text('opaque_launcher\n')
    elif failure == 'different_argv':
        (entry / 'cmdline').write_bytes(b'tail\0-F\0/another/metrics/log\0')
    elif failure == 'partial_argv':
        (entry / 'cmdline').write_bytes(b'tail\0-F\0/var/log/metrics')
    elif failure == 'invalid_utf8':
        (entry / 'cmdline').write_bytes(b'tail\0\xff\0')
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(argv)))
    assert owners[0]['active'] is True
    assert owners[0]['ownership'] == 'unregistered'
    assert 'unknown ownership' in errors[0]


@pytest.mark.parametrize('argv,executable', [
    (['tail', '-F', '/tmp/metrics.log', '--long-gate'], '/usr/bin/tail'),
    (['python', '-m', 'evaluator_worker', '--job', 'task'], '/usr/bin/python3'),
    (['python', '-c', 'run_evaluator_worker()'], '/usr/bin/python3'),
    (['python', '/tmp/eval_worker.py', '--repo', '/tmp/zvec-grep'], '/usr/bin/python3'),
    (['sh', '-c', 'python /tmp/grader_worker.py'], '/usr/bin/dash'),
    (['tail_worker', '-F', '/tmp/metrics.log'], '/usr/bin/tail'),
])
def test_code_and_opaque_measurement_signals_still_block(tmp_path, monkeypatch, argv, executable):
    entry = _proc(tmp_path, monkeypatch, argv=argv, executable=executable)
    # Other container/uid ownership never exempts an unknown measurement.
    (entry / 'cgroup').write_text('0::/system.slice/docker-' + 'a' * 64 + '.scope\n')
    (entry / 'status').write_text('Uid:\t0\t0\t0\t0\n')
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(argv)))
    assert owners[0]['active'] is True
    assert owners[0]['resource'] == 'external_measurement'
    assert owners[0]['ownership'] == 'unregistered'
    assert errors


def test_registered_measurement_remains_bound_to_owner(tmp_path, monkeypatch):
    argv = ['python', '-m', 'evaluator_worker', '--job', 'worker-exact']
    _proc(tmp_path, monkeypatch, argv=argv, executable='/usr/bin/python3')
    owners, errors = dq.external_owners(
        runner=_probe('4321 1 ' + ' '.join(argv)),
        registered_external_owners=[{'attempt_id': 'attempt-exact', 'external_id': 'worker-exact', 'status': 'active'}])
    assert owners[0]['active'] is True
    assert owners[0]['ownership'] == 'registered'
    assert owners[0]['attempt_id'] == 'attempt-exact'
    assert errors == []


def test_real_native_tail_is_distinguished_from_log_filename(tmp_path):
    log = tmp_path / 'metrics.log'
    log.write_text('synthetic fixture\n')
    process = subprocess.Popen(['/usr/bin/tail', '-F', str(log)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # A subprocess invocation supplies the exact argv; the real /proc
        # read supplies independent executable + command proof.
        line = f'{process.pid} {os.getpid()} /usr/bin/tail -F {log}'
        # Popen returning (and poll() being alive) does not prove exec has
        # published argv/comm. Wait for the same independent launch identity
        # required by the production guard before asserting paths are data.
        expected = [b"/usr/bin/tail", b"-F", os.fsencode(log)]
        entry = Path("/proc") / str(process.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                argv = (entry / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                comm = (entry / "comm").read_text().strip()
                if argv == expected and comm == "tail":
                    break
            except (FileNotFoundError, ProcessLookupError):
                pass
            time.sleep(0.01)
        else:
            pytest.fail("tail launch did not publish exact argv and comm")
        owners, errors = dq.external_owners(runner=_probe(line))
        assert owners == []
        assert errors == []
    finally:
        process.terminate()
        assert process.wait(timeout=10) == -15


@pytest.mark.parametrize("failure", ["empty_argv", "identity_changed"])
def test_launch_publication_race_remains_blocked(tmp_path, monkeypatch, failure):
    argv = ["/usr/bin/tail", "-F", "/tmp/metrics.log"]
    entry = _proc(tmp_path, monkeypatch, argv=argv)
    if failure == "empty_argv":
        (entry / "cmdline").write_bytes(b"")
    else:
        (entry / "cmdline").write_bytes(b"/usr/bin/tail\0-F\0/tmp/other.log\0")
    owners, errors = dq.external_owners(runner=_probe("4321 1 " + " ".join(argv)))
    assert owners[0]["active"] is True
    assert owners[0]["ownership"] == "unregistered"
    assert any("unknown ownership" in error for error in errors)


@pytest.mark.parametrize('launch', [
    ['python', '-m', 'gunicorn', 'example.web:app', '-k', 'example.CustomUvicornWorker'],
    ['python3', '-u', '-m', 'gunicorn', 'example.web:app', '--worker-class', 'example.CustomUvicornWorker'],
    ['python3', '/tmp/webapp.py', '--config', '/tmp/metrics/settings.json'],
])
def test_corroborated_python_launch_separates_argument_data(tmp_path, monkeypatch, launch):
    entry = _proc(tmp_path, monkeypatch, argv=launch, executable='/usr/bin/python3')
    # Root-owned /proc/exe may be unreadable. Readable argv + comm still
    # corroborate the bounded Python launch grammar, without exempting uid.
    (entry / 'exe').unlink()
    (entry / 'status').write_text('Uid:\t0\t0\t0\t0\n')
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(launch)))
    assert owners == []
    assert errors == []


def test_native_serialized_settings_are_data_with_executable_proof(tmp_path, monkeypatch):
    launch = ['/opt/native/1.2.3', '--settings', '{"review":"metrics render aitelier evaluator_worker"}']
    entry = _proc(tmp_path, monkeypatch, argv=launch, executable=launch[0])
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(launch)))
    assert owners == []
    assert errors == []
    (entry / 'exe').unlink()
    owners, errors = dq.external_owners(runner=_probe('4321 1 ' + ' '.join(launch)))
    assert owners[0]['active'] is True
    assert errors


def test_neutral_launcher_registered_identity_still_blocks(tmp_path, monkeypatch):
    launch = ['python', '-m', 'gunicorn', 'example.web:app', '-k', 'example.CustomWorker', '--owner', 'owner-exact']
    _proc(tmp_path, monkeypatch, argv=launch, executable='/usr/bin/python3')
    owners, errors = dq.external_owners(
        runner=_probe('4321 1 ' + ' '.join(launch)),
        registered_external_owners=[{'attempt_id': 'attempt-exact', 'external_id': 'owner-exact', 'status': 'active'}])
    assert owners[0]['active'] is True
    assert owners[0]['resource'] == 'external_measurement'
    assert owners[0]['ownership'] == 'registered'
    assert owners[0]['attempt_id'] == 'attempt-exact'
    assert errors == []


def test_missing_sidecar_ledger_still_blocks_without_creating_it(tmp_path):
    from tests.unit.test_deployment_quiescence import _db, _quiet_sf
    missing = tmp_path / 'home' / 'semantic-index-control' / 'control.sqlite3'
    observed = dq.measure(
        skillflow=_quiet_sf(), db=_db(tmp_path / 'state.sqlite3'), sidecar_db=missing,
        external_probe=lambda: [{'kind': 'docker', 'id': 'resident-index', 'name': 'aitelier-zg',
                                 'service': 'zvec-grep', 'active': False}])
    assert observed['quiescent'] is False
    assert any('shared sidecar ledger is missing' in error for error in observed['errors'])
    assert not missing.parent.exists()
