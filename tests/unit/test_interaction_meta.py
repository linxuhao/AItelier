# tests/unit/test_interaction_meta.py
# Unit tests for the interaction_meta factory functions.

from models.schemas import InteractionMeta
from core.interaction_meta import (
    for_assessment_asking,
    for_brief_review,
    for_meta_conversation_asking,
    for_task_meta_asking,
    for_task_meta_complete,
    for_checkpoint_waiting,
)


def test_for_assessment_asking():
    meta = for_assessment_asking(turn=2, max_turns=6)
    assert meta.phase == "assessment"
    assert "answer" in meta.available_actions
    assert "/skip" in meta.available_actions
    assert "/cancel" in meta.available_actions
    assert meta.turn == 2
    assert meta.max_turns == 6
    assert meta.hint  # non-empty


def test_for_assessment_asking_default_max():
    meta = for_assessment_asking(turn=0)
    assert meta.max_turns == 6
    assert meta.turn == 0


def test_for_brief_review():
    meta = for_brief_review()
    assert meta.phase == "brief_review"
    assert "approve" in meta.available_actions
    assert "revise" in meta.available_actions
    assert "restart" in meta.available_actions
    assert meta.turn is None
    assert meta.max_turns is None
    assert "Review" in meta.hint


def test_for_meta_conversation_asking():
    meta = for_meta_conversation_asking(turn=3)
    assert meta.phase == "meta_conversation"
    assert "answer" in meta.available_actions
    assert "/skip" in meta.available_actions
    assert meta.turn == 3
    assert meta.max_turns == 6


def test_for_task_meta_asking():
    meta = for_task_meta_asking(turn=1)
    assert meta.phase == "task_meta"
    assert "answer" in meta.available_actions
    assert "/skip" in meta.available_actions
    assert meta.turn == 1
    assert meta.max_turns == 4


def test_for_task_meta_complete():
    meta = for_task_meta_complete()
    assert meta.phase == "task_meta"
    assert "view_task" in meta.available_actions
    assert "completed" in meta.hint.lower() or "Task spec" in meta.hint


def test_for_checkpoint_waiting():
    meta = for_checkpoint_waiting(step_label="Step 2", rejection_count=0)
    assert meta.phase == "checkpoint"
    assert "approve" in meta.available_actions
    assert "reject" in meta.available_actions
    assert "Step 2" in meta.hint


def test_for_checkpoint_waiting_with_rejections():
    meta = for_checkpoint_waiting(step_label="Checkpoint", rejection_count=3)
    assert "revised 3" in meta.hint
