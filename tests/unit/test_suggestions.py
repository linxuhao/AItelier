"""A lesson about a config has to outlive the run that found it.

Review findings used to die inside their own run: the reviewer told the
implementer, the loop closed, and nothing carried "this pipeline is wrong in
this way" out to the pipeline. A suggestion is that finding made durable, and
recorded against the config version it was written about — because guidance
written at v3 may already be moot at v5, and applying it to latest without
re-reading is how a fix lands on the wrong thing.
"""

import pytest

from core import suggestions


@pytest.fixture(autouse=True)
def _no_engine(monkeypatch):
    """Default: no version history available (the shape an older engine gives).

    Individual tests opt into versions by re-patching. Keeping the default here
    means every test states its own version assumptions instead of inheriting
    whatever the live registry happens to hold.
    """
    monkeypatch.setattr(suggestions, "_live_version", lambda target: None)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    from core.db_manager import DBManager
    db = DBManager(str(tmp_path / "t.db"))
    monkeypatch.setattr(suggestions, "get_db_manager", lambda: db)
    return db


def test_a_suggestion_is_recorded_against_the_version_it_was_written_about(
        monkeypatch):
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 3)
    made = suggestions.create("gen_foo", "t_impl cannot see the plan",
                              "step t_impl reads {step: plan} but plan is in "
                              "another loop")
    assert made["base_version"] == 3
    assert suggestions.get(made["id"])["status"] == "open"


def test_an_open_suggestion_goes_stale_when_the_config_moves_on(monkeypatch):
    """Not an error — a prompt to re-read it before acting."""
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 3)
    sid = suggestions.create("gen_foo", "fix the gate")["id"]
    assert suggestions.get(sid)["stale_base"] is False

    monkeypatch.setattr(suggestions, "_live_version", lambda target: 5)
    stale = suggestions.get(sid)
    assert stale["stale_base"] is True
    assert stale["live_version"] == 5 and stale["base_version"] == 3


def test_a_resolved_suggestion_is_never_reported_stale(monkeypatch):
    """It is a historical fact. Flagging it stale invites re-litigating work
    that was already settled."""
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 3)
    sid = suggestions.create("gen_foo", "fix the gate")["id"]
    suggestions.resolve(sid, "applied", result_version=4)

    monkeypatch.setattr(suggestions, "_live_version", lambda target: 9)
    assert suggestions.get(sid)["stale_base"] is False


def test_applied_must_name_the_version_that_carries_the_fix(monkeypatch):
    """Without it "applied" is unfalsifiable — nothing connects the lesson to
    the change that answered it."""
    sid = suggestions.create("gen_foo", "fix the gate")["id"]
    # _live_version returns None here (the autouse default), so nothing can
    # supply the version implicitly.
    out = suggestions.resolve(sid, "applied")
    assert "error" in out and "result_version" in out["error"]
    assert suggestions.get(sid)["status"] == "open", "left half-resolved"


def test_applied_takes_the_live_version_when_the_engine_can_supply_one(
        monkeypatch):
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 7)
    sid = suggestions.create("gen_foo", "fix the gate")["id"]
    assert suggestions.resolve(sid, "applied")["result_version"] == 7


def test_a_resolved_suggestion_cannot_be_resolved_again(monkeypatch):
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 2)
    sid = suggestions.create("gen_foo", "x")["id"]
    suggestions.resolve(sid, "rejected", note="no longer applies")

    again = suggestions.resolve(sid, "applied", result_version=3)
    assert "error" in again
    assert suggestions.get(sid)["status"] == "rejected"
    assert suggestions.get(sid)["result_version"] is None


def test_a_finding_is_still_recorded_when_the_engine_has_no_versions():
    """Losing the lesson to protect its metadata would be backwards."""
    made = suggestions.create("gen_foo", "the gate never fires")
    assert made.get("error") is None
    assert made["base_version"] is None
    assert suggestions.get(made["id"])["stale_base"] is False


def test_listing_filters_by_target_and_status():
    suggestions.create("gen_a", "one")
    b = suggestions.create("gen_b", "two")["id"]
    suggestions.resolve(b, "rejected", note="wrong diagnosis")

    assert [s["target"] for s in suggestions.list_for(status="open")] == ["gen_a"]
    assert [s["id"] for s in suggestions.list_for("gen_b", "rejected")] == [b]
    assert suggestions.open_count("gen_a") == 1
    assert suggestions.open_count("gen_b") == 0


@pytest.mark.parametrize("target,title", [("", "t"), ("gen_a", "")])
def test_an_empty_target_or_title_is_refused(target, title):
    assert "error" in suggestions.create(target, title)
