"""Audit all reports before the final verifier is allowed to render a verdict."""
import json
from pathlib import Path

from aitelier.gate_evidence import audit_evidence


def verify_evidence(*, out_dir: str = "", run_id: str = "", gates: str = "", **kwargs) -> dict:
    if not out_dir or not Path(out_dir).is_absolute():
        return {"written": None, "passed": False,
                "error": "verify_evidence requires absolute out_dir=$STEP_DIR"}
    try:
        declared = json.loads(gates)
    except (TypeError, json.JSONDecodeError) as exc:
        declared = []
        parse_error = str(exc)
    else:
        parse_error = ""
    verdict = audit_evidence(Path(out_dir).parent, run_id, declared)
    if parse_error:
        verdict.update(passed=False, state="audit_error",
                       summary=f"Gate declaration is unreadable: {parse_error}")
    target = Path(out_dir) / "evidence_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    return {"written": "evidence_report.json", "passed": verdict["passed"],
            "state": verdict["state"],
            "stale_reports": verdict["stale_reports"],
            "missing_reports": verdict["missing_reports"],
            "skipped_gates": verdict["skipped_gates"]}
