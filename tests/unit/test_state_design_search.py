"""Real SQLite search semantics and real authenticated HTTP/MCP queries."""
from contextlib import contextmanager
import json
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from api.state_only import create_app
from core.state_commands import READ_REQUESTS, WRITE_REQUESTS, execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_graph import StateGraphError, StateNotFound


TOKEN = 'isolated-design-search-token-' + 'x'*40
HEADERS = {'Authorization': 'Bearer ' + TOKEN}


def add(s, did, expected=0, **patch):
    return s.design.create_revision(**dict(project_id='game', design_id=did, expected_revision=expected,
        title='Training rule', statement='养成练功获得修为，普通移动不要求 Ready。', rationale='明确阶段边界，防止误用。',
        open_questions=[], scope={'phase':'cultivation'}, lifecycle_status='approved') | patch)


def baseline(s, ids, name='b1', previous=None):
    return s.design.create_baseline('game', name, [{'design_id':did,'revision':rev} for did,rev in ids], previous)


@pytest.fixture
def service(tmp_path):
    s=StateService(StateDatabase(str(tmp_path/'state.sqlite')), actor='test-author')
    s.create_project('game','Game')
    return s


def test_current_adopted_and_latest_draft_are_both_returned(service):
    add(service,'training');b=baseline(service,[('training',1)])
    add(service,'training',1,statement='养成练功新方案', lifecycle_status='draft',open_questions=['待审查'])
    result=service.design.search('game','练功')
    assert [(r['revision'],r['adopted'],r['is_latest'],r['lifecycle_status']) for r in result['items']]==[
        (1,True,False,'approved'),(2,False,True,'draft')]
    assert result['baseline_id']=='b1' and result['manifest_hash']==b['manifest_hash']
    assert result['searched_revisions']==2 and len(result['selection_hash'])==64


def test_exact_baseline_never_silently_searches_latest_or_unselected_versions(service):
    add(service,'training');baseline(service,[('training',1)])
    add(service,'training',1,statement='New proposal only',lifecycle_status='draft')
    add(service,'unselected',statement='Another proposal only')
    baseline(service,[('training',2)],'b2','b1')
    old=service.design.search('game','Ready',baseline_id='b1')
    assert len(old['items'])==1 and old['items'][0]['revision']==1
    assert old['items'][0]['adopted'] is True
    assert service.design.search('game','proposal',baseline_id='b1')['items']==[]
    assert len(service.design.search('game','proposal')['items'])==2


def test_no_baseline_searches_latest_only_and_empty_project_is_explicit(service):
    empty=service.design.search('game','something')
    assert empty['items']==[] and empty['baseline_id'] is None and empty['searched_revisions']==0
    add(service,'training');add(service,'training',1);add(service,'training',2,lifecycle_status='historical')
    result=service.design.search('game','练功')
    assert len(result['items'])==1 and result['items'][0]['revision']==3
    assert result['items'][0]['adopted'] is False and result['items'][0]['lifecycle_status']=='historical'


def test_literal_unicode_terms_case_insensitive_and_reasons_cover_all_fields(service):
    add(service,'Ready.Policy',title='READY 中文标题',statement='修为 ledger: 大地图 move',rationale='Straße 确切理由')
    result=service.design.search('game','ready 修为 STRASSE')
    assert len(result['items'])==1
    matched=result['items'][0]['matched_fields']
    assert matched=={'design_id':['ready'],'title':['ready'],'statement':['修为'],'rationale':['strasse']}
    assert service.design.search('game','不存在 READY')['total']==0
    assert service.design.search('game','READY ready')['total']==1


def test_id_and_title_rank_ahead_of_repeated_body_matches_and_ties_are_stable(service):
    add(service,'a-body',statement='needle '*100)
    add(service,'z-title',title='needle')
    add(service,'needle-id')
    add(service,'b-body',statement='needle')
    result=service.design.search('game','needle')
    assert [r['design_id'] for r in result['items']]==['needle-id','z-title','a-body','b-body']
    assert [r['score'] for r in result['items']]==[8,4,2,2]
    page1=service.design.search('game','needle',limit=2)
    page2=service.design.search('game','needle',limit=2,offset=page1['next_offset'])
    assert page1['selection_hash']==page2['selection_hash']
    assert page2['next_offset'] is None and page2['total']==4
    assert page1['truncated'] and page2['truncated']
    assert page1['items']+page2['items']==result['items']
    assert service.design.search('game','needle',offset=100)['items']==[]


def test_scope_is_an_explicit_label_filter_not_automatic_semantic_exclusion(service):
    add(service,'cultivation')
    add(service,'overworld',scope={'phase':'overworld','system':'training'})
    assert service.design.search('game','修为')['total']==2
    assert service.design.search('game','修为',scope={'phase':'overworld'})['items'][0]['design_id']=='overworld'
    assert service.design.search('game','修为',scope={'phase':'all'})['total']==0
    assert service.design.search('game','修为',scope={'unknown':'value'})['total']==0


def test_results_are_compact_do_not_expose_full_body_or_arbitrary_sql(service):
    add(service,'large',statement='修为 '+('private prose '*1000),rationale='Long reasoning '+('r'*10000))
    result=service.design.search('game','修为');item=result['items'][0]
    assert len(item['snippet'])==240 and item['snippet_truncated'] is True
    assert not {'statement','rationale','relations','payload_json'} & item.keys()
    for literal in ["' OR 1=1 --", '%', '*', '" OR "', 'AND', ';DELETE']:
        assert service.design.search('game',literal)['items']==[]
    assert service.design.get_revision('game','large',1)['content']['statement'].startswith(item['snippet'])


def test_project_boundaries_and_unknown_baseline_fail_without_fallback(service):
    add(service,'visible');service.create_project('other','Other')
    service.design.create_revision('other','secret',0,'Secret','ONLY_OTHER_PROJECT','Reason',[],{'scope':'other'})
    assert service.design.search('game','ONLY_OTHER_PROJECT')['items']==[]
    with pytest.raises(StateNotFound):service.design.search('game','Ready',baseline_id='missing')
    with pytest.raises(StateNotFound):service.design.search('missing','Ready')


@pytest.mark.parametrize('args',[
    {'query':''},{'query':' '},{'query':'x'*201},{'query':'1 2 3 4 5 6 7 8 9'},
    {'query':None},{'query':[]},{'query':'Ready','limit':0},{'query':'Ready','limit':101},
    {'query':'Ready','limit':True},{'query':'Ready','offset':-1},{'query':'Ready','offset':False},
    {'query':'Ready','scope':{}},{'query':'Ready','scope':['invalid']},
])
def test_invalid_inputs_explicitly_fail(service,args):
    with pytest.raises(StateGraphError):service.design.search('game',**args)


def test_query_is_read_only_has_constant_query_count_and_no_engine(service,monkeypatch):
    add(service,'a');baseline(service,[('a',1)])
    def forbidden():raise AssertionError('Query must not initialize a workflow engine')
    service.runtime_factory=forbidden
    with service.db.get_connection() as c:before=list(c.iterdump())
    queries=[];real=service.db.get_connection
    @contextmanager
    def counted():
        with real() as c:
            c.set_trace_callback(queries.append);yield c
    monkeypatch.setattr(service.db,'get_connection',counted)
    result=execute(service,'search_design_items',{'project_id':'game','query':'Ready'})
    assert result['total']==1
    # Project, baseline head, baseline row, one joined candidate query. No per-item lookups.
    assert len([q for q in queries if q.lstrip().upper().startswith(('SELECT','WITH'))])<=4
    with real() as c:assert list(c.iterdump())==before
    assert 'search_design_items' in READ_REQUESTS and 'search_design_items' not in WRITE_REQUESTS


def test_thousand_item_candidate_scan_is_bounded_and_measured(service,record_property):
    # One transaction and real immutable payloads; this is a scale fixture, not LLM output.
    from core.state_graph import canonical,digest
    payload=add(service,'seed')['content']
    with service.store.transaction(write=True) as c:
        c.executemany('INSERT INTO state_design_revisions VALUES(?,?,?,?,?,?,?)',[
            ('game',f'item-{i:04}',1,canonical(payload),digest(payload),'scale-fixture','fixture') for i in range(1000)])
    start=perf_counter();result=service.design.search('game','修为',limit=10);elapsed=perf_counter()-start
    record_property('scan_1001_revisions_seconds',elapsed)
    assert result['total']==1001 and len(result['items'])==10 and result['truncated']
    assert elapsed<5,'1001 small revisions should not require a new search service'


def test_real_http_mcp_search_has_auth_strict_input_and_no_write_surface(tmp_path):
    app=create_app(str(tmp_path/'api.sqlite'),TOKEN)
    s=app.state.state_service;s.create_project('game','Game');add(s,'readable')
    with TestClient(app) as c:
        payload={'project_id':'game','query':'修为 Ready'}
        assert c.post('/api/state/query/search_design_items',json=payload).status_code==401
        assert c.post('/api/state/query/search_design_items',json={**payload,'limit':True},headers=HEADERS).status_code==422
        assert c.post('/api/state/query/search_design_items',json={**payload,'sql':'DROP TABLE x'},headers=HEADERS).status_code==422
        assert c.post('/api/state/commands/search_design_items',json=payload,headers=HEADERS).status_code==422
        result=c.post('/api/state/query/search_design_items',json=payload,headers=HEADERS)
        assert result.status_code==200 and result.json()['items'][0]['design_id']=='readable'
        rpc={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'state_graph_read','arguments':{'action':'search_design_items','arguments':payload}}}
        res=c.post('/mcp/',json=rpc,headers={**HEADERS,'Accept':'application/json, text/event-stream'})
        assert res.status_code==200,res.text
        envelope=res.json()['result'];assert not envelope.get('isError'),envelope
        assert json.loads(envelope['content'][0]['text'])['result']==result.json()
        rpc['params']['arguments']['arguments']['baseline_id']='unknown'
        err=c.post('/mcp/',json=rpc,headers={**HEADERS,'Accept':'application/json, text/event-stream'})
        assert err.json()['result']['isError'] is True
