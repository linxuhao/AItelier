# Skillflow v2 Architecture Design

## Overview

Skillflow is a generic LLM pipeline config reader/executor. AItelier is a host (front+back) that runs skillflow configs. A pipeline is fully defined by **config files + templates + tools** — AItelier itself contains no pipeline-specific logic.

```
┌─ Skillflow (generic) ──────────────────────────────────────┐
│ • Graph execution (claim/confirm/advance/recover)         │
│ • Conditional transitions on output fields                │
│ • Tool registry + dynamic import                          │
│ • Tool nodes (non-LLM execution)                          │
│ • Context assembly from cross-config + previous steps     │
│ • Stale claim recovery (built into poll loop)             │
│ • Feedback loopback (tool node error → agent retry)       │
└───────────────────────────────────────────────────────────┘

┌─ skillflow/tools/ ─────────────────────────────────────────┐
│ • read_file, write, list_tree, web_search, web_fetch      │
│ • repo_apply, repo_validate                               │
│ • json_schema, py_lint, cpp_compile                       │
│ • save_draft_brief, suggest_submit_project                │
│ • Each tool: tool.yaml (schema) + impl.py (code)          │
└───────────────────────────────────────────────────────────┘

┌─ Configs ─────────────────────────────────────────────────┐
│ • meta_conversation.yaml  (graph config)                  │
│ • dpe_default.yaml        (graph config)                  │
│ • novel_writing.yaml      (graph config)                  │
│ • agent_configs/*.yaml    (LLM config by step_id)         │
│ • templates/*.md          (per-step system prompts)       │
└───────────────────────────────────────────────────────────┘

┌─ AItelier (host) ─────────────────────────────────────────┐
│ • Load config → register graph + agent configs            │       
│ • UI rendering (project list, checkpoint review)          │
│ • User management, SSE streaming, DB                      │
│ • Meta conversation entry point                           │
└───────────────────────────────────────────────────────────┘
```

---

## 1. Configuration Separation

### Graph Config → defines step I/O + flow (skillflow reads)
### Agent Config → defines LLM call parameters (skillflow reads, indexed by step_id)

```
┌─ Graph Config (skillflow) ───────────────────────────────────┐
│ 定义 step 的 I/O + 流程                                      │
│                                                              │
│ • id, type (agent / tool / gate)                             │
│ • agent_config: "name"        → 引用 agent config            │
│ • context                     → 输入来源声明                  │
│ • output                      → 产出约束                      │
│ • validation                  → 验证规则 (tool 列表)          │
│ • checkpoint                  → 用户审批点                     │
│ • transitions                 → 流转规则 + max_loop + feedback │
└──────────────────────────────────────────────────────────────┘
         step_id 引用
         │
         ▼
┌─ Agent Config (skillflow) ────────────────────────────────────┐
│ 定义 LLM 怎么调用                                              │
│                                                              │
│ • model, temperature                                         │
│ • template                                                   │
│ • tools (可用工具列表)                                        │
│ • thinking (enable, effort)                                  │
│ • max_tool_turns                                             │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Complete Graph Config Example

```yaml
# configs/dpe_default.yaml
name: "dpe_default"
description: "AItelier DPE: Research → Architect → PM → Task Loop → Verify"

begin: "1_5"

end_conditions:
  combinator: or
  conditions:
    - type: node_reached
      node: "5"
    - type: max_total_steps
      limit: 200
    - type: max_run_duration_seconds
      limit: 3600

steps:
  # ════════════════════════════════════════════════════════════
  # Project Planning Phase
  # ════════════════════════════════════════════════════════════

  - id: "1_5"
    type: "agent"
    agent_config: "researcher"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { config: "meta_conversation", step: "meta", output: "step1_goals.json" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        sota: "step1_5_sota.md"
    checkpoint: true
    checkpoint_label: "SOTA / Codebase Analysis Review"
    transitions:
      - to: "1_5_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "1_5_review"
    type: "agent"
    agent_config: "researcher_reviewer"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "1_5", output: "step1_5_sota.md" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    validation:
      - files: ["review_verdict.json"]
        tool: "json_schema"
        inline_schema:
          type: object
          required: [passed]
          properties:
            passed: { type: boolean }
            feedback: { type: string }
            suggestions:
              type: array
              items: { type: string }
    transitions:
      - to: "2"
        match: { field: "passed", value: true }
      - to: "1_5"
        match: { field: "passed", value: false }
        max_loop: 3

  - id: "2"
    type: "agent"
    agent_config: "architect"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "1_5", output: "step1_5_sota.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        design: "step2_design.md"
    checkpoint: true
    checkpoint_label: "Architecture Review"
    transitions:
      - to: "2_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "2_review"
    type: "agent"
    agent_config: "architect_reviewer"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "2", output: "step2_design.md" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    validation:
      - files: ["review_verdict.json"]
        tool: "json_schema"
        inline_schema:
          type: object
          required: [passed]
          properties:
            passed: { type: boolean }
            feedback: { type: string }
    transitions:
      - to: "3"
        match: { field: "passed", value: true }
      - to: "2"
        match: { field: "passed", value: false }
        max_loop: 3

  - id: "3"
    type: "agent"
    agent_config: "pm"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "1_5", output: "step1_5_sota.md" }
      - source: { step: "2", output: "step2_design.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        tasks_manifest: "tasks_manifest.json"
        task_card: "tasks/*.json"
    checkpoint: true
    checkpoint_label: "Review Task Breakdown"
    transitions:
      - to: "3_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "3_review"
    type: "agent"
    agent_config: "pm_reviewer"
    context:
      - source: { step: "3", output: "tasks_manifest.json" }
      - source: { step: "3", output: "tasks/*.json" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    validation:
      - files: ["review_verdict.json"]
        tool: "json_schema"
        inline_schema:
          type: object
          required: [passed]
          properties:
            passed: { type: boolean }
            feedback: { type: string }
    transitions:
      - to: "task_gate"
        match: { field: "passed", value: true }
      - to: "3"
        match: { field: "passed", value: false }
        max_loop: 3

  # ════════════════════════════════════════════════════════════
  # Task Dispatch Gate
  # ════════════════════════════════════════════════════════════

  - id: "task_gate"
    type: "gate"
    transitions:
      - to: "t_plan"
        match: { has_tasks: true }
      - to: "5"
        match: { has_tasks: false }

  # ════════════════════════════════════════════════════════════
  # Task Loop (per task)
  # ════════════════════════════════════════════════════════════

  - id: "t_plan"
    type: "agent"
    agent_config: "task_planner"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "1_5", output: "step1_5_sota.md", mode: "summary" }
      - source: { step: "2", output: "step2_design.md", mode: "interfaces" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        plan: "task_plan.md"
        subtask_manifest: "subtasks_manifest.json"
        subtask_card: "subtasks/*.json"
    transitions:
      - to: "t_plan_review"

  - id: "t_plan_review"
    type: "agent"
    agent_config: "task_planner_reviewer"
    context:
      - source: { step: "t_plan", output: "task_plan.md" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: "t_impl"
        match: { field: "passed", value: true }
      - to: "t_plan"
        match: { field: "passed", value: false }
        max_loop: 3

  # ── Task: Implement → Apply to Repo → Validate → Review ──

  - id: "t_impl"
    type: "agent"
    agent_config: "task_implementer"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "2", output: "step2_design.md", mode: "interfaces" }
      - source: { step: "t_plan", output: "task_plan.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "write"
      # 无 fixed → LLM 自由命名文件
      # 写进 t_impl.tmp/
    transitions:
      - to: "t_impl_apply"
        # 直接进入 apply，不用 match（无条件）

  - id: "t_impl_apply"
    type: "tool"
    tool: "repo_apply"
    # repo_apply: 将 t_impl.tmp/ 的文件
    #             1. 复制到 project/ repo
    #             2. git add + git commit
    #             返回 { applied: true/false, files: [...], error: "..." }
    params:
      source_dir: "t_impl.tmp"
    transitions:
      - to: "t_impl_validate"
        match: { field: "applied", value: true }
      - to: "t_impl"
        match: { field: "applied", value: false }
        max_loop: 3
        feedback: true         # ← 把 error message 传回 t_impl

  - id: "t_impl_validate"
    type: "tool"
    tool: "repo_validate"
    # repo_validate: 对 project/ repo 运行一系列验证：
    #   1. syntax lint (ruff/eslint/...)
    #   2. compile (python -m compileall / gcc / ...)
    #   3. run tests (pytest / npm test / ...)
    # 返回 { all_passed: true/false,
    #         results: [{tool, file, passed, error_message}] }
    params:
      validations:
        - tool: "syntax_lint"
          files: ["*.py", "*.js", "*.html"]
        - tool: "py_compile"
          files: ["*.py"]
        - tool: "pytest"
          files: ["*_test.py", "test_*.py"]
    transitions:
      - to: "t_impl_review"
        match: { field: "all_passed", value: true }
      - to: "t_impl"
        match: { field: "all_passed", value: false }
        max_loop: 5
        feedback: true         # ← 把错误信息作为 feedback 传回 t_impl
                               #   agent 第二次见到 step 时 context 里会有
                               #   [Previous Feedback] 包含 validate 失败的信息

  - id: "t_impl_review"
    type: "agent"
    agent_config: "task_implementer_reviewer"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "2", output: "step2_design.md", mode: "interfaces" }
      - source: { step: "t_plan", output: "task_plan.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: "t_verify"
        match: { field: "passed", value: true }
      - to: "t_impl"
        match: { field: "passed", value: false }
        max_loop: 3

  # ── Task: Verify ──

  - id: "t_verify"
    type: "agent"
    agent_config: "task_verifier"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "t_plan", output: "task_plan.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        report: "task_verify_report.json"
    transitions:
      - to: "t_verify_review"

  - id: "t_verify_review"
    type: "agent"
    agent_config: "task_verifier_reviewer"
    context:
      - source: { step: "t_verify", output: "task_verify_report.json" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: "task_loop"
        match: { field: "passed", value: true }
      - to: "t_verify"
        match: { field: "passed", value: false }
        max_loop: 3

  # ════════════════════════════════════════════════════════════
  # Task Loop Gate
  # ════════════════════════════════════════════════════════════

  - id: "task_loop"
    type: "gate"
    transitions:
      - to: "t_plan"
        match: { more_tasks: true }
        max_loop: 200
      - to: "5"
        match: { all_done: true }
      - to: "1_5"
        match: { refresh_needed: true }
        max_loop: 5

  # ════════════════════════════════════════════════════════════
  # Final Verification
  # ════════════════════════════════════════════════════════════

  - id: "5"
    type: "agent"
    agent_config: "final_verifier"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "1_5", output: "step1_5_sota.md" }
      - source: { step: "2", output: "step2_design.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        readme: "final/README.md"
        report: "final/verify_report.json"
    transitions:
      - to: "5_review"

  - id: "5_review"
    type: "agent"
    agent_config: "final_verifier_reviewer"
    context:
      - source: { config: "meta_conversation", output: "brief.md" }
      - source: { step: "5", output: "final/verify_report.json" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: null    # end
        match: { field: "passed", value: true }
      - to: "5"
        match: { field: "passed", value: false }
        max_loop: 3
```

---

## 3. Agent Config (separate file, indexed by step_id)

```yaml
# agent_configs/dpe_default.yaml

researcher:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.2
  template: "step1_5_researcher.md"
  tools: ["web_search", "web_fetch"]     # 这个 step 可用的 tool 列表
  thinking:
    enable: true
    effort: "max"

researcher_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "step1_5_researcher_red.md"
  tools: []
  thinking:
    enable: true

architect:
  model: "deepseek/deepseek-v4-pro"
  temperature: 0.2
  template: "step2_architect.md"
  tools: ["web_search", "web_fetch", "read_file", "list_tree"]
  thinking:
    enable: true
    effort: "max"

architect_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "step2_architect_red.md"
  tools: []
  thinking:
    enable: true

pm:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.2
  template: "step3_pm.md"
  tools: ["web_search", "web_fetch", "read_file", "list_tree"]

pm_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "step3_pm_red.md"
  tools: []

task_planner:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.2
  template: "task_plan.md"
  tools: ["web_search", "web_fetch", "read_file", "list_tree"]
  max_tool_turns: 10

task_planner_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "task_plan_red.md"
  tools: []

task_implementer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.2
  template: "task_implementer.md"
  tools: ["read_file", "list_tree", "write"]
  max_tool_turns: 15

task_implementer_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "task_implementer_red.md"
  tools: []

task_verifier:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.2
  template: "task_verify.md"
  tools: ["read_file", "list_tree"]
  max_tool_turns: 10

task_verifier_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "task_verify_red.md"
  tools: []

final_verifier:
  model: "deepseek/deepseek-v4-pro"
  temperature: 0.2
  template: "step5_verifier.md"
  tools: ["read_file", "list_tree"]
  max_tool_turns: 10

final_verifier_reviewer:
  model: "deepseek/deepseek-v4-flash"
  temperature: 0.1
  template: "step5_verifier_red.md"
  tools: []
```

Note: agent config 不再有 `context`、`output`、`validation` — 这些都在 graph config 的 step 定义里。

---

## 4. Meta Conversation Config

Meta conversation 就是普通的 skillflow graph。AItelier 启动时先跑这个，产出 `brief.md` + `step1_goals.json`。

```yaml
# configs/meta_conversation.yaml
name: "meta_conversation"
description: "Project requirements gathering via natural conversation"

begin: "intent_detect"

steps:
  - id: "intent_detect"
    type: "agent"
    agent_config: "intent_detector"
    context: []
    output:
      mode: "content"
      fixed:
        result: "intent_result.json"
    transitions:
      - to: "meta"
        match: { field: "intent", value: "new_project" }
      - to: "meta"
        match: { field: "intent", value: "existing_code" }
      - to: null
        match: { field: "status", value: "rejected" }

  - id: "meta"
    type: "agent"
    agent_config: "meta_conversation_agent"
    context: []
    output:
      mode: "write"
    checkpoint: true
    checkpoint_label: "Review Project Brief"
    transitions:
      - to: null
        match: { from: "checkpoint", value: "approved" }
        # brief.md + step1_goals.json 已由 agent 通过
        # save_draft_brief / suggest_submit_project tools 写入
```

---

## 5. Tool System

### 5.1 Directory Structure

```
skillflow/tools/
├── read_file/
│   ├── tool.yaml
│   └── impl.py
├── write/
│   ├── tool.yaml
│   └── impl.py
├── list_tree/
│   ├── tool.yaml
│   └── impl.py
├── dir_tree/                 # context tool
│   ├── tool.yaml
│   └── impl.py
├── web_search/
│   ├── tool.yaml
│   └── impl.py
├── web_fetch/
│   ├── tool.yaml
│   └── impl.py
├── repo_apply/               # 文件合入 repo
│   ├── tool.yaml
│   └── impl.py
├── repo_validate/            # repo 级验证
│   ├── tool.yaml
│   └── impl.py
├── syntax_lint/
│   ├── tool.yaml
│   └── impl.py
├── py_compile/
│   ├── tool.yaml
│   └── impl.py
├── pytest/
│   ├── tool.yaml
│   └── impl.py
├── json_schema/
│   ├── tool.yaml
│   └── impl.py
├── save_draft_brief/
│   ├── tool.yaml
│   └── impl.py
└── suggest_submit_project/
    ├── tool.yaml
    └── impl.py
```

### 5.2 Tool Definition (tool.yaml)

```yaml
# skillflow/tools/read_file/tool.yaml
name: read_file
description: >
  Read a file from the project workspace. Returns file content with line numbers.
parameters:
  path:
    type: string
    description: "Relative path from project root (e.g. 'src/main.py')"
    required: true
  start_line:
    type: integer
    description: "0-indexed start line (optional, default 0)"
    required: false
  end_line:
    type: integer
    description: "Exclusive end line (optional, default end of file)"
    required: false
limits:
  max_lines: 300
  max_size_kb: 50
```

```yaml
# skillflow/tools/repo_apply/tool.yaml
name: repo_apply
description: >
  Apply files from the step staging dir to the project repository.
  Copies files, performs git add + git commit.
  Returns { applied: true/false, files: [...], commit_hash: "...", error: "..." }
type: "system"    # "agent" = exposed to LLM, "system" = skillflow internal only
parameters:
  source_dir:
    type: string
    description: "Source directory (relative to step output dir)"
    required: true
```

```yaml
# skillflow/tools/repo_validate/tool.yaml
name: repo_validate
description: >
  Run a list of validations against the project repo.
  Each validation is a tool call with file globs.
  Returns { all_passed: true/false, results: [{tool, file, passed, error_message}] }
type: "system"
parameters:
  validations:
    type: array
    items:
      type: object
      properties:
        tool: { type: string }
        files: { type: array, items: { type: string } }
```

### 5.3 Tool Implementation (impl.py)

```python
# skillflow/tools/repo_apply/impl.py
# Export: repo_apply  (function name must == tool name)

import shutil
import subprocess
from pathlib import Path

def repo_apply(source_dir: str, *, workspace_root: str, project_root: str) -> dict:
    """Apply draft files to the project repo."""
    src = Path(workspace_root) / source_dir
    dst = Path(project_root)

    if not src.exists():
        return {"applied": False, "files": [], "error": f"Source dir not found: {source_dir}"}

    applied_files = []
    for f in src.rglob("*"):
        if f.is_file() and f.name != ".gitkeep":
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            applied_files.append(str(rel))

    # git add + commit
    subprocess.run(["git", "add", "-A"], cwd=dst, capture_output=True)
    r = subprocess.run(["git", "commit", "-m", f"step: apply {len(applied_files)} files"],
                       cwd=dst, capture_output=True, text=True)

    if r.returncode != 0:
        return {"applied": False, "files": applied_files, "error": r.stderr.strip()}

    return {"applied": True, "files": applied_files, "commit_hash": "..."}


# skillflow/tools/repo_validate/impl.py
# Export: repo_validate

def repo_validate(validations: list[dict], *, project_root: str,
                  tool_loader) -> dict:
    """Run validations against the project repo."""
    results = []
    all_passed = True

    for v in validations:
        tool_name = v["tool"]
        file_globs = v["files"]
        fn = tool_loader.load_tool_fn(tool_name)

        # Expand globs
        files = []
        for glob in file_globs:
            files.extend(Path(project_root).rglob(glob))

        for f in files:
            try:
                r = fn(file=str(f.relative_to(project_root)),
                       workspace_root=project_root)
                passed = r.get("verdict") == "passed" or r.get("passed", False)
                if not passed:
                    all_passed = False
                results.append({
                    "tool": tool_name,
                    "file": str(f.relative_to(project_root)),
                    "passed": passed,
                    "error_message": r.get("feedback", r.get("error", ""))
                })
            except Exception as e:
                all_passed = False
                results.append({
                    "tool": tool_name,
                    "file": str(f.relative_to(project_root)),
                    "passed": False,
                    "error_message": str(e)
                })

    return {"all_passed": all_passed, "results": results}
```

### 5.4 Dynamic Import

```python
# skillflow/tool_loader.py
import importlib.util
from pathlib import Path
from typing import Callable

class ToolLoader:
    def __init__(self, tools_dir: Path):
        self.tools_dir = tools_dir
        self._cache: dict[str, tuple[dict, Callable]] = {}

    def load_schema(self, name: str) -> dict:
        """Load tool.yaml for system prompt assembly."""
        import yaml
        path = self.tools_dir / name / "tool.yaml"
        return yaml.safe_load(path.read_text())

    def load_fn(self, name: str) -> Callable:
        """Dynamic import of tool implementation."""
        if name in self._cache:
            return self._cache[name][1]

        impl_path = self.tools_dir / name / "impl.py"
        spec = importlib.util.spec_from_file_location(name, impl_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fn = getattr(module, name, None)
        if fn is None:
            raise ImportError(
                f"Tool '{name}': impl.py must export function '{name}'"
            )
        self._cache[name] = ({}, fn)
        return fn
```

---

## 6. Prompt System

### 6.1 System Prompt = template.md + dynamic tool schemas

```
[Template Content]
{template.md}

[Available Tools — Multi-Turn Exploration]
按 graph config 的 agent_config → agent config 的 tools 列表，逐个加载
skillflow/tools/{name}/tool.yaml，动态拼接 tool schema + usage pattern。
```

### 6.2 User Prompt = context assembly (step context 列表驱动)

```
对于 step context 里每个 source:

  如果是 cross-config:
    source: { config: "meta_conversation", output: "brief.md" }
    → 读 workspace/{project_id}/meta_conversation/meta/brief.md

  如果是 previous step:
    source: { step: "2", output: "step2_design.md", mode: "interfaces" }
    → 读 workspace/{project_id}/dpe_default/2/step2_design.md
    → mode: "full" = 全量, "summary" = 前100行, "interfaces" = 提取 API/接口章节

  如果是 tool:
    source: { tool: "dir_tree" }
    → 调用 dir_tree tool，注入结果

[Previous Feedback] (如果有 retry → feedback loopback)
上一轮 reviewer 或 validation 的失败信息

[Step Task Card]
当前 step 的指令 (由 prompt assembler 注入；Inbox 目录已废弃)
```

---

## 7. Output → Write Tool 自动生成

```
output.mode: "content"
output.fixed:
  sota: "step1_5_sota.md"
  task_card: "tasks/*.json"

→ 自动生成 constrained write tools:
    write_sota(content) → 写入 step1_5_sota.md
    write_task_card(id, content) → 写入 tasks/{id}.json
  (id 替换 * wildcard)

output.mode: "write"
output.fixed: {}   (空或不存在)

→ 生成通用 write tool:
    write(file, content) → 写入 {file}
  LLM 自己决定文件名，无约束
```

---

## 8. Transition Feedback Loopback

```yaml
transitions:
  - to: "t_impl"
    match: { field: "all_passed", value: false }
    max_loop: 5
    feedback: true
```

`feedback: true` means: when this transition is taken, the error output of the current node (tool node's `error_message` / `results`) is injected into the target node's context as `[Previous Feedback — MUST FIX]`.

This means the agent at `t_impl` re-enters with exactly the validation failure messages, and its template can say: "If this is a retry, you will see [Previous Feedback — MUST FIX]. Fix all mentioned issues."

---

## 9. Checkpoint Display

Node has `checkpoint: true` → skillflow pauses before proceeding.
AItelier UI reads `the promoted step dir_{step_id}/` or `the step staging dir_{step_id}/` directory, displays all file contents.

No special logic needed — it's just showing the step's output files.

---

## 10. Workspace Layout Per Project

```
~/.AItelier/workspaces/{project_id}/
├── meta_conversation/          # meta conversation graph 的 workspace
│   ├── meta.tmp/
│   │   └── brief.md
│   ├── intent_detect/
│   │   └── intent_result.json
│   ├── meta/
│   │   └── brief.md            # ← 被 dpe_default graph 引用
│   │   └── step1_goals.json
│   └── Trace_meta/
├── dpe_default/                # dpe_default graph 的 workspace
│   ├── 1_5.tmp/
│   ├── 1_5/
│   │   └── step1_5_sota.md
│   ├── 2/
│   ├── Trace_1_5/
│   └── ...
├── project/                    # 共享的 project brief
│   └── project_brief.md
└── tasks/                      # task cards

~/.AItelier/projects/{project_id}/  ← 实际的代码 repo
└── src/
    └── ...
```

Cross-config context 查找路径:
```
source: { config: "meta_conversation", output: "brief.md" }
→ {workspace}/meta_conversation/meta/brief.md

source: { step: "2", output: "step2_design.md" }
→ {workspace}/{current_config}/2/step2_design.md
  (当前 config = dpe_default)
```

---

## 11. Skillflow New Capabilities Summary

| # | 能力 | 说明 |
|---|------|------|
| A | `match: {field, value}` | conditional transition on agent/tool output JSON field |
| B | Loop-back edge + `max_loop` | review fail → back to agent, with count limit |
| C | `type: "tool"` node | non-LLM node: dynamic import + execute tool function |
| D | `feedback: true` on transition | pass current node's error/result as context to target node |
| E | `type: "gate"` node (已有) | has_tasks / all_done / more_tasks |
| F | `checkpoint: true` + `match: {from: "checkpoint"}` | user review checkpoint with pass/reject routing |
| G | Cross-config context resolution | `source: {config: "X", output: "Y"}` |
| H | `recover_stale_claims` auto-invoke | built into poll loop |
| I | `validation` step config | list of {files, tool, params} → run before transition, inject feedback |
| J | `output.fixed` → constrained write tools | auto-generate write_* tools from output schema |

---

## 12. What This Enables: Novel Writing Config Example

```yaml
# configs/novel_writing.yaml
name: "novel_writing"

steps:
  - id: "outline"
    type: "agent"
    agent_config: "outline_writer"
    context:
      - source: { config: "meta_conversation", output: "novel_brief.md" }
      - source: { tool: "dir_tree" }
    output:
      mode: "content"
      fixed:
        outline: "chapter_outline.md"
    checkpoint: true
    checkpoint_label: "Review Chapter Outline"
    transitions:
      - to: "outline_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "outline_review"
    type: "agent"
    agent_config: "outline_reviewer"
    context:
      - source: { step: "outline", output: "chapter_outline.md" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: "write_chapter"
        match: { field: "passed", value: true }
      - to: "outline"
        match: { field: "passed", value: false }
        max_loop: 3

  - id: "write_chapter"
    type: "agent"
    agent_config: "chapter_writer"
    context:
      - source: { step: "outline", output: "chapter_outline.md" }
      - source: { tool: "character_ledger" }     # 自定义 context tool
    output:
      mode: "write"
    checkpoint: true
    checkpoint_label: "Review Chapter Draft"
    transitions:
      - to: "chapter_review"
        match: { from: "checkpoint", value: "approved" }

  - id: "chapter_review"
    type: "agent"
    agent_config: "chapter_reviewer"
    context:
      - source: { step: "write_chapter", output: "*" }
      - source: { step: "outline", output: "chapter_outline.md" }
    output:
      mode: "content"
      fixed:
        verdict: "review_verdict.json"
    transitions:
      - to: "chapter_summary"
        match: { field: "passed", value: true }
      - to: "write_chapter"
        match: { field: "passed", value: false }
        max_loop: 3

  - id: "chapter_summary"
    type: "agent"
    agent_config: "summarizer"
    context:
      - source: { step: "write_chapter", output: "*" }
    output:
      mode: "content"
      fixed:
        summary: "chapter_summary.md"
    transitions:
      - to: "update_ledger"

  - id: "update_ledger"
    type: "tool"
    tool: "update_ledger"      # 自定义 tool: 更新角色、情节、设定
    params:
      inputs: ["chapter_summary.md", "write_chapter.tmp/*"]
    transitions:
      - to: "submit_chapter"

  - id: "submit_chapter"
    type: "tool"
    tool: "submit_to_publisher"  # 自定义 tool
    params:
      chapter_dir: "write_chapter"
    transitions:
      - to: null

# Agent configs defined separately in agent_configs/novel_writing.yaml
```

AItelier 不需要改任何代码。这套图 + agent config + tools 定义了一个完整的小说写作 pipeline。

---

## 13. Implementation Plan

### Phase 1: Skillflow Framework

| # | Task | 说明 |
|---|------|------|
| 1 | `match: {field, value}` transition | read step output JSON field to route |
| 2 | `type: "tool"` node | dynamic import tool, execute, capture result |
| 3 | `feedback: true` on transition | inject previous node's error to target node context |
| 4 | Tool loader | `skillflow/tools/{name}/` → load yaml schema + dynamic import impl |
| 5 | `recover_stale_claims` in poll loop | auto-recover stuck claims |
| 6 | Cross-config context resolution | `source: {config, step?, output}` → read workspace files |
| 7 | `validation` list execution | run list of {files, tool, params} before transition |
| 8 | `output.fixed` → constrained write tools | auto-generate write_* from output schema |

### Phase 2: Config Migration

| # | Task | 说明 |
|---|------|------|
| 9 | Create `agent_configs/dpe_default.yaml` | extract from `dpe_roles_config.yaml` |
| 10 | Rewrite `configs/dpe_default.yaml` | new graph format, all agents + reviewers as explicit nodes |
| 11 | Create `configs/meta_conversation.yaml` + agent config | meta as skillflow graph |
| 12 | Migrate templates | review templates self-contained, no external criteria extraction |

### Phase 3: AItelier Adaptation

| # | Task | 说明 |
|---|------|------|
| 13 | Rewrite `prompt_assembler.py` | config-driven system prompt (template + tool schemas) + user prompt (context list) |
| 14 | Remove `dpe_roles_config.yaml` | fully superseded |
| 15 | Update `AItelierStepRunner` | handle tool nodes, feedback loopback |
| 16 | UI timeout cache fix | keep last successful data on timeout |
| 17 | Remove language detection from prompt | future: UI language setting |

### Phase 4: Tools Migration

| # | Task | 说明 |
|---|------|------|
| 18 | Move tool implementations to `skillflow/tools/` | from `core/web_tools.py`, `core/project_tools.py` |
| 19 | Create `tool.yaml` for each tool | schema + parameter definitions |
| 20 | Delete old hardcoded tool definitions | from `prompt_assembler.py` `_EXPLORATION_TOOLS` etc. |
