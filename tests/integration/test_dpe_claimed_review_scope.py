"""Real loop claims/producer receipts/runner closeout; only paid completion is scripted.

The pinned reviewer deliberately omits a card, as an existing generated graph can.
No positive claim context, scope policy or durable receipt is manufactured here.
"""
import asyncio
import copy
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import litellm
import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, LoopConfig, Transition

from aitelier.runner import AgentStepRunner
from core import run_isolation
from core.db_manager import DBManager
from core.tool_routes import AItelierToolLoader
from core.workspace_manager import WorkspaceManager
from core.write_scope import card_from_context, scope_from_context


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def calls(*actions):
    return [{'id': 'owned-' + str(i), 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(params)}}
        for i, (name, params) in enumerate(actions)]


class Case:
    def __init__(self, tmp, monkeypatch):
        monkeypatch.setenv('AITELIER_HOME', str(tmp / 'home'))
        monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
        monkeypatch.setenv('AITELIER_LLM_STREAM', '0')
        monkeypatch.delenv('AITELIER_FORCE_ALL_AGENT_MODELS', raising=False)
        canonical = tmp / 'canonical'; canonical.mkdir()
        git(canonical, 'init', '-q')
        git(canonical, 'config', 'user.name', 'Owned regression')
        git(canonical, 'config', 'user.email', 'fixture@example.invalid')
        (canonical / 'seed.txt').write_text('generic baseline\n')
        git(canonical, 'add', 'seed.txt'); git(canonical, 'commit', '-qm', 'baseline')
        self.db = DBManager(str(tmp / 'host.db'))
        self.db.ensure_project('p', name='owned context regression',
                               repo_type='existing', repo_path=str(canonical))
        self.ws = WorkspaceManager(str(tmp / 'artifacts'), projects_base=str(tmp / 'projects'))
        loader = AItelierToolLoader(Path(skillflow.__file__).parent / 'tools')
        loader.add_tools_dir(Path(__file__).resolve().parents[2] / 'aitelier' / 'tools')
        self.sf = SkillFlow(str(tmp / 'sdk.db'), tool_loader=loader,
            workspace_base=str(tmp / 'artifacts'), artifact_history=False,
            code_path_resolver=lambda pid, run_id=None:
                run_isolation.resolve_for_resolver(self.db, run_id))
        for name in ['producer', 'reviewer']:
            self.sf.register_agent_config(name, model='deepseek/deepseek-v4-flash',
                system_prompt='Follow the supplied task and retain refusals.',
                native_tool_calling=True, fallback_to_json_mode=False, max_tool_turns=8)
        graph = PipelineGraph(name='dpe_default_v2', begin='task_loop', steps=[
            StepNode(id='task_loop', step_type='loop',
                loop=LoopConfig(source={'step': '3', 'file': 'manifest.json', 'field': 'items'},
                                item_as='current_task'),
                transitions=[Transition(to='t_impl', max_loop=10), Transition(to='done')]),
            StepNode(id='t_impl', agent_config='producer', output_mode='write',
                output_target='code', output_allow_full_write=True,
                context=[{'from': 'repository', 'mode': 'tool'},
                         {'step': '3', 'file': 'tasks/$current_task.json', 'required': True}],
                transitions=[Transition(to='t_impl_review')]),
            StepNode(id='t_impl_review', agent_config='reviewer', output_mode='content',
                output_target='artifact', output_fixed={'verdict': {'file': 'review_verdict.json'}},
                context=[{'step': 't_impl'}, {'tool': 'closeout_gate'}],
                transitions=[Transition(to='task_loop')]),
            StepNode(id='done', step_type='gate')])
        self.sf.register_graph(graph)
        cards = self.sf._workspace.get_step_dir('p', graph.name, '3')
        (cards / 'tasks').mkdir(parents=True)
        (cards / 'manifest.json').write_text(json.dumps({'items': ['generic_card']}))
        (cards / 'tasks/generic_card.json').write_text(json.dumps({
            'id': 'generic_card', 'owns': ['owned.py'], 'shared_hotspots': []}))
        self.run = self.sf.create_run(graph.name, {'project_id': 'p'}, project_id='p')
        import api.dependencies as deps
        monkeypatch.setattr(deps, 'get_skillflow', lambda: self.sf)
        monkeypatch.setattr(deps, 'get_db_manager', lambda: self.db)
        run_isolation.ensure_for_run(self.db, run_id=self.run, project_id='p',
                                    config_name=graph.name, repo_mode='code')
        self.root = Path(run_isolation.resolve_for_resolver(self.db, self.run))
        self.sf.start_run(self.run)
        self.runner = AgentStepRunner(self.db, self.ws)
        self.script = []; self.requests = []; self.payload_reads = []
        def observe(sql):
            if sql.lstrip().upper().startswith('SELECT') and 'inputs_json' in sql:
                self.payload_reads.append(sql)
        self.sf._conn.set_trace_callback(observe)
        def complete(**kwargs):
            self.requests.append(copy.deepcopy(kwargs.get('messages', [])))
            assert self.script, 'unexpected paid completion'
            return litellm.ModelResponse(model='offline', choices=[{
                'index': 0, 'finish_reason': 'tool_calls', 'message': {
                    'role': 'assistant', 'content': None, 'tool_calls': self.script.pop(0)}}],
                usage={'prompt_tokens': 8, 'completion_tokens': 8, 'total_tokens': 16})
        monkeypatch.setattr(litellm, 'completion', complete)

    def claim(self, expected):
        for _ in range(12):
            claim = self.sf.claim_next_step(self.run)
            if claim:
                assert claim.step_id == expected
                assert claim.inputs['_resolved_context']['[current_task]'] == 'generic_card'
                return claim
            self.sf.advance_run(self.run)
        raise AssertionError('normal SDK did not claim ' + expected)

    def execute(self, claim, script):
        self.script = script
        result = asyncio.run(self.runner.execute(claim))
        assert not self.script, 'normal runner did not consume scripted completion'
        return result

    def produce(self, violation=False):
        self.producer = self.claim('t_impl')
        context = self.producer.inputs['_resolved_context']
        assert card_from_context(context, 'generic_card')['owns'] == ['owned.py']
        script = []
        if violation:
            script.append(calls(('create', {'file': 'forbidden.py', 'content': 'DENIED = True\n'})))
        script += [calls(('create', {'file': 'owned.py', 'content': 'VALUE = 1\n'})),
                   calls(('finish_step', {'summary': 'owned implementation'}))]
        result = self.execute(self.producer, script)
        self.sf.confirm_step(self.producer.token, result)
        self.artifact = Path(self.producer.inputs['_artifact_dir'])
        self.code = self.artifact / 'code_changes.json'
        self.authority = self.artifact / 'write_scope_receipt.json'
        receipt = json.loads(self.code.read_text())
        assert receipt['step_instance_id'] == self.producer.token.step_instance_id
        assert receipt['commit'] == git(self.root, 'rev-parse', 'HEAD')
        self.review = self.claim('t_impl_review')
        context = self.review.inputs['_resolved_context']
        assert card_from_context(context, 'generic_card') is None
        assert scope_from_context(context).policy == 'missing-task-card-deny'
        return self.review

    def review_now(self, passed=True):
        self.payload_reads.clear()
        result = self.execute(self.review, [
            calls(('write_verdict', {'content': json.dumps({'passed': passed})})),
            calls(('finish_step', {'summary': 'review complete'}))])
        messages = json.dumps(self.requests[-2:])
        self.sf.confirm_step(self.review.token, result)
        print('CLAIMED_REVIEW_SCOPE ' + json.dumps({
            'producer_instance': self.producer.token.step_instance_id,
            'producer_epoch': self.producer.token.claim_epoch,
            'review_instance': self.review.token.step_instance_id,
            'review_epoch': self.review.token.claim_epoch,
            'producer_context_labels': list(self.producer.inputs['_resolved_context']),
            'review_context_labels': list(self.review.inputs['_resolved_context']),
            'scope_receipt': json.loads(self.authority.read_text()),
            'code_receipt': json.loads(self.code.read_text()),
            'payload_reads': self.payload_reads, 'review_messages': self.requests[-2:]}))
        return result, messages


def test_normal_claimed_review_uses_producer_scope_without_reviewer_card(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch); c.produce()
    result, messages = c.review_now()
    assert result.flags['passed'] is True
    assert 'owned.py' in messages and 'out-of-scope delivered' not in messages
    reads = [sql for sql in c.payload_reads
             if 'SELECT inputs_json FROM skillflow_steps WHERE id =' in sql
             and 'completion_seq =' in sql]
    assert len(reads) == 1
    assert 'id = ' + str(c.producer.token.step_instance_id) in reads[0]


def test_real_scope_refusal_is_review_content_not_identity_failure(tmp_path, monkeypatch):
    c = Case(tmp_path, monkeypatch); c.produce(violation=True)
    result, messages = c.review_now(passed=False)
    assert result.flags['passed'] is False
    assert 'attempted scope violations' in messages and 'forbidden.py' in messages
    assert not (c.root / 'forbidden.py').exists()
    assert json.loads(c.authority.read_text())['violations']


@pytest.mark.parametrize('bad', ['missing', 'malformed', 'run', 'task', 'producer', 'epoch',
                                'scope', 'producer_card_missing', 'producer_card_malformed',
                                'producer_task', 'review_epoch', 'review_task'])
def test_corrupt_binding_refuses_before_review_completion(tmp_path, monkeypatch, bad):
    c = Case(tmp_path, monkeypatch); c.produce(); before = len(c.requests)
    if bad == 'missing': c.authority.unlink()
    elif bad == 'malformed': c.authority.write_text('{broken')
    elif bad == 'producer':
        data = json.loads(c.code.read_text()); data['step_instance_id'] += 100
        c.code.write_text(json.dumps(data))
    elif bad in ['run', 'task', 'epoch', 'scope']:
        data = json.loads(c.authority.read_text())
        if bad == 'run': data['run_id'] = 'foreign-run'
        elif bad == 'task': data['task'] = 'sibling'
        elif bad == 'epoch': data['claim_history'][-1]['claim_epoch'] += 1
        else: data['allowed_scope']['owns'] = ['foreign.py']
        c.authority.write_text(json.dumps(data))
    elif bad == 'review_epoch':
        c.review = replace(c.review, token=replace(c.review.token, claim_epoch=c.review.token.claim_epoch + 1))
    elif bad == 'review_task':
        c.review.inputs['_resolved_context']['[current_task]'] = 'sibling'
    else:
        with c.sf._lock:
            row = c.sf._conn.execute('SELECT inputs_json FROM skillflow_steps WHERE id = ?',
                                    (c.producer.token.step_instance_id,)).fetchone()
            data = json.loads(row['inputs_json']); context = data['_resolved_context']
            if bad == 'producer_task': context['[current_task]'] = 'sibling'
            else:
                label = next(k for k in context if k.endswith('tasks/generic_card.json'))
                if bad == 'producer_card_missing': del context[label]
                else: context[label] = '{broken'
            c.sf._conn.execute('UPDATE skillflow_steps SET inputs_json = ? WHERE id = ?',
                              (json.dumps(data), c.producer.token.step_instance_id))
            c.sf._conn.commit()
    with pytest.raises(RuntimeError, match='Task review attribution blocked'):
        c.execute(c.review, [])
    assert len(c.requests) == before
