"""Private driver deliveries. State project mailboxes remain separate."""
from __future__ import annotations
import asyncio
import json
import time
from uuid import uuid4
from core.state_graph import StateGraphError, now
from core.state_privacy import UntrustedDatabase

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_inbox_messages (
 message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender_driver_id TEXT,
 project_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('note','request','review_request','handoff_offer','handoff_reply','lease_notice','takeover_notice','subagent_orphaned')), delivery_mode TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, refs_json TEXT NOT NULL,
 reply_to_message_id TEXT, created_at TEXT NOT NULL,
 CHECK(delivery_mode IN ('transient','standing')));
CREATE TABLE IF NOT EXISTS driver_inbox_deliveries (
 delivery_id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES driver_inbox_messages,
 target_driver_id TEXT NOT NULL REFERENCES drivers, seq INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('unread','acknowledged','resolved')),
 version INTEGER NOT NULL CHECK(version>0),
 UNIQUE(message_id,target_driver_id), UNIQUE(target_driver_id,seq));
CREATE TABLE IF NOT EXISTS driver_inbox_requests (
 actor TEXT NOT NULL, operation TEXT NOT NULL, request_key TEXT NOT NULL,
 payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(actor,operation,request_key));
"""
KINDS = {"note", "request", "review_request", "handoff_offer", "handoff_reply"}
SYSTEM_KINDS = {"lease_notice", "takeover_notice", "subagent_orphaned", "handoff_offer"}

class DriverInbox:
    def __init__(self, store, actor, driver_id=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    def _authorize(self, driver_id, *, write=False, break_glass_reason=None):
        if not self.store.project_read_trusted:
            raise StateGraphError("not_inbox_recipient")
        owner = self.actor.startswith("owner:")
        with self.store.transaction() as conn:
            caller = conn.execute("SELECT is_admin,status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()
        if self.driver_id == driver_id and self.driver_id and caller and caller["status"]=="active":
            return
        if owner and (not write or break_glass_reason):
            return
        if write and break_glass_reason and caller and caller["status"]=="active" and caller["is_admin"]:
            return
        raise StateGraphError("not_inbox_recipient")

    @staticmethod
    def _driver(conn, driver_id):
        row = conn.execute("SELECT driver_id FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise StateGraphError("unknown_driver")

    def _replay(self, conn, operation, request_key, payload):
        row = conn.execute("SELECT payload_json,result_json FROM driver_inbox_requests WHERE actor=? AND operation=? AND request_key=?",
                           (self.actor, operation, request_key)).fetchone()
        if row:
            if row["payload_json"] != payload:
                raise StateGraphError("idempotency_conflict")
            return json.loads(row["result_json"])

    def _record(self, conn, operation, request_key, payload, result):
        conn.execute("INSERT INTO driver_inbox_requests VALUES(?,?,?,?,?)",
                     (self.actor, operation, request_key, payload, json.dumps(result)))
        return result

    def send_driver_message(self, request_key, subject, body, target_driver_id=None,
                            project_members=None, project_id=None, kind="note",
                            delivery_mode="transient", refs=None, reply_to_message_id=None,
                            _system=False):
        if type(request_key) is not str or not request_key or len(request_key)>320:
            raise StateGraphError("invalid_request")
        if type(subject) is not str or not 1 <= len(subject) <= 200 or type(body) is not str or len(body)>8000:
            raise StateGraphError("invalid_request")
        if bool(target_driver_id) == bool(project_members) or delivery_mode not in ("transient","standing"):
            raise StateGraphError("invalid_request")
        if kind not in (SYSTEM_KINDS if _system else KINDS) or (not _system and not self.driver_id):
            raise StateGraphError("invalid_request")
        refs = {} if refs is None else refs
        if not isinstance(refs, dict):
            raise StateGraphError("invalid_refs")
        payload = json.dumps([subject, body, target_driver_id, project_members, project_id, kind, delivery_mode, refs, reply_to_message_id, None if _system else self.driver_id], sort_keys=True)
        with self.store.transaction(write=True) as conn:
            replay = self._replay(conn, "send", request_key, payload)
            if replay is not None:
                return replay
            if not _system:
                self._driver(conn, self.driver_id)
                if conn.execute("SELECT status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()[0]!="active":
                    raise StateGraphError("inactive_driver")
            if project_members:
                self.store._project(conn, project_members)
                targets = [r["driver_id"] for r in conn.execute(
                    "SELECT p.driver_id FROM project_drivers p JOIN drivers d USING(driver_id) WHERE p.project_id=? AND p.status='member' AND p.driver_id<>? ORDER BY p.driver_id",
                    (project_members, self.driver_id or ""))]
            else:
                targets = [target_driver_id]
            if not targets or len(targets)>200:
                raise StateGraphError("no_recipients")
            for target in targets:
                self._driver(conn, target)
            if project_id:
                self.store._project(conn, project_id)
            for key, value in refs.items():
                tables = {"attempt_id": ("state_attempts","attempt_id"), "issue_id":("state_issues","issue_id"),
                          "claim_id":("state_node_claims","claim_id"), "subagent_id":("driver_subagents","subagent_id")}
                if key == "node_key" and project_id:
                    self.store._node(conn, project_id, value)
                elif key in tables:
                    if not isinstance(value,str) or not value:
                        raise StateGraphError("invalid_refs")
                    table, column = tables[key]
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        raise StateGraphError("invalid_refs")
                    if not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone():
                        raise StateGraphError("invalid_refs")
                else:
                    raise StateGraphError("invalid_refs")
            thread = None
            if reply_to_message_id:
                prior = conn.execute("SELECT m.thread_id FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE m.message_id=? AND d.target_driver_id=?",
                                     (reply_to_message_id, self.driver_id)).fetchone()
                if prior is None:
                    raise StateGraphError("unknown_message")
                thread = prior["thread_id"]
            mid = str(uuid4())
            conn.execute("INSERT INTO driver_inbox_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (mid,thread or mid,None if _system else self.driver_id,project_id,kind,delivery_mode,subject,body,json.dumps(refs),reply_to_message_id,now()))
            deliveries=[]
            for target in targets:
                seq=conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM driver_inbox_deliveries WHERE target_driver_id=?", (target,)).fetchone()[0]
                did=str(uuid4())
                conn.execute("INSERT INTO driver_inbox_deliveries VALUES(?,?,?,?,?,?)", (did,mid,target,seq,"unread",1))
                deliveries.append({"delivery_id":did,"target_driver_id":target,"seq":seq,"status":"unread","version":1})
            result=self._record(conn,"send",request_key,payload,{"message_id":mid,"deliveries":deliveries})
        from core.state_changes import notify
        notify(self.store.db.db_path)
        return result

    def system_notice(self, **kwargs):
        return self.send_driver_message(_system=True, **kwargs)

    def list_driver_messages(self, driver_id=None, after=0, limit=100):
        driver_id = driver_id or self.driver_id
        self._authorize(driver_id)
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise StateGraphError("invalid_request")
        with self.store.transaction() as conn:
            self._driver(conn, driver_id)
            high=conn.execute("SELECT COALESCE(MAX(seq),0) FROM driver_inbox_deliveries WHERE target_driver_id=?", (driver_id,)).fetchone()[0]
            rows=[dict(r) for r in conn.execute("SELECT d.*,m.thread_id,m.sender_driver_id,m.project_id,m.kind,m.delivery_mode,m.subject,m.body,m.refs_json FROM driver_inbox_deliveries d JOIN driver_inbox_messages m USING(message_id) WHERE d.target_driver_id=? AND d.seq>? ORDER BY d.seq LIMIT ?", (driver_id,after,limit+1))]
        page=rows[:limit]
        return {"driver_id":driver_id,"messages":page,"next_after":page[-1]["seq"] if len(rows)>limit else max(after,high),"truncated":len(rows)>limit}

    def transition(self, delivery_id, expected_version, request_key, target_status, break_glass_reason=None):
        if target_status not in ("acknowledged","resolved") or type(expected_version) is not int or expected_version<1:
            raise StateGraphError("invalid_request")
        if not isinstance(request_key,str) or not 1<=len(request_key)<=320 or not isinstance(delivery_id,str):
            raise StateGraphError("invalid_request")
        payload=json.dumps([delivery_id,expected_version,target_status,break_glass_reason])
        with self.store.transaction(write=True) as conn:
            row=conn.execute("SELECT * FROM driver_inbox_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row is None:
                raise StateGraphError("unknown_delivery")
            self._authorize(row["target_driver_id"],write=True,break_glass_reason=break_glass_reason)
            replay=self._replay(conn,target_status,request_key,payload)
            if replay is not None:
                return replay
            if row["version"]!=expected_version:
                raise StateGraphError("version_conflict")
            if row["status"]!=("unread" if target_status=="acknowledged" else "acknowledged"):
                raise StateGraphError("invalid_transition")
            conn.execute("UPDATE driver_inbox_deliveries SET status=?,version=version+1 WHERE delivery_id=? AND version=?", (target_status,delivery_id,expected_version))
            result={**dict(row),"status":target_status,"version":expected_version+1}
            return self._record(conn,target_status,request_key,payload,result)

    async def wait_for_driver_inbox(self, after=0, timeout_seconds=30, return_when_idle=False, driver_id=None):
        if type(timeout_seconds) not in (int,float) or not 0<=timeout_seconds<=900:
            raise StateGraphError("invalid_request")
        from core.state_changes import subscribe
        deadline=time.monotonic()+timeout_seconds
        with subscribe(self.store.db.db_path) as signal:
            while True:
                signal.clear()
                result=self.list_driver_messages(driver_id,after)
                if result["messages"] or return_when_idle or time.monotonic()>=deadline:
                    return result
                try:
                    await asyncio.wait_for(signal.wait(),timeout=min(1,max(0,deadline-time.monotonic())))
                except asyncio.TimeoutError:
                    pass


    def acknowledge_driver_message(self, delivery_id, expected_version, request_key, break_glass_reason=None):
        return self.transition(delivery_id, expected_version, request_key, "acknowledged", break_glass_reason)

    def resolve_driver_message(self, delivery_id, expected_version, request_key, break_glass_reason=None):
        return self.transition(delivery_id, expected_version, request_key, "resolved", break_glass_reason)


    def active_standing(self):
        self._authorize(self.driver_id)
        if not self.driver_id:
            return []
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT m.*,d.delivery_id,d.seq,d.status FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE d.target_driver_id=? AND m.delivery_mode='standing' AND d.status IN ('unread','acknowledged') ORDER BY d.seq LIMIT 8", (self.driver_id,))]
            total=conn.execute("SELECT COUNT(*) FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE d.target_driver_id=? AND m.delivery_mode='standing' AND d.status IN ('unread','acknowledged')", (self.driver_id,)).fetchone()[0]
            return {"items":rows,"matched_total":total}
