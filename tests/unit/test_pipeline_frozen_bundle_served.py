"""Frozen bundle: identity of what is actually SERVED, and the recorded whole
manifest across adoption, rollback, reconciliation, reload and restart.

Real SkillFlow (SQLite), the real SDK ToolLoader and the host's
AItelierToolLoader, ConfigRegistry/ConfigManifest, RegistrationTransaction and
the boot scan. Fixture tools and providers only return synthetic constants.
The only injected faults reuse the provider tests' failing ConfigRegistry and
rollback ``BEGIN IMMEDIATE`` connection.
"""
import importlib.util
from pathlib import Path

import pytest
import skillflow
import yaml
from skillflow import PipelineGraph, SkillFlow

from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr
from core import tool_routes
from core.config_registry import ConfigRegistry
from core.tool_routes import AItelierToolLoader

from test_pipeline_frozen_bundle import ROOT, _capture, _runtime, _target
from test_pipeline_frozen_bundle_providers import _FailAtRollbackBegin, _reregister


def _host_runtime(tmp_path, monkeypatch):
    """``_runtime`` with the loader production builds (api.dependencies)."""
    gen = tmp_path / "gen"
    gen.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(gen))
    loader = AItelierToolLoader(Path(skillflow.__file__).parent / "tools",
                                ROOT / "aitelier" / "tools")
    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "proj"))
    return sf, ConfigRegistry(), gen


def _write_tool(tools, name, text, *, schema=True, impl=True):
    """A tool whose callable reads a MODULE constant: OLD and NEW versions have
    byte-identical function code, so only the module's bytes tell them apart."""
    tdir = tools / name
    tdir.mkdir(parents=True, exist_ok=True)
    if schema:
        (tdir / "tool.yaml").write_text(
            f"name: {name}\ndescription: {text} SCHEMA\nparameters: {{}}\n", encoding="utf-8")
    if impl:
        (tdir / "impl.py").write_text(
            f"MESSAGE = {text + ' CALLABLE'!r}\n\ndef {name}(**kw):\n"
            f"    return {{'content': MESSAGE}}\n", encoding="utf-8")
    return tdir


def _with_context_tool(tmp_path, sf, registry, name, text="OLD"):
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    tools = tmp_path / "tools"
    tdir = _write_tool(tools, name, text)
    sf._tool_loader.add_tools_dir(tools)
    _reregister(sf, cfgs, lambda d: d["steps"][0]["context"].append(
        {"source": {"tool": name}}))
    return tools, tdir, lambda: fb.capture(sf, registry, "toy", template_base=tpl,
                                           configs_dirs=[cfgs], tools_dir=tools)


def _import(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_loader_cache_is_proven_by_value_or_refused(tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    loader = sf._tool_loader
    tools, tdir, cap = _with_context_tool(tmp_path, sf, registry, "served_probe")

    cold = cap()                                          # nothing cached: files serve
    assert "served_probe" not in loader._cache
    assert cold["tools"]["served_probe"]["files"]["impl.py"] == fb.sha(
        (tdir / "impl.py").read_bytes())

    loader.load_schema("served_probe")                    # a cached schema equal to the
    assert cap()["identity"] == cold["identity"]          # file is proven by its value
    old_yaml = (tdir / "tool.yaml").read_text(encoding="utf-8")
    (tdir / "tool.yaml").write_text(old_yaml.replace("OLD", "NEW"), encoding="utf-8")
    assert loader.load_schema("served_probe")["description"] == "OLD SCHEMA"
    with pytest.raises(fb.BundleRefused, match="cached schema that differs"):
        cap()
    (tdir / "tool.yaml").write_text(old_yaml, encoding="utf-8")

    sealed = fb.seal(cold, "base", gen)                   # bytes on disk unchanged
    fn = loader.load_fn("served_probe")
    assert fn()["content"] == "OLD CALLABLE"
    # The base SDK loader records no bytes for what it compiled: even with the
    # file unchanged, the cached callable is unprovable and refused.
    with pytest.raises(fb.BundleRefused, match="recorded no bytes"):
        cap()
    with pytest.raises(fb.BundleRefused, match="recorded no bytes"):
        fb.register_sealed(sf, registry, sealed["physical_name"], gen)
    assert sealed["physical_name"] not in sf._graphs
    assert loader._cache["served_probe"][1] is fn         # nothing cleared or reloaded


def test_host_loader_identity_follows_compiled_bytes_cold_warm_and_drift(
        tmp_path, monkeypatch):
    sf, registry, gen = _host_runtime(tmp_path, monkeypatch)
    loader = sf._tool_loader
    tools, tdir, cap = _with_context_tool(tmp_path, sf, registry, "served_probe")
    old_impl = (tdir / "impl.py").read_bytes()

    cold = cap()
    fn = loader.load_fn("served_probe")
    assert fn()["content"] == "OLD CALLABLE"
    assert loader.loaded_impl("served_probe") == (str(tdir / "impl.py"), fb.sha(old_impl))
    loader.load_schema("served_probe")
    warm = cap()                                          # matching warm: same identity
    assert warm["identity"] == cold["identity"]
    fb.verify_providers(sf, cold)
    sealed = fb.seal(cold, "host", gen)

    # Ordinary edit of impl.py: the loader keeps serving the OLD callable.
    (tdir / "impl.py").write_text(old_impl.decode().replace("OLD", "NEW"), encoding="utf-8")
    assert loader.load_fn("served_probe")() == {"content": "OLD CALLABLE"}
    with pytest.raises(fb.BundleRefused, match="compiled from different bytes"):
        cap()
    with pytest.raises(fb.BundleRefused, match="compiled from different bytes"):
        fb.register_sealed(sf, registry, sealed["physical_name"], gen)
    assert sealed["physical_name"] not in sf._graphs
    (tdir / "tool.yaml").write_text("name: served_probe\ndescription: NEW SCHEMA\n"
                                    "parameters: {}\n", encoding="utf-8")
    with pytest.raises(fb.BundleRefused):
        cap()
    assert loader._cache["served_probe"][1] is fn         # the bundle code retargets nothing
    assert loader.load_schema("served_probe")["description"] == "OLD SCHEMA"

    # The operator's own reload (the loader API register_tool/edit_tool use)
    # makes NEW the served provider; only then does a capture describe it.
    loader.add_tools_dir(tools)
    assert loader.load_fn("served_probe")() == {"content": "NEW CALLABLE"}
    fresh = cap()
    assert fresh["identity"] != cold["identity"]
    assert fresh["tools"]["served_probe"]["files"]["impl.py"] == fb.sha(
        (tdir / "impl.py").read_bytes())

    # Host routes still apply on every load, and the route table is identity.
    assert loader.load_fn("pytest") is tool_routes.native_pytest
    fb.verify_providers(sf, fresh)
    monkeypatch.setitem(tool_routes.NATIVE_ROUTES, "served_probe", tool_routes.native_pytest)
    with pytest.raises(fb.BundleRefused, match="tool loader changed"):
        fb.verify_providers(sf, fresh)


def test_role_resolved_schema_must_match_the_served_one(tmp_path, monkeypatch):
    sf, registry, _ = _host_runtime(tmp_path, monkeypatch)
    loader = sf._tool_loader
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    tools = tmp_path / "tools"
    tdir = _write_tool(tools, "role_probe", "OLD")
    loader.add_tools_dir(tools)
    maker = {"model": "flash", "tools": ["read_file", "role_probe"], "template": "maker.md",
             "temperature": 0.7, "max_output_tokens": 4096, "max_tool_turns": 11,
             "native_tool_calling": True, "thinking": {"enable": True, "effort": "high"}}
    sf.register_agent_config_from_dict("maker", maker)

    def cap():
        return fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs],
                          tools_dir=tools)

    cap()
    (tdir / "tool.yaml").write_text("name: role_probe\ndescription: NEW SCHEMA\n"
                                    "parameters: {}\n", encoding="utf-8")
    loader.add_tools_dir(tools)               # loader now serves NEW; the role does not
    assert sf.agent_registry.get("maker").tool_schemas["role_probe"]["description"] == "OLD SCHEMA"
    with pytest.raises(fb.BundleRefused, match="role 'maker' serves a stale schema"):
        cap()
    sf.register_agent_config_from_dict("maker", maker)   # re-resolved from the loader
    assert cap()["tools"]["role_probe"]["files"]["tool.yaml"] == fb.sha(
        (tdir / "tool.yaml").read_bytes())


def test_in_process_providers_are_identified_by_served_code_not_their_file(
        tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    source = tmp_path / "served_provider.py"
    old_text = ("MESSAGE = 'OLD'\n\ndef dyn_probe(**kw):\n    return {'content': MESSAGE}\n\n"
                "def ctx_probe(config_name):\n    return {'mode': MESSAGE}\n")
    source.write_text(old_text, encoding="utf-8")
    first = _import(source, "served_provider")
    schema = {"name": "dyn_probe", "description": "D", "parameters": {}}
    sf._tool_loader.register_dynamic_tool("dyn_probe", schema, first.dyn_probe)
    sf.register_capability("probe_cap", tools=["read_file"], briefing="B",
                           context_provider=first.ctx_probe, owner="test")
    _reregister(sf, cfgs, lambda d: (d["steps"][0]["context"].append(
        {"source": {"tool": "dyn_probe"}}), d["steps"][0].update(capability="probe_cap")))

    def cap():
        return fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])

    before = cap()
    assert before["locations"]["tools"]["dyn_probe"]["kind"] == "dynamic"
    assert before["tools"]["dyn_probe"]["callable"]["qualname"] == "dyn_probe"

    # The file changes on disk; the process still serves the loaded functions.
    source.write_text(old_text.replace("'OLD'", "'NEW'"), encoding="utf-8")
    assert sf._tool_loader.load_fn("dyn_probe")() == {"content": "OLD"}
    again = cap()
    assert again["tools"] == before["tools"]
    assert again["capabilities"] == before["capabilities"]
    fb.verify_providers(sf, before)

    # NEW is now loaded under the same module name and served. Its function
    # code is byte-identical; only the module constant differs.
    second = _import(source, "served_provider")
    assert second.dyn_probe.__code__.co_code == first.dyn_probe.__code__.co_code
    sf._tool_loader.register_dynamic_tool("dyn_probe", schema, second.dyn_probe)
    with pytest.raises(fb.BundleRefused, match="tool 'dyn_probe' changed"):
        fb.verify_providers(sf, before)
    sf._tool_loader.register_dynamic_tool("dyn_probe", schema, first.dyn_probe)
    fb.verify_providers(sf, before)
    sf.register_capability("probe_cap", tools=["read_file"], briefing="B",
                           context_provider=second.ctx_probe, owner="test")
    with pytest.raises(fb.BundleRefused, match="capability 'probe_cap' changed"):
        fb.verify_providers(sf, before)

    class Opaque:                       # served behaviour lives in instance state
        def __call__(self, **kw):
            return {"content": "OPAQUE"}

    sf._tool_loader.register_dynamic_tool("dyn_probe", schema, Opaque())
    with pytest.raises(fb.BundleRefused, match="no served code to identify"):
        cap()


# Every ConfigManifest hint field, each with a non-default value.
WHOLE = {"label": "Frozen manual owner", "has_task_loop": True, "scheduler_owned": False,
         "seed_file": "frozen_seed.md", "seed_default": "frozen default seed",
         "output_step": "make", "input_hint": "frozen input contract",
         "registers_generated_pipeline": True, "registers_generated_addon": True,
         "repo_mode": "none", "preamble_steps": ["make"], "labels": {"make": "Maker"},
         "checkpoint_kind": "conversational", "manual_checkpoints_only": True,
         "checkpoint_kinds": {"review": "file-review"},
         # c7 added ConfigManifest.listed (default True); composed, it is a
         # manifest field like the others and so a frozen hint.
         "listed": False}
OTHER = {**WHOLE, "label": "Second owner", "scheduler_owned": True,
         "manual_checkpoints_only": False, "seed_file": None, "seed_default": None,
         "output_step": None, "checkpoint_kinds": {}}


def _reboot(sf, tmp_path):
    loader = sf._tool_loader
    sf._conn.close()
    boot = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                     workspace_base=str(tmp_path / "ws"), projects_base=str(tmp_path / "proj"))
    registry = ConfigRegistry()
    return boot, registry, pr.load_generated_configs(boot, registry)


def _seal_from_toy(sf, registry, tmp_path, gen, hints, slug):
    registry.register_one(sf, "toy", hint_overrides=hints)
    bundle = fb.capture(sf, registry, "toy", template_base=tmp_path / "templates",
                        configs_dirs=[tmp_path / "cfgs"])
    assert bundle["hints"] == hints
    return fb.seal(bundle, slug, gen)


def test_whole_manifest_is_frozen_and_survives_reload_and_restart(tmp_path, monkeypatch):
    assert set(WHOLE) == set(fb.hint_attrs())         # every manifest field is a frozen hint
    sf, registry, gen, _, rid = _target(tmp_path, monkeypatch)
    sealed = _seal_from_toy(sf, registry, tmp_path, gen, WHOLE, "whole")
    phys = sealed["physical_name"]
    fb.register_sealed(sf, registry, phys, gen)
    assert fb.manifest_hints(registry.get(phys)) == WHOLE   # None-free and exact
    derived = fb.manifest_hints(registry.get("gen_toy"))
    assert derived["scheduler_owned"] is True and derived["seed_file"] == "seed_input.md"

    fb.adopt_bundle(sf, registry, "gen_toy", phys, config_dir=gen,
                    expected_current=fb.unit_identity(fb.read_unit(gen, "gen_toy")))
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "adopted"
    assert desc["old"]["hints"] == derived and desc["new"]["hints"] == WHOLE
    assert fb.manifest_hints(registry.get("gen_toy")) == WHOLE
    assert fb.whole_state(sf, registry, gen, "gen_toy", desc["new"]) == []

    # Settlement checks values, not existence.
    registry.register_one(sf, "gen_toy", hint_overrides=derived)
    problems = fb.whole_state(sf, registry, gen, "gen_toy", desc["new"])
    assert len(problems) == 1 and problems[0].startswith("config manifest ")
    assert {"scheduler_owned", "manual_checkpoints_only", "label"} <= set(
        problems[0][len("config manifest "):].split(", "))

    # An ordinary reload of the unchanged target restores its recorded manifest.
    assert "error" not in pr.reload_generated_pipeline(sf, registry, "gen_toy")
    assert fb.manifest_hints(registry.get("gen_toy")) == WHOLE

    boot, boot_registry, loaded = _reboot(sf, tmp_path)
    assert {"gen_toy", phys} <= set(loaded)
    assert fb.manifest_hints(boot_registry.get("gen_toy")) == WHOLE
    assert fb.manifest_hints(boot_registry.get(phys)) == WHOLE
    assert fb.whole_state(boot, boot_registry, gen, "gen_toy", desc["new"]) == []
    assert fb.launch_block("gen_toy", gen) is None
    assert boot.get_run(rid)["status"] == "paused"


def test_rollback_and_reconcile_restore_the_recorded_manifest_through_restart(
        tmp_path, monkeypatch):
    from core import registration_transaction as rt
    sf, registry, gen, _, _ = _target(tmp_path, monkeypatch)
    first = _seal_from_toy(sf, registry, tmp_path, gen, WHOLE, "first")
    second = _seal_from_toy(sf, registry, tmp_path, gen, OTHER, "second")

    def current():
        return fb.unit_identity(fb.read_unit(gen, "gen_toy"))

    fb.adopt_bundle(sf, registry, "gen_toy", first["physical_name"],
                    expected_current=current(), config_dir=gen)

    class FailingRegistry(ConfigRegistry):
        def register_one(self, *a, **k):
            raise RuntimeError("injected manifest failure after graph write")

    failing = FailingRegistry()
    failing._manifests = registry._manifests

    # 1. A failed adoption rolls back to the PREVIOUS frozen manifest.
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        fb.adopt_bundle(sf, failing, "gen_toy", second["physical_name"],
                        expected_current=current(), config_dir=gen)
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "rolled_back" and desc["old"]["hints"] == WHOLE
    assert fb.manifest_hints(registry.get("gen_toy")) == WHOLE
    sf, registry, loaded = _reboot(sf, tmp_path)
    assert "gen_toy" in loaded
    assert fb.manifest_hints(registry.get("gen_toy")) == WHOLE

    # 2. A failed rollback stays pending; reconciliation to the recorded NEW
    #    side applies that side's manifest, and a restart keeps it.
    real = sf._conn
    conn = _FailAtRollbackBegin(real)

    class ArmAtRollback(rt.RegistrationTransaction):
        def rollback(self):
            conn.armed = True
            return super().rollback()

    failing._manifests = registry._manifests
    sf._conn = conn
    try:
        with monkeypatch.context() as mp:
            mp.setattr(rt, "RegistrationTransaction", ArmAtRollback)
            with pytest.raises(Exception, match="injected rollback BEGIN"):
                fb.adopt_bundle(sf, failing, "gen_toy", second["physical_name"],
                                expected_current=current(), config_dir=gen)
    finally:
        sf._conn = real
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "pending" and desc["old"]["hints"] == WHOLE
    assert desc["new"]["hints"] == OTHER
    assert "unreconciled" in fb.launch_block("gen_toy", gen)
    fb.reconcile_adoption(sf, registry, "gen_toy", to="new", config_dir=gen)
    assert fb.manifest_hints(registry.get("gen_toy")) == OTHER
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "reconciled_new"
    assert fb.whole_state(sf, registry, gen, "gen_toy", desc["new"]) == []
    boot, boot_registry, loaded = _reboot(sf, tmp_path)
    assert "gen_toy" in loaded
    assert fb.manifest_hints(boot_registry.get("gen_toy")) == OTHER
    assert fb.whole_state(boot, boot_registry, gen, "gen_toy", desc["new"]) == []
