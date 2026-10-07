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
from skillflow.graph import PipelineGraph, StepNode, Transition

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
                   projects_base=str(tmp_path / "projects"),
                   code_path_resolver=lambda project_id, run_id=None:
                       ri.resolve_for_resolver(db, run_id) if run_id else str(tmp_path / "src"))
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


# Published artifacts must be reachable inputs on a fresh graph rewalk, while
# the original run, approval records and failed draft remain owned by that run.
def _failed_with_published_architecture(world, revision=1, design_pattern="approved_design.md", design_id="one"):
    from skillflow.core import StepResult
    sf,ws,db,attempts=world['sf'],world['ws'],world['db'],world['attempts']
    sf.register_graph(PipelineGraph(name='feature',begin='architecture',steps=[
        StepNode(id='architecture',checkpoint=True,output_fixed={
            'design':{'file':design_pattern,'target':'artifact'},
            'linter':{'file':'linter_manifest.json','target':'code'}},
            transitions=[Transition(to='implementation',match={'from':'checkpoint','value':'approved'})]),
        StepNode(id='implementation')]))
    a=attempts.reserve('game','a',1,'feature','approved-source-'+str(revision))
    pid=a['execution_project_id'];rid=sf.create_run('feature',project_id=pid)
    sf.start_run(rid);attempts.bind_run(a['attempt_id'],rid,sf)
    db.ensure_project(pid,name=pid,repo_type='existing',repo_path=str(world['src']))
    ri.ensure_for_run(db,run_id=rid,project_id=pid,config_name='feature',repo_mode='code')
    sf.advance_run(rid);claimed=sf.claim_next_step(rid)
    assert claimed.step_id=='architecture'
    out=ws._get_secure_path(pid)/'feature'/'architecture';out.mkdir(parents=True)
    from skillflow.write_tools import execute_write
    fixed=sf._get_resolver_for_run(rid).graph.steps[0].output_fixed
    written=execute_write('design',fixed,{'id':design_id,'content':
        'owner approved revision'+str(revision)+': real consumer UI, journal budget, growth receipt\n'},str(out))
    assert 'error' not in written
    design=out/written['written']
    (out/'linter_manifest.json').write_text('{}')
    sf.confirm_step(claimed.token,StepResult(outputs={'design':written['written']},flags={}))
    sf.advance_run(rid);assert sf.get_run(rid)['status']=='paused'
    sf.approve_checkpoint(rid);sf.advance_run(rid)
    ws.write_draft(pid,'implementation','unfinished.md','pending correction\n',graph_name='feature')
    sf.fail_run(rid,'Step implementation: native turn budget exhausted (32/32)')
    a=attempts.reconcile(a['attempt_id'],sf)
    return a,design


def test_relay_published_input_reaches_rewalk_with_checkpoints_preserved(world):
    a,design=_failed_with_published_architecture(world)
    before=design.read_bytes();service=world['service'];attempts=world['attempts'];ws=world['ws']
    inv=service.get_attempt(a['attempt_id'])['relay_inventory']
    assert inv['published_files']['architecture']=={'approved_design.md':_sha(before.decode())}
    b=attempts.reserve('game','a',1,'feature','published-relay',
        instruction='Keep adopted UI; a conflicting evidence-only rewalk is not an owner revision',
        continue_from=a['attempt_id'],relay_digest=inv['digest'])
    relay=service._prepare_relay(b,str(world['src']))
    pid=b['execution_project_id']
    assert relay['published_files']['architecture']==inv['published_files']['architecture']
    assert ws.seed_relay_draft(pid,'architecture','feature')==['approved_design.md']
    target=ws._draft_dir(pid,'architecture','feature')/'approved_design.md'
    assert target.read_bytes()==before
    assert design.read_bytes()==before
    assert ws.seed_relay_draft(pid,'architecture','feature')==[]
    assert world['sf']._get_resolver_for_run(a['run_id']).graph.steps[0].checkpoint is True
    assert world['sf'].get_run(a['run_id'])['status']=='failed'
    assert 'linter_manifest.json' not in inv['published_files']['architecture']


def test_relay_published_identity_refuses_stale_and_adopts_new_owner_revision(world):
    a,design=_failed_with_published_architecture(world)
    service=world['service'];attempts=world['attempts'];ws=world['ws']
    inv=service.get_attempt(a['attempt_id'])['relay_inventory']
    b=attempts.reserve('game','a',1,'feature','stale-input',continue_from=a['attempt_id'],relay_digest=inv['digest'])
    design.write_text('owner approved revision2: new consumer UI\n')
    with pytest.raises(StateConflict,match='changed since it was read'):
        service._prepare_relay(b,str(world['src']))
    assert not ws._relay_dir(b['execution_project_id'],'architecture','feature').exists()
    attempts.retire_reservation(b['attempt_id'],'stale approved input refused')
    a2,new_design=_failed_with_published_architecture(world,revision=2)
    fresh=service.get_attempt(a2['attempt_id'])['relay_inventory']
    assert fresh['digest']!=inv['digest']
    c=attempts.reserve('game','a',1,'feature','new-approved-input',continue_from=a2['attempt_id'],relay_digest=fresh['digest'])
    service._prepare_relay(c,str(world['src']))
    ws.seed_relay_draft(c['execution_project_id'],'architecture','feature')
    assert (ws._draft_dir(c['execution_project_id'],'architecture','feature')/'approved_design.md').read_bytes()==new_design.read_bytes()





@pytest.mark.parametrize('pattern,ident',[
    ('approved_design.md','ignored'),
    ('docs/approved_design.md','ignored'),
    ('docs/literal[1]?.md','ignored'),
    ('docs/*.md','planned'),
    ('docs/*.md','nested/planned'),
])
def test_relay_declared_complete_paths_match_real_writer_not_basename_collisions(world,pattern,ident):
    a,design=_failed_with_published_architecture(world,design_pattern=pattern,design_id=ident)
    directory=world['ws']._get_secure_path(a['execution_project_id'])/'feature'/'architecture'
    relative=design.relative_to(directory).as_posix()
    collision=directory/'unrelated'/relative
    collision.parent.mkdir(parents=True,exist_ok=True);collision.write_text('UNADOPTED BASENAME COLLISION\n')
    (directory/'approved_design.v1.md').write_text('OLD VERSION SIBLING\n')
    inv=world['service'].get_attempt(a['attempt_id'])['relay_inventory']
    assert inv['published_files']['architecture']=={relative:_sha(design.read_text())}
    b=world['attempts'].reserve('game','a',1,'feature','declared-path-relay',continue_from=a['attempt_id'],relay_digest=inv['digest'])
    world['service']._prepare_relay(b,str(world['src']))
    assert world['ws'].seed_relay_draft(b['execution_project_id'],'architecture','feature')==[relative]
    assert (world['ws']._draft_dir(b['execution_project_id'],'architecture','feature')/relative).read_bytes()==design.read_bytes()


@pytest.mark.parametrize('pattern,ident,decoy',[
    ('deep/*[1]?.md','nested/id','deep/id1x.md'),
    ('deep/*?.md','id','deep/unadopted.md'),
    ('deep/*-*.md','sameid','deep/different-other.md'),
    ('deep/*-*.md','line\nbreak','deep/line\nbreak-other.md'),
    ('deep/*-*.md','./x','deep/x-./y.md'),
])
def test_relay_declared_pattern_matches_the_real_writer_grammar(world,pattern,ident,decoy):
    """The writer replaces EVERY '*' with the SAME id and keeps '?', '[...]'
    literal. The old fnmatch/PurePath matcher read '?'/'[...]' as wildcards and
    let each '*' capture a different value, so it both omitted the real emitted
    name and admitted names the writer can never produce. The decoy written here
    is exactly such a name; it must stay unadopted. The id may hold a newline
    or a "./" that Path drops where it forms a component: the inventory sees the
    PUBLISHED path (deep/x-./x.md), not the raw target (deep/./x-./x.md)."""
    a,design=_failed_with_published_architecture(world,design_pattern=pattern,design_id=ident)
    directory=world['ws']._get_secure_path(a['execution_project_id'])/'feature'/'architecture'
    relative=design.relative_to(directory).as_posix()
    decoy_path=directory/decoy
    decoy_path.parent.mkdir(parents=True,exist_ok=True)
    decoy_path.write_text('A NAME THE WRITER CANNOT EMIT\n')
    inv=world['service'].get_attempt(a['attempt_id'])['relay_inventory']
    assert inv['published_files']['architecture']=={relative:_sha(design.read_text())}
    b=world['attempts'].reserve('game','a',1,'feature','writer-grammar-relay',continue_from=a['attempt_id'],relay_digest=inv['digest'])
    world['service']._prepare_relay(b,str(world['src']))
    seeded=world['ws'].seed_relay_draft(b['execution_project_id'],'architecture','feature')
    assert seeded==[relative],'only the writer-emitted name is relayed'
    assert (world['ws']._draft_dir(b['execution_project_id'],'architecture','feature')/relative).read_bytes()==design.read_bytes()


@pytest.mark.parametrize('pattern',['root/*.md','root/*-*.md','root/*[1]?.md','root/**.md',
                                     'root/*/again/*.md','*.md','docs/literal[1]?.md'])
def test_relay_matcher_admits_every_path_the_actual_sdk_writer_publishes(tmp_path,pattern):
    """Differential against the installed SDK writer itself, not a re-statement
    of its grammar: whatever execute_write publishes on disk is declared."""
    from skillflow.write_tools import execute_write
    from core.state_service import _writer_declares
    for n,ident in enumerate(['plain','nested/id','x-y','β[2]?','line\nbreak','./x','./y/.','x/.','a//b','.hidden','..x']):
        out=tmp_path/str(n)
        written=execute_write('slot',{'slot':{'file':pattern,'target':'artifact'}},{'id':ident,'content':'SDK\n'},str(out))
        assert 'error' not in written
        published=(out/written['written']).relative_to(out).as_posix()
        assert (out/published).is_file() and _writer_declares(published,pattern),(pattern,ident,published)


@pytest.mark.parametrize('pattern,filename',[
    ('deep/*?.md','deep/unadopted.md'),('deep/*[1]?.md','deep/id1x.md'),
    ('deep/*-*.md','deep/different-other.md'),('deep/*-*.md','deep/x-./y.md'),
    ('deep/*-*.md','deep/x-.x.md'),('deep/*-*.md','deep/line\nbreak-other.md'),
    ('root/*/again/*.md','root/a/again/b.md'),('docs/*.md','unrelated/docs/planned.md'),
    ('docs/*.md','docs/planned.txt'),('approved_design.md','unrelated/approved_design.md'),
])
def test_relay_matcher_refuses_paths_no_single_writer_id_publishes(pattern,filename):
    from core.state_service import _writer_declares
    assert not _writer_declares(filename,pattern)


def test_relay_actual_sdk_reader_prefers_retained_input_over_conflicting_owned_rewalk(world):
    from skillflow.read_tools import make_read_tool_fns
    a,design=_failed_with_published_architecture(world)
    before=design.read_bytes();inv=world['service'].get_attempt(a['attempt_id'])['relay_inventory']
    b=world['attempts'].reserve('game','a',1,'feature','reader-rewalk',continue_from=a['attempt_id'],relay_digest=inv['digest'])
    world['service']._prepare_relay(b,str(world['src']))
    pid=b['execution_project_id'];rid=world['sf'].create_run('feature',project_id=pid)
    world['db'].ensure_project(pid,name=pid,repo_type='existing',repo_path=str(world['src']))
    record=ri.ensure_for_run(world['db'],run_id=rid,project_id=pid,config_name='feature',repo_mode='code')
    root=ri.resolve_for_resolver(world['db'],rid)
    assert root==record['worktree_path']
    (Path(root)/'approved_design.md').write_text('CONFLICTING EVIDENCE-ONLY REWALK: abandon consumer UI and growth receipt\n')
    ws=world['ws'];ws.clean_draft_dir(pid,'architecture','feature');ws.seed_relay_draft(pid,'architecture','feature')
    reader=make_read_tool_fns([{'source_type':'repository','mode':'tool'}],
        workspace_root=str(ws._get_secure_path(pid)),current_config='feature',code_root=str(root),
        step_tmp_dir=str(ws._draft_dir(pid,'architecture','feature')),
        step_dir=str(ws.get_final_path(pid,'architecture','feature')),run_id=rid)['read']
    result=reader('approved_design.md',raw=True)
    assert result['source']=='staging' and result['content'].encode()==before
    assert 'CONFLICTING' not in result['content'] and design.read_bytes()==before
    assert world['sf'].get_run(a['run_id'])['status']=='failed'
