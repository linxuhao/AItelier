"""Authenticated State-only HTTP/MCP service, with no workflow runtime.

    export AITELIER_STATE_TOKEN='<a fresh random token of at least 32 bytes>'
    python -m api.state_only --db /absolute/private/state.sqlite --port 4450

No token argument/logging, shell executor, model config, workspace or scheduler.
This module intentionally does not import api.dependencies or core.db_manager.
"""
from __future__ import annotations
import argparse
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets

from fastapi import FastAPI
from starlette.responses import JSONResponse

from api.state_http import create_state_router
from core.state_database import StateDatabase
from core.state_service import StateService


class _BearerAuth:
    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode('utf-8')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        # Health exposes no project names, credentials, schemas or row counts.
        if scope.get('path') == '/health' and scope.get('method') == 'GET':
            await self.app(scope, receive, send)
            return
        auth = [v for k,v in scope.get('headers',[]) if k.lower()==b'authorization']
        accepted = False
        if len(auth)==1 and auth[0][:7].lower()==b'bearer ':
            accepted = secrets.compare_digest(auth[0][7:], self.token)
        if not accepted:
            await JSONResponse({'detail':'State-only bearer authentication required'},status_code=401,
                               headers={'WWW-Authenticate':'Bearer'})(scope,receive,send)
            return
        await self.app(scope,receive,send)


def create_app(db_path: str, token: str, *, with_mcp: bool = True) -> FastAPI:
    if not isinstance(token,str) or len(token.encode('utf-8'))<32 or '\n' in token or '\r' in token:
        raise ValueError('Set a dedicated State-only bearer token of at least 32 bytes')
    service = StateService(StateDatabase(db_path), actor='authenticated-state-token')
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
        register_state_tools(tool,mcp,service_factory=lambda:service)

    @asynccontextmanager
    async def lifespan(app):
        if mcp is None:
            yield
        else:
            async with mcp.session_manager.run():
                yield

    app = FastAPI(title='AItelier State DAG', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(_BearerAuth,token=token)
    app.state.state_service = service
    app.state.mode = 'state-only'
    def access():
        # The outer middleware also protects discovery and OpenAPI. This is the
        # same route factory as the full host, without its runtime dependencies.
        return None
    app.include_router(create_state_router(lambda:service,access))

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
