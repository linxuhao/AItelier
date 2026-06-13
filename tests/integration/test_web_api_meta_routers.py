# tests/integration/test_web_api_meta_routers.py
# Integration tests for web_api/meta_routers.py (meta conversation endpoints).
# Agents are mocked to avoid real LLM calls.

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from web_api.main import app as web_app
from api.dependencies import get_db_manager, get_workspace_manager
from api.auth import get_optional_user
from web_api.auth import get_current_user
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager


@pytest.fixture(name="web_client")
def web_client_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_MODE", "normal")
    test_db = DBManager(str(tmp_path / "web_meta_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


HEADERS = {"Cf-Access-User-Email": "meta@test.com"}


# ── Helper ──

def _create_project(web_client, project_id="meta_proj"):
    resp = web_client.post(
        "/api/projects",
        json={"project_id": project_id},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    return project_id


# ── detect-intent ──


def test_detect_intent(web_client):
    """detect-intent should return intent classification."""
    with patch("api.meta_routers.detect_intent", return_value={
        "intent": "new_project", "reasoning": "User wants to build something new"
    }):
        resp = web_client.post(
            "/api/meta/detect-intent",
            json={"prompt": "Build me a todo app"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["intent"] == "new_project"
    assert data["reasoning"] is not None


# ── meta start ──


def test_meta_start_asking(web_client):
    """Starting a meta conversation should return a message."""
    _create_project(web_client)
    with patch("api.meta_routers.MetaConversationAgent") as MockAgent:
        MockAgent.return_value.start.return_value = {
            "status": "asking",
            "message": "Nice idea! What features do you want?",
            "analysis_so_far": None,
            "project_brief": None,
        }
        resp = web_client.post(
            "/api/meta/start",
            json={"prompt": "I want a web app", "project_id": "meta_proj"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "asking"
    assert data["message"] == "Nice idea! What features do you want?"
    # Interaction meta: meta_conversation phase
    assert data["interaction"] is not None
    assert data["interaction"]["phase"] == "meta_conversation"
    assert "answer" in data["interaction"]["available_actions"]


def test_meta_start_complete(web_client):
    """Starting a meta conversation that immediately completes should return brief."""
    _create_project(web_client)
    with patch("api.meta_routers.MetaConversationAgent") as MockAgent:
        MockAgent.return_value.start.return_value = {
            "status": "complete",
            "message": "Got it, here's the brief!",
            "project_brief": {"goals": ["Build app"]},
        }
        resp = web_client.post(
            "/api/meta/start",
            json={"prompt": "Simple function", "project_id": "meta_proj"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["project_brief"] is not None
    assert data["interaction"]["phase"] == "brief_review"
    assert "approve" in data["interaction"]["available_actions"]


def test_meta_start_project_not_found(web_client):
    """Starting meta for nonexistent project should return 404."""
    resp = web_client.post(
        "/api/meta/start",
        json={"prompt": "test", "project_id": "nope"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_meta_start_owner_check(web_client):
    """Another user cannot start meta on a project they don't own."""
    _create_project(web_client)
    bob = {"Cf-Access-User-Email": "bob@meta.com"}
    resp = web_client.post(
        "/api/meta/start",
        json={"prompt": "test", "project_id": "meta_proj"},
        headers=bob,
    )
    assert resp.status_code == 404


# ── meta next ──


def test_meta_next_asking(web_client):
    """Continuing meta conversation should work."""
    _create_project(web_client)
    with patch("api.meta_routers.MetaConversationAgent") as MockAgent:
        MockAgent.return_value.next_turn.return_value = {
            "status": "asking",
            "message": "Great, any more details?",
        }
        resp = web_client.post(
            "/api/meta/next",
            json={
                "project_id": "meta_proj",
                "answer": "I want user auth",
                "history": [
                    {"message": "What do you want?", "answer": "A web app"},
                ],
            },
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "asking"
    assert data["interaction"]["phase"] == "meta_conversation"
    assert data["interaction"]["turn"] == 2  # len(history) + 1


def test_meta_next_project_not_found(web_client):
    """Next turn on nonexistent project should return 404."""
    resp = web_client.post(
        "/api/meta/next",
        json={"project_id": "nope", "answer": "test", "history": []},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── meta force ──


def test_meta_force_brief(web_client):
    """Force brief generation should work."""
    _create_project(web_client)
    with patch("api.meta_routers.MetaConversationAgent") as MockAgent:
        MockAgent.return_value.force_brief.return_value = {
            "status": "complete",
            "message": "Here's the brief.",
            "project_brief": {"goals": ["Forced goal"]},
        }
        resp = web_client.post(
            "/api/meta/force",
            json={
                "project_id": "meta_proj",
                "history": [
                    {"message": "What?", "answer": "Something"},
                ],
            },
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["interaction"]["phase"] == "brief_review"


def test_meta_force_project_not_found(web_client):
    """Force on nonexistent project should return 404."""
    resp = web_client.post(
        "/api/meta/force",
        json={"project_id": "nope", "history": []},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── Task-scoped meta ──


def test_task_meta_start(web_client):
    """Starting a task-scoped meta conversation should create a task and return status."""
    _create_project(web_client)
    with patch("api.meta_routers.TaskMetaConversationAgent") as MockAgent:
        MockAgent.return_value.start.return_value = {
            "status": "asking",
            "message": "What should this task do exactly?",
        }
        MockAgent.return_value.set_project_context = MagicMock()
        resp = web_client.post(
            "/api/meta/task/start",
            json={"project_id": "meta_proj", "prompt": "Add login page"},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "asking"
    assert data["task_id"] is not None
    assert data["interaction"]["phase"] == "task_meta"


def test_task_meta_next(web_client):
    """Continuing a task meta conversation should work."""
    _create_project(web_client)
    # First create a task
    task_resp = web_client.post(
        "/api/tasks",
        json={"project_id": "meta_proj", "prompt": "test task"},
        headers=HEADERS,
    )
    task_id = task_resp.json()["id"]

    with patch("api.meta_routers.TaskMetaConversationAgent") as MockAgent:
        MockAgent.return_value.next_turn.return_value = {
            "status": "complete",
            "message": "Got it, here's the task spec.",
            "task_spec": {"description": "Implement login"},
        }
        resp = web_client.post(
            "/api/meta/task/next",
            json={
                "task_id": task_id,
                "answer": "It should have email/password",
                "history": [],
            },
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["interaction"]["phase"] == "task_meta"


def test_task_meta_next_not_owner_404(web_client):
    """Another user cannot interact with task they don't own."""
    _create_project(web_client)
    task_resp = web_client.post(
        "/api/tasks",
        json={"project_id": "meta_proj", "prompt": "test"},
        headers=HEADERS,
    )
    task_id = task_resp.json()["id"]

    bob = {"Cf-Access-User-Email": "bob@task.com"}
    resp = web_client.post(
        "/api/meta/task/next",
        json={"task_id": task_id, "answer": "test", "history": []},
        headers=bob,
    )
    assert resp.status_code == 404


def test_task_meta_force(web_client):
    """Forcing task meta completion should work."""
    _create_project(web_client)
    task_resp = web_client.post(
        "/api/tasks",
        json={"project_id": "meta_proj", "prompt": "test"},
        headers=HEADERS,
    )
    task_id = task_resp.json()["id"]

    with patch("api.meta_routers.TaskMetaConversationAgent") as MockAgent:
        MockAgent.return_value.force_brief.return_value = {
            "status": "complete",
            "message": "Here's the forced spec.",
            "task_spec": {"description": "Forced spec"},
        }
        resp = web_client.post(
            "/api/meta/task/force",
            json={"task_id": task_id, "history": []},
            headers=HEADERS,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "complete"
    assert data["interaction"]["phase"] == "task_meta"
