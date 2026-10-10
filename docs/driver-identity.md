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

## Compatibility

External attempts started before P0 are owned by `authorized-state-operator`.
After enabling, the same operator now reports as `driver:<id>`; reporting on
such a legacy attempt is still accepted from any registered driver, and an
`<email>` owner continues as `owner:<email>` (`core.drivers.actor_continues`).
New attempts are owned by the exact `driver:<id>`.

Audit payloads contain only operation-defined booleans/enumerations and a durable membership row reference. Registration labels and caller-provided reason text are not copied into the append-only audit. Public methods still validate reasons; storing arbitrary prose would admit credential values. Unknown/nested payload fields and invalid or known credential/hash values are refused before insertion.
