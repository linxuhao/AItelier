## 行为测试契约 `playtest_spec.yaml`（你负责"断言阈值"部分）

架构师已在 `playtest_spec.yaml` 定义了可观测面（`scene`/`actions`/`surface`）与场景剧本骨架（`scenarios[].timeline`）。你的职责：**为每个 `scenario` 的时间线填入可量化的验收断言**，把用户故事的验收标准落成客观、可自动判定的行为检查（TDD 式）——这就是运行时 playtest 闸门的"预期"。

- 断言写在某帧的 `assert:` **映射**里，每条 `节点.属性路径: 期望`：
  - **键** = `节点名.属性路径`。节点名取**第一个点之前**（场景子路径用 `/`，如 `HUD/PausedLabel.visible` → 节点 `HUD/PausedLabel`、属性 `visible`）；节点名须来自架构师 `surface`。
  - **值是标量**（`true`/`false`/数字）→ 相等断言（`GameManager.paused: true` 即 `paused == true`；`GameManager.state: 0` 即 `state == 0`）。
  - **值是含比较运算符的字符串**（`==`/`!=`/`<`/`>`/`and`/`or`…）→ 原样作为 **GDScript 表达式**，以该节点为 `self` 求值（`Bird.velocity.y: "velocity.y != 0"`、`GameManager.score: "score >= 1"`）。
  - 运行时以 `Expression` 对**活节点**求值，得 `true` 即该断言通过。
  - **值是 `"changed"` / `"unchanged"`** → **差分断言**：把该属性此刻的值与**本场景第 0 帧的值**比较（`Player.grid_pos: "changed"`、`Score.value: "changed"`）。报告里 `actual` 会同时给出 `baseline` 与 `current`。
- **每个 scenario 至少一条差分断言（硬性要求）**：存在性断言（`visible: true`、`grid_pos: "grid_pos != null"`）在一个**完全不响应输入的死游戏上照样满分**——已经真实发生过：某作品所有断言全绿，实测玩家一步没动、敌人全程僵直、伤害从未结算。凡是"按了键之后应该变"的量，一律写成 `"changed"`，别写成"不为 null"。
- **每个关键可玩性 → 至少一条断言**：把用户故事里"按了会怎样 / 达成条件是什么"翻成断言。时间线用 `press`（单个动作）或 `actions: [a, b]`（同帧多个）触发输入、`at` 指定检查帧（先按键、隔几帧再断言，给物理留反应时间）。
- **必须有一个"开到终局"的场景**：一路推到胜/负/得分那一刻并断言它（`GameManager.state: "state == GameState.GAME_OVER"`、`Enemy.health: "health <= 0"`、`Score.value: "changed"`）。只断言中间态，等于只验证了游戏能开机。
- 挑**可观测、稳定、不脆**的量：速度符号（`velocity.y < 0`）、分数下界（`score >= 1`）、位置区间——避免依赖精确浮点相等或某一具体帧的脆值。
- 断言只能引用架构师 `surface` 声明的节点/变量。若需要新的可观测量，**回写让架构师补进 `surface`**（并要求实现暴露该脚本变量），而不是去断言私有内部状态。
- **门槛语义（重要）**：行为断言是**"建议性强信号"**——断言失败**不会硬性阻断构建**；但验收者会重点参考它判断"玩起来对不对"。所以断言要真实反映玩法正确性，宁缺毋滥、勿脆。
  硬失败（会回环重做）只有四种，其中两种是"这份 spec 什么都没测出来"：
  1. 崩溃 / 主场景跑不起来；
  2. timeline 条目里有闸门不认识的键（拼错的按键键名）；
  3. **某个按了键的场景，最终状态与"全程不按键"的对照跑分毫不差** —— 说明输入根本没进游戏（动作名与 `project.godot [input]` 对不上、被模态窗吃掉、或游戏压根不读它）。闸门每次都会额外跑一遍无输入对照来比对；
  4. 主场景加载失败。

示例（在架构师骨架上填入断言）：

```yaml
scenarios:
  - name: 拍翼让小鸟上升
    timeline:
      - { at: 0, press: flap }
      - at: 8
        assert:
          Bird.velocity.y: "velocity.y < 0"
  - name: 不拍翼则受重力下落
    timeline:
      - at: 40
        assert:
          Bird.velocity.y: "velocity.y > 0"
  - name: 越过管道加分
    timeline:
      - at: 180
        assert:
          ScoreLabel.score: "score >= 1"
```

产出：把补全断言后的**完整 `playtest_spec.yaml`** 写进你的计划产物，并指示实现者原样落地到工程根（`playtest_spec.yaml`）。

## 游戏设计档案 `design/`（全文已注入你的上下文）
拆任务、写 `playtest_spec.yaml` 的断言阈值时，**数值一律以 `design/20_content.md` 为准**（血量 / 伤害 / 冷却 / 移动力 / 阵容 / 关卡构成）——不要自己发明数字，也不要照抄 `playtest_spec.example.yaml` 里 Flappy Bird 的示例值。
- 档案与 architect 的 `step2_design.md` 冲突时：以 `step2_design.md` 里**显式声明的"设计变更"**为准；那一节没提到的，一律以档案为准。两边都没说的，当成没变。
- 任务卡里引用规则时直接引原文（"移动力 3 格 / 黯然销魂掌 25 伤害 1 格击退 冷却 4"），而不是"参考设计档案"——实现者拿到的是任务卡，不是整份档案。

## 素材任务要在卡片上声明 `capabilities: ["game_assets"]`

架构师产出的资源清单（精灵 / 音效 / BGM）不会自己变成工具。**要生成美术或音频的那些
任务卡，必须写 `capabilities: ["game_assets"]`**——写了，实现者才拿得到
`gen_image_asset` / `gen_audio_asset` 和它们的纪律说明；不写，实现者手上没有这两个
工具，只能拿 `ColorRect` 顶上，而闸门看不出区别。

- 先看 `capability_palette` 给出的清单，只声明里面有的名字（写别的会被本步骤的校验
  直接打回）。
- 只给**真的要产出素材**的任务写。纯逻辑、纯 UI 布局、纯修 bug 的任务不要写——每一次
  声明都让那个任务的每一轮多背几 KB。
- 一份资源清单没有任何一张卡片认领，就等于那份清单不会被实现。


## UX 待办的**关闭**不是任务卡,不要排

`design/40_ux_backlog.md` 里某一行由 OPEN 变成 `CLOSED(<本轮>)`,依据只能是**本轮
闸门实测的结果**——`5_compile` 的 `playtest_summary.md` 里那条场景是不是绿的。
而闸门跑在**任务循环之后**:你排的任何一张卡,执行的时候本轮的闸门产物**都还不存在**。

所以一张"关闭待办"的卡只有两种下场,两种都是坏的:

1. 诚实的那种——它读不到闸门产物,于是保持 OPEN,什么也没做。白花一轮
   plan/implement/review,而本轮"关掉某某待办"的验收标准照样没达成。
2. 不诚实的那种——它拿"场景 yaml 文件存在""交付说明里的自测表"当证据写下 CLOSED。
   实测过一次(jinyong-hud,2026-08-26):写下 CLOSED,复核时发现盘上的闸门报告
   是**别的轮次**的,又改回 OPEN,来回两轮全是空转。

**关闭由 `5_design` 做。** 那一步跑在全部闸门之后、终审之前,手上有本轮真实的
`playtest_summary.md`,它的职责里写明了要按证据关闭待办。你不需要为它排卡,
它不在任务循环里。

你**该**排的是这两种:

- **记录卡**:把本轮做了什么追加进 `40_ux_backlog.md` 的记录区和交付说明,
  相关行**保持 OPEN**,备注写"修复已落,闸门证据待验"。这个不需要闸门结果,
  在循环里做得了。
- **场景卡**:为每一条待办加一条 playtest 断言。**这才是关闭的前提** ——
  没有场景,`5_design` 到时候也没有证据可引。

一句话:**你负责把证据造出来,不负责宣布结论。**
