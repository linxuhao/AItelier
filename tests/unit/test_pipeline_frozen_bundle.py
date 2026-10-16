"""Source conformance tests for core.pipeline_frozen_bundle (UNRUN by the author).

They use the REAL SkillFlow, AgentRegistry, ConfigRegistry/ConfigManifest,
pipeline_registry registration + RegistrationTransaction and the boot scan —
no dict fakes of the registry or manifest.
"""
import copy
import json
from pathlib import Path

import pytest
import skillflow
import yaml
from skillflow import PipelineGraph, SkillFlow
from skillflow.tool_loader import ToolLoader

from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr
from core.config_registry import ConfigManifest, ConfigRegistry

ROOT = Path(__file__).resolve().parents[2]

TOY = """
name: toy
description: toy pipeline
begin: make
end_conditions:
  combinator: or
  conditions:
    - {type: node_reached, node: review, result: completed}
steps:
  - id: make
    step_type: agent
    agent_config: maker
    context:
      - source: {config: toy, output: task.md}
      - source: {config: other_a, output: a.md}
      - source: {config: other_b, output: b.md}
    output: {mode: content, fixed: {draft: draft.md}}
    transitions: [{to: review}]
  - id: review
    step_type: agent
    agent_config: reviewer
    output: {mode: content, fixed: {verdict: verdict.md}}
    transitions: [{to: null}]
"""


def _runtime(tmp_path, monkeypatch):
    gen = tmp_path / "gen"
    gen.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(gen))
    loader = ToolLoader(Path(skillflow.__file__).parent / "tools",
                        ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))
    return sf, ConfigRegistry(), gen


def _toy(tmp_path, sf, registry, maker_text="MAKER v1"):
    cfgs, tpl = tmp_path / "cfgs", tmp_path / "templates"
    cfgs.mkdir(exist_ok=True)
    tpl.mkdir(exist_ok=True)
    (tpl / "maker.md").write_text(maker_text, encoding="utf-8")
    (cfgs / "toy.yaml").write_text(TOY, encoding="utf-8")
    sf.register_agent_config_from_dict("maker", {
        "model": "flash", "tools": ["read_file"], "template": "maker.md",
        "temperature": 0.7, "max_output_tokens": 4096, "max_tool_turns": 11,
        "native_tool_calling": True, "thinking": {"enable": True, "effort": "high"}})
    sf.register_agent_config_from_dict("reviewer", {
        "model": "pro", "tools": ["read_file"], "system_prompt": "REVIEWER"})
    sf.register_graph(PipelineGraph.from_yaml(cfgs / "toy.yaml"))
    registry.register_one(sf, "toy")
    return cfgs, tpl


def _capture(tmp_path, sf, registry, **kw):
    cfgs, tpl = _toy(tmp_path, sf, registry, **kw)
    return fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs],
                      tools_dir=tmp_path / "tools"), cfgs, tpl


def test_capture_freezes_actual_roles_templates_and_manifest(tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    bundle, _, _ = _capture(tmp_path, sf, registry)
    maker = bundle["roles"]["maker"]
    assert maker["model"] == "flash" and maker["tools"] == ["read_file"]
    assert maker["config"]["temperature"] == 0.7 and maker["config"]["max_tool_turns"] == 11
    assert "template" not in maker["config"]
    assert bundle["prompts"][maker["prompt"]] == "MAKER v1"      # real template bytes
    assert bundle["prompts"]["inline:reviewer"] == "REVIEWER"
    assert isinstance(registry.get("toy"), ConfigManifest)
    assert bundle["identity"] == fb.sha(fb.body_of(bundle))
    fb.verify_inputs(bundle)


def test_changed_template_missing_and_symlink_refuse(tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    bundle, cfgs, tpl = _capture(tmp_path, sf, registry)
    (tpl / "maker.md").write_text("MAKER v2", encoding="utf-8")
    with pytest.raises(fb.BundleRefused, match="changed"):
        fb.verify_inputs(bundle)
    with pytest.raises(fb.BundleRefused, match="changed"):
        fb.seal(bundle, "arm", tmp_path / "gen")
    tampered = copy.deepcopy(bundle)
    tampered["prompts"]["template:maker.md"] = "evil"
    with pytest.raises(fb.BundleRefused, match="identity"):
        fb.compile_bundle(tampered, "arm")
    (tpl / "maker.md").unlink()
    real = tmp_path / "real.md"
    real.write_text("MAKER v1", encoding="utf-8")
    (tpl / "maker.md").symlink_to(real)
    with pytest.raises(fb.BundleRefused, match="symlink"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])
    (tpl / "maker.md").unlink()
    with pytest.raises(fb.BundleRefused, match="missing"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])


def test_unresolved_role_and_tool_refuse(tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    _, cfgs, tpl = _capture(tmp_path, sf, registry)[0:3]
    sf.agent_registry._configs.pop("reviewer")
    with pytest.raises(fb.BundleRefused, match="unresolved role"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])
    sf.register_agent_config_from_dict("reviewer", {
        "model": "pro", "tools": ["no_such_tool_anywhere"], "system_prompt": "R"})
    with pytest.raises(fb.BundleRefused, match="unresolved tool"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])


def test_compile_is_private_deterministic_and_embeds_prompts(tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    bundle, _, _ = _capture(tmp_path, sf, registry)
    one, two = fb.compile_bundle(bundle, "arm"), fb.compile_bundle(bundle, "arm")
    assert one == two
    phys = one["physical_name"]
    assert phys == f"gen_rsi_arm_{bundle['identity'][:16]}"
    data = yaml.safe_load(one["graph_yaml"])
    assert data["name"] == phys
    assert {s["agent_config"] for s in data["steps"]} == {
        f"{phys}__maker", f"{phys}__reviewer"}
    assert one["role_map"] == {"maker": f"{phys}__maker", "reviewer": f"{phys}__reviewer"}
    role = one["roles"][f"{phys}__maker"]
    assert role["system_prompt"] == "MAKER v1" and "template" not in role
    assert role["model"] == "flash" and role["max_tool_turns"] == 11
    assert yaml.safe_load(one["graph_yaml"])["steps"][0]["context"][0]["source"]["config"] == phys
    assert "toy" in sf._graphs and phys not in sf._graphs   # original untouched
    with pytest.raises(fb.BundleRefused):
        fb.compile_bundle(bundle, "Bad Target!")


def test_seal_is_immutable_and_tamper_evident(tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    bundle, _, _ = _capture(tmp_path, sf, registry)
    sealed = fb.seal(bundle, "arm", gen)
    assert fb.seal(bundle, "arm", gen)["digest"] == sealed["digest"]      # idempotent
    phys = sealed["physical_name"]
    roles_file = gen / f"{phys}.roles.json"
    original = roles_file.read_text(encoding="utf-8")
    roles_file.write_text(original.replace("MAKER v1", "MAKER hacked"), encoding="utf-8")
    with pytest.raises(fb.BundleRefused, match="altered"):
        fb.verify_sealed(gen, phys)
    with pytest.raises(fb.BundleRefused):          # same identity, changed bytes
        fb.seal(bundle, "arm", gen)
    roles_file.write_text(original, encoding="utf-8")
    fb.verify_sealed(gen, phys)
    roles_file.unlink()
    roles_file.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(fb.BundleRefused, match="symlink|missing"):
        fb.verify_sealed(gen, phys)


def test_whitelist_allows_only_prompt_description_and_public_context_order(
        tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    bundle, _, _ = _capture(tmp_path, sf, registry)
    graph = copy.deepcopy(bundle["graph"])
    graph["description"] = "reworded"
    graph["steps"][0]["context"][1], graph["steps"][0]["context"][2] = (
        graph["steps"][0]["context"][2], graph["steps"][0]["context"][1])
    cand = fb.derive_candidate(bundle, {"prompts": {"template:maker.md": "MAKER v2"},
                                        "graph": graph})
    assert cand["derived_from"] == bundle["identity"] and cand["identity"] != bundle["identity"]
    assert cand["prompts"]["template:maker.md"] == "MAKER v2"
    fb.verify_identity(cand)
    fb.verify_inputs(cand)       # base bytes still bind to the actual files

    def refused(edit):
        with pytest.raises(fb.BundleRefused):
            fb.derive_candidate(bundle, edit)

    refused({"roles": {}})                                   # unknown key
    refused({"prompts": {"inline:reviewer": "lenient"}})     # checker prompt
    refused({"prompts": {"template:nope.md": "x"}})
    for mutate in (
            lambda g: g["steps"][0].update(agent_config="other"),
            lambda g: g["steps"][0]["transitions"].append({"to": "make"}),
            lambda g: g["steps"][0].update(output={"mode": "content", "fixed": {"x": "y"}}),
            lambda g: g["steps"][0].update(timeout_seconds=5),
            lambda g: g.update(capabilities=["x"]),
            lambda g: g["steps"][0]["context"].append({"source": {"tool": "dir_tree"}}),
            lambda g: g["steps"][0]["context"][1]["source"].update(required=True),
            lambda g: g["steps"][0]["context"].pop(),
            lambda g: g["steps"].pop(),
            lambda g: g.update(unknown_key=1)):
        changed = copy.deepcopy(bundle["graph"])
        mutate(changed)
        refused({"graph": changed})


def test_builtin_investigate_reference_and_candidate_same_conversion(tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    for name, cfg in yaml.safe_load(
            (ROOT / "agent_configs" / "investigate.yaml").read_text()).items():
        sf.register_agent_config_from_dict(name, cfg)
    sf.register_graph(PipelineGraph.from_yaml(ROOT / "configs" / "investigate.yaml"))
    original = sf.agent_registry.get("investigator").to_dict()
    ref = fb.capture(sf, registry, "investigate", configs_dirs=[ROOT / "configs"],
                     tools_dir=tmp_path / "tools")
    prompt = (ROOT / "templates" / "investigate.md").read_text(encoding="utf-8")
    assert ref["prompts"]["template:investigate.md"] == prompt
    cand = fb.derive_candidate(ref, {"prompts": {"template:investigate.md": prompt + "\nBe brief."}})
    arms = [fb.seal(b, "inv", gen) for b in (ref, cand)]
    assert arms[0]["physical_name"] != arms[1]["physical_name"]
    for arm in arms:
        role = next(iter(arm["roles"].values()))
        assert role["model"] == "flash" and role["max_output_tokens"] == 8192
        assert role["max_tool_turns"] == 32 and role["tools"] == ["list_tree"]
        assert role["native_tool_calling"] is True and role["thinking"] == {"enable": True}
        fb.register_sealed(sf, registry, arm["physical_name"], gen)
        assert isinstance(registry.get(arm["physical_name"]), ConfigManifest)
    assert sf.agent_registry.get("investigator").to_dict() == original   # builtin intact
    assert "investigate" in sf._graphs


def _target(tmp_path, monkeypatch):
    """A real generated target ``gen_toy`` booted through the real boot scan, plus
    a sealed candidate bundle and a paused run pinned to the old roles."""
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    bundle, _, _ = _capture(tmp_path, sf, registry)
    cand = fb.derive_candidate(bundle, {"prompts": {"template:maker.md": "MAKER v2"}})
    sealed = fb.seal(cand, "arm", gen)
    doc = yaml.safe_load(TOY)
    doc["name"] = "gen_toy"
    for step in doc["steps"]:
        step["agent_config"] = "gen_toy__" + step["agent_config"]
        step["context"] = [dict(c, source=dict(c["source"], config="gen_toy"))
                           if c["source"]["config"] == "toy" else c
                           for c in step.get("context", [])]
    (gen / "gen_toy.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (gen / "gen_toy.roles.json").write_text(json.dumps({
        "gen_toy__maker": {"model": "flash", "tools": ["read_file"],
                           "system_prompt": "OLD MAKER"},
        "gen_toy__reviewer": {"model": "pro", "tools": ["read_file"],
                              "system_prompt": "REVIEWER"}}), encoding="utf-8")
    assert "gen_toy" in pr.load_generated_configs(sf, registry)
    rid = sf.create_run("gen_toy", project_id="old-paused")
    sf.start_run(rid)
    sf.pause_run(rid)
    return sf, registry, gen, sealed, rid


def test_adoption_swaps_whole_unit_keeps_old_roles_and_pinned_run(tmp_path, monkeypatch):
    from core.run_graph_view import pinned_run_graph
    sf, registry, gen, sealed, rid = _target(tmp_path, monkeypatch)
    current = fb.unit_identity(fb.read_unit(gen, "gen_toy"))
    result = fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                             expected_current=current, config_dir=gen)
    assert result["previous"] == current and result["identity"] != current
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "adopted"
    unit = fb.read_unit(gen, "gen_toy")
    roles = json.loads(unit["roles_text"])
    assert roles["gen_toy__maker"]["system_prompt"] == "OLD MAKER"       # never overwritten
    new_maker = next(r for r in roles if "__rsi" in r and r.endswith("_maker"))
    assert roles[new_maker]["system_prompt"] == "MAKER v2"
    assert roles[new_maker]["model"] == "flash" and roles[new_maker]["max_tool_turns"] == 11
    assert sf._graphs["gen_toy"].steps[0].agent_config == new_maker
    assert isinstance(registry.get("gen_toy"), ConfigManifest)
    # the paused run is still pinned to the OLD graph and its OLD frozen role
    assert pinned_run_graph(sf, rid)["steps"][0]["agent_config"] == "gen_toy__maker"
    assert sf.agent_registry.get("gen_toy__maker").config["system_prompt"] == "OLD MAKER"
    # a fresh boot reloads both whole bundles from persisted bytes
    booted, boot_registry = SkillFlow(":memory:"), ConfigRegistry()
    loaded = pr.load_generated_configs(booted, boot_registry)
    assert {"gen_toy", sealed["physical_name"]} <= set(loaded)
    assert booted.agent_registry.get("gen_toy__maker").config["system_prompt"] == "OLD MAKER"
    assert booted.agent_registry.get(new_maker).config["system_prompt"] == "MAKER v2"


def test_adoption_refuses_changed_target_and_never_runs_when_unexpected(tmp_path, monkeypatch):
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    before = fb.read_unit(gen, "gen_toy")
    with pytest.raises(fb.BundleRefused, match="target changed"):
        fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                        expected_current="0" * 64, config_dir=gen)
    with pytest.raises(fb.BundleRefused, match="generated config"):
        fb.adopt_bundle(sf, registry, "toy", sealed["physical_name"],
                        expected_current="0" * 64, config_dir=gen)
    assert fb.read_unit(gen, "gen_toy") == before
    assert fb.read_descriptor(gen, "gen_toy") is None


def test_registration_failure_rolls_back_files_live_state_and_descriptor(tmp_path, monkeypatch):
    from skillflow.core import graph_digest
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    before = fb.read_unit(gen, "gen_toy")
    live_before = graph_digest(sf._graphs["gen_toy"].to_dict())
    agents_before = set(sf.agent_registry._configs)

    class Failing(ConfigRegistry):
        def register_one(self, *_a, **_k):
            raise RuntimeError("injected manifest failure")

    failing = Failing()
    failing._manifests = registry._manifests
    with pytest.raises(RuntimeError, match="injected"):
        fb.adopt_bundle(sf, failing, "gen_toy", sealed["physical_name"],
                        expected_current=fb.unit_identity(before), config_dir=gen)
    assert fb.read_unit(gen, "gen_toy") == before
    assert graph_digest(sf._graphs["gen_toy"].to_dict()) == live_before
    assert set(sf.agent_registry._configs) == agents_before
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "rolled_back"
    assert fb.launch_block("gen_toy", gen) is None


def test_crash_mixed_state_blocks_only_target_until_reconciled(tmp_path, monkeypatch):
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    old = fb.read_unit(gen, "gen_toy")
    new = fb.adopted_unit(gen, "gen_toy", sealed["physical_name"])
    fb.write_descriptor(gen, "gen_toy", {
        "schema": fb.SCHEMA, "target": "gen_toy", "sealed": sealed["physical_name"],
        "state": "pending", "expected_current": fb.unit_identity(old),
        "old": {"identity": fb.unit_identity(old), **old},
        "new": {"identity": fb.unit_identity(new), "graph_yaml": new["graph_yaml"],
                "roles_text": new["roles_text"], "hints": new["hints"]}})
    # process died after the graph file was replaced but before the roles file
    (gen / "gen_toy.yaml").write_text(new["graph_yaml"], encoding="utf-8")

    booted, boot_registry = SkillFlow(":memory:"), ConfigRegistry()
    loaded = pr.load_generated_configs(booted, boot_registry)
    assert "gen_toy" not in loaded and sealed["physical_name"] in loaded
    assert "gen_toy" not in booted._graphs
    assert "unreconciled" in fb.launch_block("gen_toy", gen)
    assert fb.launch_block(sealed["physical_name"], gen) is None      # others unaffected
    with pytest.raises(fb.BundleRefused, match="unreconciled"):
        fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                        expected_current=fb.unit_identity(old), config_dir=gen)

    out = fb.reconcile_adoption(sf, registry, "gen_toy", to="old", config_dir=gen)
    assert out["identity"] == fb.unit_identity(old)
    assert fb.read_unit(gen, "gen_toy") == old                         # bytes restored
    assert sf._graphs["gen_toy"].steps[0].agent_config == "gen_toy__maker"
    assert fb.launch_block("gen_toy", gen) is None
    with pytest.raises(fb.BundleRefused, match="no pending"):
        fb.reconcile_adoption(sf, registry, "gen_toy", to="old", config_dir=gen)


def test_corrupt_descriptor_and_altered_seal_block_boot_for_that_name(tmp_path, monkeypatch):
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    fb.descriptor_path(gen, "gen_toy").write_text("{not json", encoding="utf-8")
    (gen / f"{sealed['physical_name']}.roles.json").write_text("{}", encoding="utf-8")
    booted = SkillFlow(":memory:")
    loaded = pr.load_generated_configs(booted, ConfigRegistry())
    assert "gen_toy" not in loaded and sealed["physical_name"] not in loaded
    assert "corrupt" in fb.launch_block("gen_toy", gen)
