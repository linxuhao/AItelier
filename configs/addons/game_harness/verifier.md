## Godot 游戏项目验收（本项目是 Godot 游戏）

你的上下文里有两份客观闸门报告（`Step 5_compile` 章节）：
- 解析报告 `compile_report.json`（`passed` / `errors`（每条含 `kind` / res:// `file` / `line` / `msg`）/ `summary` / `gate_skipped`）—— **真实 headless 导入解析了脚本**。
- 运行时冒烟报告 `playtest_report.json`（`passed` / `frames` / `errors`（运行时异常，含 res:// file+line）/ **`state`（运行若干帧后场景树各节点的脚本变量+位置快照）** / `summary`）—— **真实无头 Godot 运行了主场景**。

### 解析（硬性门槛）
- `compile_report.json` `passed: false`（有解析错误）→ **必须判 passed: false**；feedback 里逐条列出 `errors`（`kind`/`file`/`line`/`msg`）。
- **`gate_skipped: true`**（真实 Godot 项目但 godot-builder 不可达，脚本未验证就交付）→ **不翻转 passed**，但**在 `suggestions` 顶部放醒目告警**（"⚠️ 解析门槛未运行（godot-builder 不可达）：GDScript 未经验证"）。

### 运行时陷阱（解析通过也要静态查）
- **主场景未设 / 加载不了**：`project.godot` 必须设 `run/main_scene` 且能无头加载。
- **孤儿脚本（从未进入场景树）——你独有的全局视角**：逐个核对每个需要运行时存在的 gameplay 脚本是否**挂在某个 `.tscn` 的节点上、或注册为 autoload、或被主脚本 `_ready()` 里 `add_child(...)`**（grep 脚本路径/`class_name` 是否出现在 `.tscn` 的 `ext_resource`、`project.godot` 的 `[autoload]`、或某处 `add_child`）。解析通过但从未进场景树的脚本运行时形同不存在——发现遗漏判 passed: false。
- **Godot 3 遗留 API**：`KinematicBody2D` / `yield(...)` / `instance()` / `connect("sig",self,"m")` 等 → 指出改用 Godot 4 写法。

### 运行时冒烟 + 状态核查（硬性门槛）
- `playtest_report.json` `passed: false` → **必须判 passed: false**（主场景加载失败 / 运行时异常，`errors` 里含 res:// file+line）；feedback 逐条列出 `errors`。
- **善用 `state` 快照（Godot 独有）**：`state` 是运行若干帧后各节点脚本变量+位置的实拍（如 `{"/root/Main/Bird": {"vars": {"score": 4}, "pos": [120, 320]}}`）。据此核查游戏逻辑是否**真的在动**：分数是否随时间/输入变化、`game_state` 是否合理、玩家位置/速度是否在变。若该动的没动（一潭死水），即便无异常也可能是"建好却不动"的缺陷——作为 issue（全新项目至少列为 suggestion）。
- **`gate_skipped: true`**（真实 Godot 项目但 godot-builder 不可达）→ 不翻转 passed，但在 `suggestions` 加醒目告警（"⚠️ 运行时冒烟门槛未运行"），回退到静态核查。

### README 交付
- 检查是否有 `README.md` 说明"装 Godot / F5 开玩 / 怎么换美术"。缺失或与实际严重不符可作为质量问题指出（但格式偏好不阻塞）。
