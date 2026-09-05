#!/usr/bin/env python3
"""Reproduce the launch half of the 2026-09-05 incident against WHICHEVER
AItelier + skillflow are importable. Uses only API that exists before the fix.

    cd <aitelier tree> && PYTHONPATH=<skillflow tree>/src python evidence/launch_probe.py

L1  registering a seeded config's project must not, by itself, start a run
L2  a poller tick landing inside start_config_run must not start a second one
L3  a poller that starts the launcher's run first must not fail the launch
"""
import sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import skillflow
from skillflow.core import SkillFlow
from skillflow.exceptions import SkillFlowError
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader

import core.scheduler as scheduler
import api.dependencies as deps
from core.run_launcher import start_config_run

ROOT = Path(scheduler.__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"


def manifest(name):
    return SimpleNamespace(config_name=name, seed_file="plan.md",
                           scheduler_owned=True, repo_mode="code",
                           registers_generated_pipeline=False,
                           registers_generated_addon=False)


def rig(tmp):
    sf = SkillFlow(":memory:", tool_loader=ToolLoader(),
                   workspace_base=str(tmp / "ws"),
                   projects_base=str(tmp / "projects"))
    sf.register_agent_config("offload_implementer", tools=["read_file"])
    sf.register_graph(PipelineGraph.from_yaml(CONFIGS / "coding_impl.yaml"))
    reg = MagicMock(); reg.get.side_effect = manifest
    db = MagicMock()
    db.get_project.return_value = {"project_id": "p1", "config_name": "coding_impl",
                                  "meta_state": None, "brief": ""}
    scheduler.db = db
    scheduler.get_skillflow = lambda: sf
    scheduler.tick_log = lambda *a, **k: None
    scheduler.wake_scheduler = lambda *a, **k: None
    deps.get_skillflow = lambda: sf
    deps.get_config_registry = lambda: reg
    return sf, db


results = []


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        results.append((name, "FAIL", str(e)))
    except Exception as e:                                    # noqa: BLE001
        results.append((name, "ERROR", f"{type(e).__name__}: {e}"))
    else:
        results.append((name, "PASS", ""))


def l1():
    with tempfile.TemporaryDirectory() as d:
        sf, _ = rig(Path(d))
        rid = scheduler._get_or_create_skillflow_run("p1")
        assert rid is None, (
            f"registration alone started run {rid} with no plan.md "
            f"(status={sf.get_run(rid)['status']})")


def l2():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        sf, db = rig(tmp)
        db.get_project.return_value = None
        ticks = []

        def ensure(pid, **kw):
            db.get_project.return_value = {"project_id": pid, "meta_state": None,
                                           "config_name": "coding_impl", "brief": ""}
            return {}
        db.ensure_project.side_effect = ensure
        ws = MagicMock()
        ws.setup_workspace.side_effect = (
            lambda pid, **kw: ticks.append(
                scheduler._get_or_create_skillflow_run(pid)))

        start_config_run(db, ws, "coding_impl", "p1", seed_text="# plan\n",
                         repo_type="existing",
                         repo_path=str(tmp / "projects" / "p1"))
        runs = sf.list_runs()
        assert ticks == [None], (
            f"a tick inside start_config_run started run {ticks} before the "
            f"seed was written")
        assert len(runs) == 1, f"{len(runs)} runs created for one launch"


def l3():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        sf, db = rig(tmp)
        real_get_run = sf.get_run
        fired = []

        def get_run_but_the_poller_got_there_first(run_id):
            row = real_get_run(run_id)
            if row and row["status"] == "pending" and not fired:
                fired.append(run_id)
                real_start(run_id)              # the poller, between check & act
                row = real_get_run(run_id)
                return dict(row, status="pending")   # what the launcher had read
            return row
        real_start = sf.start_run
        sf.get_run = get_run_but_the_poller_got_there_first

        res = start_config_run(db, MagicMock(), "coding_impl", "p1",
                               seed_text="# plan\n", repo_type="existing",
                               repo_path=str(tmp / "projects" / "p1"))
        assert fired, "the race was not exercised"
        assert res.get("status") == "started", f"launch reported {res}"


print(f"aitelier under test:  {ROOT}")
print(f"skillflow under test: {Path(skillflow.__file__).resolve().parents[1]}")
check("L1 registration alone starts no run", l1)
check("L2 tick inside start_config_run starts no run", l2)
check("L3 poller winning the start does not fail the launch", l3)
w = max(len(n) for n, _, _ in results)
for n, v, detail in results:
    print(f"{n.ljust(w)}  {v}" + (f"  {detail}" if detail else ""))
sys.exit(0 if all(v == "PASS" for _, v, _ in results) else 1)
