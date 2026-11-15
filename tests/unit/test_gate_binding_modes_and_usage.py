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
import importlib.util
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


# ── the platform identity the receipt can no longer copy from the record ──────
#
# Until 2026-11-15 the observation `capture()` writes carried `binding["image"]`,
# so `compare_to_binding` compared the reviewed image with itself and `platform`
# answered `reviewed` for whatever image actually ran. Two halves witness the fix:
# `capture()` must record what the LAUNCHER attests (in-process, because that is
# the function that writes the field), and the judgement of such a receipt must
# spend exactly one difference on it (the real CLI, because that is what a
# consumer reads).

def _tool_module():
    """`tools/gate_binding.py` as an importable module, without a launcher."""
    spec = importlib.util.spec_from_file_location("gate_binding_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Health:
    """The one thing `capture()` reads off the sidecar: one JSON /health body."""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self, *args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_capture_records_the_attested_image_and_not_the_reviewed_one(tmp_path, monkeypatch):
    binding, digest, record = _binding(tmp_path)
    # A Source with a `.git` DIRECTORY: this arm is about the image, so the Git
    # identity is allowed to be unobservable (it is recorded as None, as before).
    source = tmp_path / "self-contained-source"
    (source / ".git").mkdir(parents=True)
    tool = _tool_module()
    monkeypatch.setenv("GATE_BINDING_FILE", str(binding))
    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda *a, **k: _Health(
        {"ok": True, "source_identity": {"path": "/srv/godot_harness.py",
                                         "sha256": record["engine_sha256"],
                                         "loaded_sha256": record["engine_sha256"],
                                         "pid": 7}}))
    ran_another = "sha256:" + "d" * 64
    monkeypatch.setenv("GATE_PLATFORM_IMAGE", ran_another)
    observed = tool.capture(record, digest, root=str(source), fixture_root=str(source))
    assert observed["image"] == ran_another
    image_differences = [item for item in tool.compare_to_binding(record, observed)
                         if item["field"] == "image"]
    assert image_differences == [{"field": "image", "bound": record["image"],
                                  "observed": ran_another}]

    # No attestation is a DIFFERENCE too: "nobody said which image this is" must
    # never come out as agreement with the record.
    monkeypatch.delenv("GATE_PLATFORM_IMAGE")
    unattested = tool.capture(record, digest, root=str(source), fixture_root=str(source))
    assert unattested["image"] == tool.UNOBSERVED_IMAGE
    assert [item["observed"] for item in tool.compare_to_binding(record, unattested)
            if item["field"] == "image"] == [tool.UNOBSERVED_IMAGE]


def test_a_receipt_that_ran_another_image_spends_exactly_that_difference(tmp_path):
    binding, digest, record = _binding(tmp_path)
    env = {"GATE_BINDING_FILE": str(binding)}
    ran_another = "sha256:" + "d" * 64

    def receipt_ran(image, name, differences):
        path = _receipt(tmp_path, record, digest, status="unreviewed", name=name,
                        differences=differences)
        written = json.loads(path.read_text())
        for half in ("before", "after"):
            written[half]["image"] = image
        path.write_text(json.dumps(written))
        return path

    another = receipt_ran(ran_another, "another-image.json",
                          [{"field": "image", "bound": IMAGE, "observed": ran_another}])
    judged = _run(["platform", str(another), "--binding-sha256", digest], env)
    assert judged.returncode == 0, judged.stderr
    assert judged.stdout.strip() == (
        f"unreviewed binding={digest[:12]} differs=1 "
        f"image:{IMAGE[:12]}->{ran_another[:12]}")

    # An unattested image is complete-but-unknown: it SETTLES (the instrument did
    # not move) and is judged as a difference, rather than making the receipt
    # unreadable.
    unattested = receipt_ran("unobserved", "unattested-image.json",
                             [{"field": "image", "bound": IMAGE, "observed": "unobserved"}])
    settled = _run(["receipt", str(unattested), "23", "--binding-sha256", digest], env)
    assert settled.stdout.strip() == "unchanged"
    unjudged = _run(["platform", str(unattested), "--binding-sha256", digest], env)
    assert unjudged.stdout.strip() == (
        f"unreviewed binding={digest[:12]} differs=1 image:{IMAGE[:12]}->unobserved")

    # The already-reported arm must keep its verdict: a receipt whose image IS the
    # reviewed one is untouched by the new field.
    assert _run(["platform", str(_receipt(tmp_path, record, digest, name="reviewed.json")),
                 "--binding-sha256", digest], env).stdout.strip() == "reviewed"


# ── a Source whose Git identity no mount of it can reach ──────────────────────
#
# The gate mounts the Source and reads its commit from inside that mount. A
# linked worktree's `.git` is a gitfile pointing at a repository elsewhere, so
# the gate can run but cannot identify it -- and the resulting None differences
# read like ordinary staleness. Mode `mountable` names that before dispatch.

def test_mountable_refuses_only_a_pointer_that_leaves_the_source(tmp_path):
    binding, digest, record = _binding(tmp_path)
    outside = tmp_path / "outside-source"
    (tmp_path / "repository" / ".git" / "worktrees" / "x").mkdir(parents=True)
    outside.mkdir()
    (outside / ".git").write_text(
        f"gitdir: {tmp_path / 'repository/.git/worktrees/x'}\n")
    inside = tmp_path / "inside-source"
    (inside / ".gitdir").mkdir(parents=True)
    (inside / ".git").write_text("gitdir: ./.gitdir\n")
    plain = tmp_path / "plain-source"
    plain.mkdir()

    def mode(source):
        record_for = tmp_path / f"mountable-{source.name}.json"
        record_for.write_text(json.dumps(dict(record, source=str(source))) + "\n")
        return _run(["mountable", str(record_for)])

    refused = mode(outside)
    assert refused.returncode == 71, (refused.stdout, refused.stderr)
    assert refused.stdout == ""  # a refusal is not an answer
    assert "gitfile pointing at" in refused.stderr, refused.stderr
    assert "outside the Source" in refused.stderr, refused.stderr
    # Everything the detector must NOT refuse: a pointer that stays inside, and a
    # Source with no gitfile at all (its Git identity is unobservable for other
    # reasons, which is a recorded None, not a refusal).
    assert mode(inside).returncode == 0, mode(inside).stderr
    assert mode(plain).returncode == 0, mode(plain).stderr
    # The binding is still read as a record: a missing Source is a different
    # refusal, and never a silent pass.
    missing = mode(tmp_path / "absent-source")
    assert missing.returncode == 71 and "not a mountable directory" in missing.stderr


def test_the_launcher_asks_that_question_before_it_mounts_anything():
    """The mode is worthless if the launcher never calls it: pin the wiring."""
    text = GATE_RUN.read_text()
    asked = text.index('mountable "$BINDING"')
    assert asked < text.index("docker run --rm --init"), "the check must precede the mount"
    assert "exit 71" in text[asked:], "a refusal needs its own exit status"
    # ... and the image is ATTESTED at dispatch time, before the container that
    # runs in it: an id read afterwards would not be the launched image.
    attested = text.index("PLATFORM_IMAGE_ID=")
    assert 'docker image inspect --format' in text[attested:]
    assert attested < text.index("docker run --rm --init")
    assert '-e GATE_PLATFORM_IMAGE="$PLATFORM_IMAGE_ID"' in text
