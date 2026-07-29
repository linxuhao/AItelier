"""A failed apply_state must leave the novel tree exactly as it was.

Live cost (novel-chapter-98264c92, chapter 5): apply_state wrote ch0005/ and
posted 林凌漆's card, then refused the second event (王超 had no bible card).
The chapter files existed, index.yaml still said 4 chapters written, and every
retry then failed DIFFERENTLY — next_chapter_number() derives from the dirs on
disk (6) while the journal declared 5. Recovery took a human with a git
checkout and rm -rf.

The tree is a git repo, so the guarantee is checkable: after a failed apply,
`git status` is clean.
"""
import subprocess

import pytest

from aitelier import novel_state as ns
from aitelier.tools.apply_state.impl import apply_state
from aitelier.tools.state_probe.impl import state_probe

from tests.unit.test_novel_tools import (GOOD_PROSE, _seed, _write_events,
                                         _write_prose)


def _git_status(ws) -> str:
    # Scoped to novel/ — in these tests the step staging dirs land in the same
    # tmp root (in production they live in the skillflow workspace, not the repo).
    return subprocess.run(["git", "status", "--porcelain", "--", "novel"], cwd=ws,
                          capture_output=True, text=True).stdout.strip()


def _events_of_the_live_failure():
    """Chapter 5's journal, in miniature: the protagonist books fine, then an
    on-stage-but-never-booked character is refused."""
    return {
        "chapter": 1, "title": "x", "summary": "林凡突破，王超登场。",
        "events": [
            {"entity_type": "protagonist", "entity_name": "林凡",
             "changes": {"power_level": 15}, "reason": "初战突破"},
            {"entity_type": "character", "entity_name": "王超",
             "create": False, "changes": {"status": "dead"}, "reason": "被杀"},
        ],
        "appearances": [{"name": "林凡"}],
    }


class TestAMidSequenceRefusalWritesNothing:
    @pytest.fixture
    def ws(self, tmp_path):
        _seed(tmp_path, git=True)
        _write_prose(tmp_path, GOOD_PROSE)
        _write_events(tmp_path, _events_of_the_live_failure())
        assert _git_status(tmp_path) == "", "seed must start from a clean tree"
        return tmp_path

    def test_the_refusal_still_teaches(self, ws):
        with pytest.raises(ValueError, match="还没有档案"):
            apply_state(workspace_root=str(ws))

    def test_no_chapter_dir_is_left_behind(self, ws):
        with pytest.raises(ValueError):
            apply_state(workspace_root=str(ws))
        assert not ns.chapter_dir(ws, 1).exists()
        assert ns.written_chapters(ws) == []
        assert ns.next_chapter_number(ws) == 1   # a retry books the SAME chapter

    def test_the_first_events_card_is_not_modified(self, ws):
        with pytest.raises(ValueError):
            apply_state(workspace_root=str(ws))
        card = ns.load_characters(ws)["林凡"]
        assert card["power_level"] == 10, "the earlier event must not survive"
        assert not card.get("progression")
        assert not ns.character_path(ws, "王超").exists()

    def test_the_ledger_and_the_tree_still_agree(self, ws):
        before = ns.load_yaml(ns.state_dir(ws) / "index.yaml")
        with pytest.raises(ValueError):
            apply_state(workspace_root=str(ws))
        assert ns.load_yaml(ns.state_dir(ws) / "index.yaml") == before

    def test_git_sees_no_change_at_all(self, ws):
        with pytest.raises(ValueError):
            apply_state(workspace_root=str(ws))
        assert _git_status(ws) == ""

    def test_the_corrected_retry_just_works(self, ws):
        """The recovery that needed a human: fix the flag and run again."""
        with pytest.raises(ValueError):
            apply_state(workspace_root=str(ws))
        record = _events_of_the_live_failure()
        record["events"][1]["create"] = True
        _write_events(ws, record)
        result = apply_state(workspace_root=str(ws))
        assert result["applied"] is True and result["chapter"] == 1
        assert ns.load_characters(ws)["王超"]["status"] == "dead"
        assert ns.load_yaml(ns.state_dir(ws) / "index.yaml")["chapters_written"] == 1


class TestAFailureAnywhereInTheWritePhase:
    """Validation catches the entity refusals; the transaction covers the rest
    of the sequence — the derived-state rebuild, the audit, a plain bug."""

    @pytest.fixture
    def ws(self, tmp_path):
        _seed(tmp_path, git=True)
        _write_prose(tmp_path, GOOD_PROSE)
        _write_events(tmp_path, {
            "chapter": 1, "title": "x", "summary": "林凡突破。",
            "events": [{"entity_type": "protagonist", "entity_name": "林凡",
                        "changes": {"power_level": 15}, "reason": "突破"}],
            "appearances": [{"name": "林凡"}],
            "arc_updates": [{"name": "求道长生", "nodes_completed": ["n1"]}],
        })
        return tmp_path

    def test_a_crash_after_the_events_rolls_everything_back(self, ws, monkeypatch):
        def boom(_ws):
            raise RuntimeError("index rebuild exploded")
        monkeypatch.setattr(ns, "rebuild_index", boom)

        with pytest.raises(RuntimeError, match="exploded"):
            apply_state(workspace_root=str(ws))

        assert _git_status(ws) == ""
        assert not ns.chapter_dir(ws, 1).exists()
        assert ns.load_characters(ws)["林凡"]["power_level"] == 10
        arcs = ns.load_yaml(ns.bible_dir(ws) / "arcs.yaml")
        assert arcs[0]["nodes"][0]["status"] == "pending"

    def test_a_failed_rollback_reports_both_causes(self, ws, monkeypatch):
        monkeypatch.setattr(ns, "rebuild_index",
                            lambda _ws: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(ns, "_rollback",
                            lambda *a: (_ for _ in ()).throw(OSError("disk gone")))
        with pytest.raises(RuntimeError, match="ROLLBACK FAILED") as e:
            apply_state(workspace_root=str(ws))
        assert "disk gone" in str(e.value) and "boom" in str(e.value)


class TestTheProbeNamesWhoHasNoCardYet:
    """Live: 李默 and 周小雨 have been on stage since chapter 3 with no bible
    card. Nobody computes that difference, so the first chapter that books an
    event for them discovers it at apply_state — the chapter's last step."""

    def test_an_on_stage_character_without_a_card_is_listed(self, tmp_path):
        _seed(tmp_path)
        ns.dump_yaml(ns.state_dir(tmp_path) / "index.yaml",
                     {"by_character": {"林凡": [1, 2], "李默": [3, 4]}})
        pack = state_probe(workspace_root=str(tmp_path),
                           out_dir=str(tmp_path / "probe"))
        text = (tmp_path / "probe" / "novel_context.md").read_text(encoding="utf-8")
        assert pack["next_chapter"] == 1
        section = text.split("出场过但还没有档案的角色")[1].split("\n## ")[0]
        assert "李默" in section and "create: true" in section
        assert "林凡" not in section          # 林凡 has a card — not listed

    def test_nothing_is_said_when_every_name_has_a_card(self, tmp_path):
        _seed(tmp_path)
        ns.dump_yaml(ns.state_dir(tmp_path) / "index.yaml",
                     {"by_character": {"林凡": [1]}})
        state_probe(workspace_root=str(tmp_path), out_dir=str(tmp_path / "probe"))
        text = (tmp_path / "probe" / "novel_context.md").read_text(encoding="utf-8")
        assert "还没有档案的角色" not in text


class TestValidateEvents:
    def test_a_created_entity_is_known_to_later_entries(self, tmp_path):
        _seed(tmp_path)
        ns.validate_events(tmp_path, [
            {"entity_type": "character", "entity_name": "王超", "create": True,
             "changes": {"status": "alive"}, "reason": "登场"},
            {"entity_type": "character", "entity_name": "王超",
             "changes": {"power_level": 3}, "reason": "同章再记一笔"},
        ])

    def test_it_refuses_the_whole_list_for_one_bad_entry(self, tmp_path):
        _seed(tmp_path)
        with pytest.raises(ValueError, match="还没有档案"):
            ns.validate_events(tmp_path, [
                {"entity_type": "protagonist", "entity_name": "林凡",
                 "changes": {"power_level": 15}, "reason": "ok"},
                {"entity_type": "character", "entity_name": "王超",
                 "changes": {"status": "dead"}, "reason": "no card"},
            ])

    def test_an_unknown_faction_still_needs_create(self, tmp_path):
        _seed(tmp_path)
        with pytest.raises(ValueError, match="faction"):
            ns.validate_events(tmp_path, [
                {"entity_type": "faction", "entity_name": "青云门",
                 "changes": {"power": 1}, "reason": "x"}])

    def test_apply_events_alone_is_atomic_too(self, tmp_path):
        """Not only via apply_state — the balances layer refuses up front."""
        _seed(tmp_path)
        with pytest.raises(ValueError, match="还没有档案"):
            ns.apply_events(tmp_path, [
                {"entity_type": "protagonist", "entity_name": "林凡",
                 "changes": {"power_level": 15}, "reason": "ok"},
                {"entity_type": "character", "entity_name": "神秘人",
                 "changes": {"tier": 9}, "reason": "no card"},
            ], 1)
        assert ns.load_characters(tmp_path)["林凡"]["power_level"] == 10
