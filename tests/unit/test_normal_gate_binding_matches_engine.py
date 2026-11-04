"""The frozen normal-gate binding must describe the engine that is on disk.

Owner ruling 2026-11-04: a merge that changes the gate engine must carry its
re-binding in the SAME delivery. The alternative - letting the binding go stale
and re-binding later - is what happened when 198ded9bcd changed
docker/godot/godot_harness.py (cb3c7e97 -> ebdb2e31) without refreshing the
binding: every project's normal gate stopped, and nothing turned red to say so.
Wuxia found it, not a check.

So this test is the check. When it fails, the failure IS the outage: gates are
refusing to run an engine the binding never approved. It goes green again when a
reviewed re-bind is produced - and the order matters, because binding before
verification is a false green.

Deliberately NOT asserted here: that the binding should be edited to whatever is
on disk. Editing it by hand would forge a review that never happened.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BINDING = (Path.home() / ".AItelier" / "director" / "reports"
                   / "ui-text-facts-runtime-20261024" / "normal-binding.json")


def _binding_path() -> Path:
    return Path(os.environ.get("AITELIER_NORMAL_BINDING", str(DEFAULT_BINDING)))


def test_the_installed_binding_matches_the_engine_in_this_tree():
    path = _binding_path()
    if not path.is_file():
        pytest.skip(f"no installed normal-gate binding at {path} (it is an installation artifact)")
    binding = json.loads(path.read_text())
    files = binding.get("files") or {}
    assert files, f"{path} names no engine files"

    mismatches = []
    for rel, bound in sorted(files.items()):
        target = ROOT / rel
        if not target.is_file():
            mismatches.append(f"  {rel}: bound {bound[:16]}… but the file is not in this tree")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != bound:
            mismatches.append(f"  {rel}:\n    bound  {bound}\n    actual {actual}")

    assert not mismatches, (
        "the normal gate's frozen binding does not describe this tree, so every "
        "project's gate refuses to run:\n" + "\n".join(mismatches) +
        "\n\nThis is the outage, not noise. Re-bind in the same delivery as the "
        "engine change: verify the new engine first, then produce the new binding. "
        "Do not edit the binding by hand - that records a review that never happened.")
