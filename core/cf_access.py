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

# How long the JWKS fetch may block, and how long a `kid` we could not resolve
# stays rejected without asking Cloudflare again.
#
# PyJWT's default socket timeout is 30 SECONDS, and its cache does not protect
# you the way it looks like it does: `get_signing_key_from_jwt` falls back to
# `get_signing_keys(refresh=True)` whenever the token's `kid` is not already
# known, which BYPASSES the 300s cache. So a token carrying a random `kid`
# forces a live HTTPS round-trip on every single request — and a failed fetch
# is never cached, so an unreachable JWKS costs the full timeout each time.
#
# On a public hostname that is an anonymous lever: this runs inside the
# write-gate, i.e. BEFORE the 403 that denies the request, so the denial is not
# the protection anyone assumed it was.
_FETCH_TIMEOUT = 3
_BAD_KID_TTL = 300
_BAD_KID_MAX = 256
_bad_kids: "dict[str, float]" = {}


def _kid_is_known_bad(token: str) -> bool:
    """True for a `kid` we recently failed to resolve — answered without a fetch."""
    import time
    try:
        import jwt
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:
        return True  # unparseable header: certainly not worth a network call
    if not kid:
        return True
    seen = _bad_kids.get(kid)
    if seen is not None and time.monotonic() - seen < _BAD_KID_TTL:
        return True
    return False


def _remember_bad_kid(token: str) -> None:
    import time
    try:
        import jwt
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:
        return
    if not kid:
        return
    if len(_bad_kids) >= _BAD_KID_MAX:
        # Bounded: the whole point is that an attacker picks the keys.
        _bad_kids.clear()
    _bad_kids[kid] = time.monotonic()


def is_configured() -> bool:
    """True if a team domain + AUD are set → JWT verification is active."""
    return bool(_TEAM_DOMAIN and _AUD)


def _client():
    global _jwk_client
    if _jwk_client is None:
        from jwt import PyJWKClient
        _jwk_client = PyJWKClient(_CERTS_URL, timeout=_FETCH_TIMEOUT)
    return _jwk_client


def verify(token: str) -> dict | None:
    """Verify a Cloudflare Access JWT. Returns its claims, or None if invalid."""
    if not token or not is_configured():
        return None
    if _kid_is_known_bad(token):
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
        _remember_bad_kid(token)
        return None


def email_from_request_headers(headers, cookies) -> str | None:
    """Extract the verified email from an Access JWT on a request, or None."""
    token = headers.get("Cf-Access-Jwt-Assertion") or cookies.get("CF_Authorization", "")
    claims = verify(token)
    email = (claims or {}).get("email")
    return email.lower() if email else None
