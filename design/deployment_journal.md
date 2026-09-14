# Deployment journal v2

The deployment gate persists `deployment-quiescence/journal.json` and a separate
`journal.json.anchor.json` in the AItelier data directory. Both are private files.
Version 1 is never automatically loaded as authorization evidence or migrated.

## Integrity and crash contract

The journal retains the existing event schema, unique event IDs, immediate
predecessor links, legal state transitions, action/digest bindings, and exact
`latest` equality. A v2 envelope adds a random journal ID and a content-hash chain.
Each SHA-256 hashes canonical JSON of `{version: 2, journal_id, position,
previous_hash, event}`; the first previous hash is null and positions begin at 0.
The separate checkpoint binds the journal ID, event count, genesis hash, head
hash, and a hash of the complete journal, including any migration provenance.
Canonical JSON uses sorted keys, compact separators and ASCII escaping. Duplicate
JSON object keys and nonfinite numbers are rejected when reading either file.

All writers hold the existing exclusive journal lock. Each transaction atomically
replaces and fsyncs the checkpoint **before** atomically replacing and fsyncing the
journal. Each replacement first writes and fsyncs a private temporary file and then
renames it and fsyncs its parent directory. After both writes succeed, the pair is
durable. A crash after checkpoint publication but before journal publication leaves
a mismatch (or a checkpoint without a journal on initialization). Every load and
mutation refuses this state without repairing, replaying, or overwriting it. A
crash before checkpoint publication leaves the old committed pair. A crash after
both durable writes leaves the new committed pair, even if the caller did not
receive the return value. There is no automatic crash-recovery choice of a winner.

This detects accidental partial writes, single-file loss, changed event contents,
prefix/suffix truncation, and re-rooting/re-hashing a stale middle event while the
separate checkpoint remains intact. It also detects independently stale copies of
either file. It does **not** authenticate the files. Someone able to rewrite both
files can create a consistent forgery, and coordinated rollback of both files is
undetectable. Loss of the entire directory is indistinguishable from a new
installation. An external authenticated/append-only checkpoint is needed to cover
those threats; this format does not claim that protection.

Preserve a mismatched pair and temporary/backup evidence for investigation. Do not
remove the anchor, relabel a v2 file as v1, recompute a checkpoint, or keep retrying
a failed cutover. Restore a separately verified complete matching pair only after
an operator has resolved the interrupted operation. New authorization supersedes
and aborts a pending gate; reconciliation cannot replay or complete it. Existing
CLI fence release and clearance/observation snapshot controls remain in force.

## Explicit one-time migration

Inspect the full legacy journal read-only, establish its provenance, and obtain
its byte SHA-256. Stop deployment-journal writers while performing the migration.
The migration itself also takes their journal lock. Invoke the installed module:

```sh
python -m core.deployment_quiescence \
  --journal /absolute/path/to/deployment-quiescence/journal.json \
  --expected-sha256 REVIEWED_64_CHARACTER_SHA256 \
  --legacy-format deployed-v1 \
  --actor OPERATOR \
  --provenance 'Reviewed snapshot and deployment evidence reference'
```

Two explicit grammars are supported:

- `linked-v1`: the prior strict reader's complete, unique, linked sequence with
  valid status/pending/usable tuples and action/digest bindings. Empty v1 journals
  and the exact three-field aborted genesis are supported. Unknown envelope
  fields are refused. Missing links are never repaired in this mode.
- `deployed-v1`: the observed deployed producer grammar. Authorizations have
  event_id/at/action/status/usable/pending/inventory_digest/blockers/errors, plus
  exactly actor/reason/ticket audit fields for overrides. Refusals have
  event_id/at/action/status/usable/inventory_digest/blockers/errors/reason.
  These roots have no prior link. Completions have exactly
  event_id/at/action/status/usable/prior_event_id/replayed, must immediately
  follow their matching authorization, and must already claim usable=true and
  replayed=false. Only these absent fields are normalized: terminal pending=false,
  completion digest copied from its uniquely linked authorization, and root links
  copied from the immediately preceding event. Chronology must be nondecreasing
  with timezone-bearing timestamps. The resulting full sequence must also pass
  the strict reader. Other historical schemas are refused, not guessed.

Both modes refuse a sequence ending in an unsettled authorization. A valid legacy
sequence cannot prove that its original prefix was preserved: v1 has no durable
root. The explicit hash/provenance review is the one-time trust boundary, not a
claim of retroactive authentication. Ambiguous histories require investigation.

Before publishing v2, the migrator writes and fsyncs two byte-for-byte copies:
`journal.json.legacy-v1.backup` and the independently named
`journal.json.legacy-v1.source`. It then writes and fsyncs
`journal.json.migration-v1.json`, an immutable transaction record containing the
exact proposed v2 journal. Only after all three safe regular files are durable
does it publish the checkpoint and journal. The proposed and published v2
journal also contains the canonical base64 encoding of the exact v1 bytes; the
transaction, journal content hash and checkpoint therefore bind that recovery
image to the migrated history. The anchored journal records both source
filenames, the transaction filename, SHA-256, byte length, selected grammar,
actor, provenance and migration time. Existing different files are
never overwritten. Existing historical completion claims are preserved; no new
completion is generated. A new aborted, unusable migration barrier requires
fresh authorization wherever the legacy history has an action binding.
Empty/minimal aborted histories remain unusable.

The journal, backup, independent source and transaction are opened without
following links. Each must be a regular file with exactly one link, and all four
must have distinct device/inode identities. Their descriptors remain open while
bytes, link counts, path identities and timestamps are rechecked after checkpoint
publication and immediately before the single-file v2 journal commit. That
atomic journal replacement is the migration commit boundary: recovery no longer
depends only on the identities observed before it because the committed file
carries the exact v1 recovery image. Every later journal load repeats the
four-file independence check, requires both legacy copies to match the embedded
bytes and hash, and requires the transaction's exact v2 journal to be the current
history prefix. Hard links through another name are refused even when their
bytes match. A same-byte pathname replacement beginning at the commit boundary
may be accepted because it cannot remove the embedded recovery image; missing,
different, linked or non-regular evidence remains blocked. A file that appears
during creation is never replaced.

On successful return the migrator has reopened and validated the committed
journal and all evidence. This is the finite operation boundary. Filesystem
changes after return are evaluated by the next load and can block future use;
the migration does not claim to prevent later external mutation. Such mutation
cannot rewrite the embedded recovery image without breaking the transaction and
checkpoint bindings.

An identical completed migration request reloads without rewriting the pair or
its provenance and verifies all migration evidence. Any crash before checkpoint
publication can be retried from the pinned legacy journal and durable transaction.
If the exact transaction checkpoint was already published, retry verifies it and
publishes only that transaction's journal, making recovery idempotent without
inventing a new journal ID, time or provenance. A conflicting transaction,
checkpoint, backup or independent source is refused and preserved. No migration
fallback can turn a lost v2 anchor, damaged migration evidence or stale pending
event into usable completion evidence.
