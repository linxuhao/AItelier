"""The launcher's record must not spell a failed call like the tool's answer.

``tools/gate_run.sh`` has three places that record a ``gate_binding.py`` call that
could not be ASKED: the ``||`` fallbacks on the ``receipt`` and ``platform``
calls, and the branch for a sidecar re-observation that failed. All three used to
be spelled with a word the tool ANSWERS with (``unknown``), so
``<suffix>.gate.exit`` recorded "unknown" whether the tool had judged readable
bytes or had never run at all -- and a failed sidecar re-observation blamed the
platform identity for something the receipt never reported.

The real launcher body is executed here, once per side of each substitution,
against owned Git fixtures. Only process boundaries are replaced: ``docker``, and
the identity tool itself -- which either delegates to the real
``tools/gate_binding.py`` or refuses, so "the tool's exact line" in the answered
run is the tool's own output rather than a copy of it. Every assertion is made on
the exit FILE the gate writes, never on a re-implementation of the shell lines.
No real Docker, sidecar, engine or GPU is touched.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
GATE_RUN = SOURCE / "tools" / "gate_run.sh"
SNAP_COORD = 'SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"'
FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")
HEAD = "1" * 40
TREE = "2" * 40
IMAGE = "sha256:" + "c" * 64
SDK = "1.5.99"

# The identity tool, replaced at the process boundary. Entries in `fail` are the
# calls that could not be asked; every other call is the real tool's answer. A
# mode can be asked more than once (the launcher observes the sidecar before
# dispatch and again after the gate), so an entry is either a bare mode -- every
# call of it fails -- or `mode@n`, which fails only the n-th call of that mode.
BINDING_STANDIN = '''import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open({calls!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
fail = json.loads(Path({fail!r}).read_text())
seen = Path({seen!r})
counts = json.loads(seen.read_text()) if seen.exists() else {{}}
counts[args[0]] = counts.get(args[0], 0) + 1
seen.write_text(json.dumps(counts))
if args[0] in fail or args[0] + "@" + str(counts[args[0]]) in fail:
    sys.stderr.write("stand-in: " + args[0] + " could not be asked\\n")
    sys.exit(1)
if args[0] == "source":
    print({source!r})
elif args[0] == "image":
    print({image!r})
elif args[0] == "sha256":
    print({binding_sha256!r})
else:
    sys.exit(subprocess.run([sys.executable, {observer!r}, *args]).returncode)
'''

DOCKER_STANDIN = '''#!{python}
import sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "inspect":
    counter = Path({inspects!r})
    count = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(count + 1))
    print("b" * 64, {image!r}, "123", "2026-01-01T00:00:00Z", "running")
    sys.exit(0)
with open({calls!r}, "a") as f:
    f.write(" ".join(args) + "\\n")
assert args[0] == "run"
Path(args[-1]).write_text({raw!r})
sys.exit({stage!r})
'''


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _substitution(mode):
    """The literal the launcher substitutes when its `mode` call could not be asked.

    `mode` is the ``tools/gate_binding.py`` mode whose call the substitution stands
    in for, and each substitution spells that mode in its own name, so the value
    is found by the name in the launcher's actual bytes -- never in a copy of
    them -- and it must be a literal rather than a variable name. Two of them are
    written as ``||`` fallbacks and the third as the branch that records a failed
    sidecar re-observation, which is why this does not key on ``||``.
    """
    used = set(re.findall(rf'"(unanswered:{mode}-tool-failed)"', GATE_RUN.read_text()))
    assert len(used) == 1, (mode, used)
    literal = used.pop()
    assert literal and "$" not in literal, (mode, literal)
    return literal


def _record():
    return dict(source=str(SOURCE), head=HEAD, tree=TREE, image=IMAGE, sdk=SDK,
                files={name: _sha(name) for name in FILES},
                engine_sha256=_sha("reviewed-engine"))


def _observation(record, digest):
    root = {"head": record["head"], "tree": record["tree"], "dirty": 0,
            "files": dict(record["files"])}
    return {
        "files": [{"path": f"/{where}/{name}", "sha256": record["files"][name]}
                  for where in ("app", "tree") for name in FILES],
        "engine": {"path": "/srv/godot_harness.py", "sha256": record["engine_sha256"],
                   "loaded_sha256": record["engine_sha256"], "pid": 7},
        "sdk": record["sdk"],
        "binding_sha256": digest,
        "python": "/usr/local/bin/python",
        "pid": 4242,
        "image": record["image"],
        "source": {"root": dict(root), "fixture": dict(root)},
    }


def _receipt(record, digest, *, unsettled=False, no_record=False):
    """A receipt the real tool reads. Every answer below is the tool's own:
    `unknown` for an unsettled receipt, `unknown receipt carries no usable
    platform record` once its platform record is dropped, else `reviewed`."""
    observed = _observation(record, digest)
    receipt = {"identity": "unchanged", "raw_exit": 23, "before": observed,
               "after": observed,
               "platform": {"binding_sha256": digest, "frozen_sha256": digest,
                            "status": "reviewed", "differences": []}}
    if unsettled:
        del receipt["after"]
    if no_record:
        del receipt["platform"]
    return json.dumps(receipt) + "\n"


def _build(root, *, fail=(), receipt=None, stage=23):
    """One owned launcher fixture: copied shell body, Git game, process stand-ins."""
    bins, game, reports, snaps = (root / name for name in ("bin", "game", "reports", "snapshots"))
    bins.mkdir(parents=True)
    game.mkdir()
    reports.mkdir()
    text = GATE_RUN.read_text()
    assert text.count(SNAP_COORD) == 1
    launcher = bins / "gate_run.sh"
    launcher.write_text(text.replace(SNAP_COORD, f'SNAPROOT="{snaps}"'))
    record = _record()
    binding = root / "binding.json"
    binding.write_bytes((json.dumps(record) + "\n").encode())
    digest = _sha(binding.read_bytes())
    receipt_text = receipt(record, digest) if receipt else _receipt(record, digest)
    calls, fails = root / "binding-calls.jsonl", root / "fail-modes.json"
    fails.write_text(json.dumps(list(fail)))
    (bins / "gate_binding.py").write_text(BINDING_STANDIN.format(
        calls=str(calls), fail=str(fails), seen=str(root / "binding-calls-by-mode.json"),
        source=str(SOURCE), image=IMAGE,
        binding_sha256=digest, observer=str(SOURCE / "tools" / "gate_binding.py")))
    fake = bins / "docker"
    fake.write_text(DOCKER_STANDIN.format(python=sys.executable,
                                          calls=str(root / "docker-calls.log"),
                                          inspects=str(root / "inspect-count"), image=IMAGE,
                                          raw=receipt_text, stage=stage))
    fake.chmod(0o755)
    subprocess.run(["git", "init", "--quiet", str(game)], check=True)
    (game / "run_tests.sh").write_text("#!/bin/sh\nexit 23\n")
    _git(game, "add", "run_tests.sh")
    _git(game, "commit", "-qm", "fixture")
    return dict(launcher=launcher, game=game, reports=reports, snaps=snaps, binding=binding,
                digest=digest, calls=calls, receipt=receipt_text)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=fixture",
                           "-c", "user.email=fixture@localhost", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _env(fx):
    return {"PATH": f"{fx['launcher'].parent}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "GATE_BINDING_FILE": str(fx["binding"])}


def _run(fx):
    return subprocess.run(["sh", str(fx["launcher"]), str(fx["game"]), str(fx["reports"]),
                           "owned"], capture_output=True, text=True, env=_env(fx))


def _exit_lines(fx):
    return (fx["reports"] / "owned.gate.exit").read_text().splitlines()


def _ask(mode, fx, *extra):
    """The real identity tool's own answer for this fixture's written receipt."""
    return subprocess.run([sys.executable, str(SOURCE / "tools" / "gate_binding.py"), mode,
                           str(fx["reports"] / "owned.platform-identity.json"), *extra,
                           "--binding-sha256", fx["digest"]],
                          capture_output=True, text=True, env=_env(fx), cwd=str(SOURCE)).stdout


def test_a_platform_call_that_failed_is_not_the_answer_it_records(tmp_path):
    """Two runs, one response boundary: the exit file must tell them apart BY VALUE."""
    answered = _build(tmp_path / "answered",
                      receipt=lambda record, digest: _receipt(record, digest, no_record=True))
    assert _run(answered).returncode == 23
    recorded = _exit_lines(answered)
    answer = _ask("platform", answered).strip()
    # The neglected-record answer is the one the old spelling paraphrased, and it
    # is reachable exactly where the launcher's fallback used to be.
    assert answer == "unknown receipt carries no usable platform record", answer
    assert recorded[0] == "23"
    assert recorded[1] == f"platform_unreviewed {answer}"

    failed = _build(tmp_path / "failed", fail=("platform",))
    assert _run(failed).returncode == 23
    substituted = _exit_lines(failed)
    assert substituted[0] == "23"
    assert substituted[1] == f"platform_unreviewed {_substitution("platform")}"
    assert substituted[1] != recorded[1]
    assert _substitution("platform") not in answer
    assert answer not in substituted[1]


def test_a_receipt_call_that_failed_is_not_the_answer_it_records(tmp_path):
    """The same pair for the substitution that feeds the identity verdict."""
    settled = lambda record, digest: _receipt(record, digest, unsettled=True)  # noqa: E731
    answered = _build(tmp_path / "answered", receipt=settled)
    assert _run(answered).returncode == 69
    recorded = _exit_lines(answered)
    answer = _ask("receipt", answered, "23").strip()
    assert answer == "unknown", answer
    assert recorded[0].startswith(f"{answer}-platform-identity; real rc was 23;")

    failed = _build(tmp_path / "failed", fail=("receipt",), receipt=settled)
    assert _run(failed).returncode == 69
    substituted = _exit_lines(failed)
    assert substituted[0].startswith(
        f"{_substitution("receipt")}-platform-identity; real rc was 23;")
    assert substituted[0] != recorded[0]
    # ... and each spelling appears only where its own call produced it.
    assert _substitution("receipt") not in recorded[0]
    assert answer not in substituted[0]


def test_a_sidecar_call_that_failed_is_not_the_identity_answer_it_records(tmp_path):
    """The third substitution: a re-observation that failed is not `unknown` either.

    One run could not re-observe the sidecar -- its before-dispatch observation
    succeeded, so the gate still measured and the receipt itself answered
    `unchanged` -- while the other run's receipt answered `unknown`. Both end in
    the same exit file with exit 69, and the file must spell the two apart, or a
    sidecar transport failure reads as the platform identity's own answer.
    """
    settled = lambda record, digest: _receipt(record, digest)  # noqa: E731
    unanswered = _build(tmp_path / "unanswered", fail=("sidecar@2",), receipt=settled)
    assert _run(unanswered).returncode == 69
    substituted = _exit_lines(unanswered)
    # The receipt ANSWERED, so the failed call is the re-observation alone -- and
    # it really was the second sidecar call (the gate ran and measured).
    assert _ask("receipt", unanswered, "23").strip() == "unchanged"
    modes = [json.loads(line)[0] for line in unanswered["calls"].read_text().splitlines()]
    assert modes.count("sidecar") == 2, modes
    assert substituted[0].startswith(
        f"{_substitution("sidecar")}-platform-identity; real rc was 23;")

    answered = _build(tmp_path / "answered",
                      receipt=lambda record, digest: _receipt(record, digest, unsettled=True))
    assert _run(answered).returncode == 69
    recorded = _exit_lines(answered)
    answer = _ask("receipt", answered, "23").strip()
    assert answer == "unknown", answer
    assert recorded[0].startswith(f"{answer}-platform-identity; real rc was 23;")
    # Two spellings for two different facts, in the file the consumer reads.
    assert substituted[0] != recorded[0]
    assert _substitution("sidecar") not in recorded[0]
    assert answer not in substituted[0]
