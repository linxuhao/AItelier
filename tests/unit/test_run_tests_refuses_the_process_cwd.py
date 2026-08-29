"""run_tests handed no project ROOT must refuse, not test some other tree.

`repo = Path(project_root or workspace_root).resolve()` had two ways to go
wrong.

* Both empty: `Path("")` is the process CWD. In the container that is `/app`,
  the AItelier checkout, which IS a git repository with ~2000 tests of its own.
  `repo.exists()` is true, so nothing downstream refuses.
* `project_root` empty, `workspace_root` absolute: the fallback then resolves to
  the DPS WORKSPACE, and pytest runs over the run's own step-output tree. This
  is the shape the engine actually produces for a repo-less run — it omits
  `project_root` and still supplies `workspace_root` — so a guard on the
  fallback's RESULT passes it while a guard on `project_root` refuses it.

How the empty value gets there: a run that declares no code repository has no
project root to give. AItelier's `_exec_tool` sends `project_root=""` meaning
"no opinion"; skillflow >=1.5.52 then asks its code-path resolver and OMITS the
argument, but every earlier release — including the 1.5.46 the container
installs from PyPI — forwards "" into both roots. Either way the tool ends up
defaulting, so the refusal has to live here, in the tool.

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
    """A relative `workspace_root` must not rescue an empty `project_root`."""
    out = tmp_path / "step_out"
    out.mkdir()

    result = run_tests(project_root="", workspace_root=".", out_dir=str(out))

    assert result["passed"] is False
    assert "absolute path" in _report(out)["summary"]


def test_an_absolute_workspace_root_does_not_satisfy_the_guard(tmp_path):
    """The shape a repo-less run actually produces, and the one a guard on
    `project_root or workspace_root` waves through.

    The engine omits `project_root` for a run that declares no code repository
    and still supplies an absolute `workspace_root` — the DPS workspace. That
    passes `is_absolute()`, `exists()` and every check downstream, so pytest runs
    over the run's own step-output tree and writes a `test_report.json` about it.
    There is no repository here to test; the only correct answer is to refuse.
    """
    ws = tmp_path / "dps_workspace"
    (ws / "pipeline_forge" / "t_tool_impl").mkdir(parents=True)
    (ws / "test_step_output.py").write_text(
        "def test_step_output():\n    assert True\n", encoding="utf-8")
    out = tmp_path / "step_out"
    out.mkdir()

    result = run_tests(project_root="", workspace_root=str(ws),
                       out_dir=str(out))

    assert result["passed"] is False, (
        "an absolute workspace_root satisfied a guard that is about the code "
        "repository; pytest just ran over the DPS workspace")
    report = _report(out)
    assert "absolute path" in report["summary"], report["summary"]
    assert "test_step_output" not in json.dumps(report)


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
