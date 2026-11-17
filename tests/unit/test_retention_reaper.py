"""Witnesses for ``deploy/retention_reaper.py`` — the ~/.AItelier reclamation pass.

Four criteria, one group each:

  c1 SHAPE + DRY RUN DEFAULT — with no ``--delete`` the home is byte-for-byte
     unchanged; a symlink target is never deleted and is COUNTED as refused, and
     a path resolving outside ``--home`` is refused rather than skipped.
  c2 GODOT EVIDENCE — ``godot-control/evidence/<32hex>/`` defaults to the owner's
     3 day retention (ruled 2026-11-16), is aged by the DIRECTORY's mtime, and
     ``--retention godot-evidence=N`` overrides it.
  c3 RELEASE-WATCH RUNS — an expired ``release-watch/runs/<run>/`` is only a
     candidate when nothing under ``release-watch/outcomes/`` mentions the run
     directory name; a referenced run is kept as ``referenced-by-outcome``.
  c4 IMMUNE SET — ``state-report-evidence/``, ``aitelier.db*``, ``artifacts/``,
     ``dsh-drivers/*/sessions`` and ``dsh-drivers/*/todo`` never become
     candidates. The witness is a live deletion run with every retention forced
     to zero: the immune entries are still there, and every path handed to the
     single deletion primitive was enumerated under a managed root.
  c5 CITED SUBTREES — an outcome that cites ``runs/<id>/<segment>/demo`` keeps that
     subtree and makes the REST of the run reclaimable (``uncited-remainder``); a
     run cited only at its own directory keeps every byte. The class report states
     kept, reclaimable and deleted bytes, and the three add back up to the class
     total measured before the pass.

The fixtures are all ``tmp_path``; nothing here reads or writes a real
``~/.AItelier``.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deploy import retention_reaper as reaper  # noqa: E402

DAY = 86400.0

# Ages the brief used: comfortably inside, just past, and far past 3 days.
INSIDE, JUST_PAST, FAR_PAST = 1.0, 4.0, 40.0


# --------------------------------------------------------------------------- #
# fixture builders
# --------------------------------------------------------------------------- #


def _set_age(path: Path, days: float, now: float) -> None:
    """Stamp an entry's own mtime ``days`` before ``now`` (a symlink is never followed)."""
    stamp = now - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _make_dir(
    root: Path,
    name: str,
    *,
    now: float,
    days: float | None,
    payload: bytes | None = b"x" * 1024,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    if payload is not None:
        (path / "blob.bin").write_bytes(payload)
    if days is not None:
        _set_age(path, days, now)
    return path


def _make_file(
    root: Path,
    name: str,
    *,
    now: float,
    days: float | None,
    payload: bytes = b"y" * 64,
) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if days is not None:
        _set_age(path, days, now)
    return path


def _bytes_below(path: Path) -> int:
    """Bytes an entry is measured at: everything below it (spelled independently here)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in sorted(dirnames + filenames):
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _bytes_kept_with(path: Path) -> int:
    """Bytes that stay when ``path`` is retained: the entry itself plus all below it."""
    return os.lstat(path).st_size + _bytes_below(path)


def _make_run(
    root: Path,
    name: str,
    *,
    now: float,
    days: float,
    files: dict[str, bytes],
) -> Path:
    """One run directory: run-relative files, aged by the run directory's own mtime."""
    run = root / name
    for rel, payload in files.items():
        target = run / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _set_age(run, days, now)
    return run


def _snapshot(root: Path) -> dict[str, tuple[int, int, bool, str | None]]:
    """Every entry below ``root``: size, mtime, symlink-ness and link target."""
    snapshot: dict[str, tuple[int, int, bool, str | None]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            st = os.lstat(path)
            is_link = stat.S_ISLNK(st.st_mode)
            snapshot[path.relative_to(root).as_posix()] = (
                st.st_size,
                st.st_mtime_ns,
                is_link,
                os.readlink(path) if is_link else None,
            )
    return snapshot


def _records(class_report: dict) -> list[dict]:
    return [
        record
        for key in ("candidates", "deleted", "kept", "refused")
        for record in class_report[key]
    ]


def _decisions(class_report: dict) -> dict[str, str]:
    """path -> status for one class."""
    return {record["path"]: record["status"] for record in _records(class_report)}


def _reasons(class_report: dict) -> dict[str, str]:
    """path -> reason for one class."""
    return {record["path"]: record["reason"] for record in _records(class_report)}


def _independent(path: str) -> bool:
    """Is this home-relative path in the never-delete set? (spelled independently here)"""
    top = path.strip("/").split("/")
    rel = "/".join(top)
    if rel == "aitelier.db" or rel.startswith("aitelier.db"):
        return True
    if rel == "state-report-evidence" or rel.startswith("state-report-evidence/"):
        return True
    if rel == "artifacts" or rel.startswith("artifacts/"):
        return True
    if len(top) >= 3 and top[0] == "dsh-drivers" and top[2] in ("sessions", "todo"):
        return True
    return False


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


# --------------------------------------------------------------------------- #
# c1 — shape, dry run by default, refusals instead of silent skips
# --------------------------------------------------------------------------- #


def test_dry_run_leaves_the_home_byte_for_byte_unchanged(home: Path) -> None:
    now = time.time()
    evidence = home / "godot-control" / "evidence"
    for name, days in (("a" * 32, INSIDE), ("b" * 32, JUST_PAST), ("c" * 32, FAR_PAST)):
        _make_dir(evidence, name, now=now, days=days)
    _make_dir(home / "scratch", "old", now=now, days=FAR_PAST)
    _make_dir(home / "worktrees-scratch", "old", now=now, days=FAR_PAST)

    before = _snapshot(home)
    report = reaper.run(home, now=now)
    after = _snapshot(home)

    assert report["mode"] == "dry-run"
    assert after == before, "a run without --delete must not touch the home"
    assert report["totals"]["deleted_count"] == 0
    # It did find work — a reaper that reports zero candidates proves nothing.
    assert report["totals"]["candidate_count"] == 4
    assert all(
        record["status"] == "would-delete"
        for class_report in report["classes"].values()
        for record in class_report["candidates"]
    )


def test_a_symlink_target_is_refused_counted_and_never_deleted(home: Path, tmp_path: Path) -> None:
    now = time.time()
    scratch = home / "scratch"
    keepme = _make_dir(scratch, "keepme", now=now, days=INSIDE)
    expired = _make_dir(scratch, "expired", now=now, days=FAR_PAST)
    inside_link = scratch / "inlink"
    inside_link.symlink_to(keepme)  # stays inside --home -> refused as a symlink

    outside = tmp_path / "outside-home"
    outside.mkdir()
    (outside / "precious.bin").write_bytes(b"do not delete me")
    escapee = scratch / "escapee"
    escapee.symlink_to(outside)  # resolves outside --home -> refused as an escape

    before = _snapshot(home)
    dry = reaper.run(home, now=now)
    scratch_report = dry["classes"]["scratch"]
    reasons = _reasons(scratch_report)

    assert reasons["scratch/inlink"] == "symlink"
    assert reasons["scratch/escapee"] == "outside-home"
    assert scratch_report["refused_count"] == 2
    assert dry["totals"]["refused_count"] == 2
    # Refused entries are not silently absent: each one is in the report.
    assert {record["path"] for record in scratch_report["refused"]} == {
        "scratch/inlink",
        "scratch/escapee",
    }
    assert _snapshot(home) == before, "the dry run must not even touch the links"

    deleted = reaper.run(home, now=now, delete=True)["classes"]["scratch"]
    assert keepme.is_dir(), "the target of a symlink must survive"
    assert (keepme / "blob.bin").exists()
    assert inside_link.is_symlink()
    assert escapee.is_symlink()
    assert (outside / "precious.bin").read_bytes() == b"do not delete me"
    assert {record["path"] for record in deleted["deleted"]} == {"scratch/expired"}
    assert deleted["refused_count"] == 2


def test_a_class_root_escaping_home_is_refused_as_a_whole(home: Path, tmp_path: Path) -> None:
    now = time.time()
    outside = tmp_path / "elsewhere-scratch"
    (outside / "ancient").mkdir(parents=True)
    (outside / "ancient" / "f.bin").write_bytes(b"z" * 10)
    _set_age(outside / "ancient", FAR_PAST, now)
    (home / "scratch").symlink_to(outside)

    report = reaper.run(home, now=now, delete=True)
    scratch_report = report["classes"]["scratch"]

    assert scratch_report["refused_count"] == 1
    assert scratch_report["candidates"] == [] and scratch_report["deleted"] == []
    assert scratch_report["refused"][0]["reason"] == "outside-home"
    assert (outside / "ancient" / "f.bin").exists(), "nothing outside --home may be deleted"


def test_cli_shape_requires_home_and_defaults_to_dry_run(home: Path, tmp_path: Path) -> None:
    now = time.time()
    _make_dir(home / "godot-control" / "evidence", "d" * 32, now=now, days=FAR_PAST)
    _make_dir(home / "scratch", "old", now=now, days=FAR_PAST)

    no_home = subprocess.run(
        [sys.executable, "-m", "deploy.retention_reaper"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert no_home.returncode == 2, no_home.stderr
    assert "--home" in no_home.stderr

    report_path = tmp_path / "reports" / "reaper.json"
    before = _snapshot(home)
    dry = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.retention_reaper",
            "--home",
            str(home),
            "--json",
            str(report_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert dry.returncode == 0, dry.stderr
    assert _snapshot(home) == before
    assert "dry run" in dry.stdout
    assert "would-delete" in dry.stdout

    payload = json.loads(report_path.read_text())
    assert payload["mode"] == "dry-run"
    assert payload["home"] == str(home)
    assert payload["classes"]["godot-evidence"]["candidate_count"] == 1
    assert payload["classes"]["scratch"]["candidate_count"] == 1


# --------------------------------------------------------------------------- #
# c2 — godot evidence keeps 3 days, aged by directory mtime, override works
# --------------------------------------------------------------------------- #


def _godot_fixture(home: Path, now: float) -> dict[str, Path]:
    evidence = home / "godot-control" / "evidence"
    return {
        "one_day": _make_dir(evidence, "1" * 32, now=now, days=INSIDE, payload=b"a" * 1000),
        "four_days": _make_dir(evidence, "4" * 32, now=now, days=JUST_PAST, payload=b"b" * 2000),
        "forty_days": _make_dir(evidence, "c" * 32, now=now, days=FAR_PAST, payload=b"c" * 4000),
    }


def test_godot_evidence_keeps_three_days_and_deletes_past_it(home: Path) -> None:
    now = time.time()
    created = _godot_fixture(home, now)
    # The files inside a kept directory are NEWER than the directory. Age must come
    # from the directory's own mtime, not from its newest child.
    _set_age(created["four_days"] / "blob.bin", 0.0, now)

    assert reaper.CLASS_BY_NAME["godot-evidence"].default_days == 3.0

    dry = reaper.run(home, now=now)
    class_report = dry["classes"]["godot-evidence"]
    assert class_report["retention_days"] == 3.0
    assert class_report["candidate_count"] == 2
    assert class_report["candidate_bytes"] == 6000
    assert _decisions(class_report) == {
        "godot-control/evidence/" + "1" * 32: "kept",
        "godot-control/evidence/" + "4" * 32: "would-delete",
        "godot-control/evidence/" + "c" * 32: "would-delete",
    }
    assert _reasons(class_report)["godot-control/evidence/" + "1" * 32] == "within-retention"
    assert _reasons(class_report)["godot-control/evidence/" + "4" * 32] == "expired"

    report = reaper.run(home, now=now, delete=True)
    class_report = report["classes"]["godot-evidence"]
    assert class_report["deleted_count"] == 2
    assert class_report["deleted_bytes"] == 6000
    assert created["one_day"].is_dir()
    assert not created["four_days"].exists()
    assert not created["forty_days"].exists()


def test_retention_override_is_per_class_and_honoured(home: Path) -> None:
    now = time.time()
    created = _godot_fixture(home, now)

    report = reaper.run(home, now=now, delete=True, retention={"godot-evidence": 30.0})
    class_report = report["classes"]["godot-evidence"]

    assert class_report["retention_days"] == 30.0
    assert created["one_day"].is_dir()
    assert created["four_days"].is_dir()
    assert not created["forty_days"].exists()
    assert class_report["deleted_count"] == 1

    # Other classes keep their own defaults; the override is not a global knob.
    assert report["classes"]["scratch"]["retention_days"] == 14.0
    assert report["classes"]["worktrees-scratch"]["retention_days"] == 14.0


def test_unknown_or_malformed_retention_is_a_usage_error(home: Path) -> None:
    for bad in ("nonsense=3", "scratch", "scratch=soon", "scratch=-1"):
        with pytest.raises(ValueError):
            reaper._parse_retention([bad])
    assert reaper._parse_retention(["scratch=7"]) == {"scratch": 7.0}

    usage = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.retention_reaper",
            "--home",
            str(home),
            "--retention",
            "nonsense=3",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert usage.returncode == 2
    assert "unknown retention class" in usage.stderr


# --------------------------------------------------------------------------- #
# c3 — release-watch runs survive while an outcome mentions them
# --------------------------------------------------------------------------- #


def test_release_watch_run_is_kept_when_an_outcome_mentions_it(home: Path) -> None:
    now = time.time()
    runs = home / "release-watch" / "runs"
    outcomes = home / "release-watch" / "outcomes"
    outcomes.mkdir(parents=True)
    referenced_body = "a" * 40
    referenced_name = "b" * 40
    orphan = "c" * 40
    recent = "d" * 40
    for name, days in (
        (referenced_body, FAR_PAST),
        (referenced_name, FAR_PAST),
        (orphan, FAR_PAST),
        (recent, INSIDE),
    ):
        _make_dir(runs, name, now=now, days=days, payload=b"log" * 100)

    # one outcome mentions a run in its BODY, another by its FILE NAME.
    _make_file(
        outcomes,
        "verdict.json",
        now=now,
        days=None,
        payload=json.dumps({"runs": [f"release-watch/runs/{referenced_body}/watch.log"]}).encode(),
    )
    _make_file(outcomes, f"{referenced_name}.json", now=now, days=None, payload=b'{"ok": true}')

    dry = reaper.run(home, now=now)
    class_report = dry["classes"]["release-watch-runs"]
    assert _decisions(class_report) == {
        f"release-watch/runs/{referenced_body}": "kept",
        f"release-watch/runs/{referenced_name}": "kept",
        f"release-watch/runs/{orphan}": "would-delete",
        f"release-watch/runs/{recent}": "kept",
    }
    assert _reasons(class_report)[f"release-watch/runs/{referenced_body}"] == "referenced-by-outcome"
    assert _reasons(class_report)[f"release-watch/runs/{referenced_name}"] == "referenced-by-outcome"
    assert _reasons(class_report)[f"release-watch/runs/{recent}"] == "within-retention"

    report = reaper.run(home, now=now, delete=True)
    assert (runs / referenced_body).is_dir(), "a run an outcome talks about must survive"
    assert (runs / referenced_name).is_dir()
    assert not (runs / orphan).exists(), "an expired, unmentioned run is the whole point"
    assert (runs / recent).is_dir()

    # The marker the brief names is visible in both renderings.
    class_report = report["classes"]["release-watch-runs"]
    assert {
        record["reason"]
        for record in class_report["kept"]
        if record["path"].endswith(referenced_body)
    } == {"referenced-by-outcome"}
    assert "referenced-by-outcome" in reaper.render_text(report)


def test_release_watch_run_is_kept_when_outcomes_cannot_be_read(home: Path, monkeypatch) -> None:
    """An unreadable outcome is not evidence of absence: keep the run."""
    now = time.time()
    runs = home / "release-watch" / "runs"
    run = "e" * 40
    _make_dir(runs, run, now=now, days=FAR_PAST)
    _make_file(home / "release-watch" / "outcomes", "broken.json", now=now, days=None, payload=b"{}")

    real_read_bytes = Path.read_bytes

    def _explode(self: Path) -> bytes:
        if "outcomes" in self.parts:
            raise OSError("permission denied (simulated)")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _explode)

    report = reaper.run(home, now=now, delete=True)
    class_report = report["classes"]["release-watch-runs"]
    assert (runs / run).is_dir()
    assert class_report["refused_count"] == 0
    assert {record["reason"] for record in class_report["kept"]} == {"outcome-unreadable"}


# --------------------------------------------------------------------------- #
# c5 — outcomes cite PATHS: the cited subtree stays, the rest of the run goes
# --------------------------------------------------------------------------- #

#: The three run ids of the c5 fixture (fixed so the report order is fixed too).
CITED, WHOLE, SPENT = "1" * 40, "2" * 40, "3" * 40


def _cited_runs_fixture(home: Path, now: float) -> dict[str, Path]:
    """One run cited BELOW itself, one cited at its directory, one spent.

    Every run looks like the measured shape on this machine: a per-segment
    ``<segment>/demo`` directory holding the verdict, with the raw ``<segment>/raw``
    logs next to it. The outcome cites one ``demo`` subtree and one bare run
    directory, and says nothing at all about the third run.
    """
    runs = home / "release-watch" / "runs"
    outcomes = home / "release-watch" / "outcomes"
    outcomes.mkdir(parents=True)
    created = {
        "cited": _make_run(
            runs,
            CITED,
            now=now,
            days=FAR_PAST,
            files={
                "seg-a/demo/verdict.bin": b"v" * 4096,
                "seg-a/raw/watch.log": b"r" * 8192,
                "seg-b/demo/shots.bin": b"s" * 1024,
            },
        ),
        "whole": _make_run(
            runs,
            WHOLE,
            now=now,
            days=FAR_PAST,
            files={
                "seg-a/demo/verdict.bin": b"p" * 2048,
                "seg-a/raw/watch.log": b"t" * 512,
            },
        ),
        "spent": _make_run(
            runs,
            SPENT,
            now=now,
            days=FAR_PAST,
            files={"seg-a/demo/verdict.bin": b"e" * 7000},
        ),
    }
    _make_file(
        outcomes,
        "verdict.json",
        now=now,
        days=None,
        payload=json.dumps(
            {
                "verdicts": [
                    f"release-watch/runs/{CITED}/seg-a/demo",
                    f"release-watch/runs/{WHOLE}",
                ]
            }
        ).encode(),
    )
    return created


def test_a_cited_subtree_is_kept_and_the_rest_of_the_run_is_reclaimable(home: Path) -> None:
    now = time.time()
    created = _cited_runs_fixture(home, now)
    cited_rel = f"release-watch/runs/{CITED}"
    demo_rel = f"{cited_rel}/seg-a/demo"
    demo_bytes = _bytes_kept_with(created["cited"] / "seg-a" / "demo")
    run_bytes = _bytes_below(created["cited"])
    assert demo_bytes < run_bytes, "the fixture must have bytes outside the citation"

    report = reaper.run(home, now=now)
    class_report = report["classes"]["release-watch-runs"]

    # (a) the cited run keeps exactly the subtree the outcome points at ...
    kept = [record for record in class_report["kept"] if record["path"] == cited_rel]
    assert len(kept) == 1, class_report["kept"]
    assert kept[0]["bytes"] == demo_bytes, "a cited subtree keeps only its own bytes"
    assert kept[0]["status"] == "kept"
    assert kept[0]["reason"] == "referenced-by-outcome"
    assert kept[0]["kept_by"] == [demo_rel], "the kept record must name the citation"

    # ... and the REST of that run is reclaimable, with a reason.
    rest = [record for record in class_report["candidates"] if record["path"] == cited_rel]
    assert len(rest) == 1, class_report["candidates"]
    assert rest[0]["status"] == "would-delete"
    assert rest[0]["reason"] == "uncited-remainder"
    assert rest[0]["bytes"] == run_bytes - demo_bytes
    assert rest[0]["kept_bytes"] == demo_bytes
    assert rest[0]["kept_by"] == [demo_rel]

    # (b) a run cited only at its OWN directory keeps every byte, as before, and
    # the record names the citation that kept it.
    whole_rel = f"release-watch/runs/{WHOLE}"
    whole_kept = [record for record in class_report["kept"] if record["path"] == whole_rel]
    assert len(whole_kept) == 1
    assert whole_kept[0]["reason"] == "referenced-by-outcome"
    assert whole_kept[0]["kept_by"] == [whole_rel], "the record must name what kept it"
    assert whole_kept[0]["bytes"] == _bytes_below(created["whole"])
    assert not [record for record in class_report["candidates"] if record["path"] == whole_rel]

    # The pass that really deletes takes the uncited remainder and nothing else.
    reaper.run(home, now=now, delete=True)
    assert (created["cited"] / "seg-a" / "demo" / "verdict.bin").read_bytes() == b"v" * 4096
    assert not (created["cited"] / "seg-a" / "raw").exists()
    assert not (created["cited"] / "seg-b").exists()
    assert (created["whole"] / "seg-a" / "raw" / "watch.log").exists()
    assert not created["spent"].exists()


def test_release_watch_report_reconciles_kept_reclaimable_and_deleted_bytes(
    home: Path, tmp_path: Path
) -> None:
    now = time.time()
    created = _cited_runs_fixture(home, now)
    runs = home / "release-watch" / "runs"
    class_total = sum(_bytes_below(runs / name) for name in (CITED, WHOLE, SPENT))
    cited_rel = f"release-watch/runs/{CITED}"
    demo_rel = f"{cited_rel}/seg-a/demo"
    demo_bytes = _bytes_kept_with(created["cited"] / "seg-a" / "demo")
    whole_bytes = _bytes_below(created["whole"])

    report_path = tmp_path / "reports" / "reaper.json"
    printed = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy.retention_reaper",
            "--home",
            str(home),
            "--json",
            str(report_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert printed.returncode == 0, printed.stderr
    doc = json.loads(report_path.read_text())["classes"]["release-watch-runs"]

    # The --json document states all three quantities, measured at the start.
    assert doc["total_bytes"] == class_total
    assert doc["kept_bytes"] + doc["reclaimable_bytes"] + doc["deleted_bytes"] == doc["total_bytes"]
    assert doc["deleted_bytes"] == 0, "a dry run deletes nothing"
    assert doc["kept_bytes"] == demo_bytes + whole_bytes
    assert doc["reclaimable_bytes"] == class_total - doc["kept_bytes"]
    # Kept bytes name the cited path that kept them; reclaimable bytes carry a reason.
    assert [
        record["kept_by"] for record in doc["kept"] if record["path"] == cited_rel
    ] == [[demo_rel]]
    assert [(record["path"], record["reason"]) for record in doc["candidates"]] == [
        (cited_rel, "uncited-remainder"),
        (f"release-watch/runs/{SPENT}", "expired"),
    ]
    # No entry counted twice: the records partition the class total byte for byte.
    records = [record for key in ("kept", "candidates", "deleted") for record in doc[key]]
    assert sum(record["bytes"] for record in records) == class_total
    assert len({record["path"] for record in doc["kept"]}) == len(doc["kept"])

    # ... and the human text states the same three numbers and the same citation.
    text = printed.stdout
    assert f"total={doc['total_bytes']} B" in text
    assert f"kept={doc['kept_count']} ({doc['kept_bytes']} B)" in text
    assert f"reclaimable={doc['candidate_count']} ({doc['reclaimable_bytes']} B)" in text
    assert f"deleted={doc['deleted_count']} ({doc['deleted_bytes']} B)" in text
    assert f"kept-by={demo_rel}" in text
    assert "uncited-remainder" in text

    # The same identity holds in the pass that really deletes: the reclaimed bytes
    # move to ``deleted`` and never sit in two columns at once.
    deleted = reaper.run(home, now=now, delete=True)["classes"]["release-watch-runs"]
    assert deleted["total_bytes"] == class_total
    assert (
        deleted["kept_bytes"] + deleted["reclaimable_bytes"] + deleted["deleted_bytes"]
        == deleted["total_bytes"]
    )
    assert deleted["kept_bytes"] == demo_bytes + whole_bytes
    assert deleted["reclaimable_bytes"] == 0
    assert deleted["deleted_bytes"] == class_total - deleted["kept_bytes"]
    assert not created["spent"].exists()


# --------------------------------------------------------------------------- #
# c4 — the immune set never becomes a candidate
# --------------------------------------------------------------------------- #


IMMUNE_FIXTURES = (
    "state-report-evidence/old-run",
    "aitelier.db",
    "aitelier.db-wal",
    "artifacts/old-artifact",
    "dsh-drivers/dsh-aaaaaaaaaaaa/sessions/old-session",
    "dsh-drivers/dsh-aaaaaaaaaaaa/todo/old-plan",
)


def _seed_immune(home: Path, now: float) -> dict[str, Path]:
    seeded: dict[str, Path] = {}
    for rel in IMMUNE_FIXTURES:
        if rel.endswith((".db", ".db-wal", ".db-shm")):
            seeded[rel] = _make_file(home, rel, now=now, days=FAR_PAST, payload=b"sqlite bytes")
        else:
            seeded[rel] = _make_dir(home, rel, now=now, days=FAR_PAST, payload=b"live state")
    return seeded


def test_immune_state_survives_a_delete_run_with_zero_retention(home: Path) -> None:
    now = time.time()
    immune = _seed_immune(home, now)
    control = _make_dir(home / "scratch", "expired-work", now=now, days=FAR_PAST)

    zero = {cls.name: 0.0 for cls in reaper.MANAGED_CLASSES}
    report = reaper.run(home, now=now, delete=True, retention=zero)

    for rel, path in immune.items():
        assert path.exists(), f"{rel} is in the immune set and must never be deleted"

    # The deletion really ran: the expired scratch entry is gone.
    assert not control.exists()
    assert report["classes"]["scratch"]["deleted_count"] == 1

    # ... and no record in the report is under an immune path — not even a refusal,
    # because they were never enumerated at all.
    reported = [
        record["path"]
        for class_report in report["classes"].values()
        for record in _records(class_report)
    ]
    assert reported, "the report is empty — this witness would be vacuous"
    for rel in reported:
        assert not _independent(rel), rel


def test_the_only_deletions_are_under_a_managed_root(home: Path, monkeypatch) -> None:
    """Every path the single deletion primitive sees came from a managed class root."""
    now = time.time()
    _seed_immune(home, now)
    _make_dir(home / "scratch", "expired-work", now=now, days=FAR_PAST)
    _make_dir(home / "godot-control" / "evidence", "f" * 32, now=now, days=FAR_PAST)
    _make_dir(home / "worktrees-scratch", "wt", now=now, days=FAR_PAST)

    removed: list[str] = []
    real_remove = reaper._remove

    def _spy(path: Path) -> None:
        removed.append(path.relative_to(home).as_posix())
        real_remove(path)

    monkeypatch.setattr(reaper, "_remove", _spy)
    zero = {cls.name: 0.0 for cls in reaper.MANAGED_CLASSES}
    reaper.run(home, now=now, delete=True, retention=zero)

    managed_roots = tuple(cls.rel_root for cls in reaper.MANAGED_CLASSES)
    assert set(removed) == {"scratch/expired-work", "godot-control/evidence/" + "f" * 32, "worktrees-scratch/wt"}
    for rel in removed:
        assert any(rel == root or rel.startswith(root + "/") for root in managed_roots), rel
        assert not _independent(rel), rel


def test_the_immune_set_is_out_of_reach_not_switched_off() -> None:
    """Structural half of c4: no managed root and no import can reach the immune set."""
    for cls in reaper.MANAGED_CLASSES:
        assert not _independent(cls.rel_root), cls.name
        assert not reaper.is_immune(cls.rel_root), cls.name

    # The module's own pattern list and an independent spelling must agree.
    for rel in IMMUNE_FIXTURES + ("dsh-drivers/dsh-1/sessions/s-1", "dsh-drivers/dsh-1/todo/t-1"):
        assert reaper.is_immune(rel), rel
        assert _independent(rel), rel
    for rel in ("scratch/old", "godot-control/evidence/" + "a" * 32):
        assert not reaper.is_immune(rel), rel
        assert not _independent(rel), rel

    source = (REPO_ROOT / "deploy" / "retention_reaper.py").read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "core" not in imported, "the reaper must run on the host without core/*"
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)

    # One deletion primitive, and its only call site is the planning loop.
    remove_callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_remove"
    ]
    assert len(remove_callers) == 1, "expected exactly one call site for _remove"
    enclosing = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(call in ast.walk(node) for call in remove_callers)
    ]
    assert enclosing == ["_plan_class"], enclosing
    assert source.count("shutil.rmtree(") == 1
    assert source.count("os.unlink(") == 1
