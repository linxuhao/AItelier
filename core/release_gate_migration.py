"""One-time hardening for the exact persisted generated Godot pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path

import yaml

from core.output_migration import write_migrated_config

_LOG = logging.getLogger(__name__)

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
ALL_GATES = json.dumps([
    ["5_test", "test_report.json"],
    ["5_compile", "compile_report.json"],
    ["5_compile", "playtest_report.json"],
    ["5_vision", "vision_report.json"],
    ["5_final_test", "test_report.json"],
])

_NO_CAPTURES = {"to": "5_final_test", "match": {
    "from_file": "vision_report.json", "field": "blind_reason",
    "value": "no_captures"}}
_BLIND = {"to": "5_vision_human", "match": {
    "from_file": "vision_report.json", "field": "blind", "value": True}}
_R2_VISION_FAILURE = {"to": "5_final_test", "match": {
    "from_file": "vision_report.json", "field": "passed", "value": False}}


def _edge(to: str, *, field: str | None = None, value=None,
          max_loop: int | None = None) -> dict:
    edge: dict = {"to": to}
    if field is not None:
        edge["match"] = {"field": field, "value": value}
    if max_loop is not None:
        edge["max_loop"] = max_loop
    return edge


_TARGET_COMPILE = [
    _edge("5_vision", field="release_evidence", value="known_failure"),
    _edge("5_vision", field="release_evidence", value="passed"),
    _edge("5_release_wait"),
]
_TARGET_VISION = [
    _NO_CAPTURES,
    _BLIND,
    _edge("5_final_test", field="release_evidence", value="known_failure"),
    _edge("5_knowledge", field="release_evidence", value="passed"),
    _edge("5_release_wait"),
]
_TARGET_FINAL = [
    _edge("5_review", field="release_evidence", value="passed"),
    _edge("5_final_test_replan", field="release_evidence",
          value="known_failure"),
    _edge("5_release_wait"),
]
_TARGET_WAIT = [{"to": "5_test", "match": {
    "from": "checkpoint", "value": "approved"}, "max_loop": 4}]

_REQUIRED_STEPS = {
    "3": ("agent", None),
    "5_test": ("tool", "run_tests"),
    "5_compile": ("tool", "godot_compile"),
    "5_vision": ("tool", "godot_vision"),
    "5_vision_human": ("tool", "restage"),
    "5_vision_judged": ("tool", "vision_human_pass"),
    "5_knowledge": ("tool", "knowledge_sync"),
    "5_design": ("agent", None),
    "5_final_test": ("tool", "run_tests"),
    "5_final_test_replan": ("gate", None),
    "5_review": ("agent", None),
}
_GATE_TOOLS = {"run_tests", "godot_compile", "godot_playtest", "godot_vision"}


def _set(params: dict, key: str, value, changes: list[dict], step: str) -> None:
    if params.get(key) == value:
        return
    params[key] = value
    changes.append({"step": step, "parameter": key, "value": value})


def _same_edges(actual, expected) -> bool:
    return isinstance(actual, list) and actual == expected


def _tool_grants(value, path):
    """Normalize the documented string-list grant; reject other iterables.

    SkillFlow 1.5.75 also iterates mapping keys at claim and execution time, so
    a mapping is executable despite being outside the documented contract.
    Reject it rather than treating unknown values as harmless metadata.
    """
    if not isinstance(value, list):
        shape = "mapping keys are executable" if isinstance(value, dict) \
            else f"got {type(value).__name__}"
        raise ValueError(
            f"release gate tool grants at {path!r} must be a list of "
            f"non-empty strings ({shape})")
    if not all(isinstance(tool, str) and tool for tool in value):
        raise ValueError(
            f"release gate tool grants at {path!r} must be a list of "
            "non-empty strings")
    yield from ((path + (index,), tool) for index, tool in enumerate(value))


def _gate_invocations(value, path=()):
    """Yield every executable gate reference, including nested hook arrays."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (key,)
            if (key in {"tool", "tool_name"} and isinstance(child, str)
                    and child in _GATE_TOOLS):
                yield child_path, child
            if key == "extra_tools":
                for tool_path, tool in _tool_grants(child, child_path):
                    if tool in _GATE_TOOLS:
                        yield tool_path, tool
            yield from _gate_invocations(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _gate_invocations(child, path + (index,))


def _role_gate_invocations(document: dict, roles: dict | None):
    """Yield gate tools granted by paired roles used by this graph."""
    if not isinstance(roles, dict):
        return
    used = {
        step.get("agent_config")
        for step in document.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("agent_config"), str)
    }
    for role in sorted(used):
        if role not in roles:
            continue
        config = roles[role]
        if not isinstance(config, dict):
            raise ValueError(f"release gate paired role {role!r} must be a mapping")
        if "tools" not in config:
            continue
        path = ("roles", role, "tools")
        for tool_path, tool in _tool_grants(config["tools"], path):
            if tool in _GATE_TOOLS:
                yield tool_path, tool


def release_gate_ownership_error(document: dict, roles: dict | None = None) -> str:
    """Return why a release-shaped graph has an unsafe executable gate owner."""
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        return ""
    raw_steps = document["steps"]
    if not all(isinstance(step, dict) and isinstance(step.get("id"), str)
               and step["id"] for step in raw_steps):
        return ""
    ids = [step["id"] for step in raw_steps]
    if not set(_REQUIRED_STEPS).issubset(ids):
        return ""
    if len(ids) != len(set(ids)):
        return "release gate graph has duplicate step ids"
    steps = {step["id"]: step for step in raw_steps}
    wrong_types = [
        step_id for step_id, (step_type, tool) in _REQUIRED_STEPS.items()
        if (steps[step_id].get("step_type") != step_type
            or (tool is not None and steps[step_id].get("tool_name") != tool))
    ]
    if wrong_types:
        return f"release gate steps have wrong type/tool: {wrong_types}"
    if roles is not None and not isinstance(roles, dict):
        return "release gate paired roles must be a mapping"

    indices = {step["id"]: index for index, step in enumerate(raw_steps)}
    expected = {
        (("steps", indices["5_test"], "tool_name"), "run_tests"),
        (("steps", indices["5_compile"], "tool_name"), "godot_compile"),
        (("steps", indices["5_vision"], "tool_name"), "godot_vision"),
        (("steps", indices["5_final_test"], "tool_name"), "run_tests"),
    }
    try:
        actual = set(_gate_invocations(document))
        role_grants = set(_role_gate_invocations(document, roles))
    except ValueError as exc:
        return str(exc)
    if actual != expected or role_grants:
        extra = sorted(actual - expected) + sorted(role_grants)
        missing = sorted(expected - actual)
        return ("release gate ownership mismatch; "
                f"unexpected={extra}, missing={missing}")
    return ""


def _source_shape(document: dict, roles: dict | None = None
                  ) -> tuple[dict[str, dict], bool] | None:
    """Accept only the complete original, r2, or r3 generated-game topology."""
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        return None
    raw_steps = document["steps"]
    if not all(isinstance(step, dict) and isinstance(step.get("id"), str)
               and step["id"] for step in raw_steps):
        return None
    ids = [step["id"] for step in raw_steps]
    if len(ids) != len(set(ids)):
        return None
    steps = {step["id"]: step for step in raw_steps}
    if any(step_id not in steps for step_id in _REQUIRED_STEPS):
        return None
    if release_gate_ownership_error(document, roles):
        return None

    for step_id in ("5_test", "5_compile", "5_vision", "5_final_test"):
        if not isinstance(steps[step_id].get("tool_params", {}), dict):
            return None
    if not all(isinstance(step.get("transitions", []), list)
               and all(isinstance(edge, dict)
                       for edge in step.get("transitions", []))
               for step in raw_steps):
        return None

    base_routes = (
        _same_edges(steps["5_test"].get("transitions"), [{"to": "5_compile"}])
        and _same_edges(steps["5_vision_human"].get("transitions"), [{
            "match": {"from": "checkpoint", "value": "approved"},
            "to": "5_vision_judged"}])
        and _same_edges(steps["5_vision_judged"].get("transitions"), [
            {"to": "5_knowledge"}])
        and _same_edges(steps["5_knowledge"].get("transitions"), [
            {"to": "5_design"}])
        and _same_edges(steps["5_design"].get("transitions"), [
            {"match": {"_error": True}, "to": "5_final_test"},
            {"to": "5_final_test"}])
        and _same_edges(steps["5_final_test_replan"].get("transitions"), [
            {"max_loop": 4, "to": "3"}])
    )
    if not base_routes:
        return None

    old_vision = [_NO_CAPTURES, _BLIND, {"to": "5_knowledge"}]
    r2_vision = [_NO_CAPTURES, _BLIND, _R2_VISION_FAILURE,
                 {"to": "5_knowledge"}]
    old_final = [
        {"match": {"field": "skipped", "from_file": "test_report.json",
                   "value": True}, "to": "5_final_test_replan"},
        {"match": {"field": "no_tests_collected",
                   "from_file": "test_report.json", "value": True},
         "to": "5_final_test_replan"},
        {"match": {"field": "passed", "from_file": "test_report.json",
                   "value": True}, "to": "5_review"},
        {"to": "5_final_test_replan"},
    ]
    has_wait = "5_release_wait" in steps
    old_or_r2 = (
        not has_wait
        and _same_edges(steps["5_compile"].get("transitions"),
                        [{"to": "5_vision"}])
        and (steps["5_vision"].get("transitions") in (old_vision, r2_vision))
        and _same_edges(steps["5_final_test"].get("transitions"), old_final)
    )
    target = (
        has_wait
        and _same_edges(steps["5_compile"].get("transitions"), _TARGET_COMPILE)
        and _same_edges(steps["5_vision"].get("transitions"), _TARGET_VISION)
        and _same_edges(steps["5_final_test"].get("transitions"), _TARGET_FINAL)
        and steps["5_release_wait"].get("step_type") == "tool"
        and steps["5_release_wait"].get("tool_name") == "verify_evidence"
        and steps["5_release_wait"].get("checkpoint") is True
        and steps["5_release_wait"].get("checkpoint_reject_to") == "3"
        and isinstance(steps["5_release_wait"].get("tool_params"), dict)
        and _same_edges(steps["5_release_wait"].get("transitions"), _TARGET_WAIT)
    )
    if not (old_or_r2 or target):
        return None
    return steps, has_wait


def migrate_release_document(document: dict, roles: dict | None = None) -> list[dict]:
    """Transactionally migrate only the complete generated-game graph shape."""
    from skillflow.graph import GraphResolver, PipelineGraph

    try:
        PipelineGraph._from_dict(document)
    except Exception:
        return []
    source = _source_shape(document, roles)
    if source is None:
        return []

    candidate = copy.deepcopy(document)
    candidate_source = _source_shape(candidate, roles)
    if candidate_source is None:
        return []
    steps, has_wait = candidate_source
    changes: list[dict] = []

    for step_id in ("5_test", "5_final_test"):
        _set(steps[step_id]["tool_params"], "repo_gate", False,
             changes, step_id)
    _set(steps["5_test"]["tool_params"], "evidence_cycle_start", True,
         changes, "5_test")
    _set(steps["5_compile"]["tool_params"], "evidence_cycle_from", "5_test",
         changes, "5_compile")
    _set(steps["5_compile"]["tool_params"], "fail_fast_gates", TEST_GATE,
         changes, "5_compile")
    _set(steps["5_vision"]["tool_params"], "evidence_cycle_from", "5_test",
         changes, "5_vision")
    _set(steps["5_vision"]["tool_params"], "fail_fast_gates", COMPILE_GATES,
         changes, "5_vision")
    _set(steps["5_final_test"]["tool_params"], "evidence_cycle_from", "5_test",
         changes, "5_final_test")
    _set(steps["5_final_test"]["tool_params"], "fail_fast_gates",
         PRE_FINAL_GATES, changes, "5_final_test")

    for step_id, transitions in (
        ("5_compile", _TARGET_COMPILE),
        ("5_vision", _TARGET_VISION),
        ("5_final_test", _TARGET_FINAL),
    ):
        if steps[step_id]["transitions"] != transitions:
            steps[step_id]["transitions"] = copy.deepcopy(transitions)
            changes.append({"step": step_id, "transitions": transitions})

    if not has_wait:
        wait = {
            "id": "5_release_wait",
            "step_type": "tool",
            "tool_name": "verify_evidence",
            "timeout_seconds": 120,
            "tool_params": {"out_dir": "$STEP_DIR", "gates": ALL_GATES},
            "checkpoint": True,
            "checkpoint_label": (
                "Release evidence unresolved — retry verification after recovery"),
            "checkpoint_reject_to": "3",
            "transitions": copy.deepcopy(_TARGET_WAIT),
        }
        candidate["steps"].append(wait)
        changes.append({"step": "5_release_wait", "added": True})
    else:
        _set(steps["5_release_wait"]["tool_params"], "out_dir", "$STEP_DIR",
             changes, "5_release_wait")
        _set(steps["5_release_wait"]["tool_params"], "gates", ALL_GATES,
             changes, "5_release_wait")

    try:
        graph = PipelineGraph._from_dict(candidate)
        if GraphResolver(graph).validate():
            return []
    except Exception:
        return []
    if not changes:
        return []
    document.clear()
    document.update(candidate)
    return changes


def migrate_generated_release_gates(config_dir: Path) -> list[dict]:
    """Atomically migrate each valid saved graph; isolate malformed files."""
    from skillflow.output_targets import atomic_json

    config_dir = Path(config_dir)
    backup_dir = config_dir.parent / "migration_backups" / "release-fail-fast-v3"
    reports = []
    for path in sorted(config_dir.glob("gen_*.yaml")):
        try:
            original = path.read_bytes()
            document = yaml.safe_load(original)
            roles = None
            roles_path = path.with_suffix(".roles.json")
            if roles_path.exists():
                roles = json.loads(roles_path.read_text(encoding="utf-8"))
                if not isinstance(roles, dict):
                    raise ValueError("generated roles file is not a mapping")
            changes = migrate_release_document(document, roles=roles)
            if not changes:
                continue
            rendered = yaml.safe_dump(document, allow_unicode=True,
                                      sort_keys=False).encode()
            backup = write_migrated_config(path, original, rendered, backup_dir)
        except Exception as exc:
            _LOG.warning("skipping release migration for %s: %s", path.name, exc)
            continue
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
