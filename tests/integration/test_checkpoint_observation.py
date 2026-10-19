"""Controlled observer conformance, never a historical failure reproduction."""
import json
import asyncio
import traceback
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
    original_app = client.app
    raw = Path('/raw') if Path('/raw').is_dir() else tmp_path
    failures = []
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
                failures.append({'status': status, 'type': type(exc).__name__,
                    'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name}
                               for f in traceback.extract_tb(exc.__traceback__)],
                    'thread': threading.get_ident(), 'loop': id(asyncio.get_running_loop())})
                (raw/(tmp_path.name+'-asgi.json')).write_text(json.dumps(failures, indent=2))
                raise
    monkeypatch.setattr(client, 'app', ObservedASGI())
    topology = {'sdk_db_path': sf._db_path, 'trace_db_path': sf._trace_db_path,
                'trace_uses_engine_connection': sf._trace_db_path is None,
                'engine_connection_id': id(sf._conn), 'engine_lock_type': type(sf._lock).__name__,
                'shared_testclient_portal': client.portal is not None}
    (raw/(tmp_path.name+'-topology.json')).write_text(json.dumps(topology, indent=2))
    monkeypatch.setattr(observation.logger, 'error',
                        lambda message, **kw: rows.append(kw['extra']['checkpoint_observation']))
    return rows


@pytest.mark.parametrize('route,phase', [('run', 'route_run_lookup'), ('project', 'run_lookup')])
def test_original_untranslated_rest_failure(normal, monkeypatch, records, route, phase):
    sf, db, ws, pid, rid, root, client, cp, wakes = normal
    marker = MarkerError('SECRET must not appear in diagnostics')
    def fail_lookup(value):
        raise marker
    monkeypatch.setattr(sf, 'get_run', fail_lookup)
    path = f'/api/runs/{rid}/checkpoint/approve' if route == 'run' else f'/api/meta/{pid}/checkpoint/approve'
    reply = client.post(path, json={'run_id': rid, 'checkpoint': 'gather',
        'expected_checkpoint': cp['expected_checkpoint'], 'decision_id': 'observed-marker'})
    assert reply.status_code == 500 and reply.text == 'Internal Server Error'
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
    assert any(f['function'] == 'execute' for e in records[0]['exceptions'] for f in e['frames'])


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
