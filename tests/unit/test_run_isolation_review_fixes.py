"""Regressions for the independent review's findings (P1-2, P1-3, P2-1, P2-2, P3-1).

Each test here fails on the reviewed implementation and states the contract the
fix has to meet. They are grouped by finding, and every one of them is about a
reachable behaviour rather than a shape: a lease that outlives its run wedges a
checkout for the life of the database; a code run on a path that is not a
repository was recorded as working when it was not; an interactive editor wrote
into a checkout a run was holding; and a routing fix had quietly acquired a
remote branch push per run.
"""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from core import datadir, run_isolation as ri
from core.db_manager import DBManager
from core.run_isolation import CheckoutLeased
from skillflow.exceptions import IsolationUnavailable


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
def db(tmp_path):
    return DBManager(str(tmp_path / "aitelier.db"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    return h


class _FakeSF:
    """A run table and an admitted-operation count, and nothing else.

    The matrix below is about what the reconciler DOES with each answer, so the
    answers are stated directly. The real engine is used in
    `test_two_sequential_direct_runs...`, where the terminal transition itself is
    the thing under test.
    """

    def __init__(self, runs=None, ops=None, raises=False):
        self._runs = runs or {}
        self._ops = ops or {}
        self._raises = raises

    def get_run(self, run_id):
        if self._raises:
            raise sqlite3.OperationalError("database is locked")
        return self._runs.get(run_id)

    def audit_operation_owners(self, run_id=None):
        if self._raises:
            raise sqlite3.OperationalError("database is locked")
        n = self._ops.get(run_id, 0)
        return {"lost": [], "unknown": [], "alive": n,
                "recovery_required": False, "hint": ""}


def _direct_run(db, tmp_path, run_id, pid, src):
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    return ri.ensure_for_run(db, run_id=run_id, project_id=pid,
                             config_name="coding_impl", repo_mode="code",
                             requested_mode=ri.MODE_DIRECT)


# ── P1-2: the lease has a lifecycle ──────────────────────────────────

def test_a_terminal_quiet_run_releases_its_lease(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-1", "p1", src)
    sf = _FakeSF(runs={"run-1": {"status": "completed"}}, ops={"run-1": 0})

    released, why = ri.reconcile_run_lease(db, sf, "run-1")
    assert released is True, why
    assert ri.lease_holder(db, ri.canonical_checkout(src)) is None


@pytest.mark.parametrize("status", ["running", "paused", "pending"])
def test_a_live_or_paused_run_keeps_its_lease(db, home, tmp_path, status):
    """`paused` is a run waiting for a person, not a run that ended."""
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-2", "p2", src)
    sf = _FakeSF(runs={"run-2": {"status": status}}, ops={"run-2": 0})

    released, why = ri.reconcile_run_lease(db, sf, "run-2")
    assert released is False and "terminal" in why
    assert ri.lease_holder(db, ri.canonical_checkout(src))["run_id"] == "run-2"


def test_a_terminal_run_still_draining_keeps_its_lease(db, home, tmp_path):
    """Terminal status is not quiescence: an admitted operation cannot be
    called off, and its effects may still be landing in that checkout."""
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-3", "p3", src)
    sf = _FakeSF(runs={"run-3": {"status": "failed"}}, ops={"run-3": 1})

    released, why = ri.reconcile_run_lease(db, sf, "run-3")
    assert released is False and "admitted" in why
    assert ri.lease_holder(db, ri.canonical_checkout(src))["run_id"] == "run-3"


def test_a_missing_run_keeps_its_lease(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-4", "p4", src)
    released, why = ri.reconcile_run_lease(db, _FakeSF(), "run-4")
    assert released is False and "unknown" in why
    assert ri.lease_holder(db, ri.canonical_checkout(src)) is not None


def test_a_lookup_error_keeps_its_lease(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-5", "p5", src)
    released, why = ri.reconcile_run_lease(db, _FakeSF(raises=True), "run-5")
    assert released is False and "could not" in why.lower()
    assert ri.lease_holder(db, ri.canonical_checkout(src)) is not None


def test_reconciliation_is_idempotent(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-6", "p6", src)
    sf = _FakeSF(runs={"run-6": {"status": "completed"}}, ops={"run-6": 0})
    first = ri.reconcile_run_lease(db, sf, "run-6")
    second = ri.reconcile_run_lease(db, sf, "run-6")
    assert first[0] is True
    assert second[0] is False and "no lease" in second[1].lower()
    assert ri.lease_holder(db, ri.canonical_checkout(src)) is None


def test_startup_reconciliation_releases_only_the_quiet_terminal_ones(
        db, home, tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for p in (a, b, c):
        _init_repo(p)
    _direct_run(db, tmp_path, "run-a", "pa", a)
    _direct_run(db, tmp_path, "run-b", "pb", b)
    _direct_run(db, tmp_path, "run-c", "pc", c)
    sf = _FakeSF(runs={"run-a": {"status": "completed"},
                       "run-b": {"status": "paused"},
                       "run-c": {"status": "failed"}},
                 ops={"run-c": 2})

    report = ri.reconcile_all_leases(db, sf)
    assert report["released"] == ["run-a"]
    assert {r["run_id"] for r in report["retained"]} == {"run-b", "run-c"}
    assert ri.lease_holder(db, ri.canonical_checkout(a)) is None
    assert ri.lease_holder(db, ri.canonical_checkout(b)) is not None
    assert ri.lease_holder(db, ri.canonical_checkout(c)) is not None


def test_a_restart_with_a_pending_drain_preserves_the_lease(db, home, tmp_path):
    """The restart case specifically: the process that admitted the operation
    is gone, and that says nothing about whether its `git commit` finished."""
    src = tmp_path / "src"
    _init_repo(src)
    _direct_run(db, tmp_path, "run-d", "pd", src)
    sf = _FakeSF(runs={"run-d": {"status": "failed"}}, ops={"run-d": 1})
    ri.reconcile_all_leases(db, sf)
    assert ri.lease_holder(db, ri.canonical_checkout(src))["run_id"] == "run-d"
    # …and a second run still cannot take the checkout.
    db.ensure_project("pd2", name="pd2", repo_type="existing", repo_path=str(src))
    with pytest.raises(CheckoutLeased):
        ri.ensure_for_run(db, run_id="run-d2", project_id="pd2",
                          config_name="coding_impl", repo_mode="code",
                          requested_mode=ri.MODE_DIRECT)


def test_two_sequential_direct_runs_on_one_checkout(db, home, tmp_path):
    """The whole point, through the real engine and the real cancel path."""
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode, Transition

    src = tmp_path / "src"
    _init_repo(src)
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(PipelineGraph(
        name="serial", begin="s1",
        steps=[StepNode(id="s1", step_type="agent", output_mode="content",
                        output_fixed={"out": "out.txt"},
                        transitions=[Transition(to=None)])]))

    db.ensure_project("s1p", name="s1p", repo_type="existing", repo_path=str(src))
    db.ensure_project("s2p", name="s2p", repo_type="existing", repo_path=str(src))

    rid1 = sf.create_run("serial", {"project_id": "s1p"}, project_id="s1p")
    sf.start_run(rid1)
    ri.ensure_for_run(db, run_id=rid1, project_id="s1p", config_name="serial",
                      repo_mode="code", requested_mode=ri.MODE_DIRECT)

    rid2 = sf.create_run("serial", {"project_id": "s2p"}, project_id="s2p")
    with pytest.raises(CheckoutLeased):
        ri.ensure_for_run(db, run_id=rid2, project_id="s2p",
                          config_name="serial", repo_mode="code",
                          requested_mode=ri.MODE_DIRECT)

    report = sf.stop_run(rid1, "done with it")
    assert report["outcome"] == "stopped", report
    assert ri.reconcile_run_lease(db, sf, rid1)[0] is True

    rec = ri.ensure_for_run(db, run_id=rid2, project_id="s2p",
                            config_name="serial", repo_mode="code",
                            requested_mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT


def test_a_real_admitted_operation_holds_the_lease_through_reconciliation(
        db, home, tmp_path):
    """Same question asked of the real engine's own table, not a fake."""
    from skillflow.core import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode, Transition

    src = tmp_path / "src"
    _init_repo(src)
    sf_path = tmp_path / "sf2.db"
    sf = SkillFlow(str(sf_path))
    sf.register_graph(PipelineGraph(
        name="drain", begin="s1",
        steps=[StepNode(id="s1", step_type="agent", output_mode="content",
                        output_fixed={"out": "out.txt"},
                        transitions=[Transition(to=None)])]))
    db.ensure_project("dp", name="dp", repo_type="existing", repo_path=str(src))
    rid = sf.create_run("drain", {"project_id": "dp"}, project_id="dp")
    sf.start_run(rid)
    ri.ensure_for_run(db, run_id=rid, project_id="dp", config_name="drain",
                      repo_mode="code", requested_mode=ri.MODE_DIRECT)
    sf.stop_run(rid, "cancelled")

    # An operation admitted before the stop, in the engine's own table.
    conn = sqlite3.connect(str(sf_path))
    conn.execute(
        "INSERT INTO skillflow_active_ops (run_id, step_instance_id, "
        "claim_epoch, owner, kind, detail) VALUES (?, 0, 0, 'pid:1@boot', "
        "'delivery', 's1')", (rid,))
    conn.commit()
    conn.close()

    released, why = ri.reconcile_run_lease(db, sf, rid)
    assert released is False and "admitted" in why
    assert ri.lease_holder(db, ri.canonical_checkout(src))["run_id"] == rid


# ── P1-3: a code run on a path that is not a repository ──────────────

def test_a_code_run_on_a_non_git_path_fails_closed(db, home, tmp_path):
    """No implicit direct mode, and no row claiming the run is working."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    db.ensure_project("ng", name="ng", repo_type="existing", repo_path=str(plain))

    with pytest.raises(IsolationUnavailable) as e:
        ri.ensure_for_run(db, run_id="run-ng", project_id="ng",
                          config_name="dpe_default_v2", repo_mode="code")
    assert str(plain) in str(e.value)
    assert ri.record(db, "run-ng") is None, (
        "a refused run must not leave a record saying it is working somewhere")
    assert ri.lease_holder(db, ri.canonical_checkout(plain)) is None


def test_a_refused_run_can_never_be_handed_a_root(db, home, tmp_path):
    """Which is what makes 'no repo_apply copy may occur' true: the copy needs
    a project_root, and resolution refuses to produce one."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    db.ensure_project("ng2", name="ng2", repo_type="existing", repo_path=str(plain))
    with pytest.raises(IsolationUnavailable):
        ri.ensure_for_run(db, run_id="run-ng2", project_id="ng2",
                          config_name="dpe_default_v2", repo_mode="code")
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(db, "run-ng2", run_created_at="9999-01-01 00:00:00")


def test_a_new_project_is_bootstrapped_before_it_is_isolated(db, home, tmp_path,
                                                            monkeypatch):
    """A `repo_type: new` project whose repository has not been created yet is
    set up through the ordinary workspace bootstrap, then isolated — rather
    than refused for not being what nobody had created."""
    import api.dependencies as deps
    from core.workspace_manager import WorkspaceManager
    # monkeypatch, not assignment: `ws_instance` is a module-level singleton and
    # leaving this test's copy behind pointed every later bootstrap at a data
    # root that only exists inside this test.
    monkeypatch.setattr(deps, "ws_instance",
                        WorkspaceManager(str(tmp_path / "ws"),
                                         projects_base=str(datadir.projects_dir())))
    db.ensure_project("fresh", name="fresh", repo_type="new",
                      repo_path=str(datadir.projects_dir() / "fresh"))
    assert not (datadir.projects_dir() / "fresh" / ".git").exists()

    rec = ri.ensure_for_run(db, run_id="run-fresh", project_id="fresh",
                            config_name="dpe_default_v2", repo_mode="code")
    assert rec["mode"] == ri.MODE_WORKTREE
    assert (datadir.projects_dir() / "fresh" / ".git").exists()
    assert Path(rec["worktree_path"]).is_dir()


def test_explicit_direct_mode_leases_even_a_checkout_it_would_not_isolate(
        db, home, tmp_path):
    """Direct mode is a claim on a WRITABLE directory. Whether that directory
    is a git repository changes what a worktree could be cut from; it does not
    change who is allowed to write there."""
    plain = tmp_path / "plain"
    plain.mkdir()
    db.ensure_project("d1", name="d1", repo_type="existing", repo_path=str(plain))
    db.ensure_project("d2", name="d2", repo_type="existing", repo_path=str(plain))
    rec = ri.ensure_for_run(db, run_id="run-p1", project_id="d1",
                            config_name="coding_impl", repo_mode="code",
                            requested_mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT
    assert ri.lease_holder(db, ri.canonical_checkout(plain))["run_id"] == "run-p1"
    with pytest.raises(CheckoutLeased):
        ri.ensure_for_run(db, run_id="run-p2", project_id="d2",
                          config_name="coding_impl", repo_mode="code",
                          requested_mode=ri.MODE_DIRECT)


# ── P2-1: the interactive write surface ──────────────────────────────

@pytest.fixture
def butler(db, home, tmp_path, monkeypatch):
    import api.dependencies as deps
    from core.meta_agent import MetaAgent
    from core.workspace_manager import WorkspaceManager

    src = tmp_path / "src"
    _init_repo(src)
    (src / "file.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "file"], cwd=src, check=True)
    db.ensure_project("held", name="held", repo_type="existing", repo_path=str(src))
    db.ensure_project("holder", name="holder", repo_type="existing",
                      repo_path=str(src))
    monkeypatch.setattr(deps, "db_instance", db)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    ri.ensure_for_run(db, run_id="run-holding", project_id="holder",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    return MetaAgent(db, ws, owner_email="t@local", mode="coding"), src


def test_butler_edit_and_create_refuse_on_a_leased_checkout(butler):
    agent, src = butler
    agent._files_read.add(("held", str(src / "file.txt")))
    edited = agent._tool_edit_file({"project_id": "held", "path": "file.txt",
                                    "old_str": "original", "new_str": "changed"})
    assert "error" in edited and "run-holding" in edited["error"]
    assert (src / "file.txt").read_text() == "original\n"

    created = agent._tool_create_file({"project_id": "held", "path": "new.txt",
                                       "content": "x"})
    assert "error" in created and "run-holding" in created["error"]
    assert not (src / "new.txt").exists()


@pytest.mark.asyncio
async def test_butler_bash_refuses_on_a_leased_checkout(butler):
    """The whole call, on the checkout — not a guess about what the command does."""
    agent, src = butler
    res = await agent._tool_bash({"project_id": "held", "command": "echo hello"})
    assert "error" in res and "run-holding" in res["error"]
    assert "hello" not in str(res.get("stdout", ""))


def test_butler_reads_are_unaffected_by_a_lease(butler):
    agent, src = butler
    tree = agent._tool_list_code_tree({"project_id": "held"})
    assert "error" not in tree
    read = agent._tool_read_code_file({"project_id": "held", "path": "file.txt"})
    assert "error" not in read and "original" in read.get("content", "")


# ── P2-2: pushing is a delivery policy, not a routing side effect ─────

def _push_fixture(db, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    src = tmp_path / "src"
    _init_repo(src)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=src,
                   check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=src,
                   check=True)
    db.ensure_project("pp", name="pp", repo_type="existing", repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id="run-push", project_id="pp",
                            config_name="dpe_default_v2", repo_mode="code")
    (Path(rec["worktree_path"]) / "work.txt").write_text("work\n")
    subprocess.run(["git", "add", "-A"], cwd=rec["worktree_path"], check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "work"], cwd=rec["worktree_path"], check=True)
    return origin, src, rec


def test_an_isolated_run_pushes_nothing_by_default(db, home, tmp_path,
                                                   monkeypatch):
    """Routing git_push_post at the run worktree must not, by itself, create
    and push a remote branch per run."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, src, rec = _push_fixture(db, tmp_path)

    import aitelier.tools.git_push_post.impl as impl
    calls = []
    real = subprocess.run

    def spy(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(impl.subprocess, "run", spy)
    res = impl.git_push_post(project_root=str(src), project_id="pp",
                             run_id="run-push")

    assert res["pushed"] is False and res["action"] == "skip"
    assert not any("push" in c for c in calls), f"it pushed anyway: {calls}"
    remote_branches = _git(origin, "branch", "--list")
    assert "codex/run/run-push" not in remote_branches


def test_an_isolated_run_pushes_its_branch_only_when_asked(db, home, tmp_path,
                                                           monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, src, rec = _push_fixture(db, tmp_path)

    import aitelier.tools.git_push_post.impl as impl
    res = impl.git_push_post(project_root=str(src), project_id="pp",
                             run_id="run-push", policy="run_branch")
    assert res["pushed"] is True, res
    assert "codex/run/run-push" in _git(origin, "branch", "--list")


def test_direct_mode_push_keeps_its_existing_contract(db, home, tmp_path,
                                                      monkeypatch):
    """A checkout that a run works in directly still pushes what it always
    pushed: the branch that checkout is on, to the configured remote."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    src = tmp_path / "src"
    _init_repo(src)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=src,
                   check=True)
    db.ensure_project("dd", name="dd", repo_type="existing", repo_path=str(src))
    ri.ensure_for_run(db, run_id="run-direct", project_id="dd",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)

    import aitelier.tools.git_push_post.impl as impl
    res = impl.git_push_post(project_root=str(src), project_id="dd",
                             run_id="run-direct")
    assert res["pushed"] is True, res
    assert "main" in _git(origin, "branch", "--list")


# ── P3-1: the guard itself must fail closed ──────────────────────────

def test_the_operator_guard_fails_closed_when_it_cannot_check(db, home,
                                                              tmp_path,
                                                              monkeypatch):
    """A guard that cannot reach its database must refuse the mutation. The
    resolution path already fails closed on a lookup it cannot make; a guard
    that fails OPEN on the same condition is the one that is wrong."""
    from core.workspace_manager import WorkspaceManager
    import api.dependencies as deps

    src = tmp_path / "src"
    _init_repo(src)
    db.ensure_project("gg", name="gg", repo_type="existing", repo_path=str(src))
    monkeypatch.setattr(deps, "db_instance", db)

    def boom():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(deps, "get_db_manager", boom)
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    (src / "operator.txt").write_text("by hand\n")
    with pytest.raises(Exception) as e:
        ws.repo_commit("gg", "operator commit while the lease is unknowable")
    assert "lease" in str(e.value).lower()
    assert _git(src, "status", "--porcelain") != "", "it committed anyway"


# ── H3: the push decision comes from the DECLARED mode ───────────────
# `.git` being a gitfile means "a linked worktree", which is not the same
# statement as "this run was isolated into one". The director works out of
# worktrees routinely, and a project whose repo_path IS a worktree, run in
# explicit direct mode, had its intentional private push silently skipped.

def _linked_worktree_project(db, tmp_path, pid, *, mode):
    """A project whose repo_path is itself a linked worktree, with a remote."""
    origin = tmp_path / f"origin-{pid}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    upstream = tmp_path / f"upstream-{pid}"
    base = _init_repo(upstream)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=upstream, check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], cwd=upstream,
                   check=True)
    wt = tmp_path / f"wt-{pid}"
    subprocess.run(["git", "worktree", "add", "-q", "-b", f"feature/{pid}",
                    str(wt), base], cwd=upstream, check=True)
    (wt / "work.txt").write_text("work\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "work"], cwd=wt, check=True)
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(wt))
    rec = ri.ensure_for_run(db, run_id=f"run-{pid}", project_id=pid,
                            config_name="coding_impl", repo_mode="code",
                            requested_mode=mode)
    return origin, wt, rec


def _spy_git(monkeypatch):
    import aitelier.tools.git_push_post.impl as impl
    calls = []
    real = subprocess.run

    def spy(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(impl.subprocess, "run", spy)
    return impl, calls


def test_an_explicit_direct_run_in_a_linked_worktree_still_pushes(db, home,
                                                                  tmp_path,
                                                                  monkeypatch):
    """Declared direct. The tree happens to be a linked worktree, which the old
    heuristic read as "isolated" and refused."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, wt, rec = _linked_worktree_project(db, tmp_path, "dw",
                                               mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT
    assert (wt / ".git").is_file(), "fixture is not a linked worktree"

    impl, calls = _spy_git(monkeypatch)
    res = impl.git_push_post(project_root=str(wt), project_id="dw",
                             run_id="run-dw")
    assert res["pushed"] is True, res
    assert any("push" in c for c in calls), "no git push was invoked"
    assert "feature/dw" in _git(origin, "branch", "--list")


def test_an_isolated_run_in_the_same_shape_still_pushes_nothing(db, home,
                                                                tmp_path,
                                                                monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, _, rec = _linked_worktree_project(db, tmp_path, "iw",
                                              mode=ri.MODE_WORKTREE)
    assert rec["mode"] == ri.MODE_WORKTREE
    impl, calls = _spy_git(monkeypatch)
    res = impl.git_push_post(project_root=rec["worktree_path"],
                             project_id="iw", run_id="run-iw")
    assert res["pushed"] is False and res["action"] == "skip"
    assert not any("push" in c for c in calls), f"it pushed anyway: {calls}"
    assert _git(origin, "branch", "--list") == ""


def test_an_isolated_run_pushes_its_declared_branch_on_request(db, home,
                                                               tmp_path,
                                                               monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, _, rec = _linked_worktree_project(db, tmp_path, "rb",
                                              mode=ri.MODE_WORKTREE)
    impl, calls = _spy_git(monkeypatch)
    res = impl.git_push_post(project_root=rec["worktree_path"],
                             project_id="rb", run_id="run-rb",
                             policy="run_branch")
    assert res["pushed"] is True, res
    assert any("push" in c for c in calls)
    assert "codex/run/run-rb" in _git(origin, "branch", "--list")


def test_a_known_run_with_no_record_refuses_rather_than_guessing(db, home,
                                                                 tmp_path,
                                                                 monkeypatch):
    """A run id was supplied and the record is not there. The filesystem shape
    is not an answer to what that run declared."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin, wt, _ = _linked_worktree_project(db, tmp_path, "nr",
                                             mode=ri.MODE_DIRECT)
    with db.get_connection() as conn:
        conn.execute("DELETE FROM run_isolation WHERE run_id = ?", ("run-nr",))
        conn.commit()
    impl, calls = _spy_git(monkeypatch)
    res = impl.git_push_post(project_root=str(wt), project_id="nr",
                             run_id="run-nr")
    assert res["pushed"] is False
    assert not any("push" in c for c in calls)
    assert "declare" in res.get("detail", "").lower() or \
        "record" in res.get("detail", "").lower(), res


def test_an_invocation_with_no_run_id_keeps_the_old_contract(db, home, tmp_path,
                                                             monkeypatch):
    """The legacy boundary, preserved deliberately: no run was named, so there
    is no declaration to consult and the tool does what it always did."""
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    src = tmp_path / "plain"
    _init_repo(src)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=src,
                   check=True)
    db.ensure_project("lg", name="lg", repo_type="existing", repo_path=str(src))
    impl, calls = _spy_git(monkeypatch)
    res = impl.git_push_post(project_root=str(src), project_id="lg")
    assert res["pushed"] is True, res
    assert "main" in _git(origin, "branch", "--list")
