"""Unit tests for AgentStepRunner."""

import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from skillflow.core import ClaimedStep, ClaimToken, StepResult


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_repo_info.return_value = {"repo_type": "new", "repo_path": None, "repo_url": None}
    db.list_tasks_by_project.return_value = []
    db.get_project.return_value = {"project_id": "test", "brief": "Test brief"}
    db.get_project_meta_state.return_value = None
    db.should_refresh_planning.return_value = False
    db.has_only_failed_tasks.return_value = False
    return db


@pytest.fixture
def mock_ws(tmp_path):
    ws = MagicMock()
    ws._get_secure_path.return_value = tmp_path / "ws"
    ws.get_code_path.return_value = tmp_path / "code"
    return ws


@pytest.fixture
def claimed_step():
    token = ClaimToken(
        step_id="1_5", run_id="test-run",
        step_instance_id=1, version=1, claimed_at=0,
    )
    return ClaimedStep(
        token=token, step_id="1_5",
        step_config={
            "template": "step1_5_researcher.md",
            "model": "test/model",
            "output_mode": "content",
            "fixed_outputs": {"sota": "step1_5_sota.md"},
            "tools": ["web_search", "web_fetch"],
        },
        run_context={"project_id": "test-proj"},
        inputs={},
    )


def test_runner_imports():
    """Verify AgentStepRunner can be imported."""
    from aitelier.runner import AgentStepRunner
    assert AgentStepRunner is not None


def test_runner_instantiation(mock_db, mock_ws):
    """Runner can be instantiated with DB and WS managers."""
    from aitelier.runner import AgentStepRunner
    runner = AgentStepRunner(mock_db, mock_ws, None, None, None)
    assert runner is not None







