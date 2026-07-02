# tests/unit/test_coding_mode.py
# Coding mode (Phase 1): mode gating, coding tools (edit_file/create_file/bash),
# full-transcript persistence, budget-pause event.

import json
import os
import pytest
from unittest.mock import MagicMock, AsyncMock

from core.meta_agent import (
    MetaAgent, TOOL_DEFINITIONS, CODING_TOOL_DEFINITIONS,
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
        assert names == {"edit_file", "create_file", "bash"}
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

    async def test_bash_output_truncated(self, coding_agent):
        result = await coding_agent._execute_tool(
            "bash", {"project_id": "p", "command": "yes x | head -c 20000"})
        assert result["truncated"] is True
        assert "truncated" in result["output"]
        assert len(result["output"]) < 12000


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

    def test_narrative_history_skips_tool_rows(self, db_manager):
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
            ("user", "hi"), ("assistant", "done")]

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
