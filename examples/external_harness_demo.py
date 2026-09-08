#!/usr/bin/env python3
"""Own-harness validation against a State-only API; zero workflow/model calls.

    python examples/external_harness_demo.py --report /tmp/external-demo.json

Uses temporary SQLite and the real authenticated HTTP app. Two independent
Python subprocess checks stand in for a custom harness's test/review workers.
The first implementation fails, the corrected attempt passes; the server never
executes submitted code and never creates a SkillFlow run. This demo harness
itself owns and waits for the subprocesses before reporting quiescence.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from api.state_only import create_app


def run_demo():
    with tempfile.TemporaryDirectory(prefix='aitelier-external-demo-') as directory:
        root=Path(directory);token=secrets.token_urlsafe(40)
        db=root/'state.sqlite';app=create_app(str(db),token)
        headers={'Authorization':'Bearer '+token}
        observations=[]
        with TestClient(app) as client:
            def command(action,body):
                res=client.post('/api/state/commands/'+action,json=body,headers=headers)
                if res.status_code!=200:raise AssertionError(action+': '+res.text)
                return res.json()
            command('create_project',{'project_id':'demo','title':'Own-harness project'})
            command('add_nodes',{'project_id':'demo','nodes':[
                {'key':'progress','goal':'Correct progress arithmetic','acceptance':[
                    {'id':'behaviour','kind':'test','description':'Actual arithmetic tests'},
                    {'id':'compile','kind':'test','description':'Independent Python syntax check'}]},
                {'key':'dependent','goal':'A feature using progress','dependencies':['progress'],
                 'acceptance':[{'id':'integration','kind':'integration','description':'Dependent feature must also be checked'}]}
            ]})
            for attempt_number,source in enumerate(['def add(a,b): return a+b+1\n','def add(a,b): return a+b\n'],1):
                identity={'project_id':'demo','node_key':'progress','expected_revision':1,
                          'harness':'two-python-checkers','external_id':f'job/{attempt_number}',
                          'request_key':f'attempt-{attempt_number}'}
                a=command('start_external_attempt',identity)
                assert command('start_external_attempt',identity)['attempt_id']==a['attempt_id']
                assert a['run_id'] is a['workflow'] is a['execution_project_id'] is None
                job=root/f'job-{attempt_number}';job.mkdir();(job/'feature.py').write_text(source)
                artifact=hashlib.sha256(source.encode()).hexdigest()
                # Real harness-owned workers, not reports fabricated by the API.
                checks=[('behaviour','from feature import add; assert add(4,3)==7; assert add(-2,2)==0'),
                        ('compile',"from pathlib import Path; compile(Path('feature.py').read_text(),'feature.py','exec')")]
                outputs=[]
                for check,code in checks:
                    proc=subprocess.run([sys.executable,'-B','-c',code],cwd=job,capture_output=True,text=True,timeout=10)
                    body={'criterion':check,'artifact':artifact,'returncode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr,
                          'context_hash':a['context_hash'],'external_id':a['external_id']}
                    raw=json.dumps(body,sort_keys=True).encode();path=job/(check+'.json');path.write_bytes(raw)
                    outputs.append({'criterion':check,'verdict':'pass' if proc.returncode==0 else 'fail',
                                    'report_ref':str(path),'report_sha256':hashlib.sha256(raw).hexdigest()})
                final=json.dumps({'checks':outputs,'all_workers_waited':True,'artifact':artifact,'context_hash':a['context_hash']},sort_keys=True).encode()
                final_path=job/'final.json';final_path.write_bytes(final)
                completed={'attempt_id':a['attempt_id'],'observation_id':'finished','expected_version':0,
                           'context_hash':a['context_hash'],'status':'candidate','artifact':artifact,'artifact_kind':'sha256',
                           'quiescent':True,'report_ref':str(final_path),'report_sha256':hashlib.sha256(final).hexdigest()}
                a=command('report_external_attempt',completed)
                for item in outputs:
                    command('record_evidence',{'attempt_id':a['attempt_id'],'evidence_id':a['attempt_id']+'-'+item['criterion'],
                        'criterion_id':item['criterion'],'verdict':item['verdict'],'artifact':artifact,
                        'report_ref':item['report_ref'],'report_sha256':item['report_sha256']})
                target={'project_id':'demo','node_key':'progress','expected_revision':1,'attempt_id':a['attempt_id']}
                res=client.post('/api/state/commands/verify_node',json=target,headers=headers)
                if attempt_number==1:
                    assert res.status_code==409 and 'behaviour' in res.text,res.text
                    assert client.get('/api/state/projects/demo/nodes/dependent',headers=headers).json()['node']['readiness']=='blocked'
                else:
                    assert res.status_code==200,res.text
                    assert client.get('/api/state/projects/demo/nodes/dependent',headers=headers).json()['node']['readiness']=='ready'
                    assert json.loads(res.json()['provenance_json'])['external_id']=='job/2'
                observations.append({'attempt':attempt_number,'status':a['status'],'check_verdicts':outputs,
                                     'acceptance_http_status':res.status_code,'artifact':artifact})
            command('revise_node',{'project_id':'demo','node_key':'progress','expected_revision':1,'reason':'Owner revises the goal'})
            assert client.post('/api/state/commands/verify_node',json=target,headers=headers).status_code==409
        # Fresh app/process context recovers from only the State DB.
        reopened=create_app(str(db),token)
        with TestClient(reopened) as client:
            assert client.get('/api/state/projects/demo/nodes/progress',headers=headers).json()['node']['status']=='STALE'
            assert client.get('/api/state/attempts/'+a['attempt_id'],headers=headers).json()['external_id']=='job/2'
        with app.state.state_service.db.get_connection() as c:
            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            assert 'runs' not in tables and 'tasks' not in tables and not any(x.startswith('skillflow') for x in tables)
        assert not any(x.startswith('skillflow') for x in sys.modules),'Demo unexpectedly imported the workflow runtime'
        return {'result':'PASS','mode':'State-only HTTP/MCP app + own actual Python subprocess harness',
                'attempts':observations,'workflow_runtime_imported':False,'workflow_runs_created':0,
                'legacy_execution_tables_created':False,'restart_recovered':True,'old_revision_rejected':True,
                'production_data_used':False,'note':'Test/report files were temporary demo artifacts, not retained production evidence or a real LLM subagent benchmark.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',type=Path);args=parser.parse_args()
    result=run_demo();body=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.report:args.report.write_text(body)
    print(body,end='')
    return 0


if __name__=='__main__':raise SystemExit(main())
