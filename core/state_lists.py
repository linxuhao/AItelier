"""The family lists (step 25 C4, design-steward 7.9.5): a shopping list and a to-do list.

Two lists of the FAMILY, never private to the owner: whatever is on them the bound family
group can read, whoever added it (steward.md says so). Who may do what:

- the steward in a family-group turn (its tool's request carries the public-read header and
  names the turn the host recorded with a bound group's audience, core.state_groups
  .group_turn - the same gate as the family memory): show, add, check / uncheck. This is
  step 23's one explicit write exception besides the family memory (owner 2026-11-21);
- the steward in an owner turn (its request carries no header): show, add, check, remove;
- the owner on the dashboard (owner session): everything, and clearing the done items.

Who added or checked an item is taken by the backend, never from the call: in a group turn
the asker the audience record names (one sender: that member's dashboard name, else
「家庭群」), in an owner turn the steward, on the dashboard the owner. Every item passes the
family group's exit check when it is ADDED, from wherever (core.state_groups.exit_check): a
hit is refused `withheld` with its kind, so nothing on a list can make a group reply
withheld later. Unbinding the family group keeps every item and forgets who of that group
added or checked it (forget_group_senders, director 489 (a)): an item's `added_by` / `done_by`
is then null, which each reader shows as 「曾由一位成员添加」 / 「曾由一位成员勾掉」 (the
dashboard's i18n, the host's list tools for the model). Re-checking a checked item changes
nothing, so it never fills in a who the unbind forgot; a keyed request's result is kept in the
request ledger without `added_by` / `done_by`, and its replay reads them from the item now
(review D1, D2). The bounds are LIST_LIMITS. No write emits a State event (nothing wakes
anyone, 7.7.7). The table is private (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import secrets
import unicodedata
from datetime import timedelta
from types import MappingProxyType

from core.state_graph import StateConflict, StateGraphError
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_steward import _hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_list_items (
 item_id TEXT PRIMARY KEY,
 list TEXT NOT NULL CHECK(list IN ('shopping','todo')),
 text TEXT NOT NULL,
 added_by_role TEXT NOT NULL CHECK(added_by_role IN ('owner','member','steward')),
 added_by_ref TEXT, added_by_label TEXT,
 added_via TEXT NOT NULL CHECK(added_via IN ('group','owner_dm','dashboard')),
 added_at TEXT NOT NULL,
 done_at TEXT, done_by_role TEXT, done_by_ref TEXT, done_by_label TEXT,
 deleted_at TEXT);
CREATE INDEX IF NOT EXISTS state_steward_list_items_open ON state_steward_list_items(list, deleted_at, done_at);
"""

LISTS = ("shopping", "todo")
# Design-steward 7.9.5: at most 10 items per add, 100 open items per list, 100 characters an
# item; a checked item is deleted 30 days after it was checked (the group rows' 30 days). A read
# shows at most 100 checked items of a list, newest first, and counts the rest (director 505).
LIST_LIMITS = MappingProxyType({"per_add": 10, "open_per_list": 100, "item_chars": 100, "done_days": 30,
                                "per_check": 50, "done_shown": 100})
GROUP_LABEL = "家庭群"
STEWARD_LABEL = "管家"
OWNER_LABEL = "业主"
GROUP_TURN_ONLY = "lists_group_turn_only"


def _now():
    from core import state_groups
    return state_groups._now()


def _clean(text) -> str:
    return unicodedata.normalize("NFKC", str(text)).strip()


def forget_group_senders(conn, refs) -> None:
    """7.9.5 (director 489 (a)): the family group was unbound. Its items stay (text, checked
    state and times unchanged); every `added_by_ref` / `done_by_ref` that is one of the group's
    senders (`refs`) becomes NULL, and a member's stored name with it (the owner stays 「业主」):
    the list is the family's, only the group's 「who」 goes."""
    refs = sorted({r for r in refs if r})
    if not refs:
        return
    marks = ",".join("?" * len(refs))
    conn.execute("UPDATE state_steward_list_items SET added_by_ref=NULL,added_by_label=CASE WHEN "
                 f"added_by_role='member' THEN NULL ELSE added_by_label END WHERE added_by_ref IN ({marks})", refs)
    conn.execute("UPDATE state_steward_list_items SET done_by_ref=NULL,done_by_label=CASE WHEN "
                 f"done_by_role='member' THEN NULL ELSE done_by_label END WHERE done_by_ref IN ({marks})", refs)


def _initialize_schema(db) -> None:
    """The schema setup, run once per process per database file (core.state_schema_setup)."""
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


class StateLists:
    def __init__(self, store, actor, driver_id=None, steward=None, groups=None, public_view=False):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.steward, self.groups = steward, groups
        self.project_read_trusted = store.project_read_trusted
        self.public_view = public_view
        if not isinstance(store.db, UntrustedDatabase):
            # Once per process per database file (core.state_schema_setup).
            from core.state_schema_setup import once
            once(store.db, "lists", _initialize_schema, store.db)

    # ---------------------------------------------------------------- who is writing
    def _caller(self, conn, turn, as_owner, project_id=None):
        """(role, ref, label, via) of this call, from the credential and the audience record."""
        if as_owner:
            self.steward._owner()
            return "owner", None, OWNER_LABEL, "dashboard"
        if self.public_view:
            if not isinstance(turn, str) or not turn:
                raise StateGraphError("invalid_request: a group turn's list request names its turn")
            row = self.groups.group_turn(conn, turn, GROUP_TURN_ONLY)
            role, ref, label = self.groups.turn_asker(conn, row)
            return role, ref, label or GROUP_LABEL, "group"
        self.steward._steward(conn, project_id)
        return "steward", None, STEWARD_LABEL, "owner_dm"

    @staticmethod
    def _retain(conn, moment):
        cutoff = (moment - timedelta(days=LIST_LIMITS["done_days"])).isoformat()
        conn.execute("DELETE FROM state_steward_list_items WHERE done_at<? OR deleted_at<?", (cutoff, cutoff))

    @staticmethod
    def _label(conn, role, ref, stored):
        """The name shown now: a member's current dashboard name (a rename shows, the key is
        the sender_ref), else what was stored - None for a member of a group since unbound
        (forget_group_senders)."""
        if role == "member" and ref is not None:
            row = conn.execute("SELECT label FROM state_group_members WHERE sender_ref=? AND label IS NOT NULL "
                               "LIMIT 1", (ref,)).fetchone()
            if row is not None:
                return row[0]
        return stored

    def _item(self, conn, row):
        return {"item_id": row["item_id"], "list": row["list"], "text": row["text"],
                "added_by": self._label(conn, row["added_by_role"], row["added_by_ref"], row["added_by_label"]),
                "added_by_role": row["added_by_role"], "added_via": row["added_via"], "added_at": row["added_at"],
                "done": row["done_at"] is not None, "done_at": row["done_at"],
                "done_by": self._label(conn, row["done_by_role"], row["done_by_ref"], row["done_by_label"])
                if row["done_at"] is not None else None}

    # The request ledger (driver_inbox_requests) keeps a list result WITHOUT its who
    # (added_by / done_by): a member's name must not outlive the unbind that forgot it there
    # (review D2). A replay returns the stored answer with who read from the item now - the
    # same name a list read would show (done_by only where that answer says done), null once
    # forgotten or the item is gone.
    _WHO = ("added_by", "done_by")

    def _keyed(self, conn, operation, request_key, request_hash):
        replay = self.steward._replay(conn, operation, request_key, request_hash) if request_key else None
        for item in (replay or {}).get("items", ()):
            row = conn.execute("SELECT * FROM state_steward_list_items WHERE item_id=?", (item["item_id"],)).fetchone()
            now = self._item(conn, row) if row is not None else {}
            item.update(added_by=now.get("added_by"), done_by=now.get("done_by") if item.get("done") else None)
        return replay

    def _record(self, conn, operation, request_key, request_hash, result):
        if request_key:
            stored = {**result, "items": [{**i, **dict.fromkeys(self._WHO)} for i in result["items"]]} \
                if "items" in result else result
            self.steward._record(conn, operation, request_key, request_hash, stored)
        return result

    # ---------------------------------------------------------------- reads
    @writer_only_read("list_list_items")
    def list_items(self, list=None, include_done=True, turn=None, project_id=None, as_owner=False):
        """Both lists (or one): open items oldest first, then the checked ones newest first.

        At most LIST_LIMITS["done_shown"] (100) checked items of a list are shown, while a
        checked item is kept done_days (30): with the checked items, `done_total` counts each
        list's checked items and `done_more` how many of them are not shown (director 505)."""
        if list is not None and list not in LISTS:
            raise StateGraphError("invalid_list: shopping or todo")
        with self.store.transaction() as conn:
            self._caller(conn, turn, as_owner, project_id)
            out, total, more = {}, {}, {}
            for name in ([list] if list else LISTS):
                rows = conn.execute("SELECT * FROM state_steward_list_items WHERE list=? AND deleted_at IS NULL AND "
                                    "done_at IS NULL ORDER BY added_at, rowid", (name,)).fetchall()
                if include_done:
                    done = conn.execute("SELECT * FROM state_steward_list_items WHERE list=? AND deleted_at IS NULL "
                                        "AND done_at IS NOT NULL ORDER BY done_at DESC, rowid DESC LIMIT ?",
                                        (name, LIST_LIMITS["done_shown"])).fetchall()
                    total[name] = conn.execute("SELECT COUNT(*) FROM state_steward_list_items WHERE list=? AND "
                                               "deleted_at IS NULL AND done_at IS NOT NULL", (name,)).fetchone()[0]
                    more[name] = total[name] - len(done)
                    rows += done
                out[name] = [self._item(conn, r) for r in rows]
        return {"lists": out, **({"done_total": total, "done_more": more} if include_done else {})}

    # ---------------------------------------------------------------- writes
    def add_items(self, list, items, project_id=None, request_key=None, turn=None, as_owner=False):
        """Add up to per_add items. An item equal (after NFKC and trimming) to an open item of
        the same list is that item, not a second one. Every new item passes the exit check;
        one hit refuses the whole add (withheld: <kind>). A list would pass open_per_list:
        list_full, nothing added."""
        if list not in LISTS:
            raise StateGraphError("invalid_list: shopping or todo")
        if not isinstance(items, (tuple, type([]))) or not items:
            raise StateGraphError("invalid_request: items is a nonempty list of texts")
        if len(items) > LIST_LIMITS["per_add"]:
            raise StateGraphError(f"too_many_items: at most {LIST_LIMITS['per_add']} items at a time")
        texts = []
        for item in items:
            cleaned = _clean(item)
            if not cleaned:
                raise StateGraphError("empty_item")
            if len(cleaned) > LIST_LIMITS["item_chars"]:
                raise StateGraphError(f"item_too_long: at most {LIST_LIMITS['item_chars']} characters")
            if cleaned not in texts:
                texts.append(cleaned)
        moment = _now()
        with self.store.transaction(write=True) as conn:
            role, ref, label, via = self._caller(conn, turn, as_owner, project_id)
            request_hash = _hash(["add", list, texts, via, ref])
            replay = self._keyed(conn, "add_list_items", request_key, request_hash)
            if replay is not None:
                return replay
            self._retain(conn, moment)
            existing = {r["text"]: r for r in conn.execute(
                "SELECT * FROM state_steward_list_items WHERE list=? AND deleted_at IS NULL AND done_at IS NULL", (list,))}
            new = [t for t in texts if t not in existing]
            for text in new:
                reason = self.groups.exit_check(conn, text, moment)
                if reason is not None:
                    if self.public_view:
                        # A group turn learns only that an item was blocked, never why (7.7.7, review F3).
                        raise StateGraphError("withheld: nothing was added")
                    from core.state_groups import WITHHELD_REASONS
                    raise StateGraphError(f"withheld: {reason} ({WITHHELD_REASONS[reason]}); nothing was added")
            if len(existing) + len(new) > LIST_LIMITS["open_per_list"]:
                raise StateConflict(f"list_full: at most {LIST_LIMITS['open_per_list']} open items on a list; "
                                    "check or remove some first")
            added = {}
            for text in new:
                item_id = "li-" + secrets.token_hex(8)
                conn.execute("INSERT INTO state_steward_list_items(item_id,list,text,added_by_role,added_by_ref,"
                             "added_by_label,added_via,added_at) VALUES(?,?,?,?,?,?,?,?)",
                             (item_id, list, text, role, ref, label, via, moment.isoformat()))
                added[text] = item_id
            result = {"items": [{**self._item(conn, existing[t] if t in existing else conn.execute(
                "SELECT * FROM state_steward_list_items WHERE item_id=?", (added[t],)).fetchone()),
                "added": t in added} for t in texts]}
            return self._record(conn, "add_list_items", request_key, request_hash, result)

    def check_items(self, item_ids, done=True, project_id=None, request_key=None, turn=None, as_owner=False):
        """Check (done) or uncheck items; an id that is not on a list is answered in `unknown`."""
        ids = [i for i in dict.fromkeys(item_ids or []) if isinstance(i, str) and i]
        if not ids or len(ids) > LIST_LIMITS["per_check"]:
            raise StateGraphError(f"invalid_request: 1 to {LIST_LIMITS['per_check']} item_ids")
        moment = _now()
        with self.store.transaction(write=True) as conn:
            role, ref, label, via = self._caller(conn, turn, as_owner, project_id)
            request_hash = _hash(["check", ids, bool(done), via, ref])
            replay = self._keyed(conn, "check_list_items", request_key, request_hash)
            if replay is not None:
                return replay
            self._retain(conn, moment)
            changed, unknown = [], []
            for item_id in ids:
                if done:
                    # Re-checking a checked item changes nothing: who checked it is set only by
                    # the check that set done_at (never fills a who an unbind forgot, review D1).
                    hit = conn.execute("UPDATE state_steward_list_items SET done_at=COALESCE(done_at,?),"
                                       "done_by_role=CASE WHEN done_at IS NULL THEN ? ELSE done_by_role END,"
                                       "done_by_ref=CASE WHEN done_at IS NULL THEN ? ELSE done_by_ref END,"
                                       "done_by_label=CASE WHEN done_at IS NULL THEN ? ELSE done_by_label END "
                                       "WHERE item_id=? AND deleted_at IS NULL",
                                       (moment.isoformat(), role, ref, label, item_id)).rowcount
                else:
                    hit = conn.execute("UPDATE state_steward_list_items SET done_at=NULL,done_by_role=NULL,"
                                       "done_by_ref=NULL,done_by_label=NULL WHERE item_id=? AND deleted_at IS NULL",
                                       (item_id,)).rowcount
                if hit:
                    changed.append(self._item(conn, conn.execute(
                        "SELECT * FROM state_steward_list_items WHERE item_id=?", (item_id,)).fetchone()))
                else:
                    unknown.append(item_id)
            result = {"items": changed, "unknown": unknown}
            return self._record(conn, "check_list_items", request_key, request_hash, result)

    def remove_items(self, item_ids, project_id=None, request_key=None, as_owner=False):
        """Remove items (the owner's turn or dashboard; never a group turn)."""
        if self.public_view:
            raise StateGraphError("public_view_read_only: remove_list_items is not available to a group turn")
        ids = [i for i in dict.fromkeys(item_ids or []) if isinstance(i, str) and i]
        if not ids or len(ids) > LIST_LIMITS["per_check"]:
            raise StateGraphError(f"invalid_request: 1 to {LIST_LIMITS['per_check']} item_ids")
        moment = _now()
        with self.store.transaction(write=True) as conn:
            self._caller(conn, None, as_owner, project_id)
            request_hash = _hash(["remove", ids])
            replay = self._keyed(conn, "remove_list_items", request_key, request_hash)
            if replay is not None:
                return replay
            removed = [i for i in ids if conn.execute(
                "UPDATE state_steward_list_items SET deleted_at=? WHERE item_id=? AND deleted_at IS NULL",
                (moment.isoformat(), i)).rowcount]
            self._retain(conn, moment)
            result = {"removed": removed, "unknown": [i for i in ids if i not in removed]}
            return self._record(conn, "remove_list_items", request_key, request_hash, result)

    def clear_done(self, list=None):
        """The dashboard's 「清空已完成」: remove every checked item (of one list or both)."""
        self.steward._owner()
        if list is not None and list not in LISTS:
            raise StateGraphError("invalid_list: shopping or todo")
        moment = _now()
        with self.store.transaction(write=True) as conn:
            sql, args = ("UPDATE state_steward_list_items SET deleted_at=? WHERE done_at IS NOT NULL AND "
                         "deleted_at IS NULL"), [moment.isoformat()]
            if list is not None:
                sql += " AND list=?"
                args.append(list)
            cleared = conn.execute(sql, args).rowcount
            self._retain(conn, moment)
        return {"cleared": cleared}
