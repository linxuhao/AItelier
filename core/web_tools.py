# core/web_tools.py
# Web search (SearXNG) and fetch tools for DPE agents.

import ipaddress
import os
import re
import time as _time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

# ── Config ──────────────────────────────────────────────────────────────

SEARXNG_URL = os.getenv("SEARXNG_URL", "").rstrip("/")
SEARCH_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "10"))
FETCH_TIMEOUT = int(os.getenv("WEB_FETCH_TIMEOUT", "15"))
FETCH_MAX_CHARS = int(os.getenv("WEB_FETCH_MAX_CHARS", "10000"))
HTTP_MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
HTTP_RETRY_BACKOFF = float(os.getenv("HTTP_RETRY_BACKOFF", "1.0"))


def _http_retry(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) with retry on transient errors.

    Retries on: ConnectionRefused, TimeoutException, 5xx responses.
    Backoff: HTTP_RETRY_BACKOFF * 2^attempt seconds (1s, 2s, 4s by default).
    """
    last_error = None
    for attempt in range(HTTP_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_error = e
            if attempt < HTTP_MAX_RETRIES - 1:
                _time.sleep(HTTP_RETRY_BACKOFF * (2 ** attempt))
        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code >= 500 and attempt < HTTP_MAX_RETRIES - 1:
                _time.sleep(HTTP_RETRY_BACKOFF * (2 ** attempt))
            else:
                raise
    raise last_error

# Private/network ranges — SSRF protection
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(hostname: str) -> bool:
    """Check if a hostname resolves to a private IP (SSRF protection)."""
    import socket
    try:
        addrinfo = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in addrinfo:
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return True
    except socket.gaierror:
        return True  # unresolvable = blocked
    return False


# ── HTML → text ─────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _html_to_text(html: str) -> str:
    """Strip HTML tags, collapse whitespace."""
    html = _SCRIPT_STYLE_RE.sub("", html)
    text = _TAG_RE.sub("", html)
    # Decode common entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")]:
        text = text.replace(entity, char)
    text = _WS_RE.sub("\n\n", text).strip()
    return text


def _extract_title(html: str) -> str:
    """Extract <title> content from HTML."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ── WebSearchTool ───────────────────────────────────────────────────────

class WebSearchTool:
    """Search the web via SearXNG JSON API."""

    def search(self, query: str, max_results: int = 5,
               categories: str = "general", language: str = "auto") -> dict:
        """
        Search SearXNG and return structured results.

        :param query: Search query string
        :param max_results: Maximum number of results to return (1-10)
        :param categories: SearXNG category (general, news, it, science, etc.)
        :param language: Language code (auto, en, zh, etc.)
        :return: {"results": [{"title", "url", "snippet"}], "query", "total"}
        """
        max_results = max(1, min(10, max_results))

        # Web search is optional. If no backend is configured, return cleanly
        # so the pipeline proceeds (agents fall back to model knowledge)
        # instead of failing on a dead/relative URL.
        if not SEARXNG_URL:
            return {
                "query": query,
                "total": 0,
                "results": [],
                "note": (
                    "web_search is not configured — set SEARXNG_URL to a "
                    "SearXNG instance (JSON API) to enable it. The pipeline "
                    "continues without web results."
                ),
            }

        params = {
            "q": query,
            "format": "json",
            "categories": categories,
        }
        if language != "auto":
            params["language"] = language

        url = f"{SEARXNG_URL}/search?{urlencode(params)}"

        def _do():
            resp = httpx.get(url, timeout=SEARCH_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            return resp.json()

        try:
            data = _http_retry(_do)
        except httpx.TimeoutException:
            return {"error": f"Search timed out after {SEARCH_TIMEOUT}s (retried {HTTP_MAX_RETRIES}x)", "query": query, "results": []}
        except httpx.HTTPStatusError as e:
            return {"error": f"Search HTTP error: {e.response.status_code}", "query": query, "results": []}
        except Exception as e:
            return {"error": f"Search failed: {e}", "query": query, "results": []}

        raw_results = data.get("results", [])
        results = []
        for r in raw_results[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            })

        return {
            "query": query,
            "total": data.get("number_of_results", len(results)),
            "results": results,
        }


# ── WebFetchTool ────────────────────────────────────────────────────────

class WebFetchTool:
    """Fetch a URL and extract readable text content."""

    def fetch(self, url: str, max_chars: Optional[int] = None) -> dict:
        """
        Fetch a URL and return its text content.

        :param url: URL to fetch
        :param max_chars: Maximum characters to return (default from config)
        :return: {"url", "title", "content", "content_length"} or {"error"}
        """
        if max_chars is None:
            max_chars = FETCH_MAX_CHARS

        # SSRF check
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return {"error": f"Invalid URL: {url}"}
        if parsed.scheme not in ("http", "https"):
            return {"error": f"Unsupported scheme: {parsed.scheme}"}
        if _is_private_host(hostname):
            return {"error": f"Blocked: private/internal IP for {hostname}"}

        def _do():
            resp = httpx.get(
                url,
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "AItelier-DPE/1.0"},
            )
            resp.raise_for_status()
            return resp

        try:
            resp = _http_retry(_do)
        except httpx.TimeoutException:
            return {"error": f"Fetch timed out after {FETCH_TIMEOUT}s (retried {HTTP_MAX_RETRIES}x)", "url": url}
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code}", "url": url}
        except Exception as e:
            return {"error": f"Fetch failed: {e}", "url": url}

        content_type = resp.headers.get("content-type", "")
        raw = resp.text

        if "text/html" in content_type:
            title = _extract_title(raw)
            content = _html_to_text(raw)
        else:
            title = ""
            content = raw

        # Truncate
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"

        return {
            "url": url,
            "title": title,
            "content": content,
            "content_length": len(content),
            "truncated": truncated,
        }
