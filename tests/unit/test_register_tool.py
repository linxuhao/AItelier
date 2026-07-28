"""register_tool — resolution by identity, and nothing unloadable ever published.

The bug these cover: inside pipeline_forge's tool_loop every item shares one
source_dir, so resolving "the first tool.yaml found" registers item 1's code under
item 2's name. The registry then holds a tool whose impl.py exports the wrong
function, and every later run that references the name fails to load it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location(
    "register_tool_impl",
    Path(__file__).resolve().parents[2] / "aitelier" / "tools" / "register_tool" / "impl.py",
)
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)


@pytest.fixture
def gen_dir(tmp_path, monkeypatch):
    """Point the durable generated-tools dir at a tmp path."""
    d = tmp_path / "gen_tools"
    d.mkdir()
    monkeypatch.setattr(rt, "generated_tools_dir", lambda: d)
    return d


def _write_tool(root: Path, name: str, *, declare_name: bool = True, body: str = "") -> Path:
    """A minimal well-formed tool dir: tool.yaml + an impl exporting `name`."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    schema = {"description": f"{name} tool", "parameters": {}}
    if declare_name:
        schema = {"name": name, **schema}
    (d / "tool.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")
    (d / "impl.py").write_text(body or f"def {name}(**kwargs):\n    return {{'ok': '{name}'}}\n",
                               encoding="utf-8")
    return d


# ── the loop case ────────────────────────────────────────────────────────

def test_second_loop_item_registers_its_own_code_not_the_first(tmp_path, gen_dir):
    """Two tools under one source_dir: each must resolve to its own subtree."""
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    _write_tool(src, "beta_tool")

    for name in ("alpha_tool", "beta_tool"):
        res = rt.register_tool(tool_name=name, source_dir=str(src))
        assert res["registered"] is True, res
        impl = (gen_dir / name / "impl.py").read_text(encoding="utf-8")
        assert f"def {name}(" in impl
        assert yaml.safe_load((gen_dir / name / "tool.yaml").read_text())["name"] == name


def test_flat_sibling_is_not_mistaken_for_this_tool(tmp_path, gen_dir):
    """source_dir itself holds tool A; asking for B must fail, not copy A."""
    src = tmp_path / "step_out"
    src.mkdir()
    (src / "tool.yaml").write_text(yaml.safe_dump({"name": "alpha_tool"}), encoding="utf-8")
    (src / "impl.py").write_text("def alpha_tool(**k):\n    return {}\n", encoding="utf-8")

    res = rt.register_tool(tool_name="beta_tool", source_dir=str(src))
    assert res["registered"] is False
    assert "beta_tool" in res["error"]
    assert not (gen_dir / "beta_tool").exists()


def test_nested_layout_wins(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(src))
    assert res["registered"] is True
    assert (gen_dir / "alpha_tool" / "impl.py").exists()


def test_lone_unnamed_flat_tool_still_registers(tmp_path, gen_dir):
    """The single-tool layout that works today must keep working."""
    src = tmp_path / "step_out"
    src.mkdir()
    (src / "tool.yaml").write_text(yaml.safe_dump({"description": "x"}), encoding="utf-8")
    (src / "impl.py").write_text("def solo_tool(**k):\n    return {}\n", encoding="utf-8")

    res = rt.register_tool(tool_name="solo_tool", source_dir=str(src))
    assert res["registered"] is True, res
    assert (gen_dir / "solo_tool" / "impl.py").exists()


def test_loop_var_falls_back_to_task_name(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    res = rt.register_tool(tool_name="$current_tool", task_name="alpha_tool",
                           source_dir=str(src))
    assert res["registered"] is True, res


# ── nothing unloadable is published ──────────────────────────────────────

def test_impl_not_exporting_the_tool_name_is_rejected(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool", body="def something_else(**k):\n    return {}\n")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(src))
    assert res["registered"] is False
    assert "exports no function named 'alpha_tool'" in res["error"]
    assert not (gen_dir / "alpha_tool").exists()


def test_impl_that_fails_to_import_is_rejected(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool", body="import a_module_that_does_not_exist\n")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(src))
    assert res["registered"] is False
    assert "failed to import" in res["error"]
    assert not (gen_dir / "alpha_tool").exists()


def test_a_rejected_rebuild_leaves_the_working_version_installed(tmp_path, gen_dir):
    """Stage → verify → swap: a bad rebuild must not destroy what already works."""
    good = tmp_path / "good"
    _write_tool(good, "alpha_tool")
    assert rt.register_tool(tool_name="alpha_tool", source_dir=str(good))["registered"]

    bad = tmp_path / "bad"
    _write_tool(bad, "alpha_tool", body="def nope(**k):\n    return {}\n")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(bad))

    assert res["registered"] is False
    assert "def alpha_tool(" in (gen_dir / "alpha_tool" / "impl.py").read_text(encoding="utf-8")


def test_staging_is_cleaned_up_and_never_visible(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool", body="def nope(**k):\n    return {}\n")
    rt.register_tool(tool_name="alpha_tool", source_dir=str(src))
    assert not (gen_dir.parent / ".tool_staging" / "alpha_tool").exists()
    assert [p.name for p in gen_dir.iterdir()] == []


# ── ownership ────────────────────────────────────────────────────────────

def test_owner_is_recorded(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    rt.register_tool(tool_name="alpha_tool", source_dir=str(src), owner="gen_alpha")
    schema = yaml.safe_load((gen_dir / "alpha_tool" / "tool.yaml").read_text(encoding="utf-8"))
    assert schema["x-generated-by"] == "gen_alpha"
    assert schema["name"] == "alpha_tool"          # existing keys survive


def test_replacing_another_pipelines_tool_is_reported(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    rt.register_tool(tool_name="alpha_tool", source_dir=str(src), owner="gen_first")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(src), owner="gen_second")

    assert res["registered"] is True          # not blocked — re-generation is legitimate
    assert res["replaced_owner"] == "gen_first"
    assert "gen_second" in res["warning"]


def test_same_owner_rebuild_is_quiet(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    rt.register_tool(tool_name="alpha_tool", source_dir=str(src), owner="gen_alpha")
    res = rt.register_tool(tool_name="alpha_tool", source_dir=str(src), owner="gen_alpha")
    assert "replaced_owner" not in res


def test_project_id_is_the_default_owner(tmp_path, gen_dir):
    src = tmp_path / "step_out"
    _write_tool(src, "alpha_tool")
    rt.register_tool(tool_name="alpha_tool", source_dir=str(src), project_id="forge-x-1")
    schema = yaml.safe_load((gen_dir / "alpha_tool" / "tool.yaml").read_text(encoding="utf-8"))
    assert schema["x-generated-by"] == "forge-x-1"


# ── argument handling ────────────────────────────────────────────────────

def test_missing_tool_name(tmp_path, gen_dir):
    assert rt.register_tool(source_dir=str(tmp_path))["registered"] is False


def test_missing_source_dir(gen_dir):
    res = rt.register_tool(tool_name="alpha_tool", source_dir="/nope/nope")
    assert res["registered"] is False
    assert "source_dir not found" in res["error"]


class TestParamNamingAdvisory:
    """One concept, six spellings — that is where the fatal-typo class comes from.

    `write(file=…)` vs `read_file(path=…)`: an agent following the most recent
    example it saw mistypes the next call. Renaming the existing tools would break
    every config's `tool_params` and every role prompt, so this only stops the
    divergence GROWING. Advisory — never blocking.
    """

    def _tool(self, d, name, params):
        t = d / name
        t.mkdir(parents=True, exist_ok=True)
        (t / "tool.yaml").write_text(
            yaml.safe_dump({"name": name, "description": "x", "parameters": params}),
            encoding="utf-8")
        (t / "impl.py").write_text(
            f"def {name}(**kw):\n    return {{}}\n", encoding="utf-8")
        return t

    def test_a_variant_spelling_is_flagged(self, tmp_path):
        t = self._tool(tmp_path, "reader", {"file": {"type": "string"}})
        notes = rt._nonstandard_param_names(t)
        assert notes and "'path'" in notes[0]

    def test_the_canonical_name_is_silent(self, tmp_path):
        t = self._tool(tmp_path, "reader", {"path": {"type": "string"}})
        assert rt._nonstandard_param_names(t) == []

    def test_an_unrelated_parameter_is_silent(self, tmp_path):
        t = self._tool(tmp_path, "searcher", {"query": {"type": "string"},
                                              "max_results": {"type": "integer"}})
        assert rt._nonstandard_param_names(t) == []

    def test_a_plural_variant_is_flagged_against_paths(self, tmp_path):
        t = self._tool(tmp_path, "checker", {"files": {"type": "array"}})
        notes = rt._nonstandard_param_names(t)
        assert notes and "'paths'" in notes[0]

    def test_it_never_blocks_registration(self, tmp_path, gen_dir):
        """A naming note must not stop a working tool from registering."""
        src = tmp_path / "src"
        self._tool(src, "reader", {"file": {"type": "string"}})
        res = rt.register_tool(tool_name="reader", source_dir=str(src))
        assert res["registered"] is True
        assert res["param_naming"]
