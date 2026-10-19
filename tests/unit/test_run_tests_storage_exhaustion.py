"""Storage exhaustion is not a test regression (execution.storage-exhaustion-is-not-a-test-regression).

Historical attestation (iss-8403bf9711304e5c, 2026-09-19): on a host whose root
was 100% full (349M free) pytest's ``tmp_path`` (``make_numbered_dir``) swallowed
the ENOSPC of its ``mkdir`` and raised only "could not create numbered dir ...
after 10 tries". One round reported 2561 passed / 6 skipped / 41 errors and was
looped back to the implementer; the same commit in a clean container passed
(2602 passed, rc 0). The original cause on that host is NOT re-derived here.

These tests never fill a real filesystem. The storage answers are fault-injected
at the ``os.mkdir`` / ``os.statvfs`` syscalls of the REAL executor entry
(``core.repository_executor_entry.run`` launching a real pytest subprocess), and
the REAL public ``run_tests`` consumes its result; a future evaluator that owns a
<=32MiB tmpfs may replace the injection with a real full filesystem. UNRUN by
their author (source-only workflow).
"""
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import release_disposition, report_state
from core import repository_executor_entry as entry


GREEN = "def test_ok():\n    assert 1 + 1 == 2\n"
RED = "def test_red():\n    assert 1 + 1 == 3\n"
PROBE = ".aitelier-storage-probe-"


def _call_entry(root, args, timeout, **kwargs):
    result = entry.run({"repo": str(root), "args": args, "timeout": min(timeout, 120),
                        "pytest_timeout": True, "import_module": ""})
    return SimpleNamespace(**result)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    calls = []

    def execute(root, args, timeout, **kwargs):
        calls.append(list(args))
        return _call_entry(root, args, timeout)

    monkeypatch.setattr(rt.repository_executor, "execute", execute)
    repo = tmp_path / "repo"
    repo.mkdir()
    return SimpleNamespace(tmp=tmp_path, repo=repo, calls=calls, state=tmp_path / "state",
                           out=tmp_path / "out", monkeypatch=monkeypatch)


def _run(env, source, **kwargs):
    (env.repo / "test_sample.py").write_text(source)
    result = rt.run_tests(project_root=str(env.repo), out_dir=str(env.out), repo_gate=False,
                          state_dir=str(env.state), run_id="storage-run", **kwargs)
    path = env.out / "test_report.json"
    return result, (json.loads(path.read_text()) if path.exists() else None)


def _baseline(env) -> Path:
    return Path(rt._baseline_dir(str(env.state), env.repo.resolve())) / rt.BASELINE_FILE


def _inject(env, number, when):
    """The entry's own probe mkdir in the child's tmp namespace answers `number`."""
    real, seen = os.mkdir, {"n": 0}

    def mkdir(path, *args, **kwargs):
        if os.path.dirname(str(path)) == "/tmp" and os.path.basename(str(path)).startswith(PROBE):
            seen["n"] += 1
            if when(seen["n"]):
                raise OSError(number, os.strerror(number), str(path))
        return real(path, *args, **kwargs)

    env.monkeypatch.setattr(entry.os, "mkdir", mkdir)
    return seen


def _vfs(env, **fields):
    real = os.statvfs
    base = dict(f_bsize=4096, f_frsize=4096, f_blocks=1000, f_bfree=500, f_bavail=500,
                f_files=1000, f_ffree=500, f_favail=500)
    base.update(fields)

    def statvfs(path):
        return SimpleNamespace(**base) if str(path) == "/tmp" else real(path)

    env.monkeypatch.setattr(entry.os, "statvfs", statvfs)


# --- the pure classification ---------------------------------------------------

def _fail(number):
    return {"ok": False, "errno": number, "errname": errno.errorcode.get(number, "")}


def _observation(mkdir=None, write=None, vfs=None):
    ok = {"ok": True, "errno": None}
    return {"path": "/tmp", "mkdir": mkdir or ok, "write": write or ok, "statvfs": vfs}


def _storage(before, after=()):
    return {"schema": rt.STORAGE_SCHEMA, "before": list(before), "after": list(after)}


@pytest.mark.parametrize("number,state", [
    (errno.ENOSPC, "exhausted_preflight"), (errno.EDQUOT, "exhausted_preflight"),
    (errno.EACCES, "ambiguous"), (errno.EROFS, "ambiguous"), (None, "ambiguous")])
def test_only_a_trusted_enospc_or_edquot_is_exhaustion(number, state):
    failed = _fail(number) if number else {"ok": False, "errno": None}
    storage = _storage([_observation(mkdir=failed)])
    assert rt.diagnose_storage(storage, 1)["state"] == state


def test_probe_failure_after_a_clean_start_is_exhaustion_only_for_a_nonclean_session():
    storage = _storage([_observation()], [_observation(write=_fail(errno.ENOSPC))])
    assert rt.diagnose_storage(storage, 1)["state"] == "exhausted_during_run"
    assert rt.diagnose_storage(storage, 0)["state"] == "ambiguous"   # clean pytest: do not waive


def test_clean_and_unknown_observations_waive_nothing():
    assert rt.diagnose_storage(_storage([_observation()]), 1)["state"] == "clean"
    for missing in (None, {}, {"schema": "something-else", "before": []}, "ENOSPC"):
        assert rt.diagnose_storage(missing, 1)["state"] == "unknown"


def test_bytes_versus_inodes_come_from_retained_statvfs_not_from_a_threshold():
    inodes = {"f_blocks": 1000, "f_bavail": 500, "f_files": 100, "f_favail": 0}
    byte = {"f_blocks": 1000, "f_bavail": 0, "f_files": 100, "f_favail": 50}
    plenty = {"f_blocks": 1000, "f_bavail": 900, "f_files": 100, "f_favail": 90}
    for vfs, resource in ((inodes, ["inodes"]), (byte, ["bytes"]), (plenty, ["unspecified"])):
        storage = _storage([_observation(mkdir=_fail(errno.ENOSPC), vfs=vfs)])
        proof = rt.diagnose_storage(storage, 1)["proof"]
        assert proof and proof[0]["resource"] == resource
    # Almost no free space, with every syscall answering ok, is not exhaustion at all.
    scarce = {"f_blocks": 1000, "f_bavail": 1, "f_files": 100, "f_favail": 1}
    assert rt.diagnose_storage(_storage([_observation(vfs=scarce)]), 1)["state"] == "clean"


# --- the real entry + the real public run_tests -------------------------------

def test_preflight_enospc_is_unmeasured_never_a_failure_or_baseline(env):
    marker = env.tmp / "pytest-was-started"
    (env.repo / "conftest.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    _inject(env, errno.ENOSPC, lambda n: True)
    _vfs(env, f_ffree=0, f_favail=0)
    result, report = _run(env, RED)

    assert report is not None and result["written"] == "test_report.json"
    assert env.calls, "the real executor entry must have been called"
    assert not marker.exists(), "no repository code may run after a proved refusal"
    proof = report["storage_diagnosis"]["proof"]
    assert proof and proof[0]["errno"] == errno.ENOSPC and proof[0]["errname"] == "ENOSPC"
    assert proof[0]["resource"] == ["inodes"] and proof[0]["path"] == "/tmp"
    assert report["storage_diagnosis"]["observation"]["before"][0]["statvfs"]["f_favail"] == 0
    # Explicit infrastructure not-run, in the file and on the return.
    assert report["passed"] is False and report["skipped"] is True
    assert report["infrastructure_unavailable"] and report["storage_unmeasured"]
    assert report["skipped_because"] == "storage_exhausted"
    assert report_state(report) == "infrastructure_unavailable"
    assert release_disposition(report) == "unresolved" == result["release_evidence"]
    assert result["evidence_state"] == "infrastructure_unavailable" and result["storage_unmeasured"]
    assert result["storage_state"] == "exhausted_preflight"
    # Raw executor return preserved, no invented test failure, no baseline.
    assert report["returncode"] == -1 and "storage preflight refused: ENOSPC" in report["stderr"]
    assert not report["collection_errors"]
    assert all(f.startswith("pytest:storage exhausted") for f in report["failures"])
    assert result["new_failures"] == [] and result["passed_relative"] is False
    assert result["baseline_state"] == "unmeasured" and report["baseline_failures"] == []
    assert not _baseline(env).exists()


def test_exhaustion_during_the_run_keeps_the_real_red_but_not_as_a_regression(env):
    result, report = _run(env, RED)                      # seeds the known red
    assert report["baseline_state"] == "seeded" and _baseline(env).is_file()
    seeded = _baseline(env).read_bytes()
    result, report = _run(env, RED)
    assert report["baseline_state"] == "compared" and result["passed_relative"] is True

    _inject(env, errno.ENOSPC, lambda n: n >= 2)         # clean before, refused after
    result, report = _run(env, RED)
    assert report["storage_diagnosis"]["state"] == "exhausted_during_run"
    assert report["returncode"] == 1 and "test_red" in report["stdout"]    # raw pytest retained
    assert any("FAILED" in f and "test_red" in f for f in report["failures"])   # first evidence kept
    assert report["passed"] is False and report["infrastructure_unavailable"] is True
    assert report_state(report) == "infrastructure_unavailable"
    assert result["passed_relative"] is False and result["new_failures"] == []
    assert result["baseline_state"] == "unmeasured" and report["baseline_failures"] == []
    assert _baseline(env).read_bytes() == seeded, "an unmeasured run may not touch the baseline"


def test_capacity_recovered_run_measures_again_and_is_not_poisoned(env):
    _inject(env, errno.ENOSPC, lambda n: True)
    _, report = _run(env, GREEN)
    assert report["storage_unmeasured"] and not _baseline(env).exists()
    env.monkeypatch.undo()                               # capacity is back
    env.monkeypatch.setenv("AITELIER_HOME", str(env.tmp / "home"))
    env.monkeypatch.setattr(rt.repository_executor, "execute", _call_entry)
    result, report = _run(env, GREEN)
    observation = report["storage_diagnosis"]
    assert observation["state"] == "clean" and observation["observation"]["before"]
    assert all(o["mkdir"]["ok"] and o["write"]["ok"] for o in observation["observation"]["before"])
    assert result["passed"] is True and report["returncode"] == 0
    assert not report.get("skipped") and not report.get("infrastructure_unavailable")
    assert result["baseline_state"] == "seeded" and result["passed_relative"] is True
    assert report_state(report) == "passed"


def test_genuine_assertion_failure_with_clean_storage_is_still_a_new_failure(env):
    base = _baseline(env)
    base.parent.mkdir(parents=True)
    base.write_text(json.dumps({"failures": ["test_other.py::test_gone"], "updated_at": 1}))
    result, report = _run(env, RED)
    assert report["storage_diagnosis"]["state"] == "clean"
    assert report["passed"] is False and not report.get("skipped")
    assert not report.get("infrastructure_unavailable") and not report.get("storage_unmeasured")
    assert result["new_failures"] and "test_red" in result["new_failures"][0]
    assert result["passed_relative"] is False and result["baseline_state"] == "compared"
    assert report_state(report) == "failed"
