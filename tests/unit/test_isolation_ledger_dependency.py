"""The isolation ledger is the CALLER's DBManager, not the process singleton.

The record a run's isolation is decided by is written by whoever creates the
run — the launcher, the poller, the Meta controller, Coding Mode — through the
DB *that caller holds*. The host ingress then reads it back before the run can
start (`start_run` -> `_declare_unowned_run`) and again before a claim
(`_admit_source_identity`). Both reads used to go through
`api.dependencies.get_db_manager()`, the process-global DBManager, which agrees
with those writers only while the process has exactly one: deployed it does,
and every test that overrides the FastAPI DB dependency (or any embedder that
hands the host its own DBManager) splits it. The write then lands in one ledger
and the read asks another, the run looks record-less, is re-provisioned over the
tree it already owns and is refused —

    "<path> already exists and run <id> has no record naming it"

— a system refusing to adopt its own work.

These tests give the host TWO real DBManagers and pin the answer to the
caller's. They are written so that reintroducing the singleton read fails, not
merely so that the fixed code passes: the singleton is either a different live
DBManager (so the wrong ledger answers "no record") or a function that raises
on being called at all (`_forbid_the_singleton`).

No model, engine or provider is invoked; every DBManager here is a real SQLite
file and every worktree is a real `git worktree`.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import run_isolation as ri
from core.db_manager import DBManager
from core.skillflow_host import AItelierSkillFlow
from skillflow.exceptions import IsolationUnavailable

RUN = "run-split"
PROJECT = "p"
CONFIG = "dpe_default_v2"


# ── real repositories and real ledgers ────────────────────────────────

def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Per-test data root, so this test's worktrees are its own."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    return h


@pytest.fixture
def declared(tmp_path, home):
    """A run with a live isolation record on `ours`, and a second empty ledger.

    This is the split, in its smallest honest form: two real DBManagers, one
    run, one tree, and the record naming it in exactly one of the two.
    """
    src = tmp_path / "src"
    _init_repo(src)
    ours = DBManager(str(tmp_path / "ours.db"))
    singleton = DBManager(str(tmp_path / "singleton.db"))
    ours.ensure_project(PROJECT, name=PROJECT, repo_type="existing",
                        repo_path=str(src))
    rec = ri.ensure_for_run(ours, run_id=RUN, project_id=PROJECT,
                            config_name=CONFIG, repo_mode="code")
    assert Path(rec["worktree_path"]).is_dir(), "precondition: a real tree"
    # The precondition the tests depend on: the two ledgers really differ.
    assert ri.record(ours, RUN) is not None
    assert ri.record(singleton, RUN) is None
    return SimpleNamespace(ours=ours, singleton=singleton, rec=rec, src=src)


def _forbid_the_singleton(monkeypatch):
    """Make reading the process-global DBManager an outright failure.

    Stronger than pointing it at another DB: a reintroduced singleton read is
    reported as exactly what it is, instead of as an isolation refusal whose
    cause has to be read out of a traceback.
    """
    import api.dependencies as deps

    def forbidden():
        raise AssertionError(
            "the isolation path read api.dependencies.get_db_manager() — the "
            "process singleton — instead of its caller's DBManager (see "
            "AItelierSkillFlow._ledger_db)")

    monkeypatch.setattr(deps, "get_db_manager", forbidden)


def _host(monkeypatch, *, db=None):
    """A real `AItelierSkillFlow` ingress, bound to this test's engine.

    Not constructed (that would build a real engine): the ingress is the unit
    under test and touches `_workspace`, the ledger, this host's own `get_run`
    and `get_skillflow`'s identity — bound here the way production binds them.
    `db` is the constructor injection under test; `None` is "no injection".
    """
    import api.dependencies as deps
    host = object.__new__(AItelierSkillFlow)
    host._workspace = object()
    host._ledger = db
    host.get_run = lambda run_id: {
        "id": run_id, "project_id": PROJECT, "graph_name": CONFIG,
        # Modern: created after this ledger began recording isolation, so a
        # record-less read is a refusal and not "legacy, use the old answer".
        "created_at": "2999-01-01 00:00:00", "context_json": "{}"}
    monkeypatch.setattr(deps, "get_skillflow", lambda: host)
    return host


# ── the declaration reads the ledger it was handed ────────────────────

def test_the_run_declared_on_the_callers_ledger_is_seen(declared, monkeypatch):
    """The defect, at its smallest: the record exists, in the caller's ledger."""
    import api.dependencies as deps
    # The realistic split: the process singleton is a real, live DBManager,
    # just not the one this run was declared on.
    monkeypatch.setattr(deps, "get_db_manager", lambda: declared.singleton)
    host = _host(monkeypatch)

    # A caller that wrote this run's record through `ours` hands `ours` in.
    assert host._declare_unowned_run(RUN, db=declared.ours) is None

    # Nothing was provisioned against the ledger that never saw the run: the
    # host did not re-decide isolation behind the caller's back.
    assert ri.record(declared.singleton, RUN) is None


def test_the_constructor_ledger_is_what_a_host_reads_by_default(declared,
                                                                monkeypatch):
    """A host built with its own DBManager never asks the process for one."""
    _forbid_the_singleton(monkeypatch)
    host = _host(monkeypatch, db=declared.ours)
    assert host._declare_unowned_run(RUN) is None


def test_the_isolation_lookup_sees_the_record_on_the_callers_ledger(declared,
                                                                    monkeypatch):
    """The sibling read on the claim path answers from the same ledger."""
    _forbid_the_singleton(monkeypatch)
    host = _host(monkeypatch)
    # `ours` holds the record, so the resolver hands back the run's tree.
    assert host._admit_source_identity(RUN, db=declared.ours) is None


# ── and the assertion above is not vacuous ────────────────────────────

def test_the_other_ledger_still_refuses_the_same_run(declared, monkeypatch):
    """A ledger that really does hold no record refuses; the check can fail.

    Without this, a host that ignored its `db` argument entirely and answered
    "seen" to everything would pass every assertion above.
    """
    _forbid_the_singleton(monkeypatch)
    host = _host(monkeypatch)
    with pytest.raises(IsolationUnavailable, match="no\\s+isolation record"):
        host._admit_source_identity(RUN, db=declared.singleton)


# ── the default preserves the deployment's single-DBManager behaviour ──

def test_one_ledger_in_the_process_still_reads_that_ledger(declared,
                                                           monkeypatch):
    """Deployed there is one DBManager: no injection, nothing changes.

    Constructed the way `api.dependencies.get_skillflow()` constructs it, and
    reading through `get_db_manager()` as before — the accessor's last resort.
    """
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_db_manager", lambda: declared.ours)
    host = _host(monkeypatch)
    assert host._declare_unowned_run(RUN) is None
    assert host._admit_source_identity(RUN) is None


# ── start_run is the seam the launcher uses ───────────────────────────

def test_start_run_hands_its_callers_ledger_to_the_declaration(monkeypatch):
    """`sf.start_run(run_id, db=...)` is how a caller passes its own ledger.

    The end-to-end proof is `tests/integration/test_run_routers.py` (a real
    launcher, a real dependency-overridden DB, no monkeypatch); this pins the
    seam itself so a refactor of `start_run` cannot silently drop the argument.
    """
    from skillflow import SkillFlow
    host = _host(monkeypatch)
    seen = {}

    def declare(self, run_id, *, db=None):
        seen["db"] = db

    monkeypatch.setattr(AItelierSkillFlow, "_declare_unowned_run", declare)
    monkeypatch.setattr(SkillFlow, "start_run", lambda self, run_id: None)
    ledger = object()
    host.start_run("some-run", db=ledger)
    assert seen["db"] is ledger


def test_the_starting_helper_reaches_the_host_and_tolerates_a_bare_engine(
        monkeypatch):
    """Launch call sites start through `start_with_ledger`.

    The host receives the caller's ledger; a bare `SkillFlow` — an embedder, an
    evidence probe, a test driving the SDK directly — is still started with the
    only argument the SDK's own `start_run` accepts.
    """
    from skillflow import SkillFlow
    from core.skillflow_host import start_with_ledger
    host = _host(monkeypatch)
    seen = {}

    def declare(self, run_id, *, db=None):
        seen["db"] = db

    monkeypatch.setattr(AItelierSkillFlow, "_declare_unowned_run", declare)
    monkeypatch.setattr(SkillFlow, "start_run", lambda self, run_id: None)
    ledger = object()
    start_with_ledger(host, "host-run", ledger)
    assert seen["db"] is ledger

    started = []
    monkeypatch.setattr(SkillFlow, "start_run",
                        lambda self, run_id: started.append(run_id))
    start_with_ledger(SkillFlow(":memory:"), "bare-run", ledger)
    assert started == ["bare-run"]

