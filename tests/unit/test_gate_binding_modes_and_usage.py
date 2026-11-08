"""No answer of the identity tool is spelled like the launcher's substitution.

``tools/gate_run.sh`` substitutes a value when the tool could not be ASKED
(``|| identity=`` at line 116, ``|| platform=`` at line 139). If the tool can also
ANSWER with that value, the gate's record cannot tell the two apart -- which is
exactly what happened until 2026-11-08, when both substitutions spelled words the
tool prints itself. This file pins the property that makes the distinction real:

* every token the real CLI prints while exiting 0, over both answering modes and
  their input matrix, differs from the two literals in the launcher's own bytes
  (read from ``tools/gate_run.sh``, never from a copy); and
* ``--help`` says what the positional IS per mode, because argparse named every
  positional ``binding`` while mode ``platform`` reads a RECEIPT there and takes
  the reviewed binding from ``GATE_BINDING_FILE``.

``receipt_identity`` and ``platform_status`` are pure functions of the receipt and
binding files, so the fixtures are built and the real CLI is driven as a
subprocess: no engine, container or sidecar is involved and none is faked.
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

    Read from the launcher's own ``||`` assignment in its actual bytes -- never
    from a copy of them, and it must be a literal rather than a variable name.
    """
    used = re.findall(rf'\|\|\s*{mode}="([^"]*)"', GATE_RUN.read_text())
    assert len(used) == 1, (mode, used)
    literal = used[0]
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


def _answers(tmp_path):
    """One row per input class of both answering modes: (name, argv, env, expected)."""
    binding, digest, record = _binding(tmp_path)
    env = {"GATE_BINDING_FILE": str(binding)}
    reviewed = _receipt(tmp_path, record, digest, name="reviewed.json")
    differs = _receipt(tmp_path, record, digest, status="unreviewed", sdk="1.5.85", name="differs.json",
                       differences=[{"field": "sdk", "bound": SDK, "observed": "1.5.85"}])
    disagrees = _receipt(tmp_path, record, digest, status="unreviewed", name="disagrees.json")
    settled = _receipt(tmp_path, record, digest, name="settled.json")
    unsettled = _receipt(tmp_path, record, digest, unsettled=True, name="unsettled.json")
    no_record = _receipt(tmp_path, record, digest, no_record=True, name="no-record.json")
    moved = _receipt(tmp_path, record, digest, identity="mismatch", name="moved.json")
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
    ]
    out = {}
    for name, argv, row_env in rows:
        result = _run(argv, row_env)
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        out[name] = result.stdout.strip()
    return out


def test_no_answer_the_tool_prints_is_spelled_like_a_substitution(tmp_path):
    """Every exit-0 answer of both answering modes differs from both literals."""
    literals = {"identity": _substitution("identity"),
                "platform": _substitution("platform")}
    assert all(literals.values())
    assert literals["identity"] != literals["platform"]

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
    assert len(set(answers.values())) == len(answers)

    for name, answer in answers.items():
        assert answer, name
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
