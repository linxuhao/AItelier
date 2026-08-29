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

# Captured before the autouse fixture stubs it out, for the two tests that
# exercise the real lookup rather than assuming a version.
_REAL_LIVE_VERSION = suggestions._live_version


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
    monkeypatch.setattr(suggestions, "_db", lambda: db)
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
    # UNKNOWN, not False. Reporting "not stale" about a config whose version
    # nobody could read asserts it is current — the exact claim this field
    # exists to stop anyone making.
    assert suggestions.get(made["id"])["stale_base"] is None


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


def test_a_concurrent_resolve_loses_instead_of_overwriting(monkeypatch, _db):
    """The read and the write are separate transactions, so the status check is
    advisory. Without a guard on the UPDATE the second writer silently replaces
    the first's outcome and is told it succeeded."""
    monkeypatch.setattr(suggestions, "_live_version", lambda target: 3)
    sid = suggestions.create("gen_foo", "x")["id"]
    stale = suggestions.get(sid)          # the read, still `open`

    # Another process resolves it in the window between that read and the write.
    # Handing resolve() the stale snapshot is what makes the window reachable in
    # a single-threaded test; the in-process re-read would otherwise catch this
    # case on its own, which is exactly why it cannot stand in for the guard.
    with _db.get_connection() as conn:
        conn.execute("UPDATE pipeline_suggestions SET status='applied', "
                     "result_version=4, resolved_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (sid,))
        conn.commit()
    monkeypatch.setattr(suggestions, "get",
                        lambda i, _s=stale: _s if i == sid else None)

    out = suggestions.resolve(sid, "rejected", note="no longer applies")

    assert "error" in out and "someone else" in out["error"]
    with _db.get_connection() as conn:
        row = conn.execute("SELECT status, result_version FROM "
                           "pipeline_suggestions WHERE id=?", (sid,)).fetchone()
    assert (row["status"], row["result_version"]) == ("applied", 4), \
        "the first resolution was overwritten"


def test_an_addon_is_versioned_under_its_composed_alias(monkeypatch):
    """An addon is registered as its overlay's `alias` (game_harness → dpe_game),
    so looking it up by its own name yields no versions — which would switch off
    the stale-base rule for the targets it matters most for."""
    class _SF:
        _overlays = {"game_harness": {"base": "dpe_default_v2",
                                      "alias": "dpe_game"}}
        def list_graph_versions(self, name):
            return [{"version": 12}] if name == "dpe_game" else []

    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: _SF(), raising=False)
    monkeypatch.setattr(suggestions, "_live_version", _REAL_LIVE_VERSION)

    from core.baseline import graph_name_of
    assert graph_name_of("game_harness") == "dpe_game"
    assert suggestions.create("game_harness", "overlay targets a bare step id"
                              )["base_version"] == 12


def test_an_unknown_target_keeps_its_own_name(monkeypatch):
    """The control: the alias mapping must not rewrite a plain pipeline name."""
    class _SF:
        _overlays = {}
        def list_graph_versions(self, name):
            return [{"version": 5}] if name == "gen_foo" else []

    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", lambda: _SF(), raising=False)
    monkeypatch.setattr(suggestions, "_live_version", _REAL_LIVE_VERSION)

    from core.baseline import graph_name_of
    assert graph_name_of("gen_foo") == "gen_foo"
    assert suggestions.create("gen_foo", "x")["base_version"] == 5
