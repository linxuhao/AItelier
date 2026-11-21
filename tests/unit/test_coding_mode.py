# tests/unit/test_coding_mode.py
# What Coding Mode left in the host after the butler (core/meta_agent.py) was
# retired: full-transcript persistence, the pipeline catalog's self-description,
# and per-session usage accounting.


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

        # describe_pipeline: targeted lookup, no full-catalog pull.
        exact = reg.describe("code_review")
        assert len(exact) == 1 and exact[0]["config_name"] == "code_review"
        assert "git diff" in exact[0]["input_hint"]           # full contract
        assert "code_review" in {e["config_name"] for e in reg.describe("review")}  # keyword
        assert reg.describe("nonexistent_xyz") == []          # → tool says list_pipelines
        assert reg.describe("") == []

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
