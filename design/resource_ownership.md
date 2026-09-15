# Deployment resource ownership

The deployment journal and its R12 recovery contract are unchanged. Quiescence
means all **AItelier-owned operations** have settled in their authorities. It does
not mean no arbitrary host program could execute similar code. Process listings
are diagnostics only: names, argv, source text, terminals and liveness guesses
cannot grant ownership or discharge an owner.

## Authorities and launch boundaries

| Operation | Mandatory durable authority | Admission boundary |
| --- | --- | --- |
| SkillFlow tools/agents | SkillFlow active operations and run records | `AItelierSkillFlow._admit_op`, before execution |
| Checkout mutation | AItelier checkout leases/write admissions | `run_isolation.admit_write`, before mutation |
| External measurements | State external owner and attempt records | `ExternalAttempts.register` / relay registration, before dispatch; authenticated terminal report must attest all workers settled |
| Godot HTTP, CLI, direct functions | Shared resource owner plus existing HTTP render ledger | Public compile, checkgd, playtest, script and X11 function decorators, before copying/preparing or launching |
| Semantic scanner, direct/public zg, demand worker | Shared resource owner plus retained index demands | Scanner wraps the whole per-repo operation; public `zg` wraps the package binary; `IndexControl.process_once` wraps nonempty batches |
| Semantic HTTP/SSE search requests | Shared per-request resource owner | Proxy admits before forwarding; parallel requests have independent inherited lock identities |
| Godot and semantic resident services | Shared service owner | `_serve` / public `zg server run` |

All operation authorities cross the same `godot-control/deployment-admission.lock` before
committing admission. The deployment CLI holds it exclusively from measurement
through journal settlement. Resident service registration uses durable uniqueness
without waiting on the operation fence so a cutover can start a healthy service;
its requests/effects cannot start until cutover releases the fence. Health endpoints
perform only fixed read-only probes (the semantic proxy sends an empty protocol
request, never caller payloads). Sidecar locks and `resource-owners.sqlite3` use the
same mounted directory (the Godot mount aliases it as `/var/lib/aitelier-godot`).
The existing runtime and State owner protocols are retained, not duplicated.

The sidecar authority uses Python's maintained standard-library SQLite and POSIX
flock interfaces. SQLite `BEGIN IMMEDIATE`, a partial unique live-resource index,
a nonreusable UUID and monotonic generation record admission before effects.
Search/SSE requests have independent operation IDs and lifetime locks; one open
stream does not prevent another request from being registered. Godot effects are
serial across HTTP, CLI and direct calls; this extends prior
render-only serialization to compile/checkgd. Semantic writers are likewise
serial. Busy or stale ownership refuses admission; there is no automatic retry
of uncertain effects. Idle semantic batches create no owner/history entry.

Launchers pass their effect-lock descriptor and an unlinked, per-generation
capability descriptor to subprocesses. The authority stores only the capability
digest. An internal entrypoint must present that inherited descriptor and the
matching live row; reopening the named effect lock cannot borrow another owner's
admission. Verification never treats lock contention as a capability or turns a
free lock into authority, so it cannot steal a released generation during a
race. Closing the parent effect-lock reference does not unlock inherited
references. Normal completion releases the row only after
obtaining the effect lock again, proving participating children closed theirs.
Exceptions, nonzero zg results, unconfirmed child settlement and caught semantic
provider failures retain the row. A successful resident service is not an active
operation; a missing service lock makes it unknown and blocking.
External custom harnesses remain responsible for their own complete worker tree
and authenticated settlement declaration; this mechanism cannot certify a remote
report's honesty.

## Commissioning and recovery

An absent database is **unavailable**, never a quiet empty ledger. Before first
use of this boundary, stop and settle legacy owned launchers/services, preserve
any previous locks, then explicitly commission the shared authority:

```sh
python -m core.resource_ownership --directory /absolute/shared/godot-control initialize \
  --actor OPERATOR --reason 'Evidence reference establishing all legacy effects ended'
```

This exclusive-fence operation refuses an existing database. An interrupted
commissioning leaves unavailable state and cannot silently initialize it again.
Do not delete the database or lock files to make admission work. Preserve partial
commissioning evidence and resolve the stopped legacy resources before restoring
a verified authority. This release does not deploy or commission anything itself.

Inspect via `python -m core.resource_ownership --directory PATH snapshot`.
After independently settling the exact unknown operation and its associated
service, recover **that generation**:

```sh
python -m core.resource_ownership --directory PATH recover OWNER_UUID GENERATION \
  --actor OPERATOR --reason 'Evidence reference establishing service and all effects ended'
```

Recovery takes the exclusive admission fence and both the family's operation and
service locks; a live service prevents recovery of a potentially asynchronous zg
request even if its client exited. It records `reconciled` without relaunching or
reusing the operation ID. The old Godot HTTP render ledger, when present, also
requires its existing explicit lifecycle reconciliation. Neither clock expiry nor
restart clears either ledger. Upstream daemon/index lock files are no longer
unlinked at container startup; uncertain upstream state requires its own explicit
operator recovery after the service and operations are stopped.

Preflight reads resource authority, SkillFlow, AItelier, external attempts, index
demand and Godot ledgers, and the container census. Missing, inconsistent, unknown
or locked-without-registration state blocks. Exact Compose names/labels require
a live matching service registration bound to that container runtime identity and the corresponding legacy ledger; a
Godot-only deployment does not require semantic demand state. Process diagnostic
failure does not block or authorize ownership. Unsupported uncooperative programs,
manual use of internal `zg-real`, filesystem tampering, or a separate unregistered
resource namespace are outside this cooperative protocol. All owned launchers and
images must be upgraded and the shared namespace commissioned before this claim
applies; mixed-version services without authority are rejected.

This authority assumes a local POSIX filesystem with working SQLite durability
and flock semantics. It does not claim distributed locking over NFS, immunity to
privileged deletion/replacement, or independent validation of arbitrary executable
source. Tests use temporary resources and harmless children; live deployment and
restart acceptance require separately authorized evidence.
