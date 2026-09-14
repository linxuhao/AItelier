"""Deterministic proof of the frozen authoritative-context contract."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from core.state_database import StateDatabase
from core.state_service import SEED_HEADING, StateService, state_seed_text
from skillflow.read_tools import build_source_map, make_read_tool_fns


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost",
                    "commit", "-qm", message], cwd=repo, check=True)
    return _git(repo, "rev-parse", "HEAD")


def _node(key: str = "rule") -> dict:
    return {"key": key, "goal": "Use the authoritative rule",
            "acceptance": [{"id": "behaviour", "kind": "test", "description": "The rule is applied"}]}


def test_ruling_version_propagates_while_old_attempt_stays_frozen(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="fixture-reviewer")
    service.create_project("fixture", "Authoritative context fixture")
    service.store.add_nodes("fixture", [_node()])
    first = service.design.create_revision("fixture", "rule", 0, "Rule", "Use the old rule.",
        "The old rule is the first approved decision.", [], {"mode": "all"}, lifecycle_status="approved")
    service.design.create_baseline("fixture", "baseline-old", [{"design_id": "rule", "revision": first["revision"]}])
    service.design.bind_node("fixture", "rule", 1, "baseline-old",
        [{"design_id": "rule", "revision": 1, "purpose": "context", "coverage_scope": {"mode": "all"}}],
        "Bind the approved rule.")
    old = service.start_external_attempt("fixture", "rule", 2, "fixture-harness", "old-job", "old-request")
    old_seed = state_seed_text(old["context"], {}, relay=False)
    assert json.loads(old_seed[len(SEED_HEADING):].strip())["design_context"]["baseline_id"] == "baseline-old"
    failure_report=tmp_path/"old-failure.txt"
    failure_body=b'{"status":"failed","settled":true,"usable":true,"reason":"preserved failure"}'
    failure_report.write_bytes(failure_body)
    service.report_external_attempt(old["attempt_id"], "old-failure", 0, old["context_hash"], "failed",
        str(failure_report), hashlib.sha256(failure_body).hexdigest(), True,
        detail="preserved first failure")

    newer = service.design.create_revision("fixture", "rule", 1, "Rule", "Use the new rule.",
        "The owner explicitly revised the decision.", [], {"mode": "all"}, lifecycle_status="approved")
    service.design.create_baseline("fixture", "baseline-new", [{"design_id": "rule", "revision": newer["revision"]}],
        expected_baseline_id="baseline-old")
    service.store.revise_node("fixture", "rule", 2, "Adopt the revised rule.", goal="Use the revised authoritative rule")
    service.design.bind_node("fixture", "rule", 3, "baseline-new",
        [{"design_id": "rule", "revision": 2, "purpose": "context", "coverage_scope": {"mode": "all"}}],
        "Bind the revised approved rule.")
    current = service.start_external_attempt("fixture", "rule", 4, "fixture-harness", "new-job", "new-request")
    new_seed = json.loads(state_seed_text(current["context"], {}, relay=False)[len(SEED_HEADING):].strip())
    assert old["context"]["revision"] == 2
    assert old["context"]["design_context"]["baseline_id"] == "baseline-old"
    assert old["context"]["design_context"]["bindings"][0]["design"]["revision"] == 1
    assert current["context"]["revision"] == 4
    assert new_seed["design_context"]["baseline_id"] == "baseline-new"
    assert new_seed["design_context"]["bindings"][0]["design"]["revision"] == 2
    assert service.design.get_revision("fixture", "rule", 1)["content"]["statement"] == "Use the old rule."
    assert service.attempts.get(old["attempt_id"])["context"] == old["context"]


def test_same_name_sources_are_explicit_and_digest_bound(tmp_path):
    workspace, repo = tmp_path / "workspace", tmp_path / "repo"
    artifact, tool_self = workspace / "fixture" / "artifact", workspace / "fixture" / "current.tmp"
    artifact.mkdir(parents=True); tool_self.mkdir(parents=True); repo.mkdir()
    (repo / "same-name.txt").write_text("repository bytes\n")
    (artifact / "same-name.txt").write_text("artifact bytes\n")
    (tool_self / "same-name.txt").write_text("tool-self bytes\n")
    specs = [{"source_type": "repository", "path": "same-name.txt", "mode": "tool"},
             {"source_type": "step", "step_id": "artifact", "files": ["same-name.txt"], "mode": "tool"}]
    smap = build_source_map(specs, str(workspace), current_config="fixture", code_root=str(repo), step_tmp_dir=str(tool_self))
    readers = make_read_tool_fns(specs, _smap=smap)
    expected = {"repo": "repository bytes\n", "step:artifact": "artifact bytes\n", "self": "tool-self bytes\n"}
    for source, content in expected.items():
        result = readers["read"]("same-name.txt", source=source, raw=True)
        assert result["source"] in {"repo", "step:artifact", "staging"}
        assert result["content"] == content
        assert hashlib.sha256(result["content"].encode()).hexdigest() == hashlib.sha256(content.encode()).hexdigest()
    assert readers["read"]("same-name.txt", source="repo", raw=True)["content"] != readers["read"]("same-name.txt", source="step:artifact", raw=True)["content"]
    assert readers["search"]("bytes", source="repo")["matches"][0]["source"] == "repo"
    assert readers["search"]("bytes", source="step:artifact")["matches"][0]["source"] == "step:artifact"
    assert readers["read"]("same-name.txt", raw=True)["content"] == "tool-self bytes\n"
    assert readers["search"]("bytes")["matches"][0]["source"] == "staging"
