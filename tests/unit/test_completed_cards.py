"""completed_cards: the step-3 reviewer must know which ids will never run again."""
from aitelier.tools.completed_cards import impl


def test_no_project_means_everything_pending(monkeypatch):
    r = impl.completed_cards(workspace_root="/nowhere/at/all")
    assert r["completed"] == [] and "pending" in r["content"]


def test_completed_ids_are_named_and_marked_done(monkeypatch, tmp_path):
    ws = tmp_path / "ws"; (ws / "proj-x").mkdir(parents=True)
    monkeypatch.setattr("core.datadir.workspaces_dir", lambda: str(ws))
    monkeypatch.setattr("aitelier.tools.task_budget_check.impl._completed_loop_items",
                        lambda pid: {"card_b", "card_a"} if pid == "proj-x" else set())
    r = impl.completed_cards(workspace_root=str(ws / "proj-x"))
    assert r["completed"] == ["card_a", "card_b"]
    assert "ALREADY COMPLETE" in r["content"] and "card_a, card_b" in r["content"]
    assert "Do NOT review them as pending" in r["content"]


def test_a_failing_query_reads_as_nothing_completed(monkeypatch, tmp_path):
    ws = tmp_path / "ws"; (ws / "p").mkdir(parents=True)
    monkeypatch.setattr("core.datadir.workspaces_dir", lambda: str(ws))
    def boom(pid): raise RuntimeError("db down")
    monkeypatch.setattr("aitelier.tools.task_budget_check.impl._completed_loop_items", boom)
    assert impl.completed_cards(workspace_root=str(ws / "p"))["completed"] == []
