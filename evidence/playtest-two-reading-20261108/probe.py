"""Enumerate, from the readers themselves, the play-test scenario shapes that
admit two readings.

Issue iss-bd419cf4242e42b4 item 2 records that the 2026-09-24 r2 review of this
harness listed fifteen YAML shapes where the same bytes can be read two ways and
the reader silently takes one, and that the list died with the report. This
battery re-derives the list from the code rather than from the prose: it starts
from ONE valid authored request, changes exactly one path, and records what the
reader actually did with it.

TWO READERS, TWO TABLES
`rows`           the preflight `_authored_execution_plan` in
                 docker/godot/godot_harness.py, which the real play-test path
                 runs before any probe starts.
`contract_rows`  the split-contract loader `read_spec` in
                 aitelier/tools/godot_playtest/impl.py, which reads the
                 playtest/*.yaml files off disk (duplicate keys, the scenario
                 list assembled around _common.yaml).

Per row:
`should`  `refuse` -- the bytes have no single reading, or the value has a type
          nothing can consume, so the reader must refuse. Acceptance is the
          defect under test.
          `accept-one-read` -- acceptance is right, and the row states the one
          reading the reader may take, so a different reading is a misread.
`kind`    what an admitted-but-should-have-been-refused row does:
          `substitute` the reader manufactures a value the author did not write
          `carry`      the written value passes through unchanged

Classes:
refused              the reader refused and its message named the mutated key
refused-unclear      refused, but the message does not name what was refused, so
                     the author is told "no" without being told where
one-reading          accepted, and the reading is the stated one
silent-default       accepted a `substitute` row: `used_instead` differs from
                     `wrote`, and `site` is the file:line that substitutes it
unvalidated-carried  accepted a `carry` row: the value reaches a consumer typed
                     for something else, and `site` is that consumer
misread              accepted an `accept-one-read` row with another reading
raised               the preflight raised instead of refusing. No class in the
                     three-way taxonomy covers it: the request is neither read
                     nor rejected, it 500s -- and (measured, see
                     `crash_consequence`) the tool client turns that 500 into
                     `passed: true, gate_skipped: true`, so a malformed contract
                     silently passes the gate. It is recorded rather than forced
                     into a class.

`covers` maps each mutation the node's contract names to the row that measures it.

Run in a throwaway container from the repo root, no arguments:
    docker run --rm --network none --cpus 2 --memory 2g -u 1000:1000 \
      -v "$PWD":/w -w /w -e PYTHONPATH=/w -e PYTHONDONTWRITEBYTECODE=1 -e HOME=/tmp \
      aitelier:latest /usr/local/bin/python \
      evidence/playtest-two-reading-20261108/probe.py [--json OUT | --row ID]
Deterministic: fixed row order, no clock, no randomness, no dependence on the
working directory (paths derive from this file).
"""

import copy
import hashlib
import os
import traceback
import importlib.util
import json
import shutil
import sys
import tempfile
import pathlib

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[2]
HARNESS_REL = "docker/godot/godot_harness.py"
IMPL_REL = "aitelier/tools/godot_playtest/impl.py"


def _load(name, rel):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gh = _load("godot_harness_two_reading", HARNESS_REL)
impl = _load("godot_playtest_impl_two_reading", IMPL_REL)

FRAMES = 120
TIMEOUT = 60

# Every row carries the exact command that reproduces it, so a class can be
# re-measured by the row alone. `--row <id>` prints only that row's outcome.
REPRODUCE = ("python evidence/playtest-two-reading-20261108/probe.py --row %s")

# The measurement's label, not its content: the rows, their classes and the
# counts are computed from the two subjects and do not depend on either value.
NODE_KEY = "harness.the-two-reading-shapes-are-enumerated-from-the-parser-20261108"
BASE_SHA = "unrecorded"      # --base-sha, or AITELIER_BASE_SHA

# What each class means. Stated once so a reader of the report does not have to
# infer the vocabulary from the row names.
CLASSES = {
    "refused": ("the reader refused the mutation and its message named the key "
                "that was mutated"),
    "refused-unclear": ("the reader refused, but its message does not name what "
                        "was refused, so the author is told 'no' without being "
                        "told where"),
    "one-reading": ("the reader accepted and produced exactly the reading the "
                    "row states"),
    "silent-default": ("the reader accepted a mutation that had to be refused, "
                       "and emitted a value the author did not write: a "
                       "mapping's KEYS became action ids or aims, or a list "
                       "element became the id. `site` names the lines that "
                       "perform the substitution"),
    "unvalidated-carried": ("the reader accepted and passed the written value "
                            "through verbatim although the key's contract is a "
                            "string, so this reader and the run's consumer "
                            "disagree about what the bytes are. `site` names "
                            "the consumer"),
    "misread": ("the reader accepted an accept-one-read row with a reading "
                "other than the one the row states"),
    "raised": ("the reader raised instead of refusing, so the request is "
               "neither read nor rejected: it becomes HTTP 500, and the tool "
               "client turns that 500 into passed=true / gate_skipped=true / "
               "spec_used=false. See `crash_consequence`"),
}

# One valid authored request. Every row mutates exactly one path in a deep copy
# of this, so whatever happens is attributable to that path.
BASE = {
    "frames": 120,
    "scenarios": [
        {
            "name": "walk-in",
            "scene": "res://scenes/a.tscn",
            "timeline": [
                {"at": 10, "actions": ["jump"]},        # 0: action ids
                {"at": 20, "press": "walk"},            # 1: one action down
                {"at": 30, "click": "X/Button +0,0"},   # 2: one aim
                {"at": 40, "assert": {"HUD/Score.text": "3"}},  # 3: asserts
            ],
        }
    ],
}

TL = "scenarios.0.timeline"


def _walk(spec, dotted):
    node = spec
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if part.isdigit() else node[part]
    return node, parts[-1]


def _set(dotted, value):
    def apply(spec):
        node, last = _walk(spec, dotted)
        node[int(last) if last.isdigit() else last] = value
        return spec
    return apply


def _append(entry):
    def apply(spec):
        spec["scenarios"][0]["timeline"].append(entry)
        return spec
    return apply


def _pop(dotted):
    def apply(spec):
        node, last = _walk(spec, dotted)
        node.pop(last, None)
        return spec
    return apply


# (id, mutation, should, what the bytes mean / which reading is allowed, kind,
#  key, wrote, used_instead, site-markers)
# `key`/`wrote`/`used_instead` are stated here, not derived from the outcome:
# the battery then CHECKS that the reader really did that. `site` is the code
# that performs it, resolved to file:line at run time.
ROWS = [
    # --- caller and spec level -------------------------------------------
    ("caller-frames-string", _set("frames", "120"), "refuse",
     "a quoted frame count is not a frame count", "refuse",
     "frames", '"120"', None, ()),
    ("caller-frames-float", _set("frames", 120.0), "refuse",
     "a float frame count is not an exact JSON integer", "refuse",
     "frames", "120.0", None, ()),
    ("caller-frames-bool", _set("frames", True), "refuse",
     "True is an int in Python and not a frame count", "refuse",
     "frames", "true", None, ()),
    ("caller-frames-zero", _set("frames", 0), "refuse",
     "0 frames is not a probe run", "refuse", "frames", "0", None, ()),
    ("caller-frames-past-json-int", _set("frames", 1 << 53), "refuse",
     "a frame count past 2^53-1 is not an exact JSON integer", "refuse",
     "frames", str(1 << 53), None, ()),
    ("spec-scenarios-mapping", _set("scenarios", {"0": {"name": "s"}}), "refuse",
     "a mapping where the scenario list belongs", "refuse",
     "scenarios", "{0: scenario}", None, ()),
    ("spec-scenarios-empty", _set("scenarios", []), "refuse",
     "no scenarios is no request", "refuse", "scenarios", "[]", None, ()),
    ("spec-unknown-key", _set("reviewer_notes", {"assert": {"a.b": 1}}), "refuse",
     "a key nothing reads, whose value can carry assert blocks", "refuse",
     "reviewer_notes", "{assert: {a.b: '1'}}", None, ()),
    ("spec-actions-mapping", _set("actions", {"jump": 1}), "refuse",
     "actions must be a list of action ids, not a mapping", "refuse",
     "actions", "{jump: 1}", None, ()),
    ("spec-surface-list", _set("surface", ["HUD/Score.text"]), "refuse",
     "surface maps a node to attribute names", "refuse",
     "surface", "[HUD/Score.text]", None, ()),
    # --- scenario level ---------------------------------------------------
    ("scn-unknown-key", _set("scenarios.0.reviewer_notes", {"assert": {"a.b": 1}}),
     "refuse", "assert blocks parked under a key no reader evaluates", "refuse",
     "reviewer_notes", "{assert: {a.b: '1'}}", None, ()),
    ("scn-description-mapping", _set("scenarios.0.description", {"a": ["b"]}),
     "refuse", "description must be a plain string", "refuse",
     "description", "{a: [b]}", None, ()),
    ("scn-repeatability-string", _set("scenarios.0.repeatability", "yes"), "refuse",
     "the gate launcher reads 'yes' as off", "refuse",
     "repeatability", "'yes'", None, ()),
    ("scn-scene-int", _set("scenarios.0.scene", 5), "refuse",
     "a scene path must be a string", "refuse", "scene", "5", None, ()),
    ("scn-timeline-mapping", _set("scenarios.0.timeline", {"0": {"at": 1}}), "refuse",
     "a mapping where the entry list belongs", "refuse",
     "timeline", "{0: {at: 1}}", None, ()),
    ("scn-timeline-null", _set("scenarios.0.timeline", None), "refuse",
     "a null where the entry list belongs", "refuse", "timeline", "null", None, ()),
    ("scn-budget-extra-key",
     _set("scenarios.0.execution_budget",
          {"max_frames": 200, "timeout_seconds": 30, "frames": 99}), "refuse",
     "a budget that also carries a frame count nothing reads", "refuse",
     "execution_budget", "{max_frames: 200, timeout_seconds: 30, frames: 99}",
     None, ()),
    ("scn-input-observation-shape",
     _set("scenarios.0.input_observation", {"node": "HUD/Score"}), "refuse",
     "an observation missing half its contract", "refuse",
     "input_observation", "{node: HUD/Score}", None, ()),
    # --- timeline entry level: `at` ---------------------------------------
    ("tl-at-string", _set(TL + ".0.at", "10"), "refuse",
     "a quoted frame number", "refuse", "at", "'10'", None, ()),
    ("tl-at-fraction", _set(TL + ".0.at", 10.5), "refuse",
     "a fractional frame runs at int() and reads as the float", "refuse",
     "at", "10.5", None, ()),
    ("tl-at-negative", _set(TL + ".0.at", -1), "refuse",
     "a frame before the first frame", "refuse", "at", "-1", None, ()),
    ("tl-at-missing", _pop(TL + ".0.at"), "refuse",
     "an entry with no frame, which ran at 0 or never depending on its keys",
     "refuse", "at", "(absent)", None, ()),
    ("tl-at-decreasing", _set(TL + ".3.at", 5), "refuse",
     "an entry written after a later frame runs before it", "refuse",
     "at", "5", None, ()),
    ("tl-at-past-the-cap", _append({"at": 5000, "press": "walk"}), "refuse",
     "a frame past the finite 3000-frame ceiling", "refuse",
     "at", "5000", None, ()),
    # --- timeline entry level: unknown keys -------------------------------
    ("tl-unknown-key", _set(TL + ".0.presses", ["jump"]), "refuse",
     "a near-miss key next to the real one", "refuse",
     "presses", "[jump]", None, ()),
    # --- timeline entry level: plural keys ---------------------------------
    ("tl-actions-mapping", _set(TL + ".0.actions", {"jump": 1, "crouch": 2}),
     "refuse", "a mapping under actions: - its KEYS become action ids and its "
     "values vanish", "substitute",
     "actions", "{jump: 1, crouch: 2}", "press: 'jump' and press: 'crouch'",
     ('acts = e.get("actions") or []', "for a in acts:")),
    ("tl-actions-int", _set(TL + ".0.actions", 5), "refuse",
     "a bare int where the action list belongs", "substitute",
     "actions", "5", None, ('acts = e.get("actions") or []',)),
    ("tl-actions-nested-list", _set(TL + ".0.actions", [["jump"]]), "refuse",
     "an action id is a string, not a list", "carry",
     "actions", "[[jump]]", "press: ['jump']",
     ('acts = e.get("actions") or []', "for a in acts:")),
    ("tl-actions-empty-list", _set(TL + ".0.actions", []), "accept-one-read",
     "an entry whose only key is an empty actions list delivers no input", "accept",
     "actions", "[]", "one entry with `at` and no press", ()),
    ("tl-clicks-mapping", _set(TL + ".2.clicks", {"X/Button +0,0": 1}), "refuse",
     "a mapping under clicks: - its keys become aims", "substitute",
     "clicks", "{X/Button +0,0: 1}", "click: 'X/Button +0,0' (the key)",
     ('clicks = e.get("clicks") or []', "for c in clicks:")),
    ("tl-clicks-int", _set(TL + ".2.clicks", 5), "refuse",
     "a bare int where the aim list belongs", "substitute",
     "clicks", "5", None, ('clicks = e.get("clicks") or []',)),
    ("tl-clicks-empty-list", _set(TL + ".2.clicks", []), "accept-one-read",
     "an empty clicks list adds no click beside the sibling `click`", "accept",
     "clicks", "[]", "the sibling click only", ()),
    ("tl-hovers-mapping", _set(TL + ".3.hovers", {"HUD/Score": 1}), "refuse",
     "a mapping under hovers:", "substitute",
     "hovers", "{HUD/Score: 1}", "hover: 'HUD/Score' (the key)",
     ('hovers = e.get("hovers") or []', "for h in hovers:")),
    ("tl-hovers-int", _set(TL + ".3.hovers", 5), "refuse",
     "a bare int where the aim list belongs", "substitute",
     "hovers", "5", None, ('hovers = e.get("hovers") or []',)),
    # --- timeline entry level: singular keys -------------------------------
    ("tl-press-int", _set(TL + ".1.press", 123), "refuse",
     "press must name an action, not carry a number", "carry",
     "press", "123", "press: 123 reaches a String-typed consumer",
     ('func _act(action: String',)),
    ("tl-press-mapping", _set(TL + ".1.press", {"walk": 1}), "refuse",
     "press must name one action", "carry",
     "press", "{walk: 1}", "press: {walk: 1} reaches a String-typed consumer",
     ('func _act(action: String',)),
    ("tl-press-list", _set(TL + ".1.press", ["walk"]), "refuse",
     "press names one action; a list is the plural shape `actions:`", "carry",
     "press", "[walk]", "press: ['walk'] reaches a String-typed consumer",
     ('func _act(action: String',)),
    ("tl-press-null", _set(TL + ".1.press", None), "refuse",
     "a null press is not an action id", "carry",
     "press", "null", "press: null reaches a String-typed consumer",
     ('func _act(action: String',)),
    ("tl-release-list", _set(TL + ".1.release", ["walk"]), "refuse",
     "release must name one action", "carry",
     "release", "[walk]", "release: ['walk'] reaches has_action/typed consumer",
     ('var rl = e.get("release", "")',)),
    ("tl-release-int", _set(TL + ".1.release", 7), "refuse",
     "a number where an action id belongs", "carry",
     "release", "7", "release: 7 reaches has_action/typed consumer",
     ('var rl = e.get("release", "")',)),
    ("tl-click-int", _set(TL + ".2.click", 7), "refuse",
     "a bare int where the aim belongs", "substitute",
     "click", "7", None,
     ('for aim in ([aims] if isinstance(aims, str) else aims)',)),
    ("tl-hover-list", _set(TL + ".2.hover", ["HUD/A", "HUD/B"]), "refuse",
     "hover names one aim; a list is the plural shape `hovers:`", "carry",
     "hover", "[HUD/A, HUD/B]", "hover: ['HUD/A', 'HUD/B'] reaches a String consumer",
     ('var hv = e.get("hover", "")',)),
    ("tl-click-two-offsets", _set(TL + ".2.click", "X/Button +0,0 +0,5"), "refuse",
     "two offsets in one aim - the later one wins", "refuse",
     "click", "'X/Button +0,0 +0,5'", None,
     ('toks = str(aim).split(" ")[1:]',)),
    ("tl-click-tab-two-offsets",
     _set(TL + ".2.click", "X/Button\t+0,0 +0,5"), "refuse",
     "two offsets split by a TAB - the one-offset check splits on spaces only",
     "carry", "click", "'X/Button<TAB>+0,0 +0,5'",
     "the tab joins the node name; only +0,5 is an offset",
     ('toks = str(aim).split(" ")[1:]', 'var node_name := spec')),
    # --- timeline entry level: asserts and captures ------------------------
    ("tl-assert-string", _set(TL + ".3.assert", "HUD/Score.text == \"3\""), "refuse",
     "an assert written as prose instead of a mapping or list", "refuse",
     "assert", "'HUD/Score.text == \"3\"'", None, ()),
    ("tl-assert-null", _set(TL + ".3.assert", None), "refuse",
     "a null where the assert belongs", "refuse",
     "assert", "null", None, ()),
    ("tl-assert-unknown-key",
     _set(TL + ".3.assert", [{"node": "HUD/Score", "expr": "1 == 1", "wait": 2}]),
     "refuse", "an assert item carrying a key nothing evaluates", "refuse",
     "wait", "{node: HUD/Score, expr: '1 == 1', wait: 2}", None, ()),
    ("tl-assert-mode-and-expr",
     _set(TL + ".3.assert",
          [{"node": "HUD/Score", "attr": "text", "mode": "changed",
            "expr": "text == \"3\""}]), "refuse",
     "one item that is both a delta and a comparison", "refuse",
     "mode", "{mode: changed, expr: ...}", None, ()),
    ("tl-capture-before-string", _set(TL + ".3.capture_before", "hud"), "refuse",
     "a capture written as a name instead of a record", "refuse",
     "capture_before", "'hud'", None, ()),
    # --- legitimate shapes: one reading, and it is stated ------------------
    ("tl-actions-string", _set(TL + ".0.actions", "jump"), "accept-one-read",
     "one action id as a bare string reads as actions: ['jump']", "accept",
     "actions", "'jump'", "press: 'jump'", ()),
    ("tl-at-whole-float", _set(TL + ".0.at", 10.0), "accept-one-read",
     "10.0 is a whole number and runs on frame 10", "accept",
     "at", "10.0", "runs on frame 10", ()),
    ("tl-clicks-list", _set(TL + ".3.clicks", ["HUD/A +0,0", "HUD/B +1,1"]),
     "accept-one-read", "two aims on one frame read as two clicks, in file order",
     "accept", "clicks", "[HUD/A +0,0, HUD/B +1,1]",
     "click: 'HUD/A +0,0' then click: 'HUD/B +1,1'", ()),
    ("tl-hovers-string", _set(TL + ".3.hovers", "HUD/Score"), "accept-one-read",
     "one aim as a bare string reads as hovers: ['HUD/Score']", "accept",
     "hovers", "'HUD/Score'", "hover: 'HUD/Score'", ()),
]

# The token a usable refusal must contain, per refuse row. A refusal that names
# nothing leaves the author with an error they cannot locate, which is its own
# defect (class refused-unclear) and must not be booked as a clean refusal.
NAMES = {
    "caller-frames-string": "frames",
    "caller-frames-float": "frames",
    "caller-frames-bool": "frames",
    "caller-frames-zero": "frames",
    "caller-frames-past-json-int": "frames",
    "spec-scenarios-mapping": "scenarios",
    "spec-scenarios-empty": "scenarios",
    "spec-unknown-key": "reviewer_notes",
    "spec-actions-mapping": "actions",
    "spec-surface-list": "surface",
    "scn-unknown-key": "reviewer_notes",
    "scn-description-mapping": "description",
    "scn-repeatability-string": "repeatability",
    "scn-scene-int": "scene",
    "scn-timeline-mapping": "timeline",
    "scn-timeline-null": "timeline",
    "scn-budget-extra-key": "execution_budget",
    "scn-input-observation-shape": "input_observation",
    "tl-at-string": "`at`",
    "tl-at-fraction": "`at`",
    "tl-at-negative": "`at`",
    "tl-at-missing": "`at`",
    "tl-at-decreasing": "`at`",
    "tl-at-past-the-cap": "frames",
    "tl-unknown-key": "presses",
    "tl-actions-mapping": "actions",
    "tl-actions-int": "actions",
    "tl-actions-nested-list": "actions",
    "tl-clicks-mapping": "clicks",
    "tl-clicks-int": "clicks",
    "tl-hovers-mapping": "hovers",
    "tl-hovers-int": "hovers",
    "tl-press-int": "press",
    "tl-press-mapping": "press",
    "tl-press-list": "press",
    "tl-press-null": "press",
    "tl-release-list": "release",
    "tl-release-int": "release",
    "tl-click-int": "click",
    "tl-hover-list": "hover",
    "tl-click-two-offsets": "offset",
    "tl-click-tab-two-offsets": "click",
    "tl-assert-string": "assert",
    "tl-assert-null": "assert",
    "tl-assert-unknown-key": "wait",
    "tl-assert-mode-and-expr": "mode",
    "tl-capture-before-string": "capture_before",
}

# What the reader may add to (or drop from) the baseline reading, per
# accept-one-read row in `rows`. Stated here, not derived, so a row cannot agree
# with itself.
EXPECT_DELTA = {
    "tl-actions-empty-list": {"added": [{"at": 10}],
                              "removed": [{"at": 10, "press": "jump"}]},
    "tl-clicks-empty-list": {"added": [], "removed": []},
    "tl-actions-string": {"added": [], "removed": []},
    "tl-at-whole-float": {"added": [], "removed": []},
    "tl-clicks-list": {"added": [{"at": 40, "click": "HUD/A +0,0"},
                                 {"at": 40, "click": "HUD/B +1,1"}],
                       "removed": []},
    "tl-hovers-string": {"added": [{"at": 40, "hover": "HUD/Score"}],
                         "removed": []},
}

# --- the split-contract loader table ---------------------------------------
# Each row writes one contract to a temp repo and reads it with `read_spec`.
# `files` maps a repo-relative path to its YAML body.
CONTRACT_ROWS = [
    ("contract-duplicate-key", {
        "playtest/_common.yaml": "frames: 120\n",
        "playtest/w.yaml": ("scene: res://a.tscn\ntimeline:\n"
                            "  - at: 10\n    press: walk\n    press: jump\n"),
     }, "refuse", "a duplicate mapping key: YAML keeps the LAST value, so the "
     "first block is discarded and anything it asserted never runs", "refuse",
     "press", "two `press:` keys in one entry", None, ()),
    ("contract-common-scenarios", {
        "playtest/_common.yaml": ("frames: 120\nscenarios:\n  - name: from-common\n"
                                  "    timeline: [{at: 5, press: ghost}]\n"),
        "playtest/a.yaml": "scene: res://a.tscn\ntimeline:\n  - at: 10\n    press: walk\n",
     }, "refuse", "a `scenarios:` block written in _common.yaml is discarded: the "
     "loader replaces `scenarios` with the files it found", "substitute",
     "scenarios", "_common.yaml: [{name: from-common, ...}]",
     "the file-derived list only (from-common vanishes)",
     ('spec["scenarios"] = [found[n] for n in names]',)),
    ("contract-scenario-order-ghost", {
        "playtest/_common.yaml": "frames: 120\nscenario_order: [a, ghost]\n",
        "playtest/a.yaml": "scene: res://a.tscn\ntimeline:\n  - at: 10\n    press: walk\n",
     }, "refuse", "scenario_order names a file that does not exist", "refuse",
     "ghost", "scenario_order: [a, ghost]", None, ()),
    ("contract-scenario-no-timeline", {
        "playtest/w.yaml": "scene: res://a.tscn\n",
     }, "refuse", "a scenario file with no timeline", "refuse",
     "timeline", "a scenario file without `timeline:`", None, ()),
    ("contract-scenario-name-mismatch", {
        "playtest/w.yaml": ("name: other\nscene: res://a.tscn\ntimeline:\n"
                            "  - at: 10\n    press: walk\n"),
     }, "refuse", "a scenario whose `name:` disagrees with its basename", "refuse",
     "name", "name: other in w.yaml", None, ()),
    ("contract-order-and-files", {
        "playtest/_common.yaml": "frames: 120\nscenario_order: [b, a]\n",
        "playtest/a.yaml": "scene: res://a.tscn\ntimeline:\n  - at: 10\n    press: walk\n",
        "playtest/b.yaml": "scene: res://b.tscn\ntimeline:\n  - at: 10\n    press: jump\n",
     }, "accept-one-read", "scenario_order fixes the run order; unlisted files are "
     "appended sorted, so dropping a file in is enough", "accept",
     "scenario_order", "[b, a]", "scenarios run as [b, a]", ()),
]


def _preflight(spec):
    """(plans, errors) -- or ('raised', exc_type, message, site)."""
    try:
        return gh._authored_execution_plan(copy.deepcopy(spec), FRAMES, TIMEOUT)
    except Exception as exc:            # noqa: BLE001 - crashing is a row outcome
        # The line that raised, so a crash row is actionable too.
        frames = traceback.extract_tb(exc.__traceback__)
        where = []
        for frame in frames:
            if frame.filename.endswith(pathlib.Path(HARNESS_REL).name):
                rel = os.path.relpath(frame.filename, str(ROOT))
                where.append("%s:%d" % (rel, frame.lineno))
        return ("raised", type(exc).__name__, str(exc)[:300], where)


def _normalized(spec):
    """The reading the reader would run: normalised timeline of scenario 0."""
    scen = spec["scenarios"][0]
    timeline, bad = gh._normalize_timeline(copy.deepcopy(scen.get("timeline") or []))
    if bad:
        return None, bad
    return timeline, []


def _site(*markers):
    """file:line of each marker, in the file that actually carries it.

    A reading with no address is a claim, not a measurement: a later repair has
    to be writable from the row alone.
    """
    found = []
    for marker in markers:
        for rel in (HARNESS_REL, IMPL_REL):
            lines = (ROOT / rel).read_text().splitlines()
            hits = ["%s:%d" % (rel, i + 1) for i, line in enumerate(lines)
                    if marker in line]
            if hits:
                found.extend(hits)
                break
        else:
            raise AssertionError("marker not found in either subject: %r" % marker)
    return found


def _baseline_timeline():
    timeline, bad = _normalized(BASE)
    assert not bad, bad
    return timeline


def _diff(actual, baseline):
    """What the reader used instead of what the author wrote.

    A MULTISET difference: an accepted shape can duplicate an entry already in
    the baseline (a mapping under `clicks:` re-emits the same click), and a
    membership test would call that "nothing changed"."""
    def counts(entries):
        out = {}
        for entry in entries:
            key = json.dumps(entry, sort_keys=True)
            out[key] = out.get(key, 0) + 1
        return out

    have, want = counts(actual), counts(baseline)
    added, removed = [], []
    for key, n in have.items():
        added.extend([json.loads(key)] * max(0, n - want.get(key, 0)))
    for key, n in want.items():
        removed.extend([json.loads(key)] * max(0, n - have.get(key, 0)))
    return {"added": added, "removed": removed}


def classify(row):
    (row_id, apply, should, meaning, kind, key, wrote, used_instead,
     site_markers) = row
    spec = apply(copy.deepcopy(BASE))
    outcome = _preflight(spec)
    entry = {
        "id": row_id,
        "reader": "preflight",
        "mutation": meaning,
        "should": should,
        "kind": kind,
        "key": key,
        "wrote": wrote,
        "used_instead": used_instead,
        "reproduce": REPRODUCE % row_id,
    }
    if isinstance(outcome, tuple) and outcome and outcome[0] == "raised":
        entry["class"] = "raised"
        entry["outcome"] = {"exception": outcome[1], "message": outcome[2],
                            "site": outcome[3]}
        return entry
    plans, errors = outcome
    baseline = _baseline_timeline()
    if errors:
        token = NAMES[row_id]
        named = any(token in e for e in errors)
        entry["class"] = "refused" if named else "refused-unclear"
        entry["outcome"] = {"errors": errors, "must_name": token,
                            "named_the_mutation": named}
        return entry
    actual, bad = _normalized(spec)
    if bad:                       # unreachable through the preflight; kept honest
        entry["class"] = "refused-unclear"
        entry["outcome"] = {"errors": bad, "stage": "normalize"}
        return entry
    delta = _diff(actual, baseline)
    entry["outcome"] = {"reading": actual, "delta_from_baseline": delta,
                        "site": _site(*site_markers) if site_markers else []}
    if should == "accept-one-read":
        entry["class"] = "one-reading" if delta == EXPECT_DELTA[row_id] else "misread"
        entry["outcome"]["allowed_delta"] = EXPECT_DELTA[row_id]
    elif kind == "carry":
        entry["class"] = "unvalidated-carried"
    else:
        entry["class"] = "silent-default"
    return entry


def _scratch(name):
    """A FIXED scratch path, wiped first.

    `mkdtemp` puts a random component into every message the reader produces,
    and a report whose rows change between two runs cannot be re-measured
    against itself. The path is still outside the tree and still thrown away."""
    path = pathlib.Path(tempfile.gettempdir()) / "two-reading-probe" / name
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def contract_reading(files, name):
    """Write one contract to a scratch repo and read it back."""
    repo = _scratch(name)
    for rel, body in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    spec, info = impl.read_spec(repo)
    names = []
    if isinstance(spec, dict):
        names = [str((s or {}).get("name")) for s in (spec.get("scenarios") or [])
                 if isinstance(s, dict)]
    return {"source": info.get("source"), "errors": info.get("errors") or [],
            "notes": info.get("notes") or [], "scenario_names": names}


def classify_contract(row):
    (row_id, files, should, meaning, kind, key, wrote, used_instead,
     site_markers) = row
    result = contract_reading(files, row_id)
    entry = {
        "id": row_id,
        "reader": "read_spec",
        "mutation": meaning,
        "should": should,
        "kind": kind,
        "key": key,
        "wrote": wrote,
        "used_instead": used_instead,
        "outcome": result,
        "reproduce": REPRODUCE % row_id,
    }
    if should == "refuse":
        if result["errors"]:
            named = any(key in e for e in result["errors"])
            entry["class"] = "refused" if named else "refused-unclear"
        else:
            entry["class"] = "silent-default"
    else:
        entry["class"] = ("one-reading"
                          if result["scenario_names"] == ["b", "a"] else "misread")
    if entry["class"] in ("silent-default", "misread") and site_markers:
        result["site"] = _site(*site_markers)
    return entry


# The mutations the node's contract names, each pointing at the row measuring it.
COVERS = {
    "an actions: entry": ["tl-actions-mapping", "tl-actions-string",
                          "tl-actions-nested-list", "tl-actions-int"],
    "a clicks: entry and its offset": ["tl-clicks-mapping", "tl-clicks-list",
                                       "tl-click-two-offsets",
                                       "tl-click-tab-two-offsets"],
    "a frames: value": ["caller-frames-string", "caller-frames-float",
                        "caller-frames-bool", "caller-frames-zero",
                        "caller-frames-past-json-int"],
    "a scenario list reached through _common.yaml": ["contract-common-scenarios",
                                                     "contract-scenario-order-ghost",
                                                     "contract-order-and-files"],
    "a duplicate key": ["contract-duplicate-key"],
    "a mapping where a scalar is expected": ["tl-press-mapping",
                                             "scn-description-mapping"],
    "a scalar where a mapping is expected": ["tl-assert-string",
                                             "spec-actions-mapping"],
    "an unknown key": ["tl-unknown-key", "spec-unknown-key", "scn-unknown-key"],
    "a TAB-separated offset": ["tl-click-tab-two-offsets"],
    "a null value": ["tl-press-null", "tl-assert-null", "scn-timeline-null"],
    "an out-of-range number": ["tl-at-past-the-cap",
                               "caller-frames-past-json-int"],
    "an empty list": ["tl-actions-empty-list", "tl-clicks-empty-list",
                      "spec-scenarios-empty"],
}


def _consequence():
    """What a `raised` row costs downstream, measured rather than asserted.

    The sidecar answers a crashed request with HTTP 500; the tool client's only
    non-timeout handler for that is the "godot-builder unreachable" branch,
    which returns passed=True with gate_skipped=True. Both halves are measured
    here: the sidecar function raising, and the client's report for a 500.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    out = {}
    tmp = _scratch("consequence-sidecar")
    (tmp / "project.godot").write_text("config_version=5\n")
    try:
        gh.playtest_project(str(tmp), spec={"frames": 120, "scenarios": [
            {"name": "w", "scene": "res://a.tscn",
             "timeline": [{"at": 10, "actions": 5}]}]}, frames=120, timeout=60)
        out["sidecar"] = "returned normally"
    except Exception as exc:              # noqa: BLE001 - the measurement
        out["sidecar"] = {"raised": type(exc).__name__, "message": str(exc)[:160]}

    body = json.dumps({"error": "'int' object is not iterable"}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        impl._BUILDER_URL = "http://127.0.0.1:%d" % server.server_port
        repo = _scratch("consequence-tool")
        (repo / "project.godot").write_text("config_version=5\n")
        (repo / "playtest").mkdir()
        (repo / "playtest" / "_common.yaml").write_text("frames: 120\n")
        (repo / "playtest" / "w.yaml").write_text(
            "scene: res://a.tscn\ntimeline:\n  - at: 10\n    actions: 5\n")
        dest = _scratch("consequence-out")
        tool = impl.godot_playtest(project_root=str(repo), out_dir=str(dest),
                                   timeout=30)
        report = json.loads((dest / "playtest_report.json").read_text())
        out["tool_client"] = {
            "tool_returned": {"passed": tool.get("passed"),
                              "written": tool.get("written")},
            "report": {k: report.get(k) for k in
                       ("passed", "gate_skipped", "spec_used", "frames", "errors")},
        }
    finally:
        server.shutdown()
    return out


def main(argv):
    out_path = None
    only = None
    base_sha = os.environ.get("AITELIER_BASE_SHA") or BASE_SHA
    args = argv[1:]
    while args:
        flag = args.pop(0)
        if flag == "--json":
            out_path = pathlib.Path(args.pop(0)).resolve()
        elif flag == "--row":
            only = args.pop(0)
        elif flag == "--base-sha":
            base_sha = args.pop(0)
        else:
            print("unknown argument: %s" % flag, file=sys.stderr)
            return 2

    plans, baseline_errors = _preflight(BASE)
    rows = [classify(row) for row in ROWS]
    contract_rows = [classify_contract(row) for row in CONTRACT_ROWS]
    for entry in rows + contract_rows:
        # A silent default ships with the line that performs it, or it does not
        # ship: the row's own markers are the only source for that address.
        if entry["class"] in ("silent-default", "unvalidated-carried"):
            assert entry["outcome"].get("site"), (
                "%s is a %s with no substitution site" % (entry["id"], entry["class"]))
    counts = {}
    for entry in rows + contract_rows:
        counts[entry["class"]] = counts.get(entry["class"], 0) + 1
    report = {
        "probe": HERE.name,
        "node_key": NODE_KEY,
        "base_sha": base_sha,
        "classes": CLASSES,
        "mutations": [
            {k: e[k] for k in ("id", "reader", "should", "kind", "key", "wrote",
                               "used_instead", "reproduce", "class")}
            | {"site": e["outcome"].get("site") or []}
            for e in rows + contract_rows
        ],
        "subjects": {HARNESS_REL: hashlib.sha256(
                         (ROOT / HARNESS_REL).read_bytes()).hexdigest(),
                     IMPL_REL: hashlib.sha256(
                         (ROOT / IMPL_REL).read_bytes()).hexdigest()},
        "baseline": {"errors": baseline_errors, "plans": plans,
                     "reading": _baseline_timeline()},
        "rows": rows,
        "contract_rows": contract_rows,
        "counts": counts,
        "total": len(rows) + len(contract_rows),
        "covers": COVERS,
        "crash_consequence": _consequence(),
        "reproduce": {
            "all": ("python evidence/playtest-two-reading-20261108/probe.py"),
            "one": ("python evidence/playtest-two-reading-20261108/probe.py "
                    "--row <row id>"),
        },
    }
    selected = rows + contract_rows
    if only:
        selected = [e for e in selected if e["id"] == only]
        if not selected:
            print("no such row: %s" % only, file=sys.stderr)
            return 2
    print("baseline_errors=%r baseline_plans=%r" % (baseline_errors, plans))
    for entry in selected:
        outcome = entry["outcome"]
        detail = (outcome.get("errors") or outcome.get("message")
                  or outcome.get("delta_from_baseline") or outcome.get("scenario_names")
                  or "")
        if isinstance(detail, list):
            detail = detail[0] if detail else ""
        if isinstance(detail, dict):
            detail = json.dumps(detail, sort_keys=True)
        print("%-32s reader=%-10s should=%-16s class=%-20s %s"
              % (entry["id"], entry["reader"], entry["should"], entry["class"], detail))
    print("counts=%s total=%d" % (json.dumps(counts, sort_keys=True), len(selected)))
    if out_path is not None:
        out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print("wrote=%s" % out_path)
    if baseline_errors:
        print("BASELINE IS NOT VALID - every row above is meaningless", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
