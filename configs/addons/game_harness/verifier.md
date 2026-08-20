## Godot 游戏项目验收（本项目是 Godot 游戏）

你的上下文里有两份客观闸门报告（`Step 5_compile` 章节）：
- 解析报告 `compile_report.json`（`passed` / `errors`（每条含 `kind` / res:// `file` / `line` / `msg`）/ `summary` / `gate_skipped`）—— **真实 headless 导入解析了脚本**。
- 运行时冒烟报告 `playtest_report.json`（`passed`（**硬门槛：崩溃 / 主场景跑不起来 / spec 有非法键 / 输入没进游戏**）/ `spec_errors[]`/ `frames` / `errors`（运行时异常，含 res:// file+line）/ **`state`（运行若干帧后场景树各节点的脚本变量+位置快照）** / `behavior`（**行为断言结果，建议性**）/ `spec_used` / `summary`）—— **真实无头 Godot 运行了主场景**。

### 解析（硬性门槛）
- `compile_report.json` `passed: false`（有解析错误）→ **必须判 passed: false**；feedback 里逐条列出 `errors`（`kind`/`file`/`line`/`msg`）。
- **`gate_skipped: true`**（真实 Godot 项目但 godot-builder 不可达，脚本未验证就交付）→ **不翻转 passed**，但**在 `suggestions` 顶部放醒目告警**（"⚠️ 解析门槛未运行（godot-builder 不可达）：GDScript 未经验证"）。

### 运行时陷阱（解析通过也要静态查）
- **主场景未设 / 加载不了**：`project.godot` 必须设 `run/main_scene` 且能无头加载。
- **孤儿脚本（从未进入场景树）——你独有的全局视角**：逐个核对每个需要运行时存在的 gameplay 脚本是否**挂在某个 `.tscn` 的节点上、或注册为 autoload、或被主脚本 `_ready()` 里 `add_child(...)`**（grep 脚本路径/`class_name` 是否出现在 `.tscn` 的 `ext_resource`、`project.godot` 的 `[autoload]`、或某处 `add_child`）。解析通过但从未进场景树的脚本运行时形同不存在——发现遗漏判 passed: false。
- **Godot 3 遗留 API**：`KinematicBody2D` / `yield(...)` / `instance()` / `connect("sig",self,"m")` 等 → 指出改用 Godot 4 写法。

### 运行时冒烟 + 状态核查（硬性门槛）
- `playtest_report.json` `passed: false` → **必须判 passed: false**；feedback 逐条列出原因。四种硬失败各有对策：
  - `errors` 非空（运行时异常，含 res:// file+line）→ 逐条列出。
  - `spec_errors` 非空 → `playtest_spec.yaml` 的 timeline 里有闸门不认识的键（按键键名只能是 `press` / `actions`）。要求原样修键名，**不要**顺手删掉那条时间线。
  - 某个场景 `behavior.scenarios[].input_dead: true` → **该场景按了键，最终状态与"全程不按键"的对照跑完全一致：输入根本没进游戏**。这是最值钱的一条信号——它抓的正是"断言全绿但游戏是死的"。核对三处：spec 用的动作名是否真在 `project.godot` `[input]`（或内置 `ui_*`）；游戏是否真的在读这个动作；核心动作是否只绑了鼠标点击（`InputEventMouseButton`）而键盘/闸门够不着。
  - 主场景加载失败。
- **核对 spec 有没有被实现者改软**：`playtest_spec.yaml` 应与 PM 计划里那份逐字一致。若发现断言被删、`"changed"` 被弱化成 `"!= null"` / `"visible == true"`、阈值被调松、或时间线里的按键被摘掉 → **判 passed: false**，指明是哪一条。自己出卷自己判卷的构建，闸门颜色没有意义。
- **善用 `state` 快照（Godot 独有）**：`state` 是运行若干帧后各节点脚本变量+位置的实拍（如 `{"/root/Main/Bird": {"vars": {"score": 4}, "pos": [120, 320]}}`）。据此核查游戏逻辑是否**真的在动**：分数是否随时间/输入变化、`game_state` 是否合理、玩家位置/速度是否在变。若该动的没动（一潭死水），即便无异常也可能是"建好却不动"的缺陷——作为 issue（全新项目至少列为 suggestion）。
- **行为断言 `behavior`（TDD 式，建议性强信号）**：当工程根有 `playtest_spec.yaml` 时（`spec_used: true`），报告含 `behavior.all_passed` 与 `behavior.scenarios[]`（每个 `{name, passed, asserts:[{name, node, expr, passed, actual, error}]}`）。这是架构师/PM 事先写下的"玩起来应该怎样"的客观断言，闸门用 `Expression` 对活节点实测。**断言失败不硬性翻转 `passed`**（崩溃/跑不起来才由 `passed:false` 硬失败并回环），但它是可玩性的第一手客观信号：
  - 有 `passed:false` 的断言 → 逐条在 `suggestions`/`issues` 指出（带 `expr` 与实测 `actual`）。**关键玩法断言成片失败**（如"拍翼上升""加分"全不过）→ 应判 `passed: false` 回环重做；零星/边界断言失败 → 至少列为 suggestion。
  - **差分断言**（`"changed"` / `"unchanged"`）的 `actual` 形如 `{"baseline": …, "current": …}`——直接就能看出"按了键之后这个量到底动没动"。这类断言失败通常不是 spec 写错，而是**玩法真的没接通**，优先按缺陷处理。
  - `error` 非空（`node not found` / `parse error` / `execute failed`）→ 说明 spec 与实现的**契约对不上**（节点名/脚本变量名/动作名不一致，或该量不是脚本变量）→ 明确指出这一不一致（既可能是实现漏了、也可能是 spec 写错，据实判断）。
- **`spec_used: false`**：工程根没有 `playtest_spec.yaml`，回退到旧版按 `ui_accept` 的冒烟——没有行为断言，只有 `state` 快照可查；据 `state` 静态核查即可（全新项目建议提示补 `playtest_spec.yaml` 以获得客观行为门槛）。
- **`gate_skipped: true`**（真实 Godot 项目但 godot-builder 不可达）→ 不翻转 passed，但在 `suggestions` 加醒目告警（"⚠️ 运行时冒烟门槛未运行"），回退到静态核查。

### README 交付
- 检查是否有 `README.md` 说明"装 Godot / F5 开玩 / 怎么换美术"。缺失或与实际严重不符可作为质量问题指出（但格式偏好不阻塞）。
