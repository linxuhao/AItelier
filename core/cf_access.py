# core/cf_access.py
# Verify Cloudflare Access JWTs (the `Cf-Access-Jwt-Assertion` header).
#
# Cloudflare Access, when fronting the app, authenticates every request and
# injects a signed JWT. We verify it (signature against the team JWKS, audience,
# issuer, expiry) so write-gating can't be spoofed by anything reaching the
# origin off-tunnel — unlike the unsigned Cf-Access-Authenticated-User-Email
# header.

import os

_TEAM_DOMAIN = os.getenv("AITELIER_CF_TEAM_DOMAIN", "").strip().rstrip("/")
_AUD = os.getenv("AITELIER_CF_AUD", "").strip()

_CERTS_URL = f"https://{_TEAM_DOMAIN}/cdn-cgi/access/certs" if _TEAM_DOMAIN else ""
_ISSUER = f"https://{_TEAM_DOMAIN}" if _TEAM_DOMAIN else ""

# Lazily-built JWKS client (caches signing keys, fetches on demand).
_jwk_client = None


def is_configured() -> bool:
    """True if a team domain + AUD are set → JWT verification is active."""
    return bool(_TEAM_DOMAIN and _AUD)


def _client():
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient
        _jwk_client = PyJWKClient(_CERTS_URL)
    return _jwk_client


def verify(token: str) -> dict | None:
    """Verify a Cloudflare Access JWT. Returns its claims, or None if invalid."""
    if not token or not is_configured():
        return None
    try:
        import jwt
        signing_key = _client().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=_AUD,
            issuer=_ISSUER,
        )
    except Exception:
        return None


def email_from_request_headers(headers, cookies) -> str | None:
    """Extract the verified email from an Access JWT on a request, or None."""
    token = headers.get("Cf-Access-Jwt-Assertion") or cookies.get("CF_Authorization", "")
    claims = verify(token)
    email = (claims or {}).get("email")
    return email.lower() if email else None
