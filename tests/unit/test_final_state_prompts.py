"""Final output contracts in assembled prompts and generated role migration."""
import json
from pathlib import Path

import pytest

from core.output_migration import migrate_generated_outputs, migrate_role_prompt
from core.prompt_assembler import PromptAssembler, WORKSPACE_LAYOUT
from skillflow.write_tools import generate_write_tool_schemas

ROOT = Path(__file__).resolve().parents[2]
OBSOLETE = ("staging", "repo_apply", "overlay", "promotion", "没有源码", "不再经过")


@pytest.mark.parametrize("name", [
    "coding_impl", "task_implementer", "game_designer", "subagent_work",
    "system_overview", "forge_architect", "forge_emit", "forge_review_red",
    "forge_tool_impl", "step5_verifier",
])
def test_templates_express_current_output_contracts(name):
    text = (ROOT / "templates" / f"{name}.md").read_text().lower()
    for obsolete in OBSOLETE:
        assert obsolete not in text, (name, obsolete)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("target", ["code", "artifact", "mixed"])
def test_assembled_delivery_uses_current_destinations(tmp_path, native, target):
    fixed = ({"report": {"file": "report.json", "target": "artifact"},
              "readme": {"file": "README.md", "target": "code"}}
             if target == "mixed" else {})
    schemas = {s["name"]: s for s in generate_write_tool_schemas(
        "content" if fixed else "write", fixed,
        output_target="artifact" if fixed else target)}
    code = tmp_path / "code"
    code.mkdir()
    workspace = tmp_path / "artifacts"
    workspace.mkdir()
    prompt = PromptAssembler().assemble(
        "5" if fixed else "implement", workspace, code_path=code,
        tool_schemas=schemas, native=native)
    assert WORKSPACE_LAYOUT in prompt
    assert "worktree" in prompt
    for obsolete in OBSOLETE:
        assert obsolete not in prompt.lower()
    if not native:
        destinations = ["artifact", "code"] if fixed else [target]
        for destination in destinations:
            assert f"Destination: {destination}" in prompt


def test_generated_roles_migrate_with_backup_and_preserve_custom_fields(tmp_path):
    root = tmp_path / "configs"
    root.mkdir()
    path = root / "gen_contract.roles.json"
    old = "before（结果里的 `source` 字段会标明来自 `staging` 还是 `repo`）after"
    roles = {"custom": {"system_prompt": old, "model": "custom-model",
                        "tools": ["domain_tool"], "temperature": 0.15},
             "unchanged": {"system_prompt": "Keep the project-specific scope."}}
    original = json.dumps(roles, ensure_ascii=False).encode()
    path.write_bytes(original)
    reports = migrate_generated_outputs(root)
    assert len(reports) == 1 and reports[0]["roles"] == ["custom"]
    assert Path(reports[0]["backup"]).read_bytes() == original
    changed = json.loads(path.read_bytes())
    assert changed["custom"]["system_prompt"] == "before（源码写入本 run 的 worktree）after"
    assert {k: v for k, v in changed["custom"].items() if k != "system_prompt"} == {
        k: v for k, v in roles["custom"].items() if k != "system_prompt"}
    assert changed["unchanged"] == roles["unchanged"]
    after = path.read_bytes()
    assert migrate_generated_outputs(root) == [] and path.read_bytes() == after


def test_media_prompt_migration_preserves_scope_outside_output_block():
    old = ("budget and scope\n"
           "Edits write to this step's **staging**, not directly to the repository.\n"
           "old output instructions\n"
           "promotion and `repo_apply` handle delivery after `finish_step`.\n"
           "media provenance and review requirements")
    new = migrate_role_prompt(old, strict_code=True)
    assert new.startswith("budget and scope\n")
    assert new.endswith("\nmedia provenance and review requirements")
    assert "apply_patch" in new and "references" in new and "review" in new
    for obsolete in OBSOLETE:
        assert obsolete not in new.lower()
    assert migrate_role_prompt(new) == new


def test_relay_prompt_migration_preserves_card_scope_and_later_budget():
    old = ("completed card rules\n"
           "## 接力轮：仓库基线不是本轮的树（硬约束）\n"
           "old staging explanation\n"
           "## 先落盘的硬线\nkeep this budget")
    new = migrate_role_prompt(old)
    assert new.startswith("completed card rules\n")
    assert new.endswith("## 先落盘的硬线\nkeep this budget")
    assert "relay pins" in new and "owns" in new and "worktree" in new
    assert "staging" not in new
    assert migrate_role_prompt(new) == new


def test_unrelated_custom_prompt_is_unchanged():
    prompt = "Use the project's approved shader overlay; honor the card's ownership rules."
    assert migrate_role_prompt(prompt) == prompt


@pytest.mark.parametrize("content", [b"{broken", b"[]"])
def test_invalid_sidecar_is_left_for_normal_registration(tmp_path, content):
    path = tmp_path / "gen_invalid.roles.json"
    path.write_bytes(content)
    assert migrate_generated_outputs(tmp_path) == []
    assert path.read_bytes() == content


@pytest.mark.parametrize("name", [
    "coding_impl", "fix_tests", "subagent_work", "task_implementer",
    "game_designer", "novel_design",
])
def test_code_templates_send_a_stuck_patch_to_a_citation_not_to_more_copying(name):
    """A stale or ambiguous hunk used to be answered with "copy more".

    That answer costs a re-read before it costs anything else: to widen the
    copied context an agent must first go and read enough of the file to widen
    it — which is the loop these templates were feeding. Reference mode answers
    both failures, so the templates have to name it, and must not leave the old
    instruction standing beside it.
    """
    text = (ROOT / "templates" / f"{name}.md").read_text().lower()
    assert "raw=true" in text
    assert "references" in text and "sha" in text
    assert "citation" in text
    for retired in ("逐字复制", "增加前后未改动行", "缩短歧义上下文",
                    "copy current text exactly", "exact copy of current text",
                    "add unchanged surrounding lines",
                    "more unchanged surrounding lines"):
        assert retired not in text, retired


def test_checked_in_generated_impl_uses_current_code_target_contract():
    graph = __import__("yaml").safe_load(
        (ROOT / "configs" / "gen_coop_shell_ui60_20260907.yaml").read_text()
    )
    impl = next(step for step in graph["steps"] if step["id"] == "implement")
    assert impl["output"] == {"mode": "write", "target": "code"}
    assert "lifecycle" not in impl
    role = __import__("yaml").safe_load(
        (ROOT / "agent_configs" / "coding_impl.yaml").read_text()
    )["offload_implementer"]
    assert "apply_patch" in role["tools"]
    assert not {"create", "edit", "write", "repo_remove_file"} & set(role["tools"])
