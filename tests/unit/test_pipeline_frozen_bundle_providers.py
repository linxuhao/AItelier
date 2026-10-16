"""Frozen bundle: actual provider resolution, maker identity and whole-state
adoption settlement.

Real SkillFlow/AItelierSkillFlow, ToolLoader, capability registry, SQLite,
ConfigRegistry, RegistrationTransaction and boot scan. The only injected faults
are a ConfigRegistry subclass that fails after the graph is published and a
forwarding connection that fails the rollback's ``BEGIN IMMEDIATE`` once.
"""
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from skillflow import PipelineGraph, SkillFlow
from skillflow.exceptions import SkillFlowError

from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry

from test_pipeline_frozen_bundle import ROOT, TOY, _capture, _runtime, _target


def probe_context_provider(config_name):
    return {}


def _reregister(sf, cfgs, mutate, name="toy"):
    doc = yaml.safe_load((cfgs / f"{name}.yaml").read_text(encoding="utf-8"))
    mutate(doc)
    (cfgs / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    sf.register_graph(PipelineGraph.from_yaml(cfgs / f"{name}.yaml"))


def _generated_tool(tools, name, marker, impl=True):
    tdir = tools / name
    tdir.mkdir(parents=True)
    (tdir / "tool.yaml").write_text(
        f"name: {name}\ndescription: probe\nparameters: {{}}\n", encoding="utf-8")
    if impl:
        # Importing this module would leave the marker: capture/seal/register
        # must hash it, never import or call it.
        (tdir / "impl.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
            f"def {name}(**kw):\n    return {{'content': 'x'}}\n", encoding="utf-8")
    return tdir


def test_context_tool_identity_comes_from_the_actual_tool_loader(tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    tools, marker = tmp_path / "gen-tools", tmp_path / "imported.marker"
    probe = _generated_tool(tools, "probe_ctx", marker)
    sf._tool_loader.add_tools_dir(tools)
    _reregister(sf, cfgs, lambda d: d["steps"][0]["context"].extend(
        [{"source": {"tool": "probe_ctx"}}, {"source": {"tool": "dir_tree"}}]))

    def cap():
        return fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs],
                          tools_dir=tools)

    bundle = cap()
    ident = bundle["tools"]["probe_ctx"]
    assert ident["files"]["impl.py"] == fb.sha((probe / "impl.py").read_bytes())
    assert bundle["locations"]["tools"]["probe_ctx"]["kind"] == "generated"
    assert bundle["tools"]["dir_tree"]["native"] is True
    assert set(bundle["tools"]["dir_tree"]["files"]) >= {"tool.yaml", "impl.py"}
    assert "null" not in json.dumps(bundle["tools"])          # no null identity
    assert not marker.exists()

    original = (probe / "impl.py").read_text(encoding="utf-8")
    (probe / "impl.py").write_text(original + "# changed\n", encoding="utf-8")
    with pytest.raises(fb.BundleRefused, match="probe_ctx.*changed"):
        fb.seal(bundle, "arm", gen)
    (probe / "impl.py").write_text(original, encoding="utf-8")
    sealed = fb.seal(bundle, "arm", gen)
    (probe / "impl.py").write_text(original + "# changed again\n", encoding="utf-8")
    with pytest.raises(fb.BundleRefused, match="probe_ctx.*changed"):
        fb.register_sealed(sf, registry, sealed["physical_name"], gen)
    assert sealed["physical_name"] not in sf._graphs
    assert not marker.exists()

    (probe / "impl.py").unlink()                               # schema without impl
    with pytest.raises(fb.BundleRefused, match="unresolved tool 'probe_ctx'"):
        cap()
    # A tools_dir the runtime loader does not serve is not a provider.
    _generated_tool(tmp_path / "unserved", "unserved_ctx", marker)
    (probe / "impl.py").write_text(original, encoding="utf-8")
    _reregister(sf, cfgs, lambda d: d["steps"][0]["context"].append(
        {"source": {"tool": "unserved_ctx"}}))
    with pytest.raises(fb.BundleRefused, match="unresolved tool 'unserved_ctx'"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs],
                   tools_dir=tmp_path / "unserved")
    assert not marker.exists()


def test_capability_dependency_resolves_through_registry_or_refuses(tmp_path, monkeypatch):
    sf, registry, gen = _runtime(tmp_path, monkeypatch)
    _, cfgs, tpl = _capture(tmp_path, sf, registry)
    _reregister(sf, cfgs, lambda d: d["steps"][0].update(capability="probe_cap"))
    with pytest.raises(fb.BundleRefused, match="unresolved capability 'probe_cap'"):
        fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])
    sf.register_capability("probe_cap", tools=["read_file"], briefing="B1",
                           context_provider=probe_context_provider, owner="test")
    bundle = fb.capture(sf, registry, "toy", template_base=tpl, configs_dirs=[cfgs])
    cap = bundle["capabilities"]["probe_cap"]
    assert cap["tools"] == ["read_file"] and cap["context_provider"]["sha256"]
    assert cap["context_provider"]["qualname"] == "probe_context_provider"
    sealed = fb.seal(bundle, "arm", gen)
    sf.register_capability("probe_cap", tools=["read_file"], briefing="B2",
                           context_provider=probe_context_provider, owner="test")
    with pytest.raises(fb.BundleRefused, match="capability 'probe_cap' changed"):
        fb.register_sealed(sf, registry, sealed["physical_name"], gen)
    assert sealed["physical_name"] not in sf._graphs


STRUCTURE = """
name: shape
description: maker identity from structure
begin: aggregate
end_conditions:
  combinator: or
  conditions:
    - {type: node_reached, node: final_gate, result: completed}
steps:
  - id: aggregate
    agent_config: aggregator
    output: {mode: content, fixed: {summary: summary.md}}
    transitions: [{to: polish}]
  - id: polish
    agent_config: polisher
    output: {mode: content, fixed: {text: text.md}}
    transitions:
      - {to: aggregate, match: {redo: true}, max_loop: 3}
      - {to: final_gate}
  - id: final_gate
    agent_config: finisher
    output: {mode: content, fixed: {result: result.md}}
    transitions: [{to: null}]
"""


def test_maker_identity_comes_from_graph_structure_not_substrings(tmp_path, monkeypatch):
    sf, registry, _ = _runtime(tmp_path, monkeypatch)
    cfgs, tpl = tmp_path / "cfgs", tmp_path / "templates"
    cfgs.mkdir()
    tpl.mkdir()
    (tpl / "aggregate.md").write_text("AGGREGATE", encoding="utf-8")
    (cfgs / "shape.yaml").write_text(STRUCTURE, encoding="utf-8")
    sf.register_agent_config_from_dict("aggregator", {
        "model": "flash", "tools": ["read_file"], "template": "aggregate.md"})
    sf.register_agent_config_from_dict("polisher", {
        "model": "flash", "tools": ["read_file"], "system_prompt": "POLISH"})
    sf.register_agent_config_from_dict("finisher", {
        "model": "flash", "tools": ["read_file"], "system_prompt": "FINISH"})
    sf.register_graph(PipelineGraph.from_yaml(cfgs / "shape.yaml"))
    bundle = fb.capture(sf, registry, "shape", template_base=tpl, configs_dirs=[cfgs])
    # "aggregate" contains the letters g-a-t-e; it is still the maker.
    cand = fb.derive_candidate(bundle, {"prompts": {"template:aggregate.md": "AGGREGATE v2"}})
    assert cand["prompts"]["template:aggregate.md"] == "AGGREGATE v2"
    with pytest.raises(fb.BundleRefused, match="routes on its own output"):
        fb.derive_candidate(bundle, {"prompts": {"inline:polisher": "lenient"}})
    with pytest.raises(fb.BundleRefused, match="sends work back to 'aggregate'"):
        fb.derive_candidate(bundle, {"prompts": {"inline:polisher": "lenient"}})
    with pytest.raises(fb.BundleRefused, match="evaluating name"):
        fb.derive_candidate(bundle, {"prompts": {"inline:finisher": "lenient"}})


def test_shipped_reviewers_stay_protected_and_investigator_is_a_maker():
    roles = {}
    for f in sorted((ROOT / "agent_configs").glob("*.yaml")):
        roles.update(yaml.safe_load(f.read_text(encoding="utf-8")) or {})
    seen = 0
    for f in sorted((ROOT / "configs").glob("*.yaml")):
        graph = yaml.safe_load(f.read_text(encoding="utf-8"))
        evidence = fb._checker_evidence(graph, roles)
        for step in fb._steps(graph):
            if str(step.get("id", "")).endswith("_review") and step.get("agent_config"):
                assert evidence[step["agent_config"]], (f.name, step["id"])
                seen += 1
        if graph["name"] == "investigate":
            assert evidence == {"investigator": []}
    assert seen >= 5


class _FailAtRollbackBegin:
    """Forwards every call to the real sqlite3 connection; fails the next
    ``BEGIN IMMEDIATE`` once armed."""

    def __init__(self, real):
        self.real, self.armed, self.injected = real, False, 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args, **kw):
        if self.armed and sql == "BEGIN IMMEDIATE":
            self.armed = False
            self.injected += 1
            raise sqlite3.OperationalError("injected rollback BEGIN failure")
        return self.real.execute(sql, *args, **kw)


def _row(conn):
    return dict(conn.execute("SELECT name, yaml_text, version FROM skillflow_graphs "
                             "WHERE name='gen_toy'").fetchone())


def test_failed_rollback_stays_pending_until_whole_state_reconciliation(tmp_path, monkeypatch):
    from core import registration_transaction as rt
    from core.skillflow_host import AItelierSkillFlow
    sf, registry, gen, sealed, rid = _target(tmp_path, monkeypatch)
    old = fb.read_unit(gen, "gen_toy")
    before = _row(sf._conn)
    real = sf._conn
    conn = _FailAtRollbackBegin(real)

    class ArmAtRollback(rt.RegistrationTransaction):
        def rollback(self):
            conn.armed = True
            return super().rollback()

    class FailingRegistry(ConfigRegistry):
        def register_one(self, *a, **k):
            raise RuntimeError("injected manifest failure after graph write")

    failing = FailingRegistry()
    failing._manifests = registry._manifests
    sf._conn = conn
    try:
        with monkeypatch.context() as mp:
            mp.setattr(rt, "RegistrationTransaction", ArmAtRollback)
            with pytest.raises(sqlite3.OperationalError, match="injected rollback BEGIN"):
                fb.adopt_bundle(sf, failing, "gen_toy", sealed["physical_name"],
                                expected_current=fb.unit_identity(old), config_dir=gen)
    finally:
        sf._conn = real
    assert conn.injected == 1
    after = _row(real)
    assert after != before and fb.read_unit(gen, "gen_toy") == old   # mixed
    desc = fb.read_descriptor(gen, "gen_toy")
    assert desc["state"] == "pending"
    assert "persisted graph row" in desc["rollback_unverified"]["mismatches"]
    assert "injected rollback BEGIN" in desc["rollback_unverified"]["error"]
    assert fb.whole_state(sf, registry, gen, "gen_toy", old) == ["persisted graph row"]
    assert "unreconciled" in fb.launch_block("gen_toy", gen)
    with pytest.raises(fb.BundleRefused, match="unreconciled"):
        fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                        expected_current=fb.unit_identity(old), config_dir=gen)

    # A restart: boot refuses the target, and the persisted row a resolver
    # would fall back to is the NEW graph; generic run creation refuses it.
    host = AItelierSkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "ws"),
                             projects_base=str(tmp_path / "proj"))
    assert "gen_toy" not in pr.load_generated_configs(host, ConfigRegistry())
    assert "rsi" in host._get_resolver("gen_toy").graph.steps[0].agent_config
    count = host._conn.execute("SELECT count(*) FROM skillflow_runs").fetchone()[0]
    with pytest.raises(SkillFlowError, match="unreconciled"):
        host.get_or_create_run("gen_toy", "after-restart", {})
    assert host._conn.execute("SELECT count(*) FROM skillflow_runs").fetchone()[0] == count
    host._conn.close()

    # A reconciliation that cannot verify the whole state keeps it pending.
    with pytest.raises(RuntimeError, match="injected manifest failure"):
        fb.reconcile_adoption(sf, failing, "gen_toy", to="old", config_dir=gen)
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "pending"

    out = fb.reconcile_adoption(sf, registry, "gen_toy", to="old", config_dir=gen)
    assert out["identity"] == fb.unit_identity(old)
    assert fb.whole_state(sf, registry, gen, "gen_toy", old) == []
    restored = _row(real)
    assert restored["yaml_text"] == before["yaml_text"] and restored["version"] > after["version"]
    assert fb.read_descriptor(gen, "gen_toy")["state"] == "reconciled_old"
    assert fb.launch_block("gen_toy", gen) is None
    assert sf.get_run(rid)["status"] == "paused"


def test_adoption_refuses_a_target_whose_persisted_row_is_mixed(tmp_path, monkeypatch):
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    old = fb.read_unit(gen, "gen_toy")
    with sf._lock:
        sf._conn.execute("UPDATE skillflow_graphs SET yaml_text='{}' WHERE name='gen_toy'")
        sf._conn.commit()
    with pytest.raises(fb.BundleRefused, match="registered mixed .*persisted graph row"):
        fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                        expected_current=fb.unit_identity(old), config_dir=gen)
    assert fb.read_descriptor(gen, "gen_toy") is None and fb.read_unit(gen, "gen_toy") == old


def test_generic_run_creation_refuses_a_pending_target(tmp_path, monkeypatch):
    from core.skillflow_host import AItelierSkillFlow
    sf, registry, gen, sealed, _ = _target(tmp_path, monkeypatch)
    host = AItelierSkillFlow(str(tmp_path / "host.db"), tool_loader=sf._tool_loader,
                             workspace_base=str(tmp_path / "host-ws"),
                             projects_base=str(tmp_path / "host-proj"))
    host_registry = ConfigRegistry()
    assert "gen_toy" in pr.load_generated_configs(host, host_registry)
    host.create_run("gen_toy", project_id="before")                    # positive control
    old = fb.read_unit(gen, "gen_toy")
    new = fb.adopted_unit(gen, "gen_toy", sealed["physical_name"])
    fb.write_descriptor(gen, "gen_toy", {
        "schema": fb.SCHEMA, "target": "gen_toy", "sealed": sealed["physical_name"],
        "state": "pending", "expected_current": fb.unit_identity(old),
        "old": {"identity": fb.unit_identity(old), **old},
        "new": {"identity": fb.unit_identity(new), "graph_yaml": new["graph_yaml"],
                "roles_text": new["roles_text"], "hints": new["hints"]}})
    count = host._conn.execute("SELECT count(*) FROM skillflow_runs").fetchone()[0]
    with pytest.raises(SkillFlowError, match="unreconciled"):
        host.create_run("gen_toy", project_id="during")
    with pytest.raises(SkillFlowError, match="unreconciled"):
        host.get_or_create_run("gen_toy", "during-2", {})
    assert host._conn.execute("SELECT count(*) FROM skillflow_runs").fetchone()[0] == count
    fb.reconcile_adoption(host, host_registry, "gen_toy", to="old", config_dir=gen)
    assert host.create_run("gen_toy", project_id="after")
