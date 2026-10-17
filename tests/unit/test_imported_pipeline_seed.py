"""Imported generated graphs must consume the launcher's canonical seed."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from skillflow import SkillFlow

from core import pipeline_bundle as pb
from core.config_registry import ConfigRegistry
from core.prompt_assembler import PromptAssembler
from core.run_launcher import start_config_run


def bundle(seed="task.md", *, tool_begin=False, implicit_agent=False):
    first = {"id": "investigate", "agent_config": "reader",
             "context": [{"source": {"config": "gen_imported", "output": seed}},
                         {"source": {"config": "gen_imported", "output": "notes.md"}}],
             "transitions": [{"to": None}]}
    if not implicit_agent:
        first["step_type"] = "agent"
    if tool_begin:
        first = {"id": "investigate", "step_type": "tool", "tool_name": "read_file",
                 "transitions": [{"to": None}]}
    graph = {"name": "gen_imported", "begin": "investigate", "steps": [first]}
    return {pb.BUNDLE_KEY: pb.BUNDLE_VERSION, "config_name": "gen_imported",
            "graph_yaml": yaml.safe_dump(graph),
            "roles": {"reader": {"model": "host", "tools": [],
                                 "system_prompt": "Investigate the user's question."}},
            "tools": {}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    configs, tools = tmp_path / "configs", tmp_path / "tools"
    configs.mkdir(); tools.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(configs))
    monkeypatch.setattr(pb, "_generated_tools_dir", lambda: tools)
    sf = SkillFlow(str(tmp_path / "runtime.db"),
                   workspace_base=str(tmp_path / "workspaces"),
                   projects_base=str(tmp_path / "projects"))
    registry = ConfigRegistry()
    return sf, registry, configs, tools


@pytest.mark.parametrize("seed", ["task.md", "question.txt", "seed_input.md"])
@pytest.mark.parametrize("renamed", [False, True])
def test_import_launch_resolves_seed_into_first_user_prompt(runtime, monkeypatch, seed, renamed):
    sf, registry, configs, _ = runtime
    result = pb.import_pipeline(sf, registry, bundle(seed, implicit_agent=True),
                                **({"name": "renamed"} if renamed else {}))
    name = result["config_name"]
    manifest = registry.get(name)
    assert manifest.seed_file == "seed_input.md"
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = None
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    monkeypatch.setattr("api.dependencies.get_config_registry", lambda: registry)
    monkeypatch.setattr("core.scheduler.wake_scheduler", lambda *a: None)
    # Repository isolation is orthogonal: this fixture has no repository or backend.
    monkeypatch.setattr("core.run_isolation.ensure_for_run", lambda *a, **kw: None)
    marker = "IMPORT-SEED-USER-QUESTION-7be19c"
    launch = start_config_run(db, ws, name, "offline-seed", seed_text=marker,
                              seed_inputs={"notes.md": "PRESERVED-NONSEED-NOTES"})
    assert launch["status"] == "started", launch
    project = sf._workspace.get_project_path("offline-seed")
    assert (project / name / "_seed" / manifest.seed_file).read_text() == marker
    claim = sf.claim_next_step(launch["run_id"])
    assert claim is not None and claim.step_id == "investigate"
    context = claim.inputs["_resolved_context"]
    prompt = PromptAssembler().assemble(claim.step_id, project,
                                        resolved_context=context, native=True)
    print(json.dumps({"config": name, "original_seed": seed,
                      "manifest_seed": manifest.seed_file,
                      "resolved_context": context, "user_prompt": prompt}))
    assert marker in prompt
    assert "PRESERVED-NONSEED-NOTES" in prompt
    persisted = yaml.safe_load((configs / f"{name}.yaml").read_text())
    first = persisted["steps"][0]
    assert first["agent_config"] == f"{name}__reader"
    assert all(c["source"]["config"] == name for c in first["context"])
    assert sum(c["source"]["output"] == "seed_input.md" for c in first["context"]) == 1
    assert any(c["source"]["output"] == seed for c in first["context"])
    # Re-importing the persisted bundle must not duplicate its canonical source.
    again = pb.export_pipeline(name)
    pb.import_pipeline(sf, registry, again)
    assert yaml.safe_load((configs / f"{name}.yaml").read_text()) == persisted


def test_tool_begin_keeps_its_context_contract(runtime):
    sf, registry, configs, _ = runtime
    pb.import_pipeline(sf, registry, bundle(tool_begin=True))
    first = yaml.safe_load((configs / "gen_imported.yaml").read_text())["steps"][0]
    assert "context" not in first


def test_invalid_import_has_no_persistence_or_registration(runtime):
    sf, registry, configs, tools = runtime
    bad = bundle()
    graph = yaml.safe_load(bad["graph_yaml"])
    graph["steps"][0]["transitions"] = [{"to": "missing"}]
    bad["graph_yaml"] = yaml.safe_dump(graph)
    with pytest.raises(pb.BundleError, match="not a valid pipeline"):
        pb.import_pipeline(sf, registry, bad)
    assert list(configs.iterdir()) == list(tools.iterdir()) == []
    assert registry.get("gen_imported") is None
    assert not sf.list_graphs()
    assert "gen_imported__reader" not in sf.agent_registry


@pytest.mark.parametrize("renamed", [False, True])
def test_declared_later_seed_normalizes_only_its_own_input(runtime, renamed):
    sf, registry, configs, _ = runtime
    candidate = bundle("task.md")
    graph = yaml.safe_load(candidate["graph_yaml"])
    graph["x-aitelier"] = {"seed_file": "task.md"}
    graph["begin"] = "prepare"
    graph["steps"].insert(0, {"id": "prepare", "step_type": "tool", "tool_name": "read_file",
                              "tool_params": {"file": "unchanged.txt"},
                              "transitions": [{"to": "investigate"}]})
    graph["steps"][1]["context"][0]["required"] = True
    graph["steps"][1]["context"].append({"source": {"config": "gen_foreign", "output": "task.md"}})
    candidate["graph_yaml"] = yaml.safe_dump(graph)
    result = pb.import_pipeline(sf, registry, candidate, **({"name": "later_renamed"} if renamed else {}))
    name = result["config_name"]
    persisted = yaml.safe_load((configs / f"{name}.yaml").read_text())
    assert persisted["x-aitelier"]["seed_file"] == registry.get(name).seed_file == "seed_input.md"
    assert persisted["steps"][0] == graph["steps"][0]
    assert persisted["steps"][1]["context"] == [
        {"source": {"config": name, "output": "seed_input.md"}, "required": True},
        {"source": {"config": name, "output": "notes.md"}},
        {"source": {"config": "gen_foreign", "output": "task.md"}}]
    pb.import_pipeline(sf, registry, pb.export_pipeline(name))
    assert yaml.safe_load((configs / f"{name}.yaml").read_text()) == persisted


def test_later_self_output_named_like_original_seed_remains_an_output(runtime, monkeypatch):
    from skillflow.core import StepResult
    sf, registry, configs, _ = runtime
    candidate = bundle("task.md")
    graph = yaml.safe_load(candidate["graph_yaml"])
    graph["x-aitelier"] = {"seed_file": "task.md"}
    graph["steps"][0]["output"] = {"mode": "content", "fixed": {"task": "task.md"}}
    graph["steps"][0]["transitions"] = [{"to": "consume"}]
    graph["steps"].append({"id": "consume", "agent_config": "reader",
                            "context": [{"source": {"config": "gen_imported", "output": "task.md"}, "required": True}],
                            "transitions": [{"to": None}]})
    candidate["graph_yaml"] = yaml.safe_dump(graph)
    pb.import_pipeline(sf, registry, candidate)
    persisted = yaml.safe_load((configs / "gen_imported.yaml").read_text())
    assert persisted["steps"][1]["context"] == graph["steps"][1]["context"]
    db, ws = MagicMock(), MagicMock()
    db.get_project.return_value = None
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    monkeypatch.setattr("api.dependencies.get_config_registry", lambda: registry)
    monkeypatch.setattr("core.scheduler.wake_scheduler", lambda *a: None)
    monkeypatch.setattr("core.run_isolation.ensure_for_run", lambda *a, **kw: None)
    launch = start_config_run(db, ws, "gen_imported", "later-output", seed_text="ORIGINAL LAUNCH TASK")
    assert launch["status"] == "started"
    claim = sf.claim_next_step(launch["run_id"])
    assert claim.step_id == "investigate"
    reply = sf.execute_tool("create_task", {"initialContent": "LEGITIMATE LATER TASK OUTPUT"},
                            run_id=claim.token.run_id, step_id=claim.step_id,
                            step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
    assert not reply.get("error"), reply
    sf.confirm_step(claim.token, StepResult())
    consume = sf.claim_next_step(launch["run_id"])
    assert consume.step_id == "consume"
    assert consume.inputs["_resolved_context"]["gen_imported/task.md"] == "LEGITIMATE LATER TASK OUTPUT"
