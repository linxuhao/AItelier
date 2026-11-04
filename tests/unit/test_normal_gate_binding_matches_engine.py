"""The frozen normal-gate binding is a RECORD OF REVIEW, not a dispatch licence.

Owner ruling 2026-11-04, from wuxia ``iss-5e51a56608c64f60``.

The measured fact that produced the outage: ``gate_run.sh`` validated the frozen
binding in its first ~12 lines and ``exit 69`` BEFORE running anything, so one
platform rebuild (2026-10-24 -> 11-03, ``198ded9bcd`` changed
``docker/godot/godot_harness.py`` from ``cb3c7e97...`` to ``ebdb2e31...``)
stopped every project's every gate in seconds -- and collapsed "did not measure"
and "measured and failed" into the same outcome. Nothing turned red; wuxia found
it by being blocked.

The semantics asserted here, in the ruling's own shape:

  1. the gate ALWAYS runs: a stale literal never refuses before dispatch;
  2. the identity is OBSERVED at run time and recorded (dispatch record +
     ``*.platform-identity.json`` receipt);
  3. the fields that differ are DATA -- observed vs bound -- never an early exit;
  4. a RED stands as the gate's own exit code whatever the identity;
  5. a GREEN carries ``platform_unreviewed`` when the identity is not the
     reviewed binding: a measurement, not release evidence;
  6. an explicit binding (or its sha256) still judges a run against a
     historical review instead of a re-observed one.

Process boundaries only are replaced: ``docker`` is a stand-in that runs the
REAL ``run_bound``/``receipt``/``platform`` code, and ``capture`` is fed a
recorded observation (that is the container boundary). No real Docker, sidecar,
engine, GPU or live project gate is touched.

Deliberately NOT asserted: that the binding should be edited to whatever is on
disk. Editing it by hand forges a review that never happened.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
HELPER = SOURCE / "tools" / "gate_binding.py"
LAUNCHER = SOURCE / "tools" / "gate_run.sh"
FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")
SNAP_COORD = 'SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"'
IMAGE = "sha256:" + "d" * 64
# The reviewed literal of the frozen binding that went stale (2026-10-24). The
# engine file and its loaded hash are the same bytes, so one digest.
REVIEWED_20261024 = "cb3c7e97d63a8fcb7be6c6ae7dab0cc8fe1c1e19b64fdb085e6695b53140e1f1"
DEFAULT_BINDING = (Path.home() / ".AItelier" / "director" / "reports"
                   / "ui-text-facts-runtime-20261024" / "normal-binding.json")

DOCKER_STANDIN = '''#!{python}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "inspect":
    print({cid!r}, {image!r}, "4242", "2026-01-01T00:00:00Z", "running")
    sys.exit(0)
assert args[0] == "run", args
Path({calls!r}).write_text(json.dumps(args))
if not {run!r}:
    sys.exit(97)
# The real container boundary: the observation is recorded, and the receipt
# logic, the platform judgement and the stage are the real code under test.
sys.path.insert(0, {source!r})
from tools import gate_binding as observer
observation = json.loads(Path({observation!r}).read_text())
observer.capture = lambda *a, **k: observation
os.chdir(args[args.index("-w") + 1])
sys.exit(observer.run_bound(args[-2], args[-1], {root!r}, {root!r},
                            expected_sha256=args[args.index("--binding-sha256") + 1]))
'''


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture",
                           "-c", "user.email=fixture@localhost", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _helper(*args, env=None):
    return subprocess.run([sys.executable, str(HELPER), *args],
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def _platform_repo(tmp_path, stage=0):
    """A clean git Source: the reviewed Python identity files plus the stage."""
    source = tmp_path / "platform"
    source.mkdir()
    files = {}
    for relative in FILES:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / relative, target)  # the bytes under review
        files[relative] = _digest(target)
    _write(source / "run_tests.sh", f"#!/bin/sh\nexit {stage}\n", 0o755)
    _git(source, "init", "--quiet")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "reviewed platform")
    return {"source": source, "files": files,
            "head": _git(source, "rev-parse", "HEAD"),
            "tree": _git(source, "rev-parse", "HEAD^{tree}")}


def _write_binding(path, platform, files, engine, sdk="1.5.99"):
    binding = {"source": str(platform["source"]), "head": platform["head"],
               "tree": platform["tree"], "image": IMAGE, "sdk": sdk,
               "files": dict(files), "engine_sha256": engine}
    path.write_text(json.dumps(binding, indent=2) + "\n")
    return binding


def _observation(binding, binding_sha256, files, engine, dirty=0, sdk=None):
    """A recorded identity: the two readonly mounts, the engine, the SDK."""
    roots = {name: {"head": binding["head"], "tree": binding["tree"],
                    "dirty": dirty, "files": dict(files)}
             for name in ("root", "fixture")}
    return {"files": [{"path": str(Path(where) / relative), "sha256": files[relative]}
                      for relative in FILES
                      for where in ("/app", "/home/linxuhao/AItelier")],
            "engine": {"path": "/srv/godot_harness.py", "sha256": engine,
                       "loaded_sha256": engine, "pid": 4242},
            "sdk": sdk or binding["sdk"], "binding_sha256": binding_sha256,
            "python": sys.executable, "pid": 4343, "image": binding["image"],
            "source": roots}


def _rig(tmp_path, monkeypatch, *, stale, stage=0, run=True):
    """Owned launcher copy, owned git Source, owned reports and a docker stand-in."""
    bins, reports, snaps = (tmp_path / name for name in ("bin", "reports", "snapshots"))
    for path in (bins, reports, snaps):
        path.mkdir()
    platform = _platform_repo(tmp_path, stage=stage)
    bound_files = dict(platform["files"])
    bound_engine = platform["files"][FILES[0]]
    if stale:
        # Exactly the outage: the reviewed literal for the engine/harness is no
        # longer the bytes on disk. Nothing in this fixture is hand-repaired.
        bound_files[FILES[0]] = REVIEWED_20261024
        bound_engine = REVIEWED_20261024
    binding_path = tmp_path / "normal-binding.json"
    binding = _write_binding(binding_path, platform, bound_files, bound_engine)
    binding_sha256 = _digest(binding_path)
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(_observation(
        binding, binding_sha256, platform["files"], platform["files"][FILES[0]])))
    # Redirect only that owned-fixture coordinate; control flow is untouched.
    text = LAUNCHER.read_text()
    assert text.count(SNAP_COORD) == 1
    _write(bins / "gate_run.sh", text.replace(SNAP_COORD, f'SNAPROOT="{snaps}"'), 0o755)
    shutil.copyfile(HELPER, bins / "gate_binding.py")
    (bins / "gate_binding.py").chmod(0o755)
    calls = tmp_path / "docker-calls.json"
    _write(bins / "docker", DOCKER_STANDIN.format(
        python=sys.executable, cid="c" * 64, image=IMAGE, calls=str(calls),
        run=run, source=str(SOURCE), observation=str(observation),
        root=str(platform["source"])), 0o755)
    monkeypatch.setenv("PATH", str(bins) + ":" + os.environ["PATH"])
    monkeypatch.setenv("GATE_BINDING_FILE", str(binding_path))
    return {"launcher": bins / "gate_run.sh", "game": platform["source"],
            "reports": reports, "snaps": snaps, "binding_path": binding_path,
            "binding": binding, "binding_sha256": binding_sha256,
            "observed_files": platform["files"],
            "observed_engine": platform["files"][FILES[0]],
            "calls": calls,
            "receipt": reports / "owned.platform-identity.json",
            "exitf": reports / "owned.gate.exit",
            "dispatch": reports / "owned.platform-binding.json"}


def _gate(rig):
    return subprocess.run(["sh", str(rig["launcher"]), str(rig["game"]),
                           str(rig["reports"]), "owned"], capture_output=True, text=True)


def _exit_lines(rig):
    return rig["exitf"].read_text().splitlines()


# ── 1. observation is data, never a pre-flight refusal ───────────────────────

def test_a_stale_literal_is_observed_and_recorded_instead_of_refused(tmp_path):
    platform = _platform_repo(tmp_path)
    if platform["files"][FILES[0]] == REVIEWED_20261024:
        pytest.skip("the reviewed 2026-10-24 engine is on disk again: this fixture "
                    "no longer models the measured staleness")
    stale_files = dict(platform["files"])
    stale_files[FILES[0]] = REVIEWED_20261024
    binding_path = tmp_path / "normal-binding.json"
    _write_binding(binding_path, platform, stale_files, REVIEWED_20261024)
    record = tmp_path / "dispatch.json"
    result = _helper("sha256", str(binding_path), "--observe", str(record))
    # The outage itself: this used to be `exit 69` before anything ran.
    assert result.returncode == 0, result.stderr
    assert "platform_unreviewed" in result.stderr
    written = json.loads(record.read_text())
    assert written["binding_sha256"] == _digest(binding_path)
    assert written["dispatch_status"] == "source_differs"
    assert written["covers"] == ["root"]
    differences = {item["field"]: item for item in written["differences"]}
    field = f"root.files[{FILES[0]}]"
    assert differences[field]["bound"] == REVIEWED_20261024
    assert differences[field]["observed"] == platform["files"][FILES[0]]
    # The bound literals are echoed, never rewritten.
    assert written["bound"]["engine_sha256"] == REVIEWED_20261024
    assert written["bound"]["files"][FILES[0]] == REVIEWED_20261024


def test_a_reviewed_binding_records_no_difference(tmp_path):
    platform = _platform_repo(tmp_path)
    binding_path = tmp_path / "normal-binding.json"
    _write_binding(binding_path, platform, platform["files"], platform["files"][FILES[0]])
    record = tmp_path / "dispatch.json"
    result = _helper("sha256", str(binding_path), "--observe", str(record))
    assert result.returncode == 0, result.stderr
    written = json.loads(record.read_text())
    assert written["dispatch_status"] == "source_matches"
    assert written["differences"] == []
    assert written["observed"]["root"]["head"] == platform["head"]


# ── 2-5. what a verdict is WORTH: red, green+reviewed, green+unreviewed ──────

def test_a_reviewed_green_is_release_evidence(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, stale=False, stage=0)
    result = _gate(rig)
    assert result.returncode == 0, result.stderr
    assert _exit_lines(rig) == ["0"]  # byte-for-byte what a reviewed gate wrote before
    receipt = json.loads(rig["receipt"].read_text())
    assert receipt["identity"] == "unchanged" and receipt["raw_exit"] == 0
    assert receipt["platform"]["status"] == "reviewed"
    assert receipt["platform"]["differences"] == []
    assert receipt["platform"]["frozen_sha256"] == rig["binding_sha256"]
    assert json.loads(rig["dispatch"].read_text())["dispatch_status"] == "source_matches"
    assert rig["calls"].exists()  # the gate ran
    assert list(rig["snaps"].iterdir()) == []


def test_a_green_on_an_unreviewed_identity_carries_platform_unreviewed(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, stale=True, stage=0)
    result = _gate(rig)
    # A measurement, not release evidence -- and NOT a failure either.
    assert result.returncode == 0, result.stderr
    lines = _exit_lines(rig)
    assert lines[0] == "0"
    assert lines[1].startswith("platform_unreviewed ")
    assert f"engine.sha256:{REVIEWED_20261024[:12]}->{rig['observed_engine'][:12]}" in lines[1]
    assert f"files[{FILES[0]}]" in lines[1]
    receipt = json.loads(rig["receipt"].read_text())
    assert receipt["identity"] == "unchanged"
    assert receipt["platform"]["status"] == "unreviewed"
    differences = {item["field"]: item for item in receipt["platform"]["differences"]}
    assert differences["engine.sha256"]["bound"] == REVIEWED_20261024
    assert differences["engine.sha256"]["observed"] == rig["observed_engine"]
    assert differences[f"root.files[{FILES[0]}]"]["observed"] == rig["observed_engine"]
    assert receipt["before"]["engine"]["sha256"] == rig["observed_engine"]
    assert json.loads(rig["dispatch"].read_text())["dispatch_status"] == "source_differs"
    assert list(rig["snaps"].iterdir()) == []  # the measurement settled


@pytest.mark.parametrize("stale", [False, True], ids=["reviewed", "unreviewed"])
def test_a_red_is_never_downgraded_by_the_platform_identity(tmp_path, monkeypatch, stale):
    rig = _rig(tmp_path, monkeypatch, stale=stale, stage=1)
    result = _gate(rig)
    # "The code is broken" is a property of the code, not of who measured it.
    assert result.returncode == 1, result.stderr
    lines = _exit_lines(rig)
    assert lines[0] == "1"
    assert (len(lines) == 1) is (not stale)
    if stale:
        assert lines[1].startswith("platform_unreviewed ")
    assert json.loads(rig["receipt"].read_text())["raw_exit"] == 1


def test_the_flag_is_derived_from_the_receipt_not_taken_on_trust(tmp_path, monkeypatch):
    """A receipt that claims `reviewed` over differing evidence is never reviewed."""
    rig = _rig(tmp_path, monkeypatch, stale=True, stage=0)
    assert _gate(rig).returncode == 0
    receipt = json.loads(rig["receipt"].read_text())
    receipt["platform"]["status"] = "reviewed"
    receipt["platform"]["differences"] = []
    rig["receipt"].write_text(json.dumps(receipt))
    result = _helper("platform", str(rig["receipt"]),
                     "--binding-sha256", rig["binding_sha256"])
    assert result.returncode == 0
    assert not result.stdout.startswith("reviewed")
    assert result.stdout.startswith("unknown ")


# ── 6. an explicit binding still reproduces a historical judgement ───────────

def test_an_explicit_binding_judges_the_run_instead_of_a_newer_one(tmp_path, monkeypatch):
    rig = _rig(tmp_path, monkeypatch, stale=False, stage=0)
    assert _gate(rig).returncode == 0
    other = tmp_path / "other-binding.json"
    _write_binding(other, {"source": str(rig["game"]),
                           "head": _git(rig["game"], "rev-parse", "HEAD"),
                           "tree": _git(rig["game"], "rev-parse", "HEAD^{tree}")},
                   rig["observed_files"], rig["observed_engine"], sdk="1.5.85")
    judged = _helper("platform", str(rig["receipt"]), "--binding-sha256", rig["binding_sha256"])
    assert judged.stdout.strip() == "reviewed"
    # The same receipt judged against a DIFFERENT review is not promoted to it.
    foreign = _helper("platform", str(rig["receipt"]), "--binding-sha256", _digest(other),
                      env={"GATE_BINDING_FILE": str(other)})
    assert foreign.returncode == 0
    assert foreign.stdout.startswith("unknown ")
    assert not foreign.stdout.startswith("reviewed")


# ── the installed binding: recorded, echoed, never rewritten ────────────────

def test_the_installed_binding_is_recorded_verbatim_and_never_rewritten(tmp_path):
    path = Path(os.environ.get("AITELIER_NORMAL_BINDING", str(DEFAULT_BINDING)))
    if not path.is_file():
        pytest.skip(f"no installed normal-gate binding at {path} (it is an installation artifact)")
    before = path.read_bytes()
    record = tmp_path / "dispatch.json"
    result = _helper("sha256", str(path), "--observe", str(record))
    assert result.returncode == 0, result.stderr  # the outage: this used to be 69
    written = json.loads(record.read_text())
    assert written["binding_sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before  # nothing was re-bound by observing it
    binding = json.loads(before)
    assert written["bound"]["engine_sha256"] == binding["engine_sha256"]
    assert written["bound"]["files"] == binding["files"]
    observed = written["observed"]["root"]["files"]
    if observed[FILES[0]] is not None and observed[FILES[0]] != binding["files"][FILES[0]]:
        fields = {item["field"] for item in written["differences"]}
        assert f"root.files[{FILES[0]}]" in fields
