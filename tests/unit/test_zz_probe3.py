import ast
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
R2 = "3b3d6560"
R3 = "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722"


def _show(rev, path):
    out = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _lines(text, needle, pad=4):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return [f"{j+1}: {lines[j]!r}"
                    for j in range(max(0, i - pad), min(len(lines), i + pad))]
    return [f"NO {needle!r}"]


def test_probe3():
    parts = []
    zh_r2 = _show(R2, "core/output_migration.py")
    zh_r3 = _show(R3, "core/output_migration.py")
    cur = (ROOT / "core/output_migration.py").read_text(encoding="utf-8")
    parts.append("== r2 ZH: 原样贴回来 ==")
    parts += _lines(zh_r2, "原样贴回来", 6)
    parts.append("== r2 ZH: 两种模式都先整批 ==")
    parts += _lines(zh_r2, "两种模式都先整批", 4)
    parts.append("== clean ZH: 原样贴回来 ==")
    parts += _lines(cur, "原样贴回来", 4)
    parts.append("== r3 ZH: 连同被引用 ==")
    parts += _lines(zh_r3, "连同被引用", 4)
    parts.append("== r3 ZH: 原样贴回来 ==")
    parts += _lines(zh_r3, "原样贴回来", 3)
    parts.append("== joinedstr constants in core ==")
    for py in sorted((ROOT / "core").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if isinstance(node.value, ast.JoinedStr):
                names = [t.id for t in (node.targets if isinstance(node, ast.Assign)
                                        else [node.target]) if isinstance(t, ast.Name)]
                parts.append(f"{py.name}: {names} joinedstr")
    parts.append("== configs/coding_task.yaml system_prompt ==")
    for rel in ("configs/coding_task.yaml", "agent_configs/coding_task.yaml"):
        doc = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        found = []
        def walk(v, path=""):
            if isinstance(v, dict):
                for k, c in v.items():
                    if k == "system_prompt" and isinstance(c, str):
                        found.append((path, len(c)))
                    walk(c, f"{path}/{k}")
            elif isinstance(v, list):
                for i, c in enumerate(v):
                    walk(c, f"{path}[{i}]")
        walk(doc)
        parts.append(f"{rel}: {found}")
    raise AssertionError("\n".join(parts))
