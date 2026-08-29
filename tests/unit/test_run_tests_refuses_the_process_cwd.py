"""run_tests handed no project root must refuse, not test the server itself.

`repo = Path(project_root or workspace_root).resolve()` — and `Path("")` is the
process CWD. In the container that is `/app`, the AItelier checkout, which IS a
git repository with ~2000 tests of its own. `repo.exists()` is true, so nothing
downstream refused: pytest ran over AItelier's suite and `test_report.json`
handed the reviewer a report about the server.

How "" gets there: a run that declares no code repository has no project root to
give. AItelier's `_exec_tool` sends `project_root=""` meaning "no opinion";
skillflow ≥1.5.52 then asks its code-path resolver and OMITS the argument, but
every earlier release — including the 1.5.46 the container installs from PyPI —
forwards "" into both roots. Either way the tool ends up defaulting, so the
refusal has to live here, in the tool.

Reachable today: `pipeline_forge` declares `x-aitelier.repo_mode: none` and its
`t_tool_impl` step carries `capability: "tool_creation"`, which grants
`run_tests`.
"""
import json
import subprocess
from pathlib import Path

import pytest

from aitelier.tools.run_tests.impl import run_tests


@pytest.fixture
def cwd_repo(tmp_path, monkeypatch):
    """A git repo with a passing test, made the process CWD — i.e. the thing
    that must NOT be tested or reported on."""
    d = tmp_path / "server_checkout"
    (d / "tests").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "tests" / "test_theirs.py").write_text(
        "def test_theirs():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(d)
    return d


def _report(out_dir: Path) -> dict:
    return json.loads((out_dir / "test_report.json").read_text(encoding="utf-8"))


def test_it_refuses_when_both_roots_are_empty(cwd_repo, tmp_path):
    out = tmp_path / "step_out"
    out.mkdir()

    result = run_tests(project_root="", workspace_root="", out_dir=str(out))

    assert result["passed"] is False
    report = _report(out)
    assert "absolute path" in report["summary"], report["summary"]
    assert report["failures"] == []
    # The tell that it did not test the CWD: no test of the server's was seen.
    assert "test_theirs" not in json.dumps(report)
    assert "node" not in report


def test_it_refuses_a_relative_root(cwd_repo, tmp_path):
    out = tmp_path / "step_out"
    out.mkdir()

    result = run_tests(project_root="tests", workspace_root="",
                       out_dir=str(out))

    assert result["passed"] is False
    assert "absolute path" in _report(out)["summary"]


def test_a_relative_workspace_root_is_no_better_than_a_relative_project_root(
        cwd_repo, tmp_path):
    """The fallback is `project_root or workspace_root`, so the second half has
    to be guarded too — otherwise an empty project_root just moves the CWD
    resolution one operand along."""
    out = tmp_path / "step_out"
    out.mkdir()

    result = run_tests(project_root="", workspace_root=".", out_dir=str(out))

    assert result["passed"] is False
    assert "absolute path" in _report(out)["summary"]


def test_it_still_runs_the_tests_of_a_real_repo(cwd_repo, tmp_path):
    """The control: the guard must not have broken the tool it protects, and it
    must be a REAL run — a build that refused everything would pass the tests
    above."""
    repo = tmp_path / "real_project"
    repo.mkdir()
    (repo / "test_ours.py").write_text(
        "def test_ours():\n    assert True\n", encoding="utf-8")
    out = tmp_path / "step_out2"
    out.mkdir()

    result = run_tests(project_root=str(repo), workspace_root=str(repo),
                       out_dir=str(out))

    report = _report(out)
    assert result["passed"] is True, report["summary"]
    assert "absolute path" not in report["summary"]
