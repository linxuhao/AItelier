"""Trusted native round binding (PROOF): ``RoundBinding.invoke`` over the host.

One optimizer round is one ordinary SkillFlow run of ``round.graph.json`` on the
host's own engine. Each NEW port is the tool of exactly one graph step: an
invocation is checked here before any effect (actor grant, the run's actual
current node and checkpoint state, strict payload, port preconditions), then
the engine itself claims and executes that tool step, routes its flat result
and pauses at the graph's checkpoints. A tool dispatch the engine makes without
the owning invocation (another driver, no live binding) does nothing and routes
to the graph's error branch.

Effects reuse accepted providers only: the versioning catalog and whole-unit CAS
(``core.pipeline_versions.PipelineVersions``), ``derive_candidate`` /
``compile_bundle`` / ``seal`` / ``register_sealed`` for the private candidate,
the accepted ``NativeEvaluator`` for every measured cell, accepted
``validate_result`` / ``scorer_receipt`` and unchanged ``evaluate_matrix``.
Nothing is registered at import or boot: ``install`` binds the ports and graph
into the one host it is handed.

Budgets: the evaluator creates a fresh physical budget and time origin per
request, so every request carries the round/campaign residual: original
envelope minus all usage and unresolved reservations recorded in the immutable
evaluation results of this round/campaign, and the original deadline minus the
time elapsed on the host clock since the round froze. Unknown usage, an
unfinished evaluation or an expired deadline dispatches nothing. Live round
state is in-process; a round whose run exists without its live binding refuses
``UNSAFE_RESUME`` instead of reconstructing it.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from contracts.rsi.evaluator.contract import (
    Amount, Benchmark, Case, Cell, Envelope, EvaluationRequest, EvaluationResult, Refusal,
    Selection, digest, scorer_receipt, total, validate_result)
from contracts.rsi.pipeline_versioning import contract as vc
from core.pipeline_optimization import evaluate_matrix

from .contract import DESIGN, PORTS, ROLES, Edit, OptimizerError, RoundRequest, RoundView, graph_result

GRAPH_PATH = Path(__file__).with_name("round.graph.json")
TOOL_PREFIX = "rsi_opt_"
MARKER = "_rsi_optimizer"
ZERO = Amount(requests=0, input_tokens=0, output_tokens=0, usd_micros=0)
TERMINAL = ("completed", "failed", "stopped", "cancelled")
_LIVE: dict[str, "NativeRound"] = {}
_LIVE_LOCK = threading.Lock()


def fail(code, detail=""):
    raise OptimizerError(code, detail)


def _port_tool(port: str):
    def tool(run_id: str = "", step_id: str = "", **_injected):
        with _LIVE_LOCK:
            binding = _LIVE.get(run_id)
        if binding is None:
            return {"error": f"UNSAFE_RESUME: no live optimizer round binding for run {run_id!r}"}
        return binding._dispatched(port, step_id)
    tool.__name__ = TOOL_PREFIX + port
    return tool


def install(sf, loader) -> str:
    """Bind the NEW ports and the dormant round graph into THIS host only."""
    from skillflow.graph import PipelineGraph
    for port in PORTS:
        loader.register_dynamic_tool(TOOL_PREFIX + port, {
            "name": TOOL_PREFIX + port, "parameters": {},
            "description": f"NEW optimizer round port {port}; host-bound, no caller parameters"},
            _port_tool(port))
    graph = PipelineGraph.from_yaml(GRAPH_PATH)
    sf.register_graph(graph)
    return graph.name


@dataclass
class RoundHost:
    """Trusted host handles. Built by application/binding code, never from JSON."""
    sf: Any
    registry: Any
    db: Any
    ws: Any
    versions: Any                 # accepted PipelineVersions over config_dir
    store: Any                    # accepted BenchmarkStore
    evaluator: Any                # accepted core.rsi_evaluator.NativeEvaluator
    grants: dict                  # authorization_ref -> host-owned grant
    config_dir: Path
    public_package: Path
    private_package: Path
    artifacts_root: Path          # private optimizer artifacts (Source-patch candidates)
    source_root: Path             # exact Source tree serving builtin configs
    deployment: dict              # app_source / image / sdk_wheel
    graph_name: str
    clock: Callable[[], float]


def _envelope(spec: dict) -> Envelope:
    return Envelope(upper=Amount(**{k: spec[k] for k in Amount.model_fields}),
                    elapsed_ms=spec["elapsed_ms"])


class NativeRound:
    """One round of one campaign under existing host ownership."""

    def __init__(self, host: RoundHost, *, authorization_ref: str, round_id: str):
        self.host = host
        self.authorization_ref = authorization_ref
        self.round_id = round_id
        self.request: RoundRequest | None = None
        self.prepared: dict | None = None
        self.run_id: str | None = None
        self.decided: str | None = None
        self.stopped = False
        self.stop_reason: str | None = None
        self.adoption_action = "candidate"
        self.proposal: Edit | None = None
        self.candidate_bundle: dict | None = None
        self.candidate_vid: str | None = None
        self.selections: dict[str, Selection] = {}
        self.results: dict[tuple, tuple] = {}
        self.refusals: list[dict] = []
        self.numeric: dict = {}
        self.public = None
        self.selection_identity: str | None = None
        self.events: list[dict] = []
        self.current_history: list[str] = []
        self.origin: float | None = None
        self.builtin_sources: dict | None = None
        self.decision = None
        self.reference_vid: str | None = None
        self.recorded_tools: dict | None = None
        self._staged = None
        self._outcome = None
        a = self._assignment()
        self.targets = {"generated": a["target"], "builtin": a["builtin_shadow"]}

    # ── trusted host reads ───────────────────────────────────────────────
    def _grant(self, ref=None) -> dict | None:
        grant = self.host.grants.get(ref or self.authorization_ref)
        return grant if isinstance(grant, dict) else None

    def _assignment(self) -> dict:
        grant = self._grant() or {}
        return grant.get("optimizer") or {}

    def _run_row(self) -> dict | None:
        return self.host.sf.get_run(self.run_id) if self.run_id else None

    def _node(self, run) -> str:
        if run is None:
            return "stop" if self.stopped else "freeze"
        if run["status"] in TERMINAL:
            return "stop"
        return run["current_node"] or "stop"

    def _target_name(self, kind: str) -> str:
        return self.targets[kind]

    def _live_vid(self, target: str) -> str:
        return self.host.versions._capture_live(target)[2]

    def _unit_vid(self, target: str) -> str:
        """The version the target's own unit (graph, served roles and prompt
        bytes, from the accepted capture) is set to, projected with the tool
        identities the reference seal recorded. Served-provider drift is a
        separate refusal, not a change of what the target is set to."""
        from contracts.rsi.pipeline_versioning.native_provider import _captured_role, project
        from core import pipeline_frozen_bundle as fb
        h = self.host
        bundle = fb.capture(h.sf, h.registry, target, configs_dirs=[h.config_dir])
        return project(bundle["logical_name"], bundle["graph"], _captured_role(bundle),
                       self.recorded_tools)[1]

    def _cas(self, target: str) -> str:
        return self.host.versions.current(target)["unit_identity"]

    def _builtin_digests(self) -> dict:
        root = Path(self.host.source_root)
        return {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
                for rel in self._assignment()["builtin_sources"]}

    def _selection(self, arm: str, vid: str) -> Selection:
        from core import pipeline_frozen_bundle as fb
        from core.rsi_policy import sdk_digest
        h = self.host
        read = h.versions.read(vid)
        native = read["native"]
        physical = native["physical_name"]
        sealed = fb.verify_sealed(h.config_dir, physical)
        rows = h.sf.list_graph_versions(physical)
        if not rows:
            raise vc.UnknownVersion(f"seal {physical} is not registered")
        return Selection(arm=arm, logical_version=vid,
                         native_bundle="sha256:" + native["bundle_identity"],
                         native_seal="sha256:" + native["digest"], physical_config=physical,
                         sdk_graph_version=rows[0]["version"],
                         sdk_graph_digest=sdk_digest(rows[0]["digest"]),
                         roles=digest(sealed["roles"]), templates=digest(sealed["prompts"]),
                         tools=digest(sealed["tools"]), capabilities=digest(sealed["capabilities"]))

    def _benchmark(self) -> Benchmark:
        from core import rsi_checker
        h = self.host
        public = json.loads(Path(h.public_package).read_text(encoding="utf-8"))
        private = json.loads(Path(h.private_package).read_text(encoding="utf-8"))
        bid = self._assignment()["benchmark_id"]
        definition = h.store.get_definition(bid, 1)
        cases = tuple(Case(case_id=cid, split=meta["partition"], family=meta["family"],
                           prompt_identity=digest({"prompt": public[cid]["prompt"]}),
                           fixture_tree=digest(public[cid]["files"]),
                           scorer_ref_identity=digest(private[cid]["refs"]),
                           resolved_private_material=digest(private[cid]["material"]),
                           max_points=private[cid]["refs"]["max_points"])
                      for cid, meta in definition["cases"].items())
        code = rsi_checker.identity()
        return Benchmark(benchmark_id=bid, version=1, definition=definition["definition_digest"],
                         public_package=digest(public), private_package=digest(private),
                         checker_code=code, checker_package=rsi_checker.package_identity(code, cases),
                         cases=cases)

    def trusted_request(self, kind: str = "generated", *, effects: bool = False) -> RoundRequest:
        """The RoundRequest the trusted coordinator derives from actual host state.

        With ``effects`` (round preparation, before freeze) the reference unit is
        captured into the accepted catalog and its immutable seal registered for
        evaluation; without, everything is read and verified only."""
        from core import pipeline_frozen_bundle as fb
        h, a = self.host, self._assignment()
        shadow = self._target_name(kind)
        if effects:
            cap = h.versions.capture(shadow)
            if not h.sf.list_graph_versions(cap["physical_name"]):
                fb.register_sealed(h.sf, h.registry, cap["physical_name"], h.config_dir)
            vid = cap["version_id"]
        else:
            vid = self._live_vid(shadow)
        reference = self._selection("reference", vid)
        benchmark = self._benchmark()
        policy, compiled, _ = h.evaluator.compile_for((reference,), self.authorization_ref,
                                                      a["requested_model"], benchmark.public_package)
        runtime = h.evaluator.served_runtime(compiled, policy.identity)
        grant = self._grant()
        env = a["envelopes"]
        budget = dict(scope_id=grant["scope_id"], authorization_ref=self.authorization_ref,
                      pricing_identity=runtime.pricing, cell=_envelope(env["cell"]),
                      round=_envelope(env["round"]), campaign=_envelope(env["campaign"]))
        if kind == "generated":
            target = dict(kind="generated", name=shadow, expected_current=self._cas(shadow),
                          source_commit=h.deployment["app_source"])
        else:
            target = dict(kind="builtin", name="investigate", expected_current=None,
                          source_commit=h.deployment["app_source"])
        request = RoundRequest(schema="rsi.optimizer.round.v2", campaign_id=grant["campaign_id"],
                               round_id=self.round_id, round_number=a["rounds"][self.round_id],
                               design=DESIGN, target=target, reference=reference,
                               benchmark=benchmark, runtime=runtime, budget=budget)
        if effects:
            self.prepared = {"kind": kind, "reference_vid": vid, "shadow": shadow}
            self.recorded_tools = fb.verify_sealed(h.config_dir, reference.physical_config)["tools"]
        return request

    # ── RoundBinding.invoke ──────────────────────────────────────────────
    def invoke(self, port: str, payload: dict, *, actor: str) -> RoundView:
        if port not in PORTS:
            fail("UNKNOWN_PORT", port)
        if actor != ROLES[port]:
            fail("AUTHORITY", f"{actor} holds no grant for {port}")
        run = self._run_row()
        if self.stopped or (run is not None and run["status"] in TERMINAL):
            fail("STOPPED")
        if run is not None and run["status"] == "paused" and port != "stop":
            fail("CHECKPOINT", "the engine holds this round at an unapproved checkpoint")
        if self._node(run) != port and port != "stop":
            fail("ORDER", f"the round's graph is at {self._node(run)!r}, not {port!r}")
        try:
            data = PORTS[port].model_validate_json(json.dumps(payload, allow_nan=False))
        except (ValidationError, ValueError, TypeError, vc.VersioningError) as exc:
            fail("MALFORMED_INPUT", str(exc))
        check = getattr(self, "_check_" + port, None)
        prepared = check(data) if check else None
        if (self.request is not None and port != "stop"
                and self.request.target.kind == "generated"):
            if self._cas(self.request.target.name) != self.request.target.expected_current:
                fail("CONFLICT", "the original target changed since freeze")
        late = getattr(self, "_late_check_" + port, None)
        if late:
            late(data)
        if port == "stop" and run is not None and run["current_node"] != "stop":
            return self._owner_stop(data)
        return self._dispatch(port, data, prepared)

    def _dispatch(self, port, data, prepared) -> RoundView:
        h = self.host
        if port == "freeze":
            self._open_run(data)
        self._staged = (port, data, prepared, threading.get_ident())
        self._outcome = None
        before = self._run_row()
        try:
            h.sf.advance_run(self.run_id)
        finally:
            outcome, self._staged = self._outcome, None
        after = self._run_row()
        steps = [s for s in h.sf.get_steps(self.run_id) if s["step_id"] == port]
        completed = [s for s in steps if s["status"] == "completed"]
        self.events.append({"port": port, "provenance": "native", "sdk_run": self.run_id,
                            "sdk_node_before": before and before["current_node"],
                            "sdk_node_after": after and after["current_node"],
                            "sdk_status_after": after and after["status"],
                            "sdk_step_instance": completed[-1]["id"] if completed else None,
                            "decided": self.decided, "dispatched": outcome is not None})
        if outcome is None:
            fail("ORDER", f"the engine did not dispatch {port}")
        if isinstance(outcome, OptimizerError):
            raise outcome
        if after and after["status"] in TERMINAL:
            with _LIVE_LOCK:
                _LIVE.pop(self.run_id, None)
        return self.view()

    def _dispatched(self, port, step_id):
        staged = self._staged
        if (staged is None or staged[0] != port or step_id != port
                or staged[3] != threading.get_ident()):
            return {"error": f"AUTHORITY: engine dispatch of {port} without its owning invocation"}
        try:
            getattr(self, "_do_" + port)(staged[1], staged[2])
        except OptimizerError as exc:
            self._outcome = exc
            return {"error": str(exc)}
        self._outcome = True
        return graph_result(self._view(self.decided))

    def _open_run(self, request: RoundRequest):
        h = self.host
        from core import run_isolation
        pid = f"rsi-opt-{digest([request.campaign_id, request.round_id])[7:19]}"
        h.db.ensure_project(pid, name=f"optimizer {request.round_id}", repo_type="none",
                            repo_path=None, config_name=h.graph_name)
        h.ws.setup_workspace(pid, repo_type="none", graph_name=h.graph_name)
        marker = {"campaign_id": request.campaign_id, "round_id": request.round_id,
                  "round_number": request.round_number, "design": DESIGN,
                  "request": digest(request)}
        run_id = h.sf.create_run(h.graph_name, {"project_id": pid, MARKER: marker}, pid)
        run_isolation.ensure_for_run(h.db, run_id=run_id, project_id=pid,
                                     config_name=h.graph_name, repo_mode="none")
        with _LIVE_LOCK:
            _LIVE[run_id] = self
        self.run_id = run_id
        h.sf.start_run(run_id)

    def _owner_stop(self, data) -> RoundView:
        self.host.sf.stop_run(self.run_id, f"optimizer owner stop: {data.reason}")
        self.stop_reason, self.stopped = data.reason, True
        self.events.append({"port": "stop", "provenance": "native", "sdk_stop_run": self.run_id})
        with _LIVE_LOCK:
            _LIVE.pop(self.run_id, None)
        return self.view()

    # ── owner checkpoint decision (engine checkpoint CAS) ────────────────
    def pending_checkpoint(self) -> str | None:
        run = self._run_row()
        if run is None or run["status"] != "paused":
            return None
        return self.host.sf.get_checkpoint(self.run_id)["step_id"]

    def approve_checkpoint(self, *, actor: str) -> str:
        if actor != "owner":
            fail("AUTHORITY", "only the owner answers a round checkpoint")
        if self.pending_checkpoint() is None:
            fail("ORDER", "no paused checkpoint")
        cp = self.host.sf.get_checkpoint(self.run_id)
        expected = cp["expected_checkpoint"]
        decision = f"{self.run_id}:{cp['step_id']}:{expected['step_instance_id']}"
        nxt = self.host.sf.approve_checkpoint(self.run_id, expected_checkpoint=expected,
                                              decision_id=decision)
        self.events.append({"checkpoint_approved": cp["step_id"], "provenance": "native",
                            "decision_id": decision, "next_node": nxt,
                            "expected_checkpoint": expected})
        return nxt

    # ── ports: pre-effect checks (_check_*) and effects (_do_*) ──────────
    def _check_freeze(self, data: RoundRequest):
        from core import pipeline_frozen_bundle as fb
        grant = self._grant(data.budget.authorization_ref)
        a = (grant or {}).get("optimizer") or {}
        if (grant is None or data.budget.authorization_ref != self.authorization_ref
                or grant.get("scope_id") != data.budget.scope_id
                or grant.get("campaign_id") != data.campaign_id or not a):
            fail("AUTHORITY", "no existing host grant for this round's budget scope")
        for row in self.host.sf.list_runs():
            if row.get("graph_name") != self.host.graph_name:
                continue
            marker = json.loads(row.get("context_json") or "{}").get(MARKER) or {}
            if marker.get("campaign_id") == data.campaign_id and (
                    marker.get("round_id") == data.round_id
                    or marker.get("round_number") == data.round_number):
                fail("UNSAFE_RESUME", f"round already started by run {row['id']} without "
                                      "its live binding in this process")
        vid = data.reference.logical_version
        try:
            sealed = fb.verify_sealed(self.host.config_dir,
                                      self.host.versions.read(vid)["native"]["physical_name"])
        except (fb.BundleRefused, vc.VersioningError) as exc:
            fail("INPUT_MISMATCH", f"reference seal: {exc}")
        try:
            fb.verify_providers(self.host.sf, sealed)   # providers the reference froze
        except fb.BundleRefused as exc:
            fail("PROVIDER_CHANGED", str(exc))
        try:
            trusted = self.trusted_request(data.target.kind)
        except Exception as exc:                                    # noqa: BLE001
            fail("INPUT_MISMATCH", f"{type(exc).__name__}: {exc}")
        if data != trusted:
            fail("INPUT_MISMATCH", "request differs from the trusted frozen inputs")
        return {"reference_vid": vid, "tools": sealed["tools"]}

    def _do_freeze(self, data: RoundRequest, prepared):
        self.request = data
        self.origin = self.host.clock()
        self.reference_vid = prepared["reference_vid"]
        self.recorded_tools = prepared["tools"]
        self.selections["reference"] = data.reference
        if data.target.kind == "builtin":
            self.builtin_sources = self._builtin_digests()
        self.decided = "train_reference"

    def _do_train_reference(self, data, prepared):
        self.decided = "diagnose"
        self._evaluate("train", ("reference",))

    def _train_ids(self) -> set:
        return {c.case_id for c in self.request.benchmark.cases if c.split == "train"}

    def _check_diagnose(self, data):
        if not set(data.train_case_ids) <= self._train_ids():
            fail("CONTAMINATION", "diagnosis cites a non-train case")

    def _do_diagnose(self, data, prepared):
        self.events.append({"diagnosis": data.model_dump(mode="json"), "provenance": "native"})
        self.decided = "hypothesis"

    _check_hypothesis = _check_diagnose

    def _do_hypothesis(self, data, prepared):
        self.events.append({"hypothesis": data.model_dump(mode="json"), "provenance": "native"})
        self.decided = "candidate"            # behind the graph's checkpoint

    def _check_candidate(self, data: Edit):
        from core import pipeline_frozen_bundle as fb
        if not data.prompts and data.graph is None:
            fail("NO_ADMISSIBLE_EDIT", "empty edit")
        bench = self.request.benchmark
        public = json.loads(Path(self.host.public_package).read_text(encoding="utf-8"))
        forbidden = [c.case_id for c in bench.cases] + [public[c.case_id]["prompt"] for c in bench.cases]
        raw = json.dumps(data.model_dump(mode="json"), ensure_ascii=False)
        if any(v in raw for v in forbidden):
            fail("CONTAMINATION", "edit carries a frozen case id or case literal")
        shadow = self._target_name(self.request.target.kind)
        try:
            bundle = fb.capture(self.host.sf, self.host.registry, shadow,
                                configs_dirs=[self.host.config_dir])
            from contracts.rsi.pipeline_versioning.native_provider import project_capture
            if project_capture(bundle)[1] != self.reference_vid:
                fail("CONFLICT", "reference unit changed since freeze")
            edit = data.model_dump(mode="json", exclude_none=True)
            if not edit.get("prompts"):
                edit.pop("prompts", None)
            candidate = fb.derive_candidate(bundle, edit)
        except fb.BundleRefused as exc:
            fail("FORBIDDEN_EDIT", str(exc))
        return candidate

    def _do_candidate(self, data, candidate):
        self.proposal, self.candidate_bundle = data, candidate
        self.decided = "admissibility"

    def _check_admissibility(self, data):
        from contracts.rsi.pipeline_versioning.native_provider import project_capture, project_sealed
        from core import pipeline_frozen_bundle as fb
        _, vid = project_capture(self.candidate_bundle)
        if vid == self.reference_vid:
            fail("NO_ADMISSIBLE_EDIT", "candidate reproduces the reference content")
        try:
            preview = fb.compile_bundle(self.candidate_bundle, vc.seal_slug(vid))
            if project_sealed(preview)[1] != vid:
                fail("FORBIDDEN_EDIT", "sealed candidate would not reproduce its content")
            from core import pipeline_registry as pr
            pr._validated_registration(preview["physical_name"], preview["graph_yaml"],
                                       roles=preview["roles"], sf=self.host.sf)
            fb.verify_inputs(self.candidate_bundle)
            fb.verify_providers(self.host.sf, self.candidate_bundle)
        except (fb.BundleRefused, vc.VersioningError) as exc:
            fail("FORBIDDEN_EDIT", str(exc))
        return vid

    def _do_admissibility(self, data, vid):
        from core import pipeline_frozen_bundle as fb
        h = self.host
        with fb.exclusion(h.sf):
            physical = h.versions._catalog(self.candidate_bundle)
        if not h.sf.list_graph_versions(physical):
            fb.register_sealed(h.sf, h.registry, physical, h.config_dir)
        self.candidate_vid = vid
        self.selections["candidate"] = self._selection("candidate", vid)
        self.decided = "train_candidate"

    def _do_train_candidate(self, data, prepared):
        self.decided = "train_compare"
        self._evaluate("train", ("candidate",))

    def _do_train_compare(self, data, prepared):
        r = self._matrix("train")
        if r["status"] != "train_complete" or Fraction(r["public_summary"]["overall_difference"]["exact"]) <= 0:
            self.decided, self.stop_reason = "restore", "no_train_improvement"
            return
        # The frozen identity of the one possible holdout selection. Computing it
        # authorizes nothing: holdout_seal needs the owner's existing host grant.
        self.selection_identity = digest([self.request, self.candidate_vid,
                                          [out.receipt_catalog for _, out in self.results.values()],
                                          self.numeric["train"]])
        self.decided = "holdout_seal"         # behind the graph's checkpoint

    def _consumed(self, selection: str | None) -> bool:
        if not selection:
            return False
        marker = self.host.evaluator.host.artifacts_root / "holdout-consumed" / (selection[7:] + ".json")
        return marker.exists()

    def _check_holdout_seal(self, data):
        grant = self._grant() or {}
        if (self.selection_identity is None or data.selection != self.selection_identity
                or data.selection not in (grant.get("holdout_selections") or ())
                or self._consumed(data.selection)):
            fail("HOLDOUT_AUTHORITY", "no unconsumed owner grant for this exact selection")

    def _do_holdout_seal(self, data, prepared):
        self.events.append({"holdout_seal": data.selection, "provenance": "native"})
        self.decided = "holdout"

    def _do_holdout(self, data, prepared):
        self.decided = "compare"
        self._evaluate("holdout", ("reference", "candidate"))

    def _do_compare(self, data, prepared):
        self._matrix("holdout")
        self.decided = "decision"

    def _adoption_records(self, data) -> bool:
        records = self._assignment().get("adoption") or {}
        review = records.get(data.review_ref or "")
        owner = records.get(data.owner_decision_ref or "")
        return (isinstance(review, dict) and review.get("kind") == "nonauthor_review"
                and review.get("verdict") == "approve" and isinstance(owner, dict)
                and owner.get("kind") == "owner_decision" and owner.get("action") == "adopt"
                and review.get("round_id") == owner.get("round_id") == self.round_id
                and review.get("target") == owner.get("target") == self.request.target.name)

    def _late_check_decision(self, data):
        eligible = self.numeric["holdout"]["status"] == "eligible"
        if data.action in ("adopt", "source_patch") and not eligible:
            fail("NOT_ELIGIBLE")
        if data.action == "adopt" and (self.request.target.kind != "generated"
                                       or not self._adoption_records(data)):
            fail("ADOPTION_AUTHORITY", "no existing nonauthor review and owner decision records")
        if data.action == "source_patch" and self.request.target.kind != "builtin":
            fail("POLICY_REFUSED")

    def _do_decision(self, data, prepared):
        eligible = self.numeric["holdout"]["status"] == "eligible"
        if not eligible or data.action == "retain":
            self.decided = "restore"
            self.stop_reason = "holdout_rejected" if not eligible else "retained"
        elif data.action in ("adopt", "source_patch"):
            self.decided, self.adoption_action = "adoption_review", data.action
            self.decision = data
        else:
            means = self.numeric["holdout"]["case_means"]["candidate"].values()
            self.decided = "stop"
            self.stop_reason = ("target" if sum(Fraction(v["exact"]) for v in means) / 8 >= 95
                                else "manual_candidate")

    def _do_adoption_review(self, data, prepared):
        self.decided = self.adoption_action    # behind the graph's checkpoint

    def _check_adopt(self, data):
        if self.request.target.kind == "builtin":
            if self.adoption_action != "source_patch":
                fail("POLICY_REFUSED", "a builtin target is never adopted at runtime")
            return None
        if self.adoption_action != "adopt" or not self._adoption_records(self.decision):
            fail("ADOPTION_AUTHORITY")
        return None

    def _do_adopt(self, data, prepared):
        h = self.host
        if self.request.target.kind == "builtin":
            # The engine continues an approved adoption review here; a builtin
            # winner has no runtime adoption and goes on to its Source patch.
            self._verify_builtin()
            self.decided = "source_patch"
            return
        target = self.request.target.name
        try:
            done = h.versions.restore(target, self.candidate_vid,
                                      expected_current=self.request.target.expected_current)
        except vc.Conflict as exc:
            fail("CONFLICT", str(exc))
        except vc.VersioningError as exc:
            fail("PROVIDER_CHANGED", str(exc))
        live = self._unit_vid(target)
        self.events.append({"adopted": done, "observed_current": live, "provenance": "native"})
        if live != self.candidate_vid:
            fail("CONFLICT", "adoption did not serve the candidate")
        self.current_history.append(live)
        self.decided, self.stop_reason = "stop", "adopted"

    def _verify_builtin(self):
        if self._builtin_digests() != self.builtin_sources:
            fail("CONFLICT", "builtin Source changed since freeze")

    def _check_restore(self, data):
        if self.request.target.kind == "builtin":
            self._verify_builtin()

    def _do_restore(self, data, prepared):
        h = self.host
        if self.request.target.kind == "generated":
            try:
                done = h.versions.restore(self.request.target.name, self.reference_vid,
                                          expected_current=self.request.target.expected_current)
            except vc.Conflict as exc:
                fail("CONFLICT", str(exc))
            except vc.VersioningError as exc:
                fail("PROVIDER_CHANGED", str(exc))
            self.events.append({"restore_reference": done, "provenance": "native"})
        else:
            self._verify_builtin()
            self.events.append({"restore_reference": "builtin Source verified unchanged",
                                "provenance": "native"})
        if not self.stop_reason:
            self.stop_reason = "retained"
        self.decided = "stop"

    def _check_source_patch(self, data):
        if self.request.target.kind != "builtin" or self.adoption_action != "source_patch":
            fail("POLICY_REFUSED")
        self._verify_builtin()

    def _do_source_patch(self, data, prepared):
        ref = self.host.versions.read(self.reference_vid)["content"]
        cand = self.host.versions.read(self.candidate_vid)["content"]
        diff = {}
        for role, cfg in cand["roles"].items():
            before = (ref["roles"].get(role) or {}).get("prompt") or ""
            after = cfg.get("prompt") or ""
            if before != after:
                diff[role] = "".join(difflib.unified_diff(
                    before.splitlines(True), after.splitlines(True),
                    f"reference/{role}", f"candidate/{role}"))
        record = {"schema": "rsi.optimizer.source_patch_candidate.v1", "target": "investigate",
                  "source_commit": self.request.target.source_commit,
                  "builtin_sources": self.builtin_sources, "reference_version": self.reference_vid,
                  "candidate_version": self.candidate_vid, "prompt_diff": diff,
                  "decision": self.decision.model_dump(mode="json"), "applied": False,
                  "note": "private reviewed Source-patch CANDIDATE; never a runtime builtin override"}
        path = (Path(self.host.artifacts_root) / "source-patch"
                / f"{digest(record)[7:]}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
        self.events.append({"source_patch_candidate": str(path), "provenance": "native"})
        self.stop_reason, self.decided = "source_patch", "stop"

    def _check_stop(self, data):
        if self.stop_reason and data.reason != self.stop_reason:
            fail("ORDER", "stop must carry the round's trusted stop reason")
        if not self.stop_reason and data.reason not in {
                "owner_stop", "contamination", "no_admissible_edit", "budget",
                "round_limit", "invalid", "unknown"}:
            fail("ORDER")

    def _do_stop(self, data, prepared):
        self.stop_reason, self.stopped, self.decided = data.reason, True, "stop"

    # ── evaluation under residual round/campaign envelopes ──────────────
    def _campaign_usage(self) -> tuple[Amount, bool]:
        """(usage incl. reservations, unresolved) from the evaluator's immutable
        results of this campaign. A started evaluation without its sealed result,
        or any retained unknown reservation, is unresolved."""
        root = self.host.evaluator.host.artifacts_root / "evaluations"
        used, unresolved = [], False
        for started in sorted(root.glob("*/started.json")) if root.is_dir() else ():
            result = started.parent / "result.json"
            if not result.exists():
                unresolved = True              # cannot tell whose or how much
                continue
            out = EvaluationResult.model_validate_json(result.read_text(encoding="utf-8"))
            if out.receipts and out.receipts[0].campaign_id == self.request.campaign_id:
                used += [out.known, out.retained_unknown]
                unresolved = unresolved or out.retained_unknown != ZERO
        return total(used), unresolved

    def _residual(self):
        b = self.request.budget
        outs = [out for _, out in self.results.values()]
        round_used = total(x for o in outs for x in (o.known, o.retained_unknown))
        campaign_used, unresolved = self._campaign_usage()
        if unresolved or any(o.retained_unknown != ZERO for o in outs):
            return None, "unknown"
        elapsed = int((self.host.clock() - self.origin) * 1000)

        def remaining(env: Envelope, used: Amount):
            ms = env.elapsed_ms - elapsed
            if ms <= 0:
                return None
            return Envelope(upper=Amount(**{k: max(0, getattr(env.upper, k) - getattr(used, k))
                                            for k in Amount.model_fields}), elapsed_ms=ms)
        rnd, camp = remaining(b.round, round_used), remaining(b.campaign, campaign_used)
        if rnd is None or camp is None:
            return None, "budget"
        return b.model_copy(update={"round": rnd, "campaign": camp}), None

    def evaluation_request(self, split, arms, *, evaluation_id, budget, holdout_selection=None):
        h, q = self.host, self.request
        selections = tuple(self.selections[a] for a in arms)
        policy, _, _ = h.evaluator.compile_for(selections, q.budget.authorization_ref,
                                               q.runtime.requested_model, q.benchmark.public_package)
        # The frozen runtime, with the accepted compiled policy for exactly
        # these arms; the evaluator's own preflight re-derives what it serves.
        runtime = q.runtime.model_copy(update={"policy": policy.identity})
        cells = tuple(Cell(case_id=c.case_id, arm=a, repetition=r)
                      for r in range(1, 4) for c in q.benchmark.cases if c.split == split for a in arms)
        data = dict(schema="rsi.evaluation.request.v2", evaluation_id=evaluation_id,
                    campaign_id=q.campaign_id, round_id=q.round_id, split=split, repetitions=3,
                    max_parallel=1, selections=selections, benchmark=q.benchmark, cells=cells,
                    runtime=runtime, budget=budget, holdout_selection=holdout_selection)
        return EvaluationRequest(plan_identity=digest(data), **data)

    def _evaluate(self, split, arms):
        budget, why = self._residual()
        if budget is None:
            self.events.append({"evaluation_not_dispatched": [split, list(arms)], "reason": why,
                                "provenance": "native"})
            self.stop_reason, self.decided = why, "stop"
            return
        ev_id = f"{self.round_id}.{split}-{'-'.join(arms)}"
        try:
            q = self.evaluation_request(split, arms, evaluation_id=ev_id, budget=budget,
                                        holdout_selection=self.selection_identity if split == "holdout" else None)
        except Exception as exc:                                    # noqa: BLE001
            self.refusals.append({"evaluation_id": ev_id, "compile": f"{type(exc).__name__}: {exc}"})
            self.stop_reason, self.decided = "invalid", "stop"
            return
        out = self.host.evaluator.evaluate(q)
        if isinstance(out, Refusal):
            self.refusals.append({"evaluation_id": ev_id, "refusal": out.model_dump(mode="json")})
            self.events.append({"evaluation": split, "arms": list(arms), "refused": out.reason,
                                "provenance": "native"})
            self.stop_reason, self.decided = "invalid", "stop"
            return
        validate_result(q, out)
        self.results[(split, arms)] = (q, out)
        self.events.append({"evaluation": split, "arms": list(arms), "status": out.status,
                            "evaluation_id": ev_id, "provenance": "native",
                            "cells": len(out.receipts),
                            "launched": sum(1 for r in out.receipts if r.execution is not None),
                            "known": out.known.model_dump(), "retained_unknown": out.retained_unknown.model_dump(),
                            "budget_round": budget.round.model_dump(mode="json")})
        if split == "train":
            self.public = out.optimizer_projection
        if out.status != "complete":
            self.stop_reason = ("unknown" if out.status == "unknown" else "budget"
                                if any(r.reason == "BUDGET_EXHAUSTED" for r in out.receipts) else "invalid")
            self.decided = "stop"

    def _matrix(self, split):
        pairs = ([self.results[("train", ("reference",))], self.results[("train", ("candidate",))]]
                 if split == "train" else [self.results[("holdout", ("reference", "candidate"))]])
        q = pairs[0][0]
        arms = {}
        for req, _ in pairs:
            for s in req.selections:
                arms[s.arm] = {"bundle_id": s.native_bundle, "runtime_id": digest(req.runtime),
                               "checker_id": req.benchmark.checker_package}
        cases = q.benchmark.cases
        manifest = {"train_case_ids": [c.case_id for c in cases if c.split == "train"],
                    "holdout_case_ids": [c.case_id for c in cases if c.split == "holdout"],
                    "case_families": {c.case_id: c.family for c in cases},
                    "family_inventory": ["family-a", "family-b", "family-c", "family-d"],
                    "repetitions": 3, "arms": arms,
                    "input_ids": {c.case_id: c.prompt_identity for c in cases}}
        result = evaluate_matrix(manifest, [scorer_receipt(r) for _, out in pairs for r in out.receipts],
                                 stage=split)
        self.numeric[split] = result["public_summary"]
        return result

    # ── views ────────────────────────────────────────────────────────────
    def _known(self):
        outs = [out for _, out in self.results.values()]
        return total(o.known for o in outs), total(o.retained_unknown for o in outs)

    def current_version(self) -> str:
        if self.request is not None and self.request.target.kind == "builtin":
            if self.builtin_sources is not None and self._builtin_digests() != self.builtin_sources:
                return digest({"builtin_source": self._builtin_digests()})
            return self._unit_vid(self._target_name("builtin"))
        kind = self.request.target.kind if self.request else (self.prepared or {}).get("kind", "generated")
        return self._unit_vid(self._target_name(kind))

    def reference_version(self) -> str:
        if self.reference_vid:
            return self.reference_vid
        if self.prepared:
            return self.prepared["reference_vid"]
        return self._live_vid(self._target_name("generated"))

    def _view(self, phase) -> RoundView:
        known, unknown = self._known()
        return RoundView(phase=phase, reference_version=self.reference_version(),
                         candidate_version=self.candidate_vid, current_version=self.current_version(),
                         public_train=self.public, stopped=self.stopped, stop_reason=self.stop_reason,
                         known=known, retained_unknown=unknown,
                         quiescent=not any(unknown.model_dump().values()),
                         action=("retain" if phase == "restore" else self.adoption_action))

    def view(self) -> RoundView:
        run = self._run_row()
        view = self._view(self._node(run))
        if run is not None and run["status"] in TERMINAL:
            view = view.model_copy(update={"stopped": True})
        return view

    def sdk_state(self) -> dict:
        """Durable engine facts for this round's run (read only)."""
        sf = self.host.sf
        if not self.run_id:
            return {"run": None}
        run = dict(sf.get_run(self.run_id) or {})
        steps = [{k: s.get(k) for k in ("id", "step_id", "status", "version", "claim_epoch",
                                        "completion_seq", "claimed_by")}
                 for s in sf.get_steps(self.run_id)]
        with sf._ro() as conn:
            approvals = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_approved' "
                "AND json_extract(payload_json,'$.run_id')=? ORDER BY id", (self.run_id,))]
            pauses = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM skillflow_outbox WHERE event_type='checkpoint_paused' "
                "AND json_extract(payload_json,'$.run_id')=? ORDER BY id", (self.run_id,))]
        return {"run": {k: run.get(k) for k in ("id", "graph_name", "graph_version", "graph_digest",
                                                  "status", "current_node", "project_id",
                                                  "error_reason", "context_json")},
                "steps": steps, "active_ops": sf.unsettled_operations(self.run_id),
                "checkpoint_pauses": [{k: p.get(k) for k in ("step_id", "next_node", "expected_checkpoint")}
                                      for p in pauses],
                "checkpoint_approvals": [{k: p.get(k) for k in ("step_id", "next_node", "decision_id",
                                                                 "expected_checkpoint")}
                                         for p in approvals]}

    def close(self):
        with _LIVE_LOCK:
            if self.run_id:
                _LIVE.pop(self.run_id, None)
