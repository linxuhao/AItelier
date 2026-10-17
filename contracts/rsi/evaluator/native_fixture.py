"""Genuine host binding of the SAME shared ``conformance.FixtureBinding``.

A world is a durable host under one root: AItelierSkillFlow (SQLite), the
production ToolLoader factory, ConfigRegistry, DBManager, WorkspaceManager and
the host singletons of ``api.dependencies`` pointed at them. ``prepare``
registers the literal SDK-native unit through the ordinary generated-config
reload, captures and seals it through the accepted versioning binding,
registers the seals through the accepted frozen-bundle path, publishes the
literal benchmark through the accepted BenchmarkStore and asks the production
evaluator for its real compiled policy and served runtime identities.

``set_fault`` changes the actual corresponding input: host grants, roots,
served registrations, tool providers, provider registry, packages, catalog
seals, the deterministic transport's answers, or a real killed evaluator
process. ``evaluate`` is the production ``core.rsi_evaluator.NativeEvaluator``.
``observations`` reads persisted facts: SDK run rows, the provider-side request
log, BenchmarkStore rows and refusal records. Nothing patches a production
method; the deterministic transport is a labeled fixture provider.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

from .contract import (Amount, Benchmark, Budget, Case, Cell, Envelope, EvaluationRequest,
                       PhysicalAttempt, Selection, digest)
from .fake import BOUND, ZERO, load_fixture
from .native_transport import DeterministicProvider
from .providers import PhysicalRequest

REFERENCE, CANDIDATE = "gen_eval_fixture", "gen_eval_fixture_cand"
OWNER = "trusted-coordinator"
AUTH = "fixture-authorized-host-only"
VARIANT = "CANDIDATE fixture instruction variant.\n"
SDK_WHEEL = "sha256:dd403024cd5a893a71c852afdb2dcf175b9c91bf8eaec12f0544ad387ca62db9"
# Host-owned evaluator grants for the finite pilot: every tool the pilot's
# step is shown or runs, named explicitly (no derivation from the graph).
PILOT_GRANTS = {
    "agent": ["ask_more_turns", "create_findings", "edit_findings", "finish_step", "list",
              "list_tree", "read", "recall_observation", "search", "write_findings"],
    "context_source": ["dir_tree"],
}


def versions_class():
    """The accepted shared versioning application module.

    ``RSI_INTERIM_VERSIONING=1`` (development probes only, never a proof run)
    loads the accepted 5ebe binding from its own contract directory instead,
    for use before the shared application module is composed."""
    if os.environ.get("RSI_INTERIM_VERSIONING") == "1":
        import native_provider
        return native_provider.NativeVersions, "interim:native_provider.NativeVersions(5ebe)"
    from core.pipeline_versions import PipelineVersions
    return PipelineVersions, "core.pipeline_versions.PipelineVersions"


def _self(value, target):
    if isinstance(value, str):
        return value.replace("gen_eval_fixture", target)
    if isinstance(value, list):
        return [_self(v, target) for v in value]
    if isinstance(value, dict):
        return {_self(k, target): _self(v, target) for k, v in value.items()}
    return value


def _replacement_dir_tree(**kwargs):
    """A different served provider for ``dir_tree`` (tool provider drift)."""
    return {"content": "replaced provider"}


class NativeWorld:
    """One durable host under ``root``; re-openable by a second process."""

    def __init__(self, root: Path, *, fresh: bool, endpoint_fallback: bool = True):
        from api import dependencies as deps
        from core import datadir, pipeline_frozen_bundle as fb
        from core.config_registry import ConfigRegistry
        from core.db_manager import DBManager
        from core.skillflow_host import AItelierSkillFlow
        from core.workspace_manager import WorkspaceManager
        self.root = Path(root)
        self.endpoint_fallback = endpoint_fallback
        self.deps = deps
        self._saved_env = {k: os.environ.get(k) for k in (
            "AITELIER_HOME", "AITELIER_GENERATED_CONFIGS_DIR", "AITELIER_SECRETS_DIR")}
        self._saved = {k: getattr(deps, k) for k in (
            "db_instance", "_tool_loader_instance", "_skillflow_instance",
            "_config_registry_instance")}
        os.environ["AITELIER_HOME"] = str(self.root / "home")
        self.gen = self.root / "gen"
        os.environ["AITELIER_GENERATED_CONFIGS_DIR"] = str(self.gen)
        self.secrets = self.root / "secrets"
        os.environ["AITELIER_SECRETS_DIR"] = str(self.secrets)
        for d in (self.gen, self.secrets, self.root / "runtime", self.root / "private",
                  self.root / "public-package", self.root / "public-cells"):
            d.mkdir(parents=True, exist_ok=True)
        self.db = DBManager(str(self.root / "host.db"))
        deps.db_instance = self.db
        deps._tool_loader_instance = None
        self.loader = deps.get_tool_loader()
        self.tools = datadir.tools_dir()
        self.sf = AItelierSkillFlow(str(self.root / "sf.db"), tool_loader=self.loader,
                                    workspace_base=str(self.root / "ws"),
                                    projects_base=str(self.root / "projects"),
                                    code_path_resolver=deps._existing_repo_code_path,
                                    trace_db_path=str(self.root / "ws"))
        self.registry = ConfigRegistry()
        deps._skillflow_instance = self.sf
        deps._config_registry_instance = self.registry
        fb.pair_registry(self.sf, self.registry)
        self.ws = WorkspaceManager(str(self.root / "ws"), projects_base=str(self.root / "projects"))
        self.fixture = load_fixture()
        self.learned_roles: list[str] = []
        if fresh:
            self._create()
        self._register()

    # -- durable inputs ----------------------------------------------------
    def _create(self) -> None:
        unit = self.fixture["unit"]
        for target, prompt_extra in ((REFERENCE, ""), (CANDIDATE, VARIANT)):
            graph = _self(unit["graph"], target)
            roles = _self(unit["roles"], target)
            for role in roles.values():
                role["system_prompt"] = role["system_prompt"] + prompt_extra
            (self.gen / f"{target}.yaml").write_text(
                yaml.safe_dump(graph, sort_keys=False, allow_unicode=True), encoding="utf-8")
            (self.gen / f"{target}.roles.json").write_text(
                json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")
        bench = self.fixture["benchmark"]
        (self.root / "public-package" / "public.json").write_text(
            json.dumps(bench["public"], sort_keys=True, indent=1), encoding="utf-8")
        (self.root / "private" / "checker").mkdir(parents=True, exist_ok=True)
        (self.root / "private" / "checker" / "private.json").write_text(
            json.dumps(bench["private"], sort_keys=True, indent=1), encoding="utf-8")
        repo = Path(__file__).resolve().parents[3]
        providers = json.loads((repo / "llm_providers.example.json").read_text(encoding="utf-8"))
        providers = {k: v for k, v in providers.items() if k in ("ark", "deepseek")}
        (self.root / "runtime" / "llm_providers.json").write_text(
            json.dumps(providers, indent=1), encoding="utf-8")
        # Like every production route: the profile's ONE concrete endpoint
        # first and a pay-as-you-go fallback behind it, which the profile never
        # admits. ``endpoint_fallback=False`` freezes a single-candidate route.
        route = ({"rotate": ["ark/deepseek-v4-flash"], "fallback": ["deepseek/deepseek-v4-flash"]}
                 if self.endpoint_fallback else ["ark/deepseek-v4-flash"])
        (self.root / "runtime" / "model_routes.json").write_text(json.dumps({
            "ark/deepseek-v4-flash": route}, indent=1), encoding="utf-8")
        for name in ("ARK_API_KEY", "DEEPSEEK_API_KEY"):
            (self.secrets / name).write_text("fixture-not-a-secret\n", encoding="utf-8")
        host = {
            "grants": {AUTH: {"scope_id": "fixture-scope", "campaign_id": "fixture-campaign",
                              "tools": copy.deepcopy(PILOT_GRANTS),
                              "holdout_selections": [digest("host-sealed-holdout-selection")]}},
            "endpoints": {
                "ark-coding-v3": {"provider": "ark", "model": "deepseek-v4-flash",
                                  "base_url": providers["ark"]["base_url"]},
                "deepseek-paygo": {"provider": "deepseek", "model": "deepseek-v4-flash",
                                   "base_url": providers["deepseek"]["base_url"]}},
            "pricing": {"schema": "rsi.pricing.v1", "source": "fixture schedule, not live prices",
                        "unit": "usd_micros per 1M tokens", "input_overhead_tokens": 512,
                        "cached_discount": False, "error_status_billable": False,
                        "endpoints": {"ark/deepseek-v4-flash@ark-coding-v3":
                                      {"input": 2_000_000, "output": 8_000_000}}},
            "deployment": {"app_source": os.environ.get("RSI_SOURCE_SHA", "0" * 40),
                           "image": os.environ.get("RSI_IMAGE_ID", "sha256:" + "0" * 64),
                           "sdk_wheel": SDK_WHEEL}}
        (self.root / "host.json").write_text(json.dumps(host, indent=1), encoding="utf-8")
        from core.pipeline_benchmark import BenchmarkStore
        store = BenchmarkStore(self.root / "private" / "benchmark" / "store.json")
        header = bench["header"]
        store.create_draft(OWNER, header)
        for cid in header["cases"]:
            store.set_public_input(OWNER, header["benchmark_id"], 1, cid,
                                   {"prompt": bench["public"][cid]["prompt"]})
            store.set_private_refs(OWNER, header["benchmark_id"], 1, cid, bench["private"][cid]["refs"])
        store.publish(OWNER, header["benchmark_id"], 1)

    def _register(self) -> None:
        """Ordinary registration (boot-equivalent), capture/seal, sealed registration."""
        from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
        from core.pipeline_benchmark import BenchmarkStore
        for target in (REFERENCE, CANDIDATE):
            out = pr.reload_generated_pipeline(self.sf, self.registry, target)
            assert "error" not in out, out
        cls, self.versions_impl = versions_class()
        self.versions = cls(self.sf, self.registry, self.gen)
        self.captured = {}
        for arm, target in (("reference", REFERENCE), ("candidate", CANDIDATE)):
            cap = self.versions.capture(target)
            fb.register_sealed(self.sf, self.registry, cap["physical_name"], self.gen)
            self.captured[arm] = cap
        self.store = BenchmarkStore(self.root / "private" / "benchmark" / "store.json")
        self.host_doc = json.loads((self.root / "host.json").read_text(encoding="utf-8"))

    def host(self, provider):
        from core import rsi_admission
        from core.rsi_evaluator import EvaluatorHost
        doc = self.host_doc
        return EvaluatorHost(
            sf=self.sf, registry=self.registry, db=self.db, ws=self.ws, versions=self.versions,
            config_dir=self.gen, store=self.store, store_owner=OWNER,
            public_package=self.root / "public-package" / "public.json",
            private_package=self.root / "private" / "checker" / "private.json",
            public_fixture_root=self.root / "public-cells",
            artifacts_root=self.root / "private" / "evaluator",
            grants=doc["grants"], deployment=doc["deployment"], endpoints=doc["endpoints"],
            endpoint_label="ark-coding-v3",
            providers_path=self.root / "runtime" / "llm_providers.json",
            routes_path=self.root / "runtime" / "model_routes.json",
            pricing=rsi_admission.Pricing(doc["pricing"]), transport=lambda: provider,
            cell_timeout_ms=300000)

    def close(self) -> None:
        from core import agents
        for role in self.learned_roles:
            agents._LEARNED_OUTPUT_CAPS.pop(role, None)
        try:
            self.sf._conn.close()
        except Exception:                                            # noqa: BLE001
            pass
        for k, v in self._saved.items():
            setattr(self.deps, k, v)
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class NativeFixture:
    """``conformance.FixtureBinding`` over the genuine host (evidence: native)."""
    evidence_kind = "native"

    def __init__(self, base: Path | None = None, *, endpoint_fallback: bool = True):
        self.root = Path(tempfile.mkdtemp(prefix="native-world-", dir=base))
        self.world = NativeWorld(self.root, fresh=True, endpoint_fallback=endpoint_fallback)
        self.public = json.loads((self.root / "public-package" / "public.json").read_text())
        self.provider = DeterministicProvider(self.root / "provider-log.jsonl", self.public)
        from core.rsi_evaluator import NativeEvaluator
        self.evaluator = NativeEvaluator(self.world.host(self.provider))
        self.request = None
        self.fault = "none"

    # -- FixtureBinding ------------------------------------------------------
    def prepare(self, split="train", arms=("reference", "candidate")) -> EvaluationRequest:
        w, ev = self.world, self.evaluator
        from core import pipeline_frozen_bundle as fb, rsi_checker
        from core.rsi_policy import sdk_digest
        selections = []
        for arm in arms:
            cap = w.captured[arm]
            read = w.versions.read(cap["version_id"])
            native = read["native"]
            sealed = fb.verify_sealed(w.gen, cap["physical_name"])
            rows = w.sf.list_graph_versions(cap["physical_name"])
            selections.append(Selection(
                arm=arm, logical_version=cap["version_id"],
                native_bundle="sha256:" + native["bundle_identity"],
                native_seal="sha256:" + native["digest"], physical_config=cap["physical_name"],
                sdk_graph_version=rows[0]["version"], sdk_graph_digest=sdk_digest(rows[0]["digest"]),
                roles=digest(sealed["roles"]), templates=digest(sealed["prompts"]),
                tools=digest(sealed["tools"]), capabilities=digest(sealed["capabilities"])))
        bench = w.fixture["benchmark"]
        header = bench["header"]
        definition = w.store.get_definition(header["benchmark_id"], 1)
        cases = tuple(Case(case_id=cid, split=meta["partition"], family=meta["family"],
                           prompt_identity=digest({"prompt": bench["public"][cid]["prompt"]}),
                           fixture_tree=digest(bench["public"][cid]["files"]),
                           scorer_ref_identity=digest(bench["private"][cid]["refs"]),
                           resolved_private_material=digest(bench["private"][cid]["material"]),
                           max_points=bench["private"][cid]["refs"]["max_points"])
                      for cid, meta in header["cases"].items())
        code = rsi_checker.identity()
        benchmark = Benchmark(benchmark_id=header["benchmark_id"], version=1,
                              definition=definition["definition_digest"],
                              public_package=digest(bench["public"]),
                              private_package=digest(bench["private"]), checker_code=code,
                              checker_package=rsi_checker.package_identity(code, cases), cases=cases)
        policy, compiled, _ = ev.compile_for(tuple(selections), AUTH, "ark/deepseek-v4-flash",
                                             benchmark.public_package)
        runtime = ev.served_runtime(compiled, policy.identity)
        ceiling = Envelope(upper=Amount(requests=1000, input_tokens=16000000,
                                        output_tokens=2000000, usd_micros=25000000),
                           elapsed_ms=14400000)
        budget = Budget(scope_id="fixture-scope", authorization_ref=AUTH,
                        pricing_identity=runtime.pricing,
                        cell=Envelope(upper=Amount(requests=8, input_tokens=128000,
                                                   output_tokens=16000, usd_micros=1000000),
                                      elapsed_ms=300000),
                        round=ceiling, campaign=ceiling)
        cells = tuple(Cell(case_id=c.case_id, arm=a, repetition=r)
                      for r in range(1, 4) for c in cases if c.split == split for a in arms)
        data = dict(schema="rsi.evaluation.request.v2", evaluation_id="fixture-evaluation",
                    campaign_id="fixture-campaign", round_id="fixture-round", split=split,
                    repetitions=3, max_parallel=2, selections=tuple(selections),
                    benchmark=benchmark, cells=cells, runtime=runtime, budget=budget,
                    holdout_selection=digest("host-sealed-holdout-selection")
                    if split == "holdout" else None)
        self.request = EvaluationRequest(plan_identity=digest(data), **data)
        self.provider.first_cell = (cells[0].case_id, cells[0].arm)
        return self.request

    def set_fault(self, name: str) -> None:
        from .conformance import SCENARIOS
        if name not in {s["fault"] for s in SCENARIOS}:
            raise ValueError("unknown fixture fault")
        self.fault = name
        handler = getattr(self, "_fault_" + name, None)
        if handler is not None:
            handler()
        else:
            self.provider.fault = name

    def evaluate(self, request):
        return self.evaluator.evaluate(request)

    def observations(self) -> dict:
        w = self.world
        runs = [r for r in w.sf.list_runs()
                if "_rsi_evaluation" in json.loads(r.get("context_json") or "{}")]
        refusals = w.root / "private" / "evaluator" / "refusals"
        receipts = list((w.root / "private" / "evaluator" / "evaluations").rglob("receipts/*.json"))
        return {"evidence_kind": self.evidence_kind, "launches": len(runs),
                "physical_sends": len(self.provider.received()),
                "scoreboard_rows": len(w.store.list_receipts("evaluator-fixture", 1)),
                "denials": len(list(refusals.glob("*.json"))) if refusals.is_dir() else 0,
                "retained_receipts": len(receipts)}

    def probe_admission(self, vector: dict) -> dict:
        """The shared vectors over the production ``PhysicalBudget``."""
        from core.rsi_admission import AdmissionDenied, PhysicalBudget
        before = len(self.provider.received())
        budget = PhysicalBudget({n: (BOUND, 10) for n in ("a", "b", "campaign")})
        cell = self.request.cells[0]

        def request(i, elapsed=0):
            return PhysicalRequest(descriptor_id=f"probe-{i}", evaluation_id=self.request.evaluation_id,
                                   cell=cell, runtime=self.request.runtime, cause="initial",
                                   upper=BOUND, elapsed_cell_ms=elapsed, elapsed_round_ms=elapsed,
                                   elapsed_campaign_ms=elapsed)

        def attempt(res, settlement, measured, sent):
            return PhysicalAttempt(attempt_id=res.reservation_id, cause="initial",
                                   effective_runtime=self.request.runtime, reservation=BOUND,
                                   settlement=settlement, measured=measured, observed_send=sent)
        admitted = denied = 0
        retryable = False
        kind = vector["kind"]
        if kind == "concurrent":
            barrier = threading.Barrier(2)
            results = {}

            def contend(i, scope):
                barrier.wait()
                try:
                    budget.reserve(request(i), (scope, "campaign"))
                    results[scope] = True
                except AdmissionDenied as exc:
                    results[scope] = exc
            threads = [threading.Thread(target=contend, args=(i, s)) for i, s in ((1, "a"), (2, "b"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            admitted = sum(v is True for v in results.values())
            denied = 2 - admitted
            loser = next(s for s, v in results.items() if v is not True)
            retryable = results[loser].retryable
            if budget.used(loser) != ZERO:
                raise AssertionError("failed reservation partially mutated its scope")
        elif kind == "unknown":
            res = budget.reserve(request(1), ("a", "campaign")); admitted += 1
            budget.settle(res, attempt(res, "unknown", None, True))
            try:
                budget.reserve(request(2), ("b", "campaign")); admitted += 1
            except AdmissionDenied as exc:
                denied += 1; retryable = exc.retryable
        elif kind == "deadline":
            try:
                budget.reserve(request(1, elapsed=10), ("a", "campaign")); admitted += 1
            except AdmissionDenied as exc:
                denied += 1; retryable = exc.retryable
        elif kind == "no_send":
            for i, scope in ((1, "a"), (2, "b")):
                res = budget.reserve(request(i), (scope, "campaign")); admitted += 1
                budget.settle(res, attempt(res, "positive_no_send", ZERO, False))
        else:
            raise ValueError("unknown admission vector")
        return {"evidence_kind": self.evidence_kind, "admitted": admitted, "denied": denied,
                "retained_requests": budget.used("campaign").requests,
                "physical_sends": len(self.provider.received()) - before,
                "denial_retryable": retryable}

    # -- faults that change actual host inputs -------------------------------
    def _grant(self) -> dict:
        return self.evaluator.host.grants[AUTH]

    def _fault_empty_grants(self):
        self._grant()["tools"] = {"agent": [], "context_source": []}

    def _fault_missing_grants(self):
        self._grant().pop("tools")

    def _fault_private_root(self):
        # The cell read root configured at the parent that also holds the
        # private checker package and the evaluator's private artifacts.
        self.evaluator.host.public_fixture_root = self.root / "private"

    def _install_private_reader(self):
        tdir = self.world.tools / "private_reader"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "tool.yaml").write_text(yaml.safe_dump({
            "name": "private_reader", "description": "reads a file",
            "parameters": {"path": {"type": "string", "required": False}}}), encoding="utf-8")
        (tdir / "impl.py").write_text(
            "def private_reader(path='', **kwargs):\n"
            f"    return {{'content': open({str(self.root / 'private' / 'checker' / 'private.json')!r}).read()}}\n",
            encoding="utf-8")

    def _reregister(self, mutate):
        """Serve different bytes under the reference seal's physical name,
        through the host's own validated registration path."""
        from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
        w = self.world
        phys = w.captured["reference"]["physical_name"]
        sealed = fb.verify_sealed(w.gen, phys)
        data = yaml.safe_load(sealed["graph_yaml"])
        mutate(data["steps"][0])
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        graph, hints = pr._validated_registration(phys, text, roles=sealed["roles"], sf=w.sf)
        pr._commit_registration(w.sf, w.registry, phys, graph, fb.sealed_hints(hints, sealed),
                                roles=sealed["roles"], require_manifest=True)

    def _fault_hidden_source_tool(self):
        self._install_private_reader()
        self._reregister(lambda step: step["context"].append({"source": {"tool": "private_reader"}}))

    def _fault_cross_run(self):
        self._reregister(lambda step: step["context"].append(
            {"source": {"config": "gen_other_private", "output": "private.json"}}))

    def _fault_dynamic_capability(self):
        self._reregister(lambda step: step.__setitem__("capability", {"from_item": "capabilities"}))

    def _fault_hook(self):
        self._install_private_reader()
        self._reregister(lambda step: step.__setitem__("validation", [{"tool": "private_reader"}]))

    def _fault_extra_template(self):
        self._reregister(lambda step: step.setdefault("config", {}).__setitem__(
            "extra_templates", ["unrecorded.md"]))

    def _fault_provider_changed(self):
        loader = self.world.loader
        loader.register_dynamic_tool("dir_tree", loader.load_schema("dir_tree"),
                                     _replacement_dir_tree)

    def _fault_provider_absent(self):
        path = self.root / "runtime" / "llm_providers.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc.pop("ark")
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")

    def _fault_fixture_changed(self):
        path = self.root / "public-package" / "public.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["train-01"]["files"]["sample.txt"] = "PUBLIC_FIXTURE_train-01 = 99\n"
        path.write_text(json.dumps(doc, sort_keys=True, indent=1), encoding="utf-8")

    def _fault_checker_changed(self):
        path = self.root / "private" / "checker" / "private.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["train-01"]["material"]["expected_constant"] = 99
        path.write_text(json.dumps(doc, sort_keys=True, indent=1), encoding="utf-8")

    def _fault_unknown_version(self):
        from core import pipeline_frozen_bundle as fb
        for path in fb.sealed_paths(self.world.gen, self.world.captured["candidate"]["physical_name"]):
            path.unlink()

    def _fault_runtime_preflight(self):
        # A hidden output-cap source: the process-wide learned cap AgentFactory
        # applies when it builds a role's gateway.
        from core import agents
        for selection in self.request.selections:
            role = f"{selection.physical_config}__investigator"
            agents.remember_output_cap(role, 16384)
            self.world.learned_roles.append(role)

    def _fault_runtime_midcell(self):
        routes = self.root / "runtime" / "model_routes.json"

        def operator_edit(_record):
            # An operator route edit while cell 0 is in flight, through the
            # same cache reset the model registry's own writes perform.
            from core.model_routes import reset_cache
            doc = json.loads(routes.read_text(encoding="utf-8"))
            route = doc["ark/deepseek-v4-flash"]
            route = route if isinstance(route, dict) else {"rotate": list(route)}
            route["effort"] = {"ark/deepseek-v4-flash": "max"}
            doc["ark/deepseek-v4-flash"] = route
            routes.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            reset_cache()
        self.provider.hooks["runtime_midcell"] = operator_edit
        self.provider.fault = "runtime_midcell"

    def _fault_checkpoint(self):
        sf = self.world.sf

        def operator_pause(_record):
            for run in sf.list_runs(status="running"):
                if "_rsi_evaluation" in json.loads(run.get("context_json") or "{}"):
                    sf.pause_run(run["id"])
        self.provider.hooks["checkpoint"] = operator_pause
        self.provider.fault = "checkpoint"

    def _fault_unsafe_resume(self):
        """A real evaluator process starts this evaluation and is killed while
        its first cell's physical call is in flight."""
        req = self.root / "partial-request.json"
        req.write_text(self.request.model_dump_json(), encoding="utf-8")
        log = open(self.root / "partial-child.log", "w", encoding="utf-8")
        before = len(self.provider.received())
        child = subprocess.Popen([sys.executable, "-m", "contracts.rsi.evaluator.native_child",
                                  str(self.root), str(req)], stdout=log, stderr=subprocess.STDOUT,
                                 env=dict(os.environ), cwd=str(Path(__file__).resolve().parents[3]))
        deadline = time.monotonic() + 120
        while len(self.provider.received()) <= before:
            if child.poll() is not None or time.monotonic() > deadline:
                raise AssertionError("partial evaluator did not reach its first physical send; "
                                     f"see {self.root / 'partial-child.log'}")
            time.sleep(0.05)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=30)
        log.close()
        self.killed_child = {"pid": child.pid, "returncode": child.returncode}

    def close(self) -> None:
        self.provider.close()
        # A released late reply settles (as a late fact) on its own thread;
        # let that finish before the world's SQLite connections close.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(
                t.name.startswith("llm-call") and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.05)
        self.world.close()


class NativeFactory:
    """Fresh native world per scenario, closed afterwards by the harness user."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.made: list[NativeFixture] = []

    def __call__(self) -> NativeFixture:
        for old in self.made:
            old.close()
        fixture = NativeFixture(self.base)
        self.made = [fixture]
        return fixture

    def close(self):
        for fixture in self.made:
            fixture.close()
        self.made = []
