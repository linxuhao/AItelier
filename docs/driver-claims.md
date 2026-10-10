# Driver claims and leases (multi-driver P1 + P3)

Design: `design/multi-driver-coop.md` §4 (claims, leases, reclaim, subagents),
§6 (handoff) and §7.3 (conflict rules). Identity (who a driver is) is P0, see
`docs/driver-identity.md`. P1 added node claims, leases on claims and attempts,
heartbeats and lease events, all record-only. P3 adds the terminal attempt
status `abandoned`, reclaim (abandon / take over), the subagent registry with
takeover classification, voluntary handoffs, and ENFORCEMENT of the claim rules
behind a second per-project switch.

## Status

P1 merged to main (53daff7ff2). P3 implemented on branch `grok/multi-driver-p3`;
not yet merged or deployed. After the migration every project is
`multi_driver=off` and `claim_enforcement=off`, so nothing changes until the owner
turns both on for a project.

## Two switches (owner / admin driver)

- `set_multi_driver(project_id, multi_driver="on"|"off", expected_revision, reason)` —
  record claims and leases (P1). With it off, `claim_node` is refused
  (`multi_driver_off`), new attempts are written exactly as before and report
  `lease_state=legacy_unleased`.
- `set_claim_enforcement(project_id, claim_enforcement="on"|"off", expected_revision, reason)` —
  turn the recorded claims into refusals (P3). Needs `multi_driver=on`
  (`multi_driver_off` otherwise); turning `multi_driver` off turns enforcement off
  with it. Stored in its own row (`state_project_enforcement`); shares the
  `state_project_policy` revision for the CAS (read it from
  `project_overview.policy.revision`). Emits `claim_enforcement_policy_changed`.

Owner decision 2026-10-09: `set_multi_driver(on)` alone keeps P1's record-only
behaviour. A project with `multi_driver=on, claim_enforcement=off` admits any
dispatch, report and structural write exactly as P1 did; a project with
`multi_driver=off` behaves byte-for-byte as before P1.

## Claims (P1, unchanged)

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
- `fence` is per node and monotonic; a holder whose claim lapsed and was replaced
  cannot act with its old fence (`stale_fence`).
- `request_key` is idempotent per (project, node, driver); reuse for a different
  request is `request_key_reused`.
- `subagent="<your driver_id>/<label>"` marks a claim held for one of your subagents.
- The node must exist at `expected_revision` and not be closed (`node_closed`).
- In an ENFORCED project the implement claim is BOUND to the attempt it
  dispatches (`claim.attempt_id`); a terminal report (candidate/failed), an
  abandon or a takeover releases or transfers it. With enforcement off nothing
  is bound or released unless you NAME the claim (`claim_id`+`fence` on the
  start): P1 claim rows and history are byte-for-byte as before.

`release_claim(project_id, claim_id, fence, reason)` — the holder, or an admin
(`break_glass: true` in the event).

Reads (private): `list_claims`, `get_claim`; REST `GET /api/state/projects/{id}/claims[/{claim_id}]`.

## Heartbeat (P1, extended)

`heartbeat(project_id, claims=[{claim_id, fence}], attempts=[{attempt_id, fence}],
subagents=[{subagent_id}])`, 1..100 items; writes only the two lease columns.
A `subagents` item renews every live claim you hold for that label AND its
`driver_subagents` row when you are its current owner (an inherited subagent
counts). Item errors add `not_subagent_owner`, `orphaned` (nobody renews an
orphan) and `subagent_closed`.

## Lease states and events (P1, unchanged)

`healthy` → `expired` (within the 900 s grace) → `reclaimable`; `legacy_unleased`
never expires. Events: `claim_acquired`, `claim_released`, `lease_expired`,
plus P3: `claim_transferred`, `attempt_abandoned`, `attempt_ownership_transferred`,
`claim_overridden`, `subagent_*`, `handoff_*`, `driver_notice`,
`checkpoint_break_glass`. Expiry is detected lazily (claim reads, `claim_node`,
every `wait_for_state_change` iteration). An attempt is NEVER changed by expiry;
P3 makes the change an explicit, audited member action (below).

## Enforcement (P3, `claim_enforcement=on`)

Every rule has a stable error code; the message starts with it.

| rule (design §7.3) | refusal |
|---|---|
| 1 dispatch: `start_attempt` / `start_external_attempt` need YOUR live `implement` claim on the node, judged by its LEASE at the write (a claim past expiry + grace authorizes nothing, swept or not). Optional `claim_id`+`fence` pin what you believe you hold; a replay naming either is judged on the live claim (and binds it). The same check guards the real `reserved → launching` transition with the claim/fence you named carried in (`recover_attempt` takes them too): a replaced reservation claim launches only when you NAME the replacement (`claim_required` otherwise), never by a silent rebind; another driver's reservation is `not_attempt_owner` (admin: `break_glass`). | `claim_required`, `claimed_by_other` (names holder and expiry), `stale_fence`, `not_claim_owner`, `not_attempt_owner` |
| 2 observe: `report_external_attempt` needs the attempt owner and `fence=<owner_fence>`. The previous owner after a takeover/handoff is stale; anyone else is not the owner; in an ENFORCED project an admin is admitted as `break_glass` (owner notified) — with enforcement or `multi_driver` off an admin is an ordinary reporter, exactly as in P1. A report on an ABANDONED attempt by its original reporter is recorded with `late_after_abandon=1`, `resulting_status=superseded`, never current; with `quiescent=true` it must carry a retained, digest-verified report and then settles the owner-registry row (the deployment gate stops counting it). A terminal quiescent report settles the attempt's registered open subagents. | `fence_required`, `stale_fence`, `not_attempt_owner` |
| 4 structural: `revise_node`, `split_node`, `supersede_node`, `set_node_facet` over a node — or a transitive dependent, which the write invalidates — on which another driver holds a live claim or an active owned attempt need `override_reason`; the holder is notified (`override_notice`) and `claim_overridden` is emitted. Check, write, event and notices are ONE transaction. | `override_reason_required` (facts: `holders`) |
| 5 hold: `set_node_hold(held=false)` on a hold another driver placed needs `override_reason`; the placer is notified. | `override_reason_required` |
| 8 one writer per checkout: the CHECKOUT is `host:path` (the `#branch` suffix does not make a second writer safe). A checkout is held by a live exclusive claim, by the executor of an ACTIVE attempt (the workspace of the claim it was dispatched or transferred on, whatever that claim's status is now), by an attempt abandoned with `abandon_kind=unknown` until a verified quiescence report settles its owner row, by an open subagent — active, adopted or ORPHANED until its origin driver reports it settled — and by every checkout a claim was ever held FOR an unresolved orphan. A worker's own registration and the claims held for it (`subagent=<id>`) are one executor, not two writers; so is the driver's own ACTIVE attempt on the SAME node it re-claims when the claim is for the same executor (same `subagent`, or none, as the claim it was dispatched on); another node, another worker, or an attempt abandoned with quiescence unknown is another executor. Takeover and handoff keep the transferred claim's workspace and re-check occupancy. | `workspace_in_use` |
| 9 one controller per run: the checkpoint of a SkillFlow run bound to an owned attempt is answered by the owner driver only (MCP `answer_checkpoint`, REST `/checkpoint/approve|reject`). The check OPENS a decision (`driver_checkpoint_decisions`) that holds every ownership transfer of the attempt off (`checkpoint_in_progress`) until the door finishes it after the engine call; a crashed decider expires after 120 s, and a decider delayed past that expiry is refused immediately before the engine call (`checkpoint_decision_expired`: the decision must still be open and the owner/fence unchanged). | `not_attempt_owner`, `checkpoint_in_progress`, `checkpoint_decision_expired` |
| Q8 subagents: a claim held for a subagent names an OPEN subagent registered TO YOU; evidence recorded under a `<driver>/<label>` identity names one registered to you that is open or SETTLED (a finished worker's results are what gets attested). An INHERITED worker keeps its origin prefix (`codex/w1` owned by `grok` after a takeover): the registry's current ownership, not the prefix, decides, for claims (`subagent=`), evidence (`director_identity`) and heartbeats. A transferred or orphaned worker is no longer its origin driver's. Settling a worker (terminal report, owner settlement, orphan closure, confirmed abandon) releases every live claim held for it. Judged inside the write transaction. | `subagent_unregistered`, `not_your_subagent` |
| Q13 admin: an admin (owner e-mail, `owner-cli`) is never refused by these rules; the write carries `break_glass: true` and the affected driver gets a `break_glass` notice. | — |

Priority changes (`set_node_priority`), evidence and verification are unchanged:
any member may record evidence on any CANDIDATE.

## Reclaim: abandon or take over (P3, design §4.4)

Available in any `multi_driver=on` project once an attempt's `lease_state` is
`reclaimable` (expiry + 900 s). No further wait (Q12). Before that:
`lease_not_expired` with `reclaimable_at`. Both need `expected_owner_fence`
(`stale_fence`), raise the owner fence, and notify the previous owner.

- `abandon_external_attempt(attempt_id, expected_owner_fence, abandon_kind, reason, report_ref?, report_sha256?, override_reason?)`
  — the attempt ends in the new terminal status **`abandoned`** (not `failed`:
  no criterion diagnosis, no acceptance change). It frees the node's one active
  slot (readiness returns to `ready`, the frontier lists the node, a
  `return_when_idle` wait no longer waits on it, run summary counts it under
  `external_counts.other` and names the number in `abandoned_external`), releases its bound claims, voids open handoffs
  and sets `state_external_owners.status=abandoned`.
  `abandon_kind=confirmed_stopped` is your attestation that the old worker is
  quiescent and REQUIRES `report_ref`/`report_sha256` (retained like any report);
  it also settles the attempt's registered subagents. `unknown` says you do not
  know: the node's next attempt must declare `base_sha` and ride a claim whose
  `workspace` differs from the abandoned attempt's (`workspace_in_use` otherwise)
  — two possibly live workers never share a checkout. Only external attempts
  can be abandoned (`not_external`); a SkillFlow attempt's run is alive or not
  by the engine's account, so its controller is taken over instead.
- Both reclaim actions and `accept_handoff` are for project MEMBERS (`project_drivers.status=member`, judged
  inside the ownership transaction; `not_project_member` otherwise; an admin passes as `break_glass`; with
  driver identity off there is no membership to check).
- `take_over_attempt(attempt_id, expected_owner_fence, reason, override_reason?)`
  — you become owner and controller: `owner_driver_id`, fence +1, lease reset;
  the live claim bound to the attempt is `transferred` and a fresh live
  `implement` claim is created for you; every registered `active`/`adopted`
  subagent of the attempt moves to you (fence +1) and awaits `adopt_subagent`.
  `already_owner` if it is yours.
- A `legacy_unleased` attempt (no lease) is reclaimed only with an explicit
  `override_reason` (design §9.2 step 4; owner confirmation is procedural).
  An admin may reclaim before the grace or without the reason: `break_glass`.

## Subagent registry and takeover (P3, design §4.6, D8, Q8, Q9)

`driver_subagents`: `subagent_id='<driver>/<label>'`, `owner_driver_id` (changes on
takeover/handoff), `origin_driver_id` (never changes), `attempt_id`, `host`,
`runtime` (`local_process|server_process|skillflow_run|remote_session`),
`control_handle` (non-secret hint), `workspace` (`host:path#branch`, its own
branch), `context_ref`/`context_sha256` (retained), `checkpoint_ref`/`checkpoint_sha256`,
`observability`, `status` (`active|settled|adopted|orphaned_unobservable|terminated`),
`fence`, lease columns.

- `register_subagent(project_id, attempt_id, label, host, runtime, workspace, context_ref, context_sha256, control_handle="")`
  — by the attempt's owner, BEFORE the subagent starts; idempotent for identical
  arguments, `subagent_exists` otherwise; `workspace_in_use` if another open
  subagent declares the same workspace; `legacy_unleased` for an attempt without
  an owner (take it over first).
- `update_subagent_checkpoint(project_id, subagent_id, fence, checkpoint_ref, checkpoint_sha256)` — owner, current fence.
- `adopt_subagent(project_id, subagent_id, fence, observability, reason)` — by the
  new owner after a takeover/handoff: `controllable` / `observable_only` →
  `adopted` (you renew it); `unobservable` → `orphaned_unobservable`: not
  renewed, not assumed stopped, fenced out, a STANDING `subagent_orphaned`
  notice to the origin driver, and the response's `continue_from` gives you
  `checkpoint_ref` to continue at once on a NEW branch and workspace (Q9). The
  old branch is reference only.
- `report_subagent_settled(project_id, subagent_id, quiescent=true, report_ref, report_sha256, fence?)`
  — two cases. An ORPHAN: by its origin driver, with any (old) fence (the only
  old-fence write), closed to `terminated`; the standing notice is resolved and
  the current owner notified. An `active`/`adopted` worker of an attempt that is
  no longer active (candidate, failed, superseded, abandoned): by its current
  owner with the current fence (required: `fence_required`), closed to `settled`. On an active attempt:
  `not_orphaned` (report the attempt terminal instead; that settles its workers).
  `quiescence_required` without `quiescent=true`.
- Takeover/handoff moves every live claim held FOR a worker (`subagent=label`,
  any node) with it; orphaning REVOKES them. `heartbeat` judges the registry
  first: a worker you no longer own renews nothing, not even claims with its label.
- The worker's frozen instructions are retained in the private table
  `state_subagent_contexts`, never in the public report-blob table.
- `project_overview.orphaned_subagents` and `project_run_summary.orphaned_subagents`
  count orphans not yet confirmed stopped (public for an opened project; the
  registry itself is private: `list_subagents(project_id, attempt_id?, owner_driver_id?, statuses?)`,
  REST `GET /api/state/projects/{id}/subagents`).

## Handoff (P3, design §6)

- `offer_handoff(project_id, request_key, expected_owner_fence, package, attempt_id|claim_id, to_driver_id?)`
  — by the subject's owner; `to_driver_id` omitted = any member (every
  `project_drivers` row with status `member`, from the P0 registry). One open
  offer per subject (`handoff_pending`). An attempt package MUST declare
  `workers.quiescent` (`quiescence_required`): undeclared is unknown, not quiet.
  The `package` is bounded (16 KiB canonical),
  holds references only — `context_hash`, `observation_version`, `event_cursor`,
  `source`, `workspace`, `workers{quiescent,detail,host}`, `pending_checkpoint`,
  `reports[{ref,sha256}]` (≤20, retained), `open_issue_ids` (≤100),
  `note_entries` (≤50), `private_notes` (≤50), `subagents` (≤100, yours on this
  attempt), `next_step` (≤2000 chars) — and is checked against State where it
  can be (`package_mismatch`). It lives on the handoff row and is never written
  to the notebook. Offers lease 24 h, then `expired`; nothing transfers.
- `accept_handoff(project_id, handoff_id, expected_owner_fence)` — the target (or
  any member for a pool offer; never the offerer: `not_handoff_target`). Moves
  owner, fence (+1), lease, bound claim and registered subagents atomically;
  `stale_fence` if ownership moved since the offer; `handoff_expired` /
  `handoff_closed`. §6.3: if the package says `workers.quiescent=false`, the
  receiver's registered `capabilities.observable_hosts` must cover every host the
  attempt's open subagents (and the package) name, else
  `receiver_cannot_observe_workers`.
- `decline_handoff(project_id, handoff_id, reason)` closes an offer addressed to
  you; on a pool offer it only records the decline. `withdraw_handoff` — offerer
  only. Reads: `list_handoffs`, `get_handoff`; REST `GET /api/state/projects/{id}/handoffs[/{handoff_id}]`.
- A takeover or abandon of the subject voids its open offers (`withdrawn` by `system`).

## Notices (the one notifier)

Every driver-addressed notification — reclaim, takeover, orphan, handoff offer and
reply, override, break glass — goes through `core/driver_notices.py:notify`.
Until the P2 per-driver inbox is deployed it stores a pending `driver_notices`
row (`sender_driver_id` NULL for system notices) and emits a project
`driver_notice` event (payload: `target_driver_id`, `kind`, `subject`, `refs`), so a
driver waiting on the project wakes (the subject ids also sit at the payload top level, so an
attempt-scoped wait sees its notices). `set_inbox_adapter(deliver=, resolve=)` installs
connection-sharing hooks for the P2 inbox; `inbox_message(stored_notice)` normalizes a stored
notice to P2's `send_driver_message` keywords. Read yours with
`list_driver_notices(project_id, statuses?, kinds?)` (admins, and writer
credentials that are not drivers such as the owner's e-mail, pass `driver_id`); REST `GET /api/state/projects/{id}/driver-notices`. Re-pointing to
the P2 inbox is one function (`_deliver`).

## Schema migration (P3)

Transactional, idempotent, in the existing attempt initializer
(`core/state_attempt_schema.py`), rehearsed on a copy of the production database:

- `state_attempts` is REBUILT once (a CHECK cannot be altered in place): status
  gains `abandoned`, column `abandon_kind` is added; every row is copied by
  column name, the `sqlite_sequence` high-water mark is carried over, every
  index and trigger that hung off the table is recreated, counts and foreign
  keys are verified, and a damaged table fails the copy without touching the
  original. `state_external_owners` is rebuilt the same way (status gains
  `abandoned`).
- `state_external_observations` + `late_after_abandon INTEGER NOT NULL DEFAULT 0`,
  `fence INTEGER` (additive).
- New tables: `state_project_enforcement`, `state_handoffs` (private),
  `driver_subagents`, `driver_notices` (never deleted; triggers).
- Nothing is backfilled; legacy attempts stay `legacy_unleased`.

Rolling back code after `abandoned` rows exist: old code's CHECK does not know
the status, so it cannot write those attempts' rows (reads are fine). Confirm
there are none, or accept those nodes read-only, before a rollback.
