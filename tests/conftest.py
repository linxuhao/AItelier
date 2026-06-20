# tests/conftest.py
# Shared fixtures across unit and integration tests.

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from api.main import app
from api.dependencies import get_db_manager, get_workspace_manager
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager


@pytest.fixture(autouse=True, scope="session")
def _isolated_scheduler_lock():
    """Point the scheduler advisory lock at an isolated temp file for the whole
    test session, so the suite never contends with a running/orphaned AItelier
    scheduler holding the production ~/.AItelier/scheduler.lock (which would make
    start_scheduler() create 0 jobs and fail the reschedule tests)."""
    fd, path = tempfile.mkstemp(suffix="-scheduler.lock")
    os.close(fd)
    prev = os.environ.get("AITELIER_SCHEDULER_LOCK")
    os.environ["AITELIER_SCHEDULER_LOCK"] = path
    yield
    if prev is None:
        os.environ.pop("AITELIER_SCHEDULER_LOCK", None)
    else:
        os.environ["AITELIER_SCHEDULER_LOCK"] = prev
    try:
        os.remove(path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _release_scheduler_lock():
    """Release the scheduler advisory lock after each test.

    Tests that call start_scheduler() acquire an fcntl flock on
    ~/.AItelier/scheduler.lock via the module-level _scheduler_lock_fh.
    Without cleanup, subsequent tests see the lock held and skip
    scheduler creation (0 jobs, assertion failures).
    """
    yield
    import core.scheduler as _sched
    if _sched._scheduler_lock_fh is not None:
        try:
            _sched._scheduler_lock_fh.close()
        except Exception:
            pass
        _sched._scheduler_lock_fh = None


@pytest.fixture
def db_manager(tmp_path):
    """Provides an isolated SQLite database for testing."""
    db_file = tmp_path / "test.db"
    return DBManager(str(db_file))


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    """FastAPI TestClient with DB and workspace dependency overrides."""
    test_db = DBManager(str(tmp_path / "test.db"))
    test_ws = WorkspaceManager(str(tmp_path / "ws"))

    app.dependency_overrides[get_db_manager] = lambda: test_db
    app.dependency_overrides[get_workspace_manager] = lambda: test_ws
    app.state._test_mode = True

    with TestClient(app) as c:
        yield c

    app.state._test_mode = False
    app.dependency_overrides.clear()
