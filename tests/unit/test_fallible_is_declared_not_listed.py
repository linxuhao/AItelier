"""A tool's failure contract belongs to the TOOL, not to an allowlist in the gate.

`forge_registry_check` decided "can this tool fail?" from a hardcoded set of
built-in names. That set is structurally blind to the tools the forge itself
generates — which are exactly the tools a generated graph routes:

    gen_skill_packager.yaml
      - id: package
        tool_name: skill_package_zip     # returns {"passed": False, "error": ...}
        transitions:
          - to: done_gate                # ...straight into the COMPLETED terminal

A failed zip reported a successful run, and it shipped. The allowlist had also
rotted in both directions: three of its seventeen names resolved to no tool at
all, and one (`md_link_check`) was a GENERATED tool somebody had hand-added.

Now the tool declares it — `x-fallible: true` in tool.yaml — and `register_tool`
derives and stamps it for every generated tool.
"""
import sys
import textwrap

import pytest
import yaml

from aitelier.tools.forge_registry_check.impl import (
    _fallible_names, _is_fallible, forge_registry_check)
from aitelier.tools.register_tool.impl import _derive_fallible, register_tool


# ── The derivation ────────────────────────────────────────────────────────────

def _tool(tmp_path, name, body, params="  x: {type: string, required: false}"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "impl.py").write_text(textwrap.dedent(body), encoding="utf-8")
    (d / "tool.yaml").write_text(
        f"name: {name}\ndescription: t\nparameters:\n{params}\n", encoding="utf-8")
    return d


class TestDerivation:
    def test_a_tool_returning_passed_is_fallible(self, tmp_path):
        d = _tool(tmp_path, "zipper", """
            def zipper(**kw):
                if not kw:
                    return {"passed": False, "error": "nope"}
                return {"zip_path": "/tmp/a.zip"}
        """)
        assert _derive_fallible(d, "zipper") is True

    def test_a_tool_returning_error_is_fallible(self, tmp_path):
        d = _tool(tmp_path, "packer", """
            def packer(**kw):
                return {"error": "bad"}
        """)
        assert _derive_fallible(d, "packer") is True

    def test_a_plain_tool_is_not(self, tmp_path):
        d = _tool(tmp_path, "loader", """
            def loader(**kw):
                return {"content": "hi"}
        """)
        assert _derive_fallible(d, "loader") is False

    def test_a_helper_returning_error_does_not_speak_for_the_tool(self, tmp_path):
        """Scoped to the exported function. A private helper's error return says
        nothing about the contract the GRAPH has to route."""
        d = _tool(tmp_path, "reader", """
            def _fetch(p):
                return {"error": "io"}

            def reader(**kw):
                got = _fetch(kw.get("x"))
                return {"content": got.get("content", "")}
        """)
        assert _derive_fallible(d, "reader") is False

    def test_a_missing_or_unparseable_impl_is_not_guessed_at(self, tmp_path):
        d = _tool(tmp_path, "broken", "def broken(:  # not python\n")
        assert _derive_fallible(d, "broken") is False


# ── The stamp ─────────────────────────────────────────────────────────────────

class TestRegisterToolStamps:
    def test_a_generated_fallible_tool_is_stamped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
        src = tmp_path / "src"
        _tool(src, "gen_zip", """
            def gen_zip(**kw):
                return {"passed": False, "error": "x"}
        """)
        res = register_tool(tool_name="gen_zip", source_dir=str(src), owner="forge-t-1")
        assert res["registered"], res
        data = yaml.safe_load((tmp_path / "home" / "tools" / "gen_zip" /
                               "tool.yaml").read_text())
        assert data["x-fallible"] is True
        assert data["x-generated-by"] == "forge-t-1"

    def test_a_plain_generated_tool_is_stamped_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
        src = tmp_path / "src"
        _tool(src, "gen_read", """
            def gen_read(**kw):
                return {"text": "hi"}
        """)
        assert register_tool(tool_name="gen_read", source_dir=str(src))["registered"]
        data = yaml.safe_load((tmp_path / "home" / "tools" / "gen_read" /
                               "tool.yaml").read_text())
        assert data["x-fallible"] is False

    def test_an_explicit_declaration_wins_over_the_derivation(self, tmp_path, monkeypatch):
        """The tool-build agent knows things the AST does not — e.g. a tool that
        returns a variable it built, or one that raises instead of returning."""
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
        src = tmp_path / "src"
        d = _tool(src, "gen_odd", """
            def gen_odd(**kw):
                out = {}
                out["error"] = "computed, not a literal"
                return out
        """)
        (d / "tool.yaml").write_text(
            (d / "tool.yaml").read_text() + "x-fallible: true\n", encoding="utf-8")
        assert register_tool(tool_name="gen_odd", source_dir=str(src))["registered"]
        data = yaml.safe_load((tmp_path / "home" / "tools" / "gen_odd" /
                               "tool.yaml").read_text())
        assert data["x-fallible"] is True     # not overwritten by the False derivation

    def test_the_stamp_lands_even_with_no_owner(self, tmp_path, monkeypatch):
        """Provenance is optional; the routing contract is not. The stamp used to
        ride inside an `if not owner: return` guard."""
        monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
        src = tmp_path / "src"
        _tool(src, "gen_anon", """
            def gen_anon(**kw):
                return {"error": "x"}
        """)
        assert register_tool(tool_name="gen_anon", source_dir=str(src))["registered"]
        data = yaml.safe_load((tmp_path / "home" / "tools" / "gen_anon" /
                               "tool.yaml").read_text())
        assert data["x-fallible"] is True
        assert "x-generated-by" not in data


# ── The gate reads the declaration ────────────────────────────────────────────

class TestTheGateAsksTheTool:
    def test_upstream_names_survive_a_missing_registry(self):
        """skillflow's own tools ship from PyPI and cannot carry the stamp yet, so
        they are listed explicitly — and NOT intersected with the live registry. An
        empty intersection would switch the whole rule off silently, which is the
        exact fail-open shape the rule exists to catch."""
        assert {"repo_apply", "draft_commit"} <= _fallible_names(set())

    def test_the_prefix_belt_judges_the_name_the_step_uses(self):
        """A graph naming `verify_claims` needs its failure routed whether or not
        that tool resolves today; non-existence is a different violation."""
        assert _is_fallible("verify_claims", set())
        assert not _is_fallible("test_write", set())   # writes a test, cannot "fail"

    def test_a_stamped_builtin_is_recognised(self):
        assert "run_tests" in _fallible_names({"run_tests"})

    def test_the_shipped_fail_open_is_now_caught(self, tmp_path):
        """Z5, end to end: BUILD a fallible tool the way the forge does, then emit
        the shape that shipped — one unconditional edge into the completed terminal.

        The tool is generated inside the test rather than borrowed from the live
        registry, because that is the whole claim: a tool that did not exist when
        the rule was written is still classified correctly.
        """
        src = tmp_path / "src"
        _tool(src, "gen_pkg_zip", """
            def gen_pkg_zip(**kw):
                if not kw.get("x"):
                    return {"passed": False, "error": "skill_dir does not exist"}
                return {"zip_path": "/tmp/a.zip"}
        """)
        assert register_tool(tool_name="gen_pkg_zip", source_dir=str(src))["registered"]

        graph = {
            "name": "g", "description": "d", "begin": "package",
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": "done_gate", "result": "completed"}]},
            "steps": [
                {"id": "package", "step_type": "tool", "tool_name": "gen_pkg_zip",
                 "transitions": [{"to": "done_gate"}]},
                {"id": "done_gate", "step_type": "gate", "transitions": [{"to": None}]},
            ],
        }
        p = tmp_path / "g.yaml"
        p.write_text(yaml.safe_dump(graph), encoding="utf-8")
        res = forge_registry_check(graph_path=str(p), role_table="")
        assert any("gen_pkg_zip' can fail" in v for v in res["violations"]), \
            res["violations"]
