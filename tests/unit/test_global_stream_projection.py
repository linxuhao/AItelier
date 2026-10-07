"""Private event bodies are projected separately for every global subscriber."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from api import main, authz
from api.sse_manager import StreamManager, public_global_message
from core import cf_access


@pytest.fixture
def identities(monkeypatch):
    monkeypatch.setattr(authz, 'gate_enabled', lambda: True)
    monkeypatch.setattr(authz, 'WRITERS', {'writer@fixture'})
    monkeypatch.setattr(authz, 'ADMIN_TOKEN', 'fixture-admin')
    monkeypatch.setattr(cf_access, '_ISSUER', 'https://owned.fixture')
    monkeypatch.setattr(cf_access, '_AUD', 'owned-fixture-audience')
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class LocalKey:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=key.public_key())
    monkeypatch.setattr(cf_access, '_jwk_client', LocalKey())
    def token(email, **extra):
        claims = {'email': email, 'iss': cf_access._ISSUER,
                  'aud': cf_access._AUD, 'exp': int(time.time()) + 300}
        claims.update(extra)
        return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'owned'})
    writer = token('writer@fixture')
    denied = [({}, None), ({'X-AItelier-Admin-Token': 'wrong'}, None),
              ({'Cf-Access-Jwt-Assertion': 'invalid'}, None),
              ({'Cf-Access-Jwt-Assertion': token('reader@fixture')}, 'reader@fixture'),
              ({'Cf-Access-Jwt-Assertion': token('writer@fixture', exp=1)}, None),
              ({'Cf-Access-Jwt-Assertion': token('writer@fixture', aud='wrong')}, None),
              ({'Cf-Ray': 'owned', 'X-AItelier-Admin-Token': 'fixture-admin'}, None),
              ({'Cf-Access-Authenticated-User-Email': 'writer@fixture'}, None)]
    allowed = [{'Cf-Access-Jwt-Assertion': writer},
               {'X-AItelier-Admin-Token': 'fixture-admin'},
               {'Cf-Ray': 'owned', 'Cf-Access-Jwt-Assertion': writer}]
    return denied, allowed


def request(headers):
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/events/stream',
                    'headers': [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                    'app': main.app})


async def next_kind(stream, kind):
    async with asyncio.timeout(2):
        async for raw in stream:
            event = json.loads(json.loads(raw.removeprefix('data: '))['log'])
            if event['type'] == kind:
                return event
    raise AssertionError('stream ended before event')


@pytest.mark.asyncio
async def test_cached_normal_route_denies_eight_identities_keeps_authorized_raw(identities, monkeypatch):
    denied, allowed = identities
    marker = 'PRIVATE_RAW_ERROR_BODY'
    message = json.dumps({'type': 'step_failed', 'project_id': 'p1', 'step_id': 'impl',
                          'error': marker, 'source': marker, 'context': {'text': marker},
                          'output': marker, '_ts': 42})
    for headers, who in denied:
        manager = StreamManager()
        monkeypatch.setattr(main, 'stream_manager', manager)
        await manager.push_log('__global__', message)
        response = await main.stream_global_events(request(headers))
        event = await next_kind(response.body_iterator, 'step_failed')
        assert event == {'type': 'step_failed', 'project_id': 'p1', 'step_id': 'impl', '_ts': 42}
        assert manager.connection_snapshot()[0]['who'] == who
        await response.body_iterator.aclose()
        assert manager.connection_snapshot() == []
    for headers in allowed:
        manager = StreamManager()
        monkeypatch.setattr(main, 'stream_manager', manager)
        await manager.push_log('__global__', message)
        response = await main.stream_global_events(request(headers))
        event = await next_kind(response.body_iterator, 'step_failed')
        assert event['error'] == marker and event['context']['text'] == marker
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_same_live_event_raw_operator_and_public_nonwriter_are_independent(identities, monkeypatch):
    denied, allowed = identities
    manager = StreamManager()
    monkeypatch.setattr(main, 'stream_manager', manager)
    operator = await main.stream_global_events(request(allowed[0]))
    public = await main.stream_global_events(request(denied[3][0]))
    await next_kind(operator.body_iterator, 'presence')
    await next_kind(public.body_iterator, 'presence')
    marker = 'LIVE_PRIVATE_CONTEXT'
    body = {'type': 'llm_progress', 'project_id': 'p1', 'phase': 'llm', 'chars': 51,
            'elapsed': 1.5, 'error': marker, 'message': marker,
            'served_by': {'source': marker}, 'context': [marker]}
    original = json.dumps(body)
    await manager.push_log('__global__', original)
    visible, raw = await asyncio.gather(next_kind(public.body_iterator, 'llm_progress'),
                                       next_kind(operator.body_iterator, 'llm_progress'))
    assert visible == {'type': 'llm_progress', 'project_id': 'p1', 'phase': 'llm',
                       'chars': 51, 'elapsed': 1.5}
    assert raw == body and json.dumps(body) == original
    assert manager.presence_counts() == {'total': 2, 'authenticated': 2, 'anonymous': 0}
    await public.body_iterator.aclose()
    await operator.body_iterator.aclose()
    assert manager._connection_count() == 0


@pytest.mark.parametrize('kind', ['step_failed', 'future_private_event', 'PRIVATE_UNKNOWN_TYPE'])
def test_known_and_future_bodies_do_not_gain_public_nested_or_unknown_fields(kind):
    marker = 'SECRET_SOURCE_CONTENT'
    body = {'type': kind, 'project_id': {'source': marker}, 'status': {'text': marker},
            'chars': marker, '_task_id': [marker], 'message': marker,
            'checkpoint': {'approved': marker}, 'unknown': marker, 'reason': marker,
            'content': marker, 'files': [marker], 'raw': marker, 'output': marker,
            'context': {'error': marker}, 'phase': marker, 'error': marker}
    projected = public_global_message(json.dumps(body))
    assert marker not in projected and 'PRIVATE_UNKNOWN_TYPE' not in projected
    assert json.loads(projected) == {'type': kind if kind == 'step_failed' else 'execution_progress'}


@pytest.mark.parametrize('body', ['private raw line', '"private json string"', '[]', 'null'])
def test_untyped_raw_global_lines_are_private_by_default(body):
    assert public_global_message(body) is None


@pytest.mark.asyncio
async def test_capacity_refuses_before_identity_and_auth_work_stays_off_loop(monkeypatch):
    manager = StreamManager()
    monkeypatch.setattr(main, 'stream_manager', manager)
    loop_thread = threading.get_ident()
    seen = []
    def identity(req):
        seen.append(threading.get_ident())
        return True
    monkeypatch.setattr(authz, 'may_read_private', identity)
    monkeypatch.setattr(manager, 'at_capacity', lambda: True)
    response = await main.stream_global_events(request({}))
    assert [raw async for raw in response.body_iterator] == [': at capacity\n\n']
    assert seen == []
    monkeypatch.setattr(manager, 'at_capacity', lambda: False)
    response = await main.stream_global_events(request({}))
    assert len(seen) == 1 and seen[0] != loop_thread
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_unresolved_identity_keeps_public_projection(monkeypatch):
    manager = StreamManager()
    monkeypatch.setattr(main, 'stream_manager', manager)
    def unavailable(req):
        raise RuntimeError('identity unavailable')
    monkeypatch.setattr(authz, 'may_read_private', unavailable)
    await manager.push_log('__global__', json.dumps({'type': 'step_failed', 'error': 'PRIVATE'}))
    response = await main.stream_global_events(request({}))
    assert await next_kind(response.body_iterator, 'step_failed') == {'type': 'step_failed'}
    await response.body_iterator.aclose()
