#!/usr/bin/env python3
"""Offline State DAG demonstration with real SQLite, SkillFlow, Git and tests.

Run from an installed AItelier environment:
    python examples/state_graph_demo.py --report /tmp/state-demo.json

No production data, network, LLM, scheduler or deployment is used. A deterministic
fixture worker stands in for the implementation agent; its first implementation
is intentionally wrong. Independent Python subprocess tests determine evidence.
The temporary project is removed after producing the optional JSON report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from core.db_manager import DBManager
from core.state_graph import StateConflict, StateGraphStore
from core.state_attempts import StateAttempts
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    summary = run_demo()
    output = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="aitelier-state-demo-") as directory:
        root = Path(directory)
        repo = root / "source"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "State demo", "GIT_COMMITTER_NAME": "State demo",
               "GIT_AUTHOR_EMAIL": "state-demo@localhost", "GIT_COMMITTER_EMAIL": "state-demo@localhost"}

        def git(*args):
            result = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
            return result.stdout.strip()

        git("init", "-q", "-b", "main")
        (repo / "growth.py").write_text("# Isolated demonstration, not game source.\n")
        git("add", "growth.py")
        git("commit", "-qm", "seed")
        db = DBManager(str(root / "state.sqlite"))
        graph = StateGraphStore(db)
        attempts = StateAttempts(graph)
        sf = SkillFlow(str(root / "workflow.sqlite"))
        sf.register_graph(PipelineGraph(name="demo_delivery", begin="implement", steps=[StepNode(id="implement")]))
        graph.create_project("demo", "Growth feature demo")
        for name, deps, testcode in [
            ("proficiency", [], "from growth import add_progress; assert add_progress(4, 3) == 7"),
            ("monthly_plan", ["proficiency"], "from growth import month_result; assert month_result([2, 3]) == 5"),
        ]:
            graph.add_nodes("demo", [{"key": name, "goal": "Deliver " + name,
                "dependencies": deps, "acceptance": [{"id": "behaviour", "kind": "test", "description": testcode}]}])

        run_ids = []
        reports = []

        def delivery(node_key, request_key, source):
            intent = attempts.reserve("demo", node_key, 1, "demo_delivery", request_key)
            rid = sf.create_run("demo_delivery", project_id=intent["execution_project_id"])
            run_ids.append(rid)
            sf.start_run(rid)
            attempts.bind_run(intent["attempt_id"], rid, sf)
            sf.advance_run(rid)
            claim = sf.claim_next_step(rid)
            assert claim is not None
            (repo / "growth.py").write_text(source)
            git("add", "growth.py")
            git("commit", "-qm", request_key)
            sha = git("rev-parse", "HEAD")
            sf.confirm_step(claim.token, StepResult(outputs={"commit": sha}))
            sf.advance_run(rid)
            assert sf.get_run(rid)["status"] == "completed"
            attempt = attempts.reconcile(intent["attempt_id"], sf, sha)
            assert attempt["status"] == "candidate"
            return attempt

        def test_and_attest(attempt):
            criterion = attempt["context"]["acceptance"][0]
            # This process really imports/tests the committed source. No fake
            # passed=True oracle and no model-provided success flag is used.
            result = subprocess.run([sys.executable, "-B", "-c", criterion["description"]], cwd=repo,
                                    capture_output=True, text=True, timeout=10)
            body = json.dumps({"artifact": attempt["artifact_ref"], "returncode": result.returncode,
                               "stdout": result.stdout, "stderr": result.stderr}, sort_keys=True).encode()
            report_path = root / (attempt["attempt_id"] + "-test.json")
            report_path.write_bytes(body)
            report_hash = hashlib.sha256(body).hexdigest()
            attempts.record_evidence(attempt["attempt_id"], attempt["attempt_id"] + "-evidence", "behaviour",
                "pass" if result.returncode == 0 else "fail", attempt["artifact_ref"], str(report_path),
                report_hash, "offline-python-test-runner", f"Python test exited {result.returncode}")
            reports.append({"artifact": attempt["artifact_ref"], "verdict": "pass" if result.returncode == 0 else "fail",
                            "report_sha256": report_hash})
            return result.returncode

        bad = delivery("proficiency", "first-wrong-implementation", "def add_progress(a, b): return a + b + 1\n")
        assert test_and_attest(bad) != 0
        try:
            attempts.verify("demo", "proficiency", 1, bad["attempt_id"], "demo-reviewer")
        except StateConflict:
            pass
        else:
            raise AssertionError("a completed workflow with failing tests was accepted")
        assert graph.get_node("demo", "monthly_plan")["readiness"] == "blocked"

        good = delivery("proficiency", "corrected-implementation", "def add_progress(a, b): return a + b\n")
        assert test_and_attest(good) == 0
        attempts.verify("demo", "proficiency", 1, good["attempt_id"], "demo-reviewer")
        assert graph.get_node("demo", "monthly_plan")["readiness"] == "ready"

        # A new driver process/context needs only these persisted databases.
        graph = StateGraphStore(DBManager(db.db_path))
        attempts = StateAttempts(graph)
        assert attempts.get(good["attempt_id"])["run_id"] == good["run_id"]
        monthly = delivery("monthly_plan", "monthly-implementation",
            "def add_progress(a, b): return a + b\ndef month_result(actions):\n    assert len(actions) <= 2\n    return add_progress(*actions)\n")
        assert test_and_attest(monthly) == 0
        receipt = attempts.verify("demo", "monthly_plan", 1, monthly["attempt_id"], "demo-reviewer")
        assert graph.get_node("demo", "monthly_plan")["status"] == "VERIFIED"

        graph.revise_node("demo", "proficiency", 1, "Owner changes the growth rule", goal="New growth rule")
        assert graph.get_node("demo", "monthly_plan")["status"] == "STALE"
        try:
            attempts.verify("demo", "monthly_plan", 1, monthly["attempt_id"], "demo-reviewer")
        except StateConflict:
            pass
        else:
            raise AssertionError("old evidence certified a changed dependency")
        result = {"result": "PASS", "workflow_runs": len(run_ids), "actual_test_verdicts": reports,
                  "completed_but_failed_test_remained_unverified": True,
                  "verified_dependency_unlocked_next_goal": True, "new_driver_recovered_persisted_attempt": True,
                  "requirement_change_invalidated_downstream": True,
                  "historical_acceptance_receipt": receipt["receipt_id"], "events": len(graph.events("demo")),
                  "production_data_used": False, "llm_used": False,
                  "note": "Real engine/database/Git/test demonstration with a deterministic fixture worker, not an LLM quality evaluation."}
        sf._conn.close()  # no public close() on SkillFlow 1.5.67
        return result


if __name__ == "__main__":
    raise SystemExit(main())
