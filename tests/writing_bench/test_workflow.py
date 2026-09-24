"""Real SkillFlow routing, tool invocation, Git and explicit approval.

Only model-produced text, operator composition and remote backup are synthetic.
No live novel, live State database or network credential is touched.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml
import skillflow
from skillflow import PipelineGraph, SkillFlow, StepResult
from skillflow.tool_loader import ToolLoader

from aitelier.writing_bench import adapter
from aitelier.writing_bench.storage import BenchError, decode, encode, git, sha
from test_bench import bench, ledger, request, verdict

ROOT = Path(__file__).resolve().parents[2]


class Session:
    def __init__(self, tmp_path, monkeypatch, bench):
        self.bench = bench
        self.rules = {"project_id": "fiction", "entries": []}
        self.fail_backup = False
        self.backup_calls = []
        self.agent_calls = []
        self.conf = {"review_revision": 1}
        loader = ToolLoader(Path(skillflow.__file__).parent / "tools", ROOT / "aitelier/tools")
        self.sf = SkillFlow(str(tmp_path / "engine.sqlite"), tool_loader=loader,
                            workspace_base=str(tmp_path / "executions"),
                            projects_base=str(tmp_path / "projects"),
                            code_path_resolver=lambda *_args, **_kwargs: None)
        for name, role in yaml.safe_load((ROOT / "agent_configs/novel_writing_bench_v2.yaml").read_text()).items():
            self.sf.register_agent_config_from_dict(name, role)
        self.sf.register_graph(PipelineGraph.from_yaml(ROOT / "configs/novel_writing_bench_v2.yaml"))
        session = self

        class TestHost(adapter.Host):
            def __init__(self):
                self.sf = session.sf

            def rulings(self, project_id):
                assert project_id == "fiction"
                return copy.deepcopy(session.rules)

        monkeypatch.setattr(adapter, "Host", TestHost)
        monkeypatch.setattr(adapter, "load_policy", lambda project: (bench.policy, self.conf)
                            if project == "fiction" else (_ for _ in ()).throw(BenchError("unknown project")))

        def backup(b, source_run, expected_commit, conf):
            self.backup_calls.append((source_run, expected_commit))
            if self.fail_backup:
                raise BenchError("simulated network failure after acceptance")
            proof = {"verified": True, "repository_private_after": True, "remote_master_after": expected_commit,
                     "remote_tag_after": b.policy.genesis, "force": False, "mirror": False}
            return b.record_backup(source_run, expected_commit, proof)

        monkeypatch.setattr(adapter, "private_backup", backup)

    def start(self, req, project="execution"):
        cfg = self.sf._workspace.get_config_path(project, adapter.CONFIG)
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "request.json").write_bytes(encode(req))
        run = self.sf.create_run(adapter.CONFIG, {"project_id": project}, project_id=project)
        self.sf.start_run(run)
        return run

    def attest(self, run, claim, value):
        from aitelier.writing_bench.reading import Coverage, ReviewSession
        phase = "literary" if claim.step_id == "literary_review" else "ledger"
        identity, materials = self.bench.review_materials(run, phase)
        value["reviewed_chapters"] = identity["targets"]
        session = ReviewSession(Coverage(phase, identity["review_key"], identity["targets"], materials),
                                self.bench.work(run) / "reading",
                                {"run_id": run, "step_id": claim.step_id,
                                 "step_instance_id": claim.token.step_instance_id,
                                 "claim_epoch": claim.token.claim_epoch})
        if getattr(self, "use_bounded_attestation", False):
            for material in materials:
                start = 0
                while start < len(material.text):
                    page = self.sf.execute_tool("novel_bench_read", {"path": "review/" + material.path,
                               "start": start, "length": 8000}, run_id=run, step_id=claim.step_id,
                               step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
                    assert "error" not in page, page
                    # A tool result is credited only when the next model input
                    # actually contains it, not when execute_tool returned it.
                    call_id = material.path + str(start)
                    session.observe([{"role": "assistant", "tool_calls": [{"id": call_id,
                        "function": {"name": "novel_bench_read", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": call_id, "content": encode(page).decode()}])
                    start = page["end"]
        else:
            session.observe([{"role": "user", "content": m.text} for m in materials])
        assert session.guard("write_verdict", value) is None

    def drive(self, run, *, reject_literary=False, read=True):
        for _ in range(70):
            result = self.sf.advance_run(run)
            row = self.sf.get_run(run)
            if row["status"] != "running":
                return row["status"]
            if result is None:
                continue
            claim = self.sf.claim_next_step(run)
            if claim is None:
                continue
            self.agent_calls.append(claim.step_id)
            token = claim.token
            common = dict(run_id=run, step_id=claim.step_id,
                          step_instance_id=token.step_instance_id, claim_epoch=token.claim_epoch)
            if read:
                fetched = self.sf.execute_tool("novel_bench_read", {"path": "novel/bible/overview.md"}, **common)
                assert "error" not in fetched, fetched
                assert "旅人" in fetched["text"] and fetched["complete"]
            resolved = str(claim.inputs.get("_resolved_context", {}))
            assert "current_prose" in resolved or "分录" in resolved or "待接受" in resolved, resolved
            if claim.step_id == "literary_review":
                _, m = self.bench.input(run)
                obj = verdict(m["literary_key"], passed=not reject_literary)
                writer = "write_verdict"
            elif claim.step_id == "extract_ledger":
                _, m = self.bench.input(run)
                obj = {str(c["chapter"]): ledger(c["chapter"], c["title"]) for c in m["chapters"] if not c["provided_ledger"]}
                writer = "write_ledger"
            elif claim.step_id == "ledger_audit":
                bundle = decode((self.bench.work(run) / "ledgers.json").read_bytes())
                obj, writer = verdict(bundle["review_key"]), "write_verdict"
            else:
                raise AssertionError("unexpected model step: " + claim.step_id)
            if writer == "write_verdict":
                self.attest(run, claim, obj)
            saved = self.sf.execute_tool(writer, {"content": encode(obj).decode()}, **common)
            assert "error" not in saved, saved
            self.sf.confirm_step(token, StepResult(outputs={"written": saved}))
        raise AssertionError("test engine did not settle: " + str(self.sf.get_run(run)))


def test_graph_provided_ledger_and_manual_gate(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    assert session.agent_calls == ["literary_review", "ledger_audit"]
    assert not session.backup_calls
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base
    for _ in range(3):
        session.sf.advance_run(run)
    assert session.sf.get_run(run)["status"] == "paused"
    # This tests the actual trace-based host approval reader, not a seed flag.
    with pytest.raises(BenchError, match="manual checkpoint"):
        adapter.Host().approval(run)
    session.sf.approve_checkpoint(run)
    assert session.drive(run) == "completed"
    final = decode((bench.work(run) / "completed.json").read_bytes())
    assert git(bench.policy.repo, "rev-parse", "HEAD") == final["accepted_commit"]
    assert len(session.backup_calls) == 1


def test_graph_without_ledger_extractor_and_reject(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench, provided=False))
    assert session.drive(run) == "paused"
    assert session.agent_calls == ["literary_review", "extract_ledger", "ledger_audit"]
    session.sf.reject_checkpoint(run, "stage", "需要调整人物选择", redirect_to="rejected")
    assert session.drive(run) == "completed"
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base
    assert not (bench.work(run) / "accepted.json").exists()
    assert not session.backup_calls


def test_literary_refusal_is_not_acceptance(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    assert session.drive(run, reject_literary=True) == "completed"
    assert session.agent_calls == ["literary_review"]
    assert not (bench.work(run) / "accepted.json").exists()


def test_rules_change_after_manual_preview_refuses_acceptance(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    session.rules["entries"].append({"address": "note://fiction/new", "body": "新的硬裁定"})
    session.sf.approve_checkpoint(run)
    with pytest.raises(BenchError, match="rulings changed"):
        session.drive(run)
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base


def test_graph_backup_only_does_not_repeat_reviews_or_acceptance(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    session.sf.approve_checkpoint(run)
    session.fail_backup = True
    with pytest.raises(BenchError, match="network"):
        session.drive(run)
    original = decode((bench.work(run) / "accepted.json").read_bytes())
    accepted = git(bench.policy.repo, "rev-parse", "HEAD")
    assert original["accepted_commit"] == accepted
    # Stop the original executor before starting a recovery, just as the operator must.
    session.sf.stop_run(run, "switch to explicit backup-only recovery")
    session.fail_backup = False
    count = len(session.agent_calls)
    recovery = session.start({"version": 2, "operation": "backup_only", "project_id": "fiction",
                              "source_run_id": run, "expected_commit": accepted}, project="recovery")
    assert session.drive(recovery) == "completed"
    assert len(session.agent_calls) == count
    assert git(bench.policy.repo, "rev-parse", "HEAD") == accepted
    assert decode((bench.work(run) / "accepted.json").read_bytes()) == original


def test_generic_auto_driver_respects_manual_only_contract(tmp_path, monkeypatch):
    from core import run_driver, config_registry
    hints = {adapter.CONFIG: {"manual_checkpoints_only": True}}
    monkeypatch.setattr(config_registry, "_read_host_hints", lambda: hints)
    observed = []

    class FakeSF:
        def get_run(self, run_id):
            return {"graph_name": adapter.CONFIG, "status": "paused"}

    async def watch(sf, run_id, auto, limit):
        observed.append(auto)
        return "paused"

    monkeypatch.setattr(run_driver, "_watch", watch)
    assert asyncio.run(run_driver.drive_run(FakeSF(), None, None, "r", scheduler_owned=True, auto_approve=True)) == "paused"
    assert observed == [False]


def test_effective_role_change_after_preview_refuses_acceptance(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    role = yaml.safe_load((ROOT / "agent_configs/novel_writing_bench_v2.yaml").read_text())["bench_literary_v2"]
    role["temperature"] = 0.9
    session.sf.register_agent_config_from_dict("bench_literary_v2", role)
    session.sf.approve_checkpoint(run)
    with pytest.raises(BenchError, match="review role"):
        session.drive(run)
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base


def test_backup_recovery_refuses_running_original(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    session.sf.approve_checkpoint(run)
    session.fail_backup = True
    with pytest.raises(BenchError):
        session.drive(run)
    assert session.sf.get_run(run)["status"] == "running"
    accepted = git(bench.policy.repo, "rev-parse", "HEAD")
    recovery = session.start({"version": 2, "operation": "backup_only", "project_id": "fiction",
                              "source_run_id": run, "expected_commit": accepted}, project="recovery")
    with pytest.raises(BenchError, match="settle or stop"):
        session.drive(recovery)
    assert len(session.backup_calls) == 1


@pytest.mark.parametrize("metadata", [None, {}, [], {"graph_name": ""}, {"graph_name": None}])
def test_auto_driver_missing_identity_fails_closed(monkeypatch, metadata):
    from core import run_driver

    class MissingIdentity:
        def get_run(self, run_id):
            return metadata

    async def forbidden(*args):
        raise AssertionError("unknown workflow reached auto-checkpoint logic")

    monkeypatch.setattr(run_driver, "_watch", forbidden)
    monkeypatch.setattr(run_driver, "_step", forbidden)
    with pytest.raises(ValueError, match="graph identity"):
        asyncio.run(run_driver.drive_run(MissingIdentity(), None, None, "r",
                                        scheduler_owned=True, auto_approve=True))
