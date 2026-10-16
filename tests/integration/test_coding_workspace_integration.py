"""Coding Mode on ONE authentic owned working tree — actual host + SDK routes.

Everything here is the ordinary product path: the authenticated chat endpoint
(`POST /api/agent/chat`), MetaAgent's loop and dispatch, the real
`AItelierSkillFlow` engine with the static `coding_workspace` graph, run
isolation (no-checkout registration, raw materialisation, H∪I∪U seed), the
exclusive `coding_workspace` admission, closed Git and the RunnerService/SDK
checkpoint CAS. The ONLY substitute is the model: `_stream_llm` replays a
deterministic script of tool calls. No run, claim, origin or admission row is
forged; failures are injected only at named primitives and say so.

Requires ``CODING_WORKSPACE_FIXTURE`` (an owned disposable directory); the
data root lives under it so every record can be archived as evidence.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

FIXTURE = os.environ.get("CODING_WORKSPACE_FIXTURE")
pytestmark = pytest.mark.skipif(not FIXTURE, reason="requires an owned disposable fixture root")
RAW = Path(os.environ.get("CODING_WORKSPACE_RAW", "/raw"))
SCRIPTS: dict[str, list] = {}
PALETTES: list[dict] = []


def git(repo, *args, check=True, env=None):
    base = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "f@invalid", "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "f@invalid"}
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check, env={**base, **(env or {})})


def evidence(name, obj):
    try:
        RAW.mkdir(parents=True, exist_ok=True)
        (RAW / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))
    except OSError:
        pass


@pytest.fixture(scope="session")
def env():
    if "api.dependencies" in sys.modules:
        # Another conftest already composed the app on its own data root; the
        # DB singletons are bound to it, so every record must agree with it.
        home = Path(os.environ["AITELIER_HOME"])
    else:
        home = Path(FIXTURE) / ("h-" + uuid.uuid4().hex[:8])
        home.mkdir(parents=True)
        os.environ["AITELIER_HOME"] = str(home)
    os.environ["AITELIER_ZVEC_LIFECYCLE"] = "0"
    os.environ.pop("AITELIER_HOST_LAUNCHER_SOCKET", None)
    from fastapi.testclient import TestClient
    import skillflow
    from api.main import app
    from api.dependencies import get_db_manager, get_skillflow
    from core import coding_workspace as cw, meta_agent
    from core.meta_agent import MetaAgent

    async def scripted(self, messages):
        PALETTES.append({"session": self.session_id,
                         "tools": [d["function"]["name"] for d in self._tool_palette()]})
        turn = SCRIPTS[self.session_id].pop(0)
        if callable(turn):
            turn = await turn() if asyncio.iscoroutinefunction(turn) else turn()
        if isinstance(turn, str):
            yield {"_type": "text_delta", "content": turn}
            yield {"_type": "collected", "text": turn, "tool_calls": [], "usage": {}}
            return
        calls = [{"id": f"call-{uuid.uuid4().hex[:8]}", "name": n, "args": a} for n, a in turn]
        yield {"_type": "collected", "text": "", "tool_calls": calls, "usage": {}}

    MetaAgent._stream_llm = scripted
    app.state._test_mode = True
    identity = {"python": sys.version, "skillflow": skillflow.__file__,
                "skillflow_sha256": hashlib.sha256(Path(skillflow.__file__).read_bytes()).hexdigest(),
                "skillflow_core_sha256": hashlib.sha256(
                    (Path(skillflow.__file__).parent / "core.py").read_bytes()).hexdigest(),
                "coding_workspace": cw.__file__, "meta_agent": meta_agent.__file__,
                "modules": {m: hashlib.sha256(Path(f"/src/{m}").read_bytes()).hexdigest()
                            for m in ("core/coding_workspace.py", "core/meta_agent.py",
                                      "core/closed_git.py", "core/run_resources.py",
                                      "core/skillflow_host.py", "api/agent_routers.py",
                                      "core/coding_shell.py", "core/run_isolation.py",
                                      "configs/coding_workspace.yaml")},
                "home": str(home), "sys_path": sys.path[:6]}
    evidence("runtime-identity", identity)
    with TestClient(app) as client:
        yield {"client": client, "db": get_db_manager(), "sf": get_skillflow(), "home": home,
               "cw": cw}
    app.state._test_mode = False


@pytest.fixture
def project(env):
    """A real existing-repo project with a dirty Source (tracked edit, staged, untracked)."""
    def make(files=None, dirty=True):
        pid = "cwi-" + uuid.uuid4().hex[:10]
        src = Path(FIXTURE) / "repos" / pid
        src.mkdir(parents=True)
        git(src, "init", "-q", "-b", "main")
        files = files or {"hello.py": "print('helo world')\n", "notes.md": "notes\n",
                          "lib/util.py": "def two():\n    return 2\n"}
        for rel, text in files.items():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            (src / rel).write_text(text)
        git(src, "add", "-A")
        git(src, "commit", "-qm", "base")
        if dirty:
            (src / "notes.md").write_text("notes\nstaged line\n")
            git(src, "add", "notes.md")
            (src / "scratch.txt").write_text("user untracked scratch\n")
        env["db"].ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
        return pid, src
    return make


def session(env) -> str:
    r = env["client"].post("/api/agent/session/create")
    assert r.status_code == 200
    return r.json()["session_id"]


def chat(env, sid, pid, turns, message="go", commit=None):
    SCRIPTS[sid] = list(turns)
    body = {"message": message, "session_id": sid, "current_project": pid, "mode": "coding"}
    if commit:
        body["commit_message"] = commit
    r = env["client"].post("/api/agent/chat", json=body)
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert SCRIPTS[sid] == [], f"script not consumed: {SCRIPTS[sid]}"
    return events


def results(events, name=None):
    return [e["result"] for e in events if e.get("type") == "tool_result"
            and (name is None or e["name"] == name)]


def handoff(events):
    found = [e for e in events if e.get("type") == "coding_handoff"]
    assert len(found) == 1, events
    return found[0]


def index_bytes(src):
    return (Path(src) / ".git" / "index").read_bytes()


def origin_of(env, sid):
    from core import coding_workspace as cw
    owner = cw._session_owner(None, sid)
    return owner


# ── 1. fresh-session trivial edit: one tree, automatic no-commit handoff ─────

def test_trivial_edit_one_tree_no_commit_handoff(env, project):
    pid, src = project()
    sid = session(env)
    head = git(src, "rev-parse", "HEAD").stdout.strip()
    before_index = index_bytes(src)
    events = chat(env, sid, pid, [
        [("list_code_tree", {}), ("read_code_file", {"path": "hello.py"})],
        [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
        [("search_code", {"pattern": "hello world"}), ("read_code_file", {"path": "scratch.txt"})],
        "Fixed the typo.",
    ])
    tree, read = results(events, "list_code_tree")[0], results(events, "read_code_file")[0]
    assert tree["view"] == "working_tree" and "hello.py" in tree["tree"] and "scratch.txt" in tree["tree"]
    assert "helo" in read["content"]
    assert results(events, "edit_file")[0] == {"edited": "hello.py", "view": "working_tree"}
    search = results(events, "search_code")[0]
    assert search["matches"][0]["file"] == "hello.py"
    assert search["index"].startswith("semantic index disabled for coding_workspace")
    seeded = results(events, "read_code_file")[1]
    assert "user untracked scratch" in seeded["content"]  # dirty Source seeded into W
    out = handoff(events)
    assert out["status"] == "applied" and out["paths"] == ["hello.py"] and out["commit"] is None
    assert (src / "hello.py").read_text() == "print('hello world')\n"
    assert git(src, "rev-parse", "HEAD").stdout.strip() == head
    assert index_bytes(src) == before_index  # staged notes.md untouched, nothing staged
    assert (src / "scratch.txt").read_text() == "user untracked scratch\n"
    rd, origin = origin_of(env, sid)
    run = env["sf"].get_run(origin["run_id"])
    assert origin["state"] == "closed" and run["graph_name"] == "coding_workspace"
    assert run["status"] == "completed"
    from core import run_isolation as ri
    rec = ri.record(env["db"], origin["run_id"])
    assert rec["mode"] == "worktree" and rec["worktree_path"] == origin["isolation"]["worktree_path"]
    assert ri.write_admissions(env["db"]) == []
    palette = [p for p in PALETTES if p["session"] == sid]
    from core.meta_agent import CODING_PALETTE
    assert palette and all(set(p["tools"]) == set(CODING_PALETTE) for p in palette)
    for forbidden in ("approve_checkpoint", "runner_approve", "runner_reject", "start_config_run",
                      "generate_pipeline", "drive_pipeline", "config_edit", "state_graph_write",
                      "tool_list", "generate_addon", "stop_pipeline", "repin_run"):
        assert forbidden not in palette[0]["tools"]
    view = env["client"].get("/api/agent/coding/view", params={"session_id": sid}).json()
    assert view["view"] == "source" and view["handoffs"][0]["state"] == "settled"
    evidence("t1-trivial", {"events": events, "origin": origin, "view": view, "palette": palette[0]})


# ── 2. palette is enforced at dispatch; model scope selects nothing ──────────

def test_dispatch_refuses_escape_tools_and_foreign_scope(env, project):
    pid, src = project()
    other, other_src = project()
    sid = session(env)
    before = (other_src / "hello.py").read_bytes()
    events = chat(env, sid, pid, [
        [("approve_checkpoint", {"project_id": pid}), ("runner_approve", {"run_id": "x"}),
         ("start_config_run", {"config_name": "coding_impl", "seed_text": "x"}),
         ("generate_pipeline", {"description": "x"}), ("state_graph_write", {}),
         ("config_edit", {"config_name": "coding_workspace"}), ("tool_list", {})],
        [("read_code_file", {"path": "hello.py", "project_id": other}),
         ("read_code_file", {"path": "../" + other + "/hello.py"}),
         ("read_code_file", {"path": ".git/config"}),
         ("runner_start", {"task": "x", "graph_name": "coding_workspace"})],
        "Done.",
    ])
    res = results(events)
    assert all("not available in Coding Mode" in r["error"] for r in res[:7]), res[:7]
    assert "fixed by the authenticated coding session" in res[7]["error"]
    assert "Path traversal denied" in res[8]["error"]
    assert "metadata" in res[9]["error"]
    assert "only through coding_task" in res[10]["error"]
    assert (other_src / "hello.py").read_bytes() == before
    assert handoff(events)["status"] in ("no_changes", "closed")
    evidence("t2-dispatch", {"results": res})


# ── 3. one originating session per project; distinct projects parallel ──────

def test_other_session_busy_and_distinct_projects_parallel(env, project):
    pid, src = project()
    pid2, src2 = project()
    a, b = session(env), session(env)
    # A starts a governing plan: an unresolved owner of pid.
    events = chat(env, a, pid, [
        [("runner_start", {"task": "rename helper"})],
        [("runner_submit", {"step_id": "plan", "result": {"plan": "1. rename two()"}})],
        "Plan submitted for your approval.",
    ])
    assert results(events, "runner_submit")[0]["status"] == "paused"
    assert handoff(events)["status"] == "retained"
    # B on the same project is busy — reads included; never adopts A's tree.
    ev_b = chat(env, b, pid, [[("read_code_file", {"path": "hello.py"})], "ok"])
    r = results(ev_b, "read_code_file")[0]
    assert r["status"] == "busy" and "another Coding Mode session" in r["error"]
    # B on a different project works in parallel while A stays unresolved.
    ev_b2 = chat(env, b, pid2, [
        [("read_code_file", {"path": "hello.py"})],
        [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})], "done"])
    assert handoff(ev_b2)["status"] == "applied"
    assert (src2 / "hello.py").read_text() == "print('hello world')\n"
    assert (src / "hello.py").read_text() == "print('helo world')\n"
    evidence("t3-sessions", {"a": events, "b_busy": ev_b, "b_parallel": ev_b2})


def test_overlapping_request_is_busy(env, project):
    """A real second request while the first holds the project request lock."""
    pid, src = project()
    sid = session(env)
    entered, release = threading.Event(), threading.Event()

    async def hold():
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.05)
        return "first request done"

    out = {}
    SCRIPTS[sid] = [[("read_code_file", {"path": "hello.py"})], hold]

    def first():
        body = {"message": "1", "session_id": sid, "current_project": pid, "mode": "coding"}
        out["r"] = env["client"].post("/api/agent/chat", json=body)
    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(60)
    sid2 = session(env)
    try:
        # Same project, other session: busy. Same session, overlapping: busy.
        ev = chat(env, sid2, pid, [[("read_code_file", {"path": "hello.py"})], "x"])
        assert results(ev, "read_code_file")[0]["status"] == "busy"
        from core import coding_workspace as cw
        with pytest.raises(cw.Busy):
            cw.ProjectLock(pid).acquire()
    finally:
        release.set()
        t.join(60)
    assert out["r"].status_code == 200
    evidence("t3-overlap", {"busy": ev})


# ── 4. governing plan gates writes/shell/handoff; exact user approval ────────

def test_governing_plan_gate_reload_reject_stale_and_exact_approval(env, project):
    pid, src = project()
    sid = session(env)
    events = chat(env, sid, pid, [
        [("read_code_file", {"path": "lib/util.py"}), ("runner_start", {"task": "make two return 3"})],
        [("runner_submit", {"step_id": "plan", "result": {"plan": "PLAN-V1: return 3"}})],
        [("edit_file", {"path": "lib/util.py", "old_str": "2", "new_str": "3"}),
         ("create_file", {"path": "new.py", "content": "x"}),
         ("bash", {"command": "echo hi > shell.txt"}),
         ("read_code_file", {"path": "lib/util.py"})],
        "Waiting for approval.",
    ])
    gated = results(events)[3:6]
    assert all(r.get("status") == "plan_gate" for r in gated), gated
    assert "return 2" in results(events, "read_code_file")[1]["content"]  # reads still work
    assert handoff(events)["status"] == "retained"
    _, origin = origin_of(env, sid)
    plan_rid = json.loads((origin_of(env, sid)[0] / "plan.json").read_text())["run_id"]
    sf = env["sf"]
    from skillflow.exceptions import SkillFlowError
    # Generic routes cannot decide the governing plan.
    for call in (lambda: sf.approve_checkpoint(plan_rid), lambda: sf.stop_run(plan_rid, "x"),
                 lambda: sf.fail_run(plan_rid, "x"),
                 lambda: sf.reject_checkpoint(plan_rid, "plan", "no")):
        with pytest.raises(SkillFlowError, match="Coding Mode"):
            call()
    from skillflow.plugins.skill_runner import RunnerService
    fresh, _ = project()
    with pytest.raises(SkillFlowError, match="Coding Mode"):
        RunnerService(sf).start("coding_workspace", project_id=fresh)
    assert [r for r in sf.list_runs(project_id=fresh) if r["graph_name"] == "coding_workspace"] == []
    # Reload: a fresh request still gates.
    ev2 = chat(env, sid, pid, [[("edit_file", {"path": "lib/util.py", "old_str": "2",
                                               "new_str": "3"})], "still waiting"])
    assert results(ev2)[0]["status"] == "plan_gate"
    # Another session cannot escape the unresolved plan.
    other = session(env)
    ev3 = chat(env, other, pid, [[("read_code_file", {"path": "hello.py"})], "x"])
    assert results(ev3)[0]["status"] == "busy"
    client = env["client"]
    view = client.get("/api/agent/coding/view", params={"session_id": sid}).json()
    cp = view["plan"]["checkpoint"]
    assert view["plan"]["state"] == "pending" and cp["plan"] == "PLAN-V1: return 3"
    # Reject with feedback → replan → the old token is stale.
    r = client.post("/api/agent/coding/plan-decision", json={
        "session_id": sid, "decision": "reject", "feedback": "also add a test"})
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    ev4 = chat(env, sid, pid, [
        [("runner_submit", {"step_id": "plan", "result": {"plan": "PLAN-V2: return 3 + test"}})],
        "Revised plan submitted."])
    assert results(ev4)[0]["status"] == "paused"
    stale = client.post("/api/agent/coding/plan-decision", json={
        "session_id": sid, "decision": "approve", "expected_checkpoint": cp["expected_checkpoint"],
        "decision_id": "stale-" + uuid.uuid4().hex[:8]}).json()
    assert stale["status"] == "refused"
    view2 = client.get("/api/agent/coding/view", params={"session_id": sid}).json()
    assert view2["plan"]["checkpoint"]["plan"] == "PLAN-V2: return 3 + test"
    did = "dec-" + uuid.uuid4().hex[:8]
    body = {"session_id": sid, "decision": "approve", "decision_id": did,
            "expected_checkpoint": view2["plan"]["checkpoint"]["expected_checkpoint"]}
    ok = client.post("/api/agent/coding/plan-decision", json=body).json()
    assert ok["status"] == "applied", ok
    replay = client.post("/api/agent/coding/plan-decision", json=body).json()
    assert replay["status"] == "applied" and replay["replayed"] is True
    # Approved: implement in W, submit, hand off.
    ev5 = chat(env, sid, pid, [
        [("read_code_file", {"path": "lib/util.py"})],
        [("edit_file", {"path": "lib/util.py", "old_str": "return 2", "new_str": "return 3"}),
         ("create_file", {"path": "test_util.py", "content": "from lib.util import two\n"})],
        [("runner_submit", {"step_id": "implement", "result": {"summary": "two() returns 3"}})],
        "Implemented."])
    assert results(ev5, "edit_file")[0]["edited"] == "lib/util.py"
    out = handoff(ev5)
    assert out["status"] == "applied" and sorted(out["paths"]) == ["lib/util.py", "test_util.py"]
    assert (src / "lib/util.py").read_text() == "def two():\n    return 3\n"
    assert sf.get_run(plan_rid)["status"] == "completed"
    trace = [t for t in sf.get_trace(plan_rid, category="step") if t["event"] == "checkpoint_approved"]
    evidence("t4-plan", {"gated": gated, "view": view, "stale": stale, "approve": ok,
                         "replay": replay, "handoff": out, "approval_trace": trace})


def test_dismissal_closes_without_delivery_and_frees_project(env, project):
    pid, src = project()
    sid = session(env)
    chat(env, sid, pid, [[("runner_start", {"task": "big refactor"})],
                         [("runner_submit", {"step_id": "plan", "result": {"plan": "P"}})], "wait"])
    r = env["client"].post("/api/agent/coding/plan-decision",
                           json={"session_id": sid, "decision": "dismiss"}).json()
    assert r["status"] == "applied" and r["dismissed"]
    _, origin = origin_of(env, sid)
    assert origin["state"] == "dismissed"
    assert Path(origin["isolation"]["worktree_path"]).is_dir()  # retained evidence
    # A later unrelated request starts fresh from Source (no planless continuation).
    other = session(env)
    ev = chat(env, other, pid, [[("read_code_file", {"path": "hello.py"})],
                                [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
                                "fixed"])
    assert handoff(ev)["status"] == "applied"
    evidence("t4-dismiss", {"decision": r, "origin": origin})


# ── 5. unrelated historical plan does not gate later trivial work ────────────

def test_historical_plan_does_not_gate_next_trivial_request(env, project):
    pid, src = project()
    sid = session(env)
    chat(env, sid, pid, [[("runner_start", {"task": "t"})],
                         [("runner_submit", {"step_id": "plan", "result": {"plan": "P1"}})], "wait"])
    view = env["client"].get("/api/agent/coding/view", params={"session_id": sid}).json()
    env["client"].post("/api/agent/coding/plan-decision", json={
        "session_id": sid, "decision": "approve",
        "expected_checkpoint": view["plan"]["checkpoint"]["expected_checkpoint"]})
    ev = chat(env, sid, pid, [[("runner_submit", {"step_id": "implement",
                                                  "result": {"summary": "nothing needed"}})], "done"])
    assert handoff(ev)["status"] in ("no_changes", "applied")
    # Same used session, new trivial request: a NEW owner without any plan.
    ev2 = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                               [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
                               "typo fixed"])
    assert results(ev2, "edit_file")[0]["edited"] == "hello.py"
    assert handoff(ev2)["status"] == "applied"
    evidence("t5-history", {"second": ev2})


# ── 6. Source conflict changes nothing and retains W ──────────────────────────

def test_source_conflict_retains_w_and_changes_nothing(env, project):
    pid, src = project()
    sid = session(env)

    def user_edits_source():
        (src / "hello.py").write_text("print('user changed it')\n")
        return "Done."
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                              [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
                              user_edits_source])
    out = handoff(ev)
    assert out["status"] == "conflict" and out["conflicts"] == ["hello.py"]
    assert (src / "hello.py").read_text() == "print('user changed it')\n"
    from core import run_isolation as ri
    assert ri.write_admissions(env["db"]) == []
    _, origin = origin_of(env, sid)
    assert origin["state"] == "ready"
    f = env["client"].get("/api/agent/coding/file", params={"session_id": sid, "path": "hello.py"}).json()
    assert f["view"] == "working_tree" and f["content"] == "print('hello world')\n"
    stale = env["client"].get("/api/agent/coding/file", params={
        "session_id": sid, "path": "hello.py", "run_id": "other-run"})
    assert stale.status_code == 409
    # The user restores Source; the next final response delivers.
    (src / "hello.py").write_text("print('helo world')\n")
    ev2 = chat(env, sid, pid, ["delivering"])
    assert handoff(ev2)["status"] == "applied"
    assert (src / "hello.py").read_text() == "print('hello world')\n"
    evidence("t6-conflict", {"conflict": out, "file_view": f})


# ── 7. crash after the first Source effect; recovery; third state ─────────────

def _crash_after_first_source_write(monkeypatch, src):
    from core import closed_git
    real = closed_git.WorkTree.write
    count = {"n": 0}

    def write(self, rel, mode, data, **kw):
        result = real(self, rel, mode, data, **kw)
        if str(self.root) == str(src):
            count["n"] += 1
            if count["n"] == 1:
                raise OSError("INJECTED: process interrupted after the first Source write")
        return result
    monkeypatch.setattr(closed_git.WorkTree, "write", write)


def test_partial_handoff_retains_admission_and_recovers_forward(env, project, monkeypatch):
    pid, src = project()
    sid = session(env)
    turns = [[("read_code_file", {"path": "hello.py"}), ("read_code_file", {"path": "lib/util.py"})],
             [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"}),
              ("edit_file", {"path": "lib/util.py", "old_str": "2", "new_str": "22"})], "done"]
    with monkeypatch.context() as m:
        _crash_after_first_source_write(m, src)
        ev = chat(env, sid, pid, turns)
    out = handoff(ev)
    assert out["status"] == "error" and "INJECTED" in out["error"]
    from core import run_isolation as ri
    rows = ri.write_admissions(env["db"])
    assert len(rows) == 1 and rows[0]["kind"] == "coding_workspace" and "PENDING WRITERS" in rows[0]["detail"]
    rd, origin = origin_of(env, sid)
    intent = json.loads(next((rd / "handoffs").glob("*/intent.json")).read_text())
    assert origin["state"] == "handoff_pending" and intent["state"] == "applying" and intent["interrupted"]
    applied = [p for p in ("hello.py", "lib/util.py")
               if (src / p).read_text() != {"hello.py": "print('helo world')\n",
                                             "lib/util.py": "def two():\n    return 2\n"}[p]]
    assert len(applied) == 1  # exactly the first effect landed
    # Another session cannot bypass the unresolved handoff.
    ev_other = chat(env, session(env), pid, [[("read_code_file", {"path": "hello.py"})], "x"])
    assert results(ev_other)[0]["status"] == "busy"
    # Recovery: same session, writer positively quiet (lock re-acquired), rolls forward.
    ev2 = chat(env, sid, pid, ["resume"])
    recovered = handoff(ev2)
    assert recovered["status"] == "no_changes", recovered  # nothing left after roll-forward
    assert recovered["recovered"][0]["state"] == "settled"
    assert (src / "hello.py").read_text() == "print('hello world')\n"
    assert (src / "lib/util.py").read_text() == "def two():\n    return 22\n"
    assert ri.write_admissions(env["db"]) == []
    evidence("t7-partial", {"interrupted": out, "pending_row": rows[0], "intent": intent,
                            "recovered": handoff(ev2)})


def test_partial_handoff_third_state_stays_ambiguous(env, project, monkeypatch):
    pid, src = project()
    sid = session(env)
    turns = [[("read_code_file", {"path": "hello.py"}), ("read_code_file", {"path": "lib/util.py"})],
             [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"}),
              ("edit_file", {"path": "lib/util.py", "old_str": "2", "new_str": "22"})], "done"]
    with monkeypatch.context() as m:
        _crash_after_first_source_write(m, src)
        chat(env, sid, pid, turns)
    # Someone edits the not-yet-applied path to a third state.
    pending = [p for p, base in (("hello.py", "print('helo world')\n"),
                                 ("lib/util.py", "def two():\n    return 2\n"))
               if (src / p).read_text() == base]
    (src / pending[0]).write_text("THIRD STATE\n")
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})], "x"])
    r = results(ev)[0]
    assert r["status"] == "unavailable" and "ambiguous" in r["error"]
    assert (src / pending[0]).read_text() == "THIRD STATE\n"
    from core import run_isolation as ri
    assert len(ri.write_admissions(env["db"])) == 1  # retained with all evidence
    rd, _ = origin_of(env, sid)
    intent = json.loads(next((rd / "handoffs").glob("*/intent.json")).read_text())
    assert intent["state"] == "ambiguous" and intent["conflicts"] == [pending[0]]
    evidence("t7-third-state", {"result": r, "intent": intent})
    # Fixture settlement only (this project is abandoned by the test).
    row = ri.write_admissions(env["db"])[0]
    ri.clear_write_admission(env["db"], row["id"], evidence="test fixture: ambiguous handoff "
                             "case measured; project discarded")


# ── 8. optional commit under the same admission; ineligible; crash at index ──

def test_optional_commit_same_admission_no_hooks(env, project):
    pid, src = project()
    hooks = src / ".git" / "hooks"
    sentinel = Path(FIXTURE) / ("hook-ran-" + pid)
    for h in ("pre-commit", "post-commit", "commit-msg", "post-index-change", "reference-transaction"):
        (hooks / h).write_text(f"#!/bin/sh\necho {h} >> {sentinel}\n")
        (hooks / h).chmod(0o755)
    git(src, "config", "core.hooksPath", str(hooks))
    sid = session(env)
    old_head = git(src, "rev-parse", "HEAD").stdout.strip()
    staged_before = git(src, "ls-files", "-s", "notes.md").stdout
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                              [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
                              "done"], commit="Fix greeting typo")
    out = handoff(ev)
    assert out["status"] == "applied" and out["commit"]["status"] == "committed", out
    new_head = git(src, "rev-parse", "HEAD").stdout.strip()
    assert new_head == out["commit"]["commit"] != old_head
    assert git(src, "rev-parse", "HEAD^").stdout.strip() == old_head
    changed = git(src, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.split()
    assert changed == ["hello.py"]  # only the delivered delta, not the staged notes.md
    assert git(src, "ls-files", "-s", "notes.md").stdout == staged_before  # still staged
    assert git(src, "show", "HEAD:hello.py").stdout == "print('hello world')\n"
    assert not sentinel.exists()
    evidence("t8-commit", {"handoff": out, "changed": changed})


def test_commit_unavailable_for_dirty_base_but_working_bytes_delivered(env, project):
    pid, src = project()
    (src / "hello.py").write_text("print('helo world')  # user wip\n")
    sid = session(env)
    old_head = git(src, "rev-parse", "HEAD").stdout.strip()
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                              [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})],
                              "done"], commit="should not fold wip")
    out = handoff(ev)
    assert out["status"] == "applied" and out["commit"]["status"] == "unavailable"
    assert "already differed from HEAD" in out["commit"]["reason"]
    assert (src / "hello.py").read_text() == "print('hello world')  # user wip\n"
    assert git(src, "rev-parse", "HEAD").stdout.strip() == old_head
    evidence("t8-ineligible", {"handoff": out})


def test_commit_crash_after_ref_cas_recovers_index(env, project, monkeypatch):
    pid, src = project()
    sid = session(env)
    from core import closed_git
    real = closed_git._install_after_ref

    def crash(lock, head):
        raise OSError("INJECTED: interrupted after ref CAS, before index installation")
    with monkeypatch.context() as m:
        m.setattr(closed_git, "_install_after_ref", crash)
        ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                                  [("edit_file", {"path": "hello.py", "old_str": "helo",
                                                  "new_str": "hello"})], "done"], commit="Fix")
    assert handoff(ev)["status"] == "error"
    rd, _ = origin_of(env, sid)
    intent = json.loads(next((rd / "handoffs").glob("*/intent.json")).read_text())
    journal = intent["commit_journal"]
    assert intent["state"] == "applied" and journal["state"] == "ref_moved"
    assert git(src, "rev-parse", "HEAD").stdout.strip() == journal["commit"]
    assert (src / ".git" / "index.lock").exists()  # owned, not removed by an unknown path
    assert hashlib.sha256(index_bytes(src)).hexdigest() == journal["pre_index_sha256"]
    ev2 = chat(env, sid, pid, ["resume"])
    out = handoff(ev2)
    assert out["status"] == "no_changes", out
    rec = out["recovered"][0]
    assert rec["state"] == "settled" and rec["commit"]["index_installed_on_recovery"] is True
    assert hashlib.sha256(index_bytes(src)).hexdigest() == journal["staged_index_sha256"]
    assert not (src / ".git" / "index.lock").exists()
    from core import run_isolation as ri
    assert ri.write_admissions(env["db"]) == []
    evidence("t8-commit-crash", {"journal": journal, "recovered": out})


# ── 9. no-index policy across prepare/reconcile/search_lease/invalidate ──────

def test_no_index_policy_with_lifecycle_enabled(env, project, monkeypatch):
    pid, src = project()
    sid = session(env)
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "1")
    ev = chat(env, sid, pid, [[("search_code", {"pattern": "helo"})], "found"])
    assert results(ev)[0]["index"].startswith("semantic index disabled")
    from core import datadir, run_resources, run_isolation as ri
    _, origin = origin_of(env, sid)
    rec = ri.record(env["db"], origin["run_id"])
    run_resources.prepare(rec)
    run_resources.invalidate(rec["worktree_path"])
    run_resources.reconcile(env["db"], env["sf"], origin["run_id"])
    ledger = datadir.semantic_index_control_dir() / "control.sqlite3"
    if ledger.exists():
        import sqlite3
        rows = sqlite3.connect(ledger).execute("SELECT run_id FROM indexes WHERE run_id=?",
                                               (origin["run_id"],)).fetchall()
        assert rows == []
    assert not (Path(rec["worktree_path"]) / ".zvec-grep").exists()
    evidence("t9-no-index", {"search": results(ev)[0], "ledger_exists": ledger.exists()})


# ── 10. missing facility / validated grant / unknown settlement ──────────────

def _one_shot_launcher(path, reply_fn, seen):
    import socket
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            request = json.loads(buf.split(b"\n")[0])
            seen.append(request)
            conn.sendall((json.dumps(reply_fn(request)) + "\n").encode())
        srv.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_shell_missing_facility_validated_grant_and_unknown_retention(env, project, monkeypatch):
    """Owned shell controls WITHOUT starting an inner container.

    This author run holds ONE test-container slot, so the actual inner
    executor container (a second container) is not started here. The real
    host launcher validator checks the real grant against the real ledgers;
    the reply is a recorded validation-only refusal, labelled as such.
    """
    pid, src = project()
    sid = session(env)
    from core import coding_shell, repository_host_launcher as host, run_isolation as ri
    ev = chat(env, sid, pid, [[("bash", {"command": "echo x > made.txt"})], "x"])
    r = results(ev)[0]
    assert r["status"] == "unavailable" and "no local fallback" in r["error"]
    _, origin = origin_of(env, sid)
    w = Path(origin["isolation"]["worktree_path"])
    assert not (w / "made.txt").exists() and ri.write_admissions(env["db"]) == []
    entry = Path(coding_shell.__file__).with_name("repository_executor_entry.py")
    cfg = {"allowed_roots": sorted({FIXTURE, str(env["home"])}), "aitelier_home": str(env["home"]),
           "state_db": env["db"].db_path, "test_image": "sha256:" + "0" * 64,
           "executor_entry": str(entry), "allowed_uids": [os.getuid()],
           "coding_shell_engine_db": str(__import__("core.datadir", fromlist=["x"]).skillflow_db_path()),
           "coding_shell_entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
    seen, validated = [], []

    def validate_only(request):
        try:
            validated.append(host.validate_request(cfg, request))
            return {"ok": False, "error": "VALIDATION-ONLY test launcher: grant accepted, "
                                          "execution not performed", "physical_quiet": True}
        except host.LaunchRefused as exc:
            return {"ok": False, "error": f"refused: {exc}", "physical_quiet": True}
    sock = Path(FIXTURE) / f"v-{uuid.uuid4().hex[:6]}.sock"
    monkeypatch.setenv("AITELIER_HOST_LAUNCHER_SOCKET", str(sock))
    t = _one_shot_launcher(sock, validate_only, seen)
    ev2 = chat(env, sid, pid, [[("bash", {"command": "pytest -q"})], "x"])
    t.join(30)
    assert "VALIDATION-ONLY" in results(ev2)[0]["error"], results(ev2)
    # The previous interaction closed; this request bound a fresh owner.
    _, origin2 = origin_of(env, sid)
    w = Path(origin2["isolation"]["worktree_path"])
    assert origin2["run_id"] != origin["run_id"]
    assert validated and validated[0]["repo"] == str(w) and validated[0]["profile"] == "coding_shell"
    assert seen[0]["coding_grant"]["repo"] == str(w)
    assert seen[0]["coding_grant"]["source_repo"] == str(src)
    assert ri.write_admissions(env["db"]) == []  # definite refusal → retired
    # Unknown physical settlement: the W admission is RETAINED and blocks handoff.
    sock2 = Path(FIXTURE) / f"u-{uuid.uuid4().hex[:6]}.sock"
    monkeypatch.setenv("AITELIER_HOST_LAUNCHER_SOCKET", str(sock2))
    t2 = _one_shot_launcher(sock2, lambda req: {"ok": False, "error": "SIMULATED unknown CREATE",
                                                "physical_quiet": False}, [])
    ev3 = chat(env, sid, pid, [[("bash", {"command": "sleep 1"})], "done"])
    t2.join(30)
    rows = ri.write_admissions(env["db"])
    assert len(rows) == 1 and "PENDING WRITERS" in rows[0]["detail"]
    out = handoff(ev3)
    assert out["status"] == "unavailable"
    assert "not positively" in out["error"] or "unsettled" in out["error"]
    evidence("t10-shell", {"missing": r, "validated_grant": validated[0] if validated else None,
                           "request": seen[0] if seen else None, "unknown_row": rows[0],
                           "handoff": out, "label": "validation-only; no inner container"})
    ri.clear_write_admission(env["db"], rows[0]["id"],
                             evidence="test fixture: simulated unknown CREATE never dispatched Docker")


# ── 11. restart: positive quiet reclaim; interrupted creation never retried ──

CHILD = r'''
import json, os, sys
os.environ["AITELIER_HOME"] = sys.argv[1]
from api.dependencies import get_db_manager, get_skillflow
from core import coding_workspace as cw
db, sf = get_db_manager(), get_skillflow()
scope = cw.authorize_entry(db, session_id=sys.argv[2], project_id=sys.argv[3], writer="child")
req = cw.CodingRequest(db, scope)
mode = sys.argv[4]
if mode == "crash_before_bind":
    real = sf.start_run
    def start_and_die(rid):
        os._exit(17)
    sf.start_run = start_and_die
ws = req.workspace()
cw.write_text(db, ws, "hello.py", "print('hello from the old backend')\n", create=False, detail="child")
print(json.dumps({"run_id": ws.run_id, "claim": cw._token_dict(ws.claimed)}), flush=True)
os._exit(0)
'''


def _child(env, sid, pid, mode):
    script = Path(FIXTURE) / f"child-{uuid.uuid4().hex[:6]}.py"
    script.write_text(CHILD)
    return subprocess.run([sys.executable, str(script), str(env["home"]), sid, pid, mode],
                          capture_output=True, text=True, timeout=300, cwd="/src",
                          env={**os.environ, "AITELIER_HOME": str(env["home"])})


def test_restart_reclaims_same_run_after_positive_quiet(env, project):
    pid, src = project()
    sid = session(env)
    done = _child(env, sid, pid, "normal")
    assert done.returncode == 0, done.stderr[-3000:]
    old = json.loads(done.stdout.strip().splitlines()[-1])
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})], "resumed"])
    assert "old backend" in results(ev)[0]["content"]  # the SAME W, edit retained
    _, origin = origin_of(env, sid)
    assert origin["run_id"] == old["run_id"]
    assert origin["claim_history"][-1] == old["claim"]
    assert handoff(ev)["status"] == "applied"
    assert (src / "hello.py").read_text() == "print('hello from the old backend')\n"
    evidence("t11-restart", {"old": old, "origin": origin})


def test_interrupted_creation_is_settled_not_retried_or_adopted(env, project):
    pid, src = project()
    sid = session(env)
    crashed = _child(env, sid, pid, "crash_before_bind")
    assert crashed.returncode == 17
    other = session(env)
    ev = chat(env, other, pid, [[("read_code_file", {"path": "hello.py"})], "x"])
    assert results(ev)[0]["status"] == "busy"  # another session never decides it
    ev2 = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})], "ok"])
    assert "helo" in results(ev2)[0]["content"]
    creations = list((Path(env["home"]) / "native_context" / "coding_workspace").rglob("creations/*.json"))
    states = sorted(json.loads(p.read_text())["state"] for p in creations
                    if json.loads(p.read_text())["session_id"] == sid)
    assert states == ["bound", "orphan_settled"]
    orphan = [json.loads(p.read_text()) for p in creations
              if json.loads(p.read_text())["state"] == "orphan_settled"][0]
    assert env["sf"].get_run(orphan["run_id"])["status"] in ("failed", "cancelled", "completed")
    evidence("t11-creation", {"orphan": orphan, "states": states})


# ── 12. the authenticated gate is on the new routes ─────────────────────────

def test_new_routes_are_writer_gated(env, monkeypatch):
    from api import authz, main
    from api.main import app
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    # Let the localhost-only middleware pass so the WRITER gate is what answers.
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.state._test_mode = False
    try:
        for method, url, kw in (("get", "/api/agent/coding/view", {"params": {"session_id": "x"}}),
                                ("post", "/api/agent/coding/plan-decision",
                                 {"json": {"session_id": "x", "decision": "approve"}}),
                                ("post", "/api/agent/chat", {"json": {"message": "x", "mode": "coding"}})):
            r = getattr(env["client"], method)(url, **kw)
            assert r.status_code == 403, (url, r.status_code, r.text)
            assert "Not signed in" in r.text, r.text
    finally:
        app.state._test_mode = True


# ── 13. budget pause keeps the exact R/W; no reclaim without positive quiet ──

def test_budget_pause_reuses_exact_run_and_tree(env, project, monkeypatch):
    import core.meta_agent as ma
    real = ma._load_meta_agent_config
    monkeypatch.setattr(ma, "_load_meta_agent_config",
                        lambda *a, **k: {**real(*a, **k), "coding_max_tool_turns": 2})
    pid, src = project()
    sid = session(env)
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                              [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hello"})]])
    assert ev[-1]["type"] == "budget_exhausted"
    assert not [e for e in ev if e.get("type") == "coding_handoff"]
    _, first = origin_of(env, sid)
    assert first["state"] == "ready"
    assert (src / "hello.py").read_text() == "print('helo world')\n"  # not delivered yet
    from core import coding_workspace as cw
    cw.ProjectLock(pid).acquire().release()  # the request lock was released at the pause
    ev2 = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})], "continued and done"],
               message="continue")
    assert "hello world" in results(ev2)[0]["content"]
    _, second = origin_of(env, sid)
    assert second["run_id"] == first["run_id"] and second["claim"] == first["claim"]
    assert handoff(ev2)["status"] == "applied"
    evidence("t13-budget", {"first": first, "second": second})


def test_lost_claim_with_live_owner_is_not_reclaimed(env, project, monkeypatch):
    import core.meta_agent as ma
    real = ma._load_meta_agent_config
    monkeypatch.setattr(ma, "_load_meta_agent_config",
                        lambda *a, **k: {**real(*a, **k), "coding_max_tool_turns": 2})
    pid, src = project()
    sid = session(env)
    from core import coding_workspace as cw
    ev = chat(env, sid, pid, [[("read_code_file", {"path": "hello.py"})],
                              [("edit_file", {"path": "hello.py", "old_str": "helo", "new_str": "hey"})]])
    assert ev[-1]["type"] == "budget_exhausted"
    _drop_claims()  # the process-held claim is gone, but its owner (us) is alive
    ev2 = chat(env, sid, pid, ["done"])
    out = handoff(ev2)
    assert out["status"] == "unavailable" and "alive or unknown" in out["error"], out
    assert (src / "hello.py").read_text() == "print('helo world')\n"
    _, origin = origin_of(env, sid)
    assert origin["state"] == "ready" and "claim_history" not in origin
    cw._claim_put(origin["run_id"], _SAVED_CLAIMS[origin["run_id"]])
    ev3 = chat(env, sid, pid, ["done"])
    assert handoff(ev3)["status"] == "applied"
    evidence("t13-live-owner", {"refusal": out})


_SAVED_CLAIMS = {}


def _drop_claims():
    from core import coding_workspace as cw
    with cw._CLAIMS_LOCK:
        _SAVED_CLAIMS.update(cw._CLAIMS)
        cw._CLAIMS.clear()
