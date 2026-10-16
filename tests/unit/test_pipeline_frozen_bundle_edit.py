"""Supported generated-config editors versus a recorded whole bundle.

Tracked form of the independent observer (fixture
``test_independent_edit_retention.py``, sha256 f2c0b150...): after a real
verified adoption, ``edit_template(config='gen_toy', role='maker')`` rewrote the
RETAINED old role ``gen_toy__maker`` and the recorded manifest in place, the
classifier then read the changed target as ordinary, and the old pinned paused
run resumed and claimed the edited prompt.

The observer called the tool and drove the run; it asserted only the drive.
The normal-edit integration creates a new immutable unit for healthy active
content, while historical-only edits still refuse. All cases preserve: every file byte, the descriptor, each live
role, the graph rows and pin and the entire manifest. They still drive the
existing paused run to a claim of OLD MAKER. Setup is the same real helper
(actual AItelierSkillFlow, production ToolLoader factory, paired singleton
DB/SkillFlow/ConfigRegistry, normal start_run isolation, real adoption and real
boot scan). The edit tools are the actual ``api.mcp_router`` bodies, collected
through ``_register_edit_tools``; ``config_edit`` is a real ``MetaAgent``
method. No product method is patched. No provider or model body runs: each
granted claim is released unexecuted.
"""
import json

import pytest
from unittest.mock import MagicMock
from skillflow import SkillFlowError

from core import pipeline_bundle, pipeline_frozen_bundle as fb, pipeline_registry as pr
from core.config_registry import ConfigRegistry
from core.run_graph_view import pinned_run_graph
from core.skillflow_host import AItelierSkillFlow
from test_pipeline_frozen_bundle_resume import drive, refused, setup, snapshot


def _edit_tools():
    from api import mcp_router
    got = {}

    def tool(name, kind, description):
        def deco(fn):
            got[name] = fn
            return fn
        return deco
    mcp_router._register_edit_tools(tool)
    return got


def _meta_agent():
    from core.meta_agent import MetaAgent
    return MetaAgent(MagicMock(), MagicMock(), owner_email="test@local")


def held(host, registry, gen, rid):
    """Everything an in-place edit could have touched: every generated file
    byte (target, descriptor, seals), each live gen_* role, the live graph,
    its SQLite rows and the run's pin/steps/trace, and the whole manifest."""
    from skillflow.core import graph_digest
    with host._lock:
        rows = [dict(r) for r in host._conn.execute(
            "SELECT name, version, yaml_text FROM skillflow_graphs WHERE name LIKE 'gen_%' "
            "ORDER BY name")]
        versions = host._conn.execute(
            "SELECT count(*) FROM skillflow_graph_versions WHERE name='gen_toy'").fetchone()[0]
    roles = {name: (cfg.model, list(cfg.tools), cfg.config)
             for name, cfg in host.agent_registry._configs.items() if name.startswith("gen_")}
    return {"run": snapshot(host, gen, rid),
            "files": {p.name: p.read_bytes() for p in sorted(gen.iterdir()) if p.is_file()},
            "roles": roles, "rows": rows, "versions": versions,
            "live_graph": graph_digest(host._graphs["gen_toy"].to_dict()),
            "manifest": fb.manifest_hints(registry.get("gen_toy")),
            "pinned_role": pinned_run_graph(host, rid)["steps"][0]["agent_config"]}


def _active_role(host):
    return host._graphs["gen_toy"].steps[0].agent_config


EDITS = {
    # The observer's exact call: bare 'maker' resolves to the RETAINED old role.
    "edit_template_retained": lambda host, registry, gen: _edit_tools()["edit_template"](
        config="gen_toy", role="maker", template="EDITED AFTER ADOPTION"),
    # The adopted unit's own bundle-specific role, by full name.
    "edit_template_bundle_role": lambda host, registry, gen: _edit_tools()["edit_template"](
        config="gen_toy", role=_active_role(host), template="EDITED BUNDLE ROLE"),
    "edit_role": lambda host, registry, gen: _edit_tools()["edit_role"](
        config="gen_toy", role="maker", model="pro", temperature=0.9),
    "edit_pipeline": lambda host, registry, gen: _edit_tools()["edit_pipeline"](
        config="gen_toy", graph_yaml=(gen / "gen_toy.yaml").read_text().replace(
            "steps:", "description: edited in place\nsteps:", 1)),
    "config_edit_roles": lambda host, registry, gen: _meta_agent()._tool_config_edit(
        {"config_name": "gen_toy", "file": "roles", "old_str": "OLD MAKER",
         "new_str": "EDITED VIA CONFIG_EDIT"}),
    "config_edit_graph": lambda host, registry, gen: _meta_agent()._tool_config_edit(
        {"config_name": "gen_toy", "old_str": "name: gen_toy",
         "new_str": "name: gen_toy\ndescription: edited in place"}),
    "set_pipeline_capabilities": lambda host, registry, gen: pr.set_pipeline_capabilities(
        host, registry, "gen_toy", remove=["anything"]),
}


@pytest.mark.parametrize("edit", sorted(EDITS))
def test_supported_editors_version_active_content_and_preserve_old_claim(
        tmp_path, monkeypatch, record_property, edit):
    host, registry, gen, rid, _, _, _ = setup(tmp_path, monkeypatch, "healthy")
    pin = host.get_run(rid)["graph_version"]
    before = held(host, registry, gen, rid)
    assert before["pinned_role"] == "gen_toy__maker" != _active_role(host)
    assert before["roles"]["gen_toy__maker"][2]["system_prompt"] == "OLD MAKER"
    result = EDITS[edit](host, registry, gen)
    record_property("edit_result", json.dumps(result, default=str))
    if edit in ("config_edit_roles", "set_pipeline_capabilities"):
        assert result.get("error"), result
        assert held(host, registry, gen, rid) == before
    elif edit == "config_edit_graph":
        # The exact old observer inserts a duplicate YAML description before
        # the existing one. YAML's last key wins: this is a true logical no-op.
        assert "error" not in result and result["changed"] is False, result
        assert held(host, registry, gen, rid) == before
    else:
        assert "error" not in result and result["changed"], result
        after = held(host, registry, gen, rid)
        assert after["versions"] > before["versions"]
        for name, cfg in before["roles"].items():
            assert after["roles"][name] == cfg
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "adopted"
    assert fb.whole_state(host, registry, gen, "gen_toy", desc["new"]) == []
    assert fb.launch_block("gen_toy", gen, sf=host) is None
    # The existing paused run still resumes on its pin with its OLD role.
    advanced, cfg = drive(host, rid)
    assert advanced == "make" and cfg["name"] == "gen_toy__maker"
    assert cfg["config"]["system_prompt"] == "OLD MAKER"
    assert host.get_run(rid)["graph_version"] == pin
    host._conn.close()


def test_import_over_a_recorded_target_and_edits_of_a_seal_are_refused(tmp_path, monkeypatch):
    host, registry, gen, rid, loaded, _, _ = setup(tmp_path, monkeypatch, "healthy")
    before = held(host, registry, gen, rid)
    bundle = pipeline_bundle.export_pipeline("gen_toy")
    with pytest.raises(pipeline_bundle.BundleError, match="recorded whole bundle"):
        pipeline_bundle.import_pipeline(host, registry, bundle)
    # A sealed bundle: config_edit used to leave its edit on disk after the
    # failed reload, breaking the seal for good.
    phys = next(n for n in loaded if fb.is_sealed_name(n) and "before_resume" in n)
    out = _meta_agent()._tool_config_edit(
        {"config_name": phys, "file": "roles", "old_str": "MAKER v1", "new_str": "HACKED"})
    assert "sealed bundle" in out["error"], out
    role = next(iter(json.loads((gen / f"{phys}.roles.json").read_text())))
    out = _edit_tools()["edit_template"](config=phys, role=role, template="HACKED")
    assert "sealed bundle" in out["error"], out
    assert held(host, registry, gen, rid) == before
    fb.verify_sealed(gen, phys)
    _, cfg = drive(host, rid)
    assert cfg["config"]["system_prompt"] == "OLD MAKER"
    host._conn.close()


def test_ordinary_generated_pipeline_stays_editable(tmp_path, monkeypatch):
    """Useful positive: an unrelated ordinary generated config (no descriptor,
    not sealed) is still edited and reloaded live, and a new run uses it."""
    host, registry, gen, rid, loaded, _, _ = setup(tmp_path, monkeypatch, "healthy", extras=True)
    assert "gen_plain" in loaded
    tools = _edit_tools()
    out = tools["edit_template"](config="gen_plain", role="maker", template="PLAIN v2")
    assert out["changed"] and out["chars"] == 8 and out["role"] != "gen_plain__maker", out
    out = tools["edit_role"](config="gen_plain", role="reviewer", temperature=0.5)
    assert out["value"]["temperature"] == 0.5, out
    from core.pipeline_versions import resolve_active
    path = gen / "gen_plain.roles.json"
    old_source = path.read_text()
    roles = json.loads(old_source)
    reviewer = resolve_active("gen_plain", host._graphs["gen_plain"].to_dict(), "reviewer")
    roles[reviewer]["system_prompt"] = "R v2"
    out = _meta_agent()._tool_config_edit(
        {"config_name": "gen_plain", "file": "roles", "old_str": old_source,
         "new_str": json.dumps(roles)})
    assert out.get("reloaded") and "reload_error" not in out, out
    roles = json.loads((gen / "gen_plain.roles.json").read_text())
    maker = resolve_active("gen_plain", host._graphs["gen_plain"].to_dict(), "maker")
    reviewer = resolve_active("gen_plain", host._graphs["gen_plain"].to_dict(), "reviewer")
    assert roles[maker]["system_prompt"] == "PLAIN v2"
    assert roles[reviewer]["system_prompt"] == "R v2"
    assert roles["gen_plain__maker"]["system_prompt"] == "PLAIN"  # historical bytes stay exact
    assert host.agent_registry.get(maker).config["system_prompt"] == "PLAIN v2"
    plain = host.create_run("gen_plain", project_id="plain-gen")
    host.start_run(plain)
    host.pause_run(plain)
    _, cfg = drive(host, plain)
    assert cfg["config"]["system_prompt"] == "PLAIN v2"
    # The recorded target beside it is untouched and still served whole.
    desc = fb.read_descriptor(gen, "gen_toy")
    assert fb.whole_state(host, registry, gen, "gen_toy", desc["new"]) == []
    _, cfg = drive(host, rid)
    assert cfg["config"]["system_prompt"] == "OLD MAKER"
    host._conn.close()


def _tamper_roles(gen):
    """A change made outside the editors (shell, restore, hand edit)."""
    path = gen / "gen_toy.roles.json"
    roles = json.loads(path.read_text())
    roles["gen_toy__maker"]["system_prompt"] = "EDITED OUTSIDE"
    path.write_text(json.dumps(roles))


def test_recorded_target_changed_outside_the_editors_is_mixed_not_ordinary(
        tmp_path, monkeypatch):
    host, registry, gen, rid, _, _, _ = setup(tmp_path, monkeypatch, "healthy")
    pin = host.get_run(rid)["graph_version"]
    desc = fb.read_descriptor(gen, "gen_toy")
    manifest = fb.manifest_hints(registry.get("gen_toy"))
    _tamper_roles(gen)
    # The descriptor keeps identifying the recorded unit.
    unit = fb.recorded_unit(gen, "gen_toy")
    assert fb.unit_identity(unit) == desc["new"]["identity"]
    with pytest.raises(fb.BundleRefused, match="not the whole bundle its adoption recorded"):
        fb.recorded_hints(gen, "gen_toy")
    block = fb.launch_block("gen_toy", gen, sf=host)
    assert block and "files" in block
    before = snapshot(host, gen, rid)
    assert block in refused(host.resume_run, rid)
    assert snapshot(host, gen, rid) == before
    # Reload does not serve the changed files as an ordinary config.
    assert "not the whole bundle" in pr.reload_generated_pipeline(host, registry, "gen_toy")["error"]
    assert host.agent_registry.get("gen_toy__maker").config["system_prompt"] == "OLD MAKER"
    assert fb.manifest_hints(registry.get("gen_toy")) == manifest
    with pytest.raises(SkillFlowError):
        host.create_run("gen_toy", project_id="own-new-launch")
    # Recovery is reconciliation to the recorded whole bundle.
    fb.reconcile_adoption(host, registry, "gen_toy", to="new", config_dir=gen)
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "reconciled_new"
    assert fb.read_unit(gen, "gen_toy")["roles_text"] == desc["new"]["roles_text"]
    advanced, cfg = drive(host, rid)
    assert cfg["config"]["system_prompt"] == "OLD MAKER"
    assert host.get_run(rid)["graph_version"] == pin
    with pytest.raises(fb.BundleRefused, match="no pending"):
        fb.reconcile_adoption(host, registry, "gen_toy", to="new", config_dir=gen)
    host._conn.close()


def test_boot_refuses_a_changed_recorded_target_and_returns_it_to_pending(
        tmp_path, monkeypatch):
    from api import dependencies as deps
    host, registry, gen, rid, _, _, _ = setup(tmp_path, monkeypatch, "healthy")
    desc = fb.read_descriptor(gen, "gen_toy")
    _tamper_roles(gen)
    tampered = (gen / "gen_toy.roles.json").read_bytes()
    loader = host._tool_loader
    host._conn.close()
    boot = AItelierSkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                             workspace_base=str(tmp_path / "ws"),
                             projects_base=str(tmp_path / "projects"))
    boot_registry = ConfigRegistry.build(boot)
    monkeypatch.setattr(deps, "_skillflow_instance", boot)
    monkeypatch.setattr(deps, "_config_registry_instance", boot_registry)
    loaded = pr.load_generated_configs(boot, boot_registry)
    assert "gen_toy" not in loaded
    assert (gen / "gen_toy.roles.json").read_bytes() == tampered       # not migrated
    pending = fb.read_descriptor(gen, "gen_toy")
    assert pending["state"] == "pending" and "files" in pending["boot_unverified"]["mismatches"]
    assert boot.agent_registry.get("gen_toy__maker") is None or (
        boot.agent_registry.get("gen_toy__maker").config["system_prompt"] != "EDITED OUTSIDE")
    assert "unreconciled" in refused(boot.resume_run, rid)
    fb.reconcile_adoption(boot, boot_registry, "gen_toy", to="new", config_dir=gen)
    assert fb.read_unit(gen, "gen_toy")["roles_text"] == desc["new"]["roles_text"]
    _, cfg = drive(boot, rid)
    assert cfg["config"]["system_prompt"] == "OLD MAKER"
    boot._conn.close()


def test_descriptor_without_a_settled_side_fails_closed(tmp_path, monkeypatch):
    host, registry, gen, rid, _, _, _ = setup(tmp_path, monkeypatch, "healthy")
    desc = fb.read_descriptor(gen, "gen_toy")
    fb.write_descriptor(gen, "gen_toy", {**{k: v for k, v in desc.items() if k != "digest"},
                                         "state": "unknown"})
    with pytest.raises(fb.BundleRefused, match="no settled whole bundle"):
        fb.recorded_unit(gen, "gen_toy")
    block = fb.launch_block("gen_toy", gen, sf=host)
    assert block and "no settled whole bundle" in block
    before = snapshot(host, gen, rid)
    assert block in refused(host.resume_run, rid)
    out = _edit_tools()["edit_template"](config="gen_toy", role="maker", template="X")
    assert "recorded whole bundle" in out["error"]
    assert snapshot(host, gen, rid) == before
    assert "no settled whole bundle" in pr.reload_generated_pipeline(
        host, registry, "gen_toy")["error"]
    host._conn.close()
