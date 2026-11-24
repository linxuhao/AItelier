"""The steward's basic capabilities (step 24.4, design-steward 7.8.24).

A FIXED list of jobs (`CAPABILITIES`, the owner's "fixed list"); for each the owner may
store one ordered ROUTE on the dashboard: `native`, `missing`, or one to eight tools - a
host tool of the steward's own or any tool of any registered connection - the first the
main one. One row per job in `state_steward_capabilities`; no row, or a row whose route is
`null` (unset or cleared), = not set = today (the steward finds its tools with search_tools
as before). Unset and clear keep the row at the next version, so a row's version only ever
rises and a write or proposal made at an older version is refused.

Two readings never mix:
- DETECTION (api/steward_routers.py: the native facts and the exact tool name `tool`) is
  only a grey suggestion on the panel and never reads a setting;
- the SETTING is what the steward is told (`for_steward`, a `basics` field on
  list_steward_connections, only while at least one row is set).

Writes are the owner session's alone (`StewardCapabilities.set` / `clear`; never on the
steward's write whitelist). The steward may only PROPOSE a route (`capability_route`,
`validate_proposal` / `prepare_proposal` / `execute_proposal`, the same three steps as
connection_change): the proposal names tools by the gateway names the steward sees, is
checked against the effective set at creation and again against the registry at approval.

A pinned connection tool is kept by (conn_id, conn_uid, raw_name): nothing here ever
rewrites a stored route. A pin whose connection or tool is gone - or whose conn_id now
belongs to another connection (a new conn_uid) - stays as it is and reads `tool_gone`.

Nothing upstream-written (a connection's label, a tool's description) reaches the steward
from here: `for_steward` carries only the constants below, kinds, host tool names,
visibilities and gateway names for the plugin's `basics:<id>` lookup; a proposal's
description snapshot (`card`) is the dashboard's alone.

The table is private (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from core.state_graph import StateConflict, StateGraphError, canonical, now
from core.state_privacy import UntrustedDatabase, writer_only_read

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_capabilities (
 cap_id TEXT PRIMARY KEY,
 route_json TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),
 set_via TEXT NOT NULL CHECK(set_via IN ('dashboard','proposal')),
 proposal_id TEXT,
 updated_at TEXT NOT NULL);
"""

# The owner's fixed list (2026-11-23), in display and rendering order. `label` is only the
# fallback name (the web renders steward.cap.<id>, the plugin `desc`); `tool` and
# `native_fact` are what DETECTION looks for. The other fields are the route editor's and
# the steward's (design 7.8.24 "三").
CAPABILITIES = (
    {"id": "vision", "label": "看图 (Vision)", "tool": None, "native_fact": "steward_image_route",
     "allows": ("native", "missing"), "desc": "image understanding, see images", "host_tools": (),
     "host_check": None, "native_tools": "you see images in your wake", "private_only": False, "missing_hint": None},
    {"id": "ocr", "label": "图片转文字 (OCR)", "tool": "ocr_image", "native_fact": "steward_image_route",
     "allows": ("native", "conn", "missing"), "desc": "text recognition, OCR, image text", "host_tools": (),
     "host_check": None, "native_tools": "you read the text in the images you see", "private_only": False,
     "missing_hint": None},
    {"id": "pdf", "label": "读 PDF", "tool": "ocr_pdf", "native_fact": "pdf_reader",
     "allows": ("native", "conn", "missing"), "desc": "PDF reading, PDF text", "host_tools": (),
     "host_check": None, "native_tools": "read_document", "private_only": False, "missing_hint": None},
    {"id": "office_read", "label": "读 Word/Excel/PPT", "tool": None, "native_fact": None,
     "allows": ("host", "conn", "missing"), "desc": "Word Excel PowerPoint reading, docx xlsx pptx",
     "host_tools": ("read_document",), "host_check": "make_document", "native_tools": None,
     "private_only": False, "missing_hint": None},
    {"id": "office_write", "label": "生成 Office 文档", "tool": None, "native_fact": None,
     "allows": ("host", "conn", "missing"), "desc": "Word Excel PowerPoint creation, docx xlsx pptx",
     "host_tools": ("make_document",), "host_check": None, "native_tools": None, "private_only": False,
     "missing_hint": None},
    {"id": "transcribe", "label": "语音转文字", "tool": "transcribe_audio", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "speech recognition, transcription, audio text", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "tts", "label": "发语音条", "tool": "generate_speech", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "speech synthesis, voice message, TTS", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "draw", "label": "画图", "tool": "generate_image", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "image generation, picture, drawing", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "music", "label": "生成音乐", "tool": "generate_music", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "music generation, song", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "sfx", "label": "音效", "tool": "gen_sfx", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "sound effect, SFX", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "video", "label": "视频", "tool": None, "native_fact": None,
     "allows": ("conn", "missing"), "desc": "video generation, video editing", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "search", "label": "联网搜索", "tool": "web_search", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "web search, search engine", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "fetch", "label": "读网页", "tool": "web_fetch", "native_fact": None,
     "allows": ("conn", "missing"), "desc": "web page, URL reading, fetch", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "email", "label": "邮件", "tool": None, "native_fact": None,
     "allows": ("conn", "missing"), "desc": "email, mail, inbox", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "calendar", "label": "日历", "tool": None, "native_fact": None,
     "allows": ("conn", "missing"), "desc": "calendar, events, appointments", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False,
     "missing_hint": "Reminders (schedule_*, group_reminder_set) are your own and are not a calendar."},
    {"id": "cloud_files", "label": "网盘", "tool": None, "native_fact": None,
     "allows": ("conn", "missing"), "desc": "cloud drive, files, storage", "host_tools": (),
     "host_check": None, "native_tools": None, "private_only": False, "missing_hint": None},
    {"id": "memory", "label": "记忆与历史对话", "tool": None, "native_fact": "plugin_builtin",
     "allows": ("native", "conn"), "desc": "memory, notes, past conversations", "host_tools": (),
     "host_check": None, "native_tools": "memory_* in owner turns, family_memory_* in group turns",
     "private_only": True, "missing_hint": None},
    {"id": "code", "label": "计算/跑代码", "tool": None, "native_fact": None,
     "allows": ("host", "conn", "missing"), "desc": "code execution, Python, calculation",
     "host_tools": ("calculate",), "host_check": None, "native_tools": None, "private_only": False,
     "missing_hint": None},
)
BY_ID = {cap["id"]: cap for cap in CAPABILITIES}
HOST_TOOLS = frozenset(tool for cap in CAPABILITIES for tool in cap["host_tools"])
# Tools that write state shared beyond one conversation (7.8.11's "never allow on a public
# agentmcp", video_* excepted by the owner 2026-11-24): pinned on a private connection only.
SHARED_STATE_TOOLS = re.compile(r"^(actor_tts|create_[a-z_]+|delete_[a-z_]+)$")
MAX_ENTRIES = 8
# Step 24.4c (7.8.24 "九" 5): the dev-mcp tools the owner may pin - on `code` alone, from the
# dashboard alone (a proposal naming one is `needs_dashboard: dev_mcp`). The gateway forwards
# a dev-mcp call only for a tool pinned there (`code_pin`); subagent_* is never one of them.
DEV_MCP_PINNABLE = ("list_dir", "read_file", "search", "bash", "apply_patch")
DEV_MCP_CAPABILITY = "code"
NEEDS_DASHBOARD_DEV_MCP = "needs_dashboard: dev_mcp"
CARD_DESCRIPTION_CHARS = 500
# Decision 38 (the draft's recommendation): at most one open capability_route per job, at
# most five open in all, and a route the owner declined is not proposed again for 30 days.
MAX_OPEN_PROPOSALS = 5
DECLINED_QUIET = timedelta(days=30)
KIND = "capability_route"
OP_SET, OP_CLEAR = "steward_capability_set", "steward_capabilities_clear"
# The route of a row unset or cleared: not set, its version kept (never back to 0).
NOT_SET = "null"
# A row's colour per entry code (design 7.8.24 "八").
_COLOR = {"ok": "green", "connection_error": "yellow", "see_driver_status": "grey", "missing": "grey"}


def ensure_schema(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_steward_capabilities'"
                        ).fetchone() is not None


def _refused(code, detail=None) -> StateGraphError:
    """A refusal is its code (plus an entry number): never a name or a description."""
    return StateGraphError(f"{code}: {detail}" if detail else code)


def _spec(cap_id):
    cap = BY_ID.get(cap_id)
    if cap is None:
        raise _refused("unknown_capability")
    return cap


def _stored(conn) -> dict:
    """{cap_id: row} of the rows set for a job still on the list (others are ignored)."""
    if not _has_table(conn):
        return {}
    return {r["cap_id"]: r for r in conn.execute("SELECT * FROM state_steward_capabilities")
            if r["cap_id"] in BY_ID and r["route_json"] != NOT_SET}


def _versions(conn) -> dict:
    """{cap_id: version} of every row, an unset one included: what a write must expect."""
    if not _has_table(conn):
        return {}
    return {r["cap_id"]: r["version"] for r in conn.execute("SELECT cap_id,version FROM state_steward_capabilities")}


# ---------------------------------------------------------------- the route
def check_route(cap_id, route, conn, via="dashboard") -> list[dict]:
    """The stored form of `route` for `cap_id`, or the refusal's code (design "四"). Entries:
    {kind: native} | {kind: missing} | {kind: host, tool} | {kind: conn, conn_id, raw_name,
    conn_uid?}. A connection entry is resolved against the registry as it is NOW, its
    conn_uid taken from it (a conn_uid given must be that one). `via` is who writes it
    (`dashboard` or `proposal`): the same rules for both, except a dev-mcp tool (24.4c):
    a proposal naming one is `needs_dashboard: dev_mcp`; the dashboard may pin only
    DEV_MCP_PINNABLE, only on `code` (else `dev_mcp_not_allowed`)."""
    from core import state_steward_connections as sc
    cap = _spec(cap_id)
    if via not in ("dashboard", "proposal"):
        raise ValueError(via)
    if not isinstance(route, list) or not route:
        raise _refused("route_empty")
    if len(route) > MAX_ENTRIES:
        raise _refused("too_many_entries")
    kinds = [e.get("kind") if isinstance(e, dict) else None for e in route]
    for index, (entry, kind) in enumerate(zip(route, kinds), 1):
        # A host entry is judged by its tool below (host_tool_not_allowed): a job without
        # host tools allows none.
        if kind not in cap["allows"] and kind != "host":
            raise _refused("entry_not_allowed", f"entry {index}")
        if kind == "missing" and len(route) > 1:
            raise _refused("entry_not_allowed", f"entry {index}: missing stands alone")
        if kind == "native" and index != 1:
            raise _refused("entry_not_allowed", f"entry {index}: native is first")
        fields = {"native": {"kind"}, "missing": {"kind"}, "host": {"kind", "tool"},
                  "conn": {"kind", "conn_id", "raw_name", "conn_uid"}}[kind]
        required = fields - {"conn_uid"}
        if set(entry) - fields or required - set(entry) or any(
                not isinstance(entry[k], str) or not entry[k] for k in set(entry) - {"kind"}):
            raise _refused("entry_not_allowed", f"entry {index}: malformed")
    if cap["id"] == "memory" and kinds[0] != "native":
        raise _refused("entry_not_allowed", "entry 1: memory starts with native")
    out, seen = [], set()
    for index, entry in enumerate(route, 1):
        kind = entry["kind"]
        if kind in ("native", "missing"):
            out.append({"kind": kind})
            continue
        if kind == "host":
            if entry["tool"] not in cap["host_tools"]:
                raise _refused("host_tool_not_allowed", f"entry {index}")
            same = ("host", entry["tool"])
            item = {"kind": "host", "tool": entry["tool"]}
        else:
            row = conn.execute("SELECT conn_uid,visibility,url FROM state_steward_connections WHERE conn_id=?",
                               (entry["conn_id"],)).fetchone() if sc._has_tables(conn) else None
            if row is None or entry.get("conn_uid", row["conn_uid"]) != row["conn_uid"]:
                raise _refused("unknown_connection", f"entry {index}")
            if conn.execute("SELECT 1 FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?",
                            (entry["conn_id"], entry["raw_name"])).fetchone() is None:
                raise _refused("unknown_tool", f"entry {index}")
            dev = sc.is_dev_mcp(row)
            if dev and via == "proposal":
                raise StateGraphError(NEEDS_DASHBOARD_DEV_MCP)
            if dev and (cap["id"] != DEV_MCP_CAPABILITY or entry["raw_name"] not in DEV_MCP_PINNABLE):
                raise _refused("dev_mcp_not_allowed", f"entry {index}")
            if row["visibility"] != "private" and (dev or cap["private_only"] or SHARED_STATE_TOOLS.match(entry["raw_name"])):
                raise _refused("pin_must_be_private", f"entry {index}")
            same = ("conn", entry["conn_id"], entry["raw_name"])
            item = {"kind": "conn", "conn_id": entry["conn_id"], "conn_uid": row["conn_uid"],
                    "raw_name": entry["raw_name"]}
        if same in seen:
            raise _refused("duplicate_entry", f"entry {index}")
        seen.add(same)
        out.append(item)
    return out


def entry_kinds(entries) -> list[str]:
    """What the steward may read of a route: each entry's kind, a host tool by its name."""
    return [f"host:{e['tool']}" if e["kind"] == "host" else e["kind"] for e in entries]


def route_target(cap_id, entries) -> str:
    """`draw ← media/generate_image → other/paint_v2`: the owner's channel line (never the
    steward's context)."""
    names = [e["tool"] if e["kind"] == "host" else f"{e['conn_id']}/{e['raw_name']}" if e["kind"] == "conn"
             else e["kind"] for e in entries]
    return f"{cap_id} ← " + " → ".join(names)


def _route_key(entries) -> list:
    """A route as the owner sees it (a pin by connection and tool name, never its conn_uid)."""
    return [[e["kind"], e.get("tool"), e.get("conn_id"), e.get("raw_name")] for e in entries]


def code_pin(conn, conn_uid, raw_name) -> bool:
    """Whether the owner's route for `code` pins the tool `raw_name` of the connection
    `conn_uid` (the gateway's condition for any dev-mcp call, 24.4c): a job not set, set
    `missing`, or pinning other tools - or another conn_uid - is no."""
    row = _stored(conn).get(DEV_MCP_CAPABILITY)
    return row is not None and raw_name in DEV_MCP_PINNABLE and any(
        e.get("kind") == "conn" and e.get("conn_uid") == conn_uid and e.get("raw_name") == raw_name
        for e in json.loads(row["route_json"]))


def private_pins(conn, conn_uid) -> bool:
    """Whether a stored route pins, on the connection `conn_uid`, a tool that must stay on a
    private connection (a `private_only` job's entry or a shared-state tool): the
    connection may then not be made public (`pinned_private_only`)."""
    for row in _stored(conn).values():
        cap = BY_ID[row["cap_id"]]
        for e in json.loads(row["route_json"]):
            if e.get("kind") == "conn" and e.get("conn_uid") == conn_uid and (
                    cap["private_only"] or SHARED_STATE_TOOLS.match(e.get("raw_name", ""))):
                return True
    return False


# ---------------------------------------------------------------- status (read time)
class _Facts:
    """The registry as one read transaction sees it, for judging pinned entries."""

    def __init__(self, conn):
        from core import state_steward_connections as sc
        self.sc = sc
        if not sc._has_tables(conn):
            self.connections, self.tools, self.lost = {}, {}, set()
            return
        self.connections = {r["conn_id"]: r for r in conn.execute("SELECT * FROM state_steward_connections")}
        rows = conn.execute("SELECT * FROM state_steward_connection_tools").fetchall()
        self.tools = {(r["conn_id"], r["raw_name"]): r for r in rows}
        self.lost = sc._collisions(rows)

    def code(self, entry, cap_id) -> str:
        """One entry's reason code (design "八") on the job `cap_id`; `ok` and
        `connection_error` are usable."""
        if entry["kind"] in ("native", "host"):
            return "see_driver_status"
        if entry["kind"] == "missing":
            return "missing"
        row = self.connections.get(entry["conn_id"])
        tool = self.tools.get((entry["conn_id"], entry["raw_name"]))
        if row is None or row["conn_uid"] != entry["conn_uid"] or tool is None:
            return "tool_gone"
        if self.sc.is_dev_mcp(row) and (cap_id != DEV_MCP_CAPABILITY or entry["raw_name"] not in DEV_MCP_PINNABLE
                                        or row["visibility"] != "private"):
            # A pin no command writes but a connection moved onto dev_mcp keeps (24.4c): the
            # gateway refuses it, so it is never shown usable.
            return "tool_not_allowed"
        if not tool["allowed"]:
            return "tool_not_allowed"
        if tool["allowed_sha256"] != tool["schema_sha256"]:
            return "tool_changed"
        if (entry["conn_id"], entry["raw_name"]) in self.lost:
            return "tool_collision"
        if not self.sc.connection_usable(row):
            return "connection_off"
        return "connection_error" if row["last_error"] else "ok"

    def visibility(self, entry):
        """The pinned connection's visibility, or None when it is gone (or another conn_uid)."""
        row = self.connections.get(entry["conn_id"])
        return row["visibility"] if row is not None and row["conn_uid"] == entry["conn_uid"] else None


def derive_status(codes) -> dict:
    """{dot, reasons} from one code per route entry (in order): the dot is the worst before
    the first usable entry - first usable: its colour; a later one usable: yellow, and the
    reasons start with `fallback_in_use`; none usable: red. Not set: grey `not_set`."""
    if codes is None:
        return {"dot": "grey", "reasons": ["not_set"]}
    usable = [i for i, code in enumerate(codes) if _COLOR.get(code, "red") != "red"]
    fallback = bool(usable) and usable[0] > 0
    dot = "red" if not usable else "yellow" if fallback else _COLOR[codes[0]]
    return {"dot": dot, "reasons": (["fallback_in_use"] if fallback else []) + list(codes)}


def for_steward(conn):
    """The `basics` field of list_steward_connections, or None while no job is set (the
    steward is then exactly as before). Rows: only the jobs set, in list order, each entry
    in route order: {kind, tool?, visibility?, pinned_name?, available}. `available` reads
    configuration only (pinned, same conn_uid, allowed, unchanged, not out-named, the
    connection usable); `pinned_name` is the gateway name the plugin's basics:<id> lookup
    uses, given only for an entry in the effective set. Never a connection's label or a
    tool's description."""
    stored = _stored(conn)
    if not stored:
        return None
    facts = _Facts(conn)
    rows = []
    for cap in CAPABILITIES:
        row = stored.get(cap["id"])
        if row is None:
            continue
        route = []
        for e in json.loads(row["route_json"]):
            item = {"kind": e["kind"]}
            if e["kind"] == "host":
                item["tool"] = e["tool"]
            if e["kind"] == "conn":
                code = facts.code(e, cap["id"])
                item["visibility"] = facts.visibility(e)
                usable = code in ("ok", "connection_error")
                tool = facts.tools.get((e["conn_id"], e["raw_name"]))
                item["pinned_name"] = facts.sc.public_name(e["conn_id"], tool["gw_name"]) if usable else None
                item["available"] = usable
            else:
                item["available"] = e["kind"] != "missing"
            route.append(item)
        rows.append({"id": cap["id"], "route": route})
    return {"rows": rows}


# ---------------------------------------------------------------- the service
class StewardCapabilities:
    """The owner's two writes and the dashboard's read (design 7.8.24 "四")."""

    def __init__(self, store, actor, driver_id=None, steward=None):
        self.store, self.actor, self.driver_id, self.steward = store, actor, driver_id, steward
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            # Once per process per database file (core.state_schema_setup).
            from core.state_schema_setup import once
            once(store.db, "steward_capabilities", ensure_schema, store.db)

    def _owner(self):
        if self.steward is None or not self.steward.is_owner_session():
            raise StateGraphError("owner_session_required")

    def set(self, request_key, cap_id, expect_version, route=None, unset=False):
        """Write one job's route (or remove it: `unset`) as of the row version the panel read
        (0: no row); another version is `version_conflict`. Unset keeps the row, route
        `null`, at the next version. A connection entry names
        conn_id and raw_name; its conn_uid is the connection's now. The same request_key
        answers the same result; with another content it is refused."""
        self._owner()
        if (route is None) == (not unset):
            raise StateGraphError("invalid_request: give route, or unset: true")
        from core.state_steward import _hash
        request_hash = _hash([cap_id, expect_version, route, bool(unset)])
        with self.store.transaction(write=True) as conn:
            replay = self.steward._replay(conn, OP_SET, request_key, request_hash)
            if replay is not None:
                return replay
            _spec(cap_id)
            current = conn.execute("SELECT version FROM state_steward_capabilities WHERE cap_id=?",
                                   (cap_id,)).fetchone()
            if (current[0] if current else 0) != expect_version:
                raise StateConflict("version_conflict")
            # A job no longer on the list goes with the next write.
            conn.execute(f"DELETE FROM state_steward_capabilities WHERE cap_id NOT IN "
                         f"({','.join('?' * len(BY_ID))})", tuple(BY_ID))
            if unset:
                conn.execute("UPDATE state_steward_capabilities SET route_json=?, version=version+1, "
                             "set_via='dashboard', proposal_id=NULL, updated_at=? WHERE cap_id=?",
                             (NOT_SET, now(), cap_id))
                result = {"cap_id": cap_id, "setting": None}
            else:
                entries = check_route(cap_id, route, conn, via="dashboard")
                version = expect_version + 1
                conn.execute("INSERT INTO state_steward_capabilities(cap_id,route_json,version,set_via,proposal_id,"
                             "updated_at) VALUES(?,?,?,'dashboard',NULL,?) ON CONFLICT(cap_id) DO UPDATE SET "
                             "route_json=excluded.route_json, version=excluded.version, set_via='dashboard', "
                             "proposal_id=NULL, updated_at=excluded.updated_at",
                             (cap_id, canonical(entries), version, now()))
                result = {"cap_id": cap_id, "setting": {"route": entries, "version": version, "set_via": "dashboard"}}
            return self.steward._record(conn, OP_SET, request_key, request_hash, result)

    def clear(self, request_key):
        """Every job not set: the table as never set (the stop switch, design "十七"); each
        set row is kept unset at its next version."""
        self._owner()
        from core.state_steward import _hash
        request_hash = _hash(["clear"])
        with self.store.transaction(write=True) as conn:
            replay = self.steward._replay(conn, OP_CLEAR, request_key, request_hash)
            if replay is not None:
                return replay
            conn.execute(f"DELETE FROM state_steward_capabilities WHERE cap_id NOT IN "
                         f"({','.join('?' * len(BY_ID))})", tuple(BY_ID))
            cleared = conn.execute("UPDATE state_steward_capabilities SET route_json=?, version=version+1, "
                                   "set_via='dashboard', proposal_id=NULL, updated_at=? WHERE route_json!=?",
                                   (NOT_SET, now(), NOT_SET)).rowcount
            return self.steward._record(conn, OP_CLEAR, request_key, request_hash, {"cleared": cleared})

    @writer_only_read("steward_capabilities")
    def rows(self) -> dict:
        """{cap_id: {setting, version, proposal, status, spec}} for every job on the list (the
        GET /api/steward/capabilities additions; detection is the route's own). `version` is
        the row version a write expects (0: never written; an unset row keeps its own). Each
        entry of a setting's route carries its status `reason`, a connection entry its
        `visibility`."""
        self._owner()
        with self.store.transaction() as conn:
            stored, versions, facts = _stored(conn), _versions(conn), _Facts(conn)
            proposals = {}
            for row in conn.execute("SELECT * FROM state_steward_proposals WHERE kind=? AND status='open' "
                                    "ORDER BY created_at", (KIND,)):
                payload = json.loads(row["payload_json"])
                proposals[payload.get("cap_id")] = {
                    "proposal_id": row["proposal_id"], "version": row["version"], "route": payload.get("entries"),
                    "reason": row["reason"], "source": payload.get("source")}
            out = {}
            for cap in CAPABILITIES:
                row = stored.get(cap["id"])
                entries = json.loads(row["route_json"]) if row else []
                codes = [facts.code(e, cap["id"]) for e in entries]
                route = [{**e, "reason": code, **({"visibility": facts.visibility(e)} if e["kind"] == "conn" else {})}
                         for e, code in zip(entries, codes)]
                out[cap["id"]] = {
                    "setting": {"route": route, "version": row["version"], "set_via": row["set_via"]} if row else None,
                    "version": versions.get(cap["id"], 0),
                    "proposal": proposals.get(cap["id"]),
                    "status": derive_status(codes if row else None),
                    "spec": {"allows": list(cap["allows"]), "host_tools": list(cap["host_tools"]),
                             "private_only": cap["private_only"]}}
        return out


# ---------------------------------------------------------------- the steward's proposal
def validate_proposal(payload) -> dict:
    """The payload as the model may write it: {cap_id, route: 1-8 strings ("native",
    "missing", a host tool name, or a connection tool's gateway name exactly as listed),
    source: {kind: owner_message, ref} | {kind: tool_search}}. An unknown key is
    `invalid_payload`."""
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field, ValidationError

    class Source(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        kind: Literal["owner_message", "tool_search"]
        ref: str | None = Field(default=None, min_length=1, max_length=320)

    class Route(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        cap_id: str = Field(min_length=1, max_length=40)
        route: list[str] = Field(min_length=1, max_length=MAX_ENTRIES)
        source: Source

    if not isinstance(payload, dict):
        raise StateGraphError("invalid_payload: payload must be an object")
    try:
        parsed = Route.model_validate(payload)
    except ValidationError as exc:
        details = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]}
                   for e in exc.errors(include_input=False)[:10]]
        raise StateGraphError(f"invalid_payload: {details}") from None
    if any(not name or len(name) > 200 for name in parsed.route):
        raise StateGraphError("invalid_payload: a route entry is 1-200 characters")
    source = parsed.source
    if (source.kind == "owner_message") != (source.ref is not None):
        raise StateGraphError("invalid_payload: an owner_message source names its ref; tool_search has none")
    return {"cap_id": parsed.cap_id, "route": list(parsed.route),
            "source": {"kind": source.kind, **({"ref": source.ref} if source.ref is not None else {})}}


def _parse(value) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def prepare_proposal(conn, payload, home, supersedes=None) -> dict:
    """The stored payload (on create_proposal's write transaction): each gateway name
    resolved in the EFFECTIVE set only (what the steward can see), the route checked
    (`check_route`), the row version frozen (`expect_version`), the source read from the
    database, and the card's description snapshot (`card`, the dashboard's alone)."""
    from core import state_steward_connection_proposals as ccp
    from core import state_steward_connections as sc
    cap = _spec(payload["cap_id"])
    effective = {sc.public_name(e["conn_id"], e["gw_name"]): e for e in sc.effective(conn)}
    entries, card = [], []
    for index, name in enumerate(payload["route"], 1):
        if name in ("native", "missing"):
            entries.append({"kind": name})
        elif name in HOST_TOOLS:
            entries.append({"kind": "host", "tool": name})
        elif name in effective:
            found = effective[name]
            entries.append({"kind": "conn", "conn_id": found["conn_id"], "raw_name": found["raw_name"]})
            card.append({"conn_id": found["conn_id"], "raw_name": found["raw_name"],
                         "description": str(found["tool"].get("description", ""))[:CARD_DESCRIPTION_CHARS]})
        else:
            raise _refused("unknown_tool", f"entry {index}")
    entries = check_route(cap["id"], entries, conn, via="proposal")
    stamp = datetime.now(timezone.utc)
    open_rows = [(r["proposal_id"], json.loads(r["payload_json"]).get("cap_id")) for r in conn.execute(
        "SELECT proposal_id,payload_json FROM state_steward_proposals WHERE kind=? AND status='open'", (KIND,))]
    open_rows = [(pid, job) for pid, job in open_rows if pid != supersedes]
    if any(job == cap["id"] for _, job in open_rows):
        raise _refused("proposal_open_for_capability")
    for r in conn.execute("SELECT payload_json,decided_at FROM state_steward_proposals WHERE kind=? AND "
                          "status='declined'", (KIND,)):
        old = json.loads(r["payload_json"])
        if (old.get("cap_id") == cap["id"] and r["decided_at"] and stamp - _parse(r["decided_at"]) < DECLINED_QUIET
                and _route_key(old.get("entries", [])) == _route_key(entries)):
            raise _refused("route_declined_recently")
    if len(open_rows) >= MAX_OPEN_PROPOSALS:
        raise _refused("too_many_open_proposals")
    source = (ccp.source_of(conn, payload["source"], home) if payload["source"]["kind"] == "owner_message"
              else {"kind": "tool_search"})
    current = conn.execute("SELECT version FROM state_steward_capabilities WHERE cap_id=?", (cap["id"],)).fetchone()
    return {"cap_id": cap["id"], "entries": entries, "expect_version": current[0] if current else 0,
            "source": source, "card": card}


class ExecutionRefused(StateGraphError):
    """The frozen route cannot be written as it stands; nothing was written. `code` only."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def execute_proposal(conn, payload, proposal_id) -> dict:
    """Write the frozen route on the executor's write transaction: the row still at the
    version frozen (else `route_changed`), every pinned connection and tool still there
    under the conn_uid frozen (else `tool_gone`), the route checked again against the
    registry NOW (`check_route`: a connection made public since refuses a private-only pin).
    The result names kinds only: it goes to the steward's inbox."""
    cap_id, entries = payload["cap_id"], payload["entries"]
    current = conn.execute("SELECT version FROM state_steward_capabilities WHERE cap_id=?", (cap_id,)).fetchone()
    if (current[0] if current else 0) != payload["expect_version"]:
        raise ExecutionRefused("route_changed")
    for e in entries:
        if e["kind"] != "conn":
            continue
        row = conn.execute("SELECT conn_uid FROM state_steward_connections WHERE conn_id=?", (e["conn_id"],)).fetchone()
        if row is None or row[0] != e["conn_uid"] or conn.execute(
                "SELECT 1 FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?",
                (e["conn_id"], e["raw_name"])).fetchone() is None:
            raise ExecutionRefused("tool_gone")
    try:
        checked = check_route(cap_id, entries, conn, via="proposal")
    except StateGraphError as exc:
        # A code alone - but dev-mcp's refusal keeps what it is (24.4c).
        raise ExecutionRefused(NEEDS_DASHBOARD_DEV_MCP if str(exc) == NEEDS_DASHBOARD_DEV_MCP
                               else str(exc).split(":")[0]) from None
    version = payload["expect_version"] + 1
    conn.execute("INSERT INTO state_steward_capabilities(cap_id,route_json,version,set_via,proposal_id,updated_at) "
                 "VALUES(?,?,?,'proposal',?,?) ON CONFLICT(cap_id) DO UPDATE SET route_json=excluded.route_json, "
                 "version=excluded.version, set_via='proposal', proposal_id=excluded.proposal_id, "
                 "updated_at=excluded.updated_at", (cap_id, canonical(checked), version, proposal_id, now()))
    return {"cap_id": cap_id, "version": version, "entries": entry_kinds(checked)}
