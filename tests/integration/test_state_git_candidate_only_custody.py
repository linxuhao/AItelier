"""Candidate-only Git custody, proven on real repositories and real imports.

The retained bytes must recover exactly the candidate closure and nothing from
unrelated producer history that a broad ``git bundle create --all`` carried.
"""
import hashlib
import json
import os
import subprocess

import pytest

from core.state_git_artifacts import retain_candidate
from core.state_graph import StateConflict


def run(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, check=True,
                          capture_output=True).stdout


def git(repo, *args):
    return run(repo, *args).decode().strip()


def make_repo(root):
    repo = root / "producer"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@localhost")
    commits = []
    for index in range(3):
        (repo / f"file{index}").write_text(f"candidate ancestry {index}\n" * 20)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"candidate {index}")
        commits.append(git(repo, "rev-parse", "HEAD"))
    return repo, commits


def set_unrelated(repo, payload, name="unrelated"):
    """Point an orphan branch at a unique blob/commit; return (commit, blob)."""
    blob = run(repo, "hash-object", "-w", "--stdin", data=payload).decode().strip()
    tree = run(repo, "mktree", data=f"100644 blob {blob}\tsentinel.bin\n".encode()).decode().strip()
    commit = run(repo, "commit-tree", tree, "-m", "unrelated sentinel " + blob[:8]).decode().strip()
    git(repo, "update-ref", "refs/heads/" + name, commit)
    return commit, blob


def broad_bundle(repo, path):
    git(repo, "bundle", "create", str(path), "--all")
    return path


def report_for(bundle, digest=None):
    raw = bundle.read_bytes()
    return json.dumps({"status": "candidate", "settled": True, "usable": True,
                       "git_bundle": {"path": str(bundle),
                                      "sha256": digest or hashlib.sha256(raw).hexdigest()}}).encode()


def import_closure(tmp_path, name, raw, commit):
    """Empty-bare-repo import of bundle bytes; returns repo and all object ids."""
    bundle = tmp_path / (name + ".bundle")
    bundle.write_bytes(raw)
    repo = tmp_path / (name + ".git")
    git(tmp_path, "init", "-q", "--bare", str(repo))
    git(repo, "bundle", "verify", str(bundle))
    git(repo, "fetch", "--no-tags", str(bundle), commit + ":refs/heads/recovered")
    objects = set(run(repo, "cat-file", "--batch-all-objects",
                      "--batch-check=%(objectname)").decode().split())
    return repo, objects


def reachable(repo, commit):
    return {line.split()[0] for line in git(repo, "rev-list", "--objects", commit).splitlines()}


def has_object(repo, oid):
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", oid],
                          capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))


def test_broad_bundle_is_retained_as_exact_candidate_closure(tmp_path):
    repo, commits = make_repo(tmp_path)
    candidate = commits[-1]
    unrelated_commit, unrelated_blob = set_unrelated(repo, os.urandom(2 * 1024 * 1024))
    bundle = broad_bundle(repo, tmp_path / "broad.bundle")
    input_bytes = bundle.read_bytes()
    input_sha = hashlib.sha256(input_bytes).hexdigest()
    refs_before = git(repo, "show-ref")
    # Counterexample for the old verbatim retention: the input really carries
    # the unrelated history, so storing it unchanged would keep these objects.
    assert "refs/heads/unrelated" in git(repo, "bundle", "list-heads", str(bundle))
    _, broad_objects = import_closure(tmp_path, "broad-import", input_bytes, candidate)
    assert unrelated_commit in broad_objects and unrelated_blob in broad_objects

    artifact = retain_candidate(report_for(bundle), candidate)

    retained = artifact["bundle_bytes"]
    assert artifact["commit_sha"] == candidate
    assert artifact["tree_sha"] == git(repo, "rev-parse", candidate + "^{tree}")
    # Retained digest describes normalized bytes; the producer's digest is the input's.
    assert artifact["bundle_sha256"] == hashlib.sha256(retained).hexdigest()
    assert artifact["input_bundle_sha256"] == input_sha
    assert artifact["bundle_sha256"] != input_sha
    with open(artifact["retained_ref"], "rb") as stored:
        assert stored.read() == retained
    # Producer input and source refs are untouched.
    assert bundle.read_bytes() == input_bytes
    assert git(repo, "show-ref") == refs_before

    recovered, objects = import_closure(tmp_path, "recovered", retained, candidate)
    assert git(recovered, "rev-parse", "refs/heads/recovered") == candidate
    assert git(recovered, "rev-parse", "refs/heads/recovered^{tree}") == artifact["tree_sha"]
    assert set(git(recovered, "rev-list", candidate).split()) == set(commits)
    assert objects == reachable(repo, candidate)
    assert not has_object(recovered, unrelated_commit)
    assert not has_object(recovered, unrelated_blob)
    git(recovered, "fsck", "--strict")
    # Meaningful reduction for a 2 MiB incompressible unrelated payload.
    assert len(retained) < 200 * 1024
    assert len(input_bytes) - len(retained) > 1024 * 1024


def test_same_candidate_with_changing_unrelated_history_retains_identical_bytes(tmp_path):
    repo, commits = make_repo(tmp_path)
    candidate = commits[-1]
    seen = []
    for index, size in enumerate((64 * 1024, 1024 * 1024)):
        set_unrelated(repo, os.urandom(size), name=f"unrelated{index}")
        if index:
            git(repo, "update-ref", "-d", f"refs/heads/unrelated{index - 1}")
        bundle = broad_bundle(repo, tmp_path / f"broad{index}.bundle")
        artifact = retain_candidate(report_for(bundle), candidate)
        seen.append((artifact["bundle_bytes"], artifact["bundle_sha256"],
                     artifact["retained_ref"], artifact["input_bundle_sha256"]))
    assert seen[0][3] != seen[1][3]  # producer input genuinely differed
    assert seen[0][:3] == seen[1][:3]  # retained identity and size did not


def test_bad_input_still_refuses_after_candidate_is_retained(tmp_path):
    repo, commits = make_repo(tmp_path)
    candidate = commits[-1]
    bundle = broad_bundle(repo, tmp_path / "broad.bundle")
    retain_candidate(report_for(bundle), candidate)
    with pytest.raises(StateConflict, match="expected bundle"):
        retain_candidate(report_for(bundle, digest="0" * 64), candidate)
    with pytest.raises(StateConflict):
        retain_candidate(report_for(bundle), "f" * 40)
    link = tmp_path / "link.bundle"
    link.symlink_to(bundle)
    with pytest.raises(StateConflict, match="symlink|unavailable"):
        retain_candidate(report_for(link), candidate)
    # A bundle with a prerequisite is not self-contained and is refused. The
    # named ref `main` (the candidate tip) gives Git a valid, non-empty thin
    # bundle; the raw candidate SHA with ^base creates an empty bundle instead.
    thin = tmp_path / "thin.bundle"
    git(repo, "bundle", "create", str(thin), "main", "^" + commits[0])
    header = thin.read_bytes().split(b"\n\n", 1)[0].decode().splitlines()
    assert "-" + commits[0] in [line.split()[0] for line in header[1:]]
    assert candidate + " refs/heads/main" in header
    with pytest.raises(StateConflict):
        retain_candidate(report_for(thin), candidate)


def test_normalized_bytes_recover_through_state_database_alone(tmp_path):
    from tests.integration.test_state_git_artifact_durability import fixture
    service, source, producer, db, old, commit, tree, payload, bundle = fixture(tmp_path / "fixture")
    # Second candidate whose producer bundle is broad (--all) and carries unrelated history.
    (producer / "value").write_bytes(b"second candidate\n")
    git(producer, "add", "value")
    git(producer, "commit", "-qm", "second")
    second = git(producer, "rev-parse", "HEAD")
    unrelated_commit, unrelated_blob = set_unrelated(source, os.urandom(1024 * 1024))
    broad = tmp_path / "second-broad.bundle"
    broad_bundle(producer, broad)
    input_sha = hashlib.sha256(broad.read_bytes()).hexdigest()
    attempt = service.start_external_attempt("durable", "review", 1, "fixture", "review", "review")
    report = tmp_path / "second-report.json"
    body = json.dumps({**json.loads(report_for(broad)), "artifact": second}).encode()
    report.write_bytes(body)
    service.report_external_attempt(attempt["attempt_id"], "second", 0, attempt["context_hash"],
                                    "candidate", str(report), hashlib.sha256(body).hexdigest(),
                                    True, second, "git-sha1")
    # The producer's report bytes still declare the original digest.
    assert json.loads(report.read_bytes())["git_bundle"]["sha256"] == input_sha
    identity = service.get_attempt(attempt["attempt_id"])["git_artifact"]
    assert identity["commit_sha"] == second and identity["bundle_sha256"] != input_sha
    with service.store.transaction() as conn:
        stored = bytes(conn.execute("SELECT bundle_bytes FROM state_git_artifacts WHERE commit_sha=?",
                                    (second,)).fetchone()[0])
    assert hashlib.sha256(stored).hexdigest() == identity["bundle_sha256"]
    broad.unlink()
    recovered, objects = import_closure(tmp_path, "db-recovered", stored, second)
    assert objects == reachable(source, second)
    assert not has_object(recovered, unrelated_blob)
    assert not has_object(recovered, unrelated_commit)
