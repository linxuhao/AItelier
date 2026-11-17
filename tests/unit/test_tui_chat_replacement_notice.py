# tests/unit/test_tui_chat_replacement_notice.py
# The butler chat backend was retired on the owner's 2026-11-17 ruling. The CLI
# chat pane used to POST every send to it; a send must now answer with ONE
# visible message naming the replacement entry point — the project's driver
# conversation in the dashboard, served by
# `GET /api/dsh-drivers/{project_id}/conversation`.
#
# Reached the way the UI reaches it: an Input.Submitted event dispatched to the
# real ChatZone mounted in a minimal host app, through the handler Textual
# invokes when the user presses Enter. Only the host app is a stub — the HTTP
# client it carries fails the test if the send path touches it at all, which is
# what pins "no request goes out" rather than "the code looks like it does not".

import pytest
from textual.app import App
from textual.widgets import Input, Static

from cli.tui.chat import ChatZone


class _NoRequests:
    """The shared HTTP client, booby-trapped.

    The pane still owns a client for its dashboard calls; a chat send is not one
    of them any more, so any attribute touched here — `.post`, `.stream`, even a
    look at `.is_closed` — is a request path that should not exist.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"the chat send path used the HTTP client (.{name}) — it must not "
            f"call the retired backend"
        )


class _Harness(App):
    """Minimal host app: ChatZone only needs `app.http` (and never uses it)."""

    def __init__(self):
        super().__init__()
        self.http = _NoRequests()

    def compose(self):
        yield ChatZone("http://testserver")


def _messages(zone: ChatZone, css_class: str) -> list[Static]:
    log = zone.query_one("#chat-log")
    return [w for w in log.children
            if isinstance(w, Static) and css_class in w.classes]


async def _send(app: _Harness, pilot, text: str) -> None:
    """Type into the real input and press Enter — the UI's send path."""
    inp = app.query_one("#chat-input", Input)
    inp.focus()
    inp.value = text
    await pilot.press("enter")
    # The send runs as a Textual worker; pause until it has mounted its answer.
    for _ in range(50):
        await pilot.pause()
        if _messages(app.query_one(ChatZone), "chat-system"):
            break


@pytest.mark.asyncio
async def test_a_send_produces_one_message_naming_the_driver_conversation():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        await _send(app, pilot, "hello")

        # The user's own line is still echoed, so the pane does not read as a
        # dead input; what the send ANSWERS with is a single notice.
        assert [w.content for w in _messages(zone, "chat-user")] == ["You: hello"]
        notices = _messages(zone, "chat-system")
        assert len(notices) == 1, [w.content for w in notices]
        assert not _messages(zone, "chat-error"), "the send reported an error"

        notice = notices[0].content
        assert "retired" in notice
        assert "dashboard" in notice
        assert "driver conversation" in notice
        assert "GET /api/dsh-drivers/{project_id}/conversation" in notice


@pytest.mark.asyncio
async def test_a_second_send_gets_its_own_single_notice():
    """Every send answers; none is silently swallowed or answered twice."""
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        zone = app.query_one(ChatZone)
        await _send(app, pilot, "first")
        await _send(app, pilot, "second")

        assert len(_messages(zone, "chat-user")) == 2
        notices = _messages(zone, "chat-system")
        assert len(notices) == 2, [w.content for w in notices]
        assert all("GET /api/dsh-drivers/{project_id}/conversation" in w.content
                   for w in notices)
