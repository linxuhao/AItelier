"""The reclaim guard must refuse a bundle State still points at — in both shapes.

Why: 2026-11-04, 7 of 15 CANDIDATE nodes had terminal reports citing files that no
longer existed. The bundle had been reclaimed while the report still named it. The
project had the rule already; nothing enforced it, and reclamation is manual.

Two shapes have to be caught, and each one alone misses half the cases:
  * the terminal report is STORED inside the bundle (reclaiming the dir deletes the
    report State points at),
  * the terminal report lives elsewhere but its text CITES files inside the bundle
    (this is how the 2026-11-04 losses actually happened).

And one shape must still pass: a bundle nothing references is reclaimable, otherwise
the guard just blocks all cleanup and gets removed.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL = ROOT / "tools" / "report_refs.py"


def _module():
    spec = importlib.util.spec_from_file_location("report_refs_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """A stand-in State DB with only the table the guard reads."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table state_external_observations ("
        " attempt_id text, observation_id text, report_ref text)"
    )
    con.executemany("insert into state_external_observations values (?,?,?)", rows)
    con.commit()
    con.close()
    return db


def test_a_bundle_nothing_references_is_reclaimable(tmp_path, capsys):
    bundle = tmp_path / "reports" / "nobody-cites-this"
    bundle.mkdir(parents=True)
    db = _state_db(tmp_path, [])
    rc = _module().main([str(bundle), "--db", str(db)])
    assert rc == 0, "an unreferenced bundle must be reclaimable, or the guard blocks all cleanup"
    assert "free" in capsys.readouterr().out


def test_a_report_stored_inside_the_bundle_refuses_reclaim(tmp_path, capsys):
    bundle = tmp_path / "reports" / "cited-bundle"
    bundle.mkdir(parents=True)
    report = bundle / "report.json"
    report.write_text(json.dumps({"status": "candidate", "settled": True, "usable": True}))
    db = _state_db(tmp_path, [("attempt-a", "obs-a", str(report))])
    rc = _module().main([str(bundle), "--db", str(db)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "CITED" in out and "attempt-a" in out


def test_a_report_citing_files_inside_the_bundle_refuses_reclaim(tmp_path, capsys):
    """The 2026-11-04 shape: the report lives in state-report-evidence, the bundle
    it cites is elsewhere — so looking only for stored reports finds nothing."""
    bundle = tmp_path / "reports" / "elsewhere-cited"
    bundle.mkdir(parents=True)
    stored = tmp_path / "state-report-evidence" / "ab" / "somesha"
    stored.parent.mkdir(parents=True)
    stored.write_text(
        "evidence: %s/cold-proof.json and %s/native-final.txt" % (bundle, bundle)
    )
    db = _state_db(tmp_path, [("attempt-b", "obs-b", str(stored))])
    rc = _module().main([str(bundle), "--db", str(db)])
    assert rc == 1, "a report that only CITES the bundle must still block its reclamation"
    assert "cited by attempt-b" in capsys.readouterr().out


def test_an_unreadable_check_is_not_a_pass(tmp_path, capsys):
    """A guard that passes when it cannot read State would let the loss happen
    again on any machine with a moved database."""
    bundle = tmp_path / "reports" / "whatever"
    bundle.mkdir(parents=True)
    rc = _module().main([str(bundle), "--db", str(tmp_path / "no-such.db")])
    assert rc == 2
    assert "not a pass" in capsys.readouterr().err


def test_a_missing_bundle_is_refused_not_silently_absent(tmp_path):
    rc = _module().main([str(tmp_path / "gone"), "--db", str(tmp_path / "x.db")])
    assert rc == 2, "asking about a directory that does not exist must not read as 'free'"


def test_the_guard_would_have_stopped_the_real_losses():
    """Name the actual bundles this guard exists for. If a future refactor drops the
    cited-by-report branch, these are the cases that used to break."""
    mod = _module()
    assert hasattr(mod, "find_refs")
    src = Path(mod.__file__).read_text()
    assert "cited_by_report" in src and "report_stored_here" in src, (
        "both reference shapes must stay detected; the 2026-11-04 losses came from the "
        "cited_by_report shape alone"
    )
