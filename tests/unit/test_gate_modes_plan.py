"""Pin the gate composition: targeted tests/docs and product-code refusal."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gate_modes", ROOT / "scripts" / "gate_modes.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

DESELECTORS = {"-m", "--deselect", "--ignore", "--ignore-glob", "-k"}


def has_deselectors(plan: dict) -> bool:
    """`-m` at index 1 is Python's module flag, not a pytest marker filter."""
    for entry in plan["jobs"]:
        argv = entry["argv"]
        if any(flag in DESELECTORS for flag in argv[2:]):
            return True
    return False


def test_tests_and_docs_diff_gets_targeted_jobs() -> None:
    changed = [
        "tests/unit/test_gate_modes_plan.py",
        "docs/gate-modes.md",
        "tests/integration/test_web_suite_runs.py",
    ]
    plan = gate.build_plan("targeted", changed)
    assert plan["allowed"] is True
    assert [j["name"] for j in plan["jobs"]] == ["affected-tests", "whole-tree-guards"]
    affected = plan["jobs"][0]["argv"]
    assert affected[-2:] == [
        "tests/integration/test_web_suite_runs.py",
        "tests/unit/test_gate_modes_plan.py",
    ] or affected[-2:] == [
        "tests/unit/test_gate_modes_plan.py",
        "tests/integration/test_web_suite_runs.py",
    ]
    guards = plan["jobs"][1]["argv"]
    assert all(path in guards for path in gate.WHOLE_TREE_GUARDS)
    assert not has_deselectors(plan)


def test_product_python_change_refuses_targeted_by_name() -> None:
    old = "x = 1\n"
    plan = gate.build_plan(
        "targeted",
        ["core/ai_router.py", "tests/unit/test_gate_modes_plan.py"],
        old_sources={"core/ai_router.py": old},
    )
    assert plan["allowed"] is False
    assert plan["offending_path"] == "core/ai_router.py"
    assert "core/ai_router.py" in plan["reason"]
    assert plan["prescribed_mode"] == "merge"
    assert plan["jobs"] == []


def test_comment_and_docstring_only_python_is_targeted(tmp_path: Path) -> None:
    path = "core/comment_only.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text('"""new prose"""\n# new comment\nx = 1\n', encoding="utf-8")
    old = '"""old prose"""\n# old comment\nx = 1\n'
    plan = gate.build_plan("targeted", [path], root=tmp_path, old_sources={path: old})
    assert plan["allowed"] is True, plan
    assert [j["name"] for j in plan["jobs"]] == ["whole-tree-guards"]


def test_executable_python_change_refuses_targeted(tmp_path: Path) -> None:
    path = "core/behaviour.py"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text("x = 2\n", encoding="utf-8")
    plan = gate.build_plan(
        "targeted", [path], root=tmp_path, old_sources={path: "x = 1\n"}
    )
    assert plan["allowed"] is False
    assert plan["offending_path"] == path
    assert plan["prescribed_mode"] == "merge"


@pytest.mark.parametrize("mode", ["merge", "full"])
def test_merge_and_full_keep_the_whole_selection_and_split_js_jobs(mode: str) -> None:
    plan = gate.build_plan(mode, ["core/ai_router.py"])
    assert plan["allowed"] is True
    assert [j["name"] for j in plan["jobs"]] == [
        "python-tree",
        "driver-node-suite",
        "web-vitest-suite",
    ]
    python_argv = plan["jobs"][0]["argv"]
    assert python_argv == ["python", "-m", "pytest", "tests"]
    assert not has_deselectors(plan)
    assert gate.DRIVER_NODE_SUITE in plan["jobs"][1]["argv"]
    assert gate.WEB_VITEST_SUITE in plan["jobs"][2]["argv"]
