"""Controlled slow maintenance on real poll/hung-check/shutdown boundaries."""
import asyncio
import threading
from pathlib import Path

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from skillflow import SkillFlow, PipelineGraph
from skillflow.graph import StepNode
from core import scheduler as sc, run_resources
from core.db_manager import DBManager
import api.dependencies as deps


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    db=DBManager(str(tmp_path/'host.db'))
    sf=SkillFlow(str(tmp_path/'engine.db'))
    monkeypatch.setattr(sc,'db',db)
    monkeypatch.setattr(sc,'get_skillflow',lambda:sf)
    monkeypatch.setattr(deps,'get_skillflow',lambda:sf)
    monkeypatch.setattr(sc,'_last_lease_sweep',0.0)
    monkeypatch.setattr(sc,'_LEASE_SWEEP_INTERVAL_S',0.0)
    monkeypatch.setattr(sc,'_lease_sweep_future',None)
    monkeypatch.setattr(sc,'_lease_sweep_reported',{})
    monkeypatch.setattr(db,'get_active_projects',lambda **kw:[])
    monkeypatch.setattr(db,'get_next_active_project',lambda **kw:None)
    entered=threading.Event();release=threading.Event();finished=threading.Event()
    calls=[];real=run_resources.reconcile
    def slow(*args,**kwargs):
        calls.append(threading.get_ident());entered.set()
        try:
            assert release.wait(3.0),'owned slow-work release deadline expired'
            return real(*args,**kwargs)
        finally:finished.set()
    monkeypatch.setattr(run_resources,'reconcile',slow)
    yield sf,entered,release,finished,calls
    release.set()
    assert sc._lease_sweep_future is None,'owned maintenance outlived control'
    sf._conn.close()


async def _entered(event):
    deadline=asyncio.get_running_loop().time()+1
    while not event.is_set():
        assert asyncio.get_running_loop().time()<deadline,'sweep did not start'
        await asyncio.sleep(0.005)


def test_slow_actual_sweep_allows_control_work_and_real_hung_claim_check(maintenance,monkeypatch):
    async def scenario():
        sf,entered,release,finished,calls=maintenance
        sf.register_graph(PipelineGraph(name='owned',begin='a',steps=[StepNode(id='a')]))
        rid=sf.create_run('owned',project_id='owned');sf.start_run(rid);sf.advance_run(rid)
        claim=sf.claim_next_step(rid)
        recovered=[];real=sf.recover_stale_claims
        def recover(*a,**k):recovered.append(threading.get_ident());return real(*a,**k)
        monkeypatch.setattr(sf,'recover_stale_claims',recover)
        poll=asyncio.create_task(sc.poll_and_execute())
        try:
            await _entered(entered)
            assert not poll.done() and not finished.is_set()
            progress=[]
            async def control():
                await asyncio.sleep(0);progress.append('control-request')
                await sc._check_hung_claims()
            await asyncio.wait_for(control(),0.5)
            assert progress==['control-request'] and recovered==[threading.get_ident()]
            row=sf._conn.execute('SELECT status,retry_count FROM skillflow_steps WHERE id=?',(claim.token.step_instance_id,)).fetchone()
            assert row['status']=='claimed' and row['retry_count']==0
            assert not finished.is_set() and calls[0]!=threading.get_ident()
        finally:
            release.set();await poll
    asyncio.run(scenario())


def test_overlapping_scheduler_entries_share_one_actual_sweep(maintenance):
    async def scenario():
        sf,entered,release,finished,calls=maintenance
        tasks=[asyncio.create_task(sc.poll_and_execute()),asyncio.create_task(sc.poll_and_execute_demo()),asyncio.create_task(sc.poll_and_execute_owner('owner'))]
        try:
            await _entered(entered);await asyncio.sleep(0.03)
            assert len(calls)==1 and not finished.is_set()
            assert all(not t.done() for t in tasks)
        finally:release.set();await asyncio.gather(*tasks)
        assert len(calls)==1 and finished.is_set()
    asyncio.run(scenario())


def test_repeated_poll_cancellation_keeps_sweep_owned_until_real_completion(maintenance):
    async def scenario():
        sf,entered,release,finished,calls=maintenance
        poll=asyncio.create_task(sc.poll_and_execute())
        await _entered(entered);future=sc._lease_sweep_future
        poll.cancel();await asyncio.sleep(0.02);poll.cancel();await asyncio.sleep(0.02)
        assert not poll.done() and sc._lease_sweep_future is future and not future.done()
        other=asyncio.create_task(sc.poll_and_execute());await asyncio.sleep(0.02)
        assert len(calls)==1 and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):await poll
        await other
        assert finished.is_set() and sc._lease_sweep_future is None
    asyncio.run(scenario())


def test_actual_scheduler_shutdown_drain_waits_for_maintenance(maintenance):
    async def scenario():
        sf,entered,release,finished,calls=maintenance
        scheduler=AsyncIOScheduler();sc._add_scheduler_job(scheduler,{'scheduler_type':'interval','scheduler_interval':30})
        scheduler.add_job(sc.poll_and_execute,'date');scheduler.start()
        try:
            await _entered(entered);scheduler.shutdown(wait=True)
            drain=asyncio.create_task(sc.settle_scheduler_maintenance());await asyncio.sleep(0.03)
            assert not drain.done() and not finished.is_set() and sc._lease_sweep_future is not None
            release.set();await asyncio.wait_for(drain,1)
            assert not scheduler.running and finished.is_set() and sc._lease_sweep_future is None
        finally:
            release.set()
            if scheduler.running:scheduler.shutdown(wait=False)
            await sc.settle_scheduler_maintenance()
    asyncio.run(scenario())


def test_cancelled_sweep_retains_unexpected_worker_error(maintenance,monkeypatch,caplog):
    async def scenario():
        sf,entered,release,finished,calls=maintenance
        def failing():
            entered.set();release.wait(3);finished.set();raise RuntimeError('owned maintenance failure')
        monkeypatch.setattr(sc,'_sweep_ended_leases',failing)
        poll=asyncio.create_task(sc.poll_and_execute());await _entered(entered);poll.cancel();await asyncio.sleep(0.02)
        assert not poll.done() and sc._lease_sweep_future is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):await poll
        assert finished.is_set() and sc._lease_sweep_future is None
        assert 'owned maintenance failure' in caplog.text and 'lease sweep failed while poll was cancelled' in caplog.text
    asyncio.run(scenario())
