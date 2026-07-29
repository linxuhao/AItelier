"""An inline tool step that raises must leave its reason somewhere readable.

Live case, `novel-chapter-98264c92`: `apply_state` was claimed three times and
produced three `tool_call` trace rows and NOTHING else — no `tool_result`, no
error. The exception unwound through `advance_run` into APScheduler, which
printed it to container stdout, and skillflow failed the run with the generic
"Tool step 'apply_state' crashed 3 times — failing (likely a bug in the tool,
not a transient error)".

The three retries raised three DIFFERENT errors, and the generic message hid all
of them — including the second, which is the interesting one: it only happens
because the first crash left a partial write behind, so it says the tool is not
atomic. `_failure_reason` could not help either: it reads the trace, and the
trace had nothing.
"""
from unittest.mock import MagicMock

import pytest

from core.scheduler import _advance_recording_crashes


def _sf(exc=None, current_node="apply_state"):
    sf = MagicMock()
    sf.get_run.return_value = {"current_node": current_node, "project_id": "p1"}
    if exc:
        sf.advance_run.side_effect = exc
    else:
        sf.advance_run.return_value = "next_step"
    return sf


class TestTheCrashReachesTheTrace:
    def test_the_exception_is_recorded_before_it_propagates(self):
        sf = _sf(ValueError("character '王超' not in bible and create!=true"))
        with pytest.raises(ValueError):
            _advance_recording_crashes(sf, "r1", "p1")
        sf.trace.assert_called_once()
        args, kwargs = sf.trace.call_args
        assert args[1] == "tool_result"
        payload = args[3]
        assert "王超" in payload["error"]
        assert payload["error"].startswith("ValueError:")
        assert payload["step_id"] == "apply_state"

    def test_it_still_raises_so_skillflow_keeps_counting(self):
        """The crash counter and every retry semantic must be unchanged."""
        sf = _sf(RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            _advance_recording_crashes(sf, "r1", "p1")

    def test_the_happy_path_is_untouched(self):
        sf = _sf()
        assert _advance_recording_crashes(sf, "r1", "p1") == "next_step"
        sf.trace.assert_not_called()

    def test_a_failing_trace_write_does_not_mask_the_real_error(self):
        """The recorder runs while something is already going wrong."""
        sf = _sf(ValueError("the real problem"))
        sf.trace.side_effect = RuntimeError("trace db is gone")
        with pytest.raises(ValueError, match="the real problem"):
            _advance_recording_crashes(sf, "r1", "p1")

    def test_an_unreadable_run_row_does_not_mask_it_either(self):
        sf = _sf(ValueError("the real problem"))
        sf.get_run.side_effect = RuntimeError("db locked")
        with pytest.raises(ValueError, match="the real problem"):
            _advance_recording_crashes(sf, "r1", "p1")

    def test_the_recorded_shape_is_what_last_trace_error_reads(self):
        """`_last_trace_error` looks for a payload with a non-empty `error`."""
        sf = _sf(ValueError("nope"))
        with pytest.raises(ValueError):
            _advance_recording_crashes(sf, "r1", "p1")
        payload = sf.trace.call_args[0][3]
        assert isinstance(payload.get("error"), str) and payload["error"].strip()
