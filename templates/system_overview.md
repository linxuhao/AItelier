# AItelier DPE - 六步法整体说明

## 系统概述
你正在参与 **AItelier DPE (Deterministic Pipeline Engine)**，一个确定性流水线引擎驱动的 Multi-Agent 协作系统。所有工作遵循严格的六步开发流程，确保输出质量与可追溯性。

---

## 核心设计原则

### 1. 物理隔离原则 (Inbox/Outbox)
- 每一步只能读取自己 Inbox 中的静态文件
- 处理后写入 Outbox_Draft，审核通过后移至 Outbox_Final
- 大模型变为"无状态函数"，切断幻觉的向后传染

### 2. 原子操作原则
- 能用代码实现的原子操作就用代码实现
- 每个 Agent 只负责一个明确的原子操作

### 3. 最小 Prompt 原则
- 系统设计优先，不依赖 prompt 约束 Agent 行为
- Deterministic Gate 在 Red Agent 审查前先做语法拦截

### 4. 确定性验证原则
- Green Agent 生成草案 → Gate 物理拦截 → Red Agent 逻辑审查 → 全绿封卷
- 最大重试 3 次，超限熔断

---

## 核心运行机制
- **APScheduler 驱动**: 定时轮询 SQLite 任务队列
- **单线贪婪调度**: 每次只执行一个任务，独占算力
- **断点续传**: 所有状态持久化到 SQLite (WAL 模式)
- **Git 事件溯源**: 每步 Final 封卷触发 git commit，支持时光机回滚
- **上下文流转**: 上一步的 Outbox_Final 物理拷贝至下一步的 Inbox

---

## 数据存储架构

### 工作区目录结构
```
workspaces/{project_id}/
├── Global_Mount/              # 只读全局上下文 (DAG, Goals)
├── Inbox_{step}/              # 当前步骤的输入
├── Outbox_Draft_{step}/       # Green Agent 草案
├── Outbox_Final_{step}/       # 审核通过的最终产出
└── .git/                      # Git 事件溯源
```

### 步骤 ID 映射
| Step ID | 目录名示例 |
|---------|-----------|
| "1"     | Inbox_1, Outbox_Draft_1, Outbox_Final_1 |
| "2"     | Inbox_2, Outbox_Draft_2, Outbox_Final_2 |
| "3"     | Inbox_3, Outbox_Draft_3, Outbox_Final_3 |
| "t_plan" | Inbox_t_plan, Outbox_Draft_t_plan, Outbox_Final_t_plan |
| "t_impl" | Inbox_t_impl, Outbox_Draft_t_impl, Outbox_Final_t_impl |
| "5"     | Inbox_5, Outbox_Draft_5, Outbox_Final_5 |

### 数据库存储 (SQLite WAL)
- **tasks 表**: `id, project_id, prompt, status, created_at` — 任务级状态
- **subtasks 表**: `id, task_id, description, dependencies, status, retry_count` — 子任务级状态
- **io_logs 表**: 文件流转日志，关联 git commit hash

---

## 六步法完整流程

| Step | Step ID | Agent | 职责 |
|------|---------|-------|------|
| Step 1 | "1" | Researcher | 技术调研 - 搜索现有工具，避免重复造车轮 |
| Step 2 | "2" | Architect | 架构设计 - 设计技术方案 |
| Step 3 | "3" | PM | 任务分解 - 拆分为子任务 (DAG) |
| t_plan | "t_plan" | Task Planner | 任务规划 - 为单个任务制定实现计划 |
| t_impl | "t_impl" | Implementer | 代码实施 - 实现任务 |
| Step 5 | "5" | Final Verifier | 最终验证 - 集成交付 |

### Task 循环
Step 3 产出的 `tasks_manifest.json` 定义了任务列表和执行顺序。引擎按执行顺序逐一运行：
1. task_loop → t_plan → t_plan_review → t_impl → t_impl_review → 循环
2. Green Agent 生成计划/代码 → Red Agent 审查
3. Red Agent 拒绝时，反馈自动注入下一轮重试
4. 最多重试 3 次，超限熔断
5. 所有任务通过后，进入 Step 5

---

## Agent 输出规范

### Green Agent (产出方)
输出 JSON 格式：
```json
{
    "thoughts": "推理过程与分析",
    "actions": [
        {
            "tool": "write",
            "params": {
                "file": "output_file.ext",
                "content": "文件完整内容"
            }
        }
    ]
}
```

### Red Agent (审查方)
输出 JSON 格式：
```json
{
    "passed": true,
    "feedback": ""
}
```
或：
```json
{
    "passed": false,
    "feedback": "具体修改指令"
}
```

### 关键约束
- Green Agent 通过 `actions` 数组产出文件，引擎自动写入工作区
- 支持多个 actions 产出多个文件
- 状态流转由引擎自动管理，Agent 不需要自行更新数据库
- Deterministic Gate (AST/Linter) 在 Red 审查前拦截语法错误
