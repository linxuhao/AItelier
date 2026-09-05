## Godot 游戏项目验收（本项目是 Godot 游戏）

最终单测结果以 `Step 5_final_test` 的 `test_report.json` 为准：它在
`5_design` 的仓库写入之后执行。`Step 5_test` 只说明设计更新前的状态，
不能用它覆盖最终报告。最终报告缺失、跳过、零测试（`no_tests_collected:true`）或失败均不得通过验收。
编译、运行时与可读性报告仍分别审查，最终单测不替代这些闸门。

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
- **`gate_timeout: true`**（闸门等超时了）→ **必须判 passed: false**，而且不要把它当成 `gate_skipped` 的同类。区别是实的：`gate_skipped` 是**服务不在**（环境事实，没东西可判）；`gate_timeout` 是**跑着呢，我们自己把电话挂了**——这一版代码一条都没被验证过，而原因在我们这边。把它读成通过，就是让闸门以「通过」的形式消失。feedback 里写明:要么场景数涨过了预算（调 `post_playtest` 的 timeout），要么有场景挂住（边车对每条场景另有 120 秒上限，所以整套越界通常意味着条数长了）。

### README 交付
- 检查是否有 `README.md` 说明"装 Godot / F5 开玩 / 怎么换美术"。缺失或与实际严重不符可作为质量问题指出（但格式偏好不阻塞）。

### 设计档案与 UX 待办（`5_design` 就在你上游）

`5_design` 现在跑在你**之前**（`5_knowledge → 5_design → 你`），所以这一轮的
`design/` 变更和 `design/40_ux_backlog.md` 的关闭都已经落盘，在你的审查范围内。
以前它跑在你之后、直接进 `done`，那份跨 run 活得最久的档案从来没人检查过。

**待办的每一行，两种结果都可接受**，你要判的是它写得对不对，不是它有没有关：

- `CLOSED(<本轮>)` —— 证据栏必须点名**一条场景**及其闸门结果
  （例如 `playtest_summary.md: skill_button_effect_info 5/5`）。三件事都要核对：
  1. 这条场景在 `playtest_summary.md` 里确实是 `PASS`；
  2. 报告是**本轮**的（仓库里可能躺着上一轮的 `final/verify_report.json`，
     它存在、可读、格式正确，但场景名属于别的轮次）；
  3. **这条场景真的验证了这一行**——只是"某条绿场景"不算数。这一条是你的判断，
     不是机器能替你做的。
- `OPEN` + 说明 —— 场景红了、或报告里根本没有对应场景时的**正确**结果。
  一条诚实的 OPEN **不是**缺陷，不要因此判红。

**一行的当前状态只看表格那一列,不看 changelog。** `40_ux_backlog.md` 的下半部分
是一本**流水账**:同一条待办可能先被关掉、再被复核推翻改回 OPEN、再由后来的证据
重新关掉,每一步都留一条记录。那些历史条目描述的是**当时**,不是现在。

实测,jinyong-hud 2026-08-27:表格里 UX-03/04/05 三行的状态列写着
`CLOSED(jinyong-hud)`,而审查判定"三条仍为 OPEN",引用的原话是
「修复已落,post-fix 闸门证据待验」—— 那句话在全文只出现在 changelog 的三条
历史记录里,是被后一条记录**推翻过的**中间状态。整轮就因为这个被判红重跑,而那
件事早已做完。

要判一行现在是开是关,读它在表格里的那一行;changelog 用来看它**怎么**走到今天。

**不要因为"这一轮的验收标准说要关掉三条"就要求关闭。** 该关的依据是证据，不是
指标。反过来，一条**无证据的 CLOSED 是阻塞项**——它把"没验过"写成了"验过了"，
而这份档案会被下一轮的 architect 和 PM 当作事实读。



### 最终树验证缺证据

`5_final_test/test_report.json` 的 `no_tests_collected:true` 表示设计写入之后
没有执行任何 pytest 检查，即使通用工具写 `passed:true`，最终门也会拒绝放行。
重复运行不能补齐证据。应提供覆盖实际设计约束或关键行为的 pytest smoke，
或另行实现设计写入之后的 Godot 复验；本轮流水线尚未提供后一条路径。
不得创建空测试、恒真断言或删掉标记来充数。
