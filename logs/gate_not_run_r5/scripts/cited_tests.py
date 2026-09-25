"""Every test the protocol doc names, and whether the tree defines it.

usage: cited_tests.py <tree>

Reads `docs/repo-gate-unmeasured-protocol.md`, collects `<file>.py::<name>`
and the `::<name>` / `` `<name>` `` continuations after it in the same cell or
sentence, and looks each function up in the named file under tests/.
"""
import ast
import re
import sys
from pathlib import Path

tree = Path(sys.argv[1])
doc = (tree / "docs" / "repo-gate-unmeasured-protocol.md").read_text()
# Node ids of configs/coding_impl.yaml, which look like test names.
NODES = {"test_gate_absent", "test_evidence_missing", "test_evidence",
         "test_outcome", "test_gate_report_unattributable"}
files = {p.name: p for p in (tree / "tests").rglob("test_*.py")}
defs = {}
for name, path in files.items():
    defs[name] = {n.name for n in ast.walk(ast.parse(path.read_text()))
                  if isinstance(n, ast.FunctionDef)}

missing, found = [], 0
for chunk in re.split(r"[|;\n]", doc):
    current = None
    for m in re.finditer(r"([\w/]*?(test_\w+\.py))?::(\w+)|`(test_\w+)`", chunk):
        if m.group(2):
            current = m.group(2)
        name = m.group(3) or m.group(4)
        if name.endswith(".py") or not name.startswith("test_") or name in NODES:
            continue
        if current is None:
            if m.group(4):
                if not any(name in d for d in defs.values()):
                    missing.append(f"(any file)::{name}")
                else:
                    found += 1
            continue
        if current not in defs or name not in defs[current]:
            missing.append(f"{current}::{name}")
        else:
            found += 1
print(f"FOUND {found}")
for item in missing:
    print(f"MISSING {item}")
sys.exit(1 if missing else 0)
