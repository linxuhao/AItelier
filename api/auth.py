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
