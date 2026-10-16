"""Benign stdout/stderr collector fixtures; no sandbox or process-FD probes."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys

import pytest
from core import repository_executor as executor, repository_host_launcher as host


@pytest.fixture
def collect(tmp_path, monkeypatch):
    state = {"quiet": False, "retained": []}
    original = bytearray
    def tracked_buffer():
        value = original()
        state["retained"].append(value)
        return value
    monkeypatch.setattr(host, "bytearray", tracked_buffer, raising=False)
    monkeypatch.setattr(host, "docker_command", lambda *args: ["benign-wire-fixture"])
    @contextmanager
    def owned(*args, **kwargs):
        proc = subprocess.Popen([sys.executable, "-c",
            "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes());"
            "sys.stdout.flush();sys.stderr.buffer.write(pathlib.Path(sys.argv[2]).read_bytes())",
            str(tmp_path / "out"), str(tmp_path / "err")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close(); proc.stderr.close()
            state["quiet"] = True
    monkeypatch.setattr(executor, "owned_container", owned)
    def run(stdout, stderr=b"", profile="coding_shell"):
        (tmp_path / "out").write_bytes(stdout)
        (tmp_path / "err").write_bytes(stderr)
        return host._run_docker({}, {"profile": profile, "timeout": 5})
    return run, state


def wire(stdout, stderr="", **extra):
    return json.dumps({"returncode": 7, "stdout": stdout, "stderr": stderr,
                       "timed_out": True, "output_truncated": True, **extra}).encode()


@pytest.mark.parametrize("stdout,stderr", [(b"x" * 160000, b""), (b"", b"x" * 160000),
                                          (b"x" * 50000, b"y" * 50000)])
def test_oversized_wire_is_refused_before_unbounded_retention_and_settles(collect, stdout, stderr):
    run, state = collect
    with pytest.raises(host.LaunchRefused, match="wire output limit"):
        run(stdout, stderr)
    assert state["quiet"]
    assert sum(map(len, state["retained"])) <= host.CODING_SHELL_WIRE_LIMIT


@pytest.mark.parametrize("stdout,stderr", [("a" * 12001, ""), ("a" * 6000, "b" * 6001),
                                          ("€" * 4001, "")])
def test_decoded_combined_utf8_limit_refuses_under_wire_limit(collect, stdout, stderr):
    run, state = collect
    data = wire(stdout, stderr)
    assert len(data) < host.CODING_SHELL_WIRE_LIMIT
    with pytest.raises(host.LaunchRefused, match="decoded output limit"):
        run(data)
    assert state["quiet"]


@pytest.mark.parametrize("stdout,stderr", [("a" * 6000, "b" * 6000), ("\x01" * 12000, ""),
                                          ("€" * 4000, ""), ("😀" * 3000, "")])
def test_exact_byte_boundary_and_json_expansion_preserve_bare_status(collect, stdout, stderr):
    run, state = collect
    expected = json.loads(wire(stdout, stderr))
    assert run(wire(stdout, stderr)) == expected
    assert state["quiet"] and expected["returncode"] == 7 and expected["timed_out"]
    assert expected["output_truncated"]


def test_invalid_unicode_result_refuses_honestly(collect):
    run, state = collect
    with pytest.raises(host.LaunchRefused, match="invalid UTF-8 output"):
        run(wire("\ud800"))
    assert state["quiet"]


def test_existing_readonly_result_semantics_are_not_restricted(collect):
    run, state = collect
    expected = wire("x" * 90000)
    assert run(expected, profile="") == json.loads(expected)
    assert state["quiet"]


def test_actual_owned_source_identity():
    assert Path(host.__file__).resolve() == Path("/src/core/repository_host_launcher.py")
