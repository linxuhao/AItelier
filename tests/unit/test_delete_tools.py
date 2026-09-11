"""Direct code deletion replaces deferred manifests; artifact folders remain."""
from pathlib import Path
import pytest
from aitelier.tools.repo_remove_file.impl import repo_remove_file


def test_delete_is_immediate_and_never_writes_a_manifest(tmp_path):
    (tmp_path / "old.gd").write_text("old")
    (tmp_path / "keep.gd").write_text("keep")
    got = repo_remove_file("old.gd", project_root=str(tmp_path), output_target="code")
    assert got["deleted"] == "old.gd"
    assert not (tmp_path / "old.gd").exists()
    assert (tmp_path / "keep.gd").read_text() == "keep"
    assert not list(tmp_path.rglob("_deletions.json"))
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("bad", ["", "   ", "../secret", "a/../../b", ".git", ".git/config", "/etc/passwd"])
def test_delete_refuses_unsafe_paths(tmp_path, bad):
    (tmp_path / "keep").write_text("keep")
    got = repo_remove_file(bad, project_root=str(tmp_path), output_target="code")
    assert got.get("error"), got
    assert (tmp_path / "keep").read_text() == "keep"


def test_delete_refuses_symlink(tmp_path):
    target = tmp_path / "keep"; target.write_text("keep")
    (tmp_path / "link").symlink_to(target)
    got = repo_remove_file("link", project_root=str(tmp_path), output_target="code")
    assert got.get("error")
    assert target.read_text() == "keep"


@pytest.mark.parametrize("target", ["artifact", ""])
def test_artifact_and_legacy_steps_cannot_delete_code(tmp_path, target):
    (tmp_path / "keep").write_text("keep")
    got = repo_remove_file("keep", project_root=str(tmp_path), output_target=target)
    assert got.get("error") and (tmp_path / "keep").exists()


def test_missing_or_directory_is_not_a_successful_delete(tmp_path):
    (tmp_path / "directory").mkdir()
    for name in ("missing", "directory"):
        assert repo_remove_file(name, project_root=str(tmp_path), output_target="code").get("error")
    assert (tmp_path / "directory").is_dir()


def test_empty_root_never_means_process_cwd():
    assert repo_remove_file("README.md", output_target="code").get("error")


RESERVED_TOOL_PREFIXES = ("write_", "create_", "edit_", "delete_")


def test_no_host_tool_uses_a_skillflow_reserved_prefix():
    import pathlib
    tools_dir = pathlib.Path(__file__).resolve().parents[2] / "aitelier" / "tools"
    offenders = [d.name for d in tools_dir.iterdir()
                 if (d / "tool.yaml").exists()
                 and d.name.startswith(RESERVED_TOOL_PREFIXES)]
    assert offenders == [], (
        f"host tools shadowed by skillflow's write-tool dispatcher: {offenders} — "
        f"every call is routed to write_tools.execute_* and never reaches the impl"
    )
