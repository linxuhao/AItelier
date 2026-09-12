from pathlib import Path

from core.meta_agent import MetaAgent
from core.prompt_assembler import PromptAssembler
import aitelier.tools.semantic_search.impl as semantic_impl
import api.project_routers as project_routers
from fastapi import HTTPException


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".zvec-grep/index.zvec").mkdir(parents=True)
    (repo / ".zvec-grep/index.zvec/CURRENT").write_text("INTERNAL_ONLY_NEEDLE\n")
    (repo / ".zvec-grep/files.zvec").mkdir()
    (repo / ".zvec-grep/files.zvec/LOG").write_bytes(b"BIN\0INTERNAL_ONLY_NEEDLE")
    (repo / "nested/.zvec-grep/index.zvec").mkdir(parents=True)
    (repo / "nested/.zvec-grep/index.zvec/MANIFEST").write_text("INTERNAL_ONLY_NEEDLE\n")
    (repo / "src").mkdir()
    (repo / "src/main.py").write_text("REAL_SOURCE_NEEDLE = 1\n")
    (repo / ".github").mkdir()
    (repo / ".github/workflow.yml").write_text("VISIBLE_HIDDEN_SOURCE_NEEDLE\n")
    return repo


class _Workspace:
    def __init__(self, repo):
        self.repo = repo

    def get_code_path(self, _project_id):
        return self.repo

    def _get_secure_path(self, _project_id):
        return self.repo


class _Db:
    def get_project(self, project_id):
        return {"project_id": project_id, "repo_path": str(self.repo)}

    def __init__(self, repo):
        self.repo = repo


def _agent(repo):
    agent = object.__new__(MetaAgent)
    agent.ws = _Workspace(repo)
    return agent


def test_butler_search_and_tree_hide_root_and_nested_index_storage(tmp_path):
    repo = _repo(tmp_path)
    agent = _agent(repo)
    assert agent._tool_search_code({"project_id": "p", "pattern": "INTERNAL_ONLY_NEEDLE"})["matches"] == []
    assert [m["file"] for m in agent._tool_search_code({"project_id": "p", "pattern": "REAL_SOURCE_NEEDLE"})["matches"]] == ["src/main.py"]
    hidden = agent._tool_search_code({"project_id": "p", "pattern": "VISIBLE_HIDDEN_SOURCE_NEEDLE"})
    assert [m["file"] for m in hidden["matches"]] == [".github/workflow.yml"]
    for result in (agent._tool_list_code_tree({"project_id": "p"}),
                   agent._tool_list_workspace_tree({"project_id": "p"})):
        assert not any(".zvec-grep" in path for path in result["tree"])
        assert ".github/workflow.yml" in result["tree"]
    assert agent._tool_list_code_tree(
        {"project_id": "p", "subdir": ".zvec-grep"}
    )["tree"] == []


def test_prompt_tree_hides_index_storage_but_keeps_dot_source(tmp_path):
    repo = _repo(tmp_path)
    tree = PromptAssembler()._build_workspace_tree(repo, "step", code_path=repo)
    assert ".zvec-grep" not in tree
    assert ".github" in tree
    assert "main.py" in tree


def test_http_workspace_tree_prunes_storage_and_refuses_it_as_subdir(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(project_routers, "_require_writer_for_external_repo",
                        lambda *_args: None)
    out = project_routers.workspace_tree(
        "p", None, None, "code", None, _Db(repo), _Workspace(repo)
    )
    assert not any(".zvec-grep" in path for path in out["tree"])
    assert ".github/workflow.yml" in out["tree"]
    try:
        project_routers.workspace_tree(
            "p", None, ".zvec-grep", "code", None, _Db(repo), _Workspace(repo)
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("internal storage subdir was exposed")


def test_semantic_search_always_sends_explicit_internal_storage_exclusion(tmp_path, monkeypatch):
    seen = {}

    def fake_call(arguments):
        seen.update(arguments)
        return {"result": {"content": [{"type": "text", "text": "src/main.py"}]}}

    monkeypatch.setattr(semantic_impl, "_call", fake_call)
    result = semantic_impl.semantic_search(
        "needle", globs=["src/**"], project_root=str(tmp_path.resolve())
    )
    assert result["content"] == "src/main.py"
    assert seen["globs"] == ["src/**", "!**/.zvec-grep/**"]
