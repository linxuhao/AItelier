# tests/unit/test_cli_client.py
# Unit tests for cli/client.py (APIClient).
# Uses httpx.MockTransport to mock HTTP calls without a real server.

import pytest
import httpx
from unittest.mock import MagicMock
from cli.client import APIClient

BASE = "http://testserver"


def _make_client(handler):
    """Create APIClient backed by a mock transport."""
    transport = httpx.MockTransport(handler)
    raw = httpx.Client(base_url=BASE, transport=transport)
    client = APIClient(base_url=BASE)
    client._client = raw
    return client


# ── Constructor ──


def test_default_base_url():
    client = APIClient()
    assert "localhost" in client.base_url


def test_custom_base_url():
    client = APIClient(base_url="http://example.com:8080")
    assert client.base_url == "http://example.com:8080"


def test_base_url_trailing_slash_stripped():
    client = APIClient(base_url="http://example.com/")
    assert client.base_url == "http://example.com"


# ── health ──


def test_health_returns_true():
    def handler(request):
        return httpx.Response(200, json={"status": "ok"})
    client = _make_client(handler)
    assert client.health() is True


def test_health_returns_false_on_connection_error():
    client = APIClient(base_url=BASE)
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.ConnectError("refused")
    client._client = mock_client
    assert client.health() is False


def test_health_returns_false_on_timeout():
    client = APIClient(base_url=BASE)
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.TimeoutException("timed out")
    client._client = mock_client
    assert client.health() is False


# ── Task methods ──


def test_create_task():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/tasks"
        return httpx.Response(200, json={"id": 1, "project_id": "p1", "status": "pending"})
    client = _make_client(handler)
    result = client.create_task("p1", "test prompt")
    assert result["id"] == 1


def test_create_task_with_brief():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["project_brief"] == "# Brief"
        return httpx.Response(200, json={"id": 2, "project_id": "p2"})
    client = _make_client(handler)
    result = client.create_task("p2", "prompt", project_brief="# Brief")
    assert result["id"] == 2


def test_get_task():
    def handler(request):
        assert request.url.path == "/api/tasks/42"
        return httpx.Response(200, json={"id": 42, "prompt": "hello"})
    client = _make_client(handler)
    result = client.get_task(42)
    assert result["id"] == 42


def test_list_tasks():
    def handler(request):
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json=[{"id": 1}])
    client = _make_client(handler)
    result = client.list_tasks(limit=10)
    assert len(result) == 1


def test_get_step_output():
    def handler(request):
        assert "/steps/1/output" in request.url.path
        return httpx.Response(200, json={"step_id": "1", "files": {}})
    client = _make_client(handler)
    result = client.get_step_output(1, "1")
    assert result["step_id"] == "1"


def test_rollback():
    def handler(request):
        assert "rollback" in request.url.path
        return httpx.Response(200, json={"success": True})
    client = _make_client(handler)
    result = client.rollback(1, "abc123")
    assert result["success"] is True


# ── Project methods ──


def test_list_projects():
    def handler(request):
        assert request.url.path == "/api/projects"
        return httpx.Response(200, json=[{"project_id": "p1"}])
    client = _make_client(handler)
    result = client.list_projects()
    assert len(result) == 1


def test_create_project():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["project_id"] == "new_proj"
        assert body["name"] == "Test"
        return httpx.Response(201, json={"project_id": "new_proj", "name": "Test"})
    client = _make_client(handler)
    result = client.create_project("new_proj", name="Test")
    assert result["project_id"] == "new_proj"


def test_get_project():
    def handler(request):
        return httpx.Response(200, json={"project_id": "p1"})
    client = _make_client(handler)
    result = client.get_project("p1")
    assert result["project_id"] == "p1"


def test_delete_project():
    def handler(request):
        assert request.method == "DELETE"
        return httpx.Response(200, json={"success": True})
    client = _make_client(handler)
    result = client.delete_project("p1")
    assert result["success"] is True


def test_update_project():
    def handler(request):
        assert request.method == "PATCH"
        return httpx.Response(200, json={"project_id": "p1", "name": "Updated"})
    client = _make_client(handler)
    result = client.update_project("p1", name="Updated")
    assert result["name"] == "Updated"


def test_list_tasks_by_project():
    def handler(request):
        assert "/tasks" in request.url.path
        return httpx.Response(200, json=[{"id": 1}])
    client = _make_client(handler)
    result = client.list_tasks_by_project("p1")
    assert len(result) == 1


# ── Settings ──


def test_get_scheduler_settings():
    def handler(request):
        assert "/settings/scheduler" in request.url.path
        return httpx.Response(200, json={"scheduler_type": "interval", "scheduler_interval": 60})
    client = _make_client(handler)
    result = client.get_scheduler_settings()
    assert result["scheduler_type"] == "interval"


def test_update_scheduler_settings():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["scheduler_type"] == "interval"
        assert body["scheduler_interval"] == 120
        return httpx.Response(200, json={"scheduler_type": "interval", "scheduler_interval": 120})
    client = _make_client(handler)
    result = client.update_scheduler_settings("interval", scheduler_interval=120)
    assert result["scheduler_interval"] == 120


# ── Meta ──


def test_detect_intent():
    def handler(request):
        assert "detect-intent" in request.url.path
        return httpx.Response(200, json={"intent": "new_project", "reasoning": "test"})
    client = _make_client(handler)
    result = client.detect_intent("build app")
    assert result["intent"] == "new_project"


def test_meta_start():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["prompt"] == "test"
        assert body["project_id"] == "p1"
        return httpx.Response(200, json={"status": "asking", "message": "What?"})
    client = _make_client(handler)
    result = client.meta_start("test", "p1")
    assert result["status"] == "asking"


def test_meta_next():
    def handler(request):
        return httpx.Response(200, json={"status": "complete"})
    client = _make_client(handler)
    result = client.meta_next("p1", "answer", [])
    assert result["status"] == "complete"


def test_meta_force():
    def handler(request):
        return httpx.Response(200, json={"status": "complete"})
    client = _make_client(handler)
    result = client.meta_force("p1", [])
    assert result["status"] == "complete"


def test_task_meta_start():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["prompt"] == "test"
        return httpx.Response(200, json={"status": "asking", "task_id": 1})
    client = _make_client(handler)
    result = client.task_meta_start("p1", "test")
    assert result["status"] == "asking"


def test_task_meta_next():
    def handler(request):
        return httpx.Response(200, json={"status": "complete"})
    client = _make_client(handler)
    result = client.task_meta_next(1, "answer", [])
    assert result["status"] == "complete"


def test_task_meta_force():
    def handler(request):
        return httpx.Response(200, json={"status": "complete"})
    client = _make_client(handler)
    result = client.task_meta_force(1, [])
    assert result["status"] == "complete"


# ── Error handling ──


def test_http_error_raises():
    def handler(request):
        return httpx.Response(404, json={"detail": "Not found"})
    client = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_task(99999)
