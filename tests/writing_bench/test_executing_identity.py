from pathlib import Path
import pytest
from dataclasses import replace
from aitelier.writing_bench import bench as implementation
from aitelier.writing_bench.storage import BenchError, decode
from test_bench import bench, request, RULES, CONTRACTS, observed_verdict

def reviewed(bench):
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    bench.ledgers("run1")
    bench.replay_candidate("run1")
    value, proof = observed_verdict(bench, "run1")
    bench.literary("run1", value, proof=proof)
    return observed_verdict(bench, "run1", "ledger")

def test_disk_only_change_preserves_reviewed_stage(bench):
    value, proof = reviewed(bench)
    target = Path(implementation.__file__)
    original = target.read_bytes()
    before = implementation.validate_ledger.__code__
    try:
        target.write_bytes(original + b"\n# disk-only deployment fixture\n")
        assert implementation.validate_ledger.__code__ is before
        stage = bench.stage("run1", value, proof=proof)
        assert stage["requires_manual_approval"] and not stage["accepted"]
    finally:
        target.write_bytes(original)

def test_actual_policy_change_refused(bench):
    value, proof = reviewed(bench)
    bench.policy = replace(bench.policy, max_context_bytes=180001)
    with pytest.raises(BenchError, match="policy or implementation changed"):
        bench.stage("run1", value, proof=proof)

def test_actual_loaded_module_change_refused(bench):
    import importlib
    from aitelier.writing_bench import context
    value, proof = reviewed(bench)
    target = Path(context.__file__)
    original = target.read_bytes()
    before = context.WINDOW
    try:
        target.write_bytes(original.replace(b"WINDOW = 5", b"WINDOW = 4"))
        Path(context.__cached__).unlink(missing_ok=True)
        importlib.invalidate_caches()
        importlib.reload(context)
        assert context.WINDOW == 4 and before == 5
        with pytest.raises(BenchError, match="policy or implementation changed"):
            bench.stage("run1", value, proof=proof)
    finally:
        target.write_bytes(original)
        Path(context.__cached__).unlink(missing_ok=True)
        importlib.invalidate_caches()
        importlib.reload(context)

def test_actual_native_import_boundary(bench):
    import sys
    names = ["aitelier.writing_bench." + part for part in
             ("adapter", "bench", "context", "reading", "storage")]
    names += ["aitelier.novel_state", "core.dpe_pipeline", "core.ai_router"]
    print("BEFORE", {name: name in sys.modules for name in names})
    value, proof = reviewed(bench)
    print("REVIEWED", {name: name in sys.modules for name in names})
    assert bench.stage("run1", value, proof=proof)["requires_manual_approval"]
    print("STAGED", {name: name in sys.modules for name in names})

def test_graph_disk_edit_after_reviews_reaches_manual_stage(tmp_path, monkeypatch, bench):
    from test_workflow import Session
    session = Session(tmp_path, monkeypatch, bench)
    target = Path(implementation.__file__)
    original = target.read_bytes()
    attest = session.attest

    def after_review(run, claim, value):
        attest(run, claim, value)
        if claim.step_id == "ledger_audit":
            target.write_bytes(original + b"\n# disk edit after graph reviews\n")

    session.attest = after_review
    try:
        run = session.start(request(bench))
        assert session.drive(run) == "paused"
        assert session.agent_calls == ["literary_review", "ledger_audit"]
        assert not session.backup_calls
    finally:
        target.write_bytes(original)


def test_identity_stable_across_processes_and_hash_seeds():
    import os
    import subprocess
    import sys
    command = [sys.executable, "-c",
               "from aitelier.writing_bench.bench import engine_identity; print(engine_identity())"]
    values = [subprocess.check_output(command, env={**os.environ, "PYTHONHASHSEED": seed},
                                     text=True).strip() for seed in ("1", "2")]
    assert values[0] == values[1] == implementation.engine_identity()

def test_preinstrumentation_disk_manifest_is_not_translated(bench, monkeypatch):
    from aitelier import novel_state
    from aitelier.writing_bench.storage import encode, sha
    root = Path(implementation.__file__).resolve().parents[2]
    files = [root / "aitelier/writing_bench" / name for name in
             ("__init__.py", "adapter.py", "bench.py", "context.py", "reading.py", "storage.py")]
    files += [Path(novel_state.__file__), root / "core/dpe_pipeline.py", root / "core/ai_router.py"]
    old = sha(encode({str(p.relative_to(root)): sha(p.read_bytes()) for p in files}))
    with monkeypatch.context() as old_process:
        old_process.setattr(implementation, "engine_identity", lambda: old)
        value, proof = reviewed(bench)
    with pytest.raises(BenchError, match="policy or implementation changed"):
        bench.stage("run1", value, proof=proof)


def test_missing_capture_fails_before_freeze(bench, monkeypatch):
    monkeypatch.delattr(implementation, "__bench_implementation_code__")
    with pytest.raises(RuntimeError, match="implementation capture missing"):
        bench.freeze(request(bench), "run1", RULES, CONTRACTS)
