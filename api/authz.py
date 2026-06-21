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


def gate_enabled() -> bool:
    """True when Cloudflare Access verification is configured → gate is active."""
    return cf_access.is_configured()


def request_can_write(request: Request) -> bool:
    """Whether a request is authorized to write.

    Gate off → everyone. Otherwise: an off-tunnel admin token (host CLI) OR an
    allowlisted Cloudflare Access email. The admin token is honored only when
    NOT arriving via Cloudflare, so a leaked token can't be replayed through the
    public edge (which always carries Cf-Ray / the Access JWT).
    """
    if not gate_enabled():
        return True
    via_cloudflare = bool(
        request.headers.get("Cf-Ray")
        or request.headers.get("Cf-Access-Jwt-Assertion")
    )
    token = request.headers.get("X-AItelier-Admin-Token", "")
    if (not via_cloudflare and ADMIN_TOKEN and token
            and hmac.compare_digest(token, ADMIN_TOKEN)):
        return True
    email = cf_access.email_from_request_headers(request.headers, request.cookies)
    return bool(email and email in WRITERS)


def require_writer(request: Request) -> None:
    """FastAPI dependency: 403 unless the request may write. Locks read (GET)
    endpoints to writers. Bypassed in test mode, mirroring the write_gate
    middleware so the test suite's TestClient is unaffected."""
    if getattr(request.app.state, "_test_mode", False):
        return
    if not request_can_write(request):
        raise HTTPException(status_code=403, detail="Write access required")
