"""Lossless artifact recovery against SkillFlow's native read/edit surfaces."""

import json
from pathlib import Path

from core.prompt_assembler import PromptAssembler
from skillflow.read_tools import build_source_map, make_read_tool_fns
from skillflow.write_tools import execute_generic_edit


def _read_fns(candidate: Path, promoted: Path):
    specs = [{"source_type": "step", "step_id": "design", "mode": "tool"}]
    source_map = build_source_map(
        specs,
        workspace_root=str(candidate.parent),
        current_config="revision",
        step_tmp_dir=str(candidate),
        step_dir=str(promoted),
        artifact_candidate=True,
        artifact_revision=True,
        current_step="design",
        output_target="artifact",
    )
    return make_read_tool_fns(
        specs,
        step_tmp_dir=str(candidate),
        step_dir=str(promoted),
        _smap=source_map,
    )


def test_native_raw_read_and_exact_edit_preserve_artifact_bytes(tmp_path):
    candidate = tmp_path / "design.tmp"
    promoted = tmp_path / "design"
    candidate.mkdir()
    promoted.mkdir()
    samples = {
        "note.md": "标题\r\n\tunchanged🙂\r\nneedle\t🙂\r\n尾部\r\n",
        "data.json": json.dumps(
            {"escaped": "quote: \\\"", "unicode": "雪🙂", "tab": "a\tb"},
            ensure_ascii=False,
        ) + "\n",
    }
    for name, text in samples.items():
        original = text.encode("utf-8")
        (candidate / name).write_bytes(original)
        fns = _read_fns(candidate, promoted)
        raw = fns["read"](name, source="self", raw=True)
        assert raw["content"].encode("utf-8") == original
        assert raw["content"].startswith("标题\r\n") if name.endswith(".md") else True

        old = "needle\t🙂" if name.endswith(".md") else '"unicode": "雪🙂"'
        new = old.replace("🙂", "🌙") if name.endswith(".md") else '"unicode": "月🌙"'
        result = execute_generic_edit(
            {"file": name, "old_str": old, "new_str": new},
            output_dir=str(candidate),
            fallback_source_dir=str(promoted),
            strict_paths=True,
        )
        assert "error" not in result
        old_bytes, new_bytes = old.encode(), new.encode()
        offset = original.index(old_bytes)
        expected = original.replace(old_bytes, new_bytes, 1)
        got = (candidate / name).read_bytes()
        assert got == expected
        assert got[:offset] == original[:offset]
        assert got[offset + len(new_bytes):] == original[offset + len(old_bytes):]


def test_native_raw_paging_and_strict_ambiguity_or_duplicate_refusal(tmp_path):
    candidate = tmp_path / "design.tmp"
    promoted = tmp_path / "design"
    candidate.mkdir()
    promoted.mkdir()
    (candidate / "roundtrip.md").write_bytes(b"one\r\ntarget\r\nlast\r\n")
    fns = _read_fns(candidate, promoted)
    window = fns["read"]("roundtrip.md", source="self", raw=True,
                          start_line=1, end_line=2)
    assert window["content"] == "target\r\n"
    assert window["returned_lines"] == 1

    (candidate / "a").mkdir()
    (candidate / "b").mkdir()
    (candidate / "a" / "target.md").write_text("a", encoding="utf-8")
    (candidate / "b" / "target.md").write_text("b", encoding="utf-8")
    ambiguous = fns["read"]("missing/target.md", source="self", raw=True)
    assert "error" in ambiguous
    assert {item["path"] for item in ambiguous["candidates"]} == {
        "a/target.md", "b/target.md"
    }

    repeated = candidate / "repeated.md"
    original = b"same\r\nsame\r\nuntouched\r\n"
    repeated.write_bytes(original)
    result = execute_generic_edit(
        {"file": "repeated.md", "old_str": "same", "new_str": "changed"},
        output_dir=str(candidate), fallback_source_dir=str(promoted), strict_paths=True,
    )
    assert "error" in result
    assert repeated.read_bytes() == original


def test_candidate_read_does_not_resurrect_deleted_promoted_artifact(tmp_path):
    candidate = tmp_path / "design.tmp"
    promoted = tmp_path / "design"
    candidate.mkdir()
    promoted.mkdir()
    (promoted / "gone.md").write_text("promoted", encoding="utf-8")
    (candidate / "_deletions.json").write_text(
        json.dumps({"deletions": ["gone.md"]}), encoding="utf-8"
    )
    fns = _read_fns(candidate, promoted)
    result = fns["read"]("gone.md", source="self", raw=True)
    assert "error" in result
    assert "deleted this step" in result["error"]
    assert "content" not in result


def test_revision_prompt_preserves_first_failure_and_requires_native_raw_retry(tmp_path):
    prompt = PromptAssembler().assemble(
        "design", tmp_path, native=True,
        tool_schemas={
            "read": {"description": "Read", "parameters": {"properties": {}}},
            "edit_design": {"description": "Edit", "parameters": {}},
        },
    )
    assert "preserve that first failure" in prompt
    assert "raw=true, source=\"self\"" in prompt
    assert "Do not use fuzzy matching" in prompt
    assert "do not rewrite the whole file" in prompt
    assert "recall_observation" in prompt
    assert "start_line" in prompt and "end_line" in prompt
