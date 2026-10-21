"""A fresh attempt must not silently leave the node's previous candidate behind.

Real tiny Git histories, the real StateAttempts/StateService admission paths and
the real run_isolation base request; the launcher is captured so no workflow
or paid model runs. The account is observation only: no case below may refuse,
merge, adopt or clear anything.

UNRUN at authoring time: delivered source-only for an independent evaluator.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_graph import StateGraphStore
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

ACCOUNT_EVENTS = ("prior_candidate_included", "prior_candidate_unreconciled")


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


def _commit_file(repo, name, text):
    (repo / name).write_text(text)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.db"))
    store = StateGraphStore(db, project_read_trusted=True)
    store.create_project("game", "Long-running game")
    store.add_nodes("game", [{"key": "a", "goal": "Implement a", "dependencies": [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour validated"}]}])
    attempts = StateAttempts(store)
    sf = SkillFlow(str(tmp_path / "sf.db"), workspace_base=str(tmp_path / "workspaces"),
                   projects_base=str(tmp_path / "projects"),
                   code_path_resolver=lambda project_id, run_id=None:
                       ri.resolve_for_resolver(db, run_id) if run_id else str(tmp_path / "src"))
    sf.register_graph(PipelineGraph(name="feature", begin="implementation", steps=[StepNode(id="implementation")]))
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    src = tmp_path / "src"
    head = _init_repo(src)
    yield {"db": db, "store": store, "attempts": attempts, "sf": sf, "src": src, "head": head,
           "service": StateService(db, ws, sf, {}, project_read_trusted=True)}
    sf._conn.close()


def _manifest():
    return SimpleNamespace(seed_file="seed.md", output_step="implementation",
                           scheduler_owned=True, repo_mode="code")


def _capture_launcher(monkeypatch):
    import core.run_launcher as launcher
    calls = []
    monkeypatch.setattr(launcher, "start_config_run",
                        lambda *a, **k: calls.append((a, k)) or {"status": "error", "message": "captured"})
    return calls


def _prior_candidate(world, artifact, key="prior"):
    """A settled earlier attempt of node 'a' that retained `artifact`."""
    a = world["attempts"].reserve("game", "a", 1, "feature", key)
    with world["db"].get_connection() as conn:
        conn.execute("UPDATE state_attempts SET status='candidate',artifact_ref=? WHERE attempt_id=?",
                     (artifact, a["attempt_id"]))
        conn.commit()
    return a["attempt_id"]


def _events(world, *types):
    with world["db"].get_connection() as conn:
        rows = conn.execute("SELECT event_type,payload_json FROM state_events WHERE node_key='a' "
                            "ORDER BY seq").fetchall()
    return [(r[0], json.loads(r[1])) for r in rows if r[0] in types]


def _launch(world, monkeypatch, key, base_sha=None):
    calls = _capture_launcher(monkeypatch)
    attempt = world["attempts"].reserve("game", "a", 1, "feature", key, base_sha=base_sha)
    world["service"]._launch_or_recover(attempt, _manifest(), str(world["src"]))
    return world["attempts"].get(attempt["attempt_id"]), calls


def _unreachable_candidate(world):
    """A real commit the original HEAD does not contain (a failed-then-rederived tree)."""
    cand = _commit_file(world["src"], "cand.txt", "candidate\n")
    subprocess.run(["git", "reset", "-q", "--hard", world["head"]], cwd=world["src"], check=True)
    return cand


def test_requested_base_that_lacks_prior_candidate_is_reported_excluded(world, monkeypatch):
    cand = _unreachable_candidate(world)
    prior_id = _prior_candidate(world, cand)
    attempt, calls = _launch(world, monkeypatch, "fresh", base_sha=world["head"])
    account = attempt["context"]["prior_candidate"]
    assert account["state"] == "excluded"
    assert account["prior_attempt_id"] == prior_id and account["prior_artifact"] == cand
    assert account["selected_base"] == world["head"] and account["base_origin"] == "requested_base"
    assert calls, "visibility is not a refusal: the deliberate fresh base still launches"
    assert ri.requested_base(world["db"], attempt["execution_project_id"])["base_sha"] == world["head"]
    assert [e[0] for e in _events(world, *ACCOUNT_EVENTS)] == ["prior_candidate_unreconciled"]
    assert _git(world["src"], "rev-parse", "HEAD") == world["head"], "nothing was merged or adopted"


def test_requested_base_containing_prior_candidate_is_reported_included(world, monkeypatch):
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt, _ = _launch(world, monkeypatch, "fresh", base_sha=cand)
    account = attempt["context"]["prior_candidate"]
    assert account["state"] == "included" and account["selected_base"] == cand
    assert [e[0] for e in _events(world, *ACCOUNT_EVENTS)] == ["prior_candidate_included"]


def _provision(world, attempt):
    """The real isolation receipt for `attempt`: a genuine run and worktree provisioned
    by the normal ensure_for_run path, bound to the attempt."""
    exec_id = attempt["execution_project_id"]
    world["db"].ensure_project(exec_id, name=exec_id, repo_type="existing", repo_path=str(world["src"]))
    run_id = world["sf"].create_run("feature", project_id=exec_id)
    rec = ri.ensure_for_run(world["db"], run_id=run_id, project_id=exec_id, config_name="feature")
    world["attempts"].bind_run(attempt["attempt_id"], run_id, world["sf"])
    return rec


def test_ordinary_no_base_is_unknown_until_the_real_receipt_then_reconciled_excluded(world, monkeypatch):
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt, calls = _launch(world, monkeypatch, "no-base")
    account = attempt["context"]["prior_candidate"]
    assert calls
    # No materialized base yet: explicit UNKNOWN, never a guess from source HEAD.
    assert account["state"] == "unknown" and account["selected_base"] is None
    assert account["pending_base"] is True and "HEAD" in account["reason"]
    # The normal provision receipt names the actual base; reconcile through it.
    attempt = world["attempts"].get(attempt["attempt_id"])
    rec = _provision(world, attempt)
    assert rec["base_sha"] == world["head"]
    done = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    account = done["context"]["prior_candidate"]
    assert account["state"] == "excluded" and account["selected_base"] == rec["base_sha"]
    assert account["base_origin"] == "provisioned_base" and "pending_base" not in account
    again = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    assert again["context"]["prior_candidate"] == account
    assert len(_events(world, *ACCOUNT_EVENTS)) == 2, "pending unknown, then one reconciliation; no duplicates"


def test_no_base_reconciles_to_included_when_provisioned_head_contains_the_candidate(world, monkeypatch):
    cand = _commit_file(world["src"], "cand.txt", "candidate\n")
    _prior_candidate(world, cand)
    attempt, _ = _launch(world, monkeypatch, "no-base")
    assert attempt["context"]["prior_candidate"]["state"] == "unknown"
    rec = _provision(world, world["attempts"].get(attempt["attempt_id"]))
    done = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    account = done["context"]["prior_candidate"]
    assert account["state"] == "included" and account["selected_base"] == cand == rec["base_sha"]


def test_requested_base_account_matches_the_real_provision_receipt(world, monkeypatch):
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt, _ = _launch(world, monkeypatch, "fresh", base_sha=cand)
    assert attempt["context"]["prior_candidate"]["state"] == "included"
    rec = _provision(world, world["attempts"].get(attempt["attempt_id"]))
    assert rec["base_sha"] == cand, "the director's request is what the worktree really got"
    done = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    assert done["context"]["prior_candidate"] == attempt["context"]["prior_candidate"]
    assert len(_events(world, *ACCOUNT_EVENTS)) == 1


def test_source_head_drift_after_admission_is_judged_against_the_provisioned_base(world, monkeypatch):
    """Normal no-base selection reads HEAD late, at provision. HEAD moves between the
    account and the provision; the account must name the base the worktree really got,
    not the earlier HEAD."""
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt, _ = _launch(world, monkeypatch, "no-base")
    assert attempt["context"]["prior_candidate"]["state"] == "unknown"
    subprocess.run(["git", "reset", "-q", "--hard", cand], cwd=world["src"], check=True)
    rec = _provision(world, world["attempts"].get(attempt["attempt_id"]))
    assert rec["base_sha"] == cand != world["head"]
    done = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    account = done["context"]["prior_candidate"]
    assert account["state"] == "included" and account["selected_base"] == cand
    assert account["selected_base"] != world["head"]


def test_missing_receipt_leaves_pending_unknown_not_a_guess(world, monkeypatch):
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt, _ = _launch(world, monkeypatch, "no-base")
    done = world["service"]._reconcile_prior_candidate(world["attempts"].get(attempt["attempt_id"]))
    assert done["context"]["prior_candidate"]["state"] == "unknown"
    assert len(_events(world, *ACCOUNT_EVENTS)) == 1



def test_account_is_judged_from_ancestry_not_from_status_or_names(world, monkeypatch):
    """The prior attempt's status is reported but never decides containment."""
    cand = _unreachable_candidate(world)
    prior_id = _prior_candidate(world, cand)
    with world["db"].get_connection() as conn:
        conn.execute("UPDATE state_attempts SET status='failed' WHERE attempt_id=?", (prior_id,))
        conn.commit()
    attempt, _ = _launch(world, monkeypatch, "fresh", base_sha=world["head"])
    account = attempt["context"]["prior_candidate"]
    assert account["state"] == "excluded" and account["prior_attempt_status"] == "failed"
    assert account["prior_artifact"] == cand, "the original retained candidate is not rewritten"


def test_unavailable_historical_carrier_is_unknown_never_included_or_absent(world, monkeypatch):
    prior_id = _prior_candidate(world, "c" * 40)
    attempt, calls = _launch(world, monkeypatch, "fresh", base_sha=world["head"])
    account = attempt["context"]["prior_candidate"]
    assert account["state"] == "unknown" and account["prior_attempt_id"] == prior_id
    assert "unavailable" in account["reason"]
    assert calls, "an unknown carrier does not refuse the launch"
    assert [e[0] for e in _events(world, *ACCOUNT_EVENTS)] == ["prior_candidate_unreconciled"]


def test_unresolvable_inputs_are_unknown_not_included(world):
    from core import state_prior_candidate as pc
    cand = _unreachable_candidate(world)
    prior = {"attempt_id": "x", "status": "candidate", "artifact_ref": cand}
    assert pc.account(prior, str(world["src"]), "d" * 40, "requested_base")["state"] == "unknown"
    assert pc.account(prior, None, world["head"], "requested_base")["state"] == "unknown"
    assert pc.account(prior, str(world["src"]), "not-a-sha", "requested_base")["state"] == "unknown"
    assert pc.account(prior, str(world["src"] / "missing"), world["head"], "requested_base")["state"] == "unknown"


def test_no_prior_candidate_means_no_fabricated_account(world, monkeypatch):
    attempt, calls = _launch(world, monkeypatch, "first", base_sha=world["head"])
    assert "prior_candidate" not in attempt["context"]
    assert calls and not _events(world, *ACCOUNT_EVENTS)


def test_non_git_prior_artifact_is_not_a_commit_account(world, monkeypatch):
    _prior_candidate(world, "e" * 64)
    attempt, _ = _launch(world, monkeypatch, "fresh", base_sha=world["head"])
    assert "prior_candidate" not in attempt["context"] and not _events(world, *ACCOUNT_EVENTS)


def test_retry_creates_one_account_and_one_event(world, monkeypatch):
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    attempt = world["attempts"].reserve("game", "a", 1, "feature", "fresh", base_sha=world["head"])
    service, src = world["service"], str(world["src"])
    first = service._pin_prior_candidate(attempt, src)["context"]["prior_candidate"]
    again = service._pin_prior_candidate(attempt, src)["context"]["prior_candidate"]
    replay = world["attempts"].reserve("game", "a", 1, "feature", "fresh", base_sha=world["head"])
    assert first == again == replay["context"]["prior_candidate"]
    assert len(_events(world, *ACCOUNT_EVENTS)) == 1
    with world["db"].get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_attempts WHERE request_key='fresh'").fetchone()[0] == 1


def test_relay_base_precedence_is_unchanged(world):
    service = world["service"]
    selected, relay_base = "a" * 40, "b" * 40
    attempt = {"context": {"base_sha": selected}, "execution_project_id": "sg-x"}
    assert service._dependency_ref(attempt, {"base_sha": relay_base}) == relay_base
    assert service._dependency_ref({"context": {}, "execution_project_id": "sg-x"}, None) == "HEAD"
    assert service._dependency_ref(attempt, None) == selected


# -- supported external path ------------------------------------------
# The source binding is immutable once any attempt exists, so it is bound BEFORE
# the prior attempts are created (the immutable guard is not relaxed).

def _bind(world):
    if not world["service"].portfolio.source_binding("game"):
        world["service"].bind_source("game", str(world["src"]))


def _external(world, request_key, base_sha=None):
    return world["service"].start_external_attempt(
        "game", "a", 1, "fixture", "job-" + request_key, request_key, base_sha=base_sha)


def test_external_requested_exact_base_reports_excluded(world):
    _bind(world)
    cand = _unreachable_candidate(world)
    prior_id = _prior_candidate(world, cand)
    attempt = _external(world, "ext-1", base_sha=world["head"])
    account = attempt["context"]["prior_candidate"]
    assert attempt["status"] == "running" and attempt["context"]["base_sha"] == world["head"]
    assert account["state"] == "excluded" and account["prior_attempt_id"] == prior_id
    assert account["selected_base"] == world["head"] and account["base_origin"] == "requested_base"
    replay = _external(world, "ext-1", base_sha=world["head"])
    assert replay["attempt_id"] == attempt["attempt_id"]
    assert replay["context"]["prior_candidate"] == account
    assert len(_events(world, *ACCOUNT_EVENTS)) == 1


def test_external_requested_base_containing_candidate_reports_included(world):
    _bind(world)
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    account = _external(world, "ext-inc", base_sha=cand)["context"]["prior_candidate"]
    assert account["state"] == "included" and account["selected_base"] == cand


def test_external_without_declared_base_is_unknown_not_source_head(world):
    _bind(world)
    cand = _unreachable_candidate(world)
    prior_id = _prior_candidate(world, cand)
    account = _external(world, "ext-2")["context"]["prior_candidate"]
    assert account["state"] == "unknown" and account["prior_attempt_id"] == prior_id
    assert account["selected_base"] is None and account["base_origin"] == "unresolved"
    assert "HEAD" in account["reason"]
    assert [e[0] for e in _events(world, *ACCOUNT_EVENTS)] == ["prior_candidate_unreconciled"]


def test_external_without_prior_candidate_has_no_account(world):
    _bind(world)
    attempt = _external(world, "ext-3", base_sha=world["head"])
    assert "prior_candidate" not in attempt["context"] and not _events(world, *ACCOUNT_EVENTS)


def test_external_account_is_built_outside_the_write_transaction(world, monkeypatch):
    """Git must not run while State's BEGIN IMMEDIATE is held: the builder runs
    before the reservation transaction opens."""
    _bind(world)
    cand = _unreachable_candidate(world)
    _prior_candidate(world, cand)
    from core import state_prior_candidate as pc
    seen = []
    real = pc.account
    store = world["service"].store
    real_tx = store.transaction
    state = {"writing": 0}

    from contextlib import contextmanager

    @contextmanager
    def tracked(*a, **k):
        if k.get("write"):
            state["writing"] += 1
        try:
            with real_tx(*a, **k) as conn:
                yield conn
        finally:
            if k.get("write"):
                state["writing"] -= 1

    def spy(*a, **k):
        seen.append(state["writing"])
        return real(*a, **k)
    monkeypatch.setattr(store, "transaction", tracked)
    monkeypatch.setattr(pc, "account", spy)
    attempt = _external(world, "ext-tx", base_sha=world["head"])
    assert seen == [0], "Git ran outside any State write transaction"
    assert attempt["context"]["prior_candidate"]["state"] == "excluded"

