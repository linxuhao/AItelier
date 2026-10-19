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
import hashlib
import json
import os
from pathlib import Path

import pytest

import aitelier.gate_evidence as gate_evidence
import aitelier.tools.run_tests.impl as rt
from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, RED, TMP_RED, _baseline, _inject, _run, env)


def _unkeyed_gate(env):
    gate = {"script": "tests/run_tests.sh", "returncode": 1,
            "output": "GATE_FAILURE_NO_CASE_IDENTITIES", "measured": rt.REPO_GATE_MEASURED_FAIL}
    env.monkeypatch.setattr(rt, "_acquire_repo_gate", lambda *a, **k: dict(gate))


def _run_with_gate(env, source):
    (env.repo / "test_sample.py").write_text(source)
    result = rt.run_tests(project_root=str(env.repo), out_dir=str(env.out), repo_gate=True,
                          state_dir=str(env.state), run_id="storage-run")
    return result, json.loads((env.out / "test_report.json").read_text())


def _is_report(path):
    return path.name == "test_report.json" or path.name.startswith(".test_report.json.tmp")


def _fail_report_write(env):
    real = Path.write_text

    def write_text(self, *args, **kwargs):
        if _is_report(self):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "write_text", write_text)


def _fail_baseline_write(env, number=errno.ENOSPC):
    """The baseline write truncates its target and then answers `number`."""
    real = Path.write_text
    calls = []

    def write_text(self, *args, **kwargs):
        if rt.BASELINE_FILE in self.name:
            calls.append(str(self))
            real(self, "", encoding="utf-8")           # really truncate what it opened
            raise OSError(number, os.strerror(number), str(self))
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "write_text", write_text)
    return calls


def test_during_run_exhaustion_with_unkeyed_default_gate_stays_unmeasured(env):
    _unkeyed_gate(env)
    _inject(env, errno.ENOSPC, lambda n: n >= 2)      # clean before, refused after
    result, report = _run_with_gate(env, TMP_RED)
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
    result, report = _run(env, RED.replace("1 + 1 == 3", "1 + 1 == 2"))  # same node, would prune
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


# --- R3: baseline persistence and report publication compose truthfully -------

def test_failed_baseline_seed_after_real_red_claims_no_seed_or_attribution(env):
    calls = _fail_baseline_write(env)
    result, report = _run(env, RED, evidence_cycle_start=True)
    assert calls, "the baseline write must really have been attempted"
    # The genuine red stays a red, with its raw evidence and identity.
    assert report["returncode"] == 1 and "test_red" in report["stdout"]
    assert any("test_red" in f for f in report["failures"])
    # ...but no seed that is not on disk is claimed, in the file or the return.
    for view in (report, result):
        assert view["baseline_state"] == "unmeasured"
        assert view["passed_relative"] is False and view["new_failures"] == []
        assert view["baseline_persisted"] is False and "ENOSPC" in view["baseline_error"]
    assert not report.get("baseline_seeded") and report["baseline_failures"] == []
    assert gate_evidence.report_state(report) == "failed"
    assert report["evidence_state"] == result["evidence_state"] == "failed"
    # This legacy routing label includes both real failed and attributed known-red reports.
    assert report["release_evidence"] == result["release_evidence"] == "known_failure"
    assert not _baseline(env).exists() or _baseline(env).read_text() == ""
    assert result["written"] == "test_report.json"   # the report itself WAS delivered


def test_failed_seed_does_not_reset_attribution_on_recovery(env):
    """After a failed seed the next delivered run seeds normally; nothing is pretended."""
    _fail_baseline_write(env)
    _run(env, RED)
    env.monkeypatch.setattr(Path, "write_text", _REAL_WRITE_TEXT)
    result, report = _run(env, RED)
    assert result["baseline_state"] == "seeded" and result["baseline_persisted"] is True
    assert json.loads(_baseline(env).read_text())["failures"]


def test_failed_prune_leaves_existing_baseline_intact_and_claims_nothing(env):
    _run(env, RED)                                     # delivered seed
    before = _baseline(env).read_bytes()
    _fail_baseline_write(env)
    result, report = _run(env, RED.replace("1 + 1 == 3", "1 + 1 == 2"))  # same test_red, now green
    assert result["baseline_state"] == "unmeasured" and result["baseline_persisted"] is False
    assert result["passed"] is True                    # absolute green is still green
    assert _baseline(env).read_bytes() == before, "a failed prune may not damage the baseline"


@pytest.mark.parametrize("existing", [False, True])
def test_report_publish_failure_after_baseline_is_a_nondelivery_and_keeps_old_report(env, existing):
    _run(env, RED if existing else GREEN)
    if not existing:
        _baseline(env).unlink()                        # next run really needs a seed
    source = RED.replace("1 + 1 == 3", "1 + 1 == 2") if existing else RED
    previous = (env.out / "test_report.json").read_bytes()
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == "test_report.json":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(src, dst, *args, **kwargs)

    env.monkeypatch.setattr(rt.os, "replace", replace)
    result, _ = _run(env, source)
    assert result["written"] is None and result["passed"] is False and result["_error"]
    assert result["report_write_failed"] and result["release_evidence"] == "unresolved"
    assert result["baseline_persisted"] is True
    stored = json.loads(_baseline(env).read_text())["failures"]
    assert (not stored) if existing else any("test_red" in f for f in stored)
    assert result["returncode_observed"] == (0 if existing else 1)
    if not existing:
        assert "test_red" in result["stdout"]
    assert result["raw_bounds"]["stdout"]["complete"]
    assert (env.out / "test_report.json").read_bytes() == previous, "never truncated"
    assert not list(env.out.glob(".test_report.json.tmp*"))


def test_report_restage_failure_after_baseline_error_is_not_a_written_pass(env):
    """R2-D1: the diagnostic rewrite failing used to be swallowed."""
    real = Path.write_text
    staged = []

    def write_text(self, *args, **kwargs):
        if rt.BASELINE_FILE in self.name:
            raise OSError(errno.ENOSPC, "No space left on device")
        if self.name.startswith(".test_report.json.tmp"):
            staged.append(1)
            if len(staged) >= 2:                       # the re-stage after baseline_error
                real(self, "{", encoding="utf-8")       # partial write, then fail
                raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "write_text", write_text)
    result, report = _run(env, GREEN)
    assert len(staged) == 2
    assert result["written"] is None and result["passed"] is False and result["_error"]
    assert result["baseline_persisted"] is False and "ENOSPC" in result["baseline_error"]
    assert report is None and not list(env.out.glob(".test_report.json.tmp*"))


def test_oversized_output_is_marked_incomplete_never_called_full_raw(env):
    _fail_report_write(env)
    execute = rt.repository_executor.execute
    captured = {}

    def capture(*args, **kwargs):
        done = execute(*args, **kwargs)
        captured["stdout"] = done.stdout
        return done

    env.monkeypatch.setattr(rt.repository_executor, "execute", capture)
    noisy = "def test_noisy():\n    print('x' * 70000)\n    assert False\n"
    (env.repo / "conftest.py").write_text("")
    result, _ = _run(env, noisy, )
    bounds = result["raw_bounds"]["stdout"]
    assert len(captured["stdout"]) > 60_000, "the real child must produce oversized output"
    assert bounds["chars"] > 60_000, "the real child must produce oversized output"
    assert bounds["chars"] == len(captured["stdout"])
    assert bounds["sha256"] == hashlib.sha256(captured["stdout"].encode()).hexdigest()
    assert bounds["complete"] is False
    assert result["stdout"].startswith("[...truncated head...]")
    assert len(result["stdout"]) <= 60_000 + len("[...truncated head...]")


_REAL_WRITE_TEXT = Path.write_text
