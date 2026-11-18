"""Step 16, backend: the steward's scheduled reminders (design-steward 7.4).

The schedules' clock is `core.state_steward._now`, fixed here per test. HTTP runs
the real app with the write gate ON and test mode OFF: the owner is a Cloudflare
Access writer email, drivers authenticate with their real tokens."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, state_steward
from core.drivers import DriverRegistry
from core.dsh_driver import DshDrivers
from core.state_commands import STEWARD_WRITE_ACTIONS, ProjectPrivate, execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from core.state_steward import ScheduleConflict, ScheduleInvalid, preview_schedule

HOME = "aitelier-home"
OWNER = "owner@example.com"
OWNER_HEADERS = {"X-Test-Email": OWNER}
PARIS = ZoneInfo("Europe/Paris")
DAILY_9 = {"kind": "cron", "expr": "0 9 * * *"}
SCHEDULE_ACTIONS = ("create_steward_schedule", "update_steward_schedule", "delete_steward_schedule",
                    "fire_steward_schedule")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


@pytest.fixture
def clock(monkeypatch):
    fixed = Clock(utc(2027, 1, 1, 0, 0))
    monkeypatch.setattr(state_steward, "_now", fixed)
    return fixed


@pytest.fixture
def world(tmp_path, clock):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    registry = DriverRegistry(db, "steward-schedule-pepper-" * 3)
    registry.seed("steward-schedule-owner-cli-token")
    seeder = StateService(db, actor="fixture", project_read_trusted=True)
    for project in ("alpha", HOME):
        seeder.store.create_project(project, project.title())
    runtime = DshDrivers(db, registry, tmp_path / "drivers",
                         command=[sys.executable, "-c", "import time; time.sleep(90)"])
    steward = runtime._provision(HOME, "aitelier", "driver", "fixture")
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    tokens = {name: (runtime.root / row["driver_id"] / "token").read_text().strip()
              for name, row in (("steward", steward), ("director", director))}

    def as_driver(row):
        return StateService(db, actor="driver:" + row["driver_id"], driver_id=row["driver_id"],
                            project_read_trusted=True)

    return type("World", (), dict(
        db=db, registry=registry, runtime=runtime, tokens=tokens, steward_row=steward, as_driver=as_driver,
        owner=StateService(db, actor="owner:" + OWNER, project_read_trusted=True),
        anonymous=StateService(db, actor="anonymous", project_read_trusted=False),
        steward=as_driver(steward), director=as_driver(director)))


@pytest.fixture
def client(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as c:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield c
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def token(world, who):
    return {"X-AItelier-Driver-Token": world.tokens[who]}


def body(response):
    data = response.json()
    return data.get("result", data) if isinstance(data, dict) else data


def create(world, schedule=None, key="c1", **extra):
    return execute(world.steward, "create_steward_schedule",
                   {"request_key": key, "title": "Morning", "instruction": "look at the inbox",
                    "schedule": schedule or DAILY_9, **extra}, allow_write=True)


def fire(service, schedule_id, expected, key="f1"):
    return execute(service, "fire_steward_schedule",
                   {"request_key": key, "schedule_id": schedule_id, "expected_next_run_at": expected},
                   allow_write=True)


def events(world):
    with world.db.get_connection() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM state_events WHERE project_id=? AND event_type='steward_schedule_changed' "
            "ORDER BY rowid", (HOME,))]


def runs(schedule, tz="Europe/Paris"):
    return preview_schedule(schedule, tz)["next_runs"]


# --- next_run: once and cron --------------------------------------------------------------

def test_a_once_schedule_runs_at_its_time(world, clock):
    row = create(world, {"kind": "once", "at": "2027-01-02T09:00:00+01:00"})
    assert row["next_run_at"] == "2027-01-02T08:00:00+00:00" and row["enabled"] is True
    assert row["schedule"] == {"kind": "once", "at": "2027-01-02T08:00:00+00:00"}
    # A naive time is a wall time in the row's zone.
    naive = create(world, {"kind": "once", "at": "2027-07-02T09:00:00"}, key="c2")
    assert naive["next_run_at"] == "2027-07-02T07:00:00+00:00"
    assert runs({"kind": "once", "at": "2027-01-02T08:00:00Z"}) == ["2027-01-02T09:00:00+01:00"]


def test_cron_month_end_and_names(clock):
    assert runs({"kind": "cron", "expr": "0 9 31 * *"}) == [
        "2027-01-31T09:00:00+01:00", "2027-03-31T09:00:00+02:00", "2027-05-31T09:00:00+02:00",
        "2027-07-31T09:00:00+02:00", "2027-08-31T09:00:00+02:00"]
    # 2027-01-01 is a Friday.
    assert runs({"kind": "cron", "expr": "0 9 * * mon-fri"}) == [
        "2027-01-01T09:00:00+01:00", "2027-01-04T09:00:00+01:00", "2027-01-05T09:00:00+01:00",
        "2027-01-06T09:00:00+01:00", "2027-01-07T09:00:00+01:00"]
    assert runs({"kind": "cron", "expr": "30 8 1 jan,jul *"}) == [
        "2027-01-01T08:30:00+01:00", "2027-07-01T08:30:00+02:00", "2028-01-01T08:30:00+01:00",
        "2028-07-01T08:30:00+02:00", "2029-01-01T08:30:00+01:00"]
    assert runs({"kind": "cron", "expr": "0 9 * * sun"})[0] == "2027-01-03T09:00:00+01:00"


def test_weekday_numbers_are_standard_cron(clock):
    """0 and 7 are Sunday, 1-5 Monday to Friday (APScheduler's from_crontab alone
    would read 1-5 as Tuesday to Saturday)."""
    weekdays = runs({"kind": "cron", "expr": "0 9 * * 1-5"})
    assert [datetime.fromisoformat(t).strftime("%a") for t in weekdays] == ["Fri", "Mon", "Tue", "Wed", "Thu"]
    for expr in ("0 9 * * 0", "0 9 * * 7", "0 9 * * SUN"):
        assert runs({"kind": "cron", "expr": expr})[0] == "2027-01-03T09:00:00+01:00", expr
    every_other = runs({"kind": "cron", "expr": "0 9 * * */2"})
    assert [datetime.fromisoformat(t).strftime("%a") for t in every_other] == ["Sat", "Sun", "Tue", "Thu", "Sat"]
    assert runs({"kind": "cron", "expr": "0 9 * * 5-7"})[:3] == [
        "2027-01-01T09:00:00+01:00", "2027-01-02T09:00:00+01:00", "2027-01-03T09:00:00+01:00"]


def test_dst_spring_forward_takes_apschedulers_answer(clock):
    """2027-03-28 02:30 does not exist in Europe/Paris: the run is 01:30Z (03:30
    summer time), the instant APScheduler 3.11.3's own zone-aware trigger gives."""
    from apscheduler.triggers.cron import CronTrigger
    clock.moment = utc(2027, 3, 27, 12, 0)
    assert runs({"kind": "cron", "expr": "30 2 * * *"})[:3] == [
        "2027-03-28T03:30:00+02:00", "2027-03-29T02:30:00+02:00", "2027-03-30T02:30:00+02:00"]
    theirs = CronTrigger.from_crontab("30 2 * * *", timezone=PARIS).get_next_fire_time(None, clock.moment)
    assert theirs.astimezone(timezone.utc) == utc(2027, 3, 28, 1, 30)


def test_dst_fall_back_runs_the_repeated_time_once(world, clock):
    """2027-10-31 02:30 occurs twice in Europe/Paris: the expression fires once that day."""
    clock.moment = utc(2027, 10, 30, 12, 0)
    row = create(world, {"kind": "cron", "expr": "30 2 * * *"})
    assert row["next_run_at"] == "2027-10-31T00:30:00+00:00"   # 02:30 summer time, the first one
    clock.moment = utc(2027, 10, 31, 0, 30, 5)
    out = fire(world.steward, row["schedule_id"], row["next_run_at"])
    assert out["fired"] is True and out["missed"] == 0
    assert out["schedule"]["next_run_at"] == "2027-11-01T01:30:00+00:00", "not 2027-10-31T01:30Z again"
    # Even fired late, inside the repeated hour, the second 02:30 is not a new run.
    clock.moment = utc(2027, 10, 30, 12, 0)
    late = create(world, {"kind": "cron", "expr": "30 2 * * *"}, key="c2")
    clock.moment = utc(2027, 10, 31, 1, 10)   # 02:10 winter time
    out = fire(world.steward, late["schedule_id"], late["next_run_at"], key="f2")
    assert out["fired"] is True and out["missed"] == 0
    assert out["schedule"]["next_run_at"] == "2027-11-01T01:30:00+00:00"


# --- refusals at save -----------------------------------------------------------------------

@pytest.mark.parametrize("schedule, tz, code, reason", [
    ({"kind": "cron", "expr": "61 * * * *"}, "Europe/Paris", "invalid_cron", "higher than the maximum value (59)"),
    ({"kind": "cron", "expr": "every morning"}, "Europe/Paris", "invalid_cron", "Wrong number of fields"),
    ({"kind": "cron", "expr": "*/5 * * * *"}, "Europe/Paris", "too_frequent", "less than 15 minutes apart"),
    ({"kind": "cron", "expr": "0,10 9 * * *"}, "Europe/Paris", "too_frequent", "less than 15 minutes apart"),
    ({"kind": "once", "at": "2026-12-31T23:59:00Z"}, "Europe/Paris", "at_in_past", "is not in the future"),
    ({"kind": "cron", "expr": "0 9 * * *"}, "Mars/Olympus", "invalid_timezone", "not an IANA time zone"),
    ({"kind": "weekly"}, "Europe/Paris", "invalid_schedule", "schedule must be"),
    ({"kind": "cron", "expr": "0 9 * * 8"}, "Europe/Paris", "invalid_cron", "is not 0-7 or sun-sat"),
    ({"kind": "cron", "expr": "0 9 1 * mon"}, "Europe/Paris", "invalid_cron", "both restricted"),
    ({"kind": "cron", "expr": "0 0 30 2 *"}, "Europe/Paris", "invalid_cron", "never runs"),
])
def test_invalid_schedules_are_refused_with_the_reason(world, client, schedule, tz, code, reason):
    with pytest.raises(ScheduleInvalid) as refused:
        create(world, schedule, tz=tz)
    assert refused.value.code == code and reason in refused.value.reason, refused.value
    r = client.post("/api/steward/schedules", headers=OWNER_HEADERS,
                    json={"title": "t", "instruction": "i", "schedule": schedule, "tz": tz})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code, r.text
    assert reason in r.json()["detail"]["reason"]
    r = client.post("/api/steward/schedules/preview", headers=OWNER_HEADERS, json={"schedule": schedule, "tz": tz})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code, r.text
    r = client.post("/api/state/commands/create_steward_schedule", headers=token(world, "steward"), json={
        "request_key": "http", "title": "t", "instruction": "i", "schedule": schedule, "tz": tz})
    assert r.status_code == 422 and r.json()["detail"].startswith(code + ": "), r.text
    assert execute(world.owner, "list_steward_schedules", {})["schedules"] == []


def test_exactly_fifteen_minutes_is_allowed(clock):
    assert len(runs({"kind": "cron", "expr": "*/15 * * * *"})) == 5


def test_the_preview_endpoint(world, client):
    r = client.post("/api/steward/schedules/preview", headers=OWNER_HEADERS,
                    json={"schedule": {"kind": "cron", "expr": "0 9 * * 1-5"}, "tz": "Europe/Paris"})
    assert r.status_code == 200, r.text
    assert len(r.json()["next_runs"]) == 5 and all(t.endswith("T09:00:00+01:00") for t in r.json()["next_runs"])


# --- disabled ----------------------------------------------------------------------------------

def test_disabled_has_no_next_run_and_reenabling_counts_from_now(world, clock):
    row = create(world, enabled=False)
    assert row["enabled"] is False and row["next_run_at"] is None
    clock.moment = utc(2027, 2, 10, 12, 0)
    row = execute(world.steward, "update_steward_schedule", {"request_key": "u1", "schedule_id": row["schedule_id"],
                  "expected_version": 1, "enabled": True}, allow_write=True)
    assert row["next_run_at"] == "2027-02-11T08:00:00+00:00" and row["version"] == 2
    row = execute(world.steward, "update_steward_schedule", {"request_key": "u2", "schedule_id": row["schedule_id"],
                  "expected_version": 2, "enabled": False}, allow_write=True)
    assert row["next_run_at"] is None
    # Disabled across several runs: none is caught up on re-enabling.
    clock.moment = utc(2027, 3, 1, 12, 0)
    row = execute(world.steward, "update_steward_schedule", {"request_key": "u3", "schedule_id": row["schedule_id"],
                  "expected_version": 3, "enabled": True}, allow_write=True)
    assert row["next_run_at"] == "2027-03-02T08:00:00+00:00" and row["last_missed"] == 0
    # A title change keeps the next run.
    row = execute(world.steward, "update_steward_schedule", {"request_key": "u4", "schedule_id": row["schedule_id"],
                  "expected_version": 4, "title": "Renamed"}, allow_write=True)
    assert row["next_run_at"] == "2027-03-02T08:00:00+00:00" and row["title"] == "Renamed"


def test_a_disabled_row_does_not_fire(world, clock):
    row = create(world)
    execute(world.steward, "update_steward_schedule", {"request_key": "u", "schedule_id": row["schedule_id"],
            "expected_version": 1, "enabled": False}, allow_write=True)
    clock.moment = utc(2027, 1, 2)
    out = fire(world.steward, row["schedule_id"], row["next_run_at"])
    assert out["fired"] is False and out["schedule"]["next_run_at"] is None


# --- fire ------------------------------------------------------------------------------------

def test_concurrent_fires_of_one_run_fire_once(world, clock):
    row = create(world)
    clock.moment = utc(2027, 1, 1, 8, 0, 3)
    barrier, outcomes = threading.Barrier(4), []

    def one(i):
        steward = world.as_driver(world.steward_row)
        barrier.wait()
        outcomes.append(steward.steward.fire_schedule(row["schedule_id"], row["next_run_at"], project_id=HOME,
                                                      request_key=f"fire-{i}"))
    threads = [threading.Thread(target=one, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [o["fired"] for o in outcomes].count(True) == 1, outcomes
    fired = next(o for o in outcomes if o["fired"])
    assert fired["due_at"] == row["next_run_at"] and fired["late_seconds"] == 3 and fired["missed"] == 0
    assert fired["schedule"]["next_run_at"] == "2027-01-02T08:00:00+00:00"
    assert fired["schedule"]["last_fired_by"] == world.steward_row["driver_id"]
    assert fired["schedule"]["version"] == 2


def test_fire_over_http_once(world, client, clock):
    row = create(world)
    clock.moment = utc(2027, 1, 1, 8, 1)
    payload = {"schedule_id": row["schedule_id"], "expected_next_run_at": row["next_run_at"]}
    first = client.post("/api/state/commands/fire_steward_schedule", headers=token(world, "steward"),
                        json={**payload, "request_key": "fire-a"})
    second = client.post("/api/state/commands/fire_steward_schedule", headers=token(world, "steward"),
                         json={**payload, "request_key": "fire-b"})
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    assert body(first)["fired"] is True and body(first)["late_seconds"] == 60
    assert body(second)["fired"] is False and body(second)["schedule"]["version"] == 2
    replay = client.post("/api/state/commands/fire_steward_schedule", headers=token(world, "steward"),
                         json={**payload, "request_key": "fire-a"})
    assert body(replay) == body(first), "the same request_key is a replay"


def test_a_run_not_yet_due_does_not_fire(world, clock):
    row = create(world)
    clock.moment = utc(2027, 1, 1, 7, 59, 59)
    assert fire(world.steward, row["schedule_id"], row["next_run_at"])["fired"] is False
    assert fire(world.steward, row["schedule_id"], "2027-01-01T07:00:00+00:00", key="f2")["fired"] is False


def test_runs_crossed_while_down_fire_once_and_count_from_now(world, clock):
    row = create(world)                                   # next 2027-01-01 09:00 Paris
    clock.moment = utc(2027, 1, 4, 10, 0)                 # down until 01-04 11:00 Paris
    out = fire(world.steward, row["schedule_id"], row["next_run_at"])
    assert out["fired"] is True and out["missed"] == 3, out    # 01-02, 01-03, 01-04
    assert out["due_at"] == "2027-01-01T08:00:00+00:00"
    assert out["late_seconds"] == 3 * 86400 + 2 * 3600
    assert out["schedule"]["next_run_at"] == "2027-01-05T08:00:00+00:00"
    assert out["schedule"]["last_run_at"] == "2027-01-04T10:00:00+00:00" and out["schedule"]["last_missed"] == 3
    # The restarted host fires the same stale run again: nothing.
    again = fire(world.steward, row["schedule_id"], row["next_run_at"], key="f2")
    assert again["fired"] is False and again["schedule"]["version"] == out["schedule"]["version"]


def test_a_once_schedule_fires_once_then_disables(world, clock):
    row = create(world, {"kind": "once", "at": "2027-01-01T12:00:00Z"})
    clock.moment = utc(2027, 1, 1, 13, 0)
    out = fire(world.steward, row["schedule_id"], row["next_run_at"])
    assert out["fired"] is True and out["missed"] == 0 and out["late_seconds"] == 3600
    assert out["schedule"]["enabled"] is False and out["schedule"]["next_run_at"] is None
    assert fire(world.steward, row["schedule_id"], row["next_run_at"], key="f2")["fired"] is False
    # Re-enabling a once whose time has passed is refused.
    with pytest.raises(ScheduleInvalid, match="at_in_past"):
        execute(world.steward, "update_steward_schedule", {"request_key": "u", "schedule_id": row["schedule_id"],
                "expected_version": 2, "enabled": True}, allow_write=True)


def test_a_deleted_row_is_never_listed_and_never_fires(world, clock):
    row = create(world)
    out = execute(world.steward, "delete_steward_schedule", {"request_key": "d", "schedule_id": row["schedule_id"],
                  "expected_version": 1}, allow_write=True)
    assert out == {"deleted": True, "schedule_id": row["schedule_id"]}
    assert execute(world.steward, "list_steward_schedules", {})["schedules"] == []
    clock.moment = utc(2027, 1, 2)
    assert fire(world.steward, row["schedule_id"], row["next_run_at"]) == {"fired": False, "schedule": None}


# --- versions ----------------------------------------------------------------------------------

def test_a_stale_version_is_refused_with_the_current_row(world, client):
    row = create(world)
    r = client.put(f"/api/steward/schedules/{row['schedule_id']}", headers=OWNER_HEADERS,
                   json={"expected_version": 1, "title": "Owner's title"})
    assert r.status_code == 200 and r.json()["updated_by"] == "owner" and r.json()["version"] == 2, r.text
    # The steward edits the version it saw: refused, with the owner's row.
    with pytest.raises(ScheduleConflict) as refused:
        execute(world.steward, "update_steward_schedule", {"request_key": "u", "schedule_id": row["schedule_id"],
                "expected_version": 1, "title": "Steward's title"}, allow_write=True)
    assert isinstance(refused.value, StateConflict) and refused.value.row["title"] == "Owner's title"
    assert str(refused.value).startswith("version_conflict: ")
    assert json.loads(str(refused.value)[len("version_conflict: "):])["version"] == 2
    r = client.post("/api/state/commands/update_steward_schedule", headers=token(world, "steward"), json={
        "request_key": "u-http", "schedule_id": row["schedule_id"], "expected_version": 1, "title": "x"})
    assert r.status_code == 409 and r.json()["detail"].startswith("version_conflict: "), r.text
    assert json.loads(r.json()["detail"][len("version_conflict: "):])["title"] == "Owner's title"
    # And the owner, behind a steward edit.
    execute(world.steward, "update_steward_schedule", {"request_key": "u2", "schedule_id": row["schedule_id"],
            "expected_version": 2, "title": "Steward's title"}, allow_write=True)
    r = client.put(f"/api/steward/schedules/{row['schedule_id']}", headers=OWNER_HEADERS,
                   json={"expected_version": 2, "title": "late"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "version_conflict", r.text
    assert r.json()["detail"]["schedule"]["title"] == "Steward's title"
    assert r.json()["detail"]["schedule"]["updated_by"] == "steward"
    r = client.delete(f"/api/steward/schedules/{row['schedule_id']}?expected_version=2", headers=OWNER_HEADERS)
    assert r.status_code == 409 and r.json()["detail"]["schedule"]["version"] == 3, r.text
    r = client.delete(f"/api/steward/schedules/{row['schedule_id']}?expected_version=3", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json() == {"deleted": True}, r.text
    with pytest.raises(StateGraphError, match="unknown_schedule"):
        execute(world.steward, "delete_steward_schedule", {"request_key": "d", "schedule_id": row["schedule_id"],
                "expected_version": 4}, allow_write=True)


# --- who -----------------------------------------------------------------------------------------

def test_the_owner_round_trip_over_http(world, client):
    r = client.post("/api/steward/schedules", headers=OWNER_HEADERS, json={
        "title": "Weekly", "instruction": "summarise the week", "tz": "America/New_York",
        "schedule": {"kind": "cron", "expr": "0 9 * * mon"}})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["created_by"] == row["updated_by"] == "owner" and row["tz"] == "America/New_York"
    assert row["schedule_id"].startswith("sch-") and len(row["schedule_id"]) == 16
    assert row["next_run_at"] == "2027-01-04T14:00:00+00:00"
    assert set(row) == {"schedule_id", "version", "title", "instruction", "schedule", "tz", "enabled", "next_run_at",
                        "last_run_at", "last_missed", "created_by", "updated_by", "last_fired_by", "created_at",
                        "updated_at"}
    listed = client.get("/api/steward/schedules", headers=OWNER_HEADERS)
    assert listed.status_code == 200 and listed.json()["schedules"] == [row], listed.text
    # The steward reads it too, and may change the owner's row: updated_by says so.
    listed = client.get("/api/steward/schedules", headers=token(world, "steward"))
    assert listed.status_code == 200 and listed.json()["schedules"] == [row], listed.text
    changed = execute(world.steward, "update_steward_schedule", {"request_key": "u", "schedule_id": row["schedule_id"],
                      "expected_version": 1, "instruction": "summarise the week briefly"}, allow_write=True)
    assert changed["created_by"] == "owner" and changed["updated_by"] == "steward"


def test_the_owner_routes_refuse_every_other_caller(world, client):
    row = create(world)
    sid = row["schedule_id"]
    writes = [("POST", "/api/steward/schedules", {"title": "t", "instruction": "i", "tz": "UTC", "schedule": DAILY_9}),
              ("POST", "/api/steward/schedules/preview", {"schedule": DAILY_9, "tz": "UTC"}),
              ("PUT", f"/api/steward/schedules/{sid}", {"expected_version": 1, "title": "t"}),
              ("DELETE", f"/api/steward/schedules/{sid}?expected_version=1", None)]
    for method, path, payload in writes:
        for who, code in (("director", "owner_session_required"), ("steward", "steward_read_only")):
            r = client.request(method, path, json=payload, headers=token(world, who))
            assert r.status_code == 403 and code in r.text, (method, path, who, r.text)
        assert client.request(method, path, json=payload).status_code == 403, (method, path)
        r = client.request(method, path, json=payload, headers={"X-Test-Email": "stranger@example.com"})
        assert r.status_code == 403, (method, path, r.text)
    for headers in ({}, token(world, "director"), {"X-Test-Email": "stranger@example.com"}):
        assert client.get("/api/steward/schedules", headers=headers).status_code == 403
    current = execute(world.steward, "list_steward_schedules", {})["schedules"]
    assert [(s["schedule_id"], s["version"], s["title"]) for s in current] == [(sid, 1, "Morning")]


def test_the_steward_commands_are_the_stewards_own(world):
    assert set(SCHEDULE_ACTIONS) <= STEWARD_WRITE_ACTIONS
    row = create(world)
    assert row["created_by"] == row["updated_by"] == "steward" and row["tz"] == "Europe/Paris"
    arguments = {
        "create_steward_schedule": {"request_key": "k", "title": "t", "instruction": "i", "schedule": DAILY_9},
        "update_steward_schedule": {"request_key": "k", "schedule_id": row["schedule_id"], "expected_version": 1,
                                    "title": "t"},
        "delete_steward_schedule": {"request_key": "k", "schedule_id": row["schedule_id"], "expected_version": 1},
        "fire_steward_schedule": {"request_key": "k", "schedule_id": row["schedule_id"],
                                  "expected_next_run_at": row["next_run_at"]}}
    for action in SCHEDULE_ACTIONS:
        for service in (world.director, world.owner):
            with pytest.raises(StateGraphError, match="steward_role_required"):
                execute(service, action, {"project_id": HOME, **arguments[action]}, allow_write=True)
        # Never another project's, even named.
        with pytest.raises(StateGraphError, match="steward_project_required"):
            execute(world.steward, action, {"project_id": "alpha", **arguments[action]}, allow_write=True)
    with pytest.raises(StateGraphError, match="steward_role_required"):
        execute(world.director, "list_steward_schedules", {})
    with pytest.raises(ProjectPrivate):
        execute(world.anonymous, "list_steward_schedules", {})
    # The owner's session reads the list on the State surface too.
    assert [s["schedule_id"] for s in execute(world.owner, "list_steward_schedules", {})["schedules"]] \
        == [row["schedule_id"]]
    assert execute(world.steward, "list_steward_schedules", {})["schedules"][0]["version"] == 1, "nothing changed"


def test_the_stewards_default_zone_can_be_overridden(world, monkeypatch):
    monkeypatch.setenv("AITELIER_OWNER_TZ", "Asia/Shanghai")
    row = create(world)
    assert row["tz"] == "Asia/Shanghai" and row["next_run_at"] == "2027-01-01T01:00:00+00:00"


def test_the_list_is_soonest_first_and_can_hide_disabled(world, clock):
    later = create(world, {"kind": "cron", "expr": "0 18 * * *"}, key="a")
    off = create(world, key="b", enabled=False)
    sooner = create(world, key="c")
    listed = execute(world.steward, "list_steward_schedules", {})["schedules"]
    assert [s["schedule_id"] for s in listed] == [sooner["schedule_id"], later["schedule_id"], off["schedule_id"]]
    listed = execute(world.steward, "list_steward_schedules", {"include_disabled": False})["schedules"]
    assert [s["schedule_id"] for s in listed] == [sooner["schedule_id"], later["schedule_id"]]


# --- the change event ----------------------------------------------------------------------------

def test_every_change_emits_the_event_and_nothing_else_does(world, client, clock):
    row = create(world)
    sid = row["schedule_id"]
    assert events(world) == [{"schedule_id": sid}]
    assert create(world) == row and len(events(world)) == 1, "a replay changes nothing"
    execute(world.steward, "update_steward_schedule", {"request_key": "u", "schedule_id": sid,
            "expected_version": 1, "title": "t2"}, allow_write=True)
    r = client.put(f"/api/steward/schedules/{sid}", headers=OWNER_HEADERS, json={"expected_version": 2, "title": "t3"})
    assert r.status_code == 200, r.text
    assert len(events(world)) == 3
    clock.moment = utc(2027, 1, 1, 8, 0)
    assert fire(world.steward, sid, row["next_run_at"])["fired"] is True
    assert len(events(world)) == 4
    assert fire(world.steward, sid, row["next_run_at"], key="f2")["fired"] is False
    assert len(events(world)) == 4, "a fire that fired nothing changes nothing"
    owner_row = client.post("/api/steward/schedules", headers=OWNER_HEADERS, json={
        "title": "o", "instruction": "i", "tz": "UTC", "schedule": DAILY_9}).json()
    r = client.delete(f"/api/steward/schedules/{sid}?expected_version=4", headers=OWNER_HEADERS)
    assert r.status_code == 200, r.text
    assert events(world)[4:] == [{"schedule_id": owner_row["schedule_id"]}, {"schedule_id": sid}]
