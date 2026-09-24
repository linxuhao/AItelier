"""One named test per mutation an independent review measured surviving 31c5be92.

M9a  `_review_evidence` skips validation when the certificate is missing.
M9b  `validate_certificate` returns early when the certificate is missing.
N3   `observed_review` accepts a certificate from another run/step/attempt.
N4b  the report target comparison ignores the chapter title.
N4c  the report target comparison prefixes the chapter prose hash.
N5   `load_certificate` skips the stored certificate checksum.
N8   any tool output is credited as an observed read.
N9   certificate validation ignores the `complete` flag.

Each test is written against the SHIPPED guard, so planting the real mutation in
the real source file turns exactly this test red. Patches live in
`final/mutations/<id>.patch`; the two-pole logs are beside the delivery notes.
Every `pytest.raises` block below holds exactly the one call under test.
"""
from __future__ import annotations

import copy
import json

import pytest

from aitelier.writing_bench import adapter
from aitelier.writing_bench.reading import (
    Coverage, Material, ReviewSession, load_certificate, material_identity,
    validate_certificate, validate_targets,
)
from aitelier.writing_bench.storage import BenchError, decode, encode, sha
from test_bench import CONTRACTS, RULES, bench, ledger, request, verdict  # noqa: F401
from test_workflow import Session


KEY = "a" * 64
TARGETS = [{"chapter": 1, "title": "启程", "prose_sha256": "b" * 64}]
TEXT = "# 本次待接受完整正文（只审以下章节）\n\n旅人推开门。\n留下钥匙。\n"
MATERIALS = [Material("current_prose.md", "step:prepare", TEXT)]


def native_page(material, start=0, end=None, raw=False):
    lines = material.text.splitlines(keepends=True)
    end = len(lines) if end is None else end
    return {"source": material.source, "path": material.path, "start_line": start,
            "returned_lines": end - start, "total_lines": len(lines),
            "truncated": end < len(lines),
            "content": ("".join(lines[start:end]) if raw else
                        "\n".join(f"{i + 1}\t{lines[i].rstrip(chr(10))}"
                                  for i in range(start, end)))}


def tool_message(value, name="read", call="call-1"):
    return [{"role": "assistant", "content": None,
             "tool_calls": [{"id": call, "type": "function",
                             "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call,
             "content": json.dumps(value, ensure_ascii=False)}]


def fully_read(targets, materials):
    coverage = Coverage("literary", KEY, copy.deepcopy(targets), materials)
    coverage.observe([{"role": "user", "content": m.text} for m in materials])
    return coverage


def test_m9a_a_domain_verdict_without_any_host_certificate_is_refused(bench):
    """M9a: no certificate must not be a shortcut through `_review_evidence`."""
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    identity = bench.review_materials("run1", "literary")[0]
    value = verdict(identity["review_key"], reviewed_chapters=copy.deepcopy(identity["targets"]))
    with pytest.raises(BenchError):
        bench.literary("run1", value)


def test_m9b_validate_certificate_refuses_a_missing_certificate():
    """M9b: `validate_certificate(None, ...)` must refuse at its own entry."""
    report = verdict(KEY, reviewed_chapters=copy.deepcopy(TARGETS))
    identity = material_identity("literary", KEY, copy.deepcopy(TARGETS), list(MATERIALS))
    with pytest.raises(BenchError):
        validate_certificate(None, identity, report)


def test_m9_a_legacy_receipt_without_a_certificate_cannot_be_reused(bench):
    """M9 (reuse shape): an old self-reported receipt with `reading: null` is not
    evidence for a new run, so the reuse path must refuse it."""
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    _, m = bench.input("run1")
    identity = bench.review_materials("run1", "literary")[0]
    value = verdict(m["literary_key"], reviewed_chapters=copy.deepcopy(identity["targets"]))
    legacy = {"report": value, "review_key": m["literary_key"],
              "reused_from": None, "reading": None}
    (bench.work("run1") / "literary.json").write_bytes(encode(legacy))
    reuse = request(bench, sid="ledgerfix", value=ledger(location="院门"))
    reuse["reuse_literary_from"] = "run1"
    bench.freeze(reuse, "run2", RULES, CONTRACTS)
    with pytest.raises(BenchError):
        bench.literary("run2")


def test_n3_a_certificate_from_another_run_is_refused(tmp_path, monkeypatch, bench):
    """N3: the certificate's claim must be this run and this reviewer attempt."""
    from skillflow import StepResult

    session = Session(tmp_path, monkeypatch, bench)
    run = session.start(request(bench))
    session.sf.advance_run(run)
    claim = session.sf.claim_next_step(run)
    assert claim.step_id == "literary_review"
    identity, materials = bench.review_materials(run, "literary")
    value = verdict(identity["review_key"], reviewed_chapters=identity["targets"])
    # Correct step instance and epoch, but a DIFFERENT run id: only the run/step
    # binding distinguishes this from an honest certificate.
    forged = ReviewSession(Coverage("literary", identity["review_key"], identity["targets"], materials),
                           bench.work(run) / "reading",
                           {"run_id": "some-other-run", "step_id": "literary_review",
                            "step_instance_id": claim.token.step_instance_id,
                            "claim_epoch": claim.token.claim_epoch})
    forged.observe([{"role": "user", "content": m.text} for m in materials])
    assert forged.guard("write_verdict", value) is None
    saved = session.sf.execute_tool("write_verdict", {"content": encode(value).decode()},
                                    run_id=run, step_id=claim.step_id,
                                    step_instance_id=claim.token.step_instance_id,
                                    claim_epoch=claim.token.claim_epoch)
    assert "error" not in saved, saved
    session.sf.confirm_step(claim.token, StepResult(outputs={"written": saved}))
    with pytest.raises(BenchError, match="another reviewer attempt"):
        adapter.Host().observed_review(bench, run, "literary", value)


def test_n4b_the_chapter_title_is_part_of_the_review_target():
    """N4b: a report naming a different title for this chapter is not this chapter."""
    wrong = copy.deepcopy(TARGETS)
    wrong[0]["title"] = "另一章"
    report = verdict(KEY, reviewed_chapters=wrong)
    with pytest.raises(BenchError, match="target mismatch"):
        validate_targets(report, KEY, copy.deepcopy(TARGETS))


def test_n4c_the_chapter_prose_hash_must_match_in_full():
    """N4c: a prefix of the frozen prose hash must not alias the whole hash."""
    truncated = copy.deepcopy(TARGETS)
    truncated[0]["prose_sha256"] = "b" * 8
    report = verdict(KEY, reviewed_chapters=truncated)
    with pytest.raises(BenchError, match="target mismatch"):
        validate_targets(report, KEY, copy.deepcopy(TARGETS))


def test_n5_the_published_certificate_checksum_is_verified(tmp_path):
    """N5: a certificate changed after publication must not load."""
    report = verdict(KEY, reviewed_chapters=copy.deepcopy(TARGETS))
    session = ReviewSession(fully_read(TARGETS, list(MATERIALS)), tmp_path,
                            {"run_id": "r", "step_id": "literary_review", "step_instance_id": 1})
    assert session.guard("write_verdict", report) is None
    pointer = decode((tmp_path / "literary-session.json").read_bytes())
    filename = pointer["certificate"]
    tampered = decode((tmp_path / filename).read_bytes())
    tampered["report_sha256"] = "f" * 64
    (tmp_path / filename).write_bytes(encode(tampered))
    with pytest.raises(BenchError, match="changed"):
        load_certificate(tmp_path, "literary")


def test_n8_only_the_allowlisted_read_tools_grant_coverage():
    """N8: a tool result from an unrelated tool is not observed reading."""
    coverage = Coverage("literary", KEY, copy.deepcopy(TARGETS), list(MATERIALS))
    material = MATERIALS[0]
    coverage.observe(tool_message(native_page(material), name="web_search", call="other-tool"))
    assert coverage.ranges[material.path] == []
    assert coverage.missing()


def test_n9_the_certificate_complete_flag_is_required():
    """N9: coverage alone is not a complete reading certificate."""
    report = verdict(KEY, reviewed_chapters=copy.deepcopy(TARGETS))
    coverage = fully_read(TARGETS, list(MATERIALS))
    cert = {"identity": coverage.identity, "report_sha256": sha(encode(report)),
            "claim": {"run_id": "r"}, "coverage": coverage.ranges, "complete": False}
    with pytest.raises(BenchError, match="incomplete"):
        validate_certificate(cert, coverage.identity, report)
