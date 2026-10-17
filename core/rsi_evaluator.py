"""Trusted RSI evaluation coordinator over ordinary host/SDK runs.

``NativeEvaluator(host).evaluate(EvaluationRequest)`` returns the contract's
``EvaluationResult`` or a before-launch ``Refusal``. Authority, providers, roots
and private material come from the trusted ``EvaluatorHost`` built by the
application, never from request JSON.

Preflight has no effects and refuses in a fixed order: authorization and
one-use holdout seal; unsafe resume of an evaluation that started without its
retained live scope; native version identities (accepted versioning catalog
and seals); sealed tool/capability providers; the profile's model provider;
benchmark definition, public package, private package and trusted checker
identities; the compiled finite policy (``core.rsi_policy``); the full served
runtime profile; and finally that the served physical configs are exactly the
sealed selections.

Each planned cell then runs once, in plan order: a public fixture repository
pinned at an exact commit, an ordinary ``start_config_run`` of the sealed
physical config (read snapshot isolation, published seed), a trusted in-process
binding (``core.rsi_admission``) for ingress/dispatch/physical admission, and
the existing non-scheduler driver ``core.run_driver.drive_run`` without
checkpoint auto-approval. Settlement, output, accounting and runtime facts are
read back from the SDK, the workspace and the admission scope. The complete
delivered ``findings.md`` is snapshotted immutably and scored by
``core.rsi_checker`` inside this process. The first invalid/unknown cell stops
new dispatch; remaining cells are recorded as not-started/ABORTED. Every cell's
strict row goes through the accepted ``BenchmarkStore``. Nothing here selects
or adopts a version.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from contracts.rsi.evaluator.contract import (
    CellReceipt, CheckerResult, Endpoint, EvaluationRequest, EvaluationResult,
    Execution, PublicCell, Refusal, Runtime, TrainProjection, benchmark_row, digest, total,
    validate_result)
from core import rsi_admission as admission
from core import rsi_checker as checker
from core import rsi_policy as policy_mod

_LIVE: dict[str, "NativeEvaluator"] = {}
_LIVE_LOCK = threading.Lock()
ZERO = admission.ZERO


class _Refuse(Exception):
    def __init__(self, reason: str, detail: str):
        self.reason, self.detail = reason, detail
        super().__init__(f"{reason}: {detail}")


@dataclass
class EvaluatorHost:
    """Trusted host handles. Built by application/binding code only."""
    sf: Any
    registry: Any
    db: Any
    ws: Any
    versions: Any                      # accepted NativeVersions(-compatible).read
    config_dir: Path                   # generated configs dir holding the seals
    store: Any                         # accepted core.pipeline_benchmark.BenchmarkStore
    store_owner: str
    public_package: Path               # public companion package (prompts + fixture files)
    private_package: Path              # private refs + resolved material (checker-only)
    public_fixture_root: Path          # per-cell public read repositories
    artifacts_root: Path               # private evaluator artifacts (receipts, snapshots)
    grants: dict                       # authorization_ref -> host-owned grant
    deployment: dict                   # app_source, image, sdk_wheel (deployment identity)
    endpoints: dict                    # nonsecret label -> {provider, model, base_url}
    endpoint_label: str
    providers_path: Path               # served provider registry handed to gateways
    routes_path: Path                  # served route table handed to gateways
    pricing: admission.Pricing
    transport: Callable[[], Any]       # inner httpx transport factory (real or fixture)
    cell_timeout_ms: int
    clock: Callable[[], float] = time.monotonic


@dataclass
class CellBinding:
    """Everything the host ingress/dispatch/gateway may ask about one cell run."""
    scope: admission.CellScope
    policy: policy_mod.CellPolicy
    marker: dict
    ingress_denials: list = field(default_factory=list)
    driver: int | None = None          # thread ident of the coordinator's driver


def transport_identity() -> str:
    from core import pipeline_frozen_bundle as fb
    return digest([fb._served_identity(obj, "physical admission")["sha256"] for obj in (
        admission.AdmissionTransport, admission._SettlingStream, admission.CellScope,
        admission.PhysicalBudget)])


def _git(cwd, *args) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="rsi-evaluator", GIT_AUTHOR_EMAIL="rsi-evaluator@localhost",
               GIT_COMMITTER_NAME="rsi-evaluator", GIT_COMMITTER_EMAIL="rsi-evaluator@localhost",
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
               GIT_CONFIG_NOSYSTEM="1", HOME=str(cwd))
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def _write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"immutable artifact {path} already holds different bytes")
        return
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _drive(make_coro, binding):
    """Run the ordinary driver on ONE dedicated thread; the host ingress admits
    claim/advance of the cell run from that thread only."""
    box: dict = {}

    def runner():
        binding.driver = threading.get_ident()
        try:
            box["value"] = asyncio.run(make_coro())
        except BaseException as exc:                         # noqa: BLE001
            box["error"] = exc
        finally:
            binding.driver = -1                              # nobody drives it now
    t = threading.Thread(target=runner, name="rsi-cell-driver")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


class NativeEvaluator:
    """The one trusted coordinator. Holds no run/pin/operation ledger."""

    def __init__(self, host: EvaluatorHost):
        self.host = host
        self.bindings: dict[str, CellBinding] = {}

    # ── identities ───────────────────────────────────────────────────────
    def _evaluation_dir(self, evaluation_id: str) -> Path:
        return self.host.artifacts_root / "evaluations" / evaluation_id

    def _gateway_profile(self, role_name: str):
        """Build the gateway the runner would build for this role, with the
        frozen provider registry/route table, and read its effective settings.
        Construction sends nothing."""
        from core.agents import AgentFactory
        factory = AgentFactory(registry=self.host.sf.agent_registry)
        factory.gateway_paths = (str(self.host.providers_path), str(self.host.routes_path))
        gateway = factory._build_gateway(role_name)
        return gateway, factory._get_config(role_name).get("config", {})

    def served_runtime(self, compiled: dict, policy_identity: str) -> Runtime:
        """The full runtime profile this host would actually serve now."""
        import importlib.metadata
        h = self.host
        entry = h.endpoints.get(h.endpoint_label)
        if not isinstance(entry, dict):
            raise _Refuse("PROVIDER_UNAVAILABLE", f"no endpoint {h.endpoint_label!r}")
        try:
            providers = json.loads(Path(h.providers_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _Refuse("PROVIDER_UNAVAILABLE", f"provider registry unreadable: {exc}")
        served = providers.get(entry["provider"]) if isinstance(providers, dict) else None
        if not isinstance(served, dict):
            raise _Refuse("PROVIDER_UNAVAILABLE", f"provider {entry['provider']!r} is not served")
        if served.get("base_url") != entry["base_url"]:
            raise _Refuse("PROVIDER_CHANGED", f"provider {entry['provider']!r} endpoint changed")
        profiles = set()
        for arm in compiled.values():
            gateway, cfg = self._gateway_profile(arm["role"])
            concrete = f"{entry['provider']}/{entry['model']}"
            if gateway.active_model != concrete:
                raise _Refuse("RUNTIME_DRIFT", f"gateway binds {gateway.active_model}, not {concrete}")
            effort = gateway._binding_effort() if gateway.enable_thinking else "disabled"
            if effort == admission.PROVIDER_DEFAULT_EFFORT:
                raise _Refuse("RUNTIME_DRIFT", "a configured effort may not spell the omission pin")
            if gateway.enable_thinking and effort is None:
                # Thinking on, no effort transmitted: pin that omission, never a level.
                effort = admission.PROVIDER_DEFAULT_EFFORT
            temperature = cfg.get("temperature", 0.2)
            profiles.add(json.dumps({
                "requested_model": gateway.internal_model, "effort": effort,
                "temperature_micros": int(round(float(temperature) * 1_000_000)),
                "max_output_tokens": int(gateway.max_output_tokens),
                "max_tool_turns": int(cfg.get("max_tool_turns", 10))}, sort_keys=True))
        if len(profiles) != 1:
            raise _Refuse("RUNTIME_DRIFT", "arms would run under different runtime settings")
        p = json.loads(profiles.pop())
        routes = Path(h.routes_path).read_bytes() if Path(h.routes_path).is_file() else b""
        try:
            return Runtime(
                app_source=h.deployment["app_source"], image=h.deployment["image"],
                sdk_version=importlib.metadata.version("skillflow-py"),
                sdk_wheel=h.deployment["sdk_wheel"],
                routes="sha256:" + hashlib.sha256(routes).hexdigest(),
                pricing=h.pricing.identity, policy=policy_identity,
                endpoint=Endpoint(provider=entry["provider"], model=entry["model"],
                                  endpoint=h.endpoint_label),
                cell_timeout_ms=h.cell_timeout_ms,
                transport_evidence=transport_identity(), **p)
        except ValueError as exc:
            raise _Refuse("RUNTIME_DRIFT", f"served runtime outside the contract domain: {exc}")

    def _packages(self) -> tuple[dict, dict]:
        try:
            public = json.loads(Path(self.host.public_package).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _Refuse("INPUT_MISMATCH", f"public package unreadable: {exc}")
        try:
            private = json.loads(Path(self.host.private_package).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise _Refuse("CHECKER_MISMATCH", f"private package unreadable: {exc}")
        return public, private

    def compile(self, request: EvaluationRequest):
        """(CompiledPolicy, per-arm compilation, sealed units). No effects."""
        return self.compile_for(request.selections, request.budget.authorization_ref,
                                request.runtime.requested_model,
                                request.benchmark.public_package)

    def compile_for(self, selections, authorization_ref: str, requested_model: str,
                    public_package: str):
        from core import pipeline_frozen_bundle as fb
        grant = self.host.grants.get(authorization_ref) or {}
        sealed = {}
        for s in selections:
            try:
                sealed[s.arm] = fb.verify_sealed(self.host.config_dir, s.physical_config)
            except fb.BundleRefused as exc:
                raise _Refuse("UNKNOWN_ID", str(exc))
        private_roots = [Path(self.host.private_package).parent, self.host.artifacts_root]
        try:
            return (*policy_mod.compile_policy(
                self.host.sf, self.host.registry, selections, sealed,
                grants=grant.get("tools"), requested_model=requested_model,
                public_root=self.host.public_fixture_root, private_roots=private_roots,
                public_fixture=public_package), sealed)
        except policy_mod.PolicyRefused as exc:
            raise _Refuse("POLICY_REFUSED", str(exc))
        except fb.BundleRefused as exc:
            raise _Refuse("PROVIDER_UNAVAILABLE" if "unresolved" in str(exc) else
                          "PROVIDER_CHANGED", str(exc))

    # ── preflight (no effects) ───────────────────────────────────────────
    def _preflight(self, request: EvaluationRequest) -> dict:
        from core import pipeline_frozen_bundle as fb
        h = self.host
        grant = h.grants.get(request.budget.authorization_ref)
        if (not isinstance(grant, dict) or grant.get("scope_id") != request.budget.scope_id
                or grant.get("campaign_id") != request.campaign_id):
            raise _Refuse("POLICY_REFUSED", "no host authorization for this budget scope")
        if request.split == "holdout":
            seal = request.holdout_selection
            if seal not in (grant.get("holdout_selections") or ()):
                raise _Refuse("POLICY_REFUSED", "holdout needs a host-sealed selection record")
            if (h.artifacts_root / "holdout-consumed" / (seal[7:] + ".json")).exists():
                raise _Refuse("POLICY_REFUSED", "holdout selection already consumed")
        edir = self._evaluation_dir(request.evaluation_id)
        if (edir / "started.json").exists():
            with _LIVE_LOCK:
                live = request.evaluation_id in _LIVE
            if (edir / "result.json").exists():
                raise _Refuse("UNKNOWN_ID", "evaluation id already sealed")
            raise _Refuse("UNSAFE_RESUME", "evaluation started without its retained live scope"
                          if not live else "evaluation already running")

        for s in request.selections:
            try:
                read = h.versions.read(s.logical_version)
            except Exception as exc:                                   # noqa: BLE001
                raise _Refuse("UNKNOWN_ID", f"{type(exc).__name__}: {exc}")
            native = read.get("native") or {}
            if (native.get("physical_name") != s.physical_config
                    or "sha256:" + str(native.get("bundle_identity")) != s.native_bundle
                    or "sha256:" + str(native.get("digest")) != s.native_seal
                    or read.get("version_id") != s.logical_version):
                raise _Refuse("UNKNOWN_ID", f"{s.arm} native identity does not match the catalog")
        sealed = {}
        for s in request.selections:
            try:
                sealed[s.arm] = fb.verify_sealed(h.config_dir, s.physical_config)
            except fb.BundleRefused as exc:
                raise _Refuse("UNKNOWN_ID", str(exc))
            unit = sealed[s.arm]
            if (digest(unit["roles"]) != s.roles or digest(unit["prompts"]) != s.templates
                    or digest(unit["tools"]) != s.tools or digest(unit["capabilities"]) != s.capabilities):
                raise _Refuse("UNKNOWN_ID", f"{s.arm} sealed unit identities differ")
            try:
                fb.verify_providers(h.sf, unit)
            except fb.BundleRefused as exc:
                raise _Refuse("PROVIDER_UNAVAILABLE" if str(exc).startswith("unresolved")
                              else "PROVIDER_CHANGED", str(exc))
        entry = h.endpoints.get(h.endpoint_label) or {}
        try:
            providers = json.loads(Path(h.providers_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            providers = {}
        if not isinstance(providers, dict) or entry.get("provider") not in providers:
            raise _Refuse("PROVIDER_UNAVAILABLE", "the profile's model provider is not served")

        bench = request.benchmark
        try:
            definition = h.store.get_definition(bench.benchmark_id, bench.version)
            refs = h.store.get_private_refs(h.store_owner, bench.benchmark_id, bench.version)
        except Exception as exc:                                       # noqa: BLE001
            raise _Refuse("UNKNOWN_ID", f"benchmark definition: {exc}")
        if definition.get("definition_digest") != bench.definition or definition.get("status") != "published":
            raise _Refuse("INPUT_MISMATCH", "benchmark definition differs")
        public, private = self._packages()
        if digest(public) != bench.public_package:
            raise _Refuse("INPUT_MISMATCH", "public fixture package bytes differ")
        if digest(private) != bench.private_package:
            raise _Refuse("CHECKER_MISMATCH", "private checker package bytes differ")
        for case in bench.cases:
            pub, prv = public.get(case.case_id) or {}, private.get(case.case_id) or {}
            served = definition["cases"].get(case.case_id) or {}
            if (digest({"prompt": pub.get("prompt")}) != case.prompt_identity
                    or digest(pub.get("files")) != case.fixture_tree
                    or served.get("input_identity") != case.prompt_identity
                    or served.get("partition") != case.split):
                raise _Refuse("INPUT_MISMATCH", f"case {case.case_id} public identity differs")
            if (digest(prv.get("refs")) != case.scorer_ref_identity
                    or refs.get(case.case_id) != prv.get("refs")
                    or served.get("checker_identity") != case.scorer_ref_identity
                    or digest(prv.get("material")) != case.resolved_private_material
                    or (prv.get("refs") or {}).get("max_points") != case.max_points):
                raise _Refuse("CHECKER_MISMATCH", f"case {case.case_id} scorer identity differs")
        code = checker.identity()
        if code != bench.checker_code or checker.package_identity(code, bench.cases) != bench.checker_package:
            raise _Refuse("CHECKER_MISMATCH", "trusted checker code/package differs")

        policy, compiled, _ = self.compile(request)
        runtime = self.served_runtime(compiled, policy.identity)
        if runtime != request.runtime:
            diff = sorted(k for k in Runtime.model_fields
                          if getattr(runtime, k) != getattr(request.runtime, k))
            raise _Refuse("RUNTIME_DRIFT", f"served runtime differs in {diff}")
        for s in request.selections:
            arm = compiled[s.arm]
            if (arm["graph_version"], arm["graph_digest"]) != (s.sdk_graph_version, s.sdk_graph_digest):
                raise _Refuse("PROVIDER_CHANGED", f"{s.arm} served SDK graph differs from selection")
            blocked = fb.launch_block(s.physical_config, h.config_dir, sf=h.sf, registry=h.registry)
            if blocked:
                raise _Refuse("PROVIDER_CHANGED", blocked)
        return {"policy": policy, "compiled": compiled, "public": public, "private": private,
                "grant": grant}

    def _refusal(self, request, reason: str, detail: str) -> Refusal:
        refusal = Refusal(schema="rsi.evaluation.refusal.v2",
                          evaluation_id=getattr(request, "evaluation_id", None), reason=reason,
                          before_launch=True, execution=None, scoreboard_mutated=False,
                          retryable=False)
        record = {"refusal": refusal.model_dump(mode="json"), "detail": detail[:2000],
                  "plan_identity": getattr(request, "plan_identity", None)}
        _write_once(self.host.artifacts_root / "refusals" / f"{uuid.uuid4().hex}.json",
                    json.dumps(record, sort_keys=True, indent=1))
        return refusal

    # ── evaluation ───────────────────────────────────────────────────────
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult | Refusal:
        if not isinstance(request, EvaluationRequest):
            raise TypeError("typed EvaluationRequest required; malformed JSON must parse before effects")
        try:
            ctx = self._preflight(request)
        except _Refuse as refused:
            return self._refusal(request, refused.reason, refused.detail)
        edir = self._evaluation_dir(request.evaluation_id)
        with _LIVE_LOCK:
            if request.evaluation_id in _LIVE or (edir / "started.json").exists():
                return self._refusal(request, "UNSAFE_RESUME", "evaluation already started")
            _LIVE[request.evaluation_id] = self
        try:
            return self._execute(request, ctx, edir)
        finally:
            with _LIVE_LOCK:
                _LIVE.pop(request.evaluation_id, None)

    def begin(self, request: EvaluationRequest) -> Path:
        """Durable start marker: an evaluation past this point that loses its
        process cannot be resumed (its live scopes were in-process only)."""
        edir = self._evaluation_dir(request.evaluation_id)
        _write_once(edir / "started.json", json.dumps({
            "evaluation_id": request.evaluation_id, "plan_identity": request.plan_identity,
            "pid": os.getpid()}, sort_keys=True))
        return edir

    def _execute(self, request, ctx, edir) -> EvaluationResult:
        h = self.host
        self.begin(request)
        if request.split == "holdout":
            _write_once(h.artifacts_root / "holdout-consumed" / (request.holdout_selection[7:] + ".json"),
                        json.dumps({"evaluation_id": request.evaluation_id}))
        b = request.budget
        budget = admission.PhysicalBudget({"round": (b.round.upper, b.round.elapsed_ms),
                                           "campaign": (b.campaign.upper, b.campaign.elapsed_ms)},
                                          clock=h.clock)
        t0 = h.clock()
        started = {"round": t0, "campaign": t0}
        receipts, stopped = [], False
        tag = digest([request.evaluation_id, request.plan_identity])[7:23]
        for index, cell in enumerate(request.cells):
            if stopped or budget.closed:
                receipt = self._not_started(request, index, cell, tag, "ABORTED")
            else:
                receipt = self._run_cell(request, ctx, index, cell, tag, budget, started)
                stopped = receipt.disposition not in ("scored", "task_failure")
            receipts.append(receipt)
            h.store.append_receipt(h.store_owner, benchmark_row(receipt, request.benchmark))
            _write_once(edir / "receipts" / f"{receipt.receipt_id}.json",
                        receipt.model_dump_json(indent=1))
        receipts = tuple(receipts)
        valid = all(r.disposition in ("scored", "task_failure") for r in receipts)
        status = "complete" if valid else (
            "unknown" if any(r.disposition == "unknown" for r in receipts) else "invalid")
        projection = None
        if request.split == "train":
            projection = self._train_projection(request, ctx["public"], receipts, status)
        result = EvaluationResult(
            schema="rsi.evaluation.result.v2", evaluation_id=request.evaluation_id,
            plan_identity=request.plan_identity, status=status, expected_cells=request.cells,
            receipts=receipts, receipt_catalog=tuple((r.receipt_id, digest(r)) for r in receipts),
            known=total(r.known for r in receipts),
            retained_unknown=total(r.retained_unknown for r in receipts),
            optimizer_projection=projection, grants_authority=False)
        validate_result(request, result)
        for run_id, binding in list(self.bindings.items()):
            if not binding.scope.pending:
                admission.unbind_run(run_id, binding)
                del self.bindings[run_id]
        _write_once(edir / "admission-facts.json", json.dumps(budget.facts(), indent=1))
        _write_once(edir / "result.json", result.model_dump_json(indent=1))
        return result

    def _train_projection(self, request, public, receipts, status) -> TrainProjection:
        train = [c for c in request.benchmark.cases if c.split == "train"]
        cells = []
        for r in receipts:
            score = (str(Fraction(r.checker.raw_points, r.checker.max_points) * 100) if r.checker
                     else "0" if r.disposition == "task_failure" else None)
            cells.append(PublicCell(case_id=r.cell.case_id, arm=r.cell.arm,
                                    repetition=r.cell.repetition, status=r.disposition,
                                    normalized_score=score))
        return TrainProjection(schema="rsi.optimizer.train.v2", evaluation_id=request.evaluation_id,
                               status=status,
                               train_prompts=tuple((c.case_id, public[c.case_id]["prompt"]) for c in train),
                               cells=tuple(cells), grants_authority=False)

    def _base_receipt(self, request, index, cell, tag) -> dict:
        case = next(c for c in request.benchmark.cases if c.case_id == cell.case_id)
        selection = next(s for s in request.selections if s.arm == cell.arm)
        b = request.benchmark
        return dict(schema="rsi.cell.receipt.v2", receipt_id=f"rcpt-{tag}-{index:03d}",
                    evaluation_id=request.evaluation_id, campaign_id=request.campaign_id,
                    plan_identity=request.plan_identity, cell=cell, case=case,
                    selection=selection, benchmark_definition=b.definition,
                    public_package=b.public_package, private_package=b.private_package,
                    checker_package=b.checker_package, runtime_before=request.runtime)

    def _not_started(self, request, index, cell, tag, reason) -> CellReceipt:
        return CellReceipt(**self._base_receipt(request, index, cell, tag), runtime_after=None,
                           execution=None, disposition="not_started", reason=reason,
                           checker=None, attempts=(), elapsed_ms=None, known=ZERO,
                           retained_unknown=ZERO, envelope_exceeded=False)

    def _public_repo(self, request, index, files: dict) -> tuple[Path, str]:
        root = self.host.public_fixture_root / request.evaluation_id / f"cell-{index:03d}"
        root.mkdir(parents=True, exist_ok=False)
        for name, text in sorted(files.items()):
            target = (root / name)
            if root.resolve() not in target.resolve().parents:
                raise RuntimeError(f"public fixture file escapes its root: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "public fixture snapshot")
        return root, _git(root, "rev-parse", "HEAD")

    def _run_cell(self, request, ctx, index, cell, tag, budget, started) -> CellReceipt:
        from core.run_driver import drive_run
        from core.run_launcher import start_config_run
        h = self.host
        base = self._base_receipt(request, index, cell, tag)
        case, selection = base["case"], base["selection"]
        arm = ctx["compiled"][cell.arm]
        public = ctx["public"][cell.case_id]
        cell_key = admission.cell_scope(cell)
        b = request.budget
        budget.add_scope(cell_key, b.cell.upper, b.cell.elapsed_ms)
        t_cell = h.clock()
        scope = admission.CellScope(
            budget=budget, cell=cell, cell_key=cell_key, scopes=(cell_key, "round", "campaign"),
            runtime=request.runtime,
            endpoints={label: e["base_url"] for label, e in h.endpoints.items()},
            pricing=h.pricing, evaluation_id=request.evaluation_id, inner=h.transport,
            started={**started, "cell": t_cell}, clock=h.clock)
        scope.providers_path = str(h.providers_path)
        scope.routes_path = str(h.routes_path)
        repo, commit = self._public_repo(request, index, public["files"])
        project_id = f"rsi-{tag[:10]}-{index:03d}"
        marker = {"evaluation_id": request.evaluation_id, "plan_identity": request.plan_identity,
                  "cell": cell.model_dump(mode="json"), "policy": ctx["policy"].identity,
                  "budget_scope": request.budget.scope_id, "public_commit": commit}
        launch = start_config_run(h.db, h.ws, selection.physical_config, project_id,
                                  seed_inputs={arm["seed_file"]: public["prompt"]},
                                  repo_type="existing", repo_path=str(repo),
                                  evaluation_cell={"marker": marker, "read_source_repo": str(repo),
                                                   "read_base_sha": commit})
        run_id = launch.get("run_id")
        if launch.get("status") != "started" or not run_id:
            if run_id:
                return self._settle_cell(request, ctx, base, run_id, project_id, arm, scope,
                                         t_cell, launch_error=launch.get("message", ""))
            return self._not_started(request, index, cell, tag, "INFRASTRUCTURE")
        binding = CellBinding(scope=scope, policy=policy_mod.CellPolicy(ctx["policy"], arm),
                              marker=marker)
        scope.trace = lambda event, payload, _rid=run_id: h.sf.trace(
            _rid, "rsi_admission", event, payload, step_id=arm["step"])
        admission.bind_run(run_id, binding)
        self.bindings[run_id] = binding
        drive_error = ""
        try:
            _drive(lambda: drive_run(h.sf, h.db, h.ws, run_id, scheduler_owned=False,
                                     auto_approve=False), binding)
        except Exception as exc:                                        # noqa: BLE001
            drive_error = f"{type(exc).__name__}: {exc}"
        return self._settle_cell(request, ctx, base, run_id, project_id, arm, scope, t_cell,
                                 drive_error=drive_error)

    def _settle_cell(self, request, ctx, base, run_id, project_id, arm, scope, t_cell, *,
                     launch_error="", drive_error="") -> CellReceipt:
        h = self.host
        live = scope.abandon_pending("cell ended with the physical call still in flight")
        run = h.sf.get_run(run_id) or {}
        steps = h.sf.get_steps(run_id) or []
        try:
            unsettled = h.sf.unsettled_operations(run_id)
        except Exception:                                               # noqa: BLE001
            unsettled = ["unreadable"]
        attempts = tuple(scope.attempts)
        unknown = [a for a in attempts if a.settlement == "unknown"]
        status = run.get("status")
        # A physical call still in flight means the execution itself is not
        # settled, whatever the SDK row says; otherwise report the SDK state.
        state = "unknown" if live else {"completed": "completed", "failed": "failed",
                                        "paused": "checkpoint"}.get(status, "unknown")
        settled_ops = not unsettled and not live and not scope.pending
        execution = Execution(
            provenance="native", project_id=project_id, run_id=run_id,
            step_instance_ids=tuple(str(s["id"]) for s in steps) or ("no-step-row",),
            sdk_graph_version=run.get("graph_version") or 1,
            sdk_graph_digest=policy_mod.sdk_digest(run.get("graph_digest") or "0" * 64),
            state=state, operations_settled=settled_ops)
        elapsed = int((h.clock() - t_cell) * 1000)
        try:
            runtime_after = self.served_runtime({arm["selection"].arm: arm}, request.runtime.policy)
        except _Refuse:
            runtime_after = None
        denial = scope.denial()
        known = total(a.measured for a in attempts if a.measured is not None)
        retained = total(a.reservation for a in attempts if a.settlement == "unknown")
        envelope = bool(denial is not None and denial.reason == "BUDGET_EXHAUSTED")
        receipt = dict(base, runtime_after=runtime_after, execution=execution, attempts=attempts,
                       elapsed_ms=elapsed, known=known, retained_unknown=retained,
                       envelope_exceeded=envelope, checker=None)

        def final(disposition, reason, check=None):
            return CellReceipt(**{**receipt, "disposition": disposition, "reason": reason,
                                  "checker": check})

        if live:
            return final("unknown", "UNKNOWN_EXECUTION")
        if denial is not None and denial.reason in ("UNKNOWN_EXECUTION", "UNKNOWN_ACCOUNTING"):
            return final("unknown", denial.reason)
        if unknown:
            return final("unknown", "UNKNOWN_ACCOUNTING")
        if denial is not None:
            return final("invalid", denial.reason if denial.reason in (
                "BUDGET_EXHAUSTED", "RUNTIME_DRIFT", "POLICY_REFUSED") else "INFRASTRUCTURE")
        if not settled_ops or status not in ("completed", "failed", "paused"):
            return final("unknown", "UNKNOWN_EXECUTION")
        if status == "paused":
            return final("invalid", "UNEXPECTED_CHECKPOINT")
        if runtime_after != request.runtime:
            return final("invalid", "RUNTIME_DRIFT")
        if launch_error or drive_error:
            return final("invalid", "INFRASTRUCTURE")
        if (execution.sdk_graph_version, execution.sdk_graph_digest) != (
                base["selection"].sdk_graph_version, base["selection"].sdk_graph_digest):
            return final("invalid", "INPUT_MISMATCH")
        if status == "failed":
            error = " ".join(str(s.get("error") or s.get("last_error") or "") for s in steps)
            behaviour = ("NativeTurnBudgetExhausted", "NativeOutputCapExhausted", "MaxRetriesExceeded")
            if any(name in error for name in behaviour):
                return final("task_failure", "NONE")
            return final("invalid", "INFRASTRUCTURE")
        out_dir = h.ws.get_final_path(project_id, arm["step"], arm["physical"])
        path = out_dir / arm["output"]
        if not path.is_file() or path.is_symlink():
            return final("task_failure", "NONE")
        data = path.read_bytes()
        if not data.strip():
            return final("task_failure", "NONE")        # delivered nothing
        try:
            checker.decode_findings(data)
        except checker.CheckerInputError:
            return final("invalid", "OUTPUT_MISMATCH")
        file_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        snapshot = digest({arm["output"]: file_digest})
        snap = h.artifacts_root / "outputs" / (file_digest[7:] + ".md")
        if not snap.exists():
            snap.parent.mkdir(parents=True, exist_ok=True)
            tmp = snap.with_name(snap.name + f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, snap)
        if snap.read_bytes() != data:
            return final("invalid", "OUTPUT_MISMATCH")
        # Score with the same decoded packages verified before this evaluation
        # started, just as launch and the train projection consume ctx["public"].
        # Host package paths may now contain a later version.
        private = ctx["private"][base["case"].case_id]
        public = ctx["public"][base["case"].case_id]
        try:
            points, diagnostic = checker.score_findings(
                snap.read_bytes(), material=private["material"], refs=private["refs"],
                public_files=tuple(sorted(public["files"])))
        except checker.CheckerInputError:
            return final("invalid", "SCORER_ERROR")
        private_result = digest({"receipt": base["receipt_id"], "diagnostic": diagnostic})
        _write_once(h.artifacts_root / "private-results" / (private_result[7:] + ".json"),
                    json.dumps({"receipt": base["receipt_id"], "diagnostic": diagnostic},
                               sort_keys=True))
        check = CheckerResult(output_snapshot=snapshot, complete_files=(arm["output"],),
                              checker_package=request.benchmark.checker_package,
                              resolved_private_material=base["case"].resolved_private_material,
                              private_result=private_result, raw_points=points,
                              max_points=base["case"].max_points)
        return final("scored", "NONE", check)

