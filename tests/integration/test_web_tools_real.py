# tests/integration/test_web_tools_real.py
# Integration tests for core/web_tools.py — hits real SearXNG server and real URLs.
# These are non-deterministic (depend on live external services), so the whole
# module is marked 'network' and deselected by default (run: pytest -m network).

import pytest
from core.web_tools import WebSearchTool, WebFetchTool, SEARXNG_URL

pytestmark = pytest.mark.network


# ── Helpers ──

def _searxng_available() -> bool:
    """Check if SearXNG is reachable."""
    import httpx
    try:
        resp = httpx.get(f"{SEARXNG_URL}/search?q=test&format=json", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


requires_searxng = pytest.mark.skipif(
    not _searxng_available(),
    reason="SearXNG server not reachable"
)


# ── WebSearchTool ──

@requires_searxng
class TestWebSearchToolReal:
    def test_search_basic(self):
        tool = WebSearchTool()
        result = tool.search("python httpx")

        assert "error" not in result
        assert result["query"] == "python httpx"
        assert len(result["results"]) > 0
        # Each result should have title, url, snippet
        for r in result["results"]:
            assert "title" in r
            assert "url" in r
            assert "snippet" in r
            assert r["url"].startswith("http")

    def test_search_max_results(self):
        tool = WebSearchTool()
        result = tool.search("python", max_results=2)

        assert "error" not in result
        assert len(result["results"]) <= 2

    def test_search_empty_query_still_works(self):
        tool = WebSearchTool()
        result = tool.search("")

        # SearXNG may return empty or general results
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_search_it_category(self):
        tool = WebSearchTool()
        result = tool.search("fastapi tutorial", categories="it")

        assert "error" not in result
        assert len(result["results"]) > 0


# ── WebFetchTool ──

@requires_searxng
class TestWebFetchToolReal:
    def test_fetch_html_page(self):
        tool = WebFetchTool()
        # PyPI is stable and returns HTML
        result = tool.fetch("https://pypi.org/project/httpx/")

        assert "error" not in result
        assert result["title"] != ""
        assert "httpx" in result["content"].lower()
        assert result["content_length"] > 0

    def test_fetch_plain_text(self):
        tool = WebFetchTool()
        # Python.org robots.txt is a small plain text file
        result = tool.fetch("https://www.python.org/robots.txt")

        assert "error" not in result
        assert result["title"] == ""
        assert "User-agent" in result["content"] or "Disallow" in result["content"]

    def test_fetch_truncation(self):
        tool = WebFetchTool()
        result = tool.fetch("https://pypi.org/project/httpx/", max_chars=200)

        assert "error" not in result
        assert result["truncated"] is True
        assert result["content_length"] > 200  # includes truncation notice

    def test_fetch_blocks_localhost(self):
        tool = WebFetchTool()
        result = tool.fetch("http://127.0.0.1/admin")

        assert "error" in result
        assert "private" in result["error"].lower() or "Blocked" in result["error"]

    def test_fetch_invalid_scheme(self):
        tool = WebFetchTool()
        result = tool.fetch("ftp://example.com")

        assert "error" in result

    def test_fetch_http_error_status(self):
        tool = WebFetchTool()
        # Request a URL that returns a non-2xx status. httpbin sometimes returns
        # 503 when rate-limited, so assert the contract (a non-OK status is
        # surfaced as an "HTTP <code>" error) rather than the exact 404.
        result = tool.fetch("https://httpbin.org/status/404")

        assert "error" in result
        assert "HTTP" in result["error"]


# ── Combined search + fetch workflow ──

@requires_searxng
class TestSearchFetchWorkflow:
    def test_search_then_fetch(self):
        """Simulate the agent workflow: search, then fetch top result."""
        search = WebSearchTool()
        fetch = WebFetchTool()

        search_result = search.search("python httpx library")
        assert "error" not in search_result
        assert len(search_result["results"]) > 0

        top_url = search_result["results"][0]["url"]
        assert top_url.startswith("http")

        fetch_result = fetch.fetch(top_url)
        assert "error" not in fetch_result
        assert fetch_result["content_length"] > 0
