"""Authenticated State-only HTTP/MCP service, with no workflow runtime.

    export AITELIER_STATE_TOKEN='<a fresh random token of at least 32 bytes>'
    python -m api.state_only --db /absolute/private/state.sqlite --port 4450

No token argument/logging, shell executor, model config, workspace or scheduler.
This module deliberately does not import api.dependencies or core.db_manager.
"""
from __future__ import annotations
import argparse
import contextvars
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets

from fastapi import FastAPI
from starlette.responses import JSONResponse

from api.state_http import create_state_router
from core.state_database import StateDatabase
from core.state_service import StateService


# Who the current request is: (actor, driver_id). The dedicated State-only token
# stays the single shared caller it always was; with driver identity enabled
# (design/multi-driver-coop.md §3) a registered LAN driver's own token, as a
# Bearer or in X-AItelier-Driver-Token, is accepted too and recorded as
# `driver:<id>`. Set by the ASGI middleware, read by the service factory.
_DEDICATED_ACTOR = 'authenticated-state-token'
_CALLER: contextvars.ContextVar = contextvars.ContextVar('state_only_caller',
                                                         default=(_DEDICATED_ACTOR, None))


class _BearerAuth:
    def __init__(self, app, token: str, registry=None):
        self.app, self.token = app, token.encode('utf-8')
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        # Health exposes no project names, credentials, schemas or row counts.
        if scope.get('path') == '/health' and scope.get('method') == 'GET':
            await self.app(scope, receive, send)
            return
        auth = [v for k,v in scope.get('headers',[]) if k.lower()==b'authorization']
        driver_header = [v for k,v in scope.get('headers',[]) if k.lower()==b'x-aitelier-driver-token']
        accepted = False
        caller = (_DEDICATED_ACTOR, None)
        presented = None
        if len(auth)==1 and auth[0][:7].lower()==b'bearer ':
            presented = auth[0][7:]
            accepted = secrets.compare_digest(presented, self.token)
        elif len(auth)==0 and len(driver_header)==1:
            presented = driver_header[0]
        if not accepted and presented and self.registry is not None:
            try:
                row = self.registry.lookup_token(presented.decode('utf-8'))
            except UnicodeDecodeError:
                row = None
            if row is not None:
                accepted = True
                caller = ('driver:' + row['driver_id'], row['driver_id'])
        if not accepted:
            await JSONResponse({'detail':'State-only bearer authentication required'},status_code=401,
                               headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
            return
        reset = _CALLER.set(caller)
        try:
            await self.app(scope,receive,send)
        finally:
            _CALLER.reset(reset)


def create_app(db_path: str, token: str, *, with_mcp: bool = True) -> FastAPI:
    if not isinstance(token,str) or len(token.encode('utf-8'))<32 or '\n' in token or '\r' in token:
        raise ValueError('Set a dedicated State-only bearer token of at least 32 bytes')
    database = StateDatabase(db_path)
    shared = StateService(database, actor=_DEDICATED_ACTOR, project_read_trusted=True)
    from core import drivers
    registry = drivers.registry_for(database)

    def service():
        actor, driver_id = _CALLER.get()
        if driver_id is None:
            return shared
        return StateService(database, actor=actor, project_read_trusted=True, driver_id=driver_id)
    mcp = None
    if with_mcp:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from api.state_graph_tools import register_state_tools
        hosts=['127.0.0.1','127.0.0.1:*','localhost','localhost:*','testserver']
        hosts += [h.strip() for h in os.environ.get('AITELIER_STATE_ALLOWED_HOSTS','').split(',') if h.strip()]
        mcp = FastMCP('aitelier-state-only', stateless_http=True, json_response=True, streamable_http_path='/',
                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                          allowed_hosts=hosts, allowed_origins=['http://127.0.0.1:*','http://localhost:*']))
        def tool(name, kind, description):
            # The outer ASGI middleware authenticates ALL MCP HTTP messages,
            # including discovery and prompts. No workflow tools are registered.
            return mcp.tool(name=name,description=description)
        register_state_tools(tool,mcp,service_factory=service)

    @asynccontextmanager
    async def lifespan(app):
        if mcp is None:
            yield
        else:
            async with mcp.session_manager.run():
                yield

    app = FastAPI(title='AItelier State DAG', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(_BearerAuth,token=token,registry=registry)
    app.state.state_service = shared
    app.state.mode = 'state-only'
    def access(request=None):
        # The outer middleware also protects discovery and OpenAPI. This is the
        # same route factory as the full host, without its runtime dependencies.
        #
        # `request` is accepted because the router's PRIVATE-READ verdict is
        # called with the Request — it is a real authorization dependency in the
        # host, and a no-argument stub would raise instead of returning a verdict.
        # This deployment decides before either verdict runs: the ASGI bearer
        # middleware refuses every request that lacks the dedicated token, reads
        # and writes alike, so nothing reaches these routes unauthenticated.
        return None
    app.include_router(create_state_router(service,access,access))
    from api.state_http import apply_project_privacy
    apply_project_privacy(app)

    @app.get('/health')
    def health():
        return {'status':'ok','mode':'state-only','workflow_runtime':False}

    if mcp is not None:
        app.mount('/mcp',mcp.streamable_http_app())
    return app


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,type=Path)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=4450)
    parser.add_argument('--without-mcp',action='store_true')
    args=parser.parse_args()
    token=os.environ.get('AITELIER_STATE_TOKEN','')
    app=create_app(str(args.db),token,with_mcp=not args.without_mcp)
    import uvicorn
    uvicorn.run(app,host=args.host,port=args.port)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
