"""state_graph_read's advertised actions ARE core.state_commands.READ_REQUESTS.

The description is what a client reads BEFORE it can call anything, so a name it
omits is an action nobody can discover and a name it invents is a call that
cannot work. The two drifted — `get_receipt`, `project_catalog`,
`project_overview`, `project_run_summary`, `project_attempts`, `references`,
`run_owners`, `attempt_detail`, `design_catalog`, `get_design_revision`,
`get_design_baseline`, `get_design_bindings`, `export_design_markdown` and
`check_design_markdown` were dispatchable and unnamed, so a caller handed a
receipt id could not learn the id could be dereferenced (iss-81058e51b53d46b0).

The description now DERIVES the action list from the registry at import time
(`api.state_graph_tools.STATE_GRAPH_READ_DESCRIPTION`), and this file is the
proof over the REAL objects: the description string captured through the real
`api.state_graph_tools.register_state_tools` (the same registration path the MCP
app uses) compared with the real `READ_REQUESTS`. Derivation is what makes drift
impossible going forward; these tests are what notice if the derivation itself is
bypassed — a hand-typed list handed to `@tool` would fail here.

Nothing is excluded today: every `READ_REQUESTS` action is dispatchable through
this tool, so hiding one would be the same defect in the other direction.
`api.state_graph_tools.UNDESCRIBED_READ_ACTIONS` is the ONLY way to legitimately
leave one out, and it must carry a one-line reason per name (hook-internal,
mailbox-internal, ...). An unknown name, a reason-less entry, or an "excluded"
action that is still advertised all fail below — so an exclusion is a recorded
decision rather than an oversight.
"""
from __future__ import annotations

from api import state_graph_tools
from core.state_commands import READ_REQUESTS


def registered_descriptions() -> dict[str, str]:
    """Every description the real registration path hands to the tool decorator.

    A recorder instead of `api.mcp_router.build_mcp()`: the same
    `register_state_tools` call, without mutating the process-global MCP app
    (and therefore without touching `_TOOL_KIND` or another test's wiring)."""
    descriptions: dict[str, str] = {}

    def tool(name, kind, description):
        def decorate(fn):
            descriptions[name] = description
            return fn
        return decorate

    class MCP:
        def prompt(self, **kwargs):
            return lambda fn: fn

        def resource(self, *args, **kwargs):
            return lambda fn: fn

    state_graph_tools.register_state_tools(tool, MCP())
    return descriptions


def state_graph_read_description() -> str:
    descriptions = registered_descriptions()
    assert "state_graph_read" in descriptions, sorted(descriptions)
    return descriptions["state_graph_read"]


def advertised(description: str) -> set[str]:
    """The action names the description itself offers to a caller."""
    assert state_graph_tools.ACTION_LIST_MARKER in description, (
        "state_graph_read no longer carries the generated action list "
        f"({state_graph_tools.ACTION_LIST_MARKER!r} is missing from its description)")
    tail = description.split(state_graph_tools.ACTION_LIST_MARKER, 1)[1]
    assert state_graph_tools.ACTION_LIST_END in tail, (
        f"the action list is not terminated by {state_graph_tools.ACTION_LIST_END!r}")
    listed = tail.split(state_graph_tools.ACTION_LIST_END, 1)[0]
    return {name.strip() for name in listed.split(",") if name.strip()}


def drift_reports(description: str) -> list[str]:
    """Every way `description` disagrees with the registry, as readable lines.

    Returns messages rather than a boolean on purpose: the failures this guards
    are "which action went missing", and a boolean cannot say that. The two
    directions are separate, because they are separate defects — a MISSING name
    hides a real action from every client, a STALE name offers a call that
    `execute` refuses with "unknown state graph action".
    """
    named = advertised(description)
    expected = set(READ_REQUESTS) - set(state_graph_tools.UNDESCRIBED_READ_ACTIONS)
    problems = []
    for missing in sorted(expected - named):
        problems.append(
            f"missing action {missing!r}: core.state_commands.READ_REQUESTS dispatches it and "
            "state_graph_read does not offer it, so a client cannot discover it")
    for stale in sorted(named - set(READ_REQUESTS)):
        problems.append(
            f"unknown action {stale!r}: state_graph_read offers it but "
            "core.state_commands.READ_REQUESTS has no such action")
    return problems


def test_the_registered_description_names_exactly_the_registrys_reads():
    description = state_graph_read_description()
    problems = drift_reports(description)
    assert problems == [], "\n".join(problems)
    # The invariant stated positively as well, so a checker bug cannot pass by
    # comparing two empty sets: get_receipt is advertised, and it is registered.
    assert advertised(description) == set(READ_REQUESTS)
    assert "get_receipt" in advertised(description)


def test_the_description_keeps_the_prose_that_explains_its_actions():
    """The derivation replaces the NAMES, never the meaning around them.

    Each sentence below answers a question a bare name list cannot: what the
    note index hides, what a search matches, what a design query is not, and who
    may read any of it anonymously."""
    description = state_graph_read_description()
    for sentence in (
        # Note-index semantics.
        "get_driver_note returns the entry index, its counts and revision, never free text",
        "superseded_count/delisted_count report what it hides",
        # Search semantics.
        "search_driver_note_entries matches entry assertions and bodies (Unicode case-insensitive literal)",
        "search_driver_note_history returns bounded redacted excerpts of retired section text",
        # The authority statement and the transport boundary.
        "requires writer authorization even though it does not mutate",
        "Design queries return candidates/review hints, not semantic proof",
        "Exact arguments: state_graph_help",
        "core.state_commands.PUBLIC_READS decides which of these an anonymous visitor may read",
    ):
        assert sentence in description, sentence


def test_a_deleted_action_name_is_reported_missing_by_name():
    """The mutation the guard must catch: a genuinely advertised action vanishes.

    `get_receipt` is the exact one iss-81058e51b53d46b0 was filed about, so its
    removal is the regression, not a hypothetical."""
    description = state_graph_read_description()
    assert "get_receipt" in advertised(description)
    mutilated = description.replace("get_receipt, ", "", 1)
    assert "get_receipt" not in advertised(mutilated)
    problems = drift_reports(mutilated)
    assert len(problems) == 1, problems
    assert "missing action 'get_receipt'" in problems[0], problems
    assert "core.state_commands.READ_REQUESTS dispatches it" in problems[0], problems


def test_an_invented_action_name_is_reported_as_unknown():
    """The other direction: a name the registry does not have must not survive."""
    description = state_graph_read_description()
    marker = state_graph_tools.ACTION_LIST_MARKER
    mutilated = description.replace(marker, marker + "get_receipt_reader, ", 1)
    problems = drift_reports(mutilated)
    assert len(problems) == 1, problems
    assert "unknown action 'get_receipt_reader'" in problems[0], problems
    assert "READ_REQUESTS has no such action" in problems[0], problems


def test_every_registry_action_is_either_advertised_or_a_recorded_exclusion():
    """The exclusion map is the only legitimate way to hide a read action.

    It is empty today. If it ever is not, each entry must name a real
    `READ_REQUESTS` action, carry a one-line reason, and actually be absent from
    the description — otherwise the "exclusion" is an oversight wearing its
    clothes."""
    description = state_graph_read_description()
    named = advertised(description)
    for action, reason in state_graph_tools.UNDESCRIBED_READ_ACTIONS.items():
        assert action in READ_REQUESTS, (
            f"{action!r} is excluded from the state_graph_read description but is not a "
            "READ_REQUESTS action at all")
        assert isinstance(reason, str) and reason.strip(), (
            f"{action!r} is excluded without a reason; an exclusion must record why "
            "(hook-internal, mailbox-internal, ...)")
        assert "\n" not in reason.strip(), f"{action!r}'s reason must be one line"
        assert action not in named, (
            f"{action!r} is listed as excluded but the description advertises it; "
            "drop the exclusion instead")
    covered = named | set(state_graph_tools.UNDESCRIBED_READ_ACTIONS)
    assert covered == set(READ_REQUESTS)
