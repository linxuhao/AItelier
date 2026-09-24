"""The three mutants that survived the previous round's suite, one test each.

M3: certificate validation ignores a coverage gap.
M9: a missing certificate skips validation instead of refusing.
M11: the literary-reuse path skips the evidence checks.

Each test below is written against the *shipped* guard, so planting the real
mutation in the real file turns exactly this test red. Logs of those two-pole
runs live in `logs/` beside the delivery notes in `final/`.
"""
from __future__ import annotations

import copy

import pytest

from aitelier.writing_bench import adapter, bench as bench_module
from aitelier.writing_bench.bench import Bench, Policy, validate_ledger
from aitelier.writing_bench.reading import (
    Coverage, Material, load_certificate, material_identity, validate_certificate,
)
from aitelier.writing_bench.storage import BenchError, decode, encode, git, sha
from test_bench import (  # noqa: F401 — fixtures re-exported
    CONTRACTS, RULES, bench, ledger, request, staged, verdict,
)
from aitelier.writing_bench.storage import BenchError, decode, encode, git, sha


KEY = "a" * 64
TARGETS = [{"chapter": 1, "title": "启程", "prose_sha256": "b" * 64}]
MATERIALS = [Material("current_prose.md", "step:prepare",
                      "# 本次待接受完整正文（只审以下章节）\n\n旅人推开门。\n留下钥匙。\n")]


def _certificate(spans):
    """A certificate whose coverage is exactly `spans`, everything else complete."""
    report = verdict(KEY, reviewed_chapters=copy.deepcopy(TARGETS))
    coverage = Coverage("literary", KEY, copy.deepcopy(TARGETS), MATERIALS)
    coverage.ranges[MATERIALS[0].path] = spans
    cert = {"identity": coverage.identity, "report_sha256": sha(encode(report)),
            "claim": {"run_id": "r", "step_id": "literary_review"},
            "coverage": {MATERIALS[0].path: spans}, "complete": True}
    return report, cert, coverage.identity


def test_m3_coverage_gap_must_be_refused_by_certificate_and_by_the_domain_gate():
    """M3: the `holes()` refusal is the whole guard for a partial read."""
    size = len(MATERIALS[0].text)
    for spans in ([], [[0, 5]], [[0, size - 3]]):
        report, cert, identity = _certificate(spans)
        with pytest.raises(BenchError, match="incomplete observed material"):
            validate_certificate(cert, identity, report)
    # Complete coverage is still accepted: the guard is a gap test, not a ban.
    report, cert, identity = _certificate([[0, size]])
    validate_certificate(cert, identity, report)


def test_m3_a_gap_in_the_domain_gate_never_stages(bench, monkeypatch):
    """The same refusal reaches `Bench.literary`: a holed certificate cannot carry."""
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    _, m = bench.input("run1")
    identity, materials = bench.review_materials("run1", "literary")
    coverage = Coverage("literary", identity["review_key"], identity["targets"], materials)
    coverage.observe([{"role": "user", "content": materials[0].text}])   # omit the rest
    report = verdict(identity["review_key"], reviewed_chapters=identity["targets"])
    with pytest.raises(BenchError, match="has not been presented"):
        coverage.certificate(report, {"run_id": "run1"})
    assert m["engine"] == bench_module.engine_identity()


def test_m9_missing_certificate_is_refused_at_the_real_tool_entry(bench, tmp_path, monkeypatch):
    """M9: the domain gate must refuse when the host certificate is absent.

    A complete, schema-valid, target-matching `passed=true` verdict is written
    through the REAL generated write tool with no reading session behind it. If
    a missing certificate skipped validation, this report would carry the run.
    """
    from test_workflow import Session

    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    session.sf.advance_run(run)
    claim = session.sf.claim_next_step(run)
    assert claim.step_id == "literary_review"
    identity, _ = bench.review_materials(run, "literary")
    value = verdict(identity["review_key"], reviewed_chapters=identity["targets"])
    saved = session.sf.execute_tool("write_verdict", {"content": encode(value).decode()},
                                    run_id=run, step_id=claim.step_id,
                                    step_instance_id=claim.token.step_instance_id,
                                    claim_epoch=claim.token.claim_epoch)
    assert "error" not in saved
    # No host session ever observed this report, so no certificate exists.
    with pytest.raises(BenchError):
        load_certificate(bench.work(run) / "reading", "literary")
    with pytest.raises(BenchError):
        adapter.Host().observed_review(bench, run, "literary", value)
    # And the same report cannot become evidence by going around the gate. This
    # block holds exactly the one call under test: an earlier version repeated
    # `bench.literary` and then appended `Host().observed_review`, which raises on
    # its own and made the block pass even when the missing certificate was
    # silently accepted.
    with pytest.raises(BenchError):
        bench.literary(run, value)


def test_m11_reuse_never_accepts_a_legacy_self_reported_receipt(bench):
    """M11: reuse preserves the ORIGINAL receipt, so it must still prove it.

    A prior receipt whose `reading` is None is exactly the legacy shape the new
    contract may not bless. If the reuse branch skipped `_review_evidence`, this
    receipt would silently become the evidence for a new run.
    """
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    identity, materials = bench.review_materials("run1", "literary")
    m = bench.input("run1")[1]
    value = verdict(m["literary_key"], reviewed_chapters=identity["targets"])
    # A certificate IS present, and it is bound to a different report. If the
    # reuse branch skipped `_review_evidence`, the original bytes would be
    # blessed as this run's evidence without ever being checked.
    stale = {"identity": {"protocol": 1, "phase": "literary",
                          "review_key": identity["review_key"],
                          "targets": identity["targets"], "materials": []},
             "report_sha256": "f" * 64, "claim": {"run_id": "run1"},
             "coverage": {}, "complete": True}
    receipt = {"report": value, "review_key": m["literary_key"],
               "reused_from": None, "reading": stale}
    (bench.work("run1") / "literary.json").write_bytes(encode(receipt))

    reuse = request(bench, sid="ledgerfix", value=ledger(location="院门"))
    reuse["reuse_literary_from"] = "run1"
    bench.freeze(reuse, "run2", RULES, CONTRACTS)
    with pytest.raises(BenchError):
        bench.literary("run2")


def test_m11_reuse_still_passes_when_the_original_certificate_is_valid(bench):
    """The positive pole: an honest reuse keeps working through the same checks."""
    req = request(bench)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    _, m = bench.input("run1")
    identity, materials = bench.review_materials("run1", "literary")
    value = verdict(m["literary_key"], reviewed_chapters=identity["targets"])
    coverage = Coverage("literary", identity["review_key"], identity["targets"], materials)
    coverage.observe([{"role": "user", "content": x.text} for x in materials])
    cert = coverage.certificate(value, {"run_id": "run1", "step_id": "literary_review",
                                        "step_instance_id": 1})
    bench.literary("run1", value, proof=cert)

    reuse = request(bench, sid="ledgerfix", value=ledger(location="院门"))
    reuse["reuse_literary_from"] = "run1"
    bench.freeze(reuse, "run2", RULES, CONTRACTS)
    assert bench.literary("run2")["reused_from"] == "run1"
