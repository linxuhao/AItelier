import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
R2 = "3b3d6560"
R3 = "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722"


def _git(rev, path):
    out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _block(rev, which):
    text = _git(rev, "core/output_migration.py")
    m = re.search(rf'STRICT_PATCH_GUIDANCE_{which} = """(.*?)"""', text, re.S)
    assert m, (rev, which)
    return m.group(1)


def test_probe5():
    parts = []
    for tag, body in (("R2_EN", _block(R2, "EN")),
                      ("R2_ZH", _block(R2, "ZH")),
                      ("R3_ZH", _block(R3, "ZH")),
                      ("R2_FIXTESTS", _git(R2, "templates/fix_tests.md"))):
        parts.append(f"<<<{tag}>>>")
        parts.append(body)
        parts.append(f"<<<END {tag}>>>")
    raise AssertionError("\n".join(parts))
