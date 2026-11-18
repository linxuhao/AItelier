# tests/unit/test_cli_client_refusals.py
# A refusal from the server must reach the CLI user as the server's own
# sentence, not httpx's bare "Client error '403 Forbidden' for url ...".
#
# The exception TYPE is part of the contract, not an implementation detail:
# nine sites in cli/app.py catch httpx.HTTPStatusError and read `.response`
# (status code, JSON body), so a refusal that raised anything else -- or lost
# the response -- would break them.
#
# Style follows tests/unit/test_cli_client.py: httpx.MockTransport stubs
# injected as the APIClient's own `_client`.

import ast
from pathlib import Path

import httpx
import pytest

from cli.client import APIClient

BASE = "http://stub"
CLIENT_SOURCE = Path(__file__).resolve().parents[2] / "cli" / "client.py"


def _make_client(handler):
    """APIClient whose own httpx.Client is replaced by a stubbed transport.

    APIClient.__init__ builds its own client, so the stub is injected as the
    instance attribute it reads.
    """
    client = APIClient(base_url=BASE)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE
    )
    return client


def _httpx_own_message(exc: httpx.HTTPStatusError) -> str:
    """What httpx itself would have said about the same response."""
    with pytest.raises(httpx.HTTPStatusError) as baseline:
        exc.response.raise_for_status()
    return str(baseline.value)


# ── (a) a refusal body carries the server's sentence and code ──


def test_refusal_carries_the_servers_detail_and_code():
    seen = {}

    def handler(request):
        response = httpx.Response(
            403,
            json={"detail": "not allowed here", "code": "not_authenticated"},
        )
        seen["response"] = response
        return response

    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_project("p1")

    exc = caught.value
    message = str(exc)
    assert isinstance(exc, httpx.HTTPStatusError)
    assert "403" in message
    assert "not allowed here" in message
    assert "not_authenticated" in message
    # the response (and its request) survive, because cli/app.py reads them
    assert exc.response is seen["response"]
    assert exc.request is seen["response"].request
    assert exc.response.status_code == 403
    assert exc.response.json()["code"] == "not_authenticated"


def test_refusal_code_falls_back_to_the_denial_header():
    """api/authz.py puts the stable code in the header when the body has none."""

    def handler(request):
        return httpx.Response(
            403,
            json={"detail": "admin token required"},
            headers={"X-AItelier-Denial": "admin_required"},
        )

    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.list_projects()

    message = str(caught.value)
    assert "admin token required" in message
    assert "admin_required" in message


# ── (b) a body with no server sentence keeps httpx's own wording ──


@pytest.mark.parametrize(
    "response_kwargs",
    [
        pytest.param({"text": "upstream exploded"}, id="not-json"),
        pytest.param({"json": ["not", "an", "object"]}, id="json-not-object"),
        pytest.param({"json": {"detail": {"nested": "object"}}}, id="detail-not-string"),
        pytest.param({"json": {"error": "no detail key"}}, id="no-detail"),
    ],
)
def test_body_without_a_server_sentence_keeps_httpx_wording(response_kwargs):
    def handler(request):
        return httpx.Response(500, **response_kwargs)

    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.list_projects()

    exc = caught.value
    assert type(exc) is httpx.HTTPStatusError
    assert "500" in str(exc)
    assert f"{BASE}/api/projects" in str(exc)
    # equivalent to httpx's own wording, reproduced in the helper
    assert str(exc) == _httpx_own_message(exc)


def test_500_with_a_non_json_body_raises_only_httpstatuserror():
    """Nothing but HTTPStatusError may escape a refusal path."""

    def handler(request):
        return httpx.Response(500, content=b"\x00not json at all")

    client = _make_client(handler)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_project("p1")

    assert type(caught.value) is httpx.HTTPStatusError
    assert "500" in str(caught.value)
    assert f"{BASE}/api/projects/p1" in str(caught.value)


# ── (c) a successful response is untouched ──


def test_successful_response_is_returned_unchanged():
    payload = {"project_id": "p1", "name": "kept as-is"}

    def handler(request):
        return httpx.Response(200, json=payload)

    client = _make_client(handler)

    assert client.get_project("p1") == payload


# ── (d) no call site keeps a bare httpx status check ──


def test_client_module_contains_no_bare_status_check_call():
    """A bare re-introduced status check would bypass the helper.

    Asserted on the AST, not on the text: a comment may talk about the old
    call, but no CALL to an attribute named `raise_for_status` may remain.
    """
    tree = ast.parse(CLIENT_SOURCE.read_text(), filename=str(CLIENT_SOURCE))

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "raise_for_status"
    ]

    assert offenders == []


def test_every_client_call_site_goes_through_the_helper():
    """The helper is the only status check left, so it must be used widely.

    `>= 37` pins the conversion this change made without forbidding a later
    method from being added the right way.
    """
    tree = ast.parse(CLIENT_SOURCE.read_text(), filename=str(CLIENT_SOURCE))

    defined = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    ]
    assert "raise_for_refusal" in defined, "the helper must be module-level"

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "raise_for_refusal"
    ]

    assert len(calls) >= 37, f"expected 37 routed call sites, found {len(calls)}"
