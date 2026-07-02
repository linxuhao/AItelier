"""AItelier custom lint backends, registered into skillflow's lint tool.

Called once at startup (api/dependencies.py:get_tool_loader). After that,
a DPE architect can map extensions to these backends in
linter_manifest.json (e.g. {".js": "eslint"}) and skillflow's `lint`
tool dispatches here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _eslint_cmd() -> list[str] | None:
    """Resolve an eslint invocation, or None if no node toolchain exists."""
    if shutil.which("eslint"):
        return ["eslint"]
    if shutil.which("npx"):
        return ["npx", "--yes", "eslint@9"]
    return None


def eslint_backend(fp: Path) -> dict:
    """Syntax-level JS check: eslint with no config (parse errors only).

    Mirrors the strictness of the built-in ruff backend (E9-class only):
    with --no-config-lookup no style rules run, but parse errors still
    fail. cwd must be the file's directory — eslint ignores files outside
    its base path.
    """
    cmd = _eslint_cmd()
    if cmd is None:
        return {"file": str(fp), "passed": True,
                "error_message": "eslint unavailable (no node/npx) — skipped"}
    try:
        r = subprocess.run(
            cmd + ["--no-config-lookup", fp.name],
            cwd=fp.parent, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"file": str(fp), "passed": False,
                "error_message": f"eslint failed to run: {e}"}
    if r.returncode != 0:
        return {"file": str(fp), "passed": False,
                "error_message": (r.stdout.strip() or r.stderr.strip())}
    return {"file": str(fp), "passed": True, "error_message": ""}


def register_all() -> None:
    """Register all AItelier backends with skillflow's lint registry."""
    from skillflow.lint_backends import register_backend

    register_backend("eslint", eslint_backend)
