"""PROOF: the SAME literal round scenarios on the genuine host binding.

Runs only in the authorized isolated SDK1.5.94 container. Every scenario uses a
fresh native world (``native_fixture``): the round is an ordinary engine run of
``round.graph.json`` whose tool steps are the NEW ports; every measured cell is
the accepted NativeEvaluator (ordinary launch/claim/runner, real gateway sends
admitted by the real transport boundary, deterministic transport FIXTURE as the
provider, trusted checker, BenchmarkStore rows). Evidence: $OPTIMIZER_EVIDENCE.
"""
import hashlib
import importlib
import importlib.metadata
import json
import os
import traceback
from pathlib import Path

import pytest

from .conformance import LITERAL, ROLES, SCENARIOS, edit_fixture, run_scenario
from .contract import OptimizerError
from .native_fixture import PROFILES, REFERENCE, NativeRoundFactory


def evidence(name, value):
    target = os.getenv("OPTIMIZER_EVIDENCE")
    if target:
        Path(target, name + ".json").write_text(json.dumps(value, indent=1, default=repr) + "\n")


@pytest.fixture
def factory(tmp_path_factory):
    base = Path(os.getenv("RSI_WORK") or tmp_path_factory.mktemp("worlds"))
    made = NativeRoundFactory(base)
    yield made
    made.close()


@pytest.mark.parametrize("vector", SCENARIOS, ids=lambda v: v["name"])
def test_same_shared_round_scenarios_native(vector, factory):
    result, error = None, None
    # Predeclared synthetic profile (fixtures/native_profiles.json), fixed before
    # the world, its benchmark, prepare, freeze or any send exist.
    factory.profile = PROFILES["scenario_profiles"].get(vector["name"], "ordinary")
    try:
        result = run_scenario(factory, vector)
    except BaseException as exc:                                    # noqa: BLE001
        error = traceback.format_exc()
        raise
    finally:
        native = factory.made[0].native_evidence() if factory.made else None
        evidence("native-round-" + vector["name"], {"vector": vector, "result": result,
                                                    "error": error, "native": native})
    sdk, view = native["sdk"], result["observations"]["view"]
    # Every successful invocation is exactly one engine-dispatched, completed
    # tool step of this round's run, in the same order.
    done = sorted((s for s in sdk.get("steps") or () if s["status"] == "completed"),
                  key=lambda s: s["completion_seq"])
    assert [s["step_id"] for s in done] == result["ports"]
    approved = [e["checkpoint_approved"] for e in result["observations"]["events"]
                if "checkpoint_approved" in e]
    assert [a["step_id"] for a in sdk.get("checkpoint_approvals") or ()] == approved
    # Physical accounting: every provider-side request was admitted by the
    # evaluator's transport boundary and is in the round's known/unknown totals.
    assert native["trace_physical_sends"] == native["provider_sends"]
    assert view["known"]["requests"] + view["retained_unknown"]["requests"] == native["provider_sends"]
    if vector["name"] == "cross_call_round_budget_is_residual":
        assert native["provider_sends_by_split_arm"] == {"train/reference": 36, "train/candidate": 4}
    if vector["name"] == "original_deadline_does_not_restart":
        assert native["provider_sends_by_split_arm"] == {"train/reference": 36}
    if not result["ports"]:
        assert native["provider_sends"] == native["evaluator_cell_launches"] == 0
    # Per-selection policy is derived from exact sealed selections under the
    # grant frozen at freeze; every other runtime input stays the frozen one.
    for d in native["policy_derivations"]:
        assert d["frozen_runtime_without_policy"] == d["request_runtime_without_policy"]
    assert len({d["grant_tools"] for d in native["policy_derivations"]}) <= 1
    for d in native["policy_derivations"]:
        if d["arms"] == ["reference"]:
            assert d["policy"] == native["request"]["runtime"]["policy"]
    bodies = [x["body"] for x in factory.made[0].provider.received()]
    if vector.get("kind") == "builtin":
        eq = native["builtin_equivalence"]
        assert eq["equivalent"] and native["request"]["reference"]["logical_version"] == eq["shadow_version"]
        assert native["request"]["runtime"]["requested_model"] == "flash"
        # Faithful omission: thinking enabled, reasoning_effort never sent.
        assert native["request"]["runtime"]["effort"] == "provider_default"
        assert bodies and all(b.get("thinking") == {"type": "enabled"} and "reasoning_effort" not in b
                              for b in bodies)
        if vector["name"] == "builtin_source_patch_only":
            patch = json.loads(Path(native["source_patch_candidates"][0]).read_text())
            assert list(patch["source_diff"]) == ["templates/investigate.md"] and patch["applied"] is False
    elif bodies:
        # Explicit-effort profile unchanged: the literal unit's high effort is sent.
        assert native["request"]["runtime"]["effort"] == "high"
        assert all(b.get("reasoning_effort") == "high" for b in bodies)
    if vector.get("adoption"):
        assert native["adoption_intent"]["staged_before_candidate"] is True
        if result["error"] != "ADOPTION_AUTHORITY":
            assert native["adoption_records"]["reviewed"]["candidate_version"] == view["candidate_version"]


def _payload(port, f, request, decision="candidate"):
    if port == "freeze":
        return request.model_dump(mode="json")
    if port == "diagnose":
        return {"observation": "Train feedback shows missing cited evidence.", "train_case_ids": ["train-01"]}
    if port == "hypothesis":
        return {"hypothesis_id": "one-hypothesis", "change": "Ask for explicit evidence citations.",
                "falsifiable_prediction": "Mean normalized train score improves, with fixed settings and equal counts.",
                "metric": "mean_normalized", "train_case_ids": ["train-01"]}
    if port == "candidate":
        return edit_fixture("useful")
    if port == "holdout_seal":
        return f.selection_authorization()
    if port == "decision":
        return {"action": decision, "review_ref": "reviewed", "owner_decision_ref": "owner-approved"}
    if port == "stop":
        return {"reason": f.round.view().stop_reason or "owner_stop"}
    return {}


def _drive(f, request, until, decision="candidate"):
    """The conformance driver's order, up to (not including) port ``until``."""
    for _ in range(24):
        obs = f.observations()
        if obs["pending_checkpoint"]:
            f.actor = "owner"
            f.approve_checkpoint()
        port = f.observations()["view"]["phase"]
        if port == until:
            return
        f.actor = ROLES[port]
        f.invoke(port, _payload(port, f, request, decision))
    raise AssertionError("did not reach " + until)


def _write_mapping_claim(f):
    """A b52-shaped claim: the literal generated unit asserted to BE the
    builtin export, with the builtin's real identity and redundant condition."""
    from .builtin_shadow import _redundant_end_conditions, capture_builtin
    from .native_fixture import REPO
    w = f.world
    b = capture_builtin(w.sf, w.registry, REPO, "investigate")
    claim = {"builtin": "investigate", "shadow": REFERENCE, "builtin_bundle_identity": b["identity"],
             "prompts": {"template:investigate.md": f"inline:{REFERENCE}__investigator"},
             "roles": {"investigator": f"{REFERENCE}__investigator"},
             "dropped_redundant_end_conditions": _redundant_end_conditions(b["graph"])}
    path = f.root / "private" / "optimizer" / "builtin-shadow" / f"{REFERENCE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claim))
    return claim


def test_builtin_export_equivalence_and_substitution_refusals(factory):
    """The actual export is equivalent to the Source builtin under its explicit
    mapping; the b52 substitution and a later mismatch of the export are refused
    before any capture, freeze or send. The accepted evaluator then refuses the
    faithful builtin's served runtime (unspecified effort): recorded, not patched."""
    from contracts.rsi.pipeline_versioning import contract as vc
    out = {}
    f = factory()                                   # (a) b52 substitution
    f.world.install_target("generated")
    out["substitution_claim"] = _write_mapping_claim(f)
    with pytest.raises(vc.UnsupportedContent) as exc:
        f.round.trusted_request("builtin", effects=True)
    out["substitution"] = {"refused": str(exc.value)[:600], "report": f.round.equivalence}
    assert f.round.equivalence["equivalent"] is False
    assert {d["field"] for d in f.round.equivalence["differences"]} >= {"graph", "roles"}
    assert f.provider.received() == [] and not f.world.versions.list_versions()
    f = factory()                                   # (b) actual export
    mapping = f.world.install_target("builtin")
    report = f.round._builtin_equivalence()
    out["export_mapping"], out["export_report"] = mapping, report
    assert report["equivalent"] is True, report
    assert [d["condition"]["type"] for d in mapping["dropped_redundant_end_conditions"]] == ["max_total_steps"]
    assert mapping["prompts"] == {"template:investigate.md": f"inline:{REFERENCE}__investigator"}
    profile = report["effective_profiles"]["investigator"]
    assert profile["builtin"] == profile["shadow"] and profile["builtin"]["internal_model"] == "flash"
    assert profile["builtin"]["effort"] is None             # nothing declared, nothing injected
    request = f.round.trusted_request("builtin", effects=True)
    out["pinned_runtime"] = request.runtime.model_dump(mode="json")
    assert request.runtime.effort == "provider_default" and request.runtime.requested_model == "flash"
    assert f.provider.received() == []
    edit = f.world.versions.edit(REFERENCE, {"kind": "role", "role": "investigator",   # (c) mismatch
                                             "changes": {"temperature": 0.7}})
    assert "error" not in edit, edit
    out["mismatch"] = f.round._builtin_equivalence()
    evidence("native-builtin-export-equivalence", out)
    assert out["mismatch"]["equivalent"] is False
    assert any(d["field"] == "roles" for d in out["mismatch"]["differences"])


def test_explicit_high_under_provider_default_pin_is_refused_before_send(factory):
    """Under the faithful builtin's omission pin, an operator route edit while
    cell 0 is in flight makes the engine's re-ask carry an explicit high effort:
    the admission boundary denies it before the physical send."""
    f = factory()
    request = f.prepare("builtin")
    assert request.runtime.effort == "provider_default"
    f.actor = "coordinator"
    f.invoke("freeze", request.model_dump(mode="json"))
    routes = f.root / "runtime" / "model_routes.json"

    def operator_edit(_record):
        from core.model_routes import reset_cache
        doc = json.loads(routes.read_text(encoding="utf-8"))
        doc["flash"]["effort"] = {"ark/deepseek-v4-flash": "high"}
        routes.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        reset_cache()
    f.provider.first_cell = ("train-01", "reference")
    f.provider.hooks["runtime_midcell"] = operator_edit
    f.provider.fault = "runtime_midcell"        # first answer asks no tool: engine re-asks
    view = f.invoke("train_reference", {})
    sends = f.provider.received()
    native = f.native_evidence()
    evidence("native-provider-default-pin-drift", {"view": view.model_dump(mode="json"),
                                                   "sent_bodies": [x["body"] for x in sends],
                                                   "native": native})
    denied = [d for d in native["trace_denials"] if d.get("reason") == "RUNTIME_DRIFT"]
    assert len(sends) == 1 and "reasoning_effort" not in sends[0]["body"]
    assert denied and denied[0]["observed"]["reasoning_effort"] == "high"
    assert denied[0]["observed"]["reasoning_effort_present"] is True
    assert view.stop_reason == "invalid" and view.known.requests == 1


def test_diagnostic_builtin_round_under_route_declared_effort(factory):
    """DIAGNOSTIC, not a shared-scenario result: the same literal builtin vector
    in a world whose frozen route table DECLARES the flash endpoint's effort.
    The unit is unchanged (equivalence still required); only the deployment
    route differs, so this shows what remains once Root decides that input."""
    vector = next(v for v in SCENARIOS if v["name"] == "builtin_source_patch_only")
    factory.flash_route_effort = "high"
    result, error = None, None
    try:
        result = run_scenario(factory, vector)
    except BaseException:                                           # noqa: BLE001
        error = traceback.format_exc()
        raise
    finally:
        native = factory.made[0].native_evidence() if factory.made else None
        evidence("diagnostic-builtin-route-declared-effort", {"result": result, "error": error,
                                                              "native": native})
    assert native["builtin_equivalence"]["equivalent"]
    patch = json.loads(Path(native["source_patch_candidates"][0]).read_text())
    assert list(patch["source_diff"]) == ["templates/investigate.md"] and patch["applied"] is False


def test_adoption_record_for_another_candidate_is_refused(factory):
    """Host records bound to another candidate (or to none) cannot adopt this
    one; the same records rebound to the actual candidate adopt it."""
    f = factory()
    request = f.prepare()
    f.approve_adoption()
    _drive(f, request, "decision", decision="adopt")
    r, out = f.round, {}
    f._bind_adoption_records()
    good = json.loads(json.dumps(f._assignment()["adoption"]))
    decision = _payload("decision", f, request, "adopt")
    for label, mutate in (("other_candidate", lambda rec: rec.update(candidate_version=request.reference.logical_version)),
                          ("other_seal", lambda rec: rec.update(candidate_seal=request.reference.native_seal)),
                          ("unbound_b52_shape", lambda rec: [rec.pop(k) for k in list(rec)
                                                            if k.startswith(("candidate", "reference", "expected"))])):
        records = json.loads(json.dumps(good))
        for rec in records.values():
            mutate(rec)
        f._assignment()["adoption"] = records
        before = f.snapshot()
        with pytest.raises(OptimizerError) as exc:
            r.invoke("decision", decision, actor="owner")
        out[label] = exc.value.code
        assert exc.value.code == "ADOPTION_AUTHORITY" and f.snapshot() == before
    f._assignment()["adoption"] = good
    r.invoke("decision", decision, actor="owner")
    _drive(f, request, "stop", decision="adopt")
    view = r.view()
    out.update(final=view.model_dump(mode="json"), records=good, history=r.current_history)
    evidence("native-adoption-candidate-binding", out)
    assert view.stop_reason == "adopted" and view.current_version == view.candidate_version == r.candidate_vid
    assert r.current_history == [r.candidate_vid]


def test_grant_change_after_freeze_is_not_recompiled(factory):
    """A host grant changed after freeze is refused at the next request, not
    compiled into a new expected policy: no cell launches, no sends."""
    f = factory()
    request = f.prepare()
    f.actor = "coordinator"
    f.invoke("freeze", request.model_dump(mode="json"))
    f.host.grants[request.budget.authorization_ref]["tools"]["agent"].append("read_file")
    view = f.invoke("train_reference", {})
    native = f.native_evidence()
    evidence("native-grant-change-after-freeze", {"view": view.model_dump(mode="json"), "native": native})
    assert view.stop_reason == "invalid" and native["provider_sends"] == native["evaluator_cell_launches"] == 0
    assert "grant changed" in json.dumps(native["refusals"])


def test_engine_dispatch_without_owning_invocation_does_nothing(factory):
    """A driver advancing the round's run directly reaches the port tool with no
    owning invocation: no evaluator work, and the graph's error branch."""
    fixture = factory()
    request = fixture.prepare()
    fixture.actor = "coordinator"
    fixture.invoke("freeze", request.model_dump(mode="json"))
    sf, run_id = fixture.world.sf, fixture.round.run_id
    sf.advance_run(run_id)                                   # foreign driver
    run = sf.get_run(run_id)
    step = [s for s in sf.get_steps(run_id, include_payloads=True)
            if s["step_id"] == "train_reference"][-1]
    native = fixture.native_evidence()
    evidence("native-foreign-dispatch", {"run": {k: run[k] for k in ("status", "current_node")},
                                         "step": {k: step[k] for k in ("status", "outputs_json")},
                                         "native": native})
    assert run["current_node"] == "stop" and step["status"] == "completed"
    assert "AUTHORITY" in json.loads(step["outputs_json"])["error"]
    assert native["provider_sends"] == native["evaluator_cell_launches"] == 0
    with pytest.raises(OptimizerError) as exc:
        fixture.invoke("train_reference", {})
    assert exc.value.code == "ORDER"


def test_import_identities():
    mods = {}
    for name in ["contracts.rsi.optimizer.contract", "contracts.rsi.optimizer.conformance",
                 "contracts.rsi.optimizer.fake", "contracts.rsi.optimizer.native_binding",
                 "contracts.rsi.optimizer.native_fixture", "contracts.rsi.optimizer.native_child",
                 "contracts.rsi.evaluator.contract", "contracts.rsi.evaluator.native_fixture",
                 "contracts.rsi.evaluator.native_transport", "contracts.rsi.pipeline_versioning.contract",
                 "contracts.rsi.pipeline_versioning.native_provider", "core.pipeline_versions",
                 "core.pipeline_frozen_bundle", "core.pipeline_optimization", "core.pipeline_benchmark",
                 "core.rsi_evaluator", "core.rsi_admission", "core.rsi_policy", "core.rsi_checker",
                 "core.skillflow_host", "core.ai_router", "core.agents", "core.run_driver",
                 "core.run_launcher", "skillflow", "skillflow.core", "skillflow.graph",
                 "skillflow.tool_loader", "litellm", "openai", "httpx"]:
        m = importlib.import_module(name)
        p = Path(m.__file__)
        mods[name] = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    graph = Path(__file__).with_name("round.graph.json")
    identity = {"sdk": importlib.metadata.version("skillflow-py"),
                "litellm": importlib.metadata.version("litellm"),
                "openai": importlib.metadata.version("openai"),
                "httpx": importlib.metadata.version("httpx"),
                "pythonpath": os.getenv("PYTHONPATH"), "source_sha": os.getenv("RSI_SOURCE_SHA"),
                "image": os.getenv("RSI_IMAGE_ID"),
                "round_graph_sha256": hashlib.sha256(graph.read_bytes()).hexdigest(),
                "modules": mods}
    evidence("import-identities", identity)
    assert identity["sdk"] == "1.5.94"
    assert all(v["path"].startswith("/source/") for k, v in mods.items()
               if k.startswith(("contracts.", "core.")))
