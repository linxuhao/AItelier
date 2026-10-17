"""Genuine host binding of the SAME shared optimizer ``conformance.Fixture``.

A world is the accepted evaluator's durable native host (``NativeWorld``:
AItelierSkillFlow, ToolLoader, ConfigRegistry, DBManager, BenchmarkStore,
accepted versioning catalog) holding ONE generated target, the literal pilot
unit. The optimizer candidate is never pre-made: it is derived from the
proposer's edit by the binding. ``install`` binds the NEW ports and the round
graph into this world's host only.

Fixture controls choose the caller context (``actor``), act as the host owner
(checkpoint answers through the engine's checkpoint CAS, holdout grant, review
and decision records in the host grant) and change actual inputs for faults:
host grants, served tool providers, learned output caps, a real concurrent
versioning edit, a real prior consumption of the holdout selection by the
evaluator, a real second process that started the round, the host clock, and
the deterministic transport's answers. The transport is the accepted fixture
provider under the evaluator's real admission boundary; its answer SCRIPT is
this fixture's synthetic data: the trusted checker scores the delivered bytes.
Nothing patches a production method or returns expected counters.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from contracts.rsi.evaluator.contract import Amount, Envelope
from contracts.rsi.evaluator.native_fixture import (AUTH, CANDIDATE, REFERENCE, NativeWorld,
                                                    _replacement_dir_tree, versions_class)
from contracts.rsi.evaluator.native_transport import DeterministicProvider

from .contract import OptimizerError
from .native_binding import NativeRound, RoundHost, install

ROUND = "optimizer-round-1"
REPO = Path(__file__).resolve().parents[3]
_CEILING = {"requests": 1000, "input_tokens": 16000000, "output_tokens": 2000000,
            "usd_micros": 25000000, "elapsed_ms": 14400000}
# Host-owned round assignment, stored in the existing host grant record.
ASSIGNMENT = {
    "rounds": {ROUND: 1}, "target": REFERENCE, "builtin_shadow": REFERENCE,
    "builtin_sources": ["configs/investigate.yaml", "agent_configs/investigate.yaml",
                        "templates/investigate.md"],
    "benchmark_id": "evaluator-fixture", "requested_model": "ark/deepseek-v4-flash",
    "envelopes": {"cell": {"requests": 8, "input_tokens": 128000, "output_tokens": 16000,
                           "usd_micros": 1000000, "elapsed_ms": 300000},
                  "round": dict(_CEILING), "campaign": dict(_CEILING)},
    "adoption": {}}
FAULTS = {"none", "no_authority", "unsafe_resume", "provider_changed", "runtime_drift",
          "no_train_improvement", "holdout_rejected", "family_regression",
          "unknown_execution", "unknown_accounting", "budget", "concurrent_edit",
          "holdout_reuse", "inclusive_margin", "target", "deadline"}
SCRIPTED = {"no_train_improvement", "holdout_rejected", "family_regression",
            "inclusive_margin", "target"}
CELL_FAULT = {"unknown_execution": "unknown_execution", "unknown_accounting": "unknown_usage",
              "budget": "envelope"}


class RoundWorld(NativeWorld):
    """The evaluator's native world with only the reference target."""

    def _create(self) -> None:
        super()._create()
        for suffix in (".yaml", ".roles.json"):
            (self.gen / f"{CANDIDATE}{suffix}").unlink()
        path = self.root / "host.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["grants"][AUTH]["optimizer"] = copy.deepcopy(ASSIGNMENT)
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")

    def _register(self) -> None:
        """Boot-equivalent: ordinary registration of the target, then of every
        catalog seal through the accepted sealed-registration path."""
        from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
        from core.pipeline_benchmark import BenchmarkStore
        out = pr.reload_generated_pipeline(self.sf, self.registry, REFERENCE)
        assert "error" not in out, out
        cls, self.versions_impl = versions_class()
        self.versions = cls(self.sf, self.registry, self.gen)
        for vid in self.versions.list_versions():
            fb.register_sealed(self.sf, self.registry,
                               self.versions.read(vid)["native"]["physical_name"], self.gen)
        self.captured = {}
        self.store = BenchmarkStore(self.root / "private" / "benchmark" / "store.json")
        self.host_doc = json.loads((self.root / "host.json").read_text(encoding="utf-8"))


class HostClock:
    """The host's monotonic clock, shared by coordinator and evaluator."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


class RoundTransport(DeterministicProvider):
    """Accepted deterministic provider with this fixture's answer script.

    Answers derive from the PUBLIC package only. Quality levels, as the
    trusted checker scores them: ``full`` (value and cited file, 10/10),
    ``value`` (value only, 6/10), ``cited_wrong`` (cited file, no value, 4/10).
    One physical send per cell, so a (case, arm)'s n-th send is repetition n.
    """

    def __init__(self, log_path, public, cases):
        super().__init__(log_path, public)
        self.cases = cases
        self.script = "useful"
        self._local = threading.local()

    def respond(self, record, body):
        self._local.record = record
        return super().respond(record, body)

    def quality(self, case, arm, rep):
        if arm == "reference":
            return "value"
        holdout = self.cases[case]["partition"] == "holdout"
        first_four = sorted(c for c, m in self.cases.items() if m["partition"] == "holdout")[:4]
        s = self.script
        if not holdout:
            return "value" if s == "no_train_improvement" else ("full" if rep == 1 else "value")
        if s in ("holdout_rejected", "no_train_improvement"):
            return "value"
        if s == "target":
            return "full"
        if s == "inclusive_margin":
            return "full" if rep == 1 or (rep == 2 and case in first_four) else "value"
        if s == "family_regression":
            return "cited_wrong" if self.cases[case]["family"] == "family-d" else "full"
        return "full" if rep == 1 else "value"

    def _answer(self, case, arm):
        files = (self.public.get(case) or {}).get("files") or {}
        name, text = next(iter(sorted(files.items())))
        value = int(text.split("=")[-1].strip())
        quality = self.quality(case, arm, self._local.record["n_for_case"])
        if quality == "full":
            return f"# Findings\n\n{name} exports `{value}`: the line `{text.strip()}` defines it.\n"
        if quality == "cited_wrong":
            return f"# Findings\n\n{name} was read, but its constant was not determined.\n"
        return f"# Findings\n\nThe constant is {value}.\n"


class NativeRoundFixture:
    """``conformance.Fixture`` over the genuine host (evidence: native)."""
    evidence_kind = "native"

    def __init__(self, base: Path | None = None, *, root: Path | None = None):
        fresh = root is None
        self.root = Path(tempfile.mkdtemp(prefix="round-world-", dir=base)) if fresh else Path(root)
        self.world = w = RoundWorld(self.root, fresh=fresh)
        self.public = json.loads((self.root / "public-package" / "public.json").read_text())
        self.provider = RoundTransport(self.root / "provider-log.jsonl", self.public,
                                       w.fixture["benchmark"]["header"]["cases"])
        self.clock = HostClock()
        from core.rsi_evaluator import NativeEvaluator
        self.evaluator = NativeEvaluator(dataclasses.replace(w.host(self.provider), clock=self.clock))
        graph_name = install(w.sf, w.loader)
        self.host = RoundHost(
            sf=w.sf, registry=w.registry, db=w.db, ws=w.ws, versions=w.versions, store=w.store,
            evaluator=self.evaluator, grants=w.host_doc["grants"], config_dir=w.gen,
            public_package=self.root / "public-package" / "public.json",
            private_package=self.root / "private" / "checker" / "private.json",
            artifacts_root=self.root / "private" / "optimizer", source_root=REPO,
            deployment=w.host_doc["deployment"], graph_name=graph_name, clock=self.clock)
        self.round = NativeRound(self.host, authorization_ref=AUTH, round_id=ROUND)
        self.actor = "coordinator"
        self.holdout_allowed = True
        self.fault = "none"
        self.prepared_request = None
        self.fault_facts: dict = {}

    def _assignment(self) -> dict:
        return self.host.grants[AUTH]["optimizer"]

    # -- Fixture protocol --------------------------------------------------
    def prepare(self, kind="generated"):
        self.prepared_request = self.round.trusted_request(kind, effects=True)
        return self.prepared_request

    def invoke(self, port, payload):
        return self.round.invoke(port, payload, actor=self.actor)

    def observations(self) -> dict:
        r = self.round
        target = r.targets["generated"]
        return {"evidence_kind": self.evidence_kind, "view": r.view().model_dump(mode="json"),
                "events": copy.deepcopy(r.events), "current_history": list(r.current_history),
                "holdout_consumed": r._consumed(r.selection_identity),
                "pending_checkpoint": r.pending_checkpoint(),
                "cas": self.world.versions.current(target)["unit_identity"],
                "numeric": copy.deepcopy(r.numeric)}

    def snapshot(self):
        w, r = self.world, self.round
        files = sorted(str(p.relative_to(self.root)) for p in (self.root / "private").rglob("*")
                       if p.is_file())
        host = {"grants": json.dumps(self.host.grants, sort_keys=True),
                "catalog": w.versions.list_versions(),
                "provider_sends": len(self.provider.received()),
                "scoreboard_rows": len(w.store.list_receipts("evaluator-fixture", 1)),
                "runs": sorted((x["id"], x["status"], x.get("current_node")) for x in w.sf.list_runs()),
                "private_files": files}
        owned = {"request": r.request and r.request.model_dump(mode="json"),
                 "proposal": r.proposal and r.proposal.model_dump(mode="json"),
                 "candidate": r.candidate_vid, "results": sorted(map(str, r.results)),
                 "selection": r.selection_identity, "stop": (r.stopped, r.stop_reason),
                 "decided": r.decided}
        return copy.deepcopy((self.observations(), r.sdk_state(), host, owned))

    def set_fault(self, name):
        if name not in FAULTS:
            raise OptimizerError("UNKNOWN_INPUT", name)
        self.fault = name
        if name in SCRIPTED:
            self.provider.script = name
        elif name in CELL_FAULT:
            self.provider.first_cell = ("train-01", "candidate")
            self.provider.fault = CELL_FAULT[name]
        else:
            getattr(self, "_fault_" + name)()

    def approve_checkpoint(self):
        if self.actor != "owner":
            raise OptimizerError("AUTHORITY", "only the owner answers a checkpoint")
        step = self.round.pending_checkpoint()
        if step is None:
            raise OptimizerError("ORDER", "no paused checkpoint")
        if step == "train_compare" and self.holdout_allowed:
            # Owner act: one host-owned grant for exactly the sealed selection.
            self.host.grants[AUTH].setdefault("holdout_selections", []).append(
                self.round.selection_identity)
        return self.round.approve_checkpoint(actor=self.actor)

    def selection_authorization(self):
        return {"selection": self.round.selection_identity}

    def approve_adoption(self):
        # Host-owned review and owner decision records for this round's target.
        target = self.round.targets["generated"]
        self._assignment()["adoption"] = {
            "reviewed": {"kind": "nonauthor_review", "verdict": "approve", "round_id": ROUND,
                         "target": target, "reviewer": "fixture-nonauthor-reviewer"},
            "owner-approved": {"kind": "owner_decision", "action": "adopt", "round_id": ROUND,
                               "target": target, "owner": "fixture-owner"}}

    def configure_budget(self, requests):
        self._assignment()["envelopes"]["round"]["requests"] = requests

    # -- faults that change actual host inputs -----------------------------
    def _fault_none(self):
        pass

    def _fault_no_authority(self):
        self.host.grants.pop(AUTH)

    def _fault_provider_changed(self):
        loader = self.world.loader
        loader.register_dynamic_tool("dir_tree", loader.load_schema("dir_tree"), _replacement_dir_tree)

    def _fault_runtime_drift(self):
        from core import agents
        for selection in self.round.selections.values():
            role = f"{selection.physical_config}__investigator"
            agents.remember_output_cap(role, 16384)
            self.world.learned_roles.append(role)

    def _fault_concurrent_edit(self):
        target = self.round.targets["generated"]
        out = self.world.versions.edit(target, {
            "kind": "prompt", "role": "investigator",
            "template": "Foreign operator instruction, edited concurrently.\n"})
        assert "error" not in out, out
        self.fault_facts["concurrent_edit"] = out

    def _fault_holdout_reuse(self):
        """A real earlier use of the same granted selection by the evaluator."""
        r = self.round
        b = r.request.budget
        budget = b.model_copy(update={"round": Envelope(
            upper=Amount(requests=0, input_tokens=0, output_tokens=0, usd_micros=0),
            elapsed_ms=b.round.elapsed_ms)})
        q = r.evaluation_request("holdout", ("reference", "candidate"),
                                 evaluation_id="prior-holdout-use", budget=budget,
                                 holdout_selection=r.selection_identity)
        out = self.evaluator.evaluate(q)
        self.fault_facts["prior_holdout_use"] = {
            "type": type(out).__name__, "status": getattr(out, "status", None),
            "reason": getattr(out, "reason", None),
            "launched": sum(1 for x in getattr(out, "receipts", ()) if x.execution is not None)}

    def _fault_deadline(self):
        self.clock.offset += self.round.request.budget.round.elapsed_ms / 1000

    def _fault_unsafe_resume(self):
        """A real second process starts this round and exits (process loss)."""
        req = self.root / "resume-request.json"
        req.write_text(self.prepared_request.model_dump_json(), encoding="utf-8")
        with open(self.root / "resume-child.log", "w", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, "-m", "contracts.rsi.optimizer.native_child",
                                   str(self.root), str(req)], stdout=log, stderr=subprocess.STDOUT,
                                  env=dict(os.environ), cwd=str(REPO), timeout=600)
        text = (self.root / "resume-child.log").read_text(encoding="utf-8")
        self.fault_facts["resume_child"] = {"returncode": proc.returncode, "log_tail": text[-2000:]}
        assert proc.returncode == 0, text[-4000:]

    # -- native evidence (read only) ---------------------------------------
    def admission_trace(self) -> list[dict]:
        rows = []
        for db in sorted(self.root.glob("ws/*/trace.db")):
            con = sqlite3.connect(db)
            try:
                rows += [{"project": db.parent.name, "event": r[0], "payload": json.loads(r[1] or "{}")}
                         for r in con.execute("select event, payload_json from skillflow_trace "
                                              "where category = 'rsi_admission' order by seq")]
            finally:
                con.close()
        return rows

    def native_evidence(self) -> dict:
        r, w = self.round, self.world
        sends = self.provider.received()
        by = {}
        for s in sends:
            split = "holdout" if (s.get("case") or "").startswith("holdout") else "train"
            key = f"{split}/{s['arm']}"
            by[key] = by.get(key, 0) + 1
        trace = self.admission_trace()
        cell_runs = [x for x in w.sf.list_runs()
                     if "_rsi_evaluation" in json.loads(x.get("context_json") or "{}")]
        return {"provider_sends": len(sends), "provider_sends_by_split_arm": by,
                "provider_evidence": self.provider.evidence,
                "trace_physical_sends": sum(1 for t in trace if t["event"] == "rsi_physical_send"),
                "trace_denials": [t["payload"] for t in trace if t["event"] == "rsi_physical_denied"],
                "evaluator_cell_launches": len(cell_runs),
                "scoreboard_rows": len(w.store.list_receipts("evaluator-fixture", 1)),
                "catalog": w.versions.list_versions(),
                "sdk": r.sdk_state(), "refusals": r.refusals, "numeric": copy.deepcopy(r.numeric),
                "view": r.view().model_dump(mode="json"),
                "selections": {a: s.model_dump(mode="json") for a, s in r.selections.items()},
                "request": r.request and r.request.model_dump(mode="json"),
                "evaluations": [{"evaluation_id": q.evaluation_id, "status": out.status,
                                 "cells": len(out.receipts), "budget_round": q.budget.round.model_dump(mode="json"),
                                 "budget_campaign": q.budget.campaign.model_dump(mode="json"),
                                 "runtime_policy": q.runtime.policy,
                                 "known": out.known.model_dump(), "retained_unknown": out.retained_unknown.model_dump(),
                                 "reasons": sorted({x.reason for x in out.receipts})}
                                for (q, out) in r.results.values()],
                "fault": self.fault, "fault_facts": self.fault_facts,
                "transport_script": self.provider.script, "transport_fault": self.provider.fault,
                "versions_impl": w.versions_impl,
                "source_patch_candidates": sorted(str(p) for p in (self.root / "private" / "optimizer").rglob("*.json")),
                "holdout_consumed_markers": sorted(p.name for p in (self.root / "private" / "evaluator" / "holdout-consumed").glob("*.json"))}

    def close(self) -> None:
        self.round.close()
        self.provider.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(
                t.name.startswith("llm-call") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.05)
        self.world.close()


class NativeRoundFactory:
    """Fresh native world per scenario, closed afterwards by the harness user."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.made: list[NativeRoundFixture] = []

    def __call__(self) -> NativeRoundFixture:
        for old in self.made:
            old.close()
        fixture = NativeRoundFixture(self.base)
        self.made = [fixture]
        return fixture

    def close(self):
        for fixture in self.made:
            fixture.close()
        self.made = []
