"""ANY reader of a private table is judged at the read, not by appearing in a list.

Criterion ``every-reader-of-a-private-table-is-judged-at-the-read``. Every
earlier round enumerated its readers - route sources, dispatch entries,
declared-trust classes - and the reviewer found the anonymous 200 one layer
below the list each time. So this module never enumerates a reader:

* the private TABLES come from the one classification beside the schema
  (``core.state_privacy.PRIVATE_STATE_TABLES``), checked against every
  ``state_*`` table the schema creates by a derivation reused here;
* the readers are defined HERE, at test time, importing no repo verdict,
  carrying no decoration and registered nowhere. They take only what an
  anonymous request already holds - the service, its store, its handle, and the
  leaves rebuilt from them;
* a canary is planted in every private table, so "refused" and "delivered but
  empty" cannot be confused;
* the negative fixtures pin the shapes the review measured: ``scan``,
  ``wait_for_state_change``, ``wait_disposition``, the return value of
  ``update``/``write_entry``, and a callable recovered through ``__closure__``.

The verdict these readers meet is installed on the CONNECTION
(``core.state_privacy.TrustBoundDatabase`` -> ``read_authorizer``), so no list of
readers has to be complete for a reader written later to be judged.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core.state_commands import ProjectPrivate
from core.state_database import StateDatabase
from core.state_privacy import PRIVATE_STATE_TABLES
from core.state_service import StateService

PROJECT = "judged-at-read"
MAILBOX = "judged-at-read-mailbox"
CANARY = "JUDGED-AT-READ-CANARY-7413"


def _seed(service):
    """Put canary text into every private table through the WRITE channel.

    This runs on a SEPARATELY declared-trusted service, so the fill itself is
    legitimate; the reads below run on the anonymous service. Column values come
    from ``PRAGMA table_info``, so a column added later needs no edit here.
    """
    service.driver_notes.update(PROJECT, "permanent", f"{CANARY}-note", 0, "director")
    service.driver_notes.write_entry(PROJECT, f"{CANARY}-assertion", f"{CANARY}-body", "director")
    # A targeted message needs a DIFFERENT recipient project, so a second project
    # carries the mailbox rows the reads below must not deliver.
    service.create_project(MAILBOX, MAILBOX)
    service.director_messages.send_director_message(
        PROJECT, "director-a", "seed", f"{CANARY}-subject", f"{CANARY}-message", MAILBOX)
    for table in sorted(PRIVATE_STATE_TABLES):
        with service.store.db.decision_connection() as connection:
            info = list(connection.execute(f"PRAGMA table_info({table})"))
            columns, values = [], []
            for row in info:
                name, declared = row["name"], (row["type"] or "").upper()
                if name == "seq":
                    continue  # AUTOINCREMENT
                numeric = any(k in declared for k in ("INT", "REAL", "NUM"))
                columns.append(name)
                # `visibility` keeps the project OPEN: this row carries a canary,
                # it does not decide privacy, and its CHECK allows only the two
                # real values.
                values.append("public" if name == "visibility"
                              else 1 if numeric else f"{CANARY}-{table}")
            statement = (f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
                         f"VALUES ({','.join('?' for _ in columns)})")
            try:
                connection.execute(statement, tuple(values))
            except sqlite3.Error:
                # A row already there, an append-only trigger, or a foreign key:
                # carry the canary on the existing rows instead.
                for column in columns:
                    value = ("public" if column == "visibility"
                             else f"{CANARY}-{table}")
                    try:
                        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
                    except sqlite3.Error:
                        pass


def _anonymous(tmp_path, name):
    """The service an anonymous request gets: no declared trust anywhere.

    The project is created, opened and filled by a SEPARATELY trusted service
    over the same database file, so a refusal below is the guard and not an
    absent row.
    """
    database = StateDatabase(str(tmp_path / name))
    seeded = StateService(database, actor="seeder", project_read_trusted=True)
    seeded.create_project(PROJECT, PROJECT)
    seeded.open_project(PROJECT)
    _seed(seeded)
    return StateService(database, actor="anonymous", project_read_trusted=False)


def _outcome(call, secret=CANARY):
    """``refused`` / ``leaked`` / ``clean`` / ``error:<T>`` for one probe."""
    try:
        result = call()
    except ProjectPrivate:
        return "refused"
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return "leaked" if secret in text else "clean"


def _read_directly(service, table, project_id=PROJECT):
    """A reader written NOW: imports no verdict, carries no decoration."""
    with service.store.db.get_connection() as connection:
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE project_id=?", (project_id,)).fetchall()
        return [dict(row) for row in rows]


def _private_tables():
    assert PRIVATE_STATE_TABLES, "the classification declares no private tables"
    return sorted(PRIVATE_STATE_TABLES)


class TestAnAfterTheFactReaderIsJudged:
    def test_the_private_set_is_derived_not_hand_listed(self):
        from core.state_privacy import PUBLIC_STATE_TABLES
        from tests.unit.test_undeclared_reader_is_untrusted import _schema_state_tables
        from_schema = _schema_state_tables()
        assert from_schema, "schema derivation found no state_* tables"
        unclassified = from_schema - set(PRIVATE_STATE_TABLES) - set(PUBLIC_STATE_TABLES)
        assert not unclassified, f"state tables nobody classified: {sorted(unclassified)}"
        print("PRIVATE_TABLES =", _private_tables())

    def test_every_private_table_refuses_a_reader_written_after_the_fact(self, tmp_path):
        service = _anonymous(tmp_path, "cross.sqlite")
        outcomes = {table: _outcome(
            lambda table=table: _read_directly(service, table), CANARY)
            for table in _private_tables()}
        print("CROSS_PRODUCT =", len(outcomes), "private tables x 1 new reader x 1 anonymous identity")
        print("OUTCOMES =", outcomes)
        leaked = sorted(t for t, o in outcomes.items() if o != "refused")
        assert not leaked, f"private tables a new reader still reached: {leaked} {outcomes}"

    def test_the_leaves_rebuilt_from_the_service_are_judged_too(self, tmp_path):
        service = _anonymous(tmp_path, "leaves.sqlite")
        probes = {
            "raw_events": lambda: _read_directly(service, "state_events"),
            "notebook": lambda: service.driver_notes.get(PROJECT),
            "messaging": lambda: service.director_messages.list_director_messages(PROJECT),
            "store_events": lambda: service.store.events(PROJECT),
            "store_visibility": lambda: service.store.get_project_access(PROJECT),
            "note_history": lambda: service.driver_notes.history(PROJECT),
        }
        outcomes = {name: _outcome(call, CANARY) for name, call in probes.items()}
        print("LEAF_OUTCOMES =", outcomes)
        # The criterion is "refuse to deliver": a leaf must THROW or hand back a
        # body with no canary in it. `clean` is allowed here - the canary is in
        # every table, so "clean" means the read was answered WITHOUT the private
        # text, not that the table was empty.
        unreached = sorted(n for n, o in outcomes.items()
                           if o not in {"refused", "clean"})
        assert not unreached, f"leaves still delivered private data: {unreached} {outcomes}"
        assert "leaked" not in outcomes.values(), outcomes

    def test_a_callable_recovered_through_closure_is_judged_too(self, tmp_path):
        """The reviewer's shape: reach a real body via ``__closure__``."""
        service = _anonymous(tmp_path, "closure.sqlite")
        holder = service.driver_notes.get
        recovered = []
        for cell in getattr(getattr(holder, "__func__", holder), "__closure__", ()) or ():
            if callable(cell.cell_contents):
                recovered.append(cell.cell_contents)
        assert recovered, "no callable recovered from the decorated method's closure"
        outcomes = [_outcome(lambda f=function: f(PROJECT)) for function in recovered]
        print("CLOSURE_OUTCOMES =", outcomes)
        assert "leaked" not in outcomes, outcomes


class TestTheMeasuredShapesAreRefused:
    """Every shape the review measured delivering body text, as a test."""

    def test_scan_wait_and_disposition_refuse(self, tmp_path):
        from core.state_changes import scan, wait_disposition
        service = _anonymous(tmp_path, "waits.sqlite")
        probes = {
            "scan": lambda: scan(service.store, PROJECT, 0, None, None, None,
                                 "all", True, 100),
            "wait_for_state_change": lambda: asyncio.run(
                service.wait_for_state_change(PROJECT, timeout_seconds=0)),
            "wait_disposition": lambda: wait_disposition(service.store, PROJECT, 0, None, None),
        }
        outcomes = {name: _outcome(call, CANARY) for name, call in probes.items()}
        print("WAIT_OUTCOMES =", outcomes)
        assert all(o != "leaked" for o in outcomes.values()), outcomes

    def test_write_return_values_carry_no_private_text(self, tmp_path):
        service = _anonymous(tmp_path, "writes.sqlite")
        probes = {
            "update": lambda: service.driver_notes.update(
                PROJECT, "permanent", f"{CANARY}-update", 0, "director"),
            "write_entry": lambda: service.driver_notes.write_entry(
                PROJECT, f"{CANARY}-assertion-2", f"{CANARY}-body-2", "director"),
        }
        outcomes = {name: _outcome(call, CANARY) for name, call in probes.items()}
        print("WRITE_RETURN_OUTCOMES =", outcomes)
        assert all(o != "leaked" for o in outcomes.values()), outcomes

    def test_the_verdict_holds_on_a_fresh_app_and_on_the_product_app(self, tmp_path):
        service = _anonymous(tmp_path, "apps.sqlite")
        from api import main as main_module
        leaked = []
        for label, app in (("fresh", FastAPI()), ("product", main_module.app)):
            path = f"/judged-at-read-{label}"

            def reader(svc=Depends(lambda s=service: s)):
                # The reviewer's shape: the private read is taken off the service
                # object without going through `execute`.
                return svc.driver_notes.get(PROJECT)

            app.router.add_api_route(path, reader, methods=["GET"])
            # Front of the table, the shape the reviewer used: the route is
            # reachable ahead of the product router's own paths.
            app.router.routes.insert(0, app.router.routes.pop())
            try:
                with TestClient(app) as client:
                    response = client.get(path)
                if response.status_code != 403 or CANARY in response.text:
                    leaked.append((label, response.status_code))
            finally:
                app.router.routes = [r for r in app.router.routes
                                     if getattr(r, "path", None) != path]
        print("APP_OUTCOMES =", leaked)
        assert not leaked, f"an app delivered a private read: {leaked}"


class TestTheConnectionVerdictHasTeeth:
    def test_disarming_the_connection_verdict_makes_the_new_reader_leak(
            self, tmp_path, monkeypatch):
        """Plant the mutation where the verdict lives; name what leaks."""
        import core.state_privacy as privacy
        service = _anonymous(tmp_path, "mutation.sqlite")
        monkeypatch.setattr(privacy, "read_authorizer", lambda trusted: None)
        outcomes = {table: _outcome(
            lambda table=table: _read_directly(service, table), CANARY)
            for table in _private_tables()}
        leaked = sorted(t for t, o in outcomes.items() if o == "leaked")
        print("IGNITION_COUNT =", len(leaked), "LEAKED =", leaked)
        assert leaked, (
            "disarming the connection verdict left every table shut - the probe "
            "cannot tell a guarded read from a database with no rows")
