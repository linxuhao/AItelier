"""One rollback boundary for graph, role, capability, manifest, DB and file writes."""
from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path


_MISSING = object()


def _capture(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return ("symlink", os.readlink(path), path.lstat().st_mode)
    if path.is_file():
        return ("file", path.read_bytes(), path.stat().st_mode)
    entries = {}
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path)
        if item.is_symlink():
            entries[rel] = ("symlink", os.readlink(item), item.lstat().st_mode)
        elif item.is_file():
            entries[rel] = ("file", item.read_bytes(), item.stat().st_mode)
        else:
            entries[rel] = ("dir", None, item.stat().st_mode)
    return ("dir", entries, path.stat().st_mode)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _restore(path: Path, state) -> None:
    _remove(path)
    if state is None:
        return
    kind, value, mode = state
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        path.write_bytes(value)
        path.chmod(mode)
        return
    if kind == "symlink":
        path.symlink_to(value)
        return
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    for rel, (child_kind, child_value, child_mode) in sorted(
            value.items(), key=lambda item: (len(item[0].parts), str(item[0]))):
        child = path / rel
        if child_kind == "dir":
            child.mkdir(parents=True, exist_ok=True)
            child.chmod(child_mode)
        elif child_kind == "file":
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_bytes(child_value)
            child.chmod(child_mode)
        else:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.symlink_to(child_value)


class RegistrationTransaction:
    """Rollback unless ``commit`` is called; SkillFlow's lock is re-entrant."""

    def __init__(self, sf, registry=None, *, config_names=(), paths=()):
        self.sf = sf
        self.registry = registry
        self.config_names = tuple(dict.fromkeys(config_names))
        raw_paths = [Path(path) for path in paths]
        self.paths = tuple(path for path in raw_paths if not any(
            path != other and other in path.parents for other in raw_paths))
        self._committed = False
        self._rolled_back = False

    def __enter__(self):
        self.sf._lock.acquire()
        try:
            agents = self.sf.agent_registry._configs
            self._agent_mapping = agents
            self._agent_objects = dict(agents)
            self._agent_values = copy.deepcopy(agents)
            self._capability_mapping = getattr(self.sf, "_capabilities", {})
            self._capabilities = dict(self._capability_mapping)
            # by-name-ok: registration rollback snapshots definitions, no run
            self._graphs = {
                name: self.sf._graphs.get(name, _MISSING)
                for name in self.config_names}
            # by-name-ok: registration rollback snapshots definitions, no run
            self._resolvers = {
                name: self.sf._resolvers.get(name, _MISSING)
                for name in self.config_names}
            manifests = getattr(self.registry, "_manifests", None)
            self._manifests = ({
                name: manifests.get(name, _MISSING) for name in self.config_names}
                if isinstance(manifests, dict) else None)
            self._rows = {}
            for name in self.config_names:
                self._rows[name] = {
                    table: [dict(row) for row in self.sf._conn.execute(
                        f"SELECT * FROM {table} WHERE name=?", (name,))]
                    for table in ("skillflow_graphs", "skillflow_graph_versions")
                }
            self._files = {path: _capture(path) for path in self.paths}
            return self
        except Exception:
            # __exit__ is not called when __enter__ raises.
            self.sf._lock.release()
            raise

    def commit(self):
        self._committed = True

    def rollback(self):
        if self._rolled_back:
            return
        for name, original in self._agent_objects.items():
            saved = self._agent_values[name]
            original.name = saved.name
            original.model = saved.model
            original.tools = saved.tools
            original.config = saved.config
            original.tool_schemas = saved.tool_schemas
            original.unknown_tools = saved.unknown_tools
        self._agent_mapping.clear()
        self._agent_mapping.update(self._agent_objects)
        self.sf.agent_registry._configs = self._agent_mapping
        self._capability_mapping.clear()
        self._capability_mapping.update(self._capabilities)
        self.sf._capabilities = self._capability_mapping
        for target, saved in (
                (self.sf._graphs, self._graphs),
                (self.sf._resolvers, self._resolvers)):
            for name, value in saved.items():
                if value is _MISSING:
                    target.pop(name, None)
                else:
                    target[name] = value
        manifests = getattr(self.registry, "_manifests", None)
        if isinstance(manifests, dict) and self._manifests is not None:
            for name, value in self._manifests.items():
                if value is _MISSING:
                    manifests.pop(name, None)
                else:
                    manifests[name] = value
        failure = None
        try:
            self.sf._conn.execute("BEGIN IMMEDIATE")
            for name, tables in self._rows.items():
                for table, rows in tables.items():
                    self.sf._conn.execute(
                        f"DELETE FROM {table} WHERE name=?", (name,))
                    for row in rows:
                        columns = tuple(row)
                        placeholders = ", ".join("?" for _ in columns)
                        self.sf._conn.execute(
                            f"INSERT INTO {table} ({', '.join(columns)}) "
                            f"VALUES ({placeholders})",
                            tuple(row[column] for column in columns))
            self.sf._conn.commit()
        except Exception as exc:
            self.sf._conn.rollback()
            failure = exc
        for path, state in self._files.items():
            try:
                _restore(path, state)
            except Exception as exc:
                failure = failure or exc
        self._rolled_back = True
        if failure is not None:
            raise failure

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None or not self._committed:
                self.rollback()
        finally:
            self.sf._lock.release()
        return False
