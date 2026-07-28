"""The gate's rules and the emitter's grounding must not drift apart.

A rule that the gate enforces but the palette never states costs one guaranteed
rework round on EVERY generation, forever. That is not hypothetical: the
write-mode `validation` rule shipped as a check while `forge_palette` said only
"validation gates promotion" in passing, and every subsequent generation burned a
round rediscovering it from the violation message.

The fix is one table (`forge_registry_check.RULES`) with two consumers — the gate
runs the checks, `forge_palette` renders `teaches`. These tests are the binding:
adding a check without a rule entry, or a rule the palette never shows, fails here
rather than in a live 40-minute run.
"""
import inspect
import re

import pytest

from aitelier.tools.forge_palette.impl import forge_palette
from aitelier.tools.forge_registry_check.impl import RULES, forge_registry_check

RULE_IDS = {r.id for r in RULES}


class TestTheTableIsWellFormed:
    def test_ids_are_unique(self):
        assert len(RULE_IDS) == len(RULES)

    def test_every_rule_teaches_something_actionable(self):
        for r in RULES:
            assert r.teaches.strip(), r.id
            # A one-liner that just restates the id teaches nothing. The emitter
            # needs the failure mode, not the rule's name.
            assert len(r.teaches) > 60, f"{r.id}: too thin to act on"


class TestEveryCheckThatRunsIsTaught:
    """The binding that actually holds: parse the checks the gate INVOKES."""

    def test_each_extended_check_has_a_rule(self):
        src = inspect.getsource(forge_registry_check)
        invoked = set(re.findall(r"violations\.extend\(_([a-z_]+)\(", src))
        assert invoked, "no checks found — the parse pattern went stale"
        missing = invoked - RULE_IDS
        assert not missing, (
            f"these checks run but are never taught to the emitter: {sorted(missing)}. "
            f"Add a Rule(...) for each in forge_registry_check.RULES — otherwise every "
            f"generation pays a rework round learning it from the violation message.")

    def test_the_inline_checks_are_covered_too(self):
        """The main loop's inline checks have no function to parse, so they are
        listed here explicitly. A new inline check must be added in both places."""
        inline = {
            "tool_exists", "role_defined", "counter_smell", "reviewer_is_agent",
            "reviewer_reads_maker", "context_refs_resolve", "scope_declaration",
            "completed_terminal_is_gate",
        }
        assert inline <= RULE_IDS


class TestThePaletteRendersThem:
    def test_every_rule_reaches_the_emitter(self):
        md = forge_palette(include_signatures=False)["palette_markdown"]
        for r in RULES:
            assert r.id in md, f"rule {r.id!r} is enforced but never shown to the emitter"

    def test_the_write_validation_rule_is_stated_in_full(self):
        """Z3's regression test. This is the rule whose absence was measured: it
        fired in all three pipelines of one round, each costing a rework hop."""
        md = forge_palette(include_signatures=False)["palette_markdown"]
        assert "mode: write" in md and "must declare a `validation`" in md
        assert "file_exists" in md          # the remediation snippet, not just the rule

    def test_teaching_only_rules_are_marked_as_such(self):
        md = forge_palette(include_signatures=False)["palette_markdown"]
        soft = [r for r in RULES if not r.enforced]
        assert soft, "expected at least one teaching-only rule (step id legibility)"
        assert "not auto-checked" in md

    def test_the_deleted_duplicates_did_not_take_their_content_with_them(self):
        """Six gotcha bullets were removed from the hand-written cheatsheet once
        RULES rendered them. Their detail had to move INTO `teaches`, not vanish."""
        md = forge_palette(include_signatures=False)["palette_markdown"]
        for phrase in (
            "allow_full_write",              # write vocabulary
            "UNIQUE constraint",             # duplicate max_loop
            "no matching transition",        # unreachable terminals
            "give-up branch",                # deliverable on the success path
            "wearing a branch's clothes",    # failure rejoins success
            "create_file",                   # the tools that do not exist
        ):
            assert phrase in md, f"lost when the duplicate bullet was deleted: {phrase!r}"


def test_gotchas_section_no_longer_claims_the_gates_miss_gate_checked_rules():
    """The section was titled "the 3 gates will NOT catch these" while listing six
    things the gate now rejects. A false claim there teaches the emitter to ignore
    the gate."""
    from aitelier.tools.forge_palette.impl import CHEATSHEET
    assert "gates will NOT catch" not in CHEATSHEET
    for gate_checked in ("max_loop` EDGE PER", "DELIVERABLE GOES ON",
                         "WRITE TOOLS ARE INJECTED", "EVERY TERMINAL NEEDS"):
        assert gate_checked not in CHEATSHEET, (
            f"{gate_checked!r} is duplicated in the hand-written cheatsheet AND the "
            f"rule table — that is the drift this table exists to prevent")
