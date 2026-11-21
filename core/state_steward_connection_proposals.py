"""The steward's proposals to change its own connections (step 24.1c, design-steward 7.8.10a).

The steward reads its connections and their tools (no approval) but never writes the
registry: it proposes a `connection_change` (`create_steward_proposal`), the owner
approves the card, and the backend executes the FROZEN payload in one transaction.

- `validate` is the payload model: one op (add, update, remove, reenable), a source
  (an owner message or a director's reply to the steward's handoff), and nothing that
  could carry a secret (an unknown key is `invalid_payload`). A field left out means
  "unchanged", never "reset to the default".
- `prepare` (at creation, on the proposal's write transaction) checks the payload
  against the registry as it is now - the connection's `conn_uid` and
  `config_version`, each tool's `schema_sha256` - judges whether any field WIDENS a
  permission (`widens`: then only the dashboard card can approve it) and freezes a
  `snapshot` the card shows as plain text: every field's value before, each tool's
  whole canonical definition, the source's own words read from the database.
- `execute` (at approval, on the executor's write transaction) checks all of it again
  and writes exactly the payload; any mismatch fails the proposal and writes nothing.
  Its caller announces the tool-list change after the commit: no restart (7.8.8).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from core.state_graph import StateGraphError
from core import state_steward_connections as sc

OPS = ("add", "update", "remove", "reenable")
TOOL_FIELDS = ("allowed", "outward", "egress", "blocked_args", "daily_cap")
CARD_DEFINITIONS_MAX = 64 * 1024
SOURCE_MAX_AGE = timedelta(hours=24)
QUOTE_CHARS = 500
MAX_TOOLS = 64


def _iso_now() -> str:
    return sc._iso(sc._utcnow())


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def connect_url(conn_id: str) -> str:
    """The dashboard's page where the owner enters a secret or signs in (7.8.10, 7.8.10a)."""
    base = os.environ.get("AITELIER_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return f"{base}/#/steward/connections/{conn_id}/connect"


# ---------------------------------------------------------------- the payload
def _invalid(details) -> StateGraphError:
    return StateGraphError(f"invalid_payload: {details}")


def validate(payload) -> dict:
    """The payload as the model may write it, normalized (only the fields given)."""
    from typing import Literal
    from pydantic import BaseModel, ConfigDict, Field, ValidationError

    class Source(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        kind: Literal["owner_message", "director_letter"]
        ref: str = Field(min_length=1, max_length=320)

    class Tool(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        raw_name: str = Field(min_length=1, max_length=500)
        schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
        allowed: bool | None = None
        outward: bool | None = None
        egress: bool | None = None
        blocked_args: list[str] | None = Field(default=None, max_length=64)
        daily_cap: int | None = Field(default=None, ge=1, le=100000)

    class Change(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        op: Literal["add", "update", "remove", "reenable"]
        conn_id: str = Field(pattern=sc.CONN_ID.pattern)
        source: Source
        label: str | None = Field(default=None, min_length=1, max_length=sc.LABEL_MAX)
        url: str | None = Field(default=None, min_length=1, max_length=500)
        transport: Literal["streamable-http"] | None = None
        auth_kind: Literal["none", "header", "oauth"] | None = None
        header_name: str | None = Field(default=None, min_length=1, max_length=64)
        visibility: Literal["private", "public"] | None = None
        enabled: bool | None = None
        conn_uid: str | None = Field(default=None, min_length=1, max_length=64)
        expect_config_version: int | None = Field(default=None, ge=1)
        tools: list[Tool] | None = Field(default=None, min_length=1, max_length=MAX_TOOLS)
        # Set by the steward's plugin (code, never the model): the turn had read outside content.
        external_content: bool | None = None

    if not isinstance(payload, dict):
        raise _invalid("payload must be an object")
    try:
        parsed = Change.model_validate(payload)
    except ValidationError as exc:
        details = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]}
                   for e in exc.errors(include_input=False)[:10]]
        raise _invalid(details) from None
    given = parsed.model_fields_set
    out = {k: getattr(parsed, k) for k in given if k not in ("source", "tools")}
    out["source"] = parsed.source.model_dump()
    op = parsed.op
    if "tools" in given and parsed.tools is not None:
        tools, seen = [], set()
        for tool in parsed.tools:
            changes = {k: getattr(tool, k) for k in tool.model_fields_set if k in TOOL_FIELDS}
            if not changes:
                raise _invalid(f"tools: {tool.raw_name} changes no field")
            for flag in ("allowed", "outward", "egress"):
                if flag in changes and changes[flag] is None:
                    raise _invalid(f"tools: {flag} is true or false")
            if "blocked_args" in changes:
                if changes["blocked_args"] is None or any(not a or len(a) > 1000 for a in changes["blocked_args"]):
                    raise _invalid("tools: blocked_args is a list of argument paths")
                changes["blocked_args"] = sorted(set(changes["blocked_args"]))
            if changes.get("egress") is False:
                # Every connection is a host other than the backend (7.8.4): only the owner's
                # panel takes egress off.
                raise StateGraphError("invalid_payload: egress_required")
            if tool.raw_name in seen:
                raise _invalid(f"tools: {tool.raw_name} named twice")
            seen.add(tool.raw_name)
            tools.append({"raw_name": tool.raw_name, "schema_sha256": tool.schema_sha256, **changes})
        out["tools"] = tools
    adds = {"label", "url", "transport", "auth_kind", "header_name", "visibility"}
    allowed_keys = {
        "add": {"op", "conn_id", "source", "external_content"} | adds,
        "update": {"op", "conn_id", "source", "external_content", "conn_uid", "expect_config_version", "label",
                   "visibility", "enabled", "tools"},
        "remove": {"op", "conn_id", "source", "external_content", "conn_uid", "expect_config_version"},
        "reenable": {"op", "conn_id", "source", "external_content", "conn_uid", "expect_config_version", "tools"},
    }[op]
    extra = sorted(set(out) - allowed_keys)
    if extra:
        raise _invalid(f"{op} does not take {', '.join(extra)}")
    for key, value in list(out.items()):
        if value is None and key != "external_content":
            raise _invalid(f"{key} cannot be null")
    if op == "add":
        missing = sorted({"label", "url", "transport", "auth_kind", "visibility"} - set(out))
        if missing:
            raise _invalid(f"add needs {', '.join(missing)}")
        if out["auth_kind"] == "header":
            out.setdefault("header_name", "Authorization")
        elif "header_name" in out:
            raise _invalid("header_name is for auth_kind header only")
        if not sc.header_name_ok(out.get("header_name", "Authorization")):
            raise StateGraphError("invalid_header_name")
        sc.check_url(out["url"])
    else:
        missing = sorted({"conn_uid", "expect_config_version"} - set(out))
        if missing:
            raise _invalid(f"{op} needs {', '.join(missing)}")
        if op == "update" and not {"label", "visibility", "enabled", "tools"} & set(out):
            raise _invalid("update changes at least one of label, visibility, enabled, tools")
        if op == "reenable":
            if "tools" not in out:
                raise _invalid("reenable needs tools")
            if any(set(t) - {"raw_name", "schema_sha256"} != {"allowed"} or t["allowed"] is not True
                   for t in out["tools"]):
                raise _invalid("reenable takes only allowed: true per tool")
    if "label" in out:
        out["label"] = out["label"].strip()
        if not sc.label_ok(out["label"]):
            raise StateGraphError("invalid_label")
    if out.get("external_content") is None:
        out.pop("external_content", None)
    return out


def check_address(payload) -> None:
    """`reserved_endpoint` for an `add` (resolved by address, never by name; 7.8.4). Run
    outside the write transaction: a name lookup must not hold the database's lock."""
    if payload["op"] == "add":
        sc.checked_address(payload["url"], require=False)


# ---------------------------------------------------------------- the registry, as it is now
def _squash(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _distance_one(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def _label_confusable(conn, label, conn_id) -> None:
    for (other,) in conn.execute("SELECT label FROM state_steward_connections WHERE conn_id<>?", (conn_id,)):
        if other == label or _squash(other) == _squash(label):
            raise StateGraphError("label_confusable")


def _conn_id_confusable(conn, conn_id) -> None:
    for (other,) in conn.execute("SELECT conn_id FROM state_steward_connections WHERE conn_id<>?", (conn_id,)):
        if other.rstrip("0123456789") == conn_id.rstrip("0123456789") or _distance_one(other, conn_id):
            raise StateGraphError("conn_id_confusable")


def _tool_state(row) -> str:
    if row["allowed_sha256"] and row["allowed_sha256"] != row["schema_sha256"]:
        return "changed"
    return "allowed" if row["allowed"] else "new" if not row["allowed_sha256"] else "off"


def _current(conn, payload):
    """(connection row or None, {raw_name: tool row}) after the checks shared by creation
    and execution: the connection and its version exactly as proposed, each tool's
    definition exactly as proposed. Any mismatch raises its code."""
    conn_id, op = payload["conn_id"], payload["op"]
    row = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
    if op == "add":
        if row is not None:
            raise StateGraphError(f"connection_exists: {conn_id}")
        _label_confusable(conn, payload["label"], conn_id)
        _conn_id_confusable(conn, conn_id)
        return None, {}
    if row is None:
        raise StateGraphError(f"unknown_connection: {conn_id}")
    if row["conn_uid"] != payload["conn_uid"] or row["config_version"] != payload["expect_config_version"]:
        raise StateGraphError(f"connection_changed: {conn_id} changed since it was read - read it again")
    if "label" in payload and payload["label"] != row["label"]:
        _label_confusable(conn, payload["label"], conn_id)
    tools = {}
    for item in payload.get("tools", []):
        tool = conn.execute("SELECT * FROM state_steward_connection_tools WHERE conn_id=? AND raw_name=?",
                            (conn_id, item["raw_name"])).fetchone()
        if tool is None:
            raise StateGraphError(f"schema_changed: {item['raw_name']} (no such tool now)")
        if tool["schema_sha256"] != item["schema_sha256"]:
            raise StateGraphError(f"schema_changed: {item['raw_name']}")
        if op == "reenable" and _tool_state(tool) != "changed":
            raise StateGraphError(f"not_changed: {item['raw_name']} is not a tool whose description changed")
        tools[item["raw_name"]] = tool
    if (op == "update" and payload.get("visibility") == "public" and row["visibility"] != "public"
            and row["auth_kind"] == "oauth"):
        # The account is checked again for the public role (7.8.6): on the dashboard.
        raise StateGraphError("needs_dashboard: identity")
    return row, tools


# ---------------------------------------------------------------- widening
def widened(payload, row, tools) -> list[str]:
    """Each field the payload widens (7.8.10a's table), as `field` or `tools.<raw>.<field>`:
    any one of them makes the whole proposal a widening, approved on the dashboard only."""
    op = payload["op"]
    if op in ("add", "reenable"):
        return [op] + [f"tools.{t['raw_name']}.allowed" for t in payload.get("tools", [])]
    if op == "remove":
        return []
    out = []
    if "label" in payload and payload["label"] != row["label"]:
        out.append("label")
    if payload.get("visibility") == "public" and row["visibility"] == "private":
        out.append("visibility")
    if payload.get("enabled") is True and not row["enabled"]:
        out.append("enabled")
    for item in payload.get("tools", []):
        tool, name = tools[item["raw_name"]], item["raw_name"]
        if item.get("allowed") is True and not (tool["allowed"] and tool["allowed_sha256"] == tool["schema_sha256"]):
            out.append(f"tools.{name}.allowed")
        if item.get("outward") is False and tool["outward"]:
            out.append(f"tools.{name}.outward")
        if item.get("egress") is False and tool["egress"]:
            out.append(f"tools.{name}.egress")
        if "blocked_args" in item and set(json.loads(tool["blocked_args"])) - set(item["blocked_args"]):
            out.append(f"tools.{name}.blocked_args")
        if "daily_cap" in item:
            old, new = tool["daily_cap"], item["daily_cap"]
            if old is not None and (new is None or new > old):
                out.append(f"tools.{name}.daily_cap")
    return out


# ---------------------------------------------------------------- the source
def _owner_message(conn, message_id, home):
    row = conn.execute("SELECT message_id,project_id,sender_driver_id,channel,body,created_at FROM "
                       "driver_inbox_messages WHERE message_id=?", (message_id,)).fetchone()
    if row is None or row["project_id"] != home or row["sender_driver_id"] is not None or row["channel"] != "user":
        return None
    where = conn.execute("SELECT channel FROM state_message_sources WHERE message_id=? AND direction='in'",
                         (message_id,)).fetchone()
    try:
        dashboard = conn.execute("SELECT 1 FROM state_steward_dashboard_messages WHERE message_id=?",
                                 (message_id,)).fetchone() is not None
    except Exception:  # noqa: BLE001 - no dashboard marks yet
        dashboard = False
    channel = where["channel"] if where is not None else "dashboard" if dashboard else "other"
    return {"message_id": row["message_id"], "channel": channel, "created_at": row["created_at"],
            "text": row["body"][:QUOTE_CHARS]}


def source_of(conn, source, home) -> dict:
    """What the card shows as the proposal's source, read from the database (never from
    what the model wrote): the owner's message itself, or a director's reply to a
    handoff of the steward's that cites an owner message - with that message."""
    if source["kind"] == "owner_message":
        message = _owner_message(conn, source["ref"], home)
        if message is None:
            raise StateGraphError("source_not_an_owner_message: name a message your owner wrote to you")
        if sc._utcnow() - _parse(message["created_at"]) > SOURCE_MAX_AGE:
            raise StateGraphError("source_too_old: the owner's message is older than 24 hours")
        return {"kind": "owner_message", "ref": source["ref"], "owner_message": message}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(state_director_messages)")}
    letter = conn.execute(
        "SELECT d.delivery_id,d.target_project_id,m.message_id,m.sender_project_id,m.subject,m.body,m.created_at,"
        "m.reply_to_delivery_id FROM state_director_deliveries d JOIN state_director_messages m "
        "ON m.message_id=d.message_id WHERE d.delivery_id=?", (source["ref"],)).fetchone() if columns else None
    if letter is None or letter["target_project_id"] != home:
        raise StateGraphError("source_not_found: name a director letter delivered to you")
    handoff = conn.execute(
        "SELECT m.sender_project_id,m.cites_owner_message_id FROM state_director_deliveries d JOIN "
        "state_director_messages m ON m.message_id=d.message_id WHERE d.delivery_id=?",
        (letter["reply_to_delivery_id"],)).fetchone() if (
            letter["reply_to_delivery_id"] and "cites_owner_message_id" in columns) else None
    if handoff is None or handoff["sender_project_id"] != home or not handoff["cites_owner_message_id"]:
        raise StateGraphError("source_not_a_handoff_reply: a director letter is a source only when it replies to "
                              "your handoff of an owner message")
    message = _owner_message(conn, handoff["cites_owner_message_id"], home)
    if message is None:
        raise StateGraphError("source_not_a_handoff_reply: the handoff cites no owner message")
    return {"kind": "director_letter", "ref": source["ref"],
            "letter": {"delivery_id": letter["delivery_id"], "from_project_id": letter["sender_project_id"],
                       "subject": letter["subject"], "created_at": letter["created_at"],
                       "text": letter["body"][:QUOTE_CHARS]},
            "owner_message": message}


# ---------------------------------------------------------------- creation
def _connection_facts(row) -> dict:
    return {"conn_uid": row["conn_uid"], "config_version": row["config_version"], "label": row["label"],
            "url": row["url"], "auth_kind": row["auth_kind"], "header_name": row["header_name"],
            "visibility": row["visibility"], "enabled": bool(row["enabled"]), "auth_identity": row["auth_identity"],
            "identity_confirmed": bool(row["identity_confirmed"]), "secret": sc.secret_status(row)}


def _tool_facts(tool) -> dict:
    return {"raw_name": tool["raw_name"], "gw_name": tool["gw_name"], "state": _tool_state(tool),
            "definition": json.loads(tool["definition_json"]), "allowed": bool(tool["allowed"]),
            "outward": bool(tool["outward"]), "egress": bool(tool["egress"]),
            "blocked_args": json.loads(tool["blocked_args"]), "daily_cap": tool["daily_cap"]}


def prepare(conn, payload, home) -> dict:
    """The stored payload: checked against the registry now, with `widens`, `widened` and
    the card's `snapshot` (values before, whole definitions, the source's words)."""
    row, tools = _current(conn, payload)
    source = source_of(conn, payload["source"], home)
    definitions = sum(len(tool["definition_json"].encode("utf-8")) for tool in tools.values())
    if definitions > CARD_DEFINITIONS_MAX:
        raise StateGraphError("description_too_long_for_card: the definitions are over 64 KB - change it on the "
                              "dashboard")
    marks = widened(payload, row, tools)
    snapshot = {"connection": _connection_facts(row) if row is not None else None,
                "tools": [_tool_facts(tools[t["raw_name"]]) for t in payload.get("tools", [])],
                "source": source}
    return {**payload, "widens": bool(marks), "widened": marks, "snapshot": snapshot}


# ---------------------------------------------------------------- execution
class ExecutionRefused(StateGraphError):
    """The frozen payload cannot be executed as it stands; nothing was written."""

    def __init__(self, code, connect=None):
        super().__init__(code)
        self.code, self.connect = code, connect


def execute(conn, payload, proposal_id) -> dict:
    """Write exactly the frozen payload on the caller's write transaction, after checking
    the registry is still the version proposed. Returns {result, removed}: `removed` is a
    connection whose secret file the caller deletes after the commit (OAuth revoked first)."""
    try:
        row, tools = _current(conn, payload)
    except StateGraphError as exc:
        raise ExecutionRefused(str(exc)) from None
    conn_id, op, stamp, mark = payload["conn_id"], payload["op"], _iso_now(), f"proposal:{proposal_id}"
    result = {"op": op, "conn_id": conn_id}
    if op == "add":
        conn.execute("INSERT INTO state_steward_connections(conn_id,conn_uid,label,url,auth_kind,header_name,"
                     "visibility,enabled,generation,config_version,auth_identity,created_at,updated_at,"
                     "last_probe_at,last_error,instructions,last_change,identity_confirmed) "
                     "VALUES(?,?,?,?,?,?,?,0,1,1,NULL,?,?,NULL,NULL,NULL,?,0)",
                     (conn_id, sc._secrets.token_hex(16), payload["label"], payload["url"], payload["auth_kind"],
                      payload.get("header_name") if payload["auth_kind"] == "header" else None,
                      payload["visibility"], stamp, stamp, mark))
        if payload["auth_kind"] in ("header", "oauth"):
            result["connect_url"] = connect_url(conn_id)
        return {"result": result, "removed": None}
    if op == "remove":
        conn.execute("DELETE FROM state_steward_connection_tools WHERE conn_id=?", (conn_id,))
        conn.execute("DELETE FROM state_steward_connections WHERE conn_id=?", (conn_id,))
        return {"result": result, "removed": conn_id}
    if payload.get("enabled") is True:
        status = sc.secret_status(row)
        if row["auth_kind"] == "header" and status != "set":
            raise ExecutionRefused("needs_dashboard: secret", connect_url(conn_id))
        if row["auth_kind"] == "oauth" and status != "set":
            raise ExecutionRefused("needs_dashboard: oauth_consent", connect_url(conn_id))
        if row["auth_kind"] == "oauth" and not row["identity_confirmed"]:
            raise ExecutionRefused("needs_dashboard: identity", connect_url(conn_id))
    sets, params = [], []
    for key in ("label", "visibility"):
        if key in payload:
            sets.append(f"{key}=?")
            params.append(payload[key])
    if "enabled" in payload:
        sets.append("enabled=?")
        params.append(int(payload["enabled"]))
    conn.execute(f"UPDATE state_steward_connections SET {''.join(s + ',' for s in sets)}"
                 "config_version=config_version+1, updated_at=?, last_change=? WHERE conn_id=?",
                 (*params, stamp, mark, conn_id))
    for item in payload.get("tools", []):
        tool, assignments, values = tools[item["raw_name"]], [], []
        for flag in ("allowed", "outward", "egress"):
            if flag in item:
                assignments.append(f"{flag}=?")
                values.append(int(item[flag]))
        if item.get("allowed") is True:
            assignments.append("allowed_sha256=?")
            values.append(tool["schema_sha256"])
        if "blocked_args" in item:
            assignments.append("blocked_args=?")
            values.append(json.dumps(item["blocked_args"]))
        if "daily_cap" in item:
            assignments.append("daily_cap=?")
            values.append(item["daily_cap"])
        conn.execute(f"UPDATE state_steward_connection_tools SET {','.join(assignments)} "
                     "WHERE conn_id=? AND raw_name=?", (*values, conn_id, item["raw_name"]))
    if payload.get("visibility") == "public" and row["visibility"] == "private":
        # The public default again (7.8.4): url / *_url arguments blocked, added only.
        for tool in conn.execute("SELECT raw_name,definition_json,blocked_args FROM state_steward_connection_tools "
                                 "WHERE conn_id=?", (conn_id,)).fetchall():
            blocked = set(json.loads(tool["blocked_args"]))
            added = blocked | set(sc._url_params(json.loads(tool["definition_json"]).get("inputSchema")))
            if added != blocked:
                conn.execute("UPDATE state_steward_connection_tools SET blocked_args=? WHERE conn_id=? AND raw_name=?",
                             (json.dumps(sorted(added)), conn_id, tool["raw_name"]))
    return {"result": result, "removed": None}


def after_remove(conn_id) -> None:
    """After a removal's commit: the OAuth tokens revoked where offered, then the secret
    file deleted - the same code as the owner's delete (and the startup sweep's)."""
    sc._revoke_quietly(conn_id, sc.read_secret(conn_id))
    sc.delete_secret(conn_id)
