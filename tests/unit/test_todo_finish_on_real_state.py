"""A plan finished by the todo CLI, offered to a REAL State store (throwaway sqlite).

Nothing here is stubbed: the store is `core.state_service.StateService` over a
`StateDatabase` on a tmp file, the plan is created, walked and finished through the
real `python -m core.todo_cli` in a subprocess against a real git repository, and
every State call goes through the service's own API. The chain proved is the one a
director follows after `todo_finish`:

  start_external_attempt -> CLI create/begin/done/finish -> report_external_attempt
  (candidate, the finish report) -> the SAME report is refused as verifier evidence
  (independence) -> a distinct per-criterion report is accepted -> verify_node.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "TODO_FINISH_MARKER"
INDEPENDENCE = "verifier evidence must have a report independent of every external candidate observation"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AITELIER_DSH_STATE_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@localhost")
    (repo / "app.py").write_text("value = 1\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")

    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="fixture",
                           project_read_trusted=True)
    service.create_project("p", "Throwaway project")
    service.store.add_nodes("p", [{"key": "n", "goal": "Change app.py", "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "app.py carries the change"},
        {"id": "review", "kind": "review", "description": "A reviewer inspects the delivered commit"}]}])
    service.bind_source("p", str(repo))
    return service, repo, base, tmp_path


def cli(tmp_path, *argv, expect=0):
    """The real CLI in a subprocess; its stdout is one JSON object."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)}
    done = subprocess.run([sys.executable, "-m", "core.todo_cli", argv[0], "--store", str(tmp_path / "todo"), *argv[1:]],
                          cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == expect, (argv, done.returncode, done.stdout, done.stderr)
    return json.loads(done.stdout)


def evidence_report(path: Path, criterion: str, artifact: str) -> tuple[str, str]:
    body = json.dumps({"status": "completed", "settled": True, "usable": True,
                       "criterion_id": criterion, "verdict": "pass", "artifact": artifact},
                      sort_keys=True).encode()
    path.write_bytes(body)
    return str(path), sha(body)


def test_finish_report_is_accepted_as_candidate_refused_as_evidence_and_a_distinct_review_verifies(world):
    service, repo, base, tmp_path = world
    node = service.store.get_node("p", "n")
    attempt = service.start_external_attempt("p", "n", node["revision"], "dsh-todo", "job-1", "req-1",
                                             base_sha=base)

    # -- the plan, expanded from the repository template by the real CLI ----------------
    created = cli(tmp_path, "create", "--template", "code_change",
                  "--params", json.dumps({"paths": ["app.py"], "scope": ["app.py"], "marker": MARKER,
                                          "test_argv": [sys.executable, "-c", "pass"]}),
                  "--attempt", attempt["attempt_id"], "--node-key", "n",
                  "--context-hash", attempt["context_hash"], "--base-sha", base,
                  "--workspace", str(repo), "--goal", "Change app.py")
    assert created["created"] is True and created["items"] == ["implement", "test", "deliver"]
    aid = attempt["attempt_id"]

    cli(tmp_path, "begin", "--attempt", aid, "--item", "implement")
    (repo / "app.py").write_text(f"value = 2  # {MARKER}\n")
    assert cli(tmp_path, "done", "--attempt", aid, "--item", "implement")["outcome"] == "pass"

    cli(tmp_path, "begin", "--attempt", aid, "--item", "test")
    assert cli(tmp_path, "done", "--attempt", aid, "--item", "test")["outcome"] == "pass"

    git(repo, "commit", "-qam", "change app.py")
    head = git(repo, "rev-parse", "HEAD")
    assert head != base
    cli(tmp_path, "begin", "--attempt", aid, "--item", "deliver")
    assert cli(tmp_path, "done", "--attempt", aid, "--item", "deliver")["outcome"] == "pass"

    finished = cli(tmp_path, "finish", "--attempt", aid, "--out", str(tmp_path / "out"))
    assert finished["candidate"] is True and finished["artifact"] == head
    finish_report = Path(finished["report"])
    finish_bytes = finish_report.read_bytes()
    assert sha(finish_bytes) == finished["report_sha256"]

    # -- the finish report is the worker-side candidate observation: ACCEPTED ------------
    observed = service.report_external_attempt(
        aid, "finish-1", 0, attempt["context_hash"], "candidate", str(finish_report),
        finished["report_sha256"], True, head, "git-sha1")
    assert observed["status"] == "candidate"
    assert service.store.get_node("p", "n")["status"] == "CANDIDATE"
    assert service.get_attempt(aid)["git_artifact"]["commit_sha"] == head

    # -- the SAME bytes as verifier evidence are REFUSED ---------------------------------
    # Two guards, both real. The service's own front door checks the report envelope first:
    # a candidate report (status "candidate", no criterion/verdict) is not evidence at all.
    # The independence rule itself lives in StateAttempts.record_evidence; it refuses the
    # same report_sha256 with its own message, so it is asserted there with the same digest.
    for criterion in ("behaviour", "review"):
        with pytest.raises(StateConflict) as refused:
            service.record_evidence(aid, f"{aid}-same-{criterion}", criterion, "pass", head,
                                    str(finish_report), finished["report_sha256"], "reviewer")
        assert "evidence report status is not completed" in str(refused.value), str(refused.value)
        with pytest.raises(StateConflict) as refused:
            service.attempts.record_evidence(aid, f"{aid}-same-{criterion}", criterion, "pass", head,
                                             str(finish_report), finished["report_sha256"], "reviewer",
                                             report_bytes=finish_bytes)
        assert INDEPENDENCE in str(refused.value), str(refused.value)

    # -- a distinct completed/settled/usable report per criterion is accepted ------------
    for criterion in ("behaviour", "review"):
        ref, digest = evidence_report(tmp_path / f"evidence-{criterion}.json", criterion, head)
        assert digest != finished["report_sha256"]
        service.record_evidence(aid, f"{aid}-{criterion}", criterion, "pass", head, ref, digest,
                                "independent reviewer", director_identity="reviewer@example")

    receipt = service.verify_node("p", "n", node["revision"], aid)
    assert receipt
    assert service.store.get_node("p", "n")["status"] == "VERIFIED"
