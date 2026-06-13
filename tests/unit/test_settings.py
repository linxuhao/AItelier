# tests/unit/test_settings.py
# Tests for the scheduler settings feature: DB layer, API endpoints, CLI parsing.

import asyncio
import pytest
from fastapi.testclient import TestClient


# ── DBManager settings tests ──────────────────────────────────


def test_settings_table_created(db_manager):
    """Settings table should exist after DB init."""
    with db_manager.get_connection() as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
    assert "settings" in tables


def test_get_scheduler_settings_empty(db_manager):
    """Should return empty dict when no settings are stored."""
    settings = db_manager.get_scheduler_settings()
    assert settings == {}


def test_set_and_get_setting(db_manager):
    """Round-trip: write a setting and read it back."""
    db_manager.set_scheduler_setting("scheduler_type", "interval")
    db_manager.set_scheduler_setting("scheduler_interval", "30")

    settings = db_manager.get_scheduler_settings()
    assert settings["scheduler_type"] == "interval"
    assert settings["scheduler_interval"] == "30"


def test_set_setting_upsert(db_manager):
    """Writing the same key again should update, not duplicate."""
    db_manager.set_scheduler_setting("scheduler_type", "interval")
    db_manager.set_scheduler_setting("scheduler_type", "cron")

    settings = db_manager.get_scheduler_settings()
    assert settings["scheduler_type"] == "cron"
    # Only one entry for this key
    assert list(settings.keys()).count("scheduler_type") == 1


def test_multiple_settings(db_manager):
    """Multiple distinct keys should coexist."""
    db_manager.set_scheduler_setting("scheduler_type", "cron")
    db_manager.set_scheduler_setting("scheduler_cron", "*/5 * * * *")
    db_manager.set_scheduler_setting("scheduler_interval", "0")

    settings = db_manager.get_scheduler_settings()
    assert len(settings) == 3
    assert settings["scheduler_cron"] == "*/5 * * * *"


# ── API endpoint tests ────────────────────────────────────────


def test_get_scheduler_settings_defaults(client: TestClient):
    """GET should return interval/60 when no settings are stored."""
    resp = client.get("/api/settings/scheduler")
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "interval"
    assert data["scheduler_interval"] == 60
    assert data["scheduler_cron"] is None


def test_update_scheduler_interval(client: TestClient):
    """POST should update interval and persist."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "interval",
        "scheduler_interval": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "interval"
    assert data["scheduler_interval"] == 30

    # Verify persistence via GET
    get_resp = client.get("/api/settings/scheduler")
    assert get_resp.json()["scheduler_interval"] == 30


def test_update_scheduler_cron(client: TestClient):
    """POST should accept cron expression."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "cron",
        "scheduler_cron": "*/5 * * * *",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scheduler_type"] == "cron"
    assert data["scheduler_cron"] == "*/5 * * * *"


def test_update_interval_too_small(client: TestClient):
    """Interval < 5 seconds should be rejected."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "interval",
        "scheduler_interval": 2,
    })
    assert resp.status_code == 400


def test_update_interval_missing(client: TestClient):
    """Interval type without scheduler_interval should be rejected."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "interval",
    })
    assert resp.status_code == 400


def test_update_cron_missing(client: TestClient):
    """Cron type without scheduler_cron should be rejected."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "cron",
    })
    assert resp.status_code == 400


def test_update_cron_wrong_fields(client: TestClient):
    """Cron with != 5 fields should be rejected."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "cron",
        "scheduler_cron": "*/5 * *",
    })
    assert resp.status_code == 400


def test_update_invalid_type(client: TestClient):
    """Invalid scheduler_type should be rejected."""
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "hourly",
        "scheduler_interval": 60,
    })
    assert resp.status_code == 400


def test_switch_interval_to_cron(client: TestClient):
    """Should be able to switch from interval to cron and back."""
    # Set interval
    client.post("/api/settings/scheduler", json={
        "scheduler_type": "interval",
        "scheduler_interval": 30,
    })

    # Switch to cron
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "cron",
        "scheduler_cron": "0 */2 * * *",
    })
    assert resp.status_code == 200
    assert resp.json()["scheduler_type"] == "cron"

    # Switch back to interval
    resp = client.post("/api/settings/scheduler", json={
        "scheduler_type": "interval",
        "scheduler_interval": 120,
    })
    assert resp.status_code == 200
    assert resp.json()["scheduler_type"] == "interval"
    assert resp.json()["scheduler_interval"] == 120


# ── CLI frequency parsing tests ───────────────────────────────


def test_parse_frequency_presets():
    from cli.app import _parse_frequency
    assert _parse_frequency("slow") == 300
    assert _parse_frequency("medium") == 60
    assert _parse_frequency("high") == 15


def test_parse_frequency_presets_case_insensitive():
    from cli.app import _parse_frequency
    assert _parse_frequency("SLOW") == 300
    assert _parse_frequency("Medium") == 60
    assert _parse_frequency("HIGH") == 15


def test_parse_frequency_seconds():
    from cli.app import _parse_frequency
    assert _parse_frequency("30s") == 30
    assert _parse_frequency("10s") == 10


def test_parse_frequency_minutes():
    from cli.app import _parse_frequency
    assert _parse_frequency("2m") == 120
    assert _parse_frequency("1m") == 60


def test_parse_frequency_mixed():
    from cli.app import _parse_frequency
    assert _parse_frequency("1m30s") == 90
    assert _parse_frequency("2m15s") == 135


def test_parse_frequency_bare_number():
    from cli.app import _parse_frequency
    assert _parse_frequency("45") == 45


def test_parse_frequency_too_small():
    from cli.app import _parse_frequency
    with pytest.raises(ValueError):
        _parse_frequency("3s")
    with pytest.raises(ValueError):
        _parse_frequency("2")


# ── CLI formatting tests ──────────────────────────────────────


def test_fmt_seconds():
    from cli.app import _fmt_seconds
    assert _fmt_seconds(15) == "15s"
    assert _fmt_seconds(60) == "1m"
    assert _fmt_seconds(120) == "2m"
    assert _fmt_seconds(90) == "1m 30s"
    assert _fmt_seconds(300) == "5m"


# ── Scheduler reschedule tests ────────────────────────────────


def test_start_scheduler_reads_interval_from_db(tmp_path, monkeypatch):
    """start_scheduler should read interval from DB settings."""
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "sched.db"))
    db.set_scheduler_setting("scheduler_type", "interval")
    db.set_scheduler_setting("scheduler_interval", "30")

    import core.scheduler
    monkeypatch.setattr(core.scheduler, "db", db)
    monkeypatch.setattr(core.scheduler, "poll_and_execute", lambda: None)

    async def _test():
        scheduler = core.scheduler.start_scheduler()
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].trigger.interval.seconds == 30
        scheduler.shutdown(wait=False)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_test())
    finally:
        loop.close()


def test_start_scheduler_reads_cron_from_db(tmp_path, monkeypatch):
    """start_scheduler should read cron expression from DB settings."""
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "cron.db"))
    db.set_scheduler_setting("scheduler_type", "cron")
    db.set_scheduler_setting("scheduler_cron", "*/5 * * * *")

    import core.scheduler
    monkeypatch.setattr(core.scheduler, "db", db)
    monkeypatch.setattr(core.scheduler, "poll_and_execute", lambda: None)

    async def _test():
        scheduler = core.scheduler.start_scheduler()
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        scheduler.shutdown(wait=False)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_test())
    finally:
        loop.close()


def test_start_scheduler_defaults_to_5s(tmp_path, monkeypatch):
    """start_scheduler should default to 5s interval when no settings."""
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "default.db"))

    import core.scheduler
    monkeypatch.setattr(core.scheduler, "db", db)
    monkeypatch.setattr(core.scheduler, "poll_and_execute", lambda: None)

    async def _test():
        scheduler = core.scheduler.start_scheduler()
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].trigger.interval.seconds == 5
        # T8: misfire_grace_time prevents "missed" warnings when a tick
        # takes longer than the interval (first LLM call can take ~30s).
        assert jobs[0].misfire_grace_time == 60
        scheduler.shutdown(wait=False)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_test())
    finally:
        loop.close()


def test_reschedule_swaps_job(tmp_path, monkeypatch):
    """reschedule_scheduler should replace the existing job."""
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "resched.db"))
    db.set_scheduler_setting("scheduler_type", "interval")
    db.set_scheduler_setting("scheduler_interval", "60")

    import core.scheduler
    monkeypatch.setattr(core.scheduler, "db", db)
    monkeypatch.setattr(core.scheduler, "poll_and_execute", lambda: None)

    async def _test():
        scheduler = core.scheduler.start_scheduler()
        assert len(scheduler.get_jobs()) == 1
        assert scheduler.get_jobs()[0].trigger.interval.seconds == 60

        db.set_scheduler_setting("scheduler_interval", "15")
        core.scheduler.reschedule_scheduler(scheduler)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].trigger.interval.seconds == 15
        scheduler.shutdown(wait=False)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_test())
    finally:
        loop.close()


def test_reschedule_from_interval_to_cron(tmp_path, monkeypatch):
    """reschedule_scheduler should switch from interval to cron trigger."""
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "swap.db"))
    db.set_scheduler_setting("scheduler_type", "interval")
    db.set_scheduler_setting("scheduler_interval", "60")

    import core.scheduler
    monkeypatch.setattr(core.scheduler, "db", db)
    monkeypatch.setattr(core.scheduler, "poll_and_execute", lambda: None)

    async def _test():
        scheduler = core.scheduler.start_scheduler()
        assert len(scheduler.get_jobs()) == 1

        db.set_scheduler_setting("scheduler_type", "cron")
        db.set_scheduler_setting("scheduler_cron", "0 */2 * * *")
        core.scheduler.reschedule_scheduler(scheduler)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        from apscheduler.triggers.cron import CronTrigger
        assert isinstance(jobs[0].trigger, CronTrigger)
        scheduler.shutdown(wait=False)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_test())
    finally:
        loop.close()
