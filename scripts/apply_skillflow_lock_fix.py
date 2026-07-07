#!/usr/bin/env python3
"""Apply the skillflow _get_project_id lock fix to the target source file.

Usage:
    python scripts/apply_skillflow_lock_fix.py

This script reads the skillflow core.py source, adds the missing `with self._lock:`
around the `self._conn.execute()` call in `_get_project_id`, and writes it back.
The fix serializes SQLite connection access, preventing sqlite3.InterfaceError
under concurrent access via run_in_threadpool.
"""

import re
import sys
from pathlib import Path


# The probable locations of the skillflow source, in priority order.
# The editable install path from CLAUDE.md: ~/stepflow/ or ~/skillflow/
_HOME = Path.home()
CANDIDATE_PATHS = [
    Path("/home/linxuhao/.AItelier/projects/skillflow-review/src/skillflow/core.py"),
    _HOME / "stepflow" / "src" / "skillflow" / "core.py",
    _HOME / "skillflow" / "src" / "skillflow" / "core.py",
    _HOME / ".AItelier" / "projects" / "skillflow-review" / "src" / "skillflow" / "core.py",
]


def find_target() -> Path:
    """Locate the skillflow core.py file."""
    for p in CANDIDATE_PATHS:
        if p.exists():
            return p
    # Try pip show skillflow-py
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "skillflow-py"],
            capture_output=True, text=True, check=True
        )
        for line in result.stdout.splitlines():
            if line.startswith("Location:"):
                loc = line.split(":", 1)[1].strip()
                p = Path(loc) / "skillflow" / "core.py"
                if p.exists():
                    return p
    except subprocess.CalledProcessError:
        pass
    raise FileNotFoundError(
        "Could not find skillflow/core.py. "
        "Try setting the path manually: python apply_skillflow_lock_fix.py /path/to/core.py"
    )


def apply_fix(content: str) -> str:
    """Apply the lock fix to the _get_project_id method.

    Before:
        def _get_project_id(self, run_id: str) -> str:
            row = self._conn.execute(
                "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return row["project_id"] if row else ""

    After:
        def _get_project_id(self, run_id: str) -> str:
            with self._lock:
                row = self._conn.execute(
                    "SELECT project_id FROM skillflow_runs WHERE id = ?", (run_id,)
                ).fetchone()
            return row["project_id"] if row else ""
    """
    # Pattern: _get_project_id method body where execute() is NOT wrapped in with self._lock
    pattern = r'(def _get_project_id\(self, run_id: str\) -> str:\s+)(row = self\._conn\.execute\()'
    replacement = r'\1with self._lock:\n            \2'

    new_content, count = re.subn(pattern, replacement, content, count=1)
    if count == 0:
        # Check if already fixed
        if "with self._lock:" in content and "_get_project_id" in content:
            # Verify the fix is already present
            if re.search(r'def _get_project_id.*with self\._lock:', content, re.DOTALL):
                print("✓ Lock fix already applied — no changes needed.")
                return content
        raise ValueError(
            "Could not find the pattern to fix. The method signature or "
            "indentation may have changed. Expected:\n"
            "    def _get_project_id(self, run_id: str) -> str:\n"
            "        row = self._conn.execute("
        )
    return new_content


def main():
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        if not target.exists():
            print(f"Error: {target} does not exist.", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            target = find_target()
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    print(f"Target: {target}")
    original = target.read_text(encoding="utf-8")

    # Quick check: is this the right file?
    if "class SkillFlow" not in original:
        print(f"Error: {target} does not look like skillflow/core.py (no SkillFlow class).",
              file=sys.stderr)
        sys.exit(1)

    # Back up
    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
        print(f"Backup: {backup}")

    # Apply fix
    new_content = apply_fix(original)
    target.write_text(new_content, encoding="utf-8")
    print(f"✓ Fix applied to {target}")

    # Verify
    verify = target.read_text(encoding="utf-8")
    if "with self._lock:" in verify and "row = self._conn.execute(" in verify:
        print("✓ Verification passed: lock is present in the method.")
    else:
        print("⚠ Warning: verification could not confirm the fix. Check manually.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
