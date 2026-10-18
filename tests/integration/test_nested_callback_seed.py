"""Real initialized host/import/SDK/native path; only the provider is scripted.

The callback uses an authentic CodingRequest and handoff in the same engine.
No live provider, scheduler, host launcher or production database is used.
"""
import copy
import json
import hashlib
from importlib import metadata
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import uuid

import pytest
import yaml

from api import dependencies as deps
from aitelier.runner import AgentStepRunner
from core import coding_workspace as cw, pipeline_bundle as pb, run_isolation as ri
from core.ai_router import AIGateway
from core.db_manager import DBManager
from core.run_launcher import start_config_run
from core.seed_publication import seed_dir, seed_is_published
from core.workspace_manager import WorkspaceManager

OBSERVATIONS = {}


def save(name, data):
    root = Path(os.environ.get('NESTED_SEED_RAW', '/tmp/nested-seed-observations'))
    root.mkdir(parents=True, exist_ok=True)
    (root / (name + '.json')).write_text(json.dumps(data, indent=2, default=str) + '\n')


def seed_state(sf, project_id, run_id):
    name = sf.get_run(run_id)['graph_name']
    directory = seed_dir(sf, project_id, name)
    return {'project_id': sf._get_project_id(run_id), 'run_id': run_id,
            'config': name, 'directory': str(directory),
            'published': seed_is_published(directory, 'seed_input.md'),
            'files': {p.name: p.read_text() for p in directory.iterdir() if p.is_file()}}


def seed_callback(*, project_id='', run_id='', kind='', coding_project='', session_id=''):
    sf, db = deps.get_skillflow(), deps.get_db_manager()
    result = {'before': seed_state(sf, project_id, run_id), 'kind': kind}
    if kind == 'nested':
        request = cw.CodingRequest(db, cw.authorize_entry(
            db, session_id=session_id, project_id=coding_project, writer='offline callback fixture'), sf=sf)
        try:
            ws = request.workspace()
            cw.write_text(db, ws, 'value.txt', '42\n', create=True, detail='owned callback initial value')
            assert cw.read_text(ws, 'value.txt')[1] == '42\n'
            cw.write_text(db, ws, 'value.txt', '43\n', create=False, detail='owned callback useful update')
            assert cw.read_text(ws, 'value.txt')[1] == '43\n'
            assert not (ws.source / 'value.txt').exists()
            cw.write_text(db, ws, 'task.md', 'TASK_DATA {"ticket": "CALLBACK-DISTRACTOR", "numbers": [900, 99]}\n',
                          create=True, detail='owned callback draft must never supply outer pipeline input')
            receipt = request.finish('Offline nested callback useful update complete')
            assert receipt['status'] == 'applied'
            assert (ws.source / 'value.txt').read_text() == '43\n'
            assert sf.get_run(ws.run_id)['status'] == 'completed'
            origin = cw._read(cw._run_dir(coding_project, session_id, ws.run_id) / 'origin.json')
            assert origin['state'] == 'closed' and cw._claim_get(ws.run_id) is None
            assert not ri.write_admissions(db, str(ws.root))
            assert not ri.write_admissions(db, str(ws.source))
            result['callback'] = {'run_id': ws.run_id, 'project_id': coding_project,
                                  'workspace': str(ws.root), 'source': str(ws.source),
                                  'value': (ws.source / 'value.txt').read_text(),
                                  'receipt': receipt, 'origin': origin, 'admissions': ri.write_admissions(db)}
        finally:
            request.release()
    result['after'] = seed_state(sf, project_id, run_id)
    assert result['before'] == result['after']
    OBSERVATIONS[run_id] = result
    save(run_id + '-callback', result)
    return {'passed': True}


def bundle(name, shape, coding_project, session_id):
    agent = {'id': 'investigate', 'step_type': 'agent', 'agent_config': 'receipt_reader',
             'context': [{'source': {'config': name, 'output': 'task.md'}}],
             'output': {'mode': 'content', 'fixed': {'findings': 'findings.md'}},
             'transitions': [{'to': None}]}
    steps = [agent]
    if shape != 'direct':
        steps.insert(0, {'id': 'probe', 'step_type': 'tool', 'tool_name': 'nested_seed_probe',
                        'tool_params': {'kind': shape, 'coding_project': coding_project, 'session_id': session_id},
                        'transitions': [{'to': 'investigate', 'match': {'passed': True}}]})
    graph = {'name': name, 'x-aitelier': {'repo_mode': 'none', 'seed_file': 'task.md',
                                        'output_step': 'investigate', 'scheduler_owned': True},
             'begin': steps[0]['id'], 'steps': steps}
    tool = {'name': 'nested_seed_probe', 'description': 'Owned offline useful callback',
            'parameters': {key: {'type': 'string', 'required': False} for key in
                           ('project_id', 'run_id', 'kind', 'coding_project', 'session_id')},
            'output': {'target': 'artifact'}}
    return {pb.BUNDLE_KEY: pb.BUNDLE_VERSION, 'config_name': name, 'graph_yaml': yaml.safe_dump(graph),
            'roles': {'receipt_reader': {'model': 'sonnet', 'tools': [], 'native_tool_calling': True,
                       'fallback_to_json_mode': False, 'thinking': {'enable': False},
                       'max_tool_turns': 2, 'max_output_tokens': 256,
                       'system_prompt': 'Read the user task supplied through resolved context. Write findings.md with the requested JSON receipt and arithmetic sum. If no task is supplied write NO_INPUT. Do not invent input.'}},
            'tools': {'nested_seed_probe': {'impl.py': 'from tests.integration.test_nested_callback_seed import seed_callback\n\ndef nested_seed_probe(*, project_id="", run_id="", kind="", coding_project="", session_id=""):\n    return seed_callback(project_id=project_id, run_id=run_id, kind=kind, coding_project=coding_project, session_id=session_id)\n',
                                          'tool.yaml': yaml.safe_dump(tool), 'README.md': 'Offline owned fixture only.'}}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('AITELIER_HOME', str(home))
    monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
    monkeypatch.delenv('AITELIER_HOST_LAUNCHER_SOCKET', raising=False)
    monkeypatch.setattr(deps, 'DB_PATH', home / 'app.db')
    monkeypatch.setattr(deps, 'SKILLFLOW_DB_PATH', str(home / 'engine.db'))
    monkeypatch.setattr(deps, 'WS_PATH', str(home / 'workspaces'))
    monkeypatch.setattr(deps, 'PROJECTS_PATH', str(home / 'projects'))
    monkeypatch.setattr(deps, 'db_instance', DBManager(str(home / 'app.db')))
    monkeypatch.setattr(deps, 'ws_instance', WorkspaceManager(str(home / 'workspaces'), projects_base=str(home / 'projects')))
    monkeypatch.setattr(deps, '_skillflow_instance', None)
    monkeypatch.setattr(deps, '_tool_loader_instance', None)
    monkeypatch.setattr(deps, '_config_registry_instance', None)
    monkeypatch.setattr('core.scheduler.wake_scheduler', lambda *a: None)
    sf = deps.get_skillflow()
    import skillflow
    save('runtime-identity', {'sdk_version': metadata.version('skillflow-py'),
                             'sdk_path': skillflow.__file__, 'host_engine': type(sf).__name__,
                             'sdk_core_sha256': hashlib.sha256(Path(skillflow.__file__).with_name('core.py').read_bytes()).hexdigest(),
                             'data_home': str(home),
                             'host_modules': {name: hashlib.sha256((Path(__file__).parents[2] / name).read_bytes()).hexdigest() for name in
                                              ('core/pipeline_registry.py', 'core/pipeline_bundle.py', 'core/coding_workspace.py', 'core/dpe_pipeline.py', 'aitelier/runner.py')}})
    db, ws, registry = deps.get_db_manager(), deps.get_workspace_manager(), deps.get_config_registry()
    # Keep the normal role name while using only a private offline fixture endpoint.
    routes = home / 'fixture-model-routes.json'
    routes.write_text(json.dumps({'sonnet': ['openai/offline-receipt']}))
    monkeypatch.setattr('core.model_routes.default_routes_file', lambda: str(routes))
    outbound = []
    def provider(self, kwargs, *, native=False):
        assert native
        messages = copy.deepcopy(kwargs['messages'])
        task = re.search(r'TASK_DATA (\{[^\n]+\})', '\n'.join(m.get('content') or '' for m in messages))
        content = 'NO_INPUT'
        if task:
            value = json.loads(task.group(1))
            content = json.dumps({'ticket': value['ticket'], 'sum': sum(value['numbers'])}) + '\n'
        outbound.append({'messages': messages, 'model': kwargs['model'], 'tools': kwargs.get('tools'),
                         'response_content': content})
        calls = [('create_findings', {'initialContent': content}), ('finish_step', {'summary': 'Wrote actual computed receipt'})]
        tools = [SimpleNamespace(id='call_' + uuid.uuid4().hex, type='function',
                                function=SimpleNamespace(name=name, arguments=json.dumps(args))) for name, args in calls]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tools), finish_reason='tool_calls')])
    monkeypatch.setattr(AIGateway, '_complete_prebuilt', provider)
    def no_live(*a, **kw):
        raise AssertionError('Live provider boundary must not be called')
    monkeypatch.setattr(AIGateway, '_call_llm', no_live)
    return sf, db, ws, registry, outbound


def launch(runtime, tmp_path, shape, ticket, numbers):
    sf, db, ws, registry, outbound = runtime
    pid = 'pipeline-' + ticket
    coding_pid = 'callback-' + ticket
    source = tmp_path / coding_pid
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source / 'README.md').write_text('Owned callback product fixture\n')
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@invalid',
           'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@invalid'}
    subprocess.run(['git', '-C', str(source), 'add', 'README.md'], check=True, env=env)
    subprocess.run(['git', '-C', str(source), 'commit', '-qm', 'owned callback baseline'], check=True, env=env)
    db.ensure_project(coding_pid, name=coding_pid, repo_type='existing', repo_path=str(source))
    sid = db.create_session()
    name = 'gen_seed_' + shape
    imported = pb.import_pipeline(sf, registry, bundle(name, shape, coding_pid, sid))
    payload = {'ticket': ticket, 'numbers': numbers}
    seed = 'TASK_DATA ' + json.dumps(payload) + '\nWrite findings.md as JSON with ticket and sum of numbers.\n'
    result = start_config_run(db, ws, name, pid, seed_text=seed)
    assert result['status'] == 'started', result
    return {'pid': pid, 'rid': result['run_id'], 'name': name, 'seed': seed,
            'expected': {'ticket': ticket, 'sum': sum(numbers)}, 'source': source,
            'imported': imported, 'manifest_seed': registry.get(name).seed_file}


async def drive(runtime, case, *, expect_seed=True):
    sf, db, ws, registry, outbound = runtime
    before = seed_state(sf, case['pid'], case['rid'])
    if expect_seed:
        assert before['published'][0] is True
    sf.advance_run(case['rid'])
    claim = sf.claim_next_step(case['rid'])
    assert claim is not None and claim.step_id == 'investigate'
    observed = {'case': case, 'before': before, 'after_tool': seed_state(sf, case['pid'], case['rid']),
                'claim_project': claim.run_context['project_id'], 'resolved_context': claim.inputs.get('_resolved_context'),
                'persisted_graph': Path(case['imported']['path']).read_text(),
                'callback': OBSERVATIONS.get(case['rid']), 'outbound_start': len(outbound)}
    save(case['rid'] + '-before-agent', observed)
    result = await AgentStepRunner(db, ws).execute(claim)
    sf.confirm_step(claim.token, result)
    for _ in range(8):
        sf.advance_run(case['rid'])
        if sf.get_run(case['rid'])['status'] in ('completed', 'failed'): break
    path = sf._workspace.get_step_dir(case['pid'], case['name'], 'investigate') / 'findings.md'
    observed.update(outbound=outbound[observed['outbound_start']:], output=path.read_text(), run=sf.get_run(case['rid']))
    save(case['rid'] + '-complete', observed)
    assert sf.get_run(case['rid'])['status'] == 'completed'
    assert observed['before'] == observed['after_tool']
    assert observed['claim_project'] == case['pid']
    if expect_seed:
        assert json.loads(observed['output']) == case['expected']
    else:
        assert observed['output'] == 'NO_INPUT'
    first = observed['outbound'][0]['messages']
    assert sum((m.get('content') or '').count(case['seed']) for m in first) == int(expect_seed)
    assert all('CALLBACK-DISTRACTOR' not in (m.get('content') or '') for m in first)
    assert all(case['seed'] not in (m.get('content') or '') for m in first if m['role'] == 'system')
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['direct', 'plain', 'nested'])
async def test_declared_seed_reaches_first_native_after_tool(runtime, tmp_path, shape):
    case = launch(runtime, tmp_path, shape, shape, [17, 26])
    await drive(runtime, case)


@pytest.mark.asyncio
async def test_interleaved_projects_and_runs_keep_their_own_seed(runtime, tmp_path):
    one = launch(runtime, tmp_path, 'nested', 'one', [19, 23])
    two = launch(runtime, tmp_path, 'nested', 'two', [30, 13])
    first = await drive(runtime, one)
    second = await drive(runtime, two)
    assert two['seed'] not in '\n'.join(m.get('content') or '' for m in first['outbound'][0]['messages'])
    assert one['seed'] not in '\n'.join(m.get('content') or '' for m in second['outbound'][0]['messages'])
    assert first['callback']['callback']['value'] == second['callback']['callback']['value'] == '43\n'


@pytest.mark.asyncio
async def test_missing_own_seed_never_borrows_foreign_or_callback_input(runtime, tmp_path):
    sf = runtime[0]
    one = launch(runtime, tmp_path, 'nested', 'missing', [5, 6])
    two = launch(runtime, tmp_path, 'nested', 'available', [40, 3])
    # Named loss-of-input fault; retain the published marker and another run's valid seed.
    (seed_dir(sf, one['pid'], one['name']) / 'seed_input.md').unlink()
    observed = await drive(runtime, one, expect_seed=False)
    assert two['seed'] not in '\n'.join(m.get('content') or '' for m in observed['outbound'][0]['messages'])
    assert observed['callback']['callback']['value'] == '43\n'
    await drive(runtime, two)
