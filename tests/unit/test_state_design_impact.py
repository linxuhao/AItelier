"""Exact relations and review-only impact against real immutable design rows."""
from contextlib import contextmanager

import pytest

from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_graph import StateConflict, StateGraphError, StateNotFound


SCOPE={'phase':'overworld'}


def relation(kind,target,revision=1,reason='Explicit relation; scope is reviewed, not inferred'):
    return {'type':kind,'target':{'design_id':target,'revision':revision},'rationale':reason}


def add(s,did,expected=0,relations=None,**patch):
    return s.design.create_revision(**(dict(project_id='game',design_id=did,expected_revision=expected,title=did,
        statement='Rule '+did,rationale='Explicit fixture decision',open_questions=[],scope=SCOPE,
        lifecycle_status='approved',relations=relations or [])|patch))


def baseline(s,pins,name='b1',expected=None):
    return s.design.create_baseline('game',name,[{'design_id':did,'revision':rev} for did,rev in pins],expected)


def node(s,key,deps=None):
    s.store.add_nodes('game',[{'key':key,'goal':'Implement '+key,'dependencies':deps or [],'acceptance':[
        {'id':'test','kind':'test','description':'Real behavior accepted separately'}]}])


def bind(s,nk,did,rev=1,bid='b1',expected=1,purpose='implements'):
    return s.design.bind_node('game',nk,expected,bid,[{'design_id':did,'revision':rev,'purpose':purpose,'coverage_scope':SCOPE}],
                             'Explicit test binding, never performed by an impact read')


@pytest.fixture
def service(tmp_path):
    s=StateService(StateDatabase(str(tmp_path/'state.sqlite')),actor='test-author');s.create_project('game','Game')
    return s


def graph(s):
    add(s,'d2',statement='Ready mechanism exists')
    add(s,'d3',relations=[relation('depends_on','d2')],statement='Overworld movement requires Ready')
    add(s,'d1',relations=[relation('conflicts_with','d3')],statement='Overworld movement does not require Ready')
    add(s,'d4',relations=[relation('depends_on','d3')])
    baseline(s,[(k,1) for k in ['d1','d2','d3','d4']])


def test_conflict_alternative_does_not_force_target_into_baseline(service):
    add(service,'alternative',lifecycle_status='draft',open_questions=['Alternative not chosen'])
    add(service,'choice',relations=[relation('conflicts_with','alternative')])
    result=baseline(service,[('choice',1)])
    assert len(result['manifest']['selected_revisions'])==1
    report=service.design.impact('game','choice',1)
    conflict=report['declared_conflicts']['items'][0]
    assert conflict['source_selected'] is True and conflict['target_selected'] is False
    assert conflict['both_selected'] is False
    assert report['dependent_designs']['items']==[]


def test_conflict_both_selected_is_review_warning_not_automatic_rejection(service):
    add(service,'a');add(service,'b',relations=[relation('conflicts_with','a')])
    baseline(service,[('a',1),('b',1)])
    report=service.design.impact('game','a',1)
    assert report['declared_conflicts']['total']==1
    item=report['declared_conflicts']['items'][0]
    assert item['both_selected'] is True and 'review' in item['message'].lower()
    assert item['direction']=='incoming' and item['source']=={'design_id':'b','revision':1}
    assert service.design.get_revision('game','a',1)['content']['relations']==[],'No mirror edge was written'


def test_conflict_is_symmetric_to_query_but_never_transitive(service):
    graph(service)
    d1=service.design.impact('game','d1',1);d2=service.design.impact('game','d2',1);d3=service.design.impact('game','d3',1)
    assert d1['declared_conflicts']['items'][0]['target']['design_id']=='d3'
    assert d3['declared_conflicts']['items'][0]['source']['design_id']=='d1'
    assert d2['declared_conflicts']['items']==[],'D1 excludes D3 and D3 requires D2 does not mean D1 excludes D2'
    assert [r['design_id'] for r in d2['dependent_designs']['items']]==['d3','d4']
    assert d2['dependent_designs']['items'][1]['path']==[
        {'design_id':'d2','revision':1},{'design_id':'d3','revision':1},{'design_id':'d4','revision':1}]
    assert d1['dependent_designs']['items']==[]


def test_references_and_supersedes_never_become_execution_or_dependency_edges(service):
    add(service,'base');add(service,'reader',relations=[relation('references','base')])
    baseline(service,[('base',1),('reader',1)])
    report=service.design.impact('game','base',1)
    assert report['direct_relations']['items'][0]['type']=='references'
    assert report['dependent_designs']['items']==[]
    add(service,'replacement',relations=[relation('supersedes','base')])
    baseline(service,[('replacement',1)],'b2','b1')
    report=service.design.impact('game','base',1)
    assert report['subject']['selected'] is False
    assert report['direct_relations']['items'][0]['type']=='supersedes'
    assert report['dependent_designs']['items']==[]
    with service.db.get_connection() as c:assert c.execute('SELECT COUNT(*) FROM state_dependencies').fetchone()[0]==0


def test_new_revision_never_inherits_old_relations(service):
    add(service,'alternative');add(service,'rule',relations=[relation('conflicts_with','alternative')])
    baseline(service,[('rule',1),('alternative',1)])
    old=service.design.impact('game','rule',1,baseline_id='b1')
    add(service,'rule',1,statement='Revised compatible rule')
    baseline(service,[('rule',2),('alternative',1)],'b2','b1')
    assert service.design.impact('game','rule',2)['declared_conflicts']['items']==[]
    assert service.design.impact('game','alternative',1)['declared_conflicts']['items']==[]
    assert service.design.impact('game','rule',1,baseline_id='b1')==old
    assert service.design.impact('game','rule',1)['declared_conflicts']['items'][0]['both_selected'] is False


@pytest.mark.parametrize('kind',['depends_on','references'])
def test_required_reference_exact_selection_guards_are_not_weakened(service,kind):
    add(service,'a');add(service,'b',relations=[relation(kind,'a')])
    with pytest.raises(StateGraphError,match='exact revision'):baseline(service,[('b',1)])
    add(service,'a',1)
    with pytest.raises(StateGraphError,match='exact revision'):baseline(service,[('a',2),('b',1)])


def test_approved_dependency_and_full_scope_supersession_rules_remain(service):
    add(service,'draft',lifecycle_status='draft');add(service,'dependent',relations=[relation('depends_on','draft')])
    with pytest.raises(StateGraphError,match='non-approved'):baseline(service,[('draft',1),('dependent',1)])
    with pytest.raises(StateGraphError,match='identical scope'):
        add(service,'replacement',relations=[relation('supersedes','draft')],scope={'phase':'cultivation'})
    add(service,'replacement',relations=[relation('supersedes','draft')])
    with pytest.raises(StateConflict,match='supersession'):baseline(service,[('draft',1),('replacement',1)])


def test_invalid_conflict_relations_are_atomic_and_version_scoped(service):
    add(service,'a');service.create_project('other','Other')
    service.design.create_revision('other','foreign',0,'Foreign','Foreign','Reason',[],SCOPE)
    cases=[([relation('conflicts_with','missing')],StateNotFound),
           ([relation('conflicts_with','foreign')],StateNotFound),
           ([relation('conflicts_with','self')],StateGraphError),
           ([relation('conflicts_with','a'),relation('conflicts_with','a')],StateGraphError),
           ([relation('conflicts_with','a',reason=' ')],StateGraphError),
           ([relation('implies_everything','a')],StateGraphError)]
    before=service.store.events('game')
    for relations,error in cases:
        with pytest.raises(error):add(service,'self',relations=relations)
    assert service.store.events('game')==before


def test_latest_bindings_match_exact_item_or_dependency_not_whole_manifest(service):
    graph(service)
    for nk in ['depends','direct','unrelated','downstream-state']:node(service,nk, ['depends'] if nk=='downstream-state' else [])
    bind(service,'depends','d3');bind(service,'direct','d2',purpose='context');bind(service,'unrelated','d1')
    report=service.design.impact('game','d2',1)
    rows={r['node_key']:r for r in report['affected_nodes']['items']}
    assert set(rows)=={'depends','direct'},'Manifest pins and State dependencies are not actual design-body bindings'
    assert rows['depends']['reason']=='bound_dependency' and rows['direct']['purposes']==['context']
    assert all(r['review_only'] and r['matches_requested_baseline'] for r in rows.values())
    service.store.revise_node('game','direct',2,'Changed acceptance contract, binding still current')
    current=service.design.impact('game','d2',1)['affected_nodes']['items']
    row=next(r for r in current if r['node_key']=='direct')
    assert row['node_revision']==3 and row['binding_node_revision']==2 and not row['binding_matches_node_revision']


def test_historical_bindings_remain_but_are_not_misreported_as_current(service):
    add(service,'rule');baseline(service,[('rule',1)]);node(service,'node');bind(service,'node','rule')
    add(service,'rule',1);baseline(service,[('rule',2)],'b2','b1')
    before=service.design.impact('game','rule',1)
    assert before['affected_nodes']['items'][0]['binding_baseline_id']=='b1'
    assert before['affected_nodes']['items'][0]['matches_requested_baseline'] is False
    bind(service,'node','rule',rev=2,bid='b2',expected=2)
    assert service.design.impact('game','rule',1,baseline_id='b1')['affected_nodes']['items']==[]
    with service.db.get_connection() as c:assert c.execute('SELECT COUNT(*) FROM state_design_bindings').fetchone()[0]==2
    assert service.design.impact('game','rule',2)['affected_nodes']['items'][0]['binding_baseline_id']=='b2'


def test_removal_preview_does_not_delete_old_rules_bindings_or_receipts(service):
    add(service,'a');add(service,'b');baseline(service,[('a',1),('b',1)]);node(service,'work');bind(service,'work','a')
    a=service.start_external_attempt('game','work',2,'fixture','job','once')
    service.report_external_attempt(a['attempt_id'],'done',0,a['context_hash'],'candidate','fixture-report','b'*64,True,'a'*64,'sha256')
    service.record_evidence(a['attempt_id'],'e','test','pass','a'*64,'fixture-check','c'*64)
    receipt=service.verify_node('game','work',2,a['attempt_id'])
    report=service.design.impact('game','a',1)
    assert report['affected_nodes']['items'][0]['status']=='VERIFIED'
    baseline(service,[('b',1)],'without-a','b1')
    assert service.design.impact('game','a',1,baseline_id='b1')==report
    assert service.design.impact('game','a',1)['subject']['selected'] is False
    assert service.store.get_node('game','work')['status']=='VERIFIED','Impact must not secretly invalidate or rebind'
    assert service.portfolio.attempt_detail(a['attempt_id'])['receipts'][0]['receipt_id']==receipt['receipt_id']


def test_traversal_and_payload_limits_have_honest_truncation(service):
    add(service,'root')
    for i in range(40):add(service,f'child-{i:02}',relations=[relation('depends_on','root' if i==0 else f'child-{i-1:02}')])
    baseline(service,[('root',1)]+[(f'child-{i:02}',1) for i in range(40)])
    limited=service.design.impact('game','root',1,limit=2,max_visits=3)
    assert limited['traversal']=={'visited':3,'max_visits':3,'truncated':True,'path_limit':32}
    assert limited['dependent_designs']['total_is_exact'] is False
    full=service.design.impact('game','root',1)
    assert full['dependent_designs']['total']==40 and full['dependent_designs']['total_is_exact']
    path=full['dependent_designs']['items'][-1]
    assert path['path_length']==41 and path['path_truncated'] and len(path['path'])==32
    assert path['path'][0]['design_id']=='root' and path['path'][-1]['design_id']=='child-39'


def test_multiple_paths_are_deduplicated_and_deterministic(service):
    add(service,'root');add(service,'a',relations=[relation('depends_on','root')]);add(service,'b',relations=[relation('depends_on','root')])
    add(service,'c',relations=[relation('depends_on','a'),relation('depends_on','b')])
    baseline(service,[(x,1) for x in ['root','a','b','c']])
    report=service.design.impact('game','root',1)
    assert report['dependent_designs']['total']==3
    assert report['dependent_designs']['items'][-1]['path']==[
        {'design_id':v,'revision':1} for v in ['root','a','c']]
    assert service.design.impact('game','root',1)==report


def test_reads_preserve_database_exports_and_never_do_n_plus_one(service,monkeypatch):
    graph(service);node(service,'work');bind(service,'work','d3')
    exported=service.design.export_markdown('game','b1')
    assert 'conflicts_with' in exported['markdown']
    def forbidden():raise AssertionError('Impact must not call a workflow runtime')
    service.runtime_factory=forbidden
    real=service.db.get_connection;queries=[]
    with real() as c:before=list(c.iterdump())
    @contextmanager
    def traced():
        with real() as c:c.set_trace_callback(queries.append);yield c
    monkeypatch.setattr(service.db,'get_connection',traced)
    report=execute(service,'design_impact',{'project_id':'game','design_id':'d2','revision':1,'baseline_id':'b1'})
    assert report['affected_nodes']['total']==1
    assert len([q for q in queries if q.lstrip().upper().startswith(('SELECT','WITH'))])==5
    with real() as c:assert list(c.iterdump())==before
    assert service.design.export_markdown('game','b1')==exported
    assert 'design_impact' in READ_REQUESTS and 'design_impact' not in WRITE_REQUESTS


def test_missing_selection_and_invalid_limits_are_explicit(service):
    add(service,'a')
    with pytest.raises(StateConflict,match='baseline'):service.design.impact('game','a',1)
    with pytest.raises(StateNotFound):service.design.impact('game','a',1,baseline_id='missing')
    baseline(service,[('a',1)])
    for kwargs in [{'max_visits':0},{'max_visits':1001},{'limit':101},{'limit':True}]:
        with pytest.raises(StateGraphError):service.design.impact('game','a',1,**kwargs)
    with pytest.raises(StateNotFound):service.design.impact('game','missing',1)


def test_direct_relations_and_node_results_each_report_their_own_truncation(service):
    add(service,'root')
    for i in range(5):
        add(service,f'conflict-{i}',relations=[relation('conflicts_with','root',reason='r'*500)])
        node(service,f'node-{i}')
    baseline(service,[('root',1)]+[(f'conflict-{i}',1) for i in range(5)])
    for i in range(5):bind(service,f'node-{i}','root')
    result=service.design.impact('game','root',1,limit=2)
    for name in ['direct_relations','declared_conflicts','affected_nodes']:
        assert result[name]['total']==5 and result[name]['truncated'] is True
        assert len(result[name]['items'])==2
    assert result['direct_relations']['items'][0]['rationale_truncated']
    assert len(result['direct_relations']['items'][0]['rationale'])==360
    assert result['dependent_designs']['items']==[]


def test_unselected_incoming_revision_cannot_leak_into_current_impact(service):
    add(service,'root');add(service,'other',relations=[relation('conflicts_with','root')])
    baseline(service,[('root',1)])
    assert service.design.impact('game','root',1)['declared_conflicts']['items']==[]
    # The unselected author's explicit statement is still readable by its own exact identity.
    assert service.design.impact('game','other',1)['declared_conflicts']['items'][0]['source_selected'] is False
