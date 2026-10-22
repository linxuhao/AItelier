# Future State Git custody: one database object pool

Design proposal only. This does not change the accepted candidate-only bundle
implementation, enable new writes, authorize migration/deletion, or assert a
production capacity improvement. Its source baseline is
`ed685499f46c71f7cc6fbb742b3169e158ed7093`; reviewed merge
`b0d664e295a5af0fbbbf3e0a0b042f7802dc2f45` has the same tree.

## Choice and tradeoff

Select a database-owned pool of independently compressed canonical Git objects,
with one immutable root record per new candidate. Retain the existing full-bundle
reader for historical rows. This avoids a separate service, per-candidate closure
membership tables, external alternates and chains of incremental bundles.

Current candidate-only full packs use Git delta compression well, but repeat
common ancestry for each distinct commit and store both DB and CAS copies. A
shared pack/dependency scheme could preserve deltas, but introduces pack/base
selection, dependency closure, repacking and recovery identities. Independent
objects are a smaller first implementation. They can compress substantially
worse than Git packs; cross-candidate reuse must outweigh that loss and database
row/index/WAL overhead. Selection here is architectural, not a rollout decision.
If representative measurements show it is worse, do not enable it. Keep the
accepted full-bundle path rather than adding speculative pack infrastructure.

## Persistent identities

Add two private tables, without altering or rewriting `state_git_artifacts`:

- `state_git_objects`: `oid` (40 lowercase hex, primary key), `kind` (commit,
  tree or blob), `payload_size`, `raw_sha256`, `zlib_bytes`. Object format is
  SHA-1 Git, matching the existing 40-hex contract; other formats refuse.
- `state_git_object_candidates`: `commit_sha` (primary key), `tree_sha`,
  `closure_count`, `closure_sha256`, `created_at`, `encoding='objects-v1'`.

Canonical raw bytes are ASCII `<kind> <decimal payload_size>` followed by NUL
and the exact payload. `oid=SHA1(raw)`; `raw_sha256=SHA256(raw)`. Store one
zlib level-6 compressed raw value, not a delta. Verify decompressed length,
canonical header, type and both digests before use. Hash equality never replaces
byte comparison on a conflicting insert. Same OID with different canonical
bytes is a hard conflict, including a possible SHA-1 collision.

Closure identity is SHA256 of ASCII sorted unique OIDs, each followed by LF;
count is the number of those OIDs. It is not the producer bundle digest or a
new artifact identity: the externally declared artifact remains the commit SHA.
No project/path/ref labels enter the hash. Candidate roots and all stored
objects are retained indefinitely, including failed/unaccepted attempt history.
There is no release, reference-count deletion, GC or background janitor.

The new object bytes live only in the database; do not also write an object CAS.
Existing terminal-report JSON still uses the normal immutable report store.
The original producer path/digest remains in that report, exactly as today;
it is not promised portable and cannot become a recovery dependency. Existing
CAS and historical bundle rows remain KEEP and keep their existing identities.

## Authenticated intake and atomic publication

Keep the public candidate-report schema and current bounded regular/no-follow
producer read and SHA256 check. Authenticate even a duplicate candidate report;
a previously retained candidate must not make a bad new input pass. Continue
importing the self-contained bundle into an owned empty bare repository and
validating the exact commit/tree with Git. Enumerate only the selected commit's
reachable commits, trees and blobs; never enumerate producer refs as the scope.
A gitlink entry names another repository, is not a blob dependency, and remains
a gitlink exactly as in the current bundle contract. No submodule data is fetched.
Reject shallow/prerequisite/incomplete input. Tag objects and unrelated refs are
not part of a commit-rooted closure.

Before any State write transaction, extract the selected closure's canonical
objects from the validated temporary repository, verify IDs/types/lengths and
calculate the closure identity. Stage this in owned temporary storage. Preserve
the current 128 MiB authenticated input bound and 120-second Git operation
bounds. A separate explicit decompressed-data/object-count budget is required
because a small pack can expand greatly; its supported values must be measured
against existing useful candidates before enabling writes. Exhaustion refuses
before observation publication, names the exhausted limit, preserves the raw
failure and never silently falls back to a partial candidate.

In the same State transaction that publishes the candidate observation, insert
missing pool objects, then the immutable candidate root, then the observation.
The root is never visible with only part of its closure committed. If the old
bundle table already has this commit, preserve/read that row rather than writing
a second representation. For an existing new root require exact tree, closure
count and closure identity; for every existing object verify stored bytes and
both identities. A conflict refuses the whole observation. Do not overwrite a
row with `INSERT OR REPLACE` or trust `ON CONFLICT DO NOTHING` alone.

SQLite serializes concurrent writers: recheck existing identities in that same
transaction after off-transaction staging. Two equal candidates converge; two
candidates sharing objects add each shared row once. A failed insert, failed
COMMIT or cancelled publication rolls back the entire new root/observation.
Close/rollback the transaction on every error; retry through the existing
observation request/payload identity, without changing its terminal bytes.
A completed identical retry returns the original observation and root. A changed
payload using the same observation identity still refuses. A staged temporary
copy is not durable custody and cannot grant success. Normal temporary-directory
cleanup removes only that invocation's staging, never DB/CAS/history.

## Database-only cold recovery

A valid standalone backup uses SQLite's supported backup API, including the
committed WAL state; copying a live main DB file alone is not a backup contract.
On a cold host, read the requested immutable new root and traverse its commit
and tree graph in one consistent DB read snapshot. Load each object by OID,
perform bounded decompression and all canonical identity checks, parse parents,
commit root trees and binary tree entries. Track visited OIDs to terminate
shared/cyclic-corrupt graphs. Recompute the exact visited closure count/digest
and root tree. Missing rows, malformed links/types, unexpected counts/digests,
truncation, decompression excess or hash mismatch refuse with exact identity;
never fetch a remote, consult an alternate, replace it with HEAD or continue
with a smaller closure.

Only after this proof, write the verified canonical objects to a new owned empty
bare Git repository and run strict Git fsck from the candidate. Generate a
self-contained candidate-only pack/bundle and validate it in another empty bare
repository before touching the destination. Reuse the existing destination import
contract: import only `refs/aitelier/artifacts/<commit>` and verify exact commit
and tree; do not move shared HEAD, branches, index, global Git config or producer
refs. Partial destination transfer is not acceptance. This root's DB data is the
portable authority, not an ephemeral generated pack or its digest.

Read precedence is immutable legacy row first, new root second. Legacy rows
still check their original bundle digest and use the current materializer,
including broad older bundles. No on-read conversion or historical blob rewrite.
New candidates must be reachable through the same supported external-base
preflight and dependency-proof consumers; a helper-only demonstration is
insufficient. Explicit nonancestor review/test operands are separate authenticated
inputs. Candidate custody must not pretend to include them, infer producer
association from hashes, or delete their producer evidence.

## Privacy and grounding

Both new tables, including names/counts/identities and payload bytes, are private
State data. Add each to the actual `PRIVATE_STATE_TABLES` enforcement/projection
path before schema creation can expose them; deny untrusted direct, qualified,
view/join and reused-connection reads using the existing database authorizer.
No public API/raw DB projection may return their rows, including for public
projects. The same rule applies to any temporary generated bundle path; no new
public route is introduced. Report JSON/attempt visibility stays as currently
classified. Global deduplication does not authorize cross-project existence or
size probes. Trusted existing materialization can use shared bytes without
making object-pool queries a new user capability.

Current Source locations, to be re-read by the implementer:
`core/state_git_artifacts.py` implements `retain_candidate`, `_import_bundle`,
`_assert_exact_closure`, `store_candidate` and `materialize_candidate`.
`core/state_privacy.py` owns the actual `PRIVATE_STATE_TABLES` classification.
`core/state_external.py` authenticates/retains before its write transaction and
stores the candidate alongside terminal publication. `core/state_service.py`
`start_external_attempt` performs supported explicit-base preflight through
`_materialize_external_base`; its prior-candidate visibility account is built
before that materialization. `tests/integration/test_state_git_candidate_only_custody.py`
contains the real object-universe and DB-only recovery controls;
`tests/integration/test_state_git_artifact_durability.py` covers portability and
refusals; `tests/unit/test_git_artifact_rows_are_private.py` exercises actual
DB bytes and untrusted projection. These are current full-bundle behaviors,
not evidence that this new object representation already works.

## Literal two-candidate example

Use UTF-8 file payloads `a0\n`, `b0\n`, `a1\n`, `b1\n` and `unrelated\n`.
All tree entries are mode100644, names `a.txt` then `b.txt`; sentinel uses `u.txt`.
Author and committer are `Example <example@invalid>`, timestamp
`2000-01-01T00:00:00+0000`. Commit messages are `base\n`, `candidate one\n`,
`candidate two\n`, `unrelated\n`. C1 and C2 each have sole parent B;
B and U have no parents. T0=(a0,b0), T1=(a1,b0), T2=(a0,b1), TU=(u).

| Symbol | Exact Git OID |
|---|---|
| a0 | `0042f6c56d8fc1896f3efc2cdc5060e5b5e44e02` |
| b0 | `2270a80fbb71d7109b85e7489f3f307d0141a559` |
| a1 | `da0f8ed91a8f2f0f067b3bdf26265d5ca48cf82c` |
| b1 | `c9c6af7f78bc47490dbf3e822cf2f3c24d4b9061` |
| u | `c21c9352f7526e9576892a6631e0e8cf1fccd34d` |
| T0 | `d9d4c730de327e3a57400a4bb4803793c4fcfa34` |
| T1 | `040f77ef4218dad67962e4380496d5be61133340` |
| T2 | `c9a9a62930c2d4e16b48acd07255eba05733418a` |
| TU | `dec376d8d657c5c05d657b10cd83045e3a70aa82` |
| B | `380a5db0197e920035f79c24af1b75fa6657d112` |
| C1 | `2759d251c62d5f2b1f89249bb2dc4cf45afe1ac1` |
| C2 | `14d70fb05e97227255e88cb6fb2d0c2829d72311` |
| U | `817f01df097343209bbedfc99f5b3a24f9843c82` |

First intake C1 retains exactly `{C1,B,T1,T0,a1,a0,b0}` (7 object rows).
Second intake C2 requires `{C2,B,T2,T0,a0,b0,b1}` and adds only
`{C2,T2,b1}` (3 rows), plus one root row. Union has10 objects and2 roots;
`{U,TU,u}` never enters the pool even when both producer bundles advertise U.
Cold C1 recovery contains exactly its7 objects; cold C2 exactly its7, not all10.
The example construction and independent set/hash checks are delivery evidence;
its tiny payload sizes do not predict production savings.

## Measurement and implementation acceptance before rollout

Measured current normalization of one previously retained real broad bundle was
86,634,786 to81,611,134 bytes, about5.8%, taking12.36s, with child peak RSS
352,452KiB in a bounded offline container. This chose an advertised commit;
it is not an assertion of a live State producer association. Neither the much
smaller synthetic witness nor any single canary establishes cross-candidate reuse.

Compare current new-write cost `sum(2 * bundle_i)` (DB plus CAS, excluding report
metadata) with `sum(zlib(raw_unique_objects)) + object_rows/indexes + root_rows +
WAL/transaction overhead`. Legacy costs stay unchanged. Do not substitute Git
object counts for bytes. Similar large blobs may exploit pack deltas and have
very poor per-object reuse; independent compression might lose despite common
ancestry. Real overlap, compression loss, write latency, cold-recovery latency,
CPU/RSS and WAL growth are currently unknown for the pool.

A bounded implementation must pass the following actual useful paths and
counterexamples once, with independent final-source review:

1. The literal graph above, same-candidate retries, distinct overlapping candidates,
   unrelated packed/delta objects, all ancestor/tree/blob identities and exact
   cold object universes. Independent expected sets must not call the implementation.
2. Real normal authenticated intake and supported materialization from a DB-only
   backup with original bundle/CAS/source unavailable; strict Git recovery, useful
   dependency proof and separately authenticated historical operands. Legacy broad
   rows remain byte-for-byte/read-behavior unchanged.
3. Bad producer digest/symlink/nonregular/empty/thin/missing input still refuses,
   including after successful prior intake. Corrupt compressed object, forged
   type/length/hash, missing ancestor/blob, conflicting root and decompression
   limits refuse before destination/observation effects.
4. Concurrent overlap and same-observation retries; injected INSERT and COMMIT
   failure with transaction state, same-connection retry and independent-connection
   durability; no partial root/observation or collision overwrite.
5. Actual untrusted queries/projections for both new tables through ordinary and
   reused connection paths; no private bytes, identifiers or existence oracle.
6. Offline comparison on a fixed small set of already authorized, associated
   candidates: actual bytes including DB indexes/WAL, latency/RSS and cold restore,
   with immutable originals and raw failures retained. A two-candidate synthetic
   pass is insufficient rollout evidence. Do not collect all projects or rewrite
   old history to inflate measured overlap.

Enable future object writes only after this evidence shows a useful capacity
tradeoff under the existing memory/latency constraints. Otherwise keep the
accepted full-bundle implementation. No migration, deletion or cleanup authority
is implied by this design, even if a future sample shows large savings.
