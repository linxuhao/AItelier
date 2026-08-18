# tests/unit/test_agent_config_budgets.py
# Guards the output budget of every shipped agent config.
#
# DeepSeek counts reasoning inside max_tokens — completion_tokens includes
# reasoning_tokens and there is no separate reasoning budget parameter — so a
# thinking role with a tight cap can spend the entire budget on
# reasoning_content and return finish_reason=length with no text and no tool
# call. That is how task_implementer_reviewer burned ten turns at exactly 4096
# and never emitted create_verdict: the review silently did not happen.

import glob
import os

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "agent_configs")

# AIGateway's default when a role omits max_output_tokens (core/agents.py).
DEFAULT_MAX_OUTPUT_TOKENS = 8192

# Measured on deepseek-v4-flash with a reviewer-shaped prompt: default-effort
# reasoning alone reached ~6.8k tokens, and reviewer reasoning in the run traces
# reaches ~18k chars (~6k tokens) at p99. Anything under this leaves no room for
# the tool call the step exists to make.
MIN_THINKING_BUDGET = 8192


def _roles():
    for path in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        for name, cfg in doc.items():
            if isinstance(cfg, dict):
                yield os.path.basename(path), name, cfg


def _thinking(cfg):
    t = cfg.get("thinking")
    return t if isinstance(t, dict) and t.get("enable") else None


def test_thinking_roles_have_room_for_output_after_reasoning():
    offenders = [
        f"{f}:{name} max_output_tokens="
        f"{cfg.get('max_output_tokens', DEFAULT_MAX_OUTPUT_TOKENS)}"
        for f, name, cfg in _roles()
        if _thinking(cfg)
        and cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS) < MIN_THINKING_BUDGET
    ]
    assert not offenders, (
        "thinking roles whose cap can be consumed entirely by reasoning, "
        f"leaving no tokens for the tool call: {offenders}"
    )


def test_dpe_reviewers_pin_an_explicit_reasoning_effort():
    """Unset effort means the DeepSeek server default (~high), which is what
    starved these steps. The level must stay pinned, not drift back."""
    with open(os.path.join(CONFIG_DIR, "dpe_default.yaml"), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    reviewers = [n for n in doc if n.endswith("_reviewer")]
    assert reviewers, "dpe_default.yaml lost its reviewer roles"
    for name in reviewers:
        thinking = _thinking(doc[name])
        assert thinking and thinking.get("effort"), (
            f"{name} enables thinking without pinning thinking.effort"
        )


def test_task_planner_starts_small_and_can_grow():
    """The planner is sized to ESCALATE, not to be provisioned for its worst case.

    Handing it task_implementer's 32768 up front made it write a longer essay,
    not a better plan: on `boltons` t_plan burned 39% of the run and emitted
    1.2 MB of markdown for 1.0 MB of delivered Python. It is back at 16384,
    which is safe only because a starved turn doubles the cap for the retry
    (core/dpe_pipeline.py:_run_native_step → AIGateway.escalate_output_cap).
    Two invariants keep that true: the cap must sit BELOW the escalation ceiling
    (at or above it, escalate_output_cap() declines and a starve is terminal),
    and one doubling must reach the maker-sized budget the planner needs on the
    rare task that really is that big.
    """
    from core.ai_router import OUTPUT_CAP_CEILING

    with open(os.path.join(CONFIG_DIR, "dpe_default.yaml"), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    planner = doc["task_planner"].get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    implementer = doc["task_implementer"].get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    assert planner < OUTPUT_CAP_CEILING, (
        f"task_planner max_output_tokens={planner} is at the escalation ceiling "
        f"({OUTPUT_CAP_CEILING}); a starved turn could not be retried any wider"
    )
    assert planner * 2 >= implementer, (
        f"task_planner max_output_tokens={planner} cannot reach task_implementer's "
        f"{implementer} in one escalation step"
    )


def test_task_planner_turn_budget_is_not_the_knob():
    """Measured: t_plan instances that hit the 10-turn ceiling produced complete
    4-slot output 24% of the time vs 54% for shorter ones. More turns correlate
    with WORSE output, so starvation is fixed with tokens per turn, never by
    handing the planner more turns.

    Tightened to 6 from the `boltons` run: 312 turns over 42 executions, with 51
    of 91 web_fetch calls repeating an already-fetched URL and 86% of all reads
    re-reading an already-read path — the non-redundant exploration is ~2 turns.
    A task that genuinely needs more calls ask_more_turns."""
    with open(os.path.join(CONFIG_DIR, "dpe_default.yaml"), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    assert doc["task_planner"]["max_tool_turns"] <= 6
