"""The driver presence heartbeat renews only attempts whose node can still accept
a report (issue iss-17df29dd686449a4).

`POST /api/dsh-drivers/<project>/heartbeat` renews every active attempt of the
calling driver. Its attempt SELECT had no join on ``state_nodes``, so it also
renewed attempts whose node is CLOSED (``core.state_graph.CLOSED_STATUSES``:
VERIFIED, SUPERSEDED). Every renewal sets ``lease_expires_at = now + 7200 s``
and the driver runtime calls that route every 20 minutes, so such an attempt's
expiry was pushed forward forever and it never reached the design §4.4 reclaim
precondition (expiry + 900 s grace,
``core/state_recovery.py::_reclaimable``): a non-admin director could neither
abandon nor take the attempt over.

The test drives the extracted module-level helper directly - no live driver
runtime - and then the State ``heartbeat`` action with exactly the items it
returned, which is what the route does.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from api.dsh_driver_routers import _renewable_attempts
from core import state_claims
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}
DRIVER = "grok"
KEYS = ("open", "superseded", "verified")


class Clock:
    def __init__(self):
        self.moment = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(state_claims, "_clock", fake)
    return fake


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


def svc(db, driver=None, *, admin=False):
    actor = f"driver:{driver}" if driver else "authorized-state-operator"
    return StateService(StateDatabase(str(db)), actor=actor, project_read_trusted=True,
                        driver_id=driver, is_admin=admin)


def write(service, action, **arguments):
    return execute(service, action, arguments, allow_write=True)


def read(service, action, **arguments):
    return execute(service, action, arguments)


def lease_row(db, attempt_id):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT status,lease_expires_at,last_heartbeat_at FROM state_attempts "
                                 "WHERE attempt_id=?", (attempt_id,)).fetchone())
    finally:
        conn.close()


@pytest.fixture
def world(db, clock):
    """One driver with three active attempts on an OPEN, a SUPERSEDED and a
    VERIFIED node. The attempts are registered while every node is still OPEN
    and the two nodes are closed afterwards, which is the shape that made this
    a defect: the attempt stays ``running`` while its node is already closed."""
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": key, **NODE} for key in KEYS])
    driver = svc(db, DRIVER)
    attempts = {key: write(driver, "start_external_attempt", project_id="p", node_key=key, expected_revision=1,
                           harness="h", external_id="job-" + key, request_key="rk-" + key) for key in KEYS}
    write(driver, "supersede_node", project_id="p", node_key="superseded", expected_revision=1, reason="dropped")
    # VERIFIED normally comes from verify_node over a receipted attempt; this
    # test needs only the closed status, so the row is set directly rather than
    # building an evidence/receipt chain that is not what is under test.
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE state_nodes SET status='VERIFIED' WHERE project_id='p' AND node_key='verified'")
    conn.commit()
    conn.close()
    return driver, attempts


def test_the_helper_returns_only_the_attempt_whose_node_is_open(world, db):
    driver, attempts = world
    assert {key: read(driver, "get_node", project_id="p", node_key=key)["node"]["status"] for key in KEYS} == {
        "open": "OPEN", "superseded": "SUPERSEDED", "verified": "VERIFIED"}
    assert {lease_row(db, a["attempt_id"])["status"] for a in attempts.values()} == {"running"}, \
        "all three attempts are active: the exclusion is the node, not the attempt"
    assert _renewable_attempts(driver, "p", DRIVER) == [
        {"attempt_id": attempts["open"]["attempt_id"], "fence": attempts["open"]["owner_fence"]}]


def test_the_heartbeat_action_leaves_a_closed_nodes_attempt_lease_alone(world, db, clock):
    driver, attempts = world
    renew = _renewable_attempts(driver, "p", DRIVER)
    before = {key: lease_row(db, attempt["attempt_id"]) for key, attempt in attempts.items()}
    clock.advance(60)                       # the runtime's next pump; one lease renewal later
    out = write(driver, "heartbeat", project_id="p", attempts=renew)
    assert [item["id"] for item in out["renewed"]] == [attempts["open"]["attempt_id"]] and out["refused"] == []
    after = {key: lease_row(db, attempt["attempt_id"]) for key, attempt in attempts.items()}
    for closed in ("superseded", "verified"):
        assert after[closed] == before[closed], f"{closed}: a closed node never accepts a report, so never renew"
    assert after["open"]["last_heartbeat_at"] == out["heartbeat_at"] != before["open"]["last_heartbeat_at"]
    assert after["open"]["lease_expires_at"] > before["open"]["lease_expires_at"]
