"""Minimal MCP client — reach the shared vip-gateway tool server from a tool.

AItelier's media tools (sprite / audio generation) are thin wrappers over tools
hosted on the MCP server, so the models and the GPU they need stay on one box and
every client shares the same tool surface. This speaks just enough of the
streamable-HTTP transport to call a tool; it is a client, never a server.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

# NOT `AITELIER_MCP_URL`. That name means the opposite thing on the other side
# of the product: the published DSH plugin teaches it as "where DeepSeek Harness
# finds AItelier's OWN MCP endpoint" (127.0.0.1:4444/mcp). One variable with two
# opposite meanings is a trap — set it for the plugin, put it in AItelier's .env,
# and the media tools quietly start calling AItelier itself, where none of these
# tools exist. The outbound one is the private one, so it is the one that moved.
MCP_URL = os.environ.get("AITELIER_MEDIA_MCP_URL", "http://mcp_server:9003/mcp")
_TIMEOUT = float(os.environ.get("AITELIER_MEDIA_MCP_TIMEOUT", "600"))
_HDR = {"Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}

_session: str | None = None


class MCPError(RuntimeError):
    """The MCP server was unreachable, or the tool itself failed."""


def _rpc(method: str, params: dict | None = None, notify: bool = False):
    global _session
    body: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notify:
        body["id"] = 1
    headers = dict(_HDR)
    if _session:
        headers["mcp-session-id"] = _session
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        if _session is None:
            _session = resp.headers.get("mcp-session-id")
        raw = resp.read().decode()
    # The transport answers with either bare JSON or a one-event SSE stream.
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(raw) if raw.strip() else None


def _connect() -> None:
    if _session:
        return
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "aitelier", "version": "1"}})
    _rpc("notifications/initialized", {}, notify=True)


def call_tool(name: str, arguments: dict) -> str:
    """Call one MCP tool and return its text result.

    Retries once from a clean session on a 404: the server hands out a session id
    that dies with the container, and it is restarted far more often than this
    process is (every tool added to it recreates it)."""
    global _session
    for attempt in (1, 2):
        try:
            _connect()
            r = _rpc("tools/call", {"name": name, "arguments": arguments})
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and attempt == 1:
                _session = None
                continue
            raise MCPError(f"{name}: HTTP {e.code} from {MCP_URL}") from e
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
            raise MCPError(f"{name}: MCP transport failed ({MCP_URL}): {e}") from e
    if not r or "result" not in r:
        raise MCPError(f"{name}: {(r or {}).get('error', 'no result')}")
    text = "\n".join(c.get("text", "") for c in r["result"].get("content") or []
                     if c.get("type") == "text")
    if r["result"].get("isError"):
        raise MCPError(f"{name}: {text}")
    return text


_URL = re.compile(r"https?://[^\s)\"']+")


def urls_in(text: str) -> list[str]:
    """Every URL in a tool's reply. The media tools answer in prose with the
    download link embedded, so the URL has to be picked back out."""
    return _URL.findall(text or "")


def fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            return r.read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise MCPError(f"could not download {url}: {e}") from e
