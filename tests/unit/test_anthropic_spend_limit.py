"""Observed Anthropic HTTP 400 spend-limit refusal. AUTHOR UNRUN.

Retained incident (synthetic ids here): LiteLLM raised BadRequestError /
AnthropicException `invalid_request_error` — "You have reached your specified
API usage limits. You will regain access on 2026-11-01 at 00:00 UTC." Neither the
quota classifier nor the 429 window classifier saw it, so the 400 was treated as
request-shaped: no endpoint walk. Root's measured controlled baseline on frozen
8258: native callers made 3 native requests plus 1 JSON request (replayed as
format/no-output repair); JSON-only content/tool-content callers made 1 request
(the error already escaped) but without an endpoint walk or `provider_error`
trace. The retained full c784 incident (16 failed requests, zero useful output)
is a distinct, historical scope and is not reproduced here.

Self-contained. Only `litellm.completion` is scripted; the gateway retry/walk, the
DPE native/content/tool-content loops and the SDK fixed-slot writer are real.

Expected on frozen 8258: the classifier, hold, `spend_limit_walks`, fallback-park
and artifact-dispatch tests FAIL (is_quota_exhausted False, reset None, only one
endpoint tried, replay as repair). The control tests (`wording_without_*`,
`ordinary_anthropic_*`, `request_shaped_*`, `burst_*`, `unsupported_*`) PASS both
before and after.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import litellm
import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode
from skillflow.tool_loader import ToolLoader
from tenacity import wait_none

from core import ai_router, model_routes
from core.agents import AgentFactory
from core.ai_router import AIGateway
from core.dpe_pipeline import PipelineEngine
from core.llm_quota import is_quota_exhausted, quota_reset_at
from core.workspace_manager import WorkspaceManager

OBSERVED = ('AnthropicException - {"type":"error","error":{"type":"invalid_request_error",'
            '"message":"You have reached your specified API usage limits. You will regain '
            'access on 2026-11-01 at 00:00 UTC."},"request_id":"req_owned_fixture"}')
UNSUPPORTED = 'This endpoint does not support tools in native requests.'
USEFUL = '# Owned report\n\nA useful successor produced this artifact.\n'
ENDPOINTS = ['ownedark/first', 'ownedark/first', 'ownednext/second', 'ownedark/first']
WIRE_MODELS = ['openai/first', 'openai/second']


def spend_text(reset=None):
    """The observed sentence, with a genuinely stated future UTC time on demand."""
    if reset is None:
        return OBSERVED
    return OBSERVED.replace('2026-11-01 at 00:00', reset.strftime('%Y-%m-%d at %H:%M'))


def refusal(message=OBSERVED, *, model='first', provider='anthropic'):
    return litellm.BadRequestError(
        message=message, model=model, llm_provider=provider,
        response=httpx.Response(400, request=httpx.Request('POST', 'https://offline.invalid/v1')))


def soon(hours=2):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def response(content='useful answer', *, tools=None, completion=2):
    return litellm.ModelResponse(model='owned-offline', choices=[{
        'index': 0, 'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': content, 'tool_calls': tools}}],
        usage={'prompt_tokens': 19, 'completion_tokens': completion, 'total_tokens': 19 + completion})


# ── pure classifier ──────────────────────────────────────────────────────────

def test_observed_400_is_a_spent_window_with_stated_utc_recovery():
    err = refusal()
    assert err.status_code == 400
    assert is_quota_exhausted(err)
    assert quota_reset_at(err) == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize('stated', [
    'You will regain access soon.', 'You will regain access on 2026-13-45 at 99:00 UTC.',
    'You will regain access on 2026-11-01 at 00:00 PST.', ''])
def test_missing_or_malformed_recovery_time_is_none_not_invented(stated):
    err = refusal('You have reached your specified API usage limits. ' + stated)
    assert is_quota_exhausted(err)
    assert quota_reset_at(err) is None


@pytest.mark.parametrize('case', ['string', 'other-provider', 'other-status', 'plain-exception'])
def test_wording_without_exception_provenance_is_not_endpoint_exhaustion(case):
    if case == 'string':
        err = OBSERVED
    elif case == 'other-provider':
        err = refusal(provider='openai')
    elif case == 'other-status':
        err = litellm.RateLimitError(message=OBSERVED, model='first', llm_provider='anthropic',
            response=httpx.Response(429, request=httpx.Request('POST', 'https://offline.invalid/v1')))
    else:
        err = ValueError(OBSERVED)
    assert not is_quota_exhausted(err)


@pytest.mark.parametrize('message', [
    UNSUPPORTED, 'Invalid parameter: temperature',
    'prompt is too long: 300000 tokens > 200000 maximum',
    'You have reached your usage limits.', 'Rate limit reached for requests per min.'])
def test_ordinary_anthropic_400_text_is_not_exhaustion(message):
    assert not is_quota_exhausted(refusal(message))


def test_scheduler_hold_uses_the_stated_recovery_and_never_invents_one(monkeypatch):
    import core.scheduler as sched
    sched._QUOTA_HOLD_UNTIL = 0.0
    now = datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(sched._time, 'time', lambda: now)
    try:
        sched._note_quota_exhausted(refusal())
        assert sched._quota_hold_remaining() == pytest.approx(3600 + sched._QUOTA_HOLD_GRACE, abs=2)
        sched._QUOTA_HOLD_UNTIL = 0.0
        sched._note_quota_exhausted(refusal('You have reached your specified API usage limits.'))
        assert sched._quota_hold_remaining() == pytest.approx(sched._QUOTA_HOLD_FALLBACK, abs=2)
    finally:
        sched._QUOTA_HOLD_UNTIL = 0.0
        sched._QUOTA_HOLD_REASON = ''


# ── real gateway ─────────────────────────────────────────────────────────────

@pytest.fixture
def wiring(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('AITELIER_SECRETS_DIR', str(tmp_path/'empty-secrets'))
    monkeypatch.setenv('AITELIER_LLM_STREAM', '0')
    monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
    monkeypatch.delenv('AITELIER_FORCE_ALL_AGENT_MODELS', raising=False)
    providers = tmp_path/'providers.json'
    providers.write_text(json.dumps({name: {'base_url': 'https://offline.invalid/v1'}
                                     for name in ('ownedark', 'ownednext')}))
    routes = tmp_path/'routes.json'
    routes.write_text(json.dumps({'owned-pool': ENDPOINTS}))
    model_routes.reset_cache()
    ai_router.reset_endpoint_cooldowns()
    monkeypatch.setattr(AIGateway.generate.retry, 'wait', wait_none())
    monkeypatch.setattr(AIGateway.generate_native.retry, 'wait', wait_none())

    def gateway():
        return AIGateway('owned-pool', config_path=str(providers),
                         routes_path=str(routes), enable_thinking=True)
    yield gateway
    model_routes.reset_cache()
    ai_router.reset_endpoint_cooldowns()


def invoke(gateway, mode):
    if mode == 'native':
        return gateway.generate_native([{'role': 'user', 'content': 'owned request'}]).text
    return gateway.generate('owned system', 'owned request', is_json_mode=True)


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('exhausted', [False, True], ids=['successor', 'exhausted'])
def test_spend_limit_walks_declared_endpoints_and_preserves_error(wiring, monkeypatch, mode, exhausted):
    gateway = wiring()
    message = spend_text(soon())
    calls, errors = [], []

    def complete(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == WIRE_MODELS[0] or exhausted:
            error = refusal(message, model=kwargs['model'])
            errors.append(error)
            raise error
        return response('{"useful":true}')
    monkeypatch.setattr(litellm, 'completion', complete)
    if exhausted:
        with pytest.raises(litellm.BadRequestError) as caught:
            invoke(gateway, mode)
        assert caught.value is errors[-1]
        assert caught.value._aitelier_candidates == ENDPOINTS
        assert ai_router.is_provider_failure(caught.value)
    else:
        assert invoke(gateway, mode) == '{"useful":true}'
        assert gateway.last_usage['served_by'] == 'ownednext/second'
        assert gateway.last_usage['failed_over_from'] == ['ownedark/first']
    assert calls == WIRE_MODELS, calls          # bounded: one try per distinct endpoint
    held = ai_router.endpoint_cooldowns()
    assert set(held) == ({'ownedark/first', 'ownednext/second'} if exhausted else {'ownedark/first'})
    # Parked until the STATED instant (about two hours), not the generic fallback.
    assert all(3600 < until - time.time() <= 2*3600 + 60 for until in held.values()), held


def test_spend_limit_without_stated_time_parks_for_short_fallback_only(wiring, monkeypatch):
    gateway = wiring()

    def complete(**kwargs):
        raise refusal('You have reached your specified API usage limits.', model=kwargs['model'])
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(litellm.BadRequestError):
        invoke(gateway, 'json')
    held = ai_router.endpoint_cooldowns()
    assert held and all(0 < until - time.time() <= ai_router._COOLDOWN_FALLBACK_S + 5
                        for until in held.values()), held


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('case', ['other-provider', 'request-shaped', 'unsupported-native', 'context'])
def test_request_shaped_400_does_not_walk_or_park(wiring, monkeypatch, mode, case):
    gateway, calls = wiring(), []
    error = {
        'other-provider': refusal(provider='openai'),
        'request-shaped': refusal('Invalid parameter: temperature'),
        'unsupported-native': refusal(UNSUPPORTED),
        'context': litellm.ContextWindowExceededError(
            message='Context window exceeded', model='first', llm_provider='anthropic'),
    }[case]

    def complete(**kwargs):
        calls.append(kwargs['model'])
        raise error
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(type(error)) as caught:
        invoke(gateway, mode)
    assert caught.value is error
    assert calls == WIRE_MODELS[:1]
    assert ai_router.endpoint_cooldowns() == {}
    assert not ai_router.is_provider_failure(caught.value)


def test_burst_429_still_retries_in_place_and_is_not_parked(wiring, monkeypatch):
    gateway, calls = wiring(), []

    def complete(**kwargs):
        calls.append(kwargs['model'])
        if len(calls) == 1:
            raise litellm.RateLimitError(
                message='Rate limit reached for requests per min. Please retry shortly.',
                model='first', llm_provider='anthropic',
                response=httpx.Response(429, request=httpx.Request('POST', 'https://offline.invalid/v1')))
        return response('{"useful":true}')
    monkeypatch.setattr(litellm, 'completion', complete)
    assert invoke(gateway, 'json') == '{"useful":true}'
    assert calls == [WIRE_MODELS[0]] * 2
    assert ai_router.endpoint_cooldowns() == {}


# ── real DPE consumers + SDK fixed-slot writer ───────────────────────────────


@pytest.fixture
def content_case(tmp_path, monkeypatch, wiring, request):
    cases = []

    def make(*, mode='json', read=False):
        loader = ToolLoader(Path(skillflow.__file__).parent/'tools')
        sf = SkillFlow(str(tmp_path/'sdk.db'), workspace_base=str(tmp_path/'artifacts'),
                       tool_loader=loader, artifact_history=False,
                       code_path_resolver=lambda pid, run_id=None: False)
        sf.register_agent_config_from_dict('owned-role', {
            'model': 'owned-pool', 'tools': ['read_file'],
            'native_tool_calling': mode == 'native', 'fallback_to_json_mode': True,
            'max_tool_turns': 4, 'system_prompt': 'Produce the owned report. JSON is allowed.'})
        sf.register_graph(PipelineGraph(name='owned-content', begin='work', steps=[
            StepNode(id='work', agent_config='owned-role', output_mode='content',
                     output_target='artifact', output_fixed={'report': {'file': 'report.md'}})]))
        rid = sf.create_run('owned-content', project_id='owned')
        sf.start_run(rid)
        sf.advance_run(rid)
        claim = sf.claim_next_step(rid)
        assert claim is not None
        grants = claim.inputs['_tool_schemas']
        names = ['write_report', 'read_file'] if read else ['write_report']
        assert set(names) <= set(grants), grants
        schemas = {name: grants[name] for name in names}
        import api.dependencies as dependencies
        monkeypatch.setattr(dependencies, 'get_skillflow', lambda: sf)
        ws = WorkspaceManager(str(tmp_path/'artifacts'), projects_base=str(tmp_path/'projects'))
        monkeypatch.setattr(ws, 'get_code_path', lambda *a, **k: None)
        engine = PipelineEngine(registry=sf.agent_registry, trace_callback=claim.trace)
        engine.assembler = SimpleNamespace(assemble=lambda *a, **k: 'Write the owned report as JSON.')
        events, handlers = [], []
        engine._emit = lambda event, payload=None: events.append((event, payload))
        for name in ('_run_native_step', '_run_content_step', '_run_tool_content_step'):
            original = getattr(engine, name)

            def record(*args, _name=name, _original=original, **kwargs):
                handlers.append(_name)
                return _original(*args, **kwargs)
            monkeypatch.setattr(engine, name, record)

        def build(_factory, _name):
            gateway = wiring()
            gateway.on_completion = lambda event, payload: engine._trace('llm', event, payload)
            return gateway
        monkeypatch.setattr(AgentFactory, '_build_gateway', build)

        def run():
            return engine.run_step(1, 'work', ws, 'owned', agent_config_name='owned-role',
                tool_schemas=schemas, output_dir=claim.inputs['_output_dir'],
                output_target='artifact', output_fixed=claim.inputs['_output_fixed'],
                config_name='owned-content', run_id=rid,
                step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
        case = SimpleNamespace(sf=sf, rid=rid, run=run, events=events, handlers=handlers,
                               root=Path(claim.inputs['_output_dir']))
        cases.append(case)
        return case
    yield make
    for case in cases:
        raw = os.environ.get('REVIEW_RAW')
        if raw:
            Path(raw).mkdir(parents=True, exist_ok=True)
            Path(raw, request.node.name + '-spend.json').write_text(json.dumps({
                'trace': case.sf.get_trace(case.rid), 'events': case.events,
                'handlers': case.handlers,
                'files': {str(p.relative_to(case.root)): p.read_text()
                          for p in case.root.rglob('*') if p.is_file()},
            }, indent=2, default=str) + '\n')
        case.sf._conn.close()


def artifact_reply(native=False):
    if native:
        actions = [('write_report', {'content': USEFUL}), ('finish_step', {'summary': 'Owned report ready'})]
        tools = [{'id': 'owned-' + str(i), 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(params)}} for i, (name, params) in enumerate(actions)]
        return response(None, tools=tools)
    return response(json.dumps({'actions': [{'tool': 'write_report', 'params': {'content': USEFUL}}]}))


@pytest.mark.parametrize('read', [False, True], ids=['content', 'tool-content'])
@pytest.mark.parametrize('mode', ['json', 'native'])
def test_artifact_dispatch_writes_useful_successor_after_spend_limit(content_case, monkeypatch, read, mode):
    case = content_case(mode=mode, read=read)
    calls = []
    message = spend_text(soon())

    def complete(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == WIRE_MODELS[0]:
            raise refusal(message, model=kwargs['model'])
        return artifact_reply(native=mode == 'native')
    monkeypatch.setattr(litellm, 'completion', complete)
    assert case.run() is True
    assert calls == WIRE_MODELS, calls
    assert (case.root/'report.md').read_text() == USEFUL
    assert case.handlers == (['_run_native_step'] if mode == 'native' else
                             ['_run_tool_content_step' if read else '_run_content_step'])
    assert not any(event in {'native_fallback', 'step_retry'} for event, _ in case.events)


@pytest.mark.parametrize('read', [False, True], ids=['content', 'tool-content'])
@pytest.mark.parametrize('mode', ['json', 'native'])
def test_artifact_dispatch_exhausted_spend_limit_is_original_without_replay(content_case, monkeypatch, read, mode):
    case = content_case(mode=mode, read=read)
    calls, errors = [], []
    message = spend_text(soon())

    def complete(**kwargs):
        calls.append(kwargs['model'])
        error = refusal(message, model=kwargs['model'])
        errors.append(error)
        raise error
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(litellm.BadRequestError) as caught:
        case.run()
    # Baseline: native callers replayed this as format/no-output repair (3 native
    # + 1 JSON request); JSON-only callers made 1 request with no endpoint walk.
    assert calls == WIRE_MODELS, calls
    assert caught.value is errors[-1]
    assert not (case.root/'report.md').exists()
    assert not any(event in {'native_fallback', 'step_retry', 'files_written'} for event, _ in case.events)
    rows = case.sf.get_trace(case.rid)
    failures = [row for row in rows if row['category'] == 'error']
    assert len(failures) == 1
    assert failures[0]['event'] == ('native_error' if mode == 'native' else 'provider_error')
    assert failures[0]['payload']['original_error'] == str(errors[-1])
    assert not any(row['category'] == 'tool_call' for row in rows)


@pytest.mark.parametrize('read', [False, True], ids=['content', 'tool-content'])
def test_unsupported_native_400_still_delivers_json_artifact(content_case, monkeypatch, read):
    case = content_case(mode='native', read=read)
    calls = []

    def complete(**kwargs):
        native = bool(kwargs.get('tools'))
        calls.append((kwargs['model'], native))
        if native:
            raise refusal(UNSUPPORTED)
        return artifact_reply()
    monkeypatch.setattr(litellm, 'completion', complete)
    assert case.run() is True
    assert calls == [(WIRE_MODELS[0], True)]*3 + [(WIRE_MODELS[0], False)], calls
    assert (case.root/'report.md').read_text() == USEFUL
    assert sum(event == 'native_fallback' for event, _ in case.events) == 1
    assert ai_router.endpoint_cooldowns() == {}
