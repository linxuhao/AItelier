"""Self-contained Git bundles retained with immutable State lineage.

The State database is the only carrier of future bundles: ``bundle_bytes`` holds
the one authoritative normalized full pack and ``retained_ref`` is the typed
reference ``state-db:git-bundle:sha256:<bundle_sha256>``. Historical rows keep
their absolute report-store path untouched. Neither run cleanup nor Git GC owns
these bytes.

Release. Retention is the default and a RECORDED release is the only door
(``release_bundles``, operator tool ``tools/release_bundle_store.py``). The
policy is ``RELEASE_RULE``: these bytes have no LIVE consumer any more - an
acceptance receipt, an in-flight attempt, an unretired protected reference, an
open worktree base, or a base request that can still take effect. It is
deliberately NOT "nothing names the commit": every historical identity row
(attempt, observation, evidence, unprotected history link, event) keeps naming
it after the bytes are gone, so a rule written that way releases nothing at
all. Nothing is nulled or rewritten: the ``state_git_artifacts`` row keeps its
commit, tree, digest and reference, the bytes leave the store, and one
append-only ``state_git_bundle_releases`` row says when, by whom, under which
rule, and how many bytes - so the ledger can subtract them and a reader can
tell "released" from "missing". ``state_git_bundle_custody`` is the read-side
view of that partition. The bytes are released from the LEDGER at once; the
file only returns the space after a compaction (``PRAGMA auto_vacuum`` is 0 and
``secure_delete`` is 0, so a released payload is not zeroed either - see the
release record and ``tools/release_bundle_store.py``'s offline procedure).


A producer may supply a broad bundle (for example ``--all``). The supplied bytes
are authenticated against the producer's declared SHA-256 and then normalized,
in an owned temporary bare repository, into a candidate-only bundle: one fixed
ref and a freshly generated full pack of exactly the candidate's reachable
commits, trees and blobs. The retained digest (``bundle_sha256``) describes the
normalized bytes; the producer's original digest stays only in its report.

Storage of the bytes. A row written by ``store_candidate`` keeps all its
columns but an empty ``bundle_bytes`` (X''); the exact bytes live once, keyed by
``bundle_sha256``, in the immutable ``state_git_bundle_store``: either a ``raw``
keyframe, or a ``zstd-prefix`` delta compressed with a raw keyframe's bytes as
a raw-content prefix dictionary. Depth is always 1 (a delta's base is a raw
keyframe; the schema refuses anything else). A row with non-empty
``bundle_bytes`` (written before the store, or never migrated) is read as
before. Either way the restored bytes are verified against ``bundle_sha256``
on every read, and any defect in a payload, base or codec field fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path

from core import closed_git
from core.state_graph import StateConflict, now
from core.state_report_integrity import _read_regular_nofollow

MAX_BUNDLE_BYTES = 256 * 1024 * 1024
CANDIDATE_REF = "refs/aitelier/candidate"
# Wall-clock guard for ONE Git subprocess while a candidate REPORT is imported
# and normalized. It ends a Git that hangs; it does not judge the candidate, so
# it sits far above any real run.
# Measured 2026-11-12 (Git 2.47.3, a core to itself, a 116.8 MiB bundle carrying
# 1.05 GB of blobs): the normalizing pack-objects needs 109 s of CPU, which at
# the same cost per MiB is about 240 s for a bundle at MAX_BUNDLE_BYTES. The
# former fixed 120 s refused that candidate whenever the host was 10% slower.
# Materialization is not under this guard and keeps 120 s per step: it runs
# inside the deployment admission fence and, for the fetch into the project's
# repository, that repository's lock.
GIT_TIMEOUT_SEC = 1800
GIT_TIMEOUT_ENV = "AITELIER_STATE_GIT_TIMEOUT_SEC"
MATERIALIZE_GIT_TIMEOUT_SEC = 120


def _git_timeout():
    try:
        seconds = float(os.environ.get(GIT_TIMEOUT_ENV, ""))
    except ValueError:
        return GIT_TIMEOUT_SEC
    # subprocess cannot wait for inf or 1e300; a value like that is no budget.
    return seconds if 0 < seconds <= 86400 else GIT_TIMEOUT_SEC


def _unavailable(what, exc, budget):
    """A Git step that could not run or outlived its budget says nothing about
    the candidate. For a candidate report (``budget`` given) the refusal names
    the budget and what the caller does next."""
    detail = type(exc).__name__
    if budget is not None and isinstance(exc, subprocess.TimeoutExpired):
        detail += (f" after {exc.timeout:g}s ({GIT_TIMEOUT_ENV}); the candidate was not judged, "
                   "retry the identical call")
    return StateConflict(f"Git artifact {what} unavailable: {detail}")


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    return env


def _git(repo, *args, budget=None):
    """``budget`` is the candidate-report guard in seconds; None is every other path."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=budget or MATERIALIZE_GIT_TIMEOUT_SEC, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _unavailable("validation", exc, budget) from exc
    if result.returncode:
        raise StateConflict("Git artifact validation failed: " + result.stderr[-1000:].strip())
    return result.stdout.strip()


def _import_bundle(root, raw, commit, expected_tree=None, budget=None):
    """Import bytes into a fresh bare repo; Git proves completeness and identity."""
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise StateConflict(f"Git bundle must contain 1..{MAX_BUNDLE_BYTES >> 20} MiB of bytes")
    bundle, repo = root / "candidate.bundle", root / "repo.git"
    bundle.write_bytes(raw)
    _git(root, "init", "--bare", str(repo), budget=budget)
    _git(repo, "bundle", "verify", str(bundle), budget=budget)
    _git(repo, "fetch", "--no-tags", str(bundle), commit, budget=budget)
    actual = _git(repo, "rev-parse", "FETCH_HEAD^{commit}", budget=budget)
    tree = _git(repo, "rev-parse", "FETCH_HEAD^{tree}", budget=budget)
    if actual != commit or (expected_tree is not None and tree != expected_tree):
        raise StateConflict(f"expected commit/tree {commit}/{expected_tree}; actual {actual}/{tree}")
    _git(repo, "fsck", "--strict", "--no-reflogs", commit, budget=budget)
    return repo, bundle, tree


def _assert_exact_closure(repo, commit, budget=None):
    """The imported object universe must equal the candidate's reachable objects."""
    present = set(_git(repo, "cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname)", budget=budget).split())
    reachable = {line.split()[0] for line in
                 _git(repo, "rev-list", "--objects", commit, budget=budget).splitlines() if line.strip()}
    if present != reachable:
        raise StateConflict("normalized Git bundle does not equal the candidate object closure")


def _validated_bundle(raw, commit, expected_tree=None, destination=None, exact=False, budget=None):
    """Git proves completeness in an empty repo before destination mutation."""
    with tempfile.TemporaryDirectory(prefix="state-git-artifact-") as directory:
        repo, bundle, tree = _import_bundle(Path(directory), raw, commit, expected_tree, budget)
        if exact:
            _assert_exact_closure(repo, commit, budget)
        if destination is not None:
            ref = "refs/aitelier/artifacts/" + commit
            try:
                with closed_git.repo_lock(destination):
                    # A literal local bundle is not a named remote. Reject URL
                    # rewriting before the closed transport consumes its path.
                    if any(k.startswith("url.") and k.endswith((".insteadof", ".pushinsteadof"))
                           for k, _ in closed_git.config_list(destination)):
                        raise closed_git.GitRefused("bundle destination config rewrites transport URLs")
                    closed_git.transport(destination, "fetch", "--no-tags", "--no-recurse-submodules",
                        "--no-auto-gc", "--", str(bundle), commit + ":" + ref,
                        profile=("file", ("protocol.file.allow=always",)), timeout=120)
                    actual = closed_git.out(destination, "rev-parse", ref + "^{commit}")
                    actual_tree = closed_git.out(destination, "rev-parse", ref + "^{tree}")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                raise StateConflict("Git artifact destination import refused: " + type(exc).__name__) from exc
            if actual != commit or actual_tree != tree:
                raise StateConflict(f"expected commit/tree {commit}/{tree}; actual {actual}/{actual_tree}")
        return tree


def _normalized_bundle(repo, root, commit, budget):
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
                timeout=budget, env=_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _unavailable("normalization", exc, budget) from exc
    if result.returncode:
        raise StateConflict("Git artifact normalization failed: "
                            + result.stderr[-1000:].decode(errors="replace").strip())
    header = f"# v2 git bundle\n{commit} {CANDIDATE_REF}\n\n".encode()
    return header + pack.read_bytes()


STATE_DB_PREFIX = "state-db:"
_TYPED_REF = re.compile(r"state-db:git-bundle:sha256:([0-9a-f]{64})")


def typed_ref(bundle_sha256):
    """The self-authenticating State DB reference for a normalized bundle digest."""
    return "state-db:git-bundle:sha256:" + bundle_sha256


def git_custody(retained_ref):
    """Pure classifier: ``(custody, legacy_cas_path, embedded_digest)``.

    ``state-db:`` is a reserved namespace; a malformed value there fails closed
    and is never reclassified as legacy/unrecognized. The error never echoes the
    stored reference."""
    if isinstance(retained_ref, str) and retained_ref.startswith(STATE_DB_PREFIX):
        match = _TYPED_REF.fullmatch(retained_ref)
        if match is None:
            raise StateConflict("malformed state-db custody reference")
        return "state_db_fullpack", None, match.group(1)
    if isinstance(retained_ref, str) and os.path.isabs(retained_ref):
        return "legacy_db_plus_cas_path", retained_ref, None
    return "unrecognized", None, None


def custody_metadata(row, release=None):
    """Metadata-only view of a state_git_artifacts row (no bytes): the four
    stored columns plus derived ``custody``, ``legacy_cas_path`` and
    ``released``. A valid typed reference must carry the row's own digest column.

    ``release`` is the ``state_git_bundle_releases`` row for this digest, when
    the caller has one (``bundle_release(conn, digest)``); it is the only thing
    that can tell a RELEASED candidate from one whose bytes are still held, and
    it is metadata only - answering it reads no payload. Without it the custody
    is what the row itself declares, exactly as before.
    """
    artifact = {k: row[k] for k in ("commit_sha", "tree_sha", "bundle_sha256", "retained_ref")}
    custody, legacy_path, embedded = git_custody(artifact["retained_ref"])
    if embedded is not None and embedded != artifact["bundle_sha256"]:
        raise StateConflict("custody reference does not match bundle digest")
    artifact["custody"] = "released" if release is not None else custody
    artifact["legacy_cas_path"] = legacy_path
    artifact["released"] = None if release is None else {
        k: release[k] for k in ("released_at", "actor", "rule")}
    return artifact


_IN_FLIGHT = {}  # (commit, producer bundle sha256) -> Future of the retained artifact
_IN_FLIGHT_LOCK = threading.Lock()


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
    # An identical report arriving while this one is being normalized waits for
    # its result instead of starting another: a caller whose own timeout fired
    # resends the same call, and one normalization costs minutes of CPU and
    # several hundred MiB.
    key = (commit, input_sha)
    with _IN_FLIGHT_LOCK:
        running = _IN_FLIGHT.get(key)
        if running is None:
            mine = _IN_FLIGHT[key] = Future()
    if running is not None:
        del raw
        return running.result()
    try:
        artifact = _normalize_candidate(raw, input_sha, commit)
        mine.set_result(artifact)
        return artifact
    except BaseException as exc:
        mine.set_exception(exc)
        raise
    finally:
        with _IN_FLIGHT_LOCK:
            del _IN_FLIGHT[key]


def _normalize_candidate(raw, input_sha, commit):
    with tempfile.TemporaryDirectory(prefix="state-git-candidate-") as directory:
        root, budget = Path(directory), _git_timeout()
        repo, _, tree = _import_bundle(root, raw, commit, budget=budget)
        normalized = _normalized_bundle(repo, root, commit, budget)
        if len(normalized) > MAX_BUNDLE_BYTES:
            raise StateConflict(f"normalized Git bundle exceeds {MAX_BUNDLE_BYTES >> 20} MiB")
        # Independent empty-repo proof of the normalized bytes before retention.
        if _validated_bundle(normalized, commit, tree, exact=True, budget=budget) != tree:
            raise StateConflict("normalized Git bundle changed the candidate tree")
        digest = hashlib.sha256(normalized).hexdigest()
    # The State DB BLOB is the only retained copy: no second bundle file is
    # written to the report store.
    return {"commit_sha": commit, "tree_sha": tree, "bundle_sha256": digest,
            "retained_ref": typed_ref(digest), "bundle_bytes": normalized,
            "input_bundle_sha256": input_sha}


BUNDLE_STORE = "state_git_bundle_store"
# A new bundle becomes a raw keyframe when its delta would exceed this share of
# its raw size, or when the chosen keyframe already carries this many deltas.
DELTA_MAX_FRACTION = 0.25
KEYFRAME_MAX_DEPENDENTS = 200
# Keyframes tried per new bundle: the most recent ones of comparable size
# (within 2x either way). Different repositories' candidates interleave, and a
# delta against another repository's keyframe is never useful.
KEYFRAME_CANDIDATES = 2
# Covers a dictionary of MAX_BUNDLE_BYTES plus a target of MAX_BUNDLE_BYTES (2**29 at 256 MiB).
# Frames written with a smaller window (28) still decode: the decoder only caps it.
_WINDOW_LOG = (2 * MAX_BUNDLE_BYTES - 1).bit_length()


def _zstd():
    try:
        import zstandard
    except ImportError as exc:
        raise StateConflict("Git bundle store codec unavailable: zstandard is not installed") from exc
    return zstandard


def _prefix_dict(zstd, base):
    return zstd.ZstdCompressionDict(base, dict_type=zstd.DICT_TYPE_RAWCONTENT)


def encode_delta(base, raw):
    """zstd frame of ``raw`` using ``base`` as a raw-content prefix dictionary.

    The match tables must cover the whole dictionary: the level presets index
    only a few MiB of it and find almost nothing in an 80 MB base, so the hash
    and chain sizes follow the base's size explicitly."""
    zstd = _zstd()
    log = max(17, min(27, len(base).bit_length() - 1))
    params = zstd.ZstdCompressionParameters(
        window_log=_WINDOW_LOG, hash_log=log, chain_log=log, search_log=4, min_match=5,
        strategy=zstd.STRATEGY_LAZY2, write_checksum=True, write_content_size=True)
    return zstd.ZstdCompressor(dict_data=_prefix_dict(zstd, base),
                               compression_params=params).compress(raw)


def decode_delta(base, payload, raw_size):
    """Inverse of ``encode_delta``; refuses anything but one frame of ``raw_size`` bytes."""
    zstd = _zstd()
    try:
        if zstd.frame_content_size(payload) != raw_size:
            raise StateConflict("Git bundle store delta declares a different size")
        # One frame, nothing after it (allow_extra_data=False raises otherwise).
        data = zstd.ZstdDecompressor(dict_data=_prefix_dict(zstd, base),
                                     max_window_size=1 << _WINDOW_LOG).decompress(
            payload, max_output_size=raw_size, allow_extra_data=False)
    except zstd.ZstdError as exc:
        raise StateConflict("Git bundle store delta does not decode: " + str(exc)[:200]) from exc
    if len(data) != raw_size:
        raise StateConflict("Git bundle store delta decodes to a different size")
    return data


_STORE_COLUMNS = "bundle_sha256,codec,base_sha256,raw_size,payload"


def _store_entry(conn, digest):
    # CAST: a payload damaged into another storage class is read as bytes and
    # then refused by the digest checks, never raised as a decoding error.
    row = conn.execute("SELECT bundle_sha256,codec,base_sha256,raw_size,CAST(payload AS BLOB) "
                       f"FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (digest,)).fetchone()
    return None if row is None else tuple(row)


def _keyframe_bytes(entry, digest):
    """A verified raw keyframe's bytes, or StateConflict."""
    if entry is None:
        raise StateConflict("Git bundle store delta base is missing")
    key, codec, base, size, payload = entry
    if key != digest or codec != "raw" or base is not None:
        raise StateConflict("Git bundle store delta base is not a raw keyframe")
    payload = bytes(payload)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise StateConflict("Git bundle store delta base does not match its digest")
    return payload


def load_stored_bundle(conn, digest):
    """The exact bytes stored under ``digest``, verified; StateConflict on any defect.

    A digest State RELEASED is not a missing one: the release record is read and
    the refusal names it (when, who, under which rule), so a caller can never
    mistake a deliberate retirement for corruption or a hole."""
    entry = _store_entry(conn, digest)
    if entry is None:
        released = bundle_release(conn, digest)
        if released is not None:
            raise StateConflict(_released_refusal(digest, released))
        raise StateConflict("Git bundle store has no bytes for this digest")
    key, codec, base_digest, size, payload = entry
    if key != digest or not isinstance(size, int) or not 0 < size <= MAX_BUNDLE_BYTES \
            or not isinstance(payload, (bytes, bytearray)):
        raise StateConflict("Git bundle store row is malformed")
    if codec == "raw" and base_digest is None:
        data = bytes(payload)
    elif codec == "zstd-prefix" and isinstance(base_digest, str) and base_digest != digest:
        base = _keyframe_bytes(_store_entry(conn, base_digest), base_digest)
        data = decode_delta(base, bytes(payload), size)
    else:
        raise StateConflict("Git bundle store row has an unknown codec or base")
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise StateConflict("Git bundle store bytes do not match their digest")
    return data


def retained_bundle_bytes(conn, artifact):
    """The bytes a ``state_git_artifacts`` row retains, NOT yet digest-checked.

    Non-empty ``bundle_bytes`` are returned as stored (legacy and unmigrated
    rows, unchanged behaviour). Empty bytes are resolved from the store only for
    the custody shapes that keep their bytes in the State DB; an unrecognized
    row keeps today's behaviour (empty bytes, refused by the digest check)."""
    raw = bytes(artifact["bundle_bytes"])
    if raw:
        return raw
    custody, _, _ = git_custody(artifact["retained_ref"])
    if custody not in ("state_db_fullpack", "legacy_db_plus_cas_path"):
        return raw
    return load_stored_bundle(conn, artifact["bundle_sha256"])


def store_bundle(conn, digest, raw):
    """Insert-once: ensure the store holds ``raw`` under ``digest``.

    Picks a keyframe (most recent raw keyframes of comparable size with fewer
    than KEYFRAME_MAX_DEPENDENTS deltas), keeps the first delta no larger than
    DELTA_MAX_FRACTION of the raw size that decodes back to exactly ``raw``,
    and otherwise stores ``raw`` as a new keyframe. Returns the stored
    ``(codec, payload_size)``. Runs in the caller's transaction."""
    raw = bytes(raw)
    if not raw or len(raw) > MAX_BUNDLE_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise StateConflict("Git bundle store input does not match its digest")
    existing = _store_entry(conn, digest)
    if existing is not None:
        if load_stored_bundle(conn, digest) != raw:
            raise StateConflict("Git bundle store already holds different bytes for this digest")
        return existing[1], len(existing[4])
    keyframes = conn.execute(
        f"SELECT k.bundle_sha256 FROM {BUNDLE_STORE} k WHERE k.codec='raw' "
        "AND k.raw_size BETWEEN ? AND ? AND (SELECT count(*) FROM "
        f"{BUNDLE_STORE} d WHERE d.base_sha256=k.bundle_sha256) < ? "
        "ORDER BY k.rowid DESC LIMIT ?",
        ((len(raw) + 1) // 2, len(raw) * 2, KEYFRAME_MAX_DEPENDENTS, KEYFRAME_CANDIDATES)).fetchall()
    for (base_digest,) in keyframes:
        try:
            base = _keyframe_bytes(_store_entry(conn, base_digest), base_digest)
        except StateConflict:
            continue  # a damaged keyframe is never used as a base
        payload = encode_delta(base, raw)
        if len(payload) > len(raw) * DELTA_MAX_FRACTION:
            continue
        try:
            exact = decode_delta(base, payload, len(raw)) == raw
        except StateConflict:
            exact = False
        if exact:
            conn.execute(f"INSERT INTO {BUNDLE_STORE} ({_STORE_COLUMNS},created_at) "
                         "VALUES(?,'zstd-prefix',?,?,?,?)",
                         (digest, base_digest, len(raw), payload, now()))
            return "zstd-prefix", len(payload)
    conn.execute(f"INSERT INTO {BUNDLE_STORE} ({_STORE_COLUMNS},created_at) "
                 "VALUES(?,'raw',NULL,?,?,?)", (digest, len(raw), raw, now()))
    return "raw", len(raw)


def store_candidate(conn, artifact):
    # In-memory checks first; a failure rolls back the whole write transaction.
    raw = artifact.get("bundle_bytes")
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise StateConflict("Git artifact bundle bytes are missing")
    raw = bytes(raw)
    digest = artifact.get("bundle_sha256")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(raw).hexdigest() != digest):
        raise StateConflict("Git artifact bundle bytes do not match bundle digest")
    if artifact.get("retained_ref") != typed_ref(digest):
        raise StateConflict("custody reference does not match bundle digest")
    for key in ("commit_sha", "tree_sha"):
        value = artifact.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
            raise StateConflict(f"Git artifact {key} must be 40 lowercase hex")
    old = conn.execute("SELECT tree_sha FROM state_git_artifacts WHERE commit_sha=?",
                       (artifact["commit_sha"],)).fetchone()
    if old:
        # Insert-once: the existing row's bytes and reference stay authoritative.
        if old["tree_sha"] != artifact["tree_sha"]:
            raise StateConflict("retained commit has conflicting tree identity")
        return
    # The bytes go to the content-addressed store (verified before insertion);
    # the row keeps every column, with an empty bundle_bytes. Encoding runs in
    # this write transaction: about 2-3 s per keyframe tried for an 80 MB bundle.
    store_bundle(conn, digest, raw)
    artifact = {**artifact, "bundle_bytes": b""}
    conn.execute("INSERT INTO state_git_artifacts "
                 "(commit_sha,tree_sha,bundle_sha256,retained_ref,bundle_bytes,created_at) "
                 "VALUES(?,?,?,?,?,?)", tuple(artifact[k] for k in (
                     "commit_sha", "tree_sha", "bundle_sha256", "retained_ref", "bundle_bytes")) + (now(),))


def materialize_candidate(store, commit, destination):
    """Return identity for a known external candidate; refuse missing legacy data.

    Four outcomes, not three: present bytes (fetched and verified into
    ``destination``), a legacy candidate with no retained bundle ("needs
    re-attestation"), a candidate whose bytes State RELEASED, and a candidate
    State never held. A released commit is never reported as a missing legacy
    bundle - that message tells the director to re-attest a candidate State
    deliberately retired."""
    with store.transaction() as conn:
        known = conn.execute("SELECT 1 FROM state_attempts WHERE execution_kind='external' "
                             "AND artifact_kind='git-sha1' AND artifact_ref=? LIMIT 1", (commit,)).fetchone()
        artifact = conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?", (commit,)).fetchone()
        if artifact is not None:
            artifact = dict(artifact)
            # A malformed reserved state-db: reference fails closed before any fallback.
            _, _, embedded = git_custody(artifact["retained_ref"])
            # Metadata first: a released digest is answered from its release
            # record, never by attempting a payload read it cannot satisfy.
            released = bundle_release(conn, artifact["bundle_sha256"])
            if released is not None:
                raise StateConflict(f"expected Git artifact {commit}, bundle {artifact['bundle_sha256']}; "
                                    f"actual {_released_refusal(artifact['bundle_sha256'], released)}")
            try:
                raw = retained_bundle_bytes(conn, artifact)
            except StateConflict as exc:
                raise StateConflict(f"expected Git artifact {commit}, bundle {artifact['bundle_sha256']}; "
                                    f"actual {exc}") from exc
    if artifact is None:
        if known:
            raise StateConflict(f"expected Git artifact {commit}; actual durable bundle missing (legacy candidates need re-attestation)")
        return None
    del artifact["bundle_bytes"]
    actual = hashlib.sha256(raw).hexdigest()
    if actual != artifact["bundle_sha256"]:
        raise StateConflict(f"expected Git artifact {commit}, bundle {artifact['bundle_sha256']}; actual {actual}")
    if embedded is not None and embedded != actual:
        raise StateConflict("custody reference does not match bundle digest")
    if not destination:
        raise StateConflict(f"expected Git artifact {commit}; actual source repository missing")
    _validated_bundle(raw, commit, artifact["tree_sha"], destination)
    return artifact


# --------------------------------------------------------------- release path
#
# The owner's ruling (2026-11-20): a retained bundle may leave the ledger when
# THESE BYTES HAVE NO LIVE CONSUMER ANY MORE - not when nothing names the commit.
# Every historical identity row (attempt, observation, evidence, unprotected
# history link, event payload) keeps naming the commit after a release; a rule
# written as "no reference anywhere" would select nothing at all. The five live
# classes are the ones that can still ASK State for the bytes, and each is one
# NOT EXISTS in the one deciding statement below:

RELEASE_RULE = "no-live-consumer@1"
RELEASE_TABLE = "state_git_bundle_releases"
CUSTODY_VIEW = "state_git_bundle_custody"

# THE deciding statement. Not a summary of the rule and not a parallel
# reimplementation of the reader's tables: the release function takes its
# digests from here and its facts from the row it selected.
#
# Pinned by tests/unit/test_bundle_release_rule_is_executable_sql.py: this text
# carries the version string of the rule it implements, and that test freezes
# the text's digest. A semantic edit must therefore edit this constant AND
# ``RELEASE_RULE`` (a release row records the rule that decided it, so a rule
# that changed silently would make the ledger say something false).
RELEASE_RULE_SQL = """
-- no-live-consumer@1: releasable only while no live consumer can still ask for these bytes
SELECT a.commit_sha, a.bundle_sha256, a.created_at AS artifact_created_at,
       s.codec, s.base_sha256, s.raw_size, length(s.payload) AS payload_bytes
FROM state_git_artifacts a
JOIN state_git_bundle_store s ON s.bundle_sha256 = a.bundle_sha256
WHERE (? = 0 OR julianday(a.created_at) < julianday('now', ?))
  -- 1. an acceptance receipt: accepted work is why the ledger exists
  AND NOT EXISTS (SELECT 1 FROM state_acceptances c WHERE c.artifact_ref = a.commit_sha)
  -- 2. a NON-TERMINAL attempt: an in-flight candidate can still be relayed
  AND NOT EXISTS (SELECT 1 FROM state_attempts t WHERE t.artifact_ref = a.commit_sha
                    AND t.status IN ('reserved','launching','running','paused','unknown'))
  -- 3. a protected history link that was never retired: "this must not be lost"
  AND NOT EXISTS (SELECT 1 FROM state_history_links h
                    WHERE (h.artifact_ref = a.commit_sha
                           OR (h.kind = 'commit' AND h.ref = a.commit_sha))
                      AND h.protection = 1
                      AND NOT EXISTS (SELECT 1 FROM state_context_run_protection_retirements r
                                        WHERE r.project_id = h.project_id
                                          AND r.reference_id = h.reference_id))
  -- 4. an OPEN worktree base: a tree still exists on this commit
  AND NOT EXISTS (SELECT 1 FROM run_isolation i
                    WHERE i.base_sha = a.commit_sha AND i.released_at IS NULL)
  -- 5. a base request that can still take effect. ``run_isolation_requests`` is
  -- INSERT OR REPLACE per project and is never deleted after the worktree it
  -- asked for is provisioned, so most rows are dead history: a request whose
  -- project already has a worktree AT EXACTLY THIS BASE can never apply again,
  -- and the honoured request is the strongest evidence the commit exists
  -- elsewhere. Measured on the 2026-11-20 snapshot: 661 of 684 rows honoured,
  -- the same 661 under either reading, so the narrower one costs nothing.
  AND NOT EXISTS (SELECT 1 FROM run_isolation_requests q WHERE q.base_sha = a.commit_sha
                    AND NOT EXISTS (SELECT 1 FROM run_isolation h2
                                      WHERE h2.project_id = q.project_id
                                        AND h2.base_sha = q.base_sha))
ORDER BY a.bundle_sha256
"""

# The host's worktree tables (core/db_manager.py). A State-only database has
# none, and their ABSENCE is exactly "no worktree exists, no base was requested".
_HOST_WORKTREE_TABLES = {
    "run_isolation": ("run_id", "project_id", "base_sha", "released_at"),
    "run_isolation_requests": ("project_id", "base_sha"),
}


def _prepare_release_connection(conn) -> None:
    """Make the one deciding statement runnable on a State-only database too.

    A State-only database (``core/state_database.py``) has no ``run_isolation``
    or ``run_isolation_requests``: they are host tables. An empty TEMP
    stand-in of the same name is the exact truth there, and lets ONE statement
    serve both deployments instead of a second, quietly different predicate.

    A TEMP object that already shadows either name is REFUSED: it is a partial
    copy (an untrusted connection carries only the opened projects' rows), and
    judging the store through it would release bytes a live worktree still
    needs.
    """
    for table, columns in _HOST_WORKTREE_TABLES.items():
        empty = ", ".join(f"NULL AS {column}" for column in columns)
        # SQLite stores a TEMP object's SQL without the TEMP and IF NOT EXISTS
        # words, so this is the exact text an earlier call on this connection left.
        substitute = f"CREATE VIEW {table} AS SELECT {empty} WHERE 0"
        host = conn.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?",
                            (table,)).fetchone()
        temp = conn.execute("SELECT type,sql FROM temp.sqlite_master WHERE name=?", (table,)).fetchone()
        if temp is not None:
            if temp[0] != "view" or temp[1] != substitute:
                raise StateConflict(f"refusing to judge the Git bundle store through a TEMP {table}; "
                                    "release on a connection without a projected copy")
            if host is None:
                continue      # our own empty stand-in: absent really does mean absent
            # The host table appeared after an earlier call made the stand-in;
            # a shadowing empty view would now hide real worktree rows.
            conn.execute(f"DROP VIEW temp.{table}")
        if host is None:
            conn.execute(f"CREATE TEMP VIEW {table} AS SELECT {empty} WHERE 0")


def bundle_release(conn, digest):
    """The append-only release record for ``digest``, or None. Never reads a byte.

    A database that predates the release ledger has no such table; that is "no
    release was ever recorded", which is what None means here."""
    try:
        row = conn.execute(f"SELECT bundle_sha256,commit_sha,codec,base_sha256,raw_size,payload_bytes,"
                           f"released_at,actor,rule FROM {RELEASE_TABLE} WHERE bundle_sha256=?",
                           (digest,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    keys = ("bundle_sha256", "commit_sha", "codec", "base_sha256", "raw_size", "payload_bytes",
            "released_at", "actor", "rule")
    return dict(zip(keys, tuple(row), strict=True))


def _released_refusal(digest, release):
    """The ONE refusal for bytes State deliberately retired (not missing)."""
    return (f"Git bundle {digest} was released {release['released_at']} by {release['actor']} "
            f"under {release['rule']}; the identity row remains and a new candidate must "
            "supply the bytes again")


def store_totals(conn):
    """``(rows, payload_bytes)`` over the whole store.

    Deliberately the only whole-store read the release does: ``payload`` holds
    the bytes, so this walks them (measured 39 s over 2,767 rows / 12.39 GiB on
    the production snapshot). It is the store-wide counter the ledger carries,
    and it is what makes "store + releases = what the store held before" a
    checkable identity instead of a claim."""
    row = conn.execute(f"SELECT count(*), coalesce(sum(length(payload)),0) FROM {BUNDLE_STORE}").fetchone()
    return int(row[0]), int(row[1])


def _nonnegative(value, name, *, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateConflict(f"{name} must be a non-negative integer")
    return value


def release_plan(conn, *, window_days=0, limit=None, max_payload_bytes=None):
    """The digests a release under ``RELEASE_RULE`` would take, and what it pins.

    The single place the decision is made: ``release_bundles`` executes THIS
    plan (so a dry run cannot disagree with the release that follows it), and
    the operator tool's projection is this plan's own result set.

    Order is the execution order and it is load-bearing: every DELTA first, then
    every keyframe. A delta's base is declared ``REFERENCES
    state_git_bundle_store`` and every State connection runs ``PRAGMA
    foreign_keys=ON``, so deleting a keyframe a delta still points at does not
    silently strand the delta - SQLite refuses it. Deltas first makes that
    refusal unreachable, and a keyframe whose own bytes a SURVIVING delta needs
    is reported as ``pinned`` and not selected at all.
    """
    _prepare_release_connection(conn)
    window_days = _nonnegative(window_days, "window_days")
    limit = _nonnegative(limit, "limit", allow_none=True)
    if max_payload_bytes is not None and _nonnegative(max_payload_bytes, "max_payload_bytes") == 0:
        raise StateConflict("max_payload_bytes must be positive")
    rows = [dict(row) for row in conn.execute(
        RELEASE_RULE_SQL, (window_days, f"-{window_days} days"))]
    edges = [(row[0], row[1]) for row in conn.execute(
        f"SELECT base_sha256, bundle_sha256 FROM {BUNDLE_STORE} WHERE codec='zstd-prefix'")]
    eligible = {row["bundle_sha256"] for row in rows}
    # A keyframe whose bytes a delta outside this decision still needs stays.
    pinned = {base for base, digest in edges if base is not None and digest not in eligible}
    by_digest = {row["bundle_sha256"]: row for row in rows}
    releasable = [row for row in sorted(rows, key=lambda r: (r["codec"] != "zstd-prefix",
                                                             r["bundle_sha256"]))
                  if row["bundle_sha256"] not in pinned]
    selected, payload, raw_size = [], 0, 0
    for row in releasable:
        if limit is not None and len(selected) >= limit:
            break
        if selected and max_payload_bytes is not None and payload + row["payload_bytes"] > max_payload_bytes:
            break
        selected.append(row)
        payload += int(row["payload_bytes"])
        raw_size += int(row["raw_size"])
    # A release removes the STORE's copy. A pre-migration row that still holds
    # its own ``bundle_bytes`` would keep the candidate readable through
    # ``retained_bundle_bytes`` while the ledger said "released", so the plan
    # refuses instead of writing a record that is false about the bytes.
    inline = [row["commit_sha"] for row in selected
              if conn.execute("SELECT length(bundle_bytes) FROM state_git_artifacts "
                              "WHERE commit_sha=?", (row["commit_sha"],)).fetchone()[0]]
    if inline:
        raise StateConflict(
            f"{len(inline)} selected artifact(s) still carry their own bundle_bytes "
            f"(first: {inline[0]}); migrate them into the store with "
            "tools/migrate_bundle_store.py before releasing")
    selected_digests = {row["bundle_sha256"] for row in selected}
    # The order guarantee above, checked rather than assumed: a selected
    # keyframe whose delta survives would be refused by the foreign key halfway
    # through the batch.
    stranded = {base for base, digest in edges if digest not in selected_digests and base is not None}
    clash = sorted(digest for digest in selected_digests if digest in stranded)
    if clash:
        raise StateConflict(f"release plan would strand a delta under keyframe(s) {clash}")
    return {
        "rule": RELEASE_RULE, "window_days": window_days, "limit": limit,
        "max_payload_bytes": max_payload_bytes,
        "rows": len(selected), "payload_bytes": payload, "raw_size": raw_size,
        "deltas": sum(1 for row in selected if row["codec"] == "zstd-prefix"),
        "keyframes": sum(1 for row in selected if row["codec"] == "raw"),
        "digests": [row["bundle_sha256"] for row in selected],
        "selected": selected,
        "pinned": [by_digest[digest] for digest in sorted(pinned) if digest in by_digest],
        "eligible_rows": len(rows), "eligible_payload_bytes": sum(int(row["payload_bytes"]) for row in rows),
        "truncated": len(selected) < len(releasable),
    }


def release_bundles(conn, *, actor, rule=RELEASE_RULE, window_days=0, limit=200,
                    max_payload_bytes=MAX_BUNDLE_BYTES, dry_run=False, admin=False):
    """Release the bundles ``release_plan`` selects, in ONE transaction.

    What happens, in order, all inside the caller's (or this call's) write
    transaction:

    1. the plan is computed (``release_plan``, the one deciding statement);
    2. the store-wide ``(rows, payload)`` counters are read;
    3. one ``state_git_bundle_releases`` row per digest is INSERTED - before any
       DELETE, because the narrowed ``state_git_bundle_store_no_delete`` guard
       refuses a delete that no release row names, and because the release must
       be recorded even if the delete never runs. Each row carries the store-wide
       ``rows_before``/``payload_before`` of the instant just before that row left
       and ``rows_after``/``payload_after`` of the instant just after, so the
       records CHAIN: after(k) == before(k+1), the first ``before`` is the store
       before the release and the last ``after`` is the store today;
    4. the digests are DELETED in the plan's order: deltas first, keyframes last;
    5. the counters are re-read and compared with what the ledger just claimed
       (``rows_after``/``payload_after``). A batch that does not close is rolled
       back whole, so "released" can never be a smaller number than the bytes
       that actually left.

    ``dry_run=True`` writes nothing and returns the plan. Writing needs the
    ADMIN DOOR: the same ``ClaimError('admin_required')`` the rest of State uses
    (``core.state_portfolio.retire_context_run_protection``), so no route, tool
    or future caller can move bytes out of the ledger by accident. ``rule`` must
    be the rule this code executes: a row claiming a different version would
    make the ledger describe a decision that was never made.

    ``actor`` is recorded and is required; there is no anonymous release.
    """
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise StateConflict("a release must name its actor")
    if rule != RELEASE_RULE:
        raise StateConflict(f"release records the rule it executes; this code executes {RELEASE_RULE}")
    if not dry_run and admin is not True:
        from core.state_claims import ClaimError
        raise ClaimError("admin_required", "only an authenticated admin may release retained Git bundle bytes")
    if dry_run:
        plan = release_plan(conn, window_days=window_days, limit=limit,
                            max_payload_bytes=max_payload_bytes)
        return {**plan, "dry_run": True, "actor": actor,
                "released_rows": 0, "released_payload_bytes": 0, "released_raw_size": 0,
                "rows_before": None, "payload_before": None,
                "rows_after": None, "payload_after": None}
    own = not conn.in_transaction
    if own:
        # Selection and write in ONE write transaction: the rule is judged
        # against the database the deletes will land in, never against a
        # snapshot another writer has already overtaken.
        conn.execute("BEGIN IMMEDIATE")
    try:
        plan = release_plan(conn, window_days=window_days, limit=limit,
                            max_payload_bytes=max_payload_bytes)
        if not plan["selected"]:
            if own:
                conn.commit()
            return {**plan, "dry_run": False, "actor": actor,
                    "released_rows": 0, "released_payload_bytes": 0, "released_raw_size": 0,
                    "rows_before": None, "payload_before": None,
                    "rows_after": None, "payload_after": None}
        rows_before, payload_before = store_totals(conn)
        released_at = now()
        # Each row is one instant in the ledger: what the store held just before
        # THAT row left, and just after. Consecutive rows therefore chain
        # (after(k) == before(k+1)) across batches as well as inside one, and the
        # only whole-store reads are the first ``before`` and the final ``after``.
        current_rows, current_payload = rows_before, payload_before
        for row in plan["selected"]:
            payload = int(row["payload_bytes"])
            conn.execute(
                f"INSERT INTO {RELEASE_TABLE}(bundle_sha256,commit_sha,codec,base_sha256,raw_size,"
                "payload_bytes,released_at,actor,rule,rows_before,payload_before,rows_after,payload_after) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["bundle_sha256"], row["commit_sha"], row["codec"], row["base_sha256"],
                 int(row["raw_size"]), payload, released_at, actor, rule,
                 current_rows, current_payload, current_rows - 1, current_payload - payload))
            current_rows -= 1
            current_payload -= payload
        for row in plan["selected"]:
            conn.execute(f"DELETE FROM {BUNDLE_STORE} WHERE bundle_sha256=?", (row["bundle_sha256"],))
        rows_after, payload_after = store_totals(conn)
        if (rows_after, payload_after) != (current_rows, current_payload):
            raise StateConflict(
                f"release accounting does not close: the ledger claims {(current_rows, current_payload)} "
                f"but the store holds {(rows_after, payload_after)}")
        if own:
            conn.commit()
    except BaseException:
        if own:
            conn.rollback()
        raise
    return {**plan, "dry_run": False, "actor": actor, "released_at": released_at,
            "released_rows": plan["rows"], "released_payload_bytes": plan["payload_bytes"],
            "released_raw_size": plan["raw_size"],
            "rows_before": rows_before, "payload_before": payload_before,
            "rows_after": rows_after, "payload_after": payload_after}
