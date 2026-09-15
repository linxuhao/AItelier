"""Internal/transitive Compose execution requires live runtime authority."""
import contextvars
import fcntl
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from cli import server
from core import deployment_quiescence as dq
from core.deployment_lifecycle import LIFECYCLE_COMMANDS, unguarded_findings


@pytest.fixture
def permit(resource_authority, monkeypatch):
    class Quiet:
        def list_runs(self): return []
    observation = dq.measure(skillflow=Quiet(), ownership_dir=resource_authority.root,
                             command_runner=lambda argv: SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr('api.dependencies.get_db_manager', lambda: None)
    monkeypatch.setattr('api.dependencies.get_skillflow', Quiet)
    monkeypatch.setattr(dq, 'measure', lambda **kwargs: observation)
    assert server._ACTIVE_CLEARANCE.get() is None
    clearance = server._require_deployment_clearance('restart')
    try:
        yield clearance
    finally:
        try:
            server._finish_deployment(clearance, success=False, error=RuntimeError('fixture settled'))
        except (dq.DeploymentBlocked, ValueError):
            server._ACTIVE_CLEARANCE.set(None)
            if not clearance['_cutover_fence'].closed:
                clearance['_cutover_fence'].close()


@pytest.fixture
def no_docker(monkeypatch):
    calls = []
    monkeypatch.setattr(server, '_compose_env', dict)
    monkeypatch.setattr(server, '_compose_files', list)
    monkeypatch.setattr(server.subprocess, 'run', lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0))
    return calls


@pytest.mark.parametrize('verb', sorted(LIFECYCLE_COMMANDS | {'future-unknown'}))
def test_internal_compose_has_no_unguarded_effect(no_docker, verb):
    with pytest.raises(RuntimeError, match='authority'):
        server._compose(verb, 'aitelier')
    assert no_docker == []


def test_transitive_internal_up_refuses_before_preparation(no_docker, monkeypatch):
    prepared = []
    monkeypatch.setattr(server, '_ensure_host_dirs', lambda: prepared.append(True))
    def helper(): server._compose_up()
    def wrapper(): helper()
    with pytest.raises(RuntimeError, match='authority'):
        wrapper()
    assert not prepared and not no_docker


def test_live_permit_authorizes_supported_up(permit, no_docker):
    server._compose('up', '-d', *server._COMPOSE_SERVICES)
    assert len(no_docker) == 1
    assert no_docker[0][:3] == ['docker', 'compose', 'up']


@pytest.mark.parametrize('verb', sorted(LIFECYCLE_COMMANDS - {'up'}))
def test_permit_cannot_authorize_unsupported_capabilities(permit, no_docker, verb):
    with pytest.raises(RuntimeError, match='unsupported'):
        server._compose(verb, 'aitelier')
    assert no_docker == []


@pytest.mark.parametrize('args', [('ps',), ('config',), ('--dry-run', 'up')])
def test_readonly_and_dryrun_need_no_effect_permit(no_docker, args):
    server._compose(*args)
    assert len(no_docker) == 1


def test_closed_permit_cannot_be_reused(permit, no_docker):
    dq.release_cutover_fence(permit['_cutover_fence'])
    with pytest.raises(RuntimeError, match='no longer held'):
        server._compose('up')
    assert no_docker == []


def test_unlocked_open_fence_cannot_authorize(permit, no_docker):
    fcntl.flock(permit['_cutover_fence'].fileno(), fcntl.LOCK_UN)
    with pytest.raises(RuntimeError, match='lost its exclusive fence'):
        server._compose('up')
    assert no_docker == []


def test_journal_binding_cannot_be_changed(permit, no_docker):
    permit['event']['inventory_digest'] = 'a' * 64
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up')
    assert no_docker == []


def test_settled_journal_cannot_authorize_more_effects(permit, no_docker):
    dq.finalize(permit, success=True)
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up')
    assert no_docker == []


def test_context_copy_does_not_transfer_thread_ownership(permit, no_docker):
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(context.run, server._compose, 'up')
        with pytest.raises(RuntimeError, match='active deployment authority'):
            future.result(timeout=2)
    assert no_docker == []


@pytest.mark.parametrize('source', [
    'def unguarded_caller():\n _compose_up()',
    'def _compose_up():\n _compose("up")\ndef dangerous():\n _compose_up()',
    'class Evil:\n def restart_server(self):\n  _compose("up")',
    'def outer():\n def restart_server():\n  _compose("up")',
    'def restart_server():\n _compose("up")',
    'def _compose():\n subprocess.run(["docker", "compose", *args])',
])
def test_qualified_and_transitive_routes_cannot_borrow_authority(source):
    assert unguarded_findings('cli/server.py', source)
