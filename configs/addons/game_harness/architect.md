## Godot 游戏项目专项（本项目是 Godot 游戏，务必遵守）
- **版本与语言**：目标为 **Godot 最新稳定版（Godot 4 / `4.4`）**，脚本用 **GDScript**（agent 友好、迭代快、无需编译工具链）。**只做全平台通用**，不设计任何平台专属功能。
- **交付形态 = 一个可直接运行的 Godot 工程**：Godot 的场景文件 `.tscn` 是**纯文本、可 diff、可由 agent 直接编写**——所以交付的是**完整可跑工程**（`project.godot` + `.gd` 脚本 + `.tscn` 场景），而非"纯脚本 + 人工搭场景"。**`project.godot` 必须设 `run/main_scene="res://<主场景>.tscn"`**——校验闸门会 headless 导入并运行这个主场景。场景本身就是可交付、可 diff 的文本，无需"用代码重建场景 + 烘焙菜单"。
- **"打开即玩"——用 Godot 图元做占位，不要让用户先准备美术**：占位视觉一律用 **Godot 内置图元节点，不引入任何二进制美术资源**：`Polygon2D`（圆/多边形）、`ColorRect`（矩形/UI 底）、`Sprite2D` + 代码生成的 `ImageTexture`、3D 用 `CSGBox3D` / `MeshInstance3D`+`BoxMesh`。按 category 选：主角 2D→圆 `Polygon2D` / 3D→`CSGBox3D`；障碍/平台→`ColorRect` 或矩形 `StaticBody`；地面→长条 `StaticBody`；收集品/子弹→小圆/小球；背景→`ColorRect` 或相机背景色；UI 文字→`Label`。**主场景自足**：加载即含相机 / 玩家 / 生成器 / UI / 碰撞体，按 F5 即玩。
- **输入走 Godot Input 动作**：在 `project.godot` 的 `[input]` 段定义动作，或复用内置 `ui_accept`/`ui_select`（空格/回车）。tap/click/触屏统一用 `_input(event)` 判 `InputEventMouseButton` / `InputEventScreenTouch`，或 `Input.is_action_just_pressed("ui_accept")`。把"是否有任意输入"收敛到单一方法。**运行时冒烟测试会自动周期性按 `ui_accept`，让游戏至少响应它才能被自动 playtest 推进。**
- **跨场景单例用 autoload**：`GameManager`、分数等设为 autoload（`project.godot` 的 `[autoload]` 段），用信号（`signal`/`emit`）广播状态变化。
- **交付物清单里必须包含**：一份 `README.md`（说明装 Godot 4.4+、F5 开玩、怎么把占位节点换成真美术）。工程根 `.gitignore`（含 `.godot/`）由系统自动加入，无需设计。
- **可运行性**：整仓脚本被自动 headless 导入解析校验、主场景被自动 headless 运行冒烟（捕获运行时异常 + 快照运行时各节点脚本变量状态）。确保脚本间接口（`class_name`/信号名/方法签名/节点路径）一致、主场景能被无头加载。
- **linter_manifest**：`.gd` 由 Godot 导入自动解析，**不必写进 manifest**；manifest 只覆盖其它文本文件（`.json`/`.md` 用 `basic`）。只有 GDScript/场景时可为 `{}`。

## 行为测试契约 `playtest_spec.yaml`（你负责"可观测面 + 剧本骨架"）
运行时 playtest 已升级为**脚本化剧本 + 断言的 TDD 式测试**：工程根的 `playtest_spec.yaml` 是"预期"，闸门按剧本时间线按键、并在指定帧用 `Expression` 对活节点求值断言。该文件由你与 PM 分工产出——**你定义可观测面 + 剧本骨架，PM 填断言阈值**：
- `scene`：主场景 `res://<主场景>.tscn`（默认即主场景，可省略）。
- `actions`：游戏用到的输入动作名（如 `flap`）——**这些动作必须同时在 `project.godot` `[input]` 段定义**；playtest 会按剧本按这些动作（不再只按 `ui_accept`）。
- `surface`：断言可引用的**节点→脚本变量白名单**，如 `Bird: [velocity, position]`、`ScoreLabel: [score]`。这是**给实现者的硬契约**：实现里节点名 / 脚本变量名 / 动作名必须与此**逐字一致**（断言用 `Expression` 直接对活节点求值，名字对不上断言即失败）。
- `scenarios[]`：每个 `{name, timeline}`——你只搭**骨架**（这个场景测什么行为、`at` 哪几帧、`press` 什么动作、在哪帧放 `assert` 占位），**断言阈值留给 PM 填**。
产出：把 `playtest_spec.yaml` 的 `scene`/`actions`/`surface` + 剧本骨架写进你的架构产物，作为 PM 与实现者的契约。示例见 `playtest_spec.example.yaml`。
