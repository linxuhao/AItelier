"""Single authority for the AItelier data directory.

Every persistent path (databases, workspaces, projects, configs, locks,
debug log) derives from :func:`aitelier_home`. Production composition
(api/dependencies.py) and the CLI resolve paths HERE; nothing else may call
``Path.home()`` or hardcode ``.AItelier`` — tests/unit/test_datadir_guardrail.py
enforces it. Components (DBManager, WorkspaceManager) REQUIRE explicit paths,
so production data is unreachable by accident: a test that forgets to pass a
tmp path fails loudly instead of silently opening ~/.AItelier (which is how
the test suite once fought the live backend — the orphaned-claim storm).

Resolution is call-time (not import-time) so an env override set early in a
process — e.g. tests/conftest.py isolating AITELIER_HOME before app import —
is honored regardless of import order.
"""
from __future__ import annotations

import os
from pathlib import Path


def aitelier_home() -> Path:
    """The data root: $AITELIER_HOME, else the production ~/.AItelier."""
    return Path(os.getenv("AITELIER_HOME") or Path.home() / ".AItelier")


def db_path() -> str:
    return os.getenv("DPE_DB_PATH") or str(aitelier_home() / "aitelier.db")


def skillflow_db_path() -> str:
    return os.getenv("SKILLFLOW_DB_PATH") or str(aitelier_home() / "skillflow.db")


def workspaces_dir() -> Path:
    return Path(os.getenv("DPE_WS_PATH") or aitelier_home() / "workspaces")


def projects_dir() -> Path:
    return Path(os.getenv("DPE_PROJECTS_PATH") or aitelier_home() / "projects")


def configs_dir() -> Path:
    return aitelier_home() / "configs"


def tools_dir() -> Path:
    """Generated tools authored by pipeline_forge (persisted, boot-scanned)."""
    return aitelier_home() / "tools"


def meta_dir() -> Path:
    return aitelier_home() / "meta"


def scratch_dir() -> Path:
    return aitelier_home() / "scratch"


def orphan_log_path() -> Path:
    return aitelier_home() / "orphan_dbg.log"
