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
    engine._write_scope_step_id = "t_impl"
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


def test_refusal_survives_reclaim_clean_delivery_and_review(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    artifact = tmp_path / "artifact"
    scope = WriteScope("card-a", ("owned/",))

    class Executor:
        def execute_tool(self, name, params, **kwargs):
            target = Path(kwargs["project_root"]) / params["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(params["content"], encoding="utf-8")
            return {"written": params["file"]}

    _install_skillflow(monkeypatch, Executor())
    engine = _engine(scope, artifact)
    engine._code_path = str(repo)
    first = engine._exec_tool({
        "tool": "create",
        "params": {"file": "sibling/lost.txt", "content": "blocked"},
    })
    assert first["scope_violation"] is True

    class Factory:
        def is_native(self, name):
            return False
        def get_fallback_to_json(self, name):
            return False

    engine.factory = Factory()
    def clean_delivery(*args, **kwargs):
        result = engine._exec_tool({
            "tool": "create",
            "params": {"file": "owned/clean.txt", "content": "ok"},
        })
        assert result["written"] == "owned/clean.txt"
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "t_impl [card-a] clean delivery")
        return True
    engine._run_tool_step = clean_delivery
    assert engine.run_step(
        task_id=1, step_id="t_impl", workspace=object(), project_id="p",
        agent_config_name="role", resolved_context={}, tool_schemas={},
        output_target="code", output_fixed={}, config_name="g",
        artifact_dir=str(artifact), write_scope=scope, run_id="run",
        step_instance_id=1, claim_epoch=2,
    ) is True

    # A later SkillFlow retry may allocate a new step instance while retaining
    # the same task artifact. That clean reclaim must not erase the first claim.
    engine._run_tool_step = lambda *args, **kwargs: True
    assert engine.run_step(
        task_id=1, step_id="t_impl", workspace=object(), project_id="p",
        agent_config_name="role", resolved_context={}, tool_schemas={},
        output_target="code", output_fixed={}, config_name="g",
        artifact_dir=str(artifact), write_scope=scope, run_id="run",
        step_instance_id=2, claim_epoch=1,
    ) is True

    receipt = json.loads((artifact / "write_scope_receipt.json").read_text())
    assert [(item["step_instance_id"], item["claim_epoch"])
            for item in receipt["claim_history"]] == [(1, 1), (1, 2), (2, 1)]
    assert receipt["violations"][0]["requested_path"] == "sibling/lost.txt"
    assert receipt["violations"][0]["claim_epoch"] == 1
    assert (repo / "owned/clean.txt").read_text() == "ok"

    review = closeout_gate(
        project_root=str(repo), owns=["owned/"], shared_hotspots=[],
        task_name="card-a", scope_violations=receipt["violations"],
    )
    review_context = {"[closeout_gate]": review["content"]}
    assert "sibling/lost.txt" in review_context["[closeout_gate]"]
    assert "attempted scope violations" in review_context["[closeout_gate]"]
    assert "claim epoch 1" in review_context["[closeout_gate]"]


def test_corrupt_prior_receipt_cannot_become_clean(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "write_scope_receipt.json").write_text("{broken")
    engine = _engine(WriteScope("card-a", ("owned/",)), artifact)
    engine._load_write_scope_receipt()
    engine._persist_write_scope_receipt()
    receipt = json.loads((artifact / "write_scope_receipt.json").read_text())
    assert receipt["violations"][0]["tool"] == "write_scope_receipt"
    assert "unreadable" in receipt["violations"][0]["reason"]
