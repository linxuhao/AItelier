"""Compose portable artifact recovery and review inputs at real host boundaries."""
import base64
import hashlib
import json
import shutil
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.graph import PipelineGraph

from core.db_manager import DBManager
from core.review_input_bundle import PREFIX, manifest
from core.seed_publication import seed_dir
from core.state_graph import StateConflict
from core.state_service import StateService
from tests.integration.test_state_git_artifact_durability import fixture, git, node, prune_producer
from tests.integration.test_state_graph_entrypoints import live


def request(service, operation, arguments):
    from api import state_graph_routers as routes
    app = FastAPI()
    app.state._test_mode = True
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: service
    with TestClient(app) as client:
        result = client.post('/api/state/commands/' + operation, json=arguments)
        assert result.status_code == 200, result.text
        return result.json()


@pytest.mark.parametrize('live', ['host'], indirect=True)
@pytest.mark.parametrize('damage', [None, 'missing', 'wrong-hash'])
@pytest.mark.asyncio
async def test_portable_db_exact_base_then_bounded_review_before_cost(live, monkeypatch, damage):
    import api.dependencies as deps
    from aitelier.runner import AgentStepRunner
    from core.dpe_pipeline import PipelineEngine
    from core.prompt_assembler import PromptAssembler
    service, source, producer, db, original, commit, tree, payload, bundle = fixture(live.tmp / 'producer')
    raw = ('Portable synthetic review café\n' + 'line\n' * 1200).encode()
    args = {'producer': {'project_id': 'receiver', 'attempt_id': original['attempt_id'], 'artifact': commit},
            'items': [{'name': 'review.md', 'reference': 'candidate://synthetic/review.md',
                       'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
                       'content_base64': base64.b64encode(raw).decode()}]}
    check = {'id': 'review-inputs', 'probe': 'review_input_bundle',
             'arguments': args, 'expected': manifest(args)}
    prune_producer(source, producer, commit)
    bundle.unlink()
    shutil.rmtree(source)
    # Retained report files disappear too. Only the backed-up DB survives.
    shutil.rmtree(live.tmp / 'home')
    portable = live.tmp / 'receiver.sqlite'
    with sqlite3.connect(db) as sender, sqlite3.connect(portable) as receiver:
        sender.backup(receiver)
    fresh = live.tmp / 'fresh-source'
    fresh.mkdir()
    git(fresh, 'init', '-q')
    git(fresh, 'config', 'user.name', 'fixture')
    git(fresh, 'config', 'user.email', 'fixture@localhost')
    (fresh / 'receiver.md').write_text('synthetic receiver baseline\n')
    git(fresh, 'add', 'receiver.md')
    git(fresh, 'commit', '-qm', 'receiver baseline')
    successor_db = DBManager(str(portable))
    monkeypatch.setattr(deps, 'get_db_manager', lambda: successor_db)
    successor = StateService(successor_db, live.ws, live.sf, live.registry,
                             actor='synthetic-reviewer', project_read_trusted=True)
    successor.create_project('receiver', 'Synthetic receiver')
    successor.store.add_nodes('receiver', [node('import'), node('review')])
    successor.bind_source('receiver', str(fresh))
    imported = request(successor, 'start_external_attempt', {
        'project_id': 'receiver', 'node_key': 'import', 'expected_revision': 1,
        'harness': 'synthetic', 'external_id': 'import', 'request_key': 'import', 'base_sha': commit})
    assert imported['status'] == 'running' and imported['context']['base_sha'] == commit
    assert git(fresh, 'rev-parse', commit + '^{tree}') == tree
    import subprocess
    assert subprocess.check_output(['git', '-C', str(fresh), 'show', commit + ':value']) == payload
    assert git(fresh, 'rev-parse', 'refs/aitelier/artifacts/' + commit) == commit
    assert not (fresh / 'value').exists()
    live.sf.register_agent_config('investigator', tools=['read_file'])
    from pathlib import Path
    live.sf.register_graph(PipelineGraph.from_yaml(str(Path(__file__).resolve().parents[2] / 'configs/investigate.yaml')))
    live.registry.register_one(live.sf, 'investigate', hint_overrides={
        'scheduler_owned': True, 'repo_mode': 'none', 'seed_file': 'task.md', 'output_step': 'investigate'})
    attempt = request(successor, 'start_attempt', {
        'project_id': 'receiver', 'node_key': 'review', 'expected_revision': 1,
        'workflow': 'investigate', 'request_key': 'review',
        'frozen_prerequisites': {'version': 1, 'checks': [check]}})
    assert attempt['status'] == 'running' and attempt['run_id'], attempt
    path = seed_dir(live.sf, attempt['execution_project_id'], 'investigate') / (PREFIX + 'review.md')
    assert path.read_bytes() == raw and path.stat().st_mode & 0o222 == 0
    effects = []
    monkeypatch.setattr(live.sf, '_execute_tool_impl', lambda *a, **k: effects.append('tool'))
    seen = []
    def model_boundary(*a, **kw):
        effects.append('model')
        seen.append(kw['resolved_context'])
        return {'outputs': {'result': 'synthetic review'}}
    monkeypatch.setattr(PipelineEngine, 'run_step', model_boundary)
    if damage:
        if damage == 'missing':
            path.unlink()
        else:
            path.chmod(0o644)
            path.write_text('corrupt')
            path.chmod(0o444)
        monkeypatch.setattr(live.sf, '_get_resolver_for_run', lambda *a, **k: effects.append('resolver'))
        for phase in ('advance_run', 'claim_next_step'):
            with pytest.raises(StateConflict) as refused:
                getattr(live.sf, phase)(attempt['run_id'])
            assert refused.value.report['required'] == check['expected']
            assert refused.value.report['passed'] is False
        assert effects == []
    else:
        live.sf.advance_run(attempt['run_id'])
        step = live.sf.claim_next_step(attempt['run_id'])
        assert step is not None
        await AgentStepRunner(successor_db, live.ws).execute(step)
        assert effects == ['model'] and len(seen) == 1
        key = next(k for k in seen[0] if k.startswith('[review input review.md]'))
        assert PromptAssembler._clip_context_entry(key, seen[0][key]).encode() == raw
    records = [json.loads(r[0]) for r in live.sf._conn.execute(
        "SELECT payload_json FROM skillflow_trace WHERE event LIKE 'review_inputs_%'").fetchall()]
    assert records and all(r['required'] == check['expected'] for r in records)
    assert {'claim', 'advance'} <= {r['phase'] for r in records}
    assert all(r['passed'] == (damage is None) for r in records)
