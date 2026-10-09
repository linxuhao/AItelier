"""Client-side: which credential a local caller presents (stdlib only).

design/multi-driver-coop.md §3 / D11: every LAN driver on linxuhaserver runs as
the same Linux user, so isolation between drivers is cooperative. The
convention that keeps it honest is one token FILE per driver:

    ~/.aitelier-drivers/            (0700)
    ~/.aitelier-drivers/<id>.token  (0600, one line, written by scripts/driver_token.py)

Resolution order - the first that yields a token wins:

1. ``AITELIER_DRIVER_TOKEN``         (env; for a process launched with it)
2. ``AITELIER_DRIVER_TOKEN_FILE``    (path to a token file)
3. ``~/.aitelier-drivers/$AITELIER_DRIVER_ID.token`` when ``AITELIER_DRIVER_ID`` is set
4. the legacy ``AITELIER_ADMIN_TOKEN`` (unchanged header, unchanged behavior;
   with driver identity on it authenticates as ``owner-cli``)

A driver token travels in ``X-AItelier-Driver-Token``; the legacy admin token
keeps travelling in ``X-AItelier-Admin-Token`` exactly as before, so callers
that configure nothing new send byte-identical requests.

A token file readable by group/other is refused (like ssh with a private key):
silently using it would normalize the one mistake the convention exists to
prevent. Nothing here ever prints a token.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

DRIVER_TOKEN_HEADER = "X-AItelier-Driver-Token"
ADMIN_TOKEN_HEADER = "X-AItelier-Admin-Token"
DRIVERS_DIR_NAME = ".aitelier-drivers"


class CredentialError(RuntimeError):
    """A configured credential exists but must not be used (message never holds it)."""


def drivers_dir(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / DRIVERS_DIR_NAME


def token_file_for(driver_id: str, home: Path | None = None) -> Path:
    if not driver_id or "/" in driver_id or driver_id.startswith("."):
        raise CredentialError("AITELIER_DRIVER_ID must be a plain driver id")
    return drivers_dir(home) / f"{driver_id}.token"


def read_token_file(path: Path) -> str:
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except OSError as exc:
        raise CredentialError(f"driver token file {path} is not readable ({type(exc).__name__})") from None
    if not stat.S_ISREG(info.st_mode):
        raise CredentialError(f"driver token file {path} is not a regular file")
    if info.st_mode & 0o077:
        raise CredentialError(f"driver token file {path} must be mode 0600 (chmod 600 {path})")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token:
        raise CredentialError(f"driver token file {path} must hold exactly one token line")
    return token


def driver_token(environ=None, home: Path | None = None) -> str | None:
    """The driver token configured for this process, else None (never the admin token)."""
    env = os.environ if environ is None else environ
    token = (env.get("AITELIER_DRIVER_TOKEN") or "").strip()
    if token:
        return token
    path = (env.get("AITELIER_DRIVER_TOKEN_FILE") or "").strip()
    if path:
        return read_token_file(Path(path))
    driver_id = (env.get("AITELIER_DRIVER_ID") or "").strip()
    if driver_id:
        return read_token_file(token_file_for(driver_id, home))
    return None


def auth_headers(admin_token: str | None = None, environ=None, home: Path | None = None) -> dict:
    """Headers for a local caller: its driver token, else the legacy admin token, else none."""
    token = driver_token(environ, home)
    if token:
        return {DRIVER_TOKEN_HEADER: token}
    if admin_token is None:
        env = os.environ if environ is None else environ
        admin_token = env.get("AITELIER_ADMIN_TOKEN")
    admin_token = (admin_token or "").strip()
    return {ADMIN_TOKEN_HEADER: admin_token} if admin_token else {}
