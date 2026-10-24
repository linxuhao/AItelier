"""One named test per reading guard an independent review found unwatched.

X1  `Bench.stage` trusts the upstream literary receipt instead of re-verifying it.
X3  `observed_review` ignores the certificate's `step_instance_id`.
X4  `observed_review` ignores the certificate's `step_id`.
X5  `load_certificate` ignores that the certificate claim differs from the pointer claim.
X8  `Coverage._page` credits a bounded page whose text is not the material slice.

All five guards were correct, yet planting each mutant changed nothing in
tests/writing_bench: the suite (8 stale tests already fail on the unmutated base,
144 pass) gave the same 8 failures and 144 passes for every mutant. Each test
below is written against the SHIPPED guard, so planting the real
mutation in the real source file turns exactly this test red. The positive pole
(the honest input is still accepted) sits beside every refusal, so a test cannot
pass by refusing everything. Every `pytest.raises` block holds exactly the one
call under test.
"""
from __future__ import annotations

import copy
import json

import pytest
from skillflow import StepResult

from aitelier.writing_bench import adapter
from aitelier.writing_bench.reading import (
    Coverage, Material, ReviewSession, bounded_page, load_certificate,
)
from aitelier.writing_bench.storage import BenchError, decode, encode
from test_bench import CONTRACTS, RULES, bench, ledger, observed_verdict, request, verdict  # noqa: F401
from test_workflow import Session


KEY = "a" * 64
TARGETS = [{"chapter": 1, "title": "启程", "prose_sha256": "b" * 64}]
TEXT = "# 本次待接受完整正文（只审以下章节）\n\n旅人推开门。\n留下钥匙。\n"
MATERIAL = Material("current_prose.md", "step:prepare", TEXT)


def tool_message(value, name="novel_bench_read", call="call-1"):
    return [{"role": "assistant", "content": None,
             "tool_calls": [{"id": call, "type": "function",
                             "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call,
             "content": json.dumps(value, ensure_ascii=False)}]


def test_x8_a_bounded_page_earns_coverage_only_for_text_equal_to_the_material_slice():
    """X8: coverage is what was actually presented, not the range that was requested.

    Every page below carries the right path, source, material digest and an
    in-range [start, end), exactly the metadata a bounded read of the whole
    material reports. Only the returned `text` differs from the material slice.
    """
    size = len(TEXT)
    honest = bounded_page(MATERIAL, 0, size)
    assert honest["text"] == TEXT and (honest["start"], honest["end"]) == (0, size)

    coverage = Coverage("literary", KEY, copy.deepcopy(TARGETS), [MATERIAL])
    coverage.observe(tool_message(honest))
    assert coverage.ranges[MATERIAL.path] == [[0, size]] and not coverage.missing()

    forged = {
        "a summary of the right length": "摘要" * (size // 2),
        "the tail cut off": honest["text"][:-4],
        "another slice of the same material": TEXT[4:] + TEXT[:4],
        "no text at all": None,
    }
    for label, text in forged.items():
        coverage = Coverage("literary", KEY, copy.deepcopy(TARGETS), [MATERIAL])
        coverage.observe(tool_message(dict(honest, text=text)))
        assert coverage.ranges[MATERIAL.path] == [], label
        assert coverage.missing(), label


def _reviewer_attempt(tmp_path, monkeypatch, bench):
    """A real run whose independent literary reviewer attempt is completed.

    Returns the pieces needed to publish a host reading certificate with any
    claim, and to ask the real `observed_review` whether it belongs to that
    completed attempt.
    """
    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    session.sf.advance_run(run)  # prepare
    session.sf.advance_run(run)  # ledger_ready: replay, then release literary review
    claim = session.sf.claim_next_step(run)
    assert claim.step_id == "literary_review"
    identity, materials = bench.review_materials(run, "literary")
    value = verdict(identity["review_key"], reviewed_chapters=identity["targets"])
    honest = {"run_id": run, "step_id": "literary_review",
              "step_instance_id": claim.token.step_instance_id,
              "claim_epoch": claim.token.claim_epoch}

    def publish(claimed):
        reading = ReviewSession(Coverage("literary", identity["review_key"], identity["targets"], materials),
                                bench.work(run) / "reading", claimed)
        reading.observe([{"role": "user", "content": m.text} for m in materials])
        assert reading.guard("write_verdict", value) is None

    publish(honest)
    saved = session.sf.execute_tool("write_verdict", {"content": encode(value).decode()},
                                    run_id=run, step_id=claim.step_id,
                                    step_instance_id=claim.token.step_instance_id,
                                    claim_epoch=claim.token.claim_epoch)
    assert "error" not in saved, saved
    session.sf.confirm_step(claim.token, StepResult(outputs={"written": saved}))
    # Positive pole: the honest certificate IS this completed attempt's.
    assert adapter.Host().observed_review(bench, run, "literary", value)["claim"] == honest
    return run, value, honest, publish


def test_x3_a_certificate_from_another_step_instance_is_refused(tmp_path, monkeypatch, bench):
    """X3: right run, right step, right epoch, but a different step instance."""
    run, value, honest, publish = _reviewer_attempt(tmp_path, monkeypatch, bench)
    publish(dict(honest, step_instance_id=honest["step_instance_id"] + 1000))
    with pytest.raises(BenchError, match="another reviewer attempt"):
        adapter.Host().observed_review(bench, run, "literary", value)


def test_x4_a_certificate_for_another_step_is_refused(tmp_path, monkeypatch, bench):
    """X4: right run, right step instance, right epoch, but the other reviewer step."""
    run, value, honest, publish = _reviewer_attempt(tmp_path, monkeypatch, bench)
    publish(dict(honest, step_id="ledger_audit"))
    with pytest.raises(BenchError, match="another reviewer attempt"):
        adapter.Host().observed_review(bench, run, "literary", value)


def test_x5_the_certificate_claim_must_equal_the_session_pointer_claim(tmp_path):
    """X5: a certificate whose claim differs from the host pointer's is not loaded.

    The certificate bytes and their checksum are untouched, so only the claim
    comparison can notice that the pointer now names another executor.
    """
    claim = {"run_id": "r", "step_id": "literary_review", "step_instance_id": 1, "claim_epoch": 1}
    report = verdict(KEY, reviewed_chapters=copy.deepcopy(TARGETS))
    coverage = Coverage("literary", KEY, copy.deepcopy(TARGETS), [MATERIAL])
    coverage.observe([{"role": "user", "content": MATERIAL.text}])
    session = ReviewSession(coverage, tmp_path, claim)
    assert session.guard("write_verdict", report) is None
    assert load_certificate(tmp_path, "literary")["claim"] == claim

    pointer_path = tmp_path / "literary-session.json"
    pointer = decode(pointer_path.read_bytes())
    pointer["claim"] = dict(claim, step_instance_id=2)
    pointer_path.write_bytes(encode(pointer))
    with pytest.raises(BenchError, match="executor identity changed"):
        load_certificate(tmp_path, "literary")


def _through_literary(bench):
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    bench.ledgers("run1")
    bench.replay_candidate("run1")
    value, proof = observed_verdict(bench, "run1")
    bench.literary("run1", value, proof=proof)
    return observed_verdict(bench, "run1", "ledger")


def test_x1_stage_reverifies_the_literary_receipt_it_is_given(bench):
    """X1: `stage` must re-check the stored literary receipt, not trust `bench.literary`.

    The receipt is changed after `bench.literary` accepted it: the verdict text
    no longer matches the report the reading certificate was issued for. The
    ledger evidence handed to `stage` is honest and nothing else in `stage` reads
    that verdict text, so only its own literary re-verification can refuse.
    """
    audit, proof = _through_literary(bench)
    receipt_path = bench.work("run1") / "literary.json"
    original = receipt_path.read_bytes()
    receipt = decode(original)
    receipt["report"]["feedback"] += " 改写于通过之后。"
    receipt_path.write_bytes(encode(receipt))
    with pytest.raises(BenchError, match="different verdict"):
        bench.stage("run1", audit, proof=proof)
    assert not (bench.work("run1") / "stage.json").exists()

    # Positive pole: with the receipt restored the very same call stages.
    receipt_path.write_bytes(original)
    assert bench.stage("run1", audit, proof=proof)["requires_manual_approval"]
