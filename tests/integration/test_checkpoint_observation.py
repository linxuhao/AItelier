"""Controlled observer conformance, never a historical failure reproduction."""
import json
import asyncio
import logging
import traceback
import importlib.metadata
import skillflow
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from tests.integration.test_meta_checkpoint_finalize import normal, approve
from api import meta_routers as mr
from core import checkpoint_observation as observation


class MarkerError(RuntimeError):
    pass


@pytest.fixture
def records(monkeypatch, normal, tmp_path):
    sf, db, ws, pid, rid, root, client, cp, wakes = normal
    rows = []
    original_app = client._transport.app
    raw = Path('/raw') if Path('/raw').is_dir() else tmp_path
    failures, errors = [], []
    asgi_path = raw/(tmp_path.name+'-asgi.json')
    class ObservedASGI:
        async def __call__(self, scope, receive, send):
            status = None
            async def observed_send(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                await send(message)
            try:
                await original_app(scope, receive, observed_send)
            except BaseException as exc:
                errors.append(exc)
                failures.append({'status': status, 'type': type(exc).__name__,
                    'cause_type': type(exc.__cause__).__name__ if exc.__cause__ is not None else None,
                    'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name}
                               for f in traceback.extract_tb(exc.__traceback__)],
                    'thread': threading.get_ident(), 'loop': id(asyncio.get_running_loop())})
                asgi_path.write_text(json.dumps(failures, indent=2))
                raise
    observer = ObservedASGI()
    # TestClient's transport captured its app at construction; bind that object
    # before requests, as well as the public app reference used for readback.
    monkeypatch.setattr(client._transport, 'app', observer)
    monkeypatch.setattr(client, 'app', observer)
    monkeypatch.setattr(client, '_checkpoint_observer_errors', errors, raising=False)
    monkeypatch.setattr(client, '_checkpoint_asgi_path', asgi_path, raising=False)
    assert client._transport.app is client.app is observer
    topology = {'sdk_module': skillflow.__file__, 'sdk_version': importlib.metadata.version('skillflow-py'),
                'sdk_db_path': sf._db_path, 'trace_db_path': sf._trace_db_path,
                'trace_uses_engine_connection': sf._trace_db_path is None,
                'engine_connection_id': id(sf._conn), 'engine_lock_type': type(sf._lock).__name__,
                'shared_testclient_portal': client.portal is not None}
    (raw/(tmp_path.name+'-topology.json')).write_text(json.dumps(topology, indent=2))
    class Retain(logging.Handler):
        def emit(self, record):
            row = record.checkpoint_observation
            assert json.loads(record.getMessage().split(' ', 1)[1]) == row
            assert 'SECRET' not in record.getMessage()
            rows.append(row)
            (raw/(tmp_path.name+'-records.json')).write_text(json.dumps(rows, indent=2))
    handler = Retain()
    observation.logger.addHandler(handler)
    yield rows
    observation.logger.removeHandler(handler)



def assert_outer_failure(client, marker, cause=None):
    assert client._checkpoint_observer_errors == [marker]
    assert client._checkpoint_observer_errors[0] is marker
    assert marker.__cause__ is cause
    path = client._checkpoint_asgi_path
    assert path.is_file() and path.stat().st_size > 0
    captured = json.loads(path.read_text())
    assert len(captured) == 1
    row = captured[0]
    assert row['status'] == 500 and row['type'] == type(marker).__name__
    assert row['cause_type'] == (type(cause).__name__ if cause is not None else None)
    assert row['frames'][-1]['function'].startswith('fail_')
    assert row['frames'][-1]['line'] > 0
    assert row['thread'] == client.portal.call(threading.get_ident)
    assert row['loop'] == id(client.portal.call(asyncio.get_running_loop))
    row.update(same_marker=True, same_cause=True)
    path.write_text(json.dumps(captured, indent=2))


@pytest.mark.parametrize('route,phase', [('run', 'route_run_lookup'), ('project', 'run_lookup')])
def test_original_untranslated_rest_failure(normal, monkeypatch, records, route, phase):
    sf, db, ws, pid, rid, root, client, cp, wakes = normal
    marker = MarkerError('SECRET must not appear in diagnostics')
    cause = ValueError('SECRET cause')
    def fail_lookup(value):
        raise marker from cause
    monkeypatch.setattr(sf, 'get_run', fail_lookup)
    path = f'/api/runs/{rid}/checkpoint/approve' if route == 'run' else f'/api/meta/{pid}/checkpoint/approve'
    reply = client.post(path, json={'run_id': rid, 'checkpoint': 'gather',
        'expected_checkpoint': cp['expected_checkpoint'], 'decision_id': 'observed-marker'})
    assert reply.status_code == 500 and reply.text == 'Internal Server Error'
    assert_outer_failure(client, marker, cause)
    assert len(records) == 1
    row = records[0]
    assert row['phase'] == phase and row['run_id'] == rid
    assert row['decision_id'] == 'observed-marker'
    assert row['thread_id'] != threading.get_ident()
    assert row['request_id'] and not row['translated']
    assert row['exceptions'][0]['type'].endswith('.MarkerError')
    assert row['exceptions'][0]['frames'][-1]['function'] == 'fail_lookup'
    assert row['exceptions'][0]['frames'][-1]['line'] > 0
    assert 'SECRET' not in json.dumps(row)
    assert not wakes


def test_translated_controller_boundary_is_not_changed(normal, monkeypatch, records):
    marker = MarkerError('controlled controller marker')
    def fail_controller(*args):
        raise marker
    monkeypatch.setattr(mr, 'state_checkpoint_controller', fail_controller)
    assert approve(normal).status_code == 500
    assert_outer_failure(normal[6], marker)
    assert len(records) == 1 and records[0]['phase'] == 'state_controller'


def test_real_finalize_refusal_logs_cause_without_changing_409(normal, records):
    sf, db, ws, pid, rid, root, client, cp, wakes = normal
    (root/'project/project_brief.md').mkdir(parents=True)
    response = approve(normal)
    assert response.status_code == 409
    assert response.json()['detail'].startswith('Brief approval/finalization not confirmed: ')
    assert sf.get_run(rid)['status'] != 'completed'
    assert db.get_project(pid)['meta_state'] == 'drafting' and not wakes
    assert len(records) == 1 and records[0]['translated']
    assert records[0]['phase'] == 'advance_finalize'
    assert any(e['type'].endswith('IsADirectoryError') for e in records[0]['exceptions'])
    assert any(f['function'] == 'emit_project_artifacts' for e in records[0]['exceptions'] for f in e['frames'])


@pytest.mark.parametrize('sink_failure', [False, True])
def test_original_object_chain_and_traceback_survive_sink_failure(monkeypatch, records, sink_failure):
    marker = MarkerError('SECRET')
    cause = ValueError('SECRET cause')
    def throw():
        raise marker from cause
    @observation.observe_checkpoint
    def outer():
        observation.checkpoint_phase('controlled')
        return throw()
    if sink_failure:
        def broken_sink(*args, **kwargs):
            raise RuntimeError('sink failure')
        monkeypatch.setattr(observation.logger, 'error', broken_sink)
    with pytest.raises(MarkerError) as caught:
        outer()
    assert caught.value is marker and caught.value.__cause__ is cause
    assert caught.value.__traceback__.tb_next is not None
    assert observation._current.get() is None
    if not sink_failure:
        assert records[0]['phase'] == 'controlled'
        assert records[0]['exceptions'][1]['type'] == 'builtins.ValueError'
        assert 'SECRET' not in json.dumps(records)


def test_translated_original_survives_broken_sink(normal, monkeypatch, records):
    marker = MarkerError('controlled helper marker')
    import core.meta_run as meta_run
    def fail_helper(*args, **kwargs):
        raise marker
    def broken_sink(*args, **kwargs):
        raise RuntimeError('sink failure')
    monkeypatch.setattr(meta_run, 'approve_and_submit_meta', fail_helper)
    monkeypatch.setattr(observation.logger, 'error', broken_sink)
    response = approve(normal)
    assert response.status_code == 409
    assert response.json()['detail'] == 'Brief approval/finalization not confirmed: controlled helper marker'


def test_http_refusal_is_not_misreported(records):
    marker = HTTPException(409, 'existing refusal')
    @observation.observe_checkpoint
    def refuse():
        raise marker
    with pytest.raises(HTTPException) as caught:
        refuse()
    assert caught.value is marker and not records


def test_success_replay_has_no_failure_record(normal, records):
    assert approve(normal).status_code == 200
    assert approve(normal).status_code == 200
    assert not records


@pytest.mark.parametrize('lookup,phase', [(1, 'approve_run_lookup'), (2, 'finalize_run_lookup')])
def test_each_in_lock_run_read_has_its_own_phase(normal, records, monkeypatch, tmp_path, lookup, phase):
    import sys
    from core import scheduler
    sf, db, ws, pid, rid, root, client, cp, wakes = normal
    original = sf.get_run
    count, held = 0, []
    marker = MarkerError('controlled in-lock read')
    def fail_helper_read(run_id):
        nonlocal count
        if sys._getframe(1).f_code.co_name == 'approve_meta':
            count += 1
            if count == lookup:
                held.append(scheduler._get_tick_lock(pid).locked())
                raise marker
        return original(run_id)
    monkeypatch.setattr(sf, 'get_run', fail_helper_read)
    response = approve(normal)
    assert response.status_code == 409 and held == [True] and len(records) == 1
    assert records[0]['phase'] == phase
    assert records[0]['exceptions'][0]['frames'][-1]['function'] == 'fail_helper_read'
    raw = Path('/raw') if Path('/raw').is_dir() else tmp_path
    (raw/(tmp_path.name+'-read-phase.json')).write_text(json.dumps({
        'lookup': lookup, 'phase': phase, 'lock_held_at_marker': held,
        'status': response.status_code, 'records': records}, indent=2))
