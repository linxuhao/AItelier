# P3 — enforced claims, reclaim, handoff, subagent takeover (branch `grok/multi-driver-p3`)

Driver: Claude Fable 5.1 working for `grok`. Base: main `53daff7ff2` (P0 identity + P1 record-only claims).
Design: `design/multi-driver-coop.md` §4.4, §4.6, §6, §7.3, §9.2, §10 (P3 row), §11. Node:
`driver.multi-driver-p3-enforced-claims`. Written 2026-10-10; this file is deliberately uncommitted.

Commits on the branch (small, in order): `29a981e9` core modules + schema + wiring; `675d9daa` tests +
enforcement table + guide/docs/design; `4bef70e0` docs/driver-claims.md; `200b73c3` argument tables /
run-summary shape / notice read rule (`git log 53daff7ff2..HEAD`). HEAD = `200b73c3`.

## What was built

### 1 abandoned-and-reclaim (`core/state_recovery.py`, `core/state_attempt_schema.py`)
- New terminal attempt status **`abandoned`** plus column `abandon_kind` on `state_attempts`; `abandoned`
  also on `state_external_owners.status`. Both tables are REBUILT once by one shared preserving helper
  (`_rebuild`: copy every row by column name, carry the `sqlite_sequence` high-water mark, recreate every
  index/trigger that hung off the table, count check, FK check, one write transaction; a damaged table
  fails the copy and leaves the original untouched). The old executor-neutral upgrade now uses the same
  helper. `state_external_observations` gains `late_after_abandon`, `fence` (additive).
- `abandon_external_attempt(attempt_id, expected_owner_fence, abandon_kind=confirmed_stopped|unknown, reason,
  report_ref?, report_sha256?, override_reason?)` and `take_over_attempt(attempt_id, expected_owner_fence,
  reason, override_reason?)`: refused `lease_not_expired` (with `reclaimable_at`) before expiry+900 s, allowed
  at exactly expiry+900 s with no further wait, `stale_fence` on a wrong fence, fence +1 on success, previous
  owner notified. `confirmed_stopped` needs a retained report and settles the attempt's registered subagents;
  `unknown` makes the node's next attempt declare `base_sha` and claim a different workspace
  (`workspace_in_use`). Take-over moves the bound live claim (`transferred` + fresh live claim for the taker),
  inherits registered subagents (owner decision D8), resets the lease, voids open handoffs. Legacy
  (`legacy_unleased`) attempts need `override_reason`; an admin before the grace is `break_glass`.
- Late observation on an abandoned attempt (original reporter, any fence): recorded with
  `late_after_abandon=1`, `resulting_status=superseded`; attempt and node never change. A SkillFlow attempt
  cannot be abandoned (`not_external`): its run is alive or not by the engine's account; its CONTROLLER is
  taken over instead.
- `abandoned` handled everywhere statuses are enumerated: readiness/frontier (`ACTIVE` excludes it → node
  `ready`), `wait_disposition` terminal set, `StateService._TERMINAL`, run summary (`external_counts.other`
  + top-level `abandoned_external`), external owners backfill, SkillFlow `reconcile` (an abandoned attempt's
  run outcome is historical), `get_attempt.lease_state=None`. Sweep test
  `test_abandoned_is_in_every_terminal_enumeration`.

### 2 enforced-dispatch (`core/state_enforcement.py`, policy table `state_project_enforcement`)
- Separate switch `set_claim_enforcement(project_id, on|off, expected_revision, reason)` (admin only, needs
  `multi_driver=on`, turned off automatically when `multi_driver` goes off). Stored in its OWN table
  `state_project_enforcement` — NOT a new column on `state_project_policy`, because the P1 migration test
  pins `multi_driver` as that table's last column; it shares the policy revision for the CAS.
  `project_overview.policy.claim_enforcement` reports it. `multi_driver=on` alone keeps P1 record-only
  behaviour (test `test_multi_driver_on_alone_does_not_enforce`); `multi_driver=off` projects are untouched.
- Rules (stable codes): dispatch `claim_required` / `claimed_by_other` / `stale_fence` (claim bound to the
  attempt; optional `claim_id`+`fence` on `start_attempt`/`start_external_attempt`); observe
  `fence_required` / `stale_fence` / `not_attempt_owner` (new optional `fence` on `report_external_attempt`);
  structural `override_reason_required` on revise/split/supersede/set_node_facet over a node or any
  transitive dependent held by another driver (claim or active owned attempt), with `override_notice` to the
  holder and `claim_overridden` event; hold release over another driver's hold `override_reason_required`;
  `workspace_in_use` for exclusive claims and subagent registrations; checkpoints of a run bound to an owned
  attempt decided by the owner only (`checkpoint_controller`, wired into MCP `answer_checkpoint` and the REST
  `/checkpoint/approve|reject` doors via `api/meta_routers.state_checkpoint_controller`; `run_routers`
  passes the request through). Admin writes bypass as `break_glass=true` + notice (Q13).
- Terminal external reports now release the attempt's bound live claims (design §4.3).

### 3 handoff (`core/state_handoffs.py`, table `state_handoffs`, PRIVATE)
- `offer_handoff` / `accept_handoff` / `decline_handoff` / `withdraw_handoff`; `list_handoffs`, `get_handoff`.
  Accept moves owner, fence (+1), lease, bound claim and registered subagents in one transaction (attempt
  subjects) or transfers the claim with a fresh live claim of the same purpose/workspace (claim subjects).
  `receiver_cannot_observe_workers` when `package.workers.quiescent=false` and the receiver's registered
  `capabilities.observable_hosts` do not cover the hosts of the attempt's open subagents (and the package).
  Package: ≤16 KiB canonical, allow-listed keys, list caps (reports 20 / issues 100 / notes 50 / private
  notes 50 / subagents 100), `next_step` ≤2000 chars; `context_hash`, `observation_version`, `event_cursor`,
  subagent ids and report refs are checked against State (`package_mismatch`); stored on the handoff row,
  never in the notebook. Offer lease 24 h → `expired`; one open offer per subject (`handoff_pending`);
  idempotent by `request_key`; pool offers (`to_driver_id` omitted) survive individual declines.

### 4 subagent-takeover (`core/state_subagents.py`, table `driver_subagents`)
- `register_subagent` (owner only, before the worker starts, own `#branch`, context retained by hash,
  `workspace_in_use`, idempotent / `subagent_exists`), `update_subagent_checkpoint`, `adopt_subagent`
  (`controllable|observable_only` → `adopted`, renewed by the new owner; `unobservable` →
  `orphaned_unobservable`, fence +1, never renewed (`heartbeat` item error `orphaned`), STANDING
  `subagent_orphaned` notice to the ORIGIN driver, response `continue_from` with `checkpoint_ref` and the
  new-branch rule, Q9), `report_subagent_settled` (origin driver, any old fence, only closes an orphan →
  `terminated`, resolves the standing notice, notifies the current owner). Mandatory in enforced projects at
  the two server-visible points (Q8): a claim held for a subagent and evidence under a `<driver>/<label>`
  identity → `subagent_unregistered`. `heartbeat` subagent items renew the registry row (inherited
  subagents included). `project_overview.orphaned_subagents` / `project_run_summary.orphaned_subagents`
  count open orphans; the registry itself is private (a privacy projection exposes only `project_id,status`
  of opened projects, so the public overview equals the writer's — the P1 equality test demands that).

### Notifier (`core/driver_notices.py`) — the one interface to re-point at the P2 inbox
Every driver-addressed notification (abandon, takeover, orphan, handoff offer/reply, override, break glass,
admin checkpoint, admin dispatch) calls `driver_notices.notify(conn, store, target_driver_id, kind, subject,
body, refs, project_id, delivery_mode, sender_driver_id=None)`. Today `_deliver` writes one pending
`driver_notices` row (sender NULL for system notices; kinds mirror §5.2) and one project `driver_notice`
event (payload `target_driver_id`, `kind`, `subject`, `refs`), so `wait_for_state_change` wakes the target.
`driver_notices.resolve` closes notices by id or (project, kind, ref). Read: `list_driver_notices(project_id,
driver_id?, statuses?, kinds?)` — a driver reads its own, an admin anyone's, a non-driver writer credential
(owner e-mail) names `driver_id`. Re-pointing to the P2 `driver_inbox_messages/deliveries` is replacing
`_deliver` (and optionally `resolve`); every caller already passes the inbox's tuple.

### Surface
- Typed contract (`core/state_commands.py`): 11 new write actions, 4 new private reads, new optional
  fields `claim_id`/`fence` (start_*), `fence` (report), `override_reason` (revise/split/supersede/facet/hold).
  Handlers are bound methods of `service.recovery/subagents/handoffs/notices/portfolio` so the write-opening
  mutation gate derives them. MCP: same two tools, descriptions list the actions. REST GET (all private,
  derived by the exhaustive-doors test): `/projects/{id}/subagents`, `/handoffs`, `/handoffs/{handoff_id}`,
  `/driver-notices`.
- Docs: `docs/driver-claims.md` (rewritten for P1+P3), `docs/state-agent-driver.md` (claims section),
  driver guide `core/state_driver_guide.py` ("Claims and leases" section: enforcement, reclaim, subagents,
  handoff, notices), design §10 P3 row + progress line. Canary for `state_handoffs` in
  `tests/support/state_canaries.py`; `state_project_enforcement` classified PUBLIC, `state_handoffs` PRIVATE.

## Schema changes (summary)
| table | change |
|---|---|
| `state_attempts` | REBUILT: status CHECK + `abandoned`; new `abandon_kind TEXT CHECK(...)`; lease columns kept last |
| `state_external_owners` | REBUILT: status CHECK + `abandoned` |
| `state_external_observations` | + `late_after_abandon INTEGER NOT NULL DEFAULT 0`, + `fence INTEGER` |
| `state_project_enforcement` | new (project_id PK, claim_enforcement, reason, actor, updated_at) |
| `state_handoffs` | new, no-delete trigger, FKs to project/attempt/claim |
| `driver_subagents` | new, no-delete trigger, FK to attempt |
| `driver_notices` | new, no-delete trigger |

## Migration rehearsal (copy of production, 2026-10-10 00:36–00:45 UTC)
Copy: `~/.AItelier/aitelier.db` → `/tmp/p3-rehearsal/aitelier.db` (16,724,619,264 bytes) with Python
`sqlite3.Connection.backup(pages=-1)` in 96 s. The sqlite3 CLI `.backup` was abandoned twice: it copies in
100-page steps and restarts on every production commit, so on the live DB it stalled at 5–8 GB for 10+ min.
Rehearsal script: `/tmp/p3-logs/rehearse.py` (host python, `AITELIER_HOME=/tmp/p3-rehearsal/home`); JSON:
`/tmp/p3-logs/report.json`. The copy was deleted at 00:45 UTC (`/tmp/p3-rehearsal` removed; disk back to 47 GB free).

- First run 0.87 s; second run 0.02 s and byte-identical snapshot (`second_run_idempotent: true`).
- `state_attempts` sql had no `'abandoned'` before, has it after; `state_external_owners` likewise.
- `PRAGMA foreign_key_check` = [] ; `PRAGMA integrity_check` = ok (whole 16 GB file).
- `sqlite_sequence.state_attempts` high-water: 2721 before → 2721 after (2693 rows).
- Attempt status histogram unchanged: candidate 2045, failed 569, paused 2, running 4, superseded 73.
  Owner statuses after: active 4, paused 1, settled 2152. Attempt digest (count, min/max seq, Σlen id,
  Σlen context_json) identical.
- Indexes/triggers on attempts after: `state_attempts_one_active`, `state_attempts_node`,
  `state_attempts_lease`, `state_attempt_external_identity`, `state_attempt_executor_immutable` (+ FK users).
  `lost_schema_objects: []`.
- Row counts per table, before → after (all pre-existing tables unchanged; new tables 0):

| table | before | after |
|---|---|---|
| drivers / driver_audit | 4 / 9 | 4 / 9 |
| state_attempts | 2693 | 2693 |
| state_acceptances | 482 | 482 |
| state_evidence | 6768 | 6768 |
| state_external_observations | 2352 | 2352 |
| state_external_owners | 2157 | 2157 |
| state_external_report_blobs | 6683 | 6683 |
| state_git_artifacts | 492 | 492 |
| state_events | 20959 | 20959 |
| state_nodes / state_node_revisions / state_dependencies | 578 / 983 / 637 | same |
| state_projects / state_project_policy / state_project_access | 13 / 4 / 1 | same |
| state_node_holds / state_source_bindings / state_history_links | 48 / 4 / 443 | same |
| state_issues / state_issue_nodes | 378 / 475 | same |
| state_director_messages / deliveries / idempotency / inbox_sequences | 255 / 267 / 546 / 9 | same |
| state_driver_notes / revisions / entries | 9 / 1982 / 929 | same |
| state_design_revisions / baselines / heads / bindings | 10 / 6 / 1 / 10 | same |
| state_node_claims / state_claim_history | 0 / 0 | 0 / 0 |
| state_project_enforcement, state_handoffs, driver_subagents, driver_notices | — | 0 each |

## Tests (throwaway containers: `docker run --rm --network none --cpus 2 -m 2g --user 1000:1000 -v <tree>:/src:ro aitelier:latest python -m pytest -p no:cacheprovider -q tests/`, ≤2 at once)
- New: `tests/unit/test_state_p3_enforced_claims.py` — 33 tests, one class per acceptance item
  (abandoned-and-reclaim 9, enforced-dispatch 11, handoff 5, subagent-takeover 3, enumeration sweep 1,
  REST + MCP transports 2), all passing. Includes the pre-P3 → P3 rebuild (rows, seq 999 watermark, custom
  index, triggers, FK/integrity, idempotent) and the damaged-table refusal.
- Full suite on base `53daff7ff2`: **361 failed, 6372 passed, 13 skipped, 23 errors** (58:50).
- Full suite on HEAD (first run, before `200b73c3`): 375 failed, 6413 passed, 13 skipped, 23 errors (1:05:41).
  Failure-ID diff vs base: 14 new, 0 fixed. Causes and fixes:
  - 8 × `test_private_read_verdict_at_execution` — its hand-chosen argument table must name every
    non-public read; added entries for the 4 new reads (and made a non-driver writer credential able to read
    notices by `driver_id`, so a trusted caller gets an answer).
  - 4 × `test_write_opening_coverage[register_subagent|update_subagent_checkpoint|report_subagent_settled|offer_handoff]`
    — best-effort arguments failed pydantic (64-hex digests, one handoff subject) so the mutation never
    fired; added `_ARGUMENT_SEEDS` entries (the file's own seeding mechanism).
  - 1 × `tests/integration/test_state_run_summary.py` — asserts the exact `external_counts` keys; moved the
    abandoned count to top-level `abandoned_external` (still inside `other`).
  - 1 × `test_write_opening_coverage[resolve_director_message]` (rc 4, fired 0, empty tail) and 1 ×
    `test_bash_cleanup_is_async` (0.80 s > 0.5 s loop freeze) — both pass when rerun alone; load flakes
    from two 2-CPU suites running concurrently.
  Targeted rerun after the fixes: 94 passed (run summary, private-read verdict, P3 module, P1 claims, the 4
  mutation-gate params).
- Full suite on HEAD `200b73c3` (final, alone in its container, 29:53): **361 failed, 6427 passed, 13 skipped,
  23 errors** — failure-ID set IDENTICAL to base (`comm` new = 0, fixed = 0); +55 passed = the 33 new P3 tests
  and the new parametrizations of the exhaustive-door, mutation-gate and argument-table suites.
- The 361 pre-existing base failures are untouched (web tools / SearXNG network-less, godot, etc.); logs in
  `/tmp/p3-logs/{base_full,head_full,head_full2}.log`, ID sets `/tmp/p3-logs/{base,head}_fail.txt`.

## Known gaps / decisions for review
1. **Enforcement switch is its own table**, not a `state_project_policy` column (design §9.1 lists it as a
   column) — the P1 migration test pins `multi_driver` as the last policy column and I did not weaken it.
2. **`reporting_actor` is not changed on takeover** (design §4.4 says it is): the trigger
   `state_attempt_executor_immutable` makes it immutable and I kept that invariant; ownership is judged by
   `owner_driver_id` + fence, and the original reporter's late reports are `stale_fence`/`late_after_abandon`.
3. **Structural guard is check → write → notify** in three transactions (the store methods open their own):
   a claim taken between check and write is not re-checked (small TOCTOU); a refused write sends no notice.
4. **Checkpoint rule** covers MCP `answer_checkpoint` and the REST `/checkpoint/approve|reject` doors
   (meta + run routers). Legacy credentials with identity off are unconstrained (no owner exists).
   Non-driver callers in an enforced project with an owned attempt are refused (`not_attempt_owner`) unless
   admin.
5. **Q8 mandatory registration** is enforced only where the server can see a subagent act: claims with
   `subagent=` and evidence with a `<driver>/<label>` `director_identity`. Observations carry no identity, so
   an unregistered subagent reporting through its parent is invisible by construction (design §12.1 item 5).
6. **Observability for handoff** reads `drivers.capabilities.observable_hosts` via the P0 registry; with
   identity off (no registry) any non-quiescent handoff is refused. Tests monkeypatch the lookup.
7. **Abandon settles subagents only for `confirmed_stopped`**; for `unknown` they stay `active` under the old
   owner and simply lapse (nobody attested anything about them).
8. **Pool-offer decline** does not close the offer (buffet, D2); a directed decline does.
9. Web UI badges, PostCompact hook and the AMI legacy attempt walk-through (P3 exit criterion) are not done
   here: the hook already shows `attempt_status` generically; the AMI reclaim needs the owner (Q11).
10. `heartbeat` of an inherited subagent whose id is under another driver's prefix is allowed when the
    registry says you own it (P1 refused any id not under your prefix; the registry decides now).
11. P2 inbox not on main: all notices go through `core/driver_notices.py` as described; `driver_notices`
    rows are never deleted (resolve only).

## Fix round 1 (2026-10-10, after the Codex review `P3_REVIEW_CODEX.md`)

Every finding was verified against the code of `200b73c3` before deciding. Fix commits: `44a38675`
(code + tests), `89f5fcbc` (docs). Regression tests: `tests/unit/test_state_p3_fix_round1.py` — all 19 FAIL
on `200b73c3` (verified by copying the module into a detached worktree of that commit and running it in a
throwaway container: 19 failed, 0 passed) and pass on `44a38675`. Existing P3 tests that encoded the old
behaviour were adjusted to the fixed semantics (bound claim on an explicit `claim_id`, `workers` declared in
handoff packages, `not_subagent_owner` before `not_orphaned`); no existing assertion outside P3's own module
was touched.

| # | verdict | fix (commit `44a38675`) | regression test | evidence / notes |
|---|---|---|---|---|
| 1 | CONFIRMED | `claim_launch(attempt_id, driver_id, is_admin, actor)` runs `state_enforcement.launch_authorization` inside the `reserved → launching` transaction (owner + live implement claim; admin = `break_glass` event + owner notice); `_reserve` refuses another driver's replay of an enforced owned reservation (`not_attempt_owner`) | `TestFinding1LaunchAuthorization` (3 tests) | `recover_attempt` → `_launch_or_recover` → `claim_launch` had no ownership check (state_service.py:1036/504, state_attempts.py:465). External replays were already refused by the request hash (reporting actor is hashed); SkillFlow replays were not. |
| 2 | CONFIRMED | `_release_bound_claims` re-creates every transferred claim with its workspace, purpose and subagent; `new_claim_for(purpose, workspace, subagent)` | `TestFinding2TransferKeepsWorkspace` | `new_claim_for` defaulted `workspace=""` and the caller passed none. |
| 3 | CONFIRMED | `state_enforcement.checkout_in_use` / `refuse_checkout_in_use`: one writer per CHECKOUT (`host:path`, branch suffix ignored) across live exclusive claims and open subagents INCLUDING `orphaned_unobservable`; used by `claim_node`, `register_subagent`, `_after_unknown_abandon` | `TestFinding3OrphanKeepsCheckout` | registration clash excluded orphans and compared the full `host:path#branch` string. |
| 4 | CONFIRMED | `subagent_registered` requires `owner_driver_id = caller`; `heartbeat` judges the registry row before renewing anything; takeover/handoff moves every live claim with `subagent=label` (`transfer_subagent_claims`), orphaning revokes them (`revoke_subagent_claims`, status `revoked`) | `TestFinding4FormerOwnerIsFencedOut` | state_enforcement.py:258 ignored `driver_id`; state_claims.py:479 renewed claims first. |
| 5 | CONFIRMED | attempt packages must declare `workers.quiescent` (`quiescence_required`); `_check_observability` no longer defaults undeclared to quiescent | `test_finding5_…` | `workers.get("quiescent", True)`. |
| 6 | CONFIRMED | `StructuralGuard.check/notify` take the write connection; `revise_node`/`split_node`/`supersede_node`/`set_node_facet` and the hold release run check + write + audit event + notices in ONE `BEGIN IMMEDIATE` transaction (store gained `_split`, `_supersede`, `_set_facet`) | `test_finding6_…` (a failing notice rolls the revision back; hold path too) | three transactions before. |
| 7 | CONFIRMED | `live_implement_claim` judges the lease at the write; a `reclaimable` claim is absent (`claim_required`, naming the lapsed claim); also used by `launch_authorization` | `test_finding7_…` (expiry + grace, no sweep, dispatch refused) | `dispatch_claim` checked `status='live'` only. |
| 8 | CONFIRMED | `_authorize_report` returns `(late, break_glass)`; an admin (owner-cli or owner e-mail) is admitted on any attempt, every integrity check applies, event carries `break_glass`, owner notified in the observation transaction | `test_finding8_…` | `self.is_admin` was never consulted. |
| 9 | CONFIRMED | with enforcement off nothing is bound unless the caller names `claim_id`; release-on-terminal only touches bound claims, so P1 rows/history are unchanged | `test_finding9_…` | implicit bind + unconditional release at state_external.py:279. |
| 10 | CONFIRMED | `EXTERNAL_OWNER_STATUSES` gains `abandoned`; the registry query joins `state_attempts.abandon_kind` and drops `abandoned` + `confirmed_stopped`; `abandoned` (unknown) stays a durable blocker and matches processes like `unknown`; a late `quiescent=true` report by the original reporter (or an admin) settles the owner row (`owner_settled`) | `test_finding10_…` (both kinds, through `dq.measure`) | before: invalid inventory ("unknown status 'abandoned'") and a permanent blocker for both kinds. |
| 11 | CONFIRMED | a terminal quiescent report settles the attempt's `active`/`adopted` workers (`settle_open_subagents`, `settled_subagents` in the response); `report_subagent_settled` also lets the CURRENT owner (current fence) settle a worker of an attempt that is no longer active | `test_finding11_…` (2 tests) | workers stayed active and reserved their checkout forever. |
| 12 | CONFIRMED | `new_claim_for(purpose=…)` inserts the real purpose (history, event, exclusive index) | `test_finding12_…` (review claim handed off beside an implement claim) | insert-as-implement then UPDATE violated `state_node_claims_one_exclusive`. |
| 13 | CONFIRMED | new PRIVATE table `state_subagent_contexts` (immutable, canary planted) holds the frozen instructions; nothing goes to `state_external_report_blobs` | `test_finding13_…` (+ `test_undeclared_reader…` / `test_every_private_table_reader…` via the canary) | `store_report_blob` wrote to the public blob table. |
| 14 | CONFIRMED | `_members` selects `status == "member"` | `test_finding14_…` (real `drivers` registry: member notified, removed not) | `project_drivers.status ∈ {member, removed}`; `"active"` matched nothing. |
| 15 | CONFIRMED (minor, fixed) | `driver_notice` events carry `attempt_id`/`claim_id`/`subagent_id`/`handoff_id`/`node_key`/`run_id` at the payload top level | `test_finding15_…` (attempt-scoped wait returns the notice) | `scan` filters on `$.attempt_id`. |
| 16 | CONFIRMED (minor, contract written down + helper) | `driver_notices.inbox_message(notice, project_members=…)` normalizes a P3 notice to P2's `send_driver_message` keywords; contract written in the module header (below) | `test_finding16_…` | P2 branch (`12fbef9a`, read-only inspection, not merged) differs as the review says. |

### P2 adapter contract (finding 16)
Read-only inspection of the P2 branch commit `12fbef9a` (`core/driver_inbox.py`, not merged, not pulled):
`DriverInbox.send_driver_message(request_key, subject, body, target_driver_id | project_members, project_id,
kind, delivery_mode, refs, reply_to_message_id, _system)` with: system kinds `lease_notice | takeover_notice |
subagent_orphaned | handoff_offer` (and `handoff_reply` only as a DRIVER kind with a sender); `subject` 1..200
chars; `body` ≤ 8000; `refs` keys restricted to `attempt_id | issue_id | claim_id | subagent_id` (+ `node_key`
with a project) whose values exist, no nulls, no extra keys; it opens its OWN write transaction and records
idempotency by `(actor, operation, request_key)`. P3 differs in: two extra kinds (`override_notice`,
`break_glass` → `lease_notice` with a `[kind]` body prefix), subjects up to 300 chars, richer refs (`fence`,
`held`, `break_glass`, `action`, `by_actor`, `observation_id`, `run_id`, `workspace`, …), and delivery INSIDE the
ownership transaction. `inbox_message` performs the normalization (kind map, subject/body caps, ref filter,
`request_key = notice_id`, `_system = sender is NULL`, pool offers as `project_members`). What the merge needs
on the P2 side: a connection-taking row insert (the body of `send_driver_message` after validation) callable
from `driver_notices._deliver(conn, …)`, plus a connection-taking resolve for `driver_notices.resolve`; the
reviewer's merge note (conflicts in `core/state_changes.py` and `core/state_service.py`) stands.

### Test results (fix round 1)
- Targeted suites after the fixes (claims, external, attempts, run summary, deployment quiescence, privacy
  doors, P3, fix-round module): 529 passed, 0 failed.
- Full suite on HEAD `44a38675` (docs-only `89f5fcbc` on top), one container, 29:09: **361 failed, 6446 passed,
  13 skipped, 23 errors** — failure-ID set IDENTICAL to base `53daff7ff2` (361F; `comm` new = 0, fixed = 0);
  +19 passed = the fix-round regression tests. Logs: `/tmp/p3-logs/head_full3.log`, `/tmp/p3-logs/oldcheck.log`
  (the 19 tests failing on `200b73c3`). Migration/rebuild code was not changed in this round, so the
  rehearsal was not repeated.

## Fix round 2 (2026-10-10, after `P3_REVIEW2_CODEX.md`)

Every item was verified against the code of `89f5fcbc` first; all 12 are CONFIRMED (none rejected). Fix commit:
`8c3d7079` (code, tests, docs). Regression tests: `tests/unit/test_state_p3_fix_round2.py` — all 14 FAIL on
`89f5fcbc` (the module was copied into a detached worktree of that commit and run in a throwaway container:
14 failed, 0 passed; the module's autouse fixture tolerates the pre-fix tree so each test fails for ITS
reason) and pass on `8c3d7079`. Two round-1 assertions that encoded a defect were corrected and are explained
below (F10, F16). No other existing assertion was changed.

| item | verdict | fix (commit `8c3d7079`) | regression test | evidence / notes |
|---|---|---|---|---|
| R1 stale-fence replay | CONFIRMED | `_reserve` replay judges a NAMED `claim_id`/`fence` through `dispatch_claim` (release C1, claim C2, replay with C1 → `stale_fence`); `launch_authorization` returns `(break_glass, claim_to_bind)`: a released/lapsed bound claim does not authorize, the owner's current live claim does and is BOUND at launch (`attempt_launching.claim_id`) | `TestR1StaleFenceReplay` (2) | state_attempts.py replay path returned the row untested; launch accepted any owner claim without binding. |
| R2 takeover after sweep | CONFIRMED | `checkout_in_use` now counts the executor checkout of every ACTIVE attempt (workspace of the claim it was dispatched/transferred on, whatever that claim's status is); `_release_bound_claims` sources the replacement from the latest bound claim when none is live and re-validates occupancy (`refuse_checkout_in_use(exclude_attempt=…, same_executor=…)`) | `test_r2_…` (sweep via `list_claims` before the takeover) | state_recovery.py:85 selected `status='live'` only → `workspace=""`. |
| R4 evidence authorization | CONFIRMED | `StateAttempts.record_evidence(authorize=)` runs the subagent verdict INSIDE the evidence write transaction before the insert; the service keeps the same check early (fast refusal) and passes it as `authorize` | `test_r4_…` (hook sees `conn.in_transaction`, refusal leaves no row) | state_service.py:1205 read in a separate transaction. |
| R15 decline replies | CONFIRMED | decline notices carry `attempt_id`/`claim_id` in refs (flattened to the event top level) | `test_r15_…` (attempt-scoped wait returns the reply, cursor advances past it) | state_handoffs.py:409 refs lacked the subject. |
| R16 / N7 P2 normalization | CONFIRMED | `inbox_message` takes ONE shape — the complete stored notice (row with `refs_json`, or `notify()`'s now-complete return incl. project/sender/body/refs) — and refuses a notice without `sender_driver_id`; `set_inbox_adapter(deliver=, resolve=)` installs connection-sharing hooks called inside the ownership transaction (`_deliver` → `deliver(conn, inbox_message(stored))`, `resolve` → `resolve(conn, rows_with_refs, reason)` so `handoff_id` correlation survives) | `TestR16InboxAdapter` (2); F16 rewritten | helper read `refs` only and `notify()` returned a partial dict. The P2 branch was inspected read-only (`12fbef9a`), not merged. |
| N1 unverified late quiescence | CONFIRMED | a late `quiescent=true` report on an abandoned attempt must carry a retained, digest-verified report (`retain_report(completed=False)`, stored); invalid ref/digest → `StateConflict`, nothing recorded, blocker stays | `test_n1_…`; F10 corrected (bare `/tmp/x` + zero digest now refused; real file settles; blocker checked in between) | state_external.py:261 settled on any `quiescent=True`. |
| N2 orphan side checkouts | CONFIRMED | `checkout_in_use` also reserves every checkout of a claim ever held FOR an unresolved orphan (its revoked side claims included) until settlement | `test_n2_…` (exclusive side claim on another checkout) | revocation freed them. |
| N3 worker's own checkout | CONFIRMED | `same_executor=<subagent>`: the worker's registry row and the claims held for it are one executor — `claim_node(subagent=W)` at W's workspace and `register_subagent(W)` at a workspace already claimed for W both pass; distinct executors still refuse; registration ownership is validated first | `test_n3_…` (both orders + distinct executors) | collision check saw two writers. |
| N4 settled-worker evidence | CONFIRMED | `require_registered_subagent(allow_settled=True)` for evidence (`active/adopted/settled`); checkout writes still need an open worker | `test_n4_…` (register → terminal report → attributed evidence recorded; unknown worker refused) | settlement withdrew attribution. |
| N5 admin reporting with enforcement off | CONFIRMED | the admin break-glass path exists only where `enforced(conn, project)`; otherwise P1's `actor_continues` rule applies verbatim | `test_n5_…[False/True]` | `_authorize_report` never checked `enforced`. |
| N6 fence omitted on settlement | CONFIRMED | current-owner settlement needs the current fence (`fence_required`, `stale_fence`); the old-fence exception stays origin-driver orphan closure only | `test_n6_…` | fence checked only when supplied. |

Also noted by the review and left as is: a database that ran the pre-round-1 P3 code could still hold public copies
of subagent contexts in `state_external_report_blobs` (none exists: P3 was never deployed); and the owner
re-claiming the checkout its own active attempt runs in is allowed (`owner=` in `checkout_in_use`), because
that is one writer, not two — the R1 replay tests depend on it.

### Test results (fix round 2)
- `tests/unit/test_state_p3_fix_round2.py` 14 passed; round-1 module 19 passed; P3 module 33 passed (66 total).
- Affected suites (claims, external, attempts, changes, run summary, deployment quiescence, privacy doors,
  private-read verdict, read visibility): 510 passed, 0 failed.
- Full suite on HEAD `8c3d7079`, one container, 29:50: **361 failed, 6460 passed, 13 skipped, 23 errors** —
  failure-ID set IDENTICAL to base `53daff7ff2` (361F; `comm` new = 0, fixed = 0); +14 passed = the round-2
  regression tests. Logs: `/tmp/p3-logs/head_full4.log`, `/tmp/p3-logs/oldcheck2.log`. Migration/rebuild code
  was not changed in this round, so the rehearsal was not repeated.

## Fix round 3 (2026-10-10, after `P3_REVIEW3_CODEX.md`)

Every item was verified against the code of `8c3d7079` first; all 8 are CONFIRMED (none rejected). Fix commit:
`f0fbfb8c` (code, tests, docs); the pending report edits were committed as `daa50f0c`. Regression tests:
`tests/unit/test_state_p3_fix_round3.py` — all 11 FAIL on `8c3d7079` for their own reasons (the module looks the
round-3 API up lazily; run from a detached worktree of that commit in a throwaway container: 11 failed, 0
passed) and pass on `f0fbfb8c`. Three earlier tests that encoded the silent-rebind / free-checkout behaviour
the review calls defects were corrected and are explained below. No other existing assertion was changed.

| item | verdict | fix (commit `f0fbfb8c`) | regression test | evidence / notes |
|---|---|---|---|---|
| 1 same driver, two executors, one checkout | CONFIRMED | `checkout_in_use(reclaiming=(driver, node_key))` exempts only the driver's own active attempt on the SAME node the claim re-associates with; the blanket `owner=` exemption is gone | `test_1_…` (same driver, other node → `workspace_in_use`; same node → allowed) | state_enforcement.py exempted every attempt owned by the requester. |
| 2 (R1) stale authorization at launch | CONFIRMED | `_reserve` replay validates when `claim_id` OR `fence` is named and binds the named live claim to the replayed attempt; `claim_launch(..., claim_id, fence)` carries the caller's named claim/fence into the launch transaction (`start_attempt` and `recover_attempt`, whose contract gains `claim_id`/`fence`); `launch_authorization` refuses a stale named claim/fence (`stale_fence`), launches on a still-bound live claim, and binds a replacement ONLY when the caller names it (`claim_required` names the replacement) — never a silent rebind | `TestItem2StaleAuthorizationAtLaunch` (3: fence-only replay, replacement during preflight, external replay binding → settlement releases C2, contract fields) | replay validated only with `claim_id`; `claim_launch` received no expected values; external replay left C2 unbound. |
| 3 unknown abandonment frees the checkout | CONFIRMED | the occupancy scan also reserves the executor checkout of an attempt abandoned with `abandon_kind=unknown` while its owner row is not `settled` (a verified late quiescence report settles it) | `test_3_…` (other node's claim and worker registration refused; freed after the verified report) | bound claims were released and the attempt left the active set. |
| 4 checkpoint vs transfer race | CONFIRMED | `checkpoint_controller` re-reads the owner under the write lock and OPENS a decision row (`driver_checkpoint_decisions`, 120 s TTL); `take_over_attempt`, `abandon_external_attempt` and `accept_handoff` refuse `checkpoint_in_progress` while one is open; the REST doors (approve/reject bodies extracted) and the MCP tool finish the decision in `finally` after the engine call (`finish_state_checkpoint_decision`) | `test_4_…` (open decision blocks takeover/accept; finish lifts it; former owner cannot reopen; expiry lifts a crashed decider) + `test_4_rest_door_…` | authorization transaction closed before the engine call. New table → rehearsal re-run (below). |
| 5 nonmembers reclaim / accept | CONFIRMED | `require_member` (P0 registry `project_drivers.status=member`) inside the ownership transactions of take-over, abandon and accept; admin passes as `break_glass` (event flag); identity off = no membership to check | `test_5_…` (nonmember, member removed after the offer, admin break glass; real registry) | eligibility never consulted membership. |
| 6 inherited identities | CONFIRMED | `claim_node(subagent=<origin>/<label>)` accepted when the registry says the caller currently owns it (judged in the write transaction); `execute` consults the registry before refusing a foreign-prefix `director_identity`; `require_registered_subagent` always judges a foreign prefix by current ownership | `test_6_…` (adoption → new claim; adoption → terminal report → attributed evidence; origin driver refused) | prefix checks refused `codex/w1` for `grok`. |
| 7 settled workers keep side claims | CONFIRMED | `close_worker_claims` releases every live claim held FOR a worker when it is settled (terminal report, owner settlement, orphan closure, confirmed abandon); renewal → `claim_not_live`, dispatch → `stale_fence`, checkout reusable | `test_7_…` (pre-existing side claim released; heartbeat/dispatch refused; takeover moves side claims, orphaning revokes them) | only attempt-bound claims were released. |
| 8 (minor, R16) hook without sender | CONFIRMED | `deliver(conn, message, notice)` receives the complete stored notice (sender, project, refs, delivery_mode) next to the P2 keyword set | `test_8_…` | the hook got only the keyword set. **Deferred:** a real P2 adapter (insertion into `driver_inbox_messages`/`_deliveries`, system resolution before acknowledgement, rollback) cannot be written or tested here without merging the P2 branch (`12fbef9a`, inspected read-only, not pulled): its schema, idempotency table and lifecycle live only there. The seam is complete — `set_inbox_adapter(deliver, resolve)`, both connection-sharing, the full notice and the normalized keywords on delivery, the full notice rows with `refs` (handoff_id/subagent_id correlation) on resolution — and the adapter is a ~40-line function on the P2 side: insert message + deliveries (seq per target) inside the given connection, record `(actor='system', 'send', notice_id)` idempotency, and mark deliveries `resolved` by correlation. |

Corrected earlier tests (each change encoded a defect this round fixes): round-2 `TestR1StaleFenceReplay.test_launch_requires_a_live_claim_and_binds_the_current_one` and round-1 `TestFinding1LaunchAuthorization.test_owner_with_live_claim_launches` asserted a SILENT rebind at launch — they now assert `claim_required` without a named claim and success when the replacement is named; P3-module `test_abandon_unknown_binds_the_next_attempt…` asserted that a claim at the abandoned worker's checkout succeeds — it now asserts `workspace_in_use` (item 3). The round-2 adapter test's lambda takes the third `notice` argument (item 8). The transport fixture registers `grok`/`codex` as project members (item 5).

Also found while running the suites: `tests/unit/test_mcp_router.py::test_every_run_taking_tool_actually_resolves_a_project_id` fails on base too (`_WaitSF` lacks `get_steps`); it is in the base failure set, not new.

### Migration rehearsal (round 3, new table `driver_checkpoint_decisions`)
Fresh single-snapshot copy of production (17,180,782,592 bytes, 89 s) at 05:29 UTC, rehearsed on the host with
`AITELIER_HOME` redirected, copy deleted afterwards (`/tmp/p3-rehearsal` gone; disk back to 48 GB free):
first run 0.93 s; every pre-existing table's row count unchanged (2731 attempts now; `state_attempts` seq
high-water 2759 → 2759; attempt digest equal); `lost_schema_objects: []`; new objects = the P3 tables incl.
`driver_checkpoint_decisions` (+ index) and `state_subagent_contexts`; `foreign_key_check` = [];
`integrity_check` = ok; second run byte-identical. JSON: `/tmp/p3-logs/report_round3.json`.

### Test results (fix round 3)
- `tests/unit/test_state_p3_fix_round3.py` 11 passed; round-2 14, round-1 19, P3 module 33 (77 total).
- Affected suites (claims, external, attempts, changes, run summary, deployment quiescence, privacy doors,
  private-read verdict, read visibility, MCP router, checkpoint reject target, write-opening mutation gate):
  700 passed, 1 failed — the pre-existing base failure named above.
- Full suite on HEAD `f0fbfb8c`, one container, 30:04: **361 failed, 6471 passed, 13 skipped, 23 errors** —
  failure-ID set IDENTICAL to base `53daff7ff2` (361F; `comm` new = 0, fixed = 0); +11 passed = the round-3
  regression tests. Logs: `/tmp/p3-logs/head_full5.log`, `/tmp/p3-logs/oldcheck3.log`.

## Fix round 4 (2026-10-10, after `P3_REVIEW4_CODEX.md`)

Every item was verified against the code of `0fa1175f` first; all 5 are CONFIRMED (none rejected). Fix commit:
`2bf514b8f` (code, tests, docs). Regression tests: `tests/unit/test_state_p3_fix_round4.py` — all 7 FAIL on
`0fa1175f` (module copied into a detached worktree of that commit, run in a throwaway container: 7 failed,
0 passed) and pass on `2bf514b8f`. No existing assertion was changed in this round. No migration or rebuild
code changed, so the rehearsal was not repeated.

| item | verdict | fix (commit `2bf514b8f`) | regression test | evidence / notes |
|---|---|---|---|---|
| 1 REST reject `NameError`, approve loses `_label` | CONFIRMED | `_approve_checkpoint_body(..., _step_id, _label, db, controller)` and `_reject_checkpoint_body(..., step_id, _label, _graph, db, controller)` receive graph and label from their routes | `TestItem1RestCheckpointRoutes` (3): the ACTUAL reject route through `TestClient` reaches `sf.reject_checkpoint("run-1","gather",feedback)` with the graph resolved for the target; the approve body under a running loop pushes the `checkpoint_resolved` payload with its label to the project and `__global__` channels; the routes open and FINISH the State decision on success, on an engine refusal (completed run → 400) and refuse a non-owner at the door without opening one | `_graph`/`_label` were locals of the callers (api/meta_routers.py:1018, 945, 1042); the round-3 "REST door" test only called the helpers. |
| 2 same-node exemption admits other executors / unknown-abandoned | CONFIRMED | `checkout_in_use(reclaiming=…)` exempts only an ACTIVE attempt of the driver on that node whose dispatching claim carried the same `subagent` (or none) as the new claim; an attempt abandoned with quiescence unknown is never exempt | `test_2_…` (same node + different registered worker → `workspace_in_use`; same executor → allowed; original owner on an unknown-abandoned node → refused until the verified quiescence report) | the skip keyed on (owner, node) only and also matched abandoned rows. |
| 3 decision expiry does not fence a delayed handler (minor) | CONFIRMED | cheapest sound fence: `assert_decision_open(db, decision_id)` immediately before the engine call in the REST bodies and the MCP tool (`assert_state_checkpoint_decision_open` → 409 / MCP error): the decision must be unfinished, unexpired, and the attempt's owner/fence unchanged since it opened (`checkpoint_decision_expired`). What remains is the gap between that read and the engine call: expected to be small, but NOT bounded by State (a suspended thread or engine lock contention can outlast the decision's remaining lifetime); a truly atomic State+engine mutation is not possible across two SQLite connections (BEGIN IMMEDIATE on State would block the engine's own write), so this is a stated limitation, not atomic safety | `test_3_…` (delayed past expiry + takeover → refused; finished and unknown decisions refused; REST mapping 409) | the open decision only held transfers off until expiry; a surviving handler then mutated the run. |
| 4 claim-only admin handoff loses `break_glass` (minor) | CONFIRMED | `claim_transferred` event and `accept_handoff` result carry `break_glass` for claim subjects too | `test_4_…` (non-member admin accepts a claim offer → `break_glass=True` in result and event; member → `False`) | only the attempt path recorded it (state_handoffs.py:350). |
| 5 adapter doc signature (nit) | CONFIRMED | seam comment documents `deliver(conn, message, notice)` and the complete notice's sender / correlation metadata | `test_5_…` | comment still described the two-argument hook. |

### Test results (fix round 4)
- `tests/unit/test_state_p3_fix_round4.py` 7 passed; rounds 3/2/1 and the P3 module 77 passed;
  `tests/integration/test_meta_routers.py` 14 passed (98 in one run).
- Affected suites (claims, external, attempts, changes, run summary, deployment quiescence, privacy doors,
  private-read verdict, read visibility, MCP router, checkpoint reject target, run routers, write-opening
  mutation gate): 709 passed, 1 failed — the pre-existing base failure
  `test_mcp_router.py::test_every_run_taking_tool_actually_resolves_a_project_id`.
- Full suite on HEAD `2bf514b8f`, one container, 30:16: **361 failed, 6478 passed, 13 skipped, 23 errors** —
  failure-ID set IDENTICAL to base `53daff7ff2` (361F; `comm` new = 0, fixed = 0); +7 passed = the round-4
  regression tests. Logs: `/tmp/p3-logs/head_full6.log`, `/tmp/p3-logs/oldcheck4.log`.

## Fix round 5 (2026-10-10, after `P3_REVIEW5_CODEX.md` — APPROVE WITH NITS; owner-requested fixes)

The review file was dropped at the worktree root as `P3_REVIEW5_CODEX.md` (left untracked). Every item was
verified against the code of `e46ba98e` first; all 3 are CONFIRMED. Fix commit: `5c1eb7641` (code, tests, docs,
report wording). Regression tests: `tests/unit/test_state_p3_fix_round5.py` — all 3 FAIL on `e46ba98e` (module
copied into a detached worktree of that commit, run in a throwaway container: 3 failed, 0 passed) and pass on
`5c1eb7641`. They drive the ACTUAL doors: the REST approve route and its run-scoped delegate through
`TestClient`, and the MCP `answer_checkpoint` tool callable. No existing assertion was changed. No migration
or rebuild code changed, so the rehearsal was not repeated.

| item | verdict | fix (commit `5c1eb7641`) | regression test | evidence / notes |
|---|---|---|---|---|
| 1 fence misses the failed-run reactivate/resume path | CONFIRMED | `_approve_checkpoint_body` calls `assert_state_checkpoint_decision_open` before `sf.reactivate_run` AND again before `sf.resume_run` (the budget restore between them can be slow); MCP has no failed-run path (it answers paused runs only) | `TestItem1FailedRunRescueIsFenced` (2): a handler whose engine read outlasts the decision (`sf.get_run` advances the clock 121 s) gets 409 `checkpoint_decision_expired` and neither `reactivate_run` nor `resume_run` is called, the decision is still finished; undelayed, both mutations run; the run-scoped `/api/runs/{id}/checkpoint/approve` delegate is fenced the same way | api/meta_routers.py failed branch (`:916`) mutated twice with no check. |
| 2 MCP reject checks the fence before resolving the target | CONFIRMED | `checkpoint_reject_target(...)` is resolved first, then the fence, then `sf.reject_checkpoint(..., redirect_to=…)` | `TestItem2McpRejectResolvesTargetBeforeTheFence`: recorded call order is `["target", "fence"]` and the engine receives the resolved redirect; when the target resolution itself outlasts the decision, the tool answers `checkpoint_decision_expired` and never calls `sf.reject_checkpoint` | api/mcp_router.py:1401 asserted the fence before `checkpoint_reject_target`. The REST reject body already resolved the target first (round 4). |
| 3 "milliseconds" claimed a bound (nit) | CONFIRMED | wording in `core/state_enforcement.assert_decision_open`, `docs/driver-claims.md` and the round-4 report row now says the check-to-mutation gap is expected to be small but NOT bounded by State (thread suspension, engine lock contention); a hard guarantee would need a fence inside the engine transaction or a lock shared with ownership transfers — a stated limitation, not atomic safety | — (documentation) | — |

### Test results (fix round 5)
- `tests/unit/test_state_p3_fix_round5.py` 3 passed; with round 4, `test_meta_routers.py`, `test_mcp_router.py`
  and `test_checkpoint_reject_target.py`: 110 passed, 1 failed — the pre-existing base failure
  `test_mcp_router.py::test_every_run_taking_tool_actually_resolves_a_project_id`.
- Related suites (run routers, claims, external, attempts, rounds 1–3, P3 module, privacy doors): 314 passed.
- Full suite on HEAD `5c1eb7641`, one container, 30:14: **361 failed, 6481 passed, 13 skipped, 23 errors** —
  failure-ID set IDENTICAL to base `53daff7ff2` (361F; `comm` new = 0, fixed = 0); +3 passed = the round-5
  regression tests. Logs: `/tmp/p3-logs/head_full7.log`, `/tmp/p3-logs/oldcheck5.log`.

## Rebase onto main `5b8e374d7` (2026-10-10, branch `grok/multi-driver-p3-rebased`)

Owner-approved preparation, reversible: `grok/multi-driver-p3` (HEAD `9512e51bc`, base `53daff7ff2`) is
untouched; this branch is its 14 commits replayed onto `origin/main` `5b8e374d7` (38 new commits: P1
claims already in the base, plus P2 — driver inbox `core/driver_inbox.py`, private driver notebooks,
project-inbox `at_least_n`/`broadcast` acks — and the Godot/playtest/native-starvation batches). Nothing
merged, pushed or deployed. Worktree `/home/linxuhao/AItelier-worktrees/multi-driver-p3-rebased`.

### Conflicts and resolutions (`git rebase origin/main`, 14 commits, 2 stopped)

| commit | file | conflict | resolution |
|---|---|---|---|
| `29a981e9` → `8b815fc0` (P3 core modules) | `core/state_commands.py` | P2 inserted its request models (`SendDriverMessage`, `ListDriverMessages`, `WaitForDriverInbox`, `DriverDeliveryTransition`) at the same spot where P3 inserted its 16 models (`SetClaimEnforcement` … `ListDriverNotices`) | both blocks kept, P2 first then P3; the `WRITE_REQUESTS`/`READ_REQUESTS`/`_handlers` tables auto-merged (an AST scan found no duplicate action key) |
| same | `core/state_service.py` | P2 constructs `self.driver_inbox = DriverInbox(...)` where P3 constructs `recovery`/`subagents`/`handoffs`/`notices`/`guard` | both kept: inbox first, then the five P3 components. P3's move of `self.is_admin` above `ExternalAttempts(... is_admin=)` auto-merged |
| `f0fbfb8c` → `57acaea6` (fix round 3) | `core/state_commands.py` | (a) P2 added `_director_schema(service)` (v2/v3 reply schema) at the same place P3 added `_owns_inherited_subagent`; (b) in `execute`, P2 changed the `DirectorMessageError("invalid_request")` after a `check_director_identity` failure to carry `_director_schema(service)` and added the `driver_id`-on-untrusted `ProjectPrivate` check; P3 wrapped the same failure in `if not _owns_inherited_subagent(...)` (an inherited worker keeps its origin prefix) | (a) both helpers kept; (b) P3's wrapper kept with P2's schema-carrying error inside it, P2's `ProjectPrivate` check kept after it |

The other 12 commits applied cleanly (auto-merges in `core/state_changes.py` — P3's `abandoned` in the
`wait_disposition` terminal set next to P2's inbox options —, `core/state_privacy.py`,
`core/state_driver_guide.py`, `docs/state-agent-driver.md`, `design/multi-driver-coop.md`,
`tests/support/state_canaries.py`, `tests/unit/test_write_opening_coverage.py`,
`tests/unit/test_private_read_verdict_at_execution.py`). No P2 or P3 test was dropped: the P2 files
(`test_driver_p2.py`, `test_driver_p2_scope_boundaries.py`, `test_private_director_ack_tables.py`, …) and
the six P3 files (`test_state_p3_enforced_claims.py`, `test_state_p3_fix_round1..5.py`) are all present.

Two P2 test pins were semantically (not textually) in conflict with P3 and are reconciled in one
commit on top of the replay (`0cdce82e5`):
- `tests/unit/test_private_director_ack_tables.py` asserted that the private-table set grew by EXACTLY the
  two ack tables since main `f367c46f`; P3 classifies `state_handoffs` and `state_subagent_contexts` private
  and `state_project_enforcement` public. The test now names those three as P3's additions and still asserts
  that nothing was removed from either set and no private table is public.
- `tests/unit/test_private_read_verdict_at_execution.py`: P2 made the trusted control service a registered
  driver (`execpoint`), so P3's argument `list_driver_notices(driver_id="seeder-driver")` was refused with
  `not_notice_target` for the trusted caller. The argument now names `execpoint` (a driver reads its own
  notices); the anonymous path is still refused by `writer_only_read` as before.

Semantic checks after the replay: `python -m compileall` clean; both P3 doors still derive from the action
tables (`test_write_opening_coverage`, `test_private_read_verdict_at_execution` pass); P2's inbox `refs`
validation names `driver_subagents` (`subagent_id`) and `state_node_claims` (`claim_id`), both of which P3
provides, so an inbox message may now reference a real claim or subagent.

### Inbox wiring decision: wired (commit `a42272c02`)

The P3 notifier (`core/driver_notices.py`) already had the connection-sharing seam
`set_inbox_adapter(deliver=, resolve=)` plus `inbox_message()` (the P2 keyword normalization). With P2 on
main the adapter is small and testable through the real API, so it is wired as the DEFAULT:
- `core/driver_inbox.py:deliver_notice(conn, message, notice)` writes the rows `send_driver_message` writes
  after validation — one `driver_inbox_messages` row (thread = message, `sender_driver_id` from the notice,
  NULL for system notices), one `driver_inbox_deliveries` row (`seq` = target's max+1, `unread`, version 1)
  — on the CALLER's connection, inside the ownership transaction; a rolled-back write leaves no message.
  The idempotency row `driver_inbox_requests(actor='system', operation='send', request_key=notice_id)` is
  also the correlation notice → message (no new column anywhere).
- `core/driver_inbox.py:resolve_notices(conn, notices, reason)` marks the notice's deliveries `resolved`
  (version +1, from `unread` or `acknowledged`) and records `('system','resolved',notice_id)` with the
  reason, so a settled offer / orphan stops being an open standing item (`active_standing`) without the
  target's acknowledgement.
- `driver_notices._inbox_hook` returns the installed override or the real inbox; `set_inbox_adapter()`
  with no arguments restores the default (the round-2/3 tests' doubles still work and their autouse reset
  now means "real inbox").
- Kinds/refs follow `inbox_message`: `override_notice`/`break_glass` → `lease_notice` with a `[kind]` body
  prefix (P2's `kind` CHECK has no code for them), refs reduced to `attempt_id|claim_id|subagent_id|node_key`
  (the handoff id survives on the `driver_notices` row and in the correlation, which is how resolution finds
  the delivery).
- The inbox is keyed to the P0 registry (`driver_inbox_deliveries.target_driver_id REFERENCES drivers`,
  `PRAGMA foreign_keys=ON` on every store transaction). A notice addressed to a driver id that is NOT
  registered — every P3 unit fixture that skips the registry, and a legacy owner id — keeps the
  `driver_notices` row and the `driver_notice` event and returns `inbox: {"skipped": "unregistered_driver"}`
  in the write's `notified` entry; a missing inbox schema returns `{"skipped": "inbox_unavailable"}`. Returned,
  never swallowed.
- Not changed: the `driver_notices` table and `list_driver_notices` stay (they carry the full refs, the
  resolution reason and the project scope the inbox message does not); `wait_for_state_change` keeps waking
  on the `driver_notice` event. Docs: `docs/driver-claims.md` "Notices", driver guide ("Claims and leases"),
  the module header; the `delivery` string of `list_driver_notices` names the inbox reads.

Regression test `tests/unit/test_state_p3_inbox_wiring.py` (4 tests, all through `execute`):
`offer_handoff` → the receiver's `list_driver_messages`/`wait_for_driver_inbox` show one `handoff_offer`
from `codex` with the attempt/node refs, the sender's inbox is empty, the receiver acknowledges it through
`acknowledge_driver_message` (version 2), `accept_handoff` resolves notice AND delivery (version 3,
`active_standing` 0); a takeover's `takeover_notice` and an admin's `break_glass` arrive with no sender
(`lease_notice` + prefix); unregistered target → skip + row; a write that fails after delivery leaves no
inbox or notice row.

### Test results
Throwaway containers only (`docker run --rm --network none --cpus 2 -m 2g --user 1000:1000 -v <tree>:/src:ro
-v ~/AItelier/.git:~/AItelier/.git:ro -w /src aitelier:latest python -m pytest -p no:cacheprovider -q -rfE`,
two at once, never the production containers). The `.git` read-only mount is NEW this round: a worktree's
`.git` is a pointer into the main checkout, and without it the tests that shell out to `git show`
(P2's migration test, the ack-table pin, the banned-phrase scan) fail as "not a git repository".

| run | tree | result | log |
|---|---|---|---|
| baseline | `origin/main` `5b8e374d7` (detached worktree, since removed) | **284 failed, 7117 passed, 12 skipped, 2 errors** (35:07) | `/tmp/p3rebase/base_full.log`, IDs `base_fail.txt` (286) |
| rebased HEAD | `a42272c02` (replay + reconciliation + inbox wiring) | **286 failed, 7241 passed, 12 skipped, 2 errors** (43:50) | `/tmp/p3rebase/head_full.log`, IDs `head_fail.txt` (288) |

Failure-ID diff (`comm`): fixed = 0; **new = 2**, both
`tests/integration/test_no_unfalsifiable_guarantees.py::test_a_round_file_carries_no_lie_keeping_phrase[...]`
for `P3_REPORT.md` and `core/state_enforcement.py`. That scan bans five phrases in every file changed since
`9c79f11f`. The earlier P3 rounds never exercised it: their containers had no git, so the scan's own scope
test failed identically on base and head (`head_full7.log` shows it; zero per-file cases ran) and the sets
compared equal. With git mounted the scan runs on both sides; main's own six hits (`api/config_routers.py`,
`api/project_routers.py`, `api/run_routers.py`, `core/ai_router.py`, `tests/unit/test_ai_router.py`,
`tests/unit/test_godot_retention_corrections.py`) are in the baseline set, and the two P3 hits were one word
in `assert_decision_open`'s docstring and four places in this report. Reworded in `97ec44586` (prose only, no
behaviour); targeted rerun on that commit (`/tmp/p3rebase/targeted3.log`): the scan + `test_state_p3_fix_round4/5`
+ `test_state_p3_enforced_claims` + the wiring test = **420 passed, 6 failed**, the 6 being exactly main's
six baseline hits. So on `97ec44586` the failure set is the baseline set: new = 0.

+124 passed on HEAD = the 87 P3 tests (33 + 19 + 14 + 11 + 7 + 3 in the six P3 files, `pytest --collect-only`),
the 4 wiring tests, and 33 new parametrizations of the derived suites (exhaustive doors, mutation gate,
argument table, P3 files in the banned-phrase scan).

P3 tests explicitly, after the rebase (`/tmp/p3rebase/targeted1.log`, `targeted2.log`, `targeted3.log`):
`test_state_p3_enforced_claims.py`, `test_state_p3_fix_round1..5.py`, `test_state_p3_inbox_wiring.py`, plus
P2's `test_driver_p2.py`, `test_driver_p2_scope_boundaries.py`, `test_private_director_ack_tables.py`,
`test_private_read_verdict_at_execution.py`, `test_write_opening_coverage.py`, `test_state_claims.py`,
`test_state_driver_guide.py`, `test_postcompact_driver_hook.py` — all pass (335 + 29 + 420 across the three
runs; the only failures ever seen were the two pins above before their reconciliation and two of my own
wiring tests before their fix, both recorded in `smoke2.log` / `targeted1.log`).

### Commits on this branch beyond the replay
- `0cdce82e5` Rebase onto main 5b8e374d7: reconcile two P2 test pins with P3
- `a42272c02` P3 notices delivered to the P2 driver inbox (default adapter)
- `97ec44586` Reword a banned phrase in state_enforcement and P3_REPORT
- (this section)

### Still open after the rebase
- The stale lines at the top of this report ("deliberately uncommitted", "HEAD = `200b73c3`") describe the
  first round; the branch history is the record.
- Known gap 11 above ("P2 inbox not on main") is closed by the wiring; gaps 1–10 stand.
- `wait_for_state_change` does not wake on an inbox-only change for a notice without `project_id` (none of
  P3's notices today); the inbox wait re-polls every second regardless.
- Not done here, as instructed: no merge, no push, no deploy, `grok/multi-driver-p3` untouched.

## Rebase onto main `7050c1830` (2026-10-10, second re-rebase, owner-approved)

`origin/main` moved from `5b8e374d7` to `7050c1830` (four commits, all in `core/deployment_quiescence.py`
and `tests/unit/test_deployment_quiescence_process_identity.py`: verified native executable identity for
render-owner classification, separated from argument data). `git rebase origin/main` replayed all 18 P3
commits (`8b815fc05` … `0d80eb364`) with **zero conflicts**: P3's quiescence change is additive — the
`abandoned` owner status in `EXTERNAL_OWNER_STATUSES`, the `abandoned` match in `external_owners`, and the
`state_attempts.abandon_kind` LEFT JOIN in `_db_rows` that keeps an `abandon_kind=unknown` owner as a durable
blocker while dropping a `confirmed_stopped` one — and none of it touches the native-identity code main
changed. Both behaviours are present on the new HEAD unchanged (`git diff origin/main HEAD --
core/deployment_quiescence.py` is exactly the three P3 hunks; `py_compile` clean).

### Test results
Throwaway containers only (`docker run --rm --network none --cpus 2 -m 2g --user 1000:1000 -v <tree>:/src:ro
-v ~/AItelier/.git:~/AItelier/.git:ro -w /src aitelier:latest python -m pytest -p no:cacheprovider -q -rfE
tests/`, two at once, never `aitelier` / `aitelier-godot` / `aitelier-zg`). Engine in the image: skillflow-py
1.5.89 (= the `pyproject.toml` pin). Logs under `/tmp/p3rebase2/`.

| run | tree | result | log |
|---|---|---|---|
| baseline | `origin/main` `7050c1830` (detached worktree, since removed) | **284 failed, 7132 passed, 12 skipped, 2 errors** (35:54) | `base_full.log`, IDs `base_fail.txt` (286) |
| rebased HEAD | `0d80eb364` | **284 failed, 7258 passed, 12 skipped, 2 errors** (40:22) | `head_full.log`, IDs `head_fail.txt` (286) |

Failure-ID diff (`comm`): **new = 0, fixed = 0** — the two sets are identical, so HEAD's failure set is
(trivially) a subset of the baseline's. +126 passed on HEAD = the P3 tests plus their parametrizations in the
derived suites.

Explicit run on HEAD (`targeted.log`, 1:16): `tests/unit/test_state_p3_enforced_claims.py`,
`test_state_p3_fix_round1..5.py`, `test_state_p3_inbox_wiring.py`, `tests/unit/test_deployment_quiescence.py`,
`test_deployment_quiescence_observation_tool.py`, `test_deployment_quiescence_process_identity.py`,
`tests/integration/test_deployment_quiescence.py` = **375 passed, 1 failed**. The one failure is
`tests/integration/test_deployment_quiescence.py::test_real_skillflow_cross_project_measurement_then_quiescence`,
which is **pre-existing on main**: it is in this round's baseline set, in the previous round's baseline set
(`/tmp/p3rebase/base_fail.txt`, base `5b8e374d7`), and fails identically (`assert set() == {run_a, run_b}`).
Cause: main's `d139d30ca` ("Deployment gate measures work, not its own shell") marks a blocking-status run with
zero owned operations as `resumable`, and `_normalized_owner_blockers` excludes resumable runs from
`active_runs`; the integration test still expects two freshly started, never-claimed runs to appear there. Not
a P3 effect (P3 touches only the external-owner registry rows) and not fixed here — it is a main-side test/
producer disagreement outside this task's scope. Every other P3 and quiescence test passes.

### Commits on this branch beyond the previous section
- (this section)

Not done here, as instructed: no merge, no push, no deploy, no production container touched.
