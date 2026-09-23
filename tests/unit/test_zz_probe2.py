import difflib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _show(rev, path):
    out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return f"MISSING {rev}:{path}"
    old = out.stdout
    new = (ROOT / path).read_text(encoding="utf-8")
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                               f"{rev}:{path}", f"HEAD:{path}", n=2)
    return "".join(diff)


def test_probe2():
    parts = []
    for rev in ("3b3d6560", "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722"):
        for path in ("core/output_migration.py", "templates/fix_tests.md",
                     "core/dpe_pipeline.py"):
            parts.append(f"=== {rev} {path} ===")
            parts.append(_show(rev, path))
    raise AssertionError("\n".join(parts))
