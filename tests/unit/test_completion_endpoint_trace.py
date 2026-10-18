"""Endpoint observations through real gateway, host factory and durable trace."""
import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest
from tenacity import wait_none

from aitelier.runner import AgentStepRunner
from core import ai_router, model_routes
from core.ai_router import AIGateway
from core.dpe_pipeline import PipelineEngine
from core.trace_reader import trace_list, trace_read
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode


@pytest.fixture
def case(tmp_path, monkeypatch, request):
    monkeypatch.setenv('AITELIER_LLM_STREAM', '0')
    monkeypatch.setenv('OWN_ENDPOINT_KEY', 'owned-offline-key')
    model_routes.reset_cache()
    ai_router.reset_endpoint_cooldowns()
    providers = tmp_path / 'providers.json'
    providers.write_text(json.dumps({p: {'base_url': 'https://offline.invalid/secret-url',
        'api_key_env': 'OWN_ENDPOINT_KEY'} for p in ('one', 'two')}))
    routes = tmp_path / 'routes.json'
    routes.write_text(json.dumps({'owned-pool': {'rotate': ['one/model'], 'fallback': ['two/model']}}))
    sf = SkillFlow(str(tmp_path / 'engine.db'), artifact_history=False)
    sf.register_graph(PipelineGraph(name='owned-timing', begin='work',
        steps=[StepNode(id='work', step_type='agent')]))
    sf.register_agent_config_from_dict('maker', {'model': 'owned-pool', 'config': {'system_prompt': 'owned'}})
    rid = sf.create_run('owned-timing', project_id='owned'); sf.start_run(rid); sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    gateway = AIGateway('owned-pool', config_path=str(providers), routes_path=str(routes))
    monkeypatch.setattr('core.agents.AIGateway', lambda **kw: gateway)
    monkeypatch.setattr(AIGateway.generate_native.retry, 'wait', wait_none())
    monkeypatch.setattr(AIGateway.generate.retry, 'wait', wait_none())
    engine = PipelineEngine(registry=sf.agent_registry,
        trace_callback=AgentStepRunner._make_trace_wrapper(claim))
    native = engine.factory.get_native_agent('maker')
    def rows():
        return [r for r in sf.get_trace(rid) if r['event'].startswith('completion_')]
    c = SimpleNamespace(g=gateway, native=native, engine=engine, sf=sf, rid=rid, claim=claim, rows=rows)
    yield c
    root = os.environ.get('REVIEW_RAW')
    if root:
        Path(root, request.node.name + '.json').write_text(json.dumps({
            'trace': sf.get_trace(rid), 'consumer': trace_list(sf, rid),
            'errors': trace_list(sf, rid, errors_only=True),
            'gateway': {'active_model': gateway.active_model, 'last_usage': gateway.last_usage,
                        'failovers': gateway._failovers}}, indent=2, default=str) + '\n')
    sf._conn.close(); model_routes.reset_cache(); ai_router.reset_endpoint_cooldowns()


def reply(content='useful answer', tools=None):
    return litellm.ModelResponse(model='offline', choices=[{'index': 0, 'finish_reason': 'stop',
        'message': {'role': 'assistant', 'content': content, 'tool_calls': tools}}],
        usage={'prompt_tokens': 19, 'completion_tokens': 2 if content or tools else 0,
               'total_tokens': 21 if content or tools else 19})


def observed(c, outcomes):
    rows = c.rows()
    assert [r['event'] for r in rows] == ['completion_started', 'completion_finished'] * len(outcomes)
    ids = []
    for start, end, outcome in zip(rows[::2], rows[1::2], outcomes):
        a, b = start['payload'], end['payload']
        assert a['invocation_id'] == b['invocation_id']
        ids.append(a['invocation_id'])
        assert a['outcome'] == 'started' and b['outcome'] == outcome
        assert a['served_by'] == b['served_by'] and b['model_route'] == 'owned-pool'
        assert b['elapsed_s'] >= 0 and a['wall_cap_s'] == c.g._WALL_CAP_S
        assert end['step_instance_id'] == c.claim.token.step_instance_id
        assert end['step_id'] == 'work'
    assert len(ids) == len(set(ids))
    raw = json.dumps(rows)
    assert 'offline.invalid' not in raw and 'owned-offline-key' not in raw and 'private-prompt' not in raw
    consumer = trace_list(c.sf, c.rid, category='llm', order='asc')
    assert consumer['count'] == len(rows)
    assert all('one/model' in r['summary'] or 'two/model' in r['summary'] for r in consumer['entries'])
    # Full existing consumer must return the exact persisted payload.
    full = trace_read(c.sf, c.rid, rows[-1]['seq'])
    assert full['entries'][0]['payload'] == rows[-1]['payload']
    return rows


def test_wall_timeout_is_attributed_before_failover_and_late_return(case, monkeypatch):
    c = case; release = threading.Event(); started = threading.Event(); returned = threading.Event(); calls = []
    c.g._WALL_CAP_S = 0.04
    def complete(**kw):
        calls.append(kw['model'])
        if len(calls) == 1:
            started.set(); release.wait(2); returned.set()
        return reply()
    monkeypatch.setattr(litellm, 'completion', complete)
    try:
        assert c.native.turn([{'role': 'user', 'content': 'private-prompt'}]).text == 'useful answer'
        assert started.is_set() and not returned.is_set()
        rows = observed(c, ['timeout', 'completed'])
        assert rows[1]['payload']['error_type'] == 'Timeout'
        assert rows[1]['payload']['served_by'] == 'one/model'
        assert rows[3]['payload']['served_by'] == 'two/model'
        assert 0.02 <= rows[1]['payload']['elapsed_s'] < 1
        assert 'timeout' in trace_list(c.sf, c.rid, errors_only=True)['entries'][0]['summary']
        release.set(); assert returned.wait(1)
        assert c.rows() == rows, 'abandoned worker must not append a second terminal result'
        assert c.native.turn([{'role': 'user', 'content': 'private-prompt'}]).text == 'useful answer'
        observed(c, ['timeout', 'completed', 'completed'])
        assert len(calls) == 3 and c.g.active_model == 'two/model'
    finally:
        release.set()


def test_empty_native_exhaustion_records_each_actual_endpoint(case, monkeypatch):
    c = case; calls = []
    monkeypatch.setattr(litellm, 'completion', lambda **kw: calls.append(kw['model']) or reply(''))
    with pytest.raises(ai_router.EmptyProviderResponse):
        c.native.turn([{'role': 'user', 'content': 'private-prompt'}])
    rows = observed(c, ['error', 'error'])
    assert calls == ['openai/model', 'openai/model']
    assert [r['payload']['error_type'] for r in rows[1::2]] == ['EmptyProviderResponse'] * 2
    assert trace_list(c.sf, c.rid, errors_only=True)['count'] == 2
    assert ai_router.endpoint_cooldowns() == {}


@pytest.mark.parametrize('streaming', [False, True])
def test_useful_text_tools_usage_and_real_stream_builder_preserved(case, monkeypatch, streaming):
    c = case
    tools = [{'id': 'owned-call', 'type': 'function', 'function': {'name': 'write_findings', 'arguments': '{"content":"useful"}'}}]
    expected = reply('useful answer', tools)
    calls = []
    def complete(**kw):
        calls.append(kw)
        if streaming:
            return iter([litellm.ModelResponse(stream=True, model='offline', choices=[{'index': 0,
                'delta': {'role': 'assistant', 'content': 'useful answer', 'tool_calls': [
                    {'index': 0, **tools[0]}]}, 'finish_reason': 'tool_calls'}],
                usage={'prompt_tokens': 19, 'completion_tokens': 2, 'total_tokens': 21})])
        return expected
    monkeypatch.setenv('AITELIER_LLM_STREAM', '1' if streaming else '0')
    monkeypatch.setattr(litellm, 'completion', complete)
    turn = c.native.turn([{'role': 'user', 'content': 'private-prompt'}], tools=[{'type': 'function', 'function': {'name': 'write_findings', 'parameters': {'type': 'object'}}}])
    assert turn.text == 'useful answer' and turn.tool_calls == tools
    assert c.g.last_usage['completion_tokens'] == 2 and c.g.last_usage['served_by'] == 'one/model'
    assert len(calls) == 1 and c.g.active_model == 'one/model'
    observed(c, ['completed'])


@pytest.mark.parametrize('cancel', [False, True])
def test_original_request_error_or_cancel_escapes_unchanged(case, monkeypatch, cancel):
    c = case
    error = asyncio.CancelledError('private-error-body') if cancel else ValueError('private-error-body')
    def fail(**kw): raise error
    monkeypatch.setattr(litellm, 'completion', fail)
    with pytest.raises(type(error)) as caught:
        c.native.turn([{'role': 'user', 'content': 'private-prompt'}])
    assert caught.value is error and c.g.active_model == 'one/model'
    # Preserve the existing one-shot thinking-off parameter correction.
    rows = observed(c, ['error'] if cancel else ['error', 'error'])
    assert all(r['payload']['error_type'] == type(error).__name__ for r in rows[1::2])
    assert c.g._suppress_thinking is cancel
    assert 'private-error-body' not in json.dumps(rows)


def test_json_response_and_broken_observer_do_not_change_behavior(case, monkeypatch):
    c = case; calls = []
    monkeypatch.setattr(litellm, 'completion', lambda **kw: calls.append(kw) or reply('{"ok":true}'))
    agent = c.engine.factory.get_agent('maker')
    assert agent.run('private-prompt') == '{"ok":true}'
    observed(c, ['completed'])
    def broken(*args): raise RuntimeError('owned observer failure')
    c.g.on_completion = broken
    assert agent.run('private-prompt') == '{"ok":true}'
    assert len(calls) == 2 and all(kw['response_format'] == {'type': 'json_object'} for kw in calls)
    assert len(c.rows()) == 2
    monkeypatch.setattr(litellm, 'completion', lambda **kw: reply(''))
    with pytest.raises(ai_router.EmptyProviderResponse):
        c.native.turn([{'role': 'user', 'content': 'private-prompt'}])
    assert c.g.active_model == 'two/model' and len(c.rows()) == 2
