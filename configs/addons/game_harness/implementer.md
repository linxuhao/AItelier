## Godot / GDScript 实现约定（本任务是 Godot 游戏脚本，务必遵守）
交付为**一个可直接运行的 Godot 工程**（`project.godot` + `.gd` + `.tscn`），目标"打开即玩"。整仓脚本会被 headless **导入解析**校验、主场景会被 headless **运行冒烟**（Godot 4 / `4.4`）：

- **`project.godot`**：必须设 `run/main_scene="res://<主场景>.tscn"`；跨场景单例在 `[autoload]` 段；自定义输入动作在 `[input]` 段；`config/features` 标 `"4.4"`。
- **场景 `.tscn` 直接以文本编写**：`[gd_scene format=3]` + `[ext_resource type="Script" path="res://x.gd" id="1"]` + `[node ...]` 树 + `script = ExtResource("1")`。**主场景须自足**：加载即含相机 / 玩家 / 生成器 / UI / 碰撞体，可直接跑。
- **⚠️ `.tscn` 块顺序（会导致运行时"场景加载失败/节点为 null"）**：块必须**严格按 `[gd_scene]` → 所有 `[ext_resource]` → 所有 `[sub_resource]` → 所有 `[node]` 的顺序**书写。被某个 `[node]` 用 `SubResource("X")` 引用的 `[sub_resource ... id="X"]` **必须写在该 node 之前**（放到文件末尾会导致该资源解析不到、整个场景实例化失败——`@onready var n = $Child` 得到 null，运行时崩 "null instance"）。`[gd_scene]` 头写上 `load_steps=<ext+sub+1>`。**导入解析（compile）查不出这类顺序错，只有运行冒烟(playtest)能暴露**——所以务必一次写对。
- **GDScript 规范**：`extends` 合适基类（`Node`/`CharacterBody2D`/`Area2D`/`Node3D`/`Control`…）；用**信号**（`signal foo` + `foo.emit()`）解耦；**加类型标注**（`var score: int = 0`、`func flap() -> void:`）让解析闸门更早发现错误；资源用 `res://` 或 `preload(...)`。
- **API 版本**：只用 **Godot 4** API。常见替换：`KinematicBody2D`→`CharacterBody2D`（`velocity`+`move_and_slide()`）、`yield(...)`→`await`、`.tscn format=2`→`format=3`、`OS.get_ticks_msec`→`Time.get_ticks_msec`、`instance()`→`instantiate()`、`connect("sig",self,"m")`→`sig.connect(m)`。**⚠️ 内置成员名冲突（最常见的解析错误）**：绝不要用节点基类已有的内置成员名去声明变量（`var`/`@export var`）——否则解析报 "Member ... redefined (original in native class ...)"，脚本整个加载失败。高频雷区：`CharacterBody2D`/`RigidBody2D` 已有 **`velocity`**（直接 `velocity.y = ...` 赋值，**不要** `var velocity`）；`Area2D`/物理体已有 **`gravity`**（换名如 `fall_accel`）；所有 `Node2D` 已有 `position`/`rotation`/`scale`/`global_position`。自定义状态一律换个不冲突的名字（`fall_speed`、`spin_rate`…）。
- **输入用 Godot Input**：`Input.is_action_just_pressed("ui_accept")` 或 `_input(event)` 判 `InputEventMouseButton`/`InputEventScreenTouch`/`InputEventKey`。收敛到一个方法。**冒烟测试自动按 `ui_accept`——让游戏至少响应它**，否则状态快照会显示"一潭死水"。
- **引用一致**：节点路径（`$Path` / `get_node`）、信号名、方法签名、`class_name`、autoload 名前后一致——任何一处错都会在导入解析或运行冒烟时暴露。
- **每个 gameplay 脚本都必须真正接入场景**：写了一个需要运行时存在的节点脚本（玩家/敌人/生成器/边界/管理器），**必须挂到某个 `.tscn` 的节点上、或注册为 autoload、或由主脚本在 `_ready()` 里 `add_child(...)` 实例化**。否则解析通过却从未进入场景树 → 运行时形同不存在（解析闸门查不出，但运行冒烟 + 状态快照能暴露）。
- **交付一份 `README.md`**：说明装 Godot 4.4+、Import 工程、F5 开玩、操作键，以及"怎么把占位节点（`Polygon2D`/`ColorRect`）换成 `Sprite2D`+贴图"。（工程 `.gitignore` 由系统自动加入。）
- **纯逻辑测试可选**：如需单测,抽成不依赖场景树的普通 GDScript 类,保持最小。
- **落地行为测试契约 `playtest_spec.yaml`（工程根）——这是别人给你出的卷子，不是你自己出的**：PM 已产出完整的 `playtest_spec.yaml`（场景剧本 + 断言）。**把它原样写到工程根 `playtest_spec.yaml`**，一个字都不要改。
  - **断言失败时改代码，不要改断言。** 严禁删断言、把 `"changed"` 弱化成 `"!= null"`、把阈值调松、或把按键从时间线里摘掉——这等于自己出卷自己判卷，也正是"闸门全绿、游戏不能玩"的来路。确实认为某条断言写错了（与架构师 `surface` 矛盾、或物理上不可能），**在交付说明里写清楚哪一条、为什么**，交给验收者裁决。
  - 时间线里按键只有两个合法键名：`press: <动作>` 或 `actions: [<动作>, …]`。写别的键闸门会**硬失败**报 "unknown key"。
  - 闸门每次都会额外跑一遍**无输入对照**：某个按了键的场景若最终状态与对照分毫不差，直接**硬失败**（"输入没进游戏"）。所以 spec 里用到的每个动作名都必须真在 `project.godot` `[input]` 段（或是内置 `ui_*`），且游戏真的在读它。**攻击/确认这类核心动作不要只绑鼠标点击**——只绑 `InputEventMouseButton` 的话键盘玩家和闸门都够不着它——运行时 playtest 闸门会读它，按剧本时间线按键并用 `Expression` 对活节点求值断言。**实现必须与其 `surface` 契约逐字一致**：断言引用的**节点名**（如 `Bird`/`ScoreLabel`）、**脚本变量名**（如 `velocity`/`score`）、**输入动作名**（如 `flap`，须在 `project.godot` `[input]` 段定义）必须真实存在且拼写一致——`Expression` 直接对活节点求值，名字对不上断言即失败、`error` 记 "node not found"/"parse error"。被断言的量要用**脚本变量**（`var`，能被求值/快照），且避开内置成员名冲突（见上：`velocity` 直接用基类的、别 `var velocity`）。

### 契约的存放形式：优先 `playtest/` 目录，一个场景一个文件
若工程里已经有 `playtest/` 目录（`_common.yaml` 放 `scene`/`actions`/`surface`/`scenario_order`，其余每个文件是一个场景），**契约就在那里**：改哪个场景就改 `playtest/<场景名>.yaml` 那一个文件，**不要**把整份契约重新写回根目录的 `playtest_spec.yaml`——那个文件此时已被闸门忽略（报告的 `summary` 会写明它被忽略），你写进去的改动不会被执行。只有还没拆分、根目录只有 `playtest_spec.yaml` 的工程才照旧整份落地。
拆分的原因：26 个场景挤在 1478 行一个文件里时，改一个场景就是重写全部 26 个，而重写全部 26 个正是断言凭空消失的方式——2026-08-24 的审计发现有场景"修完"之后断言数变少了，评审没看出来，因为 diff 有整个文件那么宽。

### 交付前必须自己跑过：`godot_playtest_scenario`（硬条件，不是建议）
你有 `godot_playtest_scenario(scenario="<场景名>")`：只跑那一个场景（也可以逗号分隔跑几个），秒级返回每条断言的通过情况，失败的断言会带上 `observed`——属性当时**实际**的值。它会自动把你这一步还没交付的暂存文件叠加到仓库上再跑，所以测的是你的改动，不是你正在替换的旧代码。
它是探针不是闸门：单个场景绿了 **不等于** 构建绿了。只有 `5_compile` 的 26 场景全量跑才能抓出"修好 X 弄坏了 Y"。

**硬条件:凡是新增或改动了 playtest 场景的卡片,交付前必须跑过自己的场景,
并把 `observed` 值贴进交付说明。**「改完场景当场验证」原本写在这里是一句建议,
于是新写场景直接交付的卡片没照做 —— 下面是那次的代价。

| | 耗时(实测 2026-08-25) |
|---|---|
| 单跑 1 条场景 | **16 秒** |
| 单跑 3 条 | 38 秒 |
| 全量闸门(43 条 + 视觉) | **27 分钟** |

`jinyong-clickmove`:点击移动三条场景全红,真因是全屏的 `SegmentHost` 漏写
`mouse_filter`,在 GUI 阶段把落到空地的点击吃掉了。单跑 **16 秒**就能看见
(`Player.debug_click_events` 停在 0)。实际是等到半小时后的全量闸门才发现,
连带一次重规划和第二遍全量闸门 —— 约一小时,只为一条 16 秒能看见的事实。

**新增观测量的卡片同理:交付前要确认它真的被写过。**同一天另一轮里,
六个单位的 `portrait_visible` 全读 `false`、`portrait_fail_layer` 全读 `""` ——
那不是数据,是变量的初始值(调用它的函数根本没编译过),而且两个值互相矛盾
(`false` 说不可见,`""` 说完全可见)。
**一个从没被写过的观测量,读起来和一个读数为假的观测量一模一样。**

要逼出某个变量此刻的真值,用 `inline_scenario` 传一段临时 YAML(永不写进仓库),
断言写成不可能成立的形式(`x and not x`),报告就会打印出 `observed`。
**不要**为了取值往 `playtest/` 里写临时文件 —— 未列出的文件也会被加载器执行,
忘删一个就红整道闸门。
