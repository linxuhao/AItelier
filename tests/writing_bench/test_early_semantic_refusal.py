"""Early supplied-ledger semantic replay must refuse before paid review work.

Synthetic repositories and no engine: no live novel, State database, provider
or network is touched. The guard reuses the exact stage replay machinery, so a
supplied ledger that touches an already-dead character is refused at prepare
before any literary or ledger review material is published.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aitelier.writing_bench import adapter
from aitelier.writing_bench.storage import BenchError
from test_bench import CONTRACTS, RULES, accept, bench, ledger, request, staged


def _accepted_dead_chapter(bench, run="run1", sid="draft1"):
    """Accept chapter 1 with a journal that kills 旅人."""
    value = ledger()
    value["events"] = [{"entity_type": "protagonist", "entity_name": "旅人",
                        "changes": {"status": "dead", "位置": "门外"},
                        "reason": "旅人倒下，不再是活人。"}]
    accept(bench, staged(bench, request(bench, sid=sid, value=value), run), run)


def test_replay_provided_refuses_dead_character_before_review(bench):
    _accepted_dead_chapter(bench)
    req = request(bench, sid="ch2", n=2, value=ledger(2, "过桥"))
    bench.freeze(req, "run2", RULES, CONTRACTS)
    with pytest.raises(BenchError, match="full replay refused"):
        bench.replay_provided("run2")
    # The refusal precedes every review artifact: no literary or ledger report
    # and no candidate stage was ever produced for this submission.
    assert not (bench.work("run2") / "literary.json").exists()
    assert not (bench.work("run2") / "audit.json").exists()
    assert not (bench.work("run2") / "stage.json").exists()


def test_replay_provided_passes_valid_fresh_chapter(bench):
    _accepted_dead_chapter(bench)
    value = ledger(2, "过桥")
    value["events"] = [{"entity_type": "character", "entity_name": "船夫",
                        "create": True, "changes": {"status": "alive", "位置": "渡口"},
                        "reason": "船夫登场。"}]
    value["appearances"] = [{"name": "船夫", "importance": 3}]
    req = request(bench, sid="ch2", n=2, value=value)
    bench.freeze(req, "run2", RULES, CONTRACTS)
    receipt = bench.replay_provided("run2")
    assert receipt["early_replay"] == "passed"
    assert receipt["counters"] == {"chapters_written": 2, "last_chapter": 2, "next_chapter": 3}


def test_replay_provided_defers_missing_ledger_to_extractor(bench):
    # A pure-prose submission cannot be replayed early: nothing is guessed and
    # the extractor is still the only source for a missing ledger.
    bench.freeze(request(bench, provided=False), "run1", RULES, CONTRACTS)
    receipt = bench.replay_provided("run1")
    assert receipt == {"early_replay": "deferred", "missing_chapters": [1]}
    with pytest.raises(BenchError, match="missing extracted"):
        bench.ledgers("run1")


def _accept_fresh_chapter(bench, n, sid, name):
    """Accept chapter n by creating a new living character (旅人 is dead)."""
    value = ledger(n, "过桥" if n == 2 else "夜航")
    value["events"] = [{"entity_type": "character", "entity_name": name,
                        "create": True, "changes": {"status": "alive", "位置": "渡口"},
                        "reason": name + "登场。"}]
    value["appearances"] = [{"name": name, "importance": 3}]
    accept(bench, staged(bench, request(bench, sid=sid, n=n, value=value), "run" + str(n)), "run" + str(n))


def _revision_request(bench, chapters):
    """Build one multi-chapter revision request from per-chapter parts."""
    merged = None
    for sid, n, value in chapters:
        part = request(bench, sid=sid, n=n, provided=value is not None, mode="revision", value=value)
        if merged is None:
            merged = part
        else:
            merged["chapters"].append(part["chapters"][0])
    return merged


def test_mixed_revision_missing_ledger_does_not_hide_invalid_supplied(bench):
    # Accept chapter 1 (kills 旅人), then chapters 2 and 3, and revise 2+3:
    # chapter 2 has no ledger, chapter 3 moves the already-dead 旅人.
    _accepted_dead_chapter(bench)
    _accept_fresh_chapter(bench, 2, "ch2", "船夫")
    _accept_fresh_chapter(bench, 3, "ch3", "渔夫")
    req = _revision_request(bench, [("r2", 2, None), ("r3", 3, ledger(3, "夜航"))])
    bench.freeze(req, "run9", RULES, CONTRACTS)
    with pytest.raises(BenchError, match="full replay refused"):
        bench.replay_provided("run9")
    assert not (bench.work("run9") / "literary.json").exists()
    assert not (bench.work("run9") / "audit.json").exists()
    assert not (bench.work("run9") / "stage.json").exists()


def test_mixed_revision_valid_supplied_ledger_replays_partial(bench):
    _accepted_dead_chapter(bench)
    _accept_fresh_chapter(bench, 2, "ch2", "船夫")
    _accept_fresh_chapter(bench, 3, "ch3", "渔夫")
    value = ledger(3, "夜航", "湖心")
    req = _revision_request(bench, [("r2", 2, None), ("r3", 3, value)])
    bench.freeze(req, "run9", RULES, CONTRACTS)
    receipt = bench.replay_provided("run9")
    assert receipt["early_replay"] == "partial"
    assert receipt["missing_chapters"] == [2]
    assert receipt["validated_chapters"] == [3]
    assert receipt["counters"] == {"chapters_written": 3, "last_chapter": 3, "next_chapter": 4}
    # The extracted ledger for chapter 2 is still not guessed or forged.
    with pytest.raises(BenchError, match="missing extracted"):
        bench.ledgers("run9")


def test_mixed_new_submission_replays_only_contiguous_prefix(bench):
    _accepted_dead_chapter(bench)
    # New chapters 2 (valid, provided) and 3 (missing): only the provided
    # prefix before the first missing chapter is replayable.
    value2 = ledger(2, "过桥")
    value2["events"] = [{"entity_type": "character", "entity_name": "船夫",
                         "create": True, "changes": {"status": "alive", "位置": "渡口"},
                         "reason": "船夫登场。"}]
    value2["appearances"] = [{"name": "船夫", "importance": 3}]
    req2 = request(bench, sid="n2", n=2, value=value2)
    req3 = request(bench, sid="n3", n=3, provided=False)
    req2["chapters"].append(req3["chapters"][0])
    bench.freeze(req2, "run9", RULES, CONTRACTS)
    receipt = bench.replay_provided("run9")
    assert receipt["early_replay"] == "partial"
    assert receipt["validated_chapters"] == [2] and receipt["missing_chapters"] == [3]


def test_mixed_new_submission_after_gap_is_fully_deferred(bench):
    _accepted_dead_chapter(bench)
    # New chapter 2 missing, chapter 3 provided: chapter 3 cannot be replayed
    # without the missing chapter before it, and nothing is guessed.
    req2 = request(bench, sid="g2", n=2, provided=False)
    req3 = request(bench, sid="g3", n=3, value=ledger(3, "夜航"))
    req2["chapters"].append(req3["chapters"][0])
    bench.freeze(req2, "run9", RULES, CONTRACTS)
    assert bench.replay_provided("run9") == {"early_replay": "deferred", "missing_chapters": [2]}



def test_stage_guard_still_refuses_extracted_ledger_for_dead_character(bench):
    # The later existing full-replay guard is unchanged for extractor input.
    _accepted_dead_chapter(bench)
    req = request(bench, sid="ch2", n=2, provided=False)
    with pytest.raises(BenchError, match="full replay refused"):
        staged(bench, req, "run2", extracted={"2": ledger(2, "过桥")})


def _prepare_fakes(monkeypatch, calls, bench_impl, host=None):
    monkeypatch.setattr(adapter, "_setup", lambda *a, **k: (
        Path("/injected/cfg"), Path("/injected/out"), {"project_id": "fiction"},
        bench_impl, host or SimpleNamespace(rulings=lambda project_id: {},
                                            review_contracts=lambda conf: {}), {}))
    monkeypatch.setattr(adapter, "immutable", lambda path, raw: calls.append("write:" + path.name))
    monkeypatch.setattr(adapter, "publish_review_materials",
                        lambda b, run_id, phase, out: calls.append("publish:" + phase))


def _run_prepare():
    return adapter.novel_bench(operation="prepare", workspace_root="/w", run_id="run1",
                               step_id="prepare", config_name=adapter.CONFIG, out_dir="/o")


def test_prepare_replays_supplied_ledger_before_publishing_materials(monkeypatch):
    calls = []

    class FakeBench:
        policy = SimpleNamespace(project_id="fiction")

        def freeze(self, *args):
            calls.append("freeze")

        def input(self, run_id):
            calls.append("input")
            return Path("/injected/frozen"), {"chapters": [], "literary_key": "k",
                                              "contracts": {"ledger": "1" * 64}}

        def replay_provided(self, run_id):
            calls.append("replay")
            return {"early_replay": "passed"}

        def editorial_packet(self, run_id):
            calls.append("editorial_packet")
            return "packet"

        def literary(self, run_id):
            calls.append("literary")

    _prepare_fakes(monkeypatch, calls, FakeBench())
    assert _run_prepare() == {"backup_only": False, "reuse_literary": False}
    assert calls.index("replay") < calls.index("editorial_packet") < calls.index("publish:literary")
    assert calls.index("write:early_replay.json") < calls.index("write:editor_packet.md")


def test_prepare_refuses_before_publishing_when_replay_fails(monkeypatch):
    calls = []

    class FakeBench:
        policy = SimpleNamespace(project_id="fiction")

        def freeze(self, *args):
            calls.append("freeze")

        def input(self, run_id):
            return Path("/injected/frozen"), {"chapters": []}

        def replay_provided(self, run_id):
            raise BenchError("full replay refused: ['character 旅人 is dead but received changes']")

    _prepare_fakes(monkeypatch, calls, FakeBench())
    with pytest.raises(BenchError, match="full replay refused"):
        _run_prepare()
    assert "editorial_packet" not in calls
    assert not any(call.startswith("publish:") for call in calls)
