"""A normal render after a 1400 s queue, at the production defaults, on
whichever tree is first on PYTHONPATH.

The REAL `run_tests._run_repo_gate`, admission relay and harness admission code
(`tests/gate_fixture.py:HarnessRig`); the render is a sleep. Another owner
holds the harness for 1400 s (the holder's release is an Event with no 120 s
cap, set by a timer at 1400 s). The round's gate is the fixture gate with a
1800 s client timeout, the value the game gate reads `/script` with. Its own
render takes 450 s, longer than the 348 s historical `/script` maximum. Relay
wait and keepalive are the defaults (no env override): 1500 s and 20 s.

From submission, the answer arrives after about 1850 s, which is beyond the
client's 1800 s. From admission, it arrives after 450 s.

usage: python queue_after_1400s.py   (from the tree root; takes about 31 min)
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

for name in ("AITELIER_REPO_GATE_RENDER_WAIT_SECONDS",
             "AITELIER_REPO_GATE_KEEPALIVE_SECONDS"):
    assert name not in os.environ, name

from aitelier import gate_admission  # noqa: E402
from aitelier.tools.run_tests import impl as rt  # noqa: E402
from tests.gate_fixture import HarnessRig, write_gate  # noqa: E402

QUEUE = 1400.0
RENDER = 450.0
CLIENT_TIMEOUT = 1800


class _HeldUntilSet(threading.Event):
    def wait(self, timeout=None):
        return super().wait(None)


with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    mp.setenv("AITELIER_HOME", str(work / "home"))
    rig = HarnessRig(work / "harness", mp, render_seconds=RENDER)
    rig.release = _HeldUntilSet()
    mp.setenv("GODOT_BUILDER_URL", rig.base)
    mp.setenv("FIXTURE_GATE_CLIENT_TIMEOUT", str(CLIENT_TIMEOUT))
    repo = work / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    write_gate(repo)
    print("tree:", rt.__file__)
    print("defaults: render_wait=%s keepalive=%s" % (
        gate_admission.render_wait_seconds(),
        getattr(gate_admission, "keepalive_seconds", lambda: "absent on this tree")()))
    try:
        rig.hold()
        timer = threading.Timer(QUEUE, rig.release.set)
        timer.start()
        started = time.monotonic()
        gate = rt._run_repo_gate(repo)
        elapsed = time.monotonic() - started
        timer.cancel()
    finally:
        rig.close()
    adm = gate.get("admission") or {}
    print("QUEUE1400 " + json.dumps({
        "queue_sec": QUEUE, "render_sec": RENDER, "client_timeout_sec": CLIENT_TIMEOUT,
        "elapsed_sec": round(elapsed, 1),
        "returncode": gate.get("returncode"), "measured": gate.get("measured"),
        "admission.state": adm.get("state"),
        "requests": [{k: r.get(k) for k in (
            "route", "status", "outcome", "render_owner_wait_sec", "admission_observed",
            "admitted_after_sec", "keepalives", "waited_sec", "error")}
            for r in adm.get("requests") or []],
        "harness_rendered_for": list(rig.rendered),
        "output_tail": (gate.get("output") or "")[-400:]}, indent=1))
