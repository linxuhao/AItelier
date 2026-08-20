"""gdscript_check — the per-task GDScript parse gate (host-side tool)."""

import json
import urllib.error

import pytest

from aitelier.tools.gdscript_check.impl import gdscript_check


def test_no_gd_files_passes(tmp_path):
    # A step that wrote only .tscn or docs has nothing to parse.
    (tmp_path / "notes.md").write_text("hi")
    assert gdscript_check(files=["*.gd"], workspace_root=str(tmp_path)) == {
        "all_passed": True, "results": []}


def test_paths_are_reported_relative_to_the_staging_root(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    f = tmp_path / "scripts" / "a.gd"
    f.write_text("extends Node\n")

    class _Resp:
        def read(self):
            return json.dumps({"all_passed": False, "results": [
                {"file": str(f), "passed": False, "error_message": "boom"}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    # The absolute container path is noise in the implementer's retry prompt.
    assert r["results"][0]["file"] == "scripts/a.gd"


def test_an_unreachable_builder_skips_loudly_instead_of_failing(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.gd").write_text("extends Node\n")

    def _boom(*a, **k):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    # A sidecar that is down must not fail every task in the loop...
    assert r["all_passed"] is True
    assert r["gate_skipped"] is True
    # ...but a skipped gate must never read as a green one.
    assert "GATE SKIPPED" in capsys.readouterr().out
