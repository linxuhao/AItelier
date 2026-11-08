"""A timeline input value of the wrong shape is REFUSED naming its key, never
raised out of the preflight.

The ten shapes below are the node's contract, each measured on an otherwise
valid scenario before this change (evidence/playtest-two-reading-20261108,
`report.json` rows `tl-*`, `probe.py` for how each spec was built):

  tl-actions-int      actions: 5      TypeError: 'int' object is not iterable
  tl-actions-mapping  actions: {jump: 1}
                                      iterated by its KEYS: both actions
                                      pressed, both values dropped
  tl-actions-nested-list  actions: [[1]]   carried: `press: ['jump']`
  tl-clicks-int       clicks: 5        TypeError (same iteration)
  tl-clicks-mapping   clicks: {X/Button +0,0: 1}
                                      the key became the aim
  tl-hovers-int       hovers: 5        TypeError (same iteration)
  tl-click-int        click: 5         carried to a String-typed consumer
  tl-press-int        press: 5         carried
  tl-release-int      release: 5       carried
  plus `hover: 5`, the same single-aim shape as `click:`.

A raised TypeError reached the sidecar as a bare HTTP 500: no refusal, no key
named, nothing the author could act on. Each of the ten must now come back as an
error string carrying the entry's frame, the key, and the value's type.

The accepted half proves the check is a shape check and not a blanket refusal:
every shape that already had ONE reading still runs -- `actions:` as a bare
string, empty lists, empty operand strings, and null (falsy for every reader,
so it reads as "no input").

Imports the harness the way tests/unit/test_playtest_one_reading.py does: from
the tree, through importlib, not from an installed package.
"""

import importlib.util
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness_timeline_shapes", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)


def _spec(timeline):
    """An otherwise valid authored spec carrying one scenario's timeline."""
    return {"scene": "res://main.tscn", "frames": 120,
            "scenarios": [{"name": "value_shape", "timeline": timeline}]}


def _preflight(timeline):
    """The real preflight the sidecar runs, with no monkeypatching at all."""
    return gh._authored_execution_plan(_spec(timeline), 120, 120)


# A valid entry before the bad one, so the refusal has to name entry 1 / at 10
# rather than being an artefact of the scenario having nothing else in it.
_BEFORE = {"at": 5, "press": "ui_accept"}


@pytest.mark.parametrize("entry,key,type_name", [
    ({"at": 10, "actions": 5}, "actions", "int"),
    ({"at": 10, "actions": {"jump": 1}}, "actions", "dict"),
    ({"at": 10, "actions": [[1]]}, "actions", "list"),
    ({"at": 10, "clicks": 5}, "clicks", "int"),
    ({"at": 10, "clicks": {"X/Button +0,0": 1}}, "clicks", "dict"),
    ({"at": 10, "hovers": 5}, "hovers", "int"),
    ({"at": 10, "click": 5}, "click", "int"),
    ({"at": 10, "hover": 5}, "hover", "int"),
    ({"at": 10, "press": 5}, "press", "int"),
    ({"at": 10, "release": 5}, "release", "int"),
], ids=["actions-int", "actions-mapping", "actions-list-of-list", "clicks-int",
        "clicks-mapping", "hovers-int", "click-int", "hover-int", "press-int",
        "release-int"])
def test_a_wrong_shaped_timeline_value_is_refused_naming_its_key(
        entry, key, type_name):
    # The call itself must not raise: before this change `actions: 5` /
    # `clicks: 5` / `hovers: 5` / `click: 5` / `hover: 5` raised
    # TypeError: 'int' object is not iterable out of _normalize_timeline.
    plans, errors = _preflight([_BEFORE, entry])
    assert errors, "a wrong-shaped %s must be refused, not run" % key
    hits = [e for e in errors if "`%s`" % key in e and type_name in e]
    assert hits, "no error names `%s` and its type %s: %r" % (key, type_name, errors)
    msg = hits[0]
    assert "timeline entry 1 (at: 10)" in msg, msg
    assert "scenario 'value_shape'" in msg, msg
    # Nothing about the refusal may be a runtime error: it is a spec error.
    assert len(errors) == 1, errors


@pytest.mark.parametrize("key", ["actions", "clicks", "hovers"])
def test_a_mapping_is_not_a_list_of_entries(key):
    """The silent half of the same defect: a mapping was iterated by its keys,
    so `actions: {jump: 1, crouch: 2}` pressed jump AND crouch and dropped both
    values. A mapping has no reading as a list, so it is refused by name."""
    _plans, errors = _preflight([_BEFORE, {"at": 10, key: {"jump": 1, "crouch": 2}}])
    assert len(errors) == 1, errors
    msg = errors[0]
    assert "`%s` is of type dict" % key in msg, msg
    assert "iterated" in msg, msg
    out, _ = gh._normalize_timeline([{"at": 10, key: {"jump": 1, "crouch": 2}}])
    assert out == [], "the mapping's keys must not become entries: %r" % (out,)


def test_the_keys_that_already_had_a_refusal_keep_it():
    """`actions: 5` used to raise before the aim-offset check could speak; the
    two-space-offset refusal itself is unchanged, in its own words."""
    out, errors = gh._normalize_timeline([{"at": 10, "clicks": ["A/Button +0,0 +0,5"]}])
    assert out == [] and len(errors) == 1
    assert "clicks 'A/Button +0,0 +0,5' has 2 offsets (+0,0, +0,5)" in errors[0]
    out, errors = gh._normalize_timeline([{"at": 10, "actions": ["a", 5]}])
    assert out == [] and len(errors) == 1
    assert "`actions` item 1 is of type int" in errors[0]


def test_a_valid_timeline_is_still_accepted_and_scheduled():
    """The check is a shape check: every shape with ONE reading still runs."""
    timeline = [
        {"at": 5, "press": "ui_accept", "release": "ui_accept"},
        {"at": 10, "actions": ["jump", "crouch"]},
        {"at": 15, "actions": "jump"},
        {"at": 20, "actions": [], "clicks": [], "hovers": []},
        {"at": 25, "clicks": ["HUD/Button +0,0", "HUD/Label"], "hovers": ["HUD/Label"]},
        {"at": 30, "click": "HUD/Button right", "hover": "HUD/Label"},
        # empty operands and null keep their single reading: no input
        {"at": 35, "press": "", "release": "", "click": "", "hover": ""},
        {"at": 40, "actions": None, "clicks": None, "hovers": None},
        {"at": 45, "assert": {"HUD/Score.text": 3}},
    ]
    plans, errors = _preflight(timeline)
    assert errors == [], errors
    assert len(plans) == 1 and plans[0][0] >= 45, plans
    out, bad = gh._normalize_timeline(timeline)
    assert bad == [], bad
    assert [(e["at"], e.get("press") or e.get("click") or e.get("hover"))
            for e in out if any(e.get(k) for k in ("press", "click", "hover"))] == [
        (5, "ui_accept"), (10, "jump"), (10, "crouch"), (15, "jump"),
        (25, "HUD/Button +0,0"), (25, "HUD/Label"), (25, "HUD/Label"),
        (30, "HUD/Button right")]
