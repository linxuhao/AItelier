"""Filesystem regression for task-card write isolation in one shared run tree."""
import json
import sys
import types
from pathlib import Path

from core.dpe_pipeline import PipelineEngine
from core.write_scope import WriteScope


def test_sibling_and_root_untracked_files_survive_the_next_task(monkeypatch, tmp_path):
    repo = tmp_path / "shared-run-worktree"
    repo.mkdir()
    root_probe = repo / "_probe_tmp.txt"
    sibling_probe = repo / "sibling" / "payload.bin"
    root_probe.write_bytes(b"root\x00original")
    sibling_probe.parent.mkdir()
    sibling_probe.write_bytes(b"sibling\xfforiginal")

    calls = []
    class Executor:
        def execute_tool(self, name, params, **kwargs):
            calls.append((name, params))
            if name == "repo_remove_file":
                from aitelier.tools.repo_remove_file.impl import repo_remove_file
                return repo_remove_file(**params, project_root=kwargs["project_root"],
                                        output_target="code")
            target = Path(kwargs["project_root"]) / params["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(params.get("content", "replacement"))
            return {"written": params["file"]}

    fake = types.ModuleType("api.dependencies")
    fake.get_skillflow = lambda: Executor()
    monkeypatch.setitem(sys.modules, "api.dependencies", fake)

    engine = PipelineEngine.__new__(PipelineEngine)
    engine._write_scope = WriteScope("current", ("current/",))
    engine._scope_violations = []
    engine._output_fixed = {}
    engine._output_target = "code"
    engine._artifact_dir = str(tmp_path / "receipt")
    engine._code_path = str(repo)
    engine._run_id = "run"
    engine._current_step = "t_impl"
    engine._step_instance_id = 1
    engine._claim_epoch = 1
    engine._emit = lambda *args, **kwargs: None
    engine._trace = lambda *args, **kwargs: None

    actions = [
        {"tool": "repo_remove_file", "params": {"name": "_probe_tmp.txt"}},
        {"tool": "repo_remove_file", "params": {"name": "sibling/payload.bin"}},
        {"tool": "create", "params": {"file": "_probe_tmp.txt", "content": "new"}},
        {"tool": "edit", "params": {"file": "sibling/payload.bin",
                                           "old_str": "original", "new_str": "new"}},
    ]
    for action in actions:
        assert engine._exec_tool(action)["scope_violation"] is True

    assert calls == []
    assert root_probe.read_bytes() == b"root\x00original"
    assert sibling_probe.read_bytes() == b"sibling\xfforiginal"
    receipt = json.loads((tmp_path / "receipt/write_scope_receipt.json").read_text())
    assert len(receipt["violations"]) == 4
