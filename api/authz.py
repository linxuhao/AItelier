# api/authz.py
# Single source of truth for write authorization. Used both by the write_gate
# middleware (which gates mutating *methods*) and as a FastAPI dependency
# (require_writer) to lock specific GET endpoints — e.g. the repository
# status/archive reads — to writers, since the method-based gate lets GETs pass.

import hmac
import os

from fastapi import HTTPException, Request

from core import cf_access

WRITERS = {
    e.strip().lower()
    for e in os.getenv("AITELIER_WRITERS", "").split(",")
    if e.strip()
}
ADMIN_TOKEN = os.getenv("AITELIER_ADMIN_TOKEN", "").strip()

# Stable machine-readable denial codes. The SPA maps them to localized text
# (web/src/lib/api.ts:errorMessageKey), so they are part of the API contract.
WRITE_DENIED_NOT_AUTHENTICATED = "write_denied_not_authenticated"
WRITE_DENIED_NOT_A_WRITER = "write_denied_not_a_writer"
WRITE_DENIED_BAD_ADMIN_TOKEN = "write_denied_bad_admin_token"

# Read-side siblings. A read that is still writer-only must be refused with a
# READ message: the write codes above say "to make changes", and answering a
# read request with them told the caller to ask for rights it never wanted.
READ_DENIED_NOT_AUTHENTICATED = "read_denied_not_authenticated"
READ_DENIED_NOT_A_WRITER = "read_denied_not_a_writer"
READ_DENIED_BAD_ADMIN_TOKEN = "read_denied_bad_admin_token"

# English fallback messages, for clients that don't know the codes. Kept
# deliberately generic: never echo the admin token, the writer allowlist or
# any JWT claim back to an unauthorized caller.
DENIAL_MESSAGES = {
    WRITE_DENIED_NOT_AUTHENTICATED:
        "Not signed in — sign in with an authorized account to make changes.",
    WRITE_DENIED_NOT_A_WRITER:
        "Your account has no write permission — this session is read-only.",
    WRITE_DENIED_BAD_ADMIN_TOKEN:
        "The admin token is missing or invalid.",
    READ_DENIED_NOT_AUTHENTICATED:
        "Not signed in — this State DAG record is private; sign in with an "
        "authorized account to read it.",
    READ_DENIED_NOT_A_WRITER:
        "Your account may not read this State DAG record — it is not part of "
        "the published project state.",
    READ_DENIED_BAD_ADMIN_TOKEN:
        "The admin token is missing or invalid.",
}

# The write verdict maps to its read counterpart: WHO may read a private record
# is exactly WHO may write, so there is one identity check, not two.
_READ_DENIAL_CODES = {
    WRITE_DENIED_NOT_AUTHENTICATED: READ_DENIED_NOT_AUTHENTICATED,
    WRITE_DENIED_NOT_A_WRITER: READ_DENIED_NOT_A_WRITER,
    WRITE_DENIED_BAD_ADMIN_TOKEN: READ_DENIED_BAD_ADMIN_TOKEN,
}


def gate_enabled() -> bool:
    """True when Cloudflare Access verification is configured → gate is active."""
    return cf_access.is_configured()


def is_via_cloudflare(request) -> bool:
    """True when the request arrived through Cloudflare in any form — the edge
    tunnel (`Cf-Ray`) or Access (`Cf-Access-Jwt-Assertion`). This keys the admin
    token's anti-replay rule: a leaked token must not be replayable through CF.
    """
    if request is None:
        return False
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    return bool(headers.get("Cf-Ray")
                or headers.get("Cf-Access-Jwt-Assertion"))


def is_via_tunnel(request) -> bool:
    """True when the request came through the Cloudflare TUNNEL (`Cf-Ray`).

    This is what the MCP external token gates — the public tunnel path. It is
    deliberately NARROWER than is_via_cloudflare: an Access JWT without Cf-Ray is
    Access auth, not the public tunnel, and must NOT demand the external token.
    """
    headers = getattr(request, "headers", None)
    return bool(headers and headers.get("Cf-Ray"))


# ── Driver identity (design/multi-driver-coop.md §3, P0) ─────────────────────
# A LAN driver presents its OWN token in `X-AItelier-Driver-Token`; the
# historical `X-AItelier-Admin-Token` header stays an alias so every existing
# caller keeps working. Both are honored only OFF-tunnel (the anti-replay rule
# above is unchanged). With the feature off (`core.drivers.feature_enabled()`)
# nothing below changes a verdict: the single env ADMIN_TOKEN is compared as
# before. With it on, the token is looked up in the `drivers` table, which is
# seeded from that same env token as `owner-cli`.
DRIVER_TOKEN_HEADER = "X-AItelier-Driver-Token"
ADMIN_TOKEN_HEADER = "X-AItelier-Admin-Token"
ADMIN_REQUIRED = "admin_required"


def presented_token(request) -> str:
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    return (headers.get(DRIVER_TOKEN_HEADER, "") or headers.get(ADMIN_TOKEN_HEADER, "")).strip()


def driver_registry():
    """The live DriverRegistry, or None while the feature is off.

    Resolved lazily so importing this module never opens the production
    database; tests replace this function.
    """
    from core import drivers
    if not drivers.feature_enabled():
        return None
    from api.dependencies import get_db_manager
    return drivers.registry_for(get_db_manager())


def request_identity(request):
    """WHO this request is, from the raw credential only (never from arguments).

    Returns a `core.drivers.Identity`, or None for no recognised credential.
    Feature off: the pre-P0 actors (the Access email, else the shared
    `authorized-state-operator`) as kind 'legacy'.
    """
    from core import drivers
    headers = getattr(request, "headers", None) if request is not None else None
    email = None
    if headers is not None:
        email = cf_access.email_from_request_headers(headers, getattr(request, "cookies", {}))
    registry = driver_registry()
    if registry is None:
        if email:
            return drivers.Identity("legacy", email, email=email)
        return drivers.Identity("legacy", drivers.LEGACY_ACTOR)
    if email and email in WRITERS:
        # Q1 (confirmed): the owner's browser via Cloudflare is the owner, not `public`.
        return drivers.Identity("owner", "owner:" + email, is_admin=True, email=email)
    if request is None:
        return None
    token = presented_token(request)
    if token and not is_via_cloudflare(request):
        row = registry.lookup_token(token)
        if row is not None:
            return drivers.Identity("driver", "driver:" + row["driver_id"], row["driver_id"],
                                    is_admin=bool(row["is_admin"]))
    if is_via_tunnel(request):
        from api.mcp_router import _external_token_ok
        if _external_token_ok(request):
            row = registry.public_driver()
            if row is not None:
                return drivers.Identity("driver", "driver:" + drivers.PUBLIC_DRIVER_ID,
                                        drivers.PUBLIC_DRIVER_ID)
    return None


def request_actor(request) -> str:
    """The actor string State records for this (already authorized) request."""
    from core import drivers
    identity = request_identity(request)
    return identity.actor if identity is not None else drivers.LEGACY_ACTOR


def _token_authorizes(request, token: str) -> bool:
    registry = driver_registry()
    if registry is None:
        return bool(ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN))
    return registry.lookup_token(token) is not None


def local_admin_authority(request) -> bool:
    """An OFF-tunnel admin credential: the env admin token (feature off) or an
    active `is_admin` LAN driver token (feature on). Never a Cloudflare caller."""
    if is_via_cloudflare(request):
        return False
    token = presented_token(request)
    if not token:
        return False
    registry = driver_registry()
    if registry is None:
        return bool(ADMIN_TOKEN and hmac.compare_digest(token, ADMIN_TOKEN))
    row = registry.lookup_token(token)
    return bool(row and row["is_admin"])


def require_admin(request: Request) -> None:
    """FastAPI dependency: break-glass/admin operations (driver registry writes).

    Admin = an off-tunnel `is_admin` driver token, or the owner's allowlisted
    Access email. Test mode and an unconfigured gate behave like require_writer.
    """
    if getattr(request.app.state, "_test_mode", False) or not gate_enabled():
        return
    identity = request_identity(request)
    if (identity is not None and identity.is_admin
            and (identity.kind == "owner" or not is_via_cloudflare(request))):
        return
    raise HTTPException(status_code=403, detail="This operation needs an admin driver or the owner.",
                        headers={"X-AItelier-Denial": ADMIN_REQUIRED})


def write_denial_reason(request: Request) -> str | None:
    """Why a request may NOT write — None means it may.

    Gate off → everyone. Otherwise: an off-tunnel admin token (host CLI) OR an
    allowlisted Cloudflare Access email. The admin token is honored only when
    NOT arriving via Cloudflare, so a leaked token can't be replayed through the
    public edge (which always carries Cf-Ray / the Access JWT).

    On denial the distinguishable cases are reported separately, so the caller
    learns whether to sign in, ask for write rights, or fix its admin token.
    """
    if not gate_enabled():
        return None
    via_cloudflare = is_via_cloudflare(request)
    token = presented_token(request)
    if not via_cloudflare and token and _token_authorizes(request, token):
        return None
    email = cf_access.email_from_request_headers(request.headers, request.cookies)
    if email:
        return None if email in WRITERS else WRITE_DENIED_NOT_A_WRITER
    if token:
        return WRITE_DENIED_BAD_ADMIN_TOKEN
    return WRITE_DENIED_NOT_AUTHENTICATED


def request_can_write(request: Request) -> bool:
    """Whether a request is authorized to write (the verdict alone)."""
    return write_denial_reason(request) is None


def denial_body(code: str) -> dict:
    """403 body for a denial code. `detail` keeps the flat string shape every
    other api/ error uses; `code` is the stable machine-readable sibling the
    SPA maps to localized text."""
    return {"detail": DENIAL_MESSAGES[code], "code": code}


def require_writer(request: Request) -> None:
    """FastAPI dependency: 403 unless the request may write. Locks read (GET)
    endpoints to writers. Bypassed in test mode, mirroring the write_gate
    middleware so the test suite's TestClient is unaffected."""
    if getattr(request.app.state, "_test_mode", False):
        return
    code = write_denial_reason(request)
    if code:
        raise HTTPException(status_code=403, detail=DENIAL_MESSAGES[code])


def require_reader(request: Request) -> None:
    """FastAPI dependency: 403 unless the request may read a PRIVATE record.

    The identity verdict is IDENTICAL to `require_writer` — whoever may read the
    notebooks is whoever may write — but the wording is about reading.
    `require_writer`'s copy says "to make changes", and that copy was being sent
    to read requests; a reader who is refused must be told what was refused.

    The stable code travels in `X-AItelier-Denial` rather than in the body: the
    body keeps the flat `{"detail": "..."}` shape `require_writer` already had,
    and the header lets a client tell a read refusal from a write refusal without
    parsing prose.
    """
    if getattr(request.app.state, "_test_mode", False):
        return
    code = write_denial_reason(request)
    if code:
        read_code = _READ_DENIAL_CODES[code]
        raise HTTPException(status_code=403,
                            detail=DENIAL_MESSAGES[read_code],
                            headers={"X-AItelier-Denial": read_code})


def may_read_private(request: Request) -> bool:
    """Whether THIS request's identity may read a project nobody has opened.

    Identical verdict to `require_reader`/`require_writer` — whoever may read the
    notebooks is whoever may read an unopened project — but returned as a bool so
    the State transport can record it on the service and `core.state_commands.execute`
    can consult it WITHOUT going through the route guard. That placement is what
    makes project privacy survive the guard-shape hole: a forged route that makes
    the guard stand down cannot make this decision, because it is taken once per
    request from the raw credential and stored on the shared service. Test mode and
    an unconfigured gate mean trusted, exactly as every other verdict does.
    """
    if getattr(request.app.state, "_test_mode", False):
        return True
    if not gate_enabled():
        return True
    return write_denial_reason(request) is None


def execution_progress(request: Request, row: dict) -> dict:
    """Keep public progress separate from private source/context metadata."""
    if may_read_private(request):
        return row
    fields = {"id", "project_id", "name", "status", "config_name", "config_label",
              "created_at", "updated_at", "started_at", "completed_at", "priority",
              "current_project_step", "current_node", "latest_status", "latest_step",
              "last_update", "task_count", "completed_count", "running_count",
              "failed_count", "pending_count", "task_summary", "cache_stats",
              "has_task_loop", "is_authoring", "repo_less", "step_count",
              "completed_steps", "failed_steps", "active_step"}
    return {key: value for key, value in row.items() if key in fields}
