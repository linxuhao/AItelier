"""Independent review regressions: corrupt owner and durable failure provenance."""
import json
from pathlib import Path
import subprocess
import pytest
from core import deployment_quiescence as dq
from core.semantic_index_control import index_project_once


def repo(path):
    path.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.name", "fixture"], ["config", "user.email", "fixture@example.invalid"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "code.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "code.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


def provider(command, **kwargs):
    if command[1] == "index":
        (Path(command[2]) / ".zvec-grep").mkdir(exist_ok=True)


def test_json_null_existing_owner_must_remain_unknown(tmp_path):
    projects, runs, control = (tmp_path / n for n in ("projects", "runs", "control"))
    control.mkdir()
    root = repo(projects / "writable")
    marker = control / "project-owner.json"
    marker.write_text("null\n")
    assert dq._semantic_worker_errors(control)
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        provider(command, **kwargs)
    try:
        index_project_once(control, projects, runs, execute=execute)
    except (ValueError, TypeError):
        pass
    assert marker.read_text() == "null\n", f"corrupt marker replaced: {marker.read_text()}; calls={calls}"
    assert calls == []
    assert not (root / ".zvec-grep").exists()


def test_first_failure_survives_recovery_then_next_owner(tmp_path):
    projects, runs, control = (tmp_path / n for n in ("projects", "runs", "control"))
    control.mkdir()
    root = repo(projects / "a")
    second = repo(projects / "b")
    marker = control / "project-owner.json"
    original = {"root": str(root), "status": "error", "error": "original failure reason without external raw carrier", "updated_at": 1.0}
    marker.write_text(json.dumps(original))
    index_project_once(control, projects, runs, execute=provider)
    recovered = json.loads(marker.read_text())
    assert recovered["first_failure"] == original
    index_project_once(control, projects, runs, execute=provider)
    current = json.loads(marker.read_text())
    assert current["status"] == "idle" and current["root"] == str(second)
    assert original["error"] in marker.read_text(), f"original failure discarded on owner switch: {current}"
    assert current["failure_history"] == [original]
    third = repo(projects / "c")
    index_project_once(control, projects, runs, execute=provider)
    advanced = json.loads(marker.read_text())
    assert advanced["status"] == "idle" and advanced["root"] == str(third)
    assert advanced["failure_history"] == [original]
    assert "first_failure" not in advanced  # historical error does not own this root
    assert dq._semantic_worker_errors(control) == []
