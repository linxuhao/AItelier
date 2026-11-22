"""The peak recorder answers "who held the memory at the peak", not just "it rose".

The backend-rss-1114 report could say the aitelier container grows in an
allocator-shaped way, but its probe was idle and fresh-boot, so it could not
name the holder.  `scripts/mem_peak_watch.py` is the instrument that closes that
gap: at every new ``memory.peak`` it writes ONE JSONL line with the cgroup
decomposition and the ``RssAnon`` of EVERY pid in ``cgroup.procs``, so the
holder is a field in the record rather than an inference from a timeline.

These tests drive the recorder itself — no mock of the recorder is allowed,
because a mock would prove nothing about the real cgroup read.  They run in a
disposable container where ``/sys/fs/cgroup`` IS the test container's own
cgroup, so the "container" under test is a real, isolated one:

* a child process allocates and holds ~200 MB; the recorder is pointed at the
  container's own cgroup and must name that child (by the marker in its argv)
  with the anon RSS it holds;
* the JSONL stays one JSON object per line;
* with ``--cgroup`` the module must not shell out to docker at all — proven by
  a fake `docker` on PATH that would leave a stamp, and by the module's own
  source (only the identity lookup may reach a subprocess);
* a synthetic cgroup pins the "exactly one line per NEW peak" rule and the
  "a vanished cgroup is recorded, then the recorder stops" guard.

The 10-minute live measurement against the production container is deliberately
NOT a test: it belongs to the issue's evidence, not to a test run.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "mem_peak_watch.py"
MIB = 1024 * 1024


def _load_recorder():
    """Import the recorder by path so the test can call ``watch`` in-process."""
    spec = importlib.util.spec_from_file_location("mem_peak_watch_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECORDER = _load_recorder()


def _container_cgroup() -> Path:
    """The cgroup of the container this test runs in.

    Inside a container on cgroup v2 with a private cgroup namespace,
    ``/sys/fs/cgroup`` is this container's own cgroup and carries
    ``memory.peak``.  If the runtime did not give us a namespace, the path in
    ``/proc/self/cgroup`` still names the cgroup relative to the mount.  The
    file must exist: a silent skip would leave the recorder unproven, so this
    asserts loudly with every candidate it tried.
    """
    candidates = []
    rel = Path((Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]))
    candidates.append(Path("/sys/fs/cgroup"))
    if str(rel) not in ("", "/"):
        candidates.append(Path("/sys/fs/cgroup") / rel.relative_to("/"))
    for candidate in candidates:
        if (candidate / "memory.peak").is_file():
            return candidate
    raise AssertionError(
        "no memory.peak under any candidate cgroup path "
        f"{[str(c) for c in candidates]}; /proc/self/cgroup says "
        f"{Path('/proc/self/cgroup').read_text().strip()!r}"
    )


def _rss_anon_kb(pid: int) -> int:
    """Read one process's RssAnon straight from /proc (no helper library)."""
    text = Path(f"/proc/{pid}/status").read_text()
    for line in text.splitlines():
        if line.startswith("RssAnon:"):
            return int(line.split()[1])
    raise AssertionError(f"no RssAnon in /proc/{pid}/status")


def _start_holder(tmp_path: Path, marker: str, mb: int = 200) -> subprocess.Popen:
    """Spawn a child that allocates and holds ``mb`` MiB of anonymous memory.

    The marker is the FIRST argument after the script path, so it survives the
    recorder's 200-character cmdline truncation.  The child keeps holding the
    buffer until its stdin is closed, which is what makes the "holder" real:
    the pages are resident at the moment the recorder samples.
    """
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import sys, time\n"
        "mb = int(sys.argv[2])\n"
        "buf = bytearray(mb * 1024 * 1024)\n"
        "for off in range(0, len(buf), 4096):\n"
        "    buf[off] = 1\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(holder), marker, str(mb)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30.0
    target = mb * 1024 * 3 // 4  # 3/4 of the request resident is unmistakable
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"holder exited early with {proc.returncode}")
        try:
            if _rss_anon_kb(proc.pid) >= target:
                return proc
        except (OSError, AssertionError):
            pass
        time.sleep(0.05)
    proc.kill()
    raise AssertionError(f"holder {proc.pid} never held {target} kB of anon RSS")


def _run_recorder(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _records(path: Path) -> list[dict]:
    """Every line must be exactly one JSON object; return them parsed."""
    lines = [line for line in path.read_text().splitlines() if line]
    records = []
    for number, line in enumerate(lines, start=1):
        assert line.count("\n") == 0
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"line {number} is not one JSON object: {line!r}") from exc
        assert isinstance(record, dict), f"line {number} is not a JSON object"
        records.append(record)
    return records


@pytest.fixture
def cgroup() -> Path:
    """The cgroup this test container really lives in."""
    return _container_cgroup()


def test_one_peak_record_names_the_process_that_holds_the_memory(tmp_path, cgroup):
    marker = f"mempeak-holder-{uuid.uuid4().hex[:12]}"
    holder = _start_holder(tmp_path, marker, mb=200)
    out = tmp_path / "peaks.jsonl"
    try:
        done = _run_recorder("--cgroup", str(cgroup), "--once", "--out", str(out))
        assert done.returncode == 0, done.stderr
        assert out.is_file(), done.stderr

        records = _records(out)
        assert records, "the recorder wrote no peak record"
        record = records[0]

        # Identity: the record says which cgroup and which container it is about.
        assert record["cgroup"] == str(cgroup)
        assert record["interval_s"] == 1.0
        assert record["utc"].endswith("Z") and "T" in record["utc"]

        # The peak itself, in bytes, MiB, and as a share of memory.max. The limit
        # is whatever this container was given (--memory 2g, 3g, ...), read from
        # the same cgroup file the recorder reads; "max" means no limit.
        peak = record["memory_peak_b"]
        assert isinstance(peak, int) and peak > 0
        assert record["memory_peak_mib"] == round(peak / MIB, 3)
        limit = (cgroup / "memory.max").read_text().strip()
        if limit == "max":
            assert record["memory_max_b"] is None
        else:
            assert record["memory_max_b"] == int(limit)
            assert 0 < record["memory_peak_share_of_max"] <= 1

        # The decomposition: the peak was made partly of anonymous memory, and
        # the record carried the fault counters and the event counters too.
        decomposition = record["decomposition_b"]
        assert decomposition["anon"] > 0
        assert "file" in decomposition and "kernel" in decomposition
        assert "slab" in decomposition and "kernel_stack" in decomposition
        assert isinstance(record["pgfault"], int) and record["pgfault"] > 0
        assert isinstance(record["pgmajfault"], int)
        assert "oom_kill" in record["memory_events"]

        # The holder: every pid in cgroup.procs is listed, sorted by anon RSS,
        # and the child named by its cmdline marker carries the ~200 MB.
        processes = record["processes"]
        assert record["process_count"] == len(processes)
        assert processes, "no processes in the record"
        listed = [proc["rss_anon_kb"] for proc in processes]
        assert listed == sorted(listed, key=lambda kb: (kb is None, -(kb or 0)))
        for proc in processes:
            assert {"pid", "comm", "cmdline", "rss_anon_kb", "vm_hwm_kb"} <= set(proc)
            assert len(proc["cmdline"]) <= 200

        holders = [proc for proc in processes if marker in proc["cmdline"]]
        assert holders, (
            f"no process named {marker!r} in {[p['cmdline'] for p in processes]}"
        )
        owner = holders[0]
        assert owner["pid"] == holder.pid
        assert owner["comm"].startswith("python")
        assert owner["rss_anon_kb"] >= 150_000, (
            f"holder {owner['pid']} held only {owner['rss_anon_kb']} kB anon"
        )
        assert owner["vm_hwm_kb"] >= owner["rss_anon_kb"]
        assert record["processes"][0]["rss_anon_kb"] >= owner["rss_anon_kb"]
        assert record["rss_anon_total_kb"] >= owner["rss_anon_kb"]
    finally:
        holder.stdin.close()
        holder.kill()
        holder.wait(timeout=30)


def test_with_an_explicit_cgroup_the_recorder_never_shells_out(tmp_path, cgroup):
    """`--cgroup` is the whole point: read-only, no docker, no exec anywhere."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    stamp = tmp_path / "docker-was-called"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(f"#!/bin/sh\necho called >> {stamp}\nexit 1\n")
    fake_docker.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    out = tmp_path / "explicit.jsonl"
    done = _run_recorder(
        "--cgroup", str(cgroup), "--once", "--out", str(out), env=env
    )
    assert done.returncode == 0, done.stderr
    assert not stamp.exists(), f"docker was invoked with an explicit --cgroup: {done.stderr}"
    assert len(_records(out)) == 1

    # And statically: the only subprocess the module can start is the identity
    # lookup inside docker_container_id.  Nothing here can exec into the
    # container, attach to it, or restart it.
    tree = ast.parse(SCRIPT.read_text())
    calls: list[tuple[str, ast.Call]] = []

    def walk(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call):
                target = child.func
                dotted = ""
                while isinstance(target, ast.Attribute):
                    dotted = "." + target.attr + dotted
                    target = target.value
                if isinstance(target, ast.Name):
                    dotted = target.id + dotted
                calls.append((name, child))
            walk(child, name)

    walk(tree, "<module>")
    exec_calls = [
        (function, call)
        for function, call in calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in {"subprocess", "os"}
        and call.func.attr
        in {
            "run",
            "call",
            "check_call",
            "check_output",
            "Popen",
            "system",
            "popen",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "spawnv",
            "spawnl",
        }
    ]
    assert len(exec_calls) == 1, (
        "expected exactly one process-spawning call (the identity lookup), got "
        f"{[(function, getattr(call.func, 'attr', '?')) for function, call in exec_calls]}"
    )
    function, call = exec_calls[0]
    assert function == "docker_container_id", (
        f"process-spawning call outside the identity lookup, in {function}()"
    )
    assert call.func.attr == "run"
    argv = call.args[0]
    assert isinstance(argv, ast.List)
    words = [elt.value for elt in argv.elts if isinstance(elt, ast.Constant)]
    assert words == ["docker", "inspect", "-f", "{{.Id}}"], words
    assert len(argv.elts) == 5, "the identity lookup takes the container name"
    assert "shell=True" not in SCRIPT.read_text()


def _synthetic_cgroup(root: Path, peak: int) -> Path:
    """A small cgroup-shaped directory whose peak the test can move at will."""
    root.mkdir(parents=True)
    (root / "cgroup.procs").write_text(f"{os.getpid()}\n")
    (root / "memory.current").write_text(str(peak))
    (root / "memory.max").write_text("max")
    (root / "memory.stat").write_text(
        "anon 1048576\n"
        "file 4096\n"
        "kernel 8192\n"
        "kernel_stack 4096\n"
        "slab 2048\n"
        "pgfault 777\n"
        "pgmajfault 3\n"
    )
    (root / "memory.events").write_text(
        "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"
    )
    _set_peak(root, peak)
    return root


def _set_peak(root: Path, peak: int) -> None:
    """Move ``memory.peak`` atomically so a sample never reads half a number."""
    temporary = root / ".memory.peak.tmp"
    temporary.write_text(str(peak))
    os.replace(temporary, root / "memory.peak")


def test_a_new_peak_writes_exactly_one_line_and_the_same_peak_writes_none(tmp_path):
    """The recorder writes on the CHANGE of memory.peak, not on every sample.

    The moves are sequenced off the records already on disk (not off wall-clock
    sleeps), so the test cannot race a slow sample: the first record must be the
    synthetic baseline before the test moves the peak.  The same value written
    twice must add no line.
    """
    cgroup = _synthetic_cgroup(tmp_path / "cg", 111_111)
    out = tmp_path / "dedupe.jsonl"
    deadline = time.monotonic() + 15.0

    def record_count() -> int:
        if not out.exists():
            return 0
        return len([line for line in out.read_text().splitlines() if line])

    def wait_for(count: int) -> None:
        while time.monotonic() < deadline:
            if record_count() >= count:
                return
            time.sleep(0.01)
        raise AssertionError(f"waited 15 s for {count} records, have {record_count()}")

    failures: list[BaseException] = []

    def mover() -> None:
        try:
            wait_for(1)  # the baseline 111_111 record is on disk
            _set_peak(cgroup, 222_222)
            wait_for(2)
            _set_peak(cgroup, 222_222)  # identical value: must add no line
            time.sleep(0.2)  # several sample periods at interval=0.02
            _set_peak(cgroup, 333_333)
            wait_for(3)
        except BaseException as exc:  # re-raised on the test thread below
            failures.append(exc)

    mover_thread = threading.Thread(target=mover)
    mover_thread.start()
    try:
        code, samples, records = RECORDER.watch(
            cgroup, out, interval=0.02, duration=2.0
        )
    finally:
        mover_thread.join()
    if failures:
        raise failures[0]
    assert code == 0
    assert samples > 2
    assert records == 3
    peaks = [record["memory_peak_b"] for record in _records(out)]
    assert peaks == [111_111, 222_222, 333_333], peaks
    assert len(set(peaks)) == len(peaks)
    for record in _records(out):
        assert record["memory_peak_mib"] == round(record["memory_peak_b"] / MIB, 3)


def test_a_vanished_cgroup_is_recorded_and_stops_the_recorder(tmp_path, capsys):
    cgroup = _synthetic_cgroup(tmp_path / "cg", 55_555)
    out = tmp_path / "gone.jsonl"
    remover = threading.Timer(0.15, shutil.rmtree, args=(cgroup,))
    remover.start()
    try:
        code, samples, records = RECORDER.watch(
            cgroup, out, interval=0.05, duration=5.0
        )
    finally:
        remover.join()
    assert code == RECORDER.EXIT_CGROUP_GONE, "a vanished cgroup must exit non-zero"
    assert samples >= 1 and records >= 1
    last = _records(out)[-1]
    assert last["event"] == "cgroup_gone"
    assert last["cgroup"] == str(cgroup)
    assert "disappeared" in capsys.readouterr().err
