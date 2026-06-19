# 技术架构设计 — AItelier Web Frontend

## 概述

AItelier Web Frontend 是一个**纯静态单页应用（SPA）**，使用原生 HTML/CSS/JS（零框架依赖）构建，由现有 FastAPI 后端以静态文件形式托管。它提供项目管理仪表盘、实时流水线状态更新（SSE）、检查点审查模态框，以及用于需求澄清的 Meta Agent 聊天界面。

SOTA 调研推荐的技术栈全部采用：**Pico CSS**（无类 CSS）、**marked.js + DOMPurify**（Markdown 安全渲染）、**原生 `<dialog>`**（模态框）、**原生 `EventSource`**（SSE 流）、**原生 `fetch()`**（API 通信）、**hash 路由**。

## 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                      AItelier FastAPI Server                      │
│                                                                   │
│  GET /                  → serve web/index.html (SPA entry)       │
│  GET /web/**            → serve static files (js/, css/)         │
│  GET /api/projects      → project CRUD                           │
│  POST /api/agent/chat   → meta agent SSE stream                  │
│  GET /api/events/stream → global pipeline SSE stream             │
│  GET /api/meta/{pid}/checkpoint → checkpoint state               │
│  ...                                                              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    web/index.html (SPA Shell)                      │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ <header>  AppBar — logo, nav links, connection status       ││
│  ├──────────────────────────────────────────────────────────────┤│
│  │ <main>                                                        ││
│  │  ┌─────────────────────────────────────────────────────────┐ ││
│  │  │  View: Dashboard       (hash: #/ or #/projects)         │ ││
│  │  │  ┌─────────────────────────────────────────────────┐    │ ││
│  │  │  │ Project table + create/delete actions            │    │ ││
│  │  │  └─────────────────────────────────────────────────┘    │ ││
│  │  └─────────────────────────────────────────────────────────┘ ││
│  │  ┌─────────────────────────────────────────────────────────┐ ││
│  │  │  View: Project Detail  (hash: #/projects/{id})         │ ││
│  │  │  ┌───────────────────────┬───────────────────────────┐  │ ││
│  │  │  │ Project info + tasks │ Workspace tree browser     │  │ ││
│  │  │  └───────────────────────┴───────────────────────────┘  │ ││
│  │  └─────────────────────────────────────────────────────────┘ ││
│  │  ┌─────────────────────────────────────────────────────────┐ ││
│  │  │  View: Chat            (hash: #/chat)                   │ ││
│  │  │  ┌─────────────────────────────────────────────────┐    │ ││
│  │  │  │ Chat messages (SSE streaming) + input            │    │ ││
│  │  │  └─────────────────────────────────────────────────┘    │ ││
│  │  └─────────────────────────────────────────────────────────┘ ││
│  └──────────────────────────────────────────────────────────────┘│
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ <dialog> CheckpointModal  — approve/reject with feedback    ││
│  └──────────────────────────────────────────────────────────────┘│
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ <aside> NotificationPanel  — real-time pipeline events (SSE) ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘

Data Flow:
  ┌──────────┐  fetch()   ┌──────────────┐  SSE   ┌───────────────┐
  │ API.js   │◄──────────►│ FastAPI       │◄──────►│ sse.js        │
  │ (fetch   │            │ /api/*        │        │ (EventSource) │
  │ wrapper) │            └──────────────┘        └───────┬───────┘
  └────┬─────┘                                            │
       │                                                  ▼
       ▼                                          ┌───────────────┐
  ┌──────────┐    ┌──────────┐    ┌──────────┐    │ Notification  │
  │Dashboard │    │ Project  │    │  Chat    │    │   Panel       │
  │  View    │    │  View    │    │  View    │    │ (sidebar)     │
  └──────────┘    └──────────┘    └──────────┘    └───────────────┘
       │               │               │
       └───────────────┴───────────────┘
                       │
                       ▼
               ┌──────────────┐
               │   Router     │
               │ (hashchange) │
               └──────────────┘
```

## 文件结构

所有前端文件位于仓库根目录的 `web/` 目录下，由 FastAPI 以静态文件形式托管：

```
./                          (仓库根目录)
├── web/
│   ├── index.html          SPA 入口 — HTML shell, 所有 view 的 DOM 容器
│   ├── css/
│   │   └── app.css         自定义样式（布局、聊天气泡、通知面板、响应式）
│   └── js/
│       ├── app.js          应用入口 — 初始化、路由、全局错误处理
│       ├── router.js       Hash 路由 — 基于 #/path 的视图切换
│       ├── api.js          API 客户端 — fetch 封装与错误处理
│       ├── sse.js          SSE 管理器 — EventSource 生命周期与事件分发
│       ├── utils.js        工具函数 — Markdown 渲染、时间格式化、状态样式
│       └── views/
│           ├── dashboard.js   仪表盘视图 — 项目列表、创建/删除
│           ├── project.js     项目视图 — 项目详情、任务列表、工作区浏览
│           ├── chat.js        聊天视图 — Meta Agent 对话（SSE 流）
│           └── checkpoint.js  检查点模态框 — 审批/拒绝 + 反馈
```

<span style="color:red">**关键规则**：所有写入路径均以仓库根目录 `./` 为基准。JS 模块间通过命名空间全局对象 `AItelier.*` 暴露接口（如 `AItelier.API`、`AItelier.Router`），不使用 ES6 `import/export`，避免对打包工具（webpack/vite）的依赖，保持原生可用。</span>

## 组件列表

### 1. index.html — SPA Shell

- **职责**: 单页入口文件，包含完整 DOM 结构、CDN 依赖、`<template>` 元素
- **内容**:
  - `<head>`: Pico CSS CDN、highlight.js CDN（可选）、自定义 `app.css`
  - `<body>` 顶层容器:
    - `<header id="app-bar">`: 导航栏 — logo、仪表盘/聊天链接、连接状态指示灯
    - `<main id="view-container">`: 视图容器，包含三个可切换面板:
      - `<section id="view-dashboard">`: 仪表盘视图
      - `<section id="view-project">`: 项目详情视图
      - `<section id="view-chat">`: 聊天视图
    - `<aside id="notification-panel">`: 通知侧边栏（SSE 事件流）
    - `<dialog id="checkpoint-modal">`: 检查点审批模态框
    - `<dialog id="confirm-dialog">`: 通用确认对话框
    - `<template id="tpl-project-row">`: 项目表格行模板
    - `<template id="tpl-task-row">`: 任务表格行模板
    - `<template id="tpl-chat-msg">`: 聊天消息气泡模板
    - `<template id="tpl-notification">`: 通知条目模板
  - `<script>` 标签加载所有 JS 模块（顺序加载）
- **无需认证**（API 已是 localhost-only）

### 2. app.js — 应用入口

- **职责**: 应用初始化、模块引导、全局状态、错误边界
- **接口**:
  - `AItelier.App.init()` — 应用启动入口，注册路由、启动 SSE、初始化视图
  - `AItelier.App.state` — 全局状态对象:
    - `currentView: string` — 当前激活的视图名称
    - `currentProjectId: string|null` — 当前选中的项目 ID
    - `connectionOk: boolean` — 后端连接状态
    - `reconnectAttempt: number` — 重连计数（指数退避）
  - `AItelier.App.showError(message)` — 全局错误提示（顶部 toast）
  - `AItelier.App.showReconnectBanner()` — 显示重连横幅
  - `AItelier.App.hideReconnectBanner()` — 隐藏重连横幅
- **初始加载**:
  1. 初始化 `Router`
  2. 初始化 `API` 客户端
  3. 初始化 `SSE` 管理器并连接 `/api/events/stream`
  4. 根据当前 hash 渲染对应视图
  5. 绑定全局 `unhandledrejection` / `error` 处理器

### 3. router.js — Hash 路由器

- **职责**: 基于 `window.onhashchange` 的视图切换
- **路由表**:

  | Hash 模式 | 视图 | 说明 |
  |-----------|------|------|
  | `#/` `#/projects` | Dashboard | 项目列表仪表盘 |
  | `#/projects/{id}` | ProjectDetail | 单个项目详情 + 任务 |
  | `#/chat` | Chat | Meta Agent 聊天 |

- **接口**:
  - `AItelier.Router.init(routes)` — 注册路由表，启动 hashchange 监听
  - `AItelier.Router.navigate(hash)` — 编程式导航
  - `AItelier.Router.currentRoute` — 当前匹配的路由对象 `{view, params}`
- **行为**:
  - `onhashchange` 触发时解析 hash → 匹配路由 → 调用对应视图的 `show()`/`hide()`
  - 未匹配路由 → 重定向到 `#/`
  - 视图切换时清理上一视图的资源（如聊天 SSE 连接的 abort）

### 4. api.js — API 客户端

- **职责**: 封装所有后端 REST API 调用，返回 Promise
- **接口**: 挂载于 `AItelier.API` 命名空间，每个 endpoint 一个方法:

  | 方法 | HTTP | 路径 | 说明 |
  |------|------|------|------|
  | `listProjects()` | GET | `/api/projects` | 获取项目列表 |
  | `createProject(body)` | POST | `/api/projects` | 创建项目 |
  | `getProject(id)` | GET | `/api/projects/{id}` | 获取单个项目 |
  | `patchProject(id, body)` | PATCH | `/api/projects/{id}` | 更新项目 |
  | `deleteProject(id)` | DELETE | `/api/projects/{id}` | 删除项目 |
  | `listTasks(projectId)` | GET | `/api/projects/{id}/tasks` | 列出项目任务 |
  | `submitProject(body)` | POST | `/api/projects/submit` | 提交项目简报 |
  | `retryProject(id)` | POST | `/api/projects/{id}/retry` | 重试失败项目 |
  | `getCheckpoint(pid)` | GET | `/api/meta/{pid}/checkpoint` | 获取待审批检查点 |
  | `approveCheckpoint(pid, cp)` | POST | `/api/meta/{pid}/checkpoint/approve` | 审批检查点 |
  | `rejectCheckpoint(pid, cp, fb)` | POST | `/api/meta/{pid}/checkpoint/reject` | 拒绝检查点 |
  | `detectIntent(prompt)` | POST | `/api/meta/detect-intent` | 意图检测 |
  | `assessPrompt(prompt, h)` | POST | `/api/meta/assess` | 需求评估 |
  | `listRuns(pid)` | GET | `/api/projects/{pid}/runs` | 列出运行记录 |
  | `getRunTrace(runId)` | GET | `/api/runs/{run_id}/trace` | 执行追踪 |
  | `workspaceTree(pid, sub)` | GET | `/api/projects/{pid}/workspace/tree` | 工作区目录 |
  | `workspaceFile(pid, path)` | GET | `/api/projects/{pid}/workspace/file` | 读取工作区文件 |
  | `getSchedulerSettings()` | GET | `/api/settings/scheduler` | 调度器设置 |
  | `updateSchedulerSettings(b)` | POST | `/api/settings/scheduler` | 更新调度器设置 |

- **内部行为**:
  - 所有请求默认 `timeout: 10000ms`（chat SSE 请求除外）
  - 40x/50x 响应 → 抛出 `ApiError {status, message}`
  - 网络错误 → 设置 `App.state.connectionOk = false` → 触发重连横幅
  - 支持自动重试（幂等 GET 请求在网络错误时重试 1 次）
  - 请求前检查 `App.state.connectionOk`，断开时排队请求待恢复后重放

### 5. sse.js — SSE 管理器

- **职责**: 管理与 `/api/events/stream` 的 `EventSource` 连接，分发事件到视图
- **接口**:
  - `AItelier.SSE.connect()` — 建立 EventSource 连接
  - `AItelier.SSE.disconnect()` — 断开连接
  - `AItelier.SSE.on(eventType, handler)` — 订阅事件
  - `AItelier.SSE.off(eventType, handler)` — 取消订阅
- **内部行为**:
  - 自动重连（`EventSource` 内置）
  - 维护 `_lastSeq` 单调序列号，丢弃乱序事件
  - `onerror` 触发时 → 设置 `App.state.connectionOk = false` → 显示重连横幅
  - `onopen` 时 → 恢复连接状态，触发全量状态刷新
  - 支持与 `/api/agent/chat` 的 SSE 流（聊天专用）并存（独立的 `fetch` + `ReadableStream` 解析，或直接使用 EventSource 模式）
- **事件分发**:
  ```
  SSE events → sse.js
    ├── checkpoint_reached  → notification panel + checkpoint modal
    ├── checkpoint_resolved → close modal + refresh dashboard
    ├── step_start          → notification panel + dashboard status
    ├── step_completed      → notification panel
    ├── project_completed   → notification panel + dashboard refresh
    ├── project_failed      → notification panel + dashboard refresh
    ├── run_started         → notification panel
    ├── agent_message       → notification panel
    └── files_written       → notification panel
  ```

### 6. utils.js — 工具函数

- **职责**: 共享纯函数，无状态
- **接口**:
  - `AItelier.Utils.renderMarkdown(text)` → 安全 HTML 字符串（`marked.parse()` + `DOMPurify.sanitize()`）
  - `AItelier.Utils.formatTime(isoString)` → 相对时间字符串（"2m ago"）
  - `AItelier.Utils.statusClass(status)` → CSS 类名（"status-ok" / "status-warn" / "status-err"）
  - `AItelier.Utils.statusIcon(status)` → Unicode 状态图标（✓ / ▶ / ✗ / ○）
  - `AItelier.Utils.truncate(text, maxLen)` → 截断 + 省略号
  - `AItelier.Utils.debounce(fn, ms)` → 去抖函数
  - `AItelier.Utils.escapeHtml(text)` → HTML 转义（防 XSS 的额外防线）
  - `AItelier.Utils.slugify(text)` → 生成 URL 友好的 slug

### 7. dashboard.js — 仪表盘视图

- **职责**: 项目列表展示、新建项目表单、删除确认
- **DOM**: `#view-dashboard`
- **接口**:
  - `AItelier.Dashboard.show()` — 显示视图，启动 3 秒轮询
  - `AItelier.Dashboard.hide()` — 隐藏视图，停止轮询
  - `AItelier.Dashboard.refresh()` — 立即刷新项目列表
- **行为**:
  - 每 3 秒调用 `API.listProjects()` 刷新表格
  - 表格列: 项目名称、状态（带图标）、任务进度（done/total）、最后更新
  - 空状态: "No projects yet — create your first project"
  - "New Project" 按钮 → 展开内联表单（project_id, name, repo_type, repo_path/repo_url）
  - 表单验证: 非空 slug、duplicate 409 处理
  - 删除按钮 → 弹出 `#confirm-dialog` → 确认后调用 `API.deleteProject(id)`
  - 点击项目行 → `Router.navigate('#/projects/{id}')`
  - 连接断开时: 表格保留上次数据且显示 "Reconnecting…" 覆盖层

### 8. project.js — 项目详情视图

- **职责**: 单个项目详情、关联任务列表、工作区文件浏览
- **DOM**: `#view-project`
- **接口**:
  - `AItelier.ProjectDetail.show(projectId)` — 显示指定项目的详情
  - `AItelier.ProjectDetail.hide()` — 隐藏视图
  - `AItelier.ProjectDetail.refresh()` — 刷新数据和渲染
- **子区域**:
  - **项目信息卡片**: 项目名称、状态、当前步骤、完成进度条
  - **操作按钮**: Retry（失败时）、Refresh Planning、Pause/Resume
  - **任务列表表格**: 任务 ID、提示文本、状态、当前步骤
  - **工作区文件树**: 可展开的目录树（调用 `API.workspaceTree()` / `API.workspaceFile()`）
- **行为**:
  - 进入视图时调用 `API.getProject(id)` + `API.listTasks(id)`
  - 工作区文件树: 首次点击展开时懒加载子目录
  - 点击文件 → 模态框展示内容（带语法高亮，若引入 highlight.js）
  - 3 秒轮询刷新（与 dashboard 相同策略）
  - 空任务状态: "No tasks yet — type in chat to add tasks"

### 9. chat.js — 聊天视图

- **职责**: Meta Agent 对话界面，支持 SSE 流式消息、Markdown 渲染、工具调用展示
- **DOM**: `#view-chat`
- **接口**:
  - `AItelier.Chat.show()` — 显示聊天视图
  - `AItelier.Chat.hide()` — 隐藏视图，abort 进行中的 SSE 流
  - `AItelier.Chat.sendMessage(text)` — 发送用户消息并流式接收回复
- **消息类型**（SSE 事件 → 渲染）:
  | SSE event type | 渲染方式 |
  |---|---|
  | `text_delta` | 追加到当前助手消息气泡（增量渲染） |
  | `tool_call` | 插入工具调用指示器 `🔧 Calling {name}...` |
  | `tool_result` | 更新工具调用指示器为结果摘要 |
  | `done` | 最终化助手消息，追加到历史 |
  | `error` | 显示红色错误气泡 |
- **行为**:
  - 消息气泡使用 `<template id="tpl-chat-msg">` 克隆
  - 助手消息通过 `Utils.renderMarkdown()` 渲染（支持代码块、列表等）
  - SSE 流使用 `fetch()` + `ReadableStream` 手动解析（需对流有更多控制，如 abort）
  - 发送按钮 / Enter 键提交消息
  - 连接断开时输入框禁用并显示 "Chat unavailable — reconnecting…"
  - 支持 `/help`、`/clear`、`/projects` 等斜杠命令
  - 从当前 project 上下文自动传递 `current_project` 参数

### 10. checkpoint.js — 检查点模态框

- **职责**: 展示步骤输出、审批/拒绝（含反馈）、处理竞态条件
- **DOM**: `<dialog id="checkpoint-modal">`
- **接口**:
  - `AItelier.CheckpointModal.show(projectId, checkpointData)` — 显示模态框
  - `AItelier.CheckpointModal.close()` — 关闭模态框
  - `AItelier.CheckpointModal.isOpen()` → boolean
- **行为**:
  - SSE 事件 `checkpoint_reached` 或 `checkpoint_paused` 触发自动弹出
  - 显示: 步骤标签（如 "Architecture Review"）、步骤输出文件列表、文件内容（Markdown 渲染）
  - 大文件内容放入可滚动容器（`max-height` + `overflow-y: auto`）
  - "Approve" 按钮 → `API.approveCheckpoint()` → 成功后关闭模态框
  - "Reject" 按钮 → 展开反馈输入框 → 提交时调用 `API.rejectCheckpoint()` → 关闭
  - **竞态保护**:
    - 审批/拒绝按钮点击后立即禁用（防双击）
    - 后端返回 `"already_advanced"` → 关闭模态框并静默处理（不显示错误）
    - 每 5 秒轮询 `GET /api/meta/{pid}/checkpoint` — 若返回空（404/无 checkpoint）→ 自动关闭模态框（stale detection）
  - **筛选**: 仅对 DPE pipeline 步骤（1, 2, 3）触发，Meta conversation 的 gather 检查点**不弹出**此模态框（由聊天界面处理）
  - Escape 键 → 关闭但不做操作（用户稍后可重新打开）

## 技术栈

| 层 | 选型 | 方式 | 理由 |
|----|------|------|------|
| **样式** | Pico CSS v2 (classless fluid) | CDN | 语义 HTML 自动获得响应式、暗色主题、表单、按钮、表格样式。零 CSS 类名 |
| **Markdown 渲染** | marked.js | CDN | 快速、成熟，将 Agent 回复中的 Markdown 转为 HTML |
| **XSS 防护** | DOMPurify | CDN | 对所有 `marked.parse()` 输出进行消毒 |
| **语法高亮**（可选） | highlight.js | CDN | 代码块着色（检查点文件查看、聊天代码块） |
| **模态框** | `<dialog>` 元素 | 原生 | `showModal()` / `close()`，所有现代浏览器支持，零依赖 |
| **SSE** | `EventSource` | 原生 | 自动重连、`data:` 行解析、零依赖 |
| **HTTP** | `fetch()` | 原生 | Promise-based，支持 `ReadableStream`（聊天 SSE）和 `AbortController` |
| **路由** | `window.onhashchange` | 原生 | 无服务端配置需求，FastAPI 静态文件直接托管 |
| **模板** | `<template>` 元素 | 原生 | 高效的 DOM 克隆，避免 innerHTML 拼接 |
| **模块化** | 全局命名空间 `AItelier.*` | 原生 | 避免 ES6 模块的打包依赖，各 JS 文件通过命名空间暴露接口 |

**总外部体积**: ~23KB（Pico CSS 10KB + marked.js 12KB + DOMPurify 11KB 压缩后的 ~23KB gzip）
加上 highlight.js（可选）：+~25KB

## 接口规范

### API 交互模式

```
View ──► API.js ──► fetch() ──► FastAPI /api/*
  │                              │
  │         Promise<data>        │
  ◄──────────────────────────────┘
  │
  │ 失败时:
  │ 网络错误 → API.js 设置 connectionOk=false → App 显示重连横幅
  │ HTTP 4xx  → 抛出 ApiError → View 显示内联错误
  │ HTTP 5xx  → 同上 + 自动重试 1 次（仅 GET）
```

### SSE 事件流模式

```
FastAPI ──► EventSource ──► sse.js ──► 事件分发
  /api/events/stream        │
                            ├──► NotificationPanel (所有事件)
                            ├──► Dashboard.refresh() (project_*)
                            ├──► CheckpointModal (checkpoint_*)
                            └──► App.state (connection status)
```

### 聊天 SSE 流模式

```
Chat View ──► fetch(POST /api/agent/chat) ──► ReadableStream
  │                                               │
  │  text_delta → 增量追加气泡                     │
  │  tool_call  → 插入工具调用指示器                │
  │  tool_result→ 更新指示器为结果摘要              │
  │  done       → 最终化消息                       │
  │  error      → 错误气泡                         │
  ◄───────────────────────────────────────────────┘
```

### 视图生命周期

```
Router.navigate(hash)
  │
  ├── 当前视图.hide()  — 清理定时器、abort 请求
  │
  ├── 解析路由参数
  │
  └── 目标视图.show(params)
       │
       ├── 首次: 创建 DOM（从 <template> 克隆）
       ├── 每次: 获取数据、渲染、启动轮询/SSE
       └── 后续: hide() → 清理
```

## 响应式设计

Pico CSS 提供响应式容器（`fluid` 变体）。自定义布局通过 CSS 媒体查询实现：

- **宽屏 (>= 1024px)**: 仪表盘表格 + 通知侧边栏并排
- **中屏 (768-1023px)**: 通知侧边栏折叠为底部条
- **窄屏 (< 768px)**: 单列布局，表格转卡片，导航栏垂直堆叠
- 长项目名/任务描述 → `text-overflow: ellipsis` + `max-width`

## 扩展性考虑

1. **认证**: 当 `web_api/` 启用 Cloudflare Access 认证时，前端仅需在请求中携带 `Cf-Access-User-Email` 头（Cloudflare 自动注入）。`api.js` 无需修改
2. **更多视图**: 路由表在 `router.js` 中集中定义，新增视图只需添加路由条目 + 实现 `show()/hide()` 接口
3. **暗色模式**: Pico CSS 原生支持 `data-theme="dark"` 属性，切换仅需一行 JS: `document.documentElement.dataset.theme = 'dark'`
4. **国际化**: 所有 UI 文本集中在视图模块的常量对象中，后续可提取为 i18n 资源文件
5. **WebSocket 升级**: 若 SSE 不够用，`sse.js` 的接口设计（`on/off` 事件订阅）可直接适配 WebSocket
6. **离线缓存**: 可添加 Service Worker 实现离线仪表盘缓存（PWA 渐进增强）

## 设计决策记录

| 决策 | 理由 |
|------|------|
| **多文件 JS 而非单文件** | 单个 app.js 文件会导致 2000+ 行不可维护的代码。多个文件按职责分隔，通过 `<script>` 标签顺序加载，不引入打包工具 |
| **全局命名空间而非 ES6 模块** | 原生浏览器环境中 `<script>` 标签加载的 JS 不天然支持 `import/export`（需要 `type="module"` 且路径解析复杂）。命名空间模式简单、可靠、无需构建步骤 |
| **`fetch() + ReadableStream` 用于聊天 SSE** | 聊天需要 abort 能力（用户离开视图时应终止流），而 `EventSource` 不支持手动 abort 控制。全局事件流用 `EventSource`（需要自动重连），聊天流用 `fetch()`（需要 abort 控制） |
| **不引入 highlight.js 为必需依赖** | 代码高亮是锦上添花。默认使用 `<pre><code>` 标签（Pico CSS 已提供基础样式），highlight.js 作为可选增强 |
| **template 元素而非 innerHTML 拼接** | `innerHTML` 有 XSS 风险和性能损失（每次重新解析 HTML）。`<template>` 克隆是浏览器原生优化路径 |
