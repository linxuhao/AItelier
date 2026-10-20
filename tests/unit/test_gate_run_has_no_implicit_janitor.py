"""The ordinary gate launcher must never sweep shared sidecar scratch.

The real ``tools/gate_run.sh`` shell body is executed against owned Git
fixtures. Only process boundaries are replaced: ``docker``, the Source-binding
helper and (for the baseline) the old absolute janitor path. The copied script
text is edited only to redirect absolute coordinates (snapshot root and legacy
janitor path) to owned fixture directories; control flow is never replaced.
No real Docker, sidecar, GPU, engine or global janitor is touched.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
BASE_SHA = "6f82139a1aec2d6c7bd884d8a95a0195fb4a8b83"
SNAP_COORD = 'SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"'
OLD_JANITOR = "sh /home/linxuhao/.AItelier/bin/godot_tmp_janitor.sh"
IMAGE = "sha256:" + "a" * 64

BINDING_STANDIN = '''import json, sys
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
mode = args[0]
if mode == "source":
    print({source!r})
elif mode == "image":
    print({image!r})
elif mode == "sidecar":
    Path(args[1]).write_text(json.dumps({{"cid": "b" * 64, "status": "running"}}))
else:
    sys.exit(2)
'''

DOCKER_STANDIN = '''#!{python}
import json, sys
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
assert args[0] == "run"
identity = {identity!r}
Path(args[-1]).write_text(json.dumps({{"identity": identity, "raw_exit": {stage!r}}}))
sys.exit({stage!r} if identity == "unchanged" else 69)
'''

JANITOR_STANDIN = "#!/bin/sh\nprintf 'called\\n' >> '{sentinel}'\n"


def _lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture",
                           "-c", "user.email=fixture@localhost", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _build(tmp_path, monkeypatch, script_text, *, identity="unchanged", stage=23, valid_git=True):
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
        calls=str(binding_calls), source=str(SOURCE), image=IMAGE))
    fake = bins / "docker"
    fake.write_text(DOCKER_STANDIN.format(python=sys.executable, calls=str(docker_calls),
                                          identity=identity, stage=stage))
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
                janitor_log=reports / "owned.janitor.log")


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


def test_final_useful_gate_keeps_stage_exit_and_calls_no_janitor(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="unchanged", stage=23)
    result = _run(fx)
    assert result.returncode == 23, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().strip() == "23"
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    docker = _lines(fx["docker_calls"])
    assert len(docker) == 1
    args = docker[0]
    assert args[0] == "run" and "--rm" in args and "--init" in args
    for flag, value in (("--cpus", "2"), ("--memory", "2g"), ("--memory-swap", "2g"),
                        ("--pids-limit", "512"), ("--cap-drop", "ALL"),
                        ("--security-opt", "no-new-privileges")):
        assert args[args.index(flag) + 1] == value
    assert args[args.index("-w") + 1] != str(fx["game"])
    assert IMAGE in args and f"{SOURCE}:/app:ro" in args
    modes = [c[0] for c in _lines(fx["binding_calls"])]
    assert modes == ["source", "image", "sidecar", "sidecar"]
    _assert_clean_source_and_snapshot(fx)


def test_final_sdk_identity_refusal_exits_69_without_janitor(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), identity="poisoned")
    result = _run(fx)
    assert result.returncode == 69, result.stderr
    assert (fx["reports"] / "owned.gate.exit").read_text().startswith("poisoned-platform-identity")
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    assert len(_lines(fx["docker_calls"])) == 1
    _assert_clean_source_and_snapshot(fx)


def test_final_early_invalid_git_refuses_without_janitor_or_docker(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, _final_script(), valid_git=False)
    result = _run(fx)
    assert result.returncode == 64, result.stderr
    assert "not inside a git work tree" in result.stderr
    assert _janitor_calls(fx) == [] and not fx["janitor_log"].exists()
    assert _lines(fx["docker_calls"]) == []
    assert [c[0] for c in _lines(fx["binding_calls"])] == ["source", "image"]
    assert not fx["snaps"].exists() or list(fx["snaps"].iterdir()) == []
