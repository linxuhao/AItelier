"""The owner's steward (plugin steps 13-14): its proposals and the owner's memory requests.

Proposals (design-steward 5). The steward only records an `open` row
(`create_steward_proposal`); the owner decides on the dashboard (or with an exact
phrase in a message to the steward, core.state_channels - decided_by then names the
channel and account), and the backend executes exactly the version the owner
approved. Status: open -> executing -> done | failed, or declined | withdrawn | superseded. Every terminal result the
owner caused (done, failed, declined) is delivered to the steward's driver inbox
in the SAME transaction that writes it, so it survives a restart; a result that
found no steward to deliver to keeps `notified_at` NULL and is delivered by the
startup scan (`recover`).

Memory requests (design-steward 3.4). The memory lives on the DSH host, outside
the backend's reach: an owner operation is a pending row, the steward's runtime
code (not its model) runs it and writes the result back. A result can carry the
owner's profile text, so it is kept only as long as the dashboard needs it: it
is replaced by `{"expired": true}` once the owner has read it, or
`RESULT_TTL_SECONDS` after completion, whichever comes first (lazily, on every
read and write of the table - no daemon). The payload is cleared on completion.

Schedules (design-steward 7.4, step 16). A reminder hands the steward an
instruction at a time (`once`) or on a 5-field cron expression, in its own time
zone, by the wall clock. The backend is the only place `next_run_at` is computed,
and `fire_schedule` the only place it advances: the steward's host fires a due row
with the `next_run_at` it saw, and exactly one such fire succeeds. A fire time
crossed while nobody fired is fired once, the next run counted from now, the
skipped runs reported as `missed`. Every change emits `steward_schedule_changed`
on the steward's project; the payload names the row only.

Group reminders (step 25 C11, design-steward 7.9.4): a schedule's `target` is `owner` (the
steward is woken with the instruction) or `family_group`: the instruction is then the
owner's exact words for the family group bound when the row was made (its channel,
group_ref and bound_at are copied onto the row), at most GROUP_NOTICE_CHARS characters,
exit-checked on every create and change that sets it (core.state_groups.exit_check). When
due the host posts it with `post_group_notice` (core.state_groups), which checks the row
fired for that due time (`last_due_at`): no model turn. Unbinding the group disables its
reminders; a new binding never revives them.

All three tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, key, now, text
from core.state_privacy import UntrustedDatabase, writer_only_read

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_proposals (
 proposal_id TEXT PRIMARY KEY,
 version INTEGER NOT NULL CHECK(version>0),
 kind TEXT NOT NULL CHECK(kind IN ('create_project','start_driver','stop_driver','archive_project','unarchive_project')),
 payload_json TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','executing','done','failed','declined','withdrawn','superseded')),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, supersedes TEXT,
 decided_at TEXT, decided_by TEXT, result_json TEXT, notified_at TEXT);
CREATE INDEX IF NOT EXISTS state_steward_proposals_status ON state_steward_proposals(status,created_at);
CREATE TABLE IF NOT EXISTS state_steward_memory_requests (
 request_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('profile_read','profile_update','forgotten_list','forget','restore','purge')),
 payload_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','done')),
 result_json TEXT, created_at TEXT NOT NULL, completed_at TEXT, created_by TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS state_steward_memory_requests_status ON state_steward_memory_requests(status,created_at);
CREATE TABLE IF NOT EXISTS state_steward_schedules (
 schedule_id TEXT PRIMARY KEY,
 version INTEGER NOT NULL CHECK(version>0),
 title TEXT NOT NULL, instruction TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('once','cron')),
 at TEXT, expr TEXT, tz TEXT NOT NULL,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 next_run_at TEXT, last_run_at TEXT, last_missed INTEGER NOT NULL DEFAULT 0,
 created_by TEXT NOT NULL CHECK(created_by IN ('owner','steward')),
 updated_by TEXT NOT NULL CHECK(updated_by IN ('owner','steward')),
 last_fired_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE INDEX IF NOT EXISTS state_steward_schedules_next ON state_steward_schedules(deleted_at,next_run_at);
"""
# Step 25 C11 (design-steward 7.9.4): the columns a group reminder adds, added to a table of any
# age (a fresh one is created without them too, so there is one path). `target` defaults to
# 'owner': every row written before it keeps its meaning.
TARGET_COLUMNS = (
    ("target", "TEXT NOT NULL DEFAULT 'owner' CHECK(target IN ('owner','family_group'))"),
    ("target_channel", "TEXT"), ("target_group_ref", "TEXT"), ("target_bound_at", "TEXT"),
    # The due time the row last fired for (last_run_at is the moment of the fire, never equal).
    ("last_due_at", "TEXT"))

KINDS = ("create_project", "start_driver", "stop_driver", "archive_project", "unarchive_project")
# Owner 2026-11-18: the steward may PROPOSE archiving / unarchiving a project; the
# owner approves on the card and the backend writes it as that owner. The steward
# itself keeps no archive permission (archive_project is not on its whitelist).
ARCHIVE_KINDS = ("archive_project", "unarchive_project")
# The kind CHECK a database created before the archive kinds carries; `_migrate_kinds`
# rebuilds such a table once with SCHEMA's CHECK.
OLD_KIND_CHECK = "CHECK(kind IN ('create_project','start_driver','stop_driver'))"
MEMORY_KINDS = ("profile_read", "profile_update", "forgotten_list", "forget", "restore", "purge")
# The payload keys each memory request kind may carry (memory-requests.mjs).
_MEMORY_KEYS = {"profile_read": set(), "profile_update": {"text", "expected_version", "reason"},
                "forgotten_list": set(), "forget": {"ids", "reason"}, "restore": {"id"},
                "purge": {"ids"}}
RESULT_TTL_SECONDS = 600
EXPIRED = {"expired": True}
INTERRUPTED = "执行中断，请重新提议"
MAX_JSON = 256 * 1024
# A driver in one of these phases with enabled=1 is (or is becoming) live.
LIVE_PHASES = ("starting", "running", "waiting", "dormant")
_UNSETTLED = ("reserved", "launching", "running", "paused", "unknown")


def steward_project():
    """The reserved project the steward runs in (core.dsh_driver.RESERVED_ROLES)."""
    from core.dsh_driver import RESERVED_ROLES
    return next(pid for pid, role in RESERVED_ROLES.items() if role == "steward")


def _bounded_json(value, label):
    encoded = canonical(value)
    if len(encoded) > MAX_JSON:
        raise StateGraphError(f"{label} is larger than {MAX_JSON} bytes")
    return encoded


def _hash(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _has_drivers(conn) -> bool:
    """A State-only database has no DSH driver table (and so no managed driver)."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_drivers'").fetchone() is not None


def stop_impact(conn, project_id) -> dict:
    """What stopping `project_id`'s driver would interrupt, from what the backend
    knows: the native children its runtime last reported (only while the driver
    is enabled in a live phase) and the external attempts it still owns."""
    row = conn.execute("SELECT driver_id,enabled,phase,subagents_json FROM dsh_drivers WHERE project_id=?",
                       (project_id,)).fetchone() if _has_drivers(conn) else None
    if row is None:
        return {"running_subagents": [], "unsettled_attempts": []}
    subagents = []
    if row["enabled"] and row["phase"] in LIVE_PHASES:
        try:
            subagents = [{"session_id": str(s)} for s in json.loads(row["subagents_json"] or "[]")]
        except ValueError:
            subagents = [{"session_id": "unreadable subagent list"}]
    attempts = [dict(r) for r in conn.execute(
        "SELECT attempt_id,external_id,status,lease_expires_at FROM state_attempts "
        f"WHERE project_id=? AND owner_driver_id=? AND status IN ({','.join('?' * len(_UNSETTLED))}) "
        "ORDER BY attempt_id LIMIT 200", (project_id, row["driver_id"], *_UNSETTLED))]
    return {"running_subagents": subagents, "unsettled_attempts": attempts}


def impact_is_empty(impact) -> bool:
    return not (impact["running_subagents"] or impact["unsettled_attempts"])


def _proposal(row) -> dict:
    return {"proposal_id": row["proposal_id"], "version": row["version"], "kind": row["kind"],
            "payload": json.loads(row["payload_json"]), "reason": row["reason"], "status": row["status"],
            "created_at": row["created_at"], "decided_at": row["decided_at"],
            "decided_by": row["decided_by"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None}


def _carrier_still_bound(conn, carrier):
    """M2 (design-steward 7.2): a chat decision re-reads its sender's binding inside
    the transaction that decides. Unbound since the message -> refused, row untouched."""
    if carrier is not None and carrier.get("channel"):
        from core.state_channels import require_bound
        require_bound(conn, carrier["channel"], carrier["sender_ref"])


class ProposalConflict(StateConflict):
    """The proposal is not in the state the caller expected; carries the current row."""

    def __init__(self, row: dict):
        super().__init__("proposal_changed")
        self.row = row


# ---------------------------------------------------------------- schedules: time
SCHEDULE_FLOOR = timedelta(minutes=15)
PREVIEW_RUNS = 5
# Counting the runs a long outage skipped stops here (inside a write transaction).
MISSED_CAP = 10000
# At most this many live reminders (design 7.4: a wake must stay bounded).
SCHEDULES_MAX = 50
# Step 25 C11 (7.9.4): of those, at most this many go to the family group, and a group
# reminder's text (the owner's exact words, posted as written) is at most this long.
GROUP_SCHEDULES_MAX = 20
GROUP_NOTICE_CHARS = 500
TARGETS = ("owner", "family_group")


def _now() -> datetime:
    """The schedules' clock (aware UTC); tests replace it."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def default_tz() -> str:
    """The zone of a schedule the steward creates without naming one."""
    return os.environ.get("AITELIER_OWNER_TZ") or "Europe/Paris"


class ScheduleInvalid(StateGraphError):
    """A schedule that cannot be saved: `code` is invalid_schedule, invalid_timezone,
    invalid_cron, too_frequent or at_in_past."""

    def __init__(self, code, reason):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


class ScheduleConflict(StateConflict):
    """`expected_version` is not the row's; carries the current row (also in the
    message, which is all a State caller receives)."""

    def __init__(self, row: dict):
        super().__init__("version_conflict: " + canonical(row))
        self.row = row


def _zone(tz):
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        raise ScheduleInvalid("invalid_timezone", f"{tz!r} is not an IANA time zone") from None


_DAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _weekdays(field):
    """A standard cron weekday field (0 and 7 are Sunday) as day names.
    APScheduler's from_crontab passes numbers through as ITS weekdays, where 0 is
    Monday, so `1-5` would run Tuesday to Saturday; names mean the same to both."""
    def day(token):
        if token in _DAYS:
            return _DAYS.index(token)
        if token.isdigit() and int(token) <= 7:
            return int(token)
        raise ScheduleInvalid("invalid_cron", f"weekday {token!r} is not 0-7 or sun-sat")
    days = set()
    for part in field.lower().split(","):
        span, _, step = part.partition("/")
        if span == "*":
            low, high = 0, 6
        elif "-" in span:
            low, high = (day(t) for t in span.split("-", 1))
        else:
            low = high = day(span)
            high = 7 if step else high
        if low > high or (step and not (step.isdigit() and int(step) > 0)):
            raise ScheduleInvalid("invalid_cron", f"weekday {part!r} is not a valid range or step")
        days |= {d % 7 for d in range(low, high + 1, int(step) if step else 1)}
    return ",".join(_DAYS[d] for d in sorted(days))


def _cron(expr):
    """The expression's trigger over the WALL CLOCK (naive local times carried as
    UTC); `_cron_after` maps a wall time to its instant."""
    from apscheduler.triggers.cron import CronTrigger
    if not isinstance(expr, str):
        raise ScheduleInvalid("invalid_cron", "expr must be a string")
    fields = expr.split()
    if len(fields) == 5 and fields[4] != "*":
        if fields[2] != "*":
            # Standard cron runs on EITHER restricted day field; APScheduler on both.
            raise ScheduleInvalid("invalid_cron", "day of month and day of week are both restricted; "
                                  "standard cron would run on either, so restrict only one")
        fields[4] = _weekdays(fields[4])
    try:
        return CronTrigger.from_crontab(" ".join(fields), timezone=timezone.utc)
    except ValueError as exc:
        raise ScheduleInvalid("invalid_cron", str(exc)) from None


def _cron_after(trigger, zone, after):
    """The expression's first run strictly after `after`, by the wall clock in `zone`.

    A wall time that does not exist (spring forward) maps with the offset before the
    change, as APScheduler does: `30 2 * * *` on 2027-03-28 in Europe/Paris runs at
    01:30Z (03:30 summer time). A wall time that occurs twice (fall back) runs once,
    at its first occurrence; a wall time whose first occurrence is not after `after`
    is past. (APScheduler's own zone-aware trigger answers a time at or before its
    start inside the repeated hour, so the wall clock is walked here.)"""
    wall = after.astimezone(zone).replace(tzinfo=None)
    while True:
        found = trigger.get_next_fire_time(None, (wall + timedelta(microseconds=1)).replace(tzinfo=timezone.utc))
        if found is None:
            return None
        wall = found.replace(tzinfo=None)
        moment = wall.replace(tzinfo=zone).astimezone(timezone.utc)
        if moment > after:
            return moment


def _cron_runs(trigger, zone, after, count):
    runs = []
    while len(runs) < count:
        run = _cron_after(trigger, zone, runs[-1] if runs else after)
        if run is None:
            break
        runs.append(run)
    return runs


def _shape(schedule, zone):
    """(kind, at, expr) of a schedule object, `at` as an ISO UTC string. A naive
    `at` is a wall time in the schedule's zone."""
    if not isinstance(schedule, dict) or schedule.get("kind") not in ("once", "cron"):
        raise ScheduleInvalid("invalid_schedule", "schedule must be {kind: 'once', at} or {kind: 'cron', expr}")
    if schedule["kind"] == "cron":
        if set(schedule) != {"kind", "expr"}:
            raise ScheduleInvalid("invalid_schedule", "a cron schedule is {kind: 'cron', expr}")
        _cron(schedule["expr"])
        return "cron", None, schedule["expr"].strip()
    if set(schedule) != {"kind", "at"} or not isinstance(schedule["at"], str):
        raise ScheduleInvalid("invalid_schedule", "a once schedule is {kind: 'once', at: ISO time}")
    try:
        at = datetime.fromisoformat(schedule["at"])
    except ValueError:
        raise ScheduleInvalid("invalid_schedule", "at must be an ISO 8601 time") from None
    if at.tzinfo is None:
        wall = at.replace(microsecond=0)
        at = wall.replace(tzinfo=zone)
        # A wall time the zone skips (spring forward) does not exist: refused, never
        # silently moved (design 7.4). A repeated one (fall back) is its first instant.
        if at.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != wall:
            raise ScheduleInvalid("at_in_dst_gap", f"{schedule['at']} does not exist in {zone.key}: the clocks skip it")
    return "once", _iso(at.replace(microsecond=0)), None


def _upcoming(kind, at, expr, zone, moment):
    """The next runs from `moment` of a schedule about to be saved, enforcing
    at_in_past, a cron that runs at all, and the 15-minute floor (checked over the
    next `PREVIEW_RUNS` runs only: a boundary, not a proof)."""
    if kind == "once":
        run = datetime.fromisoformat(at)
        if run <= moment:
            raise ScheduleInvalid("at_in_past", f"{at} is not in the future")
        return [run]
    runs = _cron_runs(_cron(expr), zone, moment, PREVIEW_RUNS)
    if not runs:
        raise ScheduleInvalid("invalid_cron", "the expression never runs")
    for earlier, later in zip(runs, runs[1:]):
        if later - earlier < SCHEDULE_FLOOR:
            raise ScheduleInvalid("too_frequent", f"runs at {_iso(earlier)} and {_iso(later)} are less than "
                                  f"{int(SCHEDULE_FLOOR.total_seconds() // 60)} minutes apart")
    return runs


def preview_schedule(schedule, tz) -> dict:
    """The next runs a schedule would have if saved now, in its own zone."""
    zone = _zone(tz)
    kind, at, expr = _shape(schedule, zone)
    return {"next_runs": [run.astimezone(zone).isoformat() for run in _upcoming(kind, at, expr, zone, _now())]}


def _overdue(row) -> int:
    """Seconds an enabled row is past its next run (0 when not): a stopped clock shows here."""
    if not row["enabled"] or row["next_run_at"] is None:
        return 0
    return max(0, int((_now() - datetime.fromisoformat(row["next_run_at"])).total_seconds()))


def _schedule(row) -> dict:
    schedule = {"kind": "once", "at": row["at"]} if row["kind"] == "once" else {"kind": "cron", "expr": row["expr"]}
    return {"schedule_id": row["schedule_id"], "version": row["version"], "title": row["title"],
            "instruction": row["instruction"], "schedule": schedule, "tz": row["tz"],
            "enabled": bool(row["enabled"]), "next_run_at": row["next_run_at"], "last_run_at": row["last_run_at"],
            "last_missed": row["last_missed"], "created_by": row["created_by"], "updated_by": row["updated_by"],
            "last_fired_by": row["last_fired_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            "overdue_seconds": _overdue(row), "target": row["target"], "target_channel": row["target_channel"],
            "target_group_ref": row["target_group_ref"], "target_bound_at": row["target_bound_at"],
            "last_due_at": row["last_due_at"]}


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _binding_live(conn, channel, group_ref, bound_at) -> bool:
    """The binding a group reminder recorded is still the live one (same channel, group and
    bound_at, not revoked): a rebinding of the same group is a new bound_at (7.9.4)."""
    if not _has_table(conn, "state_owner_groups"):
        return False
    return conn.execute("SELECT 1 FROM state_owner_groups WHERE channel=? AND group_ref=? AND bound_at=? AND "
                        "revoked_at IS NULL", (channel, group_ref, bound_at)).fetchone() is not None


def _schedule_out(conn, row) -> dict:
    """_schedule plus, for a group reminder, whether its group is still bound (the dashboard
    marks one whose group was unbound)."""
    out = _schedule(row)
    if row["target"] == "family_group":
        out["target_unbound"] = not _binding_live(conn, row["target_channel"], row["target_group_ref"],
                                                  row["target_bound_at"])
    return out


class StateSteward:
    def __init__(self, store, actor, driver_id=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.project_read_trusted = store.project_read_trusted
        # Starts or stops a driver for an approval made OUTSIDE the approve route (an
        # owner's chat phrase, core.state_channels): (kind, project_id, actor) -> the
        # driver row. The HTTP transport installs it (api.state_graph_routers); without
        # it such an approval is refused before anything changes.
        self.driver_runner = None
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                columns = {r[1] for r in conn.execute("PRAGMA table_info(state_steward_schedules)")}
                for name, ddl in TARGET_COLUMNS:
                    if name not in columns:
                        try:
                            conn.execute(f"ALTER TABLE state_steward_schedules ADD COLUMN {name} {ddl}")
                        except sqlite3.OperationalError as exc:  # another service added it first
                            if "duplicate column" not in str(exc):
                                raise
                conn.commit()
            self._migrate_kinds()
        # Step 25 C11: the family group's exit check and bindings (core.state_groups), set by
        # StateService once both exist; without it no group reminder can be written.
        self.groups = None

    def _migrate_kinds(self):
        """A proposals table created with OLD_KIND_CHECK refuses the archive kinds:
        rebuild it with SCHEMA's own CREATE TABLE (old table set aside, new one
        created, rows copied, old dropped, index recreated) in ONE write transaction.
        Checked again under the write lock; a table already migrated is never touched."""
        def old(conn):
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                               "AND name='state_steward_proposals'").fetchone()
            return row is not None and OLD_KIND_CHECK in row[0]
        with self.store.db.get_connection() as conn:
            if not old(conn):
                return
        columns = ("proposal_id,version,kind,payload_json,reason,status,created_by,created_at,supersedes,"
                   "decided_at,decided_by,result_json,notified_at")
        with self.store.transaction(write=True) as conn:
            if not old(conn):
                return
            conn.execute("ALTER TABLE state_steward_proposals RENAME TO state_steward_proposals_old_kinds")
            conn.execute(SCHEMA.split(";", 1)[0])  # the proposals table, with the current kind CHECK
            conn.execute(f"INSERT INTO state_steward_proposals({columns}) "
                         f"SELECT {columns} FROM state_steward_proposals_old_kinds")
            conn.execute("DROP TABLE state_steward_proposals_old_kinds")
            conn.execute("CREATE INDEX IF NOT EXISTS state_steward_proposals_status "
                         "ON state_steward_proposals(status,created_at)")

    # ---------------------------------------------------------------- identity
    def _steward(self, conn, project_id=None):
        """Fail closed: only the active steward-role driver bound to the steward's
        project (and, for a write, naming that project). Unreadable capabilities
        or binding refuse like a wrong role."""
        if not self.driver_id or not self.project_read_trusted:
            raise StateGraphError("steward_role_required")
        try:
            row = conn.execute("SELECT capabilities_json FROM drivers WHERE driver_id=? AND status='active'",
                               (self.driver_id,)).fetchone()
            capabilities = json.loads(row[0]) if row is not None else None
            bound = conn.execute("SELECT project_id FROM dsh_drivers WHERE driver_id=?",
                                 (self.driver_id,)).fetchone()
        except (sqlite3.Error, ValueError, TypeError):
            raise StateGraphError("steward_role_required") from None
        if (not isinstance(capabilities, dict) or capabilities.get("role") != "steward"
                or bound is None or bound[0] != steward_project()):
            raise StateGraphError("steward_role_required")
        if project_id is not None and project_id != steward_project():
            raise StateGraphError(f"steward_project_required: project_id must be {steward_project()}")

    def is_steward(self) -> bool:
        with self.store.transaction() as conn:
            try:
                self._steward(conn)
            except StateGraphError:
                return False
        return True

    def is_owner_session(self) -> bool:
        """The owner's own BROWSER session: an `owner:` actor that is no driver.

        The conversation doors (core.driver_inbox.DriverInbox._project_user) are
        deliberately WIDER: they also admit the owner's terminal, the local CLI's
        own credential (kind 'cli_owner', actor core.drivers.CLI_OWNER_ACTOR),
        whose actor carries no `owner:` prefix. Approving or declining a steward
        proposal stays the browser session's alone - that is the whole point of
        keying this door on the prefix.
        """
        return (not self.driver_id and self.project_read_trusted is True
                and isinstance(self.actor, str) and self.actor.startswith("owner:"))

    # ---------------------------------------------------------------- idempotency
    def _replay(self, conn, operation, request_key, payload_hash):
        row = conn.execute("SELECT payload_json,result_json FROM driver_inbox_requests "
                           "WHERE actor=? AND operation=? AND request_key=?",
                           (self.actor, operation, request_key)).fetchone()
        if row:
            if row["payload_json"] != payload_hash:
                raise StateGraphError("idempotency_conflict: request_key was already used with a "
                                      "different payload; use a new request_key")
            return json.loads(row["result_json"])
        return None

    def _record(self, conn, operation, request_key, payload_hash, result):
        # Only the payload's digest is kept: a memory result can carry the
        # owner's profile text, which must not outlive its own row's expiry.
        conn.execute("INSERT INTO driver_inbox_requests VALUES(?,?,?,?,?)",
                     (self.actor, operation, request_key, payload_hash, json.dumps(result)))
        return result

    # ---------------------------------------------------------------- proposals
    @staticmethod
    def _validate(kind, payload) -> dict:
        from pydantic import BaseModel, ConfigDict, Field, ValidationError
        from core.state_commands import _KEY_PATTERN, NewNode

        class Driver(BaseModel):
            model_config = ConfigDict(extra="forbid", strict=True)
            project_id: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)

        class CreateProject(Driver):
            title: str = Field(min_length=1, max_length=400)
            goal: str = Field(min_length=1, max_length=4000)
            # add_nodes' own node shape: {key, goal, acceptance, ...}.
            seed_nodes: list[NewNode] = Field(default_factory=list, max_length=200)

        class Archive(Driver):
            reason: str | None = Field(default=None, max_length=4000)

        model = CreateProject if kind == "create_project" else Archive if kind in ARCHIVE_KINDS else Driver
        try:
            parsed = model.model_validate(payload)
        except ValidationError as exc:
            details = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]}
                       for e in exc.errors(include_input=False)[:10]]
            raise StateGraphError(f"invalid_payload: {details}") from None
        out = parsed.model_dump()
        if kind == "create_project":
            out["seed_nodes"] = [node.model_dump(exclude_unset=True) for node in parsed.seed_nodes]
        if kind in ARCHIVE_KINDS:
            # The same rule archive_project applies to its reason; absent stays absent.
            out = {"project_id": out["project_id"],
                   **({"reason": text(out["reason"], "archive reason", 4000)} if out["reason"] is not None else {})}
        return out

    def _get(self, conn, proposal_id):
        row = conn.execute("SELECT * FROM state_steward_proposals WHERE proposal_id=?",
                           (proposal_id,)).fetchone()
        if row is None:
            raise StateNotFound("unknown_proposal")
        return row

    def create_proposal(self, project_id, request_key, kind, payload, reason, supersedes=None):
        """Record one OPEN proposal by the steward. Executes nothing."""
        if kind not in KINDS:
            raise StateGraphError(f"unknown_kind: kind must be one of {', '.join(KINDS)}")
        payload = self._validate(kind, payload)
        reason = reason.strip()
        if not reason:
            raise StateGraphError("reason must be nonempty")
        request_hash = _hash([kind, payload, reason, supersedes])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "create_steward_proposal", request_key, request_hash)
            if replay is not None:
                return replay
            target = payload["project_id"]
            if kind in ARCHIVE_KINDS and target == steward_project():
                raise StateGraphError(f"invalid_project: {target} is the steward's own project")
            exists = conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (target,)).fetchone()
            if kind == "create_project" and exists:
                raise StateConflict(f"project_exists: {target} is already a project")
            if kind != "create_project" and not exists:
                raise StateNotFound(f"unknown_project: {target}")
            if kind == "stop_driver":
                # What it would interrupt as the steward saw it; recomputed at approval.
                payload["impact"] = stop_impact(conn, target)
            if kind in ARCHIVE_KINDS:
                archived = self.store._archived(conn, target)
                if kind == "archive_project" and archived:
                    raise StateConflict(f"already_archived: {target} is already archived")
                if kind == "unarchive_project" and not archived:
                    raise StateConflict(f"not_archived: {target} is not archived")
            stamp, proposal_id = now(), "prop-" + uuid4().hex[:20]
            if supersedes is not None:
                old = self._get(conn, supersedes)
                if old["status"] != "open" or old["created_by"] != self.actor:
                    raise StateConflict("supersedes must name one of your open proposals")
                conn.execute("UPDATE state_steward_proposals SET status='superseded',version=version+1,"
                             "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                             (stamp, self.actor, canonical({"superseded_by": proposal_id}), supersedes))
            conn.execute("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,"
                         "status,created_by,created_at,supersedes) VALUES(?,1,?,?,?,'open',?,?,?)",
                         (proposal_id, kind, _bounded_json(payload, "payload"), reason, self.actor, stamp,
                          supersedes))
            return self._record(conn, "create_steward_proposal", request_key, request_hash,
                                {"proposal_id": proposal_id, "version": 1, "status": "open"})

    def withdraw_proposal(self, project_id, request_key, proposal_id, reason):
        """The steward takes back one of its own OPEN proposals."""
        reason = reason.strip()
        if not reason:
            raise StateGraphError("reason must be nonempty")
        request_hash = _hash([proposal_id, reason])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "withdraw_steward_proposal", request_key, request_hash)
            if replay is not None:
                return replay
            row = self._get(conn, proposal_id)
            if row["created_by"] != self.actor:
                raise StateGraphError("not_your_proposal")
            if row["status"] != "open":
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='withdrawn',version=version+1,"
                         "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                         (now(), self.actor, canonical({"reason": reason}), proposal_id))
            result = _proposal(self._get(conn, proposal_id))
            return self._record(conn, "withdraw_steward_proposal", request_key, request_hash,
                                {"proposal_id": proposal_id, "version": result["version"],
                                 "status": "withdrawn"})

    @writer_only_read("list_steward_proposals")
    def list_proposals(self, status="open", limit=50):
        where = "" if status == "all" else "WHERE status='open'"
        with self.store.transaction() as conn:
            rows = conn.execute(f"SELECT * FROM state_steward_proposals {where} "
                                "ORDER BY created_at DESC, proposal_id DESC LIMIT ?", (limit,)).fetchall()
        return {"proposals": [_proposal(r) for r in rows]}

    @writer_only_read("list_steward_proposals")
    def get_proposal(self, proposal_id):
        with self.store.transaction() as conn:
            return _proposal(self._get(conn, proposal_id))

    @writer_only_read("list_steward_proposals")
    def impact(self, proposal_id):
        """What stopping the proposal's target driver would interrupt, now."""
        with self.store.transaction() as conn:
            row = _proposal(self._get(conn, proposal_id))
            return stop_impact(conn, row["payload"]["project_id"])

    def _owner(self):
        if not self.is_owner_session():
            raise StateGraphError("owner_session_required")

    def decline(self, proposal_id, expected_version, reason=None):
        self._owner()
        return self.decide_decline(proposal_id, expected_version, self.actor,
                                   {"reason": reason} if reason else {})

    def decide_decline(self, proposal_id, expected_version, decided_by, record, carrier=None):
        """open -> declined. The owner's button calls it through `decline`; an owner's
        chat phrase (core.state_channels) calls it with the phrase's `carrier`."""
        with self.store.transaction(write=True) as conn:
            _carrier_still_bound(conn, carrier)
            row = self._get(conn, proposal_id)
            if row["status"] != "open" or row["version"] != expected_version:
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='declined',version=version+1,"
                         "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=?",
                         (now(), decided_by, canonical(record), proposal_id))
            self._wake(conn, proposal_id)
            out = _proposal(self._get(conn, proposal_id))
        self._notify()
        return out

    def begin_approval(self, proposal_id, expected_version):
        """open -> executing, atomically with the status and version check. Exactly
        one of any number of concurrent approvals gets past here."""
        self._owner()
        return self.decide_begin(proposal_id, expected_version, self.actor, None)

    def decide_begin(self, proposal_id, expected_version, decided_by, record, carrier=None):
        """`begin_approval` for a decider already established: the owner's session, or
        an owner's exact chat phrase (core.state_channels), whose `record` is kept in
        result_json and whose `carrier` binding is re-read in THIS transaction (M2)."""
        with self.store.transaction(write=True) as conn:
            _carrier_still_bound(conn, carrier)
            row = self._get(conn, proposal_id)
            if row["status"] != "open" or row["version"] != expected_version:
                raise ProposalConflict(_proposal(row))
            conn.execute("UPDATE state_steward_proposals SET status='executing',version=version+1,"
                         "decided_at=?,decided_by=?,result_json=? WHERE proposal_id=? AND status='open' "
                         "AND version=?", (now(), decided_by, canonical(record) if record else None,
                                           proposal_id, expected_version))
            return _proposal(self._get(conn, proposal_id))

    def execute_create_project(self, proposal_id):
        """Run an EXECUTING create_project proposal: the project, its seed nodes and
        the proposal's `done` in ONE transaction, only if the project does not exist.
        Any failure rolls all of it back; the proposal is then `failed` (separately)."""
        try:
            with self.store.transaction(write=True) as conn:
                row = self._get(conn, proposal_id)
                if row["status"] != "executing" or row["kind"] != "create_project":
                    return _proposal(row)
                payload = json.loads(row["payload_json"])
                project_id = payload["project_id"]
                if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (project_id,)).fetchone():
                    raise StateConflict(f"project_id {project_id} 已被占用")
                self.store._create_project(conn, key(project_id), text(payload["title"], "title", 400), None)
                created = self.store._add(conn, project_id, payload["seed_nodes"]) if payload["seed_nodes"] else []
                self._finish(conn, proposal_id, "done", {"project_id": project_id, "created_nodes": created})
                out = _proposal(self._get(conn, proposal_id))
        except Exception as exc:  # noqa: BLE001 - any failure is the proposal's result
            return self.fail(proposal_id, f"{type(exc).__name__}: {exc}" if not isinstance(exc, StateGraphError)
                             else str(exc))
        self._notify()
        return out

    def execute_archive(self, proposal_id):
        """Run an EXECUTING archive_project / unarchive_project proposal: the archive
        state checked again, the store's archive write (recorded as the owner who
        approved it) and the proposal's `done` in ONE transaction. A project whose
        state changed since the proposal fails it (separately), untouched."""
        try:
            with self.store.transaction(write=True) as conn:
                row = self._get(conn, proposal_id)
                if row["status"] != "executing" or row["kind"] not in ARCHIVE_KINDS:
                    return _proposal(row)
                payload = json.loads(row["payload_json"])
                project_id, archived = payload["project_id"], row["kind"] == "archive_project"
                if self.store._archived(conn, project_id) == archived:
                    raise StateConflict(f"already_archived: {project_id} is already archived" if archived
                                        else f"not_archived: {project_id} is not archived")
                self.store._set_project_archive(conn, project_id, archived, row["decided_by"],
                                                payload.get("reason", ""))
                self._finish(conn, proposal_id, "done", {"archived": archived, "project_id": project_id})
                out = _proposal(self._get(conn, proposal_id))
        except Exception as exc:  # noqa: BLE001 - any failure is the proposal's result
            return self.fail(proposal_id, f"{type(exc).__name__}: {exc}" if not isinstance(exc, StateGraphError)
                             else str(exc))
        self._notify()
        return out

    def finish(self, proposal_id, status, result):
        """Write the terminal state of an EXECUTING proposal (and wake the steward)."""
        with self.store.transaction(write=True) as conn:
            row = self._get(conn, proposal_id)
            if row["status"] != "executing":
                return _proposal(row)
            self._finish(conn, proposal_id, status, result)
            out = _proposal(self._get(conn, proposal_id))
        self._notify()
        return out

    def fail(self, proposal_id, error):
        return self.finish(proposal_id, "failed", {"error": str(error)[:4000]})

    def _finish(self, conn, proposal_id, status, result):
        previous = self._get(conn, proposal_id)["result_json"]
        merged = {**(json.loads(previous) if previous else {}), **result}
        conn.execute("UPDATE state_steward_proposals SET status=?,version=version+1,result_json=? "
                     "WHERE proposal_id=?", (status, canonical(merged), proposal_id))
        self._wake(conn, proposal_id)

    def record_confirmation(self, proposal_id, confirm_interrupt, impact):
        """Keep the owner's confirmation (and what it covered) on the executing row."""
        with self.store.transaction(write=True) as conn:
            # Merged: a chat approval's record (who, which channel, which phrase) stays.
            previous = self._get(conn, proposal_id)["result_json"]
            conn.execute("UPDATE state_steward_proposals SET result_json=? WHERE proposal_id=? "
                         "AND status='executing'",
                         (canonical({**(json.loads(previous) if previous else {}),
                                     "confirm_interrupt": bool(confirm_interrupt), "impact": impact}),
                          proposal_id))

    # ---------------------------------------------------------------- the steward's wake
    def _wake(self, conn, proposal_id):
        """Deliver one terminal result to the steward's inbox on the caller's
        connection. No steward driver yet: `notified_at` stays NULL (see recover)."""
        from core.driver_inbox import deliver_notice
        row = self._get(conn, proposal_id)
        target = conn.execute("SELECT driver_id FROM dsh_drivers WHERE project_id=?",
                              (steward_project(),)).fetchone() if _has_drivers(conn) else None
        if target is None:
            return
        subject = f"Proposal {proposal_id} ({row['kind']}): {row['status']}"
        body = (f"Your proposal {proposal_id} ({row['kind']}) is {row['status']}.\n"
                f"Result: {row['result_json'] or '{}'}")
        message = {"target_driver_id": target[0], "sender_driver_id": None, "project_id": steward_project(),
                   "kind": "note", "subject": subject, "body": body[:8000], "refs": {}}
        try:
            delivered = deliver_notice(conn, message)
        except StateGraphError:
            # The body guard refused something in the result text: say so, never lose the wake.
            delivered = deliver_notice(conn, {**message, "body": (
                f"Your proposal {proposal_id} ({row['kind']}) is {row['status']}. The result text was "
                "withheld by the message guard; read it with list_steward_proposals.")})
        if "skipped" not in delivered:
            conn.execute("UPDATE state_steward_proposals SET notified_at=? WHERE proposal_id=?",
                         (now(), proposal_id))

    def _notify(self):
        from core.state_changes import notify
        notify(self.store.db.db_path)

    # ---------------------------------------------------------------- startup scan
    def recover(self, drivers=None):
        """After a restart: settle every proposal left `executing`, then deliver every
        terminal result no steward received. create_project runs again under "only
        if the project does not exist" (its execution was one transaction, so a crash
        left no half-made project); start/stop_driver are judged by the driver's
        current phase, else failed; archive/unarchive by the project's archive state
        (done if it already matches, else run again - also one transaction).
        `drivers` is the DshDrivers manager (or None)."""
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT proposal_id,kind,payload_json FROM state_steward_proposals WHERE status='executing'")]
        for row in rows:
            if row["kind"] == "create_project":
                self.execute_create_project(row["proposal_id"])
                continue
            target = json.loads(row["payload_json"])["project_id"]
            if row["kind"] in ARCHIVE_KINDS:
                archived = row["kind"] == "archive_project"
                if self.store.is_project_archived(target) == archived:
                    self.finish(row["proposal_id"], "done",
                                {"archived": archived, "project_id": target, "recovered": True})
                else:
                    self.execute_archive(row["proposal_id"])
                continue
            current = drivers.get(target) if drivers is not None else None
            if current is not None and row["kind"] == "start_driver" and current["enabled"] \
                    and current["phase"] in LIVE_PHASES:
                self.finish(row["proposal_id"], "done", {"driver": drivers.public(current), "recovered": True})
            elif current is not None and row["kind"] == "stop_driver" and not current["enabled"] \
                    and current["phase"] in ("stopped", "stopping"):
                self.finish(row["proposal_id"], "done", {"driver": drivers.public(current), "recovered": True})
            else:
                self.fail(row["proposal_id"], INTERRUPTED)
        with self.store.transaction(write=True) as conn:
            for (proposal_id,) in conn.execute(
                    "SELECT proposal_id FROM state_steward_proposals WHERE notified_at IS NULL "
                    "AND status IN ('done','failed','declined') ORDER BY created_at").fetchall():
                self._wake(conn, proposal_id)
        self._notify()
        return {"settled": len(rows)}

    # ---------------------------------------------------------------- memory requests
    @staticmethod
    def _expire(conn):
        """Drop results the dashboard no longer needs (RESULT_TTL_SECONDS after completion)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RESULT_TTL_SECONDS)).isoformat()
        conn.execute("UPDATE state_steward_memory_requests SET result_json=? WHERE status='done' "
                     "AND completed_at<? AND result_json IS NOT ?", (canonical(EXPIRED), cutoff, canonical(EXPIRED)))

    def create_memory_request(self, kind, payload):
        """The owner's memory operation, as one pending row the steward's runtime runs."""
        self._owner()
        if kind not in MEMORY_KINDS:
            raise StateGraphError(f"unknown_kind: kind must be one of {', '.join(MEMORY_KINDS)}")
        if not isinstance(payload, dict) or set(payload) - _MEMORY_KEYS[kind]:
            raise StateGraphError(f"invalid_payload: {kind} takes {sorted(_MEMORY_KEYS[kind]) or 'no keys'}")
        request_id, stamp, home = "memreq-" + uuid4().hex[:20], now(), steward_project()
        with self.store.transaction(write=True) as conn:
            self._expire(conn)
            conn.execute("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,"
                         "created_at,created_by) VALUES(?,?,?,'pending',?,?)",
                         (request_id, kind, _bounded_json(payload, "payload"), stamp, self.actor))
            # The steward's runtime wakes on this event and runs the request; the
            # payload stays in the private row, never in the event.
            if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (home,)).fetchone():
                self.store._event(conn, home, None, "steward_memory_request_created",
                                  {"request_id": request_id, "kind": kind, "actor": self.actor})
        self._notify()
        return {"request_id": request_id, "status": "pending"}

    def read_memory_request(self, request_id):
        """The owner reads one request. A done result is handed out once, then expires."""
        self._owner()
        with self.store.transaction(write=True) as conn:
            self._expire(conn)
            row = conn.execute("SELECT * FROM state_steward_memory_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise StateNotFound("unknown_memory_request")
            result = json.loads(row["result_json"]) if row["result_json"] else None
            if row["status"] == "done" and result != EXPIRED:
                conn.execute("UPDATE state_steward_memory_requests SET result_json=? WHERE request_id=?",
                             (canonical(EXPIRED), request_id))
        return {"request_id": row["request_id"], "kind": row["kind"], "status": row["status"],
                "result": result}

    @writer_only_read("list_steward_memory_requests")
    def list_memory_requests(self, status="pending", limit=50):
        with self.store.transaction(write=True) as conn:
            self._steward(conn)
            self._expire(conn)
            rows = conn.execute("SELECT request_id,kind,payload_json FROM state_steward_memory_requests "
                                "WHERE status=? ORDER BY created_at, request_id LIMIT ?", (status, limit)).fetchall()
        return {"requests": [{"request_id": r["request_id"], "kind": r["kind"],
                              "payload": json.loads(r["payload_json"])} for r in rows]}

    def complete_memory_request(self, project_id, request_key, request_id, result):
        """pending -> done with the steward runtime's result; the payload is cleared."""
        encoded = _bounded_json(result, "result")
        request_hash = _hash([request_id, result])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._replay(conn, "complete_steward_memory_request", request_key, request_hash)
            if replay is not None:
                return replay
            row = conn.execute("SELECT status FROM state_steward_memory_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise StateNotFound("unknown_memory_request")
            if row["status"] != "pending":
                raise StateConflict("memory_request_not_pending")
            conn.execute("UPDATE state_steward_memory_requests SET status='done',result_json=?,payload_json='{}',"
                         "completed_at=? WHERE request_id=?", (encoded, now(), request_id))
            self._expire(conn)
            return self._record(conn, "complete_steward_memory_request", request_key, request_hash,
                                {"request_id": request_id, "status": "done"})

    # ---------------------------------------------------------------- schedules
    # The steward writes through its State commands (request_key, its own project);
    # the owner through the HTTP routes (as_owner=True, the owner-session check).
    def _author(self, conn, project_id, as_owner):
        if as_owner:
            self._owner()
            return "owner"
        self._steward(conn, project_id)
        return "steward"

    def _keyed(self, conn, operation, request_key, request_hash):
        return self._replay(conn, operation, request_key, request_hash) if request_key is not None else None

    def _schedule_changed(self, conn, schedule_id):
        """Emit steward_schedule_changed on the steward's project (when it exists)."""
        home = steward_project()
        if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (home,)).fetchone():
            self.store._event(conn, home, None, "steward_schedule_changed", {"schedule_id": schedule_id})

    def _done(self, conn, operation, request_key, request_hash, schedule_id, result):
        """Emit the change on the steward's project, then record the result."""
        self._schedule_changed(conn, schedule_id)
        return self._record(conn, operation, request_key, request_hash, result) if request_key is not None else result

    @staticmethod
    def _schedule_row(conn, schedule_id):
        row = conn.execute("SELECT * FROM state_steward_schedules WHERE schedule_id=? AND deleted_at IS NULL",
                           (schedule_id,)).fetchone()
        if row is None:
            raise StateNotFound("unknown_schedule")
        return row

    @writer_only_read("list_steward_schedules")
    def list_schedules(self, include_disabled=True):
        """Every live schedule, soonest first (no next run last). The owner's session
        or the steward only."""
        where = "" if include_disabled else " AND enabled=1"
        with self.store.transaction() as conn:
            if not self.is_owner_session():
                self._steward(conn)
            rows = conn.execute("SELECT * FROM state_steward_schedules WHERE deleted_at IS NULL" + where +
                                " ORDER BY next_run_at IS NULL, next_run_at, created_at, schedule_id").fetchall()
            return {"schedules": [_schedule_out(conn, r) for r in rows]}

    # ---- group reminders (step 25 C11, design-steward 7.9.4)
    @staticmethod
    def _target(target, channel):
        if target not in TARGETS:
            raise ScheduleInvalid("invalid_target", "target is owner or family_group")
        if channel is not None and channel not in ("slack", "wechat"):
            raise ScheduleInvalid("invalid_target", "channel is slack or wechat")
        if target == "owner" and channel is not None:
            raise ScheduleInvalid("invalid_target", "a channel names the family group of a family_group reminder")
        return target

    @staticmethod
    def _bind_target(conn, channel):
        """The binding a new group reminder records: the live group of `channel`, or of the one
        channel that has a group (v1: one group per channel). None bound: no_group_bound;
        groups on both channels and no channel named: channel_required."""
        rows = conn.execute("SELECT channel,group_ref,bound_at FROM state_owner_groups WHERE revoked_at IS NULL "
                            "ORDER BY channel").fetchall() if _has_table(conn, "state_owner_groups") else []
        if channel is not None:
            rows = [r for r in rows if r["channel"] == channel]
        if not rows:
            raise ScheduleInvalid("no_group_bound", "no family group is bound" + (f" on {channel}" if channel else ""))
        if len(rows) > 1:
            raise ScheduleInvalid("channel_required", "a group is bound on each channel: name the channel")
        return rows[0]["channel"], rows[0]["group_ref"], rows[0]["bound_at"]

    def _check_notice(self, conn, instruction):
        """A group reminder's text: the owner's exact words, at most GROUP_NOTICE_CHARS, and
        through the group's exit check (7.7.7) right now, so the owner learns at once."""
        if len(instruction) > GROUP_NOTICE_CHARS:
            raise ScheduleInvalid("notice_too_long", f"a family group reminder is at most {GROUP_NOTICE_CHARS} "
                                                     "characters: it is posted as written")
        if self.groups is None:
            raise ScheduleInvalid("no_group_bound", "no family group is available here")
        from core import state_groups
        reason = self.groups.exit_check(conn, instruction, state_groups._now())
        if reason is not None:
            raise ScheduleInvalid("withheld", f"{reason}: {state_groups.WITHHELD_REASONS[reason]}")

    @staticmethod
    def _group_room(conn, excluding=None):
        live = conn.execute("SELECT COUNT(*) FROM state_steward_schedules WHERE deleted_at IS NULL AND "
                            "target='family_group' AND schedule_id IS NOT ?", (excluding,)).fetchone()[0]
        if live >= GROUP_SCHEDULES_MAX:
            raise ScheduleInvalid("too_many_group_reminders",
                                  f"at most {GROUP_SCHEDULES_MAX} family group reminders; delete one first")

    def create_schedule(self, title, instruction, schedule, tz=None, enabled=True, project_id=None,
                        request_key=None, as_owner=False, target="owner", channel=None):
        title, instruction = text(title, "title", 200), text(instruction, "instruction", 2000)
        target = self._target(target or "owner", channel)
        tz = tz or default_tz()
        zone = _zone(tz)
        kind, at, expr = _shape(schedule, zone)
        # A reminder for the owner hashes as it always did (a key replayed across the upgrade).
        request_hash = _hash(["create", title, instruction, schedule, tz, enabled]
                             + ([] if target == "owner" else [target, channel]))
        with self.store.transaction(write=True) as conn:
            by = self._author(conn, project_id, as_owner)
            replay = self._keyed(conn, "create_steward_schedule", request_key, request_hash)
            if replay is not None:
                return replay
            moment = _now()
            runs = _upcoming(kind, at, expr, zone, moment)
            live = conn.execute("SELECT COUNT(*) FROM state_steward_schedules WHERE deleted_at IS NULL").fetchone()[0]
            if live >= SCHEDULES_MAX:
                raise ScheduleInvalid("too_many_schedules", f"at most {SCHEDULES_MAX} reminders; delete one first")
            bound = (None, None, None)
            if target == "family_group":
                bound = self._bind_target(conn, channel)
                self._group_room(conn)
                self._check_notice(conn, instruction)
            schedule_id, stamp = "sch-" + uuid4().hex[:12], _iso(moment)
            conn.execute("INSERT INTO state_steward_schedules(schedule_id,version,title,instruction,kind,at,expr,tz,"
                         "enabled,next_run_at,created_by,updated_by,created_at,updated_at,target,target_channel,"
                         "target_group_ref,target_bound_at) VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (schedule_id, title, instruction, kind, at, expr, tz, int(bool(enabled)),
                          _iso(runs[0]) if enabled else None, by, by, stamp, stamp, target, *bound))
            out = self._done(conn, "create_steward_schedule", request_key, request_hash, schedule_id,
                             _schedule_out(conn, self._schedule_row(conn, schedule_id)))
        self._notify()
        return out

    def update_schedule(self, schedule_id, expected_version, title=None, instruction=None, schedule=None, tz=None,
                        enabled=None, project_id=None, request_key=None, as_owner=False, target=None, channel=None):
        """Change what is given. A new schedule or zone, or enabling, recomputes the
        next run from now; disabling clears it; otherwise it is kept. A `target` (or a
        `channel`) given re-points the reminder: family_group records the group bound NOW.
        A `channel` alone keeps the row's target: it re-points a family_group row and is
        invalid_target on an owner row (review F1: an owner's private instruction never becomes
        group text by a channel). A group reminder is exit-checked when its text changes, when
        it becomes one and when it is enabled; it is never enabled while the group it recorded
        is unbound (a new binding never revives it, 7.9.4)."""
        if target is not None:
            target = self._target(target, channel)
        elif channel is not None:
            self._target("family_group", channel)
        request_hash = _hash(["update", schedule_id, expected_version, title, instruction, schedule, tz, enabled]
                             + ([] if target is None and channel is None else [target, channel]))
        with self.store.transaction(write=True) as conn:
            by = self._author(conn, project_id, as_owner)
            replay = self._keyed(conn, "update_steward_schedule", request_key, request_hash)
            if replay is not None:
                return replay
            row = self._schedule_row(conn, schedule_id)
            if row["version"] != expected_version:
                raise ScheduleConflict(_schedule(row))
            current = _schedule(row)
            new_text = instruction is not None
            title = text(title, "title", 200) if title is not None else row["title"]
            instruction = text(instruction, "instruction", 2000) if instruction is not None else row["instruction"]
            tz = tz if tz is not None else row["tz"]
            zone = _zone(tz)
            kind, at, expr = _shape(schedule if schedule is not None else current["schedule"], zone)
            enabled = bool(row["enabled"]) if enabled is None else enabled
            moment = _now()
            bound = (row["target_channel"], row["target_group_ref"], row["target_bound_at"])
            if target is None and channel is not None:
                if row["target"] != "family_group":
                    raise ScheduleInvalid("invalid_target", "a channel names the family group of a family_group "
                                                            "reminder; this one is for the owner")
                target = "family_group"
            repointed = target is not None
            target = target if repointed else row["target"]
            if target == "owner":
                bound = (None, None, None)
            elif repointed:
                bound = self._bind_target(conn, channel)
                if row["target"] != "family_group":
                    self._group_room(conn, excluding=schedule_id)
            if target == "family_group":
                if enabled and not _binding_live(conn, *bound):
                    raise ScheduleInvalid("group_unbound", "the family group this reminder was made for is no longer "
                                                           "bound; re-point it to the bound group")
                if new_text or repointed or (enabled and not row["enabled"]):
                    self._check_notice(conn, instruction)
            if (kind, at, expr, tz) != (row["kind"], row["at"], row["expr"], row["tz"]) or (
                    enabled and not row["enabled"]):
                upcoming = _upcoming(kind, at, expr, zone, moment)[0]
                next_run = _iso(upcoming) if enabled else None
            else:
                next_run = row["next_run_at"] if enabled else None
            conn.execute("UPDATE state_steward_schedules SET version=version+1,title=?,instruction=?,kind=?,at=?,"
                         "expr=?,tz=?,enabled=?,next_run_at=?,updated_by=?,updated_at=?,target=?,target_channel=?,"
                         "target_group_ref=?,target_bound_at=? WHERE schedule_id=?",
                         (title, instruction, kind, at, expr, tz, int(enabled), next_run, by, _iso(moment), target,
                          *bound, schedule_id))
            out = self._done(conn, "update_steward_schedule", request_key, request_hash, schedule_id,
                             _schedule_out(conn, self._schedule_row(conn, schedule_id)))
        self._notify()
        return out

    def delete_schedule(self, schedule_id, expected_version, project_id=None, request_key=None, as_owner=False):
        """Soft delete: the row is never listed and never fires again."""
        request_hash = _hash(["delete", schedule_id, expected_version])
        with self.store.transaction(write=True) as conn:
            by = self._author(conn, project_id, as_owner)
            replay = self._keyed(conn, "delete_steward_schedule", request_key, request_hash)
            if replay is not None:
                return replay
            row = self._schedule_row(conn, schedule_id)
            if row["version"] != expected_version:
                raise ScheduleConflict(_schedule(row))
            stamp = _iso(_now())
            conn.execute("UPDATE state_steward_schedules SET version=version+1,enabled=0,next_run_at=NULL,"
                         "updated_by=?,updated_at=?,deleted_at=? WHERE schedule_id=?", (by, stamp, stamp, schedule_id))
            out = self._done(conn, "delete_steward_schedule", request_key, request_hash, schedule_id,
                             {"deleted": True, "schedule_id": schedule_id})
        self._notify()
        return out

    def fire_schedule(self, schedule_id, expected_next_run_at, project_id=None, request_key=None):
        """The steward's host fires a due row: in ONE transaction the row must be live,
        enabled, its next_run_at the one the host saw, and due. Exactly one fire of a
        given next_run_at succeeds; any other answers fired: false with the row."""
        try:
            expected = datetime.fromisoformat(expected_next_run_at)
        except (TypeError, ValueError):
            raise StateGraphError("expected_next_run_at must be an ISO 8601 time") from None
        if expected.tzinfo is None:
            raise StateGraphError("expected_next_run_at must carry its UTC offset")
        request_hash = _hash(["fire", schedule_id, expected_next_run_at])
        with self.store.transaction(write=True) as conn:
            self._steward(conn, project_id)
            replay = self._keyed(conn, "fire_steward_schedule", request_key, request_hash)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM state_steward_schedules WHERE schedule_id=? AND deleted_at IS NULL",
                               (schedule_id,)).fetchone()
            moment = _now()
            # Compared as instants, never as strings: a reformatted timestamp still matches.
            reason = ("deleted" if row is None else "disabled" if not row["enabled"] or row["next_run_at"] is None
                      else "schedule_moved" if datetime.fromisoformat(row["next_run_at"]) != expected
                      else "not_due" if expected > moment else None)
            if reason is not None:
                result = {"fired": False, "reason": reason, "schedule": _schedule(row) if row is not None else None}
                return self._record(conn, "fire_steward_schedule", request_key, request_hash, result) \
                    if request_key is not None else result
            missed, next_run = 0, None
            if row["kind"] == "cron":
                trigger, zone, cursor = _cron(row["expr"]), _zone(row["tz"]), expected
                while missed < MISSED_CAP:
                    cursor = _cron_after(trigger, zone, cursor)
                    if cursor is None or cursor > moment:
                        break
                    missed += 1
                upcoming = _cron_after(trigger, zone, moment)
                next_run = _iso(upcoming) if upcoming is not None else None
            # last_due_at: the run this fire was for, the one post_group_notice checks (7.9.4).
            conn.execute("UPDATE state_steward_schedules SET version=version+1,last_run_at=?,last_missed=?,"
                         "next_run_at=?,enabled=?,last_fired_by=?,last_due_at=? WHERE schedule_id=?",
                         (_iso(moment), missed, next_run, int(next_run is not None), self.driver_id,
                          row["next_run_at"], schedule_id))
            out = self._done(conn, "fire_steward_schedule", request_key, request_hash, schedule_id, {
                "fired": True, "schedule": _schedule(self._schedule_row(conn, schedule_id)),
                "due_at": row["next_run_at"], "late_seconds": int((moment - expected).total_seconds()),
                "missed": missed})
        self._notify()
        return out
