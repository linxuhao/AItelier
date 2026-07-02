"""read_test_written — read back a SCRATCH file written by test_write.

Reads from the same private per-step temp folder test_write uses
(~/.AItelier/scratch/<step>/). Returns {content} or {error}.
"""

import re
from pathlib import Path


def _scratch_dir(step_id: str, run_id: str) -> Path:
    import sys
    project_root = Path(__file__).parent.parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from core import datadir
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(step_id or run_id or "default"))[:80]
    d = datadir.scratch_dir() / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, filename: str) -> Path:
    p = (base / filename).resolve()
    base_r = base.resolve()
    if p != base_r and base_r not in p.parents:
        raise ValueError("scratch filename escapes the scratch directory")
    return p


def read_test_written(filename: str, *,
                      step_id: str = "", run_id: str = "", **kwargs) -> dict:
    try:
        base = _scratch_dir(step_id, run_id)
        path = _safe_path(base, filename)
        if not path.exists():
            return {"error": f"scratch file not found: {filename}"}
        return {"content": path.read_text(encoding="utf-8")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
