# tests/unit/test_scheduler_manager.py
# Unit tests for web_api/scheduler_manager.py (UserSchedulerManager).

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
from web_api.scheduler_manager import UserSchedulerManager


@pytest.fixture
def manager():
    return UserSchedulerManager(idle_timeout=60)


def test_touch_updates_last_seen(manager):
    """touch() should record current timestamp for user."""
    before = datetime.now(timezone.utc)
    manager.touch("alice@test.com")
    after = datetime.now(timezone.utc)

    assert "alice@test.com" in manager._last_seen
    assert before <= manager._last_seen["alice@test.com"] <= after


def test_touch_updates_timestamp(manager):
    """Repeated touch() should update the timestamp."""
    manager.touch("alice@test.com")
    first = manager._last_seen["alice@test.com"]

    import time
    time.sleep(0.01)
    manager.touch("alice@test.com")
    second = manager._last_seen["alice@test.com"]

    assert second >= first


def test_get_returns_none_when_absent(manager):
    """get() should return None for user without scheduler."""
    assert manager.get("nobody@test.com") is None


@patch("web_api.scheduler_manager.start_scheduler")
def test_get_or_create_creates_scheduler(mock_start, manager):
    """get_or_create() should create scheduler on first call."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler

    result = manager.get_or_create("alice@test.com")
    assert result is mock_scheduler
    mock_start.assert_called_once_with(owner_email="alice@test.com")
    assert "alice@test.com" in manager._last_seen


@patch("web_api.scheduler_manager.start_scheduler")
def test_get_or_create_returns_existing(mock_start, manager):
    """get_or_create() should return existing scheduler on subsequent calls."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler

    first = manager.get_or_create("alice@test.com")
    second = manager.get_or_create("alice@test.com")
    assert first is second
    assert mock_start.call_count == 1


@patch("web_api.scheduler_manager.start_scheduler")
def test_remove_shuts_down_scheduler(mock_start, manager):
    """remove() should shutdown scheduler and clean up."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler
    manager.get_or_create("alice@test.com")

    manager.remove("alice@test.com")

    mock_scheduler.shutdown.assert_called_once_with(wait=False)
    assert manager.get("alice@test.com") is None
    assert "alice@test.com" not in manager._last_seen


def test_remove_nonexistent_user_no_error(manager):
    """remove() on nonexistent user should not raise."""
    manager.remove("nobody@test.com")  # should not raise


@patch("web_api.scheduler_manager.start_scheduler")
def test_reap_inactive_removes_idle_scheduler(mock_start, manager):
    """reap_inactive() should remove schedulers that are idle and have no work."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler
    manager.get_or_create("idle@test.com")

    # Simulate being idle for > timeout
    manager._last_seen["idle@test.com"] = datetime.now(timezone.utc) - timedelta(seconds=120)

    with patch("web_api.scheduler_manager.get_db_manager") as mock_db_fn:
        mock_db = MagicMock()
        mock_db.has_incomplete_tasks_for_owner.return_value = False
        mock_db.has_active_runs_for_owner.return_value = False
        mock_db_fn.return_value = mock_db
        manager.reap_inactive()

    assert manager.get("idle@test.com") is None
    mock_scheduler.shutdown.assert_called_once()


@patch("web_api.scheduler_manager.start_scheduler")
def test_reap_inactive_keeps_scheduler_with_work(mock_start, manager):
    """reap_inactive() should keep scheduler if user has incomplete tasks."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler
    manager.get_or_create("busy@test.com")

    # Simulate being idle for > timeout
    manager._last_seen["busy@test.com"] = datetime.now(timezone.utc) - timedelta(seconds=120)

    with patch("web_api.scheduler_manager.get_db_manager") as mock_db_fn:
        mock_db = MagicMock()
        mock_db.has_incomplete_tasks_for_owner.return_value = True
        mock_db_fn.return_value = mock_db
        manager.reap_inactive()

    assert manager.get("busy@test.com") is not None


@patch("web_api.scheduler_manager.start_scheduler")
def test_reap_inactive_starts_scheduler_for_active_user_with_work(mock_start, manager):
    """reap_inactive() should create scheduler for user who has work but no scheduler."""
    mock_scheduler = MagicMock()
    mock_start.return_value = mock_scheduler

    # User is in _last_seen (recently active) but has no scheduler
    manager.touch("new@test.com")

    with patch("web_api.scheduler_manager.get_db_manager") as mock_db_fn:
        mock_db = MagicMock()
        mock_db.has_incomplete_tasks_for_owner.return_value = True
        mock_db_fn.return_value = mock_db
        manager.reap_inactive()

    assert manager.get("new@test.com") is mock_scheduler


@patch("web_api.scheduler_manager.start_scheduler")
def test_active_count(mock_start, manager):
    """active_count should return number of running schedulers."""
    mock_start.return_value = MagicMock()
    assert manager.active_count == 0

    manager.get_or_create("a@t.com")
    assert manager.active_count == 1

    manager.get_or_create("b@t.com")
    assert manager.active_count == 2


@patch("web_api.scheduler_manager.start_scheduler")
def test_shutdown_all(mock_start, manager):
    """shutdown_all() should stop all schedulers."""
    mock_start.return_value = MagicMock()
    manager.get_or_create("a@t.com")
    manager.get_or_create("b@t.com")

    manager.shutdown_all()
    assert manager.active_count == 0
    assert len(manager._last_seen) == 0
