"""`create` asks "does a card exist yet?" — but it READS as "is this person new?"

Live failure: chapter 5 wrote `create: false` for 王超, who had been on stage
since chapter 3 but had never been given a booked event, so he had no bible
card. By the narrative reading the agent was RIGHT — and it was refused, the
chapter died, and apply_state's partial write left the ledger and the chapter
files disagreeing. Any agent that has read the earlier chapters gets this wrong
the same way, so the refusal has to teach the distinction, not just state it.
"""
import pytest

from aitelier import novel_state as ns


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "novel" / "state").mkdir(parents=True)
    (tmp_path / "novel" / "bible" / "characters").mkdir(parents=True)
    return tmp_path / "novel"


def _apply(ws, name, create=None):
    ev = {"entity_type": "character", "entity_name": name,
          "changes": {"status": "alive"}, "reason": "test"}
    if create is not None:
        ev["create"] = create
    return ns.apply_events(ws, [ev], 5)


class TestTheRefusalTeaches:
    def test_it_names_the_chapters_the_character_appeared_in(self, ws):
        ns.dump_yaml(ns.state_dir(ws) / "index.yaml",
                     {"by_character": {"王超": [3, 4]}})
        with pytest.raises(ValueError) as e:
            _apply(ws, "王超", create=False)
        msg = str(e.value)
        assert "王超" in msg
        assert "3, 4" in msg, msg          # where they HAVE been seen
        assert "create: true" in msg       # the exact remedy

    def test_it_states_the_distinction_that_is_misread(self, ws):
        ns.dump_yaml(ns.state_dir(ws) / "index.yaml", {"by_character": {}})
        with pytest.raises(ValueError) as e:
            _apply(ws, "路人", create=False)
        assert "出场不建档" in str(e.value)

    def test_a_never_seen_character_says_so(self, ws):
        ns.dump_yaml(ns.state_dir(ws) / "index.yaml", {"by_character": {}})
        with pytest.raises(ValueError) as e:
            _apply(ws, "查无此人", create=False)
        assert "此前从未出现" in str(e.value)

    def test_a_missing_index_does_not_turn_the_error_into_a_crash(self, ws):
        """The helper runs while something is already going wrong."""
        with pytest.raises(ValueError, match="还没有档案"):
            _apply(ws, "王超", create=False)


class TestBehaviourIsUnchanged:
    def test_create_true_still_writes_a_card(self, ws):
        ns.apply_events(ws, [{"entity_type": "character", "entity_name": "王超",
                              "create": True, "changes": {"status": "alive"},
                              "reason": "首次记事件"}], 5)
        assert ns.character_path(ws, "王超").is_file()

    def test_an_existing_card_needs_no_create_flag(self, ws):
        ns.dump_yaml(ns.character_path(ws, "郑毅"),
                     {"name": "郑毅", "status": "alive", "progression": []})
        _apply(ws, "郑毅")                       # no `create` key at all
        card = ns.load_yaml(ns.character_path(ws, "郑毅"), {})
        assert card["progression"], "the event should have been booked"

    def test_it_still_refuses_rather_than_auto_creating(self, ws):
        """The guard exists to catch typos; a silent card for a misspelling is
        exactly what it must not do."""
        ns.dump_yaml(ns.state_dir(ws) / "index.yaml",
                     {"by_character": {"王超": [3]}})
        with pytest.raises(ValueError):
            _apply(ws, "王起", create=False)     # typo
        assert not ns.character_path(ws, "王起").exists()
