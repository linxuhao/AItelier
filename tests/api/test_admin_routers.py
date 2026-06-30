"""Tests for the admin_routers.py endpoints.

Covers:
- GET /api/admin/logged-users (basic smoke test)
- DELETE /api/admin/logged-users/{email} (success, 404, auth)
"""

import pytest


@pytest.fixture
def auth_headers():
    """Simulate a writer user (email + groups)."""
    return {
        "Cf-Access-Authenticated-User-Email": "admin@test.com",
        "Cf-Access-Authenticated-User-Groups": "writers",
    }


@pytest.mark.usefixtures("client")
def test_get_logged_users(client, auth_headers):
    """GET /api/admin/logged-users returns 200 for a writer."""
    resp = client.get("/api/admin/logged-users", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.usefixtures("client")
def test_delete_user_success(client, auth_headers):
    """DELETE /api/admin/logged-users/{email} deletes an existing user."""
    # Insert via the SAME DB instance the endpoint uses. The client fixture
    # overrides get_db_manager with a per-test DB; calling get_db_manager()
    # directly would hit a different DB, so the endpoint wouldn't see the row.
    from api.main import app
    from api.dependencies import get_db_manager

    db = app.dependency_overrides[get_db_manager]()
    db.upsert_user("delete-me@test.com", "Delete Me", source="test")  # commits internally

    email_to_delete = "delete-me@test.com"
    resp = client.delete(
        f"/api/admin/logged-users/{email_to_delete}",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["email"] == email_to_delete


@pytest.mark.usefixtures("client")
def test_delete_user_not_found(client, auth_headers):
    """DELETE /api/admin/logged-users/nonexistent returns 404."""
    resp = client.delete(
        "/api/admin/logged-users/nobody@example.com",
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.skip(reason="write-gate and require_writer's dev-fallback are both "
                         "bypassed under app.state._test_mode; writer-only "
                         "enforcement is covered by tests/integration/test_cli_middleware.py")
@pytest.mark.usefixtures("client")
def test_delete_user_no_auth(client):
    """DELETE without auth headers returns 403 (enforced by the write-gate in prod)."""
    resp = client.delete("/api/admin/logged-users/someone@test.com")
    assert resp.status_code == 403
