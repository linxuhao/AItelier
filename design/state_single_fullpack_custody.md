# State single full-pack custody for portable Git candidates

Status: SOURCE CANDIDATE DESIGN, 2026-10-23. Frozen main
`b0d664e295a5af0fbbbf3e0a0b042f7802dc2f45`. Not implemented, not
independently accepted, not installed. Nothing here claims a capacity fix.
`design/state_shared_git_custody.md` (per-object sharing) is a separate,
unimplemented alternative; it is neither edited nor superseded by this file.

## 1. Decision

Keep what works: `core/state_git_artifacts.py` normalizes a producer bundle
into a candidate-only v2 bundle (hand-built header + one
`git pack-objects --revs --no-reuse-delta --window=10 --depth=50` pack, so Git
delta compression is retained) and proves it twice in empty repositories
(`_import_bundle`, then `_validated_bundle(..., exact=True)` closure equality).
`state_git_artifacts.bundle_bytes` stays the one authoritative copy.

Change one thing for FUTURE rows only: stop calling `retain_report(...)` for the
bundle, so no second filesystem-CAS copy is written. `retained_ref` for a future
row becomes a typed, self-authenticating DB reference, not a path.

No per-object canonical pool, no SQLite VFS, no daemon, no cleanup/GC, no
startup rewrite, no migration, no network fallback, no new token/gate, no HTTP
or public blob download. **No schema change**: `retained_ref TEXT NOT NULL` and
the `state_git_artifacts_no_update/no_delete` triggers
(`core/state_attempt_schema.py` 68-78) are untouched; the column's
*interpretation* is versioned by value shape.

## 2. Grounded current state

| Site | Today |
|---|---|
| `retain_candidate` (state_git_artifacts.py:119-151) | after normalization writes `normalized.bundle` to a temp dir, calls `retain_report(..., completed=False)` -> second copy in the report CAS, returns its **absolute path** as `retained_ref`. This runs *before* the State write transaction, so a later refusal leaves an orphan CAS file. |
| `store_candidate` (155-165) | in the observation write transaction; insert-once by `commit_sha`; existing row with same tree -> silent return (row never updated). |
| `materialize_candidate` (168-186) | reads `bundle_bytes` from the DB row, checks `sha256 == bundle_sha256`, `_validated_bundle` into destination. **Never reads `retained_ref`.** Missing row for a known external candidate -> `StateConflict` "durable bundle missing". |
| `state_service.get_attempt` (~1261) | returns `git_artifact = {commit_sha, tree_sha, bundle_sha256, retained_ref}` (no bytes). |
| `state_privacy.py` 244-250 | public projection of opened projects' rows: same four fields, `bundle_bytes`/`created_at` NULL. |
| `state_external.py` 186-187, 239-240 | only producer of rows: `retain_candidate` then `store_candidate`. |
| `_materialize_external_base` (state_service.py 611-629) | only runtime consumer of `materialize_candidate`. |

Path-contract consumers actually in this repo: **exactly one**,
`tests/integration/test_state_git_candidate_only_custody.py:112`
(`open(artifact["retained_ref"], "rb")`). Other uses only pass the value
through or compare equality (same file :142; `tests/support/state_canaries.py`
inserts the literal `refs/vault/canary`; `test_git_artifact_rows_are_private.py`
checks projection shape). `docs/state-external-harness.md` 143-150 describes
normalization but never promises a path. External operators/readers that may
`open()` the returned `retained_ref` are **unknown, not proven absent**; the
transition below is therefore explicit and detectable, not silent.

Unrelated, not changed: `state_external_report_blobs.retained_ref` (report JSON
path; `state_attempts.py`, `state_report_integrity.py`) and the WritingBench
`retained_ref` (`aitelier/writing_bench/bench.py:509`, different custody). The
report JSON copy of a candidate report is still written; only the *bundle* copy
disappears.

## 3. Reference representation (future rows)

```
retained_ref = "state-db:git-bundle:sha256:" + bundle_sha256     # 64 lowercase hex
```

Meaning: "the bytes are the `bundle_bytes` BLOB of the `state_git_artifacts`
row for this `commit_sha`, and their SHA-256 is this digest." It is not
absolute, has no `/`, is never passed to `open()`; no filesystem path is
invented. The digest is over the normalized full-pack bytes (header + pack),
exactly `bundle_sha256` as today.

Rows are classified by value shape with a pure helper
`git_custody(retained_ref)` in `state_git_artifacts.py`:

| shape | `custody` | `legacy_cas_path` |
|---|---|---|
| `^state-db:git-bundle:sha256:[0-9a-f]{64}$` | `state_db_fullpack` | `null` |
| absolute path (`os.path.isabs`) | `legacy_db_plus_cas_path` | the stored value |
| anything else (e.g. test canary) | `unrecognized` | `null` |

### Current vs future literal metadata

Existing row today (never rewritten): the four fields below, without `custody`
and `legacy_cas_path`. After the transition the same row is returned with them:

```json
{"commit_sha":"<40hex>","tree_sha":"<40hex>","bundle_sha256":"<A>",
 "retained_ref":"/<report-store>/<shard>/<name>",
 "custody":"legacy_db_plus_cas_path","legacy_cas_path":"/<report-store>/<shard>/<name>"}
```
Future row:
```json
{"commit_sha":"<40hex>","tree_sha":"<40hex>","bundle_sha256":"<B>",
 "retained_ref":"state-db:git-bundle:sha256:<B>",
 "custody":"state_db_fullpack","legacy_cas_path":null}
```
`custody`/`legacy_cas_path` are computed in `get_attempt`, not stored (immutable
rows stay byte-identical). **Interface transition**: `retained_ref` is now an
opaque custody reference whose form is given by `custody`; the only filesystem
path field is `legacy_cas_path`, present solely for historical rows. A consumer
that opens `retained_ref` without checking `custody` gets `FileNotFoundError`
on a non-path string for future rows (loud, never wrong data).

### Privacy

`state_privacy.py` projection SQL is unchanged (already exposes `retained_ref`,
NULLs `bundle_bytes`); a typed ref discloses only the already-public
`bundle_sha256`. `custody`/`legacy_cas_path` are derived only in owner-side
`get_attempt`. No new download surface or permission; existing public legacy
paths are left as is.

## 4. Future intake transaction

Entry: `report_external_attempt(...)` -> `state_external.py` candidate branch.

1. Unchanged: report bytes verified/retained by `_terminal_report`; descriptor
   `{path, sha256}` validated; producer file read bounded/regular/nofollow and
   authenticated against its declared digest.
2. `retain_candidate`: import, normalize, empty-repo prove (exact closure), size
   cap `MAX_BUNDLE_BYTES`, `digest = sha256(normalized)`. **New:** no
   `retained_source` file and no `retain_report` call; the only writes are the
   `TemporaryDirectory`, removed on exit. Returns the same dict with
   `retained_ref = "state-db:git-bundle:sha256:" + digest`.
3. In the existing write transaction, `store_candidate` first re-checks
   in memory: `bundle_bytes` is non-empty `bytes`, `sha256(bundle_bytes) ==
   bundle_sha256`, `retained_ref == typed(bundle_sha256)`, 40-hex commit/tree.
   Any failure -> `StateConflict`; the transaction rolls back (no observation,
   no artifact row, no version bump).
4. Insert-once by `commit_sha` as today. If a row already exists (legacy or new)
   with the same tree, nothing is inserted or updated: its existing
   `retained_ref`/bytes stay authoritative even if another Git version would
   normalize differently. Conflicting tree -> `StateConflict` as today.
5. Observation row, report blob and artifact row commit atomically.

No-effects: any refusal before step 5 leaves the DB unchanged and creates **zero**
bundle filesystem entries (today a refusal after `retain_report` can orphan a
CAS file). Concurrent intakes of one commit are serialized by the write
transaction; the second sees the first row. Idempotent replay of the same
observation payload returns before any of this.

## 5. Authoritative bytes and normal materialization

`materialize_candidate(store, commit, destination)` stays the only reader.
Additive, DB-only changes:

- Load row; `sha256(bytes) == bundle_sha256` (exists).
- If `custody(retained_ref) == state_db_fullpack`, also require
  `retained_ref == typed(bundle_sha256)`; else `StateConflict` "custody
  reference does not match bundle digest".
- Legacy/unrecognized rows: unchanged (bytes + digest only; the CAS file is
  still never consulted).
- Missing row for a known external candidate: unchanged "durable bundle missing
  (legacy candidates need re-attestation)". Corrupt bytes: digest-mismatch
  refusal. Missing destination: unchanged refusal.
- Git proves completeness/commit/tree in an empty repo before the destination is
  fetched into (`_validated_bundle`), so a bad row never mutates the destination.

Normal authorized export is this path (launch-time base materialization in
`_materialize_external_base`); no separate export API is added.

Backup-only cold handoff: a SQLite backup of the State DB alone holds
`commit_sha`, `tree_sha`, `bundle_sha256`, `retained_ref`, `bundle_bytes`. With
the producer repo, producer bundle and report-store directory absent,
`materialize_candidate` into an empty bare repo recovers the exact commit/tree;
the typed reference authenticates bundle identity (its embedded digest must
equal the column and the recomputed hash). Legacy rows keep this property.

## 6. Existing-row preservation and bounded migration cost

Preserved byte-for-byte: every existing `state_git_artifacts` row and BLOB, every
CAS bundle file, report JSON blobs, raw evidence, foreign/external owner rows,
attempts and observations. No deletion, rewrite, relink, dedup or cleanup of
historical CAS files; they are simply not referenced by new code, and are not
declared garbage. Only future rows change.

Cost evidence (private raw packet
`/home/linxuhao/.AItelier/director/reports/shared-object-cost-root-20261023`; no
private content copied here): accepted receipt
`5ff6d87e410745d1ad901c9379ae0e25`, artifact
`f061dd54e11346c41262484638184e8a0ed77eb286b3f04c1f81cb98ef5b8a87`, four current
State-associated candidates:

| measure | bytes |
|---|---|
| existing full-pack DB rows | 42,479,616 |
| existing CAS copies | 42,420,229 |
| existing DB + CAS | 84,899,845 |
| counterfactual full-pack-DB-only | 42,479,616 |
| independent zlib canonical-pool DB (rejected design) | 79,732,736 |

42,479,616 B is a **counterfactual** for those four rows had no CAS copy been
written. It is not implemented, not realized, not a production number and not a
projection: the sample is 16,781 unique objects of four adjacent AItelier
histories with high overlap, not globally representative. The pool alternative
saved 5,167,109 B (6.086%) of DB but its WAL was 80,187,592 B vs 10,699,672 B
baseline, and the director rejected it. This design claims no saving for old rows
(they keep both copies) and none for future rows beyond "no second bundle copy
is written".

## 7. Test and doc transition (implementation obligations; all UNRUN here)

- `tests/integration/test_state_git_candidate_only_custody.py:112-113`: replace
  the `open()` with assertions that `retained_ref == "state-db:git-bundle:sha256:"
  + artifact["bundle_sha256"]`, `not os.path.isabs(retained_ref)`, and that the
  report store gained no file for the bundle. Line 142 equality still holds.
- `tests/support/state_canaries.py` (`refs/vault/canary`) stays valid: classified
  `unrecognized`.
- Unit cases for `git_custody`: the three shapes plus a malformed typed value
  (wrong length/uppercase -> `unrecognized`).
- `docs/state-external-harness.md` ~149: future retained bundles are held only in
  State DB; `retained_ref` is a typed DB reference; historical rows keep a path
  in `legacy_cas_path`; consult `custody`.

## 8. Minimal real-entry conformance vectors

All through `report_external_attempt` / `get_attempt` / `materialize_candidate`
with a real broad bundle from a real repo (existing `fixture()`, `broad_bundle`,
`set_unrelated` helpers).

1. **New intake, no second CAS write.** Snapshot the report-store tree; intake a
   candidate whose producer bundle carries 1 MiB of unrelated random history.
   Expect only the report-JSON blob added; `custody=state_db_fullpack`,
   `legacy_cas_path=null`; stored bytes exclude unrelated objects. Kills today's
   second file.
2. **Existing path metadata unchanged.** Seed a legacy row (SQL, absolute path, as
   old code wrote) for commit X; intake a new attempt for X. Row bytes and
   `retained_ref` identical; `custody=legacy_db_plus_cas_path`; no new file.
   Kills rewriting legacy rows.
3. **New reference truthful.** `sha256(stored bytes) == bundle_sha256 ==` digest
   in `retained_ref`; `os.path.exists(retained_ref)` is False. Kills a fabricated
   path/URI pretending to be a file.
4. **Backup-only materialize.** SQLite backup of the DB to a fresh dir; delete
   producer repo, bundle and whole report store; open the copy;
   `materialize_candidate` into an empty bare repo: commit/tree equal,
   `git fsck --strict` clean, unrelated blob absent. Repeat with a tampered
   typed digest (triggers dropped in test) -> `StateConflict`.
5. **Rejected digest / missing bytes roll back.** (a) wrong producer digest ->
   "expected bundle"; (b) `store_candidate` with empty/None bytes or mismatching
   `bundle_sha256` -> `StateConflict`. Both: no artifact row, no observation,
   `observation_version` unchanged, report-store listing unchanged.
6. **Useful valid candidate (control).** Right after vector 5, the valid report
   for the same attempt/version succeeds, status `candidate`, and materializes.
   Proves refusals left no poison.

Accounting measure (not a claim): for N new intakes record
`sum(length(bundle_bytes))` of new-format rows and the count of bundle-named
files in the report store (expected 0); report apart from the section-6 figure.

## 9. Review and acceptance (distinct stages)

This file is a SOURCE CANDIDATE only. A non-author reviewer checks it against the
section-2 callers and cost evidence (unknown external readers stay unknown, not
absent); an independent acceptance-only readback precedes any dependency on it.
Implementation, installation and production capacity are separate later steps.

