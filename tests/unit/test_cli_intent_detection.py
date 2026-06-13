# tests/unit/test_cli_intent_detection.py
# Tests for the CLI assessment flow in _auto_create_and_run.

import pytest
from unittest.mock import patch, MagicMock


def _run_auto_create(prompt, mock_client, repo_type_choice=None, repo_source_choice=None,
                     repo_path_val=None, repo_url_val=None):
    """Helper to run _auto_create_and_run with all mocks in place."""
    import cli.app

    # Save original _state
    original_state = cli.app._state.copy()
    cli.app._state.update({
        "project_id": None,
        "page": "dashboard",
        "server_url": "http://localhost:4444",
    })

    patches = [
        patch("cli.app._monitor_pipeline"),
        patch("cli.meta_store.load_assessment", return_value=None),
        patch("cli.meta_store.clear_assessment"),
    ]

    # Build optional patches based on what the test needs
    if repo_type_choice is not None:
        patches.append(patch("cli.app._prompt_repo_type", return_value=repo_type_choice))
    if repo_path_val is not None:
        patches.append(patch("cli.app._prompt_repo_path", return_value=repo_path_val))
    if repo_url_val is not None:
        patches.append(patch("cli.app._prompt_repo_url", return_value=repo_url_val))

    # Use a context manager stack
    from contextlib import ExitStack
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]

        try:
            cli.app._auto_create_and_run(prompt, mock_client)
        finally:
            cli.app._state.update(original_state)

    return mock_client


class TestAutoCreateAndRunAssessment:
    """Tests for the unified assessment flow in _auto_create_and_run."""

    def test_new_project_submits_with_assessment(self):
        """When assessment returns complete with new_project, submits project."""
        mock_client = MagicMock()
        mock_client.assess_prompt.return_value = {
            "status": "complete",
            "intent": "new_project",
            "message": "Great idea!",
            "project_brief": {
                "project_name": "Todo App",
                "description": "A todo app",
                "goals": ["Track tasks"],
                "non_goals": [],
                "tech_constraints": [],
                "user_stories": [],
                "target_users": "Everyone",
                "success_criteria": "Tasks can be created",
            },
        }
        mock_client.submit_project.return_value = {"status": "submitted", "project_id": "todo-app"}

        _run_auto_create("build me a todo app", mock_client)

        mock_client.submit_project.assert_called_once()
        kwargs = mock_client.submit_project.call_args[1]
        assert kwargs["repo_type"] == "new"
        assert kwargs["brief"]["project_name"] == "Todo App"

    def test_existing_code_intent_prompts_for_repo(self):
        """When assessment returns existing_code intent, prompts for repo."""
        mock_client = MagicMock()
        mock_client.assess_prompt.return_value = {
            "status": "complete",
            "intent": "existing_code",
            "message": "Modifying existing code",
            "project_brief": {
                "project_name": "Dark Mode",
                "description": "Add dark mode",
                "goals": ["Dark mode toggle"],
                "non_goals": [],
                "tech_constraints": [],
                "user_stories": [],
                "target_users": "Users",
                "success_criteria": "Toggle works",
            },
        }
        mock_client.submit_project.return_value = {"status": "submitted", "project_id": "add-dark"}

        _run_auto_create(
            "add dark mode to my app", mock_client,
            repo_type_choice="existing",
            repo_path_val="/home/user/myrepo",
        )

        mock_client.submit_project.assert_called_once()
        kwargs = mock_client.submit_project.call_args[1]
        assert kwargs["repo_type"] == "existing"
        assert kwargs["repo_path"] == "/home/user/myrepo"

    def test_existing_code_clone_url(self):
        """When existing_code and user chooses clone."""
        mock_client = MagicMock()
        mock_client.assess_prompt.return_value = {
            "status": "complete",
            "intent": "existing_code",
            "message": "Fix bug",
            "project_brief": {
                "project_name": "Bug Fix",
                "description": "Fix a bug",
                "goals": ["Fix bug"],
                "non_goals": [],
                "tech_constraints": [],
                "user_stories": [],
                "target_users": "Devs",
                "success_criteria": "Bug fixed",
            },
        }
        mock_client.submit_project.return_value = {"status": "submitted", "project_id": "fix-bug"}

        _run_auto_create(
            "fix bug in my project", mock_client,
            repo_type_choice="clone",
            repo_url_val="https://github.com/user/repo",
        )

        mock_client.submit_project.assert_called_once()
        kwargs = mock_client.submit_project.call_args[1]
        assert kwargs["repo_type"] == "clone"
        assert kwargs["repo_url"] == "https://github.com/user/repo"

    def test_vague_prompt_returns_to_dashboard(self):
        """When assessment returns asking, does NOT create a project."""
        mock_client = MagicMock()
        mock_client.assess_prompt.return_value = {
            "status": "asking",
            "message": "Could you tell me more about what you'd like to build?",
        }

        # The function enters a conversation loop — patch repl_input to cancel
        with patch("cli.completer.repl_input", side_effect=KeyboardInterrupt):
            _run_auto_create("asdf", mock_client)

        mock_client.submit_project.assert_not_called()

    def test_assessment_failure_falls_back_to_legacy(self):
        """When assessment fails, falls back to legacy flow using submit_project."""
        mock_client = MagicMock()
        mock_client.assess_prompt.side_effect = Exception("Service down")
        mock_client.detect_intent.return_value = {
            "intent": "new_project", "reasoning": "greenfield"
        }
        mock_client.submit_project.return_value = {"status": "submitted", "project_id": "build-something"}

        with patch("cli.app._monitor_pipeline"):
            _run_auto_create("build something", mock_client)

        # Legacy path should use submit_project (not old create_project)
        mock_client.submit_project.assert_called_once()
        kwargs = mock_client.submit_project.call_args[1]
        assert kwargs["brief"]["description"] == "build something"
