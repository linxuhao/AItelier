# Run-owned semantic resources

## Ownership and lifecycle

The AItelier host owns semantic-index demand for recorded run worktrees and read
snapshots. `ensure_for_run` publishes preparation on start/resume. A bounded
first-search readiness wait uses the exact recorded checkout. Preparation failure
is a visible degradation and does not prevent isolation or run startup.

`semantic_search` returns `mode: semantic` for semantic results,
`mode: literal_fallback` with `degraded: true` for same-call literal source
results, or `mode: unavailable` when a source read cannot acquire a safe resource
lease. Literal results are bounded token matches, not semantic ranking, FTS
expression equivalence, or exhaustive absence evidence. No path substitutes the
source checkout or a different run for an unavailable isolated root.

The sidecar consumes the durable demand ledger at
`$AITELIER_HOME/semantic-index-control/control.sqlite3`. It does not discover
index work by enumerating checkout directories. It uses the pinned zvec-grep
0.2.0 server-mode `index`, `status --check-ready` and `index --drop --yes`
commands. A successful drop drains the daemon's root jobs/readers, closes its
watcher/runtime, and removes its manifest and vector collections. Client timeout
or failure is not a release receipt. Generation-fenced receipts and filesystem
locks prevent stale completion or a concurrent reader from authorizing removal.
Shared embedding-model downloads are not a run cleanup target.

A known terminal run with a complete, quiet operation-owner audit requests index
release even when its code is dirty or unmerged. Startup and the existing periodic
lease sweep reconcile missed transitions. Running, paused, unknown and draining
runs retain their resources; pause is not interpreted as abandonment. A resumed
run reuses its exact checkout and requests preparation again.

Source/direct-mode indexes remain operator-managed. Both containers must see
managed worktrees and their Git source metadata at identical absolute paths.
An inaccessible source or unverifiable linked-worktree backlink prevents unsafe
index operations and yields visible degradation. No agent-facing administrative
index/drop tools are added.

## Worktree reclamation

A default 24-hour inactivity delay applies to automatic checkout removal.
Inactivity is established from engine activity, isolation/release timestamps,
index demand activity and the linked checkout's Git HEAD/index metadata. A
maintenance sweep does not count as user activity. Missing, malformed or future
observations retain the checkout.

Elapsed time never sets `discarded`. Reclamation additionally requires all of:

- Exact recorded nonsymlink managed-root ownership, matching Git common directory
  and linked-worktree backlink, and unchanged branch/snapshot identity.
- A known terminal run, a complete quiet operation audit, retired host write
  admissions and exclusive resource/control ownership.
- A clean checkout, no unclassified ignored files, and a locally proven
  integration into the source checkout's current branch, or an already recorded
  explicit operator discard.
- A current positive semantic-release receipt and no reappeared vector storage.

Removal uses ordinary `git worktree remove`, never force removal or branch
removal. Branches and commits remain recoverable. Dirty or unmerged work,
unknown index leftovers, ignored evidence, artifacts, State records and trace
are not implicitly discarded. External developer worktrees under a repository's
`.cache`, including this candidate, are outside the managed run root.

These rules bound derived index retention by run demand, not all storage. Retained
unmerged code, unknown leftovers, shared model cache and historical State/trace
records still consume disk. No absolute RAM/disk ceiling is asserted.

## Configuration and diagnostics

| Variable | Default | Meaning |
| --- | --- | --- |
| `AITELIER_ZVEC_LIFECYCLE` | `0` outside Compose; `1` in Compose | Host demand/release integration. |
| `AITELIER_ZVEC_PREPARE_WAIT_SECONDS` | `10` | Readiness wait, finite value from 0 to 60. Compose passes this setting through. |
| `AITELIER_WORKTREE_IDLE_SECONDS` | `86400` | Inactivity delay before safe checkout removal. Zero disables only the delay, never the ownership/data-preservation checks. |
| `AITELIER_ZVEC_COMMAND_TIMEOUT_SECONDS` | `600` | Positive finite per-command sidecar timeout. A timeout retains uncertain resources for retry. |

Index-command failures retry with backoff. The sidecar emits `[zg-lifecycle]`
outcomes; the host emits `semantic_index_prepare_degraded`,
`semantic_index_retained`, `worktree_retained` and `worktree_reaped` diagnostics.
Retention reasons are coalesced until they change, and resolved diagnostic entries
are removed. The tracked sidecar worker PID is the Python process itself, so
shutdown signals reach its interruption handler.

Deployment requires the host and sidecar images from the same candidate, including
the copied stdlib worker. Existing historical worktree directories are not an
indexing queue. The director owns deployment coordination and real-sidecar smoke
verification; a tested candidate is not an automatically deployed change.

## Validation and handoff boundary

The implementation-session tests use real temporary Git worktrees, an isolated
SkillFlow engine, a temporary HTTP MCP server and sidecar CLI doubles. They cover
start/resume, same-call fallback, root separation, unknown operation audits,
terminal release, restart/retry, generation fencing, cross-process locks, signal
ownership, the 24-hour delay, activity reset, malformed observations, ignored
files, symlink/forged-link protection and branch preservation.

Self-review reproduced and corrected five defects before candidate delivery:
preparation errors blocking run startup; missing resource-refusal diagnostics;
worker shutdown targeting an intermediate shell; unknown index leftovers being
removed with a checkout; and another linked checkout's Git metadata passing an
insufficient common-directory-only ownership check.

The same expanded 208-test selection passed against SkillFlow 1.5.74 and 1.5.75.
The candidate source dependency declaration is unchanged; 1.5.75 was installed
only into this private test virtual environment for current-main compatibility.
New lifecycle modules/tests pass scoped Ruff checks. A broader critical-error
scan also found one existing `F823` (`logging` referenced before local assignment)
in the scheduler's unrelated generated-pipeline registration path. The same
finding was reproduced against the original base file; it is not changed or
claimed fixed by this candidate. Raw logs, environment details, file hashes and
the exact candidate SHA are retained in the immutable delivery report.

No production zvec process was started or stopped, no native embedding/index memory
benchmark was run, and no live run data was deleted. CLI-double tests do not prove
native zvec memory return or deployment behavior. Independent verification, merge,
image rebuild, deployment and a real-sidecar smoke test remain director-owned.
