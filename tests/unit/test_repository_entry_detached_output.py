"""Real Linux pipe/process regressions; source-only author has NOT run these.

Run only in the root-owned disposable isolated image. Each outer worker calls
an exact entry file through an import loader, not a substituted Popen. A finite
holder and fixture-specific PID/start-time cleanup also bound baseline failures.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


ENTRY = Path(os.environ.get(
    "AITELIER_ENTRY_UNDER_TEST",
    str(Path(__file__).resolve().parents[2] / "core/repository_executor_entry.py"),
)).resolve()
BUDGET = 1.5
WORKER_BOUND = 4.5

WORKER = r'''
import importlib.util, json, signal, sys, time
if sys.argv[3] == "ignore":
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
spec = importlib.util.spec_from_file_location("exact_executor_entry", sys.argv[1])
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
started = time.monotonic()
result = entry.run(json.loads(sys.argv[2]))
print(json.dumps({"elapsed": time.monotonic() - started, "result": result}))
'''

HOLDER = r'''
import json, os, sys, time
from pathlib import Path
# Closing these readers in the entry must never require this process to exit.
os.write(1, b"HOLDER_STDOUT_REACHED\n")
os.write(2, b"HOLDER_STDERR_REACHED\n")
record = {"pid": os.getpid(), "starttime": Path("/proc/self/stat").read_text().split(")", 1)[1].split()[19]}
Path(sys.argv[1]).write_text(json.dumps(record))
time.sleep(20)  # fail-safe only; fixture cleanup kills exactly this recorded owner
'''


def _starttime(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _cleanup_owned_process(record_path):
    if not record_path.exists():
        return
    record = json.loads(record_path.read_text())
    pid = record["pid"]
    if _starttime(pid) == record["starttime"]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while _starttime(pid) == record["starttime"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert _starttime(pid) != record["starttime"], "fixture process was not reaped by capsule --init"


def _payload(repo, timeout=BUDGET, profile=None):
    payload = {"repo": str(repo), "timeout": timeout,
               "args": [sys.executable, "-m", "pytest", "-q", "-s", "test_sample.py"],
               "pytest_timeout": True}
    if profile is not None:
        payload["profile"] = profile
        payload.pop("pytest_timeout")
    return payload


def _run_worker(payload, *, ignore_sigchld=False):
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", WORKER, str(ENTRY), json.dumps(payload),
         "ignore" if ignore_sigchld else "default"],
        capture_output=True, text=True, timeout=WORKER_BOUND,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    report = json.loads(completed.stdout)
    assert elapsed < WORKER_BOUND
    return report


def _detached_test(repo, *, blocked):
    record = repo / "holder.json"
    (repo / "holder.py").write_text(HOLDER)
    body = '''import json, os, subprocess, sys, time
from pathlib import Path

def test_detached_writer():
    Path("main.json").write_text(json.dumps({"pid": os.getpid(), "starttime": Path("/proc/self/stat").read_text().split(")", 1)[1].split()[19]}))
    subprocess.Popen([sys.executable, "holder.py", "holder.json"], start_new_session=True)
    deadline = time.monotonic() + 1
    while not Path("holder.json").exists() and time.monotonic() < deadline:
        time.sleep(.005)
    assert Path("holder.json").exists(), "holder never reached both pipe markers"
    os.write(1, b"MAIN_STDOUT_REACHED\\n")
    os.write(2, b"MAIN_STDERR_REACHED\\n")
'''
    if blocked:
        body += "    time.sleep(20)\n"
    (repo / "test_sample.py").write_text(body)
    return record, repo / "main.json"


@pytest.mark.parametrize("blocked,ignore_sigchld", [(False, False), (False, True), (True, False)])
def test_detached_output_cannot_erase_or_outlive_entry_deadline(tmp_path, blocked, ignore_sigchld):
    """Exited main, genuinely absent group, and still-blocked main are distinct."""
    record, main_record = _detached_test(tmp_path, blocked=blocked)
    try:
        report = _run_worker(_payload(tmp_path), ignore_sigchld=ignore_sigchld)
        result = report["result"]
        assert record.exists(), "counterexample never started its real detached holder"
        assert result["timed_out"] is True
        assert type(result["returncode"]) is int
        assert "HOLDER_STDOUT_REACHED" in result["stdout"]
        assert "MAIN_STDOUT_REACHED" in result["stdout"]
        assert "HOLDER_STDERR_REACHED" in result["stderr"]
        assert "MAIN_STDERR_REACHED" in result["stderr"]
        assert result["stdout"].count("HOLDER_STDOUT_REACHED") == 1
        assert result["stderr"].count("HOLDER_STDERR_REACHED") == 1
        # The holder is genuinely still alive: EOF was NOT obtained by a fake
        # process or a kill outside the owned process group.
        owner = json.loads(record.read_text())
        assert _starttime(owner["pid"]) == owner["starttime"]
        assert BUDGET <= report["elapsed"] < BUDGET + 2.5
        assert result["timeout_cleanup"]["output_complete"] is False
        assert result["timeout_cleanup"]["descendants_settled"] is None
        assert result["timeout_cleanup"]["direct_child_reaped"] is True
    finally:
        _cleanup_owned_process(main_record)
        _cleanup_owned_process(record)


@pytest.mark.parametrize("red", [False, True])
def test_ordinary_pytest_green_and_assertion_red_remain_measured(tmp_path, red):
    (tmp_path / "test_sample.py").write_text(
        "import os\ndef test_actual():\n"
        "    os.write(1, b'ORDINARY_STDOUT\\n')\n"
        "    os.write(2, b'ORDINARY_STDERR\\n')\n"
        + ("    assert False, 'ACTUAL_ASSERTION_RED'\n" if red else "    assert 2 + 2 == 4\n"))
    report = _run_worker(_payload(tmp_path, timeout=3))
    result = report["result"]
    assert result["timed_out"] is False
    assert result["returncode"] == (1 if red else 0)
    assert "ORDINARY_STDOUT" in result["stdout"]
    assert "ORDINARY_STDERR" in result["stderr"]
    assert "timeout_cleanup" not in result
    assert result["execution_identity"]["schema"] == "pytest-execution/1"
    if red:
        assert "ACTUAL_ASSERTION_RED" in result["stdout"]


def test_coding_shell_retains_existing_output_and_success_semantics(tmp_path):
    payload = {"repo": str(tmp_path), "timeout": 1,
               "profile": "coding_shell",
               "args": [sys.executable, "-c", "import os; os.write(1,b'SHELL_OUT'); os.write(2,b'SHELL_ERR')"]}
    result = _run_worker(payload)["result"]
    assert result["returncode"] == 0 and result["timed_out"] is False
    assert result["stdout"] == "SHELL_OUT" and result["stderr"] == "SHELL_ERR"
    assert result["output_truncated"] is False
    assert "timeout_cleanup" not in result
