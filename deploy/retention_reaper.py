#!/usr/bin/env python3
"""Retention reaper for ``~/.AItelier``: report, and on request delete, aged-out growth.

WHY THIS EXISTS
---------------
``~/.AItelier`` had no reclamation mechanism at all, and it accumulated by
accident rather than by need. Measured 2026-11-16:

  * ``godot-control/evidence/<32hex>/`` — one proof directory per Godot sidecar
    tool call: 3276 directories, 38.37 GB, and nothing reads them back.
  * ``release-watch/runs/`` — 28 run directories, 13.23 GB. ``release-watch/outcomes/``
    holds 100 files that cite exactly 24 distinct paths, and every one of those paths
    is a DIRECTORY below a run (``release-watch/runs/<id>/<segment>/demo``): no outcome
    names a file and no outcome names a bare run directory. A citation keeps the
    subtree it points at — not the half-gigabyte run around it, which is what a bare
    "some outcome mentioned this run" test used to keep forever.
  * ``scratch/`` 4.55 GB and ``worktrees-scratch/`` 3.3 GB, never reclaimed.
  * the whole driver home 65 GB.

This module is the single operator tool for that: it enumerates the class roots
below, ages each entry by its OWN mtime, and reports what it would remove. The
owner's 2026-11-16 ruling for the Godot proof directories is a THREE day
retention, which is why that class does not share the 14 day default.

THE TWO RULES THAT ARE NOT NEGOTIABLE HERE
------------------------------------------
1. DRY RUN BY DEFAULT. Running the tool without ``--delete`` only reports. A
   destructive default on a data directory nobody has ever pruned would turn a
   typo into a data loss event; ``--delete`` is the operator saying so explicitly.
   ``--home`` is likewise required — there is deliberately no implicit
   ``~/.AItelier`` default, so a wrong invocation is an argparse error and never
   a silent deletion in whatever home happens to be current.

2. THE IMMUNE SET IS NOT A DEFAULT THAT CAN BE FLIPPED. ``state-report-evidence/``,
   ``aitelier.db*``, ``artifacts/``, ``dsh-drivers/*/sessions`` and
   ``dsh-drivers/*/todo`` are live state: the database, the artifact revision
   store, the DSH session transcripts and todo plans. They are protected
   STRUCTURALLY — no class root (see ``MANAGED_CLASSES``) reaches them, so there
   is no delete branch to disable; the only deletion primitive, ``_remove``, is
   called from the planning loop over candidates that were enumerated under a
   managed root. ``IMMUNE_PATTERNS`` exists to make that claim checkable (and is
   asserted in ``tests/unit/test_retention_reaper.py``), not to gate a branch
   that would otherwise delete them. Adding a class whose root overlaps the
   immune set is a bug this module refuses to run with (``_check_managed_roots``).

WHAT IS REFUSED RATHER THAN SKIPPED
-----------------------------------
A target that is a symbolic link, or whose ``realpath`` is not inside ``--home``,
is never followed and never deleted: it is counted and reported as ``refused``
with the reason. Skipping it silently is how a reaper ends up deleting a
symlinked scratch directory's *target*, or a mount, while the report reads clean.

CLASSES AND THEIR AGE SIGNAL
----------------------------
``godot-control/evidence/<32hex>/`` — default 3 days (owner ruling).
``release-watch/runs/<run>/``       — default 14 days, and only when
                                        ``release-watch/outcomes/`` does not cite the
                                        run. A run cited only BELOW itself
                                        (``runs/<id>/<segment>/demo``) keeps exactly
                                        the cited subtree as ``referenced-by-outcome``
                                        and offers the rest as
                                        ``uncited-remainder``; a run cited at its own
                                        directory, named by an outcome file name, or
                                        named only as a bare word is still kept whole.
``scratch/``                        — default 14 days.
``worktrees-scratch/``              — default 14 days.

Age is the entry's own mtime (``lstat``, never followed): for these classes the
directory is written last and its mtime is when the work stopped. ``--retention
<class>=<days>`` overrides one class per flag and can name a class more than
once (last wins).

THE THREE NUMBERS THAT HAVE TO RECONCILE
----------------------------------------
Each class reports ``total_bytes``, measured before anything is removed, split into
``kept_bytes``, ``reclaimable_bytes`` and ``deleted_bytes``. The three are DISJOINT and
add back up to ``total_bytes``: a partially cited run appears once as kept bytes (the
cited subtree) and once as reclaimable bytes (the rest), never with the same byte in
both. Refused entries (symlinks, paths outside ``--home``) carry no bytes in that sum;
they are listed under ``refused`` and are the only entries the sum leaves out.

USAGE
-----
    python -m deploy.retention_reaper --home ~/.AItelier
    python -m deploy.retention_reaper --home ~/.AItelier --json /tmp/reaper.json
    python -m deploy.retention_reaper --home ~/.AItelier --retention scratch=7 --delete

Standard library only, and it imports nothing from ``core``: it has to run on the
host, outside the container, against a directory the server may be using.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "IMMUNE_PATTERNS",
    "ManagedClass",
    "MANAGED_CLASSES",
    "is_immune",
    "run",
    "main",
]

SECONDS_PER_DAY = 86400.0

#: Home-relative paths this tool must never delete, expressed as fnmatch patterns
#: (a directory pattern also gets a ``/*`` companion so its contents match too).
#: See the module docstring: this is a checkable statement of the design, not a
#: switch guarding a delete branch.
IMMUNE_PATTERNS: tuple[str, ...] = (
    "state-report-evidence",
    "state-report-evidence/*",
    "aitelier.db*",
    "artifacts",
    "artifacts/*",
    "dsh-drivers/*/sessions",
    "dsh-drivers/*/sessions/*",
    "dsh-drivers/*/todo",
    "dsh-drivers/*/todo/*",
)


@dataclass(frozen=True)
class ManagedClass:
    """One reclaimable class: where it lives, how long it is kept, how it is aged."""

    name: str
    rel_root: str
    default_days: float
    #: True when an expired entry must also be unmentioned by release-watch outcomes.
    outcome_referenced: bool = False


MANAGED_CLASSES: tuple[ManagedClass, ...] = (
    ManagedClass("godot-evidence", "godot-control/evidence", 3.0),
    ManagedClass("release-watch-runs", "release-watch/runs", 14.0, outcome_referenced=True),
    ManagedClass("scratch", "scratch", 14.0),
    ManagedClass("worktrees-scratch", "worktrees-scratch", 14.0),
)

CLASS_BY_NAME: dict[str, ManagedClass] = {cls.name: cls for cls in MANAGED_CLASSES}

def is_immune(rel_posix: str) -> bool:
    """True when a home-relative POSIX path falls in the never-delete set."""
    rel = rel_posix.strip("/")
    if not rel:
        return False
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in IMMUNE_PATTERNS)


def _check_managed_roots() -> None:
    """Refuse to run if any class root overlaps the immune set.

    A class root inside the immune set would be a delete branch that reaches live
    state, which is exactly what this module is written not to have. It is a
    programming error, so it raises instead of quietly pruning nothing.
    """
    for cls in MANAGED_CLASSES:
        if is_immune(cls.rel_root) or is_immune(cls.rel_root + "/x"):
            raise RuntimeError(
                f"managed class {cls.name!r} roots at {cls.rel_root!r}, which is in the "
                f"immune set {IMMUNE_PATTERNS!r}"
            )


# --------------------------------------------------------------------------- #
# filesystem helpers
# --------------------------------------------------------------------------- #


def _within_home(path: Path, home_real: Path) -> bool:
    """True when ``path`` resolves to ``home_real`` or below it."""
    try:
        resolved = Path(os.path.realpath(path))
    except OSError:
        return False
    return resolved == home_real or home_real in resolved.parents


def _refusal_reason(path: Path, home_real: Path) -> str | None:
    """Why ``path`` must not be deleted, or None when it may be considered.

    Escaping the home wins over being a symlink: a symlink pointing outside
    ``--home`` is a path-escape first, and the report should name the larger
    problem. A symlink that stays inside home is still refused — the reaper
    deletes what it enumerated, never whatever a link happens to point at.
    """
    if not _within_home(path, home_real):
        return "outside-home"
    if path.is_symlink():
        return "symlink"
    return None


def _tree_bytes(path: Path) -> int:
    """Sum of ``lstat().st_size`` below ``path`` (symlinks counted, not followed)."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _entry_bytes(path: Path) -> int:
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if os.path.isdir(path) and not os.path.islink(path):
        return _tree_bytes(path)
    return st.st_size


def _subtree_bytes(path: Path) -> int:
    """Bytes that stay when ``path`` is retained: the entry itself plus all below it.

    ``_tree_bytes`` counts what is *below* a directory, which is the right measure for
    an entry the reaper may remove wholesale (the directory inode goes with it). A
    subtree that is KEPT also keeps the directory inode, so this adds it — otherwise a
    partially cited run's remainder would silently carry the kept directory's own size
    and the class total would not add up.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if os.path.isdir(path) and not os.path.islink(path):
        return st.st_size + _tree_bytes(path)
    return st.st_size


def _remainders(root: Path, kept: Iterable[Path]) -> list[Path]:
    """Topmost paths below ``root`` that can go without touching any of ``kept``.

    ``kept`` are the cited subtrees that stay. A child that CONTAINS a kept path is
    not itself a removal target: it is descended into, so its other children can be.
    This names what the one deletion primitive would be handed; it decides nothing.
    """
    kept_paths = tuple(kept)
    targets: list[Path] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return targets
    for child in children:
        if any(child == other or child in other.parents for other in kept_paths):
            # ``child`` is a cited subtree (kept whole) or holds one (descent needed).
            if child in kept_paths:
                continue
            targets.extend(_remainders(child, kept_paths))
        else:
            targets.append(child)
    return targets


def _age_days(mtime: float, now: float) -> float:
    return max(0.0, (now - mtime) / SECONDS_PER_DAY)


def _remove(path: Path) -> None:
    """The one deletion primitive: a directory tree or a single entry."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        os.unlink(path)


# --------------------------------------------------------------------------- #
# release-watch outcomes index
# --------------------------------------------------------------------------- #


#: A run path as outcomes write it: ``release-watch/runs/<id>`` with any path below it.
#: The measured shape (2026-11-16) is ``release-watch/runs/<id>/<segment>/demo`` in all
#: 24 cited paths of 100 outcome files; the optional tail is kept general but never
#: guessed — what is not in the text is not a citation.
_RUN_PATH_RE = re.compile(
    r"release-watch/runs/(?P<run>[A-Za-z0-9._-]+)"
    r"(?P<below>(?:/[^\s\"'`,;:()\[\]{}<>\\|*?]+)*)"
)


def _cited_suffix(below: str) -> str:
    """Run-relative suffix of a cited path; ``""`` when the run dir itself is cited."""
    suffix = below.strip("/")
    suffix = re.sub(r"[.,;:!?]+$", "", suffix)  # a sentence-ending dot is not a segment
    return suffix.strip("/")


def _citations(blob: str) -> dict[str, set[str]]:
    """Every ``release-watch/runs/<run>[/<below>]`` path the outcome text names."""
    found: dict[str, set[str]] = {}
    for match in _RUN_PATH_RE.finditer(blob):
        found.setdefault(match.group("run"), set()).add(_cited_suffix(match.group("below")))
    return found


@dataclass
class OutcomeIndex:
    """Everything ``release-watch/outcomes/`` says, searchable by run dir name."""

    blob: str = ""
    complete: bool = True
    files: int = 0
    unreadable: list[str] = field(default_factory=list)
    #: run directory name -> the run-relative path suffixes outcomes cite below it
    #: (``""`` means the run directory itself was cited).
    citations: dict[str, set[str]] = field(default_factory=dict)

    def mentions(self, name: str) -> bool:
        return name in self.blob

    def cites(self, name: str) -> tuple[str, ...]:
        """The cited run-relative suffixes for ``name``, sorted (empty when none)."""
        return tuple(sorted(self.citations.get(name, ())))


def _read_outcomes(outcomes_dir: Path) -> OutcomeIndex:
    """Read outcomes once; a run is 'referenced' if any file path or body says its name.

    The scan is deliberately conservative: an unreadable outcome file sets
    ``complete = False`` and callers keep expired runs instead of deleting them,
    because "I could not read the evidence" must not read as "there is none".
    """
    index = OutcomeIndex()
    if not outcomes_dir.exists():
        return index
    chunks: list[str] = []
    for dirpath, dirnames, filenames in os.walk(outcomes_dir, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            index.files += 1
            try:
                rel = path.relative_to(outcomes_dir).as_posix()
            except ValueError:
                rel = path.as_posix()
            chunks.append(rel)
            try:
                chunks.append(path.read_bytes().decode("utf-8", "replace"))
            except OSError:
                index.complete = False
                index.unreadable.append(str(path))
    index.blob = "\n\u0000\n".join(chunks)
    index.citations = _citations(index.blob)
    return index


@dataclass(frozen=True)
class _Claim:
    """What ``release-watch/outcomes`` claims about ONE run directory."""

    #: True when the whole run directory is claimed and stays exactly as it is today.
    whole: bool
    #: Home-relative cited paths that keep bytes here (what the report names as kept-by).
    kept_by: tuple[str, ...]
    #: Bytes under those cited paths.
    kept_bytes: int
    #: The cited subtrees themselves, for carving the remainder out of the run.
    kept_paths: tuple[Path, ...] = ()
    #: Why this is the decision, when the reason column alone is too terse.
    detail: str | None = None


def _claim_for(
    outcomes: OutcomeIndex,
    run_name: str,
    run_rel: str,
    path: Path,
    size: int,
) -> _Claim | None:
    """Decide what the outcomes claim about one run, or None when nothing cites it.

    Three shapes, in increasing precision, and each one is only as aggressive as the
    evidence: a bare name mention keeps the whole run (as before this change), a
    citation of the run directory or of a single file inside it keeps the whole run
    too (the reaper does not carve a run around one file), and a citation of a
    directory BELOW the run keeps exactly that subtree.
    """
    suffixes = outcomes.cites(run_name)
    if not suffixes:
        if outcomes.mentions(run_name):
            return _Claim(
                whole=True,
                kept_by=(run_rel,),
                kept_bytes=size,
                detail="run directory named by an outcome, with no path below it",
            )
        return None
    if "" in suffixes:
        return _Claim(
            whole=True,
            kept_by=(run_rel,),
            kept_bytes=size,
            detail="an outcome cites the run directory itself",
        )

    kept_paths: list[Path] = []
    kept_by: list[str] = []
    for suffix in sorted(suffixes, key=lambda s: (s.count("/"), s)):
        target = path / suffix
        if target.is_symlink() or not target.exists():
            continue  # cited but absent here: it keeps no bytes in this run
        if not target.is_dir():
            # A claim on a single FILE. Keep the whole run rather than carve around it:
            # the measured shape never does this, and half a run is not evidence.
            return _Claim(
                whole=True,
                kept_by=(f"{run_rel}/{suffix}",),
                kept_bytes=size,
                detail="an outcome cites a single file inside the run",
            )
        if any(target == other or other in target.parents for other in kept_paths):
            continue  # already covered by a cited parent path
        kept_paths.append(target)
        kept_by.append(f"{run_rel}/{suffix}")

    if not kept_paths:
        return _Claim(
            whole=True,
            kept_by=(run_rel,),
            kept_bytes=size,
            detail="outcomes cite only paths that are absent under this run",
        )
    return _Claim(
        whole=False,
        kept_by=tuple(kept_by),
        kept_bytes=sum(_subtree_bytes(target) for target in kept_paths),
        kept_paths=tuple(kept_paths),
        detail="outcomes cite only these paths below the run",
    )


# --------------------------------------------------------------------------- #
# planning / execution
# --------------------------------------------------------------------------- #


def _record(entry: str, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"path": entry, "status": status, "reason": reason}
    record.update(extra)
    return record


def _plan_class(
    cls: ManagedClass,
    home: Path,
    home_real: Path,
    days: float,
    now: float,
    delete: bool,
    outcomes: OutcomeIndex | None,
) -> dict[str, Any]:
    """Enumerate one class, decide per entry, and delete the candidates when asked."""
    root = home / cls.rel_root
    report: dict[str, Any] = {
        "root": cls.rel_root,
        "retention_days": days,
        "present": root.exists() or root.is_symlink(),
        "total_bytes": 0,
        "candidate_count": 0,
        "candidate_bytes": 0,
        "reclaimable_bytes": 0,
        "deleted_count": 0,
        "deleted_bytes": 0,
        "kept_count": 0,
        "kept_bytes": 0,
        "refused_count": 0,
        "candidates": [],
        "deleted": [],
        "kept": [],
        "refused": [],
        "errors": [],
    }
    if not report["present"]:
        return report

    reason = _refusal_reason(root, home_real)
    if reason is not None:
        detail = os.path.realpath(root) if reason == "outside-home" else os.readlink(root)
        refusal = _record(cls.rel_root, "refused", reason, detail=detail)
        report["refused"].append(refusal)
        report["refused_count"] = 1
        return report

    try:
        with os.scandir(root) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
    except OSError as exc:
        report["errors"].append(f"cannot list {cls.rel_root}: {exc}")
        return report

    for entry in entries:
        path = Path(entry.path)
        rel = path.relative_to(home).as_posix()
        why = _refusal_reason(path, home_real)
        if why is not None:
            detail = os.path.realpath(path) if why == "outside-home" else os.readlink(path)
            report["refused"].append(_record(rel, "refused", why, detail=detail))
            report["refused_count"] += 1
            continue

        try:
            mtime = os.lstat(path).st_mtime
        except OSError as exc:
            report["refused"].append(
                _record(rel, "refused", "stat-failed", detail=str(exc))
            )
            report["refused_count"] += 1
            continue

        size = _entry_bytes(path)
        report["total_bytes"] += size
        age = _age_days(mtime, now)
        measured = {"age_days": round(age, 2), "bytes": size}

        if age <= days:
            report["kept"].append(_record(rel, "kept", "within-retention", **measured))
            report["kept_count"] += 1
            report["kept_bytes"] += size
            continue

        claim: _Claim | None = None
        if cls.outcome_referenced and outcomes is not None:
            if not outcomes.complete:
                report["kept"].append(
                    _record(
                        rel,
                        "kept",
                        "outcome-unreadable",
                        detail="could not read release-watch/outcomes/*; keeping",
                        **measured,
                    )
                )
                report["kept_count"] += 1
                report["kept_bytes"] += size
                continue
            claim = _claim_for(outcomes, entry.name, rel, path, size)
            if claim is not None and claim.whole:
                report["kept"].append(
                    _record(
                        rel,
                        "kept",
                        "referenced-by-outcome",
                        kept_by=list(claim.kept_by),
                        detail=claim.detail,
                        **measured,
                    )
                )
                report["kept_count"] += 1
                report["kept_bytes"] += size
                continue
            if claim is not None:
                # A citation BELOW the run keeps exactly the cited subtree; the rest of
                # the run is reclaimable. The two records are DISJOINT in bytes — the
                # kept subtree plus the remainder is the entry size measured above — so
                # the class total still reconciles and no byte is counted twice.
                report["kept"].append(
                    _record(
                        rel,
                        "kept",
                        "referenced-by-outcome",
                        age_days=round(age, 2),
                        bytes=claim.kept_bytes,
                        kept_by=list(claim.kept_by),
                        detail=claim.detail,
                    )
                )
                report["kept_count"] += 1
                report["kept_bytes"] += claim.kept_bytes

        # ---- what is left of this entry is a candidate -------------------------- #
        if claim is not None:
            reclaimable = size - claim.kept_bytes
            targets = _remainders(path, claim.kept_paths)
            reason = "uncited-remainder"
            details: dict[str, Any] = {
                "kept_bytes": claim.kept_bytes,
                "kept_by": list(claim.kept_by),
            }
        else:
            reclaimable = size
            targets = [path]
            reason = "expired"
            details = {}
        if reclaimable <= 0:
            continue

        report["candidate_count"] += 1
        report["candidate_bytes"] += reclaimable
        accounting = {"age_days": round(age, 2), "bytes": reclaimable, **details}

        if not delete:
            report["reclaimable_bytes"] += reclaimable
            report["candidates"].append(_record(rel, "would-delete", reason, **accounting))
            continue

        failures: list[str] = []
        for target in targets:  # the one deletion primitive, at its one call site
            try:
                _remove(target)
            except OSError as exc:
                failures.append(f"{target.name}: {exc}")
        if failures:
            report["kept"].append(
                _record(rel, "kept", "delete-failed", detail="; ".join(failures), **accounting)
            )
            report["kept_count"] += 1
            report["kept_bytes"] += reclaimable
            continue
        report["deleted"].append(_record(rel, "deleted", reason, **accounting))
        report["deleted_count"] += 1
        report["deleted_bytes"] += reclaimable

    return report


def _parse_retention(values: Iterable[str]) -> dict[str, float]:
    """Turn ``["class=days", ...]`` into a per-class override map."""
    overrides: dict[str, float] = {}
    for raw in values:
        name, sep, days_text = raw.partition("=")
        name = name.strip()
        if not sep or not name or not days_text.strip():
            raise ValueError(f"--retention expects <class>=<days>, got {raw!r}")
        if name not in CLASS_BY_NAME:
            raise ValueError(
                f"unknown retention class {name!r}; known classes: "
                + ", ".join(sorted(CLASS_BY_NAME))
            )
        try:
            days = float(days_text)
        except ValueError:
            raise ValueError(f"retention for {name!r} is not a number: {days_text!r}")
        if days < 0 or days != days:  # negative or NaN
            raise ValueError(f"retention for {name!r} must be a non-negative number of days")
        overrides[name] = days
    return overrides


def run(
    home: str | os.PathLike[str],
    *,
    delete: bool = False,
    retention: dict[str, float] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Plan (and, when ``delete``, execute) one reclamation pass over ``home``."""
    _check_managed_roots()
    home_path = Path(home).expanduser()
    if not home_path.is_dir():
        raise ValueError(f"--home is not a directory: {home_path}")
    home_real = Path(os.path.realpath(home_path))
    now = time.time() if now is None else now
    overrides = dict(retention or {})
    unknown = set(overrides) - set(CLASS_BY_NAME)
    if unknown:
        raise ValueError(f"unknown retention class(es): {sorted(unknown)}")

    outcomes = (
        _read_outcomes(home_path / "release-watch" / "outcomes")
        if any(cls.outcome_referenced for cls in MANAGED_CLASSES)
        else None
    )

    report: dict[str, Any] = {
        "home": str(home_path),
        "home_real": str(home_real),
        "mode": "delete" if delete else "dry-run",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "now": now,
        "immune": list(IMMUNE_PATTERNS),
        "classes": {},
        "totals": {},
    }

    totals = {
        "total_bytes": 0,
        "candidate_count": 0,
        "candidate_bytes": 0,
        "reclaimable_bytes": 0,
        "deleted_count": 0,
        "deleted_bytes": 0,
        "kept_count": 0,
        "kept_bytes": 0,
        "refused_count": 0,
    }
    for cls in MANAGED_CLASSES:
        days = overrides.get(cls.name, cls.default_days)
        class_report = _plan_class(cls, home_path, home_real, days, now, delete, outcomes)
        report["classes"][cls.name] = class_report
        for key in totals:
            totals[key] += class_report[key]

    report["totals"] = totals
    report["refused"] = [
        dict(record, klass=name)
        for name, class_report in report["classes"].items()
        for record in class_report["refused"]
    ]
    return report


def _render_record(record: dict[str, Any]) -> str:
    """One entry line; ``kept-by=`` names the citation whose bytes were kept."""
    line = "  {status:<12} {reason:<22} {age:>7.2f}d {bytes:>12} B  {path}".format(
        status=record["status"], reason=record["reason"],
        age=record.get("age_days", 0.0), bytes=record.get("bytes", 0),
        path=record["path"],
    )
    kept_by = record.get("kept_by") or []
    if kept_by:
        line += "  kept-by=" + ", ".join(kept_by)
    return line


def render_text(report: dict[str, Any]) -> str:
    """The operator-facing rendering of the same report ``--json`` writes."""
    headline = (
        "retention reaper -- {home}  ({mode})\n"
        "  candidate {candidate_count} entr(ies), {candidate_bytes} bytes; "
        "deleted {deleted_count} entr(ies), {deleted_bytes} bytes; "
        "kept {kept_count}; refused {refused_count}\n"
        "  accounted: total {total_bytes} B = kept {kept_bytes} B "
        "+ reclaimable {reclaimable_bytes} B + deleted {deleted_bytes} B\n"
    ).format(home=report["home"], mode=report["mode"], **report["totals"])
    if report["mode"] == "dry-run":
        headline += "  dry run: nothing was deleted (pass --delete to reclaim)\n"

    lines = [headline.rstrip("\n")]
    for name, class_report in report["classes"].items():
        lines.append(
            "\n[{name}] root={root} retention={days:g}d  total={total_bytes} B  "
            "kept={kept_count} ({kept_bytes} B)  "
            "reclaimable={candidate_count} ({reclaimable_bytes} B)  "
            "deleted={deleted_count} ({deleted_bytes} B)  "
            "refused={refused_count}".format(
                name=name,
                root=class_report["root"],
                days=class_report["retention_days"],
                total_bytes=class_report["total_bytes"],
                kept_count=class_report["kept_count"],
                kept_bytes=class_report["kept_bytes"],
                candidate_count=class_report["candidate_count"],
                reclaimable_bytes=class_report["reclaimable_bytes"],
                deleted_count=class_report["deleted_count"],
                deleted_bytes=class_report["deleted_bytes"],
                refused_count=class_report["refused_count"],
            )
        )
        if not class_report["present"]:
            lines.append("  absent (nothing at this path)")
            continue
        for record in class_report["candidates"] + class_report["deleted"]:
            lines.append(_render_record(record))
        for record in class_report["kept"]:
            lines.append(_render_record(record))
        for record in class_report["refused"]:
            lines.append(
                "  {status:<12} {reason:<22} {path}  ({detail})".format(
                    status=record["status"], reason=record["reason"],
                    path=record["path"], detail=record.get("detail", ""),
                )
            )
        for error in class_report["errors"]:
            lines.append(f"  error: {error}")
    if report["refused"]:
        lines.append(f"\nrefused total: {len(report['refused'])}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m deploy.retention_reaper",
        description=(
            "Report (and with --delete, remove) entries in a ~/.AItelier home that are "
            "past their retention. Dry run by default."
        ),
        epilog=(
            "classes: "
            + ", ".join(f"{cls.name} ({cls.default_days:g}d)" for cls in MANAGED_CLASSES)
            + ". Never touched: " + ", ".join(IMMUNE_PATTERNS[::2])
        ),
    )
    parser.add_argument(
        "--home",
        required=True,
        help="AItelier home to reap (required; there is no implicit default)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="actually delete candidates (without it the run is a dry run)",
    )
    parser.add_argument(
        "--retention",
        action="append",
        default=[],
        metavar="CLASS=DAYS",
        help="override one class's retention in days; repeatable",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help="write the same report to PATH as JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        overrides = _parse_retention(args.retention)
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("unreachable")  # parser.error exits
    try:
        report = run(args.home, delete=args.delete, retention=overrides)
    except ValueError as exc:
        print(f"retention_reaper: {exc}", file=sys.stderr)
        return 2

    print(render_text(report))
    if args.json_path:
        destination = Path(args.json_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"\nreport written to {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
