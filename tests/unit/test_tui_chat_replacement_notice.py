# tests/unit/test_tui_chat_replacement_notice.py
# The butler chat backend was retired on the owner's 2026-11-17 ruling, and the
# CLI chat pane is now wired to its replacement: a send is ONE
# `POST /api/state/commands/send_user_conversation_message` for the pane's
# project, and the driver's answer is read back from
# `GET /api/dsh-drivers/{project_id}/conversation` and rendered in the log.
#
# Reached the way the UI reaches it: an Input.Submitted event dispatched to the
# real ChatZone mounted in a minimal host app, through the handler Textual
# invokes when the user presses Enter. Only the host app is a stub — its HTTP
# client is a recorder that answers the two endpoints and records every request,
# so these tests pin what actually goes out (and what does not) rather than
# "the code looks like it does".

import httpx
import pytest
from textual.app import App
from textual.widgets import Input, Static

from cli.tui import chat
from cli.tui.chat import ChatZone

SEND_PATH = "/api/state/commands/send_user_conversation_message"
CONVERSATION_PATH = "/api/dsh-drivers/{project_id}/conversation"


class _Response(httpx.Response):
    """What the pane gets back: a REAL httpx response over the scripted body.

    It used to hand-roll `status_code`/`json` plus its own `raise_for_status`,
    which stopped being enough once the pane checks a response with
    `cli.client.raise_for_refusal` (the refusal check reads the status, the body
    AND the headers). Hand-rolling that surface is how this double drifted from
    what the pane really reads, so it is an httpx response now.
    """

    def __init__(self, payload: dict, status_code: int = 200):
        super().__init__(
            status_code,
            json=payload,
            request=httpx.Request("POST", "http://testserver"),
        )


def _conversation(*entries: dict, project_id: str = "alpha") -> dict:
    """One page of the real projection, as the endpoint returns it."""
    return {
        "project_id": project_id,
        "messages": list(entries),
        "has_more": False,
        "next_cursor": None,
        "end_of_history": True,
        "matched_total": len(entries),
        "unread_count": 0,
        "read_upto_message_id": None,
        "read_upto_created_at": None,
    }


def _entry(message_id: str, body: str, sender_kind: str, subject: str = "",
           segments=None) -> dict:
    return {
        "message_id": message_id,
        "thread_id": message_id,
        "project_id": "alpha",
        "sender_kind": sender_kind,
        "sender_driver_id": None if sender_kind == "user" else "dsh-1",
        "subject": subject,
        "body": body,
        "refs": {},
        "reply_to_message_id": None,
        "created_at": "2026-11-18T10:00:00Z",
        "segments": segments or [],
    }


class _RecordingClient:
    """The pane's shared HTTP client, recorded and scripted.

    `conversations` is served one page per read, the last page repeating once the
    script runs out — enough to show a thread that was empty on the first read
    and holds the driver's answer on the next. `post_status` >= 400 makes the
    send itself refuse, carrying the server's own sentence.
    """

    def __init__(self, conversations=None, post_status: int = 200, refusal: str = ""):
        self.calls: list[dict] = []
        self.conversations = list(conversations or [])
        self.post_status = post_status
        self.refusal = refusal

    def _record(self, method: str, path: str, **detail) -> None:
        self.calls.append({"method": method, "path": path, **detail})

    async def post(self, path, json=None, timeout=None):
        self._record("POST", path, json=json, timeout=timeout)
        if self.post_status >= 400:
            return _Response({"detail": self.refusal}, self.post_status)
        return _Response({"message_id": "u-1", "project_id": (json or {}).get("project_id"),
                          "sender_kind": "user"})

    async def get(self, path, params=None, timeout=None):
        self._record("GET", path, params=params, timeout=timeout)
        if not self.conversations:
            return _Response(_conversation())
        if len(self.conversations) > 1:
            return _Response(self.conversations.pop(0))
        return _Response(self.conversations[0])


class _Harness(App):
    """Minimal host app: ChatZone only needs `app.http`."""

    def __init__(self, http=None):
        super().__init__()
        self.http = http if http is not None else _RecordingClient()

    def compose(self):
        yield ChatZone("http://testserver")


def _messages(zone: ChatZone, css_class: str) -> list[Static]:
    log = zone.query_one("#chat-log")
    return [w for w in log.children
            if isinstance(w, Static) and css_class in w.classes]


def _posts(app: _Harness) -> list[dict]:
    return [call for call in app.http.calls if call["method"] == "POST"]


def _gets(app: _Harness) -> list[dict]:
    return [call for call in app.http.calls if call["method"] == "GET"]


async def _pause_until(pilot, predicate, tries: int = 400) -> None:
    """Pause the app until `predicate()` holds — the way the UI's workers land."""
    for _ in range(tries):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("the pane never reached the expected state")


async def _send(app: _Harness, pilot, text: str, until) -> None:
    """Type into the real input and press Enter — the UI's send path.

    Then wait for the turn the send starts: `until` is the observable that turn
    must produce (the send going out, or the pane's own visible refusal), and the
    end of the turn is the worker's `_agent_streaming` flag, which its `finally`
    clears only after the answer read.
    """
    zone = app.query_one(ChatZone)
    inp = app.query_one("#chat-input", Input)
    inp.focus()
    inp.value = text
    await pilot.press("enter")
    await _pause_until(pilot, until)
    await _pause_until(pilot, lambda: not zone._agent_streaming)
    # One more pass so widgets mounted by the turn's last read are in the log.
    await pilot.pause()


@pytest.fixture(autouse=True)
def _instant_answer_reads(monkeypatch):
    """Read the conversation without the real 2s wait between reads."""
    monkeypatch.setattr(chat, "CONVERSATION_READ_INTERVAL", 0.0)


@pytest.mark.asyncio
async def test_a_send_posts_one_message_for_the_panes_project():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        zone.current_project = "alpha"
        await _send(app, pilot, "hello there",
                    until=lambda: len(_posts(app)) >= 1)

        posts = _posts(app)
        assert len(posts) == 1, app.http.calls
        assert posts[0]["path"] == SEND_PATH
        assert posts[0]["json"]["project_id"] == "alpha"
        assert posts[0]["json"]["body"] == "hello there"
        # A fresh idempotency key per send, so a resend is a new message.
        assert posts[0]["json"]["request_key"].strip()

        # The user's own line is still echoed, so the pane does not read as a
        # dead input, and no error was reported for a send that went out.
        assert [w.content for w in _messages(zone, "chat-user")] == ["You: hello there"]
        assert not _messages(zone, "chat-error"), [w.content for w in _messages(zone, "chat-error")]


@pytest.mark.asyncio
async def test_a_second_send_gets_its_own_request_key():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        zone.current_project = "alpha"
        await _send(app, pilot, "first", until=lambda: len(_posts(app)) >= 1)
        await _send(app, pilot, "second", until=lambda: len(_posts(app)) >= 2)

        posts = _posts(app)
        assert [call["json"]["body"] for call in posts] == ["first", "second"]
        assert posts[0]["json"]["request_key"] != posts[1]["json"]["request_key"]


@pytest.mark.asyncio
async def test_a_send_with_no_project_posts_nothing_and_says_why():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        assert zone.current_project is None
        await _send(app, pilot, "hello",
                    until=lambda: _messages(zone, "chat-error"))

        # Nothing went out: no message can belong to no project at all.
        assert app.http.calls == [], app.http.calls
        reasons = _messages(zone, "chat-error")
        assert len(reasons) == 1, [w.content for w in reasons]
        reason = reasons[0].content
        assert "project" in reason.lower() and "select" in reason.lower()
        assert "NOT sent" in reason


@pytest.mark.asyncio
async def test_the_drivers_answer_is_rendered_once_however_often_it_is_read():
    driver = _entry("m-1", "The brief is ready.", "driver", subject="Kickoff")
    app = _Harness(_RecordingClient([
        _conversation(_entry("u-1", "hello", "user")),   # the first read: only our row
        _conversation(driver, _entry("u-1", "hello", "user")),
    ]))
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        zone.current_project = "alpha"
        await _send(app, pilot, "hello", until=lambda: len(_posts(app)) >= 1)
        await _pause_until(pilot, lambda: _messages(zone, "chat-agent"))

        # The driver's own words reach the log, with its subject; the user's own
        # row is not rendered a second time (the pane already echoed it).
        assert [w.content for w in _messages(zone, "chat-agent")] == [
            "Driver: Kickoff\nThe brief is ready."]
        assert [w.content for w in _messages(zone, "chat-user")] == ["You: hello"]

        gets = _gets(app)
        assert len(gets) >= 2, app.http.calls
        assert gets[0]["path"] == CONVERSATION_PATH.format(project_id="alpha")
        assert gets[0]["params"]["limit"] == 30

        # A second send reads the same thread again — the same message must not
        # be rendered a second time.
        await _send(app, pilot, "thanks", until=lambda: len(_posts(app)) >= 2)
        assert len(_gets(app)) >= len(gets) + 1
        assert [w.content for w in _messages(zone, "chat-agent")] == [
            "Driver: Kickoff\nThe brief is ready."]


@pytest.mark.asyncio
async def test_a_refused_send_reports_the_servers_sentence_and_is_not_a_sent_one():
    app = _Harness(_RecordingClient(post_status=403, refusal="this project is private"))
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        zone.current_project = "alpha"
        await _send(app, pilot, "hello", until=lambda: len(_posts(app)) >= 1)

        reasons = _messages(zone, "chat-error")
        assert len(reasons) == 1, [w.content for w in reasons]
        assert "NOT sent" in reasons[0].content
        assert "this project is private" in reasons[0].content
        # A send that failed is not followed by an answer read.
        assert _gets(app) == [], app.http.calls
