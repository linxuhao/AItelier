# Activating semantic index ownership

The backend and zvec sidecar share `${HOME}/.AItelier` at the same absolute
path. Compose enables `AITELIER_ZVEC_LIFECYCLE=1`. The sidecar image pins the
upstream `@zvec/zvec-grep` 0.2.2 release and includes the same standard-library
`core/semantic_index_control.py` used by the backend. A lifetime sidecar lock
and worker lock admit one writer; the legacy worktree discovery scanner is
not started. Each batch handles at most 32 recorded run demands and then one
new immediate project checkout. Project discovery excludes symlinks and run
roots. Existing project indexes continue to be served and watched by zvec.

Project baseline indexing has a durable `project-owner.json` marker. Failed
or interrupted work stays unknown until the same root passes upstream
`zg status --check-ready`. Existing indexes are not rebuilt. Run requests,
readiness, errors, generation fencing, release and restart reconciliation
use the existing `indexes` ledger. The worker does not create an empty ledger;
a real host prepare demand creates it. Terminal historical trees without a
ledger owner retain their old derived index. Active recorded runs are adopted
by the host reconciler. Terminal release applies only to recorded ledger
owners; source, Git branches, artifacts, trace and State remain retained.

The worker shares the existing deployment admission fence throughout each
batch. Deployment measurement reads the actual run ledger, project marker
and operation lock, plus the daemon's native queued/running job inventory
(including background watcher jobs). Errors and interrupted operations block
cutover. Neither a disabled feature flag nor an idle resident process hides
actual ownership. The proxy exposes only the public MCP route; administration
remains loopback inside the sidecar.

Before an authorized production cutover:

1. Capture exact source/image/version and nonsecret configuration identities;
   enumerate project/worktree index roots and manifest hashes without reading
   source content or scanning/reindexing historical trees. Query native daemon
   queued/running jobs. Capture active engine, admitted operations and external
   owners using the established deployment guard.
2. Wait for active owners to retire. Unknown ownership fails closed; any
   override must be explicit and recorded in the existing deployment journal.
   Hold the deployment admission fence while replacing services. Stop the old
   scanner before starting the new writer. Do not erase indexes, root storage,
   ledger rows, live leases or historical evidence to obtain clearance.
3. Mount the identical data root into host and sidecar; start the reviewed
   image and backend source. Record the new artifact/version/config identities.
   Let a real fresh run publish prepare demand and prove ready, concurrent-owner
   retention, terminal release and restart recovery. This is what creates and
   validates the actual ledger; a hand-created empty database is not evidence.
4. Compare retained manifest hashes and source/candidate/trace identities,
   observe State/MCP health and record the real guard results. Release the
   maintenance fence only after the concrete cutover has settled.

Upstream 0.2.2 fixes atomic daemon lease publication, Linux watcher handling,
embedding failure reporting and updates its vector backend. It does not
implement AItelier ownership. Before deployment, validate retained 0.2.0 index
compatibility and fresh prepare/drop against the real 0.2.2 package in an
isolated fixture. Source and unit checks alone are not deployed acceptance.

Reference: https://github.com/zvec-ai/zvec-grep/releases/tag/v0.2.2
