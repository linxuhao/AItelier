## 行为测试契约 `playtest_spec.yaml`（你负责"断言阈值"部分）

架构师已在 `playtest_spec.yaml` 定义了可观测面（`scene`/`actions`/`surface`）与场景剧本骨架（`scenarios[].timeline`）。你的职责：**为每个 `scenario` 的时间线填入可量化的验收断言**，把用户故事的验收标准落成客观、可自动判定的行为检查（TDD 式）——这就是运行时 playtest 闸门的"预期"。

- 断言写在某帧的 `assert:` **映射**里，每条 `节点.属性路径: 期望`：
  - **键** = `节点名.属性路径`。节点名取**第一个点之前**（场景子路径用 `/`，如 `HUD/PausedLabel.visible` → 节点 `HUD/PausedLabel`、属性 `visible`）；节点名须来自架构师 `surface`。
  - **值是标量**（`true`/`false`/数字）→ 相等断言（`GameManager.paused: true` 即 `paused == true`；`GameManager.state: 0` 即 `state == 0`）。
  - **值是含比较运算符的字符串**（`==`/`!=`/`<`/`>`/`and`/`or`…）→ 原样作为 **GDScript 表达式**，以该节点为 `self` 求值（`Bird.velocity.y: "velocity.y != 0"`、`GameManager.score: "score >= 1"`）。
  - 运行时以 `Expression` 对**活节点**求值，得 `true` 即该断言通过。
- **每个关键可玩性 → 至少一条断言**：把用户故事里"按了会怎样 / 达成条件是什么"翻成断言。时间线用 `press` 触发输入、`at` 指定检查帧（先按键、隔几帧再断言，给物理留反应时间）。
- 挑**可观测、稳定、不脆**的量：速度符号（`velocity.y < 0`）、分数下界（`score >= 1`）、位置区间——避免依赖精确浮点相等或某一具体帧的脆值。
- 断言只能引用架构师 `surface` 声明的节点/变量。若需要新的可观测量，**回写让架构师补进 `surface`**（并要求实现暴露该脚本变量），而不是去断言私有内部状态。
- **门槛语义（重要）**：行为断言是**"建议性强信号"**——断言失败**不会硬性阻断构建**（只有崩溃 / 主场景跑不起来才硬失败并回环重做）；但验收者会重点参考它判断"玩起来对不对"。所以断言要真实反映玩法正确性，宁缺毋滥、勿脆。

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
