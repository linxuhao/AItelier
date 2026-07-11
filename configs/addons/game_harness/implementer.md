## Godot / GDScript 实现约定（本任务是 Godot 游戏脚本，务必遵守）
交付为**一个可直接运行的 Godot 工程**（`project.godot` + `.gd` + `.tscn`），目标"打开即玩"。整仓脚本会被 headless **导入解析**校验、主场景会被 headless **运行冒烟**（Godot 4 / `4.4`）：

- **`project.godot`**：必须设 `run/main_scene="res://<主场景>.tscn"`；跨场景单例在 `[autoload]` 段；自定义输入动作在 `[input]` 段；`config/features` 标 `"4.4"`。
- **场景 `.tscn` 直接以文本编写**：`[gd_scene format=3]` + `[ext_resource type="Script" path="res://x.gd" id="1"]` + `[node ...]` 树 + `script = ExtResource("1")`。**主场景须自足**：加载即含相机 / 玩家 / 生成器 / UI / 碰撞体，可直接跑。
- **GDScript 规范**：`extends` 合适基类（`Node`/`CharacterBody2D`/`Area2D`/`Node3D`/`Control`…）；用**信号**（`signal foo` + `foo.emit()`）解耦；**加类型标注**（`var score: int = 0`、`func flap() -> void:`）让解析闸门更早发现错误；资源用 `res://` 或 `preload(...)`。
- **API 版本**：只用 **Godot 4** API。常见替换：`KinematicBody2D`→`CharacterBody2D`（`velocity`+`move_and_slide()`）、`yield(...)`→`await`、`.tscn format=2`→`format=3`、`OS.get_ticks_msec`→`Time.get_ticks_msec`、`instance()`→`instantiate()`、`connect("sig",self,"m")`→`sig.connect(m)`。**注意节点内置成员名冲突**：不要 `@export var gravity`（`Area2D`/物理体已有 `gravity`），换个名（如 `fall_accel`）——否则解析报 "Member redefined"。
- **输入用 Godot Input**：`Input.is_action_just_pressed("ui_accept")` 或 `_input(event)` 判 `InputEventMouseButton`/`InputEventScreenTouch`/`InputEventKey`。收敛到一个方法。**冒烟测试自动按 `ui_accept`——让游戏至少响应它**，否则状态快照会显示"一潭死水"。
- **占位视觉用内置图元，无二进制资源**：`Polygon2D` / `ColorRect` / `Sprite2D`+代码 `ImageTexture` / `CSGBox3D` / `MeshInstance3D`+`BoxMesh`。2D 渲染顺序用节点树顺序或 `z_index`。
- **引用一致**：节点路径（`$Path` / `get_node`）、信号名、方法签名、`class_name`、autoload 名前后一致——任何一处错都会在导入解析或运行冒烟时暴露。
- **每个 gameplay 脚本都必须真正接入场景**：写了一个需要运行时存在的节点脚本（玩家/敌人/生成器/边界/管理器），**必须挂到某个 `.tscn` 的节点上、或注册为 autoload、或由主脚本在 `_ready()` 里 `add_child(...)` 实例化**。否则解析通过却从未进入场景树 → 运行时形同不存在（解析闸门查不出，但运行冒烟 + 状态快照能暴露）。
- **交付一份 `README.md`**：说明装 Godot 4.4+、Import 工程、F5 开玩、操作键，以及"怎么把占位节点（`Polygon2D`/`ColorRect`）换成 `Sprite2D`+贴图"。（工程 `.gitignore` 由系统自动加入。）
- **纯逻辑测试可选**：如需单测,抽成不依赖场景树的普通 GDScript 类,保持最小。
