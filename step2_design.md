# 技术架构设计 — AItelier WebUI Chat History Persistence

## 概述

增强现有 AItelier WebUI 的聊天视图（`web/js/views/chat.js`），使其具备跨页面导航和跨浏览器会话的聊天历史持久化能力。在现有 FastAPI 后端 + SQLite `chat_history` / `sessions` 表、以及前端 session 机制的基础之上，**填补三个缺口**：

1. **历史恢复** — 用户返回聊天页面时，从后端加载并渲染持久化的消息
2. **会话选择器** — 聊天页面提供一个下拉列表，列出过去的会话，可选择并加载
3. **即时用户消息持久化** — 在用户消息发送后立即保存到后端（不等待流式响应完成）

不引入新框架、新依赖、新数据库表。所有修改均收敛在已有文件内部。

---

## 架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                     AItelier FastAPI Server                            │
│                                                                        │
│  NEW  GET /api/agent/chat/history?session_id=...                       │
│       → db.get_chat_history_by_session(session_id) → {messages: [...]} │
│                                                                        │
│  NEW  GET /api/agent/sessions?project_id=...&limit=20                  │
│       → db.list_chat_sessions(project_id, limit) → {sessions: [...]}   │
│                                                                        │
│  NEW  POST /api/agent/chat/message                                     │
│       → db.save_chat_message_with_session(session_id, project_id,      │
│                                           role, content)               │
│                                                                        │
│  EXISTING  POST /api/agent/chat          (SSE streaming + persist)     │
│  EXISTING  POST /api/agent/session/create                              │
└───────────────────────────┬──────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     Frontend — Chat View (chat.js)                     │
│                                                                        │
│  show()                                                                │
│    ├── _renderUI()            build DOM structure                      │
│    │     ├── .chat-header     ★ NEW: session selector bar              │
│    │     │     ├── <select id="session-selector">                      │
│    │     │     └── <button id="btn-new-session"> + New                 │
│    │     ├── .chat-messages   (unchanged)                              │
│    │     └── .chat-input-area (unchanged)                              │
│    ├── _initSession()         (unchanged)                              │
│    ├── _restoreHistory()      ★ NEW: load & render stored messages     │
│    └── _loadSessionList()     ★ NEW: populate session selector         │
│                                                                        │
│  _sendMessage(text)                                                   │
│    ├── _saveUserMessage(text) ★ NEW: POST /api/agent/chat/message     │
│    └── ... (existing SSE streaming flow unchanged)                     │
│                                                                        │
│  _loadSession(sessionId)      ★ NEW: switch to a different session    │
│    ├── Clear display & history                                         │
│    ├── _sessionId = sessionId                                          │
│    └── _restoreHistory()                                               │
└──────────────────────────────────────────────────────────────────────┘

Data Flow — History Restoration:
  Chat.show()
    │
    ├── _initSession() → _sessionId
    │
    ├── _restoreHistory()
    │     │
    │     └── GET /api/agent/chat/history?session_id=...
    │           │
    │           ▼
    │     messages: [{role, content, created_at}, ...]
    │           │
    │           ▼
    │     _addMessage(role, content) for each → rendered bubbles
    │     _history = messages (deduped, in-memory copy)
    │
    └── _loadSessionList()
          │
          └── GET /api/agent/sessions?project_id=...
                │
                ▼
          sessions: [{session_id, project_id, message_count, last_message, updated_at}, ...]
                │
                ▼
          <select id="session-selector"> populated, current _sessionId selected

Data Flow — Session Switch:
  User selects a different session in <select>
    │
    └── _loadSession(newSessionId)
          ├── Clear .chat-messages DOM
          ├── Clear _history array
          ├── _sessionId = newSessionId
          └── _restoreHistory() → GET history → render
```

---

## 组件列表

### 组件 1: `GET /api/agent/chat/history` — 会话历史端点（新增）

- **职责**: 返回指定会话的完整消息历史
- **HTTP**: `GET /api/agent/chat/history?session_id=<uuid>`
- **成功响应** (200):
  ```json
  {
    "session_id": "a1b2c3d4e5f6",
    "messages": [
      {"role": "user", "content": "Hello", "created_at": "2026-..."},
      {"role": "assistant", "content": "Hi there!", "created_at": "2026-..."}
    ]
  }
  ```
- **错误响应** (422): `session_id` 缺失或为空
- **实现**: 调用已有的 `db.get_chat_history_by_session(session_id, limit=100)`
- **位置**: `api/agent_routers.py` — 新增路由函数

### 组件 2: `GET /api/agent/sessions` — 会话列表端点（新增）

- **职责**: 返回会话列表（含元数据），支持按 project_id 过滤
- **HTTP**: `GET /api/agent/sessions?project_id=<optional>&limit=20`
- **成功响应** (200):
  ```json
  {
    "sessions": [
      {
        "session_id": "a1b2c3d4e5f6",
        "project_id": "my-project",
        "message_count": 12,
        "last_message": "Sure, I can help with that.",
        "updated_at": "2026-06-15T10:30:00Z"
      }
    ]
  }
  ```
- **参数**:
  - `project_id` (optional): 按项目过滤；不传则返回所有会话
  - `limit` (optional, default 20): 最大返回数
- **实现**: 调用新方法 `db.list_chat_sessions(project_id, limit)`；后端只返回有消息的会话（`message_count > 0`），过滤掉空会话
- **位置**: `api/agent_routers.py` — 新增路由函数

### 组件 3: `POST /api/agent/chat/message` — 单条消息保存端点（新增）

- **职责**: 在用户发送消息后立即保存该消息到数据库（不等流式响应完成）
- **HTTP**: `POST /api/agent/chat/message`
- **请求体**:
  ```json
  {
    "session_id": "a1b2c3d4e5f6",
    "project_id": "my-project",
    "role": "user",
    "content": "Hello, agent!"
  }
  ```
- **成功响应** (200): `{"status": "saved"}`
- **实现**: 调用已有的 `db.save_chat_message_with_session(session_id, project_id, role, content)`
- **位置**: `api/agent_routers.py` — 新增路由函数
- **注意**: 此端点仅用于保存用户消息。助手消息仍然在流式响应完成后由 `POST /api/agent/chat` 端点保存（与现有行为一致）。这是一个"安全带"措施，确保用户消息在导航离开前已落盘。

### 组件 4: `DBManager.list_chat_sessions()` — 数据库查询方法（新增）

- **职责**: 查询会话列表及元数据
- **SQL**:
  ```sql
  SELECT s.id AS session_id,
         ch.project_id,
         COUNT(ch.id) AS message_count,
         (SELECT content FROM chat_history
          WHERE session_id = s.id
          ORDER BY id DESC LIMIT 1) AS last_message,
         MAX(ch.created_at) AS updated_at
  FROM sessions s
  JOIN chat_history ch ON ch.session_id = s.id
  WHERE (? IS NULL OR ch.project_id = ?)
  GROUP BY s.id
  HAVING message_count > 0
  ORDER BY updated_at DESC
  LIMIT ?
  ```
- **位置**: `core/db_manager.py` — 新增方法
- **参数**: `project_id: str | None`, `limit: int = 20`
- **返回**: `list[dict]`

### 组件 5: `web/js/api.js` — API 客户端扩展（修改）

- **职责**: 新增两个 API 包装方法
- **新增方法**:
  - `getChatHistory(sessionId)` → `GET /api/agent/chat/history?session_id=...`
  - `listSessions(projectId)` → `GET /api/agent/sessions?project_id=...&limit=20`
  - `saveChatMessage(body)` → `POST /api/agent/chat/message`

### 组件 6: `web/js/views/chat.js` — 聊天视图增强（修改）

- **职责**: 历史恢复、会话选择器、即时用户消息保存
- **新增私有函数**:
  - `_restoreHistory()` — 从后端获取当前会话消息并渲染
  - `_loadSessionList()` — 获取会话列表并填充 `<select>`
  - `_loadSession(sessionId)` — 切换到指定会话
  - `_saveUserMessage(text)` — 立即保存用户消息到后端
  - `_buildSessionSelector()` — 构建会话选择器 DOM
- **修改的现有函数**:
  - `_renderUI()` — 新增会话选择器栏（`.chat-header`）
  - `show()` — 在 `_renderUI()` 后调用 `_restoreHistory()` + `_loadSessionList()`
  - `_sendMessage()` — 在 `_addMessage("user", text)` 后立即调用 `_saveUserMessage(text)`
  - `hide()` — 不清空 `_history`（保留以便下次 `show()` 恢复时做去重）

---

## 技术栈

| 层 | 选型 | 说明 |
|----|------|------|
| **后端 API** | FastAPI native | 在已有 `api/agent_routers.py` 中新增路由，使用已有 `DBManager` |
| **数据库** | SQLite (WAL) | 已有 `chat_history` + `sessions` 表，无需 schema 变更 |
| **会话列表查询** | Raw SQL via DBManager | 单条 JOIN + GROUP BY 聚合查询 |
| **前端** | Vanilla JS | 已有 `web/js/views/chat.js`、`web/js/api.js` |
| **会话选择器 UI** | `<select>` 原生元素 | Pico CSS 提供响应式样式 |

无新依赖。

---

## 接口规范

### 后端 → 前端 API 契约

#### `GET /api/agent/chat/history`

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| Query `session_id` | string | 是 | 会话 ID（UUID 前缀） |

**响应**:
| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 回显请求的 session_id |
| `messages` | array | 消息列表，按时间升序 |
| `messages[].role` | string | `"user"` / `"assistant"` / `"system"` |
| `messages[].content` | string | 消息文本 |
| `messages[].created_at` | string | ISO 8601 时间戳 |

#### `GET /api/agent/sessions`

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| Query `project_id` | string | 否 | 按项目过滤；不传返回所有项目 |
| Query `limit` | int | 否 | 默认 20 |

**响应**:
| 字段 | 类型 | 说明 |
|------|------|------|
| `sessions` | array | 会话列表，按 `updated_at` 降序 |
| `sessions[].session_id` | string | 会话 ID |
| `sessions[].project_id` | string | 关联项目 ID |
| `sessions[].message_count` | int | 消息总数 |
| `sessions[].last_message` | string | 最后一条消息内容（截断到 100 字符） |
| `sessions[].updated_at` | string | 最后活动时间 ISO 8601 |

#### `POST /api/agent/chat/message`

**请求体**:
| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 会话 ID |
| `project_id` | string | 是 | 项目 ID（可为空字符串 `""`） |
| `role` | string | 是 | `"user"` / `"assistant"` / `"system"` |
| `content` | string | 是 | 消息内容 |

**响应**: `{"status": "saved"}`

### 前端内部接口

#### `_restoreHistory()`
- **输入**: 无（读取 `_sessionId`）
- **行为**:
  1. 若 `_sessionId` 为 null，直接返回（无会话可恢复）
  2. 调用 `API.getChatHistory(_sessionId)`
  3. 对返回的每条消息调用 `_addMessage(role, content)` 渲染气泡
  4. 将消息追加到 `_history` 数组（去重：以 `(role, content[:100])` 为键）
  5. 滚动到底部

#### `_loadSession(sessionId)`
- **输入**: `sessionId: string` — 要切换到的会话 ID
- **行为**:
  1. 中止任何进行中的 SSE 流
  2. 清空 `.chat-messages` DOM 和 `_history` 数组
  3. 设置 `_sessionId = sessionId`
  4. 调用 `_restoreHistory()`
  5. 更新 `<select>` 的选中项

#### `_saveUserMessage(text)`
- **输入**: `text: string` — 用户消息文本
- **行为**:
  1. 若 `_sessionId` 为 null，跳过
  2. 调用 `API.saveChatMessage({session_id: _sessionId, project_id: ..., role: "user", content: text})`
  3. 异步执行，不阻塞 SSE 流；失败时静默忽略（best-effort）

---

## 关键设计决策

| 决策 | 理由 |
|------|------|
| **会话列表只返回有消息的会话** (`HAVING message_count > 0`) | 避免在 UI 中显示空会话（被创建但从未使用）。用户只关心有实际对话历史的会话 |
| **即时用户消息保存使用独立端点** | 避免修改现有 SSE 流式端点的复杂逻辑。`POST /api/agent/chat` 保持原有行为（流完成后保存），额外的 `POST /api/agent/chat/message` 提供"安全带"：在用户消息渲染后立即异步保存，确保即使页面在流完成前关闭，用户消息也已持久化 |
| **`_history` 在 `hide()` 时不清空** | 当用户返回聊天页面时，`_history` 仍保留上次会话的内存副本。`_restoreHistory()` 用它做去重 — 若 DB 中的某条消息已存在于内存 `_history` 中（基于 `role + content[:100]` 去重），则不重复渲染。这与后端 `POST /api/agent/chat` 中的去重逻辑一致 |
| **会话选择器用原生 `<select>`** | Pico CSS 原生支持 `<select>` 元素的响应式样式，无需自定义组件。满足 MVP 需求 |
| **`list_chat_sessions()` 新增为 DBManager 方法** | 遵循已有模式：所有 DB 查询集中在 `DBManager` 中，API 路由仅做薄封装 |
| **会话 ID 仅在内存中** (`_sessionId`) | 跨硬刷新（F5）时，用户获得新会话。可通过会话选择器切换到之前的会话。若后续需要跨刷新的会话持久化，可在 `sessionStorage` 中保存 `_sessionId`（本阶段不做） |

## 扩展性考虑

1. **会话删除**: `GET /api/agent/sessions` 返回的列表可按需扩展删除按钮。后端只需添加 `DELETE /api/agent/sessions/{session_id}` 端点
2. **跨硬刷新会话恢复**: 在 `sessionStorage` 中存储 `_sessionId`，`_initSession()` 优先读取 `sessionStorage`，若存在则直接使用而不是创建新会话
3. **会话重命名**: 在 `sessions` 表中增加 `name` 列，前端增加编辑功能
4. **分页**: 当前 `limit=20` 已足够。若会话数量增长，可增加 `offset` 参数实现分页

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `api/agent_routers.py` | 修改 | 新增 3 个端点 |
| `core/db_manager.py` | 修改 | 新增 `list_chat_sessions()` 方法 |
| `web/js/api.js` | 修改 | 新增 3 个 API 方法 |
| `web/js/views/chat.js` | 修改 | 新增 ~4 个函数，修改 ~3 个现有函数 |
