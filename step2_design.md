# 技术架构设计 — Logged User Tracking

## 概述

为 AItelier 添加一个"已登录用户跟踪"页面和 API，仅 writer 角色可访问。数据来源为现有的 `users` 表（SQLite），`access_rights` 在查询时从 `AITELIER_WRITERS` 环境变量动态计算。`/api/me` 端点每次调用时 upsert 当前用户的 `last_seen_at`，保证跟踪数据实时性。前端使用动态导航栏，reader 角色的 DOM 中完全不存在 tracking 链接。

---

## 架构图

```
┌────────────────────────────────────────────────────────────────┐
│                        Browser (SPA)                           │
│                                                                │
│  index.html                                                    │
│  ├─ <nav id="app-bar">                                         │
│  │   └─ <ul id="nav-links">  ← JS 动态渲染                      │
│  │       ├─ <a href="#/">Dashboard</a>                          │
│  │       ├─ <a href="#/chat">Chat</a>                           │
│  │       └─ <a href="#/tracking">Tracking</a>  ← canWrite 时可见 │
│  ├─ <section id="view-tracking">  ← UserTracking 渲染目标        │
│  └─ <script src="/web/js/views/tracking.js">                   │
│                                                                │
│  app.js  ──►  state.canWrite  ──►  决定 nav 中是否渲染 Tracking  │
│  router.js ──► #/tracking → AItelier.UserTracking              │
│  api.js   ──► getLoggedUsers() → GET /api/admin/logged-users   │
│  views/tracking.js ──► 渲染 <table> Email / Latest Access /     │
│                          Access Rights                          │
└────────────────────────────────────────────────────────────────┘
        │                              │
        │  GET /api/me                  │  GET /api/admin/logged-users
        │  (upsert last_seen_at)        │  (require_writer)
        ▼                              ▼
┌────────────────────────────────────────────────────────────────┐
│                   FastAPI Backend                               │
│                                                                │
│  api/main.py                                                   │
│  ├─ /api/me ──► upsert_user(email) ──► DBManager               │
│  └─ include_router(admin_router, prefix="/api/admin")           │
│                                                                │
│  api/admin_routers.py  (NEW)                                   │
│  └─ GET /logged-users ──► require_writer ──► authz.py           │
│                        ──► DBManager.list_logged_users()        │
│                                                                │
│  core/db_manager.py                                            │
│  ├─ upsert_user(email, display_name, source)  (NEW)            │
│  └─ list_logged_users(limit)                  (NEW)            │
│       └─ SELECT ... FROM users ORDER BY last_seen_at DESC       │
│       └─ 逐行计算 access_rights（WRITERS 集合成员 → "writer"）   │
│                                                                │
│  api/authz.py  (已有，不变)                                     │
│  ├─ require_writer → 403 if not request_can_write(request)     │
│  └─ WRITERS = set(os.getenv("AITELIER_WRITERS").split(","))    │
└────────────────────────────────────────────────────────────────┘
        │
        ▼
┌────────────────────────────────────────────────────────────────┐
│                   SQLite (aitelier.db)                          │
│                                                                │
│  users 表 (已有)                                                │
│  ├─ email        TEXT PRIMARY KEY                               │
│  ├─ display_name TEXT                                          │
│  ├─ source       TEXT DEFAULT 'cloudflare'                      │
│  ├─ created_at   DATETIME DEFAULT CURRENT_TIMESTAMP             │
│  └─ last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP             │
└────────────────────────────────────────────────────────────────┘
```

---

## 组件列表

### 1. `api/admin_routers.py` — 新建

- **职责**: 提供 writer-only 的管理 API 端点
- **接口**:
  - `GET /api/admin/logged-users?limit=50` → `List[LoggedUser]`
    - 依赖: `require_writer` (FastAPI Depends)
    - 返回: `[{email, display_name, source, last_seen_at, access_rights}]`
- **数据模型**: 新增 `LoggedUser` Pydantic model（或在 `models/schemas.py` 中定义）
- **文件路径**: `./api/admin_routers.py`

### 2. `api/main.py` — 修改

- **职责**: 注册 admin router；在 `/api/me` 中 upsert 用户
- **变更点**:
  1. 导入并 `include_router(admin_router)`
  2. 在 `whoami()` 处理器中，当 email 非空时调用 `db.upsert_user(email, ...)` 更新 `last_seen_at`
- **文件路径**: `./api/main.py`

### 3. `web_api/main.py` — 修改

- **职责**: Web API 入口也需注册 admin router
- **变更点**: 导入并 `include_router(admin_router)`
- **文件路径**: `./web_api/main.py`

### 4. `core/db_manager.py` — 修改

- **职责**: 新增两个 DB 方法
- **新增方法**:
  - `upsert_user(email, display_name=None, source='cloudflare')`:
    ```sql
    INSERT INTO users (email, display_name, source, last_seen_at)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(email) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
    ```
  - `list_logged_users(limit=50)`:
    ```sql
    SELECT email, display_name, source, created_at, last_seen_at
    FROM users ORDER BY last_seen_at DESC LIMIT ?
    ```
    在 Python 层计算每个用户的 `access_rights`（对比 `authz.WRITERS` 集合）
- **文件路径**: `./core/db_manager.py`

### 5. `web/js/views/tracking.js` — 新建

- **职责**: UserTracking 视图，渲染用户跟踪表格
- **命名空间**: `window.AItelier.UserTracking`
- **方法**:
  - `show(params)` — 从 API 获取数据并渲染到 `#view-tracking`
  - `hide()` — 清空 `#view-tracking` 内容
- **渲染内容**:
  ```html
  <table>
    <thead><tr><th>Email</th><th>Latest Access</th><th>Access Rights</th></tr></thead>
    <tbody>
      <!-- 每行: email | 格式化 last_seen_at | "writer" 或 "reader" -->
    </tbody>
  </table>
  ```
  - 空状态: "No users tracked yet."
  - 错误状态: 红色错误消息
  - 加载状态: 表格区域显示 loading 文字
- **遵循模式**: 与 `views/dashboard.js` 一致的自执行 IIFE + `window.AItelier.X = X` 暴露
- **文件路径**: `./web/js/views/tracking.js`

### 6. `web/js/api.js` — 修改

- **职责**: 新增 `getLoggedUsers()` API 方法
- **新增方法**:
  ```js
  getLoggedUsers: function(limit) {
    var path = "/api/admin/logged-users";
    if (limit) { path += "?limit=" + encodeURIComponent(limit); }
    return _get(path);
  }
  ```
- **文件路径**: `./web/js/api.js`

### 7. `web/js/app.js` — 修改

- **职责**: 注册 tracking 路由 + 动态 nav 渲染 + view 名称映射
- **变更点**:
  1. **路由注册**: 在 `routes` 数组中添加 `{ pattern: "#/tracking", view: tracking }`，变量 `tracking` 从 `window.AItelier.UserTracking` 获取（可选，trace 同款模式）
  2. **动态 nav**: 新增 `_renderNav()` 函数，在 `_applyReadOnlyMode()` 成功后调用：
     - 获取 `#nav-links` ul 元素
     - 清空并重建：Dashboard、Chat
     - 若 `state.canWrite === true`：追加 Tracking 链接
  3. **`_viewName()`**: 添加 tracking view 映射 → `"tracking"`
  4. **`_trackView()`**: 添加 `#/tracking` hash 识别 → `state.currentView = "tracking"`
  5. **`_refreshActiveViewPermissions()`**: 添加 tracking view refresh 路径
- **HTML 配合**: `index.html` 中第二个 `<ul>` 需添加 `id="nav-links"`，便于 JS 定位和动态填充
- **文件路径**: `./web/js/app.js`

### 8. `web/index.html` — 修改

- **职责**: 新增 view 容器 + 脚本引用 + nav id
- **变更点**:
  1. 在 `<main id="view-container">` 中添加 `<section id="view-tracking"></section>`
  2. 在 views 脚本区添加 `<script src="/web/js/views/tracking.js"></script>`
  3. 给第二个 `<ul>`（links 区）添加 `id="nav-links"`，方便 JS 动态填充
  4. 移除该 `<ul>` 内部的静态 `<li>` 项（改由 JS 生成）
- **文件路径**: `./web/index.html`

---

## 数据流

```
[用户加载页面]
     │
     ▼
GET /api/me ──────────────────────────────────────────────┐
     │  (email from CF JWT headers)                       │
     │  → db.upsert_user(email)                           │
     │  → return {email, can_write, gate_enabled}         │
     ▼                                                    │
app.js: state.canWrite = me.can_write                      │
app.js: _renderNav()                                       │
     │                                                    │
     ├─ canWrite=true  → nav 包含 "Tracking" 链接          │
     └─ canWrite=false → nav 不含 "Tracking" 链接          │
                                                          │
[Writer 点击 Tracking 链接]                                │
     │                                                    │
     ▼                                                    │
router.js: #/tracking → UserTracking.show()                │
     │                                                    │
     ▼                                                    │
tracking.js: API.getLoggedUsers()                          │
     │                                                    │
     ▼                                                    │
GET /api/admin/logged-users ───────────────────────────────┘
     │  (require_writer dependency → 403 if reader)
     │  → db.list_logged_users()
     │  → compute access_rights per row
     ▼
[{email, display_name, last_seen_at, access_rights}, ...]
     │
     ▼
tracking.js: render <table> into #view-tracking
```

---

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 后端框架 | FastAPI (Python) | 已有，不变 |
| 数据库 | SQLite via `sqlite3` | 已有 `users` 表，无需 migration |
| 前端框架 | Vanilla JS (IIFE 模块) | 已有模式，无需引入新框架 |
| CSS | Pico CSS (classless) | 已有 CDN，表格自动获得样式 |
| 认证 | Cloudflare Access JWT | 已有 `cf_access.py` + `authz.py` |
| 路由 | Hash-based SPA Router | 已有 `web/js/router.js` |

---

## 扩展性考虑

1. **分页**: 当前 `list_logged_users(limit=50)` 预留 limit 参数；未来可扩展为 offset/limit 分页。
2. **历史日志**: 当前只记录 `last_seen_at`；若需完整访问历史，可新增 `user_access_log` 表，在 `/api/me` 中追加一条日志记录。
3. **`access_rights` 持久化**: 当前动态计算；若未来 writer 集合变化频繁且需历史快照，可新增 `access_rights TEXT` 列，在 upsert 时写入。
4. **WebSocket 实时更新**: 当前为手动刷新；若需实时，可通过 SSE 推送新用户上线事件（tracking 页面订阅）。
5. **多租户**: `web_api/auth.py` 已有 per-user scheduler 模式；admin router 在 `web_api/main.py` 中注册后自动适配。

---

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 使用现有 `users` 表而非新建表 | `users` 表已有 email 和 last_seen_at，无需重复存储 |
| `access_rights` 查询时计算 | writer 集合来自环境变量，可能动态变化 |
| 在 `/api/me` 中 upsert `last_seen_at` | `/api/me` 每次页面加载都被调用，无需新增中间件 |
| 动态 nav（JS 生成）而非 CSS 隐藏 | 满足"reader 看不到按钮"（DOM 中不存在）的硬性要求 |
| 新建 `api/admin_routers.py` | 隔离 admin 端点，清晰标识访问控制边界 |
| 不在 DB 中添加 `access_rights` 列 | 避免 env var 与 DB 数据不一致的同步问题 |
