"""Trusted Docker transport; repository commands never fall back to the host."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

class IsolationUnavailable(RuntimeError):
    pass


@contextmanager
def _slot():
    from core import datadir
    root = datadir.aitelier_home() / "cpu-test-slots"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(4):
        stream = (root / str(number)).open("a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            continue
        try:
            yield
        finally:
            stream.close()
        return
    raise IsolationUnavailable("all four CPU test execution slots are occupied")


def execute(*args, **kwargs):
    with _slot():
        return _execute(*args, **kwargs)


def _execute(repo: Path, args: list[str], timeout: int, *,
            writable_dirs=(), relay_socket: str = "", import_module: str = "",
            pytest_timeout: bool = False, report_dir: str = "") -> subprocess.CompletedProcess:
    docker = shutil.which("docker")
    if not docker:
        raise IsolationUnavailable("Docker CPU execution facility is unavailable; local execution refused")
    repo = Path(repo).resolve()
    name = "aitelier-cpu-" + uuid.uuid4().hex
    entry = Path(__file__).with_name("repository_executor_entry.py")
    payload = {"args": args, "timeout": timeout, "repo": str(repo),
               "relay_socket": relay_socket, "import_module": import_module,
               "pytest_timeout": pytest_timeout, "report_dir": report_dir}
    command = [docker, "run", "--rm", "--init", "--network", "none",
               "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}", "--name", name,
               "-v", f"{repo}:{repo}:ro", "-v", f"{entry}:/executor.py:ro",
               "-w", str(repo)]
    for directory in writable_dirs:
        path = str(Path(directory).resolve())
        command += ["-v", f"{path}:{path}:rw"]
    if relay_socket:
        command += ["-v", f"{relay_socket}:{relay_socket}:ro"]
    command += ["--entrypoint", "python3", os.environ.get("AITELIER_TEST_IMAGE", "aitelier:latest"),
                "/executor.py", json.dumps(payload)]
    proc = None
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        stdout, stderr = proc.communicate(timeout=timeout + 15)
        if proc.returncode != 0:
            raise IsolationUnavailable(f"isolated container exited {proc.returncode}: {stderr[-2000:]}")
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise IsolationUnavailable("isolated executor returned no result") from exc
        if result.get("timed_out"):
            raise subprocess.TimeoutExpired(args, timeout, result.get("stdout"), result.get("stderr"))
        completed = subprocess.CompletedProcess(args, result["returncode"],
                                                result["stdout"], result["stderr"])
        completed.import_error = result.get("import_error", "")
        return completed
    finally:
        # UUID names are created by this invocation only. Never prune or kill peers.
        try:
            subprocess.run([docker, "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
