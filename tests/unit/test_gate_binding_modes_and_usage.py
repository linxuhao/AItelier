"""No answer of the identity tool is spelled like the launcher's substitution.

``tools/gate_run.sh`` substitutes a value when the tool could not be ASKED: the
``||`` fallbacks on the ``receipt`` and ``platform`` calls, and the branch that
records a sidecar re-observation which failed. If the tool can also ANSWER with
that value, the gate's record cannot tell the two apart -- which is exactly what
happened until 2026-11-08, when all three substitutions spelled words the tool
prints itself (``unknown``, twice as an answer and once as the identity verdict).
This file pins the property that makes the distinction real:

* the real CLI is driven as a subprocess over every answering mode and their
  input matrix, and every answer it gives while exiting 0 differs from every
  literal in the launcher's own bytes (read from ``tools/gate_run.sh``, never from
  a copy). Mode ``sidecar``'s exit-0 answer is SILENCE -- it answers by writing
  the record it observed -- so the recorded answer there is the empty string, and
  the matrix proves the rows are real by requiring the written record; and
* ``--help`` says what the positional IS per mode, because argparse named every
  positional ``binding`` while mode ``platform`` reads a RECEIPT there and takes
  the reviewed binding from ``GATE_BINDING_FILE``.

``receipt_identity`` and ``platform_status`` are pure functions of the receipt and
binding files, so their fixtures are built. The one thing mode ``sidecar`` reads
outside its own arguments is the container it observes, so ``docker inspect`` is
replaced at the process boundary and the tool's own writing and comparison logic
stays under test. No engine, container, sidecar or GPU is involved.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
TOOL = SOURCE / "tools" / "gate_binding.py"
GATE_RUN = SOURCE / "tools" / "gate_run.sh"
FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")
HEAD = "1" * 40
TREE = "2" * 40
IMAGE = "sha256:" + "c" * 64
SDK = "1.5.99"


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def _run(args, env=None):
    """The real CLI, in a process whose environment this test controls."""
    base = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if env:
        base.update(env)
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True,
                          text=True, env=base, cwd=str(SOURCE))


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


def _binding(tmp_path):
    record = dict(source=str(tmp_path / "reviewed-source"), head=HEAD, tree=TREE, image=IMAGE,
                  sdk=SDK, files={name: _sha(name) for name in FILES},
                  engine_sha256=_sha("reviewed-engine"))
    path = tmp_path / "binding.json"
    raw = (json.dumps(record) + "\n").encode()
    path.write_bytes(raw)
    return path, _sha(raw), record


def _observation(record, digest, *, sdk=None):
    root = {"head": record["head"], "tree": record["tree"], "dirty": 0,
            "files": dict(record["files"])}
    return {
        "files": [{"path": f"/{where}/{name}", "sha256": record["files"][name]}
                  for where in ("app", "tree") for name in FILES],
        "engine": {"path": "/srv/godot_harness.py", "sha256": record["engine_sha256"],
                   "loaded_sha256": record["engine_sha256"], "pid": 7},
        "sdk": record["sdk"] if sdk is None else sdk,
        "binding_sha256": digest,
        "python": "/usr/local/bin/python",
        "pid": 4242,
        "image": record["image"],
        "source": {"root": dict(root), "fixture": dict(root)},
    }


def _receipt(tmp_path, record, digest, *, status="reviewed", differences=None, sdk=None,
             identity="unchanged", error=None, unsettled=False, no_record=False,
             name="receipt.json"):
    observed = _observation(record, digest, sdk=sdk)
    receipt = {"identity": identity, "raw_exit": 23, "before": observed, "after": observed,
               "platform": {"binding_sha256": digest, "frozen_sha256": digest,
                            "status": status, "differences": list(differences or [])}}
    if identity == "mismatch":
        receipt["after"]["engine"]["pid"] += 1
        receipt["error"] = error or "IdentityMismatch: observed engine PID differs"
    if unsettled:
        del receipt["after"]
    if no_record:
        del receipt["platform"]
    path = tmp_path / name
    path.write_text(json.dumps(receipt))
    return path


def _sidecar_docker(tmp_path):
    """A `docker` that answers the way the sidecar observer asks it.

    Mode ``sidecar`` is the one mode that reads a container: it inspects
    ``aitelier-godot`` for the identity it then writes (and, given an earlier
    record, compares against). Only this process boundary is replaced, so the
    record and the comparison stay the real tool's.
    """
    bins = tmp_path / "sidecar-bin"
    bins.mkdir()
    identity = f"{'b' * 64} {IMAGE} 123 2026-01-01T00:00:00Z running"
    docker = bins / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "# the observer's only call: docker inspect --format ... aitelier-godot\n"
        '[ "$1" = inspect ] || exit 9\n'
        f'echo "{identity}"\n')
    docker.chmod(0o755)
    return bins


def _answers(tmp_path):
    """One row per input class of the three answering modes: (name, argv, env)."""
    binding, digest, record = _binding(tmp_path)
    env = {"GATE_BINDING_FILE": str(binding)}
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    sidecar_env = {"PATH": f"{_sidecar_docker(tmp_path)}:{path}"}
    reviewed = _receipt(tmp_path, record, digest, name="reviewed.json")
    differs = _receipt(tmp_path, record, digest, status="unreviewed", sdk="1.5.85", name="differs.json",
                       differences=[{"field": "sdk", "bound": SDK, "observed": "1.5.85"}])
    disagrees = _receipt(tmp_path, record, digest, status="unreviewed", name="disagrees.json")
    settled = _receipt(tmp_path, record, digest, name="settled.json")
    unsettled = _receipt(tmp_path, record, digest, unsettled=True, name="unsettled.json")
    no_record = _receipt(tmp_path, record, digest, no_record=True, name="no-record.json")
    moved = _receipt(tmp_path, record, digest, identity="mismatch", name="moved.json")
    fresh = tmp_path / "sidecar-fresh.json"
    observed_twice = tmp_path / "sidecar-settled.json"
    rows = [
        ("platform-reviewed", ["platform", str(reviewed), "--binding-sha256", digest], env),
        ("platform-unreviewed", ["platform", str(differs), "--binding-sha256", digest], env),
        ("platform-disagreeing", ["platform", str(disagrees), "--binding-sha256", digest], env),
        ("platform-unsettled", ["platform", str(unsettled), "--binding-sha256", digest], env),
        ("platform-no-record", ["platform", str(no_record), "--binding-sha256", digest], env),
        ("receipt-settled", ["receipt", str(settled), "23", "--binding-sha256", digest], env),
        ("receipt-mismatch", ["receipt", str(moved), "23", "--binding-sha256", digest], env),
        # An unusable frozen hash is still an ANSWER (`unknown`), not a refusal:
        # this is the value the launcher's identity substitution used to share.
        ("receipt-unusable-hash", ["receipt", str(settled), "23", "--binding-sha256", "no-hash"],
         env),
        # Mode `sidecar` answers by WRITING the record it observed: a fresh
        # observation and a settled pair both exit 0 with nothing on stdout. The
        # `sidecar-fresh` row runs first and writes the earlier record the settled
        # row is compared against -- the real tool produces both sides.
        ("sidecar-fresh", ["sidecar", str(fresh)], sidecar_env),
        ("sidecar-settled", ["sidecar", str(observed_twice), str(fresh)], sidecar_env),
    ]
    out = {}
    for name, argv, row_env in rows:
        result = _run(argv, row_env)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        out[name] = result.stdout.strip()
    return out


def test_no_answer_the_tool_prints_is_spelled_like_a_substitution(tmp_path):
    """Every exit-0 answer of all three answering modes differs from every literal."""
    literals = {mode: _substitution(mode) for mode in ("receipt", "platform", "sidecar")}
    assert all(literals.values())
    assert len(set(literals.values())) == len(literals)

    answers = _answers(tmp_path)
    # The matrix is not vacuous: each input class produced its own answer.
    assert answers["platform-reviewed"] == "reviewed"
    assert answers["platform-unreviewed"].startswith("unreviewed binding=")
    assert "differs=1" in answers["platform-unreviewed"]
    assert answers["platform-disagreeing"] == (
        "unknown receipt platform record disagrees with its own observation")
    assert answers["platform-unsettled"] == (
        "unknown terminal receipt cannot say which platform measured")
    assert answers["platform-no-record"] == "unknown receipt carries no usable platform record"
    assert answers["receipt-settled"] == "unchanged"
    assert answers["receipt-mismatch"] == "mismatch"
    assert answers["receipt-unusable-hash"] == "unknown"
    # Mode `sidecar`'s exit-0 answer is SILENCE: the verdict is the record it
    # wrote (or exit 70 + one stderr line when the identity moved), never a word
    # on stdout. So these rows contribute no token, and the rows are real -- the
    # tool's own observation is in the file, five identity fields and all.
    assert answers["sidecar-fresh"] == "" and answers["sidecar-settled"] == ""
    for name in ("sidecar-fresh", "sidecar-settled"):
        written = json.loads((tmp_path / f"{name}.json").read_text())
        assert set(written) == {"cid", "image", "pid", "started_at", "status"}, (name, written)
        assert written["status"] == "running" and written["image"] == IMAGE, (name, written)
    # Every mode that DOES answer on stdout answers uniquely ...
    spoken = {name: answer for name, answer in answers.items() if answer}
    assert set(spoken) == set(answers) - {"sidecar-fresh", "sidecar-settled"}, spoken
    assert len(set(spoken.values())) == len(spoken), spoken

    for name, answer in answers.items():
        for where, literal in literals.items():
            assert answer != literal, (name, where, literal)


def test_the_help_names_what_the_positional_is_per_mode():
    """argparse named every positional `binding`; mode platform reads a receipt."""
    result = _run(["--help"])
    assert result.returncode == 0, result.stderr
    flat = " ".join(result.stdout.split())
    assert "source, image, sha256 the positional is the binding" in flat, flat
    assert "platform the positional is the gate's own receipt" in flat, flat
    assert "The reviewed binding is read from GATE_BINDING_FILE" in flat, flat
    # The binding is a RECORD the caller froze, not an argument this mode takes.
    assert "never from the positional" in flat, flat


def test_platform_still_reads_its_positional_as_the_receipt(tmp_path):
    """Naming the argument did not move it: the receipt's CONTENT decides."""
    binding, digest, record = _binding(tmp_path)
    env = {"GATE_BINDING_FILE": str(binding)}
    reviewed = _receipt(tmp_path, record, digest, name="reviewed.json")
    disagrees = _receipt(tmp_path, record, digest, status="unreviewed", name="disagrees.json")
    first = _run(["platform", str(reviewed), "--binding-sha256", digest], env)
    second = _run(["platform", str(disagrees), "--binding-sha256", digest], env)
    assert first.returncode == 0 and first.stdout.strip() == "reviewed"
    assert second.returncode == 0
    assert second.stdout.strip() == (
        "unknown receipt platform record disagrees with its own observation")
    # A binding record handed to the mode that reads a receipt is not silently
    # judged as one -- it is answered as "this receipt cannot say", never `reviewed`.
    wrong = _run(["platform", str(binding), "--binding-sha256", digest], env)
    assert wrong.returncode == 0
    assert wrong.stdout.strip() == "unknown terminal receipt cannot say which platform measured"
