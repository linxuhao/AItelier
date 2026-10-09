"""Actual migrated caller code dispatches synthetic credentials to the registry."""
import importlib.util
import io
import json
from pathlib import Path
from contextlib import contextmanager
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_service import StateService

ROOT=Path(__file__).resolve().parents[2]
OWNER='synthetic-unchanged-owner-token-for-caller-tests'

def load(name,relative):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

@pytest.fixture
def world(tmp_path,monkeypatch):
    from api import authz,driver_routers,state_graph_routers as sr
    db=StateDatabase(str(tmp_path/'state.sqlite'))
    reg=DriverRegistry(db,'synthetic-caller-pepper-'*3);reg.seed(OWNER)
    token=reg.register('caller','Caller',actor='driver:owner-cli',is_admin=True)['token']
    StateService(db,actor='driver:owner-cli',project_read_trusted=True).create_project('fixture','Fixture')
    monkeypatch.setattr(authz,'driver_registry',lambda:reg)
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *a:None)
    monkeypatch.setenv('AITELIER_ADMIN_TOKEN',OWNER)
    monkeypatch.setenv('AITELIER_DRIVER_TOKEN',token)
    app=FastAPI();app.include_router(driver_routers.router);app.include_router(sr.router)
    app.dependency_overrides[sr.get_db_manager]=lambda:db
    app.dependency_overrides[sr.get_workspace_manager]=lambda:None
    with TestClient(app,base_url='http://localhost') as client:
        yield client,reg,token


def test_real_cli_client_unchanged_owner_and_driver_registry(world,monkeypatch):
    import httpx
    from cli.client import APIClient
    client,reg,token=world
    def factory(**kwargs):
        client.headers.update(kwargs['headers'])
        return client
    monkeypatch.setattr(httpx,'Client',factory)
    real=APIClient('http://localhost')
    assert real._client.get('/api/drivers/me').json()['driver_id']=='caller'
    monkeypatch.delenv('AITELIER_DRIVER_TOKEN')
    client.headers.clear()
    real=APIClient('http://localhost')
    assert real._client.get('/api/drivers/me').json()['driver_id']=='owner-cli'
    assert reg.lookup_token(OWNER)['is_admin']


def test_real_state_facets_request_reaches_table_and_state_query(world,monkeypatch):
    module=load('actual_facets_caller','scripts/state_facets_migrate.py')
    client,reg,token=world
    class Reply(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*a):self.close()
    def transport(request,timeout):
        response=client.post(request.full_url,headers=dict(request.header_items()),content=request.data)
        assert response.status_code==200,response.text
        assert reg.lookup_token(token)['driver_id']=='caller'
        return Reply(response.content)
    monkeypatch.setattr(module.urllib.request,'urlopen',transport)
    assert module.admin_token()==''
    out=module.Api('http://localhost','').query('project_overview',project_id='fixture')
    assert 'fixture' in json.dumps(out)


def test_real_mcp_script_and_postcompact_over_actual_mcp_transport(world,monkeypatch,capsys):
    from api.mcp_router import build_mcp
    client,reg,token=world
    server=build_mcp()
    script=load('actual_mcp_caller','scripts/mcp_call.py')
    hook=load('actual_postcompact_caller','.codex/hooks/postcompact_driver_state.py')
    monkeypatch.setattr(hook,'_codex_config',lambda:{})
    with TestClient(server.streamable_http_app(),base_url='http://localhost') as mcp:
        def post(url,headers,json,timeout):return mcp.post('/',headers=headers,json=json)
        monkeypatch.setattr(script.httpx,'post',post)
        assert script.main(['driver_whoami','{}'])==0
        assert json.loads(capsys.readouterr().out)['result']['content']
        class Reply(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        def transport(request,timeout):
            result=mcp.post('/',headers=dict(request.header_items()),content=request.data)
            assert result.status_code==200
            return Reply(result.content)
        monkeypatch.setattr(hook.urllib.request,'urlopen',transport)
        url,headers=hook._connection()
        result=hook._mcp_call(url,headers,'driver_whoami',{})
        assert result['driver_id']=='caller' and result['actor']=='driver:caller'
        reg.set_status('caller','suspended',1,'synthetic-test',actor='driver:owner-cli')
        assert hook._mcp_call(url,headers,'driver_whoami',{})['driver_id'] is None
        assert script.main(['state_graph_write','{"action":"set_node_priority","arguments":{}}'])==1


def test_state_only_bearer_wrapper_resolves_registered_driver(world):
    from api.state_only import _BearerAuth,_CALLER
    from starlette.responses import JSONResponse
    client,reg,token=world
    async def endpoint(scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                event=await receive()
                if event['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif event['type']=='lifespan.shutdown':
                    await send({'type':'lifespan.shutdown.complete'});return
        actor,driver_id=_CALLER.get()
        await JSONResponse({'actor':actor,'driver_id':driver_id})(scope,receive,send)
    wrapper=_BearerAuth(endpoint,'synthetic-dedicated-bearer-token-not-owner',registry=reg)
    with TestClient(wrapper,base_url='http://localhost') as actual:
        assert actual.get('/probe',headers={'X-AItelier-Driver-Token':token}).json()=={'actor':'driver:caller','driver_id':'caller'}
        assert actual.get('/probe',headers={'X-AItelier-Driver-Token':'synthetic-unknown'}).status_code==401

def test_actual_node_evaluated_dsh_patch_headers_resolve_table_and_refuse_suspension(tmp_path,monkeypatch):
    from api import authz
    from api.mcp_router import build_mcp
    from core import drivers
    headers=json.loads(Path('/reports/dsh-synthetic-evaluated-headers.json').read_text())
    reg=DriverRegistry(StateDatabase(str(tmp_path/'dsh.sqlite')),'synthetic-dsh-pepper-'*3)
    reg.seed('synthetic-dsh-owner-token')
    monkeypatch.setattr(drivers,'new_token',lambda:'synthetic-dsh-driver-token')
    reg.register('dsh-caller','DSH caller',actor='driver:owner-cli')
    monkeypatch.setattr(authz,'driver_registry',lambda:reg)
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *a:None)
    with TestClient(build_mcp().streamable_http_app(),base_url='http://localhost') as mcp:
        def call(row,name='driver_whoami',arguments=None):
            response=mcp.post('/',headers={**row['headers'],'Accept':'application/json, text/event-stream'},
                              json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments or {}}})
            assert response.status_code==200
            return response.json()['result']
        for row in headers:
            result=json.loads(call(row)['content'][0]['text'])
            assert result['driver_id']==('owner-cli' if row['name']=='owner' else 'dsh-caller')
        reg.set_status('dsh-caller','suspended',1,'synthetic-test',actor='driver:owner-cli')
        for row in headers[:2]:
            assert json.loads(call(row)['content'][0]['text'])['driver_id'] is None
            assert call(row,'state_graph_write',{'action':'set_node_priority','arguments':{}})['isError']
