"""Project ack rows; called inside the mailbox or membership transaction."""
import json
from core.state_graph import now
from core.director_messaging_protocol import DirectorMessageError

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_director_delivery_acks (
 delivery_id TEXT NOT NULL REFERENCES state_director_deliveries,
 driver_id TEXT NOT NULL, required INTEGER NOT NULL CHECK(required IN(0,1)),
 acked_at TEXT, ack_event_seq INTEGER, removed_at TEXT,
 PRIMARY KEY(delivery_id,driver_id));
CREATE TRIGGER IF NOT EXISTS state_director_acks_no_delete
BEFORE DELETE ON state_director_delivery_acks
BEGIN SELECT RAISE(ABORT,'director acknowledgements are never deleted'); END;
CREATE TRIGGER IF NOT EXISTS state_director_acks_immutable
BEFORE UPDATE ON state_director_delivery_acks
WHEN OLD.delivery_id<>NEW.delivery_id OR OLD.driver_id<>NEW.driver_id
 OR (OLD.acked_at IS NOT NULL AND (NEW.acked_at IS NOT OLD.acked_at OR (OLD.ack_event_seq IS NOT NULL AND NEW.ack_event_seq IS NOT OLD.ack_event_seq)))
BEGIN SELECT RAISE(ABORT,'director acknowledgement is immutable'); END;
CREATE TABLE IF NOT EXISTS state_director_legacy_acks (
 delivery_id TEXT PRIMARY KEY REFERENCES state_director_deliveries);
"""

def members(conn, project_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_drivers'").fetchone():
        return []
    return [r[0] for r in conn.execute("SELECT driver_id FROM project_drivers WHERE project_id=? AND status='member' ORDER BY driver_id",(project_id,))]

def details(conn, delivery, driver_id=None):
    message=conn.execute("SELECT ack_mode,ack_quorum FROM state_director_messages WHERE message_id=?",(delivery["message_id"],)).fetchone()
    rows=[dict(r) for r in conn.execute("SELECT * FROM state_director_delivery_acks WHERE delivery_id=? ORDER BY driver_id",(delivery["delivery_id"],))]
    acks=[{"driver_id":r["driver_id"],"acked_at":r["acked_at"]} for r in rows if r["acked_at"]]
    pending=[r["driver_id"] for r in rows if r["required"] and not r["removed_at"] and not r["acked_at"]]
    legacy=bool(conn.execute("SELECT 1 FROM state_director_legacy_acks WHERE delivery_id=?",(delivery["delivery_id"],)).fetchone())
    met=legacy or (not pending if message["ack_mode"]=="broadcast" else len(acks)>=message["ack_quorum"])
    return {"ack_mode":message["ack_mode"],"ack_quorum":message["ack_quorum"],"acks":acks,
            "acked_count":len(acks),"pending_drivers":pending,"acked_by_me":any(r["driver_id"]==driver_id for r in acks),
            "legacy_ack":legacy,"quorum_met":met,
            "ack_quorum_unreachable":message["ack_mode"]=="at_least_n" and not met and len(members(conn,delivery["target_project_id"]))<message["ack_quorum"]}

def membership_removed(conn, project_id, driver_id, actor):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_director_delivery_acks'").fetchone():
        return
    ids=[r[0] for r in conn.execute("SELECT a.delivery_id FROM state_director_delivery_acks a JOIN state_director_deliveries d USING(delivery_id) WHERE d.target_project_id=? AND a.driver_id=? AND a.required=1 AND a.acked_at IS NULL AND a.removed_at IS NULL",(project_id,driver_id))]
    for did in ids:
        conn.execute("UPDATE state_director_delivery_acks SET removed_at=? WHERE delivery_id=? AND driver_id=?",(now(),did,driver_id))
        row=dict(conn.execute("SELECT * FROM state_director_deliveries WHERE delivery_id=?",(did,)).fetchone())
        meta=details(conn,row)
        if meta["quorum_met"] and row["status"]!="resolved":
            mode=conn.execute("SELECT delivery_mode FROM state_director_messages WHERE message_id=?",(row["message_id"],)).fetchone()[0]
            status="resolved" if mode=="transient" else "acknowledged"
            conn.execute("UPDATE state_director_deliveries SET status=?,version=version+1 WHERE delivery_id=?",(status,did))
        conn.execute("INSERT INTO state_events(project_id,node_key,event_type,payload_json,created_at) VALUES(?,NULL,?,?,?)",
                     (project_id,"director_message_member_removed",json.dumps({"delivery_id":did,"driver_id":driver_id,"actor":actor}),now()))

