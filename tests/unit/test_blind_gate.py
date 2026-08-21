"""A gate whose checker cannot see the code must fail, not pass.

Run jinyong-ui: the godot sidecar's workspace bind mount pointed at a deleted
inode, so it answered "no Godot project" for a repo the caller was reading at
that moment. Both gates returned passed:true with file_count 0 and captures 0,
5_review read them as clean, and 21 scripts plus one genuinely failing assertion
went unexamined for the entire run. The existing gate_skipped path did not fire:
the builder was reachable — it was blind.
"""

import json

import pytest

from aitelier.tools.gdscript_check.impl import gdscript_check
from aitelier.tools.godot_compile.impl import godot_compile
from aitelier.tools.godot_playtest.impl import godot_playtest


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _godot_repo(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    return tmp_path


def test_compile_fails_when_the_builder_cannot_see_the_project(tmp_path, monkeypatch):
    repo = _godot_repo(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp({"passed": True, "file_count": 0,
                                               "no_project": True, "errors": []}))
    out = tmp_path / "out"
    godot_compile(project_root=str(repo), out_dir=str(out))
    rep = json.loads((out / "compile_report.json").read_text())
    assert rep["passed"] is False
    assert rep["blind_builder"] is True
    assert "cannot see" in rep["summary"]


def test_playtest_fails_when_the_builder_cannot_see_the_project(tmp_path, monkeypatch):
    repo = _godot_repo(tmp_path)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp({"passed": True, "frames": 0,
                                               "no_project": True, "errors": []}))
    out = tmp_path / "out"
    godot_playtest(project_root=str(repo), out_dir=str(out))
    rep = json.loads((out / "playtest_report.json").read_text())
    assert rep["passed"] is False
    assert rep["blind_builder"] is True


def test_a_genuine_non_godot_repo_still_passes(tmp_path, monkeypatch):
    # The same "no project" answer is CORRECT for a Python repo — the caller
    # never reaches the builder, and the gate must stay a no-op.
    (tmp_path / "main.py").write_text("print(1)\n")

    def _boom(*a, **k):
        raise AssertionError("the builder must not be called for a non-Godot repo")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    out = tmp_path / "out"
    godot_compile(project_root=str(tmp_path), out_dir=str(out))
    rep = json.loads((out / "compile_report.json").read_text())
    assert rep["passed"] is True
    assert "blind_builder" not in rep


def test_gdscript_check_fails_when_fewer_files_come_back_than_were_sent(tmp_path, monkeypatch):
    for n in ("a.gd", "b.gd", "c.gd"):
        (tmp_path / n).write_text("extends Node\n")
    # The sidecar silently drops paths it cannot stat.
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp({"all_passed": True, "results": []}))
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert r["all_passed"] is False
    assert "0 of the 3" in r["error_message"]


def test_gdscript_check_still_passes_when_every_file_was_checked(tmp_path, monkeypatch):
    f = tmp_path / "a.gd"
    f.write_text("extends Node\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(
        {"all_passed": True, "results": [{"file": str(f), "passed": True,
                                          "error_message": ""}]}))
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert r["all_passed"] is True
