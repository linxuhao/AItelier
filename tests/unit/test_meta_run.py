# tests/unit/test_meta_run.py
# Unit tests for core/meta_run.py — the thin AItelier-layer helpers that drive
# the meta_conversation skillflow run for the chat butler.

import json
from unittest.mock import MagicMock

from core import meta_run


def test_submit_user_answer_resumes_gather_with_feedback():
    sf = MagicMock()
    meta_run.submit_user_answer(sf, "run-1", "my answer")
    sf.reject_checkpoint.assert_called_once_with(
        "run-1", "gather", feedback="my answer", redirect_to="gather")


def test_submit_user_answer_blank_still_unblocks_run():
    sf = MagicMock()
    meta_run.submit_user_answer(sf, "run-1", "")
    # A blank answer must still carry non-empty feedback so the gather step
    # re-runs instead of erroring on empty input.
    _, kwargs = sf.reject_checkpoint.call_args
    assert kwargs["feedback"]
    assert kwargs["redirect_to"] == "gather"


def test_request_brief_changes_reopens_gather():
    sf = MagicMock()
    meta_run.request_brief_changes(sf, "run-1", "make it simpler")
    sf.reject_checkpoint.assert_called_once_with(
        "run-1", "gather", feedback="make it simpler", redirect_to="gather")


def test_approve_meta_drives_finalize_then_self_completes():
    """Approval routes the run into the finalize tool step; the graph's
    node_reached end-condition completes it — no host complete_run needed."""
    sf = MagicMock()
    seq = iter([{"status": "paused"}, {"status": "running"}, {"status": "completed"}])
    sf.get_run.side_effect = lambda rid: next(seq, {"status": "completed"})
    meta_run.approve_meta(sf, "run-1")
    sf.approve_checkpoint.assert_called_once_with("run-1")
    sf.advance_run.assert_called()              # drove the graph through finalize
    sf.complete_run.assert_not_called()         # graph self-terminated


def test_approve_meta_idempotent_when_already_completed():
    sf = MagicMock()
    sf.get_run.return_value = {"status": "completed"}
    meta_run.approve_meta(sf, "run-1")
    sf.approve_checkpoint.assert_not_called()
    sf.advance_run.assert_not_called()
    sf.complete_run.assert_not_called()


def test_approve_meta_raises_if_finalize_never_completes():
    """Single-producer guarantee: finalize is the SOLE artifact producer, so a
    run that never reaches terminal must RAISE (not be force-completed) — the
    caller then refuses to start DPE on missing artifacts."""
    import pytest
    sf = MagicMock()
    sf.get_run.return_value = {"status": "paused"}   # never becomes terminal
    with pytest.raises(RuntimeError):
        meta_run.approve_meta(sf, "run-1")
    sf.approve_checkpoint.assert_called_once_with("run-1")
    assert sf.advance_run.call_count >= 1
    sf.complete_run.assert_not_called()             # never force-completes


def test_approve_meta_raises_when_finalize_tool_errors():
    """A raising finalize tool (incomplete brief) surfaces as a RuntimeError."""
    import pytest
    sf = MagicMock()
    sf.get_run.return_value = {"status": "running"}
    sf.advance_run.side_effect = ValueError("brief has no user stories")
    with pytest.raises(RuntimeError):
        meta_run.approve_meta(sf, "run-1")
    sf.complete_run.assert_not_called()


def test_approve_meta_with_step_runner_claims_and_executes_step():
    """When step_runner is provided, claim_next_step and confirm_step are called."""
    sf = MagicMock()
    # Simulate the run transitioning through paused → running → completed
    sf.get_run.side_effect = [
        {"status": "paused"},       # first check
        {"status": "running"},      # after approve_checkpoint
        {"status": "completed"},    # final check — break the loop
    ]
    # advance_run returns a node ID once, then None on subsequent calls
    sf.advance_run.return_value = "finalize"

    claimed_token = MagicMock()
    claimed_token.run_id = "run-1"
    claimed_token.step_instance_id = 1
    claimed_token.version = 1
    claimed = MagicMock()
    claimed.token = claimed_token
    claimed.step_id = "finalize"
    sf.claim_next_step.return_value = claimed

    step_runner_result = {"written": ["project_brief.md"], "emitted": True}
    step_runner = MagicMock(return_value=step_runner_result)

    meta_run.approve_meta(sf, "run-1", step_runner=step_runner)

    sf.approve_checkpoint.assert_called_once_with("run-1")
    sf.advance_run.assert_called()
    sf.claim_next_step.assert_called_once_with("run-1")
    step_runner.assert_called_once_with(claimed)
    sf.confirm_step.assert_called_once_with(claimed_token, step_runner_result)
    sf.complete_run.assert_not_called()


def test_approve_meta_with_step_runner_fail_step_on_runner_error():
    """When step_runner raises, fail_step is called and RuntimeError propagates."""
    import pytest
    sf = MagicMock()
    sf.get_run.side_effect = [
        {"status": "paused"},
        {"status": "running"},
        {"status": "running"},      # after fail_step, loop re-checks
        {"status": "completed"},
    ]
    sf.advance_run.return_value = "finalize"

    claimed_token = MagicMock()
    claimed_token.run_id = "run-1"
    claimed_token.step_instance_id = 1
    claimed_token.version = 1
    claimed = MagicMock()
    claimed.token = claimed_token
    claimed.step_id = "finalize"
    sf.claim_next_step.return_value = claimed

    def raise_error(claimed):
        raise ValueError("tool execution failed")

    with pytest.raises(RuntimeError, match="step 'finalize' failed"):
        meta_run.approve_meta(sf, "run-1", step_runner=raise_error)

    sf.fail_step.assert_called_once_with(claimed_token, "tool execution failed", retryable=False)


def test_approve_meta_no_step_runner_preserves_backward_compat():
    """Default step_runner=None preserves existing behavior (no claim/confirm)."""
    sf = MagicMock()
    seq = iter([{"status": "paused"}, {"status": "running"}, {"status": "completed"}])
    sf.get_run.side_effect = lambda rid: next(seq, {"status": "completed"})
    meta_run.approve_meta(sf, "run-1")
    sf.approve_checkpoint.assert_called_once_with("run-1")
    sf.advance_run.assert_called()
    sf.claim_next_step.assert_not_called()
    sf.confirm_step.assert_not_called()
    sf.complete_run.assert_not_called()


def test_read_gather_state_reads_committed_json(tmp_path):
    gather_dir = tmp_path / "gather"
    gather_dir.mkdir()
    (gather_dir / "gather_state.json").write_text(
        json.dumps({"need_input": True, "question": "What is the goal?"}), encoding="utf-8")
    ws = MagicMock()
    ws.get_final_path.return_value = gather_dir

    state = meta_run.read_gather_state(ws, "pid")
    assert state == {"need_input": True, "question": "What is the goal?"}
    # Reads from the meta_conversation graph's gather step dir.
    ws.get_final_path.assert_called_once_with("pid", "gather", graph_name="meta_conversation")


def test_read_gather_state_absent_returns_none(tmp_path):
    ws = MagicMock()
    ws.get_final_path.return_value = tmp_path / "does_not_exist"
    assert meta_run.read_gather_state(ws, "pid") is None


def test_read_gather_state_malformed_returns_none(tmp_path):
    gather_dir = tmp_path / "gather"
    gather_dir.mkdir()
    (gather_dir / "gather_state.json").write_text("{ not json", encoding="utf-8")
    ws = MagicMock()
    ws.get_final_path.return_value = gather_dir
    assert meta_run.read_gather_state(ws, "pid") is None
