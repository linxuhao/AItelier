# Driver identity (multi-driver P0)

Design: `design/multi-driver-coop.md` §3 (D9, D11, Q1, Q4, Q5). This page is the
operator/driver guide for phase P0 only: identity, tokens and attribution.
Leases, inbox routing and enforced claims are later phases.

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
user can read every file of that user. The convention that keeps it honest:

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
