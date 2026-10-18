"""Host policy against the official 0.2.2 search request/response contract."""
from contextlib import contextmanager
from pathlib import Path
import pytest
import core.run_resources as resources
from aitelier.tools.semantic_search import impl


def reply(text, **extra):
    return {"result": {"content": [{"type": "text", "text": text}], **extra}}


def test_completed_add_update_delete_are_searched_under_lease(monkeypatch, tmp_path):
    (tmp_path / "added.py").write_text("new_marker\n")
    (tmp_path / "updated.py").write_text("updated_marker\n")
    deleted = tmp_path / "deleted.py"
    deleted.write_text("old_marker\n")
    deleted.unlink()
    held = []
    @contextmanager
    def lease(root):
        assert root == str(tmp_path.resolve())
        held.append(True)
        try:
            yield None
        finally:
            held.pop()
    def provider(args):
        assert held, "read lease must include the provider's freshness wait"
        assert args == {"root": str(tmp_path.resolve()), "query": "marker", "limit": 50,
                        "globs": ["*.py", "!**/.zvec-grep/**"], "fts": ["marker"],
                        "freshness": "wait_for_fresh"}
        return reply("freshness: fresh\nadded.py:1 new_marker\nupdated.py:1 updated_marker")
    monkeypatch.setattr(resources, "search_lease", lease)
    monkeypatch.setattr(impl, "_call", provider)
    result = impl.semantic_search(" marker ", limit=99, globs=["*.py"], fts=["marker"], project_root=str(tmp_path))
    assert "error" not in result and "new_marker" in result["content"]
    assert "old_marker" not in result["content"] and not held


@pytest.mark.parametrize("text,extra", [
    ("freshness: possibly_stale\nold.py:20 old_marker", {}),
    ("freshness: possibly_stale\n(no results)", {}),
    ("freshness: fresh\nold_marker", {"structuredContent": {"freshness": "possibly_stale"}}),
])
def test_explicit_stale_cannot_be_source_or_absence(monkeypatch, tmp_path, text, extra):
    monkeypatch.setattr(impl, "_call", lambda args: reply(text, **extra))
    result = impl.semantic_search("marker", project_root=str(tmp_path))
    assert "error" in result and "content" not in result
    assert result["diagnostic"] == text
    assert "search" in result["hint"] and "read" in result["hint"]


@pytest.mark.parametrize("response", [
    {"error": {"message": "refresh refused"}},
    reply("refresh failed", isError=True),
])
def test_refresh_errors_are_not_success(monkeypatch, tmp_path, response):
    monkeypatch.setattr(impl, "_call", lambda args: response)
    result = impl.semantic_search("marker", project_root=str(tmp_path))
    assert "error" in result and "content" not in result
    assert "refresh" in result["error"] and "search" in result["hint"]


def test_wait_timeout_releases_lease_and_keeps_failure(monkeypatch, tmp_path):
    held = []
    @contextmanager
    def lease(root):
        held.append(True)
        try:
            yield None
        finally:
            held.pop()
    def provider(args):
        assert held
        raise TimeoutError("freshness wait timed out")
    monkeypatch.setattr(resources, "search_lease", lease)
    monkeypatch.setattr(impl, "_call", provider)
    result = impl.semantic_search("marker", project_root=str(tmp_path))
    assert "TimeoutError: freshness wait timed out" in result["error"]
    assert "content" not in result and not held


def test_source_snippet_mentioning_stale_is_not_a_status(monkeypatch, tmp_path):
    text = "freshness: fresh\nsource.py:4\n4 freshness: possibly_stale"
    monkeypatch.setattr(impl, "_call", lambda args: reply(text))
    assert impl.semantic_search("marker", project_root=str(tmp_path))["content"] == text
