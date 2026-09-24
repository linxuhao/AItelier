"""Probe: step rows + run state after driving the real tick, with a mutation
optionally applied to the tree beforehand. usage: python p_rows.py"""
import sys, tempfile, json
from pathlib import Path
import pytest
sys.path.insert(0, ".")
import tests.skillflow.test_coding_impl_gate_absence as t
from core import gate_deferral
with pytest.MonkeyPatch.context() as mp:
    tmp = Path(tempfile.mkdtemp())
    sf, run_id = t._wire(tmp, mp, episode_max=100000, wait=1,
                         counter=tmp / "c.txt", tail=t._SILENT_TAIL)
    res = t._drive(sf, run_id, mp, ticks=12, clock_step=1)
    print("DRIVE", res)
    print("RUN", {k: v for k, v in sf.get_run(run_id).items() if k in ("status", "current_node", "error_reason")})
    for r in t._step_rows(sf, run_id)[1]:
        print("ROW", {k: r[k] for k in ("id", "step_id", "status", "retry_count", "release_count", "claim_epoch")})
    print("CLAIMS", t._claims_per_instance(sf, run_id))
