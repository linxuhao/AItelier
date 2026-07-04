# tests/unit/test_coding_mode.py
# Coding mode (Phase 1): mode gating, coding tools (edit_file/create_file/bash),
# full-transcript persistence, budget-pause event.

import json
import os
import pytest
from unittest.mock import MagicMock, AsyncMock

from core.meta_agent import (
    MetaAgent, TOOL_DEFINITIONS, CODING_TOOL_DEFINITIONS, usage_stats,
)


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    return repo


@pytest.fixture
def mock_ws(repo):
    ws = MagicMock()
    ws.get_code_path.return_value = repo
    return ws


@pytest.fixture
def coding_agent(mock_db, mock_ws):
    return MetaAgent(mock_db, mock_ws, owner_email="test@local",
                     session_id="sess1", mode="coding")


@pytest.fixture
def butler_agent(mock_db, mock_ws):
    return MetaAgent(mock_db, mock_ws, owner_email="test@local",
                     session_id="sess1")


class TestModePlumbing:
    def test_default_mode_is_butler(self, butler_agent):
        assert butler_agent.mode == "butler"

    def test_invalid_mode_falls_back_to_butler(self, mock_db, mock_ws):
        agent = MetaAgent(mock_db, mock_ws, mode="root")
        assert agent.mode == "butler"

    def test_coding_mode_raises_turn_budget(self, coding_agent, butler_agent):
        assert coding_agent.max_tool_turns == 50
        assert butler_agent.max_tool_turns == 20

    def test_coding_tool_definitions_shape(self):
        names = {td["function"]["name"] for td in CODING_TOOL_DEFINITIONS}
        assert names == {"edit_file", "create_file", "bash",
                         "runner_start", "runner_submit",
                         "runner_approve", "runner_reject", "skillflow_tool",
                         "web_search", "web_fetch"}
        butler_names = {td["function"]["name"] for td in TOOL_DEFINITIONS}
        assert not names & butler_names

    def test_coding_system_prompt_uses_template(self, coding_agent, butler_agent):
        coding = coding_agent._build_system_prompt("proj-1")
        butler = butler_agent._build_system_prompt("proj-1")
        assert "CODING MODE" in coding
        assert "proj-1" in coding
        assert "CODING MODE" not in butler

    async def test_coding_tools_rejected_in_butler_mode(self, butler_agent):
        result = await butler_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "hello.py",
                          "old_str": "a", "new_str": "b"})
        assert "only available in coding mode" in result["error"]

    async def test_bash_rejected_in_butler_mode(self, butler_agent):
        result = await butler_agent._execute_tool(
            "bash", {"project_id": "p", "command": "echo hi"})
        assert "only available in coding mode" in result["error"]


class TestEditFile:
    async def test_edit_requires_prior_read(self, coding_agent):
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "hello.py",
                          "old_str": "'hello'", "new_str": "'hi'"})
        assert "read" in result["error"]

    async def test_edit_after_read(self, coding_agent, repo):
        read = await coding_agent._execute_tool(
            "read_code_file", {"project_id": "p", "path": "hello.py"})
        assert "error" not in read
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "hello.py",
                          "old_str": "'hello'", "new_str": "'hi'"})
        assert result == {"edited": "hello.py"}
        assert "return 'hi'" in (repo / "hello.py").read_text()

    async def test_edit_ambiguous_match_fails(self, coding_agent, repo):
        (repo / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        await coding_agent._execute_tool(
            "read_code_file", {"project_id": "p", "path": "dup.py"})
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "dup.py",
                          "old_str": "x = 1", "new_str": "x = 2"})
        assert "2 times" in result["error"]
        assert (repo / "dup.py").read_text() == "x = 1\nx = 1\n"

    async def test_edit_missing_snippet_fails(self, coding_agent):
        await coding_agent._execute_tool(
            "read_code_file", {"project_id": "p", "path": "hello.py"})
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "hello.py",
                          "old_str": "nonexistent", "new_str": "x"})
        assert "not found" in result["error"]

    async def test_edit_nonexistent_file(self, coding_agent):
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "ghost.py",
                          "old_str": "a", "new_str": "b"})
        assert "does not exist" in result["error"]

    async def test_edit_path_traversal_denied(self, coding_agent):
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "../outside.py",
                          "old_str": "a", "new_str": "b"})
        assert "traversal" in result["error"].lower()

    async def test_no_project_slash_stripping(self, coding_agent, repo):
        # Unlike DPE's AT-9 normalization, a real project/ directory is honored.
        (repo / "project").mkdir()
        (repo / "project" / "real.py").write_text("v = 1\n", encoding="utf-8")
        await coding_agent._execute_tool(
            "read_code_file", {"project_id": "p", "path": "project/real.py"})
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "project/real.py",
                          "old_str": "v = 1", "new_str": "v = 2"})
        assert result == {"edited": "project/real.py"}
        assert (repo / "project" / "real.py").read_text() == "v = 2\n"


class TestCreateFile:
    async def test_create_new_file(self, coding_agent, repo):
        result = await coding_agent._execute_tool(
            "create_file", {"project_id": "p", "path": "pkg/new.py",
                            "content": "a = 1\n"})
        assert result["created"] == "pkg/new.py"
        assert (repo / "pkg" / "new.py").read_text() == "a = 1\n"

    async def test_create_refuses_existing(self, coding_agent):
        result = await coding_agent._execute_tool(
            "create_file", {"project_id": "p", "path": "hello.py",
                            "content": "clobber"})
        assert "already exists" in result["error"]

    async def test_created_file_is_editable_without_read(self, coding_agent, repo):
        await coding_agent._execute_tool(
            "create_file", {"project_id": "p", "path": "fresh.py",
                            "content": "n = 1\n"})
        result = await coding_agent._execute_tool(
            "edit_file", {"project_id": "p", "path": "fresh.py",
                          "old_str": "n = 1", "new_str": "n = 2"})
        assert result == {"edited": "fresh.py"}


class TestBash:
    async def test_bash_runs_in_repo_cwd(self, coding_agent):
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p", "command": "ls"})
        assert result["exit_code"] == 0
        assert "hello.py" in result["output"]

    async def test_bash_nonzero_exit(self, coding_agent):
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p", "command": "exit 3"})
        assert result["exit_code"] == 3

    async def test_bash_timeout(self, coding_agent):
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p", "command": "sleep 5", "timeout": 1})
        assert "timed out" in result["error"]

    async def test_bash_env_scrubbed(self, coding_agent, monkeypatch):
        monkeypatch.setenv("FAKE_API_KEY", "supersecret")
        monkeypatch.setenv("MY_PASSWORD", "hunter2")
        monkeypatch.setenv("PLAIN_VAR", "visible")
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p",
                     "command": 'echo "k=${FAKE_API_KEY:-gone} '
                                'p=${MY_PASSWORD:-gone} v=${PLAIN_VAR:-gone}"'})
        assert "k=gone" in result["output"]
        assert "p=gone" in result["output"]
        assert "v=visible" in result["output"]

    async def test_bash_env_scrub_spares_git_config_vars(self, coding_agent,
                                                         monkeypatch):
        # An unanchored _KEY match stripped GIT_CONFIG_KEY_0 but left
        # GIT_CONFIG_COUNT, which makes every git command fail with
        # "error: missing config key GIT_CONFIG_KEY_0".
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/x/helper.sh")
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p",
                     "command": 'echo "key=${GIT_CONFIG_KEY_0:-gone}"'})
        assert "key=credential.helper" in result["output"]

    async def test_bash_output_truncated(self, coding_agent):
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p", "command": "yes x | head -c 20000"})
        assert result["truncated"] is True
        assert "truncated" in result["output"]
        assert len(result["output"]) < 12000


class TestWebTools:
    async def test_web_tools_rejected_in_butler_mode(self, butler_agent):
        for name, args in (("web_search", {"query": "x"}),
                           ("web_fetch", {"url": "https://example.com"})):
            result = await butler_agent._execute_tool(name, args)
            assert "only available in coding mode" in result["error"]

    async def test_web_search_requires_query(self, coding_agent):
        result = await coding_agent._execute_tool("web_search", {"query": "  "})
        assert "required" in result["error"]

    async def test_web_search_unconfigured_returns_note(self, coding_agent,
                                                        monkeypatch):
        # No SEARXNG_URL → the tool reports itself unconfigured instead of
        # erroring, so the loop continues on model knowledge.
        import core.web_tools as wt
        monkeypatch.setattr(wt, "SEARXNG_URL", "")
        result = await coding_agent._execute_tool(
            "web_search", {"query": "python asyncio"})
        assert result["total"] == 0
        assert "not configured" in result["note"]

    async def test_web_search_delegates(self, coding_agent, monkeypatch):
        from core import web_tools
        seen = {}

        def fake_search(self, query, max_results=5, categories="general",
                        language="auto"):
            seen["query"], seen["max"] = query, max_results
            return {"query": query, "total": 1,
                    "results": [{"title": "t", "url": "u", "snippet": "s"}]}

        monkeypatch.setattr(web_tools.WebSearchTool, "search", fake_search)
        result = await coding_agent._execute_tool(
            "web_search", {"query": "svelte 5 runes", "max_results": 3})
        assert seen == {"query": "svelte 5 runes", "max": 3}
        assert result["total"] == 1

    async def test_web_fetch_invalid_url(self, coding_agent):
        result = await coding_agent._execute_tool(
            "web_fetch", {"url": "ftp://example.com/x"})
        assert "Unsupported scheme" in result["error"]

    async def test_web_fetch_blocks_private_hosts(self, coding_agent):
        result = await coding_agent._execute_tool(
            "web_fetch", {"url": "http://127.0.0.1:4444/api/me"})
        assert "private" in result["error"].lower() or "Blocked" in result["error"]

    async def test_web_fetch_passes_offset(self, coding_agent, monkeypatch):
        from core import web_tools
        seen = {}

        def fake_fetch(self, url, max_chars=None, offset=0):
            seen["url"], seen["offset"] = url, offset
            return {"url": url, "content": "hello", "truncated": False}

        monkeypatch.setattr(web_tools.WebFetchTool, "fetch", fake_fetch)
        result = await coding_agent._execute_tool(
            "web_fetch", {"url": "https://docs.python.org/3/", "offset": 500})
        assert seen == {"url": "https://docs.python.org/3/", "offset": 500}
        assert result["content"] == "hello"


class TestTranscriptPersistence:
    def test_roundtrip(self, db_manager):
        sid = db_manager.create_session()
        db_manager.save_chat_message_with_session(sid, "p", "user", "fix the bug")
        assistant = {"role": "assistant", "content": "looking",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "bash",
                                                  "arguments": "{}"}}]}
        tool = {"role": "tool", "tool_call_id": "c1", "content": '{"exit_code": 0}'}
        db_manager.save_chat_transcript_message(sid, "p", assistant)
        db_manager.save_chat_transcript_message(sid, "p", tool)

        msgs = db_manager.get_chat_transcript_by_session(sid)
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "tool"]
        assert msgs[1]["tool_calls"][0]["id"] == "c1"
        assert msgs[2]["tool_call_id"] == "c1"
        assert all("_row_id" in m for m in msgs)

    def test_incomplete_group_dropped(self, db_manager):
        sid = db_manager.create_session()
        db_manager.save_chat_message_with_session(sid, "p", "user", "go")
        # assistant with 2 calls but only 1 tool result (crash mid-turn)
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [
                         {"id": "c1", "type": "function",
                          "function": {"name": "bash", "arguments": "{}"}},
                         {"id": "c2", "type": "function",
                          "function": {"name": "bash", "arguments": "{}"}}]}
        db_manager.save_chat_transcript_message(sid, "p", assistant)
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "tool", "tool_call_id": "c1", "content": "{}"})

        msgs = db_manager.get_chat_transcript_by_session(sid)
        assert [m["role"] for m in msgs] == ["user"]

    def test_orphan_tool_rows_dropped(self, db_manager):
        sid = db_manager.create_session()
        # window cut: tool results whose assistant fell outside the LIMIT
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "tool", "tool_call_id": "c9", "content": "{}"})
        db_manager.save_chat_message_with_session(sid, "p", "user", "hi")
        msgs = db_manager.get_chat_transcript_by_session(sid)
        assert [m["role"] for m in msgs] == ["user"]

    def test_narrative_history_keeps_tool_rows_drops_empty_shells(self, db_manager):
        # Since ee0a31c tool rows are INCLUDED (the chat UI renders them as
        # collapsible blocks after reload); empty assistant tool-call shells
        # are still dropped.
        sid = db_manager.create_session()
        db_manager.save_chat_message_with_session(sid, "p", "user", "hi")
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "assistant", "content": "",
                       "tool_calls": [{"id": "c1", "type": "function",
                                       "function": {"name": "bash",
                                                    "arguments": "{}"}}]})
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "tool", "tool_call_id": "c1", "content": "{}"})
        db_manager.save_chat_message_with_session(sid, "p", "assistant", "done")
        narrative = db_manager.get_chat_history_by_session(sid)
        assert [(m["role"], m["content"]) for m in narrative] == [
            ("user", "hi"), ("tool", "{}"), ("assistant", "done")]

    def test_session_mode(self, db_manager):
        sid = db_manager.create_session()
        assert db_manager.get_session_mode(sid) == "butler"
        db_manager.set_session_mode(sid, "coding")
        assert db_manager.get_session_mode(sid) == "coding"
        assert db_manager.get_session_mode("nonexistent") == "butler"


class TestCondenser:
    def _mk_messages(self, n=30):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(n):
            msgs.append({"role": "user", "content": f"msg {i}", "_row_id": i + 1})
        return msgs

    async def test_no_compaction_below_threshold(self, coding_agent):
        msgs = self._mk_messages()
        coding_agent.compact_at_tokens = 10**9
        assert await coding_agent._maybe_compact(msgs) is msgs

    async def test_disabled_when_zero(self, coding_agent):
        coding_agent.compact_at_tokens = 0
        msgs = self._mk_messages()
        assert await coding_agent._maybe_compact(msgs) is msgs

    async def test_butler_mode_never_compacts(self, butler_agent):
        butler_agent.compact_at_tokens = 1
        msgs = self._mk_messages()
        assert await butler_agent._maybe_compact(msgs) is msgs

    async def test_compaction_replaces_head_and_persists(self, coding_agent, mock_db):
        coding_agent.compact_at_tokens = 1  # force
        mock_db.save_chat_transcript_message.return_value = 99
        coding_agent._summarize_chunk = AsyncMock(return_value="## Session summary")

        msgs = self._mk_messages(30)
        out = await coding_agent._maybe_compact(msgs)
        assert out is not msgs
        assert out[0]["role"] == "system" and out[0]["content"] == "sys"
        assert out[1]["content"] == "## Session summary"
        # 60% of 30 = 18 summarized, 12 kept
        assert len(out) == 2 + 12
        assert out[2]["content"] == "msg 18"
        # watermark = highest summarized row id
        saved = mock_db.save_chat_transcript_message.call_args[0][2]
        assert saved["compaction_through"] == 18

    async def test_compaction_never_splits_tool_group(self, coding_agent, mock_db):
        coding_agent.compact_at_tokens = 1
        mock_db.save_chat_transcript_message.return_value = 99
        coding_agent._summarize_chunk = AsyncMock(return_value="S")
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"m{i}", "_row_id": i + 1})
        # place a tool group straddling the 60% cut (index 18 in body)
        msgs[18] = {"role": "assistant", "content": None, "_row_id": 18,
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "bash", "arguments": "{}"}}]}
        msgs[19] = {"role": "tool", "tool_call_id": "c1", "content": "{}",
                    "_row_id": 19}
        out = await coding_agent._maybe_compact(msgs)
        # first kept message after the summary must not be an orphan tool result
        assert out[2]["role"] != "tool"

    async def test_summarizer_failure_leaves_messages_untouched(self, coding_agent):
        coding_agent.compact_at_tokens = 1
        coding_agent._summarize_chunk = AsyncMock(return_value=None)
        msgs = self._mk_messages()
        assert await coding_agent._maybe_compact(msgs) is msgs

    def test_clean_msgs_strips_internal_keys(self, coding_agent):
        msgs = [{"role": "user", "content": "x", "_row_id": 5,
                 "compaction_through": 3}]
        clean = coding_agent._clean_msgs(msgs)
        assert clean == [{"role": "user", "content": "x"}]

    def test_rebuild_honors_compaction_watermark(self, db_manager):
        sid = db_manager.create_session()
        for i in range(4):
            db_manager.save_chat_message_with_session(sid, "p", "user", f"old {i}")
        # compaction row summarizing everything up to the 3rd row
        rows = db_manager.get_chat_transcript_by_session(sid)
        third_id = rows[2]["_row_id"]
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "system", "content": "SUMMARY",
                       "compaction_through": third_id})
        db_manager.save_chat_message_with_session(sid, "p", "user", "new msg")

        msgs = db_manager.get_chat_transcript_by_session(sid)
        contents = [m["content"] for m in msgs]
        assert contents == ["SUMMARY", "old 3", "new msg"]

    def test_rebuild_uses_latest_compaction_only(self, db_manager):
        sid = db_manager.create_session()
        for i in range(3):
            db_manager.save_chat_message_with_session(sid, "p", "user", f"m{i}")
        rows = db_manager.get_chat_transcript_by_session(sid)
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "system", "content": "S1",
                       "compaction_through": rows[0]["_row_id"]})
        db_manager.save_chat_message_with_session(sid, "p", "user", "m3")
        rows = db_manager.get_chat_transcript_by_session(sid)
        # second compaction covers up to m2's row
        m2_row = next(m["_row_id"] for m in rows if m["content"] == "m2")
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "system", "content": "S2",
                       "compaction_through": m2_row})
        msgs = db_manager.get_chat_transcript_by_session(sid)
        contents = [m["content"] for m in msgs]
        assert contents[0] == "S2"
        assert "S1" not in contents
        assert "m3" in contents

    def test_summary_survives_large_tail(self, db_manager):
        """The summary row must NOT age out behind a long post-watermark tail.

        The old loader took the newest N rows then applied the watermark, so a
        session that grew far past N would drop the summary row, lose older
        context, and re-compact every turn. The watermark-aware loader keys on
        the watermark, so the summary is always present."""
        sid = db_manager.create_session()
        db_manager.save_chat_message_with_session(sid, "p", "user", "seed")
        rows = db_manager.get_chat_transcript_by_session(sid)
        db_manager.save_chat_transcript_message(
            sid, "p", {"role": "system", "content": "SUMMARY",
                       "compaction_through": rows[0]["_row_id"]})
        # A tail far larger than the old 400-row cap.
        for i in range(600):
            db_manager.save_chat_message_with_session(sid, "p", "user", f"t{i}")
        msgs = db_manager.get_chat_transcript_by_session(sid)
        assert msgs[0]["content"] == "SUMMARY"
        assert msgs[-1]["content"] == "t599"
        # The pre-watermark "seed" is summarized away, not resurfaced.
        assert all(m["content"] != "seed" for m in msgs)


class TestCodingTaskRunner:
    """Drive the plan-gated coding_task graph end-to-end through the butler's
    runner tools (thin wrappers over skillflow's RunnerService — the same core
    skillflow-mcp serves) against a real isolated engine — proving the engine
    holds the implement step until the plan checkpoint is approved."""

    @pytest.fixture
    def runner_env(self, tmp_path, monkeypatch, mock_db, mock_ws):
        from pathlib import Path as P
        from skillflow.core import SkillFlow
        from skillflow.graph import PipelineGraph
        import yaml as _yaml

        repo_root = P(__file__).resolve().parent.parent.parent
        sf = SkillFlow(str(tmp_path / "sf.db"),
                       workspace_base=str(tmp_path / "ws"),
                       projects_base=str(tmp_path / "proj"))
        for name, cfg in _yaml.safe_load(
                (repo_root / "agent_configs" / "coding_task.yaml").read_text()).items():
            sf.register_agent_config_from_dict(name, cfg)
        sf.register_graph(PipelineGraph.from_yaml(repo_root / "configs" / "coding_task.yaml"))

        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)

        mock_db.get_project.return_value = {"project_id": "p1"}
        agent = MetaAgent(mock_db, mock_ws, owner_email="t@local",
                          session_id="sess-ct", mode="coding")
        return agent, sf

    async def test_full_plan_gate_flow(self, runner_env):
        agent, sf = runner_env

        # 1. start → plan step instruction (honest contract, no phantom tools)
        resp = await agent._execute_tool(
            "runner_start", {"project_id": "p1", "task": "add a modulo fn"})
        assert resp["status"] == "in_progress"
        assert resp["step"] == "plan"
        assert "add a modulo fn" in resp["instruction"]  # seed reached context
        assert "### write_plan" not in resp["instruction"]
        assert "### finish_step" not in resp["instruction"]
        run_id = resp["run_id"]

        # 2. engine refuses a second concurrent run for the same project
        dup = await agent._execute_tool(
            "runner_start", {"project_id": "p1", "task": "another"})
        assert "already live" in dup["error"]

        # 3. submitting the WRONG step is rejected
        wrong = await agent._execute_tool(
            "runner_submit", {"run_id": run_id, "step_id": "implement",
                              "result": {"summary": "nope"}})
        assert "Current step is 'plan'" in wrong["error"]

        # 4. submit the plan → run pauses at the checkpoint
        resp = await agent._execute_tool(
            "runner_submit", {"run_id": run_id, "step_id": "plan",
                              "result": {"plan": "## Goal\nadd modulo"}})
        assert resp["status"] == "paused"
        assert "Plan Review" in resp["checkpoint_label"]
        # THE GATE: engine state is paused — implement is not claimable
        assert sf.get_run(run_id)["status"] == "paused"

        # 5. reject with feedback → plan step re-runs, feedback in instruction
        resp = await agent._execute_tool(
            "runner_reject", {"run_id": run_id, "feedback": "add tests too"})
        assert resp["status"] == "in_progress"
        assert resp["step"] == "plan"
        assert "add tests too" in resp["instruction"]

        # 6. revised plan → paused again → approve → implement step released
        resp = await agent._execute_tool(
            "runner_submit", {"run_id": run_id, "step_id": "plan",
                              "result": {"plan": "## Goal\nmodulo + tests"}})
        assert resp["status"] == "paused"
        resp = await agent._execute_tool(
            "runner_approve", {"run_id": run_id})
        assert resp["status"] == "in_progress"
        assert resp["step"] == "implement"
        # prior plan is in the implement step's context
        assert "modulo + tests" in resp["instruction"]

        # 7. submit implementation summary → run completes
        resp = await agent._execute_tool(
            "runner_submit", {"run_id": run_id, "step_id": "implement",
                              "result": {"summary": "done, tests pass"}})
        assert resp["status"] == "completed"
        assert resp["outputs"]["implement"]["summary"] == "done, tests pass"

        # plan artifact promoted to the workspace
        plan_file = (sf._workspace.get_config_path("p1", "coding_task")
                     / "plan" / "plan.md")
        assert plan_file.exists()
        assert "modulo + tests" in plan_file.read_text()

    async def test_skillflow_tool_proxy(self, runner_env):
        agent, sf = runner_env
        resp = await agent._execute_tool(
            "runner_start", {"project_id": "p1", "task": "t"})
        run_id = resp["run_id"]

        # write via the proxy, submit with no result — file already staged
        staged = await agent._execute_tool(
            "skillflow_tool", {"run_id": run_id, "step_id": "plan",
                               "name": "write_plan",
                               "params": {"content": "## staged"}})
        assert staged.get("written")
        resp = await agent._execute_tool(
            "runner_submit", {"run_id": run_id, "step_id": "plan"})
        assert resp["status"] == "paused"
        plan_file = (sf._workspace.get_config_path("p1", "coding_task")
                     / "plan" / "plan.md")
        assert plan_file.read_text() == "## staged"

        # host tools are redirected, not "not allowed"
        bounced = await agent._execute_tool(
            "skillflow_tool", {"run_id": run_id, "step_id": "plan",
                               "name": "edit_file", "params": {}})
        assert "not a skillflow tool" in bounced["error"]

    async def test_runner_tools_rejected_in_butler_mode(self, butler_agent):
        for name, args in (
                ("runner_start", {"project_id": "p", "task": "x"}),
                ("skillflow_tool", {"run_id": "r", "step_id": "s", "name": "n"})):
            result = await butler_agent._execute_tool(name, args)
            assert "only available in coding mode" in result["error"]

    async def test_start_requires_existing_project(self, runner_env):
        agent, _ = runner_env
        agent.db.get_project.return_value = None
        result = await agent._execute_tool(
            "runner_start", {"project_id": "ghost", "task": "x"})
        assert "not found" in result["error"]

    async def test_submit_validates_result_shape(self, runner_env):
        agent, _ = runner_env
        result = await agent._execute_tool(
            "runner_submit", {"run_id": "r", "step_id": "plan",
                              "result": "a string"})
        assert "must be an object" in result["error"]


class TestStartConfigRunAgainstProject:
    """Generic offload: start_config_run(against_project=…) resolves the
    project's repo (repo_path + repo_type=existing) — one generic param that
    replaces a per-config offload tool. Butler drives coding_impl this way."""

    async def test_against_project_resolves_existing_repo(self, coding_agent,
                                                          mock_db, monkeypatch):
        mock_db.get_project.return_value = {
            "project_id": "eval", "name": "Eval", "repo_path": "/repo/eval"}
        captured = {}

        def fake_start(db, ws, config_name, pid, **kw):
            captured.update(config_name=config_name, seed_text=kw.get("seed_text"),
                            repo_type=kw.get("repo_type"), repo_path=kw.get("repo_path"))
            return {"status": "started", "run_id": "impl-1",
                    "config_name": config_name, "scheduler_owned": True}

        import core.run_launcher as rl
        monkeypatch.setattr(rl, "start_config_run", fake_start)
        monkeypatch.setattr(rl, "generate_run_id", lambda c: f"{c}-xyz")

        result = await coding_agent._execute_tool(
            "start_config_run", {"config_name": "coding_impl",
                                 "against_project": "eval",
                                 "seed_text": "## Goal\ndo it"})
        assert result["run_id"] == "impl-1"
        assert captured == {"config_name": "coding_impl",
                            "seed_text": "## Goal\ndo it",
                            "repo_type": "existing", "repo_path": "/repo/eval"}
        mock_db.link_run_to_session.assert_called_once_with("sess1", "impl-1")

    async def test_against_project_unknown(self, coding_agent, mock_db):
        mock_db.get_project.return_value = None
        result = await coding_agent._execute_tool(
            "start_config_run", {"config_name": "coding_impl",
                                 "against_project": "ghost"})
        assert "not found" in result["error"]


class TestPipelineCatalog:
    """The catalog is generated from config self-description (x-aitelier
    input_hint) — no per-config prompt text — and pushed compact into the
    coding-mode system context."""

    def test_manifest_parses_input_hint(self, tmp_path):
        from skillflow.core import SkillFlow
        from skillflow.graph import PipelineGraph
        from core.config_registry import ConfigRegistry
        from pathlib import Path as P

        repo = P(__file__).resolve().parent.parent.parent
        sf = SkillFlow(str(tmp_path / "sf.db"),
                       workspace_base=str(tmp_path / "ws"),
                       projects_base=str(tmp_path / "proj"))
        import yaml as _yaml
        for f in ("coding_impl", "code_review"):
            for name, cfg in _yaml.safe_load(
                    (repo / "agent_configs" / f"{f}.yaml").read_text()).items():
                sf.register_agent_config_from_dict(name, cfg)
            sf.register_graph(PipelineGraph.from_yaml(repo / "configs" / f"{f}.yaml"))
        reg = ConfigRegistry.build(sf)

        cr = reg.get("code_review")
        assert "git diff" in cr.input_hint  # the contract that prevents the trap
        # compact catalog: name/desc/drive, no input_hint
        compact = reg.catalog(full=False)
        assert all(set(e) == {"config_name", "description", "drive"} for e in compact)
        assert {e["config_name"]: e["drive"] for e in compact}["code_review"] == "inline"
        assert {e["config_name"]: e["drive"] for e in compact}["coding_impl"] == "background"
        # full catalog carries the input contract for the pull
        full = {e["config_name"]: e for e in reg.catalog(full=True)}
        assert "git diff" in full["code_review"]["input_hint"]

    def test_generalist_and_gated_pipelines_register(self, tmp_path):
        """investigate (read-only), subagent (red-gated loop), fix_tests
        (objective loop) all register, validate, self-describe, and expose
        their loop-back gate — with globally-unique agent roles."""
        from skillflow.core import SkillFlow
        from skillflow.graph import PipelineGraph
        from core.config_registry import ConfigRegistry
        from pathlib import Path as P
        import yaml as _yaml

        repo = P(__file__).resolve().parent.parent.parent
        sf = SkillFlow(str(tmp_path / "sf.db"),
                       workspace_base=str(tmp_path / "ws"),
                       projects_base=str(tmp_path / "proj"))
        seen_roles = set()
        for f in ("investigate", "subagent", "fix_tests"):
            for name, cfg in _yaml.safe_load(
                    (repo / "agent_configs" / f"{f}.yaml").read_text()).items():
                assert name not in seen_roles, f"duplicate role {name}"
                seen_roles.add(name)
                sf.register_agent_config_from_dict(name, cfg)
            sf.register_graph(PipelineGraph.from_yaml(repo / "configs" / f"{f}.yaml"))
        reg = ConfigRegistry.build(sf)
        cat = {e["config_name"]: e for e in reg.catalog(full=True)}

        # all three are background (context-isolated) + self-describe
        for name in ("investigate", "subagent", "fix_tests"):
            assert cat[name]["drive"] == "background"
            assert cat[name]["input_hint"]

        # subagent: red-gated loop-back (review fails → back to work, bounded),
        # pass → a loop-EXTERNAL `done` gate (not to:null on the looped node —
        # that premature-fires node_reached; see coding_impl).
        sub = sf._get_resolver("subagent").graph
        review = next(n for n in sub.steps if n.id == "review")
        targets = {t.to: t.max_loop for t in review.transitions}
        assert targets.get("work") == 3          # loop-back with a bound
        assert targets.get("done") is None        # pass → external terminal gate
        assert any(n.id == "done" for n in sub.steps)

        # fix_tests: objective tool-fed gate loops back to fix, pass → `done`
        fix = sf._get_resolver("fix_tests").graph
        test = next(n for n in fix.steps if n.id == "test")
        ftargets = {t.to: t.max_loop for t in test.transitions}
        assert ftargets.get("fix") == 3
        assert ftargets.get("done") is None
        assert any(n.id == "done" for n in fix.steps)

        # investigate: read-only — no output.mode write, no repo_apply
        inv = sf._get_resolver("investigate").graph
        istep = next(n for n in inv.steps if n.id == "investigate")
        assert istep.output_mode == "content"

    def test_catalog_block_pushed_into_coding_prompt(self, coding_agent, monkeypatch):
        class _Reg:
            def catalog(self, full=False):
                return [{"config_name": "code_review",
                         "description": "Adversarial review of a code diff",
                         "drive": "inline"}]
        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_config_registry", lambda: _Reg())
        prompt = coding_agent._build_system_prompt("proj-1")
        assert "code_review" in prompt
        assert "[inline]" in prompt
        assert "{pipeline_catalog}" not in prompt  # placeholder filled


class TestBudgetPause:
    async def test_budget_exhausted_event(self, coding_agent):
        # Force the loop to spend its whole budget on tool-calling turns.
        coding_agent.max_tool_turns = 1

        async def fake_stream(messages):
            yield {"_type": "collected", "text": "",
                   "tool_calls": [{"id": "c1", "name": "bash",
                                   "args": {"project_id": "p",
                                            "command": "echo hi"}}]}

        coding_agent._stream_llm = fake_stream
        events = []
        async for ev in coding_agent.chat("run echo", [], "p"):
            events.append(ev)
        assert events[-1]["type"] == "budget_exhausted"
        assert "continue" in events[-1]["message"].lower()

    async def test_stream_failure_yields_resumable_pause(self, coding_agent):
        # A transient LLM/streaming error mid-turn must NOT terminate the
        # session with a generic 'error' event — it should surface a resumable
        # 'llm_interrupted' pause so the user can reply 'continue'.
        from litellm.exceptions import APIConnectionError

        async def boom_stream(messages):
            raise APIConnectionError("connection reset",
                                     llm_provider="deepseek", model="x")
            yield  # pragma: no cover — make this an async generator

        coding_agent._stream_llm = boom_stream
        events = [ev async for ev in coding_agent.chat("do work", [], "p")]
        types = [e["type"] for e in events]
        assert "error" not in types
        assert events[-1]["type"] == "llm_interrupted"
        assert "continue" in events[-1]["message"].lower()

    async def test_stream_failure_does_not_persist_partial_turn(self, coding_agent, mock_db):
        # The failed turn is atomic: nothing is written to the transcript, so a
        # 'continue' rebuilds cleanly from the last completed state.
        from litellm.exceptions import APIConnectionError

        async def boom_stream(messages):
            raise APIConnectionError("boom", llm_provider="deepseek", model="x")
            yield  # pragma: no cover

        coding_agent._stream_llm = boom_stream
        async for _ in coding_agent.chat("do work", [], "p"):
            pass
        mock_db.save_chat_transcript_message.assert_not_called()

    async def test_transcript_persisted_during_loop(self, coding_agent, mock_db):
        coding_agent.max_tool_turns = 1

        async def fake_stream(messages):
            yield {"_type": "collected", "text": "",
                   "tool_calls": [{"id": "c1", "name": "bash",
                                   "args": {"project_id": "p",
                                            "command": "echo hi"}}]}

        coding_agent._stream_llm = fake_stream
        async for _ in coding_agent.chat("run echo", [], "p"):
            pass
        saved_roles = [c.args[2]["role"] for c in
                       mock_db.save_chat_transcript_message.call_args_list]
        assert saved_roles == ["assistant", "tool"]

    async def test_disconnect_midchain_persists_complete_group(self, coding_agent, mock_db):
        """A client disconnect mid tool-chain (GeneratorExit at a yield) must
        still persist a COMPLETE group — assistant + one result per tool_call,
        never a dangling tool_call — via the finally, with a synthetic result
        for the call that was cut off."""
        coding_agent.max_tool_turns = 1

        async def fake_stream(messages):
            yield {"_type": "collected", "text": "",
                   "tool_calls": [
                       {"id": "c1", "name": "bash", "args": {"project_id": "p", "command": "a"}},
                       {"id": "c2", "name": "bash", "args": {"project_id": "p", "command": "b"}},
                   ]}
        coding_agent._stream_llm = fake_stream

        async def exec_tool(name, args):
            return {"ok": True}
        coding_agent._execute_tool = exec_tool

        gen = coding_agent.chat("run", [], "p")
        # Consume until c1's result is emitted, then simulate a disconnect.
        while True:
            evt = await gen.__anext__()
            if evt.get("type") == "tool_result":
                break
        await gen.aclose()  # GeneratorExit → finally persists the complete group

        saved = [c.args[2] for c in mock_db.save_chat_transcript_message.call_args_list]
        assert saved and saved[0]["role"] == "assistant"
        tool_ids = [m["tool_call_id"] for m in saved if m["role"] == "tool"]
        assert set(tool_ids) == {"c1", "c2"}  # both answered — nothing dangling
        c2 = next(m for m in saved if m.get("tool_call_id") == "c2")
        assert "interrupted" in c2["content"]  # cut-off call → synthetic result

    async def test_butler_mode_does_not_persist_transcript(self, butler_agent, mock_db):
        butler_agent.max_tool_turns = 1

        async def fake_stream(messages):
            yield {"_type": "collected", "text": "",
                   "tool_calls": [{"id": "c1", "name": "list_projects",
                                   "args": {}}]}

        butler_agent._stream_llm = fake_stream
        async for _ in butler_agent.chat("list", [], None):
            pass
        mock_db.save_chat_transcript_message.assert_not_called()


class TestRealUsageTelemetry:
    """Provider-reported usage (incl. DeepSeek cache hit/miss) is captured from
    the stream and accumulated per session — comparable with DPE skillflow_trace."""

    def test_db_accumulate_and_get(self, db_manager):
        zeros = {"prompt_tokens": 0, "completion_tokens": 0,
                 "cache_hit_tokens": 0, "cache_miss_tokens": 0}
        assert db_manager.get_session_usage("nope") == zeros

        db_manager.accumulate_session_usage("s1", {
            "prompt_tokens": 100, "completion_tokens": 10,
            "cache_hit_tokens": 60, "cache_miss_tokens": 40, "hit_ratio": 0.6})
        # non-numeric values and unknown keys are ignored
        db_manager.accumulate_session_usage("s1", {
            "prompt_tokens": 50, "completion_tokens": "junk", "surprise": 9})
        assert db_manager.get_session_usage("s1") == {
            "prompt_tokens": 150, "completion_tokens": 10,
            "cache_hit_tokens": 60, "cache_miss_tokens": 40}

    async def test_stream_llm_captures_usage_chunk(self, coding_agent, monkeypatch):
        from types import SimpleNamespace
        text_chunk = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="hi", tool_calls=None))])
        # DeepSeek-style final chunk: empty choices, usage attached
        usage_chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=1000, completion_tokens=50,
                prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200))

        captured_kwargs = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)

            async def gen():
                yield text_chunk
                yield usage_chunk
            return gen()

        import core.meta_agent as ma
        monkeypatch.setattr(ma.litellm, "acompletion", fake_acompletion)

        events = [e async for e in coding_agent._stream_llm(
            [{"role": "user", "content": "hi"}])]

        assert captured_kwargs["stream_options"] == {"include_usage": True}
        collected = events[-1]
        assert collected["_type"] == "collected"
        assert collected["usage"] == {
            "prompt_tokens": 1000, "completion_tokens": 50,
            "cache_hit_tokens": 800, "cache_miss_tokens": 200,
            "hit_ratio": 0.8}

    async def test_usage_accumulates_across_turns(self, db_manager, mock_ws):
        agent = MetaAgent(db_manager, mock_ws, owner_email="test@local",
                          session_id="sess-usage", mode="coding")
        agent._count_tokens = lambda msgs: 5  # skip litellm token estimation

        calls = {"n": 0}

        async def fake_stream(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                yield {"_type": "collected", "text": "",
                       "tool_calls": [{"id": "c1", "name": "bash",
                                       "args": {"project_id": "p",
                                                "command": "echo hi"}}],
                       "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                 "cache_hit_tokens": 60, "cache_miss_tokens": 40}}
            else:
                yield {"_type": "collected", "text": "done", "tool_calls": [],
                       "usage": {"prompt_tokens": 200, "completion_tokens": 20,
                                 "cache_hit_tokens": 150, "cache_miss_tokens": 50}}

        agent._stream_llm = fake_stream
        events = [e async for e in agent.chat("run echo", [], "p")]

        assert calls["n"] == 2
        assert db_manager.get_session_usage("sess-usage") == {
            "prompt_tokens": 300, "completion_tokens": 30,
            "cache_hit_tokens": 210, "cache_miss_tokens": 90}
        # Final token_usage event carries display stats from the cumulative
        # counters: hit 210/300, billed = 90 miss + 210/10 hit + 30 completion.
        last_usage_event = [e for e in events if e["type"] == "token_usage"][-1]
        assert last_usage_event["hit_ratio"] == 0.7
        assert last_usage_event["billed_tokens"] == 141

    async def test_missing_usage_is_not_persisted(self, coding_agent, mock_db):
        async def fake_stream(messages):
            yield {"_type": "collected", "text": "done", "tool_calls": [],
                   "usage": {}}

        coding_agent._stream_llm = fake_stream
        async for _ in coding_agent.chat("hi", [], None):
            pass
        mock_db.accumulate_session_usage.assert_not_called()

    def test_usage_stats_derivation(self):
        assert usage_stats(None) == {}
        assert usage_stats({}) == {}
        assert usage_stats({"prompt_tokens": 0, "cache_hit_tokens": 5}) == {}
        stats = usage_stats({"prompt_tokens": 1000, "completion_tokens": 50,
                             "cache_hit_tokens": 800, "cache_miss_tokens": 200})
        assert stats == {"hit_ratio": 0.8, "billed_tokens": 330}

    async def test_compacter_call_usage_accumulates(self, db_manager, mock_ws,
                                                    monkeypatch):
        from types import SimpleNamespace
        agent = MetaAgent(db_manager, mock_ws, owner_email="test@local",
                          session_id="sess-compact", mode="coding")
        resp = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=500, completion_tokens=40,
                                  prompt_cache_hit_tokens=100,
                                  prompt_cache_miss_tokens=400),
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="summary text"))])

        async def fake_acompletion(**kwargs):
            return resp

        import core.meta_agent as ma
        monkeypatch.setattr(ma.litellm, "acompletion", fake_acompletion)

        summary = await agent._summarize_chunk("[user] hello")
        assert summary == "summary text"
        assert db_manager.get_session_usage("sess-compact") == {
            "prompt_tokens": 500, "completion_tokens": 40,
            "cache_hit_tokens": 100, "cache_miss_tokens": 400}
