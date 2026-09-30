"""Guard -> real schema writer -> persisted report -> real adapter consumer."""
import copy
from types import SimpleNamespace

import pytest

from aitelier.writing_bench import adapter
from aitelier.writing_bench.reading import load_certificate
from aitelier.writing_bench.storage import BenchError, decode, encode
from test_bench import bench, request
from test_workflow import Session


@pytest.mark.parametrize("writer", ["create_verdict", "write_verdict"])
@pytest.mark.parametrize("shape", ["fields", "content", "initialContent", "mixed", "empty_content"])
def test_certificate_matches_real_writer_and_consumer(tmp_path, monkeypatch, bench, writer, shape):
    workflow = Session(tmp_path, monkeypatch, bench)
    run = workflow.start(request(bench))
    phases = []
    reports = {}

    def attest(run, claim, value):
        phase = "literary" if claim.step_id == "literary_review" else "ledger"
        identity, _ = bench.review_materials(run, phase)
        value["reviewed_chapters"] = identity["targets"]
        observed = adapter.begin_observed_review(SimpleNamespace(
            _config_name=adapter.CONFIG, _current_step=claim.step_id, _run_id=run,
            _step_instance_id=claim.token.step_instance_id, _claim_epoch=claim.token.claim_epoch))
        _, materials = bench.review_materials(run, phase)
        # Model assertions cannot substitute for host-observed presentation.
        forged = observed.guard(writer, value)
        assert forged and forged["missing_review_material"]
        observed.observe([{"role": "user", "content": m.text} for m in materials])
        extra = copy.deepcopy(value)
        extra["model_comment"] = "synthetic discarded top-level argument"
        extra["findings"] = [{"severity": "advisory", "location": "fixture",
                              "reason": "synthetic advice", "extra_nested": "preserved"}]
        params = extra if shape == "fields" else {shape: encode(extra).decode()}
        if shape == "mixed":
            params = {"content": encode(extra).decode(), "review_key": "0" * 64}
        elif shape == "empty_content":
            params = {"content": "", "initialContent": encode(extra).decode()}
        if shape == "fields":
            refusal = observed.guard(writer, params)
            assert refusal and "unexpected review field" in refusal["error"]
            with pytest.raises((BenchError, OSError)):
                load_certificate(bench.work(run) / "reading", phase)
            # The reviewer corrects only the discarded top-level field.
            del params["model_comment"]
        assert observed.guard(writer, params) is None
        token = claim.token
        saved = workflow.sf.execute_tool(writer, params, run_id=run, step_id=claim.step_id,
            step_instance_id=token.step_instance_id, claim_epoch=token.claim_epoch)
        assert "error" not in saved, saved
        files = list(tmp_path.rglob(claim.step_id + ".tmp/review.json"))
        assert len(files) == 1
        reports[phase] = files[0]
        persisted = decode(files[0].read_bytes())
        assert persisted["findings"][0]["extra_nested"] == "preserved"
        assert ("model_comment" in persisted) == (shape != "fields")
        phases.append(phase)
        # drive writes once more through the same schema after this callback.
        value.clear()
        value.update(persisted)

    workflow.attest = attest
    assert workflow.drive(run) == "paused"
    assert phases == ["literary", "ledger"]
    assert (bench.work(run) / "stage.json").exists()
    for phase, step in [("literary", "literary_review"), ("ledger", "ledger_audit")]:
        persisted = decode(reports[phase].parent.with_suffix("").joinpath("review.json").read_bytes())
        assert adapter.Host().observed_review(bench, run, phase, persisted)["complete"]
        for field, replacement in [("review_key", "0" * 64), ("feedback", "changed"),
                                   ("passed", False), ("findings", [])]:
            altered = copy.deepcopy(persisted)
            altered[field] = replacement
            with pytest.raises(BenchError):
                adapter.Host().observed_review(bench, run, phase, altered)
