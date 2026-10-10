# Multi-driver rollout runbook: `wuxia-myth` (P4, owner only)

Design: `design/multi-driver-coop.md` §7.2, §10 (P4 row). Driver-side behaviour: `docs/state-agent-driver.md`
("Driver loop", "Claims and leases", "Two inboxes"), `docs/driver-claims.md`, `docs/driver-identity.md`.

Turns `multi_driver` and then `claim_enforcement` ON for the State project `wuxia-myth` with members `grok` and
`codex` (owner-approved), optionally turns on advisory review marking, and says how to roll back. Every write
below is a policy CAS: read `project_overview.policy.revision` (R) right before each one.

## 0. Preconditions

1. The P4 commit is reviewed, merged and deployed; the running service lists `set_review_independence` in
   `GET /api/state/schema` `operations`.
2. You act as the admin driver: `GET /api/drivers/me` answers `"actor": "driver:owner-cli", "is_admin": true`.
3. Drivers `grok` and `codex` exist and are `active` (`GET /api/drivers`).
4. Codex was told first, in its driver inbox and in the `wuxia-myth` project inbox, and the non-terminal
   `wuxia-myth` attempts at switch time are listed (`project_overview.nodes[].latest_attempt`). Attempts that
   exist before the switch have no `owner_driver_id` (`lease_state=legacy_unleased`): enforcement's owner and
   fence checks do not apply to them, so they stay reportable as before.

## 1. Enable

1. Memberships (outside State policy): `PUT /api/drivers/{grok,codex}/projects/wuxia-myth` with
   `{"status":"member","expected_revision":<current, 0 if none>,"reason":"..."}`.
2. `set_multi_driver(project_id="wuxia-myth", multi_driver="on", expected_revision=R, reason)` — claims and
   leases are recorded; nothing is refused yet.
3. `set_claim_enforcement(project_id="wuxia-myth", claim_enforcement="on", expected_revision=R+1, reason)` —
   dispatch now needs the driver's live implement claim, reports need the owner fence (design §7.3).
4. Optional: `set_review_independence(project_id="wuxia-myth", review_independence="advisory",
   expected_revision=R+2, reason)`. `verify_node` still succeeds; a receipt whose `kind=review` criterion was
   last attested by the attempt's current or former owner carries `provenance_json.self_reviewed=true` and
   `project_overview.self_reviewed_receipts` counts it. There is no `required` mode.

Check: `project_overview.policy` shows the switches; the PostCompact hook prints
`policy multi_driver=.. claim_enforcement=.. review_independence=.. self_reviewed_receipts=..`.

## 2. Rollback (each step independent; nothing is lost)

- Advisory marking only: `set_review_independence(..., review_independence="off", ...)`. Receipts already
  marked keep their provenance (receipts are immutable); new receipts are byte-identical to pre-P4.
- Enforcement only: `set_claim_enforcement(..., claim_enforcement="off", ...)` — back to record-only.
- All of multi-driver: `set_multi_driver(..., multi_driver="off", ...)` also resets `claim_enforcement` and
  `review_independence` to `off` in the same transaction. Claims and owner/lease columns stay as history.
- Memberships may stay or be set to `{"status":"removed", ...}`.
- Image rollback is safe for P4's schema: the only change is the additive column
  `state_project_enforcement.review_independence DEFAULT 'off'`, which older code never reads or writes.
