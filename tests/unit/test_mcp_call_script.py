"""The shell helper calls the authenticated HTTP server, never host tools."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/mcp_call.py"
spec = importlib.util.spec_from_file_location("mcp_call_script", _SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("AITELIER_ADMIN_TOKEN", "test-secret-never-print")
    requests = []
    reply = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=reply)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(cli.httpx, "post", client.post)
        yield requests, reply


def test_file_arguments_use_http_and_enforce_checkpoints(tmp_path, transport, capsys):
    requests, reply = transport
    brief = tmp_path / "args.json"
    brief.write_text(json.dumps({"brief": "A safe multi-line\nbrief"}))
    assert cli.main(["run_pipeline", "@" + str(brief)]) == 0
    request = requests[0]
    assert str(request.url) == "http://127.0.0.1:4444/mcp/"
    assert request.headers["X-AItelier-Admin-Token"] == "test-secret-never-print"
    payload = json.loads(request.content)
    assert payload["method"] == "tools/call"
    assert payload["params"]["arguments"]["checkpoints"] == "ask"
    assert payload["params"]["arguments"]["brief"] == "A safe multi-line\nbrief"
    assert "test-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("arguments", ['{"checkpoints":"auto"}', '[]', '{bad'])
def test_invalid_or_auto_requests_never_reach_server(arguments, transport):
    requests, _ = transport
    assert cli.main(["run_pipeline", arguments]) == 2
    assert not requests


def test_output_is_complete_and_tool_errors_fail(transport, capsys):
    requests, reply = transport
    reply["result"] = {"content": [{"type": "text", "text": "x" * 20000}], "isError": True}
    assert cli.main(["get_pipeline", '{"config":"dpe_game"}']) == 1
    assert json.loads(capsys.readouterr().out) == reply
    assert json.loads(requests[0].content)["params"]["arguments"] == {"config": "dpe_game"}


def test_service_down_is_explicit(monkeypatch, capsys):
    monkeypatch.setenv("AITELIER_ADMIN_TOKEN", "secret")
    def offline(*args, **kwargs):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(cli.httpx, "post", offline)
    assert cli.main(["get_pipeline"]) == 1
    assert "service unavailable" in capsys.readouterr().err


def test_env_file_token_is_only_sent_in_header(monkeypatch, tmp_path, transport, capsys):
    monkeypatch.delenv("AITELIER_ADMIN_TOKEN")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    (tmp_path / "AItelier").mkdir()
    (tmp_path / "AItelier/.env").write_text('AITELIER_ADMIN_TOKEN="file-secret"\n')
    requests, _ = transport
    assert cli.main(["get_pipeline"]) == 0
    assert requests[0].headers["X-AItelier-Admin-Token"] == "file-secret"
    assert "file-secret" not in requests[0].content.decode()
    assert "file-secret" not in capsys.readouterr().out


def test_missing_credentials_prevent_http(monkeypatch, tmp_path, transport, capsys):
    monkeypatch.delenv("AITELIER_ADMIN_TOKEN")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    requests, _ = transport
    assert cli.main(["get_pipeline"]) == 2
    assert not requests
    assert "AITELIER_ADMIN_TOKEN is missing" in capsys.readouterr().err


def test_server_auth_rejection_is_failure_without_echoing_secrets(monkeypatch, capsys):
    monkeypatch.setenv("AITELIER_ADMIN_TOKEN", "secret")
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="secret"))) as client:
        monkeypatch.setattr(cli.httpx, "post", client.post)
        assert cli.main(["get_pipeline"]) == 1
    output = capsys.readouterr()
    assert "HTTP 403" in output.err
    assert "secret" not in output.err + output.out


def test_jsonrpc_error_returns_failure_and_complete_response(transport, capsys):
    _, reply = transport
    reply.clear()
    reply.update({"jsonrpc": "2.0", "id": 1,
                  "error": {"code": -32602, "message": "unknown tool"}})
    assert cli.main(["missing_tool"]) == 1
    assert json.loads(capsys.readouterr().out) == reply
