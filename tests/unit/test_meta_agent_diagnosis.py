"""The butler's diagnosis surface: trace, tool registry, config source.

A failed run's *reason* lives in its trace — gate tools record their error in the
trace payload and leave an empty output dir behind, so no file-reading tool can
surface it. These cover the tools that close that gap, plus the registry/config
readers that let the agent check what already exists before building more.
"""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest
import yaml

from core.meta_agent import MetaAgent


@pytest.fixture
def agent(tmp_path):
    return MetaAgent(MagicMock(), MagicMock(), owner_email="test@local")


# ── a stand-in trace DB, same shape skillflow writes ─────────────────────

TRACE_ROWS = [
    # (step_id, category, event, payload)
    ("survey", "step", "claimed", {"agent": "forge_surveyor"}),
    ("v_lint", "tool_result", "forge_lint", {"source": "tool_step", "error": "", "passed": True}),
    ("v_registry", "tool_call", "forge_registry_check",
     {"source": "tool_step", "params": {"graph_path": "/x/pipeline.yaml", "role_table": "/x/rt.yaml"}}),
    ("v_registry", "tool_result", "forge_registry_check",
     {"source": "tool_step", "passed": False,
      "error": "Registry check failed — step 'spec': agent_config 'spec_maker' not defined in role table"}),
    ("emit_review", "response", "agent_response", {"text": "The graph is structurally sound."}),
]


@pytest.fixture
def traced_run(tmp_path, agent, monkeypatch):
    """Wire a fake skillflow whose trace_query hits a real sqlite DB."""
    db = tmp_path / "trace.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE skillflow_trace (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, step_id TEXT,
        step_instance_id INTEGER, seq INTEGER NOT NULL, category TEXT NOT NULL,
        event TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    for i, (step, cat, ev, payload) in enumerate(TRACE_ROWS, start=1):
        conn.execute("INSERT INTO skillflow_trace (run_id, step_id, seq, category, event, "
                     "payload_json) VALUES (?,?,?,?,?,?)",
                     ("run-1", step, i, cat, ev, json.dumps(payload)))
    conn.commit()
    conn.row_factory = sqlite3.Row

    run_row = {"id": "run-1", "project_id": "forge-demo-1", "status": "failed",
               "error": "Cycle limit exceeded", "graph_name": "pipeline_forge"}
    sf = MagicMock()
    sf.get_run.side_effect = lambda r: run_row if r == "run-1" else None
    sf.list_runs.side_effect = lambda project_id=None, **k: (
        [run_row] if project_id == "forge-demo-1" else [])
    sf.trace_query.side_effect = lambda rid, sql, params=(): conn.execute(sql, params).fetchall()

    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    return sf


class TestTrace:
    def test_lists_newest_first_by_default(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1"})
        assert res["count"] == len(TRACE_ROWS)
        assert [e["seq"] for e in res["entries"]] == sorted(
            [e["seq"] for e in res["entries"]], reverse=True)
        assert res["run_status"] == "failed"

    def test_accepts_a_project_id(self, agent, traced_run):
        """The butler mostly holds project ids — both must work."""
        res = agent._tool_trace_list({"run": "forge-demo-1"})
        assert res["run_id"] == "run-1"
        assert res["count"] == len(TRACE_ROWS)

    def test_unknown_run_says_what_it_tried(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "nope"})
        assert "No run found" in res["error"]

    def test_errors_only_surfaces_the_real_reason(self, agent, traced_run):
        """The whole point: 'Cycle limit exceeded' → the gate error behind it."""
        res = agent._tool_trace_list({"run": "run-1", "errors_only": True})
        assert res["count"] == 1
        entry = res["entries"][0]
        assert entry["step"] == "v_registry"
        assert "not defined in role table" in entry["summary"]

    def test_errors_only_skips_a_passing_gate(self, agent, traced_run):
        """v_lint records error:"" on success — that must not read as a failure."""
        res = agent._tool_trace_list({"run": "run-1", "errors_only": True})
        assert all(e["step"] != "v_lint" for e in res["entries"])

    def test_filters_by_step(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1", "step": "v_registry"})
        assert res["count"] == 2
        assert {e["step"] for e in res["entries"]} == {"v_registry"}

    def test_filters_by_category(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1", "category": "tool_result"})
        assert {e["category"] for e in res["entries"]} == {"tool_result"}

    def test_summaries_are_capped(self, agent, traced_run):
        res = agent._tool_trace_list({"run": "run-1"})
        assert all(len(e["summary"]) <= 221 for e in res["entries"])

    def test_search_finds_the_step_that_failed(self, agent, traced_run):
        res = agent._tool_trace_search({"run": "run-1", "query": "role table"})
        assert res["count"] == 1
        assert res["entries"][0]["step"] == "v_registry"

    def test_search_requires_a_query(self, agent, traced_run):
        assert "error" in agent._tool_trace_search({"run": "run-1"})

    def test_read_returns_full_payloads(self, agent, traced_run):
        res = agent._tool_trace_read({"run": "run-1", "seq": 3, "seq_end": 4})
        assert res["count"] == 2
        payload = res["entries"][-1]["payload"]
        assert payload["passed"] is False
        assert "spec_maker" in payload["error"]        # untruncated

    def test_read_caps_the_range(self, agent, traced_run):
        res = agent._tool_trace_read({"run": "run-1", "seq": 1, "seq_end": 9999})
        assert res["count"] <= 20

    def test_read_needs_an_integer_seq(self, agent, traced_run):
        assert "error" in agent._tool_trace_read({"run": "run-1", "seq": "abc"})


class TestToolRegistry:
    @pytest.fixture
    def loader(self, monkeypatch, tmp_path):
        schemas = {
            "web_search": {"description": "Search the web via SearXNG",
                           "parameters": {"query": {}}},
            "md_link_check": {"description": "Check markdown links resolve",
                              "parameters": {"path": {}}, "x-generated-by": "gen_mdlink"},
        }
        ld = MagicMock()
        ld.list_tools.return_value = list(schemas)
        ld.load_schema.side_effect = lambda n: schemas[n]
        ld._find_tool_dir.side_effect = lambda n: tmp_path
        sf = MagicMock()
        sf._tool_loader = ld
        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        return ld

    def test_list_reports_params_and_provenance(self, agent, loader):
        res = agent._tool_tool_list({})
        by_name = {t["name"]: t for t in res["tools"]}
        assert by_name["web_search"]["params"] == ["query"]
        assert by_name["md_link_check"]["generated_by"] == "gen_mdlink"

    def test_list_filters_by_name(self, agent, loader):
        res = agent._tool_tool_list({"filter": "link"})
        assert [t["name"] for t in res["tools"]] == ["md_link_check"]

    def test_search_matches_on_purpose_not_just_name(self, agent, loader):
        """'does something already do this?' must be answerable by description."""
        res = agent._tool_tool_search({"query": "check that markdown links resolve"})
        assert res["tools"][0]["name"] == "md_link_check"

    def test_search_requires_a_query(self, agent, loader):
        assert "error" in agent._tool_tool_search({})

    def test_read_rejects_an_unregistered_tool(self, agent, loader):
        res = agent._tool_tool_read({"name": "not_a_tool"})
        assert "not registered" in res["error"]

    def test_read_rejects_an_arbitrary_file(self, agent, loader):
        res = agent._tool_tool_read({"name": "web_search", "file": "../../etc/passwd"})
        assert "error" in res


class TestConfigSource:
    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text(
            yaml.safe_dump({"name": "gen_demo", "begin": "a",
                            "steps": [{"id": "a", "step_type": "gate"}]}), encoding="utf-8")
        (d / "gen_demo.roles.json").write_text(
            json.dumps({"gen_demo__maker": {"system_prompt": "be good"}}), encoding="utf-8")
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        return d

    def test_reads_a_generated_graph(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo"})
        assert "begin: a" in res["content"]

    def test_reads_the_role_table(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo", "file": "roles"})
        assert "be good" in res["content"]

    def test_reads_a_builtin_config(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "dpe_default"})
        assert "steps:" in res["content"]

    def test_rejects_a_path_in_the_name(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "../../etc/passwd"})
        assert "bare pipeline name" in res["error"]

    def test_missing_file_names_the_roles_alternative(self, agent, gen_dir):
        res = agent._tool_config_read({"config_name": "gen_demo",
                                       "file": "templates/nope.md"})
        assert "roles.json" in res["error"]

    def test_search_finds_which_config_uses_a_tool(self, agent, gen_dir):
        res = agent._tool_config_search({"query": "forge_registry_check"})
        assert any(h["config"] == "pipeline_forge" for h in res["hits"])

    def test_search_requires_a_query(self, agent, gen_dir):
        assert "error" in agent._tool_config_search({})


class TestConfigEdit:
    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text("name: gen_demo\nbegin: a\n", encoding="utf-8")
        import core.pipeline_registry as pr
        import api.dependencies as deps
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        monkeypatch.setattr(pr, "reload_generated_pipeline",
                            lambda sf, reg, name: {"config_name": name})
        monkeypatch.setattr(deps, "get_skillflow", lambda: MagicMock())
        monkeypatch.setattr(deps, "get_config_registry", lambda: MagicMock())
        return d

    def test_edits_and_reloads(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "begin: a", "new_str": "begin: b"})
        assert res["edited"] and res["reloaded"]
        assert "begin: b" in (gen_dir / "gen_demo.yaml").read_text()

    def test_refuses_an_ambiguous_match(self, agent, gen_dir):
        (gen_dir / "gen_demo.yaml").write_text("x: 1\nx: 1\n", encoding="utf-8")
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "x: 1", "new_str": "x: 2"})
        assert "appears 2 times" in res["error"]

    def test_refuses_a_missing_match(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "nope", "new_str": "y"})
        assert "not found" in res["error"]

    def test_refuses_to_edit_a_builtin_config(self, agent, gen_dir):
        """Repo configs are code — a chat session does not rewrite them in place."""
        res = agent._tool_config_edit({"config_name": "dpe_default",
                                       "old_str": "steps:", "new_str": "steps:"})
        assert "built-in config" in res["error"]

    def test_a_broken_edit_warns_that_the_registry_is_stale(self, agent, gen_dir, monkeypatch):
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "reload_generated_pipeline",
                            lambda sf, reg, name: {"error": "invalid graph"})
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "old_str": "begin: a", "new_str": "begin: !!"})
        assert res["reload_error"] == "invalid graph"
        assert "previous version" in res["warning"]


class TestConfigEditMessages:
    """A refusal has to say the true reason — the agent acts on what it is told."""

    @pytest.fixture
    def gen_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "configs"
        d.mkdir()
        (d / "gen_demo.yaml").write_text("name: gen_demo\n", encoding="utf-8")
        import core.pipeline_registry as pr
        monkeypatch.setattr(pr, "generated_configs_dir", lambda: d)
        return d

    def test_a_template_of_a_generated_pipeline_is_not_called_built_in(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "gen_demo",
                                       "file": "templates/forge_explain.md",
                                       "old_str": "x", "new_str": "y"})
        assert "built-in config" not in res["error"]
        assert "roles.json" in res["error"]        # points at the real edit surface

    def test_a_real_builtin_still_says_so(self, agent, gen_dir):
        res = agent._tool_config_edit({"config_name": "dpe_default",
                                       "old_str": "steps:", "new_str": "steps:"})
        assert "built-in config" in res["error"]
