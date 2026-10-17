"""PROOF checks: the SAME shared conformance against the genuine host binding.

Runs only in the authorized isolated SDK1.5.94 container. Every scenario uses a
fresh native world (``native_fixture``): ordinary registration, accepted
versioning capture/seal, ea100 sealed registration, ordinary launch/claim/
runner/confirm, real gateway/litellm/openai SDK sends admitted by the real
transport boundary, a deterministic transport FIXTURE as the provider, real
BenchmarkStore rows. Evidence is written to $EVALUATOR_EVIDENCE.
"""
import hashlib
import importlib
import importlib.metadata
import json
import os
import sqlite3
from pathlib import Path

import pytest

from .conformance import ADMISSION_SCENARIOS, SCENARIOS, run_admission_scenario, run_scenario
from .contract import EvaluationResult, Refusal, benchmark_row, digest, scorer_receipt
from .native_fixture import NativeFactory, NativeFixture


def evidence(name, value):
    target = os.getenv("EVALUATOR_EVIDENCE")
    if target:
        Path(target, name + ".json").write_text(json.dumps(value, indent=1, default=repr) + "\n")


@pytest.fixture
def factory(tmp_path_factory):
    base = Path(os.getenv("RSI_WORK") or tmp_path_factory.mktemp("worlds"))
    made = NativeFactory(base)
    yield made
    made.close()


def _trace(fixture):
    rows = []
    for db in sorted(fixture.root.glob("ws/*/trace.db")):
        con = sqlite3.connect(db)
        rows += [{"project": db.parent.name, "seq": r[0], "category": r[1], "event": r[2],
                  "payload": json.loads(r[3] or "{}")}
                 for r in con.execute("select seq, category, event, payload_json from skillflow_trace "
                                      "where category = 'rsi_admission' order by seq")]
        con.close()
    return rows


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["name"])
def test_same_shared_scenarios_native(scenario, factory):
    result = run_scenario(factory, scenario)
    fixture = factory.made[0]
    sends = fixture.provider.received()
    admission = _trace(fixture)
    evidence("native-scenario-" + scenario["name"], {
        **result, "provider_requests": len(sends),
        "trace_physical_sends": sum(1 for r in admission if r["event"] == "rsi_physical_send"),
        "trace_denials": [r["payload"] for r in admission if r["event"] == "rsi_physical_denied"],
        "versions_impl": fixture.world.versions_impl,
        "killed_child": getattr(fixture, "killed_child", None)})
    # Every request the provider side received was admitted (durable trace),
    # and nothing admitted went unaccounted at the provider.
    assert sum(1 for r in admission if r["event"] == "rsi_physical_send") == len(sends)


@pytest.mark.parametrize("vector", ADMISSION_SCENARIOS, ids=lambda v: v["name"])
def test_same_shared_admission_vectors_native(vector, factory):
    evidence("native-admission-" + vector["name"], run_admission_scenario(factory, vector))


def _evaluate(factory, fault="none", split="train"):
    fixture = factory()
    request = fixture.prepare(split)
    fixture.set_fault(fault)
    return fixture, request, fixture.evaluate(request)


def test_useful_native_cell_and_matrix_custody(factory):
    """Complete train matrix: real scored outputs, strict rows, allowlisted
    train projection, no private/holdout bytes ever reached the provider."""
    from core import pipeline_optimization as numerical
    fixture, request, result = _evaluate(factory)
    assert isinstance(result, EvaluationResult) and result.status == "complete"
    first = result.receipts[0]
    snapshot = fixture.root / "private" / "evaluator" / "outputs"
    files = sorted(snapshot.glob("*.md"))
    assert files, "complete findings snapshots retained"
    assert first.checker.output_snapshot == digest(
        {"findings.md": "sha256:" + hashlib.sha256(files[0].read_bytes()).hexdigest()}) or any(
        first.checker.output_snapshot == digest({"findings.md": "sha256:" + hashlib.sha256(
            f.read_bytes()).hexdigest()}) for f in files)
    points = {r.cell.arm: sorted({x.checker.raw_points for x in result.receipts
                                  if x.cell.arm == r.cell.arm}) for r in result.receipts}
    assert points == {"reference": [6], "candidate": [10]}
    sent = fixture.provider.received()
    private = json.loads((fixture.root / "private" / "checker" / "private.json").read_text())
    blob = json.dumps([s["body"] for s in sent])
    assert not any(m["material"]["sentinel"] in blob for m in private.values())
    assert "PUBLIC_HOLDOUT" not in blob and "expected_constant" not in blob
    # Public cell roots hold only public fixture bytes.
    cells = fixture.root / "public-cells"
    leaked = [p for p in cells.rglob("*") if p.is_file() and ".git" not in p.parts
              and b"PRIVATE_CHECKER" in p.read_bytes()]
    assert leaked == []
    projection = result.optimizer_projection.model_dump(mode="json")
    assert set(projection) == {"schema", "evaluation_id", "status", "train_prompts", "cells",
                               "grants_authority"}
    text = json.dumps(projection)
    assert "PRIVATE_CHECKER" not in text and "HOLDOUT" not in text and "private_result" not in text
    rows = fixture.world.store.list_receipts("evaluator-fixture", 1)
    assert len(rows) == 72 and all(r["validity"] == "VALID" for r in rows)
    from core.pipeline_benchmark import ROW
    assert all(set(r) == set(ROW) | {"seq", "recorded_by"} for r in rows)
    cases = request.benchmark.cases
    manifest = {"train_case_ids": [c.case_id for c in cases if c.split == "train"],
                "holdout_case_ids": [c.case_id for c in cases if c.split == "holdout"],
                "case_families": {c.case_id: c.family for c in cases},
                "family_inventory": ["family-a", "family-b", "family-c", "family-d"],
                "repetitions": 3,
                "arms": {s.arm: {"bundle_id": s.native_bundle, "runtime_id": digest(request.runtime),
                                 "checker_id": request.benchmark.checker_package}
                         for s in request.selections},
                "input_ids": {c.case_id: c.prompt_identity for c in cases}}
    stage = numerical.evaluate_matrix(manifest, [scorer_receipt(r) for r in result.receipts],
                                      stage="train")
    expected = {"reference": request.selections[0].native_bundle,
                "candidate": request.selections[1].native_bundle}
    aggregate = fixture.world.store.aggregate("evaluator-fixture", 1, {
        arm: {"bundle_identity": bundle, "runtime": benchmark_row(result.receipts[0],
                                                                   request.benchmark)["runtime"]}
        for arm, bundle in expected.items()})
    evidence("native-useful-matrix", {
        "status": result.status, "points": points, "rows": len(rows),
        "stage": stage["status"], "difference": stage["public_summary"]["overall_difference"],
        "full_document_aggregate": aggregate["status"], "missing": len(aggregate["missing_cells"]),
        "snapshots": len(files), "provider_requests": len(sent),
        "first_receipt": first.model_dump(mode="json"), "projection_keys": sorted(projection)})
    assert stage["status"] == "train_complete"
    assert stage["public_summary"]["overall_difference"]["exact"] == "40"
    assert aggregate["status"] == "INVALID" and len(aggregate["missing_cells"]) == 48


def test_transport_retries_and_repairs_are_admitted_with_unchanged_profile(factory):
    """Underlying openai-SDK retries are NOT disabled: each actual resend is
    admitted. Format repairs resend with the exact frozen profile."""
    out = {}
    for fault, causes in (("transport_retry", ["initial", "transport_retry", "transport_retry"]),
                          ("repair", ["initial", "format_repair", "format_repair"]),
                          ("retry", ["initial", "retry", "retry"])):
        fixture, request, result = _evaluate(factory, fault)
        first = result.receipts[0]
        sent = [s for s in fixture.provider.received()
                if (s["case"], s["arm"]) == (request.cells[0].case_id, request.cells[0].arm)][:3]
        wire = [{k: s["body"].get(k) for k in ("model", "max_tokens", "reasoning_effort", "thinking",
                                                "temperature")} | {"url": s["url"]} for s in sent]
        out[fault] = {"causes": [a.cause for a in first.attempts],
                      "settlements": [a.settlement for a in first.attempts],
                      "wire": wire, "disposition": first.disposition}
        assert result.status == "complete" and first.disposition == "scored"
        assert [a.cause for a in first.attempts] == causes
        assert all(a.effective_runtime == request.runtime for a in first.attempts)
        assert len({json.dumps(w, sort_keys=True) for w in wire}) == 1   # same effective profile
        assert wire[0]["thinking"] == {"type": "enabled"} and wire[0]["reasoning_effort"] == "high"
    evidence("native-retry-repair-proof", out)


def test_dispatch_and_ingress_refuse_without_live_grant(factory):
    """Host dispatch denies ungranted and unbound tools; ingress refuses an
    evaluator-owned run whose live binding is gone (process loss)."""
    from core import rsi_admission
    from skillflow.exceptions import SkillFlowError
    fixture, request, result = _evaluate(factory, "unsafe_resume")
    assert isinstance(result, Refusal) and result.reason == "UNSAFE_RESUME"
    sf = fixture.world.sf
    orphan = [r for r in sf.list_runs()
              if "_rsi_evaluation" in json.loads(r.get("context_json") or "{}")]
    assert len(orphan) == 1
    run_id = orphan[0]["id"]
    assert rsi_admission.binding_for_run(run_id) is None
    with pytest.raises(SkillFlowError, match="UNSAFE_RESUME"):
        sf.claim_next_step(run_id)
    with pytest.raises(SkillFlowError, match="UNSAFE_RESUME"):
        sf.advance_run(run_id)
    denied = sf.execute_tool("list_tree", {}, run_id=run_id, step_id="investigate")
    assert "not granted" in denied.get("error", "")
    # The same run under a live binding built by the production compiler, as
    # its coordinator would hold it: exact (run, step, agent) membership.
    from types import SimpleNamespace
    from core import rsi_policy
    from core.rsi_evaluator import CellBinding
    from skillflow.exceptions import ToolArgumentsUnavailable
    policy, compiled, _ = fixture.evaluator.compile(request)
    marker = json.loads(orphan[0]["context_json"])["_rsi_evaluation"]
    arm = compiled[json.loads(orphan[0]["context_json"])["_rsi_evaluation"]["cell"]["arm"]]
    live = CellBinding(scope=None, policy=rsi_policy.CellPolicy(policy, arm), marker=marker)
    rsi_admission.bind_run(run_id, live)
    try:
        granted = sf.execute_tool("list_tree", {}, run_id=run_id, step_id="investigate")
        ungranted = sf.execute_tool("read_file", {"path": "x"}, run_id=run_id, step_id="investigate")
        wrong_step = sf.execute_tool("list_tree", {}, run_id=run_id, step_id="other")
        hook = sf._execute_tool_hook(SimpleNamespace(run_id=run_id), None, "on_deliver",
                                     {"tool": "repo_apply"})
        with pytest.raises(ToolArgumentsUnavailable):
            sf._execute_tool_inline(SimpleNamespace(tool_name="list_tree", tool_params={}),
                                    run_id=run_id, graph_name=orphan[0]["graph_name"])
    finally:
        rsi_admission.unbind_run(run_id, live)
    empty = policy.model_copy(update={"grants": tuple(g.model_copy(update={"providers": ()})
                                                      for g in policy.grants)})
    hollow = CellBinding(scope=None, policy=rsi_policy.CellPolicy(empty, arm), marker=marker)
    rsi_admission.bind_run(run_id, hollow)
    try:
        empty_grant = sf.execute_tool("list_tree", {}, run_id=run_id, step_id="investigate")
    finally:
        rsi_admission.unbind_run(run_id, hollow)
    assert "not granted" not in str(granted.get("error", ""))
    for refused in (ungranted, wrong_step, empty_grant):
        assert "not granted" in refused.get("error", "")
    assert hook["passed"] is False and "not granted" in hook["error"]
    evidence("native-process-loss", {"refusal": result.model_dump(mode="json"),
                                     "orphan_run": orphan[0]["status"], "dispatch_unbound": denied,
                                     "dispatch_granted": {k: str(v)[:200] for k, v in granted.items()},
                                     "dispatch_ungranted": ungranted, "dispatch_wrong_step": wrong_step,
                                     "dispatch_empty_grant": empty_grant, "hook": hook,
                                     "killed_child": fixture.killed_child})


def test_gateway_retry_decorator_sends_are_admitted(factory):
    """The gateway's own model-retry layer (tenacity on generate_native) is a
    physical-send source too: a burst 429 is retried in place (production-like
    route), a 503 twice on a single-candidate route; each resend is admitted."""
    out = {}
    for label, fallback, status, failures, causes in (
            ("burst429-fallback-route", True, 429, 1, ["initial", "retry"]),
            ("503-single-route", False, 503, 2, ["initial", "retry", "retry"])):
        fixture = NativeFixture(factory.base, endpoint_fallback=fallback)
        try:
            request = fixture.prepare()
            fixture.provider.fault = "tenacity"
            fixture.provider.tenacity_status = status
            fixture.provider.tenacity_failures = failures
            result = fixture.evaluate(request)
            first = result.receipts[0]
            out[label] = {"status": result.status, "causes": [a.cause for a in first.attempts],
                          "settlements": [a.settlement for a in first.attempts],
                          "provider_requests": len(fixture.provider.received())}
            assert result.status == "complete" and first.disposition == "scored"
            assert [a.cause for a in first.attempts] == causes
            assert len(fixture.provider.received()) == 72 + len(causes) - 1
        finally:
            fixture.close()
    evidence("native-gateway-retry-decorator", out)


def test_import_identities(factory):
    names = ["core.rsi_evaluator", "core.rsi_admission", "core.rsi_policy", "core.rsi_checker",
             "core.ai_router", "core.agents", "core.dpe_pipeline", "core.run_launcher",
             "core.run_driver", "core.run_isolation", "core.skillflow_host",
             "core.pipeline_frozen_bundle", "core.pipeline_benchmark", "core.pipeline_optimization",
             "contracts.rsi.evaluator.contract", "contracts.rsi.evaluator.conformance",
             "contracts.rsi.evaluator.native_fixture", "contracts.rsi.evaluator.native_transport",
             "skillflow.core", "skillflow.graph", "litellm", "openai", "httpx"]
    fixture = factory()
    names.append(fixture.world.versions.__class__.__module__)
    files = []
    for name in names:
        module = importlib.import_module(name)
        path = Path(module.__file__)
        files.append({"module": name, "path": str(path),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    dists = {d: importlib.metadata.version(d) for d in ("skillflow-py", "litellm", "openai",
                                                        "httpx", "pydantic")}
    evidence("import-identities", {"files": files, "distributions": dists,
                                   "versions_impl": fixture.world.versions_impl})
    assert dists["skillflow-py"] == "1.5.94"
    assert all(f["path"].startswith("/source/") for f in files
               if f["module"].startswith(("core.", "contracts.")))
