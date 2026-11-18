# tests/unit/test_cli_refusals_tui.py
# A refusal from the server must reach a TUI/CLI pane as the server's own
# sentence, not httpx's bare "Client error '403 Forbidden' for url ...".
#
# cli/client.py already quotes the refusal (raise_for_refusal); these four
# consumer modules were still checking responses with `resp.raise_for_status()`,
# which reads the status code and throws the body away. So the first test below
# pins the replacement itself: no call to an attribute named `raise_for_status`
# may remain in any of them, each one imports `raise_for_refusal` from
# `cli.client`, and the number of calls matches the sites that existed (nothing
# was dropped rather than converted). The remaining tests drive real module code
# against a stubbed transport, because "the source looks converted" is not the
# same claim as "the user is shown the server's sentence".

import ast
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCES = (
    "cli/tui/app.py",
    "cli/tui/dashboard.py",
    "cli/tui/chat.py",
    "cli/server.py",
)
# Sites measured on the base commit (a8e75fad95) before the conversion.
EXPECTED_CALLS = {
    "cli/tui/app.py": 2,
    "cli/tui/dashboard.py": 3,
    "cli/tui/chat.py": 32,
    "cli/server.py": 1,
}
BASE = "http://stub"
SENTENCE = "Not signed in — sign in with an authorized account to make changes."


def _tree(rel: str) -> ast.Module:
    return ast.parse((REPO / rel).read_text(encoding="utf-8"))


def _calls_named(tree: ast.Module, name: str) -> list[ast.Call]:
    """Calls of `x.<name>(...)` — the shape a bare status check has."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == name
    ]


def _plain_calls(tree: ast.Module, name: str) -> list[ast.Call]:
    """Calls of `<name>(...)` — the shape the imported helper has."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == name
    ]


# ── (a) the source: every status check goes through the refusal helper ──


def test_no_bare_status_check_remains_in_the_four_modules():
    for rel in SOURCES:
        offenders = [call.lineno for call in _calls_named(_tree(rel), "raise_for_status")]
        assert offenders == [], f"{rel} still calls raise_for_status at {offenders}"


def test_each_module_imports_raise_for_refusal_from_cli_client():
    for rel in SOURCES:
        imported = {
            alias.name
            for node in ast.walk(_tree(rel))
            if isinstance(node, ast.ImportFrom) and node.module == "cli.client"
            for alias in node.names
        }
        assert "raise_for_refusal" in imported, f"{rel} does not import it from cli.client"


def test_every_existing_status_check_became_a_refusal_call():
    """Count parity: a check deleted instead of converted would satisfy (a)."""
    for rel, expected in EXPECTED_CALLS.items():
        calls = _plain_calls(_tree(rel), "raise_for_refusal")
        assert len(calls) == expected, f"{rel}: {len(calls)} refusal calls, expected {expected}"


# ── (b) the behaviour: the module's own path shows the server's sentence ──


def _stub_refusal() -> httpx.MockTransport:
    """A server that refuses with the body the write gate actually sends."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"detail": SENTENCE, "code": "not_authenticated"},
            headers={"X-AItelier-Denial": "not_authenticated"},
            request=request,
        )

    return httpx.MockTransport(handler)


def _httpx_own_message(exc: httpx.HTTPStatusError) -> str:
    """What the same response says through httpx's own status check."""
    with pytest.raises(httpx.HTTPStatusError) as baseline:
        exc.response.raise_for_status()
    return str(baseline.value)


# The seam: cli/tui/chat.py:ChatZone._send_to_driver_conversation. It is the
# module's own send path -- it performs the POST on the app's shared client, runs
# the refusal check that used to be `response.raise_for_status()`, and renders the
# failure through the module's own `_failure_text` into `_add_error`, which is the
# text the user reads in the pane. Only the host object is a stand-in (a fake with
# the three attributes that path touches): the function under test is the real
# `ChatZone._send_to_driver_conversation`, so this is the module's code, not a
# copy of it. `_failure_text` is spied on only to CAPTURE the exception the
# module's refusal check raised; the spy delegates to the real function, so the
# rendering under assertion is still the module's.


async def test_chat_send_failure_shows_the_servers_sentence(monkeypatch):
    from cli.tui import chat

    seen: dict = {}
    real_failure_text = chat._failure_text

    def spy(exc, response):
        seen["exc"] = exc
        seen["response"] = response
        return real_failure_text(exc, response)

    errors: list[str] = []
    client = httpx.AsyncClient(transport=_stub_refusal(), base_url=BASE)
    host = SimpleNamespace(
        app=SimpleNamespace(http=client),
        current_project="alpha",
        _add_error=errors.append,
    )

    monkeypatch.setattr(chat, "_failure_text", spy)
    try:
        await chat.ChatZone._send_to_driver_conversation(host, "alpha", "hello")
    finally:
        await client.aclose()

    exc = seen["exc"]
    assert isinstance(exc, httpx.HTTPStatusError)
    assert exc.response is seen["response"]

    # What the pane shows: the server's sentence, in the module's own wording.
    # (`_failure_text` renders the response's own `detail`, so the pane text
    # carries the sentence while the refusal CODE rides on the exception below.)
    assert len(errors) == 1
    assert SENTENCE in errors[0]

    # The sentence is on the EXCEPTION too, so a caller that shows `str(exc)`
    # (or passes no response) still quotes the server rather than the status.
    assert SENTENCE in str(exc)
    assert "not_authenticated" in str(exc)
    assert SENTENCE in chat._failure_text(exc, None)

    # And that is exactly what the bare status check was losing.
    assert SENTENCE not in _httpx_own_message(exc)
    assert str(exc) != _httpx_own_message(exc)


# A second seam with NO monkeypatching at all, to keep the claim above honest:
# cli/tui/chat.py:BriefReviewModal._run_async_post is the module's own submit
# call, and it hands `str(exception)` back to its caller as the user-visible
# error. Nothing here is a substitute for module code -- only the host object
# (what `self.app.http` resolves to) is a stand-in.
async def test_brief_review_post_reports_the_servers_sentence():
    from cli.tui import chat

    client = httpx.AsyncClient(transport=_stub_refusal(), base_url=BASE)
    host = SimpleNamespace(app=SimpleNamespace(http=client))
    try:
        ok, message = await chat.BriefReviewModal._run_async_post(
            host, "/api/projects/submit", {"project_id": "alpha"})
    finally:
        await client.aclose()

    assert ok is False
    assert SENTENCE in message
    assert "not_authenticated" in message
    assert "403" in message  # httpx's own headline still leads, as before
