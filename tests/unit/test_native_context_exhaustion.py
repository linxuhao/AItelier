"""Context-budget exhaustion after a handoff is an explicit incomplete outcome.

Real DPEAgentNative + real native loop + real SkillFlow tools; only the
provider boundary (gateway.generate_native) is scripted. Before this fix the
host's own NativeTurnBudgetExhausted ("context handoff still exceeds active
endpoint budget") was raised inside the generation try and swallowed by its
generic `except Exception`: native_error, break, and then `files_written` for
the partial file — or, with nothing written, a retry/JSON fallback of the very
request the host had just declared infeasible.
"""
import copy
import json

import pytest

from core.agents import DPEAgentNative
from core.ai_router import NativeTurn
from core.dpe_pipeline import NativeTurnBudgetExhausted
from tests.unit import test_native_direct_code as f


def call(name, **args):
    return {'id': name, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


class ScriptedGateway:
    """Provider boundary only: a 10k window, 8192 output reserve, and a
    prompt estimate that exceeds it after the first call unless a handoff
    shrank it (recoverable) — or regardless (infeasible)."""
    litellm_model = 'offline/scripted-boundary'

    def __init__(self, script, infeasible):
        self.script = iter(script); self.calls = []; self.infeasible = infeasible
        self.max_output_tokens = 8192; self.last_usage = {}; self.last_outbound = {}

    def declared_input_window(self):
        return 10000

    def estimate_request_tokens(self, messages, tools):
        handed = any('context_handoff' in str(m.get('content', '')) for m in messages)
        return 1900 if self.calls and (self.infeasible or not handed) else 100

    def generate_native(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        result = next(self.script)
        self.last_usage = {'completion_tokens': 7, 'reasoning_tokens': 0}
        return result


def run(tmp_path, monkeypatch, script, *, infeasible, retries=1, fallback=False):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    gateway = ScriptedGateway(script, infeasible)
    e, ws = f.host(sf, rid, claim, root,
                   lambda **kw: (_ for _ in ()).throw(AssertionError('fake turn used')))
    e.factory.get_native_agent = lambda _: DPEAgentNative(gateway, 'Owned offline native')
    e.factory.get_max_tool_turns = lambda _: 3
    e.factory.get_max_retries = lambda _: retries
    e.factory.get_fallback_to_json = lambda _: fallback
    e.factory.get_agent = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError('JSON fallback replayed an exhausted request'))
    emitted = []
    original_emit = e._emit
    e._emit = lambda kind, data: (emitted.append(kind), original_emit(kind, data))
    return sf, rid, claim, root, gateway, e, ws, emitted


def trace(sf, rid):
    rows = sf.get_trace(rid)
    return rows, [r['event'] for r in rows]


def payload(rows, event):
    found = [r['payload'] for r in rows if r['event'] == event]
    return [json.loads(p) if isinstance(p, str) else p for p in found]


def test_infeasible_handoff_after_write_stops_incomplete_and_keeps_effects(tmp_path, monkeypatch):
    create = NativeTurn(tool_calls=[call('create', file='useful.py', content='answer = 42\n')])
    sf, rid, claim, root, gateway, e, ws, emitted = run(
        tmp_path, monkeypatch, [create], infeasible=True, fallback=True)

    with pytest.raises(NativeTurnBudgetExhausted, match='context handoff still exceeds'):
        f.execute(e, ws, rid, claim)

    # Exactly the one useful provider call; the infeasible turn is never sent.
    assert len(gateway.calls) == 1 and gateway.max_output_tokens == 8192
    assert (root / 'useful.py').read_text() == 'answer = 42\n'
    rows, events = trace(sf, rid)
    assert 'side_effect_completed' in events and events.count('context_handoff') == 1
    assert e._context_segment == 1
    # Not a generation error, not a delivery, not a fallback.
    assert 'native_error' not in events and 'native_fallback' not in events
    assert not {'files_written', 'step_done', 'native_fallback'} & set(emitted)
    assert 'step_budget' not in events
    [exhausted] = payload(rows, 'context_budget_exhausted')
    assert exhausted['budget_exhausted'] == 'context'
    assert exhausted['written_files'] == ['useful.py']
    assert exhausted['first_write_turn'] == 1
    assert exhausted['turn_budget'] == {'turns_used': 1, 'max_turns': 3}
    assert exhausted['output_budget']['completion_tokens'] == 7
    assert exhausted['context_boundary'] == {
        'limit': 10000, 'estimated_prompt_tokens': 1900, 'output_reserve': 8192,
        'handoff': True, 'unknown': False}
    assert 'context_budget_exhausted' in emitted


def test_infeasible_handoff_before_write_is_not_retried_or_replayed_as_json(tmp_path, monkeypatch):
    read = NativeTurn(tool_calls=[call('read', path='baseline.py')])
    sf, rid, claim, root, gateway, e, ws, emitted = run(
        tmp_path, monkeypatch, [read], infeasible=True, retries=2, fallback=True)

    with pytest.raises(NativeTurnBudgetExhausted, match='context handoff still exceeds'):
        f.execute(e, ws, rid, claim)

    assert len(gateway.calls) == 1
    assert not (root / 'useful.py').exists()
    rows, events = trace(sf, rid)
    assert 'native_error' not in events and 'native_fallback' not in events
    assert not {'files_written', 'step_done', 'step_retry', 'native_fallback'} & set(emitted)
    [exhausted] = payload(rows, 'context_budget_exhausted')
    assert exhausted['written_files'] == [] and exhausted['first_write_turn'] is None
    assert exhausted['turn_budget'] == {'turns_used': 1, 'max_turns': 3}


def test_feasible_handoff_after_write_still_finishes(tmp_path, monkeypatch):
    create = NativeTurn(tool_calls=[call('create', file='useful.py', content='answer = 42\n')])
    finish = NativeTurn(tool_calls=[call('finish_step', summary='owned work checked')])
    sf, rid, claim, root, gateway, e, ws, emitted = run(
        tmp_path, monkeypatch, [create, finish], infeasible=False)

    assert f.execute(e, ws, rid, claim) is True

    assert len(gateway.calls) == 2 and gateway.max_output_tokens == 8192
    handed = gateway.calls[-1]['messages'][1]['content']
    assert 'context_handoff' in handed and 'useful.py' in handed
    assert (root / 'useful.py').read_text() == 'answer = 42\n'
    rows, events = trace(sf, rid)
    assert events.count('context_handoff') == 1
    assert 'context_budget_exhausted' not in events and 'native_error' not in events
    assert 'files_written' in emitted
    [budget] = payload(rows, 'step_budget')
    assert budget['budget_exhausted'] is None and budget['written_files'] == ['useful.py']
