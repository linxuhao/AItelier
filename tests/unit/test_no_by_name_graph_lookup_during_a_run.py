"""A live run's decisions come from its PINNED graph, not from the name.

Five review rounds found this same class, one site at a time: the runner
plugin's staging contract, `_get_checkpoint_info` (which decides which step a
human is answering), the butler's own reject, the retry guard, the baseline
recorded after a drive. Each fix was a line; each round found more. Patching
one at a time does not converge, so this is the inventory.

The rule: `_get_resolver(name)`, `_graphs[name]` and `_resolvers[name]` return
whatever is registered NOW. A run executes the version it started with, so
where a `run_id` is in hand they answer about a different graph. Use
`_get_resolver_for_run` / `_graph_for_run`.

Every exception is a `# by-name-ok: <reason>` on the line, or the line above.
ONE mechanism on purpose. This started with a file-level allowlist as well, and
that is what let a real bug through: `core/baseline.py` was waived as "shape,
not execution", which was true of the file when it was written and false of the
line added to it later. A file-level exemption blankets code that does not
exist yet.
"""

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCAN = ("core", "api", "web_api", "aitelier")
_MARKER = "by-name-ok:"


def _by_name_lookups(tree) -> list[int]:
    """Lines that reach a graph by name, in all three syntactic forms.

    Three, because the first version detected only the first: the subscript was
    named in this file's own rule and never checked, and `getattr(sf,
    "_graphs", {})` — the form `core/baseline.py` actually uses — was invisible,
    so the file whose exemption was hiding a bug could not have been caught even
    without it.
    """
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "_get_resolver":
                hits.append(n.lineno)
            elif (n.func.attr == "get"
                  and isinstance(n.func.value, ast.Attribute)
                  and n.func.value.attr in ("_graphs", "_resolvers")):
                hits.append(n.lineno)
        elif (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute)
              and n.value.attr in ("_graphs", "_resolvers")):
            hits.append(n.lineno)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "getattr" and len(n.args) >= 2
              and isinstance(n.args[1], ast.Constant)
              and n.args[1].value in ("_graphs", "_resolvers")):
            hits.append(n.lineno)
    return hits


def _marked(src: str) -> set[int]:
    """Lines exempted by a marker on themselves or the line directly above.

    One line of reach, not three: a wider reach meant one marker covered a
    neighbouring real lookup with a reason written for something else.
    """
    out, lines = set(), src.splitlines()
    for i, line in enumerate(lines, 1):
        if _MARKER in line:
            out.update({i, i + 1})
    return out


def test_no_run_scoped_lookup_resolves_a_graph_by_name():
    offenders = []
    for d in _SCAN:
        for path in sorted((_ROOT / d).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            marked = _marked(src)
            offenders += [f"{path.relative_to(_ROOT)}:{ln}"
                          for ln in _by_name_lookups(tree) if ln not in marked]

    assert not offenders, (
        "these reach a graph by NAME:\n  " + "\n  ".join(sorted(offenders))
        + "\n\nA run executes the version it started with, so these answer "
        "about a DIFFERENT graph once the config is edited. Use "
        "`_get_resolver_for_run(run_id)` or `_graph_for_run(run_id)`. If the "
        "site genuinely has no run — registration, a catalogue listing, "
        "creation time, an addon — put `# by-name-ok: <reason>` on the line "
        "above it.")
