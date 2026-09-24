"""Engine probe for harness.a-scenario-has-one-reading, criterion
a-path-that-does-not-resolve-never-falls-back-to-a-leaf.

Runs the harness mounted at /srv/godot_harness.py in-process (no HTTP server,
no port, no shared lock) against /probe/proj: a scene whose only button is
Panel/PlanEditKind1. Scenario 1 clicks and asserts through the qualified path
X/PlanEditKind1, which does not exist while the leaf PlanEditKind1 does;
scenario 2 uses the bare leaf name. Exit code: 0 when the harness report is
passed, 1 when it is not.
"""
import json
import os
import sys

os.makedirs("/tmp/ctl", exist_ok=True)
sys.path.insert(0, "/srv")
import godot_harness as h  # noqa: E402

SPEC = {"scenarios": [
    {"name": "qualified_path_that_does_not_resolve", "timeline": [
        {"at": 10, "clicks": ["X/PlanEditKind1"]},
        {"at": 20, "assert": {"Panel/PlanEditKind1.presses": 1,
                              "X/PlanEditKind1.presses": 1}}]},
    {"name": "bare_leaf_name", "timeline": [
        {"at": 10, "clicks": ["PlanEditKind1"]},
        {"at": 20, "assert": {"PlanEditKind1.presses": 1}}]},
]}

print("HARNESS_SHA1:", os.popen("sha1sum /srv/godot_harness.py").read().split()[0])
print("SPEC:", json.dumps(SPEC))
r = h.playtest_project("/probe/proj", spec=SPEC, captures=0)
print("passed:", r["passed"])
print("summary:", r["summary"])
print("spec_errors:", json.dumps(r["spec_errors"], ensure_ascii=False, indent=1))
print("errors:", json.dumps(r["errors"], ensure_ascii=False, indent=1))
print("render_mode:", r.get("render_mode"))
for s in r["behavior"]["scenarios"]:
    print("scenario %s: ran=%s passed=%s input_dead=%s"
          % (s["name"], s["ran"], s["passed"], s["input_dead"]))
    for a in s["asserts"]:
        print("  assert", json.dumps({k: a.get(k) for k in
                                      ("name", "node", "frame", "passed", "actual",
                                       "observed", "error")}, ensure_ascii=False))
sys.exit(0 if r["passed"] else 1)
