"""Real subprocess inventory in an owned PATH; no host Docker or ps is called."""
import json
import subprocess
import sys

import pytest
from skillflow.core import SkillFlow

from core import deployment_quiescence as dq

DOCKER_ROW = "owned-cid\taitelier-resident\taitelier\tUp 2 minutes\n"
PROCESS_ROW = "810001 1 zvec-grep --serve\n"


def _owned_commands(tmp_path, monkeypatch, *, missing=None, failing=None, sleeping=None):
    bins = tmp_path / "owned-bin"
    bins.mkdir()
    calls = tmp_path / "command-calls.jsonl"
    for name, output in (("docker", DOCKER_ROW), ("ps", PROCESS_ROW)):
        if name == missing:
            continue
        script = (
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            f"with open({str(calls)!r}, 'a') as f:\n"
            f"    f.write(json.dumps({name!r}) + '\\n')\n"
            f"time.sleep({11 if name == sleeping else 0})\n"
            f"sys.stdout.write({output!r})\n"
            f"sys.stderr.write({'owned nonzero failure' if name == failing else ''!r})\n"
            f"sys.exit({23 if name == failing else 0})\n")
        executable = bins / name
        executable.write_text(script, encoding="utf-8")
        executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(bins))
    monkeypatch.setattr(dq, "PROC_ROOT", tmp_path / "owned-proc")
    return bins, calls


def _observed_commands(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


@pytest.mark.parametrize("missing", ["docker", "ps"])
def test_missing_inventory_binary_retains_other_observation(tmp_path, monkeypatch, missing):
    _, calls = _owned_commands(tmp_path, monkeypatch, missing=missing)
    try:
        owners, errors = dq.external_owners()  # actual default runner and subprocess
    except FileNotFoundError as exc:
        print(json.dumps({"missing": missing, "exception": repr(exc),
            "errno": exc.errno, "filename": exc.filename,
            "successful_command_calls": _observed_commands(calls)}))
        raise
    print(json.dumps({"missing": missing, "owners": owners, "errors": errors,
        "successful_command_calls": _observed_commands(calls)}))
    healthy = "ps" if missing == "docker" else "docker"
    assert _observed_commands(calls) == [healthy]
    assert len(owners) == 1
    assert owners[0]["kind"] == ("process" if healthy == "ps" else "docker")
    assert owners[0]["active"] is False
    prefix = "docker inventory failed:" if missing == "docker" else "process inventory failed:"
    assert len(errors) == 1 and errors[0].startswith(prefix)
    assert "FileNotFoundError" in errors[0] and missing in errors[0]


@pytest.mark.parametrize("missing", ["docker", "ps"])
def test_partial_default_inventory_refuses_owned_clearance(tmp_path, monkeypatch, missing):
    _, calls = _owned_commands(tmp_path, monkeypatch, missing=missing)
    sf = SkillFlow(str(tmp_path / "owned-skillflow.sqlite"))
    try:
        observed = dq.measure(skillflow=sf)  # actual default inventory, owned database
    finally:
        sf._conn.close()
    print(json.dumps({"missing": missing, "observation": observed}, sort_keys=True))
    assert len(observed["external_owners"]) == 1
    assert len(observed["errors"]) == 1
    assert observed["quiescent"] is False
    assert dq._validate_observation(observed) is None
    journal = tmp_path / "owned-journal.json"
    with pytest.raises(dq.DeploymentBlocked, match="not quiescent"):
        dq.authorize("restart", observed, journal=journal)
    data = json.loads(journal.read_text())
    assert data["latest"]["status"] == "aborted"
    assert not any(event["status"] == "authorized" for event in data["events"])
    assert len(_observed_commands(calls)) == 1


def test_healthy_default_inventory_allows_owned_quiet_clearance(tmp_path, monkeypatch):
    _, calls = _owned_commands(tmp_path, monkeypatch)
    sf = SkillFlow(str(tmp_path / "owned-skillflow.sqlite"))
    try:
        observed = dq.measure(skillflow=sf)
    finally:
        sf._conn.close()
    assert len(observed["external_owners"]) == 2
    assert observed["errors"] == [] and observed["quiescent"] is True
    assert _observed_commands(calls) == ["docker", "ps"]
    clearance = dq.authorize("restart", observed, journal=tmp_path / "owned-journal.json")
    assert clearance["event"]["status"] == "authorized"
    # Journal attestation only: no restart action or service mutation is executed.
    dq.finalize(clearance, success=False, error="owned test performs no deployment",
                journal=tmp_path / "owned-journal.json")


@pytest.mark.parametrize("failing", ["docker", "ps"])
def test_real_nonzero_inventory_retains_other_and_refuses_clearance(tmp_path, monkeypatch, failing):
    _, calls = _owned_commands(tmp_path, monkeypatch, failing=failing)
    sf = SkillFlow(str(tmp_path / "owned-skillflow.sqlite"))
    try:
        observed = dq.measure(skillflow=sf)
    finally:
        sf._conn.close()
    assert len(observed["external_owners"]) == 1
    assert _observed_commands(calls) == ["docker", "ps"]
    assert len(observed["errors"]) == 1
    assert "owned nonzero failure" in observed["errors"][0]
    assert observed["quiescent"] is False
    with pytest.raises(dq.DeploymentBlocked, match="not quiescent"):
        dq.authorize("restart", observed, journal=tmp_path / "owned-journal.json")


def test_real_timeout_still_escapes_and_cannot_produce_clearance(tmp_path, monkeypatch):
    _, calls = _owned_commands(tmp_path, monkeypatch, sleeping="docker")
    sf = SkillFlow(str(tmp_path / "owned-skillflow.sqlite"))
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            dq.measure(skillflow=sf)
    finally:
        sf._conn.close()
    assert exc.value.cmd[0] == "docker" and exc.value.timeout == 10
    assert _observed_commands(calls) == ["docker"]
    assert not (tmp_path / "owned-journal.json").exists()


def test_permission_error_is_not_swallowed(tmp_path, monkeypatch):
    bins, _ = _owned_commands(tmp_path, monkeypatch)
    (bins / "docker").chmod(0o600)
    with pytest.raises(PermissionError):
        dq.external_owners()
