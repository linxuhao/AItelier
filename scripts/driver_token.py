#!/usr/bin/env python3
"""Register (or rotate) a LAN driver and store its token in its 0600 file.

    python3 scripts/driver_token.py register codex --display-name "Codex driver"
    python3 scripts/driver_token.py rotate grok-bot

Talks to the running server over loopback (127.0.0.1:4444) with the OWNER's
credential (the legacy admin token = `owner-cli`, or an is_admin driver token
resolved by core/driver_credentials.py). The new token goes straight into
~/.aitelier-drivers/<id>.token (dir 0700, file 0600) and is never printed.
The driver process then runs with AITELIER_DRIVER_ID=<id> (or
AITELIER_DRIVER_TOKEN_FILE=<path>). See docs/driver-identity.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.driver_credentials import (
    CredentialError,
    auth_headers,
    drivers_dir,
    token_file_for,
)

_BASE = os.environ.get("AITELIER_URL", "http://127.0.0.1:4444").rstrip("/")


def _owner_headers() -> dict:
    env = dict(os.environ)
    # The OWNER registers drivers: never the target driver's own token.
    for key in ("AITELIER_DRIVER_TOKEN", "AITELIER_DRIVER_TOKEN_FILE", "AITELIER_DRIVER_ID"):
        env.pop(key, None)
    owner_file = os.environ.get("AITELIER_OWNER_TOKEN_FILE")
    if owner_file:
        env["AITELIER_DRIVER_TOKEN_FILE"] = owner_file
    if not env.get("AITELIER_ADMIN_TOKEN"):
        dotenv = Path(__file__).resolve().parents[1] / ".env"
        for line in dotenv.read_text().splitlines() if dotenv.exists() else []:
            line = line.strip().removeprefix("export ")
            if line.startswith("AITELIER_ADMIN_TOKEN="):
                env["AITELIER_ADMIN_TOKEN"] = line.split("=", 1)[1].strip().strip("\"'")
    return auth_headers(environ=env)


def _call(method: str, path: str, body: dict | None, headers: dict) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(_BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # The server never echoes a token in an error; still, print only the detail.
        try:
            detail = json.loads(exc.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}")


def write_token_file(driver_id: str, token: str, home: Path | None = None) -> Path:
    directory = drivers_dir(home)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = token_file_for(driver_id, home)
    tmp = path.with_suffix(".token.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register")
    reg.add_argument("driver_id")
    reg.add_argument("--display-name", required=True)
    reg.add_argument("--host-label", default="linxuhaserver")
    reg.add_argument("--admin", action="store_true")
    rot = sub.add_parser("rotate")
    rot.add_argument("driver_id")
    options = parser.parse_args(argv)
    try:
        headers = _owner_headers()
    except CredentialError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not headers:
        print("No owner credential (AITELIER_ADMIN_TOKEN or AITELIER_OWNER_TOKEN_FILE).", file=sys.stderr)
        return 2
    if options.command == "register":
        result = _call("POST", "/api/drivers", {"driver_id": options.driver_id,
                                                "display_name": options.display_name,
                                                "host_label": options.host_label,
                                                "is_admin": options.admin}, headers)
    else:
        current = _call("GET", f"/api/drivers/{options.driver_id}", None, headers)
        result = _call("POST", f"/api/drivers/{options.driver_id}/rotate",
                       {"expected_revision": current["revision"]}, headers)
    path = write_token_file(options.driver_id, result.pop("token"))
    result.pop("token_notice", None)
    print(json.dumps({"driver": result["driver"], "token_file": str(path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
