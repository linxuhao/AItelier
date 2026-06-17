# tests/unit/test_user_stories_present.py
# Unit tests for the user_stories_present validation tool — enforces that a
# FINALIZED meta_conversation brief carries at least one user story.

import json

from aitelier.tools.user_stories_present.impl import user_stories_present


def _write_state(tmp_path, data: dict) -> str:
    (tmp_path / "gather_state.json").write_text(json.dumps(data), encoding="utf-8")
    return str(tmp_path)


def test_passes_when_file_absent(tmp_path):
    # Nothing written yet → nothing to enforce.
    assert user_stories_present(files=["gather_state.json"],
                                workspace_root=str(tmp_path)) == {"all_passed": True}


def test_passes_on_question_turn(tmp_path):
    ws = _write_state(tmp_path, {"need_input": True, "question": "What is the goal?"})
    assert user_stories_present(files=["gather_state.json"], workspace_root=ws)["all_passed"] is True


def test_passes_when_brief_has_a_user_story(tmp_path):
    ws = _write_state(tmp_path, {"need_input": False,
                                 "brief": {"user_stories": ["As a user, I want X, so that Y"]}})
    assert user_stories_present(files=["gather_state.json"], workspace_root=ws)["all_passed"] is True


def test_fails_when_finalizing_with_empty_user_stories(tmp_path):
    ws = _write_state(tmp_path, {"need_input": False, "brief": {"user_stories": []}})
    result = user_stories_present(files=["gather_state.json"], workspace_root=ws)
    assert result["all_passed"] is False
    assert "user story" in result["results"][0]["error"].lower()


def test_fails_when_user_stories_key_missing(tmp_path):
    ws = _write_state(tmp_path, {"need_input": False, "brief": {"description": "no stories here"}})
    assert user_stories_present(files=["gather_state.json"], workspace_root=ws)["all_passed"] is False


def test_fails_when_user_stories_are_all_blank(tmp_path):
    ws = _write_state(tmp_path, {"need_input": False, "brief": {"user_stories": ["", "   "]}})
    assert user_stories_present(files=["gather_state.json"], workspace_root=ws)["all_passed"] is False


def test_invalid_json_fails_gracefully(tmp_path):
    (tmp_path / "gather_state.json").write_text("{ not valid json", encoding="utf-8")
    result = user_stories_present(files=["gather_state.json"], workspace_root=str(tmp_path))
    assert result["all_passed"] is False
    assert "json" in result["results"][0]["error"].lower()
