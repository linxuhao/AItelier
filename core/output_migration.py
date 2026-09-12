"""Output destination migration, applied only by a supporting runtime at boot.

Data-file changes never repin live/historical runs. Backups retain original bytes;
ambiguous legacy copy contracts fail rather than guessing a destination.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

COPY_TOOLS = {"repo_apply", "repo_delete"}
CODE_SLOTS = {"linter_manifest", "readme"}
ARTIFACT_SLOTS = {"design", "report"}
GENERIC_CODE_MUTATORS = {"create", "edit", "write", "repo_remove_file"}
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


def _migrate_generated_role_prompts(config_dir: Path, backup_dir: Path) -> list[dict]:
    reports = []
    code_roles = _code_roles(config_dir)
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
    backup_dir = config_dir.parent / "migration_backups" / "output-target-v1"
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
        if not changes:
            continue
        PipelineGraph._from_dict(document)  # validate everything before any write
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
