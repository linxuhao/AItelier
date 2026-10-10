# Driver claims and leases (multi-driver P1 + P3)

Design: `design/multi-driver-coop.md` §0a (the P3 scope and its five
acceptance criteria), §4 (claims, leases, reclaim), §6 (handoff) and §7.3
(conflict rules). Identity (who a driver is) is P0, see
`docs/driver-identity.md`. P1 added node claims, leases on claims and attempts,
heartbeats and lease events, all record-only. P3 adds the terminal attempt
status `abandoned`, reclaim (abandon / take over), point-to-point handoffs, a
record-only subagent registry, and ENFORCEMENT of the claim rules behind one
per-project switch.

## Status

P1 merged to main (53daff7ff2). P3 merged to main (6dc8674eb) and slimmed on
2026-10-10 (owner-approved, branch `grok/multi-driver-p3-slim`): checkout
occupancy, checkpoint decision fences, pool handoffs, subagent adoption/orphans
and mandatory registration were removed or deferred. Every production project
is `multi_driver=off` and `claim_enforcement=off`, so nothing changes until the
owner turns both on for a project.

P3 acceptance (State node `driver.multi-driver-p3-enforced-claims`):
1. migration: the `state_attempts` rebuild keeps every historical row, the seq
   high-water mark, indexes and triggers; a second run changes nothing;
2. mutual exclusion: with `claim_enforcement=on`, no start without a live
   implement claim; observe/report/structural writes by a non-owner or with an
   old fence are refused (`stale_fence` / `not_attempt_owner` / `not_owner`);
3. reclaim: abandon/take over are refused before lease expiry + 900 s and
   allowed at once after; fence +1; every later write of the old owner and its
   workers is refused; the old owner gets a P2 inbox notice;
4. handoff: offer/accept moves ownership atomically with fence +1;
   decline/withdraw change no ownership;
5. compatibility: with `claim_enforcement=off` P1 behaviour and tests are
   unchanged; the full-suite failure set equals the baseline.

## Switches (owner / admin driver)

- `set_multi_driver(project_id, multi_driver="on"|"off", expected_revision, reason)` —
  record claims and leases (P1). With it off, `claim_node` is refused
  (`multi_driver_off`), new attempts are written exactly as before and report
  `lease_state=legacy_unleased`.
- `set_claim_enforcement(project_id, claim_enforcement="on"|"off", expected_revision, reason)` —
  turn the recorded claims into refusals (P3). It is the ONLY enforcement
  switch: every rule asks one helper (`core.state_enforcement.enforced`), which
  reads `claim_enforcement` and treats it as `off` whenever `multi_driver` is
  off. Turning it on needs `multi_driver=on` (`multi_driver_off` otherwise);
  turning `multi_driver` off turns it off too. Stored in its own row
  (`state_project_enforcement`); shares the `state_project_policy` revision for
  the CAS (read it from `project_overview.policy.revision`). Emits
  `claim_enforcement_policy_changed`.
- `set_review_independence(project_id, review_independence="off"|"advisory", expected_revision, reason)` —
  P4 (design §7.2), advisory only, no `required` mode. With `advisory`, `verify_node`
  still succeeds, but a `kind=review` criterion whose latest evidence came from the
  attempt's current or former owner marks the receipt `provenance_json` with
  `self_reviewed=true` (+ `self_reviewed_criteria`); `project_overview.self_reviewed_receipts`
  counts them. Admin only; needs `multi_driver=on` (`multi_driver_off`); turning
  `multi_driver` off resets it. Same row and CAS as enforcement (column
  `review_independence`, added on open). Emits `review_independence_policy_changed`.
  Rollout steps: `docs/multi-driver-rollout.md`.

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
  abandon, a takeover or an accepted handoff releases or transfers it (a
  transferred claim keeps its purpose and `workspace`). With enforcement off nothing
  is bound or released unless you NAME the claim (`claim_id`+`fence` on the
  start): P1 claim rows and history are byte-for-byte as before.

`release_claim(project_id, claim_id, fence, reason)` — the holder, or an admin
(`break_glass: true` in the event).

Reads (private): `list_claims`, `get_claim`; REST `GET /api/state/projects/{id}/claims[/{claim_id}]`.

## Heartbeat (P1, unchanged)

`heartbeat(project_id, claims=[{claim_id, fence}], attempts=[{attempt_id, fence}],
subagents=[{subagent_id}])`, 1..100 items; writes only the two lease columns.
A `subagents` item renews every live claim you hold for that label
(`no_live_claims` if there is none). The subagent registry has no lease.

## Lease states and events (P1, unchanged)

`healthy` → `expired` (within the 900 s grace) → `reclaimable`; `legacy_unleased`
never expires. Events: `claim_acquired`, `claim_released`, `lease_expired`,
plus P3: `claim_transferred`, `attempt_abandoned`, `attempt_ownership_transferred`,
`claim_overridden`, `subagent_registered`, `handoff_*`, `checkpoint_break_glass`. Expiry is detected lazily (claim reads, `claim_node`,
every `wait_for_state_change` iteration). An attempt is NEVER changed by expiry;
P3 makes the change an explicit, audited member action (below).

## Enforcement (P3, `claim_enforcement=on`)

Every rule has a stable error code; the message starts with it.

| rule (design §7.3) | refusal |
|---|---|
| 1 dispatch: `start_attempt` / `start_external_attempt` need YOUR live `implement` claim on the node, judged by its LEASE at the write (a claim past expiry + grace authorizes nothing, swept or not). Optional `claim_id`+`fence` pin what you believe you hold; a replay naming either is judged on the live claim (and binds it). The same check guards the real `reserved → launching` transition with the claim/fence you named carried in (`recover_attempt` takes them too): a replaced reservation claim launches only when you NAME the replacement (`claim_required` otherwise), never by a silent rebind; another driver's reservation is `not_attempt_owner`. | `claim_required`, `claimed_by_other` (names holder and expiry), `stale_fence`, `not_claim_owner`, `not_attempt_owner` |
| 2 observe: `report_external_attempt` needs the attempt owner and `fence=<owner_fence>`. The previous owner after a takeover/handoff is stale; anyone else is not the owner. An admin is an ordinary reporter (no break-glass reporting path). A report on an ABANDONED attempt by its original reporter is recorded with `late_after_abandon=1`, `resulting_status=superseded`, never current; with `quiescent=true` it must carry a retained, digest-verified report and then settles the owner-registry row (the deployment gate stops counting it). | `fence_required`, `stale_fence`, `not_attempt_owner` |
| 4 structural: `revise_node`, `split_node`, `supersede_node`, `set_node_facet` over a node — or a transitive dependent, which the write invalidates — on which another driver holds a live claim or an active owned attempt are refused. Owner check only, in the write's transaction; there is no override. Ask the holder to release or hand it off. | `not_owner` (facts: `holders`) |
| 5 hold: `set_node_hold(held=false)` on a hold another driver placed needs `override_reason`; the placer gets a driver-inbox notice and `claim_overridden` is emitted. | `override_reason_required` |
| 9 one controller per run: the checkpoint of a SkillFlow run bound to an owned attempt — and the failed-run rescue behind the same approve route — is answered by the owner driver only (MCP `answer_checkpoint`, REST `/api/meta/{project}/checkpoint/approve|reject`, `/api/runs/{run}/checkpoint/approve|reject`). ONE check at the entry of the route; nothing is held open until the engine call (ownership moving in between is an accepted gap). | `not_attempt_owner` (409 over REST) |
| Q13 admin: an admin (owner e-mail, `owner-cli`) is never refused by rules 1, 4, 5 and 9 or by the reclaim grace; the event of its write carries `break_glass: true`. That flag is the whole mechanism: no notice, no separate path. | — |

After `abandon_kind=unknown` the node's next attempt must declare `base_sha`
and ride a claim whose checkout (`host:path`; a different `#branch` is the same
writer) differs from the abandoned attempt's (`workspace_in_use`). There is no
other checkout check: two claims on different nodes may declare the same
checkout (deferred, design §0a).

Priority changes (`set_node_priority`), evidence and verification are unchanged:
any member may record evidence on any CANDIDATE, under any `director_identity`
P0 accepts (no subagent registration is required).

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
  quiescent and REQUIRES `report_ref`/`report_sha256` (retained like any report).
  `unknown` says you do not
  know: the node's next attempt must declare `base_sha` and ride a claim whose
  `workspace` differs from the abandoned attempt's (`workspace_in_use` otherwise)
  — two possibly live workers never share a checkout. Only external attempts
  can be abandoned (`not_external`); a SkillFlow attempt's run is alive or not
  by the engine's account, so its controller is taken over instead.
- Both reclaim actions are for project MEMBERS (`project_drivers.status=member`, judged
  inside the ownership transaction; `not_project_member` otherwise; an admin passes as `break_glass`; with
  driver identity off there is no membership to check).
- `take_over_attempt(attempt_id, expected_owner_fence, reason, override_reason?)`
  — you become owner and controller: `owner_driver_id`, fence +1, lease reset;
  the live claim bound to the attempt is `transferred` and a fresh live
  `implement` claim (same `workspace`) is created for you. The previous owner's
  workers are not inherited: the new fence refuses their State writes, and you
  register the workers you start. `already_owner` if it is yours.
- The previous owner gets a `takeover_notice` in its P2 driver inbox (see Notices).
- A `legacy_unleased` attempt (no lease) is reclaimed only with an explicit
  `override_reason` (design §9.2 step 4; owner confirmation is procedural).
  An admin may reclaim before the grace or without the reason: `break_glass`.

## Subagent registry (P3, record only, design §4.6)

`driver_subagents`: `subagent_id='<parent driver>/<label>'`, `parent_driver_id`,
`project_id`, `attempt_id`, `node_key`, `workspace` (the checkout it writes),
`created_at`. No instructions, leases, checkpoints or takeover state.

- `register_subagent(project_id, attempt_id, label, workspace)` — by the
  attempt's owner (`not_attempt_owner`), on an active attempt
  (`attempt_not_active`), in a `multi_driver=on` project; idempotent for
  identical arguments, `subagent_exists` otherwise.
- `list_subagents(project_id, attempt_id?, parent_driver_id?)` (private); REST
  `GET /api/state/projects/{id}/subagents`.
- The registry is for visibility: nothing requires it and it grants nothing
  (claims, evidence and heartbeats do not consult it). The record does not move
  on a takeover or handoff; the old owner's workers are fenced out by the
  attempt's fence +1 alone. Adoption, orphan handling and mandatory
  registration are deferred (design §0a).

## Handoff (P3, point to point, design §6)

- `offer_handoff(project_id, request_key, expected_owner_fence, package, attempt_id|claim_id, to_driver_id)`
  — by the subject's owner, to ONE other driver (`to_driver_id` is required;
  there are no pool offers). One open offer per subject (`handoff_pending`).
  The `package` is bounded (16 KiB canonical), holds references only —
  `context_hash`, `observation_version`, `event_cursor`, `source`, `workspace`,
  `workers{quiescent,detail,host}`, `pending_checkpoint`, `reports[{ref,sha256}]`
  (≤20, retained), `open_issue_ids` (≤100), `note_entries` (≤50),
  `private_notes` (≤50), `subagents` (≤100), `next_step` (≤2000 chars) — and is
  checked against State where it can be (`package_mismatch`). It lives on the
  handoff row and is never written to the notebook. Offers lease 24 h, then
  `expired`; nothing transfers. The receiver gets a `handoff_offer` inbox
  message (sender = the offerer, refs carry `handoff_id`).
- `accept_handoff(project_id, handoff_id, expected_owner_fence)` — the named
  receiver only (`not_handoff_target`, also for the offerer). Moves owner,
  fence (+1), lease and the bound claim (with its workspace) atomically;
  `stale_fence` if ownership moved since the offer; `handoff_expired` /
  `handoff_closed`. When the package does not declare `workers.quiescent=true`
  the accept still proceeds; its result and the `attempt_ownership_transferred`
  event carry `quiescence_warning` (with the attempt's registered subagents).
  The offerer gets a `handoff_reply` message.
- `decline_handoff(project_id, handoff_id, reason)` closes the offer (the
  offerer is told); `withdraw_handoff` — offerer (an admin: `break_glass` in the
  event). Neither changes ownership. Reads: `list_handoffs`, `get_handoff`; REST
  `GET /api/state/projects/{id}/handoffs[/{handoff_id}]`.
- A takeover or abandon of the subject voids its open offers (`withdrawn` by `system`).

## Notices (P2 driver inbox)

Every driver-addressed notification — reclaim/takeover (`takeover_notice`, no
sender), handoff offer and reply (`handoff_offer` / `handoff_reply`, sender =
the acting driver), hold override (`lease_notice`) — goes through
`core/driver_notices.py:notify`, which writes ONE message with one delivery into
the target's per-driver inbox (`core/driver_inbox.py:deliver_notice`) on the
connection of the write that caused it: a rolled-back write leaves no message.
Read it with `list_driver_messages` / `wait_for_driver_inbox`; acknowledge or
resolve it like any inbox message. Refs are reduced to `attempt_id`,
`claim_id`, `handoff_id`, `node_key`. There is no second copy (no notice table,
no `driver_notice` event) and nothing is resolved by the system. The inbox is
keyed to the driver registry, so a target that is not a registered driver gets
nothing (`notified: {"skipped": "unregistered_driver"}`); the write itself
proceeds.

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
  `driver_subagents` (record-only shape; never deleted; trigger).
- Nothing is backfilled; legacy attempts stay `legacy_unleased`.
- Slimming (2026-10-10), at startup: the tables of removed mechanisms
  (`state_subagent_contexts`, `driver_notices`, `driver_checkpoint_decisions`)
  are dropped only while EMPTY (a table holding rows is left untouched and
  unused), and the takeover-era `driver_subagents` shape is dropped when empty
  or renamed to `driver_subagents_p3_takeover` when it holds rows, before the
  record-only table is created. All were empty in production (rehearsed on a
  copy, see the P3 slim report).

Rolling back code after `abandoned` rows exist: old code's CHECK does not know
the status, so it cannot write those attempts' rows (reads are fine). Confirm
there are none, or accept those nodes read-only, before a rollback.
