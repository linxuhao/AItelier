"""The mutation catalog's structural integrity. Anchor validation against
product source lives in run_mutations.py (the runner), not here — that way a
mutation that changes its own anchor text cannot produce a false 'kill' via
this test going red. What remains: the catalog file must parse and the goal
table entries must be present."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "tools" / "mutation_catalog"))
from mutations import MUTATIONS  # noqa: E402


def test_the_catalog_parses_and_each_entry_has_required_keys():
    """The catalog must load, every entry must have 'edits' and 'targeted',
    and every edit must have 'file', 'anchor', 'replacement'.  This checks
    file-level syntax, NOT product-source anchor hits (the runner does that)."""
    assert isinstance(MUTATIONS, dict)
    for name, spec in sorted(MUTATIONS.items()):
        assert "edits" in spec, f"{name}: missing 'edits'"
        assert "targeted" in spec, f"{name}: missing 'targeted'"
        assert isinstance(spec["edits"], list) and len(spec["edits"]) > 0
        for edit in spec["edits"]:
            for key in ("file", "anchor", "replacement"):
                assert key in edit, f"{name}: edit missing '{key}'"


def test_the_goal_table_mutations_are_all_present():
    for name in ("N9", "M21", "M21b", "G2", "G2b", "DUPIMPL", "RESTART"):
        assert name in MUTATIONS, name
