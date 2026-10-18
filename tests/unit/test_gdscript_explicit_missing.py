"""Explicit required GDScript targets through the shipped registry and consumer."""
import json
import urllib.error
from pathlib import Path

import pytest
import skillflow
from skillflow.step_validation import StepValidator

from core.tool_routes import AItelierToolLoader


def _loader():
    loader = AItelierToolLoader(Path(skillflow.__file__).resolve().parent / "tools")
    loader.add_tools_dir(Path(__file__).resolve().parents[2] / "aitelier" / "tools")
    return loader


def _helper(monkeypatch, unavailable=False):
    calls = []

    def check(req, **kwargs):
        calls.append(json.loads(req.data)["files"])
        if unavailable:
            raise urllib.error.URLError("stub helper unavailable")

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({"all_passed": True, "results": [
                    {"file": f, "passed": True, "error_message": ""}
                    for f in calls[-1]]}).encode()

        return Response()

    monkeypatch.setattr("urllib.request.urlopen", check)
    return calls


@pytest.mark.parametrize("mixed", [False, True], ids=["missing-only", "mixed"])
@pytest.mark.parametrize("unavailable", [False, True], ids=["helper-ok", "helper-down"])
@pytest.mark.parametrize("consumer", ["registered", "automatic"])
def test_required_missing_literal_cannot_pass(tmp_path, monkeypatch, mixed,
                                             unavailable, consumer):
    (tmp_path / "present.gd").write_text("extends Node\n")
    missing = "game/required_missing.gd"
    files = ["present.gd", missing] if mixed else [missing]
    calls = _helper(monkeypatch, unavailable)
    loader = _loader()
    if consumer == "registered":
        result = loader.load_fn("gdscript_check")(
            files=files, workspace_root=str(tmp_path))
        passed, issues = result["all_passed"], result["results"]
    else:
        # The engine's changed-file filter must retain required literal paths,
        # including one absent from the candidate inventory.
        result = StepValidator(loader, tmp_path, candidate_files=["present.gd"]).validate(
            [{"tool": "gdscript_check", "files": files}])
        passed, issues = result["passed"], result["errors"]
    assert passed is False, result
    assert any(issue.get("file") == missing
               and "missing" in issue.get("error_message", "").lower()
               for issue in issues), result
    # A known local absence needs no helper, and cannot become a helper skip.
    assert calls == []


@pytest.mark.parametrize("files", [None, [], ["*.gd"]])
def test_empty_discovery_remains_a_noop(tmp_path, monkeypatch, files):
    calls = _helper(monkeypatch)
    result = _loader().load_fn("gdscript_check")(
        files=files, workspace_root=str(tmp_path))
    assert result == {"all_passed": True, "results": []}
    assert calls == []


def test_present_literal_is_actually_checked_through_automatic_validation(tmp_path, monkeypatch):
    (tmp_path / "present.gd").write_text("extends Node\n")
    calls = _helper(monkeypatch)
    result = StepValidator(_loader(), tmp_path, candidate_files=["present.gd"]).validate(
        [{"tool": "gdscript_check", "files": ["present.gd"]}])
    assert result == {"passed": True, "errors": [], "warnings": []}
    assert calls == [[str(tmp_path / "present.gd")]]


def test_deleted_gdscript_is_not_a_required_literal_under_discovery(tmp_path, monkeypatch):
    calls = _helper(monkeypatch)
    result = StepValidator(_loader(), tmp_path, candidate_files=["deleted.gd"]).validate(
        [{"tool": "gdscript_check", "files": ["*.gd"]}])
    assert result == {"passed": True, "errors": [], "warnings": []}
    assert calls == []
