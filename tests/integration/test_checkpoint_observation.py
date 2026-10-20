"""Current-path observation controls, not reproduction of the historical 500."""
import asyncio
import json
import logging
import threading
import traceback
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.testclient import TestClient

from api import meta_routers as mr
from core import checkpoint_observation as observation, meta_run
from tests.unit.test_normal_checkpoint_protocol import live, _body, _approval_count, PID


class MarkerError(RuntimeError):
    pass


@pytest.fixture
def observed(live, monkeypatch, tmp_path):
    sf, rid, original_client, tools = live
    rows, requests, exceptions, translated = [], [], [], []
    raw = Path('/raw') if Path('/raw').is_dir() else tmp_path
    path = raw / (tmp_path.name + '-observation.json')
    app = original_client._transport.app

    async def retain_http_error(request, exc):
        translated.append(exc)
        return await http_exception_handler(request, exc)
    app.add_exception_handler(HTTPException, retain_http_error)

    class ObservedASGI:
        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await app(scope, receive, send)
            row = {'path': scope['path'], 'thread': threading.get_ident(),
                   'loop': id(asyncio.get_running_loop())}
            requests.append(row)
            async def retain_send(message):
                if message['type'] == 'http.response.start':
                    row['status'] = message['status']
                await send(message)
            try:
                await app(scope, receive, retain_send)
            except BaseException as exc:
                exceptions.append(exc)
                row['exception_type'] = type(exc).__name__
                row['cause_type'] = type(exc.__cause__).__name__ if exc.__cause__ else None
                row['frames'] = [{'file': f.filename, 'line': f.lineno, 'function': f.name}
                                 for f in traceback.extract_tb(exc.__traceback__)]
                raise
            finally:
                path.write_text(json.dumps({'requests': requests, 'records': rows}, indent=2))

    class Retain(logging.Handler):
        def emit(self, record):
            row = record.checkpoint_observation
            assert json.loads(record.getMessage().split(' ', 1)[1]) == row
            assert 'SECRET' not in record.getMessage()
            rows.append(row)
            path.write_text(json.dumps({"requests": requests, "records": rows}, indent=2))

    handler = Retain()
    observation.logger.addHandler(handler)
    transport_app = ObservedASGI()
    # Bind the real ASGI callable at construction, before transport captures it.
    with TestClient(transport_app, raise_server_exceptions=False) as client:
        assert client._transport.app is transport_app
        yield sf, rid, client, rows, requests, exceptions, translated, path
    observation.logger.removeHandler(handler)
    assert observation._current.get() is None


def approval(observed, route='project', body=None):
    sf, rid, client, *_ = observed
    path = f'/api/meta/{PID}/checkpoint/approve' if route == 'project' else f'/api/runs/{rid}/checkpoint/approve'
    return client.post(path, json=body or _body(sf, rid))


def assert_transport(observed, status):
    sf, rid, client, rows, requests, exceptions, translated, path = observed
    assert requests and requests[-1]['status'] == status
    assert path.is_file() and path.stat().st_size > 0
    saved = json.loads(path.read_text())
    assert saved['requests'] == requests and saved['records'] == rows
    assert requests[-1]['thread'] == client.portal.call(threading.get_ident)
    assert requests[-1]['loop'] == id(client.portal.call(asyncio.get_running_loop))


@pytest.mark.parametrize('route,phase', [('project', 'run_lookup'), ('run', 'route_run_lookup')])
def test_original_request_exception_and_frames(observed, monkeypatch, route, phase):
    sf, rid, client, rows, requests, exceptions, translated, path = observed
    body = _body(sf, rid, 'marker-decision')
    marker, cause = MarkerError('SECRET payload'), ValueError('SECRET cause')
    def fail_lookup(run_id):
        raise marker from cause
    monkeypatch.setattr(sf, 'get_run', fail_lookup)
    response = approval(observed, route, body)
    assert response.status_code == 500 and response.text == 'Internal Server Error'
    assert exceptions == [marker] and exceptions[0] is marker and marker.__cause__ is cause
    assert_transport(observed, 500)
    assert requests[-1]['frames'][-1]['function'] == 'fail_lookup'
    assert requests[-1]['frames'][-1]['line'] > 0
    assert len(rows) == 1 and rows[0]['phase'] == phase
    assert rows[0]['request_id'] and rows[0]['run_id'] == rid
    assert rows[0]['decision_id'] == body['decision_id'] and not rows[0]['translated']
    assert rows[0]['thread_id'] != threading.get_ident()
    assert rows[0]['exceptions'][0]['type'].endswith('.MarkerError')
    assert rows[0]['exceptions'][1]['type'] == 'builtins.ValueError'
    assert rows[0]['exceptions'][0]['frames'][-1]['function'] == 'fail_lookup'
    assert 'SECRET' not in json.dumps(rows)


def test_controller_failure_preserves_existing_500(observed, monkeypatch):
    marker = MarkerError('controller')
    def fail_controller(*args):
        raise marker
    monkeypatch.setattr(mr, 'state_checkpoint_controller', fail_controller)
    response = approval(observed)
    assert response.status_code == 500 and observed[5] == [marker]
    assert len(observed[3]) == 1 and observed[3][0]['phase'] == 'state_controller'
    assert_transport(observed, 500)


@pytest.mark.parametrize('broken_sink', [False, True])
def test_sdk_translation_keeps_original_cause_and_response(observed, monkeypatch, broken_sink):
    sf, rid, client, rows, requests, exceptions, translated, path = observed
    marker, cause = MarkerError('controlled SDK refusal'), ValueError('original cause')
    def fail_approve(*args, **kwargs):
        raise marker from cause
    monkeypatch.setattr(sf, 'approve_checkpoint', fail_approve)
    if broken_sink:
        def fail_sink(*args, **kwargs):
            raise SystemExit('sink unavailable')
        monkeypatch.setattr(observation.logger, 'error', fail_sink)
    response = approval(observed)
    assert response.status_code == 409 and response.json() == {'detail': 'controlled SDK refusal'}
    assert len(translated) == 1 and translated[0].__cause__ is marker
    assert marker.__cause__ is cause and not exceptions
    assert sf.get_run(rid)['status'] == 'paused' and _approval_count(sf, rid) == 0
    assert_transport(observed, 409)
    if not broken_sink:
        assert len(rows) == 1 and rows[0]['translated'] and rows[0]['phase'] == 'sdk_approve'
        assert rows[0]['exceptions'][0]['frames'][-1]['function'] == 'fail_approve'
        assert rows[0]['exceptions'][1]['type'] == 'builtins.ValueError'


@pytest.mark.parametrize('route', ['project', 'run'])
def test_broken_sink_cannot_replace_request_error(observed, monkeypatch, route):
    sf, rid, client, rows, requests, exceptions, translated, path = observed
    body = _body(sf, rid)
    marker = MarkerError('SECRET')
    def fail_lookup(run_id):
        raise marker
    def fail_sink(*args, **kwargs):
        raise SystemExit('logging handler died')
    monkeypatch.setattr(sf, 'get_run', fail_lookup)
    monkeypatch.setattr(observation.logger, 'error', fail_sink)
    response = approval(observed, route, body)
    assert response.status_code == 500 and response.text == 'Internal Server Error'
    assert exceptions == [marker] and exceptions[0] is marker and not rows
    assert_transport(observed, 500)


def test_success_and_retained_decision_replay(observed):
    sf, rid, *_ = observed
    body = _body(sf, rid)
    assert approval(observed, 'project', body).status_code == 200
    assert approval(observed, 'run', body).status_code == 200
    assert sf.get_run(rid)['status'] == 'running' and _approval_count(sf, rid) == 1
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    from skillflow.core import StepResult
    sf.confirm_step(claim.token, StepResult(outputs={'done': True}))
    sf.advance_run(rid)
    assert sf.get_run(rid)['status'] == 'completed'
    assert approval(observed, 'run', body).status_code == 200
    assert _approval_count(sf, rid) == 1 and not observed[3]
    assert_transport(observed, 200)


def test_real_stale_refusal_keeps_new_checkpoint(observed):
    sf, rid, *_ = observed
    body = _body(sf, rid)
    sf.reject_checkpoint(rid, 'plan', 'revise')
    claim = sf.claim_next_step(rid)
    from skillflow.core import StepResult
    sf.confirm_step(claim.token, StepResult(outputs={'proposal': 'new revision'}))
    sf.advance_run(rid)
    current = sf.get_checkpoint(rid)
    response = approval(observed, 'run', body)
    assert response.status_code == 409 and 'Stale' in response.text
    assert sf.get_checkpoint(rid) == current and _approval_count(sf, rid) == 0
    assert observed[6][0].__cause__ is not None
    assert len(observed[3]) == 1 and observed[3][0]['translated']
    assert_transport(observed, 409)


@pytest.mark.parametrize('lookup,phase', [(1, 'approve_run_lookup'), (2, 'finalize_run_lookup')])
def test_current_standalone_helper_lookup_phases(observed, monkeypatch, lookup, phase):
    sf, rid, *_ = observed
    body = _body(sf, rid)
    original = sf.get_run
    count = 0
    marker = MarkerError('SECRET helper')
    def fail_helper_lookup(run_id):
        nonlocal count
        if sys._getframe(1).f_code is meta_run.approve_meta.__wrapped__.__code__:
            count += 1
            if count == lookup:
                raise marker
        return original(run_id)
    monkeypatch.setattr(sf, 'get_run', fail_helper_lookup)
    with pytest.raises(MarkerError) as caught:
        meta_run.approve_meta(sf, rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert caught.value is marker
    assert len(observed[3]) == 1 and observed[3][0]['phase'] == phase
    assert observed[3][0]['exceptions'][0]['frames'][-1]['function'] == 'fail_helper_lookup'
    assert observation._current.get() is None
    assert observed[7].is_file() and json.loads(observed[7].read_text())['records']


def test_current_helper_finalize_cause_and_sink_failure(observed, monkeypatch):
    sf, rid, *_ = observed
    body = _body(sf, rid)
    marker = MarkerError('controlled finalize refusal')
    def fail_advance(run_id):
        raise marker
    monkeypatch.setattr(sf, 'advance_run', fail_advance)
    with pytest.raises(RuntimeError) as caught:
        meta_run.approve_meta(sf, rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert caught.value.__cause__ is marker
    assert len(observed[3]) == 1 and observed[3][0]['phase'] == 'advance_finalize'
    assert observed[3][0]['exceptions'][1]['frames'][-1]['function'] == 'fail_advance'
    assert _approval_count(sf, rid) == 1
    def fail_sink(*args, **kwargs):
        raise SystemExit('sink died')
    monkeypatch.setattr(observation.logger, 'error', fail_sink)
    with pytest.raises(RuntimeError) as retry:
        meta_run.approve_meta(sf, rid, expected_checkpoint=body['expected_checkpoint'], decision_id=body['decision_id'])
    assert retry.value.__cause__ is marker and _approval_count(sf, rid) == 1


def test_nested_scope_and_context_reset():
    marker = MarkerError('not emitted as text')
    seen = []
    @observation.observe_checkpoint
    def inner():
        seen.append(observation._current.get()['request_id'])
        raise marker
    @observation.observe_checkpoint
    def outer():
        seen.append(observation._current.get()['request_id'])
        inner()
    with pytest.raises(MarkerError) as caught:
        outer()
    assert caught.value is marker and len(set(seen)) == 1
    assert observation._current.get() is None


def test_old_decision_replay_does_not_answer_later_pause(observed):
    from skillflow.graph import PipelineGraph, StepNode, Transition
    from skillflow.core import StepResult
    sf, _, client, rows, *_ = observed
    graph = PipelineGraph(name='observer_two_pauses', begin='first', steps=[
        StepNode(id='first', step_type='agent', checkpoint=True, transitions=[Transition(to='second')]),
        StepNode(id='second', step_type='agent', checkpoint=True, transitions=[Transition(to='done')]),
        StepNode(id='done', step_type='agent', transitions=[])])
    sf.register_graph(graph)
    rid = sf.create_run(graph.name, project_id=PID)
    sf.start_run(rid)
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'proposal': 'first'}))
    sf.advance_run(rid)
    body = _body(sf, rid, 'retained-first')
    path = f'/api/runs/{rid}/checkpoint/approve'
    assert client.post(path, json=body).status_code == 200
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'proposal': 'second'}))
    sf.advance_run(rid)
    later = sf.get_checkpoint(rid)
    assert later['step_id'] == 'second'
    assert client.post(path, json=body).status_code == 200
    assert sf.get_checkpoint(rid) == later and _approval_count(sf, rid) == 1
    assert not rows
    assert_transport(observed, 200)


def test_request_local_identity_is_not_shared_between_threads():
    from concurrent.futures import ThreadPoolExecutor
    @observation.observe_checkpoint
    def identity(value):
        row = observation._current.get()
        observation.checkpoint_phase('controlled-thread', decision_id=value)
        return row['request_id'], row['decision_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(identity, ['one', 'two']))
    assert values[0][0] != values[1][0]
    assert [v[1] for v in values] == ['one', 'two']
    assert observation._current.get() is None
