# 技术架构设计

## 概述

本功能为 Aitieler web UI 的缓存命中率展示增加总 token 消耗量的显示。改动极小：在后端已有计算的基础上暴露 `total_tokens` 字段，前端增加一个简单的数字格式化工具函数并在两处 badge 文本中追加 token 数量。

无新增组件、无新增端点、无数据流变更。

---

## 架构图（文字描述）

```
                         skillflow_trace (SQLite)
                               │
                    ┌──────────┴──────────┐
                    │ SUM(cache_hit)       │
                    │ SUM(cache_miss)      │
                    └──────────┬──────────┘
                               │
              ┌────────────────┴────────────────┐
              │  api/_cache_stats.py            │
              │  _build_stats_dict(hit, miss)   │
              │    → return {..., total_tokens} │  ← 改动：新增 total_tokens
              │                                 │
              │  compute_cache_stats_per_step() │  ← 自动受益
              │  compute_cache_stats_batch()    │  ← 自动受益
              └────────────────┬────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
  GET /api/runs          GET /api/runs/{id}     (其他消费者)
  list_all_runs()        get_run_detail()
  → runs[].cache_stats   → cache_stats            ← 改动：新增 total_tokens
  → 自动受益             → cache_stats_by_step    ← 自动受益
         │                     │
         ▼                     ▼
  web/js/views/           web/js/views/
  dashboard.js            project.js
  _createRow()            _renderRunOverviewHtml()
  → cache badge           → per-step cache badge
  → 追加 token 数          → 追加 token 数
  + _fmtTokens() helper   + _fmtTokens() helper
```

数据流：
1. `skillflow_trace` 表存储每条 trace 的 `cache_hit_tokens` / `cache_miss_tokens`（JSON payload）
2. `_cache_stats.py` 的 SQL 查询按 step_id 或 run_id 聚合 SUM，传入 `_build_stats_dict()`
3. `_build_stats_dict()` 计算 `total = hit + miss` 并返回 dict（**改动：dict 中增加 `total_tokens` 键**）
4. `run_routers.py` 的 `get_run_detail()` 在构建 `cache_stats` 时也显式加入 `total_tokens`（**改动：dict 中增加 `total_tokens` 键**）
5. 前端从 API 响应中读取 `cache_stats.total_tokens`，格式化后追加到 badge 文本中

---

## 组件列表

### 组件1: `api/_cache_stats.py` — `_build_stats_dict()`

- **职责**: 将原始 cache_hit_tokens / cache_miss_tokens 聚合为包含 total_tokens 的统计 dict
- **改动**: 
  - 现有 return dict: `{"cache_hit_tokens", "cache_miss_tokens", "hit_ratio"}`
  - 新 return dict: `{"cache_hit_tokens", "cache_miss_tokens", "hit_ratio", "total_tokens"}`
  - `total_tokens = cache_hit_tokens + cache_miss_tokens`（第20行已计算，仅需加入返回 dict）
- **接口**: `Dict[str, Any]` —— 从两个 int 参数构建
- **消费者**: `compute_cache_stats_per_step()`, `compute_cache_stats_batch()`, 以及最终所有 API 端点

### 组件2: `api/run_routers.py` — `get_run_detail()`

- **职责**: 为单个 run 构建完整的 cache_stats 汇总
- **改动**:
  - 现有 `cache_stats` dict: `{"cache_hit_tokens", "cache_miss_tokens", "hit_ratio"}`
  - 新 `cache_stats` dict: 增加 `"total_tokens": total`（第208行已计算 `total`，仅需加入 dict）
- **接口**: 返回给 `GET /api/runs/{run_id}` 的 JSON 响应
- **消费者**: `project.js` 前端视图

### 组件3: `api/run_routers.py` — `list_all_runs()`

- **职责**: 批量返回所有 run 的列表
- **改动**: 无。它调用 `compute_cache_stats_batch()`，后者返回 `_build_stats_dict()` 的 dict，`total_tokens` 自动包含在内。
- **接口**: 返回给 `GET /api/runs` 的 JSON 响应，每个 run 携带 `cache_stats` 对象（可能为 null）
- **消费者**: `dashboard.js` 前端视图

### 组件4: `web/js/views/dashboard.js` — `_createRow()` + `_fmtTokens()`

- **职责**: 渲染 dashboard run 列表行中的 cache 内联 badge，显示缓存命中率 + 总 token 数
- **改动**:
  - 新增 `_fmtTokens(n)` 工具函数（~5行）：`n < 1000` 时返回原始数字字符串，否则返回 `(n/1000).toFixed(1) + "k"`
  - 修改 `_createRow()` 中 cache badge 构建逻辑（第264-273行）：在现有 `" · Cache 72.3%"` 后追加 `" · 12.5k"`
  - 使用 `cs.total_tokens`（新增字段），当其为 `null`/`undefined` 时退化为不追加（与 `hit_ratio` 为 null 时的行为一致）
- **接口**: 无外部接口变更 — 读取 `project.cache_stats.total_tokens`

### 组件5: `web/js/views/project.js` — `_renderRunOverviewHtml()` + `_fmtTokens()`

- **职责**: 渲染项目详情页 Pipeline stepper 中每个 step 的 cache badge，显示缓存命中率 + 总 token 数
- **改动**:
  - 新增 `_fmtTokens(n)` 工具函数（与 dashboard.js 中相同逻辑）
  - 修改 `_renderRunOverviewHtml()` 中 per-step cache badge 构建逻辑（第591-599行）：在现有 `"72.3%"` 后追加 `" · 12.5k"`
  - 使用 `cs.total_tokens`（新增字段）
  - 注意：per-step cache stats 来自 `run.cache_stats_by_step[stepId]`，由 `compute_cache_stats_per_step()` 提供，该函数返回 `_build_stats_dict()` 的 dict，故 `total_tokens` 自动包含
- **接口**: 无外部接口变更 — 读取 `run.cache_stats_by_step[stepId].total_tokens`

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **后端聚合** | Python 3.11+ / FastAPI | `_build_stats_dict()` 和 `get_run_detail()` 中的内联算术，无第三方依赖 |
| **前端格式化** | Vanilla JavaScript | `_fmtTokens()` 工具函数，约5行，遵循项目现有的 `_fmtNum` 模式 |
| **前端渲染** | 原生 DOM API | `textContent` 赋值，无框架 |
| **CSS** | 不修改 | 已有 `.cache-inline-badge`, `.run-step-badge.run-step-cache`, `.cache-badge-high/mid/low` 类名继续使用 |

---

## 接口规范

### 后端 API 响应格式变更

#### `GET /api/runs` — `list_all_runs()`
```json
{
  "runs": [
    {
      "project_id": "...",
      "cache_stats": {
        "cache_hit_tokens": 1000,
        "cache_miss_tokens": 500,
        "hit_ratio": 0.6667,
        "total_tokens": 1500        // 新增
      }
    }
  ]
}
```

#### `GET /api/runs/{run_id}` — `get_run_detail()`
```json
{
  "cache_stats": {
    "cache_hit_tokens": 1000,
    "cache_miss_tokens": 500,
    "hit_ratio": 0.6667,
    "total_tokens": 1500           // 新增
  },
  "cache_stats_by_step": {
    "1": {
      "cache_hit_tokens": 300,
      "cache_miss_tokens": 100,
      "hit_ratio": 0.75,
      "total_tokens": 400          // 新增（自动来自 _build_stats_dict）
    }
  }
}
```

### 前端显示格式

#### Dashboard run list (`dashboard.js`)
```
· Cache 72.3% · 12.5k          (hit_ratio >= 0.7 → green badge)
· Cache 45.2% · 7.8k           (0.3 <= hit_ratio < 0.7 → yellow badge)
· Cache 18.9% · 123            (<1000 → raw number, hit_ratio < 0.3 → red badge)
```
- 当 `cs.hit_ratio == null` 时：整个 `· Cache ...` 文本不渲染（现有逻辑，与 `total_tokens` 无关）
- 当 `cs.total_tokens` 缺失（向后兼容）时：仅显示百分比，不追加 token 数

#### Project detail per-step badge (`project.js`)
```
72.3% · 12.5k                  (badge 文本，与 dashboard 格式一致但无 "· Cache" 前缀)
18.9% · 123
```
- 当 `cs.hit_ratio == null` 时：整个 badge 不渲染（现有逻辑）

### `_fmtTokens()` 规范

```
输入     → 输出
123      → "123"
999      → "999"
1000     → "1.0k"
1234     → "1.2k"
12543    → "12.5k"
99999    → "100.0k"
123456   → "123.5k"
```

实现（伪代码）：
```javascript
function _fmtTokens(n) {
  if (typeof n !== "number" || n < 1000) return String(n);
  return (n / 1000).toFixed(1) + "k";
}
```

---

## 改动清单

| # | 文件 | 改动类型 | 描述 |
|---|------|----------|------|
| 1 | `api/_cache_stats.py` | 修改 | `_build_stats_dict()` 返回 dict 增加 `"total_tokens": total` |
| 2 | `api/run_routers.py` | 修改 | `get_run_detail()` 的 `cache_stats` dict 增加 `"total_tokens": total` |
| 3 | `web/js/views/dashboard.js` | 修改 | 新增 `_fmtTokens()`；`_createRow()` 追加 token 数到 cache badge |
| 4 | `web/js/views/project.js` | 修改 | 新增 `_fmtTokens()`；`_renderRunOverviewHtml()` 追加 token 数到 per-step badge |

---

## 扩展性考虑

- **`_fmtTokens()` 位置**: 目前每个 view 文件各定义一份（遵循项目现有模式，如 `dashboard.js` 和 `project.js` 各自定义 `_STEP_LABELS`, `_STATUS_CLASS_MAP` 等）。如果后续更多视图需要 token 格式化，可提取到 `web/js/utils.js` 作为 `AItelier.Utils.formatTokens()`。
- **M 级 token 格式化**: 当前 MVP 仅做 k 级格式化。如果未来单 run token 数超过百万，可在 `_fmtTokens()` 中增加 `>= 1_000_000 → (n/1_000_000).toFixed(1) + "M"` 分支，改动仅限该工具函数。
- **后端 `total_tokens` 字段**: 在所有 dict 中统一键名为 `"total_tokens"`，与已有的 `"cache_hit_tokens"` / `"cache_miss_tokens"` 命名风格一致。未来如有其他 token 统计需求（如按模型分组的 token），可按相同模式扩展。
- **无新增数据库字段**: `total_tokens` 是查询时从已有数据派生，不涉及 schema 变更。
