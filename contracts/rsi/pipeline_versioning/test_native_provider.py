"""The SAME r2 vectors against the standalone fake and the actual native provider.

Native worlds use the accepted component's real host pieces: AItelierSkillFlow
(SQLite), the production ToolLoader factory of ``api.dependencies`` (with the
owned generated-tools dir), ConfigRegistry, ``capability_registry.define`` /
``archive`` for capability definitions, ``reload_generated_pipeline`` for
ordinary registration, and the normal SDK run lifecycle (create/start/pause,
advance/claim/confirm to completion, claims released unexecuted). No product
method is patched; no provider, tool or model body runs.
"""
import hashlib
import inspect
import json
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from skillflow.core import StepResult

import contract as c
import fake
import native_provider
from core import capability_registry
from core import datadir
from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr
from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from core.run_graph_view import pinned_run_graph
from core.skillflow_host import AItelierSkillFlow
from native_provider import NativeVersions, logical_role
from test_contract import literal, load_vectors, run_conformance, vectors_sha256

OUT = Path("/evidence")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _self(value, target):
    if isinstance(value, str):
        return value.replace(c.SELF, target)
    if isinstance(value, list):
        return [_self(v, target) for v in value]
    if isinstance(value, dict):
        return {_self(k, target): _self(v, target) for k, v in value.items()}
    return value


class NativeFixture:
    """Host actions outside the versioning API, through actual host code."""

    def __init__(self, root: Path):
        from api import dependencies as deps
        self.root = root
        self.mp = pytest.MonkeyPatch()
        self.mp.setenv("AITELIER_HOME", str(root / "home"))
        self.gen = root / "gen"
        self.gen.mkdir()
        self.mp.setenv("AITELIER_GENERATED_CONFIGS_DIR", str(self.gen))
        self.mp.setattr(deps, "db_instance", DBManager(str(root / "host.db")))
        self.mp.setattr(deps, "_tool_loader_instance", None)
        self.loader = deps.get_tool_loader()
        self.tools = datadir.tools_dir()
        self.sf = AItelierSkillFlow(str(root / "sf.db"), tool_loader=self.loader,
                                    workspace_base=str(root / "ws"),
                                    projects_base=str(root / "projects"))
        self.registry = ConfigRegistry()
        self.mp.setattr(deps, "_skillflow_instance", self.sf)
        self.mp.setattr(deps, "_config_registry_instance", self.registry)
        self.runs = {}

    def install_tool(self, name, files):
        tdir = self.tools / name
        shutil.rmtree(tdir, ignore_errors=True)
        tdir.mkdir(parents=True)
        for fname, text in files.items():
            (tdir / fname).write_bytes(text.encode("utf-8"))

    def remove_tool(self, name):
        shutil.rmtree(self.tools / name)

    def define_capability(self, name, tools, briefing):
        """The host's one capability write path (RegistrationTransaction)."""
        out = capability_registry.define(self.sf, name, tools=tools, briefing=briefing,
                                         owner="owned-fixture")
        assert out.get("ok"), out

    def archive_capability(self, name):
        out = capability_registry.archive(self.sf, name)
        assert out.get("ok"), out

    def register(self, target, unit, content):
        """Ordinary generated-config files + the normal reload path. The
        vector's resolved ``content`` is NOT used: capture must produce it."""
        graph, roles = _self(unit["graph"], target), _self(unit["roles"], target)
        (self.gen / f"{target}.yaml").write_text(
            yaml.safe_dump(graph, sort_keys=False, allow_unicode=True), encoding="utf-8")
        (self.gen / f"{target}.roles.json").write_text(
            json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")
        out = pr.reload_generated_pipeline(self.sf, self.registry, target)
        assert "error" not in out, out

    def start_run(self, run, target):
        project = "fx-" + run.lower().replace("_", "-")
        path = self.sf._workspace.get_config_path(project, target) / "task.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("owned fixture task", encoding="utf-8")
        rid = self.sf.create_run(target, project_id=project)
        self.sf.start_run(rid)
        self.runs[run] = rid
        return self.sf.get_run(rid)["graph_version"]

    def pause_run(self, run):
        self.sf.pause_run(self.runs[run])

    def complete_run(self, run):
        rid = self.runs[run]
        for _ in range(6):
            self.sf.advance_run(rid)
            claim = self.sf.claim_next_step(rid)
            if claim:
                self.sf.confirm_step(claim.token, StepResult(
                    outputs={"result": "owned fixture completion"}))
            if self.sf.get_run(rid)["status"] == "completed":
                return
        raise AssertionError(f"run {run} did not complete: {self.sf.get_run(rid)}")

    def observe_runs(self):
        out = {}
        for key, rid in self.runs.items():
            run = self.sf.get_run(rid)
            prompts = {}
            for step in pinned_run_graph(self.sf, rid)["steps"]:
                if step["agent_config"]:
                    role = self.sf.agent_registry.get(step["agent_config"])
                    prompts[logical_role(run["graph_name"], step["agent_config"])] = (
                        role.config.get("system_prompt") if role else None)
            out[key] = {"status": run["status"], "graph_version": run["graph_version"],
                        "prompts": prompts}
        return out

    def claim_prompt(self, run):
        rid = self.runs[run]
        if self.sf.get_run(rid)["status"] == "paused":
            self.sf.resume_run(rid)
        self.sf.advance_run(rid)
        claim = self.sf.claim_next_step(rid)
        assert claim is not None, self.sf.get_run(rid)
        prompt = claim.inputs["_agent_config"]["config"]["system_prompt"]
        self.sf.release_claim(claim.token, "versioning conformance observation, unexecuted")
        return {"step": claim.step_id, "prompt": prompt}

    def tamper_seal(self, version_id):
        prefix = c.SEAL_PREFIX + c.seal_slug(version_id) + "_"
        hit = sorted(self.gen.glob(prefix + "*.roles.json"))
        assert hit, version_id
        for path in hit:                  # an on-disk corruption of sealed bytes
            path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    def _state(self):
        files = {str(p.relative_to(self.root)): _sha(p.read_bytes())
                 for base in (self.gen, self.tools) for p in sorted(base.rglob("*"))
                 if p.is_file()}
        graphs = {n: [[r["version"], r["digest"]] for r in self.sf.list_graph_versions(n)]
                  for n in sorted(self.sf._graphs)}
        runs = {r["id"]: [r["status"], r["graph_version"], r["graph_digest"]]
                for r in self.sf.list_runs()}
        roles = {n: repr(self.sf.agent_registry.get(n).to_dict())
                 for n in sorted(self.sf.agent_registry.list_names())}
        manifests = {n: repr(fb.manifest_hints(self.registry.get(n)))
                     for n in sorted(self.sf._graphs) if self.registry.get(n) is not None}
        capabilities = {n: fb._capability_identity(self.sf, n)
                        for n in sorted(self.sf._capabilities)}
        return {"files": files, "graphs": graphs, "runs": runs, "roles": roles,
                "manifests": manifests, "capabilities": capabilities}

    def snapshot(self):
        return json.dumps(self._state(), sort_keys=True)

    def snapshot_outside(self, target, since=None):
        """Everything except the target's own files, graph and manifest, and
        the target roles absent from *since* (new bundle-specific roles).
        Runs (status and pins), seals, tools, other targets and every role
        that existed before, the target's retained old roles included, stay in."""
        state = self._state()
        own = {f"gen/{target}{s}" for s in (".yaml", ".roles.json", ".adoption.json")}
        state["files"] = {k: v for k, v in state["files"].items() if k not in own}
        state["graphs"].pop(target, None)
        state["manifests"].pop(target, None)
        if since is not None:
            existed = json.loads(since)["roles"]
            state["roles"] = {k: v for k, v in state["roles"].items()
                              if k in existed or not k.startswith(target + pr._ROLE_SEP)}
        return json.dumps(state, sort_keys=True)

    def close(self):
        self.sf._conn.close()
        self.mp.undo()


class _Worlds:
    def __init__(self, base: Path):
        self.base, self.made = base, []

    def __call__(self):
        root = Path(tempfile.mkdtemp(prefix="world-", dir=self.base))
        fixture = NativeFixture(root)
        api = NativeVersions(fixture.sf, fixture.registry, fixture.gen)
        self.made.append((api, fixture))
        return api, fixture


def _write(name, data):
    if OUT.is_dir():
        (OUT / name).write_text(json.dumps(data, indent=1, default=repr), encoding="utf-8")


def test_same_vectors_pass_on_fake_and_native(tmp_path):
    fake_report, native_report = {}, {}
    worlds = _Worlds(tmp_path)
    fake_fails = run_conformance(fake.make_fake_world, fake_report)
    native_fails = run_conformance(worlds, native_report)
    vectors = load_vectors()
    _write("shared-vectors.json", {
        "vectors_sha256": vectors_sha256(),
        "scenario_count": len(vectors["scenarios"]),
        "step_count": sum(len(s["steps"]) for s in vectors["scenarios"]),
        "skipped_scenarios": [],
        "fake_failures": fake_fails, "native_failures": native_fails,
        "fake_per_scenario": fake_report["scenarios"],
        "native_per_scenario": native_report["scenarios"],
        "native_worlds": len(worlds.made)})
    _write("native-transcript.json", native_report["transcripts"])
    _write("fake-transcript.json", fake_report["transcripts"])
    assert fake_fails == []
    assert native_fails == []


def test_native_binding_is_thin_and_its_identities_are_distinct(tmp_path):
    """Useful native capture/list/read/restore observed directly, plus
    structural checks that the binding keeps no ledger of its own."""
    v = load_vectors()
    api, fx = _Worlds(tmp_path)()
    try:
        tool = v["tools"]["fx_probe@1"]
        fx.install_tool(tool["name"], tool["files"])
        fx.register("gen_vt", literal(v["units"]["U_A"]), None)
        fx.register("gen_vsrc", literal(v["units"]["U_A_prompt"]), None)
        one, two = api.capture("gen_vt"), api.capture("gen_vsrc")
        rid = fx.start_run("R_old", "gen_vt")
        before = api.current("gen_vt")
        seals_before = sorted(p.name for p in fx.gen.glob("gen_rsi_*"))
        restored = api.restore("gen_vt", two["version_id"],
                               expected_current=before["unit_identity"])
        after = api.current("gen_vt")
        again = api.capture("gen_vt")
        read = api.read(two["version_id"])
        desc = fb.read_descriptor(fx.gen, "gen_vt")
        evidence = {
            "captures": [one, two], "restore": restored, "capture_after_restore": again,
            "current_before": before, "current_after": after,
            "seal_files_before_restore": seals_before,
            "seal_files_after": sorted(p.name for p in fx.gen.glob("gen_rsi_*")),
            "read_native": read["native"], "logical_id": read["version_id"],
            "descriptor_state": desc["state"], "descriptor_sealed": desc["sealed"],
            "whole_state": fb.whole_state(fx.sf, fx.registry, fx.gen, "gen_vt",
                                          fb.recorded_unit(fx.gen, "gen_vt")),
            "launch_block": fb.launch_block("gen_vt", fx.gen, sf=fx.sf),
            "runs": fx.observe_runs(),
            "binding_state": sorted(vars(api)),
            "binding_public_api": sorted(n for n in dir(NativeVersions) if not n.startswith("_")),
            "native_co_filenames": {f.__name__: f.__code__.co_filename for f in (
                fb.capture, fb.compile_bundle, fb.seal, fb.verify_sealed, fb.adopted_unit,
                fb.verify_providers, fb.adopt_bundle)},
            "binding_co_filename": NativeVersions.restore.__code__.co_filename,
        }
        _write("native-evidence.json", evidence)
        assert one["version_id"] == v["ids"]["C_A"] and two["version_id"] == v["ids"]["C_A_prompt"]
        assert again == {"target": "gen_vt", "version_id": two["version_id"], "created": False,
                         "physical_name": two["physical_name"]}
        assert evidence["seal_files_after"] == seals_before            # restore seals nothing
        assert read["native"]["bundle_identity"] != read["version_id"][len("sha256:"):]
        assert restored["unit_identity"] == after["unit_identity"] != before["unit_identity"]
        assert after["graph_version"] > before["graph_version"]       # SDK integer is not the id
        assert desc["state"] == "adopted" and desc["sealed"] == two["physical_name"]
        assert evidence["whole_state"] == [] and evidence["launch_block"] is None
        assert fx.sf.get_run(fx.runs["R_old"])["graph_version"] == rid
        assert evidence["binding_state"] == ["_dir", "_registry", "_sf"]
        assert evidence["binding_public_api"] == sorted(
            ["canonical_bytes", "version_id_of", "capture", "list_versions", "read",
             "current", "restore"])
        src = inspect.getsource(native_provider)
        for forbidden in ("create_run", "start_run", "pause_run", "confirm_step",
                          "sqlite", "INSERT", "UPDATE "):
            assert forbidden not in src, forbidden
    finally:
        fx.close()
