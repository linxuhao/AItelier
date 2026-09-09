"""Offline request contracts for OpenCode Go conversation routing."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from core.ai_router import AIGateway


@pytest.fixture
def gateway(tmp_path):
    config = tmp_path / 'providers.json'
    config.write_text(json.dumps({'opencodego': {'base_url': 'https://opencode.ai/zen/go/v1'},
                                  'other': {'base_url': 'https://example.test/v1'}}))
    routes = tmp_path / 'routes.json'
    routes.write_text('{}')
    return lambda: AIGateway('opencodego/omen-alpha', config_path=str(config), routes_path=str(routes))


def test_native_json_share_session_and_instances_are_distinct(gateway):
    g = gateway()
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{}', tool_calls=[]), finish_reason='stop')])
    g._complete_prebuilt = Mock(return_value=response)
    g.generate('system', 'json please', is_json_mode=True)
    g.generate_native([{'role': 'user', 'content': 'hello'}], tools=[{'type': 'function'}])
    first, second = [c.args[0] for c in g._complete_prebuilt.call_args_list]
    assert first['response_format'] == {'type': 'json_object'}
    assert 'response_format' not in second and second['tools']
    assert first['extra_headers'] == second['extra_headers']
    assert first['extra_headers']['User-Agent'].startswith('AItelier/')
    session = first['extra_headers']['x-opencode-session']
    assert session and session != gateway()._build_kwargs([])['extra_headers']['x-opencode-session']


def test_rebinding_scopes_managed_headers_and_preserves_callers(gateway):
    g = gateway()
    caller = {'User-Agent': 'caller/2', 'X-Correlation-ID': 'test'}
    kwargs = g._build_kwargs([], extra_headers=caller)
    session = kwargs['extra_headers']['x-opencode-session']
    assert 'AItelier/' in kwargs['extra_headers']['User-Agent']
    assert 'caller/2' in kwargs['extra_headers']['User-Agent']
    assert caller == {'User-Agent': 'caller/2', 'X-Correlation-ID': 'test'}
    original = kwargs['extra_headers'].copy()
    g._apply_binding(kwargs)
    assert kwargs['extra_headers'] == original
    g._bind('other/model')
    g._apply_binding(kwargs)
    assert kwargs['extra_headers'] == caller
    assert 'extra_headers' not in g._build_kwargs([])
    g._bind('opencodego/omen-alpha')
    g._apply_binding(kwargs)
    assert kwargs['extra_headers']['x-opencode-session'] == session
    assert kwargs['extra_headers']['X-Correlation-ID'] == 'test'
