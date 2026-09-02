"""A role climbs the output-cap ladder once per process, not once per claim.

Measured on jinyong-r3b 2026-09-02: 18 turns produced reasoning only and no
tool call, because `max_tokens` bounds reasoning AND output together on these
providers and each turn's `reasoning_tokens` landed right at the configured
cap. `t_impl` accounted for 9 of them while its 24-turn budget was already the
binding constraint (12 of 28 instances hit `turn_budget_exhausted`). The
gateway is rebuilt per claim, so every card re-discovered the same wall and
paid a turn for it.
"""
import pytest

from core import agents as agents_mod
from core.agents import AgentFactory, remember_output_cap


@pytest.fixture(autouse=True)
def _clean_store():
    agents_mod._LEARNED_OUTPUT_CAPS.clear()
    yield
    agents_mod._LEARNED_OUTPUT_CAPS.clear()


class TestTheStore:
    def test_it_only_ever_raises(self):
        remember_output_cap("r", 65536)
        remember_output_cap("r", 8192)          # a later, smaller escalation
        assert agents_mod._LEARNED_OUTPUT_CAPS["r"] == 65536

    def test_it_ignores_empty_inputs(self):
        remember_output_cap("", 4096)
        remember_output_cap("r", 0)
        assert agents_mod._LEARNED_OUTPUT_CAPS == {}


class TestTheNextClaimStartsThere:
    """Behaviour: build the gateway twice for one role and read the cap."""

    def _factory(self, cap):
        from skillflow.agent_registry import AgentRegistry
        reg = AgentRegistry()
        reg.register("impl", model="deepseek/deepseek-v4-flash",
                     template="task_implementer.md", tools=["read_file"],
                     max_output_tokens=cap)
        return AgentFactory(registry=reg)

    def test_without_a_learned_cap_the_config_wins(self):
        assert self._factory(20000)._build_gateway("impl").max_output_tokens == 20000

    def test_a_learned_cap_carries_into_the_next_claim(self):
        f = self._factory(20000)
        assert f._build_gateway("impl").max_output_tokens == 20000
        remember_output_cap("impl", 32768)      # what escalation records
        assert f._build_gateway("impl").max_output_tokens == 32768

    def test_a_raised_config_still_wins_over_a_lower_learned_cap(self):
        # An edit to agent_configs must not be capped by yesterday's discovery.
        remember_output_cap("impl", 16384)
        assert self._factory(32768)._build_gateway("impl").max_output_tokens == 32768

    def test_the_learned_cap_is_per_role(self):
        remember_output_cap("impl", 65536)
        from skillflow.agent_registry import AgentRegistry
        reg = AgentRegistry()
        reg.register("other", model="deepseek/deepseek-v4-flash",
                     template="task_implementer.md", tools=["read_file"],
                     max_output_tokens=8192)
        assert AgentFactory(registry=reg)._build_gateway("other").max_output_tokens == 8192


def test_the_escalation_site_records_it():
    # The store is useless unless the one place that escalates calls it.
    import inspect
    from core.dpe_pipeline import PipelineEngine
    src = inspect.getsource(PipelineEngine)
    assert "remember_output_cap(" in src
    # Anchor on the ASSIGNMENT, not the first textual mention — the comment
    # block above the call site also says "escalate_output_cap()", and the
    # first version of this test measured its distance from the comment.
    i = src.index("escalated = agent.gateway.escalate_output_cap()")
    assert "remember_output_cap(" in src[i:i + 900], \
        "recorded too far from the escalation to be on that path"
