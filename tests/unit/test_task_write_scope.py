import json
import subprocess
import sys
import types
from pathlib import Path

from aitelier.tools.closeout_gate.impl import closeout_gate
from aitelier.tools.repo_delete.impl import repo_delete
from core.dpe_pipeline import PipelineEngine
from core.write_scope import (WriteScope, is_repo_mutator, mutation_paths,
                              scope_from_context)


def _engine(scope, artifact):
    engine = PipelineEngine.__new__(PipelineEngine)
    engine._write_scope = scope
    engine._scope_violations = []
    engine._output_fixed = {}
    engine._output_target = "code"
    engine._artifact_dir = str(artifact)
    engine._run_id = "run"
    engine._current_step = "t_impl"
    engine._step_instance_id = 1
    engine._claim_epoch = 1
    engine._emit = lambda *args, **kwargs: None
    engine._trace = lambda *args, **kwargs: None
    return engine


def _install_skillflow(monkeypatch, executor):
    fake = types.ModuleType("api.dependencies")
    fake.get_skillflow = lambda: executor
    monkeypatch.setitem(sys.modules, "api.dependencies", fake)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_context_is_structured_and_missing_loop_card_denies():
    context = {
        "[current_task]": "t1",
        "Step 3 — tasks/t1.json": json.dumps({
            "id": "t1", "owns": ["src/"],
            "shared_hotspots": ["shared/one.py"]}),
    }
    scope = scope_from_context(context)
    assert scope and scope.policy == "task-card"
    assert scope.authorizes("src/a.py")
    assert scope.authorizes("shared/one.py")
    assert not scope.authorizes("shared/two.py")
    globbed = WriteScope("t2", ("design/*.md",))
    assert globbed.authorizes("design/one.md")
    assert not globbed.authorizes("design/archive/one.md")

    missing = scope_from_context({"[current_task]": "t1"})
    assert missing and missing.policy == "missing-task-card-deny"
    assert not missing.authorizes("anything.py")
    assert scope_from_context({"approved plan": "standalone"}) is None


def test_mutator_inventory_includes_slots_delete_and_media():
    for name in ("create", "edit", "write", "create_code", "append_notes",
                 "repo_remove_file", "gen_image_asset", "gen_audio_asset"):
        assert is_repo_mutator(name)
    assert not is_repo_mutator("test_write")
    assert mutation_paths("repo_remove_file", {"name": "old.py"}) == ["old.py"]
    assert mutation_paths("gen_image_asset", {"dest": "art/a.png"}) == ["art/a.png"]
    assert mutation_paths("write_card", {"id": "x"},
                          {"card": {"file": "cards/*.json"}}) == ["cards/x.json"]
    assert mutation_paths("write_card", {}, {"card": {"file": "cards/*.json"}}) == [None]


def test_preexisting_unowned_files_are_byte_identical(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    artifact = tmp_path / "artifacts"
    repo.mkdir()
    (repo / "_probe_tmp.txt").write_bytes(b"root-before\x00")
    (repo / "sibling").mkdir()
    (repo / "sibling/data.bin").write_bytes(b"sibling-before\xff")
    (repo / "owned").mkdir()
    (repo / "owned/delete.txt").write_text("authorized", encoding="utf-8")

    class Executor:
        def __init__(self):
            self.calls = []

        def execute_tool(self, name, params, **kwargs):
            self.calls.append((name, dict(params)))
            if name == "repo_remove_file":
                from aitelier.tools.repo_remove_file.impl import repo_remove_file
                return repo_remove_file(**params, project_root=kwargs["project_root"],
                                        output_target="code")
            path = repo / params["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.get("content", "changed"), encoding="utf-8")
            return {"written": params["file"]}

    executor = Executor()
    _install_skillflow(monkeypatch, executor)
    engine = _engine(WriteScope("t1", ("owned/",)), artifact)
    engine._code_path = str(repo)

    for tool, params in (
        ("repo_remove_file", {"name": "_probe_tmp.txt"}),
        ("repo_remove_file", {"name": "sibling/data.bin"}),
        ("create", {"file": "_probe_tmp.txt", "content": "recreated"}),
        ("edit", {"file": "sibling/data.bin", "old_str": "x", "new_str": "y"}),
    ):
        result = engine._exec_tool({"tool": tool, "params": params})
        assert result["scope_violation"] is True

    assert (repo / "_probe_tmp.txt").read_bytes() == b"root-before\x00"
    assert (repo / "sibling/data.bin").read_bytes() == b"sibling-before\xff"
    assert executor.calls == []

    result = engine._exec_tool({"tool": "repo_remove_file",
                                "params": {"name": "owned/delete.txt"}})
    assert result["deleted"] == "owned/delete.txt"
    assert not (repo / "owned/delete.txt").exists()
    receipt = json.loads((artifact / "write_scope_receipt.json").read_text())
    assert len(receipt["violations"]) == 4
    assert {v["requested_path"] for v in receipt["violations"]} == {
        "_probe_tmp.txt", "sibling/data.bin"}


def test_closeout_exposes_actual_and_attempted_scope_violations(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    (repo / "owned.txt").write_text("ok")
    (repo / "sibling.txt").write_text("bad")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "t_impl [t1] 2 files")

    attempted = [WriteScope("t1", ("owned.txt",)).refusal("create", "probe.tmp")]
    result = closeout_gate(project_root=str(repo), owns=["owned.txt"],
                           shared_hotspots=[], task_name="t1",
                           scope_violations=attempted)
    assert result["depth"] == "deep"
    assert result["out_of_scope"] == ["sibling.txt"]
    assert result["scope_violations"] == attempted
    assert "attempted scope violations" in result["content"]


def test_queued_delete_preflights_whole_transaction_and_preserves_sibling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    for name, content in (("owned.txt", "owned"), ("sibling.txt", "sibling")):
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    workspace = tmp_path / "workspace"
    card_dir = workspace / "graph" / "3" / "tasks"
    card_dir.mkdir(parents=True)
    (card_dir / "t1.json").write_text(json.dumps({"owns": ["owned.txt"]}))
    stage = tmp_path / "stage"
    stage.mkdir()
    manifest = stage / "_deletions.json"
    manifest.write_text(json.dumps(["owned.txt", "sibling.txt"]))

    refused = repo_delete(str(stage), project_root=str(repo),
                          workspace_root=str(workspace), config_name="graph",
                          task_name="t1")
    assert refused["passed"] is False and refused["deleted"] == []
    assert (repo / "owned.txt").read_text() == "owned"
    assert (repo / "sibling.txt").read_text() == "sibling"
    assert manifest.exists()

    manifest.write_text(json.dumps(["owned.txt"]))
    accepted = repo_delete(str(stage), project_root=str(repo),
                           workspace_root=str(workspace), config_name="graph",
                           task_name="t1", step_id="t_impl")
    assert accepted["committed"] is True
    assert accepted["deleted"] == ["owned.txt"]
    assert not (repo / "owned.txt").exists()
    assert (repo / "sibling.txt").read_text() == "sibling"


def test_dpe_task_card_sources_are_required():
    import yaml
    graph = yaml.safe_load(Path("configs/dpe_default.yaml").read_text())
    nodes = {node["id"]: node for node in graph["steps"]}
    for step_id in ("t_impl", "t_impl_review"):
        cards = [entry["source"] for entry in nodes[step_id]["context"]
                 if isinstance(entry.get("source"), dict)
                 and entry["source"].get("file") == "tasks/$current_task.json"]
        assert cards == [{"step": "3", "file": "tasks/$current_task.json",
                          "required": True}]
