"""Exercise the real compose wrapper with an owned executable, never a runtime DB."""
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cli import server
from core import deployment_quiescence as dq


@pytest.fixture
def operated_backend(tmp_path, monkeypatch):
    facts = {
        'schema_version': 1, 'observed_at': dq._now(),
        'runtime_identity': {
            'pid': os.getpid(),
            'pid_namespace': os.readlink('/proc/self/ns/pid'),
            'mount_namespace': os.readlink('/proc/self/ns/mnt'),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        },
        'runs': [], 'checkout_leases': [], 'write_admissions': [],
        'registered_external_owners': [], 'errors': [],
    }
    facts['digest'] = dq._observation_digest(facts)
    log = tmp_path / 'commands.jsonl'
    executable = tmp_path / 'docker'
    executable.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
with open(os.environ['OWNED_COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'compose':
    assert sys.argv[2:] == ['-f', os.environ['OWNED_COMPOSE_FILE'], 'ps', '-q', 'aitelier']
    mode = os.environ.get('OWNED_COMPOSE_MODE', 'cid')
    if mode == 'nonzero':
        print('a' * 64)
        sys.exit(7)
    if mode == 'empty': sys.exit(0)
    if mode == 'malformed': print('unavailable')
    elif mode == 'multiple': print('a' * 64 + '\\n' + 'b' * 64)
    elif mode == 'timeout': time.sleep(1)
    else: print('a' * 64)
elif sys.argv[1:] == ['inspect', '--format', '{{.State.Pid}}', 'a' * 64]:
    print(os.environ['OWNED_BACKEND_PID'])
else:
    raise AssertionError('unexpected command')
''')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('OWNED_COMMAND_LOG', str(log))
    monkeypatch.setenv('OWNED_COMPOSE_FILE', str(server._COMPOSE_FILE))
    monkeypatch.setenv('OWNED_BACKEND_PID', str(os.getpid()))
    monkeypatch.setenv('AITELIER_ADMIN_TOKEN', 'owned-test-token')
    client_type = httpx.Client

    def respond(request):
        assert str(request.url) == 'http://127.0.0.1:4444/api/admin/deployment-runtime-observation'
        assert request.method == 'POST'
        assert request.headers['X-AItelier-Admin-Token'] == 'owned-test-token'
        return httpx.Response(int(os.environ.get('OWNED_HTTP_STATUS', '200')), json=facts)

    monkeypatch.setattr(server.httpx, 'Client', lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(respond)))
    return facts, log


def test_real_compose_capture_matches_owned_process_namespace(operated_backend, monkeypatch, capsys):
    facts, log = operated_backend
    real_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return real_run(command, **kwargs)

    monkeypatch.setattr(server.subprocess, 'run', run)
    assert server._live_runtime_observation('http://127.0.0.1:4444') == facts
    assert len(log.read_text().splitlines()) == 2
    compose_kwargs = calls[0][1]
    assert compose_kwargs['capture_output'] is True
    assert compose_kwargs['text'] is True
    assert compose_kwargs['timeout'] == 15
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize('mode', ['nonzero', 'empty', 'malformed', 'multiple'])
def test_real_compose_failure_refuses_before_inspect(operated_backend, monkeypatch, mode):
    monkeypatch.setenv('OWNED_COMPOSE_MODE', mode)
    with pytest.raises(ValueError, match='container identity is unavailable'):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert len(operated_backend[1].read_text().splitlines()) == 1


@pytest.mark.parametrize('output', [None, b'a' * 64])
def test_real_subprocess_result_wrong_stdout_type_refuses(operated_backend, monkeypatch, output):
    real_run = subprocess.run

    def corrupt_capture(command, **kwargs):
        result = real_run(command, **kwargs)
        result.stdout = output  # Boundary corruption after the owned executable actually ran.
        return result

    monkeypatch.setattr(server.subprocess, 'run', corrupt_capture)
    with pytest.raises(ValueError, match='container identity is unavailable'):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert len(operated_backend[1].read_text().splitlines()) == 1


def test_real_compose_timeout_is_explicit(operated_backend, monkeypatch):
    monkeypatch.setenv('OWNED_COMPOSE_MODE', 'timeout')
    real_run = subprocess.run

    def shorten_timeout(command, **kwargs):
        assert kwargs['timeout'] == 15
        return real_run(command, **{**kwargs, 'timeout': 0.1})

    monkeypatch.setattr(server.subprocess, 'run', shorten_timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert len(operated_backend[1].read_text().splitlines()) == 1


@pytest.mark.parametrize('shape', ['schema', 'digest', 'stale', 'projection', 'boot'])
def test_runtime_facts_refuse_before_real_compose(operated_backend, shape):
    facts, log = operated_backend
    if shape == 'schema': facts['schema_version'] = True
    elif shape == 'digest': facts['digest'] = 'tampered'
    elif shape == 'stale': facts['observed_at'] = '2000-01-01T00:00:00+00:00'
    elif shape == 'projection': facts['transport_projection'] = True
    elif shape == 'boot': facts['runtime_identity']['boot_id'] = 'foreign-boot'
    if shape in {'stale', 'boot'}: facts['digest'] = dq._observation_digest(facts)
    with pytest.raises(ValueError):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert not log.exists()


def test_real_compose_namespace_mismatch_refuses(operated_backend):
    facts, log = operated_backend
    facts['runtime_identity']['pid_namespace'] = 'pid:[99999999]'
    facts['digest'] = dq._observation_digest(facts)
    with pytest.raises(ValueError, match='operated backend'):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert len(log.read_text().splitlines()) == 2


@pytest.mark.parametrize('url', ['https://127.0.0.1:4444', 'http://foreign.example:4444'])
def test_remote_authority_refuses_before_client_or_compose(operated_backend, monkeypatch, url):
    monkeypatch.setattr(server.httpx, 'Client', lambda **kwargs: pytest.fail('credential-bearing client created'))
    with pytest.raises(ValueError, match='local backend'):
        server._live_runtime_observation(url)
    assert not operated_backend[1].exists()


def test_untrusted_http_response_refuses_before_real_compose(operated_backend, monkeypatch):
    monkeypatch.setenv('OWNED_HTTP_STATUS', '403')
    with pytest.raises(httpx.HTTPStatusError):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert not operated_backend[1].exists()


def test_source_imports_and_owned_container_caps():
    root = Path(os.environ['EXPECTED_SOURCE']).resolve()
    for module in (server, dq):
        assert Path(module.__file__).resolve().is_relative_to(root)
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '512'
    assert not {'api.dependencies', 'core.db_manager', 'skillflow.core'} & set(sys.modules)
