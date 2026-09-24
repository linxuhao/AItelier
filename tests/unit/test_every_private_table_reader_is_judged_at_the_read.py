"""ANY reader of a private table is judged at the read, not by appearing in a list.

Criterion ``every-reader-of-a-private-table-is-judged-at-the-read``. The readers
below are DEFINED HERE, at test time: they import no verdict, carry no
decoration and are registered nowhere. Each holds only what an anonymous request
holds - ``svc``, ``svc.store``, ``svc.db``, leaves rebuilt from them, callables
reached through ``__self__``/``__func__``/``__closure__``, and the connections
those hand out - and reads one private table.

The matrix is every shape x every private table (derived from
``core.state_privacy.PRIVATE_STATE_TABLES``) x one anonymous identity x one
OPENED project, and the canaries are the ones ``tests/support/state_canaries.py``
plants (one distinct canary per table, read back first on a raw connection).
Each cell is one of

* ``REFUSED`` - the one refusal (``ProjectPrivate``);
* ``ABSENT``  - the attribute the shape needs does not exist on this tree (the
  call result is recorded);
* ``CLEAN``   - an answer that carries no canary of that table;
* ``LEAKED``  - an answer that carries it;
* ``ERROR``   - anything else. A compile error or ``OperationalError`` is a
  failed MEASUREMENT, never a refusal, so an ``ERROR`` fails the test.

Shapes the director ruled out of scope on 2026-09-23 (the connection's own
``set_authorizer(None)``/``backup``/``blobopen``, reopening the file by its path,
forging a trust declaration) are run and printed with scope ``out`` and are not
part of the pass condition.

The file imports no name that only exists on this round, so it also runs on the
base; attributes missing there are ``ABSENT``.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import types

import pytest
from fastapi import Depends

from api.state_graph_routers import get_service
from core.state_commands import ProjectPrivate, execute
from core.state_privacy import PRIVATE_STATE_TABLES
from core.state_service import StateService
from tests.support import state_canaries as C


class Absent(Exception):
    """The attribute or parameter a shape needs does not exist on this tree."""


def _need(owner, name):
    if not hasattr(owner, name):
        raise Absent(f"{type(owner).__name__}.{name} does not exist")
    return getattr(owner, name)


def _rows(connection, sql, params=()):
    return [tuple(row) for row in connection.execute(sql, params).fetchall()]


def _columns(path, table):
    """Column names, known to anyone who reads the public schema source."""
    raw = sqlite3.connect(path)
    try:
        return [row[1] for row in raw.execute(f"PRAGMA table_info({table})")]
    finally:
        raw.close()


def _has_dbpage(path):
    raw = sqlite3.connect(path)
    try:
        raw.execute("SELECT 1 FROM sqlite_dbpage LIMIT 1").fetchall()
        return True
    except sqlite3.Error:
        return False
    finally:
        raw.close()


def _notebook_parameter(svc):
    try:
        parameters = inspect.signature(svc.store.db.get_connection).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "notebook" not in parameters:
        raise Absent("get_connection has no notebook parameter")


# ---------------------------------------------------------------- the readers
def S01(svc, table):
    with svc.store.db.get_connection() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S02(svc, table):
    with svc.db.get_connection() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S03(svc, table):
    with _need(svc.store.db, "decision_connection")() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S04(svc, table):
    with _need(svc.store.db, "unrestricted").get_connection() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S05(svc, table):
    with svc.store.transaction(write=True) as c:
        return _rows(c, f"SELECT * FROM {table}")


def S06(svc, table):
    with _need(svc.store, "_decision")() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S11(svc, table):
    owner = svc.director_messages.list_director_messages.__self__
    with owner.store.transaction(write=True) as c:
        return _rows(c, f"SELECT * FROM {table}")


def S12(svc, table):
    method = svc.driver_notes.get
    assert method.__func__.__closure__, "no closure on the decorated method"
    owner = method.__self__
    with _need(owner.store.db, "unrestricted").get_connection() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S13(svc, table):
    with svc.store.transaction() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S14(svc, table):
    with svc.store.db.get_connection() as c:
        c.execute("ATTACH DATABASE ? AS judged", (svc.store.db.db_path,))
        return _rows(c, f"SELECT * FROM judged.{table}")


def S16(svc, table):
    with svc.store.db.get_connection() as c:
        return list(c.iterdump())


def S17(svc, table):
    with svc.store.db.get_connection() as c:
        c.execute(f"CREATE TEMP VIEW judged_view AS SELECT * FROM main.{table}")
        return _rows(c, "SELECT * FROM judged_view")


def S18(svc, table):
    joined = " || '|' || ".join(f"quote({column})"
                                for column in _columns(svc.store.db.db_path, table))
    with svc.store.db.get_connection() as c:
        c.execute("CREATE TEMP TABLE judged_loot(x)")
        c.execute("CREATE TEMP TABLE judged_kick(x)")
        c.execute("CREATE TEMP TRIGGER judged_trigger AFTER INSERT ON judged_kick BEGIN "
                  f"INSERT INTO judged_loot SELECT {joined} FROM main.{table}; END")
        c.execute("INSERT INTO judged_kick VALUES (1)")
        return _rows(c, "SELECT * FROM judged_loot")


def S19(svc, table):
    if not _has_dbpage(svc.store.db.db_path):
        raise Absent("this sqlite build has no sqlite_dbpage")
    with svc.store.db.get_connection() as c:
        return [bytes(row[0]).decode("latin-1")
                for row in c.execute("SELECT data FROM sqlite_dbpage").fetchall()]


def X01(svc, table):
    """A CTE named like the table, reading the database's own table."""
    with svc.store.db.get_connection() as c:
        return _rows(c, f"WITH {table} AS (SELECT * FROM main.{table}) SELECT * FROM {table}")


def X02(svc, table):
    """The notebook connection of the UNOPENED project."""
    _notebook_parameter(svc)
    with svc.store.db.get_connection(notebook=C.SHUT) as c:
        return _rows(c, f"SELECT * FROM {table}")


def X03(svc, table):
    """The notebook connection of the opened project, reading ``main.``."""
    _notebook_parameter(svc)
    with svc.store.db.get_connection(notebook=C.OPEN) as c:
        return _rows(c, f"SELECT * FROM main.{table}")


def X04(svc, table):
    """The notebook connection of the opened project, unqualified name."""
    _notebook_parameter(svc)
    with svc.store.transaction(notebook=C.OPEN) as c:
        return _rows(c, f"SELECT * FROM {table}")


def X05(svc, table):
    """``temp.``-qualified name."""
    with svc.store.db.get_connection() as c:
        try:
            return _rows(c, f"SELECT * FROM temp.{table}")
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            raise Absent(f"the armed connection has no temp copy of {table}: {exc}")


def X06(svc, table):
    """A store rebuilt from the anonymous service's handle."""
    from core.state_graph import StateGraphStore
    with StateGraphStore(svc.db).transaction() as c:
        return _rows(c, f"SELECT * FROM {table}")


def X07(svc, table):
    """A store rebuilt from the store's handle, through ``__self__``."""
    from core.state_graph import StateGraphStore
    owner = svc.store.get_project.__self__
    with StateGraphStore(owner.db).transaction() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S07(svc, table):
    with svc.store.db.get_connection() as c:
        c.set_authorizer(None)
        return _rows(c, f"SELECT * FROM main.{table}")


def S08(svc, table):
    with svc.store.db.get_connection() as c:
        memory = sqlite3.connect(":memory:")
        c.backup(memory)
        return _rows(memory, f"SELECT * FROM {table}")


def S15(svc, table):
    out = []
    with svc.store.db.get_connection() as c:
        for column in _columns(svc.store.db.db_path, table):
            for rowid in range(1, 12):
                try:
                    with c.blobopen(table, column, rowid, readonly=True) as blob:
                        out.append(bytes(blob.read()).decode("utf-8", "replace"))
                except (sqlite3.Error, TypeError, ValueError):
                    continue
    return out


def S09(svc, table):
    connection = sqlite3.connect(svc.store.db.db_path)
    try:
        return _rows(connection, f"SELECT * FROM {table}")
    finally:
        connection.close()


def S10(svc, table):
    with type(svc.db)(svc.store.db.db_path).get_connection() as c:
        return _rows(c, f"SELECT * FROM {table}")


def S20(svc, table):
    from core.state_graph import StateGraphStore
    with StateGraphStore(svc.store.db, project_read_trusted=True).transaction() as c:
        return _rows(c, f"SELECT * FROM {table}")


IN_SCOPE = [S01, S02, S03, S04, S05, S06, S11, S12, S13, S14, S16, S17, S18, S19,
            X01, X02, X03, X04, X05, X06, X07]
OUT_OF_SCOPE = [S07, S08, S15, S09, S10, S20]


def outcome(shape, svc, table):
    try:
        out = shape(svc, table)
    except ProjectPrivate:
        return "REFUSED", ""
    except Absent as exc:
        return "ABSENT", str(exc)
    except Exception as exc:  # a failed measurement, never a refusal
        return "ERROR", f"{type(exc).__name__}: {str(exc)[:120]}"
    text = out if isinstance(out, str) else repr(out)
    return ("LEAKED" if C.canary(table) in text else "CLEAN"), ""


def _matrix(svc, shapes):
    cells = {}
    for shape in shapes:
        for table in sorted(PRIVATE_STATE_TABLES):
            cells[(shape.__name__, table)] = outcome(shape, svc, table)
    return cells


def _print(label, cells):
    for (shape, table), (result, detail) in sorted(cells.items()):
        print(f"CELL {label} shape={shape} table={table} outcome={result} {detail}".rstrip())
    tally = {}
    for result, _ in cells.values():
        tally[result] = tally.get(result, 0) + 1
    shapes = sorted({s for s, _ in cells})
    print(f"TALLY {label}: {len(shapes)} shapes x {len(PRIVATE_STATE_TABLES)} private tables x "
          f"1 anonymous identity x 1 opened project = {len(cells)} cells; {sorted(tally.items())}")


@pytest.fixture
def anonymous(tmp_path):
    db = C.new_database(tmp_path, "judged.sqlite")
    present = C.canary_presence(db.db_path)
    print("CANARY_PRESENT =", present)
    assert all(present.values()), f"canaries missing before any read: {present}"
    return StateService(db, actor="anonymous", project_read_trusted=False)


class TestTheCanariesAreReal:
    def test_the_private_set_is_derived_not_hand_listed(self):
        from core.state_privacy import PUBLIC_STATE_TABLES
        from tests.unit.test_undeclared_reader_is_untrusted import _schema_state_tables
        from_schema = _schema_state_tables()
        assert from_schema, "schema derivation found no state_* tables"
        unclassified = from_schema - set(PRIVATE_STATE_TABLES) - set(PUBLIC_STATE_TABLES)
        assert not unclassified, f"state tables nobody classified: {sorted(unclassified)}"

    def test_every_private_table_carries_its_own_canary(self, tmp_path):
        db = C.new_database(tmp_path, "present.sqlite")
        present = C.canary_presence(db.db_path)
        print("CANARY_PRESENT =", present)
        assert sorted(present) == sorted(PRIVATE_STATE_TABLES)
        assert all(present.values()), present
        assert len({C.canary(t) for t in PRIVATE_STATE_TABLES}) == len(PRIVATE_STATE_TABLES)


class TestAnAfterTheFactReaderIsJudged:
    def test_no_in_scope_reader_delivers_a_canary(self, anonymous):
        cells = _matrix(anonymous, IN_SCOPE)
        _print("in", cells)
        leaked = sorted(k for k, v in cells.items() if v[0] == "LEAKED")
        errors = sorted((k, v[1]) for k, v in cells.items() if v[0] == "ERROR")
        assert not errors, f"measurement failures (not refusals): {errors}"
        assert not leaked, "in-scope readers delivered private rows: " + ", ".join(
            f"{shape}/{table}" for shape, table in leaked)

    def test_out_of_scope_shapes_are_measured_and_printed(self, anonymous):
        """Not a pass condition (director ruling 2026-09-23): printed so the
        delivery note can state exactly what they do on this tree."""
        _print("out", _matrix(anonymous, OUT_OF_SCOPE))

    def test_removing_the_row_verdict_leaks_every_private_table(self, anonymous, monkeypatch):
        """The layer this round placed is `core.state_privacy._arm`. Without
        it, the same after-the-fact reader reaches every private table."""
        import core.state_privacy as privacy
        if not hasattr(privacy, "_arm"):
            pytest.skip("no row verdict on this tree")
        ignition = []

        def disarmed(connection, notebook):
            ignition.append(1)
            return types.SimpleNamespace(denied=False)

        monkeypatch.setattr(privacy, "_arm", disarmed)
        cells = _matrix(anonymous, [S01])
        leaked = sorted(t for (_, t), (r, _) in cells.items() if r == "LEAKED")
        print("DISARMED IGNITION_COUNT =", len(ignition), "LEAKED =", leaked)
        assert ignition, "the disarmed layer never ran"
        assert leaked == sorted(PRIVATE_STATE_TABLES), (
            f"only {len(leaked)} of {len(PRIVATE_STATE_TABLES)} private tables leaked without "
            f"the row verdict; not leaking: {sorted(set(PRIVATE_STATE_TABLES) - set(leaked))}")


# ---------------------------------------------------------------- the review's paths
def _closure_body(method):
    function = getattr(method, "__func__", method)
    for cell in function.__closure__ or ():
        if callable(cell.cell_contents):
            return cell.cell_contents
    raise Absent(f"{function.__qualname__} has no callable in its closure")


def _run(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _reviewed_paths(svc):
    """The shapes earlier reviews measured delivering private text, each called
    with the arguments it really takes."""
    from core.state_changes import scan, wait_disposition
    return {
        "scan": lambda: scan(svc.store, C.OPEN, 0, None, None, None, "all", False, 100),
        "wait_for_state_change": lambda: _run(svc.wait_for_state_change(
            C.OPEN, timeout_seconds=0, actionable_only=False)),
        "wait_disposition": lambda: wait_disposition(svc.store, C.OPEN, 0, None, None, 0),
        "update_return": lambda: svc.driver_notes.update(
            C.OPEN, "permanent", "anonymous write", 1, "anonymous"),
        "write_entry_return": lambda: svc.driver_notes.write_entry(
            C.OPEN, "anonymous assertion", "anonymous body", "anonymous"),
        "closure_list_director_messages": lambda: _closure_body(
            svc.director_messages.list_director_messages)(svc.director_messages, C.OPEN),
        "closure_events": lambda: _closure_body(svc.store.events)(svc.store, C.OPEN),
        "closure_get_project_access": lambda: _closure_body(
            svc.store.get_project_access)(svc.store, C.OPEN),
        "closure_project_visibility": lambda: _closure_body(svc.project_visibility)(svc, C.OPEN),
        "closure_wait_for_state_change": lambda: _run(_closure_body(svc.wait_for_state_change)(
            svc, C.OPEN, timeout_seconds=0, actionable_only=False)),
        "closure_driver_note_get_unopened": lambda: _closure_body(svc.driver_notes.get)(
            svc.driver_notes, C.SHUT),
    }


def _reviewed_outcome(call):
    try:
        out = call()
    except ProjectPrivate:
        return "REFUSED", ""
    except Absent as exc:
        return "ABSENT", str(exc)
    except Exception as exc:
        return "ERROR", f"{type(exc).__name__}: {str(exc)[:120]}"
    text = json.dumps(out, default=str)
    leaked = C.leaked_tables(text)
    return ("LEAKED" if leaked else "CLEAN"), ",".join(leaked)


# A private ACTION's body must REFUSE; a notebook body on an unopened project
# must answer without any canary (the notebook is public only when opened).
_MUST_REFUSE = {"scan", "wait_for_state_change", "update_return", "write_entry_return",
                "closure_list_director_messages", "closure_events",
                "closure_get_project_access", "closure_project_visibility",
                "closure_wait_for_state_change"}


class TestTheReviewedPathsAreRefused:
    def test_in_process(self, anonymous):
        results = {name: _reviewed_outcome(call)
                   for name, call in _reviewed_paths(anonymous).items()}
        for name, (result, detail) in sorted(results.items()):
            print(f"REVIEWED in-process path={name} outcome={result} {detail}".rstrip())
        wrong = {n: r for n, r in results.items()
                 if r[0] in {"LEAKED", "ERROR"} or (n in _MUST_REFUSE and r[0] != "REFUSED")}
        assert not wrong, wrong

    @pytest.mark.parametrize("mode", ["fresh", "main"])
    def test_over_http_unguarded(self, tmp_path, monkeypatch, mode):
        C.arm(monkeypatch)
        db = C.new_database(tmp_path, f"reviewed-{mode}.sqlite")
        app = C.app_for(mode, db)
        client = C.client_for(app, mode)
        results = {}
        names = sorted(_MUST_REFUSE | {"wait_disposition", "closure_driver_note_get_unopened"})
        try:
            assert C.request_is_anonymous(app, client), "the per-request service is not anonymous"
            for name in names:
                def plain(svc=Depends(get_service), _name=name):
                    return _reviewed_paths(svc)[_name]()

                C.mount(app, f"/judged-http/{name}", plain, guarded=False)
                try:
                    response = client.get(f"/judged-http/{name}")
                    status, text = response.status_code, response.text
                except Exception as exc:
                    status, text = "EXC", f"{type(exc).__name__}: {exc}"
                finally:
                    C.unmount(app, plain)
                results[name] = (status, C.leaked_tables(text), text[:100])
        finally:
            C.release(app)
        for name, (status, leaked, head) in sorted(results.items()):
            print(f"REVIEWED http app={mode} path={name} guarded=False status={status} "
                  f"leaked={leaked} body={head!r}")
        print(f"CROSS_PRODUCT http-unguarded app={mode}: {len(names)} paths x 1 unguarded route x GET "
              f"x 1 anonymous identity x 1 opened project = {len(results)} requests")
        wrong = {k: v for k, v in results.items()
                 if v[1] or (k in _MUST_REFUSE and (v[0], v[2]) != (403, _REFUSAL))
                 or v[0] not in (200, 403)}
        assert not wrong, wrong

    @pytest.mark.parametrize("mode", ["fresh", "main"])
    def test_behind_the_product_guard(self, tmp_path, monkeypatch, mode):
        """Each reader is written INLINE in a dispatch-family handler (an honest
        public ``execute`` first), the shape the product guard admits - proved
        by ``g_control``, which must answer 200. A refusal must then be the
        reader's own refusal, not the guard's. (Handlers that open a
        connection inline are refused by the guard itself on this tree; the
        unguarded matrix measures those readers.)"""
        C.arm(monkeypatch)
        db = C.new_database(tmp_path, f"guarded-{mode}.sqlite")
        app = C.app_for(mode, db)
        client = C.client_for(app, mode)
        results = {}
        try:
            assert C.request_is_anonymous(app, client), "the per-request service is not anonymous"
            for name, handler in sorted(GUARDED.items()):
                handler._state_route = ("read", None)
                C.mount(app, f"/api/state/judged-guarded/{name}/{{action}}", handler, guarded=True)
                try:
                    response = client.get(f"/api/state/judged-guarded/{name}/get_graph")
                    status, text = response.status_code, response.text
                except Exception as exc:
                    status, text = "EXC", f"{type(exc).__name__}: {exc}"
                finally:
                    C.unmount(app, handler)
                results[name] = (status, C.leaked_tables(text), text[:100])
        finally:
            C.release(app)
        for name, (status, leaked, head) in sorted(results.items()):
            print(f"REVIEWED http app={mode} path={name} guarded=True status={status} "
                  f"leaked={leaked} body={head!r}")
        print(f"CROSS_PRODUCT http-guarded app={mode}: {len(GUARDED)} inline handlers x 1 product-guarded "
              f"dispatch route x GET x 1 anonymous identity x 1 opened project = {len(results)} requests")
        assert results["g_control"][0] == 200, f"the guard did not admit the control: {results['g_control']}"
        wrong = {k: v for k, v in results.items() if k != "g_control"
                 and (v[1] or (v[0], v[2]) != (403, _REFUSAL))}
        assert not wrong, wrong


_REFUSAL = '{"detail":"This State DAG record is not available."}'


def g_control(action: str, svc=Depends(get_service)):
    execute(svc, action, {"project_id": C.OPEN})
    return {"ok": True}


def g_closure_get_project_access(action: str, svc=Depends(get_service)):
    execute(svc, action, {"project_id": C.OPEN})
    return svc.store.get_project_access.__func__.__closure__[1].cell_contents(svc.store, C.OPEN)


def g_closure_events(action: str, svc=Depends(get_service)):
    execute(svc, action, {"project_id": C.OPEN})
    return svc.store.events.__func__.__closure__[1].cell_contents(svc.store, C.OPEN)


def g_closure_list_director_messages(action: str, svc=Depends(get_service)):
    execute(svc, action, {"project_id": C.OPEN})
    return svc.director_messages.list_director_messages.__func__.__closure__[1].cell_contents(
        svc.director_messages, C.OPEN)


def g_closure_project_visibility(action: str, svc=Depends(get_service)):
    execute(svc, action, {"project_id": C.OPEN})
    return svc.project_visibility.__func__.__closure__[1].cell_contents(svc, C.OPEN)


GUARDED = {function.__name__: function for function in (
    g_control, g_closure_get_project_access, g_closure_events, g_closure_list_director_messages,
    g_closure_project_visibility)}


# ---------------------------------------------------------------- the matrix over HTTP
@pytest.mark.parametrize("mode", ["fresh", "main"])
def test_the_in_scope_matrix_over_http(tmp_path, monkeypatch, mode):
    """Every in-scope shape x every private table, driven by an unguarded route
    whose service is the product ``get_service`` for an anonymous request."""
    C.arm(monkeypatch)
    db = C.new_database(tmp_path, f"matrix-{mode}.sqlite")
    app = C.app_for(mode, db)
    client = C.client_for(app, mode)
    shapes = {shape.__name__: shape for shape in IN_SCOPE}

    def reader(shape: str, table: str, svc=Depends(get_service)):
        try:
            return {"rows": repr(shapes[shape](svc, table))}
        except Absent as exc:
            return {"absent": str(exc)}

    cells = {}
    try:
        assert C.request_is_anonymous(app, client), "the per-request service is not anonymous"
        C.mount(app, "/judged-matrix/{shape}/{table}", reader, guarded=False)
        for name in sorted(shapes):
            for table in sorted(PRIVATE_STATE_TABLES):
                try:
                    response = client.get(f"/judged-matrix/{name}/{table}")
                    status, text = response.status_code, response.text
                except Exception as exc:
                    status, text = "EXC", f"{type(exc).__name__}: {str(exc)[:120]}"
                if status == 403:
                    result = "REFUSED"
                elif status == 200 and '"absent"' in text:
                    result = "ABSENT"
                elif status == 200:
                    result = "LEAKED" if C.canary(table) in text else "CLEAN"
                else:
                    result = "ERROR"
                cells[(name, table)] = (result, "" if result != "ERROR" else f"{status} {text[:120]}")
    finally:
        C.unmount(app, reader)
        C.release(app)
    _print(f"http-{mode}", cells)
    leaked = sorted(k for k, v in cells.items() if v[0] == "LEAKED")
    errors = sorted((k, v[1]) for k, v in cells.items() if v[0] == "ERROR")
    assert not errors, f"measurement failures (not refusals): {errors}"
    assert not leaked, f"{mode}: in-scope readers delivered private rows: {leaked}"
