"""Self-contained Git bundles retained with immutable State lineage.

The State database is the portable carrier; the existing report store holds a
second content-addressed copy. Neither run cleanup nor Git GC owns these bytes.
There is deliberately no release operation: attempts/observations remain
recoverable, so State cannot yet prove that their artifacts are unreferenced.

A producer may supply a broad bundle (for example ``--all``). The supplied bytes
are authenticated against the producer's declared SHA-256 and then normalized,
in an owned temporary bare repository, into a candidate-only bundle: one fixed
ref and a freshly generated full pack of exactly the candidate's reachable
commits, trees and blobs. The retained digest (``bundle_sha256``) describes the
normalized bytes; the producer's original digest stays only in its report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from core.state_graph import StateConflict, now
from core.state_report_integrity import _read_regular_nofollow, retain_report

MAX_BUNDLE_BYTES = 128 * 1024 * 1024
CANDIDATE_REF = "refs/aitelier/candidate"


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    return env


def _git(repo, *args):
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=120, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateConflict(f"Git artifact validation unavailable: {type(exc).__name__}") from exc
    if result.returncode:
        raise StateConflict("Git artifact validation failed: " + result.stderr[-1000:].strip())
    return result.stdout.strip()


def _import_bundle(root, raw, commit, expected_tree=None):
    """Import bytes into a fresh bare repo; Git proves completeness and identity."""
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise StateConflict("Git bundle must contain 1..128 MiB of bytes")
    bundle, repo = root / "candidate.bundle", root / "repo.git"
    bundle.write_bytes(raw)
    _git(root, "init", "--bare", str(repo))
    _git(repo, "bundle", "verify", str(bundle))
    _git(repo, "fetch", "--no-tags", str(bundle), commit)
    actual = _git(repo, "rev-parse", "FETCH_HEAD^{commit}")
    tree = _git(repo, "rev-parse", "FETCH_HEAD^{tree}")
    if actual != commit or (expected_tree is not None and tree != expected_tree):
        raise StateConflict(f"expected commit/tree {commit}/{expected_tree}; actual {actual}/{tree}")
    _git(repo, "fsck", "--strict", "--no-reflogs", commit)
    return repo, bundle, tree


def _assert_exact_closure(repo, commit):
    """The imported object universe must equal the candidate's reachable objects."""
    present = set(_git(repo, "cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname)").split())
    reachable = {line.split()[0] for line in
                 _git(repo, "rev-list", "--objects", commit).splitlines() if line.strip()}
    if present != reachable:
        raise StateConflict("normalized Git bundle does not equal the candidate object closure")


def _validated_bundle(raw, commit, expected_tree=None, destination=None, exact=False):
    """Git proves completeness in an empty repo before destination mutation."""
    with tempfile.TemporaryDirectory(prefix="state-git-artifact-") as directory:
        repo, bundle, tree = _import_bundle(Path(directory), raw, commit, expected_tree)
        if exact:
            _assert_exact_closure(repo, commit)
        if destination is not None:
            ref = "refs/aitelier/artifacts/" + commit
            _git(destination, "fetch", "--no-tags", str(bundle), commit + ":" + ref)
            actual = _git(destination, "rev-parse", ref + "^{commit}")
            actual_tree = _git(destination, "rev-parse", ref + "^{tree}")
            if actual != commit or actual_tree != tree:
                raise StateConflict(f"expected commit/tree {commit}/{tree}; actual {actual}/{actual_tree}")
        return tree


def _normalized_bundle(repo, root, commit):
    """Write a candidate-only v2 bundle. A hand-built header plus a pack from
    ``pack-objects --revs`` of the single commit (no reuse of the producer's
    objects or deltas, one thread, pinned window/depth) keeps unrelated packed
    objects out and makes the bytes independent of the producer's pack layout.
    Output is stable for one Git version; cross-version identity is not promised."""
    pack = root / "candidate.pack"
    try:
        with open(pack, "wb") as out:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "pack-objects",
                 "--revs", "--stdout", "--no-reuse-delta", "--no-reuse-object",
                 "--threads=1", "--window=10", "--depth=50", "--compression=6",
                 "--delta-base-offset", "-q"],
                input=(commit + "\n").encode(), stdout=out, stderr=subprocess.PIPE,
                timeout=120, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateConflict(f"Git artifact normalization unavailable: {type(exc).__name__}") from exc
    if result.returncode:
        raise StateConflict("Git artifact normalization failed: "
                            + result.stderr[-1000:].decode(errors="replace").strip())
    header = f"# v2 git bundle\n{commit} {CANDIDATE_REF}\n\n".encode()
    return header + pack.read_bytes()


def retain_candidate(report_bytes, commit):
    descriptor = json.loads(report_bytes).get("git_bundle")
    if (not isinstance(descriptor, dict) or set(descriptor) != {"path", "sha256"}
            or not isinstance(descriptor["path"], str)
            or not Path(descriptor["path"]).is_absolute()
            or not isinstance(descriptor["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"])):
        raise StateConflict("git-sha1 candidate requires git_bundle with absolute local path and sha256")
    path = Path(descriptor["path"])
    # Producer input: bounded regular/nofollow read, authenticated by its declared digest.
    raw = _read_regular_nofollow(path, max_bytes=MAX_BUNDLE_BYTES)
    input_sha = hashlib.sha256(raw).hexdigest()
    if input_sha != descriptor["sha256"]:
        raise StateConflict(f"expected bundle {descriptor['sha256']}; actual {input_sha}")
    with tempfile.TemporaryDirectory(prefix="state-git-candidate-") as directory:
        root = Path(directory)
        repo, _, tree = _import_bundle(root, raw, commit)
        normalized = _normalized_bundle(repo, root, commit)
        if len(normalized) > MAX_BUNDLE_BYTES:
            raise StateConflict("normalized Git bundle exceeds 128 MiB")
        # Independent empty-repo proof of the normalized bytes before retention.
        if _validated_bundle(normalized, commit, tree, exact=True) != tree:
            raise StateConflict("normalized Git bundle changed the candidate tree")
        digest = hashlib.sha256(normalized).hexdigest()
        retained_source = root / "normalized.bundle"
        retained_source.write_bytes(normalized)
        retained_ref, retained_bytes = retain_report(str(retained_source), digest, completed=False,
                                                    max_bytes=MAX_BUNDLE_BYTES)
    if retained_bytes != normalized:
        raise StateConflict("Git bundle changed during retention")
    return {"commit_sha": commit, "tree_sha": tree, "bundle_sha256": digest,
            "retained_ref": retained_ref, "bundle_bytes": normalized,
            "input_bundle_sha256": input_sha}



def store_candidate(conn, artifact):
    old = conn.execute("SELECT tree_sha FROM state_git_artifacts WHERE commit_sha=?",
                       (artifact["commit_sha"],)).fetchone()
    if old:
        if old["tree_sha"] != artifact["tree_sha"]:
            raise StateConflict("retained commit has conflicting tree identity")
        return
    conn.execute("INSERT INTO state_git_artifacts "
                 "(commit_sha,tree_sha,bundle_sha256,retained_ref,bundle_bytes,created_at) "
                 "VALUES(?,?,?,?,?,?)", tuple(artifact[k] for k in (
                     "commit_sha", "tree_sha", "bundle_sha256", "retained_ref", "bundle_bytes")) + (now(),))


def materialize_candidate(store, commit, destination):
    """Return identity for a known external candidate; refuse missing legacy data."""
    with store.transaction() as conn:
        known = conn.execute("SELECT 1 FROM state_attempts WHERE execution_kind='external' "
                             "AND artifact_kind='git-sha1' AND artifact_ref=? LIMIT 1", (commit,)).fetchone()
        artifact = conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?", (commit,)).fetchone()
    if artifact is None:
        if known:
            raise StateConflict(f"expected Git artifact {commit}; actual durable bundle missing (legacy candidates need re-attestation)")
        return None
    artifact = dict(artifact)
    raw = bytes(artifact.pop("bundle_bytes"))
    actual = hashlib.sha256(raw).hexdigest()
    if actual != artifact["bundle_sha256"]:
        raise StateConflict(f"expected Git artifact {commit}, bundle {artifact['bundle_sha256']}; actual {actual}")
    if not destination:
        raise StateConflict(f"expected Git artifact {commit}; actual source repository missing")
    _validated_bundle(raw, commit, artifact["tree_sha"], destination)
    return artifact
