"""Storage diagnosis must agree with the namespace pytest REALLY uses (UNRUN by its source-only author).

Third round (R3) for the second independent review of
`5bc303a09da9728a226df8b32619d7e6132064a8`: a separate `--noconftest` resolver
process could not see a conftest `pytest_configure` that set
`option.basetemp`, so an ENOSPC in that real namespace read as `clean`. The
namespace is reported by the REAL pytest session when its normal temp factory
first initializes its base, after pytest's own reset/creation. The entry probes
that actual base; collection or earlier tests may already have run.

These tests use the REAL executor entry launching a REAL pytest subprocess and
the REAL public `run_tests`. Only the filesystem ANSWER is injected (finite; no
filesystem is filled). Physical byte/inode exhaustion belongs to the separately
registered bounded-tmpfs controls.
"""
import errno
import os
import time

import pytest

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import release_disposition, report_state
from core import repository_executor_entry as entry
from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, PROBE, RED, TMP_GREEN, _baseline, _inject, _run, env)

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
    return parent / "pytest-base"


def _conftest_basetemp(env):
    """A normal conftest `pytest_configure` selects basetemp (the R2 failure path)."""
    parent = env.tmp / "hooked"
    parent.mkdir()
    (env.repo / "conftest.py").write_text(
        "def pytest_configure(config):\n"
        f"    config.option.basetemp = {str(parent / 'hooked-base')!r}\n")
    return parent / "hooked-base"


def test_conftest_configured_basetemp_is_the_witnessed_namespace(env):
    parent = _conftest_basetemp(env)
    result, report = _run(env, TMP_TEST)
    resolution = report["storage_diagnosis"]["observation"]["tmp_resolution"]
    assert resolution["resolved"] is True and resolution["source"] == "pytest_initialized_basetemp"
    assert resolution["path"] == str(parent)
    assert report["returncode"] == 0 and result["passed"] is True
    assert report["storage_diagnosis"]["state"] == "clean"
    # The configured basetemp was really used by the real tmp_path.
    assert parent.is_dir()


def test_conftest_configured_basetemp_exhaustion_is_never_a_known_failure(env):
    marker = env.tmp / "a-test-ran"
    parent = _conftest_basetemp(env)
    seen = _inject_in(env, parent, errno.ENOSPC)
    result, report = _run(env, f"def test_tmp(tmp_path):\n    open({str(marker)!r}, 'w')\n")
    assert seen, "the entry must probe the namespace the conftest configured"
    diagnosis = report["storage_diagnosis"]
    assert diagnosis["state"] == "exhausted_during_run"
    assert diagnosis["proof"][0]["path"] == str(parent)
    assert diagnosis["observation"]["refused_at"] == "temp_factory_use"
    assert not marker.exists(), "no test may run after the entry's refusal"
    assert report["storage_unmeasured"] is True and report["passed"] is False
    assert report_state(report) == "infrastructure_unavailable"
    assert release_disposition(report) == "unresolved" == result["release_evidence"]
    assert result["passed_relative"] is False and result["new_failures"] == []
    assert result["baseline_state"] == "unmeasured" and not _baseline(env).exists()


def test_conftest_basetemp_does_not_override_the_witness_with_another_root(env):
    """A full unrelated /tmp is not where this session creates tmp_path."""
    _conftest_basetemp(env)
    seen = _inject_in(env, "/tmp", errno.ENOSPC)
    result, report = _run(env, TMP_TEST)
    assert not seen and result["passed"] is True
    assert report["storage_diagnosis"]["state"] == "clean"


def test_namespace_witness_spends_the_same_single_wall_budget(env):
    """No separate resolver budget: a slow configuration counts against `timeout`."""
    (env.repo / "conftest.py").write_text(
        "import time\ndef pytest_configure(config):\n    time.sleep(5)\n")
    (env.repo / "test_sample.py").write_text(GREEN)
    started = time.monotonic()
    result = entry.run({"repo": str(env.repo), "timeout": 1, "pytest_timeout": True,
                        "import_module": "",
                        "args": ["python3", "-m", "pytest", str(env.repo), "-q",
                                 "--rootdir", str(env.repo)]})
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert elapsed < 4, f"one wall budget, observed {elapsed:.3f}s for timeout=1"


def test_normal_basetemp_reset_precedes_the_required_temp_observation(env):
    base = _conftest_basetemp(env)
    base.mkdir()
    stale = base / "stale-from-previous-session"
    stale.write_text("old disposable data")
    source = ("def test_tmp(tmp_path):\n"
              "    assert not (tmp_path.parent / 'stale-from-previous-session').exists()\n")
    result, report = _run(env, source)
    resolution = report["storage_diagnosis"]["observation"]["tmp_resolution"]
    assert result["passed"] is True and not stale.exists()
    assert resolution["path"] == str(base)
    assert resolution["observed_at"] == "temp_factory_use"
    assert resolution["initialization_error"] is None


def test_unused_temp_factory_does_not_reset_or_probe_its_basetemp(env):
    base = _conftest_basetemp(env)
    base.mkdir()
    stale = base / "still-owned-by-normal-pytest-cleanup"
    stale.write_text("not used by this session")
    probe, seen = entry._probe_dir, []

    def observe(path):
        seen.append(str(path))
        return probe(path)

    env.monkeypatch.setattr(entry, "_probe_dir", observe)
    result, report = _run(env, GREEN)                  # no tmp_path fixture
    assert result["passed"] is True and stale.read_text() == "not used by this session"
    assert str(base) not in seen and str(base.parent) not in seen
    resolution = report["storage_diagnosis"]["observation"]["tmp_resolution"]
    assert resolution["source"] == "not_required"


def test_late_required_temp_refusal_keeps_an_earlier_real_red(env):
    """pytest's Exit path prints no assertion detail for the completed red; the
    same invocation's JUnit does, and the report must keep it (R4 finding).

    The original stdout is NOT required to contain the detail (it really does
    not); it must stay exactly what the child printed. The completed red's
    identity AND assertion text must reach the report/return diagnostics,
    without becoming a baseline key or a new failure.
    """
    base = _conftest_basetemp(env)
    _inject_in(env, base, errno.ENOSPC)
    marker = env.tmp / "tmp-body-ran"
    source = ("def test_earlier_red():\n    assert False, 'FIRST_REAL_RED'\n"
              f"def test_tmp(tmp_path):\n    open({str(marker)!r}, 'w').write('x')\n")
    result, report = _run(env, source)
    # A real, nonempty child invocation that stopped at the storage refusal.
    assert report["stdout"].strip(), "the child pytest must really have run"
    assert report["returncode"] not in (0, None)
    assert not marker.exists()
    assert report["storage_diagnosis"]["state"] == "exhausted_during_run"
    assert report["storage_diagnosis"]["observation"]["refused_at"] == "temp_factory_use"
    assert "INCOMPLETE" in report["summary"] and "NOT TAKEN" not in report["summary"]
    # Completed-red first evidence from this session's JUnit, on usable surfaces.
    reds = report["completed_before_storage_refusal"]
    assert [r["node_id"] for r in reds] == ["test_sample.py::test_earlier_red"]
    assert "FIRST_REAL_RED" in reds[0]["message"]
    assert "AssertionError" in reds[0]["detail"] and "FIRST_REAL_RED" in reds[0]["detail"]
    assert any("test_sample.py::test_earlier_red" in f and "FIRST_REAL_RED" in f
               for f in report["failures"])
    assert "FIRST_REAL_RED" in report["summary"]
    assert result["completed_before_storage_refusal"] == reds
    # The tmp-dependent test never completed, so it is not listed as a red.
    assert not any("test_tmp" in r["node_id"] for r in reds)
    # Still unmeasured: never attributed, never a baseline or relative pass.
    assert report["storage_unmeasured"] is True and report["passed"] is False
    assert report_state(report) == "infrastructure_unavailable"
    assert result["new_failures"] == [] and result["passed_relative"] is False
    assert result["baseline_state"] == "unmeasured" and not _baseline(env).exists()


def test_completed_red_retention_is_only_for_a_late_storage_refusal(env):
    """Inverse controls: a clean red keeps its ordinary path and adds no JUnit copy."""
    _conftest_basetemp(env)
    result, report = _run(env, "def test_red():\n    assert False, 'ORDINARY_RED'\n")
    assert report["storage_diagnosis"]["state"] == "clean"
    assert "completed_before_storage_refusal" not in report
    assert result["completed_before_storage_refusal"] == []
    assert any("test_red" in f for f in report["failures"])
    assert result["baseline_state"] == "seeded"


def test_late_refusal_without_an_earlier_red_retains_no_invented_red(env):
    base = _conftest_basetemp(env)
    _inject_in(env, base, errno.ENOSPC)
    source = "def test_green():\n    pass\n" + TMP_TEST
    result, report = _run(env, source)
    assert report["storage_diagnosis"]["state"] == "exhausted_during_run"
    assert "completed_before_storage_refusal" not in report
    assert result["completed_before_storage_refusal"] == []
    assert result["new_failures"] == [] and not _baseline(env).exists()


def test_junit_completed_reds_reader_is_bounded(tmp_path):
    xml = tmp_path / "junit.xml"
    long_detail = "x" * 5000
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase file="a.py" classname="a" name="t_ok"/>'
        f'<testcase file="a.py" classname="a" name="t_red"><failure message="boom">{long_detail}'
        '</failure></testcase>'
        '<testcase file="a.py" classname="a" name="t_err"><error message="setup">E</error></testcase>'
        '</testsuite></testsuites>')
    reds = rt._junit_completed_reds(xml)
    assert [(r["node_id"], r["kind"]) for r in reds] == [("a.py::t_red", "failure"),
                                                          ("a.py::t_err", "error")]
    assert len(reds[0]["detail"]) == 2000 and reds[0]["detail_complete"] is False
    assert rt._junit_completed_reds(tmp_path / "missing.xml") == []
    assert rt._junit_node_ids(xml) == {"a.py::t_ok", "a.py::t_red", "a.py::t_err"}


def test_nonzero_session_without_identities_is_unmeasured_even_when_clean(env):
    (env.repo / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 3\n")
    result, report = _run(env, GREEN)
    assert report["returncode"] not in (0, None) and report["failures"] == []
    assert result["passed_relative"] is False and result["baseline_state"] == "unmeasured"
    assert report_state(report) == "failed" and not _baseline(env).exists()


def test_real_assertion_red_is_still_identified_with_a_witnessed_namespace(env):
    _conftest_basetemp(env)
    result, report = _run(env, RED)
    assert report["storage_diagnosis"]["state"] == "clean"
    assert any("test_red" in f for f in report["failures"])
    assert result["baseline_state"] == "seeded" and _baseline(env).is_file()

def test_configured_basetemp_exhaustion_is_unmeasured_through_the_public_path(env):
    parent = _configure_basetemp(env)
    result, report = _run(env, GREEN)
    assert result["passed"] is True
    before = _baseline(env).read_bytes()
    seen = _inject_in(env, parent, errno.ENOSPC)
    result, report = _run(env, TMP_TEST)
    assert seen, "the entry must have probed the namespace pytest was configured to use"
    diagnosis = report["storage_diagnosis"]
    assert diagnosis["state"] == "exhausted_during_run"
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
    result, report = _run(env, TMP_GREEN)
    assert seen["n"] >= 1
    resolution = report["storage_diagnosis"]["observation"]["tmp_resolution"]
    assert resolution["source"] == "pytest_initialized_basetemp"
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
