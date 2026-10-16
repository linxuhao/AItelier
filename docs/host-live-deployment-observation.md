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
decline. A supplied callback is called exactly once under the fence, on a
quiescent observation too, because its answer may restrict or refuse. Declining,
raising, or combining it with an override file refuses the action even when
nothing blocks it. Callers that pass no callback keep the plain quiescent
authorization; there is no new prompt. It receives a private snapshot; the same original measurement is consumed
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


## Coordinated owners in a backend-only decision

Without this field, a backend-only decision refuses any authoritative owner, and
an unscoped decision cannot acknowledge unknowns while authoritative owners are
present. Owners who stated their work is unaffected therefore had no way to be
recorded. A backend-only decision may add `coordinated_owners`, a list of
`{"kind", "id", "scope_ref"}` records with non-empty strings and no other keys.
`kind` is `registered_external_owners` (named by `attempt_id`) or
`godot_render_owners` (named by `owner_id`), and the named row must be `active`.

Each record is the local operator's audited assertion that the owner authorized
an unaffected scope for this backend-only action, obtained outside the payload.
`scope_ref` points at that authorization; neither it nor the ticket is
authentication or proof, and nothing reads or verifies it. The trust boundary
is unchanged: under `backend_only` only the local backend restart/redeploy path
honors the field. The Godot-only recreation refuses any `backend_only`
decision, because a backend-only scope covers the backend service effect only;
it has its own `godot_only` scope (below). Even on the backend path, the field
is honored only when that trusted caller passes `backend_only=True` and the
decision itself requests `backend_only: true`.

The records must exactly cover the remaining authoritative owners of the held
inventory. A missing, extra, stale, duplicate, malformed or unknown-row name
refuses. Run, operation, checkout lease, non-ready semantic demand, known
process, `owner_lost` render, measurement-failure and inventory-error blockers
cannot be named and still refuse. Unknown owners and process noise still need
`acknowledge_unknown`. Action, actor, ticket, expiry and digest checks are
unchanged. The journal records the original blockers, errors and digest, plus
`override_scope: coordinated_owners`, the records with their scope_refs and
every affected coordinated and unknown row. It marks nothing settled.

A quiescent inventory needs no permission, so a plain decision supplied with it
is still ignored and the action is authorized as before. A decision that
restricts or refuses is not ignored. That means one carrying `backend_only`,
`deployment_attempt_id`, `coordinated_owners` or an `_invalid_override`
refusal, or one that is not an object. It is fully validated first. This holds
whether the decision came from a file or from a callback. `_invalid_override`
is a reserved refusal: its presence refuses, whatever its value (`false`,
`null`, empty or `0` included). If it is
malformed, stale or refused, the action is refused even though nothing blocks
it. If it is accepted, the `authorized` event carries its audit with
`override_scope: quiescent_restriction`, so the backend-only effect selection
below still applies. A coordinated record can never be accepted on a quiescent
inventory, because there is nothing left for it to cover.

A backend-only decision operates only the backend service: the trusted CLI uses
`docker compose restart --no-deps aitelier` or `up -d --no-deps aitelier`, so
zvec-grep (its `depends_on`) and godot-builder are not started, restarted or
recreated. Unscoped startup and restart keep compose's default dependency
behavior. Neither the field nor the scope promotes the shared Source checkout.

Backend-only acknowledgement authorizes only the backend action named in the
held decision. It does not authorize changing the shared source checkout or
promoting another artifact. Its journal distinguishes a declared controller
with unknown owner metadata from a recorded owner match, and includes uncertain
recovery rows in affected_ownership rather than claiming they are settled.


## Godot-only audited decisions

The backend-only scope above was earned by the backend-only effect, so it never
authorizes recreating Godot. Before this scope, the normal Godot recreation had
no way to record unaffected active registered owners next to unknown process
noise: acknowledging the unknowns refused the authoritative owners, and omitting
the acknowledgement refused the unknowns. That refusal is unchanged for every
decision that does not request the scope below.

`aitelier`'s `recreate_godot_builder` is the only caller that passes
`authorize(..., godot_only=True)` (the CLI clearance's `godot_service`). Its
decision may request `godot_only: true`. Requesting it from the backend
restart/redeploy path or from an unscoped caller refuses, a decision requesting
both `backend_only` and `godot_only` refuses, and a non-boolean value refuses.
A trusted caller cannot pass both flags.

The scope classifies the inventory as the backend-only scope does, because the
recreation touches neither Source nor the semantic sidecar. Ready/ready demand
at an equal revision with no error is passive. Explicit State settlement is
passive. The exact own AItelier deployment controller is exempt metadata when
`deployment_attempt_id` names it and its recorded `owner_driver_id` matches the
audited `driver:<id>` actor. Missing owner metadata, and paused, unknown or
unsettled recovery rows, stay disclosed unknowns and need `acknowledge_unknown`.

`coordinated_owners` under this scope may name only `registered_external_owners`
rows (by `attempt_id`, status `active`, each with its `scope_ref`). The exact
cover rules, refusals and journal fields are those of the backend scope. Godot
render owners cannot be named here, active or `owner_lost`, because this action
recreates their service. Runs, operations, checkout leases and writes, known or
registered processes, docker render owners, non-ready semantic demand,
measurement failures and inventory errors cannot be named either. Unknown
process noise still needs `acknowledge_unknown`.

The journal records `godot_only: true`, the controller identity, the cover with
its scope_refs, every affected coordinated and unknown row, and the original
blockers, errors and digest. Nothing is settled or cleared. The cutover fence,
CID/PID/image expectations, quota and Source binding rechecks, health/source
checks and the `--no-deps --force-recreate` Godot-only effect are unchanged.
This scope does not promote the shared Source, restart the backend or zvec-grep,
or touch the GPU. It is a Source contract only and does not perform a recreation.
