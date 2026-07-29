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
}


def gate_enabled() -> bool:
    """True when Cloudflare Access verification is configured → gate is active."""
    return cf_access.is_configured()


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
    via_cloudflare = bool(
        request.headers.get("Cf-Ray")
        or request.headers.get("Cf-Access-Jwt-Assertion")
    )
    token = request.headers.get("X-AItelier-Admin-Token", "")
    if (not via_cloudflare and ADMIN_TOKEN and token
            and hmac.compare_digest(token, ADMIN_TOKEN)):
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
