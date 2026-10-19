"""Storage diagnosis must agree with the namespace pytest REALLY uses (UNRUN by its source-only author).

Second-round regressions for the first independent review of
`bcc9e64dcdac1629896ec2da21c8ea0c24cecc1b`: the executor entry assumed `/tmp`;
a repository `addopts = --basetemp=...` moved pytest's `tmp_path` elsewhere, so
a refusal there read as `clean` while a healthy configured namespace was refused
for an unrelated full `/tmp`.

These tests use the REAL executor entry launching a REAL pytest subprocess and
the REAL public `run_tests`. Only the filesystem ANSWER is injected (finite; no
filesystem is filled). Physical byte/inode exhaustion belongs to the separately
registered bounded-tmpfs controls.
"""
import errno
import os

import pytest

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import release_disposition, report_state
from core import repository_executor_entry as entry
from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, PROBE, _baseline, _inject, _run, env)

TMP_TEST = "def test_tmp(tmp_path):\n    assert tmp_path.is_dir()\n"


def _inject_in(env, directory, number):
    """The entry's probe mkdir directly inside `directory` answers `number`."""
    real, seen = os.mkdir, []

    def mkdir(path, *args, **kwargs):
        if (os.path.dirname(str(path)) == str(directory)
                and os.path.basename(str(path)).startswith(PROBE)):
            seen.append(str(path))
            raise OSError(number, os.strerror(number), str(path))
        return real(path, *args, **kwargs)

    env.monkeypatch.setattr(entry.os, "mkdir", mkdir)
    return seen


def _configure_basetemp(env):
    parent = env.tmp / "bounded"
    parent.mkdir()
    (env.repo / "pytest.ini").write_text(
        f"[pytest]\naddopts = --basetemp={parent / 'pytest-base'}\n")
    return parent


def test_entry_resolves_the_configured_basetemp_from_pytest_itself(env):
    parent = _configure_basetemp(env)
    (env.repo / "test_sample.py").write_text(TMP_TEST)
    resolved = entry._resolve_pytest_tmp(
        {"HOME": "/tmp", "PATH": os.environ["PATH"]},
        ["python3", "-m", "pytest", str(env.repo), "-q", "--rootdir", str(env.repo)],
        str(env.repo))
    assert resolved["resolved"] is True and resolved["source"] == "pytest_basetemp"
    assert resolved["path"] == str(parent)


def test_configured_basetemp_exhaustion_is_unmeasured_through_the_public_path(env):
    parent = _configure_basetemp(env)
    result, report = _run(env, GREEN)
    assert result["passed"] is True
    before = _baseline(env).read_bytes()
    seen = _inject_in(env, parent, errno.ENOSPC)
    result, report = _run(env, TMP_TEST)
    assert seen, "the entry must have probed the namespace pytest was configured to use"
    diagnosis = report["storage_diagnosis"]
    assert diagnosis["state"] == "exhausted_preflight"
    assert diagnosis["observation"]["tmp_root"] == str(parent)
    assert diagnosis["proof"][0]["path"] == str(parent)
    assert report["storage_unmeasured"] is True and report["passed"] is False
    assert report_state(report) == "infrastructure_unavailable"
    assert release_disposition(report) == "unresolved"
    assert result["passed_relative"] is False and result["new_failures"] == []
    assert result["baseline_state"] == "unmeasured"
    assert _baseline(env).read_bytes() == before


def test_full_default_tmp_does_not_refuse_a_healthy_configured_basetemp(env):
    _configure_basetemp(env)
    seen = _inject_in(env, "/tmp", errno.ENOSPC)      # unrelated default root is "full"
    result, report = _run(env, TMP_TEST)
    assert not seen, "/tmp is not where this pytest creates tmp_path and must not be probed"
    assert report["returncode"] == 0 and result["passed"] is True
    assert report["storage_diagnosis"]["state"] == "clean"
    assert not report.get("storage_unmeasured") and not report.get("infrastructure_unavailable")


def test_default_namespace_is_still_the_process_temp_dir(env):
    seen = _inject(env, errno.ENOSPC, lambda n: True)
    result, report = _run(env, GREEN)
    assert seen["n"] >= 1
    resolution = report["storage_diagnosis"]["observation"]["tmp_resolution"]
    assert resolution["source"] == "tempfile_gettempdir"
    assert report["storage_unmeasured"] is True


def test_namespace_that_pytest_cannot_resolve_is_unknown_not_clean(env):
    (env.repo / "pytest.ini").write_text("[pytest]\naddopts = --definitely-not-a-flag\n")
    result, report = _run(env, GREEN)
    diagnosis = report["storage_diagnosis"]
    assert diagnosis["state"] == "unknown"
    assert diagnosis["observation"]["tmp_resolution"]["resolved"] is False
    # A real non-zero pytest result with no usable failure identity is not a known red.
    assert report["returncode"] != 0 and report["failures"] == []
    assert result["passed_relative"] is False and result["baseline_state"] == "unmeasured"
    assert report_state(report) != "known_failure"
    assert not _baseline(env).exists()


@pytest.mark.parametrize("malformed", [
    {"schema": rt.STORAGE_SCHEMA},
    {"schema": rt.STORAGE_SCHEMA, "before": "copied label"},
    {"schema": rt.STORAGE_SCHEMA, "before": [{"path": "/tmp", "mkdir": {"ok": True}}]},
    {"schema": rt.STORAGE_SCHEMA, "before": [{"path": "/tmp", "mkdir": {"ok": True},
                                              "write": {"ok": True}, "authoritative": False}]},
])
def test_observation_without_a_successful_syscall_witness_is_unknown(malformed):
    assert rt.diagnose_storage(malformed, 1)["state"] == "unknown"


def test_unresolved_namespace_probe_failure_is_evidence_not_proof():
    storage = {"schema": rt.STORAGE_SCHEMA,
               "tmp_resolution": {"resolved": False, "path": "/tmp"},
               "before": [{"path": "/tmp", "authoritative": False,
                           "mkdir": {"ok": False, "errno": errno.ENOSPC},
                           "write": {"ok": False, "errno": None, "skipped": "mkdir failed"}}]}
    diagnosis = rt.diagnose_storage(storage, 1)
    assert diagnosis["state"] != "exhausted_preflight" and diagnosis["proof"] == []
