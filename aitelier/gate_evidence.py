"""Current-cycle identity and fail-closed audit for deterministic gate reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

CYCLE_FIELD = "evidence_cycle_id"
RUN_FIELD = "run_id"
CYCLE_MANIFEST = ".evidence_cycle.json"
_SKIP_MARKERS = ("gate_skipped", "skipped", "skipped_because", "blind",
                 "no_tests_collected", "collection_errors")
_UNRUN_STATUSES = {"not_run", "not-run", "not run", "unrun", "unexecuted"}


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("report is not a JSON object")
    return data


def _manifest_path(graph_dir: Path) -> Path:
    return Path(graph_dir) / CYCLE_MANIFEST


def _write_cycle_manifest(graph_dir: Path, *, run_id: str, cycle: str,
                          generated_at: str) -> None:
    """Atomically publish the current cycle outside every audited report."""
    path = _manifest_path(graph_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{uuid4().hex}")
    tmp.write_text(json.dumps({
        "version": 1,
        RUN_FIELD: run_id,
        CYCLE_FIELD: cycle,
        "evidence_generated_at": generated_at,
    }, indent=2), encoding="utf-8")
    tmp.replace(path)


def _current_cycle(graph_dir: Path, run_id: str) -> str:
    if not run_id:
        raise ValueError("current run_id is empty")
    manifest = _read_json(_manifest_path(graph_dir))
    cycle = manifest.get(CYCLE_FIELD)
    if manifest.get(RUN_FIELD) != run_id or not isinstance(cycle, str) or not cycle:
        raise ValueError("cycle manifest is not from this run/current cycle")
    return cycle


def stamp_report(report: dict, *, run_id: str, out_dir: str,
                 start_cycle: bool = False, cycle_from: str = "5_test",
                 cycle_file: str = "test_report.json") -> dict:
    """Stamp a report from an independent run-scoped cycle manifest."""
    if not isinstance(report, dict):
        return report
    generated_at = datetime.now(timezone.utc).isoformat()
    report[RUN_FIELD] = run_id
    report["evidence_generated_at"] = generated_at
    graph_dir = Path(out_dir).parent
    if start_cycle:
        cycle = f"{run_id}:{uuid4().hex}"
        report[CYCLE_FIELD] = cycle
        _write_cycle_manifest(graph_dir, run_id=run_id, cycle=cycle,
                              generated_at=generated_at)
        return report

    # Kept in the signature for graph/tool compatibility. The old implementation
    # read cycle_from/cycle_file as evidence and thereby trusted an audited report
    # as its own freshness oracle. The manifest is the only current-cycle source.
    del cycle_from, cycle_file
    try:
        report[CYCLE_FIELD] = _current_cycle(graph_dir, run_id)
    except Exception as exc:
        report[CYCLE_FIELD] = ""
        report["passed"] = False
        report["skipped_because"] = "evidence_cycle_missing"
        report["evidence_stamp_error"] = f"{_manifest_path(graph_dir)}: {exc}"
    return report


def stamp_file(path: Path, *, run_id: str, out_dir: str,
               cycle_from: str = "5_test") -> None:
    """Stamp a report written by a chained tool. Never hides a gate failure."""
    path = Path(path)
    try:
        report = _read_json(path)
    except Exception as exc:
        report = {"passed": False, "skipped_because": "report_unreadable",
                  "summary": f"{path.name} could not be read after the gate: {exc}"}
    stamp_report(report, run_id=run_id, out_dir=out_dir,
                 cycle_from=cycle_from)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def audit_evidence(graph_dir: Path, run_id: str, gates: list) -> dict:
    """Audit gate reports against an independent current-cycle identity."""
    graph_dir = Path(graph_dir)
    verdict = {
        "run_id": run_id,
        CYCLE_FIELD: "",
        "state": "fresh",
        "passed": True,
        "stale_reports": [],
        "missing_reports": [],
        "skipped_gates": [],
        "gates_audited": [],
    }
    if not gates:
        verdict.update(state="audit_error", passed=False,
                       summary="No gate reports were declared; zero evidence cannot pass.")
        return verdict

    try:
        expected_cycle = _current_cycle(graph_dir, run_id)
        if not isinstance(expected_cycle, str) or not expected_cycle:
            raise ValueError("expected evidence cycle is empty")
        verdict[CYCLE_FIELD] = expected_cycle
    except Exception as exc:
        expected_cycle = ""
        verdict.update(state="stale", passed=False,
                       summary=f"Cannot establish current evidence cycle: {exc}")

    for pair in gates:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            entry = {"step": str(pair), "file": "", "state": "unreadable",
                     "passed": False, "skipped_because": "invalid_gate_declaration",
                     "summary": f"Invalid gate declaration: {pair!r}"}
        else:
            step_id, filename = map(str, pair)
            entry = _audit_one(graph_dir, step_id, filename, run_id,
                               expected_cycle)
        verdict["gates_audited"].append(entry)
        if entry["passed"]:
            continue
        verdict["passed"] = False
        bucket = ("stale_reports" if entry["state"] in ("stale", "unreadable")
                  else "missing_reports" if entry["state"] == "missing"
                  else "skipped_gates")
        verdict[bucket].append(entry)

    if not verdict["passed"]:
        verdict["state"] = ("stale" if verdict["stale_reports"]
                            else "missing" if verdict["missing_reports"]
                            else "skipped")
    return verdict


def _audit_one(graph_dir: Path, step_id: str, filename: str, run_id: str,
               expected_cycle: str) -> dict:
    path = graph_dir / step_id / filename
    entry = {"step": step_id, "file": filename, "state": "fresh",
             "passed": True, "skipped_because": None}
    if not path.is_file():
        entry.update(state="missing", passed=False,
                     skipped_because="report_missing",
                     summary=f"{step_id}/{filename} is absent; the gate did not produce current evidence")
        return entry
    try:
        report = _read_json(path)
    except Exception as exc:
        entry.update(state="unreadable", passed=False,
                     skipped_because="report_unreadable",
                     summary=f"{step_id}/{filename} is unreadable: {exc}")
        return entry

    entry[RUN_FIELD] = report.get(RUN_FIELD)
    entry[CYCLE_FIELD] = report.get(CYCLE_FIELD)
    if (not expected_cycle or report.get(RUN_FIELD) != run_id
            or report.get(CYCLE_FIELD) != expected_cycle):
        entry.update(state="stale", passed=False,
                     summary=(f"{step_id}/{filename} belongs to run/cycle "
                              f"{report.get(RUN_FIELD)!r}/{report.get(CYCLE_FIELD)!r}, "
                              f"expected {run_id!r}/{expected_cycle!r}"))
        return entry

    raw_passed = report.get("passed", report.get("all_passed", False))
    if raw_passed is not True and raw_passed is not False:
        entry.update(state="unreadable", passed=False,
                     skipped_because="invalid_passed_type",
                     summary=(f"{step_id}/{filename} has non-boolean pass status "
                              f"{raw_passed!r}"))
        return entry
    passed = raw_passed is True
    because = report.get("skipped_because")
    markers = [name for name in _SKIP_MARKERS if report.get(name)]
    status = str(report.get("status", "")).strip().lower()
    unrun = (report.get("unrun") is True or report.get("ran") is False
             or report.get("executed") is False or status in _UNRUN_STATUSES)
    if markers or unrun:
        passed = False
        because = because or ("gate_unrun" if unrun else "gate_skipped")
        entry["state"] = "skipped"
    entry.update(passed=passed, skipped_because=because,
                 gate_summary=str(report.get("summary", ""))[:400])
    if not passed and entry["state"] == "fresh":
        entry["state"] = "failed"
    return entry
