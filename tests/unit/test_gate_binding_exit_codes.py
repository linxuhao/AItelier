"""The identity tool's failures are not answers.

Mode ``platform`` used to answer ``unknown platform record unavailable`` with exit
0 both when it had JUDGED a readable receipt and when it could not read one at
all, and the launcher's own fallback (``tools/gate_run.sh:139``) spells that same
sentence, so the gate's record could not tell "the tool answered unknown" from
"the tool could not be asked" (measured 2026-11-08 by the wuxia-myth director on
the installed bytes). This drives the real CLI as a subprocess over every input
class: unreadable inputs must exit non-zero with one stderr line naming the path
and the failure kind and NOTHING on stdout, while every verdict about bytes that
were actually read keeps exit 0 and its current token.

The fixtures are built rather than gated: ``receipt_identity`` and
``platform_status`` are pure functions of the two files, so no engine, container
or sidecar is involved and none is faked.
"""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2]
TOOL = SOURCE / "tools" / "gate_binding.py"
GATE_RUN = SOURCE / "tools" / "gate_run.sh"
FILES = ("docker/godot/godot_harness.py", "aitelier/tools/godot_playtest/impl.py")
HEAD = "1" * 40
TREE = "2" * 40
IMAGE = "sha256:" + "c" * 64


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _run(args, env=None):
    """The real CLI, in a process whose environment this test controls."""
    base = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if env:
        base.update(env)
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, env=base, cwd=str(SOURCE))


def _binding(tmp_path):
    record = {
        "source": str(tmp_path / "reviewed-source"),
        "head": HEAD,
        "tree": TREE,
        "image": IMAGE,
        "sdk": "1.5.99",
        "files": {name: _sha(name.encode()) for name in FILES},
        "engine_sha256": _sha(b"reviewed-engine"),
    }
    path = tmp_path / "binding.json"
    raw = (json.dumps(record) + "\n").encode()
    path.write_bytes(raw)
    return path, _sha(raw), record


def _observation(record, digest, *, sdk=None):
    root = {"head": record["head"], "tree": record["tree"], "dirty": 0,
            "files": dict(record["files"])}
    return {
        # The real receipt lists four paths: the two harness files as seen through
        # both mounted Sources (readonly /app copy and the writable tree).
        "files": [{"path": f"/{source}/{name}", "sha256": record["files"][name]}
                  for source in ("app", "tree") for name in FILES],
        "engine": {"path": "/srv/godot_harness.py",
                   "sha256": record["engine_sha256"],
                   "loaded_sha256": record["engine_sha256"],
                   "pid": 7},
        "sdk": record["sdk"] if sdk is None else sdk,
        "binding_sha256": digest,
        "python": "/usr/local/bin/python",
        "pid": 4242,
        "image": record["image"],
        "source": {"root": dict(root), "fixture": dict(root)},
    }


def _receipt(tmp_path, record, digest, *, status="reviewed", differences=None,
             sdk=None, identity="unchanged", name="receipt.json"):
    observed = _observation(record, digest, sdk=sdk)
    receipt = {
        "identity": identity,
        "raw_exit": 0,
        "before": observed,
        "after": observed,
        "platform": {"binding_sha256": digest, "frozen_sha256": digest,
                     "status": status, "differences": list(differences or [])},
    }
    path = tmp_path / name
    path.write_text(json.dumps(receipt))
    return path


def _rig(tmp_path):
    binding_path, digest, record = _binding(tmp_path)
    receipt = _receipt(tmp_path, record, digest)
    return binding_path, digest, record, receipt


def _assert_unanswerable(result, path, kind):
    """Not a verdict: non-zero, exactly one stderr line, nothing on stdout."""
    assert result.returncode != 0, result.stdout
    assert result.stdout == "", f"a failure must not print a verdict token: {result.stdout!r}"
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, result.stderr
    line = lines[0]
    assert line.startswith("platform identity cannot be answered: "), line
    assert kind in line, line
    assert str(path) in line, line


# ── 1. inputs that cannot be read are not verdicts ───────────────────────────

def test_a_receipt_that_does_not_exist_is_not_the_answer_unknown(tmp_path):
    missing = tmp_path / "no-such-receipt.json"
    _assert_unanswerable(_run(["platform", str(missing)]), missing, "missing")


def test_a_directory_is_refused_by_name(tmp_path):
    directory = tmp_path / "a-directory"
    directory.mkdir()
    _assert_unanswerable(_run(["platform", str(directory)]), directory,
                         "not-a-regular-file (directory)")


def test_a_symlink_to_a_real_receipt_is_still_refused(tmp_path):
    # O_NOFOLLOW: a link is not the file that was reviewed, even when it resolves.
    binding_path, digest, record, receipt = _rig(tmp_path)
    link = tmp_path / "linked-receipt.json"
    link.symlink_to(receipt)
    _assert_unanswerable(_run(["platform", str(link), "--binding-sha256", digest],
                              {"GATE_BINDING_FILE": str(binding_path)}),
                         link, "not-a-regular-file (symlink)")


def test_bytes_that_are_not_json_are_refused_rather_than_answered(tmp_path):
    bad = tmp_path / "receipt.txt"
    bad.write_text("this is not a receipt\n")
    _assert_unanswerable(_run(["platform", str(bad)]), bad, "not-json")


def test_a_json_document_that_is_not_a_receipt_is_refused(tmp_path):
    array = tmp_path / "array.json"
    array.write_text("[1, 2, 3]")
    _assert_unanswerable(_run(["platform", str(array)]), array, "not-a-receipt")


# ── 2. a readable receipt with no usable binding is also not an answer ──────

def test_an_unset_gate_binding_file_is_named_not_answered(tmp_path):
    binding_path, digest, record, receipt = _rig(tmp_path)
    _assert_unanswerable(_run(["platform", str(receipt), "--binding-sha256", digest]),
                         "GATE_BINDING_FILE", "unset")


def test_a_missing_binding_file_is_named_not_answered(tmp_path):
    binding_path, digest, record, receipt = _rig(tmp_path)
    gone = tmp_path / "gone-binding.json"
    _assert_unanswerable(_run(["platform", str(receipt), "--binding-sha256", digest],
                              {"GATE_BINDING_FILE": str(gone)}), gone, "missing")


def test_binding_bytes_that_are_not_the_expected_record_are_named(tmp_path):
    binding_path, digest, record, receipt = _rig(tmp_path)
    broken = tmp_path / "broken-binding.json"
    raw = json.dumps({"source": "x"}).encode()
    broken.write_bytes(raw)
    # The bytes ARE the ones we were told to trust (their own sha256); they are not
    # a binding record, which is a failure to read the input, not a verdict.
    _assert_unanswerable(_run(["platform", str(receipt), "--binding-sha256", _sha(raw)],
                              {"GATE_BINDING_FILE": str(broken)}),
                         broken, "not-a-binding-record")


# ── 3. every verdict about bytes that WERE read keeps exit 0 ────────────────

def test_a_reviewed_receipt_still_answers_reviewed(tmp_path):
    binding_path, digest, record, receipt = _rig(tmp_path)
    result = _run(["platform", str(receipt), "--binding-sha256", digest],
                  {"GATE_BINDING_FILE": str(binding_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reviewed"
    assert result.stderr == ""


def test_an_unreviewed_identity_keeps_its_differ_count_form(tmp_path):
    binding_path, digest, record = _binding(tmp_path)
    differences = [{"field": "sdk", "bound": "1.5.99", "observed": "1.5.98"}]
    receipt = _receipt(tmp_path, record, digest, status="unreviewed",
                       differences=differences, sdk="1.5.98")
    result = _run(["platform", str(receipt), "--binding-sha256", digest],
                  {"GATE_BINDING_FILE": str(binding_path)})
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert line.startswith("unreviewed binding="), line
    assert "differs=1" in line, line
    assert "sdk:1.5.99->1.5.98" in line, line


def test_a_record_that_disagrees_with_its_own_evidence_stays_a_verdict(tmp_path):
    # The pinned shape from test_normal_gate_binding_matches_engine.py: a receipt
    # that CLAIMS reviewed over differing evidence answers `unknown`, exit 0. The
    # non-zero classes above must not swallow it.
    binding_path, digest, record = _binding(tmp_path)
    receipt = _receipt(tmp_path, record, digest, status="reviewed", differences=[],
                       sdk="1.5.98")
    result = _run(["platform", str(receipt), "--binding-sha256", digest],
                  {"GATE_BINDING_FILE": str(binding_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("unknown "), result.stdout


# ── 4. the launcher's fallback is spelled like no answer this tool can give ─

def _fallback_literal():
    text = GATE_RUN.read_text()
    found = re.findall(r'\|\|\s*platform="([^"]*)"', text)
    assert len(found) == 1, f"expected exactly one platform fallback in gate_run.sh: {found}"
    return found[0]


def test_the_shell_fallback_is_spelled_like_no_answer(tmp_path):
    binding_path, digest, record = _binding(tmp_path)
    answers = {"reviewed"}
    for status, differences, sdk in (("unreviewed",
                                      [{"field": "sdk", "bound": "1.5.99", "observed": "1.5.98"}],
                                      "1.5.98"),
                                     ("reviewed", [], "1.5.98")):
        receipt = _receipt(tmp_path, record, digest, status=status,
                           differences=differences, sdk=sdk, name=f"r-{status}-{sdk}.json")
        result = _run(["platform", str(receipt), "--binding-sha256", digest],
                      {"GATE_BINDING_FILE": str(binding_path)})
        assert result.returncode == 0, result.stderr
        answers.add(result.stdout.strip())
        answers.add(result.stdout.strip().split(" ")[0])
    fallback = _fallback_literal()
    assert fallback not in answers, (
        "the launcher's own fallback spells an answer the tool can give, so the "
        f"gate record cannot tell them apart: {fallback!r}")
    # ...and the fallback only fires on a failing call, which is exactly the
    # classes proven non-zero above: this tool no longer prints that sentence.
    tree = ast.parse(TOOL.read_text())
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert fallback not in literals, (
        f"{fallback!r} is still a string the tool can print; the fallback is only "
        "unambiguous while the tool cannot produce it")


def test_every_unreadable_class_exits_non_zero_so_the_fallback_fires_only_there(tmp_path):
    binding_path, digest, record, receipt = _rig(tmp_path)
    directory = tmp_path / "dir"
    directory.mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text("nope")
    cases = {
        "missing": _run(["platform", str(tmp_path / "absent.json")]),
        "directory": _run(["platform", str(directory)]),
        "not-json": _run(["platform", str(bad)]),
        "unset-binding": _run(["platform", str(receipt), "--binding-sha256", digest]),
    }
    for name, result in cases.items():
        assert result.returncode != 0, f"{name} answered instead of failing: {result.stdout!r}"
        assert result.stdout == "", name
    judged = _run(["platform", str(receipt), "--binding-sha256", digest],
                  {"GATE_BINDING_FILE": str(binding_path)})
    assert judged.returncode == 0, judged.stderr
    assert judged.stdout.strip() == "reviewed"
