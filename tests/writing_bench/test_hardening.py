"""Small failure-path regressions; all files and Git repositories are synthetic."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from aitelier.writing_bench import adapter, bench as bench_module, storage
from aitelier.writing_bench.bench import Policy
from aitelier.writing_bench.storage import BenchError, encode, git, sha
from test_bench import bench, staged


@pytest.mark.parametrize("ancestor", ["submission_root", "store"])
def test_policy_refuses_canonical_nested_inside_another_root(bench, tmp_path, ancestor):
    values = dict(vars(bench.policy))
    container = tmp_path / "overlapping-root"
    values["repo"] = container / "novel"
    values[ancestor] = container
    with pytest.raises(BenchError, match="overlap"):
        Policy(**values)


def test_git_environment_cannot_redirect_the_canonical_repository(bench, tmp_path, monkeypatch):
    expected = git(bench.policy.repo, "rev-parse", "HEAD")
    other = tmp_path / "unrelated"
    other.mkdir()
    git(other, "init", "-b", "master")
    git(other, "-c", "user.name=Other", "-c", "user.email=other@test.invalid",
        "commit", "--allow-empty", "-m", "unrelated repository")
    unrelated = git(other, "rev-parse", "HEAD")
    assert unrelated != expected
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    assert git(bench.policy.repo, "rev-parse", "HEAD") == expected


def test_git_file_count_is_bounded_before_blob_processes(bench, monkeypatch):
    revision = git(bench.policy.repo, "rev-parse", "HEAD")
    original = storage.git
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "git", counted)
    monkeypatch.setattr(storage, "FILE_COUNT_LIMIT", 1, raising=False)
    with pytest.raises(BenchError, match="file count"):
        storage.git_files(bench.policy.repo, revision)
    assert calls == ["ls-tree"]


@pytest.mark.parametrize("decision", ["checkpoint_approved", "checkpoint_rejected"])
def test_approval_finds_latest_decision_beyond_recent_events(decision):
    # The interface has the same keyset pagination contract as SkillFlow1.5.77.
    rows = [{"seq": i, "event": "retry_observed", "payload": {}} for i in range(260, 2, -1)]
    rows += [{"seq": 2, "event": decision, "payload": {"step_id": "stage"}},
             {"seq": 1, "event": "checkpoint_approved", "payload": {"step_id": "stage"}}]

    class TraceHost:
        def get_trace(self, _run, *, before_seq=None, limit=None, **kwargs):
            return copy.deepcopy([r for r in rows if before_seq is None or r["seq"] < before_seq][:limit])

        def get_steps(self, _run, **kwargs):
            return [{"step_id": "stage", "status": "completed",
                     "result_flags_json": encode({"stage_sha256": "a" * 64}).decode()}]

    host = object.__new__(adapter.Host)
    host.sf = TraceHost()
    if decision == "checkpoint_rejected":
        with pytest.raises(BenchError, match="manual checkpoint"):
            host.approval("synthetic")
    else:
        assert host.approval("synthetic") == "a" * 64


def test_merge_completed_before_receipt_write_is_recoverable(bench, monkeypatch):
    stage = staged(bench)
    original = bench_module.immutable

    def crash_at_receipt(path: Path, raw: bytes):
        if path.name == "accepted.json":
            raise OSError("simulated receipt write interruption")
        return original(path, raw)

    monkeypatch.setattr(bench_module, "immutable", crash_at_receipt)
    with pytest.raises(OSError, match="receipt write interruption"):
        bench.promote("run1", sha(encode(stage)))
    assert git(bench.policy.repo, "rev-parse", "HEAD") == stage["commit"]
    assert not (bench.work("run1") / "accepted.json").exists()
    monkeypatch.setattr(bench_module, "immutable", original)
    receipt = bench.promote("run1", sha(encode(stage)))
    assert receipt["accepted_commit"] == stage["commit"]
    assert git(bench.policy.repo, "rev-parse", "HEAD^") == stage["base"]
    assert not git(bench.policy.repo, "status", "--porcelain")
