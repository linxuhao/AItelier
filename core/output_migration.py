"""Output destination migration, applied only by a supporting runtime at boot.

Data-file changes never repin live/historical runs. Backups retain original bytes;
ambiguous legacy copy contracts fail rather than guessing a destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import yaml

COPY_TOOLS = {"repo_apply", "repo_delete"}
CODE_SLOTS = {"linter_manifest", "readme"}
ARTIFACT_SLOTS = {"design", "report"}
GENERIC_CODE_MUTATORS = {"create", "edit", "write", "repo_remove_file"}
VERIFIER_READ_TOOLS = {"list_tree", "semantic_search", "git_history",
                       "web_search", "web_fetch"}
STRICT_PATCH_GUIDANCE_EN = """Use `apply_patch(patch)` for Add/Update/Delete operations in this run's code
worktree. Read affected ranges with `raw=true`; numbered output is not patch
text. If context is stale or not found, reread and copy the current text exactly.
If context matches more than once, add unchanged surrounding lines until it is
unique; never shrink ambiguous context. Patches use exact matching and complete
preflight. On a partial I/O failure, inspect `written`/`deleted` and reread
those paths before repairing the remainder. A successful call changes only the
uncommitted worktree; validation, review, and delivery remain pending."""
STRICT_PATCH_GUIDANCE_ZH = """## 写文件的工具：`apply_patch(patch)`
用严格补丁批量新建、修改或删除本 run worktree 中的代码文件。修改前用
`read(raw=true)` 读取当前范围；带行号的输出不能复制进补丁。上下文失效或
未找到时，重新 raw 读取并逐字复制当前文本；命中多处时，增加前后未改动行
直到唯一，绝不缩短歧义上下文。补丁先完整预检；若 I/O 失败返回 partial，
检查 written/deleted 并重读这些路径后再修复。成功只代表未提交 worktree
已改变，验证、审查和交付仍未通过。

不要整文件覆盖已有文件。找不到位置时先 semantic_search/search，再只读取
相关范围。后续调用能读到本轮之前已应用的补丁。"""

_VERIFIER_BOUNDARY = """# Final Verifier report-only boundary

This role may create or replace only its declared verification report artifact.
README.md belongs to the earlier delivery_documenter step. Do not call README
writers, generic code mutators, optional-path code writers, or edit any candidate
file. The resolved candidate and README are hashed immediately before and after
this verifier; any byte change is a hard failure."""


def _tools(value):
    if isinstance(value, dict):
        if "tool" in value:
            yield value["tool"]
        for child in value.values():
            yield from _tools(child)
    elif isinstance(value, list):
        for child in value:
            yield from _tools(child)


def _remove_copy_hooks(lifecycle):
    for event in list(lifecycle):
        value = lifecycle[event]
        if isinstance(value, dict) and value.get("tool") in COPY_TOOLS:
            del lifecycle[event]
        elif isinstance(value, list):
            kept = [x for x in value if not isinstance(x, dict) or x.get("tool") not in COPY_TOOLS]
            if kept:
                lifecycle[event] = kept
            else:
                del lifecycle[event]


def migrate_document(document: dict) -> list[dict]:
    changes = []

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        lifecycle = value.get("lifecycle")
        if value.get("id") and isinstance(lifecycle, dict) and "repo_apply" in set(_tools(lifecycle)):
            output = value.setdefault("output", {})
            mode = output.get("mode", value.get("output_mode"))
            fixed = output.get("fixed") or {}
            if not fixed and mode == "write":
                output["target"] = "code"
                output.pop("carry_forward", None)
                destinations = {"*": "code"}
            elif fixed and set(fixed) <= CODE_SLOTS | ARTIFACT_SLOTS:
                output["target"] = "artifact"
                destinations = {}
                for slot, entry in list(fixed.items()):
                    target = "code" if slot in CODE_SLOTS else "artifact"
                    destinations[slot] = target
                    if isinstance(entry, str):
                        entry = {"file": entry}
                        fixed[slot] = entry
                    entry["target"] = target
            else:
                raise ValueError(f"{value['id']}: copy delivery has unknown output contract; classify it explicitly before migrating")
            _remove_copy_hooks(lifecycle)
            if not lifecycle:
                del value["lifecycle"]
            changes.append({"step": value["id"], "destinations": destinations})
        # The native lint tool can auto-fix files. It must run BEFORE the
        # candidate commit, not leave new dirty code behind after delivery.
        output = value.get("output") or {}
        if value.get("id") and output.get("target") == "code":
            lifecycle = value.get("lifecycle") or {}
            checks = lifecycle.get("after_deliver")
            if isinstance(checks, list):
                mutating = [x for x in checks if isinstance(x, dict) and x.get("tool") == "lint"]
                if mutating:
                    remaining = [x for x in checks if x not in mutating]
                    if remaining:
                        lifecycle["after_deliver"] = remaining
                    else:
                        del lifecycle["after_deliver"]
                    if not lifecycle:
                        value.pop("lifecycle", None)
                    validation = value.setdefault("validation", [])
                    for check in mutating:
                        if check not in validation:
                            validation.append(check)
                    changes.append({"step": value["id"], "mutating_checks_moved_before_commit": len(mutating)})
        for child in list(value.values()):
            visit(child)

    visit(document)
    return changes


def _replace_targets(steps: list[dict], old: str, new: str, *, exclude=()) -> int:
    count = 0
    for step in steps:
        if step.get("id") in exclude:
            continue
        for edge in step.get("transitions") or []:
            if isinstance(edge, dict) and edge.get("to") == old:
                edge["to"] = new
                count += 1
    return count


def _single_successor(step: dict, label: str) -> str:
    targets = [edge.get("to") for edge in (step.get("transitions") or [])
               if isinstance(edge, dict) and edge.get("to")]
    if len(targets) != 1:
        raise ValueError(f"{label}: cannot migrate verifier with {len(targets)} successors")
    return targets[0]


def _drop_step_context(step: dict, source_step: str) -> None:
    context = step.get("context")
    if not isinstance(context, list):
        return
    step["context"] = [
        item for item in context
        if not (isinstance(item, dict)
                and isinstance(item.get("source"), dict)
                and item["source"].get("step") == source_step)
    ]


def migrate_readonly_verifier(
        document: dict, *, readme_agent_config: str = "delivery_documenter") -> list[dict]:
    """Split a generated DPE verifier's README and move it behind code owners.

    This is intentionally shape-gated. A generated graph that does not have the
    known DPE step ids is left untouched; a partial/colliding shape fails before
    boot rewrites any bytes instead of guessing release topology.
    """
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list):
        return []
    by_id = {step.get("id"): step for step in steps if isinstance(step, dict)}
    verifier = by_id.get("5")
    if not verifier:
        return []
    output = verifier.get("output") or {}
    fixed = output.get("fixed") or {}
    readme_slots = [
        name for name, slot in fixed.items()
        if ((slot == "README.md") if isinstance(slot, str)
            else isinstance(slot, dict) and slot.get("file") == "README.md")
    ]
    if not readme_slots:
        return []
    required = {"task_loop", "5_review"}
    missing = sorted(required - set(by_id))
    if missing:
        raise ValueError("5: README-owning verifier is not a recognized DPE graph; "
                         f"missing {', '.join(missing)}")
    additions = {"5_readme", "5_candidate_before", "5_candidate_after"}
    collisions = sorted(additions & set(by_id))
    if collisions:
        raise ValueError("5: partial read-only verifier migration; existing "
                         + ", ".join(collisions))
    if len(readme_slots) != 1:
        raise ValueError("5: expected exactly one README output slot")

    old_after_verifier = _single_successor(verifier, "5")
    if not _replace_targets(steps, "5", old_after_verifier, exclude={"5"}):
        raise ValueError("5: no predecessor found for README-owning verifier")

    knowledge = by_id.get("5_knowledge")
    if knowledge:
        old_after_knowledge = _single_successor(knowledge, "5_knowledge")
        if not _replace_targets(steps, "5_knowledge", old_after_knowledge,
                                exclude={"5_knowledge"}):
            raise ValueError("5_knowledge: no predecessor found during verifier migration")

    incoming_review = _replace_targets(steps, "5_review", "5_readme",
                                       exclude={"5_review", "5_knowledge"})
    if not incoming_review:
        raise ValueError("5_review: no post-candidate predecessor found")

    # The game addon historically ran design/compile after the verifier and
    # could therefore consume its report. Those steps now run before the
    # report-only verifier; remove the stale backward reference so a prior
    # iteration's report cannot leak into the candidate-building phase.
    for step_id in ("5_design", "5_compile", "5_game_evidence"):
        if step_id in by_id:
            _drop_step_context(by_id[step_id], "5")

    readme_slot = fixed.pop(readme_slots[0])
    if isinstance(readme_slot, str):
        readme_slot = {"file": readme_slot}
    readme_slot = dict(readme_slot)
    readme_slot.update(target="code", on_exists="replace")
    output["target"] = "artifact"
    for slot in fixed.values():
        if isinstance(slot, dict) and slot.get("file") == "final/verify_report.json":
            slot.setdefault("target", "artifact")
            slot["on_exists"] = "replace"

    owner = {
        "id": "5_readme", "step_type": "agent",
        "agent_config": readme_agent_config,
        "context": list(verifier.get("context") or []),
        "output": {"mode": "content", "target": "artifact",
                   "fixed": {"readme": readme_slot}},
        "transitions": [{"to": "5_candidate_before"}],
    }
    if "5_design" in by_id or "5_compile" in by_id:
        owner["config"] = {"extra_templates": ["game_harness/readme_owner.md"]}
    before = {
        "id": "5_candidate_before", "step_type": "tool",
        "tool_name": "candidate_integrity", "timeout_seconds": 120,
        "tool_params": {"project_root": "$PROJECT_ROOT", "out_dir": "$STEP_DIR",
                        "phase": "snapshot"},
        "transitions": [{"to": "5"}],
    }
    after_target = "5_knowledge" if knowledge else "5_review"
    after = {
        "id": "5_candidate_after", "step_type": "tool",
        "tool_name": "candidate_integrity", "timeout_seconds": 120,
        "tool_params": {"project_root": "$PROJECT_ROOT", "out_dir": "$STEP_DIR",
                        "phase": "verify", "baseline_step": "5_candidate_before"},
        "transitions": [{"to": after_target, "match": {"passed": True}}],
    }
    verifier["transitions"] = [{"to": "5_candidate_after"}]
    if knowledge:
        knowledge["transitions"] = [{"to": "5_review"}]
    review_context = by_id["5_review"].setdefault("context", [])
    integrity_source = {"source": {"step": "5_candidate_after",
                                    "output": "candidate_integrity_report.json",
                                    "required": True}}
    if integrity_source not in review_context:
        review_context.append(integrity_source)
    steps.extend([owner, before, after])
    labels = ((document.get("x-aitelier") or {}).get("labels"))
    if isinstance(labels, dict):
        labels.update({"5_readme": "Delivery README",
                       "5_candidate_before": "Candidate Snapshot",
                       "5_candidate_after": "Candidate Integrity"})
    return [{"step": "5", "readonly_verifier": True,
             "readme_owner": "5_readme", "moved_after": old_after_verifier}]


def require_output_engine() -> None:
    from skillflow.graph import StepNode
    from skillflow.core import SkillFlow
    if ("output_target" not in StepNode.__dataclass_fields__
            or not hasattr(SkillFlow, "output_directory")):
        raise RuntimeError("This AItelier build requires the bundled SkillFlow output-target engine. "
                           "Rebuild/install the matching wheel before restarting; old engines may ignore output.target.")



def write_migrated_config(path: Path, original: bytes, rendered: bytes, backup_dir: Path) -> Path:
    """Shared CLI/boot persistence: original backup, atomic replacement, no lost update."""
    if path.is_symlink():
        raise RuntimeError(f"Refusing to migrate a symlinked config: {path}")
    if path.read_bytes() != original:
        raise RuntimeError(f"Config changed while being migrated: {path}")
    sha = hashlib.sha256(original).hexdigest()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (path.name + "." + sha + ".bak")
    if backup.exists():
        if backup.read_bytes() != original:
            raise RuntimeError(f"Migration backup is corrupt: {backup}")
    else:
        backup.write_bytes(original)
    fd, pending = tempfile.mkstemp(prefix=".output-migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != original:
            raise RuntimeError(f"Config changed while being migrated: {path}")
        os.chmod(pending, path.stat().st_mode & 0o777)
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)
    return backup


def migrate_role_prompt(prompt: str, *, strict_code: bool = False) -> str:
    """Replace known output instructions while preserving role-specific content."""
    replacements = (
        ("（结果里的 `source` 字段会标明来自 `staging` 还是 `repo`）",
         "（源码写入本 run 的 worktree）"),
        ("（源码直接写入本 run 的 worktree，没有源码 staging 副本）",
         "（源码写入本 run 的 worktree）"),
        ("这是一个声明式内容输出，写入后由本步骤的 repo_apply 自动提交进**项目仓库**，",
         "该输出目标为 code，写入本 run 的 worktree，并由引擎校验和记录候选提交，"),
        ("你有 `create` / `edit` 两个工具，落盘目录由引擎绑定，之后由 `repo_apply` 提交进\n项目仓库：",
         "你有 `create` / `edit` 两个工具，源码写入本 run 的 worktree。\n校验通过后，引擎记录候选提交："),
        ("distinct destinations. No manual repo_apply or commit.",
         "distinct destinations. The engine records the candidate commit."),
    )
    for old, new in replacements:
        prompt = prompt.replace(old, new)

    start = "Edits write to this step's **staging**, not directly to the repository."
    end = "promotion and `repo_apply` handle delivery after `finish_step`."
    if strict_code and start in prompt and end in prompt[prompt.index(start):]:
        a, b = prompt.index(start), prompt.index(end, prompt.index(start)) + len(end)
        prompt = prompt[:a] + STRICT_PATCH_GUIDANCE_EN + prompt[b:]

    if strict_code:
        legacy_en = (
            "Edits write to this run's code worktree. Use the same repo-relative path "
            "for create, edit, read, search and tests. Each edit uses the current file, "
            "including previous edits. If a match fails, read the affected region "
            "with raw=true before retrying.\n\n"
            "Supply both old_str and new_str; an explicit empty new_str deletes the "
            "matched text. Successful writes confirm persistence. The engine validates "
            "the candidate and records its commit after finish_step. Independent review "
            "determines acceptance."
        )
        prompt = prompt.replace(legacy_en, STRICT_PATCH_GUIDANCE_EN)
        section_start = "## 写文件的工具：`create`（新文件）/ `edit`（改已有文件）"
        section_end = "\n## 输出"
        if section_start in prompt and section_end in prompt[prompt.index(section_start):]:
            a = prompt.index(section_start)
            b = prompt.index(section_end, a)
            prompt = prompt[:a] + STRICT_PATCH_GUIDANCE_ZH + prompt[b:]
        design_start = "## 工作方式：外科手术，不是重写"
        design_end = "\n## 硬约束"
        if design_start in prompt and design_end in prompt[prompt.index(design_start):]:
            a = prompt.index(design_start)
            b = prompt.index(design_end, a)
            prompt = prompt[:a] + design_start + "\n\n" + STRICT_PATCH_GUIDANCE_ZH + prompt[b:]
    start = "## 接力轮：仓库基线不是本轮的树（硬约束）"
    end = "\n## 先落盘的硬线"
    if start in prompt and end in prompt[prompt.index(start):]:
        a, b = prompt.index(start), prompt.index(end, prompt.index(start))
        prompt = prompt[:a] + (
            "## 接力输入与任务覆盖\n\n"
            "接力恢复的源码位于本 run 的 worktree。按卡片中的 relay pins、owns 和 "
            "detailed_requirements 核对本轮范围；relay pins 标识已接收的文件版本。\n"
            "审查重点是待办卡片是否覆盖剩余需求。对已接收且被明确锁定的文件，遵守卡片的修改权限。"
            "若当前文件与 pin 不符，报告具体文件和校验差异。\n"
            "指出覆盖缺口时，引用当前 worktree 的文件、对应需求和卡片归属；"
            "将已完成工作与本轮待办区分开。\n"
        ) + prompt[b:]
    return prompt


def migrate_final_verifier_prompt(prompt: str) -> str:
    """Remove known README ownership prose while retaining project additions."""
    if prompt.startswith(_VERIFIER_BOUNDARY):
        prompt = prompt[len(_VERIFIER_BOUNDARY):].lstrip("\n")
    prompt = prompt.replace(
        "你负责验证裁定，并产出/更新项目交付文档 `README.md`。",
        "你负责验证裁定，并且是 report-only；README 由更早的命名所有者负责。",
    )
    prompt = prompt.replace(
        "并产出/更新项目交付文档 `README.md`",
        "；README 由更早的命名所有者负责",
    )
    prompt = re.sub(
        r"\n4\. \*\*产出文档\*\*:.*?(?=\n## 关键约束)",
        "\n",
        prompt,
        flags=re.DOTALL,
    )
    prompt = re.sub(r"^.*create_readme.*(?:\n|$)", "", prompt,
                    flags=re.MULTILINE)
    prompt = re.sub(
        r"^.*(?:write_readme|edit_readme|apply_patch|repo_remove_file).*(?:\n|$)",
        "", prompt, flags=re.MULTILINE | re.IGNORECASE,
    )
    prompt = prompt.replace(
        "- **写作为主**: 工具调用中至少 1/3 应是写入操作，不要只读不写",
        "- **report-only**: 只写验证报告，不得修改 README 或候选代码",
    )
    return _VERIFIER_BOUNDARY + "\n\n" + prompt


def migrate_readonly_verifier_roles(document: dict, roles: dict) -> list[dict]:
    """Apply the report-only boundary to in-memory, already-namespaced roles."""
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list) or not isinstance(roles, dict):
        return []
    verifier = next((step for step in steps
                     if isinstance(step, dict) and step.get("id") == "5"), None)
    if not verifier or not verifier.get("agent_config"):
        return []
    name = verifier["agent_config"]
    role = roles.get(name)
    if not isinstance(role, dict):
        return []
    changes = []
    prompt = role.get("system_prompt")
    if isinstance(prompt, str):
        migrated = migrate_final_verifier_prompt(prompt)
        if migrated != prompt:
            role["system_prompt"] = migrated
            changes.append("prompt")
    tools = role.get("tools")
    if isinstance(tools, list):
        migrated_tools = [tool for tool in tools if tool in VERIFIER_READ_TOOLS]
        if migrated_tools != tools:
            role["tools"] = migrated_tools
            changes.append("tools")
    return [{"role": name, "readonly_verifier": changes}] if changes else []


def validate_readonly_verifier(document: dict, roles: dict) -> None:
    """Reject a partial report-only DPE boundary before it can become live."""
    steps = document.get("steps") if isinstance(document, dict) else None
    if not isinstance(steps, list):
        return
    by_id = {step.get("id"): step for step in steps if isinstance(step, dict)}
    boundary = {"5_readme", "5_candidate_before", "5_candidate_after"}
    if not (boundary & set(by_id)):
        return
    missing = sorted(boundary - set(by_id))
    if missing:
        raise ValueError("partial read-only verifier boundary; missing "
                         + ", ".join(missing))
    verifier = by_id.get("5")
    if not verifier:
        raise ValueError("read-only verifier boundary has no step 5")

    def targets(step_id: str) -> list[str | None]:
        return [edge.get("to") for edge in by_id[step_id].get("transitions", [])
                if isinstance(edge, dict)]

    if targets("5_readme") != ["5_candidate_before"]:
        raise ValueError("README owner must immediately precede candidate snapshot")
    if targets("5_candidate_before") != ["5"]:
        raise ValueError("candidate snapshot must immediately precede verifier")
    if targets("5") != ["5_candidate_after"]:
        raise ValueError("verifier must immediately precede candidate comparison")
    after_edges = by_id["5_candidate_after"].get("transitions") or []
    if (len(after_edges) != 1 or not isinstance(after_edges[0], dict)
            or after_edges[0].get("match") != {"passed": True}):
        raise ValueError("candidate comparison must gate its only successor on passed=true")

    fixed = ((verifier.get("output") or {}).get("fixed") or {})
    if set(fixed) != {"report"}:
        raise ValueError("final verifier must declare exactly one report output")
    report = fixed["report"]
    if (not isinstance(report, dict) or report.get("target") != "artifact"
            or report.get("file") != "final/verify_report.json"):
        raise ValueError("final verifier report must be the engine-bound artifact slot")
    if verifier.get("lifecycle"):
        raise ValueError("final verifier must not have lifecycle writers")

    owner = by_id["5_readme"]
    owner_fixed = ((owner.get("output") or {}).get("fixed") or {})
    readme = owner_fixed.get("readme")
    if (owner.get("output", {}).get("mode") != "content"
            or set(owner_fixed) != {"readme"}
            or not str(owner.get("agent_config") or "").endswith("delivery_documenter")
            or not isinstance(readme, dict)
            or readme.get("file") != "README.md"
            or readme.get("target") != "code"
            or owner.get("lifecycle")):
        raise ValueError("README owner must use one engine-bound fixed code slot")
    owner_role = roles.get(owner.get("agent_config")) if isinstance(roles, dict) else None
    if isinstance(owner_role, dict):
        owner_tools = owner_role.get("tools")
        if (not isinstance(owner_tools, list)
                or any(tool not in VERIFIER_READ_TOOLS for tool in owner_tools)):
            raise ValueError("README owner role contains an optional-path writer")

    for step_id, phase in (("5_candidate_before", "snapshot"),
                           ("5_candidate_after", "verify")):
        step = by_id[step_id]
        params = step.get("tool_params") or {}
        if (step.get("step_type") != "tool"
                or step.get("tool_name") != "candidate_integrity"
                or params.get("project_root") != "$PROJECT_ROOT"
                or params.get("out_dir") != "$STEP_DIR"
                or params.get("phase") != phase
                or (phase == "verify"
                    and params.get("baseline_step") != "5_candidate_before")):
            raise ValueError(f"{step_id} is not an engine-bound candidate_integrity step")

    role_name = verifier.get("agent_config")
    role = roles.get(role_name) if isinstance(roles, dict) else None
    if not isinstance(role, dict):
        raise ValueError("final verifier role is missing; refusing generic fallback")
    tools = role.get("tools")
    if not isinstance(tools, list) or any(tool not in VERIFIER_READ_TOOLS for tool in tools):
        raise ValueError("final verifier role contains a non-read tool")
    prompt = role.get("system_prompt")
    if not isinstance(prompt, str) or not prompt.startswith(_VERIFIER_BOUNDARY):
        raise ValueError("final verifier role prompt lacks report-only boundary")


def _code_roles(config_dir: Path) -> set[str]:
    """Roles for explicit generic code outputs; artifacts keep staged create/edit."""
    names: set[str] = set()

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            output = value.get("output") or {}
            role = value.get("agent_config")
            if (isinstance(role, str) and output.get("target") == "code"
                    and output.get("mode", value.get("output_mode")) == "write"
                    and not output.get("fixed")):
                names.add(role)
            for child in value.values():
                visit(child)

    for path in sorted(config_dir.glob("gen_*.yaml")):
        try:
            document = yaml.safe_load(path.read_bytes())
        except (yaml.YAMLError, UnicodeError):
            continue
        visit(document)
    return names


def _readonly_verifier_roles(config_dir: Path) -> set[str]:
    names = set()
    for path in sorted(config_dir.glob("gen_*.yaml")):
        try:
            document = yaml.safe_load(path.read_bytes())
        except (yaml.YAMLError, UnicodeError):
            continue
        for step in (document or {}).get("steps", []):
            if (isinstance(step, dict) and step.get("id") == "5"
                    and step.get("agent_config")
                    and any(s.get("id") == "5_candidate_after"
                            for s in (document or {}).get("steps", [])
                            if isinstance(s, dict))):
                names.add(step["agent_config"])
    return names


def _migrate_generated_role_prompts(config_dir: Path, backup_dir: Path) -> list[dict]:
    reports = []
    code_roles = _code_roles(config_dir)
    verifier_roles = _readonly_verifier_roles(config_dir)
    for path in sorted(config_dir.glob("gen_*.roles.json")):
        original = path.read_bytes()
        try:
            roles = json.loads(original)
        except (json.JSONDecodeError, UnicodeError):
            continue  # normal registration reports invalid role sidecars
        if not isinstance(roles, dict):
            continue
        changed = []
        tool_roles = []
        for name, role in roles.items():
            if not isinstance(role, dict):
                continue
            strict_code = name in code_roles
            before = role.get("system_prompt")
            if isinstance(before, str):
                after = migrate_role_prompt(before, strict_code=strict_code)
                if name in verifier_roles:
                    after = migrate_final_verifier_prompt(after)
                if after != before:
                    role["system_prompt"] = after
                    changed.append(name)
            if strict_code:
                tools = role.get("tools")
                if tools is None:
                    tools = []
                if isinstance(tools, list):
                    migrated = [tool for tool in tools if tool not in GENERIC_CODE_MUTATORS]
                    if "apply_patch" not in migrated:
                        migrated.append("apply_patch")
                    if migrated != tools:
                        role["tools"] = migrated
                        tool_roles.append(name)
            if name in verifier_roles and isinstance(role.get("tools"), list):
                tools = role["tools"]
                migrated = [tool for tool in tools if tool in VERIFIER_READ_TOOLS]
                if migrated != tools:
                    role["tools"] = migrated
                    if name not in tool_roles:
                        tool_roles.append(name)
        if changed or tool_roles:
            rendered = (json.dumps(roles, ensure_ascii=False, indent=2) + "\n").encode()
            backup = write_migrated_config(path, original, rendered, backup_dir)
            reports.append({"path": str(path), "backup": str(backup),
                            "before_sha256": hashlib.sha256(original).hexdigest(),
                            "after_sha256": hashlib.sha256(rendered).hexdigest(),
                            "roles": changed, "tool_roles": tool_roles})
    return reports


def migrate_generated_outputs(config_dir: Path) -> list[dict]:
    require_output_engine()
    from skillflow.graph import PipelineGraph
    backup_dir = config_dir.parent / "migration_backups" / "readonly-verifier-v2"
    reports = []
    for path in sorted(config_dir.glob("gen_*.yaml")):
        original = path.read_bytes()
        try:
            document = yaml.safe_load(original)
        except yaml.YAMLError:
            continue  # normal registration reports a malformed pre-existing file
        if not isinstance(document, dict):
            continue
        changes = migrate_document(document)
        readonly_changes = migrate_readonly_verifier(document)
        changes.extend(readonly_changes)
        if not changes:
            continue
        PipelineGraph._from_dict(document)  # validate everything before any write
        if readonly_changes:
            # Validate the paired sidecar before atomically replacing either
            # file. A missing/stale verifier role must not leave a migrated graph
            # on disk that would boot with the generic write-capable fallback.
            roles_path = path.with_suffix(".roles.json")
            if not roles_path.is_file():
                raise ValueError(f"{path.name}: read-only verifier role sidecar is missing")
            roles = json.loads(roles_path.read_bytes())
            if not isinstance(roles, dict):
                raise ValueError(f"{roles_path.name}: role sidecar is not a mapping")
            migrate_readonly_verifier_roles(document, roles)
            validate_readonly_verifier(document, roles)
        rendered = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
        sha = hashlib.sha256(original).hexdigest()
        backup = write_migrated_config(path, original, rendered, backup_dir)
        reports.append({"path": str(path), "backup": str(backup), "before_sha256": sha,
                        "after_sha256": hashlib.sha256(rendered).hexdigest(), "changes": changes})
    reports.extend(_migrate_generated_role_prompts(config_dir, backup_dir))
    if reports:
        from skillflow.output_targets import atomic_json
        atomic_json(backup_dir / "last-migration.json", {"configs": reports,
                    "note": "Only registered config files changed; pinned run graph versions were not repointed."})
    return reports
