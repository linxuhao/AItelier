"""Real lifespan-installed bridge and SSE framing, isolated from backend work."""
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from skillflow.notifications import NotificationBus
from api import main, dependencies, sse_manager
from core import scheduler


@asynccontextmanager
async def no_backend():
    yield


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    db_path = tmp_path / 'runs.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE skillflow_runs (id TEXT, project_id TEXT, graph_name TEXT)')
    monkeypatch.setattr(dependencies, 'SKILLFLOW_DB_PATH', str(db_path))
    bus = NotificationBus()
    manager = sse_manager.StreamManager()
    monkeypatch.setattr(dependencies, 'get_skillflow', lambda: SimpleNamespace(notifications=bus))
    monkeypatch.setattr(main, 'stream_manager', manager)
    monkeypatch.setattr(sse_manager, 'set_main_loop', lambda loop: None)
    monkeypatch.setattr(scheduler, 'acquire_instance_lock', lambda: True)
    monkeypatch.setattr(scheduler, 'recover_claims_on_startup', lambda: None)
    monkeypatch.setattr(scheduler, 'recover_leases_on_startup', lambda: None)
    monkeypatch.setattr(main, 'start_scheduler', lambda: None)
    endpoint = SimpleNamespace(session_manager=SimpleNamespace(run=no_backend))
    monkeypatch.setattr(main, '_mcp_endpoint', SimpleNamespace(open=lambda: endpoint, close=lambda: None))

    @asynccontextmanager
    async def subscribed():
        async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            stream = manager.event_generator('__global__')
            await anext(stream)  # actual presence frame proves registration
            async def event(kind, payload, run='r1'):
                await bus.publish(kind, payload, run_id=run)
                frame = await anext(stream)
                return json.loads(json.loads(frame.removeprefix('data: ').strip())['log'])
            try:
                yield event
            finally:
                await stream.aclose()
    return SimpleNamespace(path=db_path, subscribed=subscribed)


@pytest.mark.asyncio
async def test_event_identity_survives_uncommitted_run_insert(bridge, monkeypatch):
    # Two real connections: the bridge cannot see this row until commit.
    writer = sqlite3.connect(bridge.path)
    writer.execute('INSERT INTO skillflow_runs VALUES (?,?,?)', ('r1', 'p1', 'review'))
    lookups = []
    original = sqlite3.connect
    def observed_connect(path, *args, **kwargs):
        if str(path) == str(bridge.path):
            lookups.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    try:
        async with bridge.subscribed() as event:
            created = await event('run_created', {'project_id': 'p1', 'graph_name': 'review'})
            assert created['project_id'] == 'p1'
            writer.commit()  # explicit ordering barrier, no race or timing sleep
            terminal = await event('run_completed', {})
            assert terminal['project_id'] == 'p1'
            assert terminal['graph_name'] == 'review'
            assert terminal['run_id'] == 'r1'
            assert terminal['type'] == 'run_completed'
            assert lookups == []  # supplied identities need no speculative DB read
    finally:
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('first_read', ['missing', 'query_error'])
async def test_transient_identity_lookup_recovers(bridge, monkeypatch, first_read):
    async with bridge.subscribed() as event:
        if first_read == 'query_error':
            original = sqlite3.connect
            def unavailable(path, *args, **kwargs):
                if str(path) == str(bridge.path):
                    raise sqlite3.OperationalError('INJECTED first lookup unavailable')
                return original(path, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, 'connect', unavailable)
                first = await event('agent_notification', {})
        else:
            first = await event('agent_notification', {})
        assert not first.get('project_id')
        with sqlite3.connect(bridge.path) as db:
            db.execute('INSERT INTO skillflow_runs VALUES (?,?,?)', ('r1', 'p1', 'review'))
        terminal = await event('run_completed', {})
        assert (terminal['project_id'], terminal['graph_name'], terminal['run_id']) == ('p1', 'review', 'r1')


@pytest.mark.asyncio
async def test_run_caches_and_explicit_event_identity_stay_distinct(bridge):
    with sqlite3.connect(bridge.path) as db:
        db.executemany('INSERT INTO skillflow_runs VALUES (?,?,?)',
                       [('r1', 'p1', 'review'), ('r2', 'p2', 'review')])
    async with bridge.subscribed() as event:
        one = await event('run_completed', {}, 'r1')
        two = await event('run_completed', {}, 'r2')
        assert (one['project_id'], one['run_id']) == ('p1', 'r1')
        assert (two['project_id'], two['run_id']) == ('p2', 'r2')
        explicit = await event('run_completed', {'project_id': 'other'}, 'r1')
        assert explicit['project_id'] == 'other'  # never rewrite a mismatch into agreement
        again = await event('run_completed', {}, 'r1')
        assert again['project_id'] == 'p1'  # conflicting payload does not poison a known identity
