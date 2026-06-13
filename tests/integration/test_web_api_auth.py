# tests/integration/test_web_api_auth.py
# Tests for web_api Cloudflare auth and multi-tenant isolation.
# Tests run in NORMAL mode by default. Demo mode tests use separate fixture.

import os
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
    """TestClient for the web_api FastAPI app in NORMAL mode."""
    monkeypatch.setenv("AITELIER_MODE", "normal")
    test_db = DBManager(str(tmp_path / "web_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


@pytest.fixture(name="demo_client")
def demo_client_fixture(tmp_path, monkeypatch):
    """TestClient for the web_api FastAPI app in DEMO mode."""
    monkeypatch.setenv("AITELIER_MODE", "demo")
    test_db = DBManager(str(tmp_path / "demo_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "demo_ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


# ── Auth tests ──


def test_no_header_returns_401(web_client: TestClient):
    """Requests without Cf-Access-User-Email should get 401."""
    resp = web_client.get("/api/tasks")
    assert resp.status_code == 401
    assert "Authentication required" in resp.json()["detail"]


def test_health_check_no_auth(web_client: TestClient):
    """Health endpoint should not require auth."""
    resp = web_client.get("/health")
    assert resp.status_code == 200


def test_valid_header_authenticates(web_client: TestClient):
    """Valid Cf-Access-User-Email header should authenticate the request."""
    headers = {"Cf-Access-User-Email": "alice@example.com"}
    resp = web_client.get("/api/tasks", headers=headers)
    assert resp.status_code == 200


def test_header_creates_user(web_client: TestClient):
    """First request with a new email should auto-create the user."""
    email = "newuser@example.com"
    headers = {"Cf-Access-User-Email": email}

    web_client.get("/api/tasks", headers=headers)

    db = get_db_manager()
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    assert row is not None
    assert row["email"] == email
    assert row["source"] == "cloudflare"


def test_header_email_trimmed_and_lowercased(web_client: TestClient):
    """Email should be stripped of whitespace and lowercased."""
    headers = {"Cf-Access-User-Email": "  Alice@Example.COM  "}
    resp = web_client.get("/api/tasks", headers=headers)
    assert resp.status_code == 200

    db = get_db_manager()
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", ("alice@example.com",)).fetchone()
    assert row is not None


def test_all_endpoints_require_auth(web_client: TestClient):
    """All /api/* endpoints should reject without auth."""
    no_auth_routes = [
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("GET", "/api/projects"),
        ("POST", "/api/projects"),
        ("GET", "/api/settings/scheduler"),
        ("POST", "/api/settings/scheduler"),
        ("POST", "/api/meta/start"),
        ("POST", "/api/meta/next"),
        ("POST", "/api/meta/force"),
    ]
    for method, path in no_auth_routes:
        resp = web_client.request(method, path, json={})
        assert resp.status_code == 401, f"{method} {path} should require auth"


# ── Multi-tenant isolation tests ──


def test_create_task_sets_owner(web_client: TestClient):
    """Created task should have owner_email from auth header."""
    headers = {"Cf-Access-User-Email": "owner@test.com"}
    resp = web_client.post(
        "/api/tasks",
        json={"project_id": "tenanted_proj", "prompt": "hello"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["owner_email"] == "owner@test.com"


def test_create_project_sets_owner(web_client: TestClient):
    """Created project should have owner_email from auth header."""
    headers = {"Cf-Access-User-Email": "projowner@test.com"}
    resp = web_client.post(
        "/api/projects",
        json={"project_id": "tenanted_project", "name": "My Project"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["owner_email"] == "projowner@test.com"


def test_user_sees_only_own_tasks(web_client: TestClient):
    """Users should only see their own tasks."""
    alice = {"Cf-Access-User-Email": "alice@t.com"}
    bob = {"Cf-Access-User-Email": "bob@t.com"}

    # Alice creates a task
    web_client.post("/api/tasks", json={"project_id": "a_proj", "prompt": "Alice task"}, headers=alice)
    # Bob creates a task
    web_client.post("/api/tasks", json={"project_id": "b_proj", "prompt": "Bob task"}, headers=bob)

    # Alice sees only her task
    alice_tasks = web_client.get("/api/tasks", headers=alice).json()
    assert len(alice_tasks) == 1
    assert alice_tasks[0]["project_id"] == "a_proj"
    assert alice_tasks[0]["owner_email"] == "alice@t.com"

    # Bob sees only his task
    bob_tasks = web_client.get("/api/tasks", headers=bob).json()
    assert len(bob_tasks) == 1
    assert bob_tasks[0]["project_id"] == "b_proj"
    assert bob_tasks[0]["owner_email"] == "bob@t.com"


def test_user_sees_only_own_projects(web_client: TestClient):
    """Users should only see their own projects."""
    alice = {"Cf-Access-User-Email": "alice_p@t.com"}
    bob = {"Cf-Access-User-Email": "bob_p@t.com"}

    web_client.post("/api/projects", json={"project_id": "alice_proj"}, headers=alice)
    web_client.post("/api/projects", json={"project_id": "bob_proj"}, headers=bob)

    alice_projects = web_client.get("/api/projects", headers=alice).json()
    assert len(alice_projects) == 1
    assert alice_projects[0]["project_id"] == "alice_proj"

    bob_projects = web_client.get("/api/projects", headers=bob).json()
    assert len(bob_projects) == 1
    assert bob_projects[0]["project_id"] == "bob_proj"


def test_user_cannot_access_others_task(web_client: TestClient):
    """User should get 404 when accessing another user's task."""
    alice = {"Cf-Access-User-Email": "alice_404@t.com"}
    bob = {"Cf-Access-User-Email": "bob_404@t.com"}

    resp = web_client.post(
        "/api/tasks", json={"project_id": "a404_proj", "prompt": "secret"}, headers=alice
    )
    task_id = resp.json()["id"]

    # Bob tries to access Alice's task
    resp = web_client.get(f"/api/tasks/{task_id}", headers=bob)
    assert resp.status_code == 404


def test_user_cannot_delete_others_project(web_client: TestClient):
    """User should get 404 when trying to delete another user's project."""
    alice = {"Cf-Access-User-Email": "alice_del@t.com"}
    bob = {"Cf-Access-User-Email": "bob_del@t.com"}

    web_client.post("/api/projects", json={"project_id": "alice_del_proj"}, headers=alice)

    resp = web_client.delete("/api/projects/alice_del_proj", headers=bob)
    assert resp.status_code == 404

    # Alice can still see it
    resp = web_client.get("/api/projects/alice_del_proj", headers=alice)
    assert resp.status_code == 200


def test_user_cannot_rollback_others_task(web_client: TestClient):
    """User should get 404 on rollback of another user's task."""
    alice = {"Cf-Access-User-Email": "alice_rb@t.com"}
    bob = {"Cf-Access-User-Email": "bob_rb@t.com"}

    resp = web_client.post(
        "/api/tasks", json={"project_id": "arb_proj", "prompt": "test"}, headers=alice
    )
    task_id = resp.json()["id"]

    resp = web_client.post(
        f"/api/tasks/{task_id}/rollback",
        json={"commit_hash": "abc123"},
        headers=bob,
    )
    assert resp.status_code == 404


# ── Demo mode tests ──


def test_demo_health_shows_mode(demo_client: TestClient):
    """Health endpoint should report demo mode."""
    resp = demo_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "demo"


def test_demo_user_can_read_others_projects(demo_client: TestClient):
    """In demo mode, users can read projects owned by others."""
    alice = {"Cf-Access-User-Email": "demo_alice@t.com"}
    bob = {"Cf-Access-User-Email": "demo_bob@t.com"}

    demo_client.post("/api/projects", json={"project_id": "demo_alice_proj"}, headers=alice)

    # Bob can see Alice's project in the list
    bob_projects = demo_client.get("/api/projects", headers=bob).json()
    assert len(bob_projects) == 1
    assert bob_projects[0]["project_id"] == "demo_alice_proj"

    # Bob can GET Alice's project directly
    resp = demo_client.get("/api/projects/demo_alice_proj", headers=bob)
    assert resp.status_code == 200


def test_demo_user_can_read_others_tasks(demo_client: TestClient):
    """In demo mode, users can read tasks owned by others."""
    alice = {"Cf-Access-User-Email": "demo_alice_t@t.com"}
    bob = {"Cf-Access-User-Email": "demo_bob_t@t.com"}

    resp = demo_client.post(
        "/api/tasks", json={"project_id": "demo_t_proj", "prompt": "demo task"}, headers=alice
    )
    task_id = resp.json()["id"]

    # Bob can see Alice's task in the list
    bob_tasks = demo_client.get("/api/tasks", headers=bob).json()
    assert len(bob_tasks) == 1

    # Bob can GET Alice's task directly
    resp = demo_client.get(f"/api/tasks/{task_id}", headers=bob)
    assert resp.status_code == 200


def test_demo_user_cannot_delete_others_project(demo_client: TestClient):
    """In demo mode, write operations still require ownership."""
    alice = {"Cf-Access-User-Email": "demo_alice_del@t.com"}
    bob = {"Cf-Access-User-Email": "demo_bob_del@t.com"}

    demo_client.post("/api/projects", json={"project_id": "demo_del_proj"}, headers=alice)

    # Bob cannot delete Alice's project
    resp = demo_client.delete("/api/projects/demo_del_proj", headers=bob)
    assert resp.status_code == 404


def test_demo_user_cannot_rollback_others_task(demo_client: TestClient):
    """In demo mode, write operations still require ownership."""
    alice = {"Cf-Access-User-Email": "demo_alice_rb@t.com"}
    bob = {"Cf-Access-User-Email": "demo_bob_rb@t.com"}

    resp = demo_client.post(
        "/api/tasks", json={"project_id": "demo_rb_proj", "prompt": "test"}, headers=alice
    )
    task_id = resp.json()["id"]

    resp = demo_client.post(
        f"/api/tasks/{task_id}/rollback",
        json={"commit_hash": "abc123"},
        headers=bob,
    )
    assert resp.status_code == 404


def test_demo_user_can_read_project_tasks(demo_client: TestClient):
    """In demo mode, users can list tasks of projects they don't own."""
    alice = {"Cf-Access-User-Email": "demo_alice_pt@t.com"}
    bob = {"Cf-Access-User-Email": "demo_bob_pt@t.com"}

    demo_client.post("/api/projects", json={"project_id": "demo_pt_proj"}, headers=alice)
    demo_client.post(
        "/api/tasks", json={"project_id": "demo_pt_proj", "prompt": "task1"}, headers=alice
    )

    # Bob can list tasks of Alice's project
    resp = demo_client.get("/api/projects/demo_pt_proj/tasks", headers=bob)
    assert resp.status_code == 200
    assert len(resp.json()) == 1
