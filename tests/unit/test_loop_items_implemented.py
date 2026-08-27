"""A round must not be able to end green while a card of it was never built.

jinyong-hud, 2026-08-27: the task loop advanced `fix_battle_hud_overlap_readability`
straight to its own t_impl_review — no t_plan row, no t_impl row anywhere in the
run. The reviewer's `t_impl` context resolved to nothing, so it reviewed the
REPOSITORY (five other cards' work) and wrote `passed: true`. The two observables
the card was meant to add did not exist, and the playtest assertions written
against them failed with `Invalid named index`.

dpe_default already carries this lesson one step earlier, on t_plan. That guard
is a validation on the PRODUCING step, which cannot fire for a step that never
ran — so the round-level gate is where a skipped card is catchable at all.
"""
import json

import pytest

from aitelier.tools.loop_items_implemented.impl import loop_items_implemented


def _round(tmp_path, order, built):
    graph = tmp_path / "dpe_game"
    (graph / "3").mkdir(parents=True)
    (graph / "3" / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": order}), encoding="utf-8")
    for item in built:
        (graph / "t_impl" / item).mkdir(parents=True)
        (graph / "t_impl" / item / "x.gd").write_text("", encoding="utf-8")
    return graph


def test_every_card_built_is_complete(tmp_path):
    g = _round(tmp_path, [["a", "b"], ["c"]], ["a", "b", "c"])
    r = loop_items_implemented(out_dir=str(g / "5.tmp"), config_name="dpe_game")
    assert r["complete"] is True
    assert r["missing"] == []
    assert r["planned"] == 3 and r["implemented"] == 3


def test_a_card_with_no_impl_output_is_named(tmp_path):
    g = _round(tmp_path, [["a", "b"], ["c"]], ["a", "c"])
    r = loop_items_implemented(out_dir=str(g / "5.tmp"), config_name="dpe_game")
    assert r["complete"] is False
    assert r["missing"] == ["b"]
    assert r["implemented"] == 2


def test_the_report_says_a_reissue_needs_a_new_id(tmp_path):
    # The loop records a skipped item as COMPLETED, so re-planning it under the
    # same id is skipped again — silently, and for a second whole round.
    g = _round(tmp_path, [["a"]], [])
    r = loop_items_implemented(out_dir=str(g / "5.tmp"), config_name="dpe_game")
    assert "new id" in r["summary"]


def test_an_unlocatable_workspace_is_not_reported_as_healthy(tmp_path):
    # The failure mode this whole tool exists to kill: absence read as a pass.
    r = loop_items_implemented(out_dir=str(tmp_path / "nope" / "5.tmp"),
                               config_name="dpe_game")
    assert r["complete"] is None
    assert "NOT checked" in r["summary"]
    assert r["complete"] is not True


def test_a_missing_manifest_is_not_reported_as_healthy(tmp_path):
    graph = tmp_path / "dpe_game"
    (graph / "t_impl").mkdir(parents=True)
    r = loop_items_implemented(out_dir=str(graph / "5.tmp"), config_name="dpe_game")
    assert r["complete"] is None
    assert "NOT checked" in r["summary"]


def test_a_missing_t_impl_dir_means_nothing_was_built(tmp_path):
    graph = tmp_path / "dpe_game"
    (graph / "3").mkdir(parents=True)
    (graph / "3" / "tasks_manifest.json").write_text(
        json.dumps({"execution_order": [["a", "b"]]}), encoding="utf-8")
    r = loop_items_implemented(out_dir=str(graph / "5.tmp"), config_name="dpe_game")
    assert r["complete"] is False
    assert r["missing"] == ["a", "b"]


def test_the_reviewer_reads_a_sentence_not_a_repr(tmp_path):
    # skillflow renders result["content"] when present and str(dict) otherwise.
    g = _round(tmp_path, [["a"]], [])
    r = loop_items_implemented(out_dir=str(g / "5.tmp"), config_name="dpe_game")
    assert r["content"].startswith("IMPLEMENTATION COVERAGE:")


def test_the_round_reviewer_is_given_the_report(tmp_path):
    import yaml, pathlib
    cfg = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "configs/dpe_default.yaml")
        .read_text(encoding="utf-8"))
    node = next(s for s in cfg["steps"] if s["id"] == "5_review")
    tools = [c["source"].get("tool") for c in node["context"]]
    assert "loop_items_implemented" in tools
