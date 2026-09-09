"""Full read/review/adopt chain on actual authenticated State-only transports."""
import json
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.state_only import create_app
from api.state_http import create_state_router
from api import authz

TOKEN='isolated-design-flow-'+('x'*48)
HEADERS={'Authorization':'Bearer '+TOKEN,'Accept':'application/json, text/event-stream'}


def relation(kind,did):
    return {'type':kind,'target':{'design_id':did,'revision':1},'rationale':'Explicit test reviewer decision, not model inference.'}


def item(did,rels=None):
    return {'project_id':'p','design_id':did,'expected_revision':0,'title':did+' Ready',
            'statement':'普通移动 Ready 规则 '+did,'rationale':'Review fixture','open_questions':[],
            'scope':{'phase':'overworld'},'lifecycle_status':'approved','relations':rels or []}


def test_real_mcp_review_flow_explicit_relations_and_immutable_old_baseline(tmp_path):
    app=create_app(str(tmp_path/'state.sqlite'),TOKEN)
    with TestClient(app) as client:
        def rpc(tool,action,args,expect_error=False):
            response=client.post('/mcp/',headers=HEADERS,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':tool,'arguments':{'action':action,'arguments':args}}})
            assert response.status_code==200,response.text
            result=response.json()['result']
            assert bool(result.get('isError'))==expect_error,result
            return result if expect_error else json.loads(result['content'][0]['text'])['result']
        def write(action,**args):return rpc('state_graph_write',action,{'project_id':'p',**args})
        def read(action,**args):return rpc('state_graph_read',action,{'project_id':'p',**args})
        write('create_project',title='Fixture')
        for payload in [item('ready'),item('old',[relation('depends_on','ready')]),item('monthly',[relation('references','ready')])]:
            rpc('state_graph_write','create_design_revision',payload)
        write('create_design_baseline',baseline_id='b1',selected_revisions=[{'design_id':d,'revision':1} for d in ['ready','old','monthly']])
        found=read('search_design_items',query='Ready',limit=10)
        assert {r['design_id'] for r in found['items']}=={'ready','old','monthly'}
        read('get_design_revision',design_id='old',revision=1)
        rpc('state_graph_write','create_design_revision',item('new',[relation('conflicts_with','old'),relation('references','monthly')]))
        proposed=read('design_impact',design_id='new',revision=1,baseline_id='b1')
        assert proposed['declared_conflicts']['total']==1 and not proposed['subject']['selected']
        assert read('design_impact',design_id='ready',revision=1)['declared_conflicts']['total']==0
        before=read('design_impact',design_id='old',revision=1,baseline_id='b1')
        old_markdown=read('export_design_markdown',baseline_id='b1')
        write('create_design_baseline',baseline_id='b2',expected_baseline_id='b1',
              selected_revisions=[{'design_id':d,'revision':1} for d in ['ready','new','monthly']])
        assert read('design_impact',design_id='old',revision=1,baseline_id='b1')==before
        assert read('export_design_markdown',baseline_id='b1')==old_markdown
        assert read('design_impact',design_id='new',revision=1)['declared_conflicts']['items'][0]['target_selected'] is False
        rpc('state_graph_read','design_impact',{'project_id':'p','design_id':'new','revision':True},expect_error=True)
        rpc('state_graph_read','design_impact',{'project_id':'p','design_id':'new','revision':1,'baseline_id':'missing'},expect_error=True)
        rpc('state_graph_read','create_design_revision',item('must-not-write'),expect_error=True)
        assert read('search_design_items',query='must-not-write')['total']==0
    with app.state.state_service.db.get_connection() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'runs' not in tables and not any(t.startswith('skillflow') for t in tables)
        assert c.execute('SELECT COUNT(*) FROM state_attempts').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM state_dependencies').fetchone()[0]==0


def test_shared_app_writer_authorization_applies_to_both_new_queries(tmp_path,monkeypatch):
    standalone=create_app(str(tmp_path/'state.sqlite'),TOKEN,with_mcp=False)
    service=standalone.state.state_service;service.create_project('p','Private')
    service.design.create_revision(**item('private'))
    service.design.create_baseline('p','b1',[{'design_id':'private','revision':1}])
    app=FastAPI();app.include_router(create_state_router(lambda:service,authz.require_writer))
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz,'request_can_write',lambda request:False)
    cases=[('search_design_items',{'project_id':'p','query':'Ready'}),
           ('design_impact',{'project_id':'p','design_id':'private','revision':1})]
    with TestClient(app) as client:
        for name,args in cases:
            response=client.post('/api/state/query/'+name,json=args)
            assert response.status_code==403,response.text
            assert 'Private' not in response.text
        app.dependency_overrides[authz.require_writer]=lambda:None
        for name,args in cases:
            response=client.post('/api/state/query/'+name,json=args)
            assert response.status_code==200,response.text
            assert client.post('/api/state/query/'+name,json={**args,'force':True}).status_code==422
            assert client.post('/api/state/commands/'+name,json=args).status_code==422


def test_new_queries_remain_read_only_on_held_project_with_verified_node(tmp_path):
    app=create_app(str(tmp_path/'state.sqlite'),TOKEN,with_mcp=False);s=app.state.state_service
    s.create_project('p','Private');s.design.create_revision(**item('rule'));s.design.create_baseline('p','b1',[{'design_id':'rule','revision':1}])
    s.store.add_nodes('p',[{'key':'node','goal':'Node','acceptance':[{'id':'check','kind':'test','description':'Fixture test'}]}])
    s.design.bind_node('p','node',1,'b1',[{'design_id':'rule','revision':1,'purpose':'implements','coverage_scope':{'phase':'overworld'}}],'Explicit fixture bind')
    a=s.start_external_attempt('p','node',2,'fixture','job','once')
    s.report_external_attempt(a['attempt_id'],'done',0,a['context_hash'],'candidate','fixture-report','a'*64,True,'b'*64,'sha256')
    s.record_evidence(a['attempt_id'],'e','check','pass','b'*64,'fixture-report','c'*64)
    s.verify_node('p','node',2,a['attempt_id']);s.portfolio.set_dispatch('p','hold',0,'No further work')
    with s.db.get_connection() as c:before=list(c.iterdump())
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post('/api/state/query/search_design_items',headers=HEADERS,json={'project_id':'p','query':'Ready'}).status_code==200
            result=client.post('/api/state/query/design_impact',headers=HEADERS,json={'project_id':'p','design_id':'rule','revision':1})
            assert result.status_code==200 and result.json()['affected_nodes']['items'][0]['status']=='VERIFIED'
    with s.db.get_connection() as c:assert list(c.iterdump())==before


def test_executable_http_demo_with_forbidden_workflow_imports(tmp_path):
    source=Path(__file__).resolve().parents[2];report=tmp_path/'demo.json'
    script=r'''
import importlib.abc,runpy,sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.startswith('skillflow') or fullname in {'api.dependencies','core.db_manager','core.config_registry','core.workspace_manager'}:
            raise AssertionError('Unexpected runtime import: '+fullname)
sys.meta_path.insert(0,Guard())
sys.argv=['examples/design_review_demo.py','--report',sys.argv[1]]
runpy.run_path('examples/design_review_demo.py',run_name='__main__')
'''
    result=subprocess.run([sys.executable,'-B','-c',script,str(report)],cwd=source,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    data=json.loads(report.read_text());assert data['result']=='PASS'
    assert data['new_attempts']==data['new_acceptances']==0 and not data['workflow_runtime_imported']
    assert data['direct_conflicts']==1 and data['binding_changes_only_by_explicit_command']


def test_no_new_infrastructure_is_needed_for_reads(tmp_path):
    from core.state_commands import describe
    app=create_app(str(tmp_path/'state.sqlite'),TOKEN,with_mcp=False)
    s=app.state.state_service;s.create_project('p','Empty')
    with s.db.get_connection() as c:tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    assert not any('search' in t or 'relation' in t or 'fts' in t for t in tables)
    schemas=describe()['operations']
    for name in ['search_design_items','design_impact']:assert schemas[name]['mutates'] is False
    assert 'sql' not in schemas['search_design_items']['arguments']['properties']
    assert 'force' not in schemas['design_impact']['arguments']['properties']
