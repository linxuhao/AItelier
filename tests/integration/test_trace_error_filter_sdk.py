"""Use the installed engine's real run resolution and trace_query over its SQLite DB."""
import json
from unittest.mock import MagicMock

import pytest
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core.meta_agent import MetaAgent
from core.trace_reader import trace_list


@pytest.mark.parametrize('order', ['asc', 'desc'])
@pytest.mark.parametrize('via_butler', [False, True])
def test_errors_only_reaches_late_matches_through_real_sdk(tmp_path, monkeypatch, order, via_butler):
    sf = SkillFlow(str(tmp_path / 'sdk.db'), workspace_base=str(tmp_path / 'artifacts'),
                   artifact_history=False)
    try:
        sf.register_graph(PipelineGraph(name='trace_fixture', begin='done', steps=[
            StepNode(id='done', step_type='gate')]))
        run = sf.create_run('trace_fixture', {'project_id': 'trace-project'},
                            project_id='trace-project')
        other = sf.create_run('trace_fixture', {'project_id': 'other-project'},
                              project_id='other-project')
        # Populate only this disposable fixture DB. All reads use actual SDK APIs.
        sf._conn.execute('DELETE FROM skillflow_trace WHERE run_id IN (?, ?)', (run, other))
        payloads = [{'error': 'first failure'}]
        payloads += [{'text': 'error false failed', 'error': '', 'passed': True} for _ in range(300)]
        payloads += [{'status': 'failed', 'detail': 'late native failure'},
                     {'passed': False}, r'{"\u0065rror":"escaped key failure"}']
        for seq, payload in enumerate(payloads, 1):
            sf._conn.execute('INSERT INTO skillflow_trace '
                             '(run_id, step_id, seq, category, event, payload_json) VALUES (?,?,?,?,?,?)',
                             (run, '2', seq, 'step', 'fixture',
                              payload if isinstance(payload, str) else json.dumps(payload)))
        for rid, step, cat in [(other, '2', 'step'), (run, '1', 'step'), (run, '2', 'response')]:
            sf._conn.execute('INSERT INTO skillflow_trace '
                             '(run_id, step_id, seq, category, event, payload_json) VALUES (?,?,?,?,?,?)',
                             (rid, step, 1000 if step == '1' else 1001, cat, 'fixture',
                              json.dumps({'error': 'outside requested scope'})))
        sf._conn.commit()
        before = sf._conn.execute('SELECT * FROM skillflow_trace ORDER BY id').fetchall()
        if via_butler:
            import api.dependencies as deps
            monkeypatch.setattr(deps, 'get_skillflow', lambda: sf)
            agent = MetaAgent(MagicMock(), MagicMock(), owner_email='fixture@example.invalid')
            result = agent._tool_trace_list({'run': 'trace-project', 'step': '2',
                                            'category': 'step', 'errors_only': True,
                                            'order': order, 'limit': 12})
        else:
            result = trace_list(sf, 'trace-project', step='2', category='step',
                                errors_only=True, order=order, limit=12)
        wanted = ['first failure', 'late native failure', 'passed=false', 'escaped key failure']
        assert [e['summary'] for e in result['entries']] == (wanted if order == 'asc' else wanted[::-1])
        assert result['run_id'] == run and result['resolved']['run_id'] == run
        assert result['count'] == 4
        after = sf._conn.execute('SELECT * FROM skillflow_trace ORDER BY id').fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
    finally:
        sf._conn.close()
