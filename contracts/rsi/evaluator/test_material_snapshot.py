"""Ordinary package updates must not change an evaluation's verified inputs."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from contracts.rsi.evaluator.contract import EvaluationResult, digest
from contracts.rsi.evaluator.native_fixture import NativeFactory


@pytest.mark.parametrize("package,arm,points", [("private", "reference", 6),
                                               ("public", "candidate", 10)])
def test_verified_case_material_survives_package_update(tmp_path, package, arm, points):
    factory = NativeFactory(tmp_path)
    fixture = factory()
    try:
        request = fixture.prepare(arms=(arm,))
        host = fixture.evaluator.host
        original_public = json.loads(host.public_package.read_text())
        path = getattr(host, package + "_package")
        original = json.loads(path.read_text())
        case_id = request.cells[0].case_id

        def update(_record):
            later = json.loads(path.read_text())
            if package == "private":
                later[case_id]["material"]["expected_constant"] = 999999
            else:
                later[case_id]["prompt"] = "LATER_PUBLIC_PROMPT"
                later[case_id]["files"] = {"later.txt": "constant = 999999\n"}
            path.write_text(json.dumps(later, sort_keys=True))

        fixture.provider.hooks["package_update"] = update
        fixture.provider.fault = "package_update"
        result = fixture.evaluate(request)
        evidence_dir = os.getenv("EVALUATOR_EVIDENCE")
        if evidence_dir:
            out = Path(evidence_dir) / ("material-snapshot-" + package)
            out.mkdir()
            (out / "request.json").write_text(request.model_dump_json(indent=2))
            (out / "result.json").write_text(result.model_dump_json(indent=2))
            (out / "original-package.json").write_text(json.dumps(original, indent=2))
            (out / "later-package.json").write_bytes(path.read_bytes())
            (out / "observations.json").write_text(json.dumps({
                "original_package": digest(original),
                "later_package": digest(json.loads(path.read_text())),
                "provider_requests": len(fixture.provider.received()),
                "expected_frozen_points": points,
                "first_case_points": [r.checker.raw_points if r.checker else None
                                      for r in result.receipts if r.cell.case_id == case_id],
            }, indent=2))
        assert isinstance(result, EvaluationResult) and result.status == "complete"
        assert digest(original) != digest(json.loads(path.read_text()))
        first_case = [r for r in result.receipts if r.cell.case_id == case_id]
        assert len(first_case) == 3
        assert [r.checker.raw_points for r in first_case] == [points] * 3
        assert len(result.receipts) == len(fixture.provider.received()) == 36
        for index, receipt in enumerate(result.receipts):
            assert receipt.checker.checker_package == request.benchmark.checker_package
            assert receipt.checker.resolved_private_material == receipt.case.resolved_private_material
            public = original_public[receipt.cell.case_id]
            repo = host.public_fixture_root / request.evaluation_id / f"cell-{index:03d}"
            for name, text in public["files"].items():
                assert (repo / name).read_text() == text
            assert not (repo / "later.txt").exists()
        assert dict(result.optimizer_projection.train_prompts) == {
            c.case_id: original_public[c.case_id]["prompt"]
            for c in request.benchmark.cases if c.split == "train"}
        rows = fixture.world.store.list_receipts("evaluator-fixture", 1)
        assert len(rows) == 36 and all(r["validity"] == "VALID" for r in rows)
        first = result.receipts[0]
        snapshots = list((host.artifacts_root / "outputs").glob("*.md"))
        assert any(first.checker.output_snapshot == digest({
            "findings.md": "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()})
                   for f in snapshots)
    finally:
        factory.close()
