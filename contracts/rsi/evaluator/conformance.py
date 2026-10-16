"""Same literal scenarios for fake AND genuine host; no provider-only skip path.

The future native fixture factory implements the fixture-controls Protocol using
ordinary registration, launch/runner/SDK and actual data-store observation. Fault
injection belongs at fixture inputs/transport, never patches methods under test.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Callable, Protocol
from pydantic import ValidationError
from .contract import (EvaluationRequest, EvaluationResult, Refusal, benchmark_row,
                       digest, scorer_receipt, validate_result)


class FixtureBinding(Protocol):
    evidence_kind: str
    def prepare(self, split="train", arms=("reference", "candidate")) -> EvaluationRequest: ...
    def set_fault(self, name: str) -> None: ...
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult | Refusal: ...
    def observations(self) -> dict: ...
    def probe_admission(self, vector: dict) -> dict: ...


SCENARIOS = json.loads(Path(__file__).with_name("fixtures").joinpath("scenarios.json").read_text())["scenarios"]


def run_scenario(factory: Callable[[], FixtureBinding], scenario: dict) -> dict:
    fixture = factory()
    request = fixture.prepare(scenario["split"])
    fixture.set_fault(scenario["fault"])
    before = fixture.observations()
    outcome = fixture.evaluate(request)
    observed = fixture.observations()
    assert observed["evidence_kind"] == fixture.evidence_kind
    assert observed["physical_sends"] - before["physical_sends"] == scenario["physical_sends"]
    if scenario["status"] == "refused":
        assert isinstance(outcome, Refusal) and outcome.reason == scenario["reason"]
        assert outcome.execution is None and not outcome.scoreboard_mutated and not outcome.retryable
        assert observed["launches"] == before["launches"]
        assert observed["scoreboard_rows"] == before["scoreboard_rows"]
        assert observed["denials"] > before["denials"]
        return {"scenario":scenario["name"], "status":"pass", "evidence_kind":fixture.evidence_kind,
                "observed":observed, "reason":outcome.reason}
    assert isinstance(outcome, EvaluationResult) and outcome.status == scenario["status"]
    validate_result(request, outcome)
    assert outcome.expected_cells == request.cells
    assert tuple(r.cell for r in outcome.receipts) == request.cells
    assert len(outcome.receipts) == (72 if request.split == "train" else 48)
    assert len(set(r.receipt_id for r in outcome.receipts)) == len(outcome.receipts)
    assert observed["scoreboard_rows"] - before["scoreboard_rows"] == len(request.cells)
    for receipt in outcome.receipts:
        assert receipt.selection == next(s for s in request.selections if s.arm == receipt.cell.arm)
        assert receipt.case == next(c for c in request.benchmark.cases if c.case_id == receipt.cell.case_id)
        assert receipt.runtime_before == request.runtime
        if receipt.execution:
            assert receipt.execution.provenance == fixture.evidence_kind
            assert receipt.execution.sdk_graph_version == receipt.selection.sdk_graph_version
            assert receipt.execution.sdk_graph_digest == receipt.selection.sdk_graph_digest
        else:
            assert receipt.disposition == "not_started" and not receipt.attempts
        row = benchmark_row(receipt, request.benchmark)
        assert row["checker_identity"] == receipt.case.scorer_ref_identity
        assert row["checker_identity"] != receipt.checker_package
        assert row["bundle_identity"] == receipt.selection.native_bundle
        assert "execution" not in row and "logical_version" not in row
        assert scorer_receipt(receipt)["checker_id"] == receipt.checker_package
    if scenario["reason"]:
        assert outcome.receipts[0].reason == scenario["reason"]
        assert all(r.disposition == "not_started" and r.reason == "ABORTED" for r in outcome.receipts[1:])
    if outcome.status == "unknown":
        assert outcome.retained_unknown.requests == 1 and outcome.retained_unknown.usd_micros > 0
        assert outcome.receipts[0].attempts[0].measured is None
        assert benchmark_row(outcome.receipts[0], request.benchmark)["cost_usd"] is None
    if scenario["fault"] == "task_failure":
        assert outcome.receipts[0].disposition == "task_failure"
        assert outcome.receipts[0].checker is None
        assert not outcome.receipts[0].envelope_exceeded
        assert outcome.optimizer_projection.cells[0].normalized_score == "0"
    if request.split == "holdout":
        assert outcome.optimizer_projection is None
    else:
        public = outcome.optimizer_projection.model_dump(mode="json")
        assert set(public) == {"schema","evaluation_id","status","train_prompts","cells","grants_authority"}
        assert len(public["train_prompts"]) == 12
        assert all(cid.startswith("train-") for cid,prompt in public["train_prompts"])
        text = json.dumps(public)
        assert "HOLDOUT" not in text and "PRIVATE_CHECKER" not in text
        assert not any(x in text for x in ("rubric_ref", "private_result", "run_id", "checker_package"))
    assert not outcome.grants_authority
    # Data-model adversaries: omission/duplication must never become completion.
    payload = outcome.model_dump(mode="json")
    for receipts in (payload["receipts"][:-1], payload["receipts"] + payload["receipts"][:1]):
        changed = dict(payload, receipts=receipts)
        try:
            EvaluationResult.model_validate_json(json.dumps(changed))
        except ValidationError:
            pass
        else:
            raise AssertionError("missing/duplicate receipt accepted")
    return {"scenario":scenario["name"], "status":"pass", "evidence_kind":fixture.evidence_kind,
            "observed":observed, "result_digest":digest(outcome)}


def run_shared(factory: Callable[[], FixtureBinding]) -> list[dict]:
    """No skip/xfail/provider-selection parameter. Every scenario must execute."""
    return ([run_scenario(factory, scenario) for scenario in SCENARIOS]
            + [run_admission_scenario(factory, vector) for vector in ADMISSION_SCENARIOS])


def run_admission_scenario(factory, vector):
    """Same scope-boundary vectors over model/native production admission seam."""
    fixture = factory()
    fixture.prepare()
    result = fixture.probe_admission(vector)
    assert result['evidence_kind'] == fixture.evidence_kind
    assert result['admitted'] == vector['expected_admitted']
    assert result['denied'] == vector['expected_denied']
    assert result['retained_requests'] == vector['expected_retained_requests']
    assert result['physical_sends'] == 0  # deliberate pre-transport scope probes
    assert result['denial_retryable'] is False
    return {'scenario':vector['name'], 'status':'pass', **result}


ADMISSION_SCENARIOS = json.loads(Path(__file__).with_name('fixtures').joinpath('budget_vectors.json').read_text())['vectors']
