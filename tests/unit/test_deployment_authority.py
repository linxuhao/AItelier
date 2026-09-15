"""Internal/transitive Compose execution requires live runtime authority."""
import copy
import fcntl
import pickle
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
    clearance = server._require_deployment_clearance('restart')
    try:
        yield clearance
    finally:
        try:
            server._finish_deployment(clearance, success=False, error=RuntimeError('fixture settled'))
        except (dq.DeploymentBlocked, ValueError):
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
    with pytest.raises(RuntimeError, match='authority|unsupported'):
        server._compose(verb, 'aitelier')
    assert no_docker == []


def test_transitive_internal_up_refuses_before_preparation(no_docker, monkeypatch):
    prepared = []
    monkeypatch.setattr(server, '_ensure_host_dirs', lambda: prepared.append(True))
    def helper(): server._compose_up(('up',))
    def wrapper(): helper()
    with pytest.raises(RuntimeError, match='authority'):
        wrapper()
    assert not prepared and not no_docker


def test_live_permit_authorizes_supported_up(permit, no_docker):
    args = ('up',)
    capability = server._mint_deployment_command(permit, args)
    server._compose(*args, capability=capability)
    assert len(no_docker) == 1
    assert no_docker[0][:2] == ['docker', 'compose']
    assert no_docker[0][-1] == 'up'


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
    capability = server._mint_deployment_command(permit, ('up',))
    dq.release_cutover_fence(permit['_cutover_fence'])
    with pytest.raises(RuntimeError, match='no longer held'):
        server._compose('up', capability=capability)
    assert no_docker == []


def test_unlocked_open_fence_cannot_authorize(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    fcntl.flock(permit['_cutover_fence'].fileno(), fcntl.LOCK_UN)
    with pytest.raises(RuntimeError, match='lost its exclusive fence'):
        server._compose('up', capability=capability)
    assert no_docker == []


def test_journal_binding_cannot_be_changed(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    permit['event']['inventory_digest'] = 'a' * 64
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up', capability=capability)
    assert no_docker == []


def test_settled_journal_cannot_authorize_more_effects(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    dq.finalize(permit, success=True)
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up', capability=capability)
    assert no_docker == []


def test_context_copy_does_not_transfer_thread_ownership(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(server._compose, 'up', capability=capability)
        with pytest.raises(RuntimeError, match='explicit deployment authority'):
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


@pytest.mark.parametrize('handle', [None, object(), {}, {'event': 'forged'}, 'token'])
def test_opaque_command_handles_cannot_be_forged(permit, no_docker, handle):
    with pytest.raises(RuntimeError, match='authority'):
        server._compose('up', capability=handle)
    assert not no_docker


def test_same_thread_callback_cannot_borrow_operation(permit, no_docker):
    callback = lambda: server._compose('up')
    with pytest.raises(RuntimeError, match='authority'):
        callback()
    assert not no_docker


def test_one_operation_mints_only_one_command(permit, no_docker):
    server._mint_deployment_command(permit, ('up',))
    with pytest.raises(RuntimeError, match='already minted'):
        server._mint_deployment_command(permit, ('up', '-d'))


@pytest.mark.parametrize('args', [
    ('up', '-d'),
    ('up', 'zvec-grep'),
    ('up', '-d', '--build', 'zvec-grep'),
])
def test_operation_refuses_alternate_partial_and_extra_commands(
        permit, no_docker, args):
    with pytest.raises(RuntimeError, match='fixed plan'):
        server._mint_deployment_command(permit, args)
    capability = server._mint_deployment_command(permit, 0)
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_operation_refuses_unplanned_index_without_spending_planned_command(
        permit, no_docker):
    with pytest.raises(RuntimeError, match='already minted'):
        server._mint_deployment_command(permit, 1)
    capability = server._mint_deployment_command(permit, 0)
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_explicit_receipt_copy_cannot_mint(permit, no_docker):
    with pytest.raises(RuntimeError, match='explicit deployment authority'):
        server._mint_deployment_command(dict(permit), ('up',))


@pytest.mark.parametrize('args', [('up', '-d'), ('restart',), ('ps',), ('--dry-run', 'up')])
def test_wrong_command_spends_the_capability(permit, no_docker, args):
    capability = server._mint_deployment_command(permit, ('up',))
    with pytest.raises(RuntimeError, match='different command'):
        server._compose(*args, capability=capability)
    with pytest.raises(RuntimeError, match='unconsumed'):
        server._compose('up', capability=capability)
    assert not no_docker


def test_command_cannot_be_replayed(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    server._compose('up', capability=capability)
    with pytest.raises(RuntimeError, match='unconsumed'):
        server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_dispatch_callback_cannot_reuse_explicit_capability(permit, no_docker, monkeypatch):
    capability = server._mint_deployment_command(permit, ('up',))
    def dispatch(argv, **kwargs):
        no_docker.append(argv)
        with pytest.raises(RuntimeError, match='unconsumed'):
            server._compose('up', capability=capability)
        with pytest.raises(RuntimeError, match='authority'):
            server._compose('up')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(server.subprocess, 'run', dispatch)
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_failed_dispatch_cannot_replay(permit, no_docker, monkeypatch):
    capability = server._mint_deployment_command(permit, ('up',))
    def dispatch(*args, **kwargs): raise OSError('uncertain dispatch')
    monkeypatch.setattr(server.subprocess, 'run', dispatch)
    with pytest.raises(OSError): server._compose('up', capability=capability)
    with pytest.raises(RuntimeError, match='unconsumed'):
        server._compose('up', capability=capability)


def test_capability_freezes_full_argv_and_environment_before_callbacks(
        permit, no_docker, monkeypatch):
    operation = server._OPERATIONS[permit['_operation']]
    expected_argv = list(operation['command_plan'][0][1])
    expected_env = dict(operation['command_plan'][0][2])
    monkeypatch.setattr(server, '_compose_files', lambda: ['-f', 'review.yml'])
    monkeypatch.setattr(server, '_compose_env', lambda: {'DOCKER_HOST': 'review'})
    capability = server._mint_deployment_command(permit, ('up',))
    monkeypatch.setattr(server, '_compose_files', lambda: ['-f', 'other.yml'])
    monkeypatch.setattr(server, '_compose_env', lambda: {'DOCKER_HOST': 'other'})
    def dispatch(argv, **kwargs):
        assert argv == expected_argv
        assert kwargs['env'] == expected_env
        no_docker.append(argv)
    monkeypatch.setattr(server.subprocess, 'run', dispatch)
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_public_clearance_uses_plan_frozen_before_measurement_callback(
        resource_authority, monkeypatch, tmp_path):
    class Quiet:
        def list_runs(self): return []

    observation = dq.measure(
        skillflow=Quiet(), ownership_dir=resource_authority.root,
        command_runner=lambda argv: SimpleNamespace(
            returncode=0, stdout='', stderr=''))
    monkeypatch.setattr('api.dependencies.get_db_manager', lambda: None)
    monkeypatch.setattr('api.dependencies.get_skillflow', Quiet)
    monkeypatch.setattr(dq, 'evidence_path', lambda: tmp_path / 'journal.json')
    monkeypatch.setattr(server, '_compose_files', lambda: ['-f', 'planned.yml'])
    monkeypatch.setattr(server, '_compose_env', lambda: {'DOCKER_HOST': 'planned'})
    plan = server._deployment_command_plan((('up', '-d', 'aitelier'),))

    def measure(**kwargs):
        monkeypatch.setattr(server, '_compose_files', lambda: ['-f', 'changed.yml'])
        monkeypatch.setattr(server, '_compose_env', lambda: {'DOCKER_HOST': 'changed'})
        return observation

    monkeypatch.setattr(dq, 'measure', measure)
    clearance = server._require_deployment_clearance('restart', plan)
    with pytest.raises(RuntimeError, match='already consumed'):
        server._require_deployment_clearance('restart', plan)
    calls = []
    monkeypatch.setattr(
        server.subprocess, 'run',
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or SimpleNamespace(returncode=0))
    try:
        capability = server._mint_deployment_command(clearance, 0)
        server._compose('up', '-d', 'aitelier', capability=capability)
    finally:
        server._finish_deployment(
            clearance, success=False, error=RuntimeError('fixture settled'))
    assert calls[0][0] == [
        'docker', 'compose', '-f', 'planned.yml', 'up', '-d', 'aitelier']
    assert calls[0][1]['env'] == {'DOCKER_HOST': 'planned'}


def test_only_registered_opaque_plan_identity_is_accepted():
    args = ('up',)
    env = (('DOCKER_HOST', 'review'),)
    raw = ((args, ('docker', 'compose', '-f', 'review.yml', *args), env),)
    with pytest.raises(RuntimeError, match='malformed'):
        server._require_deployment_clearance('restart', raw)

    plan = server._deployment_command_plan((args,))
    with pytest.raises(TypeError, match='copied'):
        copy.copy(plan)
    with pytest.raises(TypeError, match='copied'):
        copy.deepcopy(plan)
    with pytest.raises(TypeError, match='serialized'):
        pickle.dumps(plan)
    reconstructed = object.__new__(type(plan))
    with pytest.raises(RuntimeError, match='unknown'):
        server._require_deployment_clearance('restart', reconstructed)
    server._DEPLOYMENT_PLANS.pop(plan)


def test_canonical_planner_refuses_duplicate_commands_and_manifests(monkeypatch):
    with pytest.raises(RuntimeError, match='unsupported'):
        server._deployment_command_plan((('up',), ('up',)))
    monkeypatch.setattr(
        server, '_compose_files',
        lambda: ['-f', 'review.yml', '-f', 'review.yml'])
    with pytest.raises(RuntimeError, match='invalid launch data'):
        server._deployment_command_plan((('up',),))


def test_unregistered_sealed_lookalike_cannot_supply_alternate_argv_or_env():
    lookalike = server._DeploymentPlan(server._DEPLOYMENT_PLAN_SEAL)
    with pytest.raises(RuntimeError, match='unknown'):
        server._require_deployment_clearance('restart', lookalike)


def test_consumption_is_atomic_between_contenders(permit, no_docker, monkeypatch):
    import threading
    capability = server._mint_deployment_command(permit, ('up',))
    # Isolate token consumption from the separately tested PID/thread ownership
    # check. This does not dispatch any command or disable production authority.
    monkeypatch.setattr(server, '_require_deployment_authority', lambda clearance: None)
    barrier = threading.Barrier(8)
    def consume():
        barrier.wait()
        try: return server._consume_deployment_command(capability, ('up',))
        except RuntimeError as exc: return exc
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: consume(), range(8)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, RuntimeError) for item in results) == 7


def test_nested_public_gate_refuses_without_waiting_on_own_fence(permit, monkeypatch):
    def acquire(): raise AssertionError('nested gate attempted blocking fence acquisition')
    monkeypatch.setattr(dq, 'acquire_cutover_fence', acquire)
    with pytest.raises(RuntimeError, match='nested deployment authority'):
        server._require_deployment_clearance('restart')


def test_settlement_invalidates_unconsumed_capability(permit, no_docker):
    capability = server._mint_deployment_command(permit, ('up',))
    server._finish_deployment(permit, success=False, error=RuntimeError('stopped'))
    with pytest.raises(RuntimeError, match='unconsumed'):
        server._compose('up', capability=capability)
    assert not no_docker


def test_command_planning_callback_cannot_mint_another_capability(permit, no_docker, monkeypatch):
    def files():
        with pytest.raises(RuntimeError, match='already minted'):
            server._mint_deployment_command(permit, ('up', '-d'))
        with pytest.raises(RuntimeError, match='authority'):
            server._compose('up')
        return []
    monkeypatch.setattr(server, '_compose_files', files)
    capability = server._mint_deployment_command(permit, ('up',))
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


@pytest.mark.parametrize(('name', 'value'), [
    ('executable', '/bin/echo'),
    ('shell', True),
    ('preexec_fn', lambda: None),
    ('cwd', '/tmp'),
    ('env', {'PATH': '/tmp'}),
    ('start_new_session', True),
    ('pass_fds', (9,)),
])
def test_execution_shape_overrides_refuse_before_consuming_capability(
        permit, no_docker, name, value):
    capability = server._mint_deployment_command(permit, 0)
    with pytest.raises(RuntimeError, match=name):
        server._compose('up', capability=capability, **{name: value})
    server._compose('up', capability=capability)
    assert len(no_docker) == 1


def test_safe_subprocess_kwargs_remain_available(permit, no_docker):
    capability = server._mint_deployment_command(permit, 0)
    server._compose('up', capability=capability, capture_output=True, text=True,
                    timeout=5, check=False)
    assert len(no_docker) == 1


def test_operation_binds_exact_journal_path(permit, no_docker, monkeypatch, tmp_path):
    capability = server._mint_deployment_command(permit, ('up',))
    monkeypatch.setattr(dq, 'evidence_path', lambda: tmp_path / 'different-journal.json')
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up', capability=capability)
    assert not no_docker


def test_operation_receipt_snapshot_is_independent_of_journal_revalidation(permit, no_docker, monkeypatch):
    capability = server._mint_deployment_command(permit, ('up',))
    # Isolate the immutable operation binding from the separately tested journal
    # check, so a second check cannot mask a lost capability binding.
    monkeypatch.setattr(dq, 'validate_pending_clearance', lambda clearance: None)
    permit['event']['inventory_digest'] = 'b' * 64
    with pytest.raises(dq.DeploymentBlocked, match='stale or differs'):
        server._compose('up', capability=capability)
    assert not no_docker
