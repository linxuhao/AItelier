"""One-time hardening for persisted generated Godot release pipelines."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from core.output_migration import write_migrated_config

TEST_GATE = json.dumps([["5_test", "test_report.json"]])
COMPILE_GATES = json.dumps([
    ["5_compile", "compile_report.json"],
    ["5_compile", "playtest_report.json"],
])
PRE_FINAL_GATES = json.dumps([
    ["5_test", "test_report.json"],
    ["5_compile", "compile_report.json"],
    ["5_compile", "playtest_report.json"],
    ["5_vision", "vision_report.json"],
])


def _set(params: dict, key: str, value, changes: list[dict], step: str) -> None:
    if params.get(key) == value:
        return
    params[key] = value
    changes.append({"step": step, "parameter": key, "value": value})


def migrate_release_document(document: dict) -> list[dict]:
    """Give a generated game graph the same single-owner fail-fast contract.

    The shape is intentionally narrow. A model-generated pipeline with different
    gate ids needs an explicit review; guessing which arbitrary test node owns a
    release would be less safe than leaving it unchanged.
    """
    if not isinstance(document, dict):
        return []
    steps = {step.get("id"): step for step in document.get("steps", [])
             if isinstance(step, dict) and step.get("id")}
    required = {
        "5_test": "run_tests", "5_compile": "godot_compile",
        "5_vision": "godot_vision", "5_final_test": "run_tests",
    }
    if any(steps.get(step, {}).get("tool_name") != tool
           for step, tool in required.items()):
        return []

    changes: list[dict] = []
    # game_harness's godot_compile owns the repo's full compile/playtest gate.
    # run_tests keeps pytest/npm ownership but must not shell run_tests.sh too.
    for step in document["steps"]:
        if not isinstance(step, dict) or step.get("tool_name") != "run_tests":
            continue
        params = step.setdefault("tool_params", {})
        _set(params, "repo_gate", False, changes, str(step.get("id")))

    test_params = steps["5_test"].setdefault("tool_params", {})
    _set(test_params, "evidence_cycle_start", True, changes, "5_test")

    compile_params = steps["5_compile"].setdefault("tool_params", {})
    _set(compile_params, "evidence_cycle_from", "5_test", changes, "5_compile")
    _set(compile_params, "fail_fast_gates", TEST_GATE, changes, "5_compile")

    vision_params = steps["5_vision"].setdefault("tool_params", {})
    _set(vision_params, "evidence_cycle_from", "5_test", changes, "5_vision")
    _set(vision_params, "fail_fast_gates", COMPILE_GATES, changes, "5_vision")

    final_params = steps["5_final_test"].setdefault("tool_params", {})
    _set(final_params, "evidence_cycle_from", "5_test", changes, "5_final_test")
    _set(final_params, "fail_fast_gates", PRE_FINAL_GATES, changes, "5_final_test")

    transitions = steps["5_vision"].setdefault("transitions", [])
    false_edge = {"to": "5_final_test", "match": {
        "from_file": "vision_report.json", "field": "passed", "value": False}}
    if false_edge not in transitions:
        # A blind report is also passed:false, but it must retain its human
        # checkpoint. Put this edge after the blind branch and before fallback.
        at = next((i + 1 for i, edge in enumerate(transitions)
                   if edge.get("match", {}).get("field") == "blind"), None)
        if at is None:
            at = next((i for i, edge in enumerate(transitions)
                       if not edge.get("match")), len(transitions))
        transitions.insert(at, false_edge)
        changes.append({"step": "5_vision", "transition": false_edge})
    return changes


def migrate_generated_release_gates(config_dir: Path) -> list[dict]:
    """Atomically migrate saved generated game configs, retaining exact bytes."""
    from skillflow.graph import PipelineGraph
    from skillflow.output_targets import atomic_json

    config_dir = Path(config_dir)
    backup_dir = config_dir.parent / "migration_backups" / "release-fail-fast-v2"
    reports = []
    for path in sorted(config_dir.glob("gen_*.yaml")):
        original = path.read_bytes()
        try:
            document = yaml.safe_load(original)
        except (yaml.YAMLError, UnicodeError):
            continue
        changes = migrate_release_document(document)
        if not changes:
            continue
        PipelineGraph._from_dict(document)
        rendered = yaml.safe_dump(document, allow_unicode=True,
                                  sort_keys=False).encode()
        backup = write_migrated_config(path, original, rendered, backup_dir)
        reports.append({
            "path": str(path), "backup": str(backup),
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "after_sha256": hashlib.sha256(rendered).hexdigest(),
            "changes": changes,
        })
    if reports:
        atomic_json(backup_dir / "last-migration.json", {
            "configs": reports,
            "note": ("Only saved config files changed; pinned historical run "
                     "graph versions were not repointed."),
        })
    return reports
