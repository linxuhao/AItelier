"""The seed file's format contract, pinned from the writing side.

Its reader is a GENERATED tool (`~/.AItelier/tools/<config>__prepare_seed`),
which is not tracked in this repository — so nothing here fails when the two
sides disagree. They did disagree: the reader decoded the whole remainder as one
JSON document while the writer had started appending a "## Relay" section after
it, and every continue_from relay died at its first step with
"Extra data: line 3 column 1" before running anything (2026-09-11,
release.mainline-green r5). These tests state what the reader is entitled to
assume, so a change on this side has to break something visible.
"""
import json

from core.state_service import SEED_HEADING, state_seed_text

CONTEXT = {"goal": "g", "instruction": "do the thing", "node_key": "n.k",
           "state_project_id": "p", "contract_hash": "h", "revision": 1,
           "acceptance": [{"id": "c1", "description": "d", "kind": "test"}]}


def _split(seed):
    """Exactly what the reader does: strip the heading, decode ONE value."""
    assert seed.startswith(SEED_HEADING), "the heading must come first"
    body = seed[len(SEED_HEADING):].strip()
    value, end = json.JSONDecoder().raw_decode(body)
    return value, body[end:].strip()


def test_a_plain_seed_is_the_heading_then_one_json_object():
    data, tail = _split(state_seed_text(CONTEXT, {}, relay=False))
    assert data["node_key"] == "n.k"
    assert tail == "", "a non-relay seed must not carry a trailing section"


def test_a_relay_seed_still_decodes_and_keeps_its_prose_after_the_json():
    """The failure mode itself: JSON first, prose after, never interleaved."""
    seed = state_seed_text(CONTEXT, {}, relay=True)
    data, tail = _split(seed)
    assert data["node_key"] == "n.k"
    assert tail, "the relay section is the whole point of the relay seed"
    assert "CONTINUES a prior attempt" in tail
    # And the shape the old reader assumed must be the one that is now WRONG,
    # or this test would pass against the bug.
    try:
        json.loads(seed[len(SEED_HEADING):].strip())
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError(
            "a relay seed parsed as a single whole-document JSON — this test "
            "cannot distinguish the fix from the bug any more")


def test_nothing_is_inserted_between_the_heading_and_the_json():
    for relay in (False, True):
        seed = state_seed_text(CONTEXT, {}, relay=relay)
        between = seed[len(SEED_HEADING):]
        assert between.lstrip().startswith("{"), (
            "the reader strips the heading and decodes immediately; anything "
            "in between makes the seed unreadable")
