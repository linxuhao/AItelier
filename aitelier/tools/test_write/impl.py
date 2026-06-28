"""test_write — write a throwaway SCRATCH file to a private per-step temp folder.

Gives agents a place to write scratch/intermediate files WITHOUT polluting the
project repo (agents otherwise drop junk like `_test_write.txt` into the repo,
which then gets committed). Scratch lives under ~/.AItelier/scratch/<step>/ and
is never promoted or committed. Read back with read_test_written.
"""

import re
from pathlib import Path


def _scratch_dir(step_id: str, run_id: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(step_id or run_id or "default"))[:80]
    d = Path.home() / ".AItelier" / "scratch" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path(base: Path, filename: str) -> Path:
    p = (base / filename).resolve()
    base_r = base.resolve()
    if p != base_r and base_r not in p.parents:
        raise ValueError("scratch filename escapes the scratch directory")
    return p


def test_write(filename: str, content: str = "", *,
               step_id: str = "", run_id: str = "", **kwargs) -> dict:
    try:
        base = _scratch_dir(step_id, run_id)
        path = _safe_path(base, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        # NOTE: deliberately NOT a "written" key — scratch files must not be
        # counted as step deliverables by the agent loop.
        return {"scratch_written": filename, "ok": True}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
