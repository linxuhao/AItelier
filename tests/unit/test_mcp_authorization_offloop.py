"""Real registered MCP dispatch keeps SQLite auth waits off its event loop."""
import asyncio
from contextlib import contextmanager
import contextvars
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx

from api import authz, mcp_router
from core.drivers import DriverRegistry
from core.state_database import StateDatabase


@pytest.fixture
def owned_auth(tmp_path, monkeypatch):
    sql_started = threading.Event()
    observations = []
    class ObservedDatabase(StateDatabase):
        @contextmanager
        def get_connection(self):
            with super().get_connection() as conn:
                def trace(sql):
                    if sql.startswith('UPDATE drivers SET last_seen_at='):
                        observations.append({'event':'real_touch_update','thread':threading.get_ident(),'at':time.monotonic()})
                        sql_started.set()
                conn.set_trace_callback(trace)
                yield conn
    db = ObservedDatabase(str(tmp_path/'drivers.sqlite'))
    registry = DriverRegistry(db, 'owned-pepper-'*4)
    token = registry.register('owned-driver','Owned driver',actor='owned-fixture')['token']
    registry._last_seen.clear();sql_started.clear();observations.clear()
    monkeypatch.setattr(authz, 'gate_enabled', lambda: True)
    monkeypatch.setattr(authz, 'driver_registry', lambda: registry)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda headers,cookies=None:None)
    monkeypatch.setattr(mcp_router, '_EXTERNAL_TOKEN', '')
    request=Request({'type':'http','method':'POST','path':'/mcp/','headers':[(b'x-aitelier-driver-token',token.encode())]})
    return SimpleNamespace(db=db,registry=registry,token=token,request=request,sql_started=sql_started,observations=observations,tmp=tmp_path)


def bind_request(request):
    # FastMCP.get_context reads this real SDK request ContextVar. No auth/body shim.
    return request_ctx.set(SimpleNamespace(request=request,request_id=1,meta=None,session=None,lifespan_context={}))


def register(server,name,fn,kind='write'):
    mcp_router._TOOL_KIND[name]=kind
    server.tool(name=name)(mcp_router._wrap(server,fn,name))
    return server._tool_manager.get_tool(name)


async def locked_touch(owned, invoke, label):
    ready=threading.Event();released=threading.Event();force_release=threading.Event();errors=[]
    def writer():
        try:
            with owned.db.get_connection() as conn:
                conn.execute('BEGIN IMMEDIATE');ready.set()
                assert owned.sql_started.wait(2),'real authorization UPDATE did not start'
                force_release.wait(.3)
                released.set();owned.observations.append({'event':'writer_releasing','thread':threading.get_ident(),'at':time.monotonic()})
                conn.commit()
        except BaseException as e:errors.append(repr(e));ready.set();released.set()
    holder=threading.Thread(target=writer,name='owned-sqlite-writer');holder.start()
    assert ready.wait(2)
    sibling={}
    async def witness():
        await asyncio.sleep(.04)
        sibling.update(progressed_before_release=not released.is_set(),real_update_started=owned.sql_started.is_set())
        owned.observations.append({'event':'async_sibling','thread':threading.get_ident(),'at':time.monotonic(),**sibling})
    tick=asyncio.create_task(witness())
    await asyncio.sleep(0)
    token=bind_request(owned.request)
    try:
        result=await invoke()
        await tick
    finally:
        request_ctx.reset(token);force_release.set();holder.join(3)
    assert not holder.is_alive() and not errors,errors
    with owned.db.get_connection() as conn:
        row=conn.execute('SELECT last_seen_at,status FROM drivers WHERE driver_id=?',('owned-driver',)).fetchone()
        assert row['last_seen_at'] and row['status']=='active'
    raw={'label':label,'result':result,'observations':owned.observations,'sibling':sibling,'loop_thread':threading.get_ident(),'touch_durable':True,'mcp_version':__import__('importlib.metadata',fromlist=['version']).version('mcp')}
    if os.environ.get('AUTH_RAW'):
        Path(os.environ['AUTH_RAW'],label+'.json').write_text(json.dumps(raw,indent=2,default=str)+'\n')
    assert owned.sql_started.is_set(),'no real auth UPDATE; not a causal control'
    assert sibling=={'progressed_before_release':True,'real_update_started':True},raw
    assert all(x['thread']!=threading.get_ident() for x in owned.observations if x['event']=='real_touch_update'),raw
    return result


@pytest.mark.asyncio
async def test_registered_tool_sqlite_authorization_does_not_block_async_sibling(owned_auth):
    server=FastMCP('owned-auth');body=[]
    def probe():
        actor=authz.request_actor(server.get_context().request_context.request)
        body.append({'actor':actor,'thread':threading.get_ident()})
        return {'actor':actor,'effect':len(body)}
    tool=register(server,'owned_auth_probe',probe)
    try:
        result=await locked_touch(owned_auth,lambda:tool.run({},context=server.get_context()),'registered-tool')
        assert result=={'actor':'driver:owned-driver','effect':1} and len(body)==1
        assert body[0]['thread']!=threading.get_ident()
    finally:mcp_router._TOOL_KIND.pop('owned_auth_probe',None)


@pytest.mark.asyncio
async def test_director_prevalidation_sqlite_auth_does_not_block_or_execute_body(owned_auth):
    server=FastMCP('owned-director');effects=[]
    def state_graph_read(action:str,arguments:dict):effects.append(arguments);return {'unexpected':True}
    register(server,'state_graph_read',state_graph_read,'read')
    mcp_router._install_director_call_guard(server)
    async def invoke():
        return await server._tool_manager.call_tool('state_graph_read',{'action':'list_director_messages','arguments':'not-an-object'},context=server.get_context())
    result=await locked_touch(owned_auth,invoke,'director-prevalidation')
    assert not effects
    assert result['result']['code']=='invalid_request',result


@pytest.mark.asyncio
@pytest.mark.parametrize('director',[False,True])
async def test_invalid_identity_refuses_before_registered_body_or_nested_validation(owned_auth,director):
    server=FastMCP('owned-denial');effects=[]
    def state_graph_read(action:str,arguments:dict):effects.append(arguments);return {'unexpected':True}
    def probe():effects.append('effect');return {'unexpected':True}
    if director:
        register(server,'state_graph_read',state_graph_read,'read');mcp_router._install_director_call_guard(server)
    else:tool=register(server,'owned_denied_probe',probe)
    bad=Request({'type':'http','method':'POST','path':'/mcp/','headers':[(b'x-aitelier-driver-token',b'owned-invalid-token')]})
    token=bind_request(bad)
    try:
        if director:
            result=await server._tool_manager.call_tool('state_graph_read',{'action':'list_director_messages','arguments':'invalid'},context=server.get_context())
            assert result['result']['code']=='unauthorized',result
        else:
            result=await tool.run({},context=server.get_context());assert result['error'].startswith('denied:'),result
        assert effects==[] and not owned_auth.sql_started.is_set()
    finally:
        request_ctx.reset(token);mcp_router._TOOL_KIND.pop('owned_denied_probe',None)


@pytest.mark.asyncio
async def test_parallel_request_context_identity_and_async_body_are_preserved(owned_auth):
    server=FastMCP('owned-context');other=owned_auth.registry.register('other-driver','Other',actor='owned-fixture')['token'];seen=[]
    async def probe():
        await asyncio.sleep(0)
        actor=authz.request_actor(server.get_context().request_context.request)
        seen.append((actor,threading.get_ident()));return {'actor':actor}
    tool=register(server,'owned_async_probe',probe)
    async def call(token):
        req=Request({'type':'http','method':'POST','path':'/mcp/','headers':[(b'x-aitelier-driver-token',token.encode())]})
        marker=bind_request(req)
        try:return await tool.run({},context=server.get_context())
        finally:request_ctx.reset(marker)
    try:
        a,b=await asyncio.gather(call(owned_auth.token),call(other))
        assert a=={'actor':'driver:owned-driver'} and b=={'actor':'driver:other-driver'}
        assert all(t==threading.get_ident() for _,t in seen)
    finally:mcp_router._TOOL_KIND.pop('owned_async_probe',None)
