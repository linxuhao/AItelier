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
            "artifact_requirement": "src/a.py", "dependencies": [], "task_type": "normal"}

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
