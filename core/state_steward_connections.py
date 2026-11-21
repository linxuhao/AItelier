"""The steward's own MCP connections (step 24.1a, design-steward 7.8.4, 7.8.5, 7.8.10a).

The owner registers MCP servers on the dashboard (`steward_connection_upsert` and its
siblings: owner session only); each registered server is a CONNECTION with a
visibility (`private`: only the owner's own turns; `public`: also a family group's)
and a list of the tools its server listed, each of them off until the owner allows it.
The steward never connects to them itself: its profile holds ONE entry, the backend's
aggregating gateway (api/steward_gateway.py), which serves exactly the EFFECTIVE tool
set computed here (`effective`) and forwards an allowed call to the connection.

Which tools a turn may use is decided twice, by code: the steward plugin's catalog
(from `list_steward_connections`, the same `effective` function) and the gateway's
second layer, which reads the turn audience the steward's host recorded before the
turn's first step (step 23's `set_steward_turn_audience`, core.state_groups) before
forwarding a call to a private connection (`audience_of`).

A tool's description and schema are the ones the owner allowed: discovery stores the
tool's canonical definition and its sha256; a tool whose definition changed since it
was allowed leaves the effective set until the owner allows it again.

Secrets (a fixed header's value) never enter this database: each connection with one
has a 0600 file in `secrets_dir()` (0700), mounted into the backend alone.

When the effective set changes, every write path calls `tools_changed()` after its
commit; the gateway listens and sends `notifications/tools/list_changed`. When the
steward has no live notification stream the fallback is a restart marker
(`state_steward_restart_due`), executed by whoever spawns the steward's process
(core.dsh_driver.fallback_restart); `state_steward_restart_done` records each such
restart so that one effective hash is restarted for at most once in 24 hours.

Every table here is private (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets as _secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from core.state_graph import StateGraphError, StateNotFound, canonical
from core.state_privacy import UntrustedDatabase, writer_only_read

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_connections (
 conn_id TEXT PRIMARY KEY, conn_uid TEXT NOT NULL, label TEXT NOT NULL, url TEXT NOT NULL,
 auth_kind TEXT NOT NULL CHECK(auth_kind IN ('none','header','oauth')), header_name TEXT,
 visibility TEXT NOT NULL CHECK(visibility IN ('private','public')),
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 generation INTEGER NOT NULL, config_version INTEGER NOT NULL, auth_identity TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_probe_at TEXT, last_error TEXT,
 instructions TEXT, last_change TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_steward_connection_tools (
 conn_id TEXT NOT NULL, raw_name TEXT NOT NULL, gw_name TEXT NOT NULL,
 definition_json TEXT NOT NULL, schema_sha256 TEXT NOT NULL,
 allowed INTEGER NOT NULL DEFAULT 0 CHECK(allowed IN (0,1)), allowed_sha256 TEXT,
 outward INTEGER NOT NULL CHECK(outward IN (0,1)), egress INTEGER NOT NULL CHECK(egress IN (0,1)),
 blocked_args TEXT NOT NULL DEFAULT '[]', daily_cap INTEGER,
 first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 PRIMARY KEY(conn_id, raw_name));
CREATE TABLE IF NOT EXISTS state_steward_tools_served (
 driver_id TEXT PRIMARY KEY, effective_hash TEXT NOT NULL, served_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_steward_restart_due (
 driver_id TEXT NOT NULL, reason TEXT NOT NULL, effective_hash TEXT NOT NULL,
 due_at TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(driver_id, reason));
CREATE TABLE IF NOT EXISTS state_steward_restart_done (
 id INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL, reasons_json TEXT NOT NULL,
 restarted_at TEXT NOT NULL, error TEXT, noticed_at TEXT);
CREATE TABLE IF NOT EXISTS state_steward_tool_calls (
 id INTEGER PRIMARY KEY AUTOINCREMENT, conn_id TEXT NOT NULL, raw_name TEXT NOT NULL,
 at TEXT NOT NULL, local_day TEXT NOT NULL, rpc_error TEXT, is_error INTEGER NOT NULL DEFAULT 0,
 request_bytes INTEGER NOT NULL DEFAULT 0, response_bytes INTEGER NOT NULL DEFAULT 0,
 duration_ms INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS state_steward_tool_calls_day ON state_steward_tool_calls(conn_id,raw_name,local_day);
"""
TABLES = ("state_steward_connections", "state_steward_connection_tools", "state_steward_tools_served", "state_steward_restart_due", "state_steward_restart_done",
          "state_steward_tool_calls")

# The steward's profile entry for the gateway is `serverName: sw`; every tool the
# steward sees from it is `mcp__sw__<conn_id>__<gw_name>` (7.8.5 "名字").
SERVER_NAME = "sw"
PUBLIC_PREFIX = f"mcp__{SERVER_NAME}__"
MAX_PUBLIC_NAME = 64  # dsh-mcp-client MAX_PUBLIC_NAME_LENGTH, counted over the whole public name
CONN_ID = re.compile(r"^[a-z][a-z0-9]{1,19}$")
CLEAN_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
# A fixed header the gateway may never set upstream: transport and identity headers.
FORBIDDEN_HEADERS = {"host", "cookie", "content-length", "content-type", "accept", "connection",
                     "transfer-encoding", "mcp-session-id", "mcp-protocol-version",
                     "x-aitelier-driver-token"}
LABEL_MAX = 40
# The 24.1a auth kinds; `oauth` arrives with 24.1b.
AUTH_KINDS_NOW = ("none", "header")
RESTART_STREAM_DELAY = timedelta(seconds=90)   # tools_stream_down: past the ~60 s reopen window
RESTART_LIMIT_WINDOW = timedelta(hours=24)
REFRESH_INTERVAL = timedelta(seconds=60)
TOOL_CALLS_KEPT = timedelta(days=30)
STREAM_DOWN = "tools_stream_down"
RESYNC = "tools_resync:"

# Hooks the gateway installs (api/steward_gateway.py): called after every commit that
# may change the effective tool set, and to read a connection's tool list.
TOOLS_CHANGED_HOOKS: list = []
PROBER = None  # (connection row dict) -> {"tools": [definition...], "instructions": str|None}


class ProbeFailed(Exception):
    """A connection that could not be read; `code` is what the row's last_error keeps."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def tools_changed() -> None:
    for hook in list(TOOLS_CHANGED_HOOKS):
        try:
            hook()
        except Exception:  # noqa: BLE001 - a notifier failure never fails the write
            pass


# ---------------------------------------------------------------- time
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    """One fixed shape, so stored times compare as text too."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_day(moment: datetime) -> str:
    from zoneinfo import ZoneInfo
    from core.state_steward import default_tz
    return moment.astimezone(ZoneInfo(default_tz())).date().isoformat()


# ---------------------------------------------------------------- names
def gw_name(conn_id: str, raw_name: str) -> str:
    """The gateway's name for one tool: `raw_name` itself when it is clean and the public
    name fits; else the clean part, shortened, plus `_` and 8 hex of sha256(raw_name) -
    so dsh-mcp-client never renames, and the public name splits back losslessly."""
    prefix = len(PUBLIC_PREFIX) + len(conn_id) + 2
    if CLEAN_NAME.match(raw_name) and prefix + len(raw_name) <= MAX_PUBLIC_NAME:
        return raw_name
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", raw_name)
    keep = MAX_PUBLIC_NAME - prefix - 9
    return clean[:keep] + "_" + hashlib.sha256(raw_name.encode("utf-8", "surrogatepass")).hexdigest()[:8]


def public_name(conn_id: str, gw: str) -> str:
    return f"{PUBLIC_PREFIX}{conn_id}__{gw}"


def split_name(name: str) -> tuple[str, str] | None:
    """`<conn_id>__<gw_name>` (the gateway's own name) -> its two parts."""
    if not isinstance(name, str) or "__" not in name:
        return None
    conn_id, gw = name.split("__", 1)
    return (conn_id, gw) if CONN_ID.match(conn_id) and gw else None


def definition_of(tool: dict) -> dict:
    """A listed tool's canonical definition: name, title, description, input schema and
    annotations - never outputSchema (7.8.5: the steward is never given one)."""
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
        raise ValueError("a tool has no name")
    schema = tool.get("inputSchema")
    out = {"name": tool["name"], "description": tool.get("description") if isinstance(tool.get("description"), str) else "",
           "inputSchema": schema if isinstance(schema, dict) else {"type": "object"}}
    if isinstance(tool.get("title"), str):
        out["title"] = tool["title"]
    if isinstance(tool.get("annotations"), dict) and tool["annotations"]:
        out["annotations"] = tool["annotations"]
    return out


def definition_json(tool: dict) -> str:
    text = canonical(definition_of(tool))
    text.encode("utf-8")  # a lone surrogate cannot be stored or hashed: refused here
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


URL_WALK_DEPTH = 12


def _is_url_name(key) -> bool:
    return isinstance(key, str) and (key == "url" or key.endswith("_url"))


def _pointer_key(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _url_params(schema: dict) -> list[str]:
    """Every `url` / `*_url` parameter of an input schema, at any depth, as an argument
    path: a top-level one by its bare name (`image_url`), a nested one as its JSON-pointer
    segments joined by `/` (`target/callback_url`), `*` standing for every item of an
    array (`pages/*/url`). It walks `properties`, `items` / `prefixItems`, `anyOf` /
    `oneOf` / `allOf` and local `$ref`s (`#/$defs/...`, `#/definitions/...`); a
    parameter named only by `additionalProperties` / `patternProperties` has no fixed
    name and is not found here (the owner can still block it by hand)."""
    root = schema if isinstance(schema, dict) else {}
    found: set[str] = set()

    def resolve(node, refs):
        while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"] not in refs:
            ref = node["$ref"]
            refs = refs | {ref}
            target = root
            if not ref.startswith("#/"):
                return None, refs
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part) if isinstance(target, dict) else None
            node = target
        return (node if isinstance(node, dict) and "$ref" not in node else None), refs

    def walk(node, path, refs, depth):
        node, refs = resolve(node, refs)
        if node is None or depth > URL_WALK_DEPTH:
            return
        for key, sub in (node.get("properties") or {}).items() if isinstance(node.get("properties"), dict) else ():
            here = path + [_pointer_key(key)] if isinstance(key, str) else None
            if here is None:
                continue
            if _is_url_name(key):
                found.add("/".join(here))
            else:
                walk(sub, here, refs, depth + 1)
        items = node.get("items")
        for sub in ([items] if isinstance(items, dict) else items if isinstance(items, list) else []) + (
                node.get("prefixItems") if isinstance(node.get("prefixItems"), list) else []):
            walk(sub, path + ["*"], refs, depth + 1)
        for keyword in ("anyOf", "oneOf", "allOf"):
            for sub in node.get(keyword) if isinstance(node.get(keyword), list) else []:
                walk(sub, path, refs, depth + 1)

    walk(root, [], frozenset(), 0)
    return sorted(found)


def blocked_present(arguments, blocked: list[str]) -> bool:
    """Whether a call's arguments carry any blocked path (`_url_params`' shape: a bare
    name is a top-level argument, `a/b` a nested one, `*` every item of an array; `~1` and
    `~0` are a key's `/` and `~`). Present means the key is there, whatever its value."""
    def present(value, parts):
        if not parts:
            return True
        head, rest = parts[0], parts[1:]
        if isinstance(value, dict):
            key = head.replace("~1", "/").replace("~0", "~")
            return key in value and present(value[key], rest)
        if isinstance(value, list) and head == "*":
            return any(present(item, rest) for item in value)
        return False
    return any(present(arguments, path.split("/")) for path in blocked if isinstance(path, str) and path)


# ---------------------------------------------------------------- secrets
def secrets_dir() -> Path:
    return Path(os.environ.get("AITELIER_STEWARD_SECRETS_DIR", "/run/aitelier-steward-secrets"))


def _secret_path(conn_id: str) -> Path:
    if not CONN_ID.match(conn_id):
        raise StateGraphError("invalid_conn_id")
    return secrets_dir() / f"{conn_id}.json"


def read_secret(conn_id: str) -> dict | None:
    try:
        value = json.loads(_secret_path(conn_id).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_secret(conn_id: str, value: dict) -> None:
    directory = secrets_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    target = _secret_path(conn_id)
    temporary = target.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(value))
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def delete_secret(conn_id: str) -> None:
    try:
        _secret_path(conn_id).unlink()
    except FileNotFoundError:
        pass


def prune_orphan_secrets(store) -> list[str]:
    """At startup: delete every secret file whose connection is gone (a delete that died
    between its commit and the file's removal). The files are judged from a finished
    read, and each is deleted only if its connection is still absent just before.
    OAuth revocation comes with 24.1b."""
    directory = secrets_dir()
    if not directory.is_dir():
        return []
    candidates = sorted(p.stem for p in directory.glob("*.json") if CONN_ID.match(p.stem))
    if not candidates:
        return []
    with store.transaction() as conn:
        known = {r[0] for r in conn.execute("SELECT conn_id FROM state_steward_connections")}
    removed = []
    for conn_id in candidates:
        if conn_id in known:
            continue
        with store.transaction() as conn:
            back = conn.execute("SELECT 1 FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
        if back is None:
            delete_secret(conn_id)
            removed.append(conn_id)
    return removed


def secret_status(row) -> str:
    """`set` / `unset` for a header connection (never the value); `set` for `none`."""
    if row["auth_kind"] == "none":
        return "set"
    secret = read_secret(row["conn_id"])
    if row["auth_kind"] == "header":
        return "set" if secret and isinstance(secret.get("header_value"), str) and secret["header_value"] else "unset"
    return "oauth_needs_consent"


# ---------------------------------------------------------------- reserved endpoints
RESERVED_SERVICES = ("aitelier", "dev_mcp", "zvec-grep", "aitelier-zg")
HOST_NAMES = ("localhost", "host.docker.internal")
HOST_LITERALS = ("127.0.0.1", "0.0.0.0", "::1")
# Every lookup of a connection's (or a reserved service's) address goes through this
# one resolver; tests replace it.
RESOLVER = socket.getaddrinfo
ROUTE_TABLE = "/proc/net/route"


def host_addresses() -> list[str]:
    """The host's own addresses (docker gateways, LAN, tailnet), read at step 0 and set
    in the backend's environment: a connection resolving to one is refused on any port."""
    raw = os.environ.get("AITELIER_STEWARD_HOST_ADDRESSES", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def route_gateways() -> set[str]:
    """Each docker network's gateway, read from the backend's route table: the default
    route's gateway, and for a directly attached network its first address (docker's
    gateway). Both are the host itself, reserved whether or not
    AITELIER_STEWARD_HOST_ADDRESSES names them (7.8.4)."""
    def dotted(raw: int) -> str:  # the table is little-endian hex
        return ".".join(str((raw >> s) & 0xFF) for s in (0, 8, 16, 24))
    found = set()
    try:
        with open(ROUTE_TABLE) as handle:
            for line in handle.readlines()[1:]:
                fields = line.split()
                if len(fields) < 8:
                    continue
                destination, gateway, mask = (int(fields[i], 16) for i in (1, 2, 7))
                if gateway:
                    found.add(dotted(gateway))
                elif mask:
                    network = int.from_bytes(destination.to_bytes(4, "little"), "big")
                    found.add(str(ipaddress.IPv4Address(network + 1)))
    except (OSError, ValueError):
        return set()
    return found


def _resolve(host: str, resolver) -> set[str]:
    try:
        return {info[4][0].split("%")[0] for info in resolver(host, None)}
    except (OSError, UnicodeError):
        return set()


def check_url(url: str) -> tuple[str, str, int]:
    """The shape a connection address may have (7.8.4, 7.8.10a): `https://`, or `http://`
    to a container name the backend reaches directly; no user info, query or fragment,
    an ASCII host. Returns (scheme, host, port)."""
    if not isinstance(url, str) or len(url) > 500 or any(c.isspace() for c in url):
        raise StateGraphError("invalid_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise StateGraphError("invalid_url") from None
    host = parts.hostname or ""
    if (parts.scheme not in ("https", "http") or not host or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or "@" in parts.netloc
            or not host.isascii()):
        raise StateGraphError("invalid_url")
    if parts.scheme == "http":
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        if is_ip or "." in host:
            raise StateGraphError("invalid_url: http only to a container name the backend reaches directly")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def reserved_addresses(resolver=None) -> set[str]:
    resolver = resolver or RESOLVER
    reserved = set(HOST_LITERALS) | set(host_addresses()) | route_gateways()
    for name in RESERVED_SERVICES + HOST_NAMES:
        reserved |= _resolve(name, resolver)
    return reserved


def checked_address(url: str, resolver=None, *, require=True) -> str | None:
    """Resolve the connection's host and refuse it (`reserved_endpoint`) when any of its
    addresses is the backend, dev_mcp, zvec-grep or the host itself - on any port, by
    the resolved address, never by the written name. Returns the address to connect to
    (the one judged: no second lookup, 7.8.10a), or None when the host does not resolve
    and `require` is false (a registration may name a server that is not up yet)."""
    resolver = resolver or RESOLVER
    _scheme, host, _port = check_url(url)
    found = _resolve(host, resolver)
    if not found:
        if require:
            raise ProbeFailed("unresolvable_host")
        return None
    reserved = reserved_addresses(resolver)
    for address in found:
        ip = ipaddress.ip_address(address)
        if address in reserved or ip.is_loopback or ip.is_unspecified or (
                getattr(ip, "ipv4_mapped", None) and (str(ip.ipv4_mapped) in reserved or ip.ipv4_mapped.is_loopback)):
            raise StateGraphError("reserved_endpoint")
    return sorted(found)[0]


# ---------------------------------------------------------------- the effective set
def _has_tables(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_steward_connections'"
                        ).fetchone() is not None


def _collisions(rows) -> set[tuple[str, str]]:
    """(conn_id, raw_name) of every tool whose gw_name an earlier tool of its connection
    already holds (first_seen_at, then raw_name): fixed order, a re-read never swaps."""
    seen, lost = set(), set()
    for row in sorted(rows, key=lambda r: (r["conn_id"], r["first_seen_at"], r["raw_name"])):
        key = (row["conn_id"], row["gw_name"])
        if key in seen:
            lost.add((row["conn_id"], row["raw_name"]))
        seen.add(key)
    return lost


def connection_usable(row) -> bool:
    """A connection whose tools can be in the effective set: enabled, its secret set,
    its auth kind available now (OAuth is 24.1b: never usable yet)."""
    return bool(row["enabled"]) and row["auth_kind"] in AUTH_KINDS_NOW and secret_status(row) == "set"


def effective(conn) -> list[dict]:
    """THE effective tool set (7.8.5 "有效工具集"), sorted by name: one entry per tool of a
    usable connection that is allowed, unchanged since allowed and not out-named.
    Visibility is not part of it. Each entry: the served definition under `tool`
    (name `<conn_id>__<gw_name>`) and the row facts the gateway and the catalog need."""
    if not _has_tables(conn):
        return []
    connections = {r["conn_id"]: r for r in conn.execute("SELECT * FROM state_steward_connections")}
    rows = conn.execute("SELECT * FROM state_steward_connection_tools").fetchall()
    lost = _collisions(rows)
    out = []
    for row in rows:
        c = connections.get(row["conn_id"])
        if (c is None or not connection_usable(c) or not row["allowed"] or row["allowed_sha256"] != row["schema_sha256"]
                or (row["conn_id"], row["raw_name"]) in lost):
            continue
        definition = json.loads(row["definition_json"])
        tool = {"name": f"{row['conn_id']}__{row['gw_name']}", "description": definition.get("description", ""),
                "inputSchema": definition.get("inputSchema", {"type": "object"})}
        for key in ("title", "annotations"):
            if key in definition:
                tool[key] = definition[key]
        out.append({"tool": tool, "conn_id": row["conn_id"], "raw_name": row["raw_name"], "gw_name": row["gw_name"],
                    "visibility": c["visibility"], "egress": bool(row["egress"]), "outward": bool(row["outward"]),
                    "blocked_args": json.loads(row["blocked_args"]), "daily_cap": row["daily_cap"]})
    return sorted(out, key=lambda e: e["tool"]["name"])


def effective_hash(entries) -> str:
    """The hash of the WHOLE list the gateway serves (names, descriptions, schemas,
    annotations): a tool re-allowed with a new description is a change too."""
    return hashlib.sha256(canonical([e["tool"] for e in entries]).encode("utf-8")).hexdigest()


def current_hash(conn) -> str:
    return effective_hash(effective(conn))


# ---------------------------------------------------------------- restart markers
def _restart_tables(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_steward_restart_due'"
                        ).fetchone() is not None


def clear_restart_markers(conn, driver_id: str) -> int:
    """Delete one driver's restart markers on the caller's connection (the spawn of a new
    steward process reads the whole list anyway). A database without the table has none."""
    try:
        return conn.execute("DELETE FROM state_steward_restart_due WHERE driver_id=?", (driver_id,)).rowcount
    except sqlite3.OperationalError as error:
        if "no such table" in str(error):
            return 0
        raise


def restarted_for(conn, driver_id: str, digest: str, moment: datetime | None = None):
    """The restart_done row within 24 h whose reasons name `digest` (any reason, any
    outcome), else None: one fallback restart per (driver, hash) per day."""
    since = _iso((moment or _utcnow()) - RESTART_LIMIT_WINDOW)
    for row in conn.execute("SELECT * FROM state_steward_restart_done WHERE driver_id=? AND restarted_at>=? "
                            "ORDER BY id DESC", (driver_id, since)):
        if any(r.get("effective_hash") == digest for r in json.loads(row["reasons_json"])):
            return row
    return None


def _owner_notice(conn, driver_id: str, done_row, text: str) -> None:
    """Tell once (per restart_done row): the dashboard connections page shows every row
    with a notice, and the steward's own inbox gets the line to pass on."""
    if done_row["noticed_at"]:
        return
    conn.execute("UPDATE state_steward_restart_done SET noticed_at=? WHERE id=?", (_iso(_utcnow()), done_row["id"]))
    try:
        from core.driver_inbox import deliver_notice
        from core.state_steward import steward_project
        deliver_notice(conn, {"target_driver_id": driver_id, "sender_driver_id": None,
                              "project_id": steward_project(), "kind": "note",
                              "subject": "Your connection tools did not sync", "body": text, "refs": {}})
    except Exception:  # noqa: BLE001 - the dashboard still shows it
        pass


LIMIT_NOTICE = ("管家的连接工具没同步上，已后备重启过一次；请按「重启管家」。"
                " (Your connection tools did not sync after one fallback restart; ask your owner to press Restart steward.)")
FAILED_NOTICE = "管家的后备重启失败（{code}）；请按「重启管家」。 (The fallback restart failed: {code}.)"


def write_marker(conn, driver_id: str, reason: str, digest: str, due: datetime) -> str:
    """One marker row unless one is there (never moved) or this hash was restarted for
    within 24 h (then the owner is told once instead). Returns what happened."""
    done = restarted_for(conn, driver_id, digest)
    if done is not None:
        _owner_notice(conn, driver_id, done, LIMIT_NOTICE)
        return "limited"
    inserted = conn.execute("INSERT OR IGNORE INTO state_steward_restart_due VALUES(?,?,?,?,?)",
                            (driver_id, reason, digest, _iso(due), _iso(_utcnow()))).rowcount
    return "written" if inserted else "exists"


def record_served(conn, driver_id: str, digest: str) -> None:
    """The gateway answered this driver's tools/list with `digest` (the current hash):
    remember it and drop its tools_stream_down marker (never a tools_resync one)."""
    conn.execute("INSERT INTO state_steward_tools_served VALUES(?,?,?) ON CONFLICT(driver_id) DO UPDATE SET "
                 "effective_hash=excluded.effective_hash, served_at=excluded.served_at",
                 (driver_id, digest, _iso(_utcnow())))
    conn.execute("DELETE FROM state_steward_restart_due WHERE driver_id=? AND reason=?", (driver_id, STREAM_DOWN))


def check_stream_markers(conn, drivers: list[dict]) -> list[str]:
    """The gateway's check (after every change, every 30 s, and once at startup), for each
    steward driver: {driver_id, enabled, running, phase, has_stream}. Writes a
    tools_stream_down marker (due +90 s) when its process runs, is not starting, has no
    live stream and was last served another hash; drops it when the served hash is
    current again; tells the owner once about a failed fallback restart."""
    digest = current_hash(conn)
    written = []
    for d in drivers:
        served = conn.execute("SELECT effective_hash FROM state_steward_tools_served WHERE driver_id=?",
                              (d["driver_id"],)).fetchone()
        if served is not None and served[0] == digest:
            conn.execute("DELETE FROM state_steward_restart_due WHERE driver_id=? AND reason=?",
                         (d["driver_id"], STREAM_DOWN))
        elif (d["enabled"] and d["running"] and not d["has_stream"] and d.get("phase") != "starting"):
            if write_marker(conn, d["driver_id"], STREAM_DOWN, digest, _utcnow() + RESTART_STREAM_DELAY) == "written":
                written.append(d["driver_id"])
        for row in conn.execute("SELECT * FROM state_steward_restart_done WHERE driver_id=? AND error IS NOT NULL "
                                "AND noticed_at IS NULL", (d["driver_id"],)).fetchall():
            _owner_notice(conn, d["driver_id"], row, FAILED_NOTICE.format(code=row["error"]))
    return written


def take_due_markers(conn, driver_id: str, moment: datetime | None = None):
    """For the restart executor: (earliest due_at, the driver's marker rows) when at least
    one is due, else (None, rows)."""
    if not _restart_tables(conn):
        return None, []
    rows = conn.execute("SELECT * FROM state_steward_restart_due WHERE driver_id=?", (driver_id,)).fetchall()
    moment = moment or _utcnow()
    due = [_parse(r["due_at"]) for r in rows if _parse(r["due_at"]) <= moment]
    return (min(due) if due else None), rows


# ---------------------------------------------------------------- the service
class StateStewardConnections:
    """Owner commands, the steward's two writes and the two reads (7.8.4)."""

    def __init__(self, store, actor, driver_id=None, steward=None):
        self.store, self.actor, self.driver_id, self.steward = store, actor, driver_id, steward
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            ensure_schema(store.db)

    # -- identity
    def _owner(self):
        if self.steward is None or not self.steward.is_owner_session():
            raise StateGraphError("owner_session_required")

    def _owner_or_steward(self):
        if self.steward is None or not (self.steward.is_owner_session() or self.steward.is_steward()):
            raise StateGraphError("owner_or_steward_required")

    def _row(self, conn, conn_id):
        row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
        if row is None:
            raise StateNotFound(f"no connection {conn_id}")
        return row

    # -- owner writes
    def upsert(self, conn_id, label, url, auth_kind="none", visibility="private", enabled=False,
               header_name=None):
        """Add a connection (disabled until the owner enables it) or change one. A new
        address or auth bumps `generation` (the gateway reopens its upstream session)
        and marks every existing tool egress again (7.8.4)."""
        self._owner()
        if not isinstance(conn_id, str) or not CONN_ID.match(conn_id):
            raise StateGraphError("invalid_conn_id: ^[a-z][a-z0-9]{1,19}$")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX or any(
                ord(c) < 32 or c in "「」\"'" or 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069
                or ord(c) in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF) for c in label):
            raise StateGraphError("invalid_label")
        if auth_kind not in ("none", "header", "oauth"):
            raise StateGraphError("invalid_auth_kind")
        if auth_kind not in AUTH_KINDS_NOW:
            raise StateGraphError("auth_kind_unavailable: oauth connections arrive with step 24.1b")
        if visibility not in ("private", "public"):
            raise StateGraphError("invalid_visibility")
        if auth_kind == "header":
            header_name = header_name or "Authorization"
            if not HEADER_NAME.match(header_name) or header_name.lower() in FORBIDDEN_HEADERS:
                raise StateGraphError("invalid_header_name")
        else:
            header_name = None
        checked_address(url, require=False)
        moment = _iso(_utcnow())
        with self.store.transaction(write=True) as conn:
            label = label.strip()
            if conn.execute("SELECT 1 FROM state_steward_connections WHERE label=? AND conn_id<>?",
                            (label, conn_id)).fetchone():
                raise StateGraphError("label_exists")
            old = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            if old is None:
                conn.execute("INSERT INTO state_steward_connections VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (conn_id, _secrets.token_hex(16), label, url, auth_kind, header_name, visibility,
                              int(bool(enabled)), 1, 1, None, moment, moment, None, None, None, "dashboard"))
            else:
                moved = (old["url"], old["auth_kind"], old["header_name"]) != (url, auth_kind, header_name)
                conn.execute("UPDATE state_steward_connections SET label=?,url=?,auth_kind=?,header_name=?,"
                             "visibility=?,enabled=?,generation=generation+?,config_version=config_version+1,"
                             "updated_at=?,last_change='dashboard' WHERE conn_id=?",
                             (label, url, auth_kind, header_name, visibility, int(bool(enabled)), int(moved),
                              moment, conn_id))
                if old["url"] != url:
                    conn.execute("UPDATE state_steward_connection_tools SET egress=1 WHERE conn_id=?", (conn_id,))
                if old["visibility"] == "private" and visibility == "public":
                    # The public default (7.8.4, 7.8.10a): url / *_url arguments blocked, added only.
                    for tool in conn.execute("SELECT raw_name,definition_json,blocked_args FROM "
                                             "state_steward_connection_tools WHERE conn_id=?", (conn_id,)).fetchall():
                        blocked = set(json.loads(tool["blocked_args"]))
                        added = blocked | set(_url_params(json.loads(tool["definition_json"]).get("inputSchema")))
                        if added != blocked:
                            conn.execute("UPDATE state_steward_connection_tools SET blocked_args=? WHERE conn_id=? "
                                         "AND raw_name=?", (json.dumps(sorted(added)), conn_id, tool["raw_name"]))
            result = self._public(conn, self._row(conn, conn_id))
        tools_changed()
        return result

    def delete(self, conn_id):
        self._owner()
        with self.store.transaction(write=True) as conn:
            self._row(conn, conn_id)
            conn.execute("DELETE FROM state_steward_connection_tools WHERE conn_id=?", (conn_id,))
            conn.execute("DELETE FROM state_steward_connections WHERE conn_id=?", (conn_id,))
        delete_secret(conn_id)
        tools_changed()
        return {"deleted": conn_id}

    def tools_set(self, conn_id, tools):
        """The owner's ticks, per tool: allowed, outward, egress, blocked_args, daily_cap.
        A field left out is unchanged. Allowing names the schema_sha256 the owner was
        shown and pins it; a definition that changed since is refused
        (`definition_changed`), never approved unseen (7.8.4)."""
        self._owner()
        if not isinstance(tools, list) or not tools or len(tools) > 500:
            raise StateGraphError("invalid_tools")
        with self.store.transaction(write=True) as conn:
            self._row(conn, conn_id)
            for item in tools:
                if not isinstance(item, dict) or not isinstance(item.get("raw_name"), str) or set(item) - {
                        "raw_name", "allowed", "outward", "egress", "blocked_args", "daily_cap", "schema_sha256"}:
                    raise StateGraphError("invalid_tools")
                row = conn.execute("SELECT * FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?",
                                   (conn_id, item["raw_name"])).fetchone()
                if row is None:
                    raise StateNotFound(f"no tool {item['raw_name']} on {conn_id}")
                sets, params = [], []
                for flag in ("allowed", "outward", "egress"):
                    if flag in item:
                        if not isinstance(item[flag], bool):
                            raise StateGraphError(f"invalid_tools: {flag} must be true or false")
                        sets.append(f"{flag}=?")
                        params.append(int(item[flag]))
                if item.get("allowed") is True:
                    if item.get("schema_sha256") != row["schema_sha256"]:
                        raise StateGraphError(f"definition_changed: {item['raw_name']} - read the tools again")
                    sets.append("allowed_sha256=?")
                    params.append(row["schema_sha256"])
                if "blocked_args" in item:
                    value = item["blocked_args"]
                    if not isinstance(value, list) or any(not isinstance(v, str) or not v or len(v) > 1000 for v in value):
                        raise StateGraphError("invalid_tools: blocked_args is a list of argument paths")
                    sets.append("blocked_args=?")
                    params.append(json.dumps(sorted(set(value))))
                if "daily_cap" in item:
                    cap = item["daily_cap"]
                    if cap is not None and (type(cap) is not int or not 1 <= cap <= 100000):
                        raise StateGraphError("invalid_tools: daily_cap is null or 1..100000")
                    sets.append("daily_cap=?")
                    params.append(cap)
                if sets:
                    conn.execute(f"UPDATE state_steward_connection_tools SET {','.join(sets)} "
                                 "WHERE conn_id=? AND raw_name=?", (*params, conn_id, item["raw_name"]))
            conn.execute("UPDATE state_steward_connections SET config_version=config_version+1,updated_at=?,"
                         "last_change='dashboard' WHERE conn_id=?", (_iso(_utcnow()), conn_id))
            result = self._tools(conn, conn_id)
        tools_changed()
        return result

    def secret_set(self, conn_id, value):
        """The fixed header's value: written to the connection's secret file, never to
        the database and never read back. An empty value clears it."""
        self._owner()
        with self.store.transaction() as conn:
            row = self._row(conn, conn_id)
        if row["auth_kind"] != "header":
            raise StateGraphError("secret_not_applicable: only a header connection has a secret value")
        if value in (None, ""):
            delete_secret(conn_id)
        else:
            if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise StateGraphError("invalid_secret")
            write_secret(conn_id, {"header_value": value})
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_steward_connections SET generation=generation+1,updated_at=? WHERE conn_id=?",
                         (_iso(_utcnow()), conn_id))
        tools_changed()
        return {"conn_id": conn_id, "secret": "set" if value else "unset"}

    def probe(self, conn_id):
        """Read the connection's tools (owner): every tool is written to the tool table,
        a new one off, a changed one out of the effective set until allowed again."""
        self._owner()
        return self._discover(conn_id)

    def refresh(self, conn_id):
        """The steward's re-read of one registered connection: discovery fields only, at
        most once a minute; never allowed, outward, visibility or enabled."""
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            row = self._row(conn, conn_id)
            if row["last_probe_at"] and _utcnow() - _parse(row["last_probe_at"]) < REFRESH_INTERVAL:
                raise StateGraphError("refresh_too_soon")
        return self._discover(conn_id)

    def _discover(self, conn_id):
        with self.store.transaction() as conn:
            row = dict(self._row(conn, conn_id))
        if PROBER is None:
            raise StateGraphError("probe_unavailable")
        try:
            listed = PROBER(row)
        except StateGraphError:
            raise
        except ProbeFailed as exc:
            self._probe_error(conn_id, exc.code)
            return {"conn_id": conn_id, "error": exc.code}
        except Exception as exc:  # noqa: BLE001 - kept as a code, never the text
            self._probe_error(conn_id, "probe_failed:" + type(exc).__name__)
            return {"conn_id": conn_id, "error": "probe_failed:" + type(exc).__name__}
        record_discovery(self.store, conn_id, listed.get("tools") or [], listed.get("instructions"))
        with self.store.transaction() as conn:
            return self._tools(conn, conn_id, instructions=self.steward.is_owner_session())

    def _probe_error(self, conn_id, code):
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_steward_connections SET last_probe_at=?, last_error=? WHERE conn_id=?",
                         (_iso(_utcnow()), code[:200], conn_id))

    # -- the steward host's writes
    def tools_resync_due(self, effective_hash):
        """The host saw tools of the effective set still missing from its registry a
        minute later: a restart marker `tools_resync:<hash>`, due now - at most one
        fallback restart per hash per day (then the owner is told once)."""
        if not isinstance(effective_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", effective_hash):
            raise StateGraphError("invalid_effective_hash")
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn)
            outcome = write_marker(conn, self.driver_id, RESYNC + effective_hash, effective_hash, _utcnow())
        return {"marker": outcome}

    # -- reads
    @writer_only_read("list_steward_connections")
    def list_connections(self):
        """Every connection (never a secret: `secret` is set / unset), the effective set's
        public names and hash (the same function the gateway serves), each connection's
        effective tools with their guard facts, and the fallback-restart state."""
        self._owner_or_steward()
        with self.store.transaction() as conn:
            entries = effective(conn)
            rows = conn.execute("SELECT * FROM state_steward_connections ORDER BY conn_id").fetchall()
            connections = []
            for row in rows:
                item = self._public(conn, row)
                item["tools"] = [{"name": public_name(e["conn_id"], e["gw_name"]), "raw_name": e["raw_name"],
                                  "gw_name": e["gw_name"], "egress": e["egress"], "outward": e["outward"],
                                  "blocked_args": e["blocked_args"], "daily_cap": e["daily_cap"]}
                                 for e in entries if e["conn_id"] == row["conn_id"]]
                connections.append(item)
            due = [dict(r) for r in conn.execute("SELECT driver_id,reason,due_at,created_at FROM state_steward_restart_due")]
            done = [{**dict(r), "reasons": json.loads(r["reasons_json"])} for r in conn.execute(
                "SELECT * FROM state_steward_restart_done WHERE restarted_at>=? ORDER BY id DESC LIMIT 20",
                (_iso(_utcnow() - RESTART_LIMIT_WINDOW),))]
            for d in done:
                d.pop("reasons_json", None)
        return {"connections": connections,
                "effective_tools": [public_name(e["conn_id"], e["gw_name"]) for e in entries],
                "effective_hash": effective_hash(entries), "restart": {"due": due, "done": done}}

    @writer_only_read("list_steward_connection_tools")
    def list_connection_tools(self, conn_id):
        self._owner_or_steward()
        with self.store.transaction() as conn:
            self._row(conn, conn_id)
            return self._tools(conn, conn_id, instructions=self.steward.is_owner_session())

    def _public(self, conn, row) -> dict:
        tools = conn.execute("SELECT allowed,outward,allowed_sha256,schema_sha256 FROM state_steward_connection_tools "
                             "WHERE conn_id=?", (row["conn_id"],)).fetchall()
        return {"conn_id": row["conn_id"], "label": row["label"], "url": row["url"], "auth_kind": row["auth_kind"],
                "header_name": row["header_name"], "visibility": row["visibility"], "enabled": bool(row["enabled"]),
                "generation": row["generation"], "config_version": row["config_version"],
                "secret": secret_status(row), "auth_identity": row["auth_identity"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "last_probe_at": row["last_probe_at"], "last_error": row["last_error"],
                "last_change": row["last_change"], "usable": connection_usable(row),
                "tool_counts": {"all": len(tools), "allowed": sum(1 for t in tools if t["allowed"]),
                                "outward": sum(1 for t in tools if t["outward"]),
                                "changed": sum(1 for t in tools if t["allowed_sha256"]
                                               and t["allowed_sha256"] != t["schema_sha256"]),
                                "new": sum(1 for t in tools if not t["allowed"] and not t["allowed_sha256"])}}

    def _tools(self, conn, conn_id, *, instructions=True) -> dict:
        """One connection's tools. The server's `instructions` text is the owner's panel
        alone, never the steward's context (7.8.4)."""
        row = self._row(conn, conn_id)
        rows = conn.execute("SELECT * FROM state_steward_connection_tools WHERE conn_id=? ORDER BY raw_name",
                            (conn_id,)).fetchall()
        lost = _collisions(rows)
        tools = []
        for t in rows:
            definition = json.loads(t["definition_json"])
            state = ("collision" if (t["conn_id"], t["raw_name"]) in lost
                     else "changed" if t["allowed_sha256"] and t["allowed_sha256"] != t["schema_sha256"]
                     else "allowed" if t["allowed"] else "new" if not t["allowed_sha256"] else "off")
            tools.append({"raw_name": t["raw_name"], "gw_name": t["gw_name"],
                          "name": public_name(conn_id, t["gw_name"]),
                          "description": definition.get("description", ""), "input_schema": definition.get("inputSchema"),
                          "annotations": definition.get("annotations"), "schema_sha256": t["schema_sha256"],
                          "allowed": bool(t["allowed"]), "outward": bool(t["outward"]), "egress": bool(t["egress"]),
                          "blocked_args": json.loads(t["blocked_args"]), "daily_cap": t["daily_cap"],
                          "first_seen_at": t["first_seen_at"], "last_seen_at": t["last_seen_at"], "state": state})
        answer = {"conn_id": conn_id, "instructions": row["instructions"], "last_probe_at": row["last_probe_at"],
                  "last_error": row["last_error"], "tools": tools}
        if not instructions:
            del answer["instructions"]
        return answer


def ensure_schema(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def record_discovery(store, conn_id: str, listed: list, instructions=None) -> dict:
    """Store what a connection's server listed (probe, refresh, an upstream session
    opening or announcing list_changed): every tool's canonical definition and hash;
    a NEW tool off, prefilled outward unless readOnlyHint is true, egress on, and on
    a public connection its url / *_url parameters blocked. Existing rows keep every
    owner field; a changed definition simply no longer equals allowed_sha256.
    A tool the server no longer lists (removed, or renamed: a rename is a removal and a
    new tool) is deleted with its owner fields, so it leaves the effective set; if it
    is listed again it is a NEW tool, off until the owner allows it again. The call
    ledger keeps its past calls. `listed` must be the server's whole list."""
    moment = _iso(_utcnow())
    parsed = {}
    for tool in listed:
        try:
            text = definition_json(tool)
        except (ValueError, UnicodeEncodeError):
            continue
        parsed.setdefault(definition_of(tool)["name"], (text, definition_of(tool)))
    changed = False
    with store.transaction(write=True) as conn:
        row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
        if row is None:
            return {"conn_id": conn_id, "gone": True}
        before = current_hash(conn)
        for raw, (text, definition) in parsed.items():
            digest = sha256_text(text)
            old = conn.execute("SELECT schema_sha256 FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?",
                               (conn_id, raw)).fetchone()
            if old is None:
                annotations = definition.get("annotations") or {}
                outward = 0 if annotations.get("readOnlyHint") is True else 1
                blocked = _url_params(definition["inputSchema"]) if row["visibility"] == "public" else []
                conn.execute("INSERT INTO state_steward_connection_tools(conn_id,raw_name,gw_name,definition_json,"
                             "schema_sha256,allowed,allowed_sha256,outward,egress,blocked_args,daily_cap,first_seen_at,"
                             "last_seen_at) VALUES(?,?,?,?,?,0,NULL,?,1,?,NULL,?,?)",
                             (conn_id, raw, gw_name(conn_id, raw), text, digest, outward, json.dumps(blocked),
                              moment, moment))
            else:
                conn.execute("UPDATE state_steward_connection_tools SET definition_json=?, schema_sha256=?, "
                             "last_seen_at=? WHERE conn_id=? AND raw_name=?", (text, digest, moment, conn_id, raw))
        for (raw,) in conn.execute("SELECT raw_name FROM state_steward_connection_tools WHERE conn_id=?",
                                   (conn_id,)).fetchall():
            if raw not in parsed:
                conn.execute("DELETE FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?", (conn_id, raw))
        conn.execute("UPDATE state_steward_connections SET last_probe_at=?, last_error=NULL, instructions=? "
                     "WHERE conn_id=?", (moment, (instructions or None) and str(instructions)[:8192], conn_id))
        changed = current_hash(conn) != before
    if changed:
        tools_changed()
    return {"conn_id": conn_id, "listed": len(parsed)}


PENDING = "pending"  # the rpc_error of a ledger row reserved before its upstream call


def reserve_call(conn, conn_id, raw_name, daily_cap, moment: datetime | None = None) -> int | None:
    """On the caller's WRITE transaction (BEGIN IMMEDIATE, so the count and the insert are
    one step): None when the tool's daily cap is reached, counting today's rows reserved
    and not yet finished too; else a ledger row reserved now (rpc_error `pending`) and
    its id, which `finish_call` fills in. A row left pending (the process died during
    the call) keeps counting: the call may have run."""
    moment = moment or _utcnow()
    if daily_cap is not None and calls_today(conn, conn_id, raw_name, moment) >= daily_cap:
        return None
    conn.execute("DELETE FROM state_steward_tool_calls WHERE at<?", (_iso(moment - TOOL_CALLS_KEPT),))
    return conn.execute("INSERT INTO state_steward_tool_calls(conn_id,raw_name,at,local_day,rpc_error) "
                        "VALUES(?,?,?,?,?)", (conn_id, raw_name, _iso(moment), _local_day(moment), PENDING)).lastrowid


def finish_call(store, call_id, *, rpc_error=None, is_error=False, request_bytes=0,
                response_bytes=0, duration_ms=0) -> None:
    """Complete one reserved ledger line: never the arguments or the result body."""
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE state_steward_tool_calls SET rpc_error=?,is_error=?,request_bytes=?,response_bytes=?,"
                     "duration_ms=? WHERE id=?", (rpc_error, int(bool(is_error)), request_bytes, response_bytes,
                                                  duration_ms, call_id))


def calls_today(conn, conn_id, raw_name, moment: datetime | None = None) -> int:
    return conn.execute("SELECT COUNT(*) FROM state_steward_tool_calls WHERE conn_id=? AND raw_name=? AND local_day=?",
                        (conn_id, raw_name, _local_day(moment or _utcnow()))).fetchone()[0]


def audience_of(conn) -> str | None:
    """The audience (`owner` or `group`) of the steward's turn as its host last recorded it:
    step 23's one-row record (core.state_groups.set_steward_turn_audience), written before
    each turn's first step runs; a failed write rejects that step, so no tool of the turn
    runs on the previous turn's row. A gateway request carries no turn (design-steward
    7.7.8a), so this reads the row alone; no row (or no table yet) is None, which the gateway
    treats as not the owner's turn (fail closed)."""
    try:
        row = conn.execute("SELECT audience FROM state_steward_turn_audience WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None
