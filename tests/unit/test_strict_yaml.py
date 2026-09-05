"""Tests for aitelier/strict_yaml.py — the duplicate-key-rejecting YAML reader.

A repeated mapping key is silently last-one-wins in stock safe_load, which is
how an authored assertion disappears from a play-test contract while the gate
still reports every surviving assertion as passed. These tests pin the
rejection, the actionability of the message, and — just as important — the
things that must NOT change: anchors, merge keys, ordinary documents, and every
other yaml user in the process.
"""

import pytest
import yaml

from aitelier.strict_yaml import (DuplicateKeyError, load_yaml_file_strict,
                                  load_yaml_strict)


def test_ordinary_documents_load_exactly_as_safe_load():
    text = "a: 1\nb:\n  - 1\n  - {c: 2}\nd: null\n"
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_empty_and_scalar_documents_are_unchanged():
    assert load_yaml_strict("", "t.yaml") is None
    assert load_yaml_strict("just a scalar\n", "t.yaml") == "just a scalar"
    assert load_yaml_strict("- 1\n- 2\n", "t.yaml") == [1, 2]


def test_a_repeated_key_is_rejected():
    with pytest.raises(DuplicateKeyError):
        load_yaml_strict("at: 1\nat: 2\n", "t.yaml")


def test_the_message_names_the_key_the_source_and_both_lines():
    """An actionable message: which key, which file, and which two lines. Without
    the line numbers the author has to re-find the collision by eye in a file
    that is hundreds of lines long."""
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_strict("scene: a\nassert: {x: 1}\nassert: {y: 2}\n",
                         "playtest/cultivation.yaml")
    msg = str(exc.value)
    assert "assert" in msg
    assert "playtest/cultivation.yaml" in msg
    assert "line 3" in msg and "line 2" in msg


def test_a_duplicate_nested_deep_in_the_document_is_rejected():
    text = ("scenarios:\n"
            "  - name: a\n"
            "    timeline:\n"
            "      - at: 10\n"
            "        assert:\n"
            "          A.b: b == 1\n"
            "        assert:\n"
            "          A.c: c == 2\n")
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_strict(text, "deep.yaml")
    assert "assert" in str(exc.value)


def test_a_duplicate_in_a_flow_mapping_is_rejected():
    with pytest.raises(DuplicateKeyError):
        load_yaml_strict("entry: {at: 1, at: 2}\n", "flow.yaml")


def test_anchors_and_aliases_still_work():
    doc = load_yaml_strict("base: &b {x: 1}\nuse: *b\n", "t.yaml")
    assert doc == {"base": {"x": 1}, "use": {"x": 1}}


def test_merge_keys_keep_their_legal_override_semantics():
    """`<<` merges a mapping in, and an explicit key legally overrides what it
    merged. That is not a duplicate — flagging it would break valid contracts."""
    doc = load_yaml_strict(
        "base: &b {x: 1, y: 2}\nuse:\n  <<: *b\n  x: 9\n", "t.yaml")
    assert doc["use"] == {"x": 9, "y": 2}


def test_two_merge_keys_in_one_mapping_are_legal():
    doc = load_yaml_strict(
        "a: &a {x: 1}\nb: &b {y: 2}\nuse:\n  <<: [*a, *b]\n  z: 3\n", "t.yaml")
    assert doc["use"] == {"x": 1, "y": 2, "z": 3}


def test_a_duplicate_beside_a_merge_key_is_still_rejected():
    with pytest.raises(DuplicateKeyError):
        load_yaml_strict("a: &a {x: 1}\nuse:\n  <<: *a\n  z: 1\n  z: 2\n",
                         "t.yaml")


def test_ordinary_yaml_errors_still_raise_yaml_errors():
    with pytest.raises(yaml.YAMLError):
        load_yaml_strict("::: not yaml :::\n[unterminated", "t.yaml")


def test_the_file_reader_names_the_path(tmp_path):
    p = tmp_path / "scenario.yaml"
    p.write_text("at: 1\nat: 2\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_file_strict(p)
    assert str(p) in str(exc.value)


def test_the_global_safe_loader_is_not_mutated():
    """NEGATIVE CONTROL. The strictness is local to this reader. If it were
    installed on yaml.SafeLoader, every other yaml user in the process (and
    every dependency) would inherit it — a far larger blast radius than the
    contract this guards."""
    load_yaml_strict("a: 1\n", "t.yaml")
    with pytest.raises(DuplicateKeyError):
        load_yaml_strict("a: 1\na: 2\n", "t.yaml")
    # Stock behaviour, untouched: last one wins, no exception.
    assert yaml.safe_load("a: 1\na: 2\n") == {"a": 2}
    assert yaml.load("a: 1\na: 2\n", Loader=yaml.SafeLoader) == {"a": 2}


# -- merge/alias graphs the first cut got wrong ----------------------------
# REGRESSION, measured by the director on e8f80a9: the first cut scanned
# node.value at construction time and called that "the authored mapping". It is
# not. SafeConstructor.flatten_mapping resolves `<<` by editing the merged
# mapping IN PLACE, so a shared anchored node reached later through an alias
# already carries the keys merged into it, and legal YAML was rejected. The
# detection now reads a snapshot taken while the document is composed, before
# any constructor runs.

_NESTED_MERGE = """base: &a {x: 1}
consumer:
  <<: &b {<<: *a, x: 2}
reused: *b
"""


def test_a_shared_anchor_merged_then_reused_is_not_a_duplicate():
    """The exact reproducer. `&b` is flattened in place while `consumer` is
    built, so `reused: *b` reaches a node carrying x twice — authored once."""
    assert load_yaml_strict(_NESTED_MERGE, "repro.yaml") == yaml.safe_load(_NESTED_MERGE)


def test_the_reused_merge_anchor_is_also_legal_from_inside_a_sequence():
    """Same trigger, reached through a list: the anchor is first CONSTRUCTED
    only after another mapping already flattened it."""
    text = ("consumer:\n  <<: &b {<<: &a {x: 1}, x: 2}\n"
            "items:\n  - *b\n  - <<: *b\n    y: 9\n")
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_a_shared_timeline_frame_reused_by_a_second_scenario_is_legal():
    """The contract-shaped version of the same graph: one authored frame merged
    into scenario a and reused verbatim by scenario b."""
    text = ("scenarios:\n  - name: a\n    timeline:\n"
            "      - <<: &frame {<<: &base {at: 1}, at: 2}\n"
            "  - name: b\n    timeline:\n      - *frame\n")
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_a_sequence_merge_reused_later_is_legal():
    """flatten_mapping has a second branch for `<<: [*a, *b]`; it must stay
    legal too, reused or not."""
    text = ("a: &a {x: 1}\nb: &b {x: 2}\n"
            "use:\n  <<: &m [*a, *b]\n  x: 3\nagain: *m\n")
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_one_anchor_merged_into_two_mappings_and_also_used_as_a_value():
    text = ("base: &a {x: 1, y: 1}\n"
            "one:\n  <<: *a\n  x: 2\n"
            "two:\n  <<: *a\n  y: 3\n"
            "plain: *a\n")
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_a_merge_chain_through_several_shared_anchors():
    text = ("a: &a {x: 1}\n"
            "b: &b {<<: *a, y: 2}\n"
            "c: &c {<<: *b, z: 3}\n"
            "use:\n  <<: *c\n  x: 9\n"
            "again: *b\n"
            "yet_again: *c\n")
    assert load_yaml_strict(text, "t.yaml") == yaml.safe_load(text)


def test_a_real_duplicate_beside_a_reused_merge_anchor_is_still_rejected():
    """The fix must not become "anything near a merge key is forgiven"."""
    text = ("base: &a {x: 1}\n"
            "consumer:\n  <<: &b {<<: *a, x: 2}\n  y: 1\n  y: 2\n"
            "reused: *b\n")
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_strict(text, "t.yaml")
    assert "'y'" in str(exc.value)


def test_a_duplicate_inside_the_shared_anchored_mapping_is_still_rejected():
    text = "consumer:\n  <<: &b {x: 1, x: 2}\nreused: *b\n"
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_strict(text, "t.yaml")
    assert "'x'" in str(exc.value)


def test_a_duplicate_in_a_mapping_that_also_dereferences_an_alias():
    text = "base: &a {x: 1}\nuse:\n  plain: *a\n  at: 1\n  at: 2\n"
    with pytest.raises(DuplicateKeyError) as exc:
        load_yaml_strict(text, "t.yaml")
    assert "'at'" in str(exc.value)


def test_a_cyclic_alias_graph_loads_without_recursing():
    """A mapping that contains itself is legal YAML and PyYAML builds it with a
    two-phase constructor. The duplicate scan must not recurse into it."""
    doc = load_yaml_strict("a: &a\n  self: *a\n", "cyc.yaml")
    assert doc["a"]["self"] is doc["a"]


def test_a_duplicate_in_a_cyclic_mapping_is_still_rejected():
    with pytest.raises(DuplicateKeyError):
        load_yaml_strict("a: &a\n  self: *a\n  k: 1\n  k: 2\n", "cyc.yaml")

