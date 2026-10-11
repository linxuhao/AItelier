"""Owned tiny legacy book through real ledger_ready dispatch; no model calls."""
import json
import shutil
import pytest
from aitelier import novel_state as ns
from aitelier.novel_ledger_migrate import migrate
from aitelier.writing_bench.storage import BenchError, BenchReplayRefused, git
from test_bench import bench, ledger, request
from test_workflow import Session

def legacy_book(bench):
    repo = bench.policy.repo
    rec = ledger()
    chapter = ns.chapter_dir(repo, 1)
    chapter.mkdir(parents=True)
    (chapter / "prose.md").write_text("# 第1章：启程\n旅人离开旧屋。\n")
    (chapter / "summary.md").write_text(rec["summary"])
    ns.dump_yaml(chapter / "events.yaml", rec)
    ns.replay_chapter(repo, 1, rec)
    # Historical layout: the journal's state change was kept on the card.
    card_path = ns.characters_dir(repo) / "旅人.yaml"
    card = ns.load_yaml(card_path, {})
    card["progression"] = [{"chapter": 1, "changes": rec["events"][0]["changes"],
                            "reason": rec["events"][0]["reason"]}]
    ns.dump_yaml(card_path, card)
    shutil.rmtree(ns.ledger_dir(repo))
    ns.rebuild_digest(repo)
    ns.rebuild_index(repo)
    git(repo, "add", "--", "novel")
    git(repo, "commit", "-m", "synthetic accepted legacy history")

def snapshot(repo):
    return {str(p.relative_to(repo)): p.read_bytes()
            for p in (repo / "novel").rglob("*") if p.is_file()}

@pytest.mark.parametrize("layout", ["legacy", "migrated", "modern_drift"])
def test_actual_readiness_boundary(tmp_path, monkeypatch, bench, layout):
    legacy_book(bench)
    repo = bench.policy.repo
    before = snapshot(repo)
    dry = migrate(repo, dry_run=True, mode="replay")
    assert dry["drift"] == [] and dry["dry_run"]
    assert snapshot(repo) == before
    if layout != "legacy":
        assert migrate(repo, mode="replay")["drift"] == []
        if layout == "modern_drift":
            p = ns.characters_dir(repo) / "旅人.yaml"
            card = ns.load_yaml(p, {})
            card["power_level"] = 99
            ns.dump_yaml(p, card)
        git(repo, "add", "--", "novel")
        git(repo, "commit", "-m", layout)
    before = snapshot(repo)
    head, genesis = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "novel-genesis")
    session = Session(tmp_path, monkeypatch, bench)
    calls, errors = [], []
    original = session.sf._execute_tool_inline
    def observed(node, **kwargs):
        if node.id == "ledger_ready":
            calls.append(node.id)
        try:
            return original(node, **kwargs)
        except BenchError as exc:
            errors.append({"type": type(exc).__name__, "reason": str(exc)})
            raise
    session.sf._execute_tool_inline = observed
    run = session.start(request(bench, n=2))
    try:
        if layout == "migrated":
            # Stop as soon as readiness releases review; no paid/synthetic review runs.
            for _ in range(20):
                session.sf.advance_run(run)
                if (bench.work(run) / "candidate_replay.json").exists():
                    break
            assert (bench.work(run) / "candidate_replay.json").exists()
            assert calls == ["ledger_ready"] and errors == []
        else:
            for _ in range(20):
                try:
                    session.sf.advance_run(run)
                except BenchError:
                    pass
                if session.sf.get_run(run)["status"] == "failed":
                    break
            print(json.dumps({"layout": layout, "calls": calls, "errors": errors,
                              "status": session.sf.get_run(run)["status"]}))
            assert len(calls) == 1, errors
            assert errors[0]["type"] == "BenchReplayRefused"
            if layout == "legacy":
                assert "legacy in-bible history" in errors[0]["reason"]
                assert "--mode replay --dry-run" in errors[0]["reason"]
                assert "--force" not in errors[0]["reason"]
            else:
                assert errors[0]["reason"] == "accepted replay drift"
                assert "migrat" not in errors[0]["reason"]
            assert session.sf.get_run(run)["status"] == "failed"
            session.sf.advance_run(run)
            assert len(calls) == 1
        assert session.agent_calls == [] and session.backup_calls == []
        assert snapshot(repo) == before
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "rev-parse", "novel-genesis") == genesis
        assert git(repo, "status", "--porcelain") == ""
    finally:
        session.sf._conn.close()
