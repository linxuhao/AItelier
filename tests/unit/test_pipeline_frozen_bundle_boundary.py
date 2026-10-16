"""Declared tool-root boundary and actual whole-unit state at boot and launch.

Real SkillFlow/AItelierSkillFlow, SQLite, ConfigRegistry.build and the boot scan
in production order; only temporary owned tool/config directories.
"""
import json

import pytest
import yaml
from skillflow import SkillFlowError

from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry
from core.skillflow_host import AItelierSkillFlow
from test_pipeline_frozen_bundle import TOY, _capture, _target
from test_pipeline_frozen_bundle_providers import _reregister
from test_pipeline_frozen_bundle_served import WHOLE, _host_runtime, _seal_from_toy


def _tool(tdir, name):
    tdir.mkdir(parents=True)
    (tdir / "tool.yaml").write_text(f"name: {name}\ndescription: d\nparameters: {{}}\n")
    (tdir / "impl.py").write_text(f"def {name}(**kw):\n    return {{'content': 'c'}}\n")


def test_tool_inputs_are_bounded_by_the_loader_root_they_resolve_in(tmp_path, monkeypatch):
    sf, registry, _ = _host_runtime(tmp_path, monkeypatch)
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    root = tmp_path / "declared-tools"
    _tool(root / "real_tool", "real_tool")
    _tool(root / "store" / "inside_alias", "inside_alias")
    # Symlinked tool directory whose target is INSIDE the same root.
    (root / "inside_alias").symlink_to(root / "store" / "inside_alias", target_is_directory=True)
    sf._tool_loader.add_tools_dir(root)
    cap = lambda: fb.capture(sf, registry, "toy", template_base=tpl,
                             configs_dirs=[cfgs], tools_dir=root)

    _reregister(sf, cfgs, lambda d: d["steps"][0]["context"].append(
        {"source": {"tool": "real_tool"}}))
    bundle = cap()
    assert bundle["locations"]["tools"]["real_tool"] == {
        "dir": str(root / "real_tool"), "root": str(root), "kind": "generated"}
    fb.verify_inputs(bundle)

    # The same directory swapped for a symlink after capture is refused on re-read.
    (root / "real_tool").rename(tmp_path / "moved")
    (root / "real_tool").symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(fb.BundleRefused, match="symlink refused"):
        fb.verify_inputs(bundle)
    with pytest.raises(fb.BundleRefused, match="symlink refused"):
        cap()
    # A recorded location without its loader root is not re-derived by guessing.
    stripped = {**bundle, "locations": {**bundle["locations"], "tools": {
        "real_tool": {"dir": str(root / "real_tool"), "kind": "generated"}}}}
    with pytest.raises(fb.BundleRefused, match="no recorded provider location"):
        fb.verify_inputs(stripped)

    _reregister(sf, cfgs, lambda d: d["steps"][0]["context"].__setitem__(
        -1, {"source": {"tool": "inside_alias"}}))
    with pytest.raises(fb.BundleRefused, match="symlink refused"):
        cap()


def _failing_sibling(gen):
    doc = yaml.safe_load(TOY)
    doc["name"] = "gen_zzbad"
    for step in doc["steps"]:
        step["agent_config"] = "gen_zzbad__" + step["agent_config"]
        for c in step.get("context", []):
            if c["source"].get("config") == "toy":
                c["source"]["config"] = "gen_zzbad"
    (gen / "gen_zzbad.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (gen / "gen_zzbad.roles.json").write_text(json.dumps({
        "gen_zzbad__maker": {"model": "flash", "tools": ["read_file"], "system_prompt": "o"},
        "gen_zzbad__reviewer": {"model": "pro", "tools": ["read_file"], "system_prompt": "r"}}))


class _FailSibling(ConfigRegistry):
    def register_one(self, sf, name, **kw):
        if name == "gen_zzbad":
            raise RuntimeError("ordinary unrelated sibling manifest failure")
        return super().register_one(sf, name, **kw)


def _boot(tmp_path, registry_cls=ConfigRegistry):
    """Production order: open the host, ConfigRegistry.build, then the scan."""
    host = AItelierSkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "ws"),
                             projects_base=str(tmp_path / "projects"))
    registry = registry_cls.build(host)
    return host, registry, pr.load_generated_configs(host, registry)


def _runs(host):
    return host._conn.execute("SELECT count(*) FROM skillflow_runs").fetchone()[0]


def test_rolled_back_boot_refuses_actual_launch_until_reconciled(tmp_path, monkeypatch):
    sf, reg, gen, _, rid = _target(tmp_path, monkeypatch)
    sealed = _seal_from_toy(sf, reg, tmp_path, gen, WHOLE, "rollback")
    fb.adopt_bundle(sf, reg, "gen_toy", sealed["physical_name"], config_dir=gen,
                    expected_current=fb.unit_identity(fb.read_unit(gen, "gen_toy")))
    adopted = fb.read_descriptor(gen, "gen_toy")
    files = fb.read_unit(gen, "gen_toy")
    _failing_sibling(gen)
    sf._conn.close()

    host, registry, loaded = _boot(tmp_path, _FailSibling)
    assert loaded == []                                         # whole scan rolled back
    assert fb.manifest_hints(registry.get("gen_toy")) != WHOLE  # build defaults
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "pending" and desc["boot_unverified"]["settled"] == "adopted"
    assert any(m.startswith("config manifest") for m in desc["boot_unverified"]["mismatches"])
    assert {k: desc[k] for k in ("old", "new")} == {k: adopted[k] for k in ("old", "new")}
    assert fb.read_unit(gen, "gen_toy") == files                # recorded bytes untouched
    assert "unreconciled" in fb.launch_block("gen_toy", gen)
    count = _runs(host)
    with pytest.raises(SkillFlowError, match="unreconciled"):    # the actual ingress
        host.create_run("gen_toy", project_id="after-failed-boot")
    assert _runs(host) == count and host.get_run(rid)["status"] == "paused"
    host._conn.close()

    # The next healthy boot keeps refusing it (no implicit repin) ...
    (gen / "gen_zzbad.yaml").unlink()
    (gen / "gen_zzbad.roles.json").unlink()
    host, registry, loaded = _boot(tmp_path)
    assert "gen_toy" not in loaded and "unreconciled" in fb.launch_block("gen_toy", gen)
    # ... until reconciliation verifies the recorded whole bundle.
    fb.reconcile_adoption(host, registry, "gen_toy", to="new", config_dir=gen)
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "reconciled_new"
    assert fb.manifest_hints(registry.get("gen_toy")) == WHOLE
    assert fb.launch_block("gen_toy", gen, sf=host, registry=registry) is None
    assert host.agent_registry.get("gen_toy__maker").config["system_prompt"] == "OLD MAKER"
    assert host.create_run("gen_toy", project_id="after-reconcile")
    assert host.get_run(rid)["status"] == "paused"
    host._conn.close()


def test_healthy_cold_boot_serves_recorded_unit_and_admits_launch(tmp_path, monkeypatch):
    sf, reg, gen, _, rid = _target(tmp_path, monkeypatch)
    sealed = _seal_from_toy(sf, reg, tmp_path, gen, WHOLE, "healthy")
    fb.adopt_bundle(sf, reg, "gen_toy", sealed["physical_name"], config_dir=gen,
                    expected_current=fb.unit_identity(fb.read_unit(gen, "gen_toy")))
    sf._conn.close()
    host, registry, loaded = _boot(tmp_path)
    assert "gen_toy" in loaded
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "adopted" and "boot_unverified" not in desc
    assert fb.whole_state(host, registry, gen, "gen_toy", desc["new"]) == []
    assert fb.launch_block("gen_toy", gen, sf=host, registry=registry) is None
    assert host.create_run("gen_toy", project_id="cold")
    assert host.agent_registry.get("gen_toy__maker").config["system_prompt"] == "OLD MAKER"
    assert host.get_run(rid)["status"] == "paused"

    # The same live state, altered in-process, is refused at launch.
    registry.register_one(host, "gen_toy")
    refused = fb.launch_block("gen_toy", gen, sf=host, registry=registry)
    assert refused and "not served as its recorded whole bundle" in refused
    assert "config manifest" in refused
    host._conn.close()


def test_sealed_arm_not_served_after_rolled_back_boot_is_refused_at_launch(tmp_path, monkeypatch):
    sf, reg, gen, _, _ = _target(tmp_path, monkeypatch)
    sealed = _seal_from_toy(sf, reg, tmp_path, gen, WHOLE, "arm2")
    phys = sealed["physical_name"]
    fb.register_sealed(sf, reg, phys, gen)
    _failing_sibling(gen)
    sf._conn.close()

    host, registry, loaded = _boot(tmp_path, _FailSibling)
    assert loaded == [] and phys in host._graphs                # row survives, roles do not
    refused = fb.launch_block(phys, gen, sf=host, registry=registry)
    assert refused and "live role" in refused and "config manifest" in refused
    count = _runs(host)
    with pytest.raises(SkillFlowError, match="not served as its recorded whole bundle"):
        host.create_run(phys, project_id="arm-after-failed-boot")
    assert _runs(host) == count
    fb.register_sealed(host, registry, phys, gen)               # existing registration path
    assert fb.launch_block(phys, gen, sf=host, registry=registry) is None
    host._conn.close()
