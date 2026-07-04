# 技术架构设计

## 概述

本功能修复 AItelier web UI 的 i18n 国际化三个问题，并执行翻译覆盖率审计。核心改动：将 `lib/i18n.ts` 迁移到 `lib/i18n.svelte.ts`，使用 Svelte 5 `$state` rune + `langStore.subscribe()` 实现响应式 `t()` 函数；为 6 种缺失语言生成完整翻译；更新所有 11 个视图组件的 import 路径；运行审计脚本确保翻译覆盖率 100%。

---

## 架构图（文字描述）

```
                  stores/i18n.ts
                  (writable store — 保持不变)
                        │
                  langStore.set()
                  langStore.subscribe()
                        │
         ┌──────────────┴──────────────┐
         │  lib/i18n.svelte.ts  (NEW)  │
         │                             │
         │  let currentLang =          │
         │    $state(get(langStore))   │
         │                             │
         │  langStore.subscribe(v =>   │
         │    currentLang = v)         │
         │                             │
         │  export function t(key) {   │
         │    // reads currentLang →   │
         │    // Svelte 5 instruments  │
         │    // as reactive dep       │
         │  }                          │
         │                             │
         │  const translations = {     │
         │    en:   { ... },           │
         │    zh-CN:{ ... },           │
         │    zh-TW:{ ... },  ← NEW    │
         │    ja:   { ... },  ← NEW    │
         │    ko:   { ... },  ← NEW    │
         │    fr:   { ... },  ← NEW    │
         │    de:   { ... },  ← NEW    │
         │    es:   { ... },  ← NEW    │
         │  }                          │
         └──────────────┬──────────────┘
                        │
         import { t } from '../lib/i18n.svelte'
                        │
         ┌──────────────┼──────────────┐
         │              │              │
    AppBar.svelte  Chat.svelte   ... (11 components)
    t('appbar.dashboard')       t('chat.title')
         │              │              │
         ▼              ▼              ▼
    语言切换时 Svelte 5 编译器检测到 currentLang
    的 $.get() 依赖 → 自动触发组件重渲染
```

### 数据流

1. 用户在 AppBar 下拉菜单选择语言 → `onLangChange()` → `setLang(lang)` → `langStore.set(lang)` + `localStorage.setItem()` + 后端 `setUserLang()` API
2. `langStore.subscribe()` 回调触发 → `currentLang = v`（在 `i18n.svelte.ts` 内部，Svelte 编译器将其编译为 `$.set(currentLang, v)`）
3. 所有正在渲染的组件中，`t()` 函数读取 `currentLang` → 编译器检测到 `$.get(currentLang)` 依赖变化 → Svelte 5 响应式系统标记这些组件的模板为 dirty → 自动重渲染
4. 重渲染时 `t(key)` 使用新语言的 `translations[lang][key]` → UI 实时更新

---

## 组件列表

### 组件1: `web/src/lib/i18n.svelte.ts`（核心改动 — 新建）

- **职责**: 响应式 i18n 翻译函数 + 全部 8 种语言的翻译字典
- **关键设计**:
  - 文件扩展名 `.svelte.ts` 启用 Svelte 5 runes 编译
  - 模块级 `let currentLang = $state(get(langStore))` — 初始值从 store 读取
  - `langStore.subscribe(v => { currentLang = v; })` — store 变化时同步到 `$state`，触发所有消费组件的重渲染
  - `export function t(key: string): string` — 签名不变，内部读取 `currentLang`（而非 `get(langStore)`）
  - `const translations: Record<Lang, Record<string, string>>` — 8 种语言的完整字典
  - `type Lang = 'en' | 'zh-CN' | 'zh-TW' | 'ja' | 'ko' | 'fr' | 'de' | 'es'` — 类型安全
- **从旧文件 `web/src/lib/i18n.ts` 迁移内容**:
  - 保留 `en` 和 `zh-CN` 翻译字典（全部现有 key）
  - 保留 `t()` 的 fallback 逻辑：`translations[lang] || translations[lang.split('-')[0]]` → `translations['en'][key] || key`
- **新增**: `zh-TW`, `ja`, `ko`, `fr`, `de`, `es` 六种语言的翻译字典（基于 `en` 原文机器翻译生成）
- **接口**: `export function t(key: string): string`（签名不变）

### 组件2: `web/src/lib/i18n.ts`（删除）

- **职责**: 旧的非响应式 i18n 模块
- **操作**: 删除此文件（内容已迁移到 `i18n.svelte.ts`）

### 组件3-13: 11 个视图组件（import 路径更新）

- **文件列表**:
  - `web/src/views/AppBar.svelte`
  - `web/src/views/Chat.svelte`
  - `web/src/views/CheckpointModal.svelte`
  - `web/src/views/ConfirmDialog.svelte`
  - `web/src/views/Dashboard.svelte`
  - `web/src/views/NotificationPanel.svelte`
  - `web/src/views/Project.svelte`
  - `web/src/views/RepoPanel.svelte`
  - `web/src/views/Trace.svelte`
  - `web/src/views/Tracking.svelte`
  - `web/src/views/WorkspaceBrowser.svelte`
- **改动**: 每个文件的 import 行从 `import { t } from '../lib/i18n';` 改为 `import { t } from '../lib/i18n.svelte';`
- **其他改动**: 无。所有 `t('...')` 调用保持不变。

### 组件4: 审计脚本（一次性工具）

- **职责**: 提取所有 `.svelte` 组件中的 `t('...')` / `t("...")` 调用 key，交叉比对 8 种语言字典，输出遗漏报告
- **实现**: Node.js 脚本（可直接 `node audit-i18n.mjs` 运行），无外部依赖
- **输出**: 按语言分组的缺失 key 列表（stdout），如果全部覆盖则输出成功消息
- **执行时机**: 翻译生成完成后，作为验证步骤运行

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **响应式状态** | Svelte 5 `$state` rune | 在 `.svelte.ts` 模块中使用，建立跨组件响应式依赖 |
| **Store 同步** | `svelte/store` `subscribe()` | 将 writable store 的变化同步到 `$state` |
| **翻译生成** | 机器翻译（Google Translate / DeepL API） | 基于 `en` 原文逐条翻译，保留 `{n}`, `{id}` 等占位符 |
| **类型系统** | TypeScript `Record<Lang, Record<string, string>>` | 编译期确保所有 8 种语言字典结构一致 |
| **审计** | Node.js 脚本 + 正则提取 | 无额外依赖，`fs` + `path` 标准库 |
| **构建** | Vite + `@sveltejs/vite-plugin-svelte` | 已配置 `compilerOptions.runes: true`，自动编译 `.svelte.ts` |

---

## 接口规范

### `t(key: string): string` 签名不变

```typescript
// 调用方完全不变
const text = t('appbar.dashboard');  // → "Dashboard" (en) / "仪表盘" (zh-CN)

// 带插值的使用模式保持不变
t('repo.actionDone').replace('{name}', 'commit');  // → "commit done."
t('dashboard.attempt').replace('{n}', '3');         // → "Attempt 3"
```

### `langStore` 接口不变

```typescript
// stores/i18n.ts — 完全不变
export const langStore = writable<string>(initialLang());
export async function setLang(lang: string): Promise<void>;  // 设置语言 + localStorage + 后端同步
export async function syncInitialLang(): Promise<void>;      // 首次访问语言同步
```

### 翻译字典结构

```typescript
type Lang = 'en' | 'zh-CN' | 'zh-TW' | 'ja' | 'ko' | 'fr' | 'de' | 'es';

const translations: Record<Lang, Record<string, string>> = {
  en:    { 'appbar.dashboard': 'Dashboard', ... },
  'zh-CN': { 'appbar.dashboard': '仪表盘', ... },
  'zh-TW': { 'appbar.dashboard': '儀表板', ... },
  ja:    { 'appbar.dashboard': 'ダッシュボード', ... },
  ko:    { 'appbar.dashboard': '대시보드', ... },
  fr:    { 'appbar.dashboard': 'Tableau de bord', ... },
  de:    { 'appbar.dashboard': 'Dashboard', ... },
  es:    { 'appbar.dashboard': 'Panel', ... },
};
```

### Import 路径变更

```diff
- import { t } from '../lib/i18n';
+ import { t } from '../lib/i18n.svelte';
```

---

## 改动清单

| # | 文件 | 操作 | 描述 |
|---|------|------|------|
| 1 | `web/src/lib/i18n.svelte.ts` | **新建** | 响应式 i18n 模块：`$state` + `langStore.subscribe()` + 8 语言完整字典 |
| 2 | `web/src/lib/i18n.ts` | **删除** | 旧非响应式模块，内容已迁移 |
| 3 | `web/src/views/AppBar.svelte` | 修改 | import 路径 `'../lib/i18n'` → `'../lib/i18n.svelte'` |
| 4 | `web/src/views/Chat.svelte` | 修改 | 同上 |
| 5 | `web/src/views/CheckpointModal.svelte` | 修改 | 同上 |
| 6 | `web/src/views/ConfirmDialog.svelte` | 修改 | 同上 |
| 7 | `web/src/views/Dashboard.svelte` | 修改 | 同上 |
| 8 | `web/src/views/NotificationPanel.svelte` | 修改 | 同上 |
| 9 | `web/src/views/Project.svelte` | 修改 | 同上 |
| 10 | `web/src/views/RepoPanel.svelte` | 修改 | 同上 |
| 11 | `web/src/views/Trace.svelte` | 修改 | 同上 |
| 12 | `web/src/views/Tracking.svelte` | 修改 | 同上 |
| 13 | `web/src/views/WorkspaceBrowser.svelte` | 修改 | 同上 |

---

## 翻译生成策略

### 源数据

以 `en` 字典为权威源（唯一 key 集合）。当前 `en` 字典包含约 160 个唯一 key（含重复 key，选取最后出现的值）。

### 生成方法

1. 从 `en` 字典提取去重后的 key-value 对
2. 对 6 种目标语言（zh-TW, ja, ko, fr, de, es）逐条翻译英文 value
3. **占位符保护**: 所有 `{n}`, `{id}`, `{label}`, `{error}`, `{msg}`, `{pct}`, `{cat}`, `{text}`, `{name}`, `{branch}` 占位符在译文中原样保留
4. **HTML 标签保护**: `<strong>`, `</strong>`, `<code>`, `</code>` 在译文中原样保留
5. 每种语言的字典 key 顺序与 `en` 保持一致，便于 diff

### zh-TW 特殊处理

`zh-TW` 与 `zh-CN` 共用大部分汉字但用繁体。在 `zh-CN` 翻译基础上做繁简转换 + 用词调整（如 "智能体" → "智慧體"），而非从英文重新翻译。

---

## 审计策略

### 步骤

1. 遍历 `web/src/views/` 下所有 `.svelte` 文件
2. 正则提取所有 `t('...')` 和 `t("...")` 调用中的 key
3. 收集全局唯一 key 集合
4. 对每种语言（en, zh-CN, zh-TW, ja, ko, fr, de, es），检查每个 key 是否存在于 `translations[lang]`
5. 输出报告：按语言列出缺失 key（如有）；全部覆盖时输出成功消息

### 边界处理

- **重复 key**: 字典中同一个 key 出现多次时，以最后一次赋值为准（JavaScript 对象语义），审计时不报错
- **旧版 key 名**: `en` 中同时存在 `repo.makePR` 和 `repo.makePr`、`repo.notGit` 和 `repo.notGitWithPath` 等新旧命名。审计时两者都视为有效 key，翻译生成时两者都需覆盖
- **不在组件中的 key**: 翻译字典中可能存在未被任何组件引用的 key（如旧版命名变体），这些不报缺失（仅审计"组件用了但字典缺了"的方向）

---

## 扩展性考虑

- **新增语言**: 只需在 `translations` 对象中添加新语言字典，并在 `Lang` 类型联合中追加语言代码。`AppBar.svelte` 的 `LANG_OPTIONS` 数组中添加对应条目即可。
- **新增翻译 key**: 在 `en` 字典中添加新 key，在 `t()` 调用中使用即可。其他 7 种语言可后续补译（fallback 到 `en`）。
- **翻译懒加载**: 当前所有 8 种语言 ~160 条翻译内联在 bundle 中（约 15-20KB gzip'd）。如果未来支持超过 20 种语言，可改为按需动态 `import()` 各语言字典，`t()` 改为 async，但当前规模不需要。
- **插值引擎**: 当前使用 `.replace('{n}', ...)` 手动替换。如果未来需要复数规则、日期格式化等，可引入 `intl-messageformat` 或 `i18next`（需评估 bundle size），但当前项目规模用 `.replace()` 完全够用。
- **`stores/i18n.ts` 保持不变**: 语言持久化逻辑（localStorage + 后端 API 同步）完全在 store 层，与 `lib/i18n.svelte.ts` 通过 `subscribe()` 松耦合，未来可独立演进任何一侧。

---

## 不可逆操作回滚设计

### 文件重命名回滚

- **操作**: `lib/i18n.ts` → 删除 + `lib/i18n.svelte.ts` → 新建
- **回滚**: 旧文件 `lib/i18n.ts` 的内容完整迁移到新文件（加上响应式改造），Git 历史可追溯。如需回滚：恢复 `lib/i18n.ts` 并删除 `lib/i18n.svelte.ts`，将 11 个组件的 import 路径改回 `'../lib/i18n'`。
- **校验**: 回滚后运行 `npm run build`（Vite build），确认无编译错误。

### 翻译字典迁移回滚

- **操作**: `en` 和 `zh-CN` 字典从旧文件复制到新文件
- **回滚**: 旧文件内容完整保留在 Git 历史中。新文件中的 `en`/`zh-CN` 字典与旧文件逐 key 一致（仅新增其他 6 种语言，不修改已有翻译）。

---

## 需要关注的风险点

1. **`$state` 在 `.svelte.ts` 模块顶层的初始化时机**: `get(langStore)` 在模块加载时执行，如果 `langStore` 尚未初始化（SSR/测试环境），`get()` 返回初始值 `initialLang()` 即 `localStorage` 值或 `browserLang()` 或 `'en'`，安全。
2. **订阅泄漏**: `langStore.subscribe()` 返回 unsubscribe 函数。由于这是应用级单例模块（整个 SPA 生命周期内不销毁），不调用 unsubscribe 也不会泄漏。
3. **Vite HMR 兼容性**: `.svelte.ts` 文件在 Vite HMR 时模块重新执行，`$state` 会重新初始化。Vite 的 Svelte 插件会处理 HMR boundary，重新执行后 `$state` 从 `get(langStore)` 获取当前值（store 在 HMR 中保持），行为正确。
