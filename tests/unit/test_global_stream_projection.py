"""Private event bodies are projected separately for every global subscriber.

This file measures the REAL per-subscriber privacy decision
(`api/sse_manager.py:public_global_message` at consumption time, gated by
`api/authz.py:may_read_private`). Two suite-level conveniences used to make
that decision untestable here:

- `api/authz.py:292` returns True for EVERY request while
  `app.state._test_mode` is set — before the gate, the credential or the
  writer allowlist is looked at. `tests/conftest.py:120` sets that flag for the
  TestClient fixture and other suites set it at module level, so whether this
  file measured the gate at all depended on suite state. With it on, every
  subscriber is a private reader: the public projection is never the taken
  branch, so a redaction assertion cannot tell a working projection from a
  passthrough. 9000 other tests rely on the shortcut, so this file turns it off
  for its own requests (`no_test_mode_shortcut`) instead of changing conftest.
- `core/cf_access.py:71` (`is_configured`) needs `_TEAM_DOMAIN` *and* `_AUD`.
  The fixture used to patch only `_ISSUER`/`_AUD`, so `verify()` returned None
  for every token, `api/auth.py:creator_email` returned None, no identity ever
  resolved and the `who` assertion was vacuous by construction — and the
  authorized half of the file only ever reached the private branch through the
  off-tunnel admin token.
"""
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


@pytest.fixture(autouse=True)
def no_test_mode_shortcut(monkeypatch):
    """Take every request in this file out of `_test_mode`.

    `api/authz.py:292` — `may_read_private()` returns True on the flag ALONE,
    before the gate or any credential is looked at. Left on, all eight denied
    identities would be private readers: the public projection is never the
    taken branch, so the file would assert nothing about the branch it exists
    to pin (and would fail for a product that redacts correctly).
    """
    monkeypatch.setattr(main.app.state, '_test_mode', False, raising=False)
    assert getattr(main.app.state, '_test_mode', None) is False
    yield


@pytest.fixture
def identities(monkeypatch):
    monkeypatch.setattr(authz, 'gate_enabled', lambda: True)
    monkeypatch.setattr(authz, 'WRITERS', {'writer@fixture'})
    monkeypatch.setattr(authz, 'ADMIN_TOKEN', 'fixture-admin')
    # All THREE of the values `core/cf_access.is_configured()` reads: the team
    # domain is what `_ISSUER`/`_CERTS_URL` are derived from at import time, and
    # without it verify() refuses every token before it ever looks at the `kid`.
    monkeypatch.setattr(cf_access, '_TEAM_DOMAIN', 'owned.fixture')
    monkeypatch.setattr(cf_access, '_ISSUER', 'https://owned.fixture')
    monkeypatch.setattr(cf_access, '_CERTS_URL', 'https://owned.fixture/cdn-cgi/access/certs')
    monkeypatch.setattr(cf_access, '_AUD', 'owned-fixture-audience')
    # The negative `kid` cache is process-global; another test's unresolvable
    # kid must not be able to turn a valid identity here into anonymous.
    monkeypatch.setattr(cf_access, '_bad_kids', {})
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class LocalKey:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=key.public_key())
    # Local on purpose: a real PyJWKClient would try to fetch the JWKS, and the
    # suite runs with no network.
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
    # (headers, who). `who` is presence attribution, NOT read authority: the
    # off-tunnel admin token is anonymous-looking yet may read private bodies.
    allowed = [({'Cf-Access-Jwt-Assertion': writer}, 'writer@fixture'),
               ({'X-AItelier-Admin-Token': 'fixture-admin'}, None),
               ({'Cf-Ray': 'owned', 'Cf-Access-Jwt-Assertion': writer}, 'writer@fixture')]
    return denied, allowed


def request(headers):
    # Every request this file builds must be judged by the real gate, never by
    # the suite-wide `_test_mode` bypass (api/authz.py:292).
    assert getattr(main.app.state, '_test_mode', False) is False
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
    for headers, who in allowed:
        manager = StreamManager()
        monkeypatch.setattr(main, 'stream_manager', manager)
        await manager.push_log('__global__', message)
        response = await main.stream_global_events(request(headers))
        event = await next_kind(response.body_iterator, 'step_failed')
        assert event['error'] == marker and event['context']['text'] == marker
        # The writer/admin path keeps its private bytes AND the per-connection
        # `who` is the RESOLVED identity — the admin token resolves to nobody
        # (it is a shared off-tunnel credential), the Access JWT to its email.
        assert manager.connection_snapshot()[0]['who'] == who
        await response.body_iterator.aclose()
        assert manager.connection_snapshot() == []


@pytest.mark.asyncio
async def test_same_live_event_raw_operator_and_public_nonwriter_are_independent(identities, monkeypatch):
    denied, allowed = identities
    manager = StreamManager()
    monkeypatch.setattr(main, 'stream_manager', manager)
    operator = await main.stream_global_events(request(allowed[0][0]))
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
