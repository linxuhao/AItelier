"""Real Git/SDK delivery and normal runner review; no model or production service."""
import json
import importlib.metadata
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, LoopConfig, Transition
from skillflow.workspace import _sanitize_item

from aitelier.runner import AgentStepRunner
from aitelier.tools.closeout_gate.impl import closeout_gate


def git(repo, *args):
    return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class Scenario:
    def __init__(self, tmp_path, monkeypatch):
        assert importlib.metadata.version('skillflow-py') == '1.5.95'
        self.repo = tmp_path / 'repo'; self.repo.mkdir()
        git(self.repo, 'init', '-q'); git(self.repo, 'config', 'user.name', 'Fixture')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'seed').write_text('baseline\n'); git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.sf = SkillFlow(':memory:', workspace_base=str(tmp_path / 'ws'),
                            code_path_resolver=lambda _: str(self.repo), artifact_history=False)
        graph = PipelineGraph(name='receipt-fixture', begin='task_loop', steps=[
            StepNode(id='task_loop', step_type='loop',
                     loop=LoopConfig(source={'step': '3', 'file': 'manifest.json', 'field': 'items'}, item_as='current_task'),
                     transitions=[Transition(to='t_impl', max_loop=20), Transition(to='done')]),
            StepNode(id='t_impl', output_mode='write', output_target='code',
                     transitions=[Transition(to='t_impl_review')]),
            StepNode(id='t_impl_review', agent_config='task_implementer_reviewer', output_mode='content',
                     output_fixed={'verdict': {'file': 'review_verdict.json'}},
                     context=[{'step': 't_impl', 'file': 'code_changes.json', 'required': True}],
                     transitions=[Transition(to='task_loop', match={'from_file': 'review_verdict.json', 'field': 'passed', 'value': True}),
                                  Transition(to='t_impl', match={'from_file': 'review_verdict.json', 'field': 'passed', 'value': False}, max_loop=3)]),
            StepNode(id='done', step_type='gate'),
        ])
        self.sf.register_agent_config("task_implementer_reviewer", model="fixture", system_prompt="fixture")
        self.sf.register_graph(graph)
        manifest = self.sf._workspace.get_step_dir('p', graph.name, '3'); manifest.mkdir(parents=True)
        (manifest / 'manifest.json').write_text(json.dumps({'items': ['card_a', 'card_b']}))
        self.run = self.sf.create_run(graph.name, {'project_id': 'p'}, project_id='p')
        self.sf.start_run(self.run)
        self.calls = []
        scenario = self
        class Engine:
            def __init__(self, **kwargs): pass
            def run_step(self, **kwargs):
                scenario.calls.append(kwargs)
                out = Path(kwargs['output_dir']); out.mkdir(parents=True, exist_ok=True)
                (out / 'review_verdict.json').write_text(json.dumps({'passed': scenario.pass_review}))
                return True
        import api.dependencies as deps
        import core.dpe_pipeline as pipeline
        import core.run_isolation as isolation
        monkeypatch.setattr(deps, 'get_skillflow', lambda: self.sf)
        monkeypatch.setattr(pipeline, 'PipelineEngine', Engine)
        monkeypatch.setattr(isolation, 'resolve_for_resolver', lambda *_: str(self.repo))
        db = MagicMock(); db.get_repo_info.return_value = {'repo_type': 'existing', 'repo_path': str(self.repo)}
        db.get_project.return_value = {}
        self.runner = AgentStepRunner(db, MagicMock())

    def claim(self, name):
        for _ in range(8):
            step = self.sf.claim_next_step(self.run)
            if step:
                assert step.step_id == name
                return step
            self.sf.advance_run(self.run)
        raise AssertionError('SDK did not release ' + name)

    def produce(self, path, content):
        step = self.claim('t_impl')
        item = self.sf.get_steps(self.run)
        row = next(r for r in item if r['id'] == step.token.step_instance_id)
        code = self.sf._code_output(self.run, 't_impl')
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(content)
        code.record([path])
        artifact = Path(step.inputs['_artifact_dir']); artifact.mkdir(parents=True, exist_ok=True)
        authority_file = artifact / 'write_scope_receipt.json'
        history = json.loads(authority_file.read_text())['claim_history'] if authority_file.exists() else []
        history.append({'step_instance_id': row['id'], 'claim_epoch': row['claim_epoch']})
        authority_file.write_text(json.dumps({'run_id': self.run, 'step_id': 't_impl', 'task': row['loop_item'],
                    'allowed_scope': {'owns': [path], 'shared_hotspots': []},
                    'claim_history': history, 'violations': []}))
        # The current committed receipt is not produced until actual SDK confirmation.
        before = json.loads((artifact / 'code_changes.json').read_text()) if (artifact / 'code_changes.json').exists() else None
        assert before is None or before['step_instance_id'] != row['id']
        self.sf.confirm_step(step.token, StepResult())
        receipt = json.loads((artifact / 'code_changes.json').read_text())
        assert receipt['step_instance_id'] == row['id'] and receipt['commit'] == git(self.repo, 'rev-parse', 'HEAD')
        review = self.claim('t_impl_review')
        card = {'owns': [path]}
        context = dict(review.inputs.get('_resolved_context', {}))
        context.update({'[current_task]': row['loop_item'], f'Step 3 — tasks/{row["loop_item"]}.json': card,
                        '[closeout_gate]': 'initial context source'})
        review = replace(review, inputs={**review.inputs, '_resolved_context': context})
        self.item_dir = artifact; self.receipt = receipt; self.review = review
        return review

    async def inspect(self, passed=True):
        self.pass_review = passed
        result = await self.runner.execute(self.review)
        context = self.calls[-1]['resolved_context']['[closeout_gate]']
        self.sf.confirm_step(self.review.token, result)
        return context


@pytest.mark.asyncio
async def test_current_item_and_immediate_retry_do_not_accuse_siblings(tmp_path, monkeypatch):
    s = Scenario(tmp_path, monkeypatch)
    s.produce('a.txt', 'A\n'); await s.inspect()
    s.produce('b with spaces.txt', 'B\n'); first_base = s.receipt['base_commit']
    context = await s.inspect(passed=False)
    assert 'out-of-scope delivered' not in context and 'a.txt' not in context
    s.produce('b with spaces.txt', 'B revised\n')
    assert s.receipt['base_commit'] == first_base
    context = await s.inspect()
    assert 'out-of-scope delivered' not in context and 'a.txt' not in context
    assert 'b with spaces.txt' in context
    assert s.calls[-1]['write_scope'] is None  # reviewer has no code-writing grant
    assert (s.repo / 'b with spaces.txt').read_text() == 'B revised\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing', 'wrong_run', 'wrong_item', 'old_instance', 'old_claim', 'stale_head', 'newer_producer', 'dirty_worktree', 'dirty_staged', 'untracked', 'hidden_dirty'])
async def test_invalid_binding_fails_before_model(tmp_path, monkeypatch, bad):
    s = Scenario(tmp_path, monkeypatch); s.produce('a.txt', 'A\n')
    code = s.item_dir / 'code_changes.json'; authority = s.item_dir / 'write_scope_receipt.json'
    if bad == 'missing': code.unlink()
    elif bad in ['wrong_run', 'old_instance']:
        data = json.loads(code.read_text()); data['run_id' if bad == 'wrong_run' else 'step_instance_id'] = 'wrong' if bad == 'wrong_run' else 999
        code.write_text(json.dumps(data))
    elif bad in ['wrong_item', 'old_claim']:
        data = json.loads(authority.read_text())
        if bad == 'wrong_item': data['task'] = 'sibling'
        else: data['claim_history'][-1]['claim_epoch'] = 0
        authority.write_text(json.dumps(data))
    elif bad == 'stale_head': git(s.repo, 'commit', '--allow-empty', '-qm', 'foreign movement')
    elif bad in ('dirty_worktree', 'dirty_staged', 'hidden_dirty'):
        (s.repo / 'a.txt').write_text('uncommitted replacement\n')
        if bad == 'dirty_staged': git(s.repo, 'add', 'a.txt')
        if bad == 'hidden_dirty': git(s.repo, 'update-index', '--assume-unchanged', 'a.txt')
    elif bad == 'untracked': (s.repo / 'untracked.txt').write_text('not in receipt\n')
    else:
        get_steps = s.sf.get_steps
        rows = get_steps(s.run); producer = next(r for r in rows if r['step_id'] == 't_impl')
        monkeypatch.setattr(s.sf, 'get_steps', lambda *_: rows + [{**producer, 'id': 999, 'status': 'claimed'}])
    with pytest.raises(RuntimeError, match='attribution blocked'):
        await s.runner.execute(s.review)
    assert not s.calls


def test_actual_full_diff_rename_delete_binary_and_noop(tmp_path):
    repo = tmp_path / 'r'; repo.mkdir(); git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'T'); git(repo, 'config', 'user.email', 't@t')
    (repo / 'old name.txt').write_text('must inspect deleted Source\n')
    (repo / 'blob.bin').write_bytes(b'\0old'); git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'old name.txt').rename(repo / 'new name.txt')
    (repo / 'blob.bin').write_bytes(b'\0new'); git(repo, 'add', '-A'); git(repo, 'commit', '-qm', 't_impl card')
    commit = git(repo, 'rev-parse', 'HEAD')
    receipt = {'target': 'code', 'base_commit': base, 'commit': commit, 'files': []}  # asserted list must not hide real changes
    result = closeout_gate(project_root=str(repo), owns=['new name.txt', 'blob.bin'], code_receipt=receipt,
                          scope_violations=[{'tool': 'apply_patch', 'requested_path': 'forbidden'}])
    assert result['out_of_scope'] == ['old name.txt']
    by_path = {f['path']: f for f in result['files']}
    assert set(by_path) == {'old name.txt', 'new name.txt', 'blob.bin'}
    assert by_path['old name.txt']['base_content'] == 'must inspect deleted Source\n'
    assert by_path['old name.txt']['status'] == 'D' and by_path['new name.txt']['status'] == 'A'
    assert by_path['blob.bin']['binary']
    assert result['scope_violations'] and 'attempted scope violations' in result['content']
    receipt['base_commit'] = commit
    no_op = closeout_gate(project_root=str(repo), owns=['unused'], code_receipt=receipt)
    assert no_op['attribution_valid'] and no_op['no_changes'] and no_op['files'] == []
