"""MCP Host allowlist wiring preserves rebinding protection and port semantics."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

from api import mcp_router

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PUBLIC = "aitelier.linxuhao.app,aitelier.linxuhao.app:*"


class _Request:
    def __init__(self, host: str):
        self.headers = {"host": host, "content-type": "application/json"}


def _compose_environment(tmp_path: Path, env_value: str = "") -> dict:
    compose = tmp_path / "docker-compose.yml"
    compose.write_text((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"AITELIER_MCP_ALLOWED_HOSTS={env_value}\n" if env_value else "",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("AITELIER_MCP_ALLOWED_HOSTS", None)
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(compose),
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["aitelier"]["environment"]


def test_compose_default_uses_public_host_and_wildcard_port(tmp_path):
    environment = _compose_environment(tmp_path)
    assert environment["AITELIER_MCP_ALLOWED_HOSTS"] == DEFAULT_PUBLIC


def test_compose_env_file_overrides_the_public_host_allowlist(tmp_path):
    environment = _compose_environment(
        tmp_path, "operator.example,operator.example:*"
    )
    assert environment["AITELIER_MCP_ALLOWED_HOSTS"] == (
        "operator.example,operator.example:*"
    )


@pytest.mark.asyncio
async def test_fastmcp_host_matching_accepts_ports_and_rejects_host_spoofing(monkeypatch):
    monkeypatch.setenv("AITELIER_MCP_ALLOWED_HOSTS", DEFAULT_PUBLIC)
    hosts = mcp_router._allowed_hosts()
    security = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[],
        )
    )

    for host in (
        "aitelier.linxuhao.app",
        "aitelier.linxuhao.app:443",
        "aitelier.linxuhao.app:8443",
        "aitelier:4444",
        "localhost:4444",
        "127.0.0.1:4444",
        "testserver",
    ):
        assert await security.validate_request(_Request(host), is_post=True) is None

    for host in (
        "aitelier.linxuhao.app.evil:443",
        "aitelier.linxuhao.app@evil.example:443",
        "evil.example",
    ):
        rejected = await security.validate_request(_Request(host), is_post=True)
        assert rejected is not None
        assert rejected.status_code == 421
