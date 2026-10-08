"""The complete candidate replays before any review is released.

Real SkillFlow routing, real adapter publication, real Git and the real stage on
synthetic owned repositories; only model text is synthetic. A missing ledger is
unknown until the extractor returns it, so a supplied later chapter is decided
together with the ACTUAL extracted ledger, never with a guess or with the old
accepted journal the revision replaces. Whatever the case, a replay refusal
comes before literary or ledger review material exists.
"""
from __future__ import annotations

import pytest

from aitelier.writing_bench import adapter
from aitelier.writing_bench.storage import BenchError, decode, git
from test_bench import CONTRACTS, RULES, accept, bench, ledger, request, staged  # noqa: F401
from test_workflow import Session

REVIEW_FILES = {"review_request.json", "editor_packet.md", "audit_packet.md", "review_context.md",
                "proposed_ledgers.md"}


def _value(n, events, title=None, summary="本章情节推进。"):
    value = ledger(n, title or ("启程" if n == 1 else "过桥"))
    value["events"], value["summary"] = events, summary
    value["appearances"] = [{"name": e["entity_name"], "importance": 3} for e in events]
    return value


def _traveler(changes, reason="旅人的实际变化。"):
    return {"entity_type": "protagonist", "entity_name": "旅人", "changes": changes, "reason": reason}


def _newcomer(name):
    return {"entity_type": "character", "entity_name": name, "create": True,
            "changes": {"status": "alive", "位置": "渡口"}, "reason": name + "登场。"}


def _accept(bench, n, value, mode="new"):
    sid = f"acc{n}"
    accept(bench, staged(bench, request(bench, sid=sid, n=n, value=value, mode=mode), sid), sid)


def _revise_2_missing_3_supplied(bench, ch3):
    """ch3 still creates 渔夫 as accepted, so no managed file is deleted."""
    ch3 = dict(ch3, events=[_newcomer("渔夫"), *ch3["events"]])
    ch3["appearances"] = [{"name": e["entity_name"], "importance": 3} for e in ch3["events"]]
    r2 = request(bench, sid="rev2", n=2, mode="revision", provided=False)
    r3 = request(bench, sid="rev3", n=3, mode="revision", value=ch3)
    return dict(r2, submission_id="mixed", chapters=r2["chapters"] + r3["chapters"])


def _snapshot(bench):
    repo = bench.policy.repo
    return {"head": git(repo, "rev-parse", "HEAD"), "status": git(repo, "status", "--porcelain"),
            "refs": git(repo, "for-each-ref", "refs/writing-bench"),
            "author": {str(p): p.read_bytes() for p in bench.policy.submission_root.rglob("*") if p.is_file()}}


def _frozen(bench, run):
    path, _ = bench.input(run)
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def _published(session, run):
    root = session.sf._workspace.get_config_path("execution", adapter.CONFIG)
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _refused_before_review(session, bench, run, before):
    """Drive until the replay refuses; prove no review work or material followed."""
    with pytest.raises(BenchError, match="full replay refused"):
        session.drive(run)
    frozen = _frozen(bench, run)
    calls = list(session.agent_calls)
    # Re-driving the refused run repeats only the CPU replay, never a model step.
    with pytest.raises(BenchError, match="full replay refused"):
        session.drive(run)
    assert session.agent_calls == calls
    assert "literary_review" not in calls and "ledger_audit" not in calls
    published = _published(session, run)
    assert not {name for name in published if name.rsplit("/", 1)[-1] in REVIEW_FILES}, published
    assert not any(name.startswith("ledger_ready/") for name in published), published
    for receipt in ("candidate_replay.json", "literary.json", "audit.json", "stage.json"):
        assert not (bench.work(run) / receipt).exists(), receipt
    with pytest.raises(BenchError, match="review not released"):
        bench.replayed(run)
    assert _frozen(bench, run) == frozen
    assert _snapshot(bench) == before
    return calls


def _staged_without_acceptance(session, bench, run, before):
    assert session.drive(run) == "paused"
    replay = decode((bench.work(run) / "candidate_replay.json").read_bytes())
    stage = decode((bench.work(run) / "stage.json").read_bytes())
    assert stage["files"] == replay["files"] and stage["counters"] == replay["counters"]
    assert stage["requires_manual_approval"] and not stage["accepted"]
    after = _snapshot(bench)
    assert after["head"] == before["head"] and after["status"] == "" and after["author"] == before["author"]
    return replay, stage


def test_all_supplied_dead_character_refused_before_any_model_step(tmp_path, monkeypatch, bench):
    _accept(bench, 1, _value(1, [_traveler({"status": "dead"}, "旅人倒下。")]))
    session = Session(tmp_path, monkeypatch, bench)
    req = request(bench, sid="ch2", n=2, value=_value(2, [_traveler({"位置": "桥边"})]))
    before = _snapshot(bench)
    run = session.start(req)
    assert _refused_before_review(session, bench, run, before) == []


def test_all_supplied_valid_chapter_replays_then_reviews_and_stages(tmp_path, monkeypatch, bench):
    _accept(bench, 1, _value(1, [_traveler({"status": "dead"}, "旅人倒下。")]))
    session = Session(tmp_path, monkeypatch, bench)
    req = request(bench, sid="ch2", n=2, value=_value(2, [_newcomer("船夫")]))
    before = _snapshot(bench)
    run = session.start(req)
    replay, stage = _staged_without_acceptance(session, bench, run, before)
    assert session.agent_calls == ["literary_review", "ledger_audit"]
    assert replay["ledger_sources"] == {"2": "author"}
    assert stage["counters"] == {"chapters_written": 2, "last_chapter": 2, "next_chapter": 3}
    assert "ledger_ready/candidate_replay.json" in _published(session, run)


def test_new_prose_only_chapter_refused_after_extraction_before_review(tmp_path, monkeypatch, bench):
    _accept(bench, 1, _value(1, [_traveler({"status": "dead"}, "旅人倒下。")]))
    session = Session(tmp_path, monkeypatch, bench)
    session.extracted = {"2": _value(2, [_traveler({"位置": "桥边"})])}
    req = request(bench, sid="ch2", n=2, provided=False)
    before = _snapshot(bench)
    run = session.start(req)
    assert _refused_before_review(session, bench, run, before) == ["extract_ledger"]


def _death_in_chapter_two(bench):
    _accept(bench, 1, _value(1, [_traveler({"位置": "门外"})]))
    _accept(bench, 2, _value(2, [_traveler({"status": "dead"}, "旅人在桥上倒下。")]))
    _accept(bench, 3, _value(3, [_newcomer("渔夫")]))


def test_missing_revision_that_removes_old_death_is_not_refused(tmp_path, monkeypatch, bench):
    # Accepted ch2 killed 旅人. The revision of ch2 has no ledger yet, and the
    # supplied ch3 moves 旅人. Judged against the OLD ch2 journal ch3 looks
    # invalid; the actual extracted ch2 keeps 旅人 alive, so the candidate is valid.
    _death_in_chapter_two(bench)
    session = Session(tmp_path, monkeypatch, bench)
    session.extracted = {"2": _value(2, [_traveler({"位置": "新桥"})], summary="旅人过桥后活了下来。")}
    req = _revise_2_missing_3_supplied(bench, _value(3, [_traveler({"位置": "湖心"})]))
    before = _snapshot(bench)
    run = session.start(req)
    replay, stage = _staged_without_acceptance(session, bench, run, before)
    assert session.agent_calls == ["extract_ledger", "literary_review", "ledger_audit"]
    assert replay["ledger_sources"] == {"2": "extractor", "3": "author"}
    assert stage["counters"] == {"chapters_written": 3, "last_chapter": 3, "next_chapter": 4}
    session.sf.approve_checkpoint(run)
    assert session.drive(run) == "completed"
    from aitelier import novel_state as ns
    card = ns.load_characters(bench.policy.repo)["旅人"]
    assert card["status"] == "alive" and card["位置"] == "湖心"


def test_same_submission_refused_when_extracted_revision_keeps_the_death(tmp_path, monkeypatch, bench):
    # Negative control for the test above: identical submission and supplied
    # ch3, only the extractor's actual ch2 differs. The decision follows it.
    _death_in_chapter_two(bench)
    session = Session(tmp_path, monkeypatch, bench)
    session.extracted = {"2": _value(2, [_traveler({"status": "dead"}, "旅人在桥上倒下。")])}
    req = _revise_2_missing_3_supplied(bench, _value(3, [_traveler({"位置": "湖心"})]))
    before = _snapshot(bench)
    run = session.start(req)
    assert _refused_before_review(session, bench, run, before) == ["extract_ledger"]


def _death_in_chapter_one(bench):
    _accept(bench, 1, _value(1, [_traveler({"status": "dead"}, "旅人倒下。")]))
    _accept(bench, 2, _value(2, [_newcomer("船夫")]))
    _accept(bench, 3, _value(3, [_newcomer("渔夫")]))


def test_original_gap_supplied_move_of_dead_character_refused_before_review(tmp_path, monkeypatch, bench):
    # The originally observed gap: a missing ch2 must not let a supplied ch3
    # that moves the ch1-dead 旅人 reach paid review.
    _death_in_chapter_one(bench)
    session = Session(tmp_path, monkeypatch, bench)
    session.extracted = {"2": _value(2, [_newcomer("船夫")])}
    req = _revise_2_missing_3_supplied(bench, _value(3, [_traveler({"位置": "湖心"})]))
    before = _snapshot(bench)
    run = session.start(req)
    assert _refused_before_review(session, bench, run, before) == ["extract_ledger"]


def test_original_gap_is_undecidable_before_extraction(tmp_path, monkeypatch, bench):
    # Same submission as the original gap. The native journal permits a
    # recorded return (status: alive) on a dead card, so the missing ch2 can
    # make the supplied ch3 valid: refusing it before extraction would be a guess.
    _death_in_chapter_one(bench)
    session = Session(tmp_path, monkeypatch, bench)
    session.extracted = {"2": _value(2, [_newcomer("船夫"), _traveler({"status": "alive"}, "旅人被救回。")])}
    req = _revise_2_missing_3_supplied(bench, _value(3, [_traveler({"位置": "湖心"})]))
    before = _snapshot(bench)
    run = session.start(req)
    replay, _ = _staged_without_acceptance(session, bench, run, before)
    assert session.agent_calls == ["extract_ledger", "literary_review", "ledger_audit"]
    assert replay["ledger_sources"] == {"2": "extractor", "3": "author"}


def test_ledger_only_correction_reuses_literary_after_its_own_replay(tmp_path, monkeypatch, bench):
    session = Session(tmp_path, monkeypatch, bench)
    first_req = request(bench)
    req = request(bench, sid="ledgerfix", value=ledger(location="院门"))
    before = _snapshot(bench)
    first = session.start(first_req)
    assert session.drive(first) == "paused"
    session.agent_calls.clear()
    req["reuse_literary_from"] = first
    second = session.start(req, project="execution2")
    assert session.drive(second) == "paused"
    assert session.agent_calls == ["ledger_audit"]
    assert decode((bench.work(second) / "literary.json").read_bytes())["reused_from"] == first
    assert decode((bench.work(second) / "candidate_replay.json").read_bytes())["ledger_sources"] == {"1": "author"}
    assert _snapshot(bench)["head"] == before["head"]


def test_review_release_requires_this_runs_replay_and_is_idempotent(bench, tmp_path):
    bench.freeze(request(bench, provided=False), "run1", RULES, CONTRACTS)
    out = tmp_path / "out"
    with pytest.raises(BenchError, match="review not released"):
        adapter.publish_review_materials(bench, "run1", "literary", out)
    assert not out.exists()
    bench.ledgers("run1", {"1": ledger()})
    with pytest.raises(BenchError, match="review not released"):
        adapter.publish_review_materials(bench, "run1", "literary", out)
    first = bench.replay_candidate("run1")
    assert bench.replay_candidate("run1") == first == bench.replayed("run1")
    adapter.publish_review_materials(bench, "run1", "literary", out)
    adapter.publish_review_materials(bench, "run1", "literary", out)
    assert {p.name for p in out.iterdir()} == {"review_request.json", "current_prose.md", "review_context.md"}
