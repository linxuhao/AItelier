"""Nested-project boundary tests: a child project.godot under the parent tree
must never be staged or explicitly parsed under the parent's res:// namespace.

Old failure this guards (measured on the pre-fix harness): staging copied the
child contract project wholesale, so the parent's `--import` imported the
child's scripts and its `preload("res://fake.gd")` resolved against the
PARENT's root — passing or failing for the wrong reason — and its global
`class_name`s collided with the parent's. Ordinary sibling scripts with no
child boundary must stay included, and a genuine parent parse error must
still fail the parent gate.
"""
import subprocess
import shutil
from pathlib import Path

import pytest

from docker.godot import godot_harness as gh


def _make_boundary_project(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "main.gd").write_text("extends Node\n")
    # ordinary sibling script, no boundary: must stay part of the parent
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "ok.gd").write_text("extends RefCounted\n")
    # nested child contract project: own root, must be excluded
    child = tmp_path / "contracts" / "child"
    child.mkdir(parents=True)
    (child / "project.godot").write_text("config_version=5\n")
    (child / "fake.gd").write_text("extends Node\nvar r = preload(\"res://fake.gd\")\n")
    (child / "nested" / "deep.gd").parent.mkdir(parents=True, exist_ok=True)
    (child / "nested" / "deep.gd").write_text("extends Node\n")
    # .gdignore exclusion, Godot's own marker mechanism
    ignored = tmp_path / "vendor"
    ignored.mkdir()
    (ignored / ".gdignore").write_text("")
    (ignored / "skip.gd").write_text("extends Node\n")
    return child, ignored


def test_excluded_roots_derived_from_filesystem(tmp_path):
    child, ignored = _make_boundary_project(tmp_path)
    ex = gh._excluded_roots(tmp_path)
    paths = {e["path"]: e["reason"] for e in ex}
    assert paths == {"contracts/child": "nested project (own project.godot)",
                     "vendor": ".gdignore"}


def test_root_itself_is_never_excluded(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    assert gh._excluded_roots(tmp_path) == []


def test_symlinked_boundary_is_not_followed(tmp_path):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "project.godot").write_text("config_version=5\n")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    ex = gh._excluded_roots(tmp_path)
    assert all(e["path"] != "link" for e in ex)


def test_staging_marks_child_roots_in_the_copy(tmp_path):
    child, _ = _make_boundary_project(tmp_path)
    dst = gh._copy_project(tmp_path)
    try:
        assert (dst / "contracts" / "child" / ".gdignore").is_file()
        # user-authored .gdignore is copied as-is; no marker is invented
        assert (dst / "vendor" / ".gdignore").is_file()
    finally:
        import shutil
        shutil.rmtree(dst.parent, ignore_errors=True)


def test_compile_counts_and_reports_only_parent_scripts(tmp_path, monkeypatch):
    child, ignored = _make_boundary_project(tmp_path)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: ("", 2))
    monkeypatch.setattr(gh.shutil, "rmtree", lambda *a, **k: None)
    r = gh.compile_project(str(tmp_path))
    assert r["passed"] is True
    # main.gd + helpers/ok.gd only; the child and the .gdignore'd vendor tree
    # are not the parent's scripts and must not pad the count.
    assert r["file_count"] == 2
    assert {e["path"] for e in r["excluded_child_roots"]} == {"contracts/child", "vendor"}
    assert "contracts/child" in r["summary"]


def test_parent_parse_error_still_fails_despite_child_boundaries(tmp_path, monkeypatch):
    _make_boundary_project(tmp_path)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(
        gh, "_parse_every_script",
        lambda d, t: ("SCRIPT ERROR: Parse Error: bad\n"
                      "          at: GDScript::reload (res://main.gd:2)\n", 1))
    monkeypatch.setattr(gh.shutil, "rmtree", lambda *a, **k: None)
    r = gh.compile_project(str(tmp_path))
    assert r["passed"] is False
    assert any(e["file"] == "res://main.gd" for e in r["errors"])


def test_parse_all_walker_honors_gdignore():
    # The explicit GDScript walk must respect the same boundary staging marks:
    # DirAccess lists ignored directories, so without this check the walker
    # would re-parse child-project scripts under the parent res://.
    assert ".gdignore" in gh._PARSE_ALL_GD
    assert "FileAccess.file_exists" in gh._PARSE_ALL_GD


@pytest.mark.parametrize("marker", ["project.godot", ".gdignore"])
def test_symlink_marker_does_not_hide_parent_scripts(tmp_path, marker):
    _make_boundary_project(tmp_path)
    sibling = tmp_path / "contracts" / "ordinary"
    sibling.mkdir()
    (sibling / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    target = tmp_path / "marker-target"
    target.write_text("config_version=5\n")
    (sibling / marker).symlink_to(target)
    assert "contracts/ordinary" not in {e["path"] for e in gh._excluded_roots(tmp_path)}
    dst = gh._copy_project(tmp_path)
    try:
        assert (dst / "contracts/ordinary/bad.gd").read_bytes() == (sibling / "bad.gd").read_bytes()
        assert not (dst / "contracts/ordinary" / marker).exists()
    finally:
        shutil.rmtree(dst.parent)


def test_broken_ignore_link_does_not_hide_parent_scripts(tmp_path):
    _make_boundary_project(tmp_path)
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / ".gdignore").symlink_to(tmp_path / "missing-marker")
    (ordinary / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    assert "ordinary" not in {e["path"] for e in gh._excluded_roots(tmp_path)}


@pytest.mark.parametrize("marker", ["project.godot", ".gdignore"])
def test_directory_named_like_a_marker_is_not_a_boundary(tmp_path, marker):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    ordinary = tmp_path / "ordinary"
    (ordinary / marker).mkdir(parents=True)
    (ordinary / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    assert gh._excluded_roots(tmp_path) == []


def test_staging_never_follows_script_or_directory_links(tmp_path, monkeypatch):
    _make_boundary_project(tmp_path)
    outside = tmp_path.parent / "outside-boundary-project"
    outside.mkdir()
    (outside / "escape.gd").write_text("extends Node\n")
    (tmp_path / "linked-dir").symlink_to(outside, target_is_directory=True)
    (tmp_path / "linked-file.gd").symlink_to(outside / "escape.gd")
    dst = gh._copy_project(tmp_path)
    try:
        assert not (dst / "linked-dir").exists()
        assert not (dst / "linked-file.gd").exists()
    finally:
        shutil.rmtree(dst.parent)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: ("", 2))
    assert gh.compile_project(str(tmp_path))["file_count"] == 2


def test_staging_marker_failure_cannot_return_an_unprotected_copy(tmp_path, monkeypatch):
    _make_boundary_project(tmp_path)
    real_write = Path.write_text

    def fail_marker(path, *args, **kwargs):
        if path.name == ".gdignore" and path.parent.name == "child":
            raise OSError("injected staging marker failure")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker)
    with pytest.raises(OSError, match="injected staging marker failure"):
        gh._copy_project(tmp_path)


def test_existing_child_ignore_bytes_survive_staging(tmp_path):
    child, _ = _make_boundary_project(tmp_path)
    (child / ".gdignore").write_text("child marker bytes\n")
    dst = gh._copy_project(tmp_path)
    try:
        assert (dst / "contracts/child/.gdignore").read_bytes() == (child / ".gdignore").read_bytes()
    finally:
        shutil.rmtree(dst.parent)


def test_root_ignore_does_not_exclude_the_parent(tmp_path):
    _make_boundary_project(tmp_path)
    (tmp_path / ".gdignore").write_text("root marker bytes\n")
    assert "." not in {e["path"] for e in gh._excluded_roots(tmp_path)}
    dst = gh._copy_project(tmp_path)
    try:
        assert not (dst / ".gdignore").exists()
        assert (dst / "main.gd").is_file()
        assert (tmp_path / ".gdignore").read_text() == "root marker bytes\n"
    finally:
        shutil.rmtree(dst.parent)


def test_root_ignore_directory_is_parent_source_and_stays_staged(tmp_path, monkeypatch):
    _make_boundary_project(tmp_path)
    hidden = tmp_path / ".gdignore"
    hidden.mkdir()
    (hidden / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    assert "." not in {e["path"] for e in gh._excluded_roots(tmp_path)}
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))

    def parse_staged(dst, timeout):
        # The parser can only report what staging copied; no Godot run.
        if (dst / ".gdignore" / "bad.gd").is_file():
            return ("SCRIPT ERROR: Parse Error: bad\n"
                    "          at: GDScript::reload (res://.gdignore/bad.gd:2)\n", 2)
        return ("", 1)

    monkeypatch.setattr(gh, "_parse_every_script", parse_staged)
    result = gh.compile_project(str(tmp_path))
    assert result["file_count"] == 3
    assert result["passed"] is False
    assert any(e["file"] == "res://.gdignore/bad.gd" for e in result["errors"])
    dst = gh._copy_project(tmp_path)
    try:
        assert (dst / ".gdignore" / "bad.gd").read_bytes() == (hidden / "bad.gd").read_bytes()
    finally:
        shutil.rmtree(dst.parent)


def test_unreadable_inventory_cannot_silently_return_no_boundaries(tmp_path, monkeypatch):
    def unreadable(*args, onerror=None, **kwargs):
        if onerror is not None:
            onerror(PermissionError("injected inventory error"))
        return iter(())

    monkeypatch.setattr(gh.os, "walk", unreadable)
    with pytest.raises(PermissionError, match="injected inventory error"):
        gh._excluded_roots(tmp_path)


def test_nested_inventory_is_sorted_and_ordinary_contract_siblings_remain(tmp_path, monkeypatch):
    child, _ = _make_boundary_project(tmp_path)
    deep = child / "inner"
    deep.mkdir()
    (deep / "project.godot").write_text("config_version=5\n")
    sibling = tmp_path / "contracts" / "child-extra"
    sibling.mkdir()
    (sibling / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    excluded = gh._excluded_roots(tmp_path)
    assert [e["path"] for e in excluded] == ["contracts/child", "vendor"]
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: (
        "SCRIPT ERROR: Parse Error: bad\n"
        "          at: GDScript::reload (res://contracts/child-extra/bad.gd:2)\n", 2))
    result = gh.compile_project(str(tmp_path))
    assert result["file_count"] == 3
    assert result["passed"] is False
    assert any(e["file"] == "res://contracts/child-extra/bad.gd" for e in result["errors"])
    assert before == {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_child_can_be_staged_and_checked_under_its_own_root(tmp_path, monkeypatch):
    child, _ = _make_boundary_project(tmp_path)
    seen = []

    def engine_stub(args, **kwargs):
        dst = Path(args[args.index("--path") + 1])
        assert (dst / "project.godot").read_bytes() == (child / "project.godot").read_bytes()
        assert (dst / "fake.gd").read_bytes() == (child / "fake.gd").read_bytes()
        assert not (dst / ".gdignore").exists()
        seen.append(dst)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(gh, "_run", engine_stub)
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: ("", 2))
    result = gh.compile_project(str(child))
    assert result["passed"] is True
    assert result["file_count"] == 2
    assert result["excluded_child_roots"] == []
    assert len(seen) == 2


def test_explicit_walk_checks_child_roots_links_and_hidden_parent_directories():
    assert 'sub.path_join("project.godot")' in gh._PARSE_ALL_GD
    assert "d.is_link(n)" in gh._PARSE_ALL_GD
    assert "d.include_hidden = true" in gh._PARSE_ALL_GD
    assert 'n != ".godot"' in gh._PARSE_ALL_GD
    assert 'n != ".git"' in gh._PARSE_ALL_GD


@pytest.mark.parametrize("kind", ["symlink", "broken-link", "directory"])
def test_invalid_parent_project_marker_cannot_be_a_pass(tmp_path, monkeypatch, kind):
    marker = tmp_path / "project.godot"
    if kind == "directory":
        marker.mkdir()
    else:
        target = tmp_path / "config-target"
        if kind == "symlink":
            target.write_text("config_version=5\n")
        marker.symlink_to(target)
    (tmp_path / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: ("", 1))
    result = gh.compile_project(str(tmp_path))
    assert result["passed"] is False
    assert result["errors"][0]["kind"] == "boundary"
    assert result["errors"][0]["file"] == "project.godot"


def test_hidden_parent_scripts_are_counted_and_keep_named_parse_errors(tmp_path, monkeypatch):
    _make_boundary_project(tmp_path)
    hidden = tmp_path / ".ordinary"
    hidden.mkdir()
    (hidden / "bad.gd").write_text("extends Node\nfunc broken(:\n")
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(gh, "_parse_every_script", lambda d, t: (
        "SCRIPT ERROR: Parse Error: bad\n"
        "          at: GDScript::reload (res://.ordinary/bad.gd:2)\n", 2))
    result = gh.compile_project(str(tmp_path))
    assert result["file_count"] == 3
    assert result["passed"] is False
    assert any(e["file"] == "res://.ordinary/bad.gd" for e in result["errors"])


def test_actual_explicit_probe_and_staging_each_carry_the_boundary(tmp_path, monkeypatch):
    child, _ = _make_boundary_project(tmp_path)
    (tmp_path / "main.gd").write_text("class_name PlayerProfile\nextends Node\n")
    (child / "fake.gd").write_text(
        'class_name PlayerProfile\nextends Node\nvar r = preload("res://fake.gd")\n')
    seen = []

    def engine_stub(args, **kwargs):
        dst = Path(args[args.index("--path") + 1])
        # This observes staged bytes and the injected parser, without a Godot run.
        assert (dst / "contracts/child/.gdignore").is_file()
        assert (dst / "contracts/child/fake.gd").read_bytes() == (child / "fake.gd").read_bytes()
        if "--script" in args:
            probe = (dst / "__parse_all.gd").read_text()
            assert 'FileAccess.file_exists(sub.path_join(".gdignore"))' in probe
            assert 'FileAccess.file_exists(sub.path_join("project.godot"))' in probe
            seen.append("explicit-parser")
            return subprocess.CompletedProcess([], 0, "PARSE_ALL_LOADED=2/2\n", "")
        seen.append("import")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(gh, "_run", engine_stub)
    result = gh.compile_project(str(tmp_path))
    assert result["passed"] is True
    assert result["file_count"] == 2
    assert seen == ["import", "import", "explicit-parser"]
    assert not (child / ".gdignore").exists()
