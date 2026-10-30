# Driver identity (multi-driver P0)

Design: `design/multi-driver-coop.md` §3 (D9, D11, Q1, Q4, Q5). This page is the
operator/driver guide for phase P0 only: identity, tokens and attribution.
Claims, leases and heartbeats (P1, record-only) are in `docs/driver-claims.md`;
inbox routing and enforced claims are later phases.

## Status

Enabled in production on 2026-10-09 (`AITELIER_DRIVER_IDENTITY=on`). Registered:
`owner-cli` (legacy admin token, is_admin), `public` (Cloudflare external token),
`grok` (Grok Bot), `codex` (Codex on the owner's MacBook Air; reaches the server over SSH).

## New driver? Start here (self-service)

On linxuhaserver, as the shared `linxuhao` account, from `~/AItelier`:

1. Pick a stable id (`[a-z0-9][a-z0-9_.-]*`, not `owner-cli`/`public`).
2. If `~/.aitelier-drivers/<id>.token` does not exist, run once:
   `python3 scripts/driver_token.py self-register <id> --display-name "<name>"`
   - creates `driver:<id>` (never admin) and writes the token to the 0600 file; the
     token is never printed;
   - idempotent: if the file already authenticates as `<id>` nothing changes;
   - refuses (exit 3, nothing changed) when `<id>` already exists but your file is
     missing or stale; only `--rotate` issues a new token, and the old one stops working;
   - refuses when identity is disabled on the server.
3. Run your driver process with `AITELIER_DRIVER_ID=<id>` (clients such as
   `scripts/mcp_call.py`, `cli/client.py` and the codex hook pick up the file).
4. Verify: `GET /api/drivers/me` (or MCP `driver_whoami`) must answer `driver:<id>`.
   Quick check without printing the token:
   `AITELIER_DRIVER_ID=<id> python3 -c "import json,urllib.request;from core.driver_credentials import auth_headers;print(json.load(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:4444/api/drivers/me',headers=auth_headers()))))"`
5. Ask the owner (or an admin driver) to add you to projects if you need a
   membership (`PUT /api/drivers/<id>/projects/<project_id>`); P0 does not
   enforce membership for writes.

Conventions: read only your own token file; the `.env` admin token is read only by
`self-register` itself, never by you; write only through the API, never the
database; don't touch other drivers' worktrees or processes.

Writing novels as a driver? Current state comes in the chapter context pack;
look up history with MCP `novel_ledger_query` or `GET /api/projects/<id>/novel/ledger`
(your driver token, private read) - see `docs/novel-ledger.md`.

## What changes

| Caller | Credential | Recorded actor (feature on) |
|---|---|---|
| Registered LAN driver (codex, grok-bot, ...) | its own token, `X-AItelier-Driver-Token` (or `Authorization: Bearer` on the State-only service) | `driver:<id>` |
| Owner CLI / scripts (unchanged config) | legacy `AITELIER_ADMIN_TOKEN` in `X-AItelier-Admin-Token` | `driver:owner-cli` (is_admin) |
| Owner in the browser via Cloudflare Access | allowlisted email (`AITELIER_WRITERS`) | `owner:<email>` |
| Remote agent through the tunnel | `X-AItelier-MCP-External-Token` | `driver:public` |
| Anything else | - | as before (`authorized-state-operator`) |

Identity is derived from the raw credential only, never from tool arguments.
A registered driver's `director_identity` (director messaging) must be its id
or `<id>/<label>`; a mismatch is refused (`invalid_request`).

Feature off (the default) keeps every verdict and every recorded actor
byte-identical to the pre-P0 behavior and creates no tables.

## Enabling (owner, one time)

Safe order - each step is a no-op for clients until the next:

1. Create the pepper secret (>= 32 chars) on the host:
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))" > ~/.aitelier-secrets/AITELIER_DRIVER_TOKEN_PEPPER; chmod 600 ~/.aitelier-secrets/AITELIER_DRIVER_TOKEN_PEPPER`
   (mounted at `/run/aitelier-secrets/` like every other secret; never an env var).
2. Set `AITELIER_DRIVER_IDENTITY=on` in `~/AItelier/.env` and redeploy. On first
   use the server creates `drivers`, `project_drivers`, `driver_audit` and seeds
   `public` plus `owner-cli` from the existing admin token, so the owner CLI,
   `scripts/mcp_call.py`, the codex hook and dsh keep working untouched.
3. Register each LAN driver: `python3 scripts/driver_token.py register codex --display-name "Codex driver"`.
4. Point the driver at its file: run it with `AITELIER_DRIVER_ID=codex`.
5. Check: `GET /api/drivers/me` (or the MCP tool `driver_whoami`) answers
   `driver:codex`.

Rollback: unset `AITELIER_DRIVER_IDENTITY` (or remove the pepper) and redeploy.
The tables stay (append-only audit, nothing deleted) and are simply unused.
Rotating the pepper invalidates every driver token except `owner-cli`, which is
re-derived from the env admin token at startup.

## Token files (LAN, same Linux account)

All LAN drivers run as `linxuhao` on linxuhaserver (D11) and reach the server
by SSH to linxuhaserver and `http://127.0.0.1:4444` (Q4; no `tailscale serve`).
Isolation between drivers is therefore **cooperative**: any process of that
user can read every file of that user. Two supported ways in, both ending at
the loopback-only port (nothing is re-bound or exposed):

    ssh -N -L 4444:127.0.0.1:4444 linxuhao@linxuhaserver   # tunnel, then http://127.0.0.1:4444
    ssh linxuhao@linxuhaserver 'python3 ~/AItelier/scripts/mcp_call.py ...'   # remote exec

Never put a token on an ssh command line or in a log; the client reads it
from the token file on the server. The convention that keeps it honest:

    ~/.aitelier-drivers/            0700
    ~/.aitelier-drivers/<id>.token  0600, one line, written by scripts/driver_token.py

Clients resolve (core/driver_credentials.py): `AITELIER_DRIVER_TOKEN`, then
`AITELIER_DRIVER_TOKEN_FILE`, then `~/.aitelier-drivers/$AITELIER_DRIVER_ID.token`,
then the legacy `AITELIER_ADMIN_TOKEN`. A token file readable by group/other is
refused. A driver must only read its own file.

Accepted risks (owner decision D11): a misbehaving driver on the same account
can read another driver's file and impersonate it; attribution is honest-actor
attribution, not proof. Mitigations: per-driver files, audit log
(`GET /api/drivers/<id>/audit`, admin), rotation
(`scripts/driver_token.py rotate <id>`), suspension (`POST /api/drivers/<id>/status`).

## API (`/api/drivers`)

- `GET /me` - whoami (reader credential). Works with the feature off (`enabled:false`).
- `GET /`, `GET /{id}`, `GET /projects/{project_id}` - reader.
- Admin (off-tunnel `is_admin` driver such as `owner-cli`, or the owner's email):
  `POST /` register (token returned once), `POST /{id}/rotate`,
  `POST /{id}/status` (`active|suspended|retired`; `public` cannot be retired),
  `POST /{id}/admin`, `PUT /{id}/projects/{project_id}` (`member|removed`, CAS on
  revision), `GET /{id}/audit`.
- With the feature off every route except `/me` answers 404.

Tokens are stored only as HMAC-SHA256(pepper, token); the audit never carries a
token or hash. `public` is not a project member by default (Q5).

## Presence: information, plus one authority (claim takeover)

Every driver view carries a derived `presence` object
(`{"status", "last_seen_at", "seconds_since_seen"}`): `GET /api/drivers`,
`GET /api/drivers/{id}`, `GET /api/drivers/me` (under `driver`), MCP
`driver_whoami` (under `driver`; the State-only service's reduced whoami has no driver view), and per member in
`GET /api/drivers/projects/{project_id}`. Nothing is stored; it is computed from
`last_seen_at` when asked.

| status | rule (both bounds inclusive) |
|---|---|
| `online` | seen within `AITELIER_PRESENCE_ONLINE_SECONDS` (default 900 = 15 min) |
| `away` | seen within `AITELIER_PRESENCE_OFFLINE_SECONDS` (default 21600 = 6 h) |
| `offline` | older than that, or never seen |

A non-positive or non-numeric value falls back to its default; if the stale bound
is not greater than the online bound both fall back to the defaults.

`last_seen_at` is refreshed (at most once a minute per driver and process; a failed write on the request path backs off for the same minute, the inbox long-poll retries after 5 s, off the event loop) by every
request that presents the driver's token and by `list_driver_messages`,
`wait_for_driver_inbox` and a `wait_for_state_change` that includes the driver
inbox, so a driver parked on a 15-minute inbox wait stays `online`. A busy database never denies the credential.

Sending: `send_driver_message`, and `send_director_message` when called with
`protocol_version="v3"` (the v2 response schema is frozen and unchanged), return
`recipient_presence` and, when any recipient is not online, `advice`: `recipient <id> is
<status> (last seen <ts>, <N> min ago); the message is queued in its inbox (director
messages: "in the project mailbox"); do not block waiting for a reply`. Shape:

- a single named recipient (`target_driver_id`): one presence object with `driver_id`;
- a fan-out (`project_members`, or any director message, whose recipients are the target
  project's active member drivers except the sender): ALWAYS a list, even with one
  member, not-online entries first, at most 200 entries; `recipient_presence_total`
  gives the full count when the list was cut.

A never-seen recipient reads "never seen"; a stamp that is unreadable or more than 60 s
in the future reads "last seen time unknown" and counts as `offline`, never as activity.
The message is delivered exactly as before. Presence is computed when the message is
first accepted and is not part of the stored idempotent record, so a replay returns the
record unchanged. A project mailbox whose only active member is the sender adds neither
field.

**Schema note:** `contracts/director_messaging/v3/schema.json` gained the optional send
result fields `recipient_presence`, `recipient_presence_total` and `advice` (the schema
stays closed). A client that vendored the earlier v3 `sendSuccess` must update its copy or
it will reject these results; v2 clients are unaffected.

What a sender should do when the recipient is `away` or `offline`: send anyway (it is
queued), then continue with work that does not depend on the answer, and poll later
instead of blocking in `wait_for_driver_inbox`/`wait_for_state_change` on a reply. For
something that cannot wait, ask the owner or route to another member.

### Presence v2: declared offline, waits, effective presence

A driver may **declare itself** offline (never the shared `public` identity: `shared_identity`): MCP tool `declare_driver_presence(status,
reason?, expected_return?)` or `POST /api/drivers/me/presence` with
`{"status": "offline"|"online", "reason"?: <=200 chars, "expected_return"?: ISO-8601}`.
The driver is always the caller (taken from the credential; there is no driver
argument, an extra field is refused). The row lives in
`driver_presence_declarations` (driver_id, status, declared_at, reason,
expected_return; created on first use). Repeating the same declaration changes
nothing; a new reason/expected_return keeps `since`; `online` deletes the row.
Every change appends a `presence` line to `driver_audit` with `{status, cause}`
only (cause: declared, wait, claim_node, heartbeat, start_external_attempt,
report_external_attempt) - never the reason text.

Every presence view (the driver views above; `holder_presence` on claims) is
now the EFFECTIVE presence. It keeps `status`, `last_seen_at` and
`seconds_since_seen`, and adds `basis`, `derived_status`, `declared`,
`declared_status`, `reason`, `since`, `expected_return`, `waiting` and
`last_wait_end`. In order:

| rule | status | basis |
|---|---|---|
| a `wait_for_driver_inbox`, `wait_for_state_change` or `wait_for_run` of the driver is in progress, or ended at most `AITELIER_PRESENCE_WAIT_GRACE_SECONDS` (default 120; bad value -> 120) ago | `online` | `wait` |
| an offline declaration exists (recent `last_seen_at` from reads does not change it) | `offline` | `declared` |
| otherwise the v1 rule above | online/away/offline | `last_seen` |

A wait OVERRIDES a declaration and, when it starts, clears it (audited, cause
`wait`). `claim_node`, `heartbeat`, `start_external_attempt` and
`report_external_attempt` by the driver also clear it and refresh
`last_seen_at`, inside their own write transaction. Ordinary reads do not.

Waits are recorded in the database (`driver_presence_waits`, ONE row per
wait: started, until = start + timeout + 30 s, ended), not in process memory:
the takeover decision below must see a wait served by any process that opens
the same SQLite file (the State-only service, the host CLI, a second worker).
A process that dies mid-wait keeps the driver online no longer than that
wait's own timeout plus the grace. Each wait costs two short writes. The start
is retried with backoff for up to 10 s; if it still cannot commit, the wait
FAILS before waiting with the retryable `wait_not_registered` (it never waits
unregistered). The end is best effort (0.5 s; a miss keeps the driver online
until `until`). Clearing a declaration happens in the SAME transaction as the
claim_node / heartbeat / start / report that proves the driver is back.

Fail closed: `determinable` is false when the driver has no registry row or no
readable `last_seen_at` and has not declared; the takeover authority never
reads such a driver as offline. Claims, refusals and takeover events show
presence WITHOUT the declared reason (only the driver views show it).

### What presence may and may not do

Presence is **information**, with exactly ONE exception:

- MAY: be shown in driver views, `list_claims` / `get_claim`
  (`holder_presence`, and `takeover_hint` on a live claim whose holder is
  offline), `claimed_by_other` refusals, and message `advice`;
- MAY (the one authority): allow another active member of a project to
  `take_over_claim` a LIVE claim, and `take_over_attempt` an active attempt
  before its lease lapses, when the owner is effectively `offline` at the
  moment of the write (`docs/driver-claims.md`);
- MAY NOT: change dispatch, reports, leases or their expiry, fences,
  `abandon_external_attempt` (lease rule and attestation unchanged), holds,
  handoffs, membership, message delivery, or any other permission. `online` and
  `away` owners are never taken over.

The guard is mechanical: `tests/unit/test_driver_presence.py` and
`tests/unit/test_driver_offline_takeover.py` refuse any presence read in
permission, claim, lease and fence code other than four describing hooks
(`annotate_claims`, `holder_note`, `mark_back`, `driver_waiting`), refuse a
hook result inside a condition there, and pin `core/state_takeover.py` as the
only module that decides on `effective_presence` (`take_over_claim` and
`offline_owner_evidence`, the latter reached only from `take_over_attempt`). An `offline` driver keeps its
claims until they lapse or a member takes them over. The owner remains the only
authority to suspend or retire a driver.

Messaging `recipient_presence` / `advice` still uses the v1 derived presence
(unchanged by this step).

## Compatibility

External attempts started before P0 are owned by `authorized-state-operator`.
After enabling, the same operator now reports as `driver:<id>`; reporting on
such a legacy attempt is still accepted from any registered driver, and an
`<email>` owner continues as `owner:<email>` (`core.drivers.actor_continues`).
New attempts are owned by the exact `driver:<id>`.

Audit payloads contain only operation-defined booleans/enumerations and a durable membership row reference. Registration labels and caller-provided reason text are not copied into the append-only audit. Public methods still validate reasons; storing arbitrary prose would admit credential values. Unknown/nested payload fields and invalid or known credential/hash values are refused before insertion.
