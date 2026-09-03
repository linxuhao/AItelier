"""A step must not be cut off silently, and a retry must be told why.

Both defects were measured on jinyong-numbers 2026-09-01, step "3" (the PM):

  * It declared 9 task cards in tasks_manifest.json and wrote 5, twice hitting
    its 20-turn cap. Its reasoning had already consumed the whole 32768-token
    output cap on turn 12 (`output_cap_starved`), so several turns produced no
    tool call at all. Nothing ever told it the budget was running out — the
    existing nudge fires only when NOTHING is written, and this step had
    written plenty. The truncated breakdown was then promoted and put to a
    human for approval.

  * skillflow DID deliver the validation error on every retry claim (proven in
    the trace: seq 5401 carries it verbatim). The host rendered it out of
    _resolved_context, where it became the last `### <label>` entry among the
    graph's context sources — measured at line 1517 of 1519, immediately behind
    a 1484-line clipped design bundle. Same words, same prompt, no salience.
"""
import pytest

from core.dpe_pipeline import PipelineEngine, _LOW_TURN_BUDGET


class TestTheAgentIsWarnedBeforeTheCliff:
    def test_warns_at_the_threshold(self):
        assert PipelineEngine._should_warn_low_budget(_LOW_TURN_BUDGET, 20)

    def test_does_not_nag_every_turn(self):
        # Exactly once: a warning repeated every turn is noise, and it would
        # also re-enter the conversation after the agent has already acted.
        fired = [r for r in range(20, -1, -1)
                 if PipelineEngine._should_warn_low_budget(r, 20)]
        assert fired == [_LOW_TURN_BUDGET]

    def test_silent_when_the_cap_never_had_room(self):
        # A 2- or 3-turn step is already all endgame; warning says nothing.
        for cap in range(0, _LOW_TURN_BUDGET + 1):
            assert not PipelineEngine._should_warn_low_budget(_LOW_TURN_BUDGET, cap)

    def test_the_warning_does_not_depend_on_what_was_written(self):
        # THE defect. The pre-existing nudge covers "nothing written"; the case
        # that cost the round is a step that has written SOME of what it owes
        # and therefore looks finished to the loop. The predicate takes no
        # written-files argument at all — assert that by signature.
        import inspect
        params = list(inspect.signature(
            PipelineEngine._should_warn_low_budget).parameters)
        assert params == ["remaining", "max_turns"], params


class TestTheWarningSaysTheThingThatMatters:
    def test_it_names_the_remaining_count(self):
        msg = PipelineEngine._low_budget_message(3, 20)
        assert "3" in msg and "20" in msg

    def test_it_binds_a_declared_manifest_to_its_items(self):
        # The failure mode is a manifest naming items whose files were never
        # written, so the warning has to reach that specific obligation.
        msg = PipelineEngine._low_budget_message(3, 20).lower()
        assert "manifest" in msg
        assert "finish_step" in msg

    def test_it_offers_the_honest_way_out(self):
        # Without this the only options are "finish" or "be cut off", and being
        # cut off is indistinguishable from finishing. Saying so is a
        # deliverable; silently missing items is not.
        msg = PipelineEngine._low_budget_message(3, 20).lower()
        assert "say so" in msg
        assert "silently" in msg


class TestTheWarningNamesTheEscapeHatch:
    def test_it_tells_the_agent_to_ask_for_turns_when_work_remains(self):
        # R5 (2026-09-03): 15 of 18 implementer instances hit the cap and none
        # asked — the warning said "stop" and never named ask_more_turns.
        msg = PipelineEngine._low_budget_message(3, 30)
        assert "ask_more_turns" in msg

    def test_grants_are_bounded(self):
        from core.dpe_pipeline import _MAX_TURN_GRANTS, _GRANT_TURNS_MAX
        assert 1 <= _MAX_TURN_GRANTS <= 3 and 3 <= _GRANT_TURNS_MAX <= 10


class TestAValidationFailureIsAnInstruction:
    def test_absent_error_adds_nothing(self):
        assert PipelineEngine._validation_error_block(None) == ""
        assert PipelineEngine._validation_error_block("") == ""

    def test_the_error_is_reproduced_verbatim(self):
        err = ("Validation failed:\ntasks_manifest.json names 4 task(s) with "
               "NO card on disk: a, b, c, d.")
        assert err in PipelineEngine._validation_error_block(err)

    def test_it_is_a_titled_block_not_a_context_entry(self):
        block = PipelineEngine._validation_error_block("boom")
        # `### <label>` is the [Pre-resolved Context] rendering that buried it.
        assert not block.lstrip().startswith("###")
        assert "[Previous Attempt Failed Validation" in block

    def test_it_says_the_step_must_re_emit_everything(self):
        # promotion REPLACES the step directory, so a file not written this
        # attempt is deleted — the exact trap the PM fell into.
        block = PipelineEngine._validation_error_block("boom").lower()
        assert "re-emit" in block
        assert "not carried over" in block


class TestTheContextCopyIsDroppedByValue:
    ERR = "Validation failed:\nsomething broke"

    def test_the_duplicate_is_removed(self):
        rc = {"Repository — design/": "docs",
              "⚠️ Previous attempt failed validation — MUST FIX": self.ERR}
        out = PipelineEngine._drop_context_value(rc, self.ERR)
        assert out == {"Repository — design/": "docs"}

    def test_matching_is_by_value_so_a_reworded_label_still_de_dups(self):
        # skillflow's label is a PRIVATE constant. Matching the string would be
        # the obvious implementation and would silently reinstate the duplicate
        # the day it is reworded, with nothing failing.
        rc = {"anything at all": self.ERR, "keep": "other"}
        assert PipelineEngine._drop_context_value(rc, self.ERR) == {"keep": "other"}

    def test_nothing_is_dropped_when_there_is_no_error(self):
        rc = {"a": "1", "b": "2"}
        assert PipelineEngine._drop_context_value(rc, None) == rc
        assert PipelineEngine._drop_context_value(None, None) == {}

    def test_an_unrelated_entry_that_merely_mentions_it_survives(self):
        rc = {"note": self.ERR + " (as discussed)"}
        assert PipelineEngine._drop_context_value(rc, self.ERR) == rc


def test_the_runner_passes_the_explicit_field_not_the_label():
    # The host must read skillflow's dedicated ClaimedStep.validation_error.
    # Reading it back out of _resolved_context by label is what produced the
    # buried rendering in the first place.
    import inspect
    src = inspect.getsource(PipelineEngine.run_step)
    assert "validation_error" in inspect.signature(PipelineEngine.run_step).parameters
    assert "self._validation_error = validation_error" in src
