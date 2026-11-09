# tests/unit/test_fixture_gate_settings_have_a_carrier.py
"""A fixture gate setting belongs to the gate program, never to the pytest parent.

`core/repository_executor_entry.py:run` builds the gate child's environment from
scratch -- `{"HOME": "/tmp", "PATH": ..., "PYTHONPATH": ...,
"PYTHONDONTWRITEBYTECODE": ...}` (line 385) plus `GATE_REPORT_DIR`,
`GODOT_BUILDER_URL` and the node keys -- and every `Popen(..., env=env)` uses
exactly that dict (line 544). A `monkeypatch.setenv(...)`, an
`os.environ[...] = ...` or an `os.environ.setdefault(...)` in the pytest
process therefore never reaches the gate program, which reads its settings with
`os.environ.get` (tests/gate_fixture.py:37-39) or, for a test-authored gate,
`os.environ["..."]` and silently keeps its default. Such a line is not a weak
carrier, it is no carrier at all -- and it reads as configuration to everyone
who comes later.

Only two shapes deliver a value to the gate child:

  * `embed_env(GATE_PY, NAME=value)` (tests/gate_fixture.py:315), which prefixes
    the program it writes with `_os.environ[NAME] = value`;
  * an `export NAME=value` inside the shell tail a launched gate runs from.

This row fails, naming file and line, when a parent-side setting of one of those
names appears anywhere under the test tree (resolved from this file, never from
the process's cwd).

The scan reads SYNTAX, not text: a gate program's own `os.environ[...]` read
lives inside a string literal, and is not a parent-side setting. The two
self-checks below pin that the matcher still recognises the dead shapes and both
carrier shapes, so an edit that turned it into a no-op cannot pass by finding
nothing.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# tests/ -- `parents[1]` of tests/unit/<this file>, resolved from the file so
# the scan does not depend on where pytest was started.
TESTS = Path(__file__).resolve().parents[1]

# The names the fixture gate programs read. Removing the last carrier of a name
# here reddens this row: the next step is to drop the read as well, not to let
# the value live only in a place the gate child cannot see.
SETTINGS = (
    "FIXTURE_GATE_CLIENT_TIMEOUT",   # tests/gate_fixture.py:37, default "60"
    "FIXTURE_GATE_NOISE",            # tests/gate_fixture.py:38, default "0"
    "FIXTURE_GATE_OP",               # tests/gate_fixture.py:39, default: basename of the repo
    "RED_GATE_MODE",                 # test_repo_gate_red_beats_absence.py:36, no default
)

# `embed_env` prefixes its program with `import os as _os`, so both spellings
# name the same mapping.
_OS_NAMES = ("os", "_os")

_EXPORT = re.compile(r"\bexport\s+(?P<name>[A-Za-z_]\w*)\s*=")


def _literal(node):
    """The string a Constant node holds, else None."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _environ_index(node):
    """`os.environ[NAME]` -> NAME's literal, else None."""
    if not isinstance(node, ast.Subscript) or not _is_environ(node.value):
        return None
    return _literal(node.slice)


def _assigned(target):
    """The sub-targets of one assignment target (tuple/list unpacking included)."""
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assigned(element)
    else:
        yield target


def _is_environ(node):
    """`os.environ`, in either spelling."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id in _OS_NAMES)


def _mapping_keys(node):
    """The literal string keys of a dict literal (`[]` when it is not one)."""
    if not isinstance(node, ast.Dict):
        return []
    return [key.value for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)]


def parent_side_writes(source, settings=SETTINGS):
    """Every `(kind, line, name)` that sets a gate setting on the pytest process."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attribute, receiver = node.func.attr, node.func.value
            name = _literal(node.args[0]) if node.args else None
            if (attribute == "setenv" and isinstance(receiver, ast.Name)
                    and "monkeypatch" in receiver.id and name in settings):
                found.append(("monkeypatch.setenv", node.lineno, name))
            elif attribute == "setdefault" and _is_environ(receiver) and name in settings:
                found.append(("os.environ.setdefault", node.lineno, name))
            elif attribute == "update" and _is_environ(receiver) and node.args:
                for key in _mapping_keys(node.args[0]):
                    if key in settings:
                        found.append(("os.environ.update({...})", node.lineno, key))
        elif isinstance(node, ast.AugAssign) and _is_environ(node.target):
            for key in _mapping_keys(node.value):
                if key in settings:
                    found.append(("os.environ |= {...}", node.target.lineno, key))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for leaf in _assigned(target):
                    name = _environ_index(leaf)
                    if name in settings:
                        found.append(("os.environ[...] = ...", leaf.lineno, name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            name = _environ_index(node.target)
            if name in settings:
                found.append(("os.environ[...] = ...", node.target.lineno, name))
    return found


def carrier_sites(source, settings=SETTINGS):
    """Every `(kind, line, name)` that delivers a gate setting INTO the child."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "embed_env":
                for keyword in node.keywords:
                    if keyword.arg in settings:
                        found.append(("embed_env", keyword.lineno, keyword.arg))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _EXPORT.finditer(node.value):
                if match.group("name") not in settings:
                    continue
                # A shell tail is text: number the line the export is written on.
                segment = ast.get_source_segment(source, node) or ""
                offset = segment.find(match.group(0))
                line = node.lineno + (segment[:offset].count("\n") if offset >= 0 else 0)
                found.append(("export", line, match.group("name")))
    return found


# A stand-in name, so this file never contributes a carrier (or a setting) of
# its own to the counts it reports.
SAMPLE = "SAMPLE_GATE_SETTING"

_SAMPLE_WITH_DEAD_SETTINGS = f"""\
def test_sample(monkeypatch):
    monkeypatch.setenv("{SAMPLE}", "1")
    os.environ["{SAMPLE}"] = "1"
    os.environ.setdefault("{SAMPLE}", "1")
    os.environ.update({{"{SAMPLE}": "1"}})
    os.environ |= {{"{SAMPLE}": "1"}}
"""

_SAMPLE_WITH_CARRIERS = f"""\
_TAIL = ("export {SAMPLE}=1\\n"
         "exec python3 - <<'PYGATE'\\n")
(repo / "gate.py").write_text(embed_env(GATE_PY, {SAMPLE}="1"))
"""


def test_a_fixture_gate_setting_is_carried_and_never_set_on_the_parent():
    sample = (SAMPLE,)
    dead_shapes = parent_side_writes(_SAMPLE_WITH_DEAD_SETTINGS, sample)
    assert len(dead_shapes) == 5, dead_shapes
    assert {kind for kind, _line, _name in dead_shapes} == {
        "monkeypatch.setenv", "os.environ[...] = ...", "os.environ.setdefault",
        "os.environ.update({...})", "os.environ |= {...}"}, dead_shapes
    carried_shapes = carrier_sites(_SAMPLE_WITH_CARRIERS, sample)
    assert len(carried_shapes) == 2, carried_shapes
    assert {kind for kind, _line, _name in carried_shapes} == {"embed_env", "export"}, \
        carried_shapes
    # The carrier shapes are not parent-side settings, and the dead shapes are
    # not carriers.
    assert parent_side_writes(_SAMPLE_WITH_CARRIERS, sample) == []
    assert carrier_sites(_SAMPLE_WITH_DEAD_SETTINGS, sample) == []

    files = sorted(path for path in TESTS.rglob("*.py") if path.is_file())
    dead, carriers = [], []
    for path in files:
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for kind, line, name in parent_side_writes(source):
            dead.append(f"{path.relative_to(TESTS.parent)}:{line}: "
                        f"{kind} of {name}: {lines[line - 1].strip()}")
        for kind, line, name in carrier_sites(source):
            carriers.append((kind, path.relative_to(TESTS.parent), line, name))
    print("GATE_SETTING_SCAN " + " ".join((
        f"files={len(files)}", f"carriers={len(carriers)}", f"dead={len(dead)}",
        "carried=" + ",".join(sorted({name for _kind, _path, _line, name in carriers})),
        "shapes=" + ",".join(sorted({kind for kind, _path, _line, _name in carriers})))))

    assert not dead, (
        "a fixture gate setting is set on the pytest parent, where the gate child "
        "cannot see it (core/repository_executor_entry.py:385 builds its env from "
        "scratch, :544 uses it for the Popen). Name file and line, then carry the "
        "value with embed_env(<gate>, NAME=value) or `export NAME=value` in the "
        "gate's shell tail:\n  " + "\n  ".join(dead))
    carried = {name for _kind, _path, _line, name in carriers}
    assert carried == set(SETTINGS), (
        f"no carrier delivers {sorted(set(SETTINGS) - carried)} to any gate program; "
        "the gate reads it and would silently use its own default")
    assert {kind for kind, _path, _line, _name in carriers} == {"embed_env", "export"}, (
        "both carrier shapes must still be found in the tree")
