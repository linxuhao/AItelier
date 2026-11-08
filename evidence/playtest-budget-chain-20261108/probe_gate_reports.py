"""Scan retained gate reports for measured play-test wall times.

Read-only. Run on the host with plain python3 (no project imports, no tests).
Usage: python3 probe_gate_reports.py [gate_reports_dir]
Prints JSON: every playtest.json with a numeric timing.wall_sec (sorted desc),
plus every manifest playtest stage duration including aborted gates.
"""
import glob
import json
import os
import sys
from datetime import datetime

root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.AItelier/gate-reports")
out = {"playtest_json_with_timing": [], "manifest_playtest_stages": []}

for p in glob.glob(os.path.join(root, "*", "playtest.json")):
    try:
        d = json.load(open(p))
    except Exception as e:  # unreadable retained report: record, do not hide
        out["playtest_json_with_timing"].append({"path": os.path.abspath(p), "error": str(e)[:120]})
        continue
    t = d.get("timing") if isinstance(d.get("timing"), dict) else {}
    w = t.get("wall_sec")
    if isinstance(w, (int, float)):
        out["playtest_json_with_timing"].append({
            "path": os.path.abspath(p), "timing_wall_sec": w, "schema": t.get("schema"),
            "passed": d.get("passed"), "gate_timeout": d.get("gate_timeout"),
            "scenario_count": t.get("scenario_count"), "control_count": t.get("control_count"),
        })

for m in glob.glob(os.path.join(root, "*", "manifest.json")):
    try:
        d = json.load(open(m))
    except Exception:
        continue
    st = (d.get("stages") or {}).get("playtest")
    if not st:
        continue
    try:
        a = datetime.fromisoformat(st["started_at"])
        b = datetime.fromisoformat(st["finished_at"])
        dur = (b - a).total_seconds()
    except Exception:
        dur = None
    out["manifest_playtest_stages"].append({
        "path": os.path.abspath(m), "duration_sec": dur, "stage_status": st.get("status"),
        "gate_status": d.get("status"), "playtest_json_present":
            os.path.exists(os.path.join(os.path.dirname(m), "playtest.json")),
    })

out["playtest_json_with_timing"].sort(key=lambda r: -(r.get("timing_wall_sec") or 0))
out["manifest_playtest_stages"].sort(key=lambda r: -(r.get("duration_sec") or 0))
print(json.dumps(out, indent=1))
