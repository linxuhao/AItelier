"""Normal run-owned reports retain full hashes; only the relay moves shorter."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

import pytest
from core.db_manager import DBManager
from core import run_isolation, repository_host_launcher as host, repository_executor as executor
from core import repository_executor_entry as entry
from aitelier import gate_admission as admission
from aitelier.tools.run_tests import impl as rt


@pytest.fixture
def owned(tmp_path, monkeypatch):
    # CPU controller's private scratch mounts occupy the NORMAL Linux home paths.
    home = Path('/home/linxuhao/.AItelier')
    monkeypatch.setenv('AITELIER_HOME', str(home))
    repo = tmp_path / 'source'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    db = DBManager(str(tmp_path / 'host.db'))
    db.ensure_project('owned', repo_type='existing', repo_path=str(repo))
    run_id = 'normal-owned-' + tmp_path.name
    row = run_isolation.ensure_for_run(db, run_id=run_id, project_id='owned', config_name='owned',
                                      repo_mode='code', requested_mode='direct')
    assert row['source_repo'] == str(repo)
    root = home / 'gate-reports' / hashlib.sha256(run_id.encode()).hexdigest()
    ticket = root / 'ticket'
    ticket.mkdir(parents=True)
    cfg = {'allowed_roots': [str(tmp_path)], 'aitelier_home': str(home),
           'state_db': str(tmp_path / 'host.db'),
           'executor_entry': str(Path(executor.__file__).with_name('repository_executor_entry.py')),
           'test_image': 'sha256:' + 'a' * 64, 'allowed_uids': [os.getuid()]}
    request = {'op': 'launch', 'run_id': run_id, 'repo': str(repo),
               'args': ['python3', '-m', 'pytest'], 'timeout': 5,
               'writable_dirs': [str(ticket)], 'report_dir': str(ticket)}
    return home, run_id, repo, ticket, cfg, request


@pytest.fixture
def upstream():
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"fixture":"real-bridge"}')
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    worker.join(2)


def test_normal_namespace_full_owner_tuple_host_validation_and_real_entry_bridge(owned, upstream, monkeypatch):
    home, run_id, repo, ticket, cfg, request = owned
    admission.relay_socket_namespace(home, create=True)
    path = admission.relay_socket_path(home, run_id, ticket.name)
    assert len(os.fsencode(path)) < 108
    assert path.parent == home / 'r' and len(path.stem) == 32
    assert ticket.parent.name == hashlib.sha256(run_id.encode()).hexdigest()
    relay = admission.AdmissionRelay(upstream, render_wait_sec=1, upstream_timeout=5)
    relay.start(unix_socket=str(path))
    try:
        request['relay_socket'] = str(path)
        fields = host.validate_request(cfg, request)
        assert fields['relay_socket'] == str(path)
        monkeypatch.setattr(host.shutil, 'which', lambda name: '/controlled/docker')
        command = host.docker_command(cfg, fields, 'owned-argv-only')
        assert f'{path}:{path}:ro' in command
        assert path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o777 == 0o600
        result = entry.run({'repo': str(repo), 'timeout': 5, 'relay_socket': str(path),
                            'args': [sys.executable, '-c',
                                     "import os,urllib.request;print(urllib.request.urlopen(os.environ['GODOT_BUILDER_URL']+'/health').read().decode())"]})
        assert result['returncode'] == 0 and 'real-bridge' in result['stdout']
        assert relay.snapshot()[0]['route'] == '/health'
    finally:
        relay.stop()
    assert not path.exists()


@pytest.mark.parametrize('other', ['run', 'ticket'])
def test_foreign_run_or_ticket_cannot_borrow_the_short_socket(owned, upstream, other):
    home, run_id, _, ticket, cfg, request = owned
    admission.relay_socket_namespace(home, create=True)
    foreign = admission.relay_socket_path(home, run_id + '-foreign' if other == 'run' else run_id,
                                           'foreign' if other == 'ticket' else ticket.name)
    relay = admission.AdmissionRelay(upstream, render_wait_sec=1, upstream_timeout=5)
    relay.start(unix_socket=str(foreign))
    try:
        request['relay_socket'] = str(foreign)
        with pytest.raises(host.LaunchRefused, match='exact owned'):
            host.validate_request(cfg, request)
    finally:
        relay.stop()


def test_default_normal_authored_gate_reaches_missing_transport_after_socket_bind(owned, monkeypatch, tmp_path):
    home, run_id, repo, *_ = owned
    script = repo / 'run_tests.sh'
    script.write_text('#!/bin/sh\npython3 -m pytest -q\n')
    script.chmod(0o755)
    monkeypatch.setenv(executor.HOST_LAUNCHER_ENV, str(tmp_path / 'missing.sock'))
    result = rt._run_repo_gate(repo, run_id=run_id)
    assert 'admission relay unavailable' not in result.get('output', '')
    assert result['passed'] is False and result['runner_error']
    assert 'host CPU launcher unavailable' in result['output']
    root = home / 'gate-reports' / hashlib.sha256(run_id.encode()).hexdigest()
    assert (root / result['ticket'] / 'admission.json').exists()
    assert not admission.relay_socket_path(home, run_id, result['ticket']).exists()


def test_overlong_namespace_is_typed_before_bind_and_preserves_existing_peer(tmp_path, monkeypatch):
    home = tmp_path / ('h' * 100)
    home.mkdir()
    monkeypatch.setenv('AITELIER_HOME', str(home))
    repo = tmp_path / 'source'
    repo.mkdir()
    script = repo / 'run_tests.sh'
    script.write_text('#!/bin/sh\nexit 0\n')
    script.chmod(0o755)
    peer_path = Path('/home/linxuhao/.AItelier/r/peer.sock')
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.bind(str(peer_path))
    peer.listen(1)
    original = peer_path.stat().st_ino
    try:
        result = rt._run_repo_gate(repo, run_id='long-owned')
        assert result['passed'] is False and result['runner_error']
        assert '107-byte filesystem budget' in result['output']
        assert peer_path.stat().st_ino == original
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(peer_path))
        connected, _ = peer.accept()
        connected.close()
        client.close()
    finally:
        peer.close()
        peer_path.unlink()


def test_no_listener_overwrite_and_cleanup_preserves_replacement_inode(owned, upstream):
    home, run_id, _, ticket, *_ = owned
    admission.relay_socket_namespace(home, create=True)
    path = admission.relay_socket_path(home, run_id, ticket.name)
    relay = admission.AdmissionRelay(upstream, render_wait_sec=1, upstream_timeout=5)
    relay.start(unix_socket=str(path))
    other = admission.AdmissionRelay(upstream, render_wait_sec=1, upstream_timeout=5)
    with pytest.raises(ValueError, match='never overwrite'):
        other.start(unix_socket=str(path))
    path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(path))
    replacement.listen(1)
    original = path.stat().st_ino
    try:
        relay.stop()
        assert path.exists() and path.stat().st_ino == original
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))
        connected, _ = replacement.accept()
        connected.close()
        client.close()
    finally:
        replacement.close()
        path.unlink()


def test_namespace_alias_or_public_permissions_never_gain_authority(tmp_path):
    home = tmp_path / 'h'
    home.mkdir()
    namespace = home / 'r'
    namespace.mkdir(mode=0o755)
    namespace.chmod(0o755)
    with pytest.raises(PermissionError):
        admission.relay_socket_namespace(home)
    namespace.rmdir()
    destination = tmp_path / 'destination'
    destination.mkdir(mode=0o700)
    namespace.symlink_to(destination)
    with pytest.raises(PermissionError):
        admission.relay_socket_namespace(home)
