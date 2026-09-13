# Run worktree resource lifecycle — implementation plan

State: `aitelier/search.run-worktree-resource-lifecycle@1`.
Attempt: `attempt-b6977d660b244d3a99ab96c70a4357a4`.
Base: `5dac031e9129c0ff212de3e4fb059076e801107d`.

## Effective contract

The host declares semantic-index demand for recorded, structurally verified run
worktrees. Start/resume requests preparation; a search waits for its exact root
within a bounded host budget and returns an explicitly labelled lexical fallback
when preparation/search is unavailable. Agents receive no index/drop tool.
Terminal status plus a successful, complete operation-owner audit authorizes
release of derived index resources, independently of merge or code retention.
Paused, running and unknown runs do not authorize release.

The sidecar consumes durable host requests, not a filesystem inventory. It calls
the pinned zvec 0.2.0 server-mode index/drop commands. `zg index ROOT --drop --yes
--mode server` closes the watcher, cancels pending jobs, waits for active root
jobs/readers, drops derived data, and evicts the root runtime. A timeout or error
is not a release receipt. Shared model downloads are never deleted.

Code checkout reclamation requires terminal/quiescent observations, exact managed
ownership, clean Git state, actual checkout/branch identity, and locally proven
integration or a pre-existing explicit operator discard. The default idle delay
is 86,400 seconds. Time never writes `discarded`. Failed Git/engine/timestamp
observations retain the checkout with a reason. Branches/commits, artifacts and
State/trace evidence are not reclamation targets. External developer worktrees,
including this candidate under `.cache`, are outside the managed run root.

## Grounding and plan review

- `core/run_isolation.py` records run roots before claim; launcher, scheduler and
  meta-agent share `ensure_for_run`. Existing lease reconciliation does not cover
  worktree-mode runs without checkout leases. The terminal resource reconciler
  must therefore enumerate isolation records, not just lease rows.
- `docker/zvec-grep-entrypoint.sh` currently indexes every historical directory.
  Remove that discovery policy; otherwise a cleaned index is recreated.
- Pinned upstream `daemon/backend.js:dropIndex` provides root-scoped draining and
  runtime eviction. Reuse it rather than deleting directories or implementing a
  vector-store abstraction. Explicit server mode forbids unsafe direct fallback.
- `is_disposable` currently ignores a nonzero Git-status exit code and proves the
  recorded branch rather than validating the actual checkout. Both checks need
  fail-closed strengthening before introducing unattended retention timing.
- A separate, small durable desired-state ledger under the host data directory
  connects host and sidecar without exposing admin HTTP/MCP tools. Generation
  receipts fence stale completion; short control locks serialize desire changes
  with final reaping; resource locks serialize search, index/drop and checkout
  removal. No SQLite transaction spans a subprocess/network call.
- Resource ledger metadata is bounded per retained run; it is not an index copy.
  Successful checkout removal removes its ledger row. Lock storage uses a fixed
  shard set. Retained unmerged code and the platform's historical records still
  consume disk: this is not a promise of an absolute disk ceiling.
- The host's normal startup/maintenance paths reconcile missed transitions. The
  sidecar retries failed requests with backoff. Neither timestamps nor process
  death prove that an operation drained.
- No base/delta index sharing, forced checkout deletion, auto-commit/archive of
  dirty work, model-cache deletion, new agent cleanup grants or production
  mutation is included.

## Implementation and checks

1. Implement the stdlib control ledger/worker and host adapter; replace sidecar
   directory scans and wire startup/resume/terminal maintenance.
2. Wire bounded exact-root readiness and same-call lexical degradation into
   `semantic_search`; retain source-selection and internal-storage exclusions.
3. Strengthen ownership/Git checks and add configurable inactivity gating plus
   positive semantic-release receipts to worktree reclamation.
4. Test real Git and real host/SkillFlow boundaries with a deterministic sidecar
   command double: fresh parallel worktrees, reload, release, stale generations,
   concurrent readers, crash/retry, failed commands, activity reset, dirty and
   unmerged preservation, ignored evidence, symlinks and out-of-scope roots.
5. Run focused and broader regressions, inspect the full diff and impact, record
   all failures and untested live-sidecar/deployment boundaries. Commit only
   owned paths and publish an immutable hashed self-review/test report.

This plan review is by the implementing session. CANDIDATE delivery is not
independent acceptance; verification, integration and deployment belong to the
AItelier director.
