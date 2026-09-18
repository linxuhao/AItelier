from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aitelier import novel_state as ns
from aitelier.writing_bench.bench import Bench, Policy, validate_ledger
from aitelier.writing_bench.storage import BenchError, decode, encode, git, read_file, sha

CONTRACTS = dict(literary="1" * 64, ledger="2" * 64, extractor="3" * 64)
RULES = {"entries": [{"address": "note://fiction/test", "body": "人物可以失败，既有代价必须保留。"}]}


@pytest.fixture
def bench(tmp_path):
    repo = tmp_path / "novel"
    repo.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Bench Test")
    git(repo, "config", "user.email", "bench@test.invalid")
    (repo / "novel/bible").mkdir(parents=True)
    (repo / "novel/bible/overview.md").write_text("# 总纲\n旅人寻找回家的路。\n")
    (repo / "novel/bible/compass.md").write_text("旅程的方向，不是已经发生的结局。\n")
    ns.dump_yaml(repo / "novel/bible/pacing.yaml", {"min_chars_per_chapter": 1, "max_chars_per_chapter": 5000})
    initial = {"name": "旅人", "status": "alive", "power_level": 1, "tier": 0}
    ns.dump_yaml(repo / "novel/bible/characters/旅人.yaml", {**initial, "initial": initial, "progression": []})
    ns.dump_yaml(repo / "novel/bible/world.yaml", {"factions": {"旅队": {"members": ["旅人"], "progression": [], "initial": {"members": ["旅人"]}}}})
    ns.dump_yaml(repo / "novel/bible/threads.yaml", [])
    ns.dump_yaml(repo / "novel/bible/arcs.yaml", [])
    ns.rebuild_digest(repo)
    ns.rebuild_index(repo)
    git(repo, "add", "--", "novel")
    git(repo, "commit", "-m", "synthetic genesis")
    genesis = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", "novel-genesis")
    inputs = tmp_path / "author"
    inputs.mkdir()
    return Bench(Policy("fiction", repo, "master", genesis, inputs, tmp_path / "store"))


def ledger(n=1, title="启程", location="门外"):
    return {"chapter": n, "title": title,
            "summary": "旅人决定离开旧屋。\n\n在门外停步，发现远处有人等候。",
            "events": [{"entity_type": "protagonist", "entity_name": "旅人",
                        "changes": {"位置": location}, "reason": "实际出门。"}],
            "appearances": [{"name": "旅人", "importance": 5}],
            "locations": [location], "thread_updates": [], "arc_updates": []}


def request(bench, sid="draft1", n=1, provided=True, mode="new", value=None):
    folder = bench.policy.submission_root / sid
    folder.mkdir(exist_ok=True)
    title = "启程" if n == 1 else "过桥"
    (folder / "prose.md").write_text(f"# 第{n}章：{title}\n\n旅人把旧钥匙留在桌上，推开门。\n她带着昨天的疑问，走向村口。\n")
    (folder / "brief.md").write_text("本章目的：人物自己选择出发。")
    ch = {"chapter": n, "title": title, "prose_file": sid + "/prose.md"}
    if provided:
        (folder / "events.json").write_bytes(encode(value or ledger(n, title)))
        ch["proposed_events_file"] = sid + "/events.json"
    return {"version": 2, "project_id": "fiction", "submission_id": sid,
            "base_commit": git(bench.policy.repo, "rev-parse", "HEAD"),
            "mode": mode, "chapters": [ch], "brief_file": sid + "/brief.md"}


def verdict(key, **kwargs):
    return {"review_key": key, "passed": True, "read_complete": True,
            "feedback": "读过冻结全文，当前检查通过。", "findings": [], **kwargs}


def staged(bench, req=None, run="run1", extracted=None):
    req = req or request(bench)
    bench.freeze(req, run, RULES, CONTRACTS)
    _, manifest = bench.input(run)
    bench.literary(run, verdict(manifest["literary_key"]))
    ledgers = bench.ledgers(run, extracted)
    return bench.stage(run, verdict(ledgers["review_key"]))


def accept(bench, stage, run="run1"):
    return bench.promote(run, sha(encode(stage)))


def test_real_git_freeze_stage_manual_accept_and_backup(bench):
    req = request(bench)
    old = git(bench.policy.repo, "rev-parse", "HEAD")
    stage = staged(bench, req)
    assert stage["requires_manual_approval"] and not stage["accepted"]
    assert git(bench.policy.repo, "rev-parse", "HEAD") == old
    assert not (bench.policy.repo / "novel/chapters/ch0001").exists()
    with pytest.raises(BenchError, match="manual approval"):
        bench.promote("run1", "0" * 64)
    receipt = accept(bench, stage)
    assert git(bench.policy.repo, "rev-parse", "HEAD^") == old
    assert git(bench.policy.repo, "status", "--porcelain") == ""
    assert receipt["status"] == "accepted_backup_pending"
    assert bench.promote("run1", sha(encode(stage))) == receipt
    assert b"\n\n" in (bench.policy.repo / "novel/chapters/ch0001/summary.md").read_bytes()
    index = ns.load_yaml(bench.policy.repo / "novel/state/index.yaml", {})
    assert index["chapters"][1]["summary"] == ledger()["summary"].split("\n")[0]
    assert stage["summary_refs"]["1"]["complete_ref"].endswith("summary.md")
    # This is a simulated remote proof, not a live network verification.
    proof = {"verified": True, "repository_private_after": True,
             "remote_master_after": stage["commit"], "remote_tag_after": bench.policy.genesis,
             "force": False, "mirror": False}
    bad = dict(proof, repository_private_after=False)
    with pytest.raises(BenchError, match="private"):
        bench.record_backup("run1", stage["commit"], bad)
    completion = bench.record_backup("run1", stage["commit"], proof)
    assert completion["status"] == "backed_up" and not completion["published"]


@pytest.mark.parametrize("bad", ["../secret.md", "/etc/passwd", "x/../secret.md", "./draft/prose.md", "x\\secret"])
def test_ref_path_traversal_refused(bench, bad):
    req = request(bench)
    req["chapters"][0]["prose_file"] = bad
    with pytest.raises((BenchError, OSError)):
        bench.freeze(req, "run1", RULES, CONTRACTS)
    assert git(bench.policy.repo, "rev-parse", "HEAD") == req["base_commit"]


def test_symlink_and_oversized_ref_refused(bench, tmp_path):
    req = request(bench)
    link = bench.policy.submission_root / "escape"
    link.symlink_to(tmp_path)
    req["chapters"][0]["prose_file"] = "escape/private"
    with pytest.raises(OSError):
        bench.freeze(req, "run1", RULES, CONTRACTS)
    req["chapters"][0]["prose_file"] = "huge.md"
    (bench.policy.submission_root / "huge.md").write_text("x" * 2_000_001)
    with pytest.raises(BenchError, match="limit"):
        bench.freeze(req, "run1", RULES, CONTRACTS)


def test_duplicate_json_and_nonfinite_refused():
    with pytest.raises(BenchError, match="duplicate"):
        decode(b'{"events":[],"events":[1]}')
    with pytest.raises(BenchError, match="non-finite"):
        decode(b'{"value":NaN}')


def test_ch22_nested_duplicate_regression():
    value = ledger()
    value["events"][0]["changes"] = {"学习进度": {"已做": "实际微量引入"}, "已做": "实际微量引入"}
    with pytest.raises(BenchError, match="duplicated nested"):
        validate_ledger(value, 1, "启程")


def test_author_ledger_not_sent_to_extractor_or_replaced(bench):
    req = request(bench)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    with pytest.raises(BenchError, match="cannot replace"):
        bench.ledgers("run1", {"1": ledger()})
    assert bench.ledgers("run1")["ledgers"] == {"1": ledger()}
    path, _ = bench.input("run1")
    assert decode(read_file(path, "chapters/ch0001/proposed_events.json")) == ledger()


def test_prose_only_extracts_missing_ledger(bench):
    req = request(bench, provided=False)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    with pytest.raises(BenchError, match="missing extracted"):
        bench.ledgers("run1")
    result = bench.ledgers("run1", {"1": ledger()})
    assert result["ledgers"]["1"] == ledger()


def test_original_file_changes_do_not_change_frozen_run(bench):
    req = request(bench)
    first = bench.freeze(req, "run1", RULES, CONTRACTS)
    (bench.policy.submission_root / "draft1/prose.md").write_text("changed after submission")
    assert bench.freeze(req, "run1", RULES, CONTRACTS) == first
    assert "changed after submission" not in bench.editorial_packet("run1")
    with pytest.raises(BenchError):
        bench.freeze(req, "run2", RULES, CONTRACTS)


def test_same_submission_id_different_valid_content_conflicts(bench):
    req = request(bench)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    path = bench.policy.submission_root / "draft1/prose.md"
    path.write_text(path.read_text() + "门边的灯忽然熄了。\n")
    with pytest.raises(BenchError, match="content conflict"):
        bench.freeze(req, "run2", RULES, CONTRACTS)


def test_frozen_input_and_baseline_tampering_fail_closed(bench):
    req = request(bench)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    root, manifest = bench.input("run1")
    p = root / "baseline/novel/bible/overview.md"
    p.write_text("different accepted story")
    with pytest.raises(BenchError, match="frozen"):
        bench.read("run1", "novel/bible/overview.md")
    p = root / "chapters/ch0001/prose.md"
    p.write_text("wrong")
    with pytest.raises(BenchError, match="frozen input"):
        bench.input("run1")


def test_review_binding_and_blockers(bench):
    bench.freeze(request(bench), "run1", RULES, CONTRACTS)
    _, m = bench.input("run1")
    for result in [verdict("wrong"), verdict(m["literary_key"], read_complete=False),
                   verdict(m["literary_key"], findings=[{"severity": "blocker", "reason": "contradiction"}])]:
        with pytest.raises(BenchError):
            bench.literary("run1", result)


def test_review_reuse_only_identical_literary_dependencies(bench):
    req = request(bench)
    bench.freeze(req, "run1", RULES, CONTRACTS)
    _, m = bench.input("run1")
    bench.literary("run1", verdict(m["literary_key"]))
    revised = ledger(location="院门")
    req2 = request(bench, sid="ledgerfix", value=revised)
    req2["reuse_literary_from"] = "run1"
    bench.freeze(req2, "run2", RULES, CONTRACTS)
    assert bench.literary("run2")["reused_from"] == "run1"
    assert bench.ledgers("run2")["ledgers"]["1"] == revised
    req3 = request(bench, sid="changed_rules")
    req3["reuse_literary_from"] = "run1"
    bench.freeze(req3, "run3", {"entries": []}, CONTRACTS)
    with pytest.raises(BenchError, match="dependencies changed"):
        bench.literary("run3")


def test_multi_chapter_revision_is_single_commit_and_full_summary(bench):
    first = staged(bench)
    accept(bench, first)
    second_req = request(bench, sid="second", n=2)
    second = staged(bench, second_req, "run2")
    accept(bench, second, "run2")
    before = git(bench.policy.repo, "rev-parse", "HEAD")
    revise1 = request(bench, sid="revision1", mode="revision", value=ledger(location="新门"))
    revise2 = request(bench, sid="revision2", n=2, mode="revision", value=ledger(2, "过桥", "新桥"))
    revise1["chapters"].append(revise2["chapters"][0])
    third = staged(bench, revise1, "run3")
    assert third["counters"] == {"chapters_written": 2, "last_chapter": 2, "next_chapter": 3}
    assert git(bench.policy.repo, "rev-parse", "HEAD") == before
    assert git(bench.policy.repo, "rev-parse", third["commit"] + "^") == before
    accept(bench, third, "run3")
    assert ns.load_characters(bench.policy.repo)["旅人"]["位置"] == "新桥"
    assert len(ns.load_characters(bench.policy.repo)["旅人"]["progression"]) == 2


def test_stage_and_approval_artifact_tampering_refused(bench):
    stage = staged(bench)
    raw = bench.work("run1") / "audit.json"
    value = json.loads(raw.read_text())
    value["feedback"] += " changed after preview"
    raw.write_bytes(encode(value))
    with pytest.raises(BenchError, match="evidence changed"):
        accept(bench, stage)


def test_changed_canonical_branch_never_overwritten(bench):
    stage = staged(bench)
    (bench.policy.repo / "README.md").write_text("another writer")
    git(bench.policy.repo, "add", "--", "README.md")
    git(bench.policy.repo, "commit", "-m", "concurrent change")
    current = git(bench.policy.repo, "rev-parse", "HEAD")
    with pytest.raises(BenchError, match="advanced"):
        accept(bench, stage)
    assert git(bench.policy.repo, "rev-parse", "HEAD") == current


def test_context_budget_is_explicit_not_truncated(bench):
    limited = Bench(Policy(bench.policy.project_id, bench.policy.repo, bench.policy.branch,
                          bench.policy.genesis, bench.policy.submission_root, bench.policy.store, 3000))
    req = request(limited)
    (limited.policy.submission_root / "draft1/brief.md").write_text("计划" * 900)
    limited.freeze(req, "run1", RULES, CONTRACTS)
    with pytest.raises(BenchError, match="exceeds budget"):
        limited.editorial_packet("run1")


def test_readonly_tool_ranges_and_recap_de_duplication(bench):
    stage = staged(bench)
    accept(bench, stage)
    bench.freeze(request(bench, sid="next", n=2), "run2", RULES, CONTRACTS)
    packet = bench.editorial_packet("run2")
    assert packet.count("来源：novel/chapters/ch0001/prose.md") == 1
    assert "来源：novel/state/digest.md" not in packet
    piece = bench.read("run2", "novel/chapters/ch0001/prose.md", 0, 10)
    assert not piece["complete"] and piece["next_start"] == 10
    with pytest.raises(BenchError):
        bench.read("run2", "../author/draft1/prose.md")
