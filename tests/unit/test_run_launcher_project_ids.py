"""generate_run_id must always satisfy the real core.project_id contract.

Regression for a long imported config name
(gen_sdk100_live_checkpoint_20261027_b0a871b25d544edc88d9578958f66e93): the
generated id was longer than 64 characters and the project validator refused
the launch. Every check here goes through the real validator, not a copy of its
regex.
"""
import re

import pytest

from core import project_id as pid_mod
from core.run_launcher import generate_run_id

OBSERVED_LONG = "gen_sdk100_live_checkpoint_20261027_b0a871b25d544edc88d9578958f66e93"
SUFFIX = re.compile(r"-[0-9a-f]{8}$")


def _check(rid: str) -> None:
    assert pid_mod.is_valid(rid), rid
    assert pid_mod.require_valid(rid) == rid
    assert rid.isascii() and 1 <= len(rid) <= 64, rid
    assert rid[0].isalnum(), rid
    assert SUFFIX.search(rid), rid


def test_observed_long_config_name_is_valid_and_keeps_its_prefix():
    # Guard that the fixture really is the refusing shape under the old algorithm.
    old_style = "".join(c if c.isalnum() else "-" for c in OBSERVED_LONG.lower()).strip("-") + "-614a4321"
    assert not pid_mod.is_valid(old_style)
    rid = generate_run_id(OBSERVED_LONG)
    _check(rid)
    assert rid.startswith("gen-sdk100-live-checkpoint-20261027-")


@pytest.mark.parametrize("length", [1, 2, 54, 55, 56, 57, 63, 64, 65, 120, 400])
def test_boundary_lengths_are_valid(length):
    rid = generate_run_id("a" * length)
    _check(rid)
    # slug capped at 55 characters + "-" + 8 hex = 64 at most
    assert len(rid) == min(length, 55) + 9


def test_truncation_never_leaves_a_dangling_or_doubled_separator():
    for cut in range(50, 60):
        rid = generate_run_id("a" * cut + "_" + "b" * 40)
        _check(rid)
        assert "--" not in rid, rid


def test_same_prefix_long_names_get_independent_suffixes():
    first = generate_run_id(OBSERVED_LONG)
    second = generate_run_id(OBSERVED_LONG)
    other = generate_run_id(OBSERVED_LONG[:-1] + "0")
    ids = {first, second, other}
    assert len(ids) == 3
    for rid in ids:
        _check(rid)
    # The truncated slug is shared; only the random suffix tells runs apart.
    assert first[:-8] == second[:-8]
    assert first[-8:] != second[-8:]


@pytest.mark.parametrize("name,prefix", [
    ("Some Weird/Config Name", "some-weird-config-name-"),
    ("dpe_default_v2", "dpe-default-v2-"),
    ("code_review", "code-review-"),
    ("x", "x-"),
    ("7days", "7days-"),
])
def test_ordinary_short_names_are_unchanged(name, prefix):
    rid = generate_run_id(name)
    _check(rid)
    assert rid.startswith(prefix)
    assert len(rid) == len(prefix) + 8


@pytest.mark.parametrize("name", ["", None, "---", "___", "   ", "配置", "日本語-パイプライン", "🚀", "../../etc"])
def test_empty_or_non_ascii_inputs_still_yield_a_valid_id(name):
    _check(generate_run_id(name))


def test_mixed_non_ascii_name_keeps_only_ascii_words():
    rid = generate_run_id("café pipeline é")
    _check(rid)
    assert rid.startswith("caf-pipeline-")
    assert generate_run_id("配置").startswith("run-")


def test_forge_style_long_prefix_is_valid():
    # api/mcp_router builds generate_run_id("forge-" + name[:40]) for forging.
    _check(generate_run_id("forge-" + "Weird Name! " * 10))
