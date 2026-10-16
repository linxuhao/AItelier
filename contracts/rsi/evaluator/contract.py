"""Immutable wire types and pure projections, NOT a runtime or source of facts.

All identity/accounting fields must be filled by trusted observed host evidence.
The standalone fake explicitly marks simulated evidence. See README for ingress
and dispatch obligations that these data types cannot themselves enforce.
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Name = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Positive = Annotated[int, Field(strict=True, gt=0)]
Nonnegative = Annotated[int, Field(strict=True, ge=0)]
Arm = Literal["reference", "candidate"]
Split = Literal["train", "holdout"]
Reason = Literal[
    "MALFORMED_INPUT", "UNKNOWN_ID", "PROVIDER_UNAVAILABLE", "PROVIDER_CHANGED",
    "POLICY_REFUSED", "INPUT_MISMATCH", "CHECKER_MISMATCH", "OUTPUT_MISMATCH",
    "RUNTIME_DRIFT", "BUDGET_EXHAUSTED", "UNEXPECTED_CHECKPOINT",
    "INFRASTRUCTURE", "SCORER_ERROR", "UNKNOWN_EXECUTION", "UNKNOWN_ACCOUNTING",
    "UNSAFE_RESUME", "ABORTED", "NONE",
]


def require(condition, message="contract invariant"):
    if not condition:
        raise ValueError(message)


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def digest(value) -> str:
    """Evaluator artifact digest; does NOT redefine the accepted version codec."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=lambda obj: obj.model_dump(mode="json")).encode()).hexdigest()


class Amount(Frozen):
    """Integer USD microdollars; no floating point admission or cached discount."""
    requests: Nonnegative
    input_tokens: Nonnegative
    output_tokens: Nonnegative
    usd_micros: Nonnegative


class Envelope(Frozen):
    upper: Amount
    elapsed_ms: Positive


class Budget(Frozen):
    scope_id: Name
    authorization_ref: Name  # lookup in host-owned grants, never self-authentication
    pricing_identity: Digest
    cell: Envelope
    round: Envelope
    campaign: Envelope


class Endpoint(Frozen):
    provider: Name
    model: str = Field(min_length=1)
    endpoint: Name  # nonsecret stable label, never URL credentials/headers


class Runtime(Frozen):
    app_source: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    image: Digest
    sdk_version: Literal["1.5.94"]
    sdk_wheel: Digest
    routes: Digest
    pricing: Digest
    policy: Digest
    endpoint: Endpoint  # first slice: ONE concrete endpoint, no fallback list
    requested_model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    temperature_micros: Nonnegative
    max_output_tokens: Positive
    max_tool_turns: Positive
    cell_timeout_ms: Positive
    transport_evidence: Digest  # proven physical-send hook or verified no-retry profile


class Selection(Frozen):
    arm: Arm
    logical_version: Digest  # existing pipeline-versioning v2 identity, not native ID
    native_bundle: Digest
    native_seal: Digest
    physical_config: Annotated[str, Field(pattern=r"^gen_rsi_v[0-9a-f]{31}_[0-9a-f]{16}$")]
    sdk_graph_version: Positive
    sdk_graph_digest: Digest
    roles: Digest
    templates: Digest
    tools: Digest
    capabilities: Digest


class Case(Frozen):
    case_id: Name
    split: Split
    family: Literal["family-a", "family-b", "family-c", "family-d"]
    prompt_identity: Digest  # BenchmarkStore digest({prompt}), not fixture tree
    fixture_tree: Digest
    scorer_ref_identity: Digest  # digest of refs+max_points, NOT resolved bytes
    resolved_private_material: Digest
    max_points: Positive


class Benchmark(Frozen):
    benchmark_id: Name
    version: Positive
    definition: Digest
    public_package: Digest
    private_package: Digest
    checker_code: Digest
    checker_package: Digest  # arm-wide code + COMPLETE per-case scorer manifest
    cases: tuple[Case, ...]

    @model_validator(mode="after")
    def fixed_inventory(self):
        require(len({c.case_id for c in self.cases}) == len(self.cases), "duplicate case")
        for split, count in (("train", 12), ("holdout", 8)):
            part = [c for c in self.cases if c.split == split]
            require(len(part) == count, "fixed 12/8 split required")
            require({c.family for c in part} == {"family-a", "family-b", "family-c", "family-d"}, "contract invariant")
        return self


class Cell(Frozen):
    case_id: Name
    arm: Arm
    repetition: Annotated[int, Field(strict=True, ge=1, le=3)]


class EvaluationRequest(Frozen):
    schema: Literal["rsi.evaluation.request.v2"]
    evaluation_id: Name
    campaign_id: Name
    round_id: Name
    plan_identity: Digest
    split: Split
    repetitions: Literal[3]
    max_parallel: Annotated[int, Field(strict=True, ge=1, le=2)]
    selections: tuple[Selection, ...]  # one arm, or both in declared interleaving
    benchmark: Benchmark
    cells: tuple[Cell, ...]
    runtime: Runtime
    budget: Budget
    holdout_selection: Digest | None  # sealed selection record; trusted lookup required

    @field_validator("repetitions", mode="before")
    @classmethod
    def exact_repetitions(cls, value):
        require(type(value) is int and value == 3, "fixed integer repetitions")
        return value

    @model_validator(mode="after")
    def fixed_plan(self):
        arms = [s.arm for s in self.selections]
        require(arms in (["reference"], ["candidate"], ["reference", "candidate"]), "arm order")
        wanted = tuple(Cell(case_id=c.case_id, arm=a, repetition=r)
                       for r in range(1, 4) for c in self.benchmark.cases
                       if c.split == self.split for a in arms)
        require(self.cells == wanted, "missing/duplicate/reordered/nonstage cell")
        require((self.split == "holdout") == (self.holdout_selection is not None), "holdout seal")
        require(self.budget.pricing_identity == self.runtime.pricing, "pricing mismatch")
        require(self.plan_identity == digest(self.model_dump(mode="json", exclude={"plan_identity"})), "plan digest")
        return self


class Execution(Frozen):
    provenance: Literal["native", "simulation"]
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    step_instance_ids: tuple[str, ...] = Field(min_length=1)
    sdk_graph_version: Positive
    sdk_graph_digest: Digest
    state: Literal["completed", "failed", "unknown", "checkpoint"]
    operations_settled: bool


class PhysicalAttempt(Frozen):
    attempt_id: Name
    cause: Literal["initial", "retry", "fallback", "format_repair", "transport_retry"]
    effective_runtime: Runtime
    reservation: Amount
    settlement: Literal["known", "positive_no_send", "unknown"]
    measured: Amount | None
    observed_send: bool | None

    @model_validator(mode="after")
    def accounting(self):
        require(self.reservation.requests == 1, "one reservation per physical attempt")
        if self.settlement == "known":
            require(self.observed_send is True and self.measured is not None and self.measured.requests == 1, "known send count")
        if self.settlement == "unknown":
            require(self.measured is None, "unknown is never zero")
        else:
            require(self.measured is not None, "settled usage required")
            require(all(getattr(self.measured, k) <= getattr(self.reservation, k)
                       for k in Amount.model_fields), "usage exceeds reservation")
            if self.settlement == "positive_no_send":
                require(self.observed_send is False and not any(self.measured.model_dump().values()), "contract invariant")
        return self


class CheckerResult(Frozen):
    output_snapshot: Digest
    complete_files: tuple[str, ...]  # actual immutable full bytes, never Meta preview
    checker_package: Digest
    resolved_private_material: Digest
    private_result: Digest  # private diagnostics remain outside public projection
    raw_points: Nonnegative
    max_points: Positive

    @model_validator(mode="after")
    def bounded_points(self):
        require(self.raw_points <= self.max_points, "contract invariant")
        require(self.complete_files == ("findings.md",), "contract invariant")
        return self


class CellReceipt(Frozen):
    schema: Literal["rsi.cell.receipt.v2"]
    receipt_id: Name
    evaluation_id: Name
    campaign_id: Name
    plan_identity: Digest
    cell: Cell
    case: Case
    selection: Selection
    benchmark_definition: Digest
    public_package: Digest
    private_package: Digest
    checker_package: Digest
    runtime_before: Runtime
    runtime_after: Runtime | None
    execution: Execution | None
    disposition: Literal["scored", "task_failure", "invalid", "unknown", "not_started"]
    reason: Reason
    checker: CheckerResult | None
    attempts: tuple[PhysicalAttempt, ...]
    elapsed_ms: Nonnegative | None
    known: Amount
    retained_unknown: Amount
    envelope_exceeded: bool

    @model_validator(mode="after")
    def lifecycle(self):
        require(self.cell.case_id == self.case.case_id and self.cell.arm == self.selection.arm, "contract invariant")
        require(len({a.attempt_id for a in self.attempts}) == len(self.attempts), "contract invariant")
        require(self.known == total(a.measured for a in self.attempts if a.measured is not None), "contract invariant")
        require(self.retained_unknown == total(a.reservation for a in self.attempts if a.settlement == "unknown"), "contract invariant")
        if self.execution is None:
            require(self.disposition == "not_started" and not self.attempts, "contract invariant")
        if self.disposition in ("scored", "task_failure"):
            require(self.execution and self.execution.operations_settled, "contract invariant")
            require(self.execution.state in ("completed", "failed"), "contract invariant")
            require(self.runtime_after == self.runtime_before and self.elapsed_ms is not None, "contract invariant")
            require(not self.envelope_exceeded and self.reason == "NONE", "contract invariant")
            require(not any(self.retained_unknown.model_dump().values()), "contract invariant")
            require(all(a.settlement != "unknown" and a.effective_runtime == self.runtime_before for a in self.attempts), "contract invariant")
            require(self.execution.sdk_graph_version == self.selection.sdk_graph_version, "contract invariant")
            require(self.execution.sdk_graph_digest == self.selection.sdk_graph_digest, "contract invariant")
            if self.disposition == "scored":
                require(self.checker and self.checker.checker_package == self.checker_package, "contract invariant")
                require(self.checker.resolved_private_material == self.case.resolved_private_material, "contract invariant")
                require(self.checker.max_points == self.case.max_points, "contract invariant")
            else:
                require(self.checker is None, "task failure takes fixed zero, not fabricated checker")
        else:
            require(self.reason != "NONE" and self.checker is None, "contract invariant")
        return self


class Refusal(Frozen):
    schema: Literal["rsi.evaluation.refusal.v2"]
    evaluation_id: Name | None
    reason: Reason
    before_launch: Literal[True]
    execution: None
    scoreboard_mutated: Literal[False]
    retryable: Literal[False]


    @model_validator(mode="after")
    def refused_reason(self):
        require(self.reason != "NONE", "refusal needs reason")
        return self


class PublicCell(Frozen):
    case_id: Name
    arm: Arm
    repetition: Positive
    status: Literal["scored", "task_failure", "invalid", "unknown", "not_started"]
    normalized_score: Annotated[str, Field(pattern=r"^[0-9]+(?:/[1-9][0-9]*)?$")] | None  # exact Fraction string; numeric feedback intentional


class TrainProjection(Frozen):
    schema: Literal["rsi.optimizer.train.v2"]
    evaluation_id: Name
    status: Literal["complete", "invalid", "unknown"]
    train_prompts: tuple[tuple[Name, str], ...]
    cells: tuple[PublicCell, ...]
    grants_authority: Literal[False]


class EvaluationResult(Frozen):
    schema: Literal["rsi.evaluation.result.v2"]
    evaluation_id: Name
    plan_identity: Digest
    status: Literal["complete", "invalid", "unknown"]
    expected_cells: tuple[Cell, ...]
    receipts: tuple[CellReceipt, ...]
    receipt_catalog: tuple[tuple[Name, Digest], ...]
    known: Amount
    retained_unknown: Amount
    optimizer_projection: TrainProjection | None
    grants_authority: Literal[False]

    @model_validator(mode="after")
    def no_drop(self):
        require(tuple(r.cell for r in self.receipts) == self.expected_cells, "exact ordered full matrix")
        require(len(set(r.receipt_id for r in self.receipts)) == len(self.receipts), "contract invariant")
        require(all(r.plan_identity == self.plan_identity and r.evaluation_id == self.evaluation_id for r in self.receipts), "contract invariant")
        require(self.receipt_catalog == tuple((r.receipt_id, digest(r)) for r in self.receipts), "contract invariant")
        valid = all(r.disposition in ("scored", "task_failure") for r in self.receipts)
        unknown = any(r.disposition == "unknown" for r in self.receipts)
        require(self.status == ("complete" if valid else "unknown" if unknown else "invalid"), "contract invariant")
        require(self.known == total(r.known for r in self.receipts), "contract invariant")
        require(self.retained_unknown == total(r.retained_unknown for r in self.receipts), "contract invariant")
        if self.optimizer_projection is not None:
            require(all(r.case.split == "train" for r in self.receipts), "holdout has no optimizer delivery")
            require(self.optimizer_projection.evaluation_id == self.evaluation_id, "contract invariant")
        return self


def total(amounts) -> Amount:
    data = {k: 0 for k in Amount.model_fields}
    for amount in amounts:
        for k in data:
            data[k] += getattr(amount, k)
    return Amount(**data)


def benchmark_row(receipt: CellReceipt, benchmark: Benchmark) -> dict:
    """Exact accepted 0f0 ROW projection, called ONLY by trusted coordinator.

    No extra fields. Unknown cost stays null; execution=null remains in the
    catalog receipt. Complete BenchmarkStore.aggregate retains its 20-case meaning.
    """
    require(receipt.benchmark_definition == benchmark.definition, "contract invariant")
    valid = receipt.disposition in ("scored", "task_failure")
    return {
        "schema": "rsi.benchmark.v1", "benchmark_id": benchmark.benchmark_id,
        "version": benchmark.version, "bundle_identity": receipt.selection.native_bundle,
        "runtime": {"route": receipt.runtime_before.requested_model,
                    "endpoints": [receipt.runtime_before.endpoint.endpoint],
                    "effort": receipt.runtime_before.effort},
        "case_id": receipt.cell.case_id, "repetition": receipt.cell.repetition,
        "arm": receipt.cell.arm, "run_receipt_id": receipt.receipt_id,
        "input_identity": receipt.case.prompt_identity,
        "checker_identity": receipt.case.scorer_ref_identity,
        "validity": "VALID" if valid else "UNKNOWN_ACCOUNTING" if receipt.disposition == "unknown" else "INVALID",
        "outcome": receipt.disposition if valid else "none",
        "raw_points": receipt.checker.raw_points if valid and receipt.checker else None,
        "cost_usd": None if receipt.disposition == "unknown" else receipt.known.usd_micros / 1_000_000,
        "duration_s": None if receipt.elapsed_ms is None else receipt.elapsed_ms / 1000,
    }


def scorer_receipt(receipt: CellReceipt) -> dict:
    """Trusted adapter into unchanged core.pipeline_optimization.evaluate_matrix.

    Caller must validate actual observed full receipt against frozen request;
    the numeric library cannot establish the truth of identities or settlement.
    """
    valid = receipt.disposition in ("scored", "task_failure")
    score = (Fraction(receipt.checker.raw_points, receipt.checker.max_points) * 100
             if receipt.checker else None)
    return {"case_id": receipt.cell.case_id, "arm": receipt.cell.arm,
            "repetition": receipt.cell.repetition, "run_receipt_id": receipt.receipt_id,
            "bundle_id": receipt.selection.native_bundle, "runtime_id": digest(receipt.runtime_before),
            "checker_id": receipt.checker_package, "input_id": receipt.case.prompt_identity,
            "accounting": "settled" if valid else "invalid",
            "outcome": receipt.disposition if valid else "invalid", "score": score}


class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult | Refusal:
        """Host-bound typed entry point. No authority follows from request JSON."""
        ...


class NativeVersionProvider(Protocol):
    def read(self, version_id: str) -> dict: ...  # accepted NativeVersions.read


class BenchmarkProvider(Protocol):
    def get_definition(self, bid: str, version: int) -> dict: ...
    def get_private_refs(self, caller: str, bid: str, version: int) -> dict: ...
    def append_receipt(self, caller: str, row: dict) -> int: ...


class TrustedChecker(Protocol):
    def check(self, complete_output_snapshot: Digest, frozen_material: Digest) -> CheckerResult:
        """NEW seam: trusted programmatic code resolves full bytes, treats as data."""
        ...


class HostBinding(Protocol):
    """NEW host seam. It owns roots/providers/policy/budget, not optimizer JSON.

    Native proof must implement through ordinary registration/launch/drive,
    SDK claim/confirm/publication and gateway physical-send admission. It must
    not implement a shadow run/pin/finish lifecycle or invent settlement facts.
    """
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult | Refusal: ...


def validate_result(request: EvaluationRequest, result: EvaluationResult) -> None:
    """Pure final identity check before projection; establishes consistency, not truth."""
    require(result.evaluation_id == request.evaluation_id and result.plan_identity == request.plan_identity)
    require(result.expected_cells == request.cells)
    for receipt in result.receipts:
        require(receipt.campaign_id == request.campaign_id)
        require(receipt.case == next(c for c in request.benchmark.cases if c.case_id == receipt.cell.case_id))
        require(receipt.selection == next(s for s in request.selections if s.arm == receipt.cell.arm))
        require(receipt.runtime_before == request.runtime)
        require(receipt.benchmark_definition == request.benchmark.definition)
        require(receipt.public_package == request.benchmark.public_package)
        require(receipt.private_package == request.benchmark.private_package)
        require(receipt.checker_package == request.benchmark.checker_package)
    if request.split == "holdout":
        require(result.optimizer_projection is None)
    else:
        projection = result.optimizer_projection
        require(projection is not None and projection.status == result.status)
        cases = [c for c in request.benchmark.cases if c.split == "train"]
        require(tuple(cid for cid,prompt in projection.train_prompts) == tuple(c.case_id for c in cases))
        require(all(digest({"prompt":prompt}) == case.prompt_identity
                    for (cid,prompt),case in zip(projection.train_prompts,cases)))
        require(len(projection.cells) == len(result.receipts))
        for public,receipt in zip(projection.cells,result.receipts):
            require((public.case_id,public.arm,public.repetition,public.status) ==
                    (receipt.cell.case_id,receipt.cell.arm,receipt.cell.repetition,receipt.disposition))
            expected = (str(Fraction(receipt.checker.raw_points, receipt.checker.max_points) * 100)
                        if receipt.checker else "0" if receipt.disposition == "task_failure" else None)
            require(public.normalized_score == expected)
