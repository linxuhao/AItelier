# tests/integration/test_native_parity.py
# Regression tests for native-mode parity with JSON mode:
#   - turn budget resolved by role (agent_config_name), not step_id (Bug H)
#   - no-tool-call reply is salvaged with a write nudge, not ended empty
#   - genuine no-op signal completes cleanly with EMPTY output (no repo copy)
#   - pure exploration exhaustion still raises (no-op path must NOT mask it)
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded

TS = {"read_file": {}, "list_tree": {}, "write": {}}


class _WS:
    """Minimal workspace double; records draft writes."""
    def __init__(self, tmp: Path):
        self.base_path = tmp
        self.projects_base = tmp / "projects"
        self.written_drafts = {}

    def _get_secure_path(self, project_id):
        return self.base_path / project_id

    def get_code_path(self, project_id, run_id=None):
        p = self.projects_base / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_draft(self, project_id, step_id, filename, content, graph_name=None):
        self.written_drafts[f"{step_id}/{filename}"] = content

    def clean_draft_dir(self, project_id, step_id, graph_name=None):
        # Reset this step's recorded staging (called at the start of a run).
        self.written_drafts = {
            k: v for k, v in self.written_drafts.items()
            if not k.startswith(f"{step_id}/")
        }


def _setup(tmp, project_id="default", step_id="t_impl"):
    (tmp / project_id / f"{step_id}.tmp").mkdir(parents=True, exist_ok=True)
    code = tmp / "projects" / project_id
    code.mkdir(parents=True, exist_ok=True)
    (code / "README.md").write_text("# existing\n")


def _turn(text="", tool_calls=None, reasoning="", truncated=False):
    return SimpleNamespace(text=text, tool_calls=tool_calls or [],
                           reasoning_content=reasoning, truncated=truncated)


def _tc(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name,
                                    "arguments": json.dumps(args or {})}}


@pytest.fixture
def engine():
    from unittest.mock import patch
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        eng = PipelineEngine()
        eng.factory = MagicMock()
        eng.factory.is_native.return_value = True
        eng.factory.get_fallback_to_json.return_value = False  # stay in native
        eng.factory.get_max_retries.return_value = 1
        eng.factory.get_max_tool_turns.return_value = 4
        nat = eng.factory.get_native_agent.return_value
        nat.gateway.litellm_model = "mock"
        nat.gateway.last_usage = {}
        return eng


def _run(engine, ws, **kw):
    return engine.run_step(task_id=1, step_id="t_impl", workspace=ws,
                           project_id="default",
                           agent_config_name="task_implementer",
                           tool_schemas=TS, **kw)


def test_budget_resolved_by_role_not_step_id(engine):
    """Bug H: the turn budget must be looked up by role, not step_id."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
                            _turn(tool_calls=[_tc("finish_step")])]
    assert _run(engine, ws) is True
    called = [c.args[0] for c in engine.factory.get_max_tool_turns.call_args_list if c.args]
    assert "task_implementer" in called
    assert "t_impl" not in called


def test_the_resume_budget_is_the_role_budget_not_the_default(engine, monkeypatch):
    """The value, not just the call — this is where the wrong lookup did harm.

    `_rebuild_from_deltas` returns `max_turns + granted extras` and a resume
    REPLACES `current_max_turns` with it, so a base resolved by step_id put a
    step resumed after a host restart on DEFAULT_MAX_TOOL_TURNS plus its grants
    while the same step run fresh got its configured budget. t_impl is
    configured at 6 against a default of 10, and the budget is measured: runs
    that reached the 10-turn ceiling produced complete output 24% of the time
    against 54% for shorter ones. So a resume silently bought four turns that
    make the output worse.

    The sibling test above asserts the call ARGUMENT and would stay green if the
    argument were right and the value discarded; this one reads what
    `_resume_from_trace` was actually handed.
    """
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
                            _turn(tool_calls=[_tc("finish_step")])]

    # The role's configured budget and the registry's default must differ, or
    # the wrong lookup and the right one return the same number and nothing is
    # being tested.
    ROLE_BUDGET, DEFAULT_BUDGET = 6, 10
    engine.factory.get_max_tool_turns.side_effect = (
        lambda key: ROLE_BUDGET if key == "task_implementer" else DEFAULT_BUDGET)

    seen = []
    monkeypatch.setattr(PipelineEngine, "_resume_from_trace",
                        lambda self, pid, max_turns: seen.append(max_turns))

    assert _run(engine, ws) is True

    assert seen, "the resume path never ran, so this test proves nothing"
    assert seen[0] == ROLE_BUDGET, (
        f"the resumed step's base budget was {seen[0]}, not the role's "
        f"{ROLE_BUDGET} — resolved by step_id, it falls back to the default "
        f"{DEFAULT_BUDGET} and the resumed step runs on a ceiling it was never "
        f"configured for")


def test_a_resume_after_a_rejected_delivery_shows_the_agent_the_rejection(engine, monkeypatch):
    """The resume path serves BOTH a host restart and a post-gate retry.

    It used to say only "you were restarted, your files are still staged", so
    an agent whose delivery the output gate had just rejected saw no failure at
    all and re-issued finish_step until validation_exhausted. Measured on
    release.mainline-green r5 (t_impl instance 4998, 2026-09-11): the claim
    carried a real GDScript parse error and the agent's own reasoning at turns
    28 and 29 was "every finish_step has returned completed, just re-issue it".

    `_validation_error_block` already existed and its docstring records this
    same lesson being learned for the fresh-start path. This asserts the RESUME
    message carries it too — the failure text itself, not merely that some
    message was appended, because a resume notice that omits the error is
    exactly the bug.
    """
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("finish_step")])]

    GATE_ERROR = "Validation failed:\n{'file': 'tests/t.gd', 'passed': False}"
    # The resume is only honoured when every file it claims is really staged.
    draft = tmp / "draft"; draft.mkdir(); (draft / "main.py").write_text("x")
    ws._draft_dir = lambda pid, step, graph: draft
    monkeypatch.setattr(PipelineEngine, "_resume_from_trace",
                        lambda self, pid, max_turns: {
                            "messages": [{"role": "user", "content": "earlier"}],
                            "turns": 2, "written_files": ["main.py"],
                            "dropped_tail": 0, "current_max_turns": 10,
                            "turn_grants": 0})

    _run(engine, ws, validation_error=GATE_ERROR)

    resumed = "\n".join(m.get("content") or "" for m in engine._native_messages
                        if m.get("role") == "user")
    assert GATE_ERROR in resumed, (
        "the resume message does not contain the gate's rejection, so the "
        "agent is asked to continue a delivery it does not know was refused")
    assert "REJECTED" in resumed, (
        "the resume message still reads as a plain host restart; an agent told "
        "only that its files are staged concludes nothing failed")

def test_no_tool_reply_is_salvaged_then_writes(engine):
    """Feature B: a prose-only reply nudges the agent to write instead of
    ending the step empty (the inst-975 failure mode)."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [
        _turn(text="No changes needed."),                                   # nudge
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    assert _run(engine, ws) is True
    assert nat.turn.call_count == 3  # salvaged, did not stop at turn 1


def test_genuine_noop_completes_clean_without_copying_repo(engine):
    """No-op completion: when the agent produces no tool call until the budget
    is exhausted, the step completes cleanly with EMPTY output. It must NOT copy
    the existing repo into the draft (the old 'floor' did, producing wholesale
    commits and corrupting binaries via read_text(errors='replace'))."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"output": "x"})
    engine.factory.get_max_tool_turns.return_value = 2
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(text="no-op"), _turn(text="still no-op")]
    assert _run(engine, ws) is True
    assert ws.written_drafts == {}  # NO repo files copied into the draft


def test_exploration_exhaustion_still_raises(engine):
    """Floor must NOT mask a real failure: an agent that keeps exploring
    (tool calls every turn) but never writes still raises."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"output": "read result"})
    engine.factory.get_max_tool_turns.return_value = 2
    nat = engine.factory.get_native_agent.return_value
    nat.turn.return_value = _turn(tool_calls=[_tc("read_file", {"path": "README.md"})])
    with pytest.raises(MaxRetriesExceeded):
        _run(engine, ws)
    assert not ws.written_drafts  # floor did not trigger


def test_exhaustion_retains_conversation_without_automatic_retry(engine):
    """A full exploration budget is incomplete, not a fresh retry budget."""
    from core.dpe_pipeline import NativeTurnBudgetExhausted
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine.factory.get_max_tool_turns.return_value = 2
    engine.factory.get_max_retries.return_value = 2
    snapshots = []

    def rec(messages, tools, tool_choice):
        snapshots.append((tuple(m["role"] for m in messages), messages[1]["content"]))
        return _turn(tool_calls=[_tc("read_file", {"path": "README.md"})])

    engine._exec_tool = MagicMock(return_value={"output": "read result"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = rec
    with pytest.raises(NativeTurnBudgetExhausted):
        _run(engine, ws)
    assert nat.turn.call_count == 2
    assert len(snapshots[1][0]) > len(snapshots[0][0])
    assert "tool" in snapshots[1][0]
    assert snapshots[1][1] == snapshots[0][1]


def test_reasoning_starved_turn_is_reported_not_silent(engine):
    """A turn cut off at max_output_tokens with no text and no tool call is the
    silent-review failure: DeepSeek bills reasoning inside that cap, so an
    over-long chain of thought can consume the whole budget and the step's
    write/verdict is never emitted. Untagged it is indistinguishable from a
    deliberate no-op — the shape that let task_implementer_reviewer burn ten
    turns at 4096 and 'review' nothing."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    engine.factory.get_max_tool_turns.return_value = 3
    nat = engine.factory.get_native_agent.return_value
    nat.gateway.max_output_tokens = 4096
    nat.gateway.last_usage = {"completion_tokens": 4096, "reasoning_tokens": 4096}
    nat.turn.side_effect = [
        _turn(reasoning="t" * 900, truncated=True),                          # starved
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    starved = [p for ev, p in events if ev == "output_cap_starved"]
    assert len(starved) == 1, [ev for ev, _ in events]
    assert starved[0]["max_output_tokens"] == 4096
    assert starved[0]["reasoning_tokens"] == 4096
    # Exactly one: the two healthy turns that followed must NOT be reported, or
    # the warning stops meaning anything.
    assert starved[0]["turn"] == 1


# ── Starved-turn recovery: escalate the cap instead of repeating the call ──
#
# Detecting the starve was only half of it. The step used to reissue a
# byte-identical call — same model, prompt, effort and cap — which necessarily
# starves again; observed live as task_implementer burning two consecutive full
# 32768-token budgets on pure reasoning and buying nothing with either.

def _wire_real_escalation(nat, cap):
    """Give the mock gateway the REAL escalation, so these tests exercise the
    pipeline↔gateway contract rather than a mock that agrees with itself."""
    from core.ai_router import AIGateway
    nat.gateway.max_output_tokens = cap
    nat.gateway.escalate_output_cap = lambda: AIGateway.escalate_output_cap(
        nat.gateway)
    return nat


def test_starved_turn_escalates_output_cap_for_the_retry(engine):
    """The retry after a starve must be a DIFFERENT call: same everything, but
    double the cap that caused the truncation."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value, 8192)
    nat.turn.side_effect = [
        _turn(reasoning="t" * 900, truncated=True),                          # starved
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    esc = [p for ev, p in events if ev == "output_cap_escalated"]
    assert len(esc) == 1, [ev for ev, _ in events]
    assert esc[0]["previous_cap"] == 8192
    assert esc[0]["new_cap"] == 16384
    assert esc[0]["turn"] == 1
    # The point of the whole change: the cap the next turn actually uses moved.
    assert nat.gateway.max_output_tokens == 16384


def test_escalated_cap_persists_across_the_rest_of_the_step(engine):
    """The condition that starved turn 1 — a huge stable prefix plus deep
    reasoning — is still there on turn 2, so the raised cap must not reset per
    turn. A second starve escalates again, from the already-raised value."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    engine.factory.get_max_tool_turns.return_value = 5
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value, 8192)
    nat.turn.side_effect = [
        _turn(reasoning="t" * 900, truncated=True),                          # starved
        _turn(reasoning="t" * 900, truncated=True),                          # starved again
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    esc = [p for ev, p in events if ev == "output_cap_escalated"]
    assert [(e["previous_cap"], e["new_cap"]) for e in esc] == [
        (8192, 16384), (16384, 32768)]
    assert nat.gateway.max_output_tokens == 32768


def test_starve_at_the_ceiling_reports_instead_of_escalating(engine):
    """At the ceiling there is nothing left to double into but an API error.
    The step must say so rather than claim an escalation it did not make."""
    from core.ai_router import OUTPUT_CAP_CEILING
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value,
                                OUTPUT_CAP_CEILING)
    nat.turn.side_effect = [
        _turn(reasoning="t" * 900, truncated=True),                          # starved
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    with pytest.raises(MaxRetriesExceeded, match="output cap.*explicit attention required"):
        _run(engine, ws)

    assert [p for ev, p in events if ev == "output_cap_escalated"] == []
    ceil = [p for ev, p in events if ev == "output_cap_ceiling"]
    assert len(ceil) == 1
    assert ceil[0]["previous_cap"] == OUTPUT_CAP_CEILING
    assert ceil[0]["new_cap"] is None
    assert nat.gateway.max_output_tokens == OUTPUT_CAP_CEILING
    assert nat.turn.call_count == 1


def test_healthy_turns_never_escalate(engine):
    """A truncated turn that still produced a tool call is not starved, and a
    complete turn certainly is not. Escalating on either would inflate every
    step's budget and make the warning meaningless."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value, 8192)
    nat.turn.side_effect = [
        # truncated, but it got its tool call out — that is not a starve
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})],
              reasoning="t" * 900, truncated=True),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    assert [ev for ev, _ in events if ev.startswith("output_cap_")] == []
    assert nat.gateway.max_output_tokens == 8192


def test_starved_turns_truncated_reasoning_is_not_replayed(engine):
    """The starved chain of thought is a full cap's worth of tokens, cut off
    mid-sentence. Replaying it into every later turn would grow the prompt by
    exactly the budget just doubled, pushing the request toward the context
    window the raised cap has to share."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value, 8192)
    dead = "TRUNCATED-COT-" + "t" * 900
    nat.turn.side_effect = [
        _turn(reasoning=dead, truncated=True),                               # starved
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    assert _run(engine, ws) is True

    # `messages` is mutated in place, so the last call carries the final history.
    final = nat.turn.call_args_list[-1].kwargs["messages"]
    assert not any(dead in str(m.get("reasoning_content", "")) for m in final), \
        "starved turn's truncated reasoning was carried into the prompt"
    # The turn still has to appear in the history — dropping it silently would
    # leave the follow-up nudge answering nothing.
    assert any(m.get("role") == "assistant" for m in final)


# The escalation is raised "for the retry" — but `for … in range(max_turns)`
# freezes the bound at loop entry, so a starve on the LAST turn raised the cap
# and then immediately ended the step empty. Live, jinyong-usable 2026-08-23:
# nine consecutive t_plan executions starved on turn 6 of 6, each logged
# "16384 → 32768 for the retry", each returned a 0-byte task_plan.md, and the
# second escalation never once appeared — no turn ever ran at the raised cap.

def test_last_turn_starve_grants_the_turn_the_escalation_was_raised_for(engine):
    """A raised cap that no turn ever uses is not a fix, it is a log line."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    engine.factory.get_max_tool_turns.return_value = 1      # turn 1 IS the last
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value, 16384)
    nat.turn.side_effect = [
        _turn(reasoning="t" * 900, truncated=True),                          # starved
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"}),
                          _tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    granted = [p for ev, p in events if ev == "turn_granted_for_escalation"]
    assert len(granted) == 1, [ev for ev, _ in events]
    assert nat.gateway.max_output_tokens == 32768
    # The whole point: the step produced its output instead of completing empty.
    # (_exec_tool is mocked, so the write is observed through the event, not ws.)
    assert [p for ev, p in events if ev == "files_written"], [ev for ev, _ in events]
    assert not [p for ev, p in events if ev == "step_done"
                and "budget reached" in (p.get("preview") or "")]


def test_last_turn_starve_at_the_ceiling_grants_nothing(engine):
    """The grant is bounded by the escalation, not by a counter of its own: at
    OUTPUT_CAP_CEILING escalate_output_cap() declines, so there is nothing to
    buy a turn for and the loop must still end."""
    from core.ai_router import OUTPUT_CAP_CEILING
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    engine.factory.get_max_tool_turns.return_value = 1
    nat = _wire_real_escalation(engine.factory.get_native_agent.return_value,
                                OUTPUT_CAP_CEILING)
    nat.turn.side_effect = [_turn(reasoning="t" * 900, truncated=True)]      # starved
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    with pytest.raises(MaxRetriesExceeded, match="output cap.*explicit attention required"):
        _run(engine, ws)

    assert [p for ev, p in events if ev == "turn_granted_for_escalation"] == []
    assert nat.gateway.max_output_tokens == OUTPUT_CAP_CEILING
    assert nat.turn.call_count == 1          # no extra turn was taken


def test_ask_more_turns_actually_extends_the_native_loop(engine):
    """Same frozen-range() defect, second symptom: the native path answered
    "+N granted" and then stopped at the original bound anyway."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    engine.factory.get_max_tool_turns.return_value = 2
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [
        _turn(tool_calls=[_tc("ask_more_turns", {"turns": 2, "reason": "big file"})]),
        _turn(tool_calls=[_tc("list_tree", {})]),
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    events = []
    engine._emit = lambda ev, payload=None: events.append((ev, payload or {}))
    assert _run(engine, ws) is True

    req = [p for ev, p in events if ev == "agent_turn_request"]
    assert len(req) == 1 and req[0]["extra_turns"] == 2
    # Granted means USED: the write lands on turn 3, past the original bound.
    assert [p for ev, p in events if ev == "files_written"], [ev for ev, _ in events]
    assert nat.turn.call_count == 4


@pytest.fixture
def budget_case(engine, tmp_path):
    """Real native turn loop; instrumented edit writes actual retained bytes."""
    _setup(tmp_path)
    ws = _WS(tmp_path)
    draft = tmp_path / "default" / "t_impl.tmp" / "partial.gd"
    events = []
    engine._trace = lambda category, event, payload: events.append((category, event, payload))
    engine.factory.get_max_tool_turns.return_value = 20
    engine.factory.get_max_retries.return_value = 3
    engine.factory.get_fallback_to_json.return_value = True

    def execute(action):
        if action["tool"] == "edit":
            draft.write_text("partial draft; not completed\n")
            return {"edited": "partial.gd"}
        return {"ok": True}

    engine._exec_tool = MagicMock(side_effect=execute)
    return engine, ws, draft, events


def test_twenty_turn_partial_exhaustion_retains_draft_and_never_falls_back(budget_case):
    from core.dpe_pipeline import NativeTurnBudgetExhausted
    eng, ws, draft, events = budget_case
    nat = eng.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("edit")])] + [
        _turn(tool_calls=[_tc("read_file")]) for _ in range(19)]
    with pytest.raises(NativeTurnBudgetExhausted, match="explicit attention required"):
        _run(eng, ws)
    assert nat.turn.call_count == 20
    assert draft.read_text() == "partial draft; not completed\n"
    assert (ws.get_code_path("default") / "README.md").read_text() == "# existing\n"
    assert not (ws.get_code_path("default") / "partial.gd").exists()
    exhausted = [p for c, e, p in events if e == "turn_budget_exhausted"]
    assert len(exhausted) == 1 and exhausted[0]["turns"] == 20
    assert exhausted[0]["written_files"] == ["partial.gd"]
    # A JSON fallback would build a different agent and retry this partial work.
    eng.factory.get_agent.assert_not_called()


@pytest.mark.parametrize("finish_turn, write", [(2, True), (20, True), (1, False)])
def test_explicit_finish_preserves_native_success_including_final_turn(budget_case, finish_turn, write):
    eng, ws, draft, _events = budget_case
    nat = eng.factory.get_native_agent.return_value
    responses = [_turn(tool_calls=[_tc("read_file")]) for _ in range(finish_turn - 1)]
    # Deliberately put finish BEFORE edit in the final response: all calls must run.
    calls = [_tc("finish_step")]
    if write:
        calls.append(_tc("edit", cid="write-last"))
    responses.append(_turn(tool_calls=calls))
    nat.turn.side_effect = responses
    assert _run(eng, ws) is True
    assert nat.turn.call_count == finish_turn
    assert draft.exists() is write


@pytest.mark.asyncio
async def test_driver_exhaustion_fails_once_without_promotion_or_test_node(budget_case, monkeypatch, tmp_path):
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode, Transition
    from core.run_driver import _step
    import aitelier.runner
    eng, ws, draft, _events = budget_case
    nat = eng.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("edit")])] + [
        _turn(tool_calls=[_tc("read_file")]) for _ in range(19)]
    sf = SkillFlow(":memory:")
    sf.register_graph(PipelineGraph(name="budget_guard", begin="t_impl", steps=[
        StepNode(id="t_impl", step_type="agent", lifecycle={"on_deliver": {"tool": "repo_apply"}}, transitions=[Transition(to="test")]),
        StepNode(id="test", step_type="tool", tool_name="never_run_tests", transitions=[Transition(to="done")]),
        StepNode(id="done", step_type="gate", transitions=[Transition(to=None)])]))
    rid = sf.create_run("budget_guard", {"project_id": "default"}, project_id="default")
    sf.start_run(rid)
    confirm = MagicMock(wraps=sf.confirm_step)
    fail = MagicMock(wraps=sf.fail_step)
    monkeypatch.setattr(sf, "confirm_step", confirm)
    monkeypatch.setattr(sf, "fail_step", fail)

    class Runner:
        def __init__(self, **_kwargs):
            pass

        async def execute(self, _claimed):
            return _run(eng, ws)

    monkeypatch.setattr(aitelier.runner, "AgentStepRunner", Runner)
    assert await _step(sf, None, ws, rid, False, 5) == "failed"
    assert nat.turn.call_count == 20
    confirm.assert_not_called()
    assert fail.call_count == 1 and fail.call_args.kwargs["retryable"] is False
    assert draft.read_text() == "partial draft; not completed\n"
    assert sf.get_run(rid)["current_node"] == "t_impl"
    assert not (ws.get_code_path("default") / "partial.gd").exists()


def test_exhausted_trace_resume_keeps_draft_without_another_model_turn(budget_case):
    from core.dpe_pipeline import NativeTurnBudgetExhausted, PipelineEngine
    eng, ws, draft, events = budget_case
    nat = eng.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("edit")])] + [
        _turn(tool_calls=[_tc("read_file")]) for _ in range(19)]
    with pytest.raises(NativeTurnBudgetExhausted):
        _run(eng, ws)
    rebuilt = PipelineEngine._rebuild_from_deltas(
        [(event, payload) for _category, event, payload in events], 20)
    assert rebuilt["turns"] == 20 and rebuilt["written_files"] == ["partial.gd"]
    eng._resume_from_trace = MagicMock(return_value=rebuilt)
    ws._draft_dir = lambda *_args: draft.parent
    ws.clean_draft_dir = MagicMock(wraps=ws.clean_draft_dir)
    nat.turn.reset_mock()
    with pytest.raises(NativeTurnBudgetExhausted):
        _run(eng, ws)
    nat.turn.assert_not_called()
    ws.clean_draft_dir.assert_not_called()
    assert draft.read_text() == "partial draft; not completed\n"
    assert any(event == "resumed_from_trace" for _category, event, _payload in events)


@pytest.mark.parametrize("partial", [False, True])
def test_output_ceiling_stops_without_retry_or_delivery(budget_case, partial):
    from core.ai_router import OUTPUT_CAP_CEILING
    eng, ws, draft, events = budget_case
    nat = _wire_real_escalation(eng.factory.get_native_agent.return_value, OUTPUT_CAP_CEILING)
    emitted = []
    eng._emit = lambda event, payload=None: emitted.append(event)
    nat.turn.side_effect = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [
        _turn(reasoning="retained ceiling reasoning", truncated=True)]
    with pytest.raises(MaxRetriesExceeded, match="output cap.*explicit attention required"):
        _run(eng, ws)
    assert nat.turn.call_count == (2 if partial else 1)
    assert draft.exists() is partial
    if partial:
        assert draft.read_text() == "partial draft; not completed\n"
    assert not (ws.get_code_path("default") / "partial.gd").exists()
    names = [event for _category, event, _payload in events]
    assert names[-3:] == ["output_cap_starved", "output_cap_ceiling", "output_cap_exhausted"]
    failed = events[-1][2]
    assert failed["attempt"] == 1 and failed["written_files"] == (["partial.gd"] if partial else [])
    assert "step_done" not in emitted and "files_written" not in emitted
    eng.factory.get_agent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_output_ceiling_driver_has_no_confirm_or_retry(budget_case, monkeypatch, partial):
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode, Transition
    from core.run_driver import _step
    from core.ai_router import OUTPUT_CAP_CEILING
    import aitelier.runner
    eng, ws, draft, events = budget_case
    nat = _wire_real_escalation(eng.factory.get_native_agent.return_value, OUTPUT_CAP_CEILING)
    nat.turn.side_effect = ([_turn(tool_calls=[_tc("edit")])] if partial else []) + [_turn(truncated=True)]
    sf = SkillFlow(":memory:")
    sf.register_graph(PipelineGraph(name="output_guard", begin="t_impl", steps=[
        StepNode(id="t_impl", step_type="agent", lifecycle={"on_deliver": {"tool": "repo_apply"}}, transitions=[Transition(to="test")]),
        StepNode(id="test", step_type="tool", tool_name="never_run_tests", transitions=[Transition(to="done")]),
        StepNode(id="done", step_type="gate", transitions=[Transition(to=None)])]))
    rid = sf.create_run("output_guard", {"project_id": "default"}, project_id="default")
    sf.start_run(rid)
    confirm = MagicMock(wraps=sf.confirm_step)
    fail = MagicMock(wraps=sf.fail_step)
    monkeypatch.setattr(sf,"confirm_step",confirm)
    monkeypatch.setattr(sf,"fail_step",fail)
    class Runner:
        def __init__(self, **_kwargs): pass
        async def execute(self, _claimed): return _run(eng, ws)
    monkeypatch.setattr(aitelier.runner,"AgentStepRunner",Runner)
    assert await _step(sf,None,ws,rid,False,5) == "failed"
    confirm.assert_not_called()
    assert fail.call_count == 1 and fail.call_args.kwargs["retryable"] is False
    assert nat.turn.call_count == (2 if partial else 1)
    assert sf.get_run(rid)["current_node"] == "t_impl"
    assert draft.exists() is partial
    assert not (ws.get_code_path("default")/"partial.gd").exists()

def test_repeated_grant_is_denied_after_only_a_failed_tool_call(engine):
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine.factory.get_max_tool_turns.return_value = 4
    engine._exec_tool = MagicMock(side_effect=[
        {"status": "granted"}, {"error": "old_str not found"},
        {"status": "granted"}, {"status": "ok"},
    ])
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [
        _turn(tool_calls=[_tc("ask_more_turns", {"turns": 6}, "a1")]),
        _turn(tool_calls=[_tc("edit", {"path": "x"}, "e1")]),
        _turn(tool_calls=[_tc("ask_more_turns", {"turns": 6}, "a2")]),
        _turn(tool_calls=[_tc("finish_step", {}, "f1")]),
    ]
    assert _run(engine, ws) is True
    tool_payloads = []
    for call in nat.turn.call_args_list:
        for message in call.kwargs["messages"]:
            if message.get("role") == "tool":
                tool_payloads.append(json.loads(message["content"]))
    assert any(p.get("status") == "denied"
               and "no progress" in p.get("note", "").lower()
               for p in tool_payloads)


def test_native_loop_sends_a_bounded_projection_and_traces_the_reduction(engine):
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine.factory.get_max_tool_turns.return_value = 5
    engine._exec_tool = MagicMock(side_effect=[
        {"content": "a" * 50000},
        {"content": "b" * 50000},
        {"written": "main.py"},
        {"status": "ok"},
    ])
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [
        _turn(reasoning="r" * 20000,
              tool_calls=[_tc("read_file", {"path": "a"}, "r1")]),
        _turn(reasoning="s" * 20000,
              tool_calls=[_tc("read_file", {"path": "b"}, "r2")]),
        _turn(tool_calls=[_tc("write", {"file": "main.py"}, "w1")]),
        _turn(tool_calls=[_tc("finish_step", {}, "f1")]),
    ]
    events = []
    engine._trace_cb = lambda category, event, payload: events.append(
        (category, event, payload))
    assert _run(engine, ws) is True

    third_prompt = nat.turn.call_args_list[2].kwargs["messages"]
    first_read = next(m for m in third_prompt
                      if m.get("tool_call_id") == "r1")
    assert json.loads(first_read["content"])["_aitelier_compacted"] is True
    assert any(event == "prompt_projection"
               and payload["projected_chars"] < payload["original_chars"]
               for _category, event, payload in events)
