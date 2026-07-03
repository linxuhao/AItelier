# api/auth.py
# Shared auth primitives: CurrentUser model and default optional dependency.
# CLI mode: get_optional_user() returns None.
# Web mode: web_api/main.py overrides with get_current_user() (Cloudflare Access).

from pydantic import BaseModel


class CurrentUser(BaseModel):
    email: str
    display_name: str
    source: str  # "cloudflare"
    is_cli: bool = False
    lang: str | None = None


def get_optional_user() -> CurrentUser | None:
    """CLI default: no authenticated user. Returns None.
    Overridden by web_api/main.py with Cloudflare Access auth."""
    return None


def creator_email(request) -> str | None:
    """Email to attribute a NEWLY CREATED resource to, on the CLI/tunnel backend.

    Returns the verified Cloudflare Access email when the request carries one
    (the tunnel path — the write-gate has already validated it), else None for a
    genuine localhost CLI request (→ caller falls back to 'cli@local').

    Attribution ONLY: this does not gate reads/writes. Access control stays with
    the write-gate; owner_filter / get_optional_user are unchanged, so reads
    remain open and existing 'cli@local' projects stay visible. In web_api the
    caller already has a real ``user`` and never reaches this."""
    try:
        from core import cf_access
        return cf_access.email_from_request_headers(request.headers, request.cookies)
    except Exception:
        return None
