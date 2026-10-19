"""A failed delivery stays a failed delivery; storage outranks an unkeyed gate (UNRUN by its author).

Second-round regressions for the first independent review of
`bcc9e64dcdac1629896ec2da21c8ea0c24cecc1b`:

* an identity-error branch ran before the storage guard and repopulated
  `new_failures` on a run whose storage inability was proved;
* a report-write failure discarded the raw output AND the baseline had already
  been written; the evidence-cycle manifest write escaped as an exception, and
  the fail-fast early write bypassed the handler.

Real executor entry + real pytest + real public `run_tests`; only the I/O
answer is injected, finitely. Physical exhaustion is the registered bounded
Docker controls' job.
"""
import errno
import json
from pathlib import Path

import aitelier.gate_evidence as gate_evidence
import aitelier.tools.run_tests.impl as rt
from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, RED, _baseline, _inject, _run, env)


def _unkeyed_gate(env):
    gate = {"script": "tests/run_tests.sh", "returncode": 1,
            "output": "GATE_FAILURE_NO_CASE_IDENTITIES", "measured": rt.REPO_GATE_MEASURED_FAIL}
    env.monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *a, **k: dict(gate))


def _run_with_gate(env, source):
    (env.repo / "test_sample.py").write_text(source)
    result = rt.run_tests(project_root=str(env.repo), out_dir=str(env.out), repo_gate=True,
                          state_dir=str(env.state), run_id="storage-run")
    return result, json.loads((env.out / "test_report.json").read_text())


def _fail_report_write(env):
    real = Path.write_text

    def write_text(self, *args, **kwargs):
        if self.name == "test_report.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "write_text", write_text)


def test_during_run_exhaustion_with_unkeyed_default_gate_stays_unmeasured(env):
    _unkeyed_gate(env)
    _inject(env, errno.ENOSPC, lambda n: n >= 2)      # clean before, refused after
    result, report = _run_with_gate(env, RED)
    assert report["storage_diagnosis"]["state"] == "exhausted_during_run"
    assert report.get("failure_identity_error"), "must really reach the identity-error branch"
    assert result["baseline_state"] == "unmeasured" and result["new_failures"] == []
    assert result["passed_relative"] is False and not _baseline(env).exists()
    assert report["failure_identity_error"] in report["baseline_error"]   # kept as a diagnostic
    assert "test_red" in report["stdout"]                                  # raw red kept


def test_identity_error_without_storage_trouble_keeps_its_existing_meaning(env):
    _unkeyed_gate(env)
    result, report = _run_with_gate(env, GREEN)
    assert result["baseline_state"] == "unreadable" and result["passed_relative"] is False
    assert result["new_failures"]


def test_report_write_failure_keeps_raw_output_and_writes_no_baseline(env):
    _fail_report_write(env)
    result, report = _run(env, RED)
    assert report is None and result["written"] is None and result["passed"] is False
    assert result["report_write_failed"] and result["returncode_observed"] == 1
    assert "test_red" in result["stdout"] and "assert 1 + 1 == 3" in result["stdout"]
    assert any("test_red" in f for f in result["failures"])
    assert result["storage_diagnosis"]["state"] == "clean"
    assert result["raw_retained"]["stdout"] == result["stdout"]
    assert result["baseline_persisted"] is False and result["new_failures"] == []
    assert not _baseline(env).exists(), "no baseline from a report nobody can read"


def test_report_write_failure_leaves_an_existing_baseline_untouched(env):
    _run(env, RED)                                     # delivered: seeds the known red
    before = _baseline(env).read_bytes()
    _fail_report_write(env)
    result, report = _run(env, GREEN)                  # would prune the baseline if persisted
    assert result["written"] is None
    assert _baseline(env).read_bytes() == before


def test_delivered_report_still_persists_its_baseline(env):
    result, report = _run(env, RED)
    assert result["written"] == "test_report.json" and result["baseline_state"] == "seeded"
    assert _baseline(env).is_file() and report["baseline_seeded"] is True


def test_evidence_cycle_manifest_failure_is_a_nondelivery_not_an_exception(env):
    def manifest(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    env.monkeypatch.setattr(gate_evidence, "_write_cycle_manifest", manifest)
    result, report = _run(env, RED, evidence_cycle_start=True)
    assert report is None and result["written"] is None and result["passed"] is False
    assert result["storage_exhausted"] is True and result["infrastructure_unavailable"]
    assert result["release_evidence"] == "unresolved" and result["returncode_observed"] == 1
    assert "test_red" in result["stdout"]
    assert not _baseline(env).exists()


def test_fail_fast_early_report_write_failure_is_a_nondelivery(env):
    blocker = {"step": "5_test", "file": "test_report.json", "state": "failed"}
    env.monkeypatch.setattr(gate_evidence, "first_upstream_blocker", lambda *a, **k: blocker)
    _fail_report_write(env)
    (env.repo / "test_sample.py").write_text(GREEN)
    result = rt.run_tests(project_root=str(env.repo), out_dir=str(env.out), repo_gate=False,
                          state_dir=str(env.state), run_id="storage-run",
                          fail_fast_gates="5_test/test_report.json")
    assert result["written"] is None and result["passed"] is False
    assert result["report_write_failed"] is True and result["storage_exhausted"] is True
    assert not _baseline(env).exists()
