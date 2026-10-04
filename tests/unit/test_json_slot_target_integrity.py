"""Public JSON dispatch preserves official fixed-slot delivery identity."""
import json
from pathlib import Path

import pytest

from core.dpe_pipeline import MaxRetriesExceeded
from skillflow import write_tools
from tests.unit.test_json_delivery_failure import EXTRA_BRACE, action, engine, response


def fixed(kind, pattern):
    return {"doc": pattern if kind == "string" else
            {"file" if kind == "file" else "output": pattern}}


def slot_engine(tmp_path, replies, outputs):
    e = engine(tmp_path, replies)
    e._tool_schemas = {"create_doc": {}, "edit_doc": {}, "finish_step": {}}
    e._output_fixed = outputs
    e.factory.is_native = lambda _: False

    def execute(call):
        e.calls.append(call)
        if call["tool"] == "create_doc":
            return write_tools.execute_create(
                "doc", outputs, call["params"], str(tmp_path))
        return write_tools.execute_edit(
            "doc", outputs, call["params"], str(tmp_path))

    e._exec_tool = execute
    return e


def attempt(e, tmp_path):
    value, error = None, None
    try:
        value = e.run_step(1, "implement", None, "fixture",
            agent_config_name="stub", tool_schemas=e._tool_schemas,
            output_fixed=e._output_fixed, output_dir=str(tmp_path),
            resolved_context={})
    except MaxRetriesExceeded as exc:
        error = str(exc)
    print(json.dumps({"value": value, "error": error, "calls": e.calls,
                     "prompts": e.prompts, "events": e.events,
                     "traces": e.traces,
                     "files": {str(p.relative_to(tmp_path)): p.read_text()
                               for p in tmp_path.rglob("*") if p.is_file()}}))
    return value, error


def create(ident, invalid=False):
    return action("create_doc", id=ident,
                  initialContent="{invalid json" if invalid else {"id": ident, "value": 1})


@pytest.mark.parametrize("kind", ["string", "file", "output"])
@pytest.mark.parametrize("repair_both", [False, True])
def test_each_official_glob_id_requires_its_own_repair(tmp_path, kind, repair_both):
    repairs = [create("a")] + ([create("b")] if repair_both else [])
    e = slot_engine(tmp_path, [EXTRA_BRACE,
        response(create("a", True), create("b", True)), response(*repairs),
        response(action("finish_step"))], fixed(kind, "docs/*.json"))
    value, error = attempt(e, tmp_path)
    assert (value is True) == repair_both
    assert json.loads((tmp_path / "docs/a.json").read_text())["id"] == "a"
    assert (tmp_path / "docs/b.json").exists() == repair_both
    assert e.prompts[1].startswith("System Error: Failed to parse JSON.")
    if not repair_both:
        assert error and "not valid JSON" in error
        assert not any(kind == "step_done" for kind, _ in e.events)


@pytest.mark.parametrize("kind", ["string", "file", "output"])
@pytest.mark.parametrize("same", [False, True])
def test_official_slot_edit_error_requires_matching_id(tmp_path, kind, same):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.json").write_text('{"value":0}')
    failed = action("edit_doc", id="a", old_str="ABSENT", new_str="1")
    repair = (action("edit_doc", id="a", old_str="0", new_str="1") if same
              else create("b"))
    e = slot_engine(tmp_path, [EXTRA_BRACE, response(failed),
        response(repair), response(action("finish_step"))],
        fixed(kind, "docs/*.json"))
    value, error = attempt(e, tmp_path)
    assert (value is True) == same
    assert json.loads((tmp_path / "docs/a.json").read_text())["value"] == (1 if same else 0)
    assert (tmp_path / "docs/b.json").exists() == (not same)
    if not same:
        assert error and "old_str" in error and "docs/a.json" in error
        assert sum(c["params"].get("id") == "b" for c in e.calls) == 1
        assert not any(kind == "step_done" for kind, _ in e.events)


@pytest.mark.parametrize("kind", ["string", "file", "output"])
@pytest.mark.parametrize("repair_tool", ["create_doc", "edit_doc"])
def test_fixed_non_glob_repairs_same_file_regardless_of_unused_id(
        tmp_path, kind, repair_tool):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/single.json").write_text('{"value":0}')
    repair = (create("b") if repair_tool == "create_doc" else
              action("edit_doc", id="b", old_str="0", new_str="1"))
    e = slot_engine(tmp_path, [EXTRA_BRACE, response(create("a", True)),
        response(repair)], fixed(kind, "docs/single.json"))
    value, error = attempt(e, tmp_path)
    assert value is True and error is None
    assert json.loads((tmp_path / "docs/single.json").read_text())["value"] == 1
    assert len(e.calls) == 2
    assert len([k for k, _ in e.events if k == "step_done"]) == 1


@pytest.mark.parametrize("kind", ["string", "file", "output"])
def test_official_default_unknown_id_is_distinct_from_unrelated_id(tmp_path, kind):
    failed = action("create_doc", initialContent="{invalid json")
    e = slot_engine(tmp_path, [EXTRA_BRACE, response(failed),
        response(create("b")), response(action("finish_step"))],
        fixed(kind, "docs/*.json"))
    value, error = attempt(e, tmp_path)
    assert value is not True and error and "not valid JSON" in error
    assert not (tmp_path / "docs/unknown.json").exists()
    assert json.loads((tmp_path / "docs/b.json").read_text())["id"] == "b"
    assert not any(kind == "step_done" for kind, _ in e.events)


@pytest.mark.parametrize("kind", ["string", "file"])
@pytest.mark.parametrize("same", [False, True])
def test_multiple_globs_match_official_target_resolution(tmp_path, kind, same):
    target = "a" if same else "b"
    e = slot_engine(tmp_path, [EXTRA_BRACE, response(create("a", True)),
        response(create(target)), response(action("finish_step"))],
        fixed(kind, "docs/*/*.json"))
    value, error = attempt(e, tmp_path)
    assert (value is True) == same
    assert (tmp_path / ("docs/" + target + "/" + target + ".json")).exists()
    assert (tmp_path / "docs/a/a.json").exists() == same
    if not same:
        assert error and not any(kind == "step_done" for kind, _ in e.events)


@pytest.mark.parametrize("kind", ["string", "file", "output"])
def test_retained_other_slot_write_is_not_replayed_during_genuine_repair(tmp_path, kind):
    e = slot_engine(tmp_path, [EXTRA_BRACE,
        response(create("b"), create("a", True)), response(create("a"))],
        fixed(kind, "docs/*.json"))
    value, error = attempt(e, tmp_path)
    assert value is True and error is None
    assert json.loads((tmp_path / "docs/a.json").read_text())["id"] == "a"
    assert json.loads((tmp_path / "docs/b.json").read_text())["id"] == "b"
    assert sum(c["params"].get("id") == "b" for c in e.calls) == 1
    assert not list(tmp_path.rglob("*.bak"))
