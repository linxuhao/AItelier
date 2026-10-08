"""Actual Bench full replay in normal SkillFlow dispatch, on disposable fiction."""
import json
from pathlib import Path

import pytest
import skillflow
from skillflow import ToolExecutionRefused, SkillFlow, PipelineGraph, StepNode, Transition
from skillflow.graph import EndCondition, EndConditions
from skillflow.tool_loader import ToolLoader

from aitelier import novel_state as ns
from aitelier.writing_bench.bench import Bench
from aitelier.writing_bench.storage import BenchError, BenchReplayRefused, git, lock
from test_bench import bench, request
from test_workflow import Session


def test_actual_workflow_refuses_dead_character_journal_once_before_review(tmp_path, monkeypatch, bench):
    repo = bench.policy.repo
    for n, changes in [(1, {'status': 'dead'}), (2, {'power_level': 2})]:
        chapter = ns.chapter_dir(repo, n)
        chapter.mkdir(parents=True)
        (chapter / 'prose.md').write_text(f'# 第{n}章\n旅人已经付出代价。\n')
        (chapter / 'summary.md').write_text('历史代价必须保留。\n')
        events = [{'entity_type': 'protagonist', 'entity_name': '旅人',
                   'changes': changes, 'reason': 'historical synthetic event'}]
        ns.dump_yaml(chapter / 'events.yaml', {'chapter': n, 'events': events,
                     'appearances': [], 'thread_updates': [], 'arc_updates': []})
        ns.apply_events(repo, events, n)
    ns.rebuild_digest(repo)
    ns.rebuild_index(repo)
    git(repo, 'add', '--', 'novel')
    git(repo, 'commit', '-m', 'synthetic dead-character replay journal')
    baseline = git(repo, 'rev-parse', 'HEAD')
    original = Bench._reset_replay
    calls, reasons = [], []

    def observed_replay(self, wt, genesis_files):
        calls.append(str(wt))
        try:
            return original(self, wt, genesis_files)
        except BenchReplayRefused as exc:
            reasons.append(str(exc))
            assert isinstance(exc, BenchError)
            assert isinstance(exc, ToolExecutionRefused)
            raise

    monkeypatch.setattr(Bench, '_reset_replay', observed_replay)
    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench, n=3))
    try:
        assert session.drive(run) == 'failed'
        for _ in range(4):
            session.sf.advance_run(run)
        assert len(calls) == 1
        assert len(reasons) == 1 and 'is dead but received changes' in reasons[0]
        assert reasons[0].startswith('full replay refused: ')
        assert session.sf.get_run(run)['error_reason'] == reasons[0]
        steps = session.sf.get_steps(run, include_payloads=True)
        refused = next(s for s in steps if s['step_id'] == 'ledger_ready')
        assert refused['status'] == 'failed' and refused['last_error'] == reasons[0]
        assert all(s['status'] == 'pending' for s in steps if s['step_id'] in ('literary_review', 'ledger_audit', 'stage', 'promote', 'backup'))
        assert session.agent_calls == []
        assert session.backup_calls == []
        assert git(repo, 'rev-parse', 'HEAD') == baseline
        assert git(repo, 'status', '--porcelain') == ''
        assert not any(Path(p).exists() for p in calls)
        assert not list((bench.root / 'scratch').glob('candidate-*'))
        assert 'candidate-' not in git(repo, 'worktree', 'list', '--porcelain')
        for name in ('candidate_replay.json', 'literary.json', 'audit.json', 'stage.json', 'accepted.json', 'completed.json'):
            assert not (bench.work(run) / name).exists()
        with lock(bench.root / '.delivery.lock'):
            pass
        with session.sf._ro() as conn:
            assert conn.execute('SELECT COUNT(*) FROM skillflow_active_ops WHERE run_id=?', (run,)).fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM skillflow_edge_counts WHERE run_id=? AND from_step=?', (run, 'ledger_ready')).fetchone()[0] == 0
        print(json.dumps({'sdk_source': skillflow.__file__, 'host_source': ns.__file__,
                          'refusal_calls': len(calls), 'reason': reasons[0], 'run': run}, ensure_ascii=False))
    finally:
        session.sf._conn.close()


def test_plain_bench_error_is_not_definitive_and_can_retry(tmp_path):
    calls = []

    def backup(**kwargs):
        calls.append(kwargs['run_id'])
        if len(calls) == 1:
            raise BenchError('Private backup incomplete; accepted content retained.')
        return {'verified': True}

    loader = ToolLoader()
    sf = SkillFlow(str(tmp_path / 'disposable.sqlite'), tool_loader=loader,
                   workspace_base=str(tmp_path / 'workspace'), projects_base=str(tmp_path / 'projects'))
    loader.register_dynamic_tool('backup', {'name': 'backup', 'parameters': {}}, backup)
    sf.register_agent_config('unused')
    sf.register_graph(PipelineGraph(name='backup', begin='backup', steps=[
        StepNode(id='backup', step_type='tool', tool_name='backup', transitions=[Transition(to='done')]),
        StepNode(id='done', agent_config='unused', transitions=[]),
    ], end_conditions=EndConditions(conditions=[
        EndCondition(type='node_reached', node='done', result='completed')
    ])))
    run = sf.create_run('backup', project_id='disposable')
    sf.start_run(run)
    try:
        assert not issubclass(BenchError, ToolExecutionRefused)
        with pytest.raises(BenchError, match='Private backup incomplete'):
            sf.advance_run(run)
        assert sf.get_run(run)['status'] == 'running'
        sf.advance_run(run)
        assert sf.get_run(run)['status'] == 'completed'
        assert calls == [run, run]
    finally:
        sf._conn.close()
