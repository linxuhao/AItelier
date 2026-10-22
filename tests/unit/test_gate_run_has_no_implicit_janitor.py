"""The ordinary gate launcher must never sweep shared sidecar scratch.

The real ``tools/gate_run.sh`` shell body is executed against owned Git
fixtures. Only process boundaries are replaced: ``docker``, the Source-binding
helper and (for the baseline) the old absolute janitor path. The copied script
text is edited only to redirect absolute coordinates (snapshot root and legacy
janitor path) to owned fixture directories; control flow is never replaced.
No real Docker, sidecar, GPU, engine or global janitor is touched.
"""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
BASE_SHA = "6f82139a1aec2d6c7bd884d8a95a0195fb4a8b83"
SNAP_COORD = 'SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"'
OLD_JANITOR = "sh /home/linxuhao/.AItelier/bin/godot_tmp_janitor.sh"
IMAGE = "sha256:" + "a" * 64

BINDING_STANDIN = '''import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
mode = args[0]
if mode == "source":
    print({source!r})
elif mode == "image":
    print({image!r})
elif mode in ("sidecar", "receipt"):
    sys.exit(subprocess.run([sys.executable, {observer!r}, *args]).returncode)
else:
    sys.exit(2)
'''

DOCKER_STANDIN = '''#!{python}
import json, sys, time
from pathlib import Path
args = sys.argv[1:]
if args[0] == "inspect":
    counter = Path({inspects!r})
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    behavior = {after!r} if count else "unchanged"
    if behavior == "unavailable":
        sys.exit(1)
    if behavior == "malformed":
        print("unknown")
        sys.exit(0)
    cid = "c" * 64 if behavior == "mismatch" else "b" * 64
    print(cid, {image!r}, "123", "2026-01-01T00:00:00Z", "running")
    sys.exit(0)
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
assert args[0] == "run"
if {pause!r}:
    Path({request!r}).write_text(args[args.index("-w") + 1])
    deadline = time.monotonic() + 10
    while not Path({ready!r}).exists():
        if time.monotonic() > deadline:
            sys.exit(98)
        time.sleep(0.01)
raw = {raw!r}
if raw is not None:
    Path(args[-1]).write_text(raw)
sys.exit({docker_exit!r})
'''

JANITOR_STANDIN = "#!/bin/sh\nprintf 'called\\n' >> '{sentinel}'\n"


def _lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture",
                           "-c", "user.email=fixture@localhost", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _observation():
    return dict(files=[dict(path=f"/fixture/source-{i}", sha256="a" * 64) for i in range(4)],
                engine=dict(path="/srv/godot_harness.py", sha256="b" * 64,
                            loaded_sha256="b" * 64, pid=123),
                sdk="1.5.85", binding_sha256="c" * 64,
                python=sys.executable, pid=456, image=IMAGE)


def _build(tmp_path, monkeypatch, script_text, *, identity="unchanged", stage=23,
           valid_git=True, after="unchanged", pause=False):
    """Create the owned launcher copy, Git game fixture and process stand-ins."""
    bins, game, reports, snaps = (tmp_path / n for n in ("bin", "game", "reports", "snapshots"))
    for path in (bins, game, reports):
        path.mkdir()
    janitor = tmp_path / "owned-janitor.sh"
    sentinel = tmp_path / "janitor-sentinel.log"
    janitor.write_text(JANITOR_STANDIN.format(sentinel=sentinel))
    # Redirect only absolute owned-fixture coordinates in the launcher copy.
    assert script_text.count(SNAP_COORD) == 1
    redirected = script_text.replace(SNAP_COORD, f'SNAPROOT="{snaps}"')
    redirected = redirected.replace(OLD_JANITOR, f"sh {janitor}")
    launcher = bins / "gate_run.sh"
    launcher.write_text(redirected)
    binding_calls, docker_calls = tmp_path / "binding-calls.jsonl", tmp_path / "docker-calls.jsonl"
    (bins / "gate_binding.py").write_text(BINDING_STANDIN.format(
        calls=str(binding_calls), source=str(SOURCE), image=IMAGE,
        observer=str(SOURCE / "tools/gate_binding.py")))
    receipt = dict(identity=identity, raw_exit=stage,
                   before=_observation(), after=_observation())
    if identity == "mismatch":
        receipt["after"]["engine"]["pid"] += 1
        receipt["error"] = "IdentityMismatch: observed engine PID differs"
    elif identity in ("unknown", "poisoned"):
        del receipt["after"]
    elif identity in ("incomplete", "bad-observation", "null-exit", "wrong-exit"):
        receipt["identity"] = "unchanged"
        if identity == "incomplete":
            del receipt["after"]
        elif identity == "bad-observation":
            receipt.update(before={}, after={})
        elif identity == "null-exit":
            receipt["raw_exit"] = None
        else:
            receipt["raw_exit"] = stage + 1
    raw = None if identity == "missing" else "{" if identity == "malformed" else json.dumps(receipt)
    request, ready = tmp_path / "consumer-request", tmp_path / "consumer-ready"
    fake = bins / "docker"
    fake.write_text(DOCKER_STANDIN.format(python=sys.executable, calls=str(docker_calls),
                                          inspects=str(tmp_path / "inspect-count"), after=after,
                                          image=IMAGE, pause=pause, request=str(request),
                                          ready=str(ready), raw=raw,
                                          docker_exit=69 if identity in ("unknown", "poisoned", "mismatch") else stage))
    fake.chmod(0o755)
    head = None
    if valid_git:
        subprocess.run(["git", "init", "--quiet", str(game)], check=True)
        (game / "run_tests.sh").write_text("#!/bin/sh\nexit 23\n")
        _git(game, "add", "run_tests.sh")
        _git(game, "commit", "-qm", "fixture")
        head = _git(game, "rev-parse", "HEAD")
    else:
        (game / "run_tests.sh").write_text("#!/bin/sh\nexit 23\n")
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    monkeypatch.setenv("GATE_BINDING_FILE", str(binding))
    monkeypatch.setenv("PATH", f"{bins}:" + __import__("os").environ["PATH"])
    return dict(launcher=launcher, game=game, reports=reports, snaps=snaps, head=head,
                sentinel=sentinel, binding_calls=binding_calls, docker_calls=docker_calls,
                janitor_log=reports / "owned.janitor.log", raw_receipt=raw,
                consumer_request=request, consumer_ready=ready)


def _run(fx):
    return subprocess.run(["sh", str(fx["launcher"]), str(fx["game"]), str(fx["reports"]), "owned"],
                          capture_output=True, text=True)


def _janitor_calls(fx):
    return fx["sentinel"].read_text().splitlines() if fx["sentinel"].exists() else []


def _assert_clean_source_and_snapshot(fx):
    assert (fx["reports"] / "owned.gate.head").read_text().strip() == fx["head"]
    assert _git(fx["game"], "rev-parse", "HEAD") == fx["head"]
    assert _git(fx["game"], "status", "--porcelain") == ""
    assert len(_git(fx["game"], "worktree", "list", "--porcelain").splitlines()) == 3
    assert not fx["snaps"].exists() or list(fx["snaps"].iterdir()) == []


def _assert_retained_snapshot(fx):
    assert (fx["reports"] / "owned.gate.head").read_text().strip() == fx["head"]
    assert _git(fx["game"], "rev-parse", "HEAD") == fx["head"]
    assert _git(fx["game"], "status", "--porcelain") == ""
    snapshots = list(fx["snaps"].iterdir())
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert _git(snapshot, "rev-parse", "HEAD") == fx["head"]
    assert _git(snapshot, "status", "--porcelain") == ""
    assert f"worktree {snapshot}\n" in _git(fx["game"], "worktree", "list", "--porcelain") + "\n"
    assert (snapshot / "run_tests.sh").read_bytes() == (fx["game"] / "run_tests.sh").read_bytes()
    return snapshot


@pytest.fixture(scope="module")
def base_script():
    shown = subprocess.run(["git", "-C", str(SOURCE), "show", f"{BASE_SHA}:tools/gate_run.sh"],
                           capture_output=True, text=True)
    if shown.returncode:
        pytest.skip(f"exact base {BASE_SHA} is not available in this checkout")
    assert shown.stdout.count(OLD_JANITOR) == 1
    return shown.stdout


@pytest.mark.parametrize("scenario", ["useful-exit-23", "sdk-identity-refusal"])
def test_baseline_6f_invokes_janitor_even_when_identity_later_refuses(
        base_script, tmp_path, monkeypatch, scenario):
    useful = scenario == "useful-exit-23"
    fx = _build(tmp_path, monkeypatch, base_script,
                identity="unchanged" if useful else "poisoned")
    result = _run(fx)
    assert result.returncode == (23 if useful else 69), result.stderr
    assert _janitor_calls(fx) == ["called"]  # implicit shared sweep at the base
    assert fx["janitor_log"].exists()
    assert len(_lines(fx["docker_calls"])) == 1


def _final_script():
    text = (SOURCE / "tools/gate_run.sh").read_text()
    assert "janitor" not in text.lower()
    return text


@pytest.mark.parametrize("stage", [0, 23])
def test_final_useful_gate_keeps_stage_exit_and_calls_no_janitor(tmp_path, monkeypatch, stage):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="unchanged", stage=stage)
    result = _run(fx)
    assert result.returncode == stage, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().strip() == str(stage)
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    docker = _lines(fx["docker_calls"])
    assert len(docker) == 1
    args = docker[0]
    assert args[0] == "run" and "--rm" in args and "--init" in args
    assert args[args.index("--network") + 1] == "aitelier_default"
    assert args[args.index("-u") + 1] == "1000:1000"
    for flag, value in (("--cpus", "2"), ("--memory", "2g"), ("--memory-swap", "2g"),
                        ("--pids-limit", "512"), ("--cap-drop", "ALL"),
                        ("--security-opt", "no-new-privileges")):
        assert args[args.index(flag) + 1] == value
    assert args[args.index("-w") + 1] != str(fx["game"])
    assert IMAGE in args and f"{SOURCE}:/app:ro" in args
    modes = [c[0] for c in _lines(fx["binding_calls"])]
    assert modes == ["source", "image", "sidecar", "receipt", "sidecar"]
    _assert_clean_source_and_snapshot(fx)


def test_final_sdk_identity_refusal_exits_69_without_janitor(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="poisoned")
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().startswith("unknown-platform-identity")
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    assert len(_lines(fx["docker_calls"])) == 1
    # The prior green assertion demanded deletion here. No-janitor evidence
    # remains useful, but identity refusal does not establish consumer settlement.
    _assert_retained_snapshot(fx)


@pytest.mark.parametrize("identity", ["missing", "malformed", "unknown", "incomplete",
                                      "bad-observation", "null-exit", "wrong-exit"])
def test_unknown_terminal_identity_retains_snapshot(tmp_path, monkeypatch, identity):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity=identity, stage=124)
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    expected_rc = 69 if identity == "unknown" else 124
    verdict = (fx["reports"] / "owned.gate.exit").read_text()
    assert verdict.startswith(f"unknown-platform-identity; real rc was {expected_rc};")
    receipt = fx["reports"] / "owned.platform-identity.json"
    if fx["raw_receipt"] is None:
        assert not receipt.exists()
    else:
        assert receipt.read_text() == fx["raw_receipt"]
    assert _janitor_calls(fx) == []
    _assert_retained_snapshot(fx)


def test_observed_identity_mismatch_is_distinct_and_retains_snapshot(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="mismatch")
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().startswith("mismatch-platform-identity;")
    receipt = json.loads((fx["reports"] / "owned.platform-identity.json").read_text())
    assert receipt["raw_exit"] == 23 and receipt["before"] != receipt["after"]
    assert _janitor_calls(fx) == []
    _assert_retained_snapshot(fx)


@pytest.mark.parametrize("stage", [-9, 124, 125, 126, 127, 137])
def test_termination_exit_does_not_authorize_snapshot_cleanup(tmp_path, monkeypatch, stage):
    fx = _build(tmp_path, monkeypatch, _final_script(), stage=stage)
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().startswith(
        f"unknown-platform-identity; real rc was {stage % 256};")
    assert json.loads((fx["reports"] / "owned.platform-identity.json").read_text())["raw_exit"] == stage
    _assert_retained_snapshot(fx)


@pytest.mark.parametrize("after", ["unavailable", "malformed", "mismatch"])
def test_after_sidecar_observation_cannot_authorize_cleanup(tmp_path, monkeypatch, after):
    fx = _build(tmp_path, monkeypatch, _final_script(), after=after)
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    state = "mismatch" if after == "mismatch" else "unknown"
    assert (fx["reports"] / "owned.gate.exit").read_text().startswith(
        f"{state}-platform-identity; real rc was 23;")
    assert (fx["reports"] / "owned.platform-identity.json").read_text() == fx["raw_receipt"]
    assert (fx["reports"] / "owned.sidecar-after.json").exists() == (after == "mismatch")
    assert _janitor_calls(fx) == []
    _assert_retained_snapshot(fx)


def test_stale_receipt_refuses_new_invocation_without_altering_old_reports(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), stage=0)
    assert _run(fx).returncode == 0
    old = {p.name: p.read_bytes() for p in fx["reports"].iterdir()}
    result = _run(fx)
    assert result.returncode == 69 and "prior evidence retained" in result.stderr
    assert {p.name: p.read_bytes() for p in fx["reports"].iterdir()} == old
    assert len(_lines(fx["docker_calls"])) == 1
    assert json.loads(old["owned.platform-identity.json"])["raw_exit"] == 0
    _assert_clean_source_and_snapshot(fx)


def test_client_exit_retains_snapshot_of_still_live_owned_consumer(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="missing", stage=124, pause=True)
    launcher = subprocess.Popen(
        ["sh", str(fx["launcher"]), str(fx["game"]), str(fx["reports"]), "owned"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    consumer = None
    try:
        deadline = time.monotonic() + 5
        while not fx["consumer_request"].exists():
            assert launcher.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        snapshot = Path(fx["consumer_request"].read_text())
        consumer = subprocess.Popen(
            [sys.executable, "-c",
             "import pathlib,sys,time\nwith open('run_tests.sh') as stream:\n"
             " pathlib.Path(sys.argv[1]).write_text(stream.read())\n"
             " while True:\n  stream.seek(0)\n  assert stream.read()\n  time.sleep(0.05)\n",
             str(fx["consumer_ready"])], cwd=snapshot,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _, stderr = launcher.communicate(timeout=15)
        assert launcher.returncode == 69, stderr
        assert consumer.poll() is None
        assert fx["consumer_ready"].read_text() == "#!/bin/sh\nexit 23\n"
        assert _assert_retained_snapshot(fx) == snapshot
        assert (fx["reports"] / "owned.gate.exit").read_text().startswith(
            "unknown-platform-identity; real rc was 124;")
    finally:
        if consumer is not None:
            if consumer.poll() is None:
                consumer.terminate()
            consumer.wait(timeout=5)
        if launcher.poll() is None:
            launcher.terminate()
            launcher.communicate(timeout=15)


@pytest.fixture(scope="module")
def observer():
    spec = importlib.util.spec_from_file_location("gate_binding_under_test", SOURCE / "tools/gate_binding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("identity,stage", [("unchanged", 0), ("unchanged", 23),
                                           ("unknown", 23), ("mismatch", 23)])
def test_bound_receipt_preserves_real_exit_and_terminal_evidence(
        tmp_path, monkeypatch, observer, identity, stage):
    script = tmp_path / "run_tests.sh"
    script.write_text(f"#!/bin/sh\nexit {stage}\n")
    script.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(observer, "load_binding", lambda *args: {})
    before, after = _observation(), _observation()
    if identity == "mismatch":
        after["engine"]["pid"] += 1
    observations = iter([before, after])

    def capture(*args):
        value = next(observations)
        if value is after and identity == "unknown":
            raise ValueError("terminal observation unavailable")
        return value

    monkeypatch.setattr(observer, "capture", capture)
    output = tmp_path / "receipt.json"
    result = observer.run_bound("unused", output)
    assert result == (stage if identity == "unchanged" else 69)
    receipt = json.loads(output.read_text())
    assert receipt["raw_exit"] == stage and receipt["identity"] == identity
    assert ("after" in receipt) == (identity != "unknown")
    assert observer.receipt_identity(output, result) == identity


def test_bound_writer_refuses_stale_receipt_without_overwriting_raw(tmp_path, monkeypatch, observer):
    output = tmp_path / "receipt.json"
    old = b'{"identity":"unchanged","raw_exit":23}\n'
    output.write_bytes(old)

    def forbidden(*args):
        pytest.fail("stale receipt must refuse before binding or execution")

    monkeypatch.setattr(observer, "load_binding", forbidden)
    assert observer.run_bound("unused", output) == 69
    assert output.read_bytes() == old


def test_frozen_source_binding_still_requires_exact_clean_git_identity(tmp_path, observer):
    source = tmp_path / "platform"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    for relative in observer.FILES:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# owned Source fixture\n")
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "platform fixture")
    binding = dict(source=str(source), head=_git(source, "rev-parse", "HEAD"),
                   tree=_git(source, "rev-parse", "HEAD^{tree}"), image=IMAGE,
                   files={p: observer.digest(source / p) for p in observer.FILES},
                   engine_sha256="b" * 64)
    manifest = tmp_path / "binding.json"
    manifest.write_text(json.dumps(binding))
    assert observer.load_binding(manifest) == binding
    (source / observer.FILES[0]).write_text("# changed fixture\n")
    with pytest.raises(observer.IdentityMismatch, match="frozen clean commit/tree"):
        observer.load_binding(manifest)


def test_final_early_invalid_git_refuses_without_janitor_or_docker(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), valid_git=False)
    result = _run(fx)
    assert result.returncode == 64, result.stderr
    assert "not inside a git work tree" in result.stderr
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    assert _lines(fx["docker_calls"]) == []
    assert [c[0] for c in _lines(fx["binding_calls"])] == ["source", "image"]
    assert not fx["snaps"].exists() or list(fx["snaps"].iterdir()) == []
