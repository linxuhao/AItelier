"""Regression coverage for early implementation progress and State relays.

The production samples spent 29-37 turns surveying before their first write.
These tests pin the host-side policy that makes that measurable and intervenes
at half budget without changing the configured cap.
"""
import inspect
import json
from pathlib import Path

from core.dpe_pipeline import (
    PipelineEngine,
    _budget_failure_report,
    _relay_acknowledgement,
    _relay_progress_context,
    _should_intervene_early,
)


def relay_context():
    return {
        "instruction": "Finish dpe_pipeline wiring and add targeted tests; do not re-ground.",
        "relay": {
            "run_id": "6e4c67b9-2849-402b-96f4-bd8ddf4386be",
            "error": "native turn budget exhausted (32/32)",
            "code_changes": {"files": {
                "core/dpe_pipeline.py": {"bytes": 195198},
                "core/write_scope.py": {"bytes": 8817},
            }},
        },
    }


def test_half_budget_intervenes_once_for_writable_steps_only():
    assert not _should_intervene_early(
        turn=15, max_turns=32, writable=True, written_files=[], already_intervened=False)
    assert _should_intervene_early(
        turn=16, max_turns=32, writable=True, written_files=[], already_intervened=False)
    assert not _should_intervene_early(
        turn=16, max_turns=32, writable=True, written_files=["a.py"], already_intervened=False)
    assert not _should_intervene_early(
        turn=16, max_turns=32, writable=False, written_files=[], already_intervened=False)
    assert not _should_intervene_early(
        turn=17, max_turns=32, writable=True, written_files=[], already_intervened=True)


def test_reference_failure_is_reported_as_regrounding_with_actual_progress():
    report = _budget_failure_report(
        max_turns=32, first_write_turn=30,
        written_files=["core/dpe_pipeline.py", "core/write_scope.py"],
        reads_searches=49, tool_failures=0, expansion_requests=[], relay=None)
    assert report["first_write_turn"] == 30
    assert report["reads_searches"] == 49
    assert report["written_files"] == ["core/dpe_pipeline.py", "core/write_scope.py"]
    assert report["failure_class"] == "re_grounding"
    assert report["expansion_requested"] is False
    assert report["remaining_delivery"] == ["not reported before exhaustion"]
    assert report["escalation_policy"] == "relay_retained_work_then_split_if_repeated"


def test_repeated_relay_failure_escalates_without_raising_the_cap():
    report = _budget_failure_report(
        max_turns=40, first_write_turn=38, written_files=["tests/test_scope.py"],
        reads_searches=49, tool_failures=0,
        expansion_requests=[{"turn": 30, "asked": 8, "granted": 8,
                             "reason": "finish wiring and tests"}],
        relay=_relay_progress_context(relay_context()))
    assert report["consecutive_budget_failure"] is True
    assert report["first_failure_run_id"].startswith("6e4c67b9")
    assert report["expansion_requested"] is True
    assert report["remaining_delivery"] == ["finish wiring and tests"]
    assert report["escalation_policy"] == "split_or_external_executor"
    assert "max_turns" not in report  # classification never changes the configured cap


def test_state_seed_relay_retains_exact_bytes_and_incomplete_instruction():
    seed = "# State goal attempt\n\n" + json.dumps(relay_context()) + "\n\n## Relay\ncontinue\n"
    found = _relay_progress_context({"coding_impl/plan.md": seed})
    assert found == {
        "run_id": "6e4c67b9-2849-402b-96f4-bd8ddf4386be",
        "retained_files": {
            "core/dpe_pipeline.py": 195198,
            "core/write_scope.py": 8817,
        },
        "retained_bytes": 204015,
        "incomplete_items": [
            "Finish dpe_pipeline wiring and add targeted tests; do not re-ground."],
        "prior_budget_failure": True,
    }


def test_relay_ack_requires_exact_bytes_and_names_remaining_work():
    relay = _relay_progress_context(relay_context())
    ok, denied = _relay_acknowledgement(
        relay, {"retained_bytes": 0, "incomplete_items": ["tests"]})
    assert not ok and denied["status"] == "denied" and "error" in denied
    ok, accepted = _relay_acknowledgement(
        relay, {"retained_bytes": 204015,
                "incomplete_items": ["finish wiring", "add targeted tests"]})
    assert ok and accepted["status"] == "acknowledged"
    assert accepted["retained_files"]["core/write_scope.py"] == 8817


def test_native_loop_refuses_operations_before_relay_ack_and_records_progress():
    source = inspect.getsource(PipelineEngine._run_native_step)
    refusal = source.index('if not relay_acknowledged and tool_name != "acknowledge_relay"')
    execution = source.index('self._exec_tool({"tool": tool_name, "params": params})', refusal)
    assert refusal < execution
    assert '"early_progress_intervention"' in source
    assert '"implementation_first_write"' in source
    assert '"reads_searches": reads_searches' in source
    assert '"relay_broad_survey_refused"' in source
    assert 'reads_searches > relay_read_limit' in source


def test_coding_template_forbids_broad_regrounding_on_relay():
    text = (Path(__file__).parents[2] / "templates" / "coding_impl.md").read_text().lower()
    assert "acknowledge_relay" in text
    assert "do not re-ground" in text
    assert "half the turn budget" in text
