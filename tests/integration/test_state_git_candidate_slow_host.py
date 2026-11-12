"""A candidate inside the size bound is not refused because Git was slow.

Measured 2026-11-12 on the candidate State refused three times
(a 116.8 MiB bundle carrying 1.05 GB of blobs,
Git 2.47.3, a core to itself): the normalizing ``pack-objects`` used 109 s of
CPU, the import's fetch 8 s and its fsck 6.4 s. Each Git step had a fixed 120 s,
so the candidate was refused as soon as the host was 10% slower.
"""
import hashlib
import os
import shutil
import subprocess
import threading
import time

import pytest

from core.state_driver_guide import STATE_DRIVER_GUIDE
from core.state_git_artifacts import MAX_BUNDLE_BYTES, retain_candidate
from core.state_graph import StateConflict
from tests.integration.test_state_git_candidate_only_custody import (
    broad_bundle, make_repo, report_for)

# CPU seconds per Git step on the refused candidate, and that bundle's size.
MEASURED = {"pack-objects": 109.0, "fetch": 8.0, "fsck": 6.4}
MEASURED_MIB = 116.8
BOUND_MIB = MAX_BUNDLE_BYTES / 2**20


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AITELIER_STATE_GIT_TIMEOUT_SEC", raising=False)


@pytest.fixture
def slow_host(monkeypatch):
    """Call with a factor: from then on every budgeted Git step "takes" its
    measured time times that factor, and ends the way it would on such a host
    when its budget is shorter. Returns the list of steps that ran."""
    real, ran = subprocess.run, []

    def install(factor):
        def run(argv, *args, timeout=None, **kwargs):
            result = real(argv, *args, **kwargs)
            step = next((name for name in MEASURED if name in argv), None)
            if timeout is not None and step is not None:
                ran.append(step)
                if timeout < MEASURED[step] * factor:
                    raise subprocess.TimeoutExpired(argv, timeout)
            return result
        monkeypatch.setattr(subprocess, "run", run)
        return ran
    return install


def slow_git(tmp_path, monkeypatch, seconds=0, hang=False):
    """Put a ``git`` first on PATH whose pack-objects sleeps (or never returns)."""
    bindir, log = tmp_path / "slow-bin", tmp_path / "pack-objects.log"
    bindir.mkdir()
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = pack-objects ]; then\n'
        '    echo pack-objects >> "$SLOW_GIT_LOG"\n'
        '    [ "$SLOW_GIT_HANG" = 1 ] && exec sleep 600\n'
        '    sleep "$SLOW_GIT_SECONDS"\n'
        "  fi\n"
        "done\n"
        'exec "$REAL_GIT" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("REAL_GIT", shutil.which("git"))
    monkeypatch.setenv("SLOW_GIT_LOG", str(log))
    monkeypatch.setenv("SLOW_GIT_SECONDS", str(seconds))
    monkeypatch.setenv("SLOW_GIT_HANG", "1" if hang else "0")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return log


@pytest.mark.parametrize("factor", [
    pytest.param(3.0, id="the-refused-candidate-on-a-host-three-times-slower"),
    pytest.param(3.0 * BOUND_MIB / MEASURED_MIB,
                 id="a-bundle-at-the-size-bound-at-the-same-cost-per-MiB-three-times-slower"),
])
def test_candidate_inside_the_size_bound_is_retained_on_a_slow_host(tmp_path, slow_host, factor):
    repo, commits = make_repo(tmp_path)
    bundle = broad_bundle(repo, tmp_path / "broad.bundle")
    idle = retain_candidate(report_for(bundle), commits[-1])

    ran = slow_host(factor)
    slow = retain_candidate(report_for(bundle), commits[-1])

    assert {"pack-objects", "fetch", "fsck"} <= set(ran)
    # The budget moved; the normalization did not: same bytes as on an idle host.
    assert slow["bundle_sha256"] == idle["bundle_sha256"]
    assert slow["bundle_bytes"] == idle["bundle_bytes"]


def test_slow_report_is_accepted_and_resending_it_starts_no_second_normalization(
        tmp_path, slow_host):
    from tests.integration.test_state_git_artifact_durability import fixture
    ran = slow_host(3.0)
    service, source, producer, db, attempt, commit, tree, payload, bundle = fixture(tmp_path / "f")
    assert service.get_attempt(attempt["attempt_id"])["git_artifact"]["commit_sha"] == commit
    packs = ran.count("pack-objects")
    assert packs == 1

    # What a caller whose own timeout fired first does: resend the identical call.
    body = (tmp_path / "f" / "report.json").read_bytes()
    again = service.report_external_attempt(
        attempt["attempt_id"], "done", 0, attempt["context_hash"], "candidate",
        str(tmp_path / "f" / "report.json"), hashlib.sha256(body).hexdigest(), True, commit, "git-sha1")
    assert again["idempotent"] is True
    assert ran.count("pack-objects") == packs

    # The retained bundle then materializes for an exact-base start on the same host.
    review = service.start_external_attempt("durable", "review", 1, "fixture", "review", "review",
                                            base_sha=commit)
    assert review["attempt_id"]


def test_identical_reports_in_flight_share_one_normalization(tmp_path, monkeypatch):
    repo, commits = make_repo(tmp_path)
    bundle = broad_bundle(repo, tmp_path / "broad.bundle")
    log = slow_git(tmp_path, monkeypatch, seconds=3)
    outcomes = []

    def report():
        try:
            outcomes.append(retain_candidate(report_for(bundle), commits[-1]))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcomes.append(exc)

    threads = [threading.Thread(target=report) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(180)

    assert len(outcomes) == 3 and all(isinstance(o, dict) for o in outcomes), outcomes
    assert all(o == outcomes[0] for o in outcomes)
    assert log.read_text().splitlines() == ["pack-objects"]


def test_a_git_step_that_outlives_its_budget_is_ended_says_so_and_is_not_remembered(
        tmp_path, monkeypatch):
    repo, commits = make_repo(tmp_path)
    bundle = broad_bundle(repo, tmp_path / "broad.bundle")
    log = slow_git(tmp_path, monkeypatch, hang=True)
    monkeypatch.setenv("AITELIER_STATE_GIT_TIMEOUT_SEC", "5")

    started = time.monotonic()
    with pytest.raises(StateConflict) as refusal:
        retain_candidate(report_for(bundle), commits[-1])
    assert time.monotonic() - started < 120
    message = str(refusal.value)
    assert message.startswith("Git artifact normalization unavailable: TimeoutExpired")
    # The caller is told which budget ended it, that nothing was kept, and what to do.
    assert "5s" in message and "AITELIER_STATE_GIT_TIMEOUT_SEC" in message
    assert "nothing was retained" in message and "resend the identical report" in message

    # The refusal is about that run, not the candidate: the next report is judged afresh.
    monkeypatch.setenv("SLOW_GIT_HANG", "0")
    monkeypatch.delenv("AITELIER_STATE_GIT_TIMEOUT_SEC")
    artifact = retain_candidate(report_for(bundle), commits[-1])
    assert artifact["commit_sha"] == commits[-1]
    assert log.read_text().splitlines() == ["pack-objects", "pack-objects"]


def test_guide_tells_callers_how_long_a_candidate_report_may_take_and_what_a_timeout_means():
    from core.state_git_artifacts import GIT_TIMEOUT_ENV, GIT_TIMEOUT_SEC
    line = next(line for line in STATE_DRIVER_GUIDE.splitlines()
                if line.startswith("- A git-sha1 candidate report is answered only after"))
    # The numbers callers plan around are the ones the code uses.
    assert f"{GIT_TIMEOUT_SEC // 60} minutes" in line and GIT_TIMEOUT_ENV in line
    assert "does not cancel" in line and "idempotent=true" in line
    assert "joins the normalization in flight" in line
    # Within the measured cost at the size bound, with room for a busy host.
    assert GIT_TIMEOUT_SEC >= 3 * MEASURED["pack-objects"] * BOUND_MIB / MEASURED_MIB
