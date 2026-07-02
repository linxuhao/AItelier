# tests/unit/test_web_tools.py
# Unit tests for core/web_tools.py — uses mocking, no real network calls.

import pytest
from unittest.mock import patch, MagicMock
from core.web_tools import (
    WebSearchTool,
    WebFetchTool,
    _html_to_text,
    _extract_title,
    _is_private_host,
)


# ── _html_to_text ──

class TestHtmlToText:
    def test_strips_tags(self):
        assert _html_to_text("<p>Hello <b>world</b></p>") == "Hello world"

    def test_removes_script(self):
        html = "<body>Hi<script>alert(1)</script>Bye</body>"
        assert _html_to_text(html) == "HiBye"

    def test_removes_style(self):
        html = "<body>Text<style>.x{color:red}</style>More</body>"
        assert _html_to_text(html) == "TextMore"

    def test_decodes_entities(self):
        assert _html_to_text("a &amp; b &lt; c &gt; d") == "a & b < c > d"

    def test_collapses_whitespace(self):
        assert _html_to_text("a\n\n\n\nb") == "a\n\nb"

    def test_empty_input(self):
        assert _html_to_text("") == ""


# ── _extract_title ──

class TestExtractTitle:
    def test_basic_title(self):
        assert _extract_title("<html><title>My Page</title></html>") == "My Page"

    def test_no_title(self):
        assert _extract_title("<html><body>No title</body></html>") == ""

    def test_case_insensitive(self):
        assert _extract_title("<TITLE>Hello</TITLE>") == "Hello"

    def test_multiline_title(self):
        html = "<title>\n  Hello World  \n</title>"
        assert _extract_title(html) == "Hello World"


# ── _is_private_host ──

class TestIsPrivateHost:
    def test_localhost(self):
        assert _is_private_host("127.0.0.1") is True

    def test_private_10(self):
        assert _is_private_host("10.0.0.1") is True

    def test_private_192_168(self):
        assert _is_private_host("192.168.1.1") is True

    def test_public_ip(self):
        # 8.8.8.8 is Google DNS — public
        assert _is_private_host("8.8.8.8") is False

    def test_unresolvable(self):
        assert _is_private_host("this.domain.does.not.exist.invalid") is True


# ── WebSearchTool (mocked) ──

class TestWebSearchTool:
    @patch("core.web_tools.httpx.get")
    def test_search_returns_results(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number_of_results": 2,
            "results": [
                {"title": "Python", "url": "https://python.org", "content": "Python language"},
                {"title": "PyPI", "url": "https://pypi.org", "content": "Package index"},
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tool = WebSearchTool()
        result = tool.search("python")

        assert result["query"] == "python"
        assert len(result["results"]) == 2
        assert result["results"][0]["title"] == "Python"
        assert result["results"][0]["snippet"] == "Python language"
        assert result["total"] == 2

    @patch("core.web_tools.httpx.get")
    def test_search_max_results_cap(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number_of_results": 20,
            "results": [{"title": f"R{i}", "url": f"https://r{i}.com", "content": f"snippet {i}"} for i in range(20)],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tool = WebSearchTool()
        result = tool.search("test", max_results=3)
        assert len(result["results"]) == 3

    @patch("core.web_tools.httpx.get")
    def test_search_timeout(self, mock_get):
        import httpx
        mock_get.side_effect = httpx.TimeoutException("timeout")

        tool = WebSearchTool()
        result = tool.search("test")
        assert "error" in result
        assert "timed out" in result["error"]
        assert result["results"] == []

    @patch("core.web_tools.httpx.get")
    def test_search_http_error(self, mock_get):
        import httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=mock_resp)

        tool = WebSearchTool()
        result = tool.search("test")
        assert "error" in result
        assert "500" in result["error"]


# ── WebFetchTool (mocked) ──

class TestWebFetchTool:
    def test_fetch_invalid_url(self):
        tool = WebFetchTool()
        result = tool.fetch("")
        assert "error" in result
        assert "Invalid URL" in result["error"]

    def test_fetch_unsupported_scheme(self):
        tool = WebFetchTool()
        result = tool.fetch("ftp://example.com/file")
        assert "error" in result
        assert "Unsupported scheme" in result["error"]

    def test_fetch_blocks_private_ip(self):
        tool = WebFetchTool()
        result = tool.fetch("http://127.0.0.1/admin")
        assert "error" in result
        assert "private" in result["error"].lower() or "Blocked" in result["error"]

    @patch("core.web_tools.httpx.get")
    def test_fetch_html_page(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "<html><head><title>Test Page</title></head><body><p>Hello world</p></body></html>"
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tool = WebFetchTool()
        result = tool.fetch("https://example.com")

        assert result["title"] == "Test Page"
        assert "Hello world" in result["content"]
        assert result["truncated"] is False

    @patch("core.web_tools.httpx.get")
    def test_fetch_plain_text(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "Just plain text"
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tool = WebFetchTool()
        result = tool.fetch("https://example.com/readme.txt")

        assert result["title"] == ""
        assert result["content"] == "Just plain text"

    @patch("core.web_tools.httpx.get")
    def test_fetch_truncation(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "x" * 50000
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        tool = WebFetchTool()
        result = tool.fetch("https://example.com/big.txt", max_chars=100)

        assert result["truncated"] is True
        assert result["content"].startswith("x" * 100)
        # The notice teaches continuation instead of a dead-end "truncated"
        assert "offset=100" in result["content"]

    @patch("core.web_tools.httpx.get")
    def test_fetch_timeout(self, mock_get):
        import httpx
        mock_get.side_effect = httpx.TimeoutException("timeout")

        tool = WebFetchTool()
        result = tool.fetch("https://example.com")
        assert "error" in result
        assert "timed out" in result["error"]

    @patch("core.web_tools.httpx.get")
    def test_fetch_http_error(self, mock_get):
        import httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=mock_resp)

        tool = WebFetchTool()
        result = tool.fetch("https://example.com/missing")
        assert "error" in result
        assert "404" in result["error"]


class TestWebFetchWindowing:
    """Long pages page through windows; #fragments position the first window.

    Regression for the vitest-docs incident: a 10k head-truncation with no
    offset meant content deep in a long page was permanently unreachable —
    the agent re-fetched the same head window until the step burned out.
    """

    @staticmethod
    def _mock_resp(text, content_type="text/plain"):
        resp = MagicMock()
        resp.text = text
        resp.headers = {"content-type": content_type}
        resp.raise_for_status = MagicMock()
        return resp

    @patch("core.web_tools.httpx.get")
    def test_truncated_result_names_next_offset(self, mock_get):
        mock_get.return_value = self._mock_resp("x" * 25000)
        result = WebFetchTool().fetch("https://example.com/big")
        assert result["truncated"] is True
        assert result["total_length"] == 25000
        assert result["next_offset"] == 10000
        assert "offset=10000" in result["content"]

    @patch("core.web_tools.httpx.get")
    def test_offset_continues_from_previous_window(self, mock_get):
        text = "a" * 10000 + "MARKER" + "b" * 10000
        mock_get.return_value = self._mock_resp(text)
        result = WebFetchTool().fetch("https://example.com/big", offset=10000)
        assert result["content"].startswith("MARKER")
        assert result["offset"] == 10000
        assert result["next_offset"] == 20000

    @patch("core.web_tools.httpx.get")
    def test_final_window_not_truncated(self, mock_get):
        mock_get.return_value = self._mock_resp("x" * 25000)
        result = WebFetchTool().fetch("https://example.com/big", offset=20000)
        assert result["truncated"] is False
        assert result["next_offset"] is None
        assert "end of document" in result["content"]

    @patch("core.web_tools.httpx.get")
    def test_fragment_positions_window_at_section(self, mock_get):
        # The linked section sits far beyond the first window — the fragment
        # spelling in text ("vi.unstubAllGlobals") differs from the anchor id.
        text = "intro " * 3000 + "vi.unstubAllGlobals restores all globals"
        mock_get.return_value = self._mock_resp(text)
        result = WebFetchTool().fetch(
            "https://vitest.dev/api/vi.html#vi-unstuballglobals")
        assert "vi.unstubAllGlobals restores all globals" in result["content"]
        assert result["offset"] > 0

    @patch("core.web_tools.httpx.get")
    def test_fragment_not_found_falls_back_to_head(self, mock_get):
        mock_get.return_value = self._mock_resp("short page")
        result = WebFetchTool().fetch("https://example.com/p#nowhere-anchor")
        assert result["content"] == "short page"
        assert result["offset"] == 0

    @patch("core.web_tools.httpx.get")
    def test_explicit_offset_wins_over_fragment(self, mock_get):
        text = "a" * 10000 + "SECTION marker here" + "b" * 10000
        mock_get.return_value = self._mock_resp(text)
        result = WebFetchTool().fetch(
            "https://example.com/p#section", offset=20000)
        assert result["offset"] == 20000

    @patch("core.web_tools.httpx.get")
    def test_short_page_unchanged(self, mock_get):
        mock_get.return_value = self._mock_resp("tiny")
        result = WebFetchTool().fetch("https://example.com/small")
        assert result["content"] == "tiny"
        assert result["truncated"] is False
        assert result["next_offset"] is None
