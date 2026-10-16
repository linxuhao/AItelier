"""Real MetaAgent read boundaries, using only disposable synthetic files."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.meta_agent import MetaAgent, _MAX_READ_LINES


@pytest.fixture
def owned(tmp_path):
    root = tmp_path / "repo"
    sibling = tmp_path / "repo-private"
    outside = tmp_path / "elsewhere"
    for directory in (root, sibling, outside):
        directory.mkdir()
    (sibling / "canary.txt").write_text("SYNTHETIC_OUTSIDE_CANARY")
    (outside / "other.txt").write_text("SYNTHETIC_OTHER_CANARY")
    (root / "inside.txt").write_text("inside\nsecond\nthird")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("nested inside")
    (root / "outside-file.txt").symlink_to(sibling / "canary.txt")
    (root / "outside-dir").symlink_to(sibling, target_is_directory=True)
    (root / "inside-link.txt").symlink_to(root / "inside.txt")
    (root / "inside-dir").symlink_to(root / "sub", target_is_directory=True)
    agent = MetaAgent.__new__(MetaAgent)
    agent.ws = SimpleNamespace(get_code_path=lambda pid: root,
                               _get_secure_path=lambda pid: root)
    agent._files_read = set()
    return agent, root, sibling, outside


def selection(kind, root, sibling, outside, *, directory=False):
    tail = "" if directory else "/canary.txt"
    return {
        "relative-prefix": "../repo-private" + tail,
        "absolute-prefix": str(sibling) + tail,
        "relative-other": "../elsewhere" + ("" if directory else "/other.txt"),
        "outside-link": "outside-dir" + tail,
        "outside-file-link": "outside-file.txt",
    }[kind]


@pytest.mark.parametrize("method", ["_tool_read_code_file", "_tool_read_workspace_file"])
@pytest.mark.parametrize("kind", ["relative-prefix", "absolute-prefix", "relative-other", "outside-link", "outside-file-link"])
def test_outside_reads_are_refused_without_edit_authorization(owned, method, kind):
    agent, root, sibling, outside = owned
    result = getattr(agent, method)({"project_id": "own", "path": selection(kind, root, sibling, outside)})
    assert "error" in result, result
    assert "content" not in result
    assert not agent._files_read


@pytest.mark.parametrize("method", ["_tool_list_code_tree", "_tool_list_workspace_tree"])
@pytest.mark.parametrize("kind", ["relative-prefix", "absolute-prefix", "relative-other", "outside-link"])
def test_outside_tree_selection_is_refused(owned, method, kind):
    agent, root, sibling, outside = owned
    result = getattr(agent, method)({"project_id": "own", "subdir": selection(kind, root, sibling, outside, directory=True)})
    assert "error" in result, result
    assert "tree" not in result


@pytest.mark.parametrize("method", ["_tool_list_code_tree", "_tool_list_workspace_tree"])
def test_walkers_do_not_expose_outside_file_links(owned, method):
    agent, root, _, _ = owned
    (root / "sub" / "outside-nested.txt").symlink_to(root / "outside-file.txt")
    result = getattr(agent, method)({"project_id": "own"})
    assert "inside.txt" in result["tree"]
    assert "inside-link.txt" in result["tree"]
    assert "outside-file.txt" not in result["tree"]
    assert "sub/outside-nested.txt" not in result["tree"]
    nested = getattr(agent, method)({"project_id": "own", "subdir": "sub"})
    assert nested["tree"] == ["nested.txt"]


def test_search_does_not_read_outside_file_links(owned):
    agent, _, _, _ = owned
    result = agent._tool_search_code({"project_id": "own", "pattern": "SYNTHETIC_OUTSIDE_CANARY"})
    assert result == {"project_id": "own", "matches": [], "truncated": False}
    inside = agent._tool_search_code({"project_id": "own", "pattern": "second", "glob": "*.txt"})
    assert {match["file"] for match in inside["matches"]} == {"inside.txt", "inside-link.txt"}


@pytest.mark.parametrize("path", ["inside.txt", "inside-link.txt"])
def test_in_root_reads_keep_paging_and_tracking(owned, path):
    agent, root, _, _ = owned
    result = agent._tool_read_code_file({"project_id": "own", "path": path, "start_line": 2, "end_line": 2})
    assert result == {"path": path, "content": "2\tsecond", "start_line": 2, "end_line": 2,
                      "total_lines": 3, "truncated": True}
    assert agent._files_read == {("own", str(root / "inside.txt"))}
    assert agent._tool_read_workspace_file({"project_id": "own", "path": path}) == {
        "path": path, "content": "inside\nsecond\nthird"}


def test_large_code_and_workspace_limits_unchanged(owned):
    agent, root, _, _ = owned
    body = "\n".join(f"line{i}" for i in range(_MAX_READ_LINES + 2))
    (root / "large.txt").write_text(body)
    result = agent._tool_read_code_file({"project_id": "own", "path": "large.txt"})
    assert result["total_lines"] == _MAX_READ_LINES + 2
    assert result["end_line"] == _MAX_READ_LINES
    assert result["truncated"] is True
    (root / "large.txt").write_text("a" * 50003)
    assert len(agent._tool_read_workspace_file({"project_id": "own", "path": "large.txt"})["content"]) == 50000


@pytest.mark.parametrize("method", ["_tool_list_code_tree", "_tool_list_workspace_tree"])
def test_in_root_directory_links_and_visibility_preserved(owned, method):
    agent, root, _, _ = owned
    (root / ".github").mkdir()
    (root / ".github" / "workflow.yml").write_text("ordinary source")
    (root / ".zvec-grep").mkdir()
    (root / ".zvec-grep" / "index.txt").write_text("internal storage")
    tree = getattr(agent, method)({"project_id": "own"})["tree"]
    assert ".github/workflow.yml" in tree
    assert ".zvec-grep/index.txt" not in tree
    assert getattr(agent, method)({"project_id": "own", "subdir": "inside-dir"})["tree"] == ["nested.txt"]
    assert agent._tool_read_code_file({"project_id": "own", "path": "inside-dir/nested.txt"})["content"] == "1\tnested inside"


@pytest.mark.parametrize("method", ["_tool_list_code_tree", "_tool_list_workspace_tree"])
def test_selected_subdir_keeps_links_to_other_files_in_granted_root(owned, method):
    agent, root, _, _ = owned
    (root / "sub" / "root-file.txt").symlink_to(root / "inside.txt")
    result = getattr(agent, method)({"project_id": "own", "subdir": "sub"})
    assert result["tree"] == ["nested.txt", "root-file.txt"]


@pytest.mark.parametrize("method", ["_tool_read_code_file", "_tool_read_workspace_file"])
def test_absolute_in_root_paths_and_symlinked_grant_remain_valid(owned, method):
    agent, root, _, _ = owned
    grant = root.parent / "grant-alias"
    grant.symlink_to(root, target_is_directory=True)
    agent.ws = SimpleNamespace(get_code_path=lambda pid: grant,
                               _get_secure_path=lambda pid: grant)
    result = getattr(agent, method)({"project_id": "own", "path": str(root / "inside.txt")})
    assert "second" in result["content"]
    assert "error" not in result
