# tests/unit/test_datadir_guardrail.py
# Guardrails for the data-directory authority (core/datadir.py).
#
# History: DBManager/WorkspaceManager once DEFAULTED to ~/.AItelier, and
# server code hardcoded Path.home() paths in a dozen places — so the test
# suite silently opened the production DBs, and every TestClient lifespan
# fought the live backend (the orphaned-claim storm, 2026-07-02). These
# tests keep that class of accident from creeping back.

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Server-side code that must resolve paths via core.datadir only.
SCANNED_DIRS = ["core", "api", "aitelier", "models", "web_api"]

# The one module allowed to know where the data dir lives — plus
# asset_registry, whose ~/.local/share/aitelier_tools is the SYSTEM tools
# registry, deliberately outside the per-deployment data dir.
ALLOWED = {"core/datadir.py", "core/asset_registry.py"}

# Dangerous primitives: reaching for the user's home from server code.
_HOME_PATTERN = re.compile(r"Path\.home\(\)|expanduser\(\s*['\"]~")


def _code_lines(path: Path):
    """Yield (lineno, line) with #-comments stripped (docstrings still scanned:
    a path built in a docstring is harmless, but Path.home() rarely appears
    in prose — precision over recall is fine for a tripwire)."""
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0]
        if line.strip():
            yield i, line


def test_no_home_paths_outside_datadir():
    offenders = []
    for d in SCANNED_DIRS:
        base = REPO_ROOT / d
        if not base.is_dir():
            continue
        for f in base.rglob("*.py"):
            rel = f.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWED:
                continue
            for lineno, line in _code_lines(f):
                if _HOME_PATTERN.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Home-relative paths outside core/datadir.py — route them through "
        "the datadir authority so tests stay isolated from production data:\n"
        + "\n".join(offenders)
    )


def test_dbmanager_requires_explicit_path():
    from core.db_manager import DBManager
    with pytest.raises(TypeError):
        DBManager()  # no default — prod paths are composed, never implied
    with pytest.raises(ValueError):
        DBManager("")


def test_workspacemanager_requires_explicit_path():
    from core.workspace_manager import WorkspaceManager
    with pytest.raises(TypeError):
        WorkspaceManager()
    with pytest.raises(ValueError):
        WorkspaceManager("")


def test_datadir_honors_env_isolation(tmp_path, monkeypatch):
    """AITELIER_HOME redirects every derived path (call-time resolution)."""
    from core import datadir
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    monkeypatch.delenv("DPE_DB_PATH", raising=False)
    monkeypatch.delenv("SKILLFLOW_DB_PATH", raising=False)
    monkeypatch.delenv("DPE_WS_PATH", raising=False)
    monkeypatch.delenv("DPE_PROJECTS_PATH", raising=False)
    assert datadir.aitelier_home() == tmp_path
    for p in (datadir.db_path(), datadir.skillflow_db_path()):
        assert str(tmp_path) in p
    for p in (datadir.workspaces_dir(), datadir.projects_dir(),
              datadir.configs_dir(), datadir.meta_dir(),
              datadir.scratch_dir(), datadir.orphan_log_path()):
        assert str(p).startswith(str(tmp_path))


def test_get_db_manager_binds_to_datadir(tmp_path, monkeypatch):
    """The CLI's host-side accessor composes via the datadir authority —
    so pytest's AITELIER_HOME isolation applies to it too. (cli/tui/chat.py
    imported this for months while it didn't exist; the ImportError was
    swallowed by best-effort except blocks.)"""
    import core.db_manager as dbm
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    monkeypatch.delenv("DPE_DB_PATH", raising=False)
    monkeypatch.setattr(dbm, "_default_instance", None)
    db = dbm.get_db_manager()
    assert str(tmp_path) in db.db_path
    assert dbm.get_db_manager() is db  # process-wide singleton
