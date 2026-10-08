"""Author SOURCE fixtures: actual MCP launch, immutable State identity and two Git repos.

No model/engine/provider is invoked. Independent execution of this file remains
required; authoring or syntax parsing is not a passing CPU receipt.
"""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from skillflow.core import SkillFlow, StepResult
from core.skillflow_host import AItelierSkillFlow
from skillflow.graph import PipelineGraph, StepNode
from skillflow.tool_loader import ToolLoader

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_graph import StateGraphStore
from core.state_attempts import StateAttempts
from core.workspace_manager import WorkspaceManager

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    import api.dependencies as deps
    import api.mcp_router as router
    from api import authz
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Owned candidate fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "owned@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Owned candidate fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "owned@example.invalid")
    db = DBManager(str(tmp_path / "host.db"))
    ws = WorkspaceManager(str(tmp_path / "workspace"), projects_base=str(tmp_path / "projects"))
    monkeypatch.setattr(deps, "db_instance", db)
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_workspace_manager", lambda: ws)
    sf = AItelierSkillFlow(
        str(tmp_path / "engine.db"), workspace_base=str(tmp_path / "sf-ws"),
        tool_loader=ToolLoader(ROOT / "aitelier" / "tools"),
        code_path_resolver=deps._existing_repo_code_path)
    deps._load_and_register_agent_configs(sf)
    sf.register_graph(PipelineGraph(name="candidate_producer", begin="work",
                                    steps=[StepNode(id="work")]))
    review = PipelineGraph.from_yaml(ROOT / "configs" / "code_review.yaml")
    sf.register_graph(review)
    manifest = SimpleNamespace(config_name="code_review", repo_mode="none",
                               seed_file="review_request.md", scheduler_owned=False)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: SimpleNamespace(
        get=lambda name: manifest if name == "code_review" else None,
        list=lambda: [manifest]))
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Owned fixture")
    git(source, "config", "user.email", "owned@example.invalid")
    (source / "changed.py").write_text('VALUE = "A"\n')
    (source / "unchanged.py").write_text('UNCHANGED = "full source context"\n')
    git(source, "add", ".")
    git(source, "commit", "-qm", "canonical A")
    canonical = git(source, "rev-parse", "HEAD")
    store = StateGraphStore(db, project_read_trusted=True)
    store.create_project("source-owner", "Candidate owner")
    store.add_nodes("source-owner", [{"key": "produce", "goal": "Produce candidate",
        "acceptance": [{"id": "source", "kind": "test", "description": "Exact source"}]}])
    attempts = StateAttempts(store)
    attempt = attempts.reserve("source-owner", "produce", 1, "candidate_producer", "owned-source")
    pid = attempt["execution_project_id"]
    db.ensure_project(pid, repo_type="existing", repo_path=str(source), config_name="candidate_producer")
    rid = sf.create_run("candidate_producer", {"project_id": pid}, project_id=pid)
    attempts.bind_run(attempt["attempt_id"], rid, sf)
    rec = ri.ensure_for_run(db, run_id=rid, project_id=pid,
                            config_name="candidate_producer", repo_mode="code")
    producer = Path(rec["worktree_path"])
    (producer / "changed.py").write_text('VALUE = "B"\n')
    git(producer, "add", "changed.py")
    git(producer, "commit", "-qm", "candidate B")
    candidate = git(producer, "rev-parse", "HEAD")
    diff = git(source, "diff", canonical, candidate)
    sf.start_run(rid)
    assert sf.advance_run(rid) == "work"
    claimed = sf.claim_next_step(rid)
    assert claimed is not None
    sf.confirm_step(claimed.token, StepResult())
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    attempts.reconcile(attempt["attempt_id"], sf, candidate_artifact=candidate)
    drivers = []
    monkeypatch.setattr(router, "_start_driver", lambda run_id, **kw: drivers.append(run_id))
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "owned-review-token")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)
    mcp = router.build_mcp()
    request = SimpleNamespace(headers={"X-AItelier-Admin-Token": "owned-review-token"}, cookies={})
    mcp.get_context = lambda: SimpleNamespace(request_context=SimpleNamespace(request=request))
    fn = mcp._tool_manager.get_tool("run_pipeline").fn
    yield SimpleNamespace(db=db, ws=ws, sf=sf, attempts=attempts, attempt=attempt,
        source=source, producer=producer, record=rec, pid=pid, rid=rid,
        canonical=canonical, candidate=candidate, diff=diff, tool=fn,
        mcp=mcp, drivers=drivers, tmp=tmp_path)
    sf._conn.close()


def launch(w, **overrides):
    arguments = dict(config="code_review", against_project=w.pid,
                     against_run=w.rid, against_commit=w.candidate,
                     seed_text="Review the candidate:\n" + w.diff, checkpoints="ask")
    arguments.update(overrides)
    result = w.tool(**arguments)
    # FastMCP preserves sync/async registered callables; use the actual wrapper.
    import asyncio
    import inspect
    return asyncio.run(result) if inspect.isawaitable(result) else result


def test_exact_candidate_full_source_reads_and_ungranted_writes_refused(world):
    from core.dpe_pipeline import PipelineEngine
    w = world
    result = launch(w)
    assert "error" not in result, result
    rid = result["run_id"]
    import json
    assert json.loads(w.sf.get_run(rid)["context_json"])["review_candidate"] == {
        "project_id": w.pid, "run_id": w.rid, "commit_sha": w.candidate}
    rec = ri.record(w.db, rid)
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT and rec["base_sha"] == w.candidate
    assert Path(rec["worktree_path"]) != w.source
    assert w.sf._workspace.get_project_code_path(result["project_id"], run_id=rid) == Path(rec["worktree_path"])
    assert w.ws.get_code_path(result["project_id"], run_id=rid) == Path(rec["worktree_path"])
    assert w.sf.advance_run(rid) == "review"
    claim = w.sf.claim_next_step(rid)
    assert claim is not None
    for path, expected in [("changed.py", 'VALUE = "B"\n'),
                           ("unchanged.py", 'UNCHANGED = "full source context"\n')]:
        read = w.sf.execute_tool("read", {"source": "repo", "path": path, "raw": True},
            run_id=rid, step_id=claim.step_id,
            step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
        assert read.get("content") == expected, read
    engine = PipelineEngine()
    engine._tool_schemas = claim.inputs["_tool_schemas"]
    for name in ("repo_remove_file", "create", "edit", "apply_patch"):
        refused = engine._exec_tool({"tool": name, "params": {"path": "changed.py"}})
        assert "not granted" in refused["error"], refused
    assert git(w.source, "rev-parse", "HEAD") == w.canonical
    assert (w.source / "changed.py").read_text() == 'VALUE = "A"\n'
    assert (Path(rec["worktree_path"]) / "changed.py").read_text() == 'VALUE = "B"\n'
    assert w.drivers == [rid]


@pytest.mark.parametrize("archive", [False, True])
def test_retained_candidate_does_not_follow_mutable_producer_branch(world, archive):
    w = world
    (w.producer / "changed.py").write_text('VALUE = "C"\n')
    git(w.producer, "add", "changed.py")
    git(w.producer, "commit", "-qm", "branch moved after retained B")
    if archive:
        git(w.source, "update-ref", "refs/aitelier/test-retained/" + w.candidate, w.candidate)
        git(w.source, "worktree", "remove", str(w.producer))
        # `git update-ref -d` needs a fully-qualified ref; the recorded branch is
        # the short name passed to `worktree add -b`, so qualify it as refs/heads/.
        git(w.source, "update-ref", "-d", "refs/heads/" + w.record["branch"])
    result = launch(w)
    assert "error" not in result, result
    rec = ri.record(w.db, result["run_id"])
    assert rec["base_sha"] == w.candidate
    assert (Path(rec["worktree_path"]) / "changed.py").read_text() == 'VALUE = "B"\n'
    assert git(w.source, "rev-parse", "HEAD") == w.canonical


@pytest.mark.parametrize("case", ["unknown-commit", "wrong-commit", "unknown-run",
    "wrong-project", "foreign-repository", "undeclared-candidate", "not-completed",
    "partial-binding", "malformed-commit", "tag-object", "missing-repository",
    "code-config", "scheduler-owned-review"])
def test_conflicting_or_unavailable_identity_refuses_before_review_effects(world, monkeypatch, case):
    w = world
    args = {}
    if case == "unknown-commit": args["against_commit"] = "f" * 40
    elif case == "wrong-commit": args["against_commit"] = w.canonical
    elif case == "unknown-run": args["against_run"] = "unregistered-run"
    elif case == "wrong-project": args["against_project"] = "source-owner"
    elif case == "foreign-repository":
        other = w.tmp / "different-repository"
        subprocess.run(["git", "clone", "--no-hardlinks", str(w.source), str(other)], check=True, capture_output=True)
        # The foreign repository really has B: mere SHA availability is insufficient.
        git(other, "cat-file", "-e", w.candidate + "^{commit}")
        w.db.update_project(w.pid, repo_path=str(other))
    elif case == "undeclared-candidate":
        # Synthetic corruption only; the fixture tests absence, never supplies a branch fallback.
        with w.db.get_connection() as conn:
            conn.execute("UPDATE state_attempts SET artifact_ref=NULL WHERE attempt_id=?", (w.attempt["attempt_id"],))
            conn.commit()
    elif case == "not-completed":
        with w.sf._tx() as conn: conn.execute("UPDATE skillflow_runs SET status='running' WHERE id=?", (w.rid,))
    elif case == "partial-binding": args["against_run"] = ""
    elif case == "malformed-commit": args["against_commit"] = w.candidate + "\n"
    elif case == "tag-object":
        git(w.source, "tag", "-a", "owned-tag", "-m", "not a commit identity", w.candidate)
        args["against_commit"] = git(w.source, "rev-parse", "owned-tag")
    elif case == "missing-repository": w.db.update_project(w.pid, repo_path=str(w.tmp / "absent"))
    elif case in {"code-config", "scheduler-owned-review"}:
        import api.dependencies as deps
        manifest = deps.get_config_registry().get("code_review")
        if case == "code-config": manifest.repo_mode = "code"
        else: manifest.scheduler_owned = True
    before_runs = w.sf.list_runs()
    before_projects = w.db.list_projects_with_stats()
    def no_workspace(*a, **k): raise AssertionError("invalid candidate reached workspace effects")
    monkeypatch.setattr(w.ws, "setup_workspace", no_workspace)
    result = launch(w, **args)
    assert "error" in result, result
    assert w.sf.list_runs() == before_runs
    assert w.db.list_projects_with_stats() == before_projects
    assert w.drivers == []
    assert git(w.source, "rev-parse", "HEAD") == w.canonical
    assert (w.source / "changed.py").read_text() == 'VALUE = "A"\n'



def test_review_row_repository_swap_cannot_substitute_a_second_repo(world, monkeypatch):
    w = world
    other = w.tmp / "different-repository-after-preflight"
    subprocess.run(["git", "clone", "--no-hardlinks", str(w.source), str(other)],
                   check=True, capture_output=True)
    git(other, "cat-file", "-e", w.candidate + "^{commit}")
    original = w.ws.setup_workspace
    def swap_after_workspace(project_id, *args, **kwargs):
        result = original(project_id, *args, **kwargs)
        w.db.update_project(project_id, repo_path=str(other))
        return result
    monkeypatch.setattr(w.ws, "setup_workspace", swap_after_workspace)
    result = launch(w)
    assert "error" in result and "identity changed" in result["error"], result
    # Admission may have created a pending owned run; it never starts execution.
    reviews = w.sf.list_runs()
    pending = [row for row in reviews if row["id"] != w.rid]
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert ri.record(w.db, pending[0]["id"]) is None
    assert w.drivers == []
    assert git(w.source, "rev-parse", "HEAD") == w.canonical

def test_legacy_unpinned_against_project_keeps_source_head_snapshot(world):
    w = world
    result = launch(w, against_run="", against_commit="")
    assert "error" not in result, result
    rec = ri.record(w.db, result["run_id"])
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT and rec["base_sha"] == w.canonical
    assert "review_candidate" not in result
