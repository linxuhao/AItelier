"""The implement step's product is OBSERVED, not taken from its own report.

Each test here is one rule of the card, written so that breaking the rule makes
it fail:

* a declared change the worktree does not carry is `unconfirmed`, and the file
  (or the missing content) is named;
* an already-satisfied predicate is not a red — a write step's file exists
  BEFORE the step, so demanding redness first would teach it to delete the file;
* could-not-measure is `absent`, recorded as a bounded episode through
  `core/gate_deferral.py`, and names what it waits for;
* the tool never raises: it is not a gate and may not fail the run it observes.
"""
import json
import subprocess
from pathlib import Path

import pytest

from core.gate_deferral import LEDGER, absent_terminal_names_no_failure
from aitelier.tools.implement_evidence import impl


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def make_repo(root: Path, files: dict[str, str]) -> str:
    """A real git worktree whose HEAD holds exactly `files`. Returns the commit."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Evidence fixture")
    git(root, "config", "user.email", "evidence@test.invalid")
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return git(root, "rev-parse", "HEAD")


def deliver(root: Path, files: dict[str, str], message: str = "implemented") -> str:
    """The worker's delivery: write files and commit, as the engine would."""
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)
    return git(root, "rev-parse", "HEAD")


def declare(graph_dir: Path, *, files: list[str], base: str, commit: str,
            step: str = "implement", **extra) -> Path:
    """Publish the step's own delivery receipt, exactly where the engine does."""
    step_dir = graph_dir / step
    step_dir.mkdir(parents=True, exist_ok=True)
    receipt = step_dir / impl.RECEIPT_NAME
    receipt.write_text(json.dumps({"target": "code", "run_id": "run-fixture",
                                   "step_instance_id": 1, "base_commit": base,
                                   "commit": commit, "files": files, **extra}),
                       encoding="utf-8")
    return receipt


@pytest.fixture(autouse=True)
def _clean_episodes():
    """The ledger is process-wide state; one test's absence is not another's."""
    for key in [k for k in LEDGER.episodes if k.startswith("implement_evidence/")]:
        LEDGER.clear(key)
    yield
    for key in [k for k in LEDGER.episodes if k.startswith("implement_evidence/")]:
        LEDGER.clear(key)


@pytest.fixture
def sources(tmp_path: Path):
    """(workspace, graph dir, out dir) laid out like a real run."""
    workspace = tmp_path / "worktree"
    graph_dir = tmp_path / "ws" / "project" / "coding_impl"
    out_dir = graph_dir / "implement_evidence"
    out_dir.mkdir(parents=True)
    return workspace, graph_dir, out_dir


def observe(workspace: Path, out_dir: Path, **kwargs) -> dict:
    return impl.implement_evidence(project_root=str(workspace), out_dir=str(out_dir),
                                   run_id="run-fixture", step_id="implement_evidence",
                                   **kwargs)


def test_a_declared_change_the_worktree_carries_is_confirmed(sources):
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    commit = deliver(workspace, {"feature.py": "def tag(value):\n    return value.strip()\n",
                                 "impl.py": "x = 1\n"})
    declare(graph_dir, files=["feature.py", "impl.py"], base=base, commit=commit)

    result = observe(workspace, out_dir)

    assert result["verdict"] == "confirmed"
    assert result["state"] == "measured" and result["confirmed"] is True
    assert result["missing"] == []
    # The basis is the predicate library's own words, not a summary written here.
    by_predicate = [entry["predicate"] for entry in result["basis"]]
    assert by_predicate.count("file_changed") == 2
    assert "command_exit" in by_predicate
    assert all(entry["outcome"] == "pass" for entry in result["basis"])
    # The verdict is step OUTPUT: a file the step dir carries, plus the count.
    report = out_dir / impl.REPORT_NAME
    assert report.is_file(), "the verdict must survive in the step's output dir"
    assert json.loads(report.read_text())["verdict"] == "confirmed"
    assert result["written"] == impl.REPORT_NAME


def test_a_claim_the_worktree_contradicts_is_unconfirmed_and_names_the_file(sources):
    """The worker claims it changed feature.py; the tree holds the base version."""
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    # The receipt says feature.py was delivered, and the commit is the base
    # commit: a claim with nothing behind it.
    declare(graph_dir, files=["feature.py"], base=base, commit=base)

    result = observe(workspace, out_dir)

    assert result["verdict"] == "unconfirmed"
    assert result["state"] == "measured" and result["confirmed"] is False
    assert [m["path"] for m in result["missing"]] == ["feature.py"]
    assert "byte-identical" in result["missing"][0]["reason"]
    assert "feature.py" in result["summary"]
    assert json.loads((out_dir / impl.REPORT_NAME).read_text())["verdict"] == "unconfirmed"


def test_a_declared_content_marker_the_file_lacks_is_named_as_missing_content(sources):
    """Changed is not enough: the declared CONTENT has to be there."""
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    commit = deliver(workspace, {"feature.py": "def tag(value):\n    return value.upper()\n"})
    declare(graph_dir, files=["feature.py"], base=base, commit=commit)

    result = observe(workspace, out_dir, declared=json.dumps(
        [{"path": "feature.py", "contains": "return value.strip()"}]))

    assert result["verdict"] == "unconfirmed"
    missing = result["missing"][0]
    assert missing["path"] == "feature.py"
    assert missing["missing"] == "content"
    assert missing["expected_content"] == "return value.strip()"
    assert "value.strip()" in missing["reason"]


def test_an_already_satisfied_predicate_is_not_a_forced_red(sources):
    """A write step's file already EXISTS before it runs. That must not be red.

    Requiring the file to be absent-or-wrong first would make deleting it the
    only way to satisfy the check — the over-gating failure `todo_runtime`
    documents at its own `test_an_item_is_not_required_to_be_red_before_it_runs`.
    """
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    commit = deliver(workspace, {"feature.py": "def tag(value):\n    return value.strip()\n"})
    declare(graph_dir, files=["feature.py"], base=base, commit=commit)
    # The file exists and already carries the marker before the observation: a
    # pre-state that is already satisfying must still be a PASS.
    before = observe(workspace, out_dir, declared=json.dumps(
        [{"path": "feature.py", "contains": "return value.strip()"}]))
    assert before["verdict"] == "confirmed", before

    # And the honoured pre-state is the BASE COMMIT, not "the file was absent":
    # the very same bytes count as changed relative to the base.
    assert git(workspace, "cat-file", "-e", f"{base}:feature.py") == ""
    assert (workspace / "feature.py").read_text() != subprocess.check_output(
        ["git", "-C", str(workspace), "show", f"{base}:feature.py"], text=True)


def test_a_claim_with_no_recorded_file_is_unconfirmed_and_changes_nothing(sources):
    """Observed live: a code step can complete with `files: []` and wrote_files false."""
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    declare(graph_dir, files=[], base=base, commit=base)

    result = observe(workspace, out_dir)

    assert result["verdict"] == "unconfirmed"
    assert result["basis"] == [], "nothing declared means nothing can be measured as changed"
    assert "names no changed file" in result["missing"][0]["reason"]


def test_a_receipt_from_another_run_is_an_absence_not_evidence(sources):
    """Step output dirs are keyed by project+config+step, so runs share one.

    Last run's receipt is not THIS run's declared change: holding this worktree
    against it would compare two different runs' products.
    """
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "def tag(value):\n    return value.lower()\n"})
    commit = deliver(workspace, {"feature.py": "def tag(value):\n    return value.strip()\n"})
    declare(graph_dir, files=["feature.py"], base=base, commit=commit,
            run_id="an-earlier-run")

    result = observe(workspace, out_dir)          # this run is "run-fixture"

    assert result["verdict"] == "absent", result
    assert "an-earlier-run" in result["reason"] and "run-fixture" in result["reason"]
    assert result["waiting_for"] == "this run's own implement receipt"
    assert LEDGER.episodes["implement_evidence/run-fixture/implement_evidence"].gate \
        == "delivery_receipt"


def test_a_missing_receipt_is_an_absence_naming_what_it_waits_for(sources):
    workspace, graph_dir, out_dir = sources
    make_repo(workspace, {"feature.py": "x = 1\n"})

    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent"
    assert result["state"] == "absent" and result["confirmed"] is None
    assert "code_changes.json" in result["waiting_for"]
    key = "implement_evidence/run-fixture/implement_evidence"
    assert key in LEDGER.episodes, "the absence must be a bounded episode, not a failure"
    assert LEDGER.episodes[key].gate == "delivery_receipt"
    assert "code_changes.json" in result["reason"]


def test_an_absence_that_outlives_the_ceiling_ends_naming_the_gate_not_a_failure(sources):
    workspace, graph_dir, out_dir = sources
    make_repo(workspace, {"feature.py": "x = 1\n"})
    key = "implement_evidence/run-fixture/implement_evidence"
    import time
    LEDGER.note_absence(key, now=time.time() - 10_000_000, gate="delivery_receipt")

    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent"
    assert "terminal_reason" in result, "an expired absence must end by naming itself"
    assert "delivery_receipt" in result["terminal_reason"]
    assert absent_terminal_names_no_failure(result["terminal_reason"]), result["terminal_reason"]
    assert result["episode"]["absences"] >= 2


@pytest.mark.parametrize("failure", ["missing_binary", "timeout"])
def test_an_unrunnable_git_is_absent_not_a_failed_step(sources, monkeypatch, failure):
    """A missing binary or a timeout measured nothing, so it accuses nobody."""
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "x = 1\n"})
    declare(graph_dir, files=["feature.py"], base=base, commit=base)

    class Shim:
        TimeoutExpired = subprocess.TimeoutExpired

        def run(self, argv, *args, **kwargs):  # the only subprocess the tool needs
            if failure == "missing_binary":
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(impl, "subprocess", Shim())

    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent", result
    assert result["state"] == "absent"
    assert "git" in result["waiting_for"]
    key = "implement_evidence/run-fixture/implement_evidence"
    assert LEDGER.episodes[key].gate in {"command_exit", "file_changed"}
    assert json.loads((out_dir / impl.REPORT_NAME).read_text())["verdict"] == "absent"


def test_a_base_commit_this_worktree_lacks_is_an_absence_not_a_missing_change(sources):
    workspace, graph_dir, out_dir = sources
    make_repo(workspace, {"feature.py": "x = 1\n"})
    declare(graph_dir, files=["feature.py"], base="0" * 40, commit="0" * 40)

    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent", result
    assert "does not resolve" in result["reason"]
    assert "waiting_for" in result and "0" * 40 in result["waiting_for"]


def test_an_outside_path_in_the_receipt_is_refused_not_resolved(sources):
    workspace, graph_dir, out_dir = sources
    base = make_repo(workspace, {"feature.py": "x = 1\n"})
    commit = deliver(workspace, {"feature.py": "x = 2\n"})
    declare(graph_dir, files=["../escape.py", "feature.py"], base=base, commit=commit)

    result = observe(workspace, out_dir)

    # The real change is confirmed; the escaping path is named as refused, so a
    # receipt cannot smuggle a host path into the observation.
    assert result["verdict"] == "unconfirmed"
    assert [entry["path"] for entry in result["missing"]] == ["../escape.py"]
    assert "outside the workspace" in result["missing"][0]["reason"]


@pytest.mark.parametrize("payload", ["[]", "{not json", '{"files": "feature.py"}'])
def test_a_malformed_receipt_is_an_absence_and_never_raises(sources, payload):
    workspace, graph_dir, out_dir = sources
    make_repo(workspace, {"feature.py": "x = 1\n"})
    step_dir = graph_dir / "implement"
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / impl.RECEIPT_NAME).write_text(payload, encoding="utf-8")

    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent", result
    assert result["state"] == "absent"


def test_an_exception_inside_the_observation_is_reported_not_raised(sources, monkeypatch):
    """The tool must never raise, even on a bug inside its own observation.

    A tool-step exception reopens the step and, on the third crash, fails the
    run — which would let the OBSERVER fail a run that would otherwise pass.
    """
    workspace, graph_dir, out_dir = sources
    make_repo(workspace, {"feature.py": "x = 1\n"})

    def boom(*args, **kwargs):
        raise RuntimeError("observation bug")

    monkeypatch.setattr(impl, "_observe", boom)
    result = observe(workspace, out_dir)

    assert result["verdict"] == "absent"
    assert "observation bug" in result["reason"]
    assert (out_dir / impl.REPORT_NAME).is_file()
