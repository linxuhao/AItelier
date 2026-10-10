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
    fired; added `_ARGUMENT_SEEDS` entries (the file's documented mechanism).
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
