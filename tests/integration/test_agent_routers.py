# tests/integration/test_agent_routers.py
# Integration tests for POST /api/agent/chat SSE endpoint.

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    """TestClient with mocked agent streaming."""
    from api.main import app
    from api.dependencies import get_db_manager, get_workspace_manager
    from core.db_manager import DBManager
    from core.workspace_manager import WorkspaceManager

    test_db = DBManager(str(tmp_path / "test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))

    app.dependency_overrides[get_db_manager] = lambda: test_db
    app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    app.state._test_mode = True

    with TestClient(app) as c:
        yield c

    app.state._test_mode = False
    app.dependency_overrides.clear()


class TestAgentChatEndpoint:
    def test_endpoint_exists(self, client):
        """POST /api/agent/chat should return 200 (SSE stream)."""
        with patch("core.meta_agent.MetaAgent.chat") as mock_chat:
            async def fake_chat(*args, **kwargs):
                yield {"type": "text_delta", "content": "Hello!"}
                yield {"type": "done", "message": {"role": "assistant", "content": "Hello!"}}

            mock_chat.side_effect = lambda *a, **kw: fake_chat()

            resp = client.post(
                "/api/agent/chat",
                json={"message": "hi", "history": [], "current_project": None},
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"

            # Parse SSE events
            events = []
            for line in resp.text.strip().split("\n"):
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            assert len(events) == 2
            assert events[0]["type"] == "text_delta"
            assert events[0]["content"] == "Hello!"
            assert events[1]["type"] == "done"

    def test_empty_message(self, client):
        """POST /api/agent/chat with empty message should still stream."""
        with patch("core.meta_agent.MetaAgent.chat") as mock_chat:
            async def fake_chat(*args, **kwargs):
                yield {"type": "done", "message": {"role": "assistant", "content": "Sure!"}}

            mock_chat.side_effect = lambda *a, **kw: fake_chat()

            resp = client.post(
                "/api/agent/chat",
                json={"message": "", "history": []},
            )
            assert resp.status_code == 200

    def test_with_history(self, client):
        """POST /api/agent/chat passes history to agent."""
        with patch("core.meta_agent.MetaAgent.chat") as mock_chat:
            async def fake_chat(*args, **kwargs):
                yield {"type": "done", "message": {"role": "assistant", "content": "OK"}}

            mock_chat.side_effect = lambda *a, **kw: fake_chat()

            history = [{"role": "user", "content": "previous msg"}]
            resp = client.post(
                "/api/agent/chat",
                json={"message": "follow up", "history": history, "current_project": "my-proj"},
            )
            assert resp.status_code == 200

    def test_error_handling(self, client):
        """Agent errors are returned as SSE error events."""
        with patch("core.meta_agent.MetaAgent.chat") as mock_chat:
            async def fake_chat(*args, **kwargs):
                yield {"type": "error", "message": "Something broke"}

            mock_chat.side_effect = lambda *a, **kw: fake_chat()

            resp = client.post(
                "/api/agent/chat",
                json={"message": "test"},
            )
            assert resp.status_code == 200
            events = []
            for line in resp.text.strip().split("\n"):
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
            assert events[0]["type"] == "error"
            assert "Something broke" in events[0]["message"]

    def test_tool_call_events(self, client):
        """Tool call and result events are streamed correctly."""
        with patch("core.meta_agent.MetaAgent.chat") as mock_chat:
            async def fake_chat(*args, **kwargs):
                yield {"type": "text_delta", "content": "Let me check..."}
                yield {"type": "tool_call", "name": "list_projects", "args": {}}
                yield {"type": "tool_result", "name": "list_projects", "result": {"projects": []}}
                yield {"type": "done", "message": {"role": "assistant", "content": "No projects yet."}}

            mock_chat.side_effect = lambda *a, **kw: fake_chat()

            resp = client.post(
                "/api/agent/chat",
                json={"message": "show projects"},
            )
            events = []
            for line in resp.text.strip().split("\n"):
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

            assert len(events) == 4
            assert events[0]["type"] == "text_delta"
            assert events[1]["type"] == "tool_call"
            assert events[1]["name"] == "list_projects"
            assert events[2]["type"] == "tool_result"
            assert events[2]["result"]["projects"] == []
            assert events[3]["type"] == "done"
