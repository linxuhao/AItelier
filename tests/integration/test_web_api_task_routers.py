# tests/integration/test_web_api_task_routers.py
# Integration tests for web_api/routers.py (task endpoints with auth).
# These tests focus on task-specific logic beyond auth (covered in test_web_api_auth.py).

import pytest
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
    test_db = DBManager(str(tmp_path / "web_task_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


HEADERS = {"Cf-Access-User-Email": "user@test.com"}


def _ensure_project(pid: str):
    """Seed a run/project row so task creation (which requires an existing
    project) succeeds. /api/tasks intentionally does NOT auto-create."""
    db = web_app.dependency_overrides[get_db_manager]()
    db.ensure_project(pid, owner_email="user@test.com")


# ── Create task ──


def test_create_task_with_brief_writes_file(web_client, tmp_path):
    """Creating a task with project_brief should write project_brief.md to workspace."""
    _ensure_project("brief_proj")
    resp = web_client.post(
        "/api/tasks",
        json={
            "project_id": "brief_proj",
            "prompt": "build it",
            "project_brief": "# My Brief\nSome details.",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == "brief_proj"
    assert data["owner_email"] == "user@test.com"


def test_create_task_without_brief(web_client):
    """Task creation without project_brief should succeed."""
    _ensure_project("no_brief")
    resp = web_client.post(
        "/api/tasks",
        json={"project_id": "no_brief", "prompt": "hello"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["project_id"] == "no_brief"


def test_create_task_requires_existing_project(web_client):
    """Creating a task for a non-existent project is rejected (no auto-create)."""
    resp = web_client.post(
        "/api/tasks",
        json={"project_id": "missing_proj", "prompt": "test"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── List tasks ──


def test_list_tasks_with_limit_and_offset(web_client):
    """Pagination should work correctly."""
    for i in range(5):
        _ensure_project(f"pag_proj_{i}")
        web_client.post(
            "/api/tasks",
            json={"project_id": f"pag_proj_{i}", "prompt": f"task {i}"},
            headers=HEADERS,
        )

    # Get first 2
    resp = web_client.get("/api/tasks?limit=2&offset=0", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # Get next page
    resp = web_client.get("/api/tasks?limit=2&offset=2", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# ── Get task ──


def test_get_task_returns_full_data(web_client):
    """GET task should return all fields defined in TaskResponse."""
    _ensure_project("get_proj")
    resp = web_client.post(
        "/api/tasks",
        json={"project_id": "get_proj", "prompt": "test prompt text"},
        headers=HEADERS,
    )
    task_id = resp.json()["id"]

    resp = web_client.get(f"/api/tasks/{task_id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["status"] == "pending"
    assert data["project_id"] == "get_proj"
    assert data["owner_email"] == "user@test.com"


# ── Rollback ──


def test_rollback_nonexistent_task_404(web_client):
    """Rollback on missing task should return 404."""
    resp = web_client.post(
        "/api/tasks/99999/rollback",
        json={"commit_hash": "abc123"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


# ── Step output ──


def test_step_output_nonexistent_task_404(web_client):
    """Step output for missing task should return 404."""
    resp = web_client.get("/api/tasks/99999/steps/1/output", headers=HEADERS)
    assert resp.status_code == 404


# ── SSE stream ──
# Note: SSE stream tests are covered in tests/integration/test_api_sse.py
