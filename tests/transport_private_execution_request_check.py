"""Owned request cohort; run directly in an isolated, network-disabled container."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from api import authz, dependencies, run_routers, meta_routers, project_routers, routers, repo_routers, mcp_router
from core import cf_access

assert Path(run_routers.__file__).resolve().parent.parent == Path(__file__).resolve().parent.parent
# Only signature/issuer validation is replaced by controlled verified claims.
# The application writer/admin/tunnel authorization functions run unchanged.
cf_access.verify = lambda token: ({'email': 'writer@fixture'} if token == 'verified-writer' else
                                  {'email': 'reader@fixture'} if token == 'verified-nonwriter' else None)
assert authz.gate_enabled()
assert not getattr(FastAPI().state, '_test_mode', False)
marker = 'OWNED_PRIVATE_BODY_SENTINEL'
conn = sqlite3.connect(':memory:', check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute('CREATE TABLE skillflow_trace(run_id TEXT,seq INTEGER,step_id TEXT,category TEXT,event TEXT,payload_json TEXT,created_at TEXT)')
conn.execute('INSERT INTO skillflow_trace VALUES(?,?,?,?,?,?,?)', ('owned',1,'impl','prompt','prompt',json.dumps({'text':marker}),'2026-10-07T00:00:00Z'))
run = {'id':'owned','project_id':'private-execution','status':'completed','graph_name':'fixture','context_json':marker,'graph_path':'/private/source/fixture.yaml'}
class SF:
    def get_run(self, ref): return dict(run) if ref == 'owned' else None
    def list_runs(self, project_id=None): return [dict(run)] if project_id == 'private-execution' else []
    def get_steps(self, ref): return [{'id':1,'step_id':'impl','status':'completed'}]
    def get_trace(self, ref, step_instance_id=None, category=None, limit=100, order='asc', after_seq=None, before_seq=None):
        return [{'seq':1,'payload':{'text':marker},'category':'prompt'}]
    def trace_query(self, ref, sql, args): return conn.execute(sql,args).fetchall()
sf=SF()
class DB:
    def get_project(self, ref): return None
registry=SimpleNamespace(get=lambda name: None, list=lambda: [])
app=FastAPI()
for router in [run_routers.router,meta_routers.router,project_routers.router,routers.router,repo_routers.router]: app.include_router(router)
app.dependency_overrides[dependencies.get_db_manager]=lambda: DB()
app.dependency_overrides[dependencies.get_skillflow]=lambda: sf
app.dependency_overrides[dependencies.get_config_registry]=lambda: registry
app.dependency_overrides[dependencies.get_workspace_manager]=lambda: SimpleNamespace()
dependencies.get_skillflow=lambda: sf
run_routers.get_skillflow=lambda: sf
run_routers.compute_cache_stats_per_step=lambda ref: {}
client=TestClient(app)
private_paths=['/api/runs/owned','/api/runs/owned/trace','/api/runs/owned/checkpoint',
 '/api/meta/private-execution/checkpoint','/api/tasks/1/steps/impl/output',
 '/api/projects/private-execution/workspace/tree','/api/projects/private-execution/workspace/file?path=impl.txt',
 '/api/projects/private-execution/workspace/raw?path=impl.png','/api/repos/private/source']
controls=[{}, {'X-AItelier-Admin-Token':'invalid'}, {'Cf-Access-Jwt-Assertion':'invalid'},
 {'Cf-Access-Jwt-Assertion':'verified-nonwriter'}, {'Cf-Ray':'fixture','X-AItelier-Admin-Token':'fixture-admin'}]
checks=0
for headers in controls:
    for path in private_paths + ['/api/runs/missing/trace','/api/runs/private-execution/trace']:
        response=client.get(path,headers=headers)
        assert response.status_code==403,(path,response.status_code,sorted(headers),authz.write_denial_reason(Request({'type':'http','method':'GET','path':'/','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()],'app':app})),str(run_routers.__file__),getattr(app.state,'_test_mode',False))
        assert marker not in response.text and '/private/source' not in response.text
        checks+=1
for headers in [{'X-AItelier-Admin-Token':'fixture-admin'}, {'Cf-Access-Jwt-Assertion':'verified-writer'}]:
    for path in ['/api/runs/owned/trace','/api/runs/owned']:
        response=client.get(path,headers=headers)
        assert response.status_code==200,(path,response.status_code)
        assert marker in response.text
        checks+=1
    assert client.get('/api/runs/missing/trace',headers=headers).status_code==404
    checks+=1
# Anonymous progress projections cannot mutate the full cached record.
request=Request({'type':'http','method':'GET','path':'/','headers':[],'app':app})
row={'project_id':'private-execution','status':'completed','repo_path':'/private/source','brief':marker,'context_json':marker}
projection=authz.execution_progress(request,row)
assert projection=={'project_id':'private-execution','status':'completed'} and row['brief']==marker
checks+=1
mcp=mcp_router.build_mcp()
context=SimpleNamespace(request_context=SimpleNamespace(request=request))
mcp.get_context=lambda:context
async def call(name,args): return await mcp.call_tool(name,args)
async def cohort():
    global checks,context
    for headers in controls:
        req=Request({'type':'http','method':'POST','path':'/mcp','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()],'app':app})
        context=SimpleNamespace(request_context=SimpleNamespace(request=req))
        for name,args in [('trace_read',{'run_id':'owned','seq':1}),('trace_list',{'run_id':'owned'}),('trace_search',{'run_id':'owned','query':marker}),('get_step_output',{'run_id':'owned','step':'impl'})]:
            out=str(await call(name,args));assert 'denied:' in out and marker not in out,(name,out);checks+=1
        summary=str(await call('get_run_summary',{'run_id':'owned'}))
        assert marker not in summary and 'final_outputs' not in summary
        assert ('denied:' in summary) if 'Cf-Ray' in headers else ('completed' in summary)
        checks+=1
        status=str(await call('get_run_status',{'run_id':'owned'}));assert marker not in status
        assert ('denied:' in status) if 'Cf-Ray' in headers else ('completed' in status)
        checks+=1
    for headers in [{'X-AItelier-Admin-Token':'fixture-admin'},{'Cf-Ray':'fixture','X-AItelier-MCP-External-Token':'fixture-external'}]:
        req=Request({'type':'http','method':'POST','path':'/mcp','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()],'app':app})
        context=SimpleNamespace(request_context=SimpleNamespace(request=req))
        out=str(await call('trace_read',{'run_id':'owned','seq':1}));assert marker in out;checks+=1
asyncio.run(cohort())
print(json.dumps({'status':'completed','settled':True,'usable':True,'request_assertions':checks,'authorization':'actual decision functions; controlled verified identity only','network':'none'}))
