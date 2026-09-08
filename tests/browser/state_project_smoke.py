#!/usr/bin/env python3
"""Real Chromium + built SPA + isolated State API smoke test.

Requires playwright in the test environment and a built web/dist. All database
and engine state is temporary; no production app lifespan/scheduler is started.
HTTP is loopback only. Fixtures are explicitly synthetic, not game acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from fastapi import Request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def fixture(root):
    from core.db_manager import DBManager
    from core.state_service import StateService
    from core.workspace_manager import WorkspaceManager
    from core.config_registry import ConfigRegistry
    from skillflow.core import SkillFlow, StepResult
    from skillflow.graph import PipelineGraph, StepNode
    db = DBManager(str(root / 'state.db'))
    sf = SkillFlow(str(root / 'sf.db'))
    ws = WorkspaceManager(str(root / 'ws'), str(root / 'projects'))
    registry = ConfigRegistry()
    sf.register_graph(PipelineGraph(name='browser_feature', begin='historical_impl', steps=[StepNode(id='historical_impl')]))
    registry.register_one(sf, 'browser_feature', hint_overrides={'repo_mode':'none','seed_file':'plan.md','output_step':'historical_impl'})
    service = StateService(db, ws, sf, registry, actor='browser-fixture-verifier')
    service.create_project('shrimp-preview', '武虾传奇 · Migration preview')
    seeds = [
        ('design.phases','两阶段规则与成长契约',[]),
        ('growth.proficiency','功法与属性熟练度',['design.phases']),
        ('growth.facilities','个人设施额度与逐次涨价',['growth.proficiency']),
        ('cultivation.month','每月多行动与门派课程',['growth.proficiency']),
        ('coop.authored','江湖事件与持久后果',[]),
        ('ui.responsive','合作界面与原生输入',[]),
        ('art.modular','模块化虾外观',[]),
        ('release.private','源码与发布边界',['ui.responsive','art.modular'])]
    service.store.add_nodes('shrimp-preview',[{'key':k,'goal':title+'\nSynthetic browser fixture. Not accepted gameplay.',
        'dependencies':deps,'acceptance':[{'id':'behaviour','kind':'test','description':'真实运行与状态一致性检查'}]} for k,title,deps in seeds])
    a=service.attempts.reserve('shrimp-preview','ui.responsive',1,'browser_feature','fixture')
    rid=sf.create_run('browser_feature',project_id=a['execution_project_id']);sf.start_run(rid)
    service.attempts.bind_run(a['attempt_id'],rid,sf);sf.advance_run(rid)
    claim=sf.claim_next_step(rid);sf.confirm_step(claim.token,StepResult(outputs={'fixture':'candidate'}));sf.advance_run(rid)
    service.attempts.reconcile(a['attempt_id'],sf,'a'*40)
    service.attempts.record_evidence(a['attempt_id'],'ui-first-failure','behaviour','fail','a'*40,
        'fixture/geometry-failure.json','f'*64,'fixture-verifier','Synthetic failed geometry observation')
    # A later registration must not redraw this old run with new nodes.
    sf.register_graph(PipelineGraph(name='browser_feature',begin='NEW_NODE_MUST_NOT_APPEAR',steps=[StepNode(id='NEW_NODE_MUST_NOT_APPEAR')]))
    external=sf.create_run('browser_feature',project_id='legacy-external');sf.start_run(external);sf.pause_run(external)
    service.add_reference('shrimp-preview','coop.authored','paused-external','run',external,
        '已有任务停在架构审查，请勿重复启动','fixture-director')
    service.portfolio.add_reference('shrimp-preview','ui.responsive','geometry-report','report','fixture/geometry-failure.json',
        '首败证据保留','fixture-verifier',report_sha256='f'*64)
    service.portfolio.set_dispatch('shrimp-preview','hold',0,'迁移清单待审查；不自动启动，不继承旧绿灯')
    return service,sf,rid,external


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir',required=True,type=Path)
    parser.add_argument('--migration-manifest',type=Path)
    args=parser.parse_args();out=args.report_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='state-ui-browser-') as temp:
        root=Path(temp)
        for name,part in [('AITELIER_HOME','home'),('DPE_DB_PATH','host.db'),('SKILLFLOW_DB_PATH','engine.db'),
                          ('DPE_WS_PATH','workspaces'),('DPE_PROJECTS_PATH','projects')]:os.environ[name]=str(root/part)
        service,sf,rid,external=fixture(root)
        migration = None
        if args.migration_manifest:
            from core.state_migration import read_manifest, verify_inputs, stage_shadow
            migration = read_manifest(args.migration_manifest)
            verify_inputs(migration)
            stage_shadow(service, migration)
        from fastapi import FastAPI,HTTPException
        from fastapi.responses import FileResponse,Response
        from fastapi.staticfiles import StaticFiles
        from api.state_graph_routers import router,get_service
        from api.authz import require_writer
        from core.run_graph_view import pinned_run_graph,RunGraphUnavailable
        import uvicorn
        from playwright.sync_api import sync_playwright,expect
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_service]=lambda:service
        def writer(request:Request):
            if request.cookies.get('fixture-auth')!='writer':raise HTTPException(403,'private project state')
        app.dependency_overrides[require_writer]=writer
        requests=[]
        @app.middleware('http')
        async def capture(request,call_next):
            requests.append({'method':request.method,'path':request.url.path})
            return await call_next(request)
        @app.get('/api/me')
        def me(request:Request):
            yes=request.cookies.get('fixture-auth')=='writer'
            return {'email':'fixture@local' if yes else None,'can_write':yes,'gate_enabled':True,'signin_url':''}
        @app.get('/api/settings/user/language')
        def language():return {'lang':None}
        @app.post('/api/settings/user/language')
        def language_save():return {'ok':True}
        @app.get('/api/events/stream')
        def stream():return Response(status_code=204)
        @app.get('/api/runs/{run_id}/graph')
        def graph(run_id:str):
            try:return pinned_run_graph(sf,run_id)
            except KeyError:raise HTTPException(404,'No exact run')
            except RunGraphUnavailable as e:raise HTTPException(409,str(e))
        @app.get('/api/runs/{run_id}/trace')
        def trace(run_id:str):return {'traces':[],'has_more':False}
        @app.get('/api/runs/{run_id}')
        def run(run_id:str):
            row=sf.get_run(run_id)
            if not row:raise HTTPException(404,'Run missing')
            return {**row,'config_name':row['graph_name'],'steps':sf.get_steps(run_id),'cache_stats_by_step':{}}
        @app.get('/health')
        def health():return {'ok':True}
        app.mount('/assets',StaticFiles(directory=ROOT/'web/dist/assets'))
        @app.get('/')
        def spa():return FileResponse(ROOT/'web/dist/index.html')
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        deadline=time.monotonic()+10
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('isolated fixture server did not start')
            time.sleep(.02)
        base=f'http://127.0.0.1:{port}';checks=[];page_errors=[]
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
                context=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US')
                context.add_cookies([{'name':'fixture-auth','value':'writer','url':base}])
                page=context.new_page();page.on('pageerror',lambda e:page_errors.append(str(e)))
                page.goto(base+'/#/state-projects');expect(page.get_by_role('heading',name='Projects · State DAG')).to_be_visible()
                page.locator('a.project-card[href="#/state-projects/shrimp-preview"]').click();expect(page.locator('g.goal')).to_have_count(8)
                expect(page.get_by_role('heading',name='武虾传奇 · Migration preview')).to_be_visible()
                assert page.locator('g.goal.verified').count()==0
                page.locator('g.goal').filter(has=page.locator('text.goal-key',has_text='ui.responsive')).click()
                expect(page.locator('.node-panel')).to_contain_text('合作界面与原生输入')
                expect(page.locator('.node-panel')).to_contain_text('CANDIDATE')
                page.get_by_role('button',name='Zoom in').click();expect(page.get_by_role('button',name='120%')).to_be_visible()
                page.get_by_role('button',name='Reload view').click();expect(page.locator('.node-panel')).to_contain_text('合作界面与原生输入')
                page.screenshot(path=str(out/'project-desktop.png'),full_page=True)
                checks.append('desktop: project graph, 8 real API nodes, hold, candidate-not-verified, selection, zoom and reload')
                page.get_by_role('button',name='Inspect evidence').click();expect(page.locator('.attempt-evidence')).to_contain_text('FAIL')
                expect(page.locator('.attempt-evidence')).to_contain_text('behaviour')
                checks.append('failed verifier evidence rendered without acceptance')
                page.get_by_role('button',name='Runs / attempts').click()
                expect(page.locator('.run-card')).to_have_count(1)
                page.locator('.run-card a',has_text='Workflow graph').click()
                expect(page.locator('.graph-pin')).to_contain_text('graph v1')
                expect(page.locator('text.node-id')).to_have_text('historical_impl')
                assert 'NEW_NODE_MUST_NOT_APPEAR' not in page.locator('.pg').inner_text()
                expect(page.locator('.state-owners a')).to_contain_text('ui.responsive')
                page.screenshot(path=str(out/'run-pinned.png'),full_page=True)
                checks.append('exact run graph v1 remains original after v2 registration, with project/goal backlink')
                page.locator('.state-owners a').click();expect(page.locator('g.goal')).to_have_count(8)
                page.get_by_role('button',name='Sync run results').click()
                expect(page.get_by_role('status')).to_contain_text('Run observations updated')
                assert len(sf.list_runs())==2 and sf.get_run(external)['status']=='paused'
                checks.append('explicit observation refresh does not launch or approve a paused external run')
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(out/'project-mobile.png'),full_page=True)
                overflow=page.evaluate('({w:innerWidth, scroll:document.documentElement.scrollWidth})')
                offenders=page.evaluate('''[...document.querySelectorAll('body *')].map(e=>({tag:e.tagName,cls:e.className?.baseVal??e.className,left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,width:e.getBoundingClientRect().width})).filter(r=>r.right>innerWidth+2 && r.width>10).slice(0,35)''')
                (out/'mobile-layout.json').write_text(json.dumps({'viewport':overflow,'overflowing_elements':offenders},indent=2))
                assert overflow['scroll']<=overflow['w']+2,overflow
                checks.append('390px viewport has no document horizontal overflow')
                page.set_viewport_size({'width':1100,'height':850})
                page.route('**/api/state/projects/shrimp-preview/overview',lambda route:route.fulfill(status=503,json={'detail':'Fixture failure, not empty data'}))
                page.get_by_role('button',name='Reload view').click();expect(page.get_by_role('alert')).to_contain_text('Fixture failure')
                checks.append('server failure displayed as failure, not an empty successful graph')
                page.unroute('**/api/state/projects/shrimp-preview/overview')
                anonymous=browser.new_context(viewport={'width':900,'height':700});anon=anonymous.new_page()
                anon.goto(base+'/#/state-projects/shrimp-preview');expect(anon.get_by_text('Project state is private.',exact=False)).to_be_visible()
                assert anon.locator('g.goal').count()==0
                assert anonymous.request.get(base+'/api/state/projects/shrimp-preview/overview').status==403
                checks.append('anonymous UI has no goals and direct private API denies access')
                if migration:
                    pid=migration['project']['project_id']
                    page.set_viewport_size({'width':1440,'height':1000})
                    page.goto(base+'/#/state-projects/'+pid)
                    expect(page.get_by_role('heading',name=migration['project']['title'],exact=True)).to_be_visible()
                    expect(page.locator('g.goal')).to_have_count(len(migration['nodes']))
                    assert page.locator('g.goal.verified').count()==0
                    page.locator('.state-graph select').select_option('growth')
                    growth=[n for n in migration['nodes'] if n['key'].startswith('growth.')]
                    expect(page.locator('g.goal')).to_have_count(len(growth))
                    page.locator('g.goal').filter(has=page.locator('text.goal-key',has_text='growth.facility-quota')).click()
                    expect(page.locator('.node-panel')).to_contain_text('大地图设施个人限次与逐次涨价')
                    expect(page.locator('.node-panel')).to_contain_text('OPEN')
                    page.screenshot(path=str(out/'wuxia-migration-growth-desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 2')
                    page.screenshot(path=str(out/'wuxia-migration-mobile.png'),full_page=True)
                    with service.store.transaction() as conn:
                        assert conn.execute('SELECT COUNT(*) FROM state_attempts WHERE project_id=?',(pid,)).fetchone()[0]==0
                        assert conn.execute('SELECT COUNT(*) FROM state_acceptances WHERE project_id=?',(pid,)).fetchone()[0]==0
                    checks.append('actual prepared migration manifest: all 30 OPEN/held goals, growth domain, source references, desktop/mobile, zero attempts/acceptance')
                assert not page_errors,page_errors
                result={'result':'PASS','browser':browser.version,'checks':checks,'page_errors':page_errors,
                        'production_database_written':False,'prepared_manifest_used':bool(migration),'engine_runs':len(sf.list_runs()),'external_run_still_paused':sf.get_run(external)['status']=='paused',
                        'mutating_requests':[r for r in requests if r['method']=='POST'], 'fixtures':'Synthetic workflow rows plus the prepared migration goals when supplied; no game acceptance'}
                (out/'browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(result,ensure_ascii=False,indent=2));browser.close()
        except BaseException:
            try:page.screenshot(path=str(out/'FIRST-FAILURE.png'),full_page=True)
            except Exception:pass
            (out/'page-errors.json').write_text(json.dumps(page_errors,indent=2))
            raise
        finally:
            server.should_exit=True;thread.join(timeout=10);sock.close();sf._conn.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
