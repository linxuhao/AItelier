"""Phase2: unchanged shared vectors + genuine accepted provider controls."""
import copy
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from skillflow import PipelineGraph
from skillflow.core import StepResult
from core import pipeline_frozen_bundle as fb
from real_provider_probe import RealProviderProbe, ProviderBoundary
from test_contract import run_conformance, load_vectors
from fake import FakePipelineVersionStore
from test_pipeline_frozen_bundle_resume import setup, CONTEXT

OUT = Path("/evidence")


def test_same_full_vectors_fake_and_real(tmp_path):
    made = []
    def factory(tools):
        probe = RealProviderProbe(tmp_path / str(len(made)), tools)
        made.append(probe)
        return probe
    try:
        fake = run_conformance(FakePipelineVersionStore)
        real = run_conformance(factory)
        (OUT / "shared-vectors.json").write_text(json.dumps({
            "fake_failures": fake, "real_failures": real,
            "real_optional_capabilities_absent": ["tamper_hook"],
            "vectors_sha256": hashlib.sha256(Path(__file__).with_name("conformance_vectors.json").read_bytes()).hexdigest(),
            "actual_real_factories": len(made)}, indent=2))
        assert fake == []
        assert real == [], "unchanged literal shared vectors do not bind to actual registered provider: " + str(real[:3])
    finally:
        for probe in made:
            probe.close()


def test_minimal_native_parser_counterexample_preserves_actual_state(tmp_path):
    p = RealProviderProbe(tmp_path, load_vectors()["default_tools"])
    literal = copy.deepcopy(load_vectors()["bundles"]["A"])
    before = {"runs": p.sf.list_runs(), "graphs": p.sf.list_graphs(),
              "seals": p.list_versions(), "current": p.current_version()}
    try:
        with pytest.raises(ProviderBoundary) as exc:
            p.capture(literal)
        cause = exc.value.__cause__
        assert isinstance(cause, KeyError) and cause.args == ("name",)
        after = {"runs": p.sf.list_runs(), "graphs": p.sf.list_graphs(),
                 "seals": p.list_versions(), "current": p.current_version()}
        assert after == before
        (OUT / "minimal-counterexample.json").write_text(json.dumps({
            "literal_bundle_A": literal, "fake_capture": FakePipelineVersionStore(load_vectors()["default_tools"]).capture(literal),
            "actual_call": "PipelineGraph._from_dict(literal_A.graph)",
            "actual_exception": {"type": type(cause).__name__, "args": cause.args},
            "probe_code": exc.value.code, "before": before, "after": after,
            "provider_parser_co_filename": PipelineGraph._from_dict.__func__.__code__.co_filename}, indent=2))
    finally:
        p.close()


def test_native_capture_seal_catalog_restore_and_real_pins(tmp_path, monkeypatch):
    sf, registry, gen, old_run, loaded, rec, _ = setup(tmp_path, monkeypatch, "healthy")
    try:
        old_pin = sf.get_run(old_run)["graph_version"]
        b = fb.capture(sf, registry, "gen_toy", configs_dirs=[gen])
        one = fb.seal(b, "phase2_old", gen)
        count = len(list(gen.glob("*.bundle.json")))
        assert fb.seal(b, "phase2_old", gen) == one
        assert len(list(gen.glob("*.bundle.json"))) == count
        read = fb.verify_sealed(gen, one["physical_name"])
        assert read == one
        prompt_key = next(k for k in b["prompts"] if "maker" in k)
        changed = fb.derive_candidate(b, {"prompts": {prompt_key: b["prompts"][prompt_key] + " PHASE2"}})
        two = fb.seal(changed, "phase2_new", gen)
        history = {f.name: f.read_bytes() for f in gen.glob("*.bundle.json")}
        selected = []
        for sealed in [two, one]:
            expected = fb.unit_identity(fb.read_unit(gen, "gen_toy"))
            selected.append(fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                                           expected_current=expected, config_dir=gen))
            assert sf.get_run(old_run)["graph_version"] == old_pin
        assert history == {f.name: f.read_bytes() for f in gen.glob("*.bundle.json")}
        before = fb.unit_identity(fb.read_unit(gen, "gen_toy"))
        with pytest.raises(fb.BundleRefused):
            fb.adopt_bundle(sf, registry, "gen_toy", "gen_rsi_missing_" + "0" * 16,
                            expected_current=before, config_dir=gen)
        assert before == fb.unit_identity(fb.read_unit(gen, "gen_toy"))
        sf.resume_run(old_run)
        sf.advance_run(old_run)
        claim = sf.claim_next_step(old_run)
        assert claim.inputs["_agent_config"]["config"]["system_prompt"] == "OLD MAKER"
        sf.release_claim(claim.token, "phase2 useful positive, unexecuted")
        # A real SDK-created and genuinely completed fixture run: no forged run
        # id, pin dictionary, owner SQL or provider/model body.
        project = "phase2-finished"
        for cfg, file, text in CONTEXT:
            path = sf._workspace.get_config_path(project, cfg) / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        rid = sf.create_run("gen_toy", project_id=project)
        sf.start_run(rid)
        pin = sf.get_run(rid)["graph_version"]
        for _ in range(4):
            sf.advance_run(rid)
            c = sf.claim_next_step(rid)
            if c:
                sf.confirm_step(c.token, StepResult(outputs={"result": "owned fixture completion"}))
            if sf.get_run(rid)["status"] == "completed":
                break
        assert sf.get_run(rid)["status"] == "completed"
        expected = fb.unit_identity(fb.read_unit(gen, "gen_toy"))
        fb.adopt_bundle(sf, registry, "gen_toy", two["physical_name"], expected_current=expected, config_dir=gen)
        assert sf.get_run(rid)["graph_version"] == pin and sf.get_run(rid)["status"] == "completed"
        (OUT / "native-positive.json").write_text(json.dumps({
            "logical_bundle_identity": b["identity"], "catalog": sorted(history),
            "native_current_identity_before_restore": before, "actual_restores": selected,
            "old_run": old_run, "old_pin": old_pin, "old_claim_prompt": "OLD MAKER",
            "real_finished_run": sf.get_run(rid), "normal_isolation": rec}, indent=2))
    finally:
        sf._conn.close()


def test_native_missing_provider_refuses_restore_before_unit_effects(tmp_path, monkeypatch):
    from test_pipeline_frozen_bundle import _target
    from test_pipeline_frozen_bundle_served import _with_context_tool
    sf, registry, gen, _, rid = _target(tmp_path, monkeypatch)
    try:
        tools, tdir, capture = _with_context_tool(tmp_path, sf, registry, "phase2_owned_tool")
        b = capture()
        sealed = fb.seal(b, "phase2_tools", gen)
        def snapshot():
            return {"target": fb.read_unit(gen, "gen_toy"),
                    "catalog": {f.name: f.read_text() for f in gen.glob("*.bundle.json")},
                    "versions": sf.list_graph_versions("gen_toy"), "run": sf.get_run(rid)}
        before = snapshot()
        (tdir / "impl.py").unlink()  # actual owned provider disappeared
        with pytest.raises(fb.BundleRefused) as exc:
            fb.adopt_bundle(sf, registry, "gen_toy", sealed["physical_name"],
                            expected_current=fb.unit_identity(before["target"]), config_dir=gen)
        assert snapshot() == before
        (OUT / "missing-provider.json").write_text(json.dumps({
            "provider": str(tdir), "native_refusal": str(exc.value),
            "before": before, "after": snapshot()}, indent=2))
    finally:
        sf._conn.close()
