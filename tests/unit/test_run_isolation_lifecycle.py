"""Lease lifecycle through the ENTRY POINTS, and exclusion that is a mutex.

The previous round proved the reconciler's decision by calling the reconciler.
That is how a property can be green and unreachable at the same time: the last
independent review found two ways a lease outlives its run, and neither was
visible from a test that calls the helper directly. So every test here drives a
real entry — the registered MCP tool, a real scheduler tick — and none of them
calls `reconcile_run_lease` or `reconcile_all_leases` to tidy up at the end.

The exclusion tests are stronger than the guard they replace: a check followed
by a write is not a mutex, and the ruling is that the supported write surface
must actually exclude a direct-run acquisition in both orders.
"""

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from core import datadir, run_isolation as ri
from core.db_manager import DBManager
from core.run_isolation import CheckoutLeased
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition


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


def _graph(name="serial"):
    return PipelineGraph(
        name=name, begin="s1",
        steps=[StepNode(id="s1", step_type="agent", output_mode="content",
                        output_fixed={"out": "out.txt"},
                        transitions=[Transition(to=None)])])


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real engine, a real host database, a real repository, wired as the
    host singletons — so an entry point reaches all three the way it does in
    production."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    db = DBManager(str(tmp_path / "aitelier.db"))
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(_graph())

    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)

    src = tmp_path / "src"
    base = _init_repo(src)
    for pid in ("p1", "p2", "busy"):
        db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    return {"db": db, "sf": sf, "src": src, "base": base,
            "sf_path": tmp_path / "sf.db", "tmp": tmp_path}


def _direct_run(live, pid="p1"):
    sf, db = live["sf"], live["db"]
    rid = sf.create_run("serial", {"project_id": pid}, project_id=pid)
    sf.start_run(rid)
    ri.ensure_for_run(db, run_id=rid, project_id=pid, config_name="serial",
                      repo_mode="code", requested_mode=ri.MODE_DIRECT)
    return rid


_ADMIN_TOKEN = "lifecycle-test-admin-token"


@pytest.fixture(autouse=True)
def _authorized_mcp_writes(monkeypatch):
    """`stop_pipeline` is a WRITE tool, and `_authorize` denies a write that
    carries no verifiable identity whenever the Cloudflare gate is configured.
    On a dev box the gate is off and a bare call passes; in the operator's
    shell (`.env` exports AITELIER_CF_AUD) the same call is correctly denied,
    the run stays `running`, and every lease assertion below fails for a reason
    that has nothing to do with leases. Pin the gate ON and present the
    off-tunnel admin credential, so the entry is exercised THROUGH the gate the
    same way on every host."""
    from api import authz
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", _ADMIN_TOKEN)


def _admin_context():
    from types import SimpleNamespace
    request = SimpleNamespace(headers={"X-AItelier-Admin-Token": _ADMIN_TOKEN})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


async def _mcp_stop_async(run_id: str, reason: str = "stopped in a test",
                           mcp=None):
    """The registered MCP tool, reached the way the endpoint reaches it —
    including its authorization, with the admin credential in the request."""
    from api.mcp_router import build_mcp
    if mcp is None:
        mcp = build_mcp()
        mcp.get_context = _admin_context
    fn = mcp._tool_manager.get_tool("stop_pipeline").fn
    res = fn(run_id=run_id, reason=reason)
    return await res if asyncio.iscoroutine(res) else res


def _mcp_stop(run_id: str, reason: str = "stopped in a test", mcp=None):
    return asyncio.run(_mcp_stop_async(run_id, reason, mcp))


def _admit_real_operation(sf_path: Path, run_id: str, kind="delivery") -> int:
    """Admit an operation in the ENGINE's own table, as `_admit_op` does."""
    conn = sqlite3.connect(str(sf_path))
    cur = conn.execute(
        "INSERT INTO skillflow_active_ops (run_id, step_instance_id, "
        "claim_epoch, owner, kind, detail) VALUES (?, 0, 0, 'pid:1@boot', ?, 's1')",
        (run_id, kind))
    conn.commit()
    op_id = cur.lastrowid
    conn.close()
    return op_id


# ── H1 (root withdrew the finding): prove the entry, do not change it ──

def test_the_mcp_stop_entry_releases_a_quiet_run_lease(live):
    """The MCP surface is where an external agent stops a run. The reconcile
    call after `stop_run` is present in the source; this is the behaviour it
    produces, asserted through the registered tool rather than by reading."""
    rid = _direct_run(live)
    canonical = ri.canonical_checkout(live["src"])
    assert ri.lease_holder(live["db"], canonical)["run_id"] == rid

    out = _mcp_stop(rid)
    assert out.get("status") in ("stopped", "failed"), out
    assert ri.lease_holder(live["db"], canonical) is None, (
        "the MCP stop entry left the checkout leased")

    rid2 = _direct_run(live, "p2")
    assert ri.lease_holder(live["db"], canonical)["run_id"] == rid2


def test_the_mcp_stop_entry_syncs_cached_status_and_last_step(live, monkeypatch):
    """A terminal stop must update the AItelier project projection.

    The engine is the source of truth, but the dashboard reads the cached
    ``runs`` row. Before this regression guard, the supported MCP stop path
    failed the SkillFlow run and released its lease while leaving that row at
    ``running:<old_step>`` indefinitely.
    """
    import core.scheduler as scheduler
    from types import SimpleNamespace
    from api import authz
    from api.mcp_router import build_mcp

    monkeypatch.setattr(scheduler, "db", live["db"])
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: live["sf"])
    # Exercise the registered MCP wrapper with the supported off-tunnel admin
    # credential. This test must stay independent of the host's auth env: when
    # the production gate is enabled, a direct wrapper call with no request is
    # correctly denied as having no verifiable identity.
    test_token = "stop-regression-admin-token"
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", test_token)
    request = SimpleNamespace(headers={"X-AItelier-Admin-Token": test_token})
    context = SimpleNamespace(
        request_context=SimpleNamespace(request=request))
    mcp = build_mcp()
    monkeypatch.setattr(mcp, "get_context", lambda: context)

    rid = _direct_run(live)
    live["db"].update_project("p1", status="running:t_impl",
                              current_project_step="t_impl")

    out = _mcp_stop(rid, "operator stop regression", mcp=mcp)

    assert out["outcome"] == "stopped", out
    row = live["db"].get_project("p1")
    assert row["status"].startswith("failed:operator stop regression"), row
    assert row["current_project_step"] == "s1", row
    assert live["sf"].get_run(rid)["status"] == "failed"


def test_the_mcp_stop_entry_keeps_the_lease_while_the_run_drains(live):
    rid = _direct_run(live)
    _admit_real_operation(live["sf_path"], rid)
    out = _mcp_stop(rid)
    assert (out.get("outcome") or out.get("status")) in ("draining", "running"), out
    assert ri.lease_holder(live["db"], ri.canonical_checkout(live["src"]))["run_id"] == rid


# ── H2: the drain finishes later, and nothing restarts ───────────────

@pytest.mark.asyncio
async def test_a_lease_survives_the_drain_and_is_released_by_a_later_tick(live,
                                                                         monkeypatch):
    """The blocking case, end to end and with no restart:

    stop → draining (lease retained) → the operation actually retires through
    the engine's own supported release → an ordinary poller tick → the lease is
    gone and the next direct run gets the checkout. Nothing in this test calls
    the reconciler.
    """
    import core.scheduler as sc
    monkeypatch.setattr(sc, "db", live["db"])
    monkeypatch.setattr(sc, "get_skillflow", lambda: live["sf"])
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0, raising=False)

    rid = _direct_run(live)
    canonical = ri.canonical_checkout(live["src"])
    op_id = _admit_real_operation(live["sf_path"], rid)

    await _mcp_stop_async(rid, "cancelled mid-flight")
    assert ri.lease_holder(live["db"], canonical)["run_id"] == rid, \
        "a draining run must keep its checkout"

    # The operation retires through the engine's own operator path, which
    # completes the cancellation.
    live["sf"].release_operation(op_id, evidence="probe: the child process "
                                                 "exited and git status settled")
    assert live["sf"].get_run(rid)["status"] in ("failed", "completed")
    assert ri.lease_holder(live["db"], canonical) is not None, \
        "retiring the operation must not itself release the lease"

    await sc.poll_and_execute()

    assert ri.lease_holder(live["db"], canonical) is None, (
        "an ordinary tick after the drain completed did not release the lease "
        "— this is the restart-only failure the review found")
    rid2 = _direct_run(live, "p2")
    assert ri.lease_holder(live["db"], canonical)["run_id"] == rid2


@pytest.mark.asyncio
async def test_the_sweep_runs_while_other_projects_are_busy(live, monkeypatch):
    """An idle-only sweep starves: the queue is never idle on a busy host, and
    the lease would wait for a restart that may not come for days."""
    import core.scheduler as sc
    monkeypatch.setattr(sc, "db", live["db"])
    monkeypatch.setattr(sc, "get_skillflow", lambda: live["sf"])
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0, raising=False)

    rid = _direct_run(live)
    canonical = ri.canonical_checkout(live["src"])
    await _mcp_stop_async(rid, "done")
    ri._acquire_lease(live["db"], canonical, rid, "serial")   # re-hold, as a
    # crash between "terminal" and "released" would leave it

    ticked = []

    async def _fake_tick(pid, loop=None):
        ticked.append(pid)

    monkeypatch.setattr(sc, "_execute_skillflow_tick", _fake_tick)
    monkeypatch.setattr(sc.db, "get_active_projects",
                        lambda **kw: [{"project_id": "busy"}])

    await sc.poll_and_execute()

    assert ticked == ["busy"], f"the busy project stopped being advanced: {ticked}"
    assert ri.lease_holder(live["db"], canonical) is None, (
        "the sweep did not run because the queue was not idle")


@pytest.mark.asyncio
async def test_the_sweep_is_bounded_and_not_run_on_every_tick(live, monkeypatch):
    import core.scheduler as sc
    monkeypatch.setattr(sc, "db", live["db"])
    monkeypatch.setattr(sc, "get_skillflow", lambda: live["sf"])
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 600.0, raising=False)
    monkeypatch.setattr(sc, "_last_lease_sweep", 0.0, raising=False)

    calls = []
    monkeypatch.setattr(ri, "reconcile_all_leases",
                        lambda *a, **k: calls.append(1) or {"released": [],
                                                            "retained": []})
    monkeypatch.setattr(sc.db, "get_active_projects", lambda **kw: [])
    await sc.poll_and_execute()
    await sc.poll_and_execute()
    await sc.poll_and_execute()
    assert calls == [1], f"the sweep ran {len(calls)} times inside its interval"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["poll_and_execute", "poll_and_execute_demo",
                                   "poll_and_execute_owner"])
async def test_every_scheduler_entry_sweeps(live, monkeypatch, entry):
    import core.scheduler as sc
    monkeypatch.setattr(sc, "db", live["db"])
    monkeypatch.setattr(sc, "get_skillflow", lambda: live["sf"])
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0, raising=False)
    monkeypatch.setattr(sc.db, "get_active_projects", lambda **kw: [])
    monkeypatch.setattr(sc.db, "get_next_active_project", lambda **kw: None)

    rid = _direct_run(live)
    canonical = ri.canonical_checkout(live["src"])
    await _mcp_stop_async(rid, "done")
    ri._acquire_lease(live["db"], canonical, rid, "serial")

    fn = getattr(sc, entry)
    await (fn("someone@example.com") if entry.endswith("owner") else fn())
    assert ri.lease_holder(live["db"], canonical) is None, entry


@pytest.mark.asyncio
async def test_the_sweep_fails_closed_on_everything_it_cannot_verify(live,
                                                                    monkeypatch):
    """One sweep, three leases it must not touch: a run that is still running,
    a run the engine does not know, and a terminal run whose admitted operation
    has a LOST owner (which is not the same as retired)."""
    import core.scheduler as sc
    db, sf = live["db"], live["sf"]
    monkeypatch.setattr(sc, "db", db)
    monkeypatch.setattr(sc, "get_skillflow", lambda: sf)
    monkeypatch.setattr(sc, "_LEASE_SWEEP_INTERVAL_S", 0.0, raising=False)
    monkeypatch.setattr(sc.db, "get_active_projects", lambda **kw: [])

    repos = {}
    for name in ("paused", "unknown", "raises", "lostowner"):
        p = live["tmp"] / f"repo_{name}"
        _init_repo(p)
        repos[name] = ri.canonical_checkout(p)

    live_rid = sf.create_run("serial", {"project_id": "p1"}, project_id="p1")
    sf.start_run(live_rid)
    ri._acquire_lease(db, repos["paused"], live_rid, "serial")
    ri._acquire_lease(db, repos["unknown"], "not-a-run-this-engine-made", "serial")

    lost_rid = sf.create_run("serial", {"project_id": "p2"}, project_id="p2")
    sf.start_run(lost_rid)
    _admit_real_operation(live["sf_path"], lost_rid, kind="tool")
    sf.stop_run(lost_rid, "cancelled")
    ri._acquire_lease(db, repos["lostowner"], lost_rid, "serial")

    await sc.poll_and_execute()

    for name in ("paused", "unknown", "lostowner"):
        assert ri.lease_holder(db, repos[name]) is not None, (
            f"the sweep released the {name} lease")


# ── S1: the refusal proven with a real engine row ────────────────────

def test_a_run_this_deployment_made_with_no_record_is_refused(live):
    """No injected timestamp: a REAL engine row, created now, with no isolation
    record — which is what a declaration that lost its record looks like."""
    from skillflow.exceptions import IsolationUnavailable
    rid = live["sf"].create_run("serial", {"project_id": "p1"}, project_id="p1")
    assert ri.record(live["db"], rid) is None
    with pytest.raises(IsolationUnavailable) as e:
        ri.resolve_for_resolver(live["db"], rid)
    assert rid in str(e.value)


def test_a_run_the_engine_never_created_is_not_this_deployments_to_refuse(live):
    """The documented boundary, asserted no wider than it holds: an id this
    engine has no row for gets 'no opinion'. It is safe because every supported
    creation path writes the engine row BEFORE provisioning — which is the
    property `test_a_run_that_cannot_be_isolated_is_not_started` and the
    launcher tests cover; it is not re-asserted here."""
    assert ri.resolve_for_resolver(live["db"], "id-from-another-engine") is None


# ── S2: exclusion that is a mutex, in both orders ────────────────────

def test_an_in_flight_interactive_write_excludes_a_direct_acquisition(live,
                                                                     monkeypatch):
    """The barrier: hold a real edit_file BETWEEN admission and mutation, and
    try to take the checkout for a direct run from inside that window."""
    from core.meta_agent import MetaAgent
    from core.workspace_manager import WorkspaceManager
    import skillflow.write_tools as wt

    src = live["src"]
    (src / "file.txt").write_text("original\n")
    ws = WorkspaceManager(str(live["tmp"] / "ws"),
                          projects_base=str(live["tmp"] / "pb"))
    agent = MetaAgent(live["db"], ws, owner_email="t@local", mode="coding")
    agent._files_read.add(("p1", str((src / "file.txt").resolve())))

    seen = {}
    # The barrier sits where the tool does its surgical replace — inside the
    # admission and before the file is written.
    real_replace = wt._unique_replace

    def _barrier(*args, **kwargs):
        # Inside the admission, before the file is written.
        try:
            _direct_run(live, "p2")
            seen["acquired"] = True
        except CheckoutLeased as e:
            seen["refused"] = str(e)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(wt, "_unique_replace", _barrier)
    out = agent._tool_edit_file({"project_id": "p1", "path": "file.txt",
                                 "old_str": "original", "new_str": "changed"})

    assert out.get("edited") == "file.txt", out
    assert "acquired" not in seen, (
        "a direct run took the checkout while an interactive write was in flight")
    assert "refused" in seen and "edit_file" in seen["refused"]
    assert (src / "file.txt").read_text() == "changed\n"
    # …and the admission is gone afterwards, so the next run may have it.
    _direct_run(live, "p2")


def test_a_direct_lease_excludes_every_supported_write(live):
    from core.meta_agent import MetaAgent
    from core.workspace_manager import WorkspaceManager

    src = live["src"]
    (src / "file.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "f"], cwd=src, check=True)
    _direct_run(live, "p1")

    ws = WorkspaceManager(str(live["tmp"] / "ws"),
                          projects_base=str(live["tmp"] / "pb"))
    agent = MetaAgent(live["db"], ws, owner_email="t@local", mode="coding")
    agent._files_read.add(("p2", str((src / "file.txt").resolve())))

    edited = agent._tool_edit_file({"project_id": "p2", "path": "file.txt",
                                    "old_str": "original", "new_str": "x"})
    assert "error" in edited
    created = agent._tool_create_file({"project_id": "p2", "path": "new.txt",
                                       "content": "x"})
    assert "error" in created
    bashed = asyncio.run(agent._tool_bash({"project_id": "p2",
                                           "command": "echo hi"}))
    assert "error" in bashed
    for call in (lambda: ws.repo_commit("p2", "m"),
                 lambda: ws.repo_pull("p2"),
                 lambda: ws.repo_set_remote("p2", "https://example.invalid/x")):
        with pytest.raises(CheckoutLeased):
            call()
    assert (src / "file.txt").read_text() == "original\n"
    assert not (src / "new.txt").exists()


def test_an_admission_is_retired_even_when_the_write_raises(live):
    src = live["src"]
    with pytest.raises(ValueError):
        with ri.write_admission(live["db"], src, kind="probe", detail="boom"):
            raise ValueError("boom")
    assert ri.write_admissions(live["db"]) == []
    _direct_run(live, "p1")          # the checkout is free


def test_an_admission_only_retires_its_own_operation(live):
    src = live["src"]
    other = live["tmp"] / "other"
    _init_repo(other)
    with ri.write_admission(live["db"], other, kind="probe", detail="keep"):
        with ri.write_admission(live["db"], src, kind="probe", detail="inner"):
            pass
        rows = ri.write_admissions(live["db"])
        assert [r["detail"] for r in rows] == ["keep"], rows


def test_a_stale_admission_fails_closed_with_operator_visible_evidence(live):
    """A crashed writer leaves its row. It is not expired by time or by the
    death of its owner — an operator clears it, with evidence."""
    src = live["src"]
    op = ri.admit_write(live["db"], src, kind="bash", detail="a long build")
    with pytest.raises(CheckoutLeased) as e:
        _direct_run(live, "p1")
    msg = str(e.value)
    assert "bash" in msg and "a long build" in msg
    assert op["owner"] in msg
    assert "clear_write_admission" in msg, "the operator is not told what to do"

    ri.clear_write_admission(live["db"], op["id"],
                             evidence="probe: pid gone, git status settled")
    _direct_run(live, "p1")


def test_distinct_checkouts_are_not_serialised_against_each_other(live):
    other = live["tmp"] / "other"
    _init_repo(other)
    with ri.write_admission(live["db"], live["src"], kind="probe", detail="a"):
        with ri.write_admission(live["db"], other, kind="probe", detail="b"):
            assert len(ri.write_admissions(live["db"])) == 2


def test_the_same_checkout_under_another_name_is_the_same_admission(live):
    """Canonical binding: an admission taken through a symlink still excludes an
    acquisition that names the real path, and vice versa. (Two WRITES on one
    checkout are not serialised against each other — the contract is between
    the write surface and a direct run acquiring the checkout.)"""
    alias = live["tmp"] / "alias"
    alias.symlink_to(live["src"])
    with ri.write_admission(live["db"], alias, kind="probe", detail="through the alias"):
        with pytest.raises(CheckoutLeased) as e:
            _direct_run(live, "p1")
        assert "through the alias" in str(e.value)
    _direct_run(live, "p1")
