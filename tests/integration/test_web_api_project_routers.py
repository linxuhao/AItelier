# tests/integration/test_web_api_project_routers.py
# Integration tests for web_api/project_routers.py (project CRUD, repo validation, ownership).

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
    test_db = DBManager(str(tmp_path / "web_proj_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


HEADERS = {"Cf-Access-User-Email": "owner@test.com"}


# ── Create project ──


def test_create_project_new(web_client):
    """Creating a new project should succeed with defaults."""
    resp = web_client.post(
        "/api/projects",
        json={"project_id": "new_proj", "name": "My Project"},
        headers=HEADERS,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == "new_proj"
    assert data["owner_email"] == "owner@test.com"


def test_create_project_duplicate_409(web_client):
    """Duplicate project_id should return 409."""
    web_client.post(
        "/api/projects",
        json={"project_id": "dup_proj"},
        headers=HEADERS,
    )
    resp = web_client.post(
        "/api/projects",
        json={"project_id": "dup_proj"},
        headers=HEADERS,
    )
    assert resp.status_code == 409


def test_create_project_existing_missing_path_400(web_client):
    """repo_type=existing without repo_path should return 400."""
    resp = web_client.post(
        "/api/projects",
        json={"project_id": "exist_proj", "repo_type": "existing"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "repo_path is required" in resp.json()["detail"]


def test_create_project_existing_nonexistent_path_400(web_client):
    """repo_type=existing with nonexistent path should return 400."""
    resp = web_client.post(
        "/api/projects",
        json={
            "project_id": "bad_path_proj",
            "repo_type": "existing",
            "repo_path": "/nonexistent/path/xyz",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


def test_create_project_clone_missing_url_400(web_client):
    """repo_type=clone without repo_url should return 400."""
    resp = web_client.post(
        "/api/projects",
        json={"project_id": "clone_proj", "repo_type": "clone"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "repo_url is required" in resp.json()["detail"]


# ── List projects ──


def test_list_projects_empty(web_client):
    """Empty DB should return empty list."""
    resp = web_client.get("/api/projects", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_projects_returns_created(web_client):
    """Listing should include created projects."""
    web_client.post(
        "/api/projects",
        json={"project_id": "list_proj"},
        headers=HEADERS,
    )
    resp = web_client.get("/api/projects", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["project_id"] == "list_proj"


# ── Get project ──


def test_get_project(web_client):
    """GET project should return full details."""
    web_client.post(
        "/api/projects",
        json={"project_id": "get_proj", "name": "Get Test"},
        headers=HEADERS,
    )
    resp = web_client.get("/api/projects/get_proj", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["project_id"] == "get_proj"


def test_get_project_not_found_404(web_client):
    """GET nonexistent project should return 404."""
    resp = web_client.get("/api/projects/no_such_proj", headers=HEADERS)
    assert resp.status_code == 404


# ── Patch project ──


def test_patch_project_updates_name(web_client):
    """Patching should update fields."""
    web_client.post(
        "/api/projects",
        json={"project_id": "patch_proj"},
        headers=HEADERS,
    )
    resp = web_client.patch(
        "/api/projects/patch_proj",
        params={"name": "Updated Name"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


def test_patch_project_not_found_404(web_client):
    """Patching nonexistent project should return 404."""
    resp = web_client.patch(
        "/api/projects/nope",
        params={"name": "x"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_patch_project_owner_check(web_client):
    """Another user cannot patch a project they don't own."""
    web_client.post(
        "/api/projects",
        json={"project_id": "owner_patch_proj"},
        headers=HEADERS,
    )
    bob = {"Cf-Access-User-Email": "bob@patch.com"}
    resp = web_client.patch(
        "/api/projects/owner_patch_proj",
        params={"name": "Hacked"},
        headers=bob,
    )
    assert resp.status_code == 404


# ── Delete project ──


def test_delete_project_cascade(web_client):
    """Deleting with cascade=true should remove project."""
    web_client.post(
        "/api/projects",
        json={"project_id": "del_proj"},
        headers=HEADERS,
    )
    resp = web_client.delete("/api/projects/del_proj?cascade=true", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # Verify gone
    resp = web_client.get("/api/projects/del_proj", headers=HEADERS)
    assert resp.status_code == 404


def test_delete_project_no_cascade(web_client):
    """Deleting without cascade should also remove project."""
    web_client.post(
        "/api/projects",
        json={"project_id": "del_nc_proj"},
        headers=HEADERS,
    )
    resp = web_client.delete("/api/projects/del_nc_proj?cascade=false", headers=HEADERS)
    assert resp.status_code == 200

    resp = web_client.get("/api/projects/del_nc_proj", headers=HEADERS)
    assert resp.status_code == 404


def test_delete_project_not_found_404(web_client):
    """Deleting nonexistent project should return 404."""
    resp = web_client.delete("/api/projects/nope", headers=HEADERS)
    assert resp.status_code == 404


# ── Project tasks ──


def test_list_project_tasks(web_client):
    """Listing tasks for a project should return only that project's tasks."""
    web_client.post(
        "/api/projects",
        json={"project_id": "pt_proj"},
        headers=HEADERS,
    )
    web_client.post(
        "/api/tasks",
        json={"project_id": "pt_proj", "prompt": "task1"},
        headers=HEADERS,
    )
    resp = web_client.get("/api/projects/pt_proj/tasks", headers=HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["prompt"] == "task1"


def test_list_project_tasks_not_found(web_client):
    """Listing tasks for nonexistent project should return 404."""
    resp = web_client.get("/api/projects/nope/tasks", headers=HEADERS)
    assert resp.status_code == 404
