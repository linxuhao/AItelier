"""Offline normal gateway + real fixed-artifact dispatch controls. AUTHOR UNRUN.

Self-contained: can run as /fixtures/test_codingplan_entitlement.py with Source on
PYTHONPATH. Only litellm.completion is scripted; gateway retries, candidate walk,
DPE native/content/tool-content loops and SDK fixed-slot writes remain real.
"""
import json
import os
import time
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
from core.workspace_manager import WorkspaceManager


# Same decisive clause as retained trace 1128/1134. Identities are synthetic.
ENTITLEMENT = (
    'Your account (0000) does not have a valid CodingPlan subscription, '
    'or your subscription has expired. Please visit '
    'https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement'
    '?OpenModelVisible=false&tab=CodingPlan to review your subscription status '
    'or complete the subscription or renewal process. Request id: owned-fixture')
UNSUPPORTED = 'This endpoint does not support tools in native requests.'
USEFUL = '# Owned report\n\nA useful successor produced this artifact.\n'
ENDPOINTS = ['ownedark/first', 'ownedark/first', 'ownednext/second', 'ownedark/first']
WIRE_MODELS = ['openai/first', 'openai/second']


def refusal(message=ENTITLEMENT, *, model='first'):
    return litellm.BadRequestError(
        message=message, model=model, llm_provider='openai',
        response=httpx.Response(400, request=httpx.Request('POST', 'https://offline.invalid/v1')))


def response(content='useful answer', *, tools=None, completion=2):
    return litellm.ModelResponse(model='owned-offline', choices=[{
        'index': 0, 'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': content, 'tool_calls': tools}}],
        usage={'prompt_tokens': 19, 'completion_tokens': completion, 'total_tokens': 19 + completion})


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('AITELIER_SECRETS_DIR', str(tmp_path/'empty-secrets'))
    monkeypatch.setenv('AITELIER_LLM_STREAM', '0')
    monkeypatch.setenv('AITELIER_ZVEC_LIFECYCLE', '0')
    monkeypatch.delenv('AITELIER_FORCE_ALL_AGENT_MODELS', raising=False)
    # No key fields: no real credentials, secret lookup or environment required.
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
        # Thinking enabled avoids the separate, intentional thinking-off-key
        # request-repair branch when testing ordinary bad parameters below.
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
def test_observed_400_walks_distinct_endpoints_and_preserves_error(wiring, monkeypatch, mode, exhausted):
    gateway = wiring()
    calls, events, errors = [], [], []
    gateway.on_completion = lambda event, payload: events.append((event, payload))
    def complete(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == WIRE_MODELS[0] or exhausted:
            error = refusal(model=kwargs['model'])
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
    assert calls == WIRE_MODELS, calls
    finishes = [p for event, p in events if event == 'completion_finished']
    assert [p['outcome'] for p in finishes] == ['error', 'error' if exhausted else 'completed']
    assert [p['served_by'] for p in finishes] == ['ownedark/first', 'ownednext/second']
    assert finishes[0]['error_type'] == 'BadRequestError'
    held = ai_router.endpoint_cooldowns()
    assert set(held) == ({'ownedark/first', 'ownednext/second'} if exhausted else {'ownedark/first'})
    assert all(5*3600 < until-time.time() <= 6*3600 for until in held.values())
    if not exhausted:
        assert wiring().active_model == 'ownednext/second', 'next step must skip known unavailable endpoint'
        assert invoke(gateway, mode) == '{"useful":true}'
        assert calls == WIRE_MODELS + [WIRE_MODELS[1]], 'successful binding must stay sticky'


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('message', [
    UNSUPPORTED, 'Invalid parameter: temperature', 'Malformed JSON request',
    'Policy disallows this input', 'CodingPlan subscription parameter is invalid',
    'Your subscription has expired.', 'Unknown error',
])
def test_request_shaped_400_does_not_walk_or_park(wiring, monkeypatch, mode, message):
    gateway, calls = wiring(), []
    error = refusal(message)
    def complete(**kwargs):
        calls.append(kwargs['model'])
        raise error
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(litellm.BadRequestError) as caught:
        invoke(gateway, mode)
    assert caught.value is error
    assert calls == WIRE_MODELS[:1]
    assert ai_router.endpoint_cooldowns() == {}
    assert not ai_router.is_provider_failure(caught.value)


@pytest.fixture
def content_case(tmp_path, monkeypatch, wiring, request):
    """Real SDK claim and fixed writer; no host production DB or Git needed."""
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
        # Deliberately select each public dispatch seam from actual SDK grants.
        # Tool-content adds one genuine SDK read grant; the write is identical.
        names = ['write_report', 'read_file'] if read else ['write_report']
        assert set(names) <= set(grants), grants
        schemas = {name: grants[name] for name in names}
        import api.dependencies as dependencies
        monkeypatch.setattr(dependencies, 'get_skillflow', lambda: sf)
        ws = WorkspaceManager(str(tmp_path/'artifacts'), projects_base=str(tmp_path/'projects'))
        monkeypatch.setattr(ws, 'get_code_path', lambda *a, **k: None)
        engine = PipelineEngine(registry=sf.agent_registry, trace_callback=claim.trace)
        engine.assembler = SimpleNamespace(assemble=lambda *a, **k: 'Write the owned report as JSON.')
        events, handlers, gateways = [], [], []
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
            gateways.append(gateway)
            return gateway
        monkeypatch.setattr(AgentFactory, '_build_gateway', build)
        def run():
            return engine.run_step(1, 'work', ws, 'owned', agent_config_name='owned-role',
                tool_schemas=schemas, output_dir=claim.inputs['_output_dir'],
                output_target='artifact', output_fixed=claim.inputs['_output_fixed'],
                config_name='owned-content', run_id=rid,
                step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
        case = SimpleNamespace(sf=sf, rid=rid, claim=claim, run=run, events=events,
            handlers=handlers, gateways=gateways, engine=engine, root=Path(claim.inputs['_output_dir']))
        cases.append(case)
        return case
    yield make
    for case in cases:
        raw = os.environ.get('REVIEW_RAW')
        if raw:
            Path(raw).mkdir(parents=True, exist_ok=True)
            Path(raw, request.node.name+'.json').write_text(json.dumps({
                'trace': case.sf.get_trace(case.rid), 'events': case.events,
                'handlers': case.handlers,
                'files': {str(p.relative_to(case.root)): p.read_text()
                          for p in case.root.rglob('*') if p.is_file()},
            }, indent=2, default=str)+'\n')
        case.sf._conn.close()


def artifact_reply(native=False):
    if native:
        actions = [('write_report', {'content': USEFUL}), ('finish_step', {'summary': 'Owned report ready'})]
        tools = [{'id': 'owned-'+str(i), 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(params)}} for i, (name, params) in enumerate(actions)]
        return response(None, tools=tools)
    return response(json.dumps({'actions': [{'tool': 'write_report', 'params': {'content': USEFUL}}]}))


@pytest.mark.parametrize('read', [False, True], ids=['content', 'tool-content'])
@pytest.mark.parametrize('mode', ['json', 'native'])
@pytest.mark.parametrize('first', ['entitlement', 'blank-zero', 'blank-missing'])
def test_artifact_dispatch_writes_useful_successor(content_case, monkeypatch, read, mode, first):
    case = content_case(mode=mode, read=read)
    calls = []
    def complete(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == WIRE_MODELS[0]:
            if first == 'entitlement':
                raise refusal()
            blank = response(' \n\t', completion=0)
            if first == 'blank-missing':
                blank.usage = None
            return blank
        return artifact_reply(native=mode == 'native')
    monkeypatch.setattr(litellm, 'completion', complete)
    assert case.run() is True
    assert calls == WIRE_MODELS, calls
    assert (case.root/'report.md').read_text() == USEFUL
    assert case.handlers == (['_run_native_step'] if mode == 'native' else
                             ['_run_tool_content_step' if read else '_run_content_step'])
    assert not any(event in {'native_fallback', 'step_retry'} for event, _ in case.events)
    rows = case.sf.get_trace(case.rid)
    assert any(row['event'] == 'write_report' and row['category'] == 'tool_call' for row in rows)
    assert any(row['event'] == 'completion_finished' and row['payload'].get('outcome') == 'error' for row in rows)


@pytest.mark.parametrize('read', [False, True], ids=['content', 'tool-content'])
@pytest.mark.parametrize('mode', ['json', 'native'])
def test_artifact_dispatch_exhausted_refusal_is_original_without_replay(content_case, monkeypatch, read, mode):
    case = content_case(mode=mode, read=read)
    calls, errors = [], []
    def complete(**kwargs):
        calls.append(kwargs['model'])
        error = refusal(model=kwargs['model'])
        errors.append(error)
        raise error
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(litellm.BadRequestError) as caught:
        case.run()
    assert calls == WIRE_MODELS, calls
    assert caught.value is errors[-1]
    assert not (case.root/'report.md').exists()
    assert not any(event in {'native_fallback', 'step_retry', 'files_written'} for event, _ in case.events)
    rows = case.sf.get_trace(case.rid)
    errors_in_trace = [row for row in rows if row['category'] == 'error']
    assert len(errors_in_trace) == 1
    assert errors_in_trace[0]['event'] == ('native_error' if mode == 'native' else 'provider_error')
    assert errors_in_trace[0]['payload']['original_error'] == str(errors[-1])
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
    # Native setup installs host controls into the live schema dictionary.
    # Those actual non-write grants remain when run_step selects JSON, so both
    # initial schema subsets now enter the existing tool-content handler.
    assert 'ask_more_turns' in case.engine._tool_schemas
    assert case.handlers == ['_run_native_step', '_run_tool_content_step']
    assert sum(event == 'native_fallback' for event, _ in case.events) == 1
    assert ai_router.endpoint_cooldowns() == {}


@pytest.mark.parametrize('mode', ['native', 'json'])
@pytest.mark.parametrize('kind', ['non-badrequest', 'context'])
def test_entitlement_match_does_not_widen_exception_or_context_policy(wiring, monkeypatch, mode, kind):
    gateway, calls = wiring(), []
    error = (ValueError(ENTITLEMENT) if kind == 'non-badrequest' else
             litellm.ContextWindowExceededError(
                 message='Context window exceeded', model='first', llm_provider='openai'))
    def complete(**kwargs):
        calls.append(kwargs['model'])
        raise error
    monkeypatch.setattr(litellm, 'completion', complete)
    with pytest.raises(type(error)) as caught:
        invoke(gateway, mode)
    assert caught.value is error
    assert calls == WIRE_MODELS[:1], 'unknown context ceiling cannot justify walking to another unknown ceiling'
    assert ai_router.endpoint_cooldowns() == {}


@pytest.mark.parametrize('positive', ['completion', 'reasoning'])
def test_blank_native_with_positive_observed_usage_preserves_native_policy(wiring, monkeypatch, positive):
    gateway = wiring()
    reply = response(' \n\t', completion=7 if positive == 'completion' else 0)
    if positive == 'reasoning':
        reply.usage = litellm.Usage(prompt_tokens=19, completion_tokens=0,
            completion_tokens_details={'reasoning_tokens': 5})
    calls, events = [], []
    gateway.on_completion = lambda event, payload: events.append((event, payload))
    def complete(**kwargs):
        calls.append(kwargs['model'])
        return reply
    monkeypatch.setattr(litellm, 'completion', complete)
    turn = gateway.generate_native([{'role': 'user', 'content': 'owned request'}])
    assert turn.text == '' and turn.tool_calls == [] and turn.reasoning_content == ''
    assert calls == WIRE_MODELS[:1]
    assert gateway.active_model == 'ownedark/first' and gateway._failovers == []
    assert ai_router.endpoint_cooldowns() == {}
    assert [(p['outcome'], p['served_by']) for event, p in events
            if event == 'completion_finished'] == [('completed', 'ownedark/first')]


DESIGN = '# Owned architecture\n\nA successor produced the bounded design artifact.\n'


@pytest.fixture
def architect_case(tmp_path, monkeypatch, wiring, request):
    """Exact frozen Architect output contract, ALL actual SDK claim grants.

    This is an isolated output-consumer fixture, not the foreign game run.
    No schema filtering, replacement tool executors or handler stubs.
    """
    sf = SkillFlow(str(tmp_path/'architect-sdk.db'), workspace_base=str(tmp_path/'artifacts'),
        tool_loader=ToolLoader(Path(skillflow.__file__).parent/'tools'),
        artifact_history=False, code_path_resolver=lambda pid, run_id=None: False)
    sf.register_agent_config_from_dict('owned-architect', {
        'model': 'owned-pool', 'tools': ['read_file'],
        'native_tool_calling': request.param == 'native', 'fallback_to_json_mode': True,
        'max_tool_turns': 20, 'system_prompt': 'Produce the owned design artifact. JSON is allowed.'})
    sf.register_graph(PipelineGraph(name='owned-architect-design', begin='2', steps=[
        StepNode(id='2', agent_config='owned-architect', output_mode='content',
            output_target='artifact',
            output_fixed={'design': {'file': 'step2_design.md', 'target': 'artifact'}})]))
    rid = sf.create_run('owned-architect-design', project_id='owned')
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    assert claim is not None
    schemas = claim.inputs['_tool_schemas']  # Full grants; never filtered.
    import api.dependencies as dependencies
    monkeypatch.setattr(dependencies, 'get_skillflow', lambda: sf)
    ws = WorkspaceManager(str(tmp_path/'artifacts'), projects_base=str(tmp_path/'projects'))
    monkeypatch.setattr(ws, 'get_code_path', lambda *a, **k: None)
    engine = PipelineEngine(registry=sf.agent_registry, trace_callback=claim.trace)
    # Retain the assembler's other native APIs; only fixture prose is fixed.
    monkeypatch.setattr(engine.assembler, 'assemble', lambda *a, **k: 'Produce the owned design. JSON is allowed.')
    events, handlers, gateways = [], [], []
    engine._emit = lambda event, payload=None: events.append((event, payload))
    for name in ('_run_native_step', '_run_content_step', '_run_tool_content_step', '_run_tool_step'):
        original = getattr(engine, name)
        def observe(*args, _name=name, _original=original, **kwargs):
            handlers.append({'handler': _name, 'kwargs': dict(kwargs),
                             'live_grants': sorted(engine._tool_schemas)})
            return _original(*args, **kwargs)
        monkeypatch.setattr(engine, name, observe)
    def build(_factory, _name):
        gateway = wiring()
        gateway.on_completion = lambda event, payload: engine._trace('llm', event, payload)
        gateways.append(gateway)
        return gateway
    monkeypatch.setattr(AgentFactory, '_build_gateway', build)
    root = Path(claim.inputs['_output_dir'])
    raw = os.environ.get('REVIEW_RAW')
    # Preserve actual grants even when a generation assertion below fails.
    def evidence():
        if raw:
            Path(raw).mkdir(parents=True, exist_ok=True)
            Path(raw, request.node.name+'-architect.json').write_text(json.dumps({
                'claim_inputs': claim.inputs, 'full_grants': schemas,
                'trace': sf.get_trace(rid), 'events': events, 'handlers': handlers,
                'files': {str(p.relative_to(root)): p.read_text()
                          for p in root.rglob('*') if p.is_file()},
            }, indent=2, default=str)+'\n')
    evidence()
    # The retained trace 1137 also explicitly advertises write_design. If this
    # SDK generation differs, fail visibly with its complete actual grants.
    assert {'create_design', 'edit_design', 'write_design'} <= set(schemas), schemas
    assert claim.inputs['_output_target'] == 'artifact'
    assert claim.inputs['_output_fixed']['design']['file'] == 'step2_design.md'
    def run():
        return engine.run_step(1, '2', ws, 'owned', agent_config_name='owned-architect',
            tool_schemas=schemas, output_dir=claim.inputs['_output_dir'],
            output_target=claim.inputs['_output_target'], output_fixed=claim.inputs['_output_fixed'],
            config_name='owned-architect-design', run_id=rid,
            step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
    case = SimpleNamespace(sf=sf, rid=rid, claim=claim, run=run, engine=engine,
        events=events, handlers=handlers, gateways=gateways, root=root, mode=request.param)
    try:
        yield case
    finally:
        evidence()
        sf._conn.close()


def design_reply(native):
    actions = [{'tool': 'create_design', 'params': {'initialContent': DESIGN}}]
    if not native:
        # Artifact JSON keeps the useful single-response contract: no finish
        # action is needed to deliver this actual generated fixed-slot write.
        return response(json.dumps({'actions': actions}))
    actions.append({'tool': 'finish_step', 'params': {'summary': 'Owned design ready'}})
    tools = [{'id': 'owned-design-'+str(i), 'type': 'function', 'function': {
        'name': action['tool'], 'arguments': json.dumps(action['params'])}}
        for i, action in enumerate(actions)]
    return response(None, tools=tools)


@pytest.mark.parametrize('architect_case', ['native', 'json'], indirect=True)
@pytest.mark.parametrize('exhausted', [False, True], ids=['successor', 'exhausted'])
def test_architect_design_full_generated_grants(architect_case, monkeypatch, exhausted):
    case = architect_case
    calls, errors = [], []
    def complete(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == WIRE_MODELS[0] or exhausted:
            error = refusal(model=kwargs['model'])
            errors.append(error)
            raise error
        return design_reply(native=case.mode == 'native')
    monkeypatch.setattr(litellm, 'completion', complete)
    if exhausted:
        with pytest.raises(litellm.BadRequestError) as caught:
            case.run()
        assert caught.value is errors[-1]
        assert not (case.root/'step2_design.md').exists()
    else:
        assert case.run() is True
        assert (case.root/'step2_design.md').read_text() == DESIGN
    assert calls == WIRE_MODELS, calls
    # Full slot grants include write_design AND create/edit, so prefix dispatch
    # goes to tool-content. Forcing _run_tool_step would require dropping a
    # real grant; do not make that alteration to satisfy a guessed path.
    expected = '_run_native_step' if case.mode == 'native' else '_run_tool_content_step'
    assert [record['handler'] for record in case.handlers] == [expected], case.handlers
    assert all(record['kwargs'].get('require_explicit_completion', False) is False
               for record in case.handlers), case.handlers
    assert case.engine._output_target == 'artifact'
    assert not case.engine._json_source_effects
    assert not any(event in {'native_fallback', 'step_retry'} for event, _ in case.events)
    rows = case.sf.get_trace(case.rid)
    assert not any(row['event'] == 'json_source_completion' for row in rows)
    if exhausted:
        failures = [row for row in rows if row['category'] == 'error']
        assert len(failures) == 1
        assert failures[0]['event'] == ('native_error' if case.mode == 'native' else 'provider_error')
        assert failures[0]['payload']['original_error'] == str(errors[-1])
        assert not any(row['category'] == 'tool_call' for row in rows)
        assert not any(event == 'files_written' for event, _ in case.events)
    else:
        assert any(row['category'] == 'tool_call' and row['event'] == 'create_design' for row in rows)
