"""Nothing an agent asks for may vanish.

Thirteen defects over six drives share one shape: the engine receives a complete
delivery, fails to recognise how it was spelled, and drops it without a word. The
agent sees "you wrote nothing" and re-guesses. Each earlier instance was fixed by
teaching the parser one more spelling; this file binds the identity that makes the
NEXT unrecognised spelling report itself instead:

    writes + reads + messages + controls + unclaimed == actions

`unclaimed` is not a leftover bucket to be ignored — it is the set the handlers
must ANSWER, naming the tools the step really has.
"""
import pytest

from core.dpe_pipeline import PipelineEngine

classify = PipelineEngine._classify_actions
is_write = PipelineEngine._is_mutation_tool

# What skillflow actually grants a `mode: write` step without allow_full_write.
# Verified live against dpe_default's t_impl, which gets exactly:
#   create, repo_remove_file, edit, finish_step, list, list_tree, read,
#   read_test_written, search, test_write
# There is NO `write` — that needs allow_full_write.
MODE_WRITE = {"create": {}, "edit": {}, "finish_step": {}, "read": {},
              "search": {}, "list": {}, "read_file": {}}


def _partition(actions, schemas):
    writes = [a for a in actions if isinstance(a, dict)
              and is_write(a.get("tool", ""), schemas)]
    reads, messages, controls, unclaimed = classify(actions, schemas, writes)
    return writes, reads, messages, controls, unclaimed


class TestTheIdentityHolds:
    """Whatever the input, every action lands in exactly one bucket."""

    @pytest.mark.parametrize("actions", [
        [],
        [{"tool": "create", "params": {"file": "a.py", "content": "x"}}],
        [{"tool": "read_file", "params": {"path": "a.py"}}],
        [{"tool": "finish_step", "params": {}}],
        [{"tool": "message", "params": {"content": "hi"}}],
        [{"tool": "write_file", "params": {"path": "a.py", "content": "x"}}],
        [{"tool": "bash", "params": {"cmd": "ls"}}],
        [{}],
        ["not an object"],
        [{"tool": "create", "params": {}}, {"tool": "read"},
         {"tool": "finish_step"}, {"tool": "apply_patch"}, 42],
    ])
    def test_buckets_partition_the_actions(self, actions):
        buckets = _partition(actions, MODE_WRITE)
        assert sum(len(b) for b in buckets) == len(actions)
        # And they are disjoint — no action counted twice.
        ids = [id(a) for b in buckets for a in b]
        assert len(ids) == len(set(ids))


class TestTheGrantDecides:
    """Route by what the step was GIVEN, not by how the name is spelled."""

    def test_a_granted_read_tool_is_executed_not_dropped(self):
        """`read`/`search`/`list` are skillflow's unified read surface, injected
        into every step. The read bucket was a hardcoded four-name list, so an
        agent calling a tool it demonstrably HAS had the call discarded."""
        for name in ("read", "search", "list"):
            _, reads, _, _, unclaimed = _partition([{"tool": name}], MODE_WRITE)
            assert [a["tool"] for a in reads] == [name]
            assert unclaimed == []

    def test_an_invented_name_is_unclaimed_not_silently_dropped(self):
        """`write_file` is the single most common invention; the step has `create`."""
        writes, reads, _, _, unclaimed = _partition(
            [{"tool": "write_file", "params": {"path": "a.py", "content": "x"}}],
            MODE_WRITE)
        assert writes == [] and reads == []
        assert [a["tool"] for a in unclaimed] == ["write_file"]

    def test_a_granted_non_read_non_write_tool_still_runs(self):
        """A step granted `run_tests` may call it. Classifying by a fixed read
        list made that call vanish."""
        schemas = dict(MODE_WRITE, run_tests={})
        _, reads, _, _, unclaimed = _partition([{"tool": "run_tests"}], schemas)
        assert [a["tool"] for a in reads] == ["run_tests"]
        assert unclaimed == []

    def test_control_tools_are_not_reported_as_unknown(self):
        for name in ("finish_step", "end_step", "ask_more_turns"):
            _, _, _, controls, unclaimed = _partition([{"tool": name}], MODE_WRITE)
            assert [a["tool"] for a in controls] == [name]
            assert unclaimed == []

    def test_a_non_object_entry_is_reported_rather_than_ignored(self):
        _, _, _, _, unclaimed = _partition(["write the file"], MODE_WRITE)
        assert unclaimed == ["write the file"]


class TestTheAnswerNamesWhatTheStepCanCall:
    """An agent told only 'that didn't work' re-guesses — one step produced eight
    different envelope shapes across four drives because nothing ever named the
    contract."""

    def test_feedback_names_the_bad_tool_and_the_real_ones(self):
        msg = PipelineEngine._unclaimed_feedback(
            [{"tool": "write_file"}], MODE_WRITE)
        assert "'write_file'" in msg
        for available in MODE_WRITE:
            assert available in msg

    def test_feedback_survives_an_unnamed_or_non_object_action(self):
        msg = PipelineEngine._unclaimed_feedback([{}, 42], MODE_WRITE)
        assert "(unnamed)" in msg and "not an object" in msg

    def test_feedback_says_none_when_the_step_has_no_tools(self):
        msg = PipelineEngine._unclaimed_feedback([{"tool": "x"}], {})
        assert "(none)" in msg


class TestRepeatedFeedbackIsNoticed:
    """The runtime signature of the whole defect class: a step handed the same
    cause-free sentence again and again. Twelve instances, six drives, and
    nothing in the system ever looked at the repetition."""

    def setup_method(self):
        PipelineEngine._FEEDBACK_SEEN.clear()   # process-wide table

    def _engine(self):
        from unittest.mock import MagicMock, patch
        with patch("core.agents.AgentFactory.__init__", return_value=None):
            e = PipelineEngine()
        e.factory = MagicMock()
        e._emitted = []
        e._emit = lambda t, d: e._emitted.append((t, d))
        e._trace = lambda *a, **kw: None
        return e

    def test_third_identical_delivery_raises_an_alarm(self):
        e = self._engine()
        for _ in range(2):
            e._note_feedback("C1", "System Error: Nothing was written.")
        assert [t for t, _ in e._emitted] == []
        e._note_feedback("C1", "System Error: Nothing was written.")
        alarms = [d for t, d in e._emitted if t == "feedback_repeated"]
        assert len(alarms) == 1 and alarms[0]["count"] == 3

    def test_it_alarms_once_not_on_every_further_repeat(self):
        e = self._engine()
        for _ in range(6):
            e._note_feedback("C1", "same")
        assert len([t for t, _ in e._emitted if t == "feedback_repeated"]) == 1

    def test_differing_messages_are_not_a_repeat(self):
        """A loop that is making progress changes what it says."""
        e = self._engine()
        for i in range(5):
            e._note_feedback("C1", f"failure number {i}")
        assert e._emitted == []
        assert e._repeat_note("C1") == ""

    def test_whitespace_only_differences_still_count_as_the_same_message(self):
        e = self._engine()
        e._note_feedback("C1", "Tool failed")
        e._note_feedback("C1", "Tool  failed")
        e._note_feedback("C1", "Tool failed\n")
        assert [t for t, _ in e._emitted] == ["feedback_repeated"]

    def test_it_counts_across_step_claims_within_a_run(self):
        """A PipelineEngine is built per CLAIMED step, so per-instance counting
        sees only the retry loop inside one claim. The repetition that actually
        cost drives spans FIX LAPS — a graph returning to the same maker again and
        again with the same complaint."""
        PipelineEngine._FEEDBACK_SEEN.clear()
        seen = []
        for _ in range(3):
            e = self._engine()                 # a fresh engine each visit
            e._run_id = "run-1"
            e._note_feedback("C1", "System Error: Nothing was written.")
            seen.extend(t for t, _ in e._emitted)
        assert seen == ["feedback_repeated"]

    def test_two_runs_do_not_pool(self):
        PipelineEngine._FEEDBACK_SEEN.clear()
        seen = []
        for run in ("run-a", "run-b", "run-c"):
            e = self._engine()
            e._run_id = run
            e._note_feedback("C1", "same message")
            seen.extend(t for t, _ in e._emitted)
        assert seen == []

    def test_the_shared_table_is_bounded(self):
        PipelineEngine._FEEDBACK_SEEN.clear()
        e = self._engine()
        e._run_id = "r"
        for i in range(PipelineEngine._FEEDBACK_SEEN_CAP + 200):
            e._note_feedback("C1", f"message {i}")
        assert len(PipelineEngine._FEEDBACK_SEEN) <= PipelineEngine._FEEDBACK_SEEN_CAP

    def test_two_steps_are_counted_separately(self):
        e = self._engine()
        for step in ("C1", "C2"):
            for _ in range(2):
                e._note_feedback(step, "same message")
        assert e._emitted == []

    def test_empty_feedback_is_not_a_message(self):
        e = self._engine()
        for _ in range(5):
            e._note_feedback("C1", "")
        assert e._emitted == []

    def test_the_count_rides_on_the_step_error(self):
        """An operator must see it without opening a trace."""
        e = self._engine()
        for _ in range(4):
            e._note_feedback("C1", "System Error: No files were produced.")
        note = e._repeat_note("C1")
        assert "4x unchanged" in note
        assert "harness" in note


class TestUnresolvedGrantsReachTheFailingStep:
    """skillflow records a role's unresolvable tool grants and its own docstring
    says "a host can surface this after registration". No host did — the fact was
    produced and left sitting, the same shape as the defect it was added to fix.
    A role granted a tool that does not exist runs WITHOUT it and the only symptom
    is a step that mysteriously produces nothing, so the failing step is where the
    answer belongs."""

    def _with_unresolved(self, mapping):
        from unittest.mock import MagicMock, patch
        sf = MagicMock()
        sf.unresolved_tools.return_value = mapping
        return patch("api.dependencies.get_skillflow", return_value=sf)

    def test_the_note_names_the_dropped_tools(self):
        with self._with_unresolved({"agent_config:maker": ["write_file", "edit_file"]}):
            note = PipelineEngine._unresolved_note("maker")
        assert "write_file" in note and "edit_file" in note
        assert "maker" in note

    def test_silent_when_the_role_resolves_cleanly(self):
        with self._with_unresolved({}):
            assert PipelineEngine._unresolved_note("maker") == ""

    def test_another_roles_problem_is_not_reported_here(self):
        with self._with_unresolved({"agent_config:other": ["bogus"]}):
            assert PipelineEngine._unresolved_note("maker") == ""

    def test_no_role_name_is_not_an_error(self):
        assert PipelineEngine._unresolved_note("") == ""

    def test_a_skillflow_too_old_to_answer_does_not_break_the_failure_path(self):
        """This runs while a step is already raising; it must never mask that."""
        from unittest.mock import MagicMock, patch
        sf = MagicMock()
        sf.unresolved_tools.side_effect = AttributeError("no such method")
        with patch("api.dependencies.get_skillflow", return_value=sf):
            assert PipelineEngine._unresolved_note("maker") == ""


class TestExploratoryTurnsDoNotRaiseTheAlarm:
    """Replaying the detector over 108 runs / 60k trace rows: counting every
    feedback-bearing prompt fires 16 times with 10 false alarms (38% precision),
    and 9 of the 10 carry the thinking-turn message. Skipping it leaves 7 alarms,
    1 false, all 6 genuinely-stuck steps still caught — 86%."""

    def _engine(self):
        from unittest.mock import MagicMock, patch
        PipelineEngine._FEEDBACK_SEEN.clear()
        with patch("core.agents.AgentFactory.__init__", return_value=None):
            e = PipelineEngine()
        e.factory = MagicMock()
        e._emitted = []
        e._emit = lambda t, d: e._emitted.append((t, d))
        e._trace = lambda *a, **kw: None
        e._run_id = "r1"
        return e

    def test_a_thinking_turn_never_counts(self):
        e = self._engine()
        e._feedback_exploratory = True
        for _ in range(6):
            e._note_feedback("C1", "thoughts but no actions")
        assert e._emitted == []
        assert e._repeat_note("C1") == ""

    def test_a_rejection_still_counts(self):
        e = self._engine()
        e._feedback_exploratory = False
        for _ in range(3):
            e._note_feedback("C1", "Every write in your last response failed")
        assert [t for t, _ in e._emitted] == ["feedback_repeated"]

    def test_the_explicit_kwarg_also_suppresses(self):
        e = self._engine()
        for _ in range(4):
            e._note_feedback("C1", "same", exploratory=True)
        assert e._emitted == []

    def test_exploratory_turns_do_not_mask_a_later_real_repeat(self):
        """A step that thinks, then genuinely gets stuck, must still alarm."""
        e = self._engine()
        e._feedback_exploratory = True
        for _ in range(5):
            e._note_feedback("C1", "thinking")
        e._feedback_exploratory = False
        for _ in range(3):
            e._note_feedback("C1", "Every write in your last response failed")
        assert [t for t, _ in e._emitted] == ["feedback_repeated"]


class TestConcurrentCountingCannotKillAStep:
    """42 same-run overlapping step executions are on record in this host's DB,
    and agent steps run in a thread pool. Unsynchronised, the eviction sweep
    raises RuntimeError or KeyError and aborts the step it was only observing."""

    def test_many_threads_past_the_cap_do_not_raise(self):
        import threading
        from unittest.mock import MagicMock, patch
        PipelineEngine._FEEDBACK_SEEN.clear()
        errors: list = []

        def worker(n: int) -> None:
            try:
                with patch("core.agents.AgentFactory.__init__", return_value=None):
                    e = PipelineEngine()
                e.factory = MagicMock()
                e._emit = lambda *a, **kw: None
                e._trace = lambda *a, **kw: None
                e._run_id = f"run-{n}"
                for i in range(400):
                    e._note_feedback("C1", f"message {n}-{i}")
            except Exception as exc:          # noqa: BLE001 — the point of the test
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(PipelineEngine._FEEDBACK_SEEN) <= PipelineEngine._FEEDBACK_SEEN_CAP


class TestNonFileOutputCounts:
    """A step can produce no file and still have done its job — a deletion, a
    durable state write. Counting only files made a correct delete-only turn look
    like a no-op: the engine executed the deletion, then failed the step for
    'no file writes produced'."""

    def test_a_queued_deletion_is_an_effect(self):
        assert "queued_for_deletion" in PipelineEngine._effect_name(
            {"queued_for_deletion": "old.py", "pending_deletions": 1})

    def test_a_state_write_is_an_effect(self):
        assert PipelineEngine._effect_name({"state_written": "index.yaml"})

    def test_a_read_result_is_not_an_effect(self):
        assert PipelineEngine._effect_name({"content": "some file text"}) == ""

    def test_a_failed_call_is_not_an_effect(self):
        assert PipelineEngine._effect_name(
            {"error": "boom", "queued_for_deletion": "x"}) == ""

    def test_a_written_file_is_reported_by_written_name_not_here(self):
        """The two are disjoint so a write is never double-counted."""
        assert PipelineEngine._written_name({"written": "a.md"}) == "a.md"
        assert PipelineEngine._effect_name({"written": "a.md"}) == ""
