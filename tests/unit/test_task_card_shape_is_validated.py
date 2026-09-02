"""A PM task card without `artifact_requirement` must not validate.

step3_pm.md marks the field 必填 and task_implementer.md tells the implementer
to take its write paths from it — yet step "3" validated a nine-card breakdown
with the field missing from every card (jinyong-numbers, 2026-09-02, the PM
re-plan squeezed by its turn budget). The only card checks were existence,
manifest completeness and known capabilities. Shape is a contract; check it.
"""
import json
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _step3_card_schema():
    g = yaml.safe_load((_ROOT / "configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    node = next(s for s in g["steps"] if str(s["id"]) == "3")
    specs = [v for v in node.get("validation", [])
             if v.get("tool") == "json_schema" and "tasks/*.json" in v.get("files", [])]
    assert specs, "step 3 has no json_schema validation over tasks/*.json"
    return specs[0]["inline_schema"]


def test_the_card_schema_requires_the_implementers_target_files():
    schema = _step3_card_schema()
    assert "artifact_requirement" in schema["required"]
    assert schema["properties"]["artifact_requirement"].get("minLength", 0) >= 1


def test_id_is_not_required_because_the_filename_is_the_identity():
    # scheduler fix 2f3656f: a card body may omit "id"; requiring it here would
    # reject every such card that the scheduler was just taught to accept.
    assert "id" not in _step3_card_schema()["required"]


def test_optional_fields_stay_optional():
    req = set(_step3_card_schema()["required"])
    assert "interface_contract" not in req and "capabilities" not in req


class TestTheSchemaActuallyBites:
    """Run the real validator tool over real files, not just read the YAML."""

    def _validate(self, tmp_path, card: dict) -> dict:
        from skillflow.step_validation import StepValidator
        from api.dependencies import get_tool_loader
        d = tmp_path / "tasks"; d.mkdir()
        (d / "x.json").write_text(json.dumps(card), encoding="utf-8")
        spec = {"files": ["tasks/*.json"], "tool": "json_schema",
                "inline_schema": _step3_card_schema()}
        return StepValidator(get_tool_loader(), tmp_path, config_name="dpe_default").validate([spec])

    GOOD = {"description": "d", "detailed_requirements": "r",
            "artifact_requirement": "src/a.py", "dependencies": [], "task_type": "normal",
            "acceptance": "pytest tests/test_a.py: 0 red", "owns": ["src/a.py"],
            "stop_conditions": "a measured number contradicts the card", "evidence": "pytest output"}

    def test_a_card_that_owns_nothing_fails(self, tmp_path):
        assert self._validate(tmp_path, {**self.GOOD, "owns": []})["passed"] is False

    def test_a_complete_card_passes(self, tmp_path):
        assert self._validate(tmp_path, self.GOOD)["passed"] is True

    def test_a_card_without_artifact_requirement_fails(self, tmp_path):
        bad = {k: v for k, v in self.GOOD.items() if k != "artifact_requirement"}
        assert self._validate(tmp_path, bad)["passed"] is False

    def test_an_empty_artifact_requirement_fails(self, tmp_path):
        assert self._validate(tmp_path, {**self.GOOD, "artifact_requirement": ""})["passed"] is False

    def test_a_card_without_id_still_passes(self, tmp_path):
        assert "id" not in self.GOOD
        assert self._validate(tmp_path, self.GOOD)["passed"] is True


def test_the_card_is_self_contained():
    """workflow.games' 12-section requirement, borrowed 2026-09-03: acceptance,
    ownership, stop rules and closing evidence live ON the card."""
    req = set(_step3_card_schema()["required"])
    assert {"acceptance", "owns", "stop_conditions", "evidence"} <= req
    props = _step3_card_schema()["properties"]
    assert props["owns"]["type"] == "array" and props["owns"]["minItems"] == 1
    assert "shared_hotspots" not in req and "forbidden" not in req


def _step5_report_schema():
    g = yaml.safe_load((_ROOT / "configs" / "dpe_default.yaml").read_text(encoding="utf-8"))
    node = next(s for s in g["steps"] if str(s["id"]) == "5")
    specs = [v for v in node.get("validation", [])
             if v.get("tool") == "json_schema" and "final/verify_report.json" in v.get("files", [])]
    assert specs, "step 5 has no json_schema validation over final/verify_report.json"
    return specs[0]["inline_schema"]


def test_the_verdict_names_a_status_per_goal_from_a_closed_set():
    schema = _step5_report_schema()
    assert "goals" in schema["required"]
    item = schema["properties"]["goals"]["items"]
    assert set(item["required"]) == {"goal", "status", "evidence"}
    assert item["properties"]["status"]["enum"] == ["met", "partial", "unmet", "blocked"]


def test_a_report_with_a_status_outside_the_set_is_rejected():
    import jsonschema
    schema = _step5_report_schema()
    good = {"all_goals_met": False, "ready_for_deploy": False, "issues": [],
            "goals": [{"goal": "g", "status": "blocked", "evidence": "vision_report.json not yet produced"}]}
    jsonschema.validate(good, schema)
    bad = dict(good, goals=[{"goal": "g", "status": "probably fine", "evidence": "looks right"}])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
