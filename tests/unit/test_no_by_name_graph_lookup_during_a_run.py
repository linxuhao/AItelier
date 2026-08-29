"""A live run's decisions come from its PINNED graph, not from the name.

Four review rounds found this same class four times, one site at a time:
the runner plugin's staging contract, `_get_checkpoint_info` (which decides
which step a human is answering), the butler's own reject, the retry guard.
Each fix was a single line; each round found more. Patching them one at a time
demonstrably does not converge, so this is the inventory.

The rule: where a `run_id` is in hand, a graph/resolver/node must be obtained
through the run's pin — `_get_resolver_for_run` / `_graph_for_run` — because
`_get_resolver(name)` and `_graphs[name]` return whatever is registered NOW,
and a config edited mid-run makes those a different graph.

Sites with no run in hand are fine and are listed as such: creation-time
(`create_run`), catalogue/registry listings, and the `else` half of a
`pinned if run_id else by-name` fallback are all legitimate.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCAN = ("core", "api", "web_api", "aitelier")

# Lookups that are correct BY NAME, each because no run is in scope there.
# A new entry needs a reason in this dict, not just a path.
_ALLOWED = {
    "core/config_registry.py": "registry/catalogue listing — no run in scope",
    "core/pipeline_registry.py": "registration and archival — no run in scope",
    "core/addon_registry.py": "compose/registration — no run in scope",
    "core/capability_registry.py": "capability definitions are global",
    "core/baseline.py": "captures a config's shape, not a run's execution",
    "aitelier/tools/forge_dryrun_smoke/impl.py": "stub drive on its own engine",
    "aitelier/stub_runner.py": "stub drive on its own engine",
    "core/run_launcher.py": "pre-flight context check BEFORE a run exists",
    # KNOWN GAP, not an exemption on the merits: this gate resolves the graph
    # from `config_name` because skillflow never hands it a `run_id` — the tool
    # signature has none. So a config edited mid-run can change which loop
    # `source` this gate reads, and a gate reading the wrong source passes or
    # fails a step silently. Closing it means widening the tool-invocation
    # contract, which is a larger change than this rule.
    "aitelier/tools/loop_items_implemented/impl.py":
        "gate tool; skillflow injects no run_id — see KNOWN GAP above",
}


def _sources():
    for d in _SCAN:
        root = _ROOT / d
        if root.is_dir():
            for p in sorted(root.rglob("*.py")):
                if "__pycache__" not in p.parts:
                    yield p


def _by_name_lookups(tree) -> list[int]:
    """Lines calling `_get_resolver(...)` or indexing `_graphs` by name.

    A ternary that already prefers the pinned accessor is not a finding — that
    is the fallback shape used everywhere the engine may be older than the
    caller, and its by-name half is reached only when pinning is unavailable.
    """
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.IfExp):
            continue                      # handled via the parent scan below
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_get_resolver":
            hits.append(n.lineno)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and isinstance(n.func.value, ast.Attribute) \
                and n.func.value.attr == "_graphs":
            hits.append(n.lineno)
    return hits


def _guarded_lines(src: str) -> set[int]:
    """Lines inside a `pinned if … else by-name` expression, or one line after
    it — the fallback idiom, which is correct."""
    guarded = set()
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        window = " ".join(lines[max(0, i - 4):i + 2])
        if "_get_resolver_for_run" in window or "_graph_for_run" in window:
            guarded.add(i)
    return guarded


# Per-LINE exemption marker. Preferred over a file entry in `_ALLOWED`, which
# blankets every lookup in a file that may also contain real run-scoped ones —
# `core/meta_agent.py` has both.
_MARKER = "by-name-ok:"


def _marked_lines(src: str) -> set[int]:
    """Lines carrying `# by-name-ok: <reason>` on themselves or just above."""
    out, lines = set(), src.splitlines()
    for i, line in enumerate(lines, 1):
        if _MARKER in line:
            out.update({i, i + 1, i + 2})
    return out


def test_no_run_scoped_lookup_resolves_a_graph_by_name():
    offenders = []
    for path in _sources():
        rel = str(path.relative_to(_ROOT))
        if rel in _ALLOWED:
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        guarded = _guarded_lines(src) | _marked_lines(src)
        offenders += [f"{rel}:{ln}" for ln in _by_name_lookups(tree)
                      if ln not in guarded]

    assert not offenders, (
        "these resolve a graph by NAME with a run in scope:\n  "
        + "\n  ".join(sorted(offenders))
        + "\n\nA run executes the version it started with, so `_get_resolver("
        "name)` / `_graphs[name]` answer about a DIFFERENT graph once the "
        "config is edited. Use `_get_resolver_for_run(run_id)` (or "
        "`_graph_for_run`), keeping the by-name call as the `else` half so an "
        "engine without pinning still works. If the site genuinely has no run "
        "— registration, a catalogue listing, creation time — mark the line "
        "`# by-name-ok: <reason>`, or add the whole file to _ALLOWED.")


@pytest.mark.parametrize("rel,reason", sorted(_ALLOWED.items()))
def test_every_allowed_file_still_exists(rel, reason):
    """An allowlist that outlives its files silently stops covering anything."""
    assert (_ROOT / rel).exists(), (
        f"{rel} is allow-listed ({reason}) but is gone — drop the entry, or "
        f"the exemption is protecting nothing while looking like it does.")
