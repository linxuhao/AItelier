"""The forge gate must reject a `validation:` mapping.

`gen_dsh_code_review` shipped `validation: {type: file_exists, files: [...]}` on
four steps, passed all three structural gates, and then died on its drive with
`'str' object has no attribute 'get'` — skillflow's StepValidator iterates the
specs it is handed and calls `.get()` on each, so a mapping yields its string
KEYS. The failure lands AFTER the agent has produced its output, so every retry
re-runs the whole review.

`_write_steps_without_validation` could not catch it: a mapping is truthy, so the
step read as validated.
"""
from aitelier.tools.forge_registry_check.impl import (
    RULES, _validation_is_a_spec_list, _write_steps_without_validation)


def test_the_mapping_form_is_rejected():
    out = _validation_is_a_spec_list(
        [{"id": "correctness_review",
          "validation": {"type": "file_exists", "files": ["findings.json"]}}])
    assert len(out) == 1
    assert "correctness_review" in out[0]
    # The remedy must be the actual corrected YAML, not a restatement.
    assert "- files: ['findings.json']" in out[0]
    assert "tool: file_exists" in out[0]


def test_the_list_form_passes():
    assert _validation_is_a_spec_list(
        [{"id": "s", "validation": [{"files": ["v.json"], "tool": "file_exists"}]}]) == []


def test_a_spec_that_says_type_instead_of_tool_is_named():
    out = _validation_is_a_spec_list(
        [{"id": "s", "validation": [{"files": ["v.json"], "type": "file_exists"}]}])
    assert len(out) == 1
    assert "the key is `tool`" in out[0]


def test_a_step_with_no_validation_is_not_this_check_s_business():
    # _write_steps_without_validation owns that question; two checks firing on
    # one step would hand the emitter the same fix twice.
    assert _validation_is_a_spec_list([{"id": "s"}]) == []


def test_the_older_check_really_was_blind_to_it():
    """Why this check had to exist separately, pinned so it cannot silently
    become redundant (or silently stop being needed)."""
    step = {"id": "s", "step_type": "agent", "output": {"mode": "write"},
            "validation": {"type": "file_exists", "files": ["v.json"]}}
    assert _write_steps_without_validation([step]) == []
    assert _validation_is_a_spec_list([step])


def test_the_rule_is_taught():
    rule = next((r for r in RULES if r.id == "validation_is_a_spec_list"), None)
    assert rule is not None, "the gate enforces it; the palette must teach it"
    assert "tool" in rule.teaches and "mapping" in rule.teaches.lower()
