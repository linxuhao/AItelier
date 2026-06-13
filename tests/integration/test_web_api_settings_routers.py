# tests/integration/test_web_api_settings_routers.py
# Integration tests for web_api/settings_routers.py (scheduler settings CRUD).

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
    test_db = DBManager(str(tmp_path / "web_settings_test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))
    web_app.dependency_overrides[get_db_manager] = lambda: test_db
    web_app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    web_app.dependency_overrides[get_optional_user] = get_current_user
    with TestClient(web_app) as c:
        yield c
    web_app.dependency_overrides.clear()
    web_app.dependency_overrides[get_optional_user] = get_current_user


HEADERS = {"Cf-Access-User-Email": "admin@test.com"}


# ── GET settings ──


def test_get_scheduler_settings_defaults(web_client):
    """GET scheduler settings should return defaults."""
    resp = web_client.get("/api/settings/scheduler", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "interval"
    assert data["scheduler_interval"] == 60


# ── POST settings — interval ──


def test_update_scheduler_interval(web_client):
    """Should update to interval mode with valid value."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "interval", "scheduler_interval": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "interval"
    assert data["scheduler_interval"] == 120

    # Verify persistence
    resp = web_client.get("/api/settings/scheduler", headers=HEADERS)
    assert resp.json()["scheduler_interval"] == 120


def test_update_scheduler_interval_too_low_400(web_client):
    """Interval < 5 should be rejected."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "interval", "scheduler_interval": 3},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "must be >= 5" in resp.json()["detail"]


def test_update_scheduler_interval_zero_400(web_client):
    """Interval = 0 should be rejected."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "interval", "scheduler_interval": 0},
        headers=HEADERS,
    )
    assert resp.status_code == 400


# ── POST settings — cron ──


def test_update_scheduler_cron(web_client):
    """Should update to cron mode with valid expression."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "cron", "scheduler_cron": "*/5 * * * *"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "cron"
    assert data["scheduler_cron"] == "*/5 * * * *"


def test_update_scheduler_cron_missing_400(web_client):
    """Cron type without cron expression should return 400."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "cron"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "scheduler_cron required" in resp.json()["detail"]


def test_update_scheduler_cron_invalid_fields_400(web_client):
    """Cron expression without 5 fields should return 400."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "cron", "scheduler_cron": "*/5 * *"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "5 fields" in resp.json()["detail"]


# ── Invalid type ──


def test_update_scheduler_invalid_type_400(web_client):
    """Invalid scheduler_type should be rejected."""
    resp = web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "bogus"},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "must be 'interval' or 'cron'" in resp.json()["detail"]


# ── Round-trip ──


def test_scheduler_settings_roundtrip(web_client):
    """Should be able to switch between interval and cron and back."""
    # Set cron
    web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "cron", "scheduler_cron": "0 * * * *"},
        headers=HEADERS,
    )
    resp = web_client.get("/api/settings/scheduler", headers=HEADERS)
    assert resp.json()["scheduler_type"] == "cron"

    # Switch back to interval
    web_client.post(
        "/api/settings/scheduler",
        json={"scheduler_type": "interval", "scheduler_interval": 30},
        headers=HEADERS,
    )
    resp = web_client.get("/api/settings/scheduler", headers=HEADERS)
    assert resp.json()["scheduler_type"] == "interval"
    assert resp.json()["scheduler_interval"] == 30
