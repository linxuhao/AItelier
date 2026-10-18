"""Supported official MCP incomplete result must not become successful absence."""
from aitelier.tools.semantic_search import impl


def test_official_sdk_input_required_preserves_diagnostic_and_fallback(monkeypatch, tmp_path):
    # Exact pure official SDK constructor output; no authorization or replay.
    result = {"resultType": "input_required", "inputRequests": {},
              "requestState": "OWN_DECLARATIVE_FIXTURE_NOT_SIGNED"}
    monkeypatch.setattr(impl, "_call", lambda args: {"result": result})
    got = impl.semantic_search("concept", project_root=str(tmp_path))
    assert "error" in got and "content" not in got
    assert "input_required" in got["error"] and got["diagnostic"] == result
    assert "search" in got["hint"] and "read" in got["hint"]


def test_source_snippet_mentioning_input_required_remains_source(monkeypatch, tmp_path):
    text = 'freshness: fresh\nsource.py:1\n1 resultType: input_required'
    monkeypatch.setattr(impl, "_call", lambda args: {"result": {
        "content": [{"type": "text", "text": text}]}})
    got = impl.semantic_search("concept", project_root=str(tmp_path))
    assert "error" not in got and got["content"] == text
