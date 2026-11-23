"""Copy-on-write State databases for test fixtures.

Building State schema on a fresh disk-backed SQLite file costs 1.7-2.8 s under
suite load (about 170 DDL statements, each autocommit and each last close an
fsync in WAL mode), while a test body usually takes milliseconds. A fixture that
needs a fresh State world instead asks for a COPY of a template that a recipe
built once per session (per xdist worker), then runs its own constructors on the
copy as before; on an existing schema they are no-ops apart from the checks.

Each test still gets its own file, never a shared live database.

A recipe makes exactly the schema its fixture's constructors make, in the same
order, and no rows. Product code branches on whether a table exists (for
example, whether `drivers` is there), so one universal template holding every
table would change what a test exercises. Tests whose subject is schema
creation or migration keep building fresh files and do not use this.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

_PEPPER = "state-template-pepper-not-a-secret-" * 2


def state(path):
    """StateDatabase + trusted StateService."""
    from core.state_database import StateDatabase
    from core.state_service import StateService
    StateService(StateDatabase(path), actor="template", project_read_trusted=True)


def state_registry(path):
    """StateDatabase + trusted StateService, then DriverRegistry."""
    from core.drivers import DriverRegistry
    from core.state_database import StateDatabase
    from core.state_service import StateService
    db = StateDatabase(path)
    StateService(db, actor="template", project_read_trusted=True)
    DriverRegistry(db, _PEPPER)


def state_registry_dsh(path):
    """StateDatabase + DriverRegistry + trusted StateService + DshDrivers tables."""
    from core.drivers import DriverRegistry
    from core.dsh_driver import DshDrivers
    from core.state_database import StateDatabase
    from core.state_service import StateService
    db = StateDatabase(path)
    registry = DriverRegistry(db, _PEPPER)
    StateService(db, actor="template", project_read_trusted=True)
    DshDrivers(db, registry, Path(path).parent / "dsh-root")


def dbm(path):
    """DBManager only (the legacy host tables)."""
    from core.db_manager import DBManager
    DBManager(path)


def dbm_graph(path):
    """DBManager + trusted StateGraphStore (graph + metadata tables)."""
    from core.db_manager import DBManager
    from core.state_graph import StateGraphStore
    StateGraphStore(DBManager(path), project_read_trusted=True)


def dbm_graph_attempts(path):
    """DBManager + trusted StateGraphStore + StateAttempts."""
    from core.db_manager import DBManager
    from core.state_attempts import StateAttempts
    from core.state_graph import StateGraphStore
    StateAttempts(StateGraphStore(DBManager(path), project_read_trusted=True))


def dbm_state(path):
    """DBManager + trusted StateService."""
    from core.db_manager import DBManager
    from core.state_service import StateService
    StateService(DBManager(path), actor="template", project_read_trusted=True)


class TemplateCache:
    """Builds each recipe's template once, hands out private copies."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.built: dict = {}

    def template(self, recipe) -> Path:
        path = self.built.get(recipe)
        if path is None:
            folder = self.root / recipe.__name__
            folder.mkdir()
            path = folder / "template.sqlite"
            recipe(str(path))
            conn = sqlite3.connect(path)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            for suffix in ("-wal", "-shm"):
                leftover = Path(str(path) + suffix)
                assert not leftover.exists() or leftover.stat().st_size == 0, leftover
            self.built[recipe] = path
        return path

    def copy(self, recipe, dest) -> str:
        """Copy `recipe`'s template to `dest`; return it as str.

        An existing `dest` is left alone, as a constructor leaves an existing
        database alone: two fixtures of one test may name the same file (the
        conftest `db_manager` and `client` both open `tmp_path/test.db`).
        """
        dest = Path(dest)
        if dest.exists():
            return str(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        template = self.template(recipe)
        shutil.copyfile(template, dest)
        # The mode the constructor gave a new file (StateDatabase: 0600).
        shutil.copymode(template, dest)
        return str(dest)
