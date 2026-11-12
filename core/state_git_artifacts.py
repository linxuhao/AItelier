"""Self-contained Git bundles retained with immutable State lineage.

The State database is the only carrier of future bundles: ``bundle_bytes`` holds
the one authoritative normalized full pack and ``retained_ref`` is the typed
reference ``state-db:git-bundle:sha256:<bundle_sha256>``. Historical rows keep
their absolute report-store path untouched. Neither run cleanup nor Git GC owns
these bytes.
There is deliberately no release operation: attempts/observations remain
recoverable, so State cannot yet prove that their artifacts are unreferenced.


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


def custody_metadata(row):
    """Metadata-only view of a state_git_artifacts row (no bytes): the four
    stored columns plus derived ``custody`` and ``legacy_cas_path``. A valid typed
    reference must carry the row's own digest column."""
    artifact = {k: row[k] for k in ("commit_sha", "tree_sha", "bundle_sha256", "retained_ref")}
    custody, legacy_path, embedded = git_custody(artifact["retained_ref"])
    if embedded is not None and embedded != artifact["bundle_sha256"]:
        raise StateConflict("custody reference does not match bundle digest")
    artifact["custody"] = custody
    artifact["legacy_cas_path"] = legacy_path
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
    """The exact bytes stored under ``digest``, verified; StateConflict on any defect."""
    entry = _store_entry(conn, digest)
    if entry is None:
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
    """Return identity for a known external candidate; refuse missing legacy data."""
    with store.transaction() as conn:
        known = conn.execute("SELECT 1 FROM state_attempts WHERE execution_kind='external' "
                             "AND artifact_kind='git-sha1' AND artifact_ref=? LIMIT 1", (commit,)).fetchone()
        artifact = conn.execute("SELECT * FROM state_git_artifacts WHERE commit_sha=?", (commit,)).fetchone()
        if artifact is not None:
            artifact = dict(artifact)
            # A malformed reserved state-db: reference fails closed before any fallback.
            _, _, embedded = git_custody(artifact["retained_ref"])
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
