## Godot 游戏项目专项（本项目是 Godot 游戏，务必遵守）
- **版本与语言**：目标为 **Godot 最新稳定版（Godot 4 / `4.4`）**，脚本用 **GDScript**（agent 友好、迭代快、无需编译工具链）。**只做全平台通用**，不设计任何平台专属功能。
- **交付形态 = 一个可直接运行的 Godot 工程**：Godot 的场景文件 `.tscn` 是**纯文本、可 diff、可由 agent 直接编写**——所以交付的是**完整可跑工程**（`project.godot` + `.gd` 脚本 + `.tscn` 场景），而非"纯脚本 + 人工搭场景"。**`project.godot` 必须设 `run/main_scene="res://<主场景>.tscn"`**——校验闸门会 headless 导入并运行这个主场景。场景本身就是可交付、可 diff 的文本，无需"用代码重建场景 + 烘焙菜单"。
- **"打开即玩"——规划一套真实美术/音频资源，而不是占位图元**：实现步骤可以调 `gen_image_asset` / `gen_audio_asset` 直接生成贴图与音效，所以设计阶段要给出**资源清单**：需要哪些精灵（主角 / 障碍 / 收集品 / 背景 / 地面）、哪些音效（跳跃 / 得分 / 碰撞 / 失败）、是否要背景音乐，以及**一句贯穿全部精灵的统一风格描述**（如 "pixel art, flat colors, 16-bit retro, side view"）——风格不统一是生成美术最容易露怯的地方。**这句话只许写画风，绝不能点名游戏对象**：把物件清单（"green pipes, sandy ground, yellow bird"）写进统一风格句，会让生图模型把每样东西都画进每一张图，地面贴图里就会长出一只鸟。每个资源的主体只写在它自己那一条 prompt 里。**主场景自足**：加载即含相机 / 玩家 / 生成器 / UI / 碰撞体，按 F5 即玩。只有资源生成失败时才退回 Godot 内置图元（`Polygon2D` / `ColorRect` / `CSGBox3D`）作为占位。
- **输入走 Godot Input 动作**：在 `project.godot` 的 `[input]` 段定义动作，或复用内置 `ui_accept`/`ui_select`（空格/回车）。tap/click/触屏统一用 `_input(event)` 判 `InputEventMouseButton` / `InputEventScreenTouch`，或 `Input.is_action_just_pressed("ui_accept")`。把"是否有任意输入"收敛到单一方法。**运行时冒烟测试会自动周期性按 `ui_accept`，让游戏至少响应它才能被自动 playtest 推进。**
- **跨场景单例用 autoload**：`GameManager`、分数等设为 autoload（`project.godot` 的 `[autoload]` 段），用信号（`signal`/`emit`）广播状态变化。
- **交付物清单里必须包含**：一份 `README.md`（说明装 Godot 4.4+、F5 开玩、操作键，以及美术/音效资源分别是什么、哪些是占位）。工程根 `.gitignore`（含 `.godot/`）由系统自动加入，无需设计。
- **可运行性**：整仓脚本被自动 headless 导入解析校验、主场景被自动 headless 运行冒烟（捕获运行时异常 + 快照运行时各节点脚本变量状态）。确保脚本间接口（`class_name`/信号名/方法签名/节点路径）一致、主场景能被无头加载。
- **linter_manifest**：`.gd` **不要写进 manifest** —— 它由 `gdscript_check` 闸门在每个实现步骤之后用 `godot --check-only` 逐文件解析（宿主控制，不经 manifest，免得一个拼错的后端名把闸门静默关掉）。manifest 只覆盖其它文本文件（`.json`/`.md` 用 `basic`）。只有 GDScript/场景时可为 `{}`。

## 行为测试契约 `playtest_spec.yaml`（你负责"可观测面 + 剧本骨架"）
运行时 playtest 已升级为**脚本化剧本 + 断言的 TDD 式测试**：工程根的 `playtest_spec.yaml` 是"预期"，闸门按剧本时间线按键、并在指定帧用 `Expression` 对活节点求值断言。该文件由你与 PM 分工产出——**你定义可观测面 + 剧本骨架，PM 填断言阈值**：
- `scene`：主场景 `res://<主场景>.tscn`（默认即主场景，可省略）。
- `actions`：游戏用到的输入动作名（如 `flap`）——**这些动作必须同时在 `project.godot` `[input]` 段定义**；playtest 会按剧本按这些动作（不再只按 `ui_accept`）。
- `surface`：断言可引用的**节点→脚本变量白名单**，如 `Bird: [velocity, position]`、`ScoreLabel: [score]`。这是**给实现者的硬契约**：实现里节点名 / 脚本变量名 / 动作名必须与此**逐字一致**（断言用 `Expression` 直接对活节点求值，名字对不上断言即失败）。
- `scenarios[]`：每个 `{name, timeline}`——你只搭**骨架**（这个场景测什么行为、`at` 哪几帧、按什么动作、在哪帧放 `assert` 占位），**断言阈值留给 PM 填**。
  - 按键写法两种都可以：`- { at: 0, press: flap }`（一帧一个动作）或 `- { at: 0, actions: [move_right, skill_1] }`（同一帧多个）。**`press` / `actions` 是 timeline 条目里唯一的按键键名**，写别的（`keys:`、`input:`…）闸门会**硬失败**并报 "unknown key"——不再静默跳过。注意别把顶层的 `actions:`（动作名单声明）和 timeline 条目里的 `actions:`（这一帧按什么）搞混，两者都合法但含义不同。
  - **每个 scenario 的骨架里至少要有一次按键**：一个从头到尾不按键的场景只能测"初始画面长什么样"，测不了可玩性。
  - **至少一个场景必须开到终局**：把游戏一路推到胜/负/得分那一刻（小鸟撞死、敌人血量归零、分数 +1）。只测"能开机"的场景组合，正是"每一关闸门全绿、游戏根本不能玩"的成因。
  - **`surface` 必须包含能证明"游戏在推进"的变量**（分数、血量、状态机字符串、格子坐标），而不只是 `visible` 这类存在性字段——PM 要用它们写差分断言。
产出：把 `playtest_spec.yaml` 的 `scene`/`actions`/`surface` + 剧本骨架写进你的架构产物，作为 PM 与实现者的契约。示例见 `playtest_spec.example.yaml`。

## 游戏设计档案 `design/`（仓库根目录，全文已注入你的上下文）
这是这个游戏**跨 run 的长期设计档案**——系统、规则、内容、手感。它不是参考资料，是你的**输入约束**：本次架构必须与它一致。（代码怎么组织写在你的 `step2_design.md` 里，一次性的；游戏是什么，在 `design/` 里。目录约定见 `design/README.md`。）
- **与档案冲突时只有两种合法处理**：(a) 改你的设计去符合档案；(b) 本次 run 就是要改设计——那就在 `step2_design.md` 里写一节 **"设计变更"**：改了什么、为什么、冲击到哪些既有系统与 playtest 场景。run 通过最终验收后，`5_design` 步骤据此外科式地更新档案。**偷偷改、不声明**，等于让代码和档案分叉，下一次 run 会照着过时的档案把你的改动改回去。
- `design/90_decisions.md` 的 **Out of scope** 是**已被否决的想法 + 否决理由**。别把里面的东西重新设计回来，除非用户这次明确点名要。
- `design/20_content.md` 的数值（血量 / 伤害 / 冷却 / 移动力 / 阵容 / 关卡）是实现者会直接照抄的**事实**。你要改数值，就在"设计变更"里写出新旧值。
- 如果档案整份都是占位符，说明这是这个仓库第一次做设计：把你这次定下的系统与内容**完整**写进 `step2_design.md`，`5_design` 会把它落成档案。
