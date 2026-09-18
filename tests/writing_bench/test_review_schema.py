"""Live-smoke regression: model advice is validated before leaving its step.

The report shape reproduces c3749e69's two findings with no severity keys.
No production report is edited and no test changes a manual decision.
"""
from __future__ import annotations

import copy
from pathlib import Path

import jsonschema
import pytest
import yaml
from skillflow import StepResult

from aitelier.writing_bench.storage import encode, git
from test_bench import bench, request, verdict
from test_workflow import Session

ROOT = Path(__file__).resolve().parents[2]
STEPS = ("literary_review", "ledger_audit")


def node(step):
    graph = yaml.safe_load((ROOT / "configs/novel_writing_bench_v2.yaml").read_text())
    return next(n for n in graph["steps"] if n["id"] == step)


def report():
    return verdict("a" * 64, findings=[{
        "severity": "advisory", "location": "结尾", "reason": "可选的措辞建议。"}])


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("fault", ["missing", "unknown", "string_findings", "passed_with_blocker", "incomplete_pass"])
def test_review_schema_rejects_invalid_model_reports(step, fault):
    value = report()
    if fault == "missing":
        del value["findings"][0]["severity"]
        value["findings"][0]["reason"] = "advisory：只在文字里写严重性不算结构化字段。"
    elif fault == "unknown":
        value["findings"][0]["severity"] = "minor"
    elif fault == "string_findings":
        value["findings"] = "没有问题"
    elif fault == "passed_with_blocker":
        value["findings"][0]["severity"] = "blocker"
    else:
        value["read_complete"] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, node(step)["validation"][0]["inline_schema"])


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("outcome", ["advisory", "empty", "rejected", "unread"])
def test_review_schema_preserves_valid_positive_and_negative_outcomes(step, outcome):
    value = report()
    if outcome == "empty":
        value["findings"] = []
    if outcome in ("rejected", "unread"):
        value["passed"] = False
        value["findings"][0]["severity"] = "blocker"
    if outcome == "unread":
        value["read_complete"] = False
    jsonschema.validate(value, node(step)["validation"][0]["inline_schema"])
    assert node(step)["validation_on_exhaustion"] == "fail"


def claim_literary(session, run):
    for _ in range(10):
        session.sf.advance_run(run)
        claim = session.sf.claim_next_step(run)
        if claim:
            assert claim.step_id == "literary_review"
            return claim
    raise AssertionError("No literature claim")


def write_and_confirm(session, run, claim, value):
    token = claim.token
    result = session.sf.execute_tool(
        "write_verdict", {"content": encode(value).decode()},
        run_id=run, step_id=claim.step_id,
        step_instance_id=token.step_instance_id, claim_epoch=token.claim_epoch)
    assert "error" not in result, result
    session.sf.confirm_step(token, StepResult(outputs={"written": result}))


def test_invalid_advice_returns_to_model_then_manual_rejection_is_safe(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    claim = claim_literary(session, run)
    _, frozen = bench.input(run)
    bad = report()
    bad["review_key"] = frozen["literary_key"]
    del bad["findings"][0]["severity"]
    before = copy.deepcopy(bad)
    write_and_confirm(session, run, claim, bad)
    assert bad == before, "Invalid reports must not be silently repaired"
    assert session.sf.get_run(run)["current_node"] == "literary_review"
    assert not (bench.work(run) / "literary.json").exists()
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base
    # Simulate the SAME independent reviewer correcting only its own schema.
    retry = claim_literary(session, run)
    good = copy.deepcopy(bad)
    good["findings"][0]["severity"] = "advisory"
    write_and_confirm(session, run, retry, good)
    assert session.drive(run) == "paused"
    assert session.agent_calls == ["ledger_audit"]  # author ledger bypassed extractor
    assert not session.backup_calls
    session.sf.reject_checkpoint(run, "stage", "合成材料人工拒绝测试", redirect_to="rejected")
    assert session.drive(run) == "completed"
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base
    assert not (bench.work(run) / "accepted.json").exists()
    assert not session.backup_calls


def test_exhausted_invalid_reports_never_reach_acceptance(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    for _ in range(12):
        if session.sf.get_run(run)["status"] != "running":
            break
        claim = claim_literary(session, run)
        _, frozen = bench.input(run)
        bad = report()
        bad["review_key"] = frozen["literary_key"]
        del bad["findings"][0]["severity"]
        write_and_confirm(session, run, claim, bad)
    assert session.sf.get_run(run)["status"] == "failed"
    assert not (bench.work(run) / "literary.json").exists()
    assert not (bench.work(run) / "stage.json").exists()
    assert not (bench.work(run) / "accepted.json").exists()
    assert not session.backup_calls
    assert git(bench.policy.repo, "rev-parse", "HEAD") == base
