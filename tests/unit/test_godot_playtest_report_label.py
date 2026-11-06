"""The playtest planning fallback names the round its reader derives.

NODE: `gate.godot-playtest-fallback-names-the-provisional-round-20261106`.

`aitelier/tools/godot_playtest/impl.py` AUTHORS `playtest_report.json` itself on
the planning/contract-error fallback of `run_feedback`'s entry — the branch it
takes when the strict feedback contract is missing or unreadable, or when the
feedback plan cannot be built, i.e. before `round_feedback.run_feedback` ever
runs. That report is the THIRD producer of the provisional marker, and it used
to spell `"evidence_state": "partial"` — the word the NODE GATE answers for a
project it could not measure, i.e. a different fact.

It now writes `gate_evidence.PROVISIONAL_ROUND`, imported from the same module
`aitelier/round_feedback.py` imports it from, so no producer keeps a private
copy of the word. What this suite pins is not only the spelling:

THE WORD IS DERIVED FROM THE MARKER, NEVER READ OFF THE LABEL. `report_state`
decides from `purpose` + `PROVISIONAL_PRODUCER` (through
`_provisional_red_is_measured`), so the label the producer writes and the state
a reader answers CANNOT disagree — restoring the old literal would make the
file say `partial` while the classification still answered `provisional`. The
last assertion below forces the old literal back onto this very report and
still requires the round, which is the derivation stated as a test rather than
as a comment.
"""

from __future__ import annotations

import json

from aitelier import gate_evidence as ge
from aitelier.gate_coverage import (
    PRODUCER_ROUND_FEEDBACK, PROVISIONAL_PRODUCER, PROVISIONAL_PURPOSE)
from aitelier.tools.godot_playtest.impl import godot_playtest


def _planning_fallback_report(tmp_path):
    """Drive the fallback itself: a feedback round with no authored contract."""
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "feedback-out"
    result = godot_playtest(project_root=str(repo), out_dir=str(out),
                            purpose=PROVISIONAL_PURPOSE)
    path = out / "playtest_report.json"
    assert result["written"] == str(path), "the fallback writes the report it returns"
    return json.loads(path.read_text(encoding="utf-8")), path


def test_the_planning_fallback_writes_the_word_its_reader_derives(tmp_path):
    report, _path = _planning_fallback_report(tmp_path)

    # The branch really is the planning/contract-error fallback of the feedback
    # entry, not `run_feedback`'s own report: the producer's marker is there,
    # the contract error is what it reports, and the fields `run_feedback` adds
    # beside them (`owner_identity`, `started_at`, ...) are not.
    assert report["purpose"] == PROVISIONAL_PURPOSE
    assert report[PROVISIONAL_PRODUCER] == PRODUCER_ROUND_FEEDBACK
    assert report["passed"] is False and report["full_test_passed"] is False
    assert report["release_disposition"] == "unresolved"
    assert report["selected_state"] == "unavailable"
    assert report["gate_coverage"] is None
    assert [e.split(":", 1)[0] for e in report["errors"]] == ["ValueError"]
    assert "owner_identity" not in report and "started_at" not in report

    # 1. The producer writes the word its reader answers — imported, not spelled.
    #    THIS is the assertion that reddens if an edit restores the literal.
    assert report["evidence_state"] == ge.PROVISIONAL_ROUND, (
        "the planning fallback must name the provisional ROUND; `partial` is the "
        "node gate's word for a project it could not measure, a different fact")
    # 2. Not the node gate's word, stated as the literal it used to be.
    assert report["evidence_state"] != "partial"
    # 3. Asked instead of read: the classification answers the same word.
    assert ge.report_state(report) == ge.PROVISIONAL_ROUND

    # The derivation on this report, not on a hand-built dict: the label is not
    # the input. Force the old literal back and the answer does not move.
    assert ge.report_state(dict(report, evidence_state="partial")) == ge.PROVISIONAL_ROUND, (
        "the state is derived from the marker, so a stale label cannot disagree "
        "with it — and a reader must not start trusting the label either")
