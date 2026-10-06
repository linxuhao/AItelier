"""Generated role reload uses the real SkillFlow claim and native dispatcher."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry
from core.dpe_pipeline import PipelineEngine

@pytest.mark.parametrize("permitted", [False, True])
def test_reload_claim_and_native_dispatch_honor_exact_role(tmp_path, monkeypatch, permitted):
    name = "gen_source_probe"
    role = name + "__offload_implementer"
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "baseline.py").write_text("baseline = 1\n")
    git(repo, "add", "--", "baseline.py")
    git(repo, "commit", "-qm", "base")
    loader = ToolLoader(Path(__file__).resolve().parents[2] / "aitelier/tools",
                        Path(skillflow.__file__).parent / "tools")
    effects = []
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "artifacts"),
                   code_path_resolver=lambda *a, **kw: repo)
    # The collision that caused the real production source-only admission.
    sf.register_agent_config_from_dict("offload_implementer",
        {"model": "host", "tools": ["focused_check"], "system_prompt": "legal global checks"})
    tools = ["apply_patch"] + (["focused_check"] if permitted else [])
    roles = {role: {"model": "host", "tools": tools,
                    "system_prompt": "source only" if not permitted else "allowed checks"}}
    gdir = tmp_path / "generated"
    gdir.mkdir()
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(gdir))
    graph = {"name": name, "begin": "implement", "steps": [{
        "id": "implement", "agent_config": "offload_implementer",
        "output": {"mode": "write", "target": "code"},
        "context": [{"source": {"from": "repository", "mode": "tool"}}],
        "transitions": [{"to": None}]}]}
    (gdir / (name + ".yaml")).write_text(json.dumps(graph))
    (gdir / (name + ".roles.json")).write_text(json.dumps(roles))
    result = pr.reload_generated_pipeline(sf, ConfigRegistry(), name)
    assert "error" not in result, result
    rid = sf.create_run(name, project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    assert claim.inputs["_agent_config"]["name"] == role
    assert sf.agent_registry.get("offload_implementer").config["system_prompt"] == "legal global checks"
    schemas = claim.inputs["_tool_schemas"]
    catalog = PipelineEngine._to_openai_tools(schemas)
    assert ("focused_check" in [x["function"]["name"] for x in catalog]) is permitted
    assert "apply_patch" in schemas and "read" in schemas and "search" in schemas

    original_load = sf._tool_loader.load_fn
    def effect_spy(kind="", targets=None, scenario=""):
        effects.append({"kind": kind, "targets": targets, "scenario": scenario})
        return {"passed": True}
    monkeypatch.setattr(sf._tool_loader, "load_fn",
        lambda name: effect_spy if name == "focused_check" else original_load(name))
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    e = PipelineEngine()
    e._tool_schemas = schemas
    e._run_id = rid
    e._current_step = claim.step_id
    e._step_instance_id = claim.token.step_instance_id
    e._claim_epoch = claim.token.claim_epoch
    e._project_id = "p"
    e._config_name = name
    e._output_target = "code"
    e._output_fixed = {}
    e._output_dir = claim.inputs["_output_dir"]
    e._artifact_dir = claim.inputs["_artifact_dir"]
    e._get_code_path = lambda *a: repo
    e._write_scope_refusal = lambda *a: None
    before = sf._conn.execute("select count(*) from skillflow_trace where event='operation_admitted'").fetchone()[0]
    for kind in ("pytest", "godot_scenario"):
        out = e._exec_tool({"tool": "focused_check", "params": {"kind": kind}})
        if permitted:
            assert out.get("passed") is True, out
        else:
            assert "not granted" in out["error"]
    after = sf._conn.execute("select count(*) from skillflow_trace where event='operation_admitted'").fetchone()[0]
    assert len(effects) == (2 if permitted else 0)
    if not permitted:
        assert after == before  # no operation admission for excluded calls
    # A fabricated name is refused before even fetching the dispatch dependency.
    monkeypatch.setattr("api.dependencies.get_skillflow",
                        lambda: pytest.fail("unknown call crossed admission boundary"))
    assert "not granted" in e._exec_tool({"tool": "unknown_runtime", "params": {}})["error"]
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    assert "baseline" in str(e._exec_tool({"tool": "read", "params": {"path": "baseline.py"}}))
    assert "baseline" in str(e._exec_tool({"tool": "search", "params": {"pattern": "baseline"}}))
    out = e._exec_tool({"tool": "apply_patch", "params": {
        "patch": "*** Begin Patch\n*** Add File: added.py\n+added = True\n*** End Patch"}})
    assert "error" not in out, out
    assert (repo / "added.py").read_text() == "added = True\n"
    # Finish uses the actual native loop's built-in delivery path.
    from tests.unit.test_native_direct_code import host, response, execute
    h, ws = host(sf, rid, claim, repo,
                 lambda *a, **kw: response("finish_step", summary="source delivered"))
    assert execute(h, ws, rid, claim)

def test_validated_role_binding_is_idempotent_and_keeps_original_source():
    name = "gen_probe"
    data = {"name": name, "begin": "x", "steps": [{
        "id": "x", "agent_config": name + "__author", "transitions": [{"to": None}]}]}
    text = json.dumps(data)
    graph, _ = pr._validated_registration(name, text, roles={name + "__author": {"tools": []}})
    assert graph.steps[0].agent_config == name + "__author"
    assert json.loads(text) == data
