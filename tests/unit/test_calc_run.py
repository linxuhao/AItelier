"""Step 24.4b (design-steward 7.8.24 "十" 2, 6): the calculate sandbox image's fixed wrapper
(docker/calc/run.py) and its pins.

The wrapper runs here on this interpreter with its directories pointed at a temporary tree:
the child's stdout head and stderr tail, the timeout that kills the child's whole process group,
which entries of the output directory come back (regular files only, at most four, at most the
file bound) and the one frame. The image's pins: the base by digest, every library by version and
wheel hash, pip in --require-hashes mode, the wrapper as the entrypoint.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "docker" / "calc"


def _wrapper():
    spec = importlib.util.spec_from_file_location("calc_run_under_test", CALC / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run():
    return _wrapper()


def _program(tmp_path: Path, text: str) -> str:
    main = tmp_path / "in" / "main.py"
    main.parent.mkdir(exist_ok=True)
    main.write_text(text)
    (tmp_path / "out").mkdir(exist_ok=True)
    return str(main)


def test_the_bounds_are_the_designs(run) -> None:
    assert (run.STDOUT_BYTES, run.STDERR_BYTES, run.CHILD_SECONDS, run.MAX_FILES, run.FILE_BYTES) == (
        16 * 1024, 2 * 1024, 25, 4, 10 * 1024 * 1024)
    assert (run.IN_DIR, run.OUT_DIR) == ("/job/in", "/job/out")


def test_stdout_keeps_its_head_and_stderr_its_tail(run, tmp_path: Path) -> None:
    main = _program(tmp_path, "import sys\nsys.stdout.write('a' * 100 + 'b' * 50000)\n"
                              "sys.stderr.write('x' * 9000 + 'LAST')\nsys.exit(3)\n")
    code, timed_out, stdout, truncated, stderr = run.run_child(main, str(tmp_path / "out"), seconds=20)
    assert (code, timed_out, truncated) == (3, False, True)
    assert len(stdout) == 16 * 1024 and stdout.startswith(b"a" * 100)
    assert len(stderr) == 2048 and stderr.endswith(b"LAST")


def test_the_child_runs_isolated_in_the_output_directory(run, tmp_path: Path) -> None:
    main = _program(tmp_path, "import os, sys\nprint(os.getcwd())\nprint(sys.flags.isolated, sys.flags.dont_write_bytecode)\n")
    code, _, stdout, truncated, _ = run.run_child(main, str(tmp_path / "out"), seconds=20)
    lines = stdout.decode().split("\n")
    assert code == 0 and not truncated
    assert os.path.realpath(lines[0]) == os.path.realpath(tmp_path / "out")
    assert lines[1] == "1 1"


def test_a_program_past_its_time_is_killed_with_its_children(run, tmp_path: Path) -> None:
    main = _program(tmp_path, "import subprocess, sys, time\n"
                              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                              "print('started', flush=True)\ntime.sleep(60)\n")
    started = time.monotonic()
    code, timed_out, stdout, _, _ = run.run_child(main, str(tmp_path / "out"), seconds=1)
    assert timed_out is True and code == -9
    assert stdout == b"started\n"
    assert time.monotonic() - started < 15, "a grandchild holding the pipe did not keep the wrapper waiting"


def test_only_regular_files_come_back_at_most_four_within_the_bound(run, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(10))
    (out / "b.csv").write_text("x,y\n")
    (out / "c.big").write_bytes(bytes(2048))
    (out / "d.txt").write_text("d")
    (out / "e.txt").write_text("e")
    (out / "f.txt").write_text("f")
    os.symlink("/etc/passwd", out / "g-link.png")
    os.mkfifo(out / "h-fifo.png")
    (out / "i-dir").mkdir()
    files, skipped = run.collect(str(out), max_files=4, file_bytes=1024)
    assert [name for name, _ in files] == ["a.png", "b.csv", "d.txt", "e.txt"]
    # c.big over the bound, f.txt the fifth, the link, the FIFO and the directory.
    assert skipped == 5
    assert files[0][1].startswith(b"\x89PNG")


def test_the_frame_is_one_json_object_with_base64_files(run) -> None:
    text = run.frame(0, False, "数".encode() + b"\xff", False, b"warn", [("a.png", b"\x89PNG")], 2)
    frame = json.loads(text)
    assert set(frame) == {"v", "exit_code", "timed_out", "stdout", "stdout_truncated", "stderr_tail", "files", "skipped"}
    assert frame["stdout"] == "数�" and frame["stderr_tail"] == "warn" and frame["skipped"] == 2
    assert base64.b64decode(frame["files"][0]["data"]) == b"\x89PNG"
    assert "\n" not in text


def test_main_end_to_end(run, tmp_path: Path, capsys, monkeypatch) -> None:
    _program(tmp_path, "open('chart.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n')\nprint(2**10)\n")
    monkeypatch.setattr(run, "IN_DIR", str(tmp_path / "in"))
    monkeypatch.setattr(run, "OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(run, "MPL_CACHE", str(tmp_path / "no-cache"))
    assert run.main() == 0
    frame = json.loads(capsys.readouterr().out)
    assert frame["exit_code"] == 0 and frame["stdout"] == "1024\n" and frame["timed_out"] is False
    assert [f["name"] for f in frame["files"]] == ["chart.png"]


def test_the_image_is_pinned_base_by_digest_libraries_by_hash() -> None:
    dockerfile = (CALC / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "--require-hashes" in dockerfile and "--only-binary=:all:" in dockerfile
    assert 'ENTRYPOINT ["python3", "-I", "-B", "/opt/calc/run.py"]' in dockerfile
    assert "USER 65534:65534" in dockerfile
    assert not re.search(r"\b(curl|wget|apt-get|apk)\b", dockerfile), "no network tool is installed"
    requirements = (CALC / "requirements.txt").read_text()
    entries = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)((?:\s*\\\s*--hash=sha256:[0-9a-f]{64})+)", requirements, re.M)
    names = {name.lower() for name, _, _ in entries}
    assert {"numpy", "sympy", "pandas", "matplotlib"} <= names
    # Their runtime dependencies pinned too (pip --require-hashes refuses an unpinned one).
    assert {"mpmath", "python-dateutil", "six", "contourpy", "cycler", "fonttools", "kiwisolver", "packaging", "pillow", "pyparsing"} <= names
    requirement_lines = [line for line in requirements.splitlines() if line and not line.startswith(("#", " "))]
    assert len(requirement_lines) == len(entries), "every requirement is pinned with a hash"
