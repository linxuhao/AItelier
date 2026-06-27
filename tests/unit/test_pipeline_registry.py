"""Unit tests for core.pipeline_registry — making converter-generated pipelines
runnable: namespacing, host-agent auto-registration, live register + manifest,
in-place update, boot-time load, and graceful failures."""

import textwrap

import pytest
import yaml
from skillflow import SkillFlow

from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry

# A generated-style pipeline: invented agent roles, no registered agent configs.
GEN_YAML = textwrap.dedent("""
    name: placeholder
    description: A tiny generated pipeline
    begin: process
    end_conditions:
      combinator: or
      conditions:
        - type: node_reached
          node: done
          result: completed
    steps:
      - id: process
        step_type: agent
        agent_config: processor
        transitions:
          - to: done
      - id: done
        step_type: agent
        agent_config: summarizer
""")


@pytest.fixture
def sf():
    return SkillFlow(":memory:")


@pytest.fixture
def registry():
    return ConfigRegistry()


@pytest.fixture
def gdir(tmp_path, monkeypatch):
    d = tmp_path / "gen_configs"
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(d))
    return d


def _patch_output(monkeypatch, path):
    monkeypatch.setattr(
        "skillflow.plugins.skill_converter.get_output_file",
        lambda _sf, _rid: path)


def test_name_is_namespaced_and_never_a_core_config():
    assert pr.config_name_for("My Cool Pipeline") == "gen_my_cool_pipeline"
    for core in ("dpe_default_v2", "meta_conversation", "skill_converter"):
        n = pr.config_name_for(core)
        assert n.startswith("gen_") and n != core


def test_register_text_adds_host_agents_graph_and_manifest(sf, registry):
    pr._register_text(sf, registry, "gen_demo", GEN_YAML)
    # invented roles auto-registered as host agents (else register_graph rejects)
    assert "processor" in sf.agent_registry
    assert "summarizer" in sf.agent_registry
    # graph is live under the forced namespaced name
    assert any(g["name"] == "gen_demo" for g in sf.list_graphs())
    # manifest present + carries generated-pipeline hints: butler-driven (so
    # checkpoints relay in-chat) with a seed file (so seed_text reaches step 1).
    m = registry.get("gen_demo")
    assert m is not None
    assert m.scheduler_owned is False
    assert m.seed_file == "seed_input.md"
    assert "process" in m.steps


def test_generated_roles_namespaced_and_dont_clobber_globals(sf, registry, gdir,
                                                             tmp_path, monkeypatch):
    """A generated role that collides with a real (global) agent name must NOT bind
    to or overwrite that agent — it's namespaced per-config."""
    # a pre-existing GLOBAL agent (mimics DPE's 'researcher')
    sf.register_agent_config_from_dict(
        "researcher", {"model": "deepseek/real", "system_prompt": "DPE researcher"})
    yml = GEN_YAML.replace("agent_config: processor", "agent_config: researcher")
    src = tmp_path / "p.yaml"
    src.write_text(yml, encoding="utf-8")
    _patch_output(monkeypatch, src)

    res = pr.register_generated_pipeline(sf, registry, "r1", "My Cool Pipeline")
    cn = res["config_name"]
    ns = f"{cn}__researcher"
    # global 'researcher' untouched (still the DPE agent)
    assert sf.agent_registry.get("researcher").model == "deepseek/real"
    # generated step registered under a namespaced host agent
    assert ns in sf.agent_registry
    assert sf.agent_registry.get(ns).model == "host"
    # persisted YAML uses the namespaced role, never the bare global name
    persisted = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    roles = [s.get("agent_config") for s in persisted["steps"]
             if s.get("step_type") == "agent"]
    assert ns in roles and "researcher" not in roles


def test_namespacing_and_seed_handle_omitted_step_type(sf, registry, gdir,
                                                       tmp_path, monkeypatch):
    """skillflow defaults step_type to 'agent' when omitted; an agent step that
    leaves step_type out must still be namespaced AND seeded."""
    sf.register_agent_config_from_dict(
        "researcher", {"model": "deepseek/real", "system_prompt": "DPE"})
    yml = textwrap.dedent("""
        name: placeholder
        begin: process
        end_conditions:
          combinator: or
          conditions:
            - {type: node_reached, node: done, result: completed}
        steps:
          - id: process
            agent_config: researcher
            transitions:
              - to: done
          - id: done
            agent_config: summarizer
    """)  # NOTE: no step_type on either step
    src = tmp_path / "p.yaml"
    src.write_text(yml, encoding="utf-8")
    _patch_output(monkeypatch, src)
    res = pr.register_generated_pipeline(sf, registry, "n1", "No Type")
    cn = res["config_name"]
    assert "error" not in res
    # collision-prone role got namespaced despite missing step_type
    assert sf.agent_registry.get("researcher").model == "deepseek/real"  # global intact
    assert f"{cn}__researcher" in sf.agent_registry
    data = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    roles = [s.get("agent_config") for s in data["steps"]]
    assert f"{cn}__researcher" in roles and "researcher" not in roles
    # begin step (no step_type) still got the seed source
    bstep = next(s for s in data["steps"] if s["id"] == data["begin"])
    srcs = [(c.get("source", c) or {}) for c in bstep.get("context", [])]
    assert any(x.get("output") == "seed_input.md" for x in srcs)


def test_gen_config_seeds_first_step_and_is_butler_driven(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    src = tmp_path / "p.yaml"
    src.write_text(GEN_YAML, encoding="utf-8")
    _patch_output(monkeypatch, src)
    res = pr.register_generated_pipeline(sf, registry, "s1", "Seedy")
    cn = res["config_name"]
    m = registry.get(cn)
    assert m.scheduler_owned is False
    assert m.seed_file == "seed_input.md"
    # begin step now reads the seed (so start_config_run's seed_text reaches it)
    data = yaml.safe_load((gdir / f"{cn}.yaml").read_text())
    bstep = next(s for s in data["steps"] if s["id"] == data["begin"])
    srcs = [(c.get("source", c) or {}) for c in bstep.get("context", [])]
    assert any(x.get("config") == cn and x.get("output") == "seed_input.md"
               for x in srcs)


def test_update_overwrites_in_place(sf, registry):
    pr._register_text(sf, registry, "gen_demo", GEN_YAML)
    data = yaml.safe_load(GEN_YAML)
    data["steps"][1]["transitions"] = [{"to": "extra"}]
    data["steps"].append({"id": "extra", "step_type": "agent",
                          "agent_config": "checker"})
    pr._register_text(sf, registry, "gen_demo", yaml.safe_dump(data))
    # exactly one manifest, reflecting the NEW graph (manifest reads live + lazy)
    assert [m.config_name for m in registry.list()].count("gen_demo") == 1
    assert "extra" in registry.get("gen_demo").steps


def test_register_generated_pipeline_persists_and_updates(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    src = tmp_path / "skill_pipeline.yaml"
    src.write_text(GEN_YAML, encoding="utf-8")
    _patch_output(monkeypatch, src)

    res = pr.register_generated_pipeline(sf, registry, "run1", "My Cool Pipeline")
    assert res["config_name"] == "gen_my_cool_pipeline"
    assert res["action"] == "created"
    persisted = gdir / "gen_my_cool_pipeline.yaml"
    assert persisted.exists()
    # persisted YAML carries the namespaced name so boot re-scan agrees
    assert yaml.safe_load(persisted.read_text())["name"] == "gen_my_cool_pipeline"
    assert registry.get("gen_my_cool_pipeline") is not None

    # same name again → update in place
    res2 = pr.register_generated_pipeline(sf, registry, "run2", "My Cool Pipeline")
    assert res2["action"] == "updated"


def test_load_generated_configs_on_boot(sf, registry, gdir):
    gdir.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(GEN_YAML)
    data["name"] = "gen_boot_demo"
    (gdir / "gen_boot_demo.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    names = pr.load_generated_configs(sf, registry)
    assert "gen_boot_demo" in names
    assert registry.get("gen_boot_demo") is not None


def test_no_output_yaml_returns_error(sf, registry, gdir, monkeypatch):
    _patch_output(monkeypatch, None)
    res = pr.register_generated_pipeline(sf, registry, "x", "whatever")
    assert "error" in res


def test_invalid_graph_returns_error_and_persists_nothing(sf, registry, gdir,
                                                          tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps: []\n", encoding="utf-8")  # no begin/end_conditions
    _patch_output(monkeypatch, bad)
    res = pr.register_generated_pipeline(sf, registry, "x", "bad one")
    assert "error" in res
    assert not (gdir / "gen_bad_one.yaml").exists()


def test_wrapper_importable():
    from api.dependencies import register_pipeline_from_run
    assert callable(register_pipeline_from_run)
