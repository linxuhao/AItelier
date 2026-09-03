"""closeout_gate: the delivered diff decides review DEPTH, never whether to review."""
import subprocess
from pathlib import Path

from aitelier.tools.closeout_gate.impl import closeout_gate


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=path, check=True)
    (path / "scripts").mkdir(); (path / "playtest").mkdir()
    (path / "scripts" / "a.gd").write_text("var x = 1\n")
    (path / "playtest" / "p.yaml").write_text("name: p\n")
    _commit(path, "base")
    return path


def _commit(repo: Path, msg: str):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=repo, check=True)


def test_small_code_only_delivery_is_standard(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "scripts" / "a.gd").write_text("var x = 2\n")
    _commit(repo, "dpe_game/t_impl: card_a [p] (1 file(s))")
    r = closeout_gate(project_root=str(repo))
    assert r["depth"] == "standard"
    assert [f["path"] for f in r["files"]] == ["scripts/a.gd"]
    assert "standard" in r["summary"] and "card_a" in r["summary"]
    assert r["content"] == r["summary"]      # the key the context resolver renders


def test_touching_a_protected_path_is_deep(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "playtest" / "p.yaml").write_text("name: p\nchanged: true\n")
    _commit(repo, "dpe_game/t_impl: card_b [p] (1 file(s))")
    r = closeout_gate(project_root=str(repo))
    assert r["depth"] == "deep" and r["protected"] == ["playtest/p.yaml"]
    assert "protected" in r["reasons"][0]


def test_a_deletion_commit_after_the_delivery_is_part_of_the_card_and_deep(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / "scripts" / "a.gd").write_text("var x = 3\n")
    _commit(repo, "dpe_game/t_impl: card_c [p] (1 file(s))")
    (repo / "scripts" / "a.gd").unlink()
    _commit(repo, "step: t_impl delete [p] card_c 1 file(s)")
    r = closeout_gate(project_root=str(repo))
    assert r["depth"] == "deep" and len(r["commits"]) == 2
    assert any("deletion" in x for x in r["reasons"])


def test_head_that_is_not_a_delivery_is_deep_not_skipped(tmp_path):
    repo = _repo(tmp_path / "r")
    r = closeout_gate(project_root=str(repo))
    assert r["depth"] == "deep" and r["files"] == []


def test_missing_root_is_deep_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = closeout_gate(project_root="")
    assert r["depth"] == "deep" and "error" in r
    assert closeout_gate(project_root="relative/path")["depth"] == "deep"
