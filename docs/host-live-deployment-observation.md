# Host and initialized runtime deployment observation

The normal host CLI restart/redeploy guard now fetches original runtime facts
from POST /api/admin/deployment-runtime-observation before its existing host
OS/Docker/sidecar measurement. The endpoint is an explicit owner audit: it may
record observed-lost ownership through the existing live SDK, but does not retire
owners, construct a runtime, register graphs, authorize deployment or start work.

The endpoint requires the existing off-tunnel CLI admin token. Cloudflare
requests, including a writer identity or replayed admin token, cannot obtain this
raw inventory. The existing MCP observer remains a writer-authorized redacted
observation with an original digest; that projection is never used here.

The host CLI uses the existing admin header, a loopback HTTP backend URL and the
normal compose service/container identity. Original runtime facts carry their
canonical digest, time, boot ID and live process namespace identity. The host
checks the boot ID, snapshot age (30 seconds), schema and Docker backend PID/mount
namespaces before composing them with its own OS and shared-sidecar observations.
Freshness is checked again after host probes. Missing runtime/host facts, stale,
malformed, projected or mismatched observations remain unavailable or blocking.
There is no competing runtime, raw operation-table scan, new daemon, credential,
or automatic owner release. Existing cutover fence, blocker classification,
override rules and journal finalization are unchanged.

A stopped/uninitialized/unreachable backend cannot provide live runtime facts:
its normal guard refuses explicitly rather than constructing a substitute and
calling it quiet. This change is SOURCE only until independently reviewed and
installed; it does not perform a restart or alter production configuration.

A pre-bound `AITELIER_DEPLOY_OVERRIDE_FILE` still needs the exact fresh digest
consumed by the guard; a file from an earlier observation refuses even when the
only change is its observation time. For a human decision made at that boundary,
`aitelier recreate-godot --acknowledge-deployment` displays the fresh digest,
runtime provenance, blockers and errors while the existing cutover fence is held.
The operator supplies identity, reason, authorization reference and expiry,
separately acknowledges unknown facts if intended, then confirms that inventory.
When an override is needed, declining, invalid metadata, conflicting file/decision inputs and authoritative
owners combined with unknown-only acknowledgement refuse through the existing
journal. No acknowledgement is inferred from the command flag itself.

Headless Python callers can pass `override_decision(action, observation)` to
`restart_server` or `recreate_godot_builder`. The callback must inspect the fresh
owner identities/blockers against the operator's authorized scope before
returning an ordinary audited override with that snapshot's digest, or `None` to
decline. It receives a private snapshot; the same original measurement is consumed
by existing `authorize`, effect and `finalize` without another hidden measurement.
The callback does not modify facts or release owners; backend defaults, Godot
Source/CID/image and configuration rechecks, and quota bounds remain unchanged.

## Backend-only audited decisions

The host backend gate supplies `authorize(..., backend_only=True)` only after
the existing local-admin runtime observation and backend namespace checks. An
operator decision may request `backend_only: true` and, when the current
deployment controller is present, its exact `deployment_attempt_id`. The
controller must name an exact AItelier deployment attempt. A present recorded
`owner_driver_id` must match the audited `driver:<id>` actor. Missing owner
metadata is disclosed as unknown ownership, never a proven own-controller
exemption; only explicit unknown acknowledgement can handle it. The action, inventory
digest, expiry, backend scope and controller identity remain in the journal.
Actor/reason/ticket strings record the decision; they are not authentication.
The local CLI/API authority remains the trust boundary, and direct Python
authorization is a trusted local API.

This scope treats matching ready/ready semantic demand with no row error and
explicit State settlement (settled, or confirmed-stopped abandonment, with a
settlement timestamp) as passive metadata. It does not remove these rows or
change the measured quiescent flag. The acknowledged-unknown decision can then
coexist with passive metadata. Physical operations, known external processes,
render owners, writes, leases and inventory errors still refuse an unknown-only
override. Paused, unknown or abandoned recovery without positive settlement
remains unknown: the same inventory refuses without acknowledgement and may
authorize with it. This decision does not establish that effects stopped or
that recovery is settled. A PID's absence or a timestamp cannot prove that. A caller changing shared services cannot
claim backend-only scope from an override file; unscoped behavior is unchanged.

This source contract does not certify source compatibility, recovery, or current
production quiet. A real cutover still requires fresh supported host and runtime
observations and coordination with the affected owners.


Backend-only acknowledgement authorizes only the backend action named in the
held decision. It does not authorize changing the shared source checkout or
promoting another artifact. Its journal distinguishes a declared controller
with unknown owner metadata from a recorded owner match, and includes uncertain
recovery rows in affected_ownership rather than claiming they are settled.
