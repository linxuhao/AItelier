"""Owned actual lifespan boundaries; preserves first d255 failures in reports."""
import asyncio, threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from skillflow import SkillFlow, PipelineGraph
from skillflow.graph import StepNode
from core import scheduler as sc, run_resources
from core.db_manager import DBManager
import api.dependencies as deps

@pytest.fixture
def env(tmp_path, monkeypatch):
    sf=SkillFlow(str(tmp_path/'engine.db'),workspace_base=str(tmp_path/'ws'),projects_base=str(tmp_path/'projects'))
    db=DBManager(str(tmp_path/'host.db'))
    monkeypatch.setattr(sc,'db',db);monkeypatch.setattr(sc,'get_skillflow',lambda:sf);monkeypatch.setattr(deps,'get_skillflow',lambda:sf)
    monkeypatch.setattr(sc,'_lease_sweep_future',None);monkeypatch.setattr(sc,'_last_lease_sweep',0.0);monkeypatch.setattr(sc,'_LEASE_SWEEP_INTERVAL_S',0.0)
    monkeypatch.setattr(sc,'_lease_sweep_reported',{})
    monkeypatch.setattr(db,'get_active_projects',lambda **kw:[]);monkeypatch.setattr(db,'get_next_active_project',lambda **kw:None)
    entered=threading.Event();release=threading.Event();finished=threading.Event();calls=[];connections=[]
    real=run_resources.reconcile
    def slow(*a,**k):
        calls.append(threading.get_ident());connections.append(id(sf._conn))
        assert sf.get_run('independent-unknown') is None
        assert run_resources.terminal_quiet(sf,'independent-unknown')[0] is None
        entered.set()
        try:
            assert release.wait(5),'fixture release deadline'
            return real(*a,**k)
        finally:finished.set()
    monkeypatch.setattr(run_resources,'reconcile',slow)
    data=SimpleNamespace(sf=sf,db=db,entered=entered,release=release,finished=finished,calls=calls,connections=connections)
    yield data
    release.set()
    assert sc._lease_sweep_future is None,'maintenance remained owned after test cleanup'
    sf._conn.close()

async def entered(e):
    for _ in range(200):
        if e.entered.is_set():return
        await asyncio.sleep(.005)
    assert False,'worker did not enter actual reconcile'

async def cleanup(e,poll):
    e.release.set()
    await asyncio.gather(poll,return_exceptions=True)
    await sc.settle_scheduler_maintenance()

@pytest.mark.asyncio
async def test_real_db_concurrent_hungcheck_shared_then_new_invocation(env,monkeypatch):
    e=env;main=threading.get_ident();mainconn=id(e.sf._conn)
    e.sf.register_graph(PipelineGraph(name='independent',begin='a',steps=[StepNode(id='a')]))
    rid=e.sf.create_run('independent',project_id='independent');e.sf.start_run(rid);e.sf.advance_run(rid);claim=e.sf.claim_next_step(rid)
    seen=[];real=e.sf.recover_stale_claims
    def recover(*a,**k):seen.append(threading.get_ident());return real(*a,**k)
    monkeypatch.setattr(e.sf,'recover_stale_claims',recover)
    poll=asyncio.create_task(sc.poll_and_execute());await entered(e)
    twin=asyncio.create_task(sc.poll_and_execute_owner('owned'))
    try:
        await asyncio.wait_for(sc._check_hung_claims(),.5)
        assert seen==[main] and len(e.calls)==1 and e.calls[0]!=main
        row=e.sf._conn.execute('SELECT status,retry_count FROM skillflow_steps WHERE id=?',(claim.token.step_instance_id,)).fetchone()
        assert row['status']=='claimed' and row['retry_count']==0
        assert not e.finished.is_set()
        print('actual shared-connection reads/recovery with claimed row preserved:',e.connections,mainconn,dict(row))
    finally:
        await cleanup(e,poll);await twin
    await sc.poll_and_execute_demo()
    assert len(e.calls)==2 and sc._lease_sweep_future is None
    print('actual worker/main DB connections:',e.connections,mainconn,'hung recovery threads',seen)

@pytest.mark.asyncio
async def test_multiple_waiter_cancellation_error_and_fresh_failure(env,monkeypatch,caplog):
    e=env
    def fail():
        e.entered.set();assert e.release.wait(5);e.finished.set();raise RuntimeError('independent-worker-error')
    monkeypatch.setattr(sc,'_sweep_ended_leases',fail)
    p=asyncio.create_task(sc.poll_and_execute());await entered(e)
    drain=asyncio.create_task(sc.settle_scheduler_maintenance());await asyncio.sleep(.01)
    for _ in range(3):p.cancel();drain.cancel();await asyncio.sleep(.01)
    try:
        assert not p.done() and not drain.done() and not e.finished.is_set()
        e.release.set()
        results=await asyncio.gather(p,drain,return_exceptions=True)
        assert all(isinstance(x,asyncio.CancelledError) for x in results)
        assert 'independent-worker-error' in caplog.text and sc._lease_sweep_future is None
        with pytest.raises(RuntimeError,match='independent-worker-error'):await sc.poll_and_execute()
    finally:await cleanup(e,p);await asyncio.gather(drain,return_exceptions=True)

class Endpoint:
    def __init__(self):self.session_manager=self;self.closed=False
    def open(self):return self
    @asynccontextmanager
    async def run(self):yield
    def close(self):self.closed=True

def prepare_app(kind,e,monkeypatch):
    if kind=='api':
        import api.main as module
        endpoint=Endpoint();monkeypatch.setattr(module,'_mcp_endpoint',endpoint)
        monkeypatch.setattr(sc,'acquire_instance_lock',lambda:True)
        monkeypatch.setattr(sc,'recover_claims_on_startup',lambda:None);monkeypatch.setattr(sc,'recover_leases_on_startup',lambda:None)
        import api.sse_manager as sm
        monkeypatch.setattr(sm,'set_main_loop',lambda x:None)
    else:
        import web_api.main as module
        monkeypatch.setenv('AITELIER_MODE','normal' if kind=='web_normal' else 'demo')
    scheduler=AsyncIOScheduler();sc._add_scheduler_job(scheduler,{'scheduler_type':'interval','scheduler_interval':30})
    def start(**kw):scheduler.start();return scheduler
    monkeypatch.setattr(module,'start_scheduler',start)
    if kind=='web_normal':
        import web_api.scheduler_manager as sm
        real_manager=sm.UserSchedulerManager
        monkeypatch.setattr(sm,'start_scheduler',start)
        def manager():
            result=real_manager();result.get_or_create('owned-normal');return result
        monkeypatch.setattr(sm,'UserSchedulerManager',manager)
    return module,FastAPI(),scheduler

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['api','web','web_normal'])
async def test_actual_lifespan_clean_exit_waits_and_repeated_cancel_settles(env,monkeypatch,kind):
    e=env;module,app,scheduler=prepare_app(kind,e,monkeypatch)
    cm=module.lifespan(app);await cm.__aenter__()
    p=asyncio.create_task(sc.poll_and_execute());await entered(e)
    exiting=asyncio.create_task(cm.__aexit__(None,None,None));await asyncio.sleep(.03)
    try:
        assert not exiting.done() and not e.finished.is_set() and not scheduler.running
        exiting.cancel();await asyncio.sleep(.01);exiting.cancel();await asyncio.sleep(.01)
        assert not exiting.done()
        e.release.set()
        with pytest.raises(asyncio.CancelledError):await exiting
        assert e.finished.is_set() and sc._lease_sweep_future is None
    finally:
        await cleanup(e,p);await asyncio.gather(exiting,return_exceptions=True)
        if scheduler.running:scheduler.shutdown(wait=False)

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['api','web','web_normal'])
@pytest.mark.parametrize('exc_type',[RuntimeError,asyncio.CancelledError])
async def test_actual_lifespan_exception_exit_must_settle_owned_worker(env,monkeypatch,kind,exc_type):
    e=env;module,app,scheduler=prepare_app(kind,e,monkeypatch)
    cm=module.lifespan(app);await cm.__aenter__()
    p=asyncio.create_task(sc.poll_and_execute());await entered(e)
    exc=exc_type('independent-lifespan-error')
    exiting=asyncio.create_task(cm.__aexit__(exc_type,exc,None));await asyncio.sleep(.04)
    try:
        print('exception exit',kind,exc_type.__name__,'exit_done',exiting.done(),'scheduler_running',scheduler.running,'worker_finished',e.finished.is_set(),'future_owned',sc._lease_sweep_future is not None)
        assert not exiting.done(),'actual lifespan exception/cancellation exits before worker completes'
        assert not scheduler.running,'scheduler admission must stop on exceptional exit'
        e.release.set();await asyncio.gather(exiting,return_exceptions=True)
        assert e.finished.is_set() and sc._lease_sweep_future is None
        assert exiting.result() is False, 'original body error/cancellation must propagate from asynccontextmanager'
    finally:
        if scheduler.running:scheduler.shutdown(wait=False)
        await cleanup(e,p);await asyncio.gather(exiting,return_exceptions=True)



@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['api','web','web_normal'])
async def test_lifespan_cancel_at_first_shutdown_yield_still_drains(env,monkeypatch,kind):
    e=env;module,app,scheduler=prepare_app(kind,e,monkeypatch)
    cm=module.lifespan(app);await cm.__aenter__()
    poll=asyncio.create_task(sc.poll_and_execute());await entered(e)
    original=scheduler.shutdown
    def shutdown(**kw):
        original(**kw)
        asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
    monkeypatch.setattr(scheduler,'shutdown',shutdown)
    exiting=asyncio.create_task(cm.__aexit__(None,None,None));await asyncio.sleep(.03)
    try:
        assert not exiting.done() and not e.finished.is_set() and not scheduler.running
        e.release.set()
        with pytest.raises(asyncio.CancelledError):await exiting
        assert e.finished.is_set() and sc._lease_sweep_future is None
    finally:
        await cleanup(e,poll);await asyncio.gather(exiting,return_exceptions=True)

@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['api','web','web_normal'])
@pytest.mark.parametrize('exc_type',[RuntimeError,asyncio.CancelledError])
async def test_original_body_error_retained_after_worker_error(env,monkeypatch,kind,exc_type,caplog):
    e=env;module,app,scheduler=prepare_app(kind,e,monkeypatch)
    def fail():
        e.entered.set();assert e.release.wait(5);e.finished.set();raise RuntimeError('distinct-worker-failure')
    monkeypatch.setattr(sc,'_sweep_ended_leases',fail)
    cm=module.lifespan(app);await cm.__aenter__()
    poll=asyncio.create_task(sc.poll_and_execute());await entered(e)
    body_error=exc_type('original-body-failure')
    exiting=asyncio.create_task(cm.__aexit__(exc_type,body_error,None));await asyncio.sleep(.03)
    try:
        assert not exiting.done() and not scheduler.running
        e.release.set()
        assert await exiting is False
        # Returning False from __aexit__ preserves exact original object for its caller.
        assert e.finished.is_set() and sc._lease_sweep_future is None
        assert 'distinct-worker-failure' in caplog.text and 'lease sweep failed during lifespan exit' in caplog.text
        with pytest.raises(RuntimeError,match='distinct-worker-failure'):await poll
    finally:
        await cleanup(e,poll);await asyncio.gather(exiting,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['api','web','web_normal'])
async def test_shutdown_error_still_drains_before_error_propagates(env,monkeypatch,kind):
    e=env;module,app,scheduler=prepare_app(kind,e,monkeypatch)
    cm=module.lifespan(app);await cm.__aenter__()
    poll=asyncio.create_task(sc.poll_and_execute());await entered(e)
    original=scheduler.shutdown
    def shutdown(**kw):
        original(**kw)
        raise RuntimeError('owned-shutdown-error')
    monkeypatch.setattr(scheduler,'shutdown',shutdown)
    exiting=asyncio.create_task(cm.__aexit__(None,None,None));await asyncio.sleep(.03)
    try:
        assert not exiting.done() and not e.finished.is_set() and not scheduler.running
        e.release.set()
        with pytest.raises(RuntimeError,match='owned-shutdown-error'):await exiting
        assert e.finished.is_set() and sc._lease_sweep_future is None
        if kind=='web_normal':assert not app.state._reaper.running
    finally:
        await cleanup(e,poll);await asyncio.gather(exiting,return_exceptions=True)
        if kind=='web_normal' and app.state._reaper.running:app.state._reaper.shutdown(wait=False)
