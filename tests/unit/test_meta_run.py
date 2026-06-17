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


def test_approve_meta_completes_run():
    sf = MagicMock()
    meta_run.approve_meta(sf, "run-1")
    sf.complete_run.assert_called_once_with("run-1")


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
