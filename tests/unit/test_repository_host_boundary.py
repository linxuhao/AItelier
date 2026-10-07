"""Real Linux credentials, existing owner rows, socket transport and own children.

Docker authority is absent. Controlled children exercise launch/disconnect logic;
this cohort never claims a normal activated Docker facility.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from core import repository_host_launcher as host, repository_executor as executor
from aitelier.tools.run_tests import impl as rt


@pytest.fixture
def owned(tmp_path):
    roots = tmp_path / 'projects'
    a, b = roots / 'a', roots / 'b'
    home = tmp_path / 'home'
    for p in (a, b, home):
        p.mkdir(parents=True)
    db = home / 'aitelier.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE run_isolation(run_id TEXT,mode TEXT,worktree_path TEXT,source_repo TEXT,project_id TEXT)')
        conn.executemany('INSERT INTO run_isolation VALUES(?,?,?,?,?)',
                         [('run-a', 'worktree', str(a), str(a), 'a'),
                          ('run-b', 'worktree', str(b), str(b), 'b')])
    reports = home / 'gate-reports' / hashlib.sha256(b'run-a').hexdigest()
    ticket = reports / 'ticket-1'
    ticket.mkdir(parents=True)
    cfg = {'allowed_roots': [str(roots)], 'aitelier_home': str(home),
           'state_db': str(db), 'executor_entry': str(Path(executor.__file__).with_name('repository_executor_entry.py')),
           'test_image': 'sha256:' + 'a' * 64, 'allowed_uids': [os.getuid()]}
    request = {'op': 'launch', 'run_id': 'run-a', 'repo': str(a),
               'args': ['python3', '-m', 'pytest', '-q'], 'timeout': 1,
               'writable_dirs': [str(ticket)], 'report_dir': str(ticket)}
    return cfg, request, a, b, ticket


def test_real_linux_kernel_uid_and_existing_owned_binding(owned):
    cfg, req, a, _, ticket = owned
    left, right = socket.socketpair()
    try:
        assert host._peer_uid(left) == os.getuid()
    finally:
        left.close()
        right.close()
    fields = host.validate_request(cfg, req)
    assert fields['repo'] == str(a) and fields['writable_dirs'] == [str(ticket)]
    assert fields['run_id'] == 'run-a' and fields['project_id'] == 'a'


@pytest.mark.parametrize('malformation', ['repo-b', 'write-b', 'report-b', 'same-repo', 'ancestor',
                                          'child', 'foreign-ticket', 'symlink', 'missing-owner', 'image', 'relay-file'])
def test_owner_bindings_refuse_foreign_or_source_write_authority(owned, malformation):
    cfg, req, a, b, ticket = owned
    if malformation == 'repo-b':
        req['repo'] = str(b)
    elif malformation in ('write-b', 'report-b'):
        req['writable_dirs'] = [str(b)]
        if malformation == 'report-b':
            req['report_dir'] = str(b)
    elif malformation in ('same-repo', 'ancestor', 'child'):
        path = a if malformation == 'same-repo' else a.parent if malformation == 'ancestor' else a / 'out'
        path.mkdir(exist_ok=True)
        req['writable_dirs'] = [str(path)]
    elif malformation == 'foreign-ticket':
        foreign = Path(cfg['aitelier_home']) / 'gate-reports' / hashlib.sha256(b'run-b').hexdigest() / 'ticket'
        foreign.mkdir(parents=True)
        req['writable_dirs'] = [str(foreign)]
    elif malformation == 'symlink':
        alias = ticket.parent / 'alias'
        alias.symlink_to(ticket, target_is_directory=True)
        req['writable_dirs'] = [str(alias)]
    elif malformation == 'missing-owner':
        req['run_id'] = 'no-record'
    elif malformation == 'image':
        req['image'] = 'other:latest'
    else:
        relay = ticket / 'relay.sock'
        relay.write_text('not a socket')
        req['relay_socket'] = str(relay)
    with pytest.raises(host.LaunchRefused):
        host.validate_request(cfg, req)


@pytest.mark.parametrize('image', ['aitelier:latest', 'tests:2026.10', 'sha256:bad', 'x@sha256:' + 'z' * 64])
def test_actual_configuration_loader_refuses_mutable_or_invalid_images(owned, tmp_path, image):
    cfg, *_ = owned
    cfg['test_image'] = image
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg))
    with pytest.raises(host.LaunchRefused, match='immutable'):
        host.load_config(path)


def test_socket_handler_configured_uid_reaches_honest_missing_facility(owned):
    cfg, req, *_ = owned
    client, server = socket.socketpair()
    thread = threading.Thread(target=host._serve_connection, args=(server, cfg, threading.Semaphore(4)))
    thread.start()
    try:
        client.sendall(json.dumps(req).encode() + b'\n')
        client.settimeout(2)
        result = json.loads(client.recv(65536))
        assert result['ok'] is False
        assert 'Docker execution facility' in result['error'] and 'peer uid' not in result['error']
    finally:
        client.close()
        thread.join(3)
    assert not thread.is_alive()


def test_existing_unix_listener_is_never_unlinked_or_replaced(owned, tmp_path):
    cfg, *_ = owned
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    path = tmp_path / 'launcher.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    before = path.stat().st_ino
    try:
        with pytest.raises(host.LaunchRefused, match='never replace'):
            host.serve(path, config)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))
        accepted, _ = listener.accept()
        accepted.close()
        client.close()
        assert path.stat().st_ino == before
    finally:
        listener.close()


def test_four_actual_host_kernel_slots_refuse_fifth_and_release(owned):
    cfg, *_ = owned
    root = Path(cfg['aitelier_home']) / 'cpu-test-slots'
    slots = [executor._slot(root) for _ in range(4)]
    try:
        for slot in slots:
            slot.__enter__()
        with pytest.raises(executor.IsolationUnavailable, match='four'):
            with executor._slot(root):
                pytest.fail('fifth operation was admitted')
    finally:
        for slot in slots:
            slot.__exit__(None, None, None)
    with executor._slot(root):
        pass


def controlled_child(monkeypatch, code):
    calls = []
    original_run = subprocess.run
    def cleanup(args, **kwargs):
        if args[:3] == ['docker', 'rm', '-f']:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        return original_run(args, **kwargs)
    monkeypatch.setattr(host.subprocess, 'run', cleanup)
    monkeypatch.setattr(host, 'docker_command', lambda cfg, fields, name:
                        [sys.executable, '-c', code])
    return calls


def test_live_disconnect_promptly_stops_own_child_and_keeps_peer(owned, monkeypatch, tmp_path):
    cfg, req, *_ = owned
    fields = host.validate_request(cfg, req)
    started = tmp_path / 'started'
    calls = controlled_child(monkeypatch, 'from pathlib import Path;import time;'
                             f'Path({str(started)!r}).write_text("ready");time.sleep(60)')
    peer = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    client, server = socket.socketpair()
    result = []
    def worker():
        try:
            host._run_docker(cfg, fields, server)
        except host.LaunchRefused as exc:
            result.append(str(exc))
    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 2
    try:
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(.005)
        before = time.monotonic()
        client.close()
        thread.join(2)
        assert not thread.is_alive() and time.monotonic() - before < 2
        assert result == ['client disconnected during owned execution']
        assert len(calls) == 1 and calls[0][-1].startswith('aitelier-cpu-')
        assert peer.poll() is None
    finally:
        server.close()
        peer.kill()
        peer.wait()
        thread.join(2)


@pytest.mark.parametrize('rc', [0, 1])
def test_controlled_launch_preserves_real_result_rc_stdout_stderr(owned, monkeypatch, rc):
    cfg, req, *_ = owned
    fields = host.validate_request(cfg, req)
    result = {'returncode': rc, 'stdout': 'actual child stdout', 'stderr': 'actual child stderr', 'timed_out': False}
    calls = controlled_child(monkeypatch, 'import json;print(' + repr(json.dumps(result)) + ')')
    assert host._run_docker(cfg, fields) == result
    assert len(calls) == 1


def test_normal_client_roundtrip_threads_run_identity_without_local_fallback(owned, monkeypatch, tmp_path):
    cfg, req, a, _, ticket = owned
    path = tmp_path / 'client.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    def one():
        conn, _ = listener.accept()
        host._serve_connection(conn, cfg, threading.Semaphore(4))
    thread = threading.Thread(target=one)
    thread.start()
    try:
        with pytest.raises(executor.IsolationUnavailable, match='Docker execution facility'):
            executor._launch_via_host(str(path), a, req['args'], 1, run_id='run-a',
                                      writable_dirs=[ticket], report_dir=str(ticket))
    finally:
        listener.close()
        thread.join(3)
    assert not thread.is_alive()


def test_actual_run_tests_and_authored_gate_missing_transport_remain_nonpass(owned, monkeypatch, tmp_path):
    cfg, _, a, *_ = owned
    monkeypatch.setenv('AITELIER_HOME', cfg['aitelier_home'])
    monkeypatch.setenv(executor.HOST_LAUNCHER_ENV, str(tmp_path / 'absent.sock'))
    (a / 'test_no_fallback.py').write_text('def test_no_fallback():\n    assert False\n')
    out = tmp_path / 'out'
    rt.run_tests(project_root=str(a), out_dir=str(out), run_id='run-a', repo_gate=False)
    report = json.loads((out / 'test_report.json').read_text())
    assert report['passed'] is False and report['returncode'] == -1 and report['infrastructure_unavailable']
    gate = rt._run_node_cmd(a, ['bash', str(a / 'run_tests.sh')], 1,
                            env_overrides={'GATE_REPORT_DIR': str(tmp_path / 'report')}, run_id='run-a')
    assert gate['passed'] is False and gate['runner_error']


def test_normal_host_mode_has_one_admission_layer(owned, monkeypatch):
    cfg, _, a, *_ = owned
    monkeypatch.setenv(executor.HOST_LAUNCHER_ENV, '/configured.sock')
    def duplicate_slot(*args):
        pytest.fail('backend must not hold a host kernel slot while awaiting host admission')
    monkeypatch.setattr(executor, '_slot', duplicate_slot)
    monkeypatch.setattr(executor, '_launch_via_host', lambda *a, **kw: 'host route')
    assert executor.execute(a, ['python3', '-m', 'pytest'], 1, run_id='run-a') == 'host route'


def test_direct_mode_retains_kernel_admission(owned, monkeypatch):
    cfg, _, a, *_ = owned
    monkeypatch.delenv(executor.HOST_LAUNCHER_ENV, raising=False)
    monkeypatch.setenv('AITELIER_HOME', cfg['aitelier_home'])
    root = Path(cfg['aitelier_home']) / 'cpu-test-slots'
    slots = [executor._slot(root) for _ in range(4)]
    try:
        for slot in slots:
            slot.__enter__()
        with pytest.raises(executor.IsolationUnavailable):
            executor.execute(a, ['python3', '-m', 'pytest'], 1)
    finally:
        for slot in slots:
            slot.__exit__(None, None, None)


def test_four_simultaneous_real_socket_handlers_and_fifth_share_host_kernel_slots(owned, monkeypatch, tmp_path):
    cfg, req, *_ = owned
    cleanup = []
    commands = []
    real_run = subprocess.run
    def remove(args, **kwargs):
        if args[:3] == ['docker', 'rm', '-f']:
            cleanup.append(args[-1])
            return subprocess.CompletedProcess(args, 0)
        return real_run(args, **kwargs)
    monkeypatch.setattr(host.subprocess, 'run', remove)
    def child(cfg, fields, name):
        marker = tmp_path / name
        commands.append(name)
        data = json.dumps({'returncode': 0, 'stdout': name, 'stderr': ''})
        return [sys.executable, '-c', 'from pathlib import Path;import time;'
                f'Path({str(marker)!r}).write_text("ready");time.sleep(1);print({data!r})']
    monkeypatch.setattr(host, 'docker_command', child)
    clients = []
    threads = []
    try:
        for _ in range(4):
            client, server = socket.socketpair()
            worker = threading.Thread(target=host._serve_connection,
                                      args=(server, cfg, threading.Semaphore(4)))
            worker.start()
            client.sendall(json.dumps(req).encode() + b'\n')
            clients.append(client)
            threads.append(worker)
        deadline = time.monotonic() + 2
        while len(commands) != 4 or not all((tmp_path / n).exists() for n in commands):
            assert time.monotonic() < deadline
            time.sleep(.005)
        fifth, server = socket.socketpair()
        worker = threading.Thread(target=host._serve_connection,
                                  args=(server, cfg, threading.Semaphore(4)))
        worker.start()
        fifth.sendall(json.dumps(req).encode() + b'\n')
        fifth.settimeout(2)
        refusal = json.loads(fifth.recv(65536))
        fifth.close()
        worker.join(2)
        assert not refusal['ok'] and 'four CPU' in refusal['error']
        assert len(commands) == 4
        for client in clients:
            client.settimeout(3)
            result = json.loads(client.recv(65536))
            assert result['ok'] and result['result']['returncode'] == 0
        for worker in threads:
            worker.join(3)
            assert not worker.is_alive()
        assert sorted(cleanup) == sorted(commands)
    finally:
        for client in clients:
            client.close()
        for worker in threads:
            worker.join(3)
