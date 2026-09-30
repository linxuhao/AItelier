"""Real subprocess diagnostics, without the production daemon or storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from core import deployment_quiescence as dq
from core.semantic_index_control import IndexControl, index_project_once

OUTPUT = b"fixture stdout progress\n" + b"x" * 4096 + b"\nfixture stderr cause: missing fixture model\xff\n"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    (path / "code.py").write_text("VALUE = 1\n")
    git(path, "add", "code.py")
    git(path, "commit", "-qm", "fixture")
    return path


def provider(tmp_path, monkeypatch, mode):
    binary = tmp_path / "bin"; binary.mkdir()
    script = binary / "zg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "os.write(1, b'fixture stdout progress\\n' + b'x' * 4096 + b'\\n')\n"
        "os.write(2, b'fixture stderr cause: missing fixture model\\xff\\n')\n"
        + ("time.sleep(30)\n" if mode == "timeout" else
           "sys.exit(7)\n" if mode == "failure" else
           "Path(sys.argv[2], '.zvec-grep').mkdir(exist_ok=True)\n")
    )
    script.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])


def raw_from_error(error):
    assert len(error) <= 500
    assert "fixture stderr cause: missing fixture model" in error
    found = re.search(r"raw=(\S+) sha256=([0-9a-f]{64})", error)
    assert found, error
    raw = Path(found[1])
    assert raw.read_bytes() == OUTPUT
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == found[2]
    assert raw.stat().st_mode & 0o777 == 0o600
    return raw


@pytest.mark.parametrize("mode,exception", [
    ("failure", subprocess.CalledProcessError),
    ("timeout", subprocess.TimeoutExpired),
])
def test_real_project_failure_retains_diagnostics_and_blocks_cutover(tmp_path, monkeypatch, mode, exception):
    projects, runs, directory = (tmp_path / n for n in ("projects", "runs", "control"))
    directory.mkdir(mode=0o700)
    first, second = repo(projects / "a"), repo(projects / "b")
    provider(tmp_path, monkeypatch, mode)
    with pytest.raises(exception) as caught:
        index_project_once(directory, projects, runs, timeout=0.3 if mode == "timeout" else 3)
    marker = json.loads((directory / "project-owner.json").read_text())
    assert marker["status"] == "error" and marker["root"] == str(first)
    assert not (second / ".zvec-grep").exists()
    assert dq._semantic_worker_errors(directory)
    raw = raw_from_error(marker["error"])
    assert caught.value.output == OUTPUT
    with pytest.raises(exception):
        index_project_once(directory, projects, runs, timeout=0.3 if mode == "timeout" else 3)
    next_marker = json.loads((directory / "project-owner.json").read_text())
    assert next_marker["root"] == str(first) and next_marker["status"] == "error"
    assert raw_from_error(next_marker["error"]) != raw
    assert raw.read_bytes() == OUTPUT
    # A failed root remains the next owner; success requires a readiness check.
    calls = []
    def recovered(command, *, timeout):
        calls.append(command)
        if command[1] == "index":
            (Path(command[2]) / ".zvec-grep").mkdir()
    index_project_once(directory, projects, runs, execute=recovered)
    assert [c[1] for c in calls] == ["index", "status"]
    assert all(c[2] == str(first) for c in calls)
    assert json.loads((directory / "project-owner.json").read_text())["status"] == "idle"
    assert not (second / ".zvec-grep").exists()
    assert raw.read_bytes() == OUTPUT


@pytest.mark.parametrize("mode", ["failure", "timeout"])
def test_real_run_failure_retains_ledger_diagnostic_without_settling(tmp_path, monkeypatch, mode):
    source = repo(tmp_path / "source")
    runs = tmp_path / "runs"; runs.mkdir()
    root = runs / "run-a"
    git(source, "worktree", "add", "-qb", "run-a", str(root))
    control = IndexControl(tmp_path / "control", runs)
    control.request({"run_id": "run-a", "worktree_path": str(root),
                     "source_repo": str(source)}, "ready")
    provider(tmp_path, monkeypatch, mode)
    result = control.process_once(timeout=0.3 if mode == "timeout" else 3)
    row = control.get("run-a")
    assert result[0]["outcome"] == row["outcome"] == "error"
    assert not control.settled(row, "ready")
    assert row["done_revision"] != row["revision"]
    assert row["retry_after"] > 0
    raw_from_error(row["error"])


def test_success_has_no_failure_artifact(tmp_path, monkeypatch):
    projects, runs, directory = (tmp_path / n for n in ("projects", "runs", "control"))
    directory.mkdir(mode=0o700)
    repo(projects / "a")
    provider(tmp_path, monkeypatch, "success")
    index_project_once(directory, projects, runs, timeout=3)
    marker = json.loads((directory / "project-owner.json").read_text())
    assert marker["status"] == "idle" and marker["error"] == ""
    assert not list(directory.glob("command-*.raw"))


def test_diagnostic_write_failure_preserves_original_exception(tmp_path, monkeypatch):
    projects, runs, directory = (tmp_path / n for n in ("projects", "runs", "control"))
    directory.mkdir(mode=0o700)
    repo(projects / "a")
    original = subprocess.CalledProcessError(7, ["fixture"], output=OUTPUT)
    def failed(command, *, timeout):
        raise original
    from core import semantic_index_control as module
    actual = module.tempfile.NamedTemporaryFile
    def unavailable(*args, **kwargs):
        if kwargs.get("prefix") == "command-":
            raise OSError("fixture storage unavailable")
        return actual(*args, **kwargs)
    monkeypatch.setattr(module.tempfile, "NamedTemporaryFile", unavailable)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        index_project_once(directory, projects, runs, execute=failed)
    assert caught.value is original
    marker = json.loads((directory / "project-owner.json").read_text())
    assert marker["status"] == "error"
    assert "fixture stderr cause: missing fixture model" in marker["error"]
    assert "raw unavailable: OSError" in marker["error"]


def test_injected_text_streams_remain_compatible(tmp_path):
    from core.semantic_index_control import _failure_error
    error = _failure_error(tmp_path, subprocess.CalledProcessError(
        9, ["fixture"], output="stdout detail\n", stderr="stderr cause\n"))
    assert "exit=9" in error and "stderr cause" in error
    found = re.search(r"raw=(\S+) sha256=([0-9a-f]{64})", error)
    assert found
    assert Path(found[1]).read_bytes() == b"stdout detail\nstderr cause\n"
    assert hashlib.sha256(Path(found[1]).read_bytes()).hexdigest() == found[2]


def test_diagnostic_directory_redirect_is_not_written(tmp_path):
    from core.semantic_index_control import _failure_error
    outside = tmp_path / "outside"; outside.mkdir()
    redirect = tmp_path / "redirect"; redirect.symlink_to(outside, target_is_directory=True)
    original = subprocess.CalledProcessError(7, ["fixture"], output=OUTPUT)
    error = _failure_error(redirect, original)
    assert "fixture stderr cause: missing fixture model" in error
    assert "raw unavailable: ValueError" in error
    assert list(outside.iterdir()) == [] and original.output == OUTPUT
