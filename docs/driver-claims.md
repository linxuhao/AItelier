# Driver claims and leases (multi-driver P1)

Design: `design/multi-driver-coop.md` §4.1–4.3a and §4.5, phase P1 ("leases, alert
only"). Identity (who a driver is) is P0, see `docs/driver-identity.md`. This page
covers what P1 adds: node claims, leases on claims and attempts, heartbeats and
lease events. Enforcement, takeover/abandon, handoff and subagent registration
are P3; nothing here blocks or cancels anything.

## Status

Implemented on branch `grok/multi-driver-p1`; not yet deployed. Every project is
`multi_driver=off` after the migration, so nothing changes until the owner turns
it on for a project.

## Turning it on (owner / admin driver)

`set_multi_driver(project_id, multi_driver="on"|"off", expected_revision, reason)`
(MCP `state_graph_write`, or `POST /api/state/commands/set_multi_driver`). It
shares the `state_project_policy` revision with `set_dispatch` (read it from
`project_overview.policy.revision`) and emits `multi_driver_policy_changed`.
Only the owner's Access email or an `is_admin` driver (`owner-cli`) may call it;
anyone else gets `admin_required`.

With `multi_driver=off` (the default):

- `claim_node` is refused with `multi_driver_off`;
- new attempts are written exactly as before (no owner, no lease, same event
  payload) and report `lease_state=legacy_unleased`;
- dispatch, observation and structural writes are unchanged.

With `multi_driver=on`, claims are recorded and new attempts started by a
registered driver record `owner_driver_id`, `owner_fence=1` and a 7200-second
lease. Claims are still NOT enforced: anyone may dispatch, report, revise or
hold regardless of claims (enforcement is P3).

## Claims

`claim_node(project_id, node_key, purpose, expected_revision, request_key,
lease_seconds=7200, workspace="", subagent=None)`

| purpose | meaning | exclusive |
|---|---|---|
| `implement` | preparing or running an attempt | yes |
| `plan` | preparing revise/split | yes |
| `review` | reviewing a CANDIDATE | no |
| `investigate` | read-only investigation | no |

- At most one live `implement`/`plan` claim per node (partial unique index
  `state_node_claims_one_exclusive`); concurrent claims have exactly one winner.
- `lease_seconds` is 60..86400; grace after expiry is 900 seconds.
- `fence` is per node and monotonic: each new claim on a node gets
  `1 + max(fence)` of that node, so a holder whose claim lapsed and was replaced
  cannot act with its old fence.
- `request_key` is idempotent per (project, node, driver): repeating the same
  request returns the same claim (`idempotent: true`); reusing the key for a
  different request is `request_key_reused`.
- `subagent="<your driver_id>/<label>"` marks a claim held for one of your
  subagents (they have no credentials of their own, D3).
- The node must exist at `expected_revision` (`revision_changed`) and not be
  SUPERSEDED (an `implement` claim also refuses VERIFIED): `node_closed`.

`release_claim(project_id, claim_id, fence, reason)` — the holder, or an admin
(recorded `break_glass: true` in the `claim_released` event). Releasing an already
released claim is idempotent.

Reads (private, writer credential): `list_claims(project_id, node_keys?,
statuses? = ["live"], driver_id?, limit?)` and `get_claim(project_id, claim_id)`
(with its append-only history). REST: `GET /api/state/projects/{id}/claims` and
`GET /api/state/projects/{id}/claims/{claim_id}`.

## Heartbeat

`heartbeat(project_id, claims=[{claim_id, fence}], attempts=[{attempt_id, fence}],
subagents=[{subagent_id}])`, 1..100 items in total.

- Writes ONLY `lease_expires_at` and `last_heartbeat_at`: no `state_events` row,
  no history row, no `observation_version` bump, no `updated_at` change.
- Each item is renewed or refused independently, so one stale entry does not
  cost a parent the other 99 renewals. Response: `{renewed: [{kind, id, fence,
  lease_expires_at}], refused: [{kind, id, error}], heartbeat_at}`.
- A `subagents` item renews every live claim the caller holds for that label.
- Item errors: `not_found`, `not_claim_owner`, `not_attempt_owner`,
  `stale_fence`, `claim_not_live`, `attempt_not_active`, `legacy_unleased`,
  `not_your_subagent`, `no_live_claims`.
- Renew from the same client loop that runs your `wait_for_state_change`, every
  20–30 minutes, and only while you actually supervise the work (§12.2 item 7).

## Lease states and events

| `lease_state` | when |
|---|---|
| `healthy` | now < `lease_expires_at` |
| `expired` | expired, within the 900-second grace |
| `reclaimable` | grace passed |
| `legacy_unleased` | active attempt with no recorded lease (all pre-P1 attempts); never expires |

Only state changes emit events: `claim_acquired`, `claim_released`,
`lease_expired` (payload `subject` = claim|attempt, `lease_id`, `phase` =
expired|reclaimable, `lease_expires_at`, `reclaimable_at`, `driver_id`, `fence`;
`attempt_id` for attempts so `attempt_ids` wait filters match).

Expiry is detected lazily, never by a background job: in the private claim
reads (`list_claims`, `get_claim`), inside `claim_node`, and on every iteration
of `wait_for_state_change` (about once a second while it waits). Public reads
(`project_overview`, `get_node`, `project_run_summary`) never write; they derive
`lease_state` from the clock, so a claim past its grace that no sweep retired yet
is shown as `reclaimable`. Each phase is
emitted once per lease. When a claim reaches `reclaimable` its status becomes
`expired` (history row written) and it stops blocking a new exclusive claim.
An attempt is NEVER changed: it stays ACTIVE and keeps the node's one active
slot; recovering it (abandon/take over) is P3.

## Overview and wait

- `project_overview`: `policy.multi_driver`; per node `claims` (live claims:
  holder `driver_id`, `subagent`, `purpose`, `fence`, lease fields — the same
  public subset for every reader of an opened project; `workspace`,
  `request_key` and history are only in `list_claims`/`get_claim`) and
  `latest_attempt.owner_driver_id`, `owner_fence`, `lease_expires_at`,
  `lease_state`.
- `readiness` may be `in_progress_lease_expired` (an active attempt whose lease
  lapsed); `readiness_counts` counts it. Treat it like `in_progress` plus "the
  owner stopped renewing".
- `get_node`: `claims` (same public subset) and per attempt `owner_driver_id`, `owner_fence`,
  `lease_expires_at`, `lease_state`. Every attempt read carries `lease_state`.
- `project_run_summary`: `running_external[*]` gains `owner_driver_id`,
  `owner_fence`, `lease_expires_at`, `lease_state`, plus `live_claims`.
- `wait_for_state_change(include_lease_events=true)`: wakes on the three lease
  events; with `false` they are filtered out of the scan. With
  `return_when_idle=true`, a scope whose only nonterminal attempts are
  `reclaimable` returns `reason=action_required` with those attempts and their
  lease fields instead of waiting forever.

## Schema migration

Additive, idempotent, in the existing transactional initializers
(`docs/state-external-harness.md` "Schema migration"):

- `state_attempts` + `owner_driver_id TEXT`, `owner_fence INTEGER NOT NULL
  DEFAULT 0`, `lease_expires_at TEXT`, `last_heartbeat_at TEXT`, and the partial
  index `state_attempts_lease`. No rebuild, no CHECK change; existing rows keep
  NULL/0 (never backfilled: the historical shared actor names no driver).
- `state_project_policy` + `multi_driver TEXT NOT NULL DEFAULT 'off'`.
- New `state_node_claims` (claims are never deleted; trigger) and
  `state_claim_history` (append-only; UPDATE/DELETE refused by triggers).
- `set_dispatch` now names its columns on insert, so it keeps working on the
  wider policy row.

Back up before deploying and do not run old and new State writers on one
database. Rolling back code is safe for data (old code ignores the new columns
and tables); claims and leases simply stop being maintained.
