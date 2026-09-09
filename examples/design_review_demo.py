#!/usr/bin/env python3
"""Literal search -> reviewed direct relation -> impact -> explicit adoption.

Run: python examples/design_review_demo.py --report /tmp/design-review.json
Uses actual authenticated State-only HTTP APIs and temporary SQLite. A scripted
review fixture selects the relationship; this is NOT an LLM semantic benchmark,
a whole-project consistency proof, or a migration of the real game.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from api.state_only import create_app


SCOPE = {'phase': 'overworld'}


def run_demo():
    with tempfile.TemporaryDirectory(prefix='design-review-demo-') as folder:
        token = secrets.token_urlsafe(40)
        app = create_app(str(Path(folder)/'state.sqlite'), token)
        headers = {'Authorization': 'Bearer ' + token}
        with TestClient(app) as client:
            def call(kind, action, args):
                response = client.post('/api/state/'+kind+'/'+action, json=args, headers=headers)
                if response.status_code != 200:
                    raise AssertionError(action + ': ' + response.text)
                return response.json()
            def write(action, **args):
                return call('commands', action, {'project_id':'demo', **args})
            def read(action, **args):
                return call('query', action, {'project_id':'demo', **args})
            def rel(kind, target):
                return {'type':kind, 'target':{'design_id':target,'revision':1},
                        'rationale':'Explicit reviewer fixture: ordinary map movement and month settlement are distinct operations.'}
            def item(did, title, statement, relations=None, scope=None):
                return write('create_design_revision', design_id=did, expected_revision=0, title=title,
                             statement=statement, rationale='Synthetic example for an API integration test.',
                             open_questions=[], scope=scope or SCOPE, lifecycle_status='approved', relations=relations or [])
            write('create_project', title='Design review demonstration')
            write('add_nodes', nodes=[{'key':'movement','goal':'Implement ordinary movement','acceptance':[
                {'id':'behavior','kind':'test','description':'Run actual movement tests before accepting this goal'}]}])
            item('ready-mechanism','Ready mechanism','Ready synchronization is available for operations that require it.')
            item('movement-ready','普通移动 requires Ready','大地图普通移动必须等待全员 Ready。',[rel('depends_on','ready-mechanism')])
            item('month-ready','养成月结 uses Ready','养成月结使用 Ready；本条不决定大地图普通移动的规则。',[rel('references','ready-mechanism')],scope={'phase':'cultivation'})
            initial = [{'design_id':v,'revision':1} for v in ['ready-mechanism','movement-ready','month-ready']]
            write('create_design_baseline', baseline_id='before', selected_revisions=initial)
            def bind(bid, did, expected):
                return write('bind_node_design', node_key='movement', expected_revision=expected, baseline_id=bid,
                             reason='Explicit reviewer adoption; never triggered by search or impact.',
                             bindings=[{'design_id':did,'revision':1,'purpose':'implements','coverage_scope':SCOPE}])
            bind('before','movement-ready',1)
            candidates = read('search_design_items', query='Ready', baseline_id='before')
            assert candidates['total'] == 3
            exact = [read('get_design_revision',design_id=r['design_id'],revision=r['revision']) for r in candidates['items']]
            assert all('content' in r for r in exact)
            # This is the reviewer's explicit decision, not a similarity score
            # or an inference by the server. The month rule is only related.
            item('free-movement','普通移动 no Ready','大地图普通移动不要求全员 Ready。',
                 [rel('conflicts_with','movement-ready'), rel('references','month-ready')])
            proposed = read('design_impact', design_id='free-movement', revision=1, baseline_id='before')
            assert proposed['subject']['selected'] is False
            assert proposed['declared_conflicts']['total'] == 1
            assert proposed['declared_conflicts']['items'][0]['target']['design_id'] == 'movement-ready'
            assert read('design_impact',design_id='ready-mechanism',revision=1,baseline_id='before')['declared_conflicts']['items'] == []
            old = read('design_impact', design_id='movement-ready', revision=1, baseline_id='before')
            assert old['affected_nodes']['items'][0]['node_key'] == 'movement'
            exported = read('export_design_markdown', baseline_id='before')
            chosen = [{'design_id':v,'revision':1} for v in ['ready-mechanism','month-ready','free-movement']]
            write('create_design_baseline', baseline_id='after', selected_revisions=chosen, expected_baseline_id='before')
            assert read('design_impact',design_id='movement-ready',revision=1,baseline_id='before') == old
            assert read('export_design_markdown',baseline_id='before') == exported
            pending = read('get_design_bindings', node_key='movement')
            assert pending['baseline_review_required'] is True
            assert pending['design_context']['baseline_id'] == 'before'
            assert read('get_node',node_key='movement')['node']['revision'] == 2
            bind('after','free-movement',2)
            after = read('design_impact',design_id='movement-ready',revision=1,baseline_id='before')
            assert after['direct_relations'] == old['direct_relations']
            assert after['dependent_designs'] == old['dependent_designs']
            assert after['affected_nodes']['items'] == []  # latest bindings intentionally reflect the explicit rebind
            assert read('get_node',node_key='movement')['node']['revision'] == 3
            assert read('get_design_revision',design_id='movement-ready',revision=1)['content']['statement'].endswith('Ready。')
            assert read('get_graph')['nodes'][0]['status'] != 'VERIFIED'
        with app.state.state_service.db.get_connection() as conn:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'runs' not in tables and not any(t.startswith('skillflow') for t in tables)
            assert conn.execute('SELECT COUNT(*) FROM state_attempts').fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM state_acceptances').fetchone()[0] == 0
        assert not any(name.startswith('skillflow') for name in sys.modules)
        return {'result':'PASS', 'mode':'Authenticated real State-only HTTP and temporary SQLite',
                'matching_candidates':[(r['design_id'],r['revision']) for r in candidates['items']],
                'review_fixture':'Only ordinary movement rules conflict; monthly Ready use is a reference.',
                'direct_conflicts':proposed['declared_conflicts']['total'],
                'conflict_did_not_require_adopting_other_side':True,
                'old_baseline_and_markdown_preserved':True,'binding_changes_only_by_explicit_command':True,
                'new_attempts':0,'new_acceptances':0,'workflow_runtime_imported':False,'production_data_used':False,
                'not_claimed':'No automatic natural-language understanding or full-system conflict proof.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    result=run_demo(); output=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.report:args.report.write_text(output,encoding='utf-8')
    print(output,end='')
    return 0


if __name__=='__main__':raise SystemExit(main())
