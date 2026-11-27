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
    # Compose's default pair: the base, then the host-local override when it
    # exists. Derived from the project directory, never from the CLI helper.
    expected = ['-f', os.environ['OWNED_COMPOSE_FILE']]
    override = os.path.join(os.path.dirname(os.environ['OWNED_COMPOSE_FILE']),
                            'docker-compose.override.yml')
    if os.path.isfile(override):
        expected += ['-f', override]
    expected += ['ps', '-q', 'aitelier']
    if 'OWNED_COMPOSE_ROOT' in os.environ:
        expected = ['--project-directory', os.environ['OWNED_COMPOSE_ROOT']] + expected
    assert sys.argv[2:] == expected
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
    # A refused credential is named, not left as a bare HTTP status: the gate's
    # refusal quote this text, so the operator is told to fix the credential.
    monkeypatch.setenv('OWNED_HTTP_STATUS', '403')
    with pytest.raises(RuntimeError, match='active off-tunnel driver credential is required'):
        server._live_runtime_observation('http://127.0.0.1:4444')
    assert not operated_backend[1].exists()


def _runtime_db_modules_pulled_by_import():
    """Which of the runtime/DB modules a BARE interpreter loads for this path.

    Measured in a fresh interpreter, because in-process `sys.modules` cannot
    answer it: `tests/conftest.py` imports `api.main`/`api.dependencies`/
    `core.db_manager` for every session, so the set is already populated before
    any test body runs and an in-process check would only ever be red (or, if it
    were relaxed to ignore the conftest imports, would measure nothing at all).
    Run from the tree under test, so the import resolves against THIS source.
    """
    root = Path(__file__).resolve().parents[2]
    code = ("import json, sys, cli.server, core.deployment_quiescence;"
            "print(json.dumps(sorted({'api.dependencies', 'core.db_manager',"
            " 'skillflow.core'} & set(sys.modules))))")
    proc = subprocess.run([sys.executable, '-c', code], capture_output=True,
                          text=True, cwd=str(root), timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(not os.environ.get('EXPECTED_SOURCE'), reason=(
    'needs EXPECTED_SOURCE=<frozen Source checkout> run inside the owned CPU capsule '
    '(cgroup memory.max=2147483648 and pids.max=512 under /sys/fs/cgroup)'))
def test_source_imports_and_owned_container_caps():
    root = Path(os.environ['EXPECTED_SOURCE']).resolve()
    for module in (server, dq):
        assert Path(module.__file__).resolve().is_relative_to(root)
    assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '2147483648'
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '512'
    assert _runtime_db_modules_pulled_by_import() == []


@pytest.mark.parametrize('mismatch', [False, True])
@pytest.mark.parametrize('with_override', [False, True])
def test_explicit_canonical_compose_root_keeps_normal_identity_verification(
        operated_backend, tmp_path, monkeypatch, mismatch, with_override):
    facts, log = operated_backend
    canonical = tmp_path/'canonical-project'; canonical.mkdir()
    compose_file = canonical/'docker-compose.yml'; compose_file.write_text('services: {}\n')
    override_file = canonical/'docker-compose.override.yml'
    if with_override:
        override_file.write_text('services: {}\n')
    monkeypatch.setenv('OWNED_COMPOSE_ROOT', str(canonical))
    monkeypatch.setenv('OWNED_COMPOSE_FILE', str(compose_file))
    if mismatch:
        facts['runtime_identity']['pid_namespace'] = 'pid:[99999999]'
        facts['digest'] = dq._observation_digest(facts)
        with pytest.raises(ValueError, match='operated backend'):
            server._live_runtime_observation('http://127.0.0.1:4444', compose_root=canonical)
    else:
        assert server._live_runtime_observation('http://127.0.0.1:4444', compose_root=canonical) == facts
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    expected_files = ['-f', str(compose_file)]
    if with_override:
        expected_files += ['-f', str(override_file)]
    assert commands[0] == ['compose', '--project-directory', str(canonical),
                           *expected_files, 'ps', '-q', 'aitelier']
    assert commands[1] == ['inspect', '--format', '{{.State.Pid}}', 'a'*64]
    # The compose-capture CLI path is importable on its own: a bare interpreter
    # that imports it loads none of the runtime DB stack (a fresh interpreter,
    # since tests/conftest.py pre-imports them for the whole session).
    assert _runtime_db_modules_pulled_by_import() == []


@pytest.mark.parametrize('kind', ['relative', 'absent', 'no-compose'])
def test_explicit_compose_root_refuses_unusable_project(tmp_path, kind):
    root = tmp_path/'canonical'
    if kind == 'no-compose': root.mkdir()
    if kind == 'relative': root = Path('relative-project')
    with pytest.raises((ValueError, FileNotFoundError)):
        server._compose_files(root)


@pytest.mark.parametrize('with_override', [False, True])
def test_compose_up_executes_composes_default_file_pair(tmp_path, monkeypatch, with_override):
    """The defect end to end, through the real _compose -> subprocess.run path.

    On 2026-11-25 _compose_up executed `compose -f <base> up -d --no-deps
    aitelier` while a hand-run `docker compose up -d` in the same directory also
    loaded the gitignored override. The resulting container silently lost the
    tailnet publish, MCP allowed hosts and the loop-stack watchdog variable.
    Nothing here stubs _compose: the argv is what the fake executable actually
    received. The module default is pointed at a tmp project so no file is
    created in the real checkout.
    """
    project = (tmp_path / 'project').resolve()
    project.mkdir()
    compose_file = project / 'docker-compose.yml'
    compose_file.write_text('services: {}\n')
    override_file = project / 'docker-compose.override.yml'
    if with_override:
        override_file.write_text('services: {}\n')
    monkeypatch.setattr(server, '_PROJECT_ROOT', project)
    monkeypatch.setattr(server, '_COMPOSE_FILE', compose_file)

    log = tmp_path / 'up-argv.jsonl'
    executable = tmp_path / 'docker'
    executable.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
with open(os.environ['OWNED_UP_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
''')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('OWNED_UP_LOG', str(log))

    # Stub only what is not under test: filesystem provisioning, image
    # freshness and the post-up network diagnostic.
    monkeypatch.setattr(server, '_ensure_host_dirs', lambda: None)
    monkeypatch.setattr(server, '_image_exists', lambda: True)
    monkeypatch.setattr(server, '_image_deps_are_stale', lambda: False)
    monkeypatch.setattr(server, '_warn_if_edge_network_is_alone', lambda: None)

    server._compose_up(no_deps=True)

    expected = ['compose', '-f', str(compose_file)]
    if with_override:
        expected += ['-f', str(override_file)]
    expected += ['up', '-d', '--no-deps', 'aitelier']
    assert [json.loads(line) for line in log.read_text().splitlines()] == [expected]


def _owned_project(tmp_path, monkeypatch):
    """A tmp compose project plus a fake docker that records every argv it gets."""
    project = (tmp_path / 'project').resolve()
    project.mkdir()
    compose_file = project / 'docker-compose.yml'
    compose_file.write_text('services: {}\n')
    monkeypatch.setattr(server, '_PROJECT_ROOT', project)
    monkeypatch.setattr(server, '_COMPOSE_FILE', compose_file)
    log = tmp_path / 'argv.jsonl'
    executable = tmp_path / 'docker'
    executable.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
with open(os.environ['OWNED_UP_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
''')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('OWNED_UP_LOG', str(log))
    return project, compose_file, log


def _owned_commands(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _stub_local_repair(monkeypatch, clearance=None):
    """Only what is not under test: docker availability, the container probe, the
    deployment gate, health polling and first-run provisioning."""
    monkeypatch.setattr(server, '_DEFAULT_PORT', '4444')
    monkeypatch.setattr(server, '_docker_available', lambda: True)
    monkeypatch.setattr(server, '_container_running', lambda: False)
    monkeypatch.setattr(server, '_require_deployment_clearance',
                        lambda *a, **k: ({} if clearance is None else clearance))
    monkeypatch.setattr(server, '_wait_healthy', lambda client, max_wait: True)
    monkeypatch.setattr(server, '_ensure_host_dirs', lambda: None)
    monkeypatch.setattr(server, '_image_exists', lambda: True)
    monkeypatch.setattr(server, '_image_deps_are_stale', lambda: False)
    monkeypatch.setattr(server, '_warn_if_edge_network_is_alone', lambda: None)


@pytest.mark.parametrize('url', ['http://127.0.0.1:5555', 'http://localhost:5555',
                                'http://127.0.0.1:4445'])
def test_repair_refuses_an_endpoint_this_project_does_not_serve(tmp_path, monkeypatch, url):
    """iss-c986b2ede65846f1: the reuse answer is about the local container, the
    press is about THIS compose project, and ``--url`` separates the two.

    ``http://127.0.0.1:5555`` is another loopback deployment (a scratch backend
    started from this checkout and .env). Before the guard, the repair measured
    that deployment and then recreated this project's container.
    """
    project, compose_file, log = _owned_project(tmp_path, monkeypatch)
    _stub_local_repair(monkeypatch)
    monkeypatch.setattr(server, '_is_healthy', lambda client: pytest.fail(
        'a foreign endpoint was diagnosed by the press path'))
    with pytest.raises(RuntimeError) as refused:
        server.ensure_server_running(url, max_wait=1)
    assert url in str(refused.value)
    assert 'serves http://localhost:4444' in str(refused.value)
    assert _owned_commands(log) == []


@pytest.mark.parametrize('url', ['http://localhost:4444', 'http://127.0.0.1:4444'])
def test_repair_still_presses_the_endpoint_this_project_serves(tmp_path, monkeypatch, url):
    """Positive control: the guard is not a blanket refusal."""
    project, compose_file, log = _owned_project(tmp_path, monkeypatch)
    _stub_local_repair(monkeypatch)
    assert server.ensure_server_running(url, max_wait=1) is True
    assert _owned_commands(log) == [['compose', '-f', str(compose_file),
                                     'up', '-d', 'aitelier']]


@pytest.mark.parametrize('url', ['http://127.0.0.1:5555', 'http://localhost:4445'])
def test_restart_refuses_an_endpoint_this_project_does_not_serve(tmp_path, monkeypatch, url):
    project, compose_file, log = _owned_project(tmp_path, monkeypatch)
    _stub_local_repair(monkeypatch)
    with pytest.raises(RuntimeError) as refused:
        server.restart_server(url, max_wait=1)
    assert url in str(refused.value)
    assert _owned_commands(log) == []


def test_restart_still_restarts_the_endpoint_this_project_serves(tmp_path, monkeypatch):
    project, compose_file, log = _owned_project(tmp_path, monkeypatch)
    _stub_local_repair(monkeypatch)
    assert server.restart_server('http://localhost:4444', max_wait=1) is True
    assert _owned_commands(log) == [['compose', '-f', str(compose_file),
                                     'restart', 'aitelier']]


# iss-3c51d8e4dd1d4f8e: the guard decides BEFORE any compose command runs whether
# a --url names this project's endpoint. It used to check host and port but not
# the scheme, and accepted the IPv6 loopback literal although compose publishes
# 127.0.0.1 only. Drive the guard directly here, so both halves of that narrowing
# are visible together; the observation-path cases above are a different route.
def _guarded_url(scheme, host):
    return f'{scheme}://{host}:{server._DEFAULT_PORT}'


@pytest.mark.parametrize('scheme,host', [
    ('https', 'localhost'),
    ('https', '127.0.0.1'),
    ('http', '[::1]'),
], ids=['https-localhost', 'https-127', 'http-ipv6'])
def test_local_deployment_guard_refuses_other_schemes_and_ipv6_loopback(scheme, host):
    url = _guarded_url(scheme, host)
    with pytest.raises(RuntimeError) as refused:
        server._require_local_deployment_url(url, action='start or repair')
    assert url in str(refused.value)
    assert f'serves http://localhost:{server._DEFAULT_PORT}' in str(refused.value)


@pytest.mark.parametrize('build_url', [
    lambda: _guarded_url('http', 'localhost'),
    lambda: _guarded_url('http', '127.0.0.1'),
    lambda: server._DEFAULT_URL,
], ids=['http-localhost', 'http-127', 'module-default-url'])
def test_local_deployment_guard_accepts_this_checkouts_http_endpoint(build_url):
    server._require_local_deployment_url(build_url(), action='start or repair')
