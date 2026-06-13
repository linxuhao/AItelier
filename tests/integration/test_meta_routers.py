# tests/integration/test_meta_routers.py
# Integration tests for the meta conversation API endpoints.
# Uses FastAPI TestClient with mocked AIGateway.

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


def _complete_brief(name="Test", **extra):
    """Helper: build a minimal valid complete response."""
    brief = {
        "project_name": name,
        "description": extra.get("description", "test"),
        "user_stories": extra.get("user_stories", []),
        "goals": extra.get("goals", []),
        "non_goals": extra.get("non_goals", []),
        "tech_constraints": extra.get("tech_constraints", []),
        "target_users": extra.get("target_users", ""),
        "success_criteria": extra.get("success_criteria", ""),
    }
    return json.dumps({"status": "complete", "project_brief": brief})


def _asking(message="Tell me more?", analysis="..."):
    """Helper: build an asking response."""
    return json.dumps({"status": "asking", "message": message, "analysis_so_far": analysis})


def _ensure_project(client: TestClient, project_id: str = "test-proj"):
    """Create a project for tests that need one."""
    client.post("/api/projects", json={"project_id": project_id, "name": "Test"})


@patch("core.meta_conversation.AIGateway")
def test_meta_start_immediate_complete(mock_gw_cls, client: TestClient):
    """Agent completes on first turn — no history needed."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.return_value = _complete_brief("Quick")
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/start", json={"prompt": "build X", "project_id": "test-proj"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["project_brief"]["project_name"] == "Quick"
    assert data["message"] is None
    # Interaction meta: brief_review phase
    assert data["interaction"] is not None
    assert data["interaction"]["phase"] == "brief_review"
    assert "approve" in data["interaction"]["available_actions"]


@patch("core.meta_conversation.AIGateway")
def test_meta_start_asks_question(mock_gw_cls, client: TestClient):
    """Agent asks first message."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.return_value = _asking("What tech stack?")
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/start", json={"prompt": "build X", "project_id": "test-proj"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "asking"
    assert data["message"] == "What tech stack?"
    # Interaction meta: meta_conversation phase
    assert data["interaction"] is not None
    assert data["interaction"]["phase"] == "meta_conversation"
    assert "answer" in data["interaction"]["available_actions"]


@patch("core.meta_conversation.AIGateway")
def test_meta_start_project_not_found(mock_gw_cls, client: TestClient):
    """Should return 404 if project doesn't exist."""
    mock_gw = MagicMock()
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/start", json={"prompt": "build X", "project_id": "nonexistent"})
    assert resp.status_code == 404


@patch("core.meta_conversation.AIGateway")
def test_meta_next_multi_turn(mock_gw_cls, client: TestClient):
    """Full multi-turn flow: start → next → complete."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.side_effect = [
        _asking("What language?"),
        _asking("Any GUI?"),
        _complete_brief("Multi"),
    ]
    mock_gw_cls.return_value = mock_gw

    # Turn 1: start
    resp = client.post("/api/meta/start", json={"prompt": "build X", "project_id": "test-proj"})
    data1 = resp.json()
    assert data1["status"] == "asking"
    assert data1["interaction"]["phase"] == "meta_conversation"
    assert data1["interaction"]["turn"] == 0

    # Turn 2: answer + history
    history = [{"message": "What language?", "answer": "Python"}]
    resp = client.post("/api/meta/next", json={"project_id": "test-proj", "history": history, "answer": "Python"})
    data2 = resp.json()
    assert data2["status"] == "asking"
    assert data2["interaction"]["turn"] == 2

    # Turn 3: answer + accumulated history
    history.append({"message": "Any GUI?", "answer": "No"})
    resp = client.post("/api/meta/next", json={"project_id": "test-proj", "history": history, "answer": "No"})
    data3 = resp.json()
    assert data3["status"] == "complete"
    assert data3["project_brief"]["project_name"] == "Multi"
    assert data3["interaction"]["phase"] == "brief_review"


@patch("core.meta_conversation.AIGateway")
def test_meta_force_brief(mock_gw_cls, client: TestClient):
    """Force endpoint produces a brief immediately."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.return_value = _complete_brief("Forced")
    mock_gw_cls.return_value = mock_gw

    history = [{"message": "Q1?", "answer": "A1"}]
    resp = client.post("/api/meta/force", json={"project_id": "test-proj", "history": history})
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"
    assert resp.json()["project_brief"]["project_name"] == "Forced"
    assert resp.json()["interaction"]["phase"] == "brief_review"


def test_meta_start_empty_prompt(client: TestClient):
    """Empty prompt should be rejected."""
    _ensure_project(client)
    resp = client.post("/api/meta/start", json={"prompt": "", "project_id": "test-proj"})
    assert resp.status_code == 422


def test_meta_next_empty_answer(client: TestClient):
    """Empty answer should be rejected."""
    resp = client.post("/api/meta/next", json={"project_id": "test-proj", "history": [], "answer": ""})
    assert resp.status_code == 422


@patch("core.meta_conversation.AIGateway")
def test_meta_force_empty_history(mock_gw_cls, client: TestClient):
    """Force with empty history should still work (produces brief from scratch)."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.return_value = _complete_brief("Empty")
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/force", json={"project_id": "test-proj", "history": []})
    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"


# ── Task meta tests ──

def _task_spec_complete(desc="Test task"):
    """Helper: build a complete task spec response."""
    spec = {
        "description": desc,
        "acceptance_criteria": ["It works"],
        "scope": "Full implementation",
        "out_of_scope": "Testing",
        "estimated_complexity": "medium",
        "priority": "medium",
    }
    return json.dumps({"status": "complete", "task_spec": spec})


@patch("core.meta_conversation.AIGateway")
def test_task_meta_start_creates_task(mock_gw_cls, client: TestClient):
    """Task meta start creates a pending task."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.return_value = _task_spec_complete("Build feature")
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/task/start", json={"project_id": "test-proj", "prompt": "Build feature"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["task_id"] is not None
    assert data["task_spec"]["description"] == "Build feature"
    assert data["interaction"]["phase"] == "task_meta"


@patch("core.meta_conversation.AIGateway")
def test_task_meta_start_project_not_found(mock_gw_cls, client: TestClient):
    """Should return 404 if project doesn't exist."""
    mock_gw = MagicMock()
    mock_gw_cls.return_value = mock_gw

    resp = client.post("/api/meta/task/start", json={"project_id": "nonexistent", "prompt": "Do stuff"})
    assert resp.status_code == 404


@patch("core.meta_conversation.AIGateway")
def test_task_meta_multi_turn(mock_gw_cls, client: TestClient):
    """Task meta multi-turn conversation."""
    _ensure_project(client)
    mock_gw = MagicMock()
    mock_gw.generate.side_effect = [
        _asking("What's the scope?"),
        _task_spec_complete("Build login"),
    ]
    mock_gw_cls.return_value = mock_gw

    # Turn 1: start
    resp = client.post("/api/meta/task/start", json={"project_id": "test-proj", "prompt": "Build login"})
    task_data = resp.json()
    assert task_data["status"] == "asking"
    assert task_data["interaction"]["phase"] == "task_meta"
    assert task_data["interaction"]["turn"] == 0
    task_id = task_data["task_id"]

    # Turn 2: answer + history
    history = [{"message": "What's the scope?", "answer": "OAuth only"}]
    resp = client.post("/api/meta/task/next", json={"task_id": task_id, "history": history, "answer": "OAuth only"})
    next_data = resp.json()
    assert next_data["status"] == "complete"
    assert next_data["interaction"]["phase"] == "task_meta"
