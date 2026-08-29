"""A repo-less run's own workspace must not be presented to it as "the repo".

`WorkspaceManager.get_code_path` now answers None for a project that declares
`repo_type='none'`. Both prompt-assembly entry points then did
`if code_path is None: code_path = project_path` — the DPS WORKSPACE — and
`_build_workspace_tree` renders whatever it is given under

    # repo root (write paths are relative to here, e.g. strkit/core.py …)

So every agent step of every repo-less run (pipeline_forge, the converters, and
every generated pipeline derived `none`) was shown its own step directories
labelled as the repository, and told to write relative to them.

Before the None answer existed, `code_path` was an empty invented directory and
`_tree_lines` returned nothing, so no repo block was emitted at all. The fix
restores that outcome by decision instead of by the directory happening to be
empty.
"""
from pathlib import Path

from core.prompt_assembler import PromptAssembler


def _workspace(tmp_path) -> Path:
    """A DPS workspace with content — so nothing here passes merely because a
    directory is empty."""
    ws = tmp_path / "ws"
    (ws / "dpe_default_v2" / "1").mkdir(parents=True)
    (ws / "dpe_default_v2" / "1" / "SOTA.md").write_text("x", encoding="utf-8")
    (ws / "project").mkdir(parents=True, exist_ok=True)
    (ws / "project" / "project_brief.md").write_text("brief", encoding="utf-8")
    return ws


def test_the_workspace_tree_shows_no_repo_block_for_a_repoless_run(tmp_path):
    tree = PromptAssembler()._build_workspace_tree(
        _workspace(tmp_path), "2", code_path=None)

    assert "repo root" not in tree, (
        f"a repo-less run was shown its own workspace as the repository:\n{tree}")
    assert "SOTA.md" not in tree.split("Step_")[0], tree


def test_the_control_still_renders_a_real_repo(tmp_path):
    """Without this, the test above would pass on a build that had simply
    stopped emitting the repo block for everyone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "strkit").mkdir()
    (repo / "strkit" / "core.py").write_text("x", encoding="utf-8")

    tree = PromptAssembler()._build_workspace_tree(
        _workspace(tmp_path), "2", code_path=repo)

    assert "repo root" in tree and "core.py" in tree


def test_assemble_does_not_relabel_the_workspace_as_the_repo(tmp_path):
    """The same substitution lived in `assemble` too, and that is the one every
    agent step actually goes through."""
    prompt = PromptAssembler().assemble(
        "2", _workspace(tmp_path), code_path=None, native=True)

    assert "repo root" not in prompt, prompt[:2000]


def test_assemble_still_shows_a_real_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "core.py").write_text("x", encoding="utf-8")

    prompt = PromptAssembler().assemble(
        "2", _workspace(tmp_path), code_path=repo, native=True)

    assert "repo root" in prompt and "core.py" in prompt
