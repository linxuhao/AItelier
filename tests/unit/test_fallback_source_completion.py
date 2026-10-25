"""Real host runner + claimed SDK 1.5.99 metadata; only LLM replies are scripted.

Run only in the assigned disposable no-network container. This proposal is UNRUN.
The code graph has the observed source workflow's write/code output, repository
read context, native role, JSON fallback, apply_patch grant and finish contract.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import litellm
import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git

from aitelier.runner import AgentStepRunner
from core import run_isolation as ri
from core.agents import AgentFactory
from core.dpe_pipeline import MaxRetriesExceeded, NativeTurnBudgetExhausted
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager


UNSUPPORTED_NATIVE = 'This endpoint does not support tools in native requests.'


def action(name, **params):
    return {'tool': name, 'params': params}


def add(name, content='VALUE = 42'):
    return action('apply_patch', patch='*** Begin Patch\n*** Add File: '+name+'\n'
                  +'\n'.join('+'+line for line in content.splitlines())+'\n*** End Patch')


FINISH = action('finish_step', summary='Approved source plan complete; tests UNRUN')


@pytest.fixture(autouse=True)
def bounded_fixture_storage(tmp_path):
    yield
    shutil.rmtree(tmp_path)


class ScriptedGateway:
    litellm_model = 'offline/controlled'
    max_output_tokens = 32768
    last_usage = {}

    def __init__(self, world, replies, native='fallback', native_replies=None):
        self.world = world
        self.replies = iter(replies)
        self.native = native
        self.native_replies = iter(native_replies or [])
        self.requests, self.responses, self.snapshots = [], [], []

    def generate(self, **kwargs):
        self.requests.append(deepcopy({'mode': 'json', **kwargs}))
        self.snapshots.append({
            'files': {p.name: p.read_text() for p in self.world.root.glob('*.py')},
            'head': git(self.world.root, 'rev-parse', 'HEAD'),
        })
        reply = next(self.replies, {'actions': []})
        if isinstance(reply, BaseException):
            raise reply
        result = reply if isinstance(reply, str) else json.dumps(
            reply if isinstance(reply, dict) else {'actions': reply})
        self.responses.append(result)
        return result

    def generate_native(self, **kwargs):
        self.requests.append(deepcopy({'mode': 'native', **kwargs}))
        if self.native == 'fallback':
            # Exercise the actual three-error native loop and subsequent host
            # dispatch, rather than replacing _run_native_step with a stub.
            raise litellm.BadRequestError(
                message=UNSUPPORTED_NATIVE, model='ark/glm-5.3-flash', llm_provider='openai')
        reply = next(self.native_replies)
        if isinstance(reply, BaseException):
            raise reply
        calls = [{'id': f'owned-{len(self.requests)}-{i}', 'type': 'function',
                  'function': {'name': a['tool'],
                               'arguments': json.dumps(a.get('params', {}))}}
                 for i, a in enumerate(reply)]
        return SimpleNamespace(text='', reasoning_content='', truncated=False, tool_calls=calls)


def make_world(tmp_path, monkeypatch, *, native='fallback', target='code', turns=8,
               fallback=True, initial_files=None):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    from api import dependencies
    monkeypatch.setattr(dependencies, '_tool_loader_instance', None)
    loader = dependencies.get_tool_loader()
    db = DBManager(str(tmp_path/'host.db'))
    source = tmp_path/'source'
    source.mkdir()
    git(source, 'init', '-q')
    (source/'seed.txt').write_text('owned base\n')
    for name, content in (initial_files or {}).items():
        (source/name).write_text(content)
    git(source, 'add', '--', 'seed.txt', *(initial_files or {}))
    git(source, 'commit', '-qm', 'owned base')
    pid, config = 'owned-fallback-fixture', 'fallback_source_fixture'
    sf = SkillFlow(str(tmp_path/'engine.db'), tool_loader=loader,
                   workspace_base=str(tmp_path/'artifacts'), projects_base=str(tmp_path/'projects'),
                   code_path_resolver=lambda pid, run_id=None:
                       ri.resolve_for_resolver(db, run_id) if run_id else source)
    # Production generated-role registration uses this public host/SDK entry
    # point. Bare agent_registry.register leaves declared tools unresolved.
    sf.register_agent_config_from_dict('source_role', {
        'model': 'offline/controlled',
        'tools': ['apply_patch'] if target == 'code' else ['create'],
        'native_tool_calling': bool(native), 'fallback_to_json_mode': fallback,
        'max_tool_turns': turns, 'max_output_tokens': 32768,
        'system_prompt': 'Execute the approved source plan. Land a small slice early, '
                         'then complete all remaining slices and finish_step. Tests UNRUN.',
    })
    node = StepNode(
        id='implement', agent_config='source_role', output_mode='write', output_target=target,
        context=[{'source': {'config': config, 'output': 'seed_input.md', 'required': True}},
                 {'from': 'repository', 'mode': 'tool'}],
        transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name=config, begin=node.id, steps=[node]))
    ws = WorkspaceManager(str(tmp_path/'artifacts'))
    db.ensure_project(pid, name=pid, repo_type='existing', repo_path=str(source))
    ws.setup_workspace(pid, repo_type='existing', repo_path=str(source))
    monkeypatch.setattr(dependencies, 'get_skillflow', lambda: sf)
    monkeypatch.setattr(dependencies, 'get_db_manager', lambda: db)
    from core.seed_publication import publish_seeds, seed_dir
    publish_seeds(seed_dir(sf, pid, config), {
        'seed_input.md': '1. Add first.py.\n2. Add second.py.\n3. Deliver source; tests UNRUN.\n'})
    rid = sf.create_run(config, context={'project_id': pid}, project_id=pid)
    sf.start_run(rid)
    rec = ri.ensure_for_run(db, run_id=rid, project_id=pid, config_name=config, repo_mode='code')
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    root = Path(rec['worktree_path'])
    # Retain the whole real claim before setup assertions: an unresolved grant
    # is a fixture failure, never evidence of the completion defect.
    evidence_dir = Path(os.environ.get('FALLBACK_COMPLETION_EVIDENCE', str(root.parent/'evidence')))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    role = sf.agent_registry.get('source_role')
    (evidence_dir/(rid+'-claim.json')).write_text(json.dumps({
        'run_id': rid, 'role': role.to_dict(),
        'role_unknown_tools': role.unknown_tools,
        'claim_inputs': claim.inputs if claim else None,
    }, indent=2, default=str)+'\n')
    assert claim and not claim.inputs.get('_legacy_code_staging')
    assert claim.inputs['_output_target'] == target
    assert claim.inputs.get('_resolved_context'), 'required approved plan must really resolve'
    if target == 'code':
        assert 'apply_patch' not in role.unknown_tools
        assert role.tool_schemas['apply_patch'] == loader.load_schema('apply_patch')
        assert 'apply_patch' in claim.inputs['_tool_schemas']
        assert 'write' not in claim.inputs['_tool_schemas'], 'must exercise actual apply_patch route'
    return SimpleNamespace(sf=sf, db=db, ws=ws, root=root, rid=rid, claim=claim,
                           pid=pid, base=git(root, 'rev-parse', 'HEAD'))


def execute(world, monkeypatch, replies, *, native='fallback', native_replies=None):
    gateway = ScriptedGateway(world, replies, native, native_replies)
    monkeypatch.setattr(AgentFactory, '_build_gateway', lambda self, name: gateway)
    result, error = None, None
    try:
        result = asyncio.run(AgentStepRunner(world.db, world.ws).execute(world.claim))
        world.sf.confirm_step(world.claim.token, result)
    except Exception as exc:
        error = exc
    with world.sf.trace_connection(world.pid) as conn:
        rows = [{'category': row[0], 'event': row[1], 'payload': json.loads(row[2] or '{}')}
                for row in conn.execute(
                    'SELECT category,event,payload_json FROM skillflow_trace WHERE run_id=? ORDER BY seq',
                    (world.rid,))]
    evidence = {'run_id': world.rid, 'claim_inputs': world.claim.inputs,
                'requests': gateway.requests, 'responses': gateway.responses,
                'snapshots': gateway.snapshots, 'trace': rows,
                'error_type': type(error).__name__ if error else None, 'error': str(error),
                'base': world.base, 'head': git(world.root, 'rev-parse', 'HEAD'),
                'files': {p.name: p.read_text() for p in world.root.glob('*.py')}}
    evidence_dir = Path(os.environ.get('FALLBACK_COMPLETION_EVIDENCE', str(world.root.parent/'evidence')))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir/(world.rid+'.json')).write_text(json.dumps(evidence, indent=2, default=str)+'\n')
    world.gateway, world.rows, world.evidence = gateway, rows, evidence
    return result, error


def json_requests(world):
    return [r for r in world.gateway.requests if r['mode'] == 'json']


def writes(world):
    return [r for r in world.rows if r['category'] == 'tool_call' and r['event'] == 'apply_patch']


def assert_incomplete(world, error):
    assert error is not None, 'incomplete source returned a candidate'
    assert not any(r['event'] in {'after_validate', 'completed', 'json_source_completion'}
                   for r in world.rows), world.evidence
    assert world.evidence['head'] == world.base, 'uncompleted source was committed'


def test_fallback_two_writes_then_explicit_finish_uses_actual_host_dispatch(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    result, error = execute(w, monkeypatch, [[add('first.py')], [add('second.py')], [FINISH]])
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 3, 'first successful patch prematurely completed source plan'
    assert len(writes(w)) == 2
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n', 'second.py': 'VALUE = 42\n'}
    assert w.gateway.snapshots[1]['files'] == {'first.py': 'VALUE = 42\n'}
    assert all(s['head'] == w.base for s in w.gateway.snapshots)
    assert '"written": ["first.py"]' in json_requests(w)[1]['user_prompt']
    assert '"applied": true' in json_requests(w)[1]['user_prompt']
    completion = [r for r in w.rows if r['event'] == 'json_source_completion']
    assert len(completion) == 1 and completion[0]['payload']['turn'] == 3
    assert w.evidence['head'] != w.base
    native_errors = [r for r in w.rows if r['event'] == 'native_error']
    assert len(native_errors) == 3
    assert all(UNSUPPORTED_NATIVE in r['payload']['original_error'] for r in native_errors)
    assert [r['mode'] for r in w.gateway.requests] == ['native']*3 + ['json']*3


@pytest.mark.parametrize('finish', [FINISH, action('end_step')], ids=['finish', 'end-alias'])
def test_final_write_and_finish_same_turn(tmp_path, monkeypatch, finish):
    w = make_world(tmp_path, monkeypatch, turns=1)
    result, error = execute(w, monkeypatch, [[add('first.py'), add('second.py'), finish]])
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 1 and len(writes(w)) == 2
    assert any(r['event'] == 'json_source_completion' for r in w.rows)


def test_explicit_noop_is_legitimate(tmp_path, monkeypatch):
    initial = {'first.py': 'VALUE = 42\n', 'second.py': 'VALUE = 42\n'}
    w = make_world(tmp_path, monkeypatch, turns=1, initial_files=initial)
    result, error = execute(w, monkeypatch, [[action('message', content='Already satisfies plan'), FINISH]])
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 1 and not writes(w)
    assert w.evidence['head'] == w.base and w.evidence['files'] == initial


def test_a_successful_first_write_alone_is_not_terminal(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, turns=1)
    _, error = execute(w, monkeypatch, [[add('first.py')]])
    assert_incomplete(w, error)
    assert isinstance(error, MaxRetriesExceeded) and 'without finish_step' in str(error)
    assert len(json_requests(w)) == 1 and len(writes(w)) == 1
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n'}


@pytest.mark.parametrize('tail', [action('ask_more_turns', turns=999), action('message', content='still working')],
                         ids=['ungranted-extension', 'message'])
def test_first_write_without_finish_stops_at_existing_budget_without_replay(tmp_path, monkeypatch, tail):
    w = make_world(tmp_path, monkeypatch, turns=2)
    _, error = execute(w, monkeypatch, [[add('first.py')], [tail]])
    assert_incomplete(w, error)
    assert isinstance(error, MaxRetriesExceeded) and 'without finish_step' in str(error)
    assert len(json_requests(w)) == 2 and len(writes(w)) == 1
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n'}
    incomplete = [r for r in w.rows if r['event'] == 'json_source_incomplete']
    assert len(incomplete) == 1 and incomplete[0]['payload']['turns'] == 2


@pytest.mark.parametrize('bad', [
    action('apply_patch', patch='*** Begin Patch\n*** Update File: missing.py\n@@\n-never\n+land\n*** End Patch'),
    action('apply_patch', patch='*** Begin Patch\n*** Add File: refused.py\n+'+'x'*12001+'\n*** End Patch'),
], ids=['missing-target', 'oversized-refusal'])
def test_late_failed_or_refused_write_cannot_be_masked_by_finish(tmp_path, monkeypatch, bad):
    w = make_world(tmp_path, monkeypatch, turns=3)
    _, error = execute(w, monkeypatch, [[add('first.py')], [bad], [FINISH]])
    assert_incomplete(w, error)
    assert isinstance(error, MaxRetriesExceeded) and 'Unresolved write failures' in str(error)
    assert len(json_requests(w)) == 3
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n'}
    assert len(writes(w)) in (1, 2)  # host oversize refusal is before SDK dispatch


def test_partial_response_progress_waits_for_actual_repair_and_finish(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, turns=4)
    bad = action('apply_patch', patch='*** Begin Patch\n*** Update File: second.py\n@@\n-missing\n+VALUE = 42\n*** End Patch')
    result, error = execute(w, monkeypatch, [[add('first.py'), bad], [add('second.py')], [FINISH]])
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 3 and len(writes(w)) == 3
    assert 'Unresolved write failures' in json_requests(w)[1]['user_prompt']
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n', 'second.py': 'VALUE = 42\n'}


def test_late_provider_failure_retains_source_and_original_error(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    original = RuntimeError('controlled late provider failure')
    _, error = execute(w, monkeypatch, [[add('first.py')], original, [FINISH]])
    assert_incomplete(w, error)
    assert isinstance(error, MaxRetriesExceeded) and error.__cause__ is original
    assert any(r['event'] == 'json_source_error' and 'controlled late provider failure'
               in r['payload'].get('original_error', '') for r in w.rows)
    assert len(json_requests(w)) == 2 and len(writes(w)) == 1
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n'}


@pytest.mark.parametrize('target', ['code', 'artifact'])
def test_json_selected_writers_keep_single_response_delivery(tmp_path, monkeypatch, target):
    w = make_world(tmp_path, monkeypatch, native=False, target=target, turns=2)
    write = add('first.py') if target == 'code' else action('create', file='report.md', content='owned artifact')
    result, error = execute(w, monkeypatch, [[write]], native=False)
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 1
    assert not any(r['event'] == 'json_source_completion' for r in w.rows)


def test_native_enabled_artifact_fallback_keeps_existing_delivery(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, target='artifact')
    result, error = execute(w, monkeypatch, [[action('create', file='report.md', content='owned artifact')]])
    assert error is None and result is not None, w.evidence
    assert len(json_requests(w)) == 1
    assert not any(r['event'] == 'json_source_completion' for r in w.rows)


def test_fallback_disabled_never_selects_json(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, fallback=False)
    _, error = execute(w, monkeypatch, [[add('first.py'), FINISH]])
    assert_incomplete(w, error)
    assert not json_requests(w) and not writes(w)
    assert len([r for r in w.rows if r['event'] == 'native_error']) == 3


def test_native_partial_mutation_at_budget_wall_still_refuses_json_replay(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, native=True)
    _, error = execute(w, monkeypatch, [[add('second.py'), FINISH]], native=True,
                       native_replies=[[add('first.py')], NativeTurnBudgetExhausted('controlled native budget wall after write')])
    assert_incomplete(w, error)
    assert isinstance(error, NativeTurnBudgetExhausted)
    assert not json_requests(w) and len(writes(w)) == 1
    assert w.evidence['files'] == {'first.py': 'VALUE = 42\n'}


def test_fatal_provider_refusal_before_any_write_stays_original(tmp_path, monkeypatch):
    from core.ai_router import ProviderRefusal
    w = make_world(tmp_path, monkeypatch, native=True)
    original = ProviderRefusal('controlled transport exhausted')
    _, error = execute(w, monkeypatch, [], native=True, native_replies=[original])
    assert_incomplete(w, error)
    assert error is original
    assert not json_requests(w) and not writes(w)
