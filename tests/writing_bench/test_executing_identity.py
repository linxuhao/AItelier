from pathlib import Path
import importlib
import importlib.util
import sys
import pytest
from dataclasses import replace
from aitelier.writing_bench import bench as implementation
from aitelier.writing_bench.storage import BenchError, decode
from test_bench import bench, request, RULES, CONTRACTS, observed_verdict

def loaded_copy(monkeypatch, tmp_path, module):
    """Load a writable copy of ``module`` as the code this process runs.

    The suite runs from a read-only /app mount, so a disk-edit test cannot write
    the file its loader came from. Here the copy is executed under the module's
    own name and installed in sys.modules, which is the object ``executing_identity``
    resolves, so the identity check sees whatever these tests load and a disk edit
    made to the copy is an edit to a real file. Bound names stay bound: a module
    that did ``from .context import assemble`` holds the function object it
    captured at import, so a swapped module is visible to identity and not to an
    already-imported caller. These tests therefore measure identity's own
    behaviour (a disk-only edit does not change it, a loaded-code edit does);
    neither claims that the swapped code executes. monkeypatch restores the
    original module object at teardown. Returns (loaded module, copy path).
    """
    directory = tmp_path / "loaded"
    directory.mkdir(exist_ok=True)
    target = directory / Path(module.__file__).name
    target.write_bytes(Path(module.__file__).read_bytes())
    loaded = _execute_copy(module.__name__, target)
    monkeypatch.setitem(sys.modules, module.__name__, loaded)
    return loaded, target

def _execute_copy(name, target):
    # A rewritten copy keeps its byte size (WINDOW = 5 -> WINDOW = 4), and CPython
    # validates a cached .pyc by integer-second mtime plus size, so a re-exec in
    # the same second can silently run the old bytecode. Measured on the host
    # interpreter: without this unlink the second load still reports WINDOW == 5.
    Path(importlib.util.cache_from_source(str(target))).unlink(missing_ok=True)
    importlib.invalidate_caches()
    spec = importlib.util.spec_from_file_location(name, target)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded

def reviewed(bench):
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    bench.ledgers("run1")
    bench.replay_candidate("run1")
    value, proof = observed_verdict(bench, "run1")
    bench.literary("run1", value, proof=proof)
    return observed_verdict(bench, "run1", "ledger")

def test_disk_only_change_preserves_reviewed_stage(tmp_path, monkeypatch, bench):
    loaded, target = loaded_copy(monkeypatch, tmp_path, implementation)
    value, proof = reviewed(bench)
    original = target.read_bytes()
    before = loaded.validate_ledger.__code__
    target.write_bytes(original + b"\n# disk-only deployment fixture\n")
    assert loaded.validate_ledger.__code__ is before
    stage = bench.stage("run1", value, proof=proof)
    assert stage["requires_manual_approval"] and not stage["accepted"]

def test_actual_policy_change_refused(bench):
    value, proof = reviewed(bench)
    bench.policy = replace(bench.policy, max_context_bytes=180001)
    with pytest.raises(BenchError, match="policy or implementation changed"):
        bench.stage("run1", value, proof=proof)

def test_actual_loaded_module_change_refused(tmp_path, monkeypatch, bench):
    from aitelier.writing_bench import context
    loaded, target = loaded_copy(monkeypatch, tmp_path, context)
    value, proof = reviewed(bench)
    before = loaded.WINDOW
    frozen = implementation.engine_identity()
    target.write_bytes(target.read_bytes().replace(b"WINDOW = 5", b"WINDOW = 4"))
    reloaded = _execute_copy(context.__name__, target)
    monkeypatch.setitem(sys.modules, context.__name__, reloaded)
    assert reloaded.WINDOW == 4 and before == 5
    # Both modules execute from the same copy path, so the only difference
    # between these identities is the edited constant: the refusal below is
    # caused by the loaded code changing, not by a filename or a disk byte.
    assert implementation.engine_identity() != frozen
    with pytest.raises(BenchError, match="policy or implementation changed"):
        bench.stage("run1", value, proof=proof)

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
    loaded, target = loaded_copy(monkeypatch, tmp_path, implementation)
    session = Session(tmp_path, monkeypatch, bench)
    original = target.read_bytes()
    attest = session.attest

    def after_review(run, claim, value):
        attest(run, claim, value)
        if claim.step_id == "ledger_audit":
            target.write_bytes(original + b"\n# disk edit after graph reviews\n")

    session.attest = after_review
    run = session.start(request(bench))
    assert session.drive(run) == "paused"
    assert session.agent_calls == ["literary_review", "ledger_audit"]
    assert not session.backup_calls


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
