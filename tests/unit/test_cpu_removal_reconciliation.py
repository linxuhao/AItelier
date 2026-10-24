"""CLI lifecycle regressions: real subprocesses, no Docker daemon or host jobs.

The executable fixture emits Docker's distinct rm-conflict, inspect-404,
transport failure and still-present replies. It never runs repository code.
"""
import json
import sys
import time

import pytest

from core import repository_executor as executor


CID = "4d432b18ca8eef3b961e4614d71f05ab92a660ce3686abd26a0413c44f93dbde"
NAME = "aitelier-cpu-owned-fixture"
FAKE_CLI = r'''
import json, os, sqlite3, sys, time
from pathlib import Path
state_path = Path(os.environ["CPU_LIFECYCLE_FIXTURE_STATE"])
s = json.loads(state_path.read_text())
args = sys.argv[1:]
s["calls"].append(args)
state_path.write_text(json.dumps(s))
cid = s["cid"]
if args[:1] == ["create"]:
    if s["scenario"] == "create_refused":
        print("Error response from daemon: Conflict. The container name is already in use", file=sys.stderr)
        raise SystemExit(1)
    print(cid)
    raise SystemExit(0)
if args == ["start", "-a", cid]:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")
        connection.executemany("INSERT INTO sample VALUES (?)", [(19,), (23,)])
        total = connection.execute("SELECT SUM(value) FROM sample").fetchone()[0]
    try:
        assert total == (43 if s["scenario"] == "gone_red" else 42), "DELIBERATE_ASSERTION_RED"
        code, error = 0, ""
    except AssertionError as exc:
        code, error = 1, str(exc)
    print(json.dumps({"returncode": code, "stdout": "SQLite sum=" + str(total), "stderr": error, "timed_out": False}))
    raise SystemExit(0)
if args == ["rm", "-f", cid]:
    if s["scenario"] == "slow_rm":
        time.sleep(5)
    if s["scenario"] == "rm_success":
        print(cid)
        raise SystemExit(0)
    print("Error response from daemon: removal of container " + cid + " is already in progress", file=sys.stderr)
    raise SystemExit(1)
if args == ["container", "inspect", "--format", "{{.Id}}", cid]:
    if s["scenario"] in ("gone", "gone_red"):
        print("Error: No such container: " + cid, file=sys.stderr)
        raise SystemExit(1)
    if s["scenario"] == "wrong_404":
        print("Error: No such container: " + "b" * 64, file=sys.stderr)
        raise SystemExit(1)
    if s["scenario"] == "transport_error":
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        raise SystemExit(1)
    if s["scenario"] == "slow_inspect":
        time.sleep(5)
    print(cid)
    raise SystemExit(0)
raise SystemExit("unexpected fixture argv: " + repr(args))
'''


@pytest.fixture
def docker_cli(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    cli = tmp_path / "docker-fixture"
    cli.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
    cli.chmod(0o700)
    monkeypatch.setenv("CPU_LIFECYCLE_FIXTURE_STATE", str(state))
    # Real client timeouts remain real. Keep the test bounded without a clock mock.
    monkeypatch.setattr(executor, "REMOVE_DEADLINE", 0.6)

    def configure(scenario):
        state.write_text(json.dumps({"cid": CID, "scenario": scenario, "calls": []}))
        return str(cli), state

    return configure


def calls(state):
    return json.loads(state.read_text())["calls"]


@pytest.mark.parametrize("scenario,expected", [("gone", 0), ("gone_red", 1)])
def test_auto_removal_conflict_then_exact_absence_keeps_useful_result(docker_cli, scenario, expected):
    docker, state = docker_cli(scenario)
    with executor.owned_container([docker, "create"], docker, NAME, text=True) as proc:
        stdout, stderr = proc.communicate(timeout=2)
    result = json.loads(stdout)
    assert proc.returncode == 0 and not stderr
    assert result["returncode"] == expected and "42" in result["stdout"]
    assert ("DELIBERATE_ASSERTION_RED" in result["stderr"]) == bool(expected)
    observed = calls(state)
    assert observed[:2] == [["create"], ["start", "-a", CID]]
    assert observed[2:] == [["rm", "-f", CID], ["container", "inspect", "--format", "{{.Id}}", CID]]
    assert all(NAME not in argv for argv in observed)  # never remove by a recycled name


@pytest.mark.parametrize("scenario", ["still_present", "wrong_404", "transport_error"])
def test_nonabsence_does_not_pass_and_retains_first_daemon_error(docker_cli, scenario):
    docker, state = docker_cli(scenario)
    with pytest.raises(executor.CleanupUnconfirmed) as error:
        executor._remove_owned(docker, CID, NAME)
    assert error.value.name == NAME
    assert "already in progress" in str(error.value)
    assert CID in str(error.value)
    assert all(argv[-1] == CID for argv in calls(state))
    assert any(argv[:2] == ["container", "inspect"] for argv in calls(state))


@pytest.mark.parametrize("scenario", ["slow_rm", "slow_inspect"])
def test_cli_operations_share_one_cleanup_deadline(docker_cli, scenario):
    docker, state = docker_cli(scenario)
    started = time.monotonic()
    with pytest.raises(executor.CleanupUnconfirmed):
        executor._remove_owned(docker, CID, NAME)
    assert time.monotonic() - started < 2
    assert all(argv[-1] == CID for argv in calls(state))


def test_cleanup_failure_retains_preceding_execution_timeout(docker_cli):
    docker, state = docker_cli("still_present")
    with pytest.raises(executor.CleanupUnconfirmed) as error:
        with executor.owned_container([docker, "create"], docker, NAME, text=True) as proc:
            proc.communicate(timeout=2)
            raise RuntimeError("host launcher timed out waiting for the container")
    assert "already in progress" in str(error.value)
    assert "preceding execution error: RuntimeError: host launcher timed out waiting for the container" in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)


def test_failed_create_never_targets_an_existing_namesake(docker_cli):
    docker, state = docker_cli("create_refused")
    with pytest.raises(executor.IsolationUnavailable, match="owned container not created"):
        with executor.owned_container([docker, "create"], docker, NAME):
            pytest.fail("a refused create cannot start")
    assert calls(state) == [["create"]]


def test_rm_acknowledgement_does_not_require_an_extra_probe(docker_cli):
    docker, state = docker_cli("rm_success")
    executor._remove_owned(docker, CID, NAME)
    assert calls(state) == [["rm", "-f", CID]]
