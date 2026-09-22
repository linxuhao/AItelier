"""HTTP/MCP and executor-independent validation, with real SQLite state."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.state_only import create_app
from api.state_http import create_state_router
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

TOKEN='isolated-test-state-token-'+'x'*40
HEADER={'Authorization':'Bearer '+TOKEN}
ARTIFACT=hashlib.sha256(b'actual artifact fixture').hexdigest()
REPORT=hashlib.sha256(b'actual report fixture').hexdigest()
EVIDENCE_REPORT=hashlib.sha256(b'independent evidence fixture').hexdigest()

def candidate_report(attempt, label='final-1'):
    return hashlib.sha256((attempt['attempt_id'] + ':' + label).encode()).hexdigest()


def spec(key, deps=None):
    return {'key':key,'goal':'Deliver '+key,'dependencies':deps or [],'acceptance':[
        {'id':'test','kind':'test','description':'Run the actual behaviour test'},
        {'id':'review','kind':'review','description':'Review the artifact and report'}]}


def command(client, action, args):
    result=client.post('/api/state/commands/'+action,json=args,headers=HEADER)
    assert result.status_code==200,result.text
    return result.json()


def project(client):
    command(client,'create_project',{'project_id':'game','title':'External game'})
    command(client,'add_nodes',{'project_id':'game','nodes':[spec('a'),spec('b',['a'])]})


def register(client, key='a', request='first', eid='director/session-1/subagent-7'):
    return command(client,'start_external_attempt',{'project_id':'game','node_key':key,'expected_revision':1,
        'harness':'director-subagents','external_id':eid,'request_key':request})


def complete(client,a):
    report_path = Path(client.app.state.state_service.db.db_path).parent/(a['attempt_id']+'-final.txt')
    body = json.dumps({'attempt':a['attempt_id'],'observation':'final-1',
                       'status':'candidate','settled':True,'usable':True},
                      sort_keys=True).encode()
    report_path.write_bytes(body)
    return command(client,'report_external_attempt',{'attempt_id':a['attempt_id'],'observation_id':'final-1',
        'expected_version':a['observation_version'],'context_hash':a['context_hash'],'status':'candidate',
        'artifact':ARTIFACT,'artifact_kind':'sha256','report_ref':str(report_path),
        'report_sha256':hashlib.sha256(body).hexdigest(),'quiescent':True})


def fail(client,a):
    report_path = Path(client.app.state.state_service.db.db_path).parent/(a['attempt_id']+'-failed.json')
    body = json.dumps({'attempt':a['attempt_id'],'observation':'failed-1',
                       'status':'failed','settled':True,'usable':True},
                      sort_keys=True).encode()
    report_path.write_bytes(body)
    return command(client,'report_external_attempt',{'attempt_id':a['attempt_id'],'observation_id':'failed-1',
        'expected_version':a['observation_version'],'context_hash':a['context_hash'],'status':'failed',
        'report_ref':str(report_path),'report_sha256':hashlib.sha256(body).hexdigest(),
        'quiescent':True,'detail':'One criterion failed; scoped evidence follows'})


def evidence(client,a,check,verdict='pass',suffix=''):
    body=json.dumps({'status':'completed','settled':True,'usable':True,
                     'verdict':verdict,'criterion_id':check,
                     'artifact':ARTIFACT,'suffix':suffix},sort_keys=True).encode()
    path=Path(client.app.state.state_service.db.db_path).parent/(a['attempt_id']+'-'+check+suffix+'-evidence.json')
    path.write_bytes(body)
    return command(client,'record_evidence',{'attempt_id':a['attempt_id'],'evidence_id':a['attempt_id']+'-'+check+suffix,
        'criterion_id':check,'verdict':verdict,'artifact':ARTIFACT,'report_ref':str(path),
        'report_sha256':hashlib.sha256(body).hexdigest(),'detail':'External test/review fixture; not a production game acceptance'})


def target(a):return {'project_id':'game','node_key':a['node_key'],'expected_revision':a['node_revision'],'attempt_id':a['attempt_id']}


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path/'only-state.sqlite'),TOKEN)


def test_real_state_only_http_flow_persists_and_never_creates_workflow_tables(app,tmp_path):
    with TestClient(app) as client:
        assert client.get('/health').json()['workflow_runtime'] is False
        project(client);a=register(client)
        assert a['workflow'] is a['run_id'] is a['execution_project_id'] is None
        for action in ['recover_attempt','reconcile_attempt']:
            assert command(client,action,{'attempt_id':a['attempt_id']})['execution_kind']=='external'
        a=complete(client,a)
        assert client.post('/api/state/commands/verify_node',json=target(a),headers=HEADER).status_code==409
        evidence(client,a,'test','fail');evidence(client,a,'review')
        assert client.post('/api/state/commands/verify_node',json=target(a),headers=HEADER).status_code==409
        assert client.get('/api/state/projects/game/frontier',headers=HEADER).json()['nodes'][0]['node_key']=='a'
        evidence(client,a,'test','pass','-corrected')
        receipt=command(client,'verify_node',target(a))
        assert json.loads(receipt['provenance_json'])['harness']=='director-subagents'
        assert client.get('/api/state/projects/game/frontier',headers=HEADER).json()['nodes'][0]['node_key']=='b'
        detail=client.get('/api/state/attempts/'+a['attempt_id']+'/detail',headers=HEADER).json()
        assert detail['external_observations'][0]['status']=='candidate'
        assert len(detail['evidence'])==3
        observation=command(client,'refresh_project',{'project_id':'game'})
        assert observation['results'][0]['status']=='candidate'
        view=client.get('/api/state/projects/game/overview',headers=HEADER).json()
        assert view['nodes'][0]['latest_attempt']['execution_kind']=='external'
    restarted=create_app(str(tmp_path/'only-state.sqlite'),TOKEN)
    with TestClient(restarted) as client:
        assert client.get('/api/state/projects/game/frontier',headers=HEADER).json()['nodes'][0]['node_key']=='b'
        assert client.get('/api/state/attempts/'+a['attempt_id'],headers=HEADER).json()['external_id']==a['external_id']
    with app.state.state_service.db.get_connection() as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'runs' not in tables and 'tasks' not in tables
        assert not any(t.startswith('skillflow') for t in tables)
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_failed_quiescent_external_attempt_retains_scoped_evidence_without_promotion(app):
    with TestClient(app) as client:
        project(client)
        attempt = fail(client, register(client))

        skip_body = json.dumps({'status':'completed','settled':True,'usable':True,
                                'verdict':'skip','criterion_id':'test',
                                'artifact':ARTIFACT}, sort_keys=True).encode()
        skip_path = Path(client.app.state.state_service.db.db_path).parent/'failed-skip.json'
        skip_path.write_bytes(skip_body)
        skipped = client.post('/api/state/commands/record_evidence', json={
            'attempt_id':attempt['attempt_id'],'evidence_id':'failed-skip',
            'criterion_id':'test','verdict':'skip','artifact':ARTIFACT,
            'report_ref':str(skip_path),'report_sha256':hashlib.sha256(skip_body).hexdigest(),
        }, headers=HEADER)
        assert skipped.status_code == 422
        assert 'pass or fail' in skipped.text

        failed = evidence(client, attempt, 'test', verdict='fail')
        passed = evidence(client, attempt, 'review')
        assert failed['verdict'] == 'fail'
        assert passed['verdict'] == 'pass'
        assert evidence(client, attempt, 'review') == passed

        current = client.get(
            '/api/state/attempts/' + attempt['attempt_id'], headers=HEADER
        ).json()
        assert current['status'] == 'failed'
        assert current['artifact_ref'] is None
        graph = client.get('/api/state/projects/game', headers=HEADER).json()
        assert next(n for n in graph['nodes'] if n['node_key'] == 'a')['status'] == 'OPEN'
        assert client.post(
            '/api/state/commands/verify_node', json=target(attempt), headers=HEADER
        ).status_code == 409
        duplicate_body = json.dumps({'status':'completed','settled':True,'usable':True,
                                     'verdict':'pass','criterion_id':'test',
                                     'artifact':ARTIFACT}, sort_keys=True).encode()
        duplicate_path = Path(client.app.state.state_service.db.db_path).parent/'failed-duplicate.json'
        duplicate_path.write_bytes(duplicate_body)
        duplicate = client.post('/api/state/commands/record_evidence', json={
            'attempt_id':attempt['attempt_id'],'evidence_id':'failed-test-second',
            'criterion_id':'test','verdict':'pass','artifact':ARTIFACT,
            'report_ref':str(duplicate_path),
            'report_sha256':hashlib.sha256(duplicate_body).hexdigest(),
        }, headers=HEADER)
        assert duplicate.status_code == 409
        assert 'exactly one row' in duplicate.text
        rejected = client.post(
            '/api/state/commands/verify_node', json=target(attempt), headers=HEADER
        )
        assert rejected.status_code == 409
        assert 'candidate' in rejected.text
        assert client.get(
            '/api/state/attempts/' + attempt['attempt_id'], headers=HEADER
        ).json()['artifact_ref'] is None

        wrong_artifact = 'd' * 64
        body = json.dumps({'status':'completed','settled':True,'usable':True,
                           'verdict':'pass','criterion_id':'review',
                           'artifact':wrong_artifact}, sort_keys=True).encode()
        path = Path(client.app.state.state_service.db.db_path).parent/'wrong-artifact.json'
        path.write_bytes(body)
        rejected = client.post('/api/state/commands/record_evidence', json={
            'attempt_id':attempt['attempt_id'],'evidence_id':'wrong-artifact',
            'criterion_id':'review','verdict':'pass','artifact':wrong_artifact,
            'report_ref':str(path),'report_sha256':hashlib.sha256(body).hexdigest(),
        }, headers=HEADER)
        assert rejected.status_code == 409
        assert 'same artifact' in rejected.text

        register(client, request='replacement', eid='director/session-2/subagent-8')
        late = dict(json.loads(body))
        late['artifact'] = ARTIFACT
        late_body = json.dumps(late, sort_keys=True).encode()
        late_path = Path(client.app.state.state_service.db.db_path).parent/'late-evidence.json'
        late_path.write_bytes(late_body)
        rejected = client.post('/api/state/commands/record_evidence', json={
            'attempt_id':attempt['attempt_id'],'evidence_id':'late-old-attempt',
            'criterion_id':'review','verdict':'pass','artifact':ARTIFACT,
            'report_ref':str(late_path),
            'report_sha256':hashlib.sha256(late_body).hexdigest(),
        }, headers=HEADER)
        assert rejected.status_code == 409
        assert 'newer attempt' in rejected.text


def test_failed_external_evidence_refuses_a_revised_contract(app):
    with TestClient(app) as client:
        project(client)
        attempt = fail(client, register(client))
        command(client, 'revise_node', {
            'project_id':'game','node_key':'a','expected_revision':1,
            'reason':'Acceptance wording changed after the failed run',
            'acceptance':[
                {'id':'test','kind':'test','description':'Run the revised actual behaviour test'},
                {'id':'review','kind':'review','description':'Review the revised artifact and report'},
            ],
        })
        body = json.dumps({'status':'completed','settled':True,'usable':True,
                           'verdict':'fail','criterion_id':'test',
                           'artifact':ARTIFACT}, sort_keys=True).encode()
        path = Path(client.app.state.state_service.db.db_path).parent/'stale-contract.json'
        path.write_bytes(body)
        rejected = client.post('/api/state/commands/record_evidence', json={
            'attempt_id':attempt['attempt_id'],'evidence_id':'stale-contract',
            'criterion_id':'test','verdict':'fail','artifact':ARTIFACT,
            'report_ref':str(path),'report_sha256':hashlib.sha256(body).hexdigest(),
        }, headers=HEADER)
        assert rejected.status_code == 409
        assert 'current contract' in rejected.text


def test_failed_external_all_pass_rows_still_cannot_verify(app):
    with TestClient(app) as client:
        project(client)
        attempt = fail(client, register(client))
        evidence(client, attempt, 'test', verdict='pass')
        evidence(client, attempt, 'review', verdict='pass')
        rejected = client.post('/api/state/commands/verify_node', json=target(attempt), headers=HEADER)
        assert rejected.status_code == 409
        assert 'candidate' in rejected.text
        current = client.get('/api/state/attempts/' + attempt['attempt_id'], headers=HEADER).json()
        assert current['status'] == 'failed' and current['artifact_ref'] is None


def test_state_only_authorization_covers_discovery_queries_and_writes(app):
    with TestClient(app) as client:
        for path in ['/openapi.json','/api/state/schema','/api/state/projects','/mcp/']:
            assert client.get(path).status_code==401
            assert client.get(path,headers={'Authorization':'Bearer wrong'}).status_code==401
            assert client.get(path+'?token='+TOKEN).status_code==401
        assert client.post('/api/state/commands/create_project',json={'project_id':'secret','title':'Secret'}).status_code==401
        assert client.get('/api/state/schema',headers=HEADER).status_code==200
        assert client.get('/api/state/projects',headers=HEADER).json()['projects']==[]


def rpc(client,name,args,headers=None):
    return client.post('/mcp/',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}},
                       headers={**(HEADER if headers is None else headers),'Content-Type':'application/json','Accept':'application/json, text/event-stream'})


def test_failed_evidence_duplicate_criterion_and_skip_refuse_over_mcp(app):
    with TestClient(app) as client:
        project(client)
        attempt = fail(client, register(client))

        def refused(verdict, evidence_id, suffix):
            body = json.dumps({'status':'completed','settled':True,'usable':True,
                               'verdict':verdict,'criterion_id':'test',
                               'artifact':ARTIFACT}, sort_keys=True).encode()
            path = Path(app.state.state_service.db.db_path).parent/('mcp-failed-'+suffix+'.json')
            path.write_bytes(body)
            result = rpc(client, 'state_graph_write', {'action':'record_evidence','arguments':{
                'attempt_id':attempt['attempt_id'],'evidence_id':evidence_id,
                'criterion_id':'test','verdict':verdict,'artifact':ARTIFACT,
                'report_ref':str(path),'report_sha256':hashlib.sha256(body).hexdigest(),
            }}).json()['result']
            assert result['isError'] is True
            return result['content'][0]['text']

        assert 'pass or fail' in refused('skip', 'mcp-failed-skip', 'skip')
        evidence(client, attempt, 'test', verdict='fail')
        assert 'exactly one row' in refused('pass', 'mcp-failed-duplicate', 'duplicate')


def test_real_state_only_mcp_can_certify_external_attempt_and_returns_errors(app):
    with TestClient(app) as client:
        def tool(action,args):
            result=rpc(client,'state_graph_write',{'action':action,'arguments':args})
            assert result.status_code==200,result.text
            envelope=result.json()['result'];assert not envelope.get('isError'),envelope
            return json.loads(envelope['content'][0]['text'])['result']
        assert rpc(client,'state_graph_help',{},headers={}).status_code==401
        h=rpc(client,'state_graph_help',{}).json()['result']
        schema=json.loads(h['content'][0]['text'])
        assert 'start_external_attempt' in schema['operations'] and 'report_external_attempt' in schema['operations']
        tool('create_project',{'project_id':'game','title':'MCP external'})
        tool('add_nodes',{'project_id':'game','nodes':[spec('a')]})
        a=tool('start_external_attempt',{'project_id':'game','node_key':'a','expected_revision':1,'harness':'ci','external_id':'job/42','request_key':'k'})
        report_path=Path(app.state.state_service.db.db_path).parent/'mcp-report.txt'
        report_body=json.dumps({'status':'candidate','settled':True,'usable':True}).encode()
        report_path.write_bytes(report_body)
        a=tool('report_external_attempt',{'attempt_id':a['attempt_id'],'observation_id':'finish','expected_version':0,
          'context_hash':a['context_hash'],'status':'candidate','artifact':ARTIFACT,'artifact_kind':'sha256',
          'report_ref':str(report_path),'report_sha256':hashlib.sha256(report_body).hexdigest(),'quiescent':True})
        for check in ['test','review']:
            evidence_path=Path(app.state.state_service.db.db_path).parent/('mcp-'+check+'.json')
            evidence_path.write_text(json.dumps({'status':'completed','settled':True,
                                                  'usable':True,'verdict':'pass',
                                                  'criterion_id':check}))
            tool('record_evidence',{'attempt_id':a['attempt_id'],'evidence_id':check,'criterion_id':check,'verdict':'pass','artifact':ARTIFACT,
                 'report_ref':str(evidence_path),'report_sha256':hashlib.sha256(evidence_path.read_bytes()).hexdigest()})
        assert tool('verify_node',target(a))['receipt_id']
        bad=rpc(client,'state_graph_write',{'action':'report_external_attempt','arguments':{'attempt_id':a['attempt_id'],'status':'VERIFIED'}}).json()['result']
        assert bad['isError'] is True
        read=rpc(client,'state_graph_read',{'action':'project_attempts','arguments':{'project_id':'game'}}).json()['result']
        assert json.loads(read['content'][0]['text'])['result']['attempts'][0]['run_id'] is None


def test_external_lifecycle_never_calls_unavailable_runtime_factory(tmp_path):
    calls=[]
    def runtime():calls.append(1);raise AssertionError('Workflow engine must not be touched')
    service=StateService(StateDatabase(str(tmp_path/'bare.sqlite')),runtime_factory=runtime,actor='director',
                         project_read_trusted=True)
    service.create_project('game','Game');service.store.add_nodes('game',[spec('a')])
    a=service.start_external_attempt('game','a',1,'subagents','worker-1','request')
    report_path=tmp_path/'report.txt'
    report_body=json.dumps({'status':'candidate','settled':True,'usable':True}).encode()
    report_path.write_bytes(report_body)
    a=service.report_external_attempt(a['attempt_id'],'end',0,a['context_hash'],'candidate',
        str(report_path),hashlib.sha256(report_body).hexdigest(),True,ARTIFACT,'sha256')
    for check in ['test','review']:
        evidence_path=tmp_path/('evidence-'+check+'.json')
        evidence_path.write_text(json.dumps({'status':'completed','settled':True,
                                              'usable':True,'verdict':'pass',
                                              'criterion_id':check}))
        service.record_evidence(a['attempt_id'],check,check,'pass',ARTIFACT,str(evidence_path),
                                hashlib.sha256(evidence_path.read_bytes()).hexdigest())
    service.verify_node('game','a',1,a['attempt_id'])
    service.recover_attempt(a['attempt_id']);service.reconcile_attempt(a['attempt_id']);service.refresh_project('game')
    assert calls==[]


def test_pure_state_boot_and_mcp_work_under_workflow_import_firewall(tmp_path):
    source=Path(__file__).resolve().parents[2]
    script=r'''
import importlib.abc,sys,json,hashlib
class Forbid(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.startswith('skillflow') or fullname in {'api.dependencies','core.db_manager','core.config_registry','core.run_driver','core.workspace_manager'}:
            raise AssertionError('Forbidden workflow runtime import: '+fullname)
sys.meta_path.insert(0,Forbid())
from api.state_only import create_app
from fastapi.testclient import TestClient
app=create_app(sys.argv[1],'x'*40)
headers={'Authorization':'Bearer '+'x'*40}
with TestClient(app) as c:
    def cmd(action,body):
        r=c.post('/api/state/commands/'+action,json=body,headers=headers);assert r.status_code==200,r.text;return r.json()
    cmd('create_project',{'project_id':'p','title':'P'})
    cmd('add_nodes',{'project_id':'p','nodes':[{'key':'goal','goal':'Goal','acceptance':[{'id':'c','kind':'test','description':'Checker'}]}]})
    a=cmd('start_external_attempt',{'project_id':'p','node_key':'goal','expected_revision':1,'harness':'own-harness','external_id':'job','request_key':'once'})
    report=sys.argv[1]+'.report';report_body=json.dumps({'status':'candidate','settled':True,'usable':True}).encode();open(report,'wb').write(report_body)
    cmd('report_external_attempt',{'attempt_id':a['attempt_id'],'observation_id':'end','expected_version':0,'context_hash':a['context_hash'],'status':'candidate','artifact':'a'*64,'artifact_kind':'sha256','report_ref':report,'report_sha256':hashlib.sha256(report_body).hexdigest(),'quiescent':True})
    evidence=sys.argv[1]+'.evidence.json';evidence_body=json.dumps({'status':'completed','settled':True,'usable':True,'verdict':'pass','criterion_id':'c'}).encode();open(evidence,'wb').write(evidence_body)
    cmd('record_evidence',{'attempt_id':a['attempt_id'],'evidence_id':'e','criterion_id':'c','verdict':'pass','artifact':'a'*64,'report_ref':evidence,'report_sha256':hashlib.sha256(evidence_body).hexdigest()})
    cmd('verify_node',{'project_id':'p','node_key':'goal','expected_revision':1,'attempt_id':a['attempt_id']})
    h=c.post('/mcp/',json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'state_graph_read','arguments':{'action':'get_node','arguments':{'project_id':'p','node_key':'goal'}}}},headers={**headers,'Accept':'application/json, text/event-stream'})
    assert h.status_code==200,h.text
    assert 'VERIFIED' in h.text
assert not any(n.startswith('skillflow') for n in sys.modules)
print('PASS: state-only HTTP+MCP acceptance without workflow imports')
'''
    result=subprocess.run([sys.executable,'-B','-c',script,str(tmp_path/'no-workflows.sqlite')],cwd=source,text=True,capture_output=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS:' in result.stdout


def test_extra_fields_cannot_spoof_authenticated_reporter(app):
    with TestClient(app) as c:
        project(c)
        body={'project_id':'game','node_key':'a','expected_revision':1,'harness':'ci','external_id':'job','request_key':'r',
              'reporting_actor':'trusted-other-user'}
        assert c.post('/api/state/commands/start_external_attempt',json=body,headers=HEADER).status_code==422
        assert c.get('/api/state/projects/game/attempts',headers=HEADER).json()['attempts']==[]


def test_standalone_rejects_legacy_and_workflow_operations_without_side_effects(app):
    with TestClient(app) as c:
        project(c)
        for action,body in [('start_attempt',{'project_id':'game','node_key':'a','expected_revision':1,'workflow':'missing','request_key':'q'}),
                            ('import_tasks',{'project_id':'game','source_project_id':'legacy'})]:
            result=c.post('/api/state/commands/'+action,json=body,headers=HEADER)
            assert result.status_code in [409,422],result.text
        assert c.get('/api/state/projects/game/attempts',headers=HEADER).json()['attempts']==[]


@pytest.mark.parametrize('token',['','short','x'*31,'x'*40+'\n'])
def test_state_only_does_not_boot_unprotected(tmp_path,token):
    with pytest.raises(ValueError,match='token'):create_app(str(tmp_path/'db.sqlite'),token)
    assert not (tmp_path/'db.sqlite').exists()


def test_same_http_factory_in_embedded_mode_shares_commands_and_actor(tmp_path):
    service=StateService(StateDatabase(str(tmp_path/'db.sqlite')),actor='director@local',
                         project_read_trusted=True)
    app=FastAPI();app.state.state_service=service;app.include_router(create_state_router(lambda:service,lambda:None))
    with TestClient(app) as client:
        project(client);a=register(client)
        assert a['reporting_actor']=='director@local'
        a=complete(client,a);evidence(client,a,'test');evidence(client,a,'review')
        assert command(client,'verify_node',target(a))['reviewer']=='director@local'
        assert execute(service,'attempt_detail',{'attempt_id':a['attempt_id']})['attempt']['execution_kind']=='external'


def test_skillflow_and_external_attempts_can_verify_dependencies_in_one_project(tmp_path):
    from skillflow.core import SkillFlow,StepResult
    from skillflow.graph import PipelineGraph,StepNode
    from core.db_manager import DBManager
    from core.state_graph import StateGraphStore
    from core.state_attempts import StateAttempts
    from core.state_external import ExternalAttempts
    sf=SkillFlow(str(tmp_path/'sf.sqlite'));sf.register_graph(PipelineGraph(name='workflow',begin='work',steps=[StepNode(id='work')]))
    store=StateGraphStore(DBManager(str(tmp_path/'host.sqlite')));attempts=StateAttempts(store);external=ExternalAttempts(attempts,'external-director')
    store.create_project('p','Mixed');store.add_nodes('p',[spec('external-base'),spec('workflow-child',['external-base']),spec('external-final',['workflow-child'])])
    kinds=[]
    for i,nk in enumerate(['external-base','workflow-child','external-final']):
        if i==1:
            a=attempts.reserve('p',nk,1,'workflow','request')
            rid=sf.create_run('workflow',project_id=a['execution_project_id']);sf.start_run(rid)
            attempts.bind_run(a['attempt_id'],rid,sf);sf.advance_run(rid);claim=sf.claim_next_step(rid)
            sf.confirm_step(claim.token,StepResult(outputs={'actual':'fixture'}));sf.advance_run(rid)
            a=attempts.reconcile(a['attempt_id'],sf,ARTIFACT)
        else:
            a=external.register('p',nk,1,'own-harness','job-'+str(i),'request')
            report_path=tmp_path/(a['attempt_id']+'-done.txt')
            report_body=json.dumps({'attempt':a['attempt_id'],'observation':'done',
                                    'status':'candidate','settled':True,
                                    'usable':True},sort_keys=True).encode()
            report_path.write_bytes(report_body)
            a=external.observe(a['attempt_id'],'done',0,a['context_hash'],'candidate',str(report_path),hashlib.sha256(report_body).hexdigest(),
                               quiescent=True,artifact=ARTIFACT,artifact_kind='sha256')
        for criterion in ['test','review']:
            attempts.record_evidence(a['attempt_id'],nk+'-'+criterion,criterion,'pass',ARTIFACT,'report-'+criterion,EVIDENCE_REPORT,'shared-verifier')
        receipt=attempts.verify('p',nk,1,a['attempt_id'],'accepting-director')
        kinds.append(json.loads(receipt['provenance_json'])['execution_kind'])
    assert kinds==['external','skillflow','external']
    assert len(sf.list_runs())==1
    assert all(n['status']=='VERIFIED' for n in store.get_graph('p')['nodes'])
    store.revise_node('p','external-base',1,'new root requirement')
    assert all(n['status']=='STALE' for n in store.get_graph('p')['nodes'])
    sf._conn.close()


def test_executable_own_harness_demo_runs_actual_failed_then_passing_checks(tmp_path):
    source=Path(__file__).resolve().parents[2];report=tmp_path/'demo.json'
    result=subprocess.run([sys.executable,'-B','examples/external_harness_demo.py','--report',str(report)],cwd=source,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    data=json.loads(report.read_text());assert data['result']=='PASS'
    assert [a['acceptance_http_status'] for a in data['attempts']]==[409,200]
    assert data['attempts'][0]['check_verdicts'][0]['verdict']=='fail'
    assert all(c['verdict']=='pass' for c in data['attempts'][1]['check_verdicts'])
    assert data['workflow_runtime_imported'] is False and data['workflow_runs_created']==0


def test_http_rejects_stale_candidate_and_self_evidence_before_acceptance(app):
    with TestClient(app) as client:
        project(client)
        first = complete(client, register(client))
        stale_report = candidate_report(first)

        second = register(
            client, request="second", eid="director/session-2/subagent-8"
        )
        stale_body = {
            "attempt_id": second["attempt_id"],
            "observation_id": "copied-old-final",
            "expected_version": 0,
            "context_hash": second["context_hash"],
            "status": "candidate",
            "artifact": ARTIFACT,
            "artifact_kind": "sha256",
            "report_ref": first["observation"]["report_ref"],
            "report_sha256": stale_report,
            "quiescent": True,
        }
        rejected = client.post(
            "/api/state/commands/report_external_attempt",
            json=stale_body,
            headers=HEADER,
        )
        assert rejected.status_code == 409
        still_running = client.get(
            "/api/state/attempts/" + second["attempt_id"], headers=HEADER
        ).json()
        assert still_running["status"] == "running"
        assert still_running["observation_version"] == 0

        second = complete(client, second)
        self_evidence = {
            "attempt_id": second["attempt_id"],
            "evidence_id": "candidate-self-report",
            "criterion_id": "review",
            "verdict": "pass",
            "artifact": ARTIFACT,
            "report_ref": "reports/actual-final.json",
            "report_sha256": candidate_report(second),
        }
        rejected = client.post(
            "/api/state/commands/record_evidence",
            json=self_evidence,
            headers=HEADER,
        )
        assert rejected.status_code == 409

        wrong_artifact = dict(self_evidence)
        wrong_artifact.update(
            evidence_id="wrong-artifact",
            report_sha256=EVIDENCE_REPORT,
            artifact="d" * 64,
        )
        rejected = client.post(
            "/api/state/commands/record_evidence",
            json=wrong_artifact,
            headers=HEADER,
        )
        assert rejected.status_code == 409

        evidence(client, second, "test", verdict="skip")
        evidence(client, second, "review")
        rejected = client.post(
            "/api/state/commands/verify_node",
            json=target(second),
            headers=HEADER,
        )
        assert rejected.status_code == 409

        evidence(client, second, "test", suffix="-current")
        receipt = command(client, "verify_node", target(second))
        assert receipt["artifact_ref"] == ARTIFACT
