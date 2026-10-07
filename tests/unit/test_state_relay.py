"""A relayed State attempt inherits a failed attempt's commits AND staged draft.

Live audit 2026-09-10: 20 of 22 failed attempts died on the implement step's
turn/output budget with a draft retained in `implement.tmp/` that the next
attempt could not reach (fresh worktree from HEAD, seed without prior-attempt
context, read tools jailed to the new worktree). The one in-engine relay that
named the old path in its instruction re-grounded from scratch and failed again.
These tests bind the channel that now exists: request_base + parked draft +
seed_relay_draft, driven by `continue_from`.
"""
import hashlib
import subprocess
from pathlib import Path

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_graph import StateConflict, StateGraphStore
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.db"))
    store = StateGraphStore(db, project_read_trusted=True)
    store.create_project("game", "Long-running game")
    store.add_nodes("game", [{"key": "a", "goal": "Implement a", "dependencies": [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour validated"}]}])
    attempts = StateAttempts(store)
    # A real SkillFlow 1.5.82, initialized with an owned tmp workspace so
    # `sf._workspace` is a real WorkspaceManager: the launch path reaches
    # run_launcher.missing_cross_config_inputs, which resolves the project's
    # context root through `sf._workspace.get_project_path(project_id)`. Built
    # without workspace_base that attribute is None and the check dies before
    # any dependency admission. projects_base is pinned too (optional) so the
    # SkillFlow resolver never falls back to a non-tmp location.
    sf = SkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "workspaces"),
                   projects_base=str(tmp_path / "projects"))
    sf.register_graph(PipelineGraph(name="feature", begin="implementation", steps=[StepNode(id="implementation")]))
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    src = tmp_path / "src"
    head = _init_repo(src)
    yield {"db": db, "store": store, "attempts": attempts, "sf": sf, "ws": ws, "src": src, "head": head,
           "service": StateService(db, ws, sf, {}, project_read_trusted=True)}
    sf._conn.close()


def _failed_attempt_with_draft(w):
    """A real failed run: one commit on its branch, one file left in staging."""
    attempts, sf, db, ws, src = w["attempts"], w["sf"], w["db"], w["ws"], w["src"]
    a = attempts.reserve("game", "a", 1, "feature", "first")
    pid = a["execution_project_id"]
    rid = sf.create_run("feature", project_id=pid)
    sf.start_run(rid)
    attempts.bind_run(a["attempt_id"], rid, sf)
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id=rid, project_id=pid, config_name="feature", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "done.py").write_text("DONE = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "first half"], cwd=wt, check=True)
    ws.write_draft(pid, "implementation", "half.py", "HALF = 1\n", graph_name="feature")
    sf.fail_run(rid, "Step implementation: native turn budget exhausted (32/32)")
    return attempts.reconcile(a["attempt_id"], sf), _git(wt, "rev-parse", "HEAD")


def test_failed_attempt_reports_what_it_left_behind(world):
    a, branch_head = _failed_attempt_with_draft(world)
    observed = world["service"].reconcile_attempt(a["attempt_id"])
    inv = observed["relay_inventory"]
    assert world["service"].get_attempt(a["attempt_id"])["relay_inventory"] == inv, "the read surface shows the same inventory"
    assert inv["branch"] == f"codex/run/{a['run_id']}"
    assert inv["head_sha"] == branch_head and inv["base_sha"] == world["head"]
    assert [c["subject"] for c in inv["commits"]] == ["first half"]
    assert inv["staged_files"] == {"implementation": {"half.py": _sha("HALF = 1\n")}}
    assert inv["mainline_ahead_by"] == 0
    assert "turn budget" in inv["error"]
    assert len(inv["digest"]) == 64


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_inventory_lists_only_regular_files_and_the_copy_follows_the_inventory(world, tmp_path):
    """A symlink in staging is neither listed nor copied — a link to a directory
    outside the staging used to be FOLLOWED by copytree while the inventory hid it."""
    attempts, ws, src = world["attempts"], world["ws"], world["src"]
    a, _ = _failed_attempt_with_draft(world)
    staging = ws._draft_dir(a["execution_project_id"], "implementation", "feature")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not staged\n")
    (staging / "linked_dir").symlink_to(outside, target_is_directory=True)
    (staging / "linked.py").symlink_to(staging / "half.py")
    (staging / "sub").mkdir()
    (staging / "sub" / "deep.py").write_text("DEEP = 1\n")

    inv = world["service"].get_attempt(a["attempt_id"])["relay_inventory"]
    assert inv["staged_files"] == {"implementation": {"half.py": _sha("HALF = 1\n"), "sub/deep.py": _sha("DEEP = 1\n")}}

    b = attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"], relay_digest=inv["digest"])
    relay = world["service"]._prepare_relay(b, str(src))
    parked = ws._relay_dir(b["execution_project_id"], "implementation", "feature")
    assert sorted(str(p.relative_to(parked)) for p in parked.rglob("*") if p.is_file()) == ["half.py", "sub/deep.py"]
    assert not (parked / "linked_dir").exists() and not (parked / "linked.py").exists()
    assert relay["staged_files"] == inv["staged_files"] and relay["digest"] == inv["digest"]


def test_relay_is_refused_when_the_draft_moved_since_it_was_read(world):
    attempts, ws, src = world["attempts"], world["ws"], world["src"]
    a, _ = _failed_attempt_with_draft(world)
    inv = world["service"].get_attempt(a["attempt_id"])["relay_inventory"]
    ws.write_draft(a["execution_project_id"], "implementation", "half.py", "HALF = 2\n", graph_name="feature")
    b = attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"], relay_digest=inv["digest"])
    with pytest.raises(StateConflict, match="changed since it was read"):
        world["service"]._prepare_relay(b, str(src))
    assert not ws._relay_dir(b["execution_project_id"], "implementation", "feature").exists()
    # Through the launcher the refusal retires the reservation, so the node is
    # not stranded behind it: a fresh reservation under a new key succeeds.
    from core.state_graph import StateGraphError
    with pytest.raises(StateGraphError, match="requires relay_digest"):
        world["service"].start_attempt("game", "a", 1, "feature", "relay3", continue_from=a["attempt_id"])
    from types import SimpleNamespace
    manifest = SimpleNamespace(seed_file="seed.md", output_step="implementation", scheduler_owned=True, repo_mode="code")
    with pytest.raises(StateConflict, match="reservation was retired"):
        world["service"]._launch_or_recover(b, manifest, str(src))
    assert attempts.get(b["attempt_id"])["status"] == "superseded"
    assert attempts.reserve("game", "a", 1, "feature", "relay4", continue_from=a["attempt_id"],
                            relay_digest=inv["digest"])["status"] == "reserved"
    with pytest.raises(Exception, match="64-hex"):
        attempts.reserve("game", "a", 1, "feature", "relay2", continue_from=a["attempt_id"], relay_digest="abc")


def test_park_copy_is_verified_against_the_manifest(world, tmp_path):
    """stage_relay_draft copies by manifest and refuses a file whose bytes differ."""
    ws = world["ws"]
    from core.workspace_manager import RelayDraftChanged
    src = tmp_path / "stage"
    src.mkdir()
    (src / "a.py").write_text("A = 1\n")
    manifest = ws.relay_manifest(src)
    assert manifest == {"a.py": _sha("A = 1\n")}
    (src / "a.py").write_text("A = 2\n")
    with pytest.raises(RelayDraftChanged, match="changed"):
        ws.stage_relay_draft(src, "p", "implementation", "feature", manifest=manifest)
    assert not ws._relay_dir("p", "implementation", "feature").exists()
    with pytest.raises(RelayDraftChanged, match="regular file"):
        ws.stage_relay_draft(src, "p", "implementation", "feature", manifest={"gone.py": manifest["a.py"]})
    with pytest.raises(RelayDraftChanged, match="escapes"):
        ws.stage_relay_draft(src, "p", "implementation", "feature", manifest={"../a.py": manifest["a.py"]})


def test_relay_bases_the_new_worktree_on_the_failed_branch_and_seeds_its_draft(world):
    attempts, ws, db, sf, src = world["attempts"], world["ws"], world["db"], world["sf"], world["src"]
    a, branch_head = _failed_attempt_with_draft(world)
    b = attempts.reserve("game", "a", 1, "feature", "relay", "finish it", continue_from=a["attempt_id"])
    relay = world["service"]._prepare_relay(b, str(src))
    b = attempts.pin_relay(b["attempt_id"], relay)

    assert relay["base_sha"] == branch_head
    assert relay["staged_files"] == {"implementation": {"half.py": _sha("HALF = 1\n")}}
    assert b["context"]["relay"] == relay
    assert ri.requested_base(db, b["execution_project_id"])["base_sha"] == branch_head
    # The failed attempt's own staging is untouched — it stays evidence.
    assert (ws._draft_dir(a["execution_project_id"], "implementation", "feature") / "half.py").exists()

    # The run that the launcher (or the poller) now provisions starts where the
    # failed one stopped, whichever of them provisions first.
    pid = b["execution_project_id"]
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id="run-relay", project_id=pid, config_name="feature", repo_mode="code")
    assert rec["base_sha"] == branch_head
    assert (Path(rec["worktree_path"]) / "done.py").exists()
    assert _git(src, "rev-parse", "HEAD") == world["head"], "the source checkout never moves"

    # The engine seeds the draft into the fresh staging exactly once.
    assert ws.seed_relay_draft(pid, "implementation", "feature") == ["half.py"]
    assert (ws._draft_dir(pid, "implementation", "feature") / "half.py").read_text() == "HALF = 1\n"
    assert ws.seed_relay_draft(pid, "implementation", "feature") == [], "a loop-back must not re-seed"
    ws.clean_draft_dir(pid, "implementation", "feature")
    assert ws.seed_relay_draft(pid, "implementation", "feature") == []


def test_relay_refuses_when_the_failed_branch_is_gone(world):
    attempts, src = world["attempts"], world["src"]
    a, _ = _failed_attempt_with_draft(world)
    rec = ri.record(world["db"], a["run_id"])
    subprocess.run(["git", "worktree", "remove", "--force", rec["worktree_path"]], cwd=src, check=True)
    subprocess.run(["git", "branch", "-D", rec["branch"]], cwd=src, check=True)
    b = attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"])
    with pytest.raises(StateConflict, match="no run branch"):
        world["service"]._prepare_relay(b, str(src))
    assert ri.requested_base(world["db"], b["execution_project_id"]) is None


def test_start_attempt_exposes_continue_from_on_the_typed_surface():
    from core.state_commands import StartAttempt, describe
    assert "continue_from" in describe()["operations"]["start_attempt"]["arguments"]["properties"]
    assert "relay_digest" in describe()["operations"]["start_attempt"]["arguments"]["properties"]
    assert StartAttempt(project_id="g", node_key="a", expected_revision=1, workflow="w",
                        request_key="r").continue_from is None


# ── chosen-base dependency ancestry ───────────────────────────────────
#
# Before a fresh workflow launches on an explicitly selected immutable base,
# dependency admission must check the commit the new worktree will ACTUALLY
# start from - the selected/materialized base - not the shared repository HEAD.
# HEAD need not contain the accepted dependency at all, and the old reference
# silently checked a tree the run would never build on.


def _commit_file(repo, name, text):
    (repo / name).write_text(text)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return _git(repo, "rev-parse", "HEAD")


def _accept_dependency(world, artifact):
    """Persist a real acceptance receipt binding node 'a' to `artifact`: the
    commit whose ancestry a dependent launch must prove reachable."""
    store, db, attempts = world["store"], world["db"], world["attempts"]
    a = attempts.reserve("game", "a", 1, "feature", "dep-a")
    node = store.get_node("game", "a")
    with db.get_connection() as conn:
        conn.execute("UPDATE state_attempts SET status='candidate',artifact_ref=? WHERE attempt_id=?",
                     (artifact, a["attempt_id"]))
        conn.execute(
            "INSERT INTO state_acceptances(receipt_id,project_id,node_key,node_revision,attempt_id,"
            "artifact_ref,contract_hash,dependency_snapshot,evidence_ids,reviewer,created_at,provenance_json) "
            "VALUES('receipt-a','game','a',1,?,?,?,'{}','[\"e\"]','fixture-reviewer','2026-10-06T00:00:00Z','{}')",
            (a["attempt_id"], artifact, node["contract_hash"]))
        conn.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt='receipt-a' "
                     "WHERE project_id='game' AND node_key='a'")
        conn.commit()


def _dependent(world):
    world["store"].add_nodes("game", [{
        "key": "b", "goal": "Deliver b", "dependencies": ["a"], "acceptance": [
            {"id": "behaviour", "kind": "test", "description": "Behaviour validated"}]}])


def _manifest():
    from types import SimpleNamespace
    return SimpleNamespace(seed_file="seed.md", output_step="implementation",
                           scheduler_owned=True, repo_mode="code")


def _capture_launcher(monkeypatch):
    import core.run_launcher as launcher
    calls = []
    def recorder(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "error", "message": "captured after dependency admission"}
    monkeypatch.setattr(launcher, "start_config_run", recorder)
    return calls


def test_fresh_attempt_admits_dependency_in_selected_base_when_head_lacks_it(world, monkeypatch):
    """The discriminating positive: the chosen base contains the dependency
    commit and HEAD does not. The old HEAD reference refused this launch; the
    admission must now check the tree the worktree is actually pinned to."""
    src = world["src"]
    dep = _commit_file(src, "dep.txt", "dependency\n")
    subprocess.run(["git", "reset", "--hard", world["head"]], cwd=src, check=True)
    assert _git(src, "rev-parse", "HEAD") == world["head"]
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(["git", "merge-base", "--is-ancestor", dep, "HEAD"], cwd=src, check=True)
    _accept_dependency(world, dep)
    _dependent(world)
    attempt = world["attempts"].reserve("game", "b", 1, "feature", "selected-base-present", base_sha=dep)
    calls = _capture_launcher(monkeypatch)

    launched = world["service"]._launch_or_recover(attempt, _manifest(), str(src))

    assert calls, "the launcher was reached: the dependency is in the selected base"
    assert launched["status"] == "unknown"
    assert ri.requested_base(world["db"], attempt["execution_project_id"])["base_sha"] == dep


def test_fresh_attempt_refuses_dependency_missing_from_selected_base_even_when_head_has_it(world, monkeypatch):
    """The fail-closed inverse: HEAD contains the dependency commit, the chosen
    base does not. The old HEAD reference admitted this launch against a tree
    the run would never build on; the selected base must be refused."""
    src = world["src"]
    dep = _commit_file(src, "dep.txt", "dependency\n")
    assert _git(src, "rev-parse", "HEAD") == dep, "HEAD must contain the dependency"
    _accept_dependency(world, dep)
    _dependent(world)
    attempt = world["attempts"].reserve("game", "b", 1, "feature", "selected-base-missing", base_sha=world["head"])
    calls = _capture_launcher(monkeypatch)

    with pytest.raises(StateConflict, match="not in the source; integrate it before launching"):
        world["service"]._launch_or_recover(attempt, _manifest(), str(src))
    assert not calls, "no run may be dispatched when the selected base misses the dependency"


def test_fresh_attempt_without_base_still_checks_head(world, monkeypatch):
    """The no-base path is preserved: with no chosen or materialized base the
    admission falls back to HEAD and admits the dependency it contains."""
    src = world["src"]
    dep = _commit_file(src, "dep.txt", "dependency\n")
    _accept_dependency(world, dep)
    _dependent(world)
    attempt = world["attempts"].reserve("game", "b", 1, "feature", "no-base")
    calls = _capture_launcher(monkeypatch)

    world["service"]._launch_or_recover(attempt, _manifest(), str(src))

    assert calls, "the no-base path must still admit a dependency HEAD contains"


def test_relay_base_takes_precedence_over_any_selected_base(world):
    """A relay continues on the failed branch head, never on a fresh selected
    base, even if the context still carries one."""
    service = world["service"]
    selected, relay_base = "a" * 40, "b" * 40
    attempt = {"context": {"base_sha": selected}, "execution_project_id": "sg-x"}
    assert service._dependency_ref(attempt, {"base_sha": relay_base}) == relay_base
    assert service._dependency_ref({"context": {}, "execution_project_id": "sg-x"}, None) == "HEAD"
    assert service._dependency_ref(attempt, None) == selected


def test_missing_receipt_and_invalid_base_still_refuse(world):
    """Exact fail-closed guards stay: a dependency whose acceptance receipt is
    missing has no ancestry to trust, and an explicitly selected base must be
    an exact 40-hex commit."""
    from core.state_graph import StateGraphError
    with pytest.raises(StateConflict, match="no acceptance receipt"):
        world["service"]._dependency_context(
            {"dependencies": {"a": {"status": "VERIFIED", "verified_receipt": "missing"}}},
            str(world["src"]))
    with pytest.raises(StateGraphError):
        world["service"].start_attempt("game", "b", 1, "feature", "bad-base", base_sha="not-a-sha")
