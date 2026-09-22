"""AItelier composition: trusted project policy, State rulings, and checkpoints.

The domain package never discovers production resources. This adapter is invoked
only by a registered versioned graph with framework-injected run identity.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

import yaml

from core import datadir
from .bench import Bench, Policy
from .storage import (BenchError, TREE_LIMIT, checked_root, decode, encode, identifier, immutable,
                      lock, read_file, require, sha)

CONFIG = "novel_writing_bench_v2"
ROLE_FILES = {"literary": "bench_literary.md", "ledger": "bench_ledger_audit.md",
              "extractor": "bench_ledger_extract.md"}


class Host:
    def __init__(self):
        from api.dependencies import get_db_manager, get_skillflow
        from core.state_service import StateService
        self.sf = get_skillflow()
        self.state = StateService(get_db_manager(), actor="writing-bench-run",
                                  project_read_trusted=True)

    def review_contracts(self, policy_document: dict) -> dict:
        files = contracts(policy_document)
        effective = self.sf.agent_registry.to_dict()
        names = {"literary": "bench_literary_v2", "ledger": "bench_ledger_auditor_v2",
                 "extractor": "bench_ledger_extractor_v2"}
        require(all(name in effective for name in names.values()), "writing reviewer roles missing")
        return {kind: sha(encode({"files": files[kind], "effective_role": effective[name]}))
                for kind, name in names.items()}

    def rulings(self, project_id: str) -> dict:
        index = self.state.driver_notes.entry_index(project_id, limit=500)
        require(not index.get("truncated"), "driver note index truncated")
        entries = []
        for item in index["entries"]:
            if item.get("force") != "in_force":
                continue
            value = self.state.driver_notes.get_entry(project_id, item["entry_id"])
            if value.get("superseded_by"):
                continue
            entries.append({"address": f"note://{project_id}/{item['entry_id']}",
                            "assertion": value["assertion"], "body": value["body"]})
        return {"project_id": project_id, "entries": sorted(entries, key=lambda e: e["address"])}

    def ensure_run(self, run_id: str, step_id: str, workspace: Path) -> None:
        run = self.sf.get_run(run_id)
        require(run is not None and run["graph_name"] == CONFIG and run["status"] == "running",
                "active writing-bench run required")
        require(run["current_node"] == step_id, "step is not the current run operation")
        expected = Path(self.sf._workspace.get_project_path(run["project_id"]))
        require(expected == workspace, "framework workspace mismatch")
        resolver = self.sf._get_resolver_for_run(run_id)
        require([n.id for n in resolver.graph.steps if n.checkpoint] == ["stage"],
                "writing bench requires its single manual acceptance gate")

    def approval(self, run_id: str) -> str:
        # Approval comes from the engine's durable event and stage result, never
        # a seed boolean or a passed=true supplied by a reviewer.
        decision, before = None, None
        while decision is None:
            rows = self.sf.get_trace(run_id, category="step", order="desc",
                                     before_seq=before, limit=100)
            if not rows:
                break
            decision = next((r for r in rows if r["event"] in
                             ("checkpoint_approved", "checkpoint_rejected")), None)
            if decision is not None or len(rows) < 100:
                break
            cursor = rows[-1].get("seq")
            require(type(cursor) is int and cursor > 0 and (before is None or cursor < before),
                    "checkpoint trace cursor did not advance")
            before = cursor
        require(decision is not None and decision["event"] == "checkpoint_approved"
                and decision["payload"].get("step_id") == "stage", "manual checkpoint approval required")
        stages = [s for s in self.sf.get_steps(run_id, include_payloads=True)
                  if s["step_id"] == "stage" and s["status"] == "completed"]
        require(len(stages) == 1, "ambiguous approved stage")
        flags = decode((stages[0].get("result_flags_json") or "{}").encode())
        require(isinstance(flags.get("stage_sha256"), str), "engine stage fingerprint missing")
        return flags["stage_sha256"]


def contracts(policy_document: dict) -> dict:
    source = Path(__file__).resolve().parents[2]
    roles = read_file(source, "agent_configs/novel_writing_bench_v2.yaml")
    return {kind: sha(encode({"role_file": sha(roles), "template": sha(read_file(source, "templates/" + file)),
                             "operator_policy": sha(encode(policy_document))}))
            for kind, file in ROLE_FILES.items()}


def load_policy(project_id: str) -> tuple[Policy, dict]:
    identifier(project_id)
    home = datadir.writing_bench_dir()
    document = decode(read_file(home, "projects.json"))
    require(document.get("version") == 2 and project_id in document.get("projects", {}),
            "operator has not enabled this writing project")
    conf = document["projects"][project_id]
    require(set(conf) <= {"repo", "branch", "genesis", "submission_root", "max_context_bytes", "backup", "review_revision"},
            "unknown operator policy field")
    policy = Policy(project_id, Path(conf["repo"]), conf["branch"], conf["genesis"],
                    Path(conf["submission_root"]), home / "artifacts", conf.get("max_context_bytes", 180_000))
    return policy, conf


def _request(cfg: Path) -> dict:
    # Supports the host's ordinary seeded graph layout, never a path supplied by
    # an LLM. Different contents in two seed locations are an explicit conflict.
    found = []
    for name in ("_seed/request.json", "request.json"):
        if (cfg / name).is_file():
            found.append(read_file(cfg, name, 64000))
    require(found and all(raw == found[0] for raw in found), "missing or conflicting seed request")
    return decode(found[0])


def _setup(workspace_root: str, run_id: str, step_id: str, config_name: str,
           out_dir: str) -> tuple:
    require(config_name == CONFIG and run_id and workspace_root, "injected bench run context required")
    workspace = checked_root(Path(workspace_root))
    cfg = workspace / CONFIG
    out = checked_root(Path(out_dir))
    require(out.is_relative_to(workspace), "output outside run workspace")
    request = _request(cfg)
    policy, conf = load_policy(request.get("project_id"))
    bench = Bench(policy)
    host = Host()
    host.ensure_run(run_id, step_id, workspace)
    return cfg, out, request, bench, host, conf


def _source_run(request: dict, run_id: str) -> str:
    return identifier(request["source_run_id"]) if request.get("operation") == "backup_only" else run_id


def _current_inputs(bench: Bench, host: Host, conf: dict, run_id: str) -> None:
    path, m = bench.input(run_id)
    require(m["contracts"] == host.review_contracts(conf), "review role, template or operator policy changed")
    require(read_file(path, "rulings.json") == encode(host.rulings(bench.policy.project_id)),
            "effective driver rulings changed; review new inputs")


def private_backup(bench: Bench, source_run: str, expected_commit: str, conf: dict) -> dict:
    # Serialize recovery and ordinary delivery with promotion for this project.
    # A second executor may re-verify, but cannot overlap a push or an acceptance.
    with lock(bench.root / ".delivery.lock"):
        return _private_backup_locked(bench, source_run, expected_commit, conf)


def _private_backup_locked(bench: Bench, source_run: str, expected_commit: str, conf: dict) -> dict:
    """Call a pinned operator-approved backup engine; never acquire credentials here."""
    spec = conf.get("backup")
    require(isinstance(spec, dict) and set(spec) == {"tool", "config", "source_sha256", "seed"},
            "private backup adapter is not configured")
    tool = identifier(spec["tool"])
    engine_config = identifier(spec["config"])
    raw = read_file(datadir.tools_dir(), tool + "/impl.py")
    require(sha(raw) == spec["source_sha256"], "backup engine changed; operator review required")
    receipt = bench.accepted(source_run, expected_commit)
    seed = dict(spec["seed"])
    require(seed.get("repo_path") == str(bench.policy.repo) and seed.get("branch") == bench.policy.branch
            and seed.get("state_project_id") == bench.policy.project_id, "backup policy target mismatch")
    seed.update(expected_head=expected_commit, expected_tag=receipt["genesis"])
    cfg = bench.work(source_run) / "backup-attempts" / uuid.uuid4().hex / engine_config
    immutable(cfg / "_seed/seed_input.md", encode(seed))
    module_spec = importlib.util.spec_from_file_location("_bench_backup_" + spec["source_sha256"],
                                                      datadir.tools_dir() / tool / "impl.py")
    module = importlib.util.module_from_spec(module_spec)
    # Execute the exact bytes just checked, not a second read of a mutable file.
    exec(compile(raw, str(datadir.tools_dir() / tool / "impl.py"), "exec"), module.__dict__)
    try:
        result = getattr(module, tool)(config_dir=str(cfg), out_dir=str(cfg / "push"), operation="push")
        proof = decode(read_file(cfg / "push", "backup_result.json"))
        require(result.get("verified") is True, "backup engine did not verify")
        require(proof.get("repository_full_name") == seed["owner"] + "/" + seed["repo_name"],
                "backup proof repository mismatch")
        # A retry performs a fresh remote verification. Keep the first receipt
        # for exactly this accepted commit; preserve each new probe separately.
        if (bench.work(source_run) / "completed.json").exists():
            bench.record_backup(source_run, expected_commit,
                                bench._json(bench.work(source_run), "backup.json"))
            require(proof.get("remote_master_after") == expected_commit
                    and proof.get("remote_tag_after") == receipt["genesis"]
                    and proof.get("repository_private_after") is True, "remote drift after saved backup")
            return bench._json(bench.work(source_run), "completed.json")
        return bench.record_backup(source_run, expected_commit, proof)
    except Exception:
        # Do not copy credential-bearing command output to a user-visible failure.
        raise BenchError("Private backup incomplete; accepted content retained. Resume backup_only for the same commit.") from None


def novel_bench(*, operation: str, workspace_root: str = "", run_id: str = "",
                step_id: str = "", config_name: str = "", out_dir: str = "", **kwargs) -> dict:
    expected_steps = {"prepare", "literary_check", "ledger_ready", "stage", "promote", "backup", "rejected"}
    require(operation in expected_steps and operation == step_id, "wrong bench operation/step")
    cfg, out, request, bench, host, conf = _setup(workspace_root, run_id, step_id, config_name, out_dir)
    if operation == "prepare":
        if request.get("operation") == "backup_only":
            require(set(request) == {"version", "operation", "project_id", "source_run_id", "expected_commit"}
                    and request["version"] == 2, "invalid backup recovery request")
            source = _source_run(request, run_id)
            original = host.sf.get_run(source)
            require(original is not None and original["graph_name"] == CONFIG
                    and original["status"] in ("completed", "failed", "stopped", "cancelled"),
                    "settle or stop the original backup executor before recovery")
            receipt = bench.accepted(source, request["expected_commit"])
            immutable(out / "recovery.json", encode(receipt))
            return {"backup_only": True}
        bench.freeze(request, run_id, host.rulings(bench.policy.project_id), host.review_contracts(conf))
        path, m = bench.input(run_id)
        immutable(out / "editor_packet.md", bench.editorial_packet(run_id).encode())
        missing = [c["chapter"] for c in m["chapters"] if not c["provided_ledger"]]
        immutable(out / "extraction_request.json", encode({"extract_chapters": missing,
                  "format": "one JSON object keyed by decimal chapter number, each value a complete ledger"}))
        reuse = bool(request.get("reuse_literary_from"))
        if reuse:
            bench.literary(run_id)
        return {"backup_only": False, "reuse_literary": reuse}
    if operation == "rejected":
        result = {"status": "changes_requested", "accepted": False,
                  "next_action": "Revise author files and submit a new ID; no automatic rewrite or acceptance."}
        immutable(out / "disposition.json", encode(result))
        return result
    if operation == "literary_check":
        report = None if request.get("reuse_literary_from") else decode(read_file(cfg, "literary_review/review.json"))
        receipt = bench.literary(run_id, report)
        immutable(out / "literary_receipt.json", encode(receipt))
        _, m = bench.input(run_id)
        return {"needs_extraction": any(not c["provided_ledger"] for c in m["chapters"])}
    if operation == "ledger_ready":
        _, m = bench.input(run_id)
        missing = any(not c["provided_ledger"] for c in m["chapters"])
        extracted = decode(read_file(cfg, "extract_ledger/ledgers.json")) if missing else None
        ledger = bench.ledgers(run_id, extracted)
        immutable(out / "audit_packet.md", bench.audit_packet(run_id).encode())
        return {"review_key": ledger["review_key"]}
    if operation == "stage":
        _current_inputs(bench, host, conf, run_id)
        result = bench.stage(run_id, decode(read_file(cfg, "ledger_audit/review.json")))
        raw = read_file(bench.work(run_id), "stage.json", TREE_LIMIT)
        immutable(out / "stage_report.json", raw)
        immutable(out / "semantic_changes.json", read_file(bench.work(run_id), "semantic_changes.json", TREE_LIMIT))
        immutable(out / "candidate.patch", read_file(bench.work(run_id), "candidate.patch", TREE_LIMIT))
        path, m = bench.input(run_id)
        for ch in m["chapters"]:
            immutable(out / f"chapter_{ch['chapter']:04d}.md", read_file(path, f"chapters/ch{ch['chapter']:04d}/prose.md"))
        for file in ("literary.json", "audit.json", "ledgers.json"):
            immutable(out / file, read_file(bench.work(run_id), file, TREE_LIMIT))
        manual = {"status": "awaiting_manual_approval", "commit": result["commit"], "stage_sha256": sha(raw),
                  "normal_wait": True, "effects_on_approval": "exact acceptance then private backup",
                  "no_decision": "No acceptance, no timeout escalation, no automatic approval."}
        immutable(out / "approval_manifest.json", encode(manual))
        immutable(out / "review_bundle.md", ("# 导演终审\n\n请阅读完整本章、独立编辑意见、分录及 semantic_changes.json；"
                  "candidate.patch 保留所有实际变更。\n\n等待手动批准是正常状态；没有点击不会接受。\n\n"
                  + encode(manual).decode()).encode())
        return manual
    if operation == "promote":
        _current_inputs(bench, host, conf, run_id)
        approved = host.approval(run_id)
        require(sha(read_file(cfg, "stage/stage_report.json", TREE_LIMIT)) == approved, "displayed approval artifact changed")
        receipt = bench.promote(run_id, approved)
        immutable(out / "accepted.json", encode(receipt))
        immutable(out / "backup_only_seed.json", encode({"version": 2, "operation": "backup_only",
                  "project_id": bench.policy.project_id, "source_run_id": run_id,
                  "expected_commit": receipt["accepted_commit"]}))
        return receipt
    source = _source_run(request, run_id)
    receipt = bench._json(bench.work(source), "accepted.json")
    expected = request["expected_commit"] if request.get("operation") == "backup_only" else receipt["accepted_commit"]
    result = private_backup(bench, source, expected, conf)
    immutable(out / "completion.json", encode(result))
    return result


def novel_bench_read(*, path: str, start: int = 0, length: int = 12000,
                     config_name: str = "", run_id: str = "", step_id: str = "") -> dict:
    require(config_name == CONFIG and run_id and step_id in
            ("literary_review", "extract_ledger", "ledger_audit"), "frozen writing-bench review context required")
    # Agent tools receive a code root while tool steps receive an artifact root.
    # Neither a model parameter nor that code-root alias selects our baseline.
    host = Host()
    run = host.sf.get_run(run_id)
    require(run is not None and run.get("project_id"), "unknown review run")
    workspace = checked_root(Path(host.sf._workspace.get_project_path(run["project_id"])))
    host.ensure_run(run_id, step_id, workspace)
    request = _request(workspace / CONFIG)
    policy, _ = load_policy(request.get("project_id"))
    return Bench(policy).read(run_id, path, start, length)
