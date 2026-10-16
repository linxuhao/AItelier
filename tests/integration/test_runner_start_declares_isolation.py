"""`runner_start` must declare what its run works in BEFORE the run can resolve a tool.

`RunnerService.start` (skillflow 1.5.90) creates, starts AND claims in one call:
`SkillTool(action="next")` is `create_run` -> `start_run` -> `advance_run` ->
`claim_next_step` -> resolve the step's context and tools. The butler gets no
turn between those, so a declaration appended after `start` returns is too late
for the first context read and the first code-path resolution.

This drives the REAL `coding_task` graph content through the REAL
`RunnerService`, on a real `AItelierSkillFlow` wired with the deployment's own
code-path resolver and a real `ConfigRegistry`. Only the three process
singletons are redirected at the isolated fixtures. No model, provider or
network is involved.

`coding_task` itself is now an artifact-only plan (repo_mode none: Coding Mode
code changes live in the session's coding_workspace tree), so the code-owning
case registers the same graph content as `code_task_probe` with repo_mode code.
The seam under test — declaration before the first claim — is the engine/host
start path, not the Coding Mode tool that used to call it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import api.dependencies as deps
from core import run_isolation as ri
from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from skillflow.plugins.skill_runner import RunnerService
from core.skillflow_host import AItelierSkillFlow
from core.workspace_manager import WorkspaceManager
from skillflow.exceptions import IsolationUnavailable
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader

ROOT = Path(__file__).resolve().parents[2]


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "runner fixture")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "runner@example.invalid")
    db = DBManager(str(tmp_path / "host.db"))
    ws = WorkspaceManager(str(tmp_path / "workspace"),
                          projects_base=str(tmp_path / "projects"))

    # Which runs the code-path resolver was asked about, and whether the run
    # already held an isolation record at that moment.
    asked: list[tuple[str, bool]] = []

    def resolver(project_id, run_id=None):
        if run_id:
            asked.append((run_id, ri.record(db, run_id) is not None))
        return deps._existing_repo_code_path(project_id, run_id)

    sf = AItelierSkillFlow(
        str(tmp_path / "engine.db"), workspace_base=str(tmp_path / "sf-ws"),
        projects_base=str(tmp_path / "projects"),
        tool_loader=ToolLoader(ROOT / "aitelier" / "tools"),
        code_path_resolver=resolver)
    deps._load_and_register_agent_configs(sf)
    sf.register_graph(PipelineGraph.from_yaml(ROOT / "configs" / "coding_task.yaml"))
    # The same graph content under two declared axes: code-owning and repo-less.
    text = (ROOT / "configs" / "coding_task.yaml").read_text()
    for name in ("notes_task", "code_task_probe"):
        copy = tmp_path / f"{name}.yaml"
        copy.write_text(text.replace('"coding_task"', f'"{name}"'))
        sf.register_graph(PipelineGraph.from_yaml(copy))
    registry = ConfigRegistry.build(sf)
    registry.register_one(sf, "notes_task", hint_overrides={"repo_mode": "none"})
    registry.register_one(sf, "code_task_probe", hint_overrides={"repo_mode": "code"})
    assert registry.get("coding_task").repo_mode == "none"   # artifact-only plan
    assert registry.get("code_task_probe").repo_mode == "code"
    assert registry.get("notes_task").repo_mode == "none"

    monkeypatch.setattr(deps, "db_instance", db)
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_workspace_manager", lambda: ws)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: registry)

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "app.py").write_text("VALUE = 1\n")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "base")
    db.ensure_project("code-p", repo_type="existing", repo_path=str(source))
    db.ensure_project("notes-p", repo_type="none")
    yield type("W", (), dict(db=db, ws=ws, sf=sf, source=source,
                             asked=asked, tmp=tmp_path))
    sf._conn.close()


CODE = "code_task_probe"


async def _start(w, project, graph=CODE):
    try:
        return RunnerService(w.sf).start(graph, project_id=project,
                                         seeds={"task.md": "add a fn"})
    except IsolationUnavailable as exc:
        return {"error": str(exc)}


async def test_code_graph_run_is_owned_before_its_first_claim_and_resolution(world):
    w = world
    resp = await _start(w, "code-p")
    assert resp.get("status") == "in_progress" and resp["step"] == "plan", resp
    assert "add a fn" in resp["instruction"]            # seed reached the first context
    run_id = resp["run_id"]

    rec = ri.record(w.db, run_id)
    assert rec and rec["mode"] == ri.MODE_WORKTREE
    assert rec["project_id"] == "code-p" and rec["config_name"] == CODE
    tree = Path(rec["worktree_path"])
    assert tree != w.source and tree.is_dir()
    assert ri.resolve_for_resolver(w.db, run_id) == rec["worktree_path"]
    # The host's own write/read root for this run is the run-owned tree too.
    assert Path(w.ws.get_code_path("code-p", run_id=run_id)) == tree
    # Every resolution the engine made for this run saw the record already there.
    assert w.asked and all(had for _, had in w.asked), w.asked


async def test_the_coding_task_plan_gets_no_writable_tree(world):
    w = world
    resp = await _start(w, "code-p", graph="coding_task")
    assert resp.get("status") == "in_progress", resp
    rec = ri.record(w.db, resp["run_id"])
    assert rec and rec["mode"] == ri.MODE_READ_SNAPSHOT and not rec["branch"]


async def test_repo_less_graph_stays_repo_less(world):
    w = world
    resp = await _start(w, "notes-p", graph="notes_task")
    assert resp.get("status") == "in_progress", resp
    rec = ri.record(w.db, resp["run_id"])
    assert rec and rec["mode"] == ri.MODE_NONE and not rec["worktree_path"]
    assert ri.resolve_for_resolver(w.db, resp["run_id"]) is False
    assert w.ws.get_code_path("notes-p", run_id=resp["run_id"]) is None
    assert all(had for _, had in w.asked), w.asked


async def test_declaration_refusal_is_reported_and_leaves_no_usable_run(world, monkeypatch):
    w = world
    real = ri.ensure_for_run

    def refuse(*a, **kw):
        raise IsolationUnavailable("fixture: refuse to provision")

    monkeypatch.setattr(ri, "ensure_for_run", refuse)
    resp = await _start(w, "code-p")
    assert "error" in resp and "run_id" not in resp, resp
    assert "fixture: refuse to provision" in resp["error"]
    # Nothing claimed, nothing resolved against a tree.
    assert w.asked == []
    dead = w.sf.get_run_by_project("code-p", CODE)
    assert dead and dead["status"] == "failed", dead
    assert all(s["status"] == "pending" and not s.get("claim_epoch")
               for s in w.sf.get_steps(dead["id"]))
    assert ri.record(w.db, dead["id"]) is None

    # The refusal does not poison the project: a retry is not "already live".
    monkeypatch.setattr(ri, "ensure_for_run", real)
    again = await _start(w, "code-p")
    assert again.get("status") == "in_progress", again
    assert again["run_id"] != dead["id"]
    assert ri.record(w.db, again["run_id"])["mode"] == ri.MODE_WORKTREE


async def test_provisioning_refusal_is_reported_not_papered_over(world, monkeypatch):
    """A tree that cannot be provisioned must not become a shared-checkout run."""
    w = world
    monkeypatch.setattr(ri, "_provision_tree", lambda *a, **kw: (_ for _ in ()).throw(
        IsolationUnavailable("fixture: git worktree add failed")))
    resp = await _start(w, "code-p")
    assert "error" in resp and "git worktree add failed" in resp["error"], resp
    assert w.asked == []
    assert _git(w.source, "worktree", "list").count("\n") == 0   # only the main tree
    dead = w.sf.get_run_by_project("code-p", CODE)
    assert dead["status"] == "failed"
    assert ri.record(w.db, dead["id"]) is None


def test_a_run_that_already_holds_a_record_is_not_redeclared(world, monkeypatch):
    """The other creation paths declare first; the start seam must be a no-op for them."""
    w = world
    rid = w.sf.create_run(CODE, {"project_id": "code-p"}, project_id="code-p")
    ri.ensure_for_run(w.db, run_id=rid, project_id="code-p", config_name=CODE,
                      repo_mode="code")
    calls = []
    monkeypatch.setattr(ri, "ensure_for_run", lambda *a, **kw: calls.append(kw))
    w.sf.start_run(rid)
    assert calls == []
    assert w.sf.get_run(rid)["status"] == "running"
