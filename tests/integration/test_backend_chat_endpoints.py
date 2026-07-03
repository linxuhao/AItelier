"""Integration tests for backend chat persistence endpoints.

Covers:
  - DBManager.list_chat_sessions()
  - GET /api/agent/chat/history
  - GET /api/agent/sessions
  - POST /api/agent/chat/message
"""

import pytest
from fastapi.testclient import TestClient
from core.db_manager import DBManager


# ── Helpers ──────────────────────────────────────────────────────────────

def _seed_session(db: DBManager, session_id: str, project_id: str,
                  messages: list[tuple[str, str]] | None = None):
    """Helper: create a session and optionally save messages to it."""
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,)
        )
        conn.commit()
    if messages:
        for role, content in messages:
            db.save_chat_message_with_session(
                session_id, project_id, role, content,
            )


# ═════════════════════════════════════════════════════════════════════════
#  DBManager.list_chat_sessions tests
# ═════════════════════════════════════════════════════════════════════════


class TestListChatSessions:
    def test_empty_when_no_sessions(self, db_manager: DBManager):
        """Returns empty list when no sessions exist."""
        assert db_manager.list_chat_sessions() == []

    def test_excludes_sessions_without_messages(self, db_manager: DBManager):
        """Sessions without messages are excluded (HAVING message_count > 0)."""
        with db_manager.get_connection() as conn:
            conn.execute("INSERT INTO sessions (id) VALUES ('empty-sess')")
            conn.commit()
        assert db_manager.list_chat_sessions() == []

    def test_returns_sessions_with_messages(self, db_manager: DBManager):
        """Sessions with messages are returned with correct fields."""
        _seed_session(db_manager, "sess-1", "proj-a", [
            ("user", "Hello"),
            ("assistant", "Hi there"),
        ])
        results = db_manager.list_chat_sessions()
        assert len(results) == 1
        row = results[0]
        assert row["session_id"] == "sess-1"
        assert row["project_id"] == "proj-a"
        assert row["message_count"] == 2
        assert row["last_message"] == "Hi there"
        assert row["updated_at"] is not None

    def test_last_message_is_most_recent(self, db_manager: DBManager):
        """last_message contains the content of the newest message."""
        _seed_session(db_manager, "sess-last", "proj-x", [
            ("user", "First"),
            ("assistant", "Second"),
            ("user", "Third"),
        ])
        results = db_manager.list_chat_sessions()
        assert len(results) == 1
        assert results[0]["last_message"] == "Third"

    def test_first_message_is_opening_user_question(self, db_manager: DBManager):
        """first_message holds the session's opening user message (the question)."""
        _seed_session(db_manager, "sess-first", "proj-f", [
            ("user", "First"),
            ("assistant", "Second"),
            ("user", "Third"),
        ])
        results = db_manager.list_chat_sessions()
        assert len(results) == 1
        assert results[0]["first_message"] == "First"

    def test_filters_by_project_id(self, db_manager: DBManager):
        """When project_id is provided, only sessions for that project."""
        _seed_session(db_manager, "s-a", "proj-1", [("user", "A")])
        _seed_session(db_manager, "s-b", "proj-2", [("user", "B")])

        filtered = db_manager.list_chat_sessions(project_id="proj-1")
        assert len(filtered) == 1
        assert filtered[0]["session_id"] == "s-a"

        filtered = db_manager.list_chat_sessions(project_id="proj-2")
        assert len(filtered) == 1
        assert filtered[0]["session_id"] == "s-b"

    def test_limit_parameter(self, db_manager: DBManager):
        """Respects the limit parameter."""
        for i in range(5):
            _seed_session(db_manager, f"sess-{i}", "proj-l", [
                ("user", f"Msg {i}"),
            ])

        limited = db_manager.list_chat_sessions(limit=2)
        assert len(limited) == 2

        unlimited = db_manager.list_chat_sessions()
        assert len(unlimited) == 5

    def test_default_limit_returns_more_than_old_cap(self, db_manager: DBManager):
        """Regression: the default limit must exceed the old hardcoded cap of 20,
        so sessions past 20 don't silently vanish from the history dropdown.
        (Bug: 27 sessions existed but only 20 were ever returned.)"""
        for i in range(25):
            _seed_session(db_manager, f"many-{i:02d}", "proj-m", [
                ("user", f"Question {i}"),
            ])
        results = db_manager.list_chat_sessions()  # default limit
        assert len(results) == 25

    def test_ordered_by_updated_at_desc(self, db_manager: DBManager):
        """Sessions ordered by most recent activity first."""
        import time
        _seed_session(db_manager, "old", "proj-o", [("user", "Old message")])
        time.sleep(0.05)
        _seed_session(db_manager, "new", "proj-n", [("user", "New message")])

        results = db_manager.list_chat_sessions()
        assert results[0]["session_id"] == "new"
        assert results[1]["session_id"] == "old"

    def test_no_project_id_uses_max(self, db_manager: DBManager):
        """Without project_id filter, uses MAX(ch.project_id)."""
        _seed_session(db_manager, "s-multi", "proj-a", [("user", "A")])
        db_manager.save_chat_message_with_session("s-multi", "proj-b", "user", "B")

        results = db_manager.list_chat_sessions()
        assert len(results) == 1
        # MAX('proj-a', 'proj-b') = 'proj-b'
        assert results[0]["project_id"] == "proj-b"


# ═════════════════════════════════════════════════════════════════════════
#  API endpoint tests
#  Both `client` and `db_manager` fixtures share the same tmp_path, so they
#  use the same SQLite database file.
# ═════════════════════════════════════════════════════════════════════════


class TestGetChatHistoryAPI:
    """GET /api/agent/chat/history?session_id=..."""

    def test_returns_messages_in_chronological_order(self, client: TestClient,
                                                      db_manager: DBManager):
        """Messages returned oldest-first (chronological)."""
        _seed_session(db_manager, "hist-sess", "proj-h", [
            ("user", "First"),
            ("assistant", "Second"),
            ("user", "Third"),
        ])

        resp = client.get("/api/agent/chat/history?session_id=hist-sess")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "hist-sess"
        assert len(data["messages"]) == 3
        # Oldest first
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][0]["content"] == "First"
        assert data["messages"][1]["content"] == "Second"
        assert data["messages"][2]["content"] == "Third"

    def test_empty_session_id_returns_422(self, client: TestClient):
        """Empty session_id returns 422."""
        resp = client.get("/api/agent/chat/history?session_id=")
        assert resp.status_code == 422
        assert "session_id is required" in resp.json()["detail"]

    def test_missing_session_id_returns_422(self, client: TestClient):
        """Missing session_id query param returns 422."""
        resp = client.get("/api/agent/chat/history")
        assert resp.status_code == 422

    def test_unknown_session_returns_empty_messages(self, client: TestClient):
        """Non-existent session returns empty messages list."""
        resp = client.get("/api/agent/chat/history?session_id=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []

    def test_response_shape(self, client: TestClient, db_manager: DBManager):
        """Response has session_id and messages keys with expected fields."""
        _seed_session(db_manager, "shape-test", "proj-s", [("user", "Hello")])

        resp = client.get("/api/agent/chat/history?session_id=shape-test")
        data = resp.json()
        assert "session_id" in data
        assert "messages" in data
        msg = data["messages"][0]
        assert "role" in msg
        assert "content" in msg
        assert "created_at" in msg

    def test_usage_stats_in_response(self, client: TestClient,
                                     db_manager: DBManager):
        """Cumulative real usage surfaces as hit_ratio + billed_tokens."""
        _seed_session(db_manager, "usage-sess", "proj-u", [("user", "Hello")])
        db_manager.accumulate_session_usage("usage-sess", {
            "prompt_tokens": 1000, "completion_tokens": 50,
            "cache_hit_tokens": 800, "cache_miss_tokens": 200})

        resp = client.get("/api/agent/chat/history?session_id=usage-sess")
        data = resp.json()
        assert data["hit_ratio"] == 0.8
        # billed-equivalent = miss + hit/10 + completion
        assert data["billed_tokens"] == 200 + 80 + 50

    def test_usage_stats_zero_when_none_recorded(self, client: TestClient,
                                                 db_manager: DBManager):
        """Sessions with no recorded usage report zeros, not garbage."""
        _seed_session(db_manager, "nousage-sess", "proj-u", [("user", "Hi")])
        resp = client.get("/api/agent/chat/history?session_id=nousage-sess")
        data = resp.json()
        assert data["hit_ratio"] == 0
        assert data["billed_tokens"] == 0


class TestListSessionsAPI:
    """GET /api/agent/sessions"""

    def test_returns_session_list(self, client: TestClient, db_manager: DBManager):
        """Returns all sessions with messages."""
        _seed_session(db_manager, "sess-list-a", "proj-a", [("user", "Hello")])
        _seed_session(db_manager, "sess-list-b", "proj-b", [("user", "World")])

        resp = client.get("/api/agent/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert len(data["sessions"]) == 2

    def test_filter_by_project_id(self, client: TestClient, db_manager: DBManager):
        """Filters sessions by project_id."""
        _seed_session(db_manager, "sf-a", "proj-x", [("user", "From X")])
        _seed_session(db_manager, "sf-b", "proj-y", [("user", "From Y")])

        resp = client.get("/api/agent/sessions?project_id=proj-x")
        data = resp.json()
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["project_id"] == "proj-x"

    def test_respects_limit(self, client: TestClient, db_manager: DBManager):
        """Respects the limit query parameter."""
        for i in range(5):
            _seed_session(db_manager, f"slim-{i}", "proj-l", [("user", f"Msg {i}")])

        resp = client.get("/api/agent/sessions?limit=2")
        data = resp.json()
        assert len(data["sessions"]) == 2

    def test_session_fields(self, client: TestClient, db_manager: DBManager):
        """Each session has the expected fields."""
        _seed_session(db_manager, "field-test", "proj-f", [("user", "Content")])

        resp = client.get("/api/agent/sessions")
        s = resp.json()["sessions"][0]
        assert "session_id" in s
        assert "project_id" in s
        assert "message_count" in s
        assert "last_message" in s
        assert "updated_at" in s


class TestSaveMessageAPI:
    """POST /api/agent/chat/message"""

    def test_saves_message_and_returns_status(self, client: TestClient,
                                               db_manager: DBManager):
        """Saves a message and returns {'status': 'saved'}."""
        _seed_session(db_manager, "save-sess", "proj-s", [])

        resp = client.post("/api/agent/chat/message", json={
            "session_id": "save-sess",
            "project_id": "proj-s",
            "role": "user",
            "content": "Hello!",
        })
        assert resp.status_code == 200
        assert resp.json() == {"status": "saved"}

        # Verify it was persisted
        messages = db_manager.get_chat_history_by_session("save-sess", limit=10)
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello!"
        assert messages[0]["role"] == "user"

    def test_saves_assistant_message(self, client: TestClient, db_manager: DBManager):
        """Saves assistant role message."""
        _seed_session(db_manager, "assist-sess", "proj-a", [])

        resp = client.post("/api/agent/chat/message", json={
            "session_id": "assist-sess",
            "project_id": "proj-a",
            "role": "assistant",
            "content": "Sure, I can help!",
        })
        assert resp.status_code == 200

        msgs = db_manager.get_chat_history_by_session("assist-sess", limit=10)
        assert msgs[0]["role"] == "assistant"

    def test_rejects_invalid_role(self, client: TestClient):
        """Invalid role returns 422 validation error."""
        resp = client.post("/api/agent/chat/message", json={
            "session_id": "sess-bad",
            "project_id": "proj-bad",
            "role": "invalid_role",
            "content": "test",
        })
        assert resp.status_code == 422

    def test_saves_system_message(self, client: TestClient, db_manager: DBManager):
        """Saves system role message."""
        _seed_session(db_manager, "sys-sess", "proj-sys", [])

        resp = client.post("/api/agent/chat/message", json={
            "session_id": "sys-sess",
            "project_id": "proj-sys",
            "role": "system",
            "content": "System message",
        })
        assert resp.status_code == 200

    def test_large_brief_not_truncated_under_cap(self, client: TestClient,
                                                  db_manager: DBManager):
        """Fix C: a large brief (>2000 chars) is preserved, not clipped to 2000."""
        _seed_session(db_manager, "trunc-sess", "proj-t", [])

        long_content = "x" * 5000
        resp = client.post("/api/agent/chat/message", json={
            "session_id": "trunc-sess",
            "project_id": "proj-t",
            "role": "user",
            "content": long_content,
        })
        assert resp.status_code == 200

        msgs = db_manager.get_chat_history_by_session("trunc-sess", limit=10)
        assert len(msgs) == 1
        assert len(msgs[0]["content"]) == 5000  # was clipped to 2000 before the fix


# ═════════════════════════════════════════════════════════════════════════
#  POST /api/agent/chat — assistant-response persistence (#3 regression)
# ═════════════════════════════════════════════════════════════════════════


class _FakeAgent:
    """Stand-in for MetaAgent whose chat() replays a scripted event stream."""

    _events: list = []

    def __init__(self, *a, **k):
        pass

    async def chat(self, message, history, current_project=None):
        for ev in self._events:
            yield ev


def _post_chat(client, events, session_id="brief-sess"):
    from unittest.mock import patch
    _FakeAgent._events = events
    with patch("api.agent_routers.MetaAgent", _FakeAgent):
        return client.post("/api/agent/chat", json={
            "message": "approve", "session_id": session_id, "history": [],
        })


class TestChatAssistantPersistence:
    """The brief is often presented in an EARLIER turn (before a tool call);
    capturing only the final `done` message dropped it, so reload/soft-nav lost
    it. The full streamed narrative must be persisted."""

    BRIEF = "## User Stories\n- As a player, I want RULE 42 enforced verbatim."

    def test_earlier_turn_prose_is_persisted_not_just_final_done(
            self, client: TestClient, db_manager: DBManager):
        _seed_session(db_manager, "brief-sess", "p", [])
        events = [
            {"type": "text_delta", "content": "Here is your brief:\n"},
            {"type": "text_delta", "content": self.BRIEF},
            {"type": "tool_call", "name": "noop", "args": {}},
            {"type": "tool_result", "name": "noop", "result": {"status": "ok"}},
            {"type": "text_delta", "content": "\nLet me know about changes."},
            {"type": "done", "message": {"role": "assistant",
                                         "content": "Let me know about changes."}},
        ]
        resp = _post_chat(client, events)
        assert resp.status_code == 200

        msgs = db_manager.get_chat_history_by_session("brief-sess", limit=10)
        saved = " ".join(m["content"] for m in msgs if m["role"] == "assistant")
        assert "RULE 42 enforced verbatim" in saved  # earlier-turn brief survived

    def test_tool_surfaced_brief_persisted_when_model_emits_no_prose(
            self, client: TestClient, db_manager: DBManager):
        """Safety net: brief delivered only via a tool result still survives."""
        _seed_session(db_manager, "brief-sess2", "p", [])
        events = [
            {"type": "tool_call", "name": "answer_project_conversation", "args": {}},
            {"type": "tool_result", "name": "answer_project_conversation",
             "result": {"status": "brief_review", "brief_markdown": self.BRIEF,
                        "run_id": "r1"}},
            {"type": "text_delta", "content": "Please review the brief above."},
            {"type": "done", "message": {"content": "Please review the brief above."}},
        ]
        resp = _post_chat(client, events, session_id="brief-sess2")
        assert resp.status_code == 200

        msgs = db_manager.get_chat_history_by_session("brief-sess2", limit=10)
        saved = " ".join(m["content"] for m in msgs if m["role"] == "assistant")
        assert "RULE 42 enforced verbatim" in saved
