"""JSON delivery repairs are tied to failed targets through the actual loop."""
import json

import pytest

from core.dpe_pipeline import MaxRetriesExceeded
from tests.unit.test_json_delivery_failure import EXTRA_BRACE, action, engine, response, run


def owned_engine(tmp_path, replies, failed, *, failure_result=None,
                 wrong_receipt=False):
    e = engine(tmp_path, replies)
    remaining = set(failed)

    def execute(call):
        e.calls.append(call)
        tool, params = call["tool"], call["params"]
        if tool == "read":
            return {"content": "owned read"}
        if tool == "gen_image_asset":
            path = tmp_path / "media.bin"
            path.write_bytes(b"retained media")
            return {"written": "media.bin"}
        name = params["file"]
        if name in remaining:
            remaining.remove(name)
            return (failure_result or {"error": "required write failed: " + name}).copy()
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params["content"])
        return {"written": "unrelated.py" if wrong_receipt else name}

    e._exec_tool = execute
    return e


def attempt(e):
    value, error = None, None
    try:
        value = run(e)
    except MaxRetriesExceeded as exc:
        error = str(exc)
    print(json.dumps({"value": value, "error": error, "calls": e.calls,
                      "prompts": e.prompts, "events": e.events, "traces": e.traces}))
    return value, error


def create(name):
    return action("create", file=name, content="VALUE = 1\n")


def edit(name):
    return action("edit", file=name, content="REPAIRED = 1\n")


@pytest.mark.parametrize("control", ["finish_step", "end_step", "ask_more_turns", "read"])
def test_later_unrelated_success_cannot_settle_failed_target(tmp_path, control):
    tail = action("read", path="required.py") if control == "read" else action(control)
    replies = [EXTRA_BRACE, response(create("required.py")),
               response(create("unrelated.py")), response(tail)]
    e = owned_engine(tmp_path, replies, ["required.py"])
    value, error = attempt(e)
    assert value is not True
    assert error and "required write failed: required.py" in error
    assert not any(kind == "step_done" for kind, _ in e.events)
    assert (tmp_path / "unrelated.py").read_text() == "VALUE = 1\n"
    assert not (tmp_path / "required.py").exists()
    assert [c["params"].get("file") for c in e.calls].count("unrelated.py") == 1


@pytest.mark.parametrize("failure", [
    {"error": "owned required failure"},
    {"success": False, "message": "owned required failure"},
    {"ok": False, "message": "owned required failure"},
    {"status": "failed", "message": "owned required failure"},
    {"result": {"error": "owned required failure"}},
    {"results": [{"success": True}, {"error": "owned required failure"}]},
])
def test_mixed_write_error_shapes_remain_pending(tmp_path, failure):
    e = owned_engine(tmp_path, [EXTRA_BRACE,
        response(create("partial.py"), edit("required.py")),
        response(action("finish_step"))], ["required.py"], failure_result=failure)
    value, error = attempt(e)
    assert value is not True
    assert error and "owned required failure" in error
    assert not any(kind == "step_done" for kind, _ in e.events)
    assert (tmp_path / "partial.py").read_text() == "VALUE = 1\n"
    assert not (tmp_path / "required.py").exists()
    assert [c["params"].get("file") for c in e.calls].count("partial.py") == 1


@pytest.mark.parametrize("repair_both", [False, True])
def test_each_outstanding_target_needs_its_own_repair(tmp_path, repair_both):
    repair = [edit("a.py")] + ([edit("b.py")] if repair_both else [])
    e = owned_engine(tmp_path, [EXTRA_BRACE,
        response(create("partial.py"), edit("a.py"), edit("b.py")),
        response(*repair), response(action("finish_step"))], ["a.py", "b.py"])
    value, error = attempt(e)
    assert (value is True) == repair_both
    assert (tmp_path / "a.py").read_text() == "REPAIRED = 1\n"
    assert (tmp_path / "b.py").exists() == repair_both
    assert [c["params"].get("file") for c in e.calls].count("partial.py") == 1
    if not repair_both:
        assert error and "required write failed: b.py" in error
        assert not any(kind == "step_done" for kind, _ in e.events)


def test_genuine_create_to_edit_repair_after_controls_retains_media_once(tmp_path):
    e = owned_engine(tmp_path, [response(action("gen_image_asset")), EXTRA_BRACE,
        response(create("required.py")), response(action("ask_more_turns")),
        response(action("read", path="required.py")), response(edit("./required.py"))],
        ["required.py"])
    value, error = attempt(e)
    assert value is True and error is None
    assert (tmp_path / "required.py").read_text() == "REPAIRED = 1\n"
    assert (tmp_path / "media.bin").read_bytes() == b"retained media"
    assert [c["tool"] for c in e.calls].count("gen_image_asset") == 1
    assert len(e.prompts) == 6
    assert "required write failed" in e.prompts[-1]


def test_a_success_receipt_for_another_path_is_not_repair(tmp_path):
    e = owned_engine(tmp_path, [EXTRA_BRACE, response(edit("required.py")),
        response(edit("required.py")), response(action("finish_step"))],
        ["required.py"], wrong_receipt=True)
    value, error = attempt(e)
    assert value is not True and error and "required write failed" in error
    assert not any(kind == "step_done" for kind, _ in e.events)


def test_failed_same_target_receipt_does_not_erase_original_error(tmp_path):
    replies = [EXTRA_BRACE, response(edit("required.py")),
               response(edit("required.py")), response(action("finish_step"))]
    e = engine(tmp_path, replies)
    errors = iter(["original required failure", "later still failed"])

    def execute(call):
        e.calls.append(call)
        (tmp_path / "required.py").write_text("retained partial bytes")
        return {"written": "required.py", "error": next(errors)}

    e._exec_tool = execute
    value, error = attempt(e)
    assert value is not True and error and "original required failure" in error
    assert (tmp_path / "required.py").read_text() == "retained partial bytes"
    assert len(e.calls) == 2
    assert not any(kind == "step_done" for kind, _ in e.events)


@pytest.mark.parametrize("repair", [False, True])
def test_real_patch_partial_publication_requires_only_failed_target_repair(
        tmp_path, monkeypatch, repair):
    from skillflow.strict_patch import apply_code_patch
    import skillflow.write_tools as tools

    patch = "*** Begin Patch\n*** Add File: a.py\n+A = 1\n*** Add File: b.py\n+B = 1\n*** End Patch\n"
    next_patch = ("*** Begin Patch\n*** Add File: b.py\n+B = 1\n*** End Patch\n" if repair else
                  "*** Begin Patch\n*** Add File: unrelated.py\n+C = 1\n*** End Patch\n")
    e = engine(tmp_path, [EXTRA_BRACE, response(action("apply_patch", patch=patch)),
        response(action("apply_patch", patch=next_patch)), response(action("finish_step"))])
    e._tool_schemas["apply_patch"] = {}
    real_write = tools._write_output_text
    writes, results = [], []
    refused = [False]

    def write(path, *args, **kwargs):
        writes.append(path.name)
        if path.name == "b.py" and not refused[0]:
            refused[0] = True
            raise OSError("owned b.py publication failed")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(tools, "_write_output_text", write)

    def execute(call):
        e.calls.append(call)
        result = apply_code_patch(call["params"]["patch"], tmp_path)
        results.append(result)
        return result

    e._exec_tool = execute
    value, error = attempt(e)
    assert results[0]["partial"] is True and results[0]["phase"] == "publish"
    assert results[0]["written"] == ["a.py"]
    assert (value is True) == repair
    assert (tmp_path / "a.py").read_text() == "A = 1\n"
    assert writes.count("a.py") == 1
    assert (tmp_path / "b.py").exists() == repair
    if not repair:
        assert error and "owned b.py publication failed" in error
        assert not any(kind == "step_done" for kind, _ in e.events)

def test_unrequested_partial_patch_receipt_cannot_clear_required_failure(tmp_path):
    patch = "*** Begin Patch\n*** Add File: other.py\n+OTHER = 1\n*** End Patch\n"
    e = engine(tmp_path, [EXTRA_BRACE, response(edit("required.py")),
        response(action("apply_patch", patch=patch)),
        response(action("apply_patch", patch=patch)), response(action("finish_step"))])
    e._tool_schemas["apply_patch"] = {}
    number = [0]

    def execute(call):
        e.calls.append(call)
        number[0] += 1
        if number[0] == 1:
            return {"error": "original required.py failure"}
        if number[0] == 2:
            return {"error": "other.py publication failed", "phase": "publish",
                    "partial": True, "written": ["required.py"]}
        (tmp_path / "other.py").write_text("OTHER = 1\n")
        return {"written": ["other.py"], "applied": True}

    e._exec_tool = execute
    value, error = attempt(e)
    assert value is not True
    assert error and "original required.py failure" in error
    assert not (tmp_path / "required.py").exists()
    assert (tmp_path / "other.py").read_text() == "OTHER = 1\n"
    assert not any(kind == "step_done" for kind, _ in e.events)
