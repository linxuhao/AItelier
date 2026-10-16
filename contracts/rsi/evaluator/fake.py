"""Standalone deterministic semantic model, NEVER evidence of native enforcement.

No SDK lifecycle is fabricated: execution IDs below are explicitly simulation
IDs. Native fixture binding must obtain IDs and settlement from the SDK instead.
"""
from __future__ import annotations

import copy
import json
import threading
from fractions import Fraction
from pathlib import Path

from .contract import (Amount, Benchmark, Budget, Cell, CellReceipt, CheckerResult,
                       Endpoint, Envelope, EvaluationRequest, EvaluationResult,
                       Execution, PhysicalAttempt, PublicCell, Refusal, Runtime,
                       Selection, TrainProjection, digest, total)

FIXTURES = Path(__file__).with_name("fixtures")
ZERO = Amount(requests=0, input_tokens=0, output_tokens=0, usd_micros=0)
BOUND = Amount(requests=1, input_tokens=100, output_tokens=20, usd_micros=2000)
USAGE = Amount(requests=1, input_tokens=80, output_tokens=10, usd_micros=1000)


class BudgetDenied(Exception):
    """Nonretryable. A native binding must preserve this through ALL failover paths."""
    retryable = False


class FakeReservations:
    """In-memory MODEL of atomic scope admission, no provider billing assertion."""
    def __init__(self, limits: dict[str, Envelope]):
        self.limits = limits
        self.used = {key: ZERO for key in limits}
        self.closed = False
        self.lock = threading.Lock()

    def reserve(self, scopes: tuple[str, ...], upper: Amount, elapsed_ms=0):
        with self.lock:
            if self.closed or any(elapsed_ms >= self.limits[s].elapsed_ms or any(
                    getattr(self.used[s], k) + getattr(upper, k) > getattr(self.limits[s].upper, k)
                    for k in Amount.model_fields) for s in scopes):
                raise BudgetDenied("BUDGET_EXHAUSTED")
            for s in scopes:
                self.used[s] = total((self.used[s], upper))

    def settle(self, scopes, reserved, measured):
        with self.lock:
            if measured is None:
                self.closed = True  # unknown reservations retained, no new dispatch
                return
            for s in scopes:
                self.used[s] = Amount(**{k: getattr(self.used[s], k) - getattr(reserved, k)
                                       + getattr(measured, k) for k in Amount.model_fields})


def load_fixture():
    return {"unit": json.loads((FIXTURES / "unit.json").read_text()),
            "benchmark": json.loads((FIXTURES / "benchmark.json").read_text()),
            "identity": json.loads((FIXTURES / "identity.json").read_text())}


class FakeFixture:
    """Fixture-control interface shared with future genuine host fixture binding.

    set_fault modifies this model's owning inputs. The real binding must modify
    the actual corresponding fixture input/provider/transport and observe it,
    never patch the production method being asserted into the desired answer.
    """
    evidence_kind = "simulation"

    def __init__(self):
        self.fixture = load_fixture()
        self.unit = copy.deepcopy(self.fixture["unit"])
        self.grants = {"list_tree", "dir_tree", "read", "search", "list", "write_findings"}
        self.root_kind = "public-only"
        self.extra_templates = ()
        self.provider = "recorded"
        self.public_package = self.fixture["benchmark"]["companion"]["public_package"]
        self.private_package = self.fixture["benchmark"]["companion"]["private_package"]
        self.fault = "none"
        self.authorized = True
        self.launches = self.sends = self.rows = self.denials = 0
        self.receipts = []
        self.request = None

    def prepare(self, split="train", arms=("reference", "candidate")):
        # Identities here identify literal simulated providers, never native seals.
        bench = Benchmark.model_validate_json(json.dumps(self.fixture["benchmark"]["companion"]))
        runtime = Runtime(
            app_source="c7f9123f66e6a64ba64bfb08f4b6df25e56e1e0e",
            image="sha256:9e384ae465e311d07342f6a776f62ff7c3c036d0582858529a4de61111a30694",
            sdk_version="1.5.94",
            sdk_wheel="sha256:dd403024cd5a893a71c852afdb2dcf175b9c91bf8eaec12f0544ad387ca62db9",
            routes=digest("fixture-routes"), pricing=digest("fixture-pricing-not-live"),
            policy=digest("finite-pilot-policy-v2"),
            endpoint=Endpoint(provider="ark", model="deepseek-v4-flash", endpoint="ark-coding-v3"),
            requested_model="ark/deepseek-v4-flash", effort="high", temperature_micros=200000,
            max_output_tokens=8192, max_tool_turns=32, cell_timeout_ms=300000,
            transport_evidence=digest("simulation-no-real-transport-proof"))
        self.actual_runtime = runtime
        selections = []
        for arm in arms:
            vid = self.fixture["identity"][arm]["logical_version"]
            selections.append(Selection(
                arm=arm, logical_version=vid, native_bundle=digest(["simulated-native-bundle", arm]),
                native_seal=digest(["simulated-seal", arm]),
                physical_config="gen_rsi_v" + vid[7:38] + "_" + digest(arm)[7:23],
                sdk_graph_version=1, sdk_graph_digest=digest(["simulated-sdk-graph", arm]),
                roles=digest(self.unit["roles"]), templates=digest(()),
                tools=digest("simulated-tool-providers"), capabilities=digest(())))
        ceiling = Envelope(upper=Amount(requests=1000, input_tokens=16000000,
                                       output_tokens=2000000, usd_micros=25000000), elapsed_ms=14400000)
        budget = Budget(scope_id="fixture-scope", authorization_ref="fixture-authorized-host-only",
                        pricing_identity=runtime.pricing,
                        cell=Envelope(upper=Amount(requests=8, input_tokens=128000,
                                                  output_tokens=16000, usd_micros=1000000), elapsed_ms=300000),
                        round=ceiling, campaign=ceiling)
        cells = tuple(Cell(case_id=c.case_id, arm=a, repetition=r)
                      for r in range(1, 4) for c in bench.cases if c.split == split for a in arms)
        data = dict(schema="rsi.evaluation.request.v2", evaluation_id="fixture-evaluation",
                    campaign_id="fixture-campaign", round_id="fixture-round", split=split,
                    repetitions=3, max_parallel=2, selections=tuple(selections), benchmark=bench,
                    cells=cells, runtime=runtime, budget=budget,
                    holdout_selection=digest("host-sealed-holdout-selection") if split == "holdout" else None)
        self.request = EvaluationRequest(plan_identity=digest(data), **data)
        return self.request

    def set_fault(self, name):
        allowed = {s["fault"] for s in json.loads((FIXTURES / "scenarios.json").read_text())["scenarios"]}
        if name not in allowed:
            raise ValueError("unknown fixture fault")
        self.fault = name
        step = self.unit["graph"]["steps"][0]
        if name == "empty_grants": self.grants = set()
        if name == "missing_grants": self.grants = None
        if name == "hidden_source_tool": step["context"].append({"source": {"tool": "private_reader"}})
        if name == "private_root": self.root_kind = "benchmark-parent"
        if name == "cross_run": step["context"].append({"source": {"config": "other", "output": "private.json"}})
        if name == "dynamic_capability": step["capability"] = {"from_item": "capabilities"}
        if name == "hook": step["validation"] = [{"tool": "private_reader"}]
        if name == "extra_template": self.extra_templates = ("unrecorded.md",)
        if name == "provider_changed": self.provider = "changed"
        if name == "provider_absent": self.provider = None
        if name == "fixture_changed": self.public_package = digest("changed-public")
        if name == "checker_changed": self.private_package = digest("changed-private")
        if name == "runtime_preflight":
            self.actual_runtime = self.actual_runtime.model_copy(update={"max_output_tokens":4096})

    def observations(self):
        return {"evidence_kind": self.evidence_kind, "launches": self.launches,
                "physical_sends": self.sends, "scoreboard_rows": self.rows,
                "denials": self.denials, "retained_receipts": len(self.receipts)}

    def _refuse(self, request, reason):
        self.denials += 1
        return Refusal(schema="rsi.evaluation.refusal.v2", evaluation_id=request.evaluation_id,
                       reason=reason, before_launch=True, execution=None,
                       scoreboard_mutated=False, retryable=False)

    def _preflight(self, request):
        if not self.authorized: return "POLICY_REFUSED"
        if self.fault == "unsafe_resume": return "UNSAFE_RESUME"
        if self.fault == "unknown_version" or request.selections != self.request.selections: return "UNKNOWN_ID"
        if self.provider is None: return "PROVIDER_UNAVAILABLE"
        if self.provider != "recorded": return "PROVIDER_CHANGED"
        if self.actual_runtime != request.runtime: return "RUNTIME_DRIFT"
        if self.public_package != request.benchmark.public_package: return "INPUT_MISMATCH"
        if self.private_package != request.benchmark.private_package: return "CHECKER_MISMATCH"
        # Finite shape, exact effective fixture graph including source tools and hooks.
        if self.unit != self.fixture["unit"] or self.extra_templates or self.root_kind != "public-only":
            return "POLICY_REFUSED"
        if self.grants != {"list_tree", "dir_tree", "read", "search", "list", "write_findings"}:
            return "POLICY_REFUSED"
        if request != self.request: return "UNKNOWN_ID"
        return None

    def evaluate(self, request):
        if not isinstance(request, EvaluationRequest):
            raise TypeError("typed EvaluationRequest required; malformed JSON must parse before effects")
        reason = self._preflight(request)
        if reason: return self._refuse(request, reason)
        limits = {"round":request.budget.round, "campaign":request.budget.campaign}
        limits.update({f"cell-{i}":request.budget.cell for i in range(len(request.cells))})
        admission = FakeReservations(limits)
        stopped = False
        for index, cell in enumerate(request.cells):
            case = next(c for c in request.benchmark.cases if c.case_id == cell.case_id)
            selection = next(s for s in request.selections if s.arm == cell.arm)
            reason = "ABORTED" if stopped else "NONE"
            state = "not_started" if stopped else "scored"
            execution = None
            attempts = []
            check = None
            elapsed = None
            envelope = False
            if not stopped:
                self.launches += 1
                state = "scored"
                execution = Execution(provenance="simulation", project_id=f"fake:project-{index}",
                                      run_id=f"fake:run-{index}", step_instance_ids=(f"fake:step-{index}",),
                                      sdk_graph_version=selection.sdk_graph_version,
                                      sdk_graph_digest=selection.sdk_graph_digest,
                                      state="completed", operations_settled=True)
                causes = ["initial"]
                if index == 0 and self.fault in {"repair", "retry", "transport_retry"}:
                    causes += [{"repair":"format_repair", "retry":"retry", "transport_retry":"transport_retry"}[self.fault]] * 2
                if index == 0 and self.fault in {"runtime_midcell", "fallback"}:
                    causes += ["format_repair" if self.fault == "runtime_midcell" else "fallback"]
                for number, cause in enumerate(causes):
                    scopes = (f"cell-{index}", "round", "campaign")
                    effective = request.runtime
                    if number and self.fault in {"runtime_midcell", "fallback"}:
                        # Effective mismatch is observed BEFORE another physical send.
                        reason, state, stopped = "RUNTIME_DRIFT", "invalid", True
                        self.denials += 1
                        break
                    try:
                        admission.reserve(scopes, BOUND)
                    except BudgetDenied:
                        reason, state, stopped, envelope = "BUDGET_EXHAUSTED", "invalid", True, True
                        self.denials += 1
                        break
                    self.sends += 1
                    unknown = index == 0 and self.fault in {"unknown_usage", "unknown_execution"}
                    measured = None if unknown else USAGE
                    admission.settle(scopes, BOUND, measured)
                    attempts.append(PhysicalAttempt(attempt_id=f"p-{index}-{number}", cause=cause,
                                                    effective_runtime=effective, reservation=BOUND,
                                                    settlement="unknown" if unknown else "known",
                                                    measured=measured, observed_send=True))
                    if unknown:
                        reason = "UNKNOWN_ACCOUNTING" if self.fault == "unknown_usage" else "UNKNOWN_EXECUTION"
                        state, stopped = "unknown", True
                        execution = execution.model_copy(update={"state":"unknown", "operations_settled":False})
                        break
                elapsed = 1
                if index == 0 and self.fault == "envelope":
                    state, reason, stopped, envelope = "invalid", "BUDGET_EXHAUSTED", True, True
                if index == 0 and self.fault == "checkpoint":
                    state, reason, stopped = "invalid", "UNEXPECTED_CHECKPOINT", True
                    execution = execution.model_copy(update={"state":"checkpoint"})
                if index == 0 and self.fault == "task_failure":
                    state = "task_failure"
                    execution = execution.model_copy(update={"state":"failed"})
                if state == "scored":
                    check = CheckerResult(output_snapshot=digest({"findings.md":"SIMULATED full immutable output\n"}),
                                          complete_files=("findings.md",), checker_package=request.benchmark.checker_package,
                                          resolved_private_material=case.resolved_private_material,
                                          private_result=digest(["simulated-checker-result", cell.model_dump()]),
                                          raw_points=5 if cell.arm == "reference" else 6, max_points=case.max_points)
            receipt = CellReceipt(
                schema="rsi.cell.receipt.v2", receipt_id=f"receipt-{index:03d}",
                evaluation_id=request.evaluation_id, campaign_id=request.campaign_id,
                plan_identity=request.plan_identity, cell=cell, case=case, selection=selection,
                benchmark_definition=request.benchmark.definition, public_package=request.benchmark.public_package,
                private_package=request.benchmark.private_package, checker_package=request.benchmark.checker_package,
                runtime_before=request.runtime, runtime_after=request.runtime if execution else None,
                execution=execution, disposition=state, reason=reason, checker=check,
                attempts=tuple(attempts), elapsed_ms=elapsed,
                known=total(a.measured for a in attempts if a.measured is not None),
                retained_unknown=total(a.reservation for a in attempts if a.settlement == "unknown"),
                envelope_exceeded=envelope)
            self.receipts.append(receipt)
            self.rows += 1  # MODEL only; real binding must append to actual BenchmarkStore
        status = "complete" if not stopped else "unknown" if any(r.disposition == "unknown" for r in self.receipts) else "invalid"
        public = None
        if request.split == "train":
            public = TrainProjection(schema="rsi.optimizer.train.v2", evaluation_id=request.evaluation_id,
                                     status=status, train_prompts=tuple((c.case_id,self.fixture["benchmark"]["public"][c.case_id]["prompt"])
                                                                       for c in request.benchmark.cases if c.split == "train"),
                                     cells=tuple(PublicCell(case_id=r.cell.case_id, arm=r.cell.arm,
                                                           repetition=r.cell.repetition, status=r.disposition,
                                                           normalized_score=str(Fraction(r.checker.raw_points, r.checker.max_points) * 100)
                                                           if r.checker else "0" if r.disposition == "task_failure" else None)
                                                 for r in self.receipts), grants_authority=False)
        receipts = tuple(self.receipts)
        return EvaluationResult(schema="rsi.evaluation.result.v2", evaluation_id=request.evaluation_id,
                                plan_identity=request.plan_identity, status=status, expected_cells=request.cells,
                                receipts=receipts, receipt_catalog=tuple((r.receipt_id,digest(r)) for r in receipts),
                                known=total(r.known for r in receipts), retained_unknown=total(r.retained_unknown for r in receipts),
                                optimizer_projection=public, grants_authority=False)

    def probe_admission(self, vector):
        """Shared vectors over MODEL admission, no claimed physical network send."""
        from concurrent.futures import ThreadPoolExecutor
        one = Envelope(upper=BOUND, elapsed_ms=10)
        admission = FakeReservations({'a':one, 'b':one, 'campaign':one})
        admitted = denied = 0
        kind = vector['kind']
        if kind == 'concurrent':
            barrier = threading.Barrier(2)
            def attempt(cell):
                barrier.wait()
                try: admission.reserve((cell,'campaign'),BOUND); return True
                except BudgetDenied: return False
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, ('a','b')))
            admitted, denied = sum(results), 2-sum(results)
            loser = 'a' if not results[0] else 'b'
            if admission.used[loser] != ZERO:
                raise AssertionError('failed reservation partially mutated scope')
        elif kind == 'unknown':
            admission.reserve(('a','campaign'),BOUND); admitted += 1
            admission.settle(('a','campaign'),BOUND,None)
            try: admission.reserve(('b','campaign'),BOUND); admitted += 1
            except BudgetDenied: denied += 1
        elif kind == 'deadline':
            try: admission.reserve(('a','campaign'),BOUND,elapsed_ms=10); admitted += 1
            except BudgetDenied: denied += 1
        elif kind == 'no_send':
            for cell in ('a','b'):
                admission.reserve((cell,'campaign'),BOUND); admitted += 1
                admission.settle((cell,'campaign'),BOUND,ZERO)
        else:
            raise ValueError('unknown admission vector')
        return {'evidence_kind':'simulation', 'admitted':admitted, 'denied':denied,
                'retained_requests':admission.used['campaign'].requests,
                'physical_sends':0, 'denial_retryable':False}
