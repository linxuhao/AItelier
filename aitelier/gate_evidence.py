"""Current-cycle identity and fail-closed audit for deterministic gate reports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

CYCLE_FIELD = "evidence_cycle_id"
RUN_FIELD = "run_id"
_SKIP_MARKERS = ("gate_skipped", "skipped", "skipped_because", "blind",
                 "no_tests_collected", "collection_errors")


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("report is not a JSON object")
    return data


def stamp_report(report: dict, *, run_id: str, out_dir: str,
                 start_cycle: bool = False, cycle_from: str = "5_test",
                 cycle_file: str = "test_report.json") -> dict:
    """Stamp a report with this run and verification-cycle identity."""
    if not isinstance(report, dict):
        return report
    report[RUN_FIELD] = run_id
    report["evidence_generated_at"] = datetime.now(timezone.utc).isoformat()
    if start_cycle:
        report[CYCLE_FIELD] = f"{run_id}:{uuid4().hex}"
        return report

    source = Path(out_dir).parent / cycle_from / cycle_file
    try:
        seed = _read_json(source)
        cycle = seed.get(CYCLE_FIELD)
        if not cycle or seed.get(RUN_FIELD) != run_id:
            raise ValueError("source report is not from this run/current cycle")
        report[CYCLE_FIELD] = cycle
    except Exception as exc:
        report[CYCLE_FIELD] = ""
        report["passed"] = False
        report["skipped_because"] = "evidence_cycle_missing"
        report["evidence_stamp_error"] = f"{source}: {exc}"
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
    """Audit declared gate reports against the first gate's cycle identity."""
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

    first_step, first_file = gates[0]
    try:
        seed = _read_json(graph_dir / first_step / first_file)
        expected_cycle = seed.get(CYCLE_FIELD)
        if not expected_cycle or seed.get(RUN_FIELD) != run_id:
            raise ValueError("first gate report has no current-run cycle identity")
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

    passed = bool(report.get("passed", report.get("all_passed", False)))
    because = report.get("skipped_because")
    markers = [name for name in _SKIP_MARKERS if report.get(name)]
    if markers:
        passed = False
        because = because or "gate_skipped"
        entry["state"] = "skipped"
    entry.update(passed=passed, skipped_because=because,
                 gate_summary=str(report.get("summary", ""))[:400])
    if not passed and entry["state"] == "fresh":
        entry["state"] = "failed"
    return entry
