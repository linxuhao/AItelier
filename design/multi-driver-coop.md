# 多 driver 协作（multi-driver coop）—— 同一个 State project 由多个 director 共同驱动

Status: **approved for implementation**（所有者 2026-10-09 16:11 批准）。实施按第 10 节 P0–P5 进行，从 P0 开始；对应的实现节点在 `aitelier` State DAG 中。
进度：**P0 已合并（main 6790f14446，2026-10-09）并于 2026-10-09 在生产启用**（`AITELIER_DRIVER_IDENTITY=on`；已登记 `owner-cli`、`public`、`grok`、`codex`）；新 LAN driver 自助领取 token：`scripts/driver_token.py self-register`（见 `docs/driver-identity.md`）。P1–P5 未开始。
Date: 2026-10-09（初稿）；同日按所有者裁决 D1–D12 及全部待决问题的答复多次修订，16:11 批准实施，见第 0 节。
Scope: `core/state_*`、`core/director_messaging*.py`、`api/state_*`、`api/mcp_router.py`、`api/authz.py`、
`.codex/hooks/postcompact_driver_state.py`、driver guide（`core/state_driver_guide.py`、`docs/state-agent-driver.md`）。

> 本文是仓库里的设计文档（未迁移成 State design item）。按 `docs/state-agent-driver.md`「Synchronize design」
> 的规则，如果以后把它迁移成 `create_design_revision`，那份 State design revision 就成为唯一可写来源，
> 本文件只保留为历史提案，不再维护同一节规范。

---

## 0. Decisions（2026-10-09，所有者裁决）

| # | 裁决 | 对本文的影响 |
|---|---|---|
| D1 | driver 可以经**公网**连接（公网 MCP 端点 `aitelier.linxuhao.app/mcp`），**不限于** Tailscale。 | 由 D5/D6 细化：公网 = Cloudflare，整体算一个 driver。 |
| D2 | **自助餐（buffet）模式**：不设强制角色，任何成员 driver 都可以 claim 任何 ready 的工作。取消强制的实现/审查分工；review 独立性**最多是 advisory**，可选，**默认关闭**。 | 删除 lead/implement/review 角色（第 7 节重写）；项目成员一律平等；结构性写操作靠 CAS + 通知，不靠角色。`review_independence` 只有 `off`（默认）/`advisory`，没有 `required`。 |
| D3 | 租约 = **2 小时**，可续租；**每个 driver 负责为自己的 subagent 续租**。 | 4.2/4.3 默认值改为 7200 秒；subagent 不持有凭据，也不单独心跳，由父 driver 代发 `heartbeat`（4.3a）。 |
| D4 | 其余待决问题经由 Grok Bot 提交给所有者。 | 第 11 节改写为"一问一默认值"的形式。 |
| D5 | 公网访问**只经 Cloudflare**；所有非公网访问都走 **Tailscale 局域网（LAN）**。 | 两种入口，按 `api/authz.py:is_via_cloudflare` 区分（3.2）。 |
| D6 | 现在的 admin token 是单用户的，改为**多个 admin/driver token**。**所有经 Cloudflare 进来的公网流量算作一个 driver**（`public`），现有 `AITELIER_MCP_EXTERNAL_TOKEN` 不变；**每个 LAN driver 用自己的 token**。 | 取消初稿的"每个 driver 一个 CF service token"，`cf_access` 的 email/`common_name` 问题不再是 driver 身份的阻塞项（3.2）。 |
| D7 | **State DAG 的项目/director inbox 与新的 per-driver inbox 分开**。新增 **per-driver 私有笔记**，与共享 driver notebook 并存：共享笔记 = 所有 driver 都遵守的规则；私有笔记 = 每个 driver 自己的进度、草稿、待办。**已确认**（16:00 补充）：**只有该 driver 自己能写，其他 driver 只读。** | 第 5 节重写：项目 inbox 保持 v2；新增 driver inbox（5.2）和私有笔记（5.3，实现方式见 D10）。 |
| D8 | attempt 被回收时，原 driver 的 **subagent 由回收方接管**。 | 新增 4.6：subagent 登记、接管流程，以及接管方观察不到原 subagent 时（例如另一台机器上的本地进程）怎么办。 |
| D9 | driver 是数据库里**一等、持久的实体**：一张**全局** `drivers` 表（身份、token 哈希、类型 LAN / public-CF、显示名、状态、创建时间、最后活跃时间），**不是**某个 State DAG 的临时 operator；项目成员关系是**单独的关联表**。 | 第 3 节按此重构；表名 `drivers` / `project_drivers`，不挂在任何 State project 之下。 |
| D10 | 私有笔记**复用**项目 driver notebook 的设计和代码（entry 形式的 notebook），采用**目录模式**：每个 driver 一本，notebook 目录按 driver 索引。沿用现有 `write_driver_note_entry` / `supersede` / `delist` / `get` / `search` 那一套，只是作用域换成 driver；**本人写、其他人只读**。 | 5.3 重写；取消上一版单独设计的 `driver_private_notes` 表和 `write_private_note` 等动作。 |
| D11 | 所有 LAN driver **共用一个 Linux 账号**（`linxuhaserver` 上的 `linxuhao`），经 Tailscale SSH 登录后访问 `127.0.0.1:4444`（SSH 隧道或远程执行），不用 `tailscale serve`。**driver 身份隔离靠协作自觉，不由操作系统强制。** 仍建议每个 driver 一个 0600 的 token 文件，以保持整洁、避免误用别人的 token。 | 3.2 改写，写明**接受的风险**（3.2a）；Q4 因此解决；取消"每个 driver 一个 Linux 用户"的建议。 |
| D12 | 项目 inbox 的投递和 ack 有两种模式：**`at_least_n`**（N 个不同的 driver ack 后，消息即视为处理完毕；默认 N=1，用于"有人接一下"）和 **`broadcast`**（每个成员 driver 都必须各自 ack；用于规则变更和通知）。这同时解决了共享 ack 的可见性问题。 | 5.1a 设计：逐个收件人的 ack 记录、完成规则、API 参数、与 v2 transient/standing 的关系；现有消息迁移为 `at_least_n`、N=1。 |
| 16:11 批准 | 剩余 12 个待决问题（Q2、Q3、Q7、Q8、Q9、Q11–Q17）**全部按推荐默认值批准**；本文转为 **approved for implementation**。 | 第 11 节改为"已裁决"表。 |
| Q1 答复 | 所有者用浏览器经 Cloudflare Access 做的操作**算所有者本人**，不算 `public`。**已确认。** | 3.2 表格。 |
| Q5 答复 | `public` = 任何经 Cloudflare、带 external token 的调用方（云端 agent、手机上的 agent、不在 Tailscale 上的 agent）。**维持默认：`public` 默认不是任何项目的成员。** | 3.4、12.3。 |
| Q6 答复 | 共享项目 inbox 里的 ack = "**我来处理**"，记录 ack 的 driver。**已确认。** | 5.1。 |

由此被取消的早期内容：Tailscale-only 的凭据限制；每个 driver 一个 Cloudflare Access service token；`lead`/`implement`/`review` 三种角色及基于角色的权限；
`target_role` 寻址；`review_not_independent` 拒绝；`role_required` 错误码；30 分钟租期；项目 inbox 按 driver 投递（v3 重建 delivery 表）。

## 1. 起因：要解决什么问题

所有者计划让两个 driver 一起驱动高度自动化的 `wuxia-myth` 和 `aitelier` 两个项目：

- **Codex driver**：运行在所有者的 MacBook Air 上（Codex CLI / Codex 子代理）；
- **Grok Bot**：运行在另一台机器上，经 Tailscale（LAN）访问 `linxuhaserver`。

现在 State DAG 默认一个项目只有一个 director。真让第二个 driver 进来，会碰到下面几个具体问题，每一条都能在代码或现有数据里找到依据。

### 1.1 身份塌缩：所有非 Cloudflare 写入方都是同一个 actor

- `api/state_graph_routers.py:authenticated_actor`（第 12–18 行）：只要请求没带 Cloudflare Access 邮箱，actor 一律是字符串
  `"authorized-state-operator"`。`X-AItelier-Admin-Token`（`api/authz.py:108`）和 MCP external token
  （`api/mcp_router.py:153 _EXTERNAL_TOKEN`、`_authorize` 第 171 行）都会走到这一支。
- 线上数据（2026-10-09 只读查询 `~/.AItelier/aitelier.db`）：`state_attempts` 里全部 **2026** 条 external attempt 的
  `reporting_actor` 都是 `authorized-state-operator`（另有 529 条 SkillFlow attempt 为 NULL）。
  `state_director_messages.director_identity` 里至少出现过 20 种自报身份（`codex-director`、`claude-director`、
  `dot-director`、`chatgpt-*` …），`state_evidence` 里有 17 种不同的 `director_identity`。
- `docs/state-graph.md`「Trust boundary for evidence」和 `docs/state-external-harness.md:277-282` 都写明：
  `director_identity` 只是自报的出处，**不是授权依据**；"一个共享 bearer token 就代表一个调用方"。

后果：

1. `core/state_external.py:ExternalAttempts.observe` 第 87–88 行用 `owner['reporting_actor'] != self.actor`
   判断"是不是这个 attempt 的报告者"。两个 driver 共用 admin token 时，这个检查等于没有：Grok 能替 Codex 的
   attempt 报 `failed`/`candidate`，反过来也一样，而且审计里分不出是谁报的。
2. 反过来，如果给其中一个 driver 换成独立凭据（比如 CF 邮箱），它又**完全无法**接手另一个 driver 遗留的 attempt：
   observe 要求 actor 完全相等，而系统里没有任何转移所有权的操作。
3. "独立 review"在结构上无法表达：`record_evidence` 的 `reviewer` 来自 transport actor，两个 driver 写出来都是同一个值。

### 1.2 Director inbox 按项目寻址，不按 driver 寻址，并且拒收同项目消息

`core/director_messaging.py:SQLiteDirectorMessaging.send_director_message`：

- 第 229–235 行：定向消息若 `target_project_id == sender_project_id`，直接 `_invalid()`，返回笼统的
  `invalid_request`（`core/director_messaging_protocol.py` 的 `ERROR_CODES` 里没有更具体的错误码）；
- 第 254–256 行：broadcast 显式排除发送方自己的项目；
- 第 262–270 行：回复要求被引用的 delivery 的 `target_project_id == sender_project_id`，所以只能跨项目往返；
- `state_director_deliveries` 的唯一键是 `(message_id, target_project_id)`，状态（`unread → acknowledged → resolved`，
  见 `_transition` 第 392–428 行）**每个项目只有一份**。同一项目里的两个 driver 共用一个收件状态，谁先 ack，
  另一个就再也看不到"未读"。

绕过办法已经出现过：10-06 创建了 `novel-lingwu-deputy`（"灵舞 · 副导演收件箱(只收 director message,不建节点)"），
开一个空 State project 专门当邮箱。它没有节点，也没有 delivery，等于用项目命名空间冒充 driver 身份。
所有者明确不想要这种 hack。

现有消息量（只读统计）：`aitelier → wuxia-myth` transient 共 61 条（54 acknowledged / 7 resolved），
`wuxia-myth → aitelier` 36 条（14 条仍 unread）。跨项目消息本身运作良好，本设计**不改**跨项目语义。

### 1.3 active attempt 没有租约，遗留的 attempt 会永远占住节点

- `core/state_attempts.py` 第 21 行 `ACTIVE = ("reserved","launching","running","paused","unknown")`，以及第 36–37 行
  部分唯一索引 `state_attempts_one_active`：每个节点只能有一个 active attempt。
- `docs/state-external-harness.md:128-133`："A timeout or lost callback should be `unknown`; it keeps the active slot.
  **No expiry** automatically assumes the workers stopped. The harness must resolve and report that outcome."
- 能释放这个槽位的只有：原 reporting_actor 带着原 `context_hash` 和预期的 observation version 报一个
  `candidate`/`failed`（`observe` 第 80–82 行要求 `quiescent=true`）；或者 `retire_reservation`
  （`state_attempts.py:401`），但它只能退役还没 dispatch 的 `reserved`。

**引发本节的实例**：`activememoryindex-research` / `compression.fact-identity-openai-binding`

| 字段 | 值 |
|---|---|
| attempt | `attempt-4547c5dc58554a008a4a44eac58f7d37` |
| harness / external_id | `codex-mac-q4-owned-cleanup-offline` / `fact-identity-q4-owned-cleanup-repair-20261004-v1` |
| 登记时间 | 2026-10-04T12:03:19Z（巴黎时间 14:03） |
| observation_version | 0（从未上报过一次） |
| `state_external_owners.status` | `active`，`settled_at` 为 NULL |
| 节点上最后一个事件 | seq 11784 `external_attempt_registered`（就是这次登记） |
| driver note 最后一次提到 | 10-04 12:05Z 的 handoff entry `72aaab718d13` |

同一节点之前的三个 attempt 都在 40–75 分钟内上报并 settle。10-05 起，项目工作已经转给另一个 Claude Code 会话
（D3 forget、rc2–rc5 等），这个 attempt 再也没人管。到 2026-10-09 它已占住节点 **5 天**，
节点在 overview 里一直显示 `in_progress`，导致 `wait_for_state_change(return_when_idle=true)` 永远不会返回
`nothing_to_wait`，frontier 也不会把这个节点放出来。系统没有任何信号能说明这个 owner 已经消失，
其他 driver 也没有任何合法操作能接手它。

目前所有项目里 active/paused 的 attempt 一共 8 个（2026-10-09 15:40 巴黎时间）：

- **AMI**：上面那个，自 10-04 起 running。
- **wuxia-myth** 共 6 个：
  - `validation.monthly-pixel-journey`：10-06 起 paused；
  - `coop.reconnect`：10-07 起 paused；
  - 当天新开的 4 个：`release.daily-demo-20261005`、`ui.plan-offer-panel-fits-available-viewport`、
    `harness.scenario-deltas-use-explicit-before-controls`、`harness.playtest-assertions-observe-their-real-operands`。
- **aitelier**：`harness.explicit-before-controls-preserve-causal-deltas`，当天新开，running。

两个 driver 一起工作时，"某个 attempt 的主人还在不在"会从偶发事件变成常态问题。

### 1.4 hold 被挪用为"所有权标记"

`core/state_metadata.py`：`state_node_holds`（第 9–15 行）和 `require_dispatch`（第 64–67 行）。
hold 的语义只是"禁止新 dispatch"（`docs/state-agent-driver.md` 第 9 行："hold prevents new dispatch; it does not cancel workers"）。
但线上 `wuxia-myth` 有 8 个 held 节点，其中 3 个的 reason 以 `Existing external work: ...` 开头，
`aitelier` 的 10 个 held 节点里也有 2 个是这样——hold 被当成了"这个节点有人在做"的标记。
hold 的 `actor` 字段同样全是 `authorized-state-operator`，看不出是哪个 driver 放的 hold，也就不知道该由谁来解。

### 1.5 等待与唤醒：所有消息叫醒所有人

`core/state_changes.py:scan` 第 69 行：只要设置了任何 filter，`director_message_received` 都会**绕过 filter**直接返回。
项目里只有一个 director 时这是对的；两个 driver 共享项目事件流时，每条发给 A 的消息也会叫醒 B，
B 还会去 ack 它（见 1.2），从而把 A 的消息"吃掉"。

### 1.6 已经够用、不需要改的部分

- 所有写操作都已经是 compare-and-swap：`expected_revision`、`expected_priority`、`expected_version`、
  observation version、`request_key` 幂等（`state_attempts._reserve` 第 217–222 行）。
- driver notebook 已改为"只有 entry，每条都是规则"（2026-10-06 owner ruling），同一 revision 竞争只有一个赢家，
  适合多人共享。in-flight 状态不进 notebook。
- 跨项目 director messaging v2（transient / standing、PostCompact 投影 `project_active_standing`）。
- 事件流 + cursor（`state_events.seq`）天然支持多个独立读者。

## 2. 目标与非目标

### 目标

1. **一等 driver 身份（D9）**：driver 是全局、持久的实体，每个 LAN driver 有独立 token，公网整体是一个 `public` driver；actor 由 transport 推导，不由参数自报。
   所有 State 写入（attempt、observation、evidence、hold、priority、note entry、message）都能归属到具体的 driver。
2. **节点 claim + 租约 + 心跳**：driver 先声明"我在做这个节点"，并且必须续租；过期之后有明确、可审计、
   **不假装 worker 已停**的回收流程。AMI 那种遗留 attempt 应当在约 1 小时内变成可处理的事件，而不是 5 天无声占位。
3. **两种 inbox 分开（D7）**：项目 inbox 保持 v2 不变；新增 per-driver inbox 承载 driver 对 driver 的消息和系统通知，每个 driver 各自 ack；
   外加 per-driver 私有笔记（自己写、别人只读）。
8. **subagent 可被接管（D8）**：attempt 被回收时，原 driver 登记的 subagent 一并转给回收方。
4. **交接（handoff）协议**：attempt/claim 的所有权可以原子地转移，并携带冻结的 context、cursor、产物和报告引用。
5. **并发写冲突规则**：明确哪些写操作需要 claim，哪些靠 CAS 就够。
6. **自助餐模式（D2）**：项目成员一律平等，任何成员都可以 claim 任何 ready 的节点；review 独立性只做可选的 advisory 标注，默认关闭。
7. 单 driver 项目的行为**完全不变**（项目级开关，默认关闭）。

### 非目标

- 不做远程进程探测：State 仍然不检查 Mac 上或其他机器上的 worker 是否存活（与 `state_external.py` 文件头注释一致）。
- 不做密码学意义上的独立审查（PKI、阈值签名）。两个 driver 都归所有者控制，"独立"只是结构上的约束。
- 不自动接受、不自动 verify、不自动重派；租约过期只产生事件，并开放一个**需要显式调用**的回收动作。
- 不做跨项目的依赖或 claim；不改跨项目消息语义。
- 不引入新的调度器：不新增自动派发 attempt 的 poller。
- 不改 SkillFlow 引擎的 claim/reaper（那一层已经有 `release_claim`、30 秒 supervisor）。
- 不做强制角色、不做任务指派器（D2）：谁做什么由 driver 自己 claim，系统只保证不会两人同时做同一件事。

## 3. Driver：全局、持久的一等实体（D5、D6、D9）

### 3.1 数据模型

driver 不属于任何 State project，所以表名不带 `state_` 前缀。它们放在同一个 `aitelier.db` 里，
由新模块 `core/drivers.py`（唯一写入方）维护。

```sql
CREATE TABLE IF NOT EXISTS drivers (
    driver_id     TEXT PRIMARY KEY,              -- 稳定 key：'codex-macbook-air'、'grok-bot'、'owner-cli'、'public'
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK(kind IN ('lan','public_cf')),
    is_admin      INTEGER NOT NULL DEFAULT 0 CHECK(is_admin IN (0,1)),  -- admin token = is_admin 的 LAN driver（D6）
    token_hash    TEXT,                          -- lan：HMAC-SHA256(pepper, token)；public_cf：NULL（用现有 external token）
    token_rotated_at TEXT,
    host_label    TEXT NOT NULL DEFAULT '',      -- 仅作描述，如 'macbook-air'，非授权依据
    capabilities_json TEXT NOT NULL DEFAULT '{}',-- 见 3.3
    status        TEXT NOT NULL CHECK(status IN ('active','suspended','retired')),
    revision      INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT,                          -- 每次认证成功时节流更新（最多每 60 秒写一次）
    CHECK((kind='lan' AND token_hash IS NOT NULL) OR (kind='public_cf' AND token_hash IS NULL AND is_admin=0))
);
CREATE UNIQUE INDEX IF NOT EXISTS drivers_token ON drivers(token_hash) WHERE token_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS drivers_one_public ON drivers(kind) WHERE kind='public_cf';  -- 公网只有一个 driver

CREATE TABLE IF NOT EXISTS project_drivers (     -- 项目成员关系，单独的关联表；没有角色（D2）
    project_id TEXT NOT NULL, driver_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('member','removed')),
    revision INTEGER NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, driver_id),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id),
    FOREIGN KEY(driver_id) REFERENCES drivers(driver_id)
);
CREATE TABLE IF NOT EXISTS driver_audit (        -- append-only，触发器禁止 UPDATE/DELETE
    seq INTEGER PRIMARY KEY AUTOINCREMENT, driver_id TEXT NOT NULL,
    operation TEXT NOT NULL,                     -- register / rotate / suspend / retire / set_admin / membership
    payload_json TEXT NOT NULL,                  -- 永不含 token 明文或哈希
    actor TEXT NOT NULL, created_at TEXT NOT NULL
);
```

- **token 明文不入库**，只存 `HMAC-SHA256(pepper, token)`。pepper 是 `~/.aitelier-secrets/` 里的一个 secret 文件，
  和 LLM key 的处理方式一样（AGENTS.md「API-key secret」）。token 只在 `register_driver` / `rotate_driver_token` 的响应里出现一次，
  driver 端存进自己机器上 chmod 600 的文件，不写进笔记、消息、报告或日志。
- `last_seen_at` 只是诊断字段，**不**参与租约判断（租约以 4 节的心跳为准），这样也不会因为 driver 只是在读而让它的租约续命。
- 初始数据：
  - `owner-cli`：`kind=lan`、`is_admin=1`。迁移时把现有 `AITELIER_ADMIN_TOKEN` 的哈希写进来，所有者的 CLI 和脚本不用改。
  - `public`：`kind=public_cf`，代表所有 Cloudflare 流量。
  - `codex-macbook-air`、`grok-bot`：`kind=lan`，各自一个新 token。

### 3.2 认证与 actor 推导

两种入口（D5），按现有的 `api/authz.py:is_via_cloudflare`（`Cf-Ray` 或 `Cf-Access-Jwt-Assertion`）区分：

| 入口 | 凭据 | 认成谁 |
|---|---|---|
| **公网（经 Cloudflare）** | MCP：现有 `AITELIER_MCP_EXTERNAL_TOKEN`，不变（D6）。REST：见 Q2。 | 一律是 `driver:public` |
| **公网（经 Cloudflare），带所有者的 Access 邮箱** | 现有的 CF Access JWT（`core/cf_access.py`），邮箱 ∈ `AITELIER_WRITERS` | **所有者本人**（`owner:<email>`，有 admin 权限），**不是** `public`（Q1，已确认） |
| **LAN（Tailscale 上 SSH 到服务器后访问 127.0.0.1:4444）** | `X-AItelier-Driver-Token`（原 `X-AItelier-Admin-Token` 作为别名保留），**只在非 Cloudflare 请求上有效**（与现有 admin token 防重放规则相同） | `driver:<driver_id>`；`is_admin=1` 的 driver 额外拥有 admin 权限 |

具体改动：

- `api/authz.py`：`ADMIN_TOKEN` 从"一个环境变量字符串比较"（第 19 行、第 108–109 行）改为"查 `drivers` 表里的 token 哈希"。
  `write_denial_reason` 的判断顺序不变：经 Cloudflare 的请求一律不认 LAN token。
- `api/mcp_router.py:_authorize`：tunnel 请求在 external token 通过后，**把身份定为 `driver:public`**，而不再是无名的
  `authorized-state-operator`。这是唯一需要改的公网逻辑。
- `api/state_graph_routers.py:authenticated_actor`（第 12–18 行）：改为返回 `driver:<id>`、`owner:<email>` 之一；
  不再有兜底的 `authorized-state-operator`。历史数据里的这个字符串原样保留（9.2）。
- **cf_access 的 email/`common_name` 问题**：D6 之后 driver 不再用 CF service token，所以不需要为 driver 身份解析 `common_name`；
  `cf_access.email_from_request_headers` 只服务于所有者的浏览器会话，保持现状。只有将来要在公网区分多个 driver 时，才需要回头处理（见 Q3）。
- 其他用到 admin token 的调用方要一起改成"查表"或改用别名请求头：`api/admin_routers.py`（第 50 行附近）、`api/project_routers.py`、
  `cli/client.py`、`scripts/mcp_call.py`、`scripts/state_facets_migrate.py`、`.codex/hooks/postcompact_driver_state.py`、
  `aitelier/tools/run_tests/impl.py`、`integrations/dsh/cordis.patch.yml`，另有约 30 个测试文件（按仓库及其 worktree 副本 grep 统计）。
  `api/state_only.py:_BearerAuth` 的独立 State 服务器同样只有单 token，要改成查同一张表。
- **LAN 可达性（D11，已确认）**：`docker-compose.yml` 第 143–144 行把 4444 只发布在 `127.0.0.1`，**保持不变**，也不用 `tailscale serve`。
  LAN driver 经 Tailscale **SSH 到 `linxuhaserver`**，再访问 `http://127.0.0.1:4444`，有两种方式：
  - SSH 端口转发：`ssh -N -L 14444:127.0.0.1:4444 linxuhao@linxuhaserver`，本地连 `127.0.0.1:14444`；
  - 远程执行：`ssh linxuhao@linxuhaserver 'curl … http://127.0.0.1:4444/…'`（Grok Bot 目前就是这样）。

  这样过来的请求不带 `Cf-Ray`，走 LAN 分支，靠请求头里的 driver token 识别身份。由于经过 docker 端口映射，来源 IP 都一样，**不能**用 IP 区分 driver。
  Codex 的 MCP 客户端需要一条常驻的 SSH 隧道。隧道断了，MCP 就断了，但租约是 2 小时，短时断线没有影响。
- **身份隔离是协作式的（D11）**，见 3.2a。

- MCP 和 REST 用同一个推导函数，不允许 transport 有私有路径。
- `director_identity`：对已注册的 driver，必须等于 `driver_id` 或以 `driver_id/` 开头。subagent 用子标签，例如 `codex-macbook-air/sub-3`、
  `public/chatgpt-web`。不提供则自动填 `driver_id`。subagent **不持有自己的凭据**，它的写操作经由父 driver 的凭据发出（D3）。
  对 `public` 来说，子标签是区分公网各个 agent 的唯一线索，但它只是出处，**不是**身份（12.3 风险 2）。
- **break-glass**：`is_admin=1` 的 driver（默认只有 `owner-cli`）和所有者的 Access 邮箱可以越过 claim 规则，每次写入都在事件里标
  `break_glass=true`，并通知受影响的 owner。

### 3.2a 共用 Linux 账号：协作式身份隔离与接受的风险（D11）

所有 LAN driver 都以 `linxuhao` 身份登录 `linxuhaserver`。所以 driver token 的作用是**归属**（谁做了什么、谁持有哪个 claim），
**不是**隔离边界。操作系统不会阻止一个 driver 使用另一个 driver 的身份。

**约定（driver guide 写明，靠自觉遵守）**：
- 每个 driver 的 token 放在自己的文件里：`~/.aitelier-drivers/<driver_id>.token`，目录权限 0700，文件权限 0600。
  在共用账号下，这挡不住其他 driver 读取，作用是**避免误用**：脚本只按自己的 `driver_id` 拼路径，不去扫描整个目录，
  也不依赖某个共享的环境变量。token 不写进 `.env`、笔记、消息、报告或日志。
- driver 只读自己的 token 文件，不读其他 driver 的，也不读 `~/AItelier/.env` 里的 admin token。admin 操作只能由所有者
  通过 `owner-cli` 执行。
- 所有写入都走 4444 的 API，**不**直接写 `~/.AItelier/aitelier.db`，也不 `docker exec` 进容器改状态（AGENTS.md 本来就要求经由 API）。
- 每个 driver 用自己的工作目录和分支（"一 checkout 一 writer"、4.6）。不动其他 driver 的进程、tmux 会话和临时文件。
- 收到别的 driver 的 token、或者发现自己拿错了 token，应立即停止，并通知所有者轮换（`rotate_driver_token`）。

**接受的风险（所有者已知晓并接受）**：
1. **冒用**：任何 LAN driver 在技术上都能读到其他 driver 的 token 和 admin token，以其他身份写入，越过 claim、fence 和"本人才能写"的私有笔记规则。
   系统**无法区分**"driver A 自己写的"和"driver B 用 A 的 token 写的"。
2. **绕过 API**：任何 LAN driver 都能直接写数据库或进容器，此时不留下任何 driver 归属，也不受 CAS 和触发器以外的任何检查。
3. **误删、误杀**：共用账号下，driver 可以结束别人的进程、改别人的工作目录。D8 里"回收方能否控制 subagent"在同一台机器上
   **总是**技术上可行；是否这样做，由协议决定，不由权限决定。
4. **审计的含义**：`driver_audit`、事件里的 `driver:<id>` 和 `director_identity` 只表示"**以哪个 token 写入**"，不能证明真实来源。
   出现争议时，以所有者的裁决为准。
5. **token 泄露的范围**：任一 driver 的机器或会话被攻破，等于整台服务器上的 `linxuhao` 被攻破，所有 driver token 和 admin token 都要轮换。

**为什么仍然值得做 per-driver token**：driver 都是合作的 agent，主要风险是**误用**而不是恶意。per-driver token 足以消除 1.1 的身份塌缩，
让 claim、fence、inbox、私有笔记的归属正确，这是本设计要解决的问题。如果将来接入不完全信任的 driver，再改成每个 driver 一个 Linux 用户
（不在 docker 组，`authorized_keys` 限制 `permitopen="127.0.0.1:4444"`）。数据模型不需要改。

### 3.3 capabilities（driver 声明自己能做什么）

`capabilities_json` 是声明式的能力清单，**不是授权**（D2 之后也没有角色授权）。它的用途是：在 driver 自选 review 工作、
挑选 handoff 接收方、判断能否观察某个 subagent（4.6）时做匹配，并在 `project_overview` 里给其他 driver 看。

```json
{
  "harnesses": ["codex-cli", "codex-subagent"],        // 可执行的 external harness 族
  "workflows": true,                                   // 能否 start_attempt（SkillFlow）并控制 checkpoint
  "observable_hosts": ["linxuhaserver"],               // 能观察和控制哪些主机上的进程（D8 接管时用）
  "can_reach_checkout": ["linxuhaserver:/home/linxuhao/.AItelier/projects/wuxia-commercial-batch-next"],
  "models": ["gpt-6.1-sol"],                           // 仅描述，供"指定评委"类规则匹配
  "max_wait_seconds": 300                              // 例：Codex MCP 300 秒截断（docs/state-agent-driver.md 第 20 行）
}
```

### 3.4 注册流程

1. 所有者（`owner-cli` 或 Access 邮箱）调用 `register_driver(driver_id, display_name, kind='lan', host_label, capabilities)`，
   响应里一次性返回 token。`public` 由迁移自动创建，不能注册第二个。
2. 所有者调用 `set_project_driver(project_id, driver_id, status='member', expected_revision, reason)`。没有角色（D2）。
   `public` = 任何经 Cloudflare、带 external token 的调用方（云端 agent、手机 agent、不在 Tailscale 上的 agent）；**默认不是任何项目的成员**（Q5，已维持），需要时由所有者临时加入。
3. driver 启动时调用 `whoami`，确认自己的 `driver_id`、所属项目、`is_admin`，然后才开始等待或 dispatch。
4. `rotate_driver_token`、`suspend_driver`、`retire_driver`、`set_driver_admin` 每次都写一行 `driver_audit`。
   `suspend_driver` 会让该 driver 名下所有 claim 的租约立即到期，但**不会**把 attempt 改成终止状态（4.4）。
   retired 的 driver 永不删除：历史 attempt、证据和笔记都通过外键指向它。

## 4. Node claim、租约、心跳与遗留 attempt 的回收

### 4.1 概念

- **claim**：driver 对一个节点声明"我正在处理它"，带上用途（`purpose`）：
  - `implement`：准备或正在执行 attempt；
  - `review`：在审一个 CANDIDATE；
  - `investigate`：只读调查，不阻止别人 review，但阻止别人 dispatch；
  - `plan`：准备 revise/split，阻止别人 dispatch 和改结构。
- **lease**：claim 和 active attempt 都带 `lease_expires_at`。持有者用 `heartbeat` 续租。
- **fence**：每个 claim 或 attempt 的所有权有一个单调递增的 `fence` 整数。所有权一变（handoff、回收），fence 就加 1。
  此后带旧 fence 的写入（observe、evidence、hold 解除等）一律拒绝，错误码 `stale_fence`。
  这是标准的 fencing token 做法，用来防止"以为自己还是 owner"的旧 driver 在交接后继续写。

### 4.2 数据模型

```sql
CREATE TABLE IF NOT EXISTS state_node_claims (
    claim_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('implement','review','investigate','plan')),
    status TEXT NOT NULL CHECK(status IN ('live','released','expired','transferred','revoked')),
    fence INTEGER NOT NULL,
    node_revision INTEGER NOT NULL,
    attempt_id TEXT,                       -- implement claim 在 dispatch 后绑定
    workspace TEXT NOT NULL DEFAULT '',    -- 声明的 checkout / worktree（host:path#branch），用于"一 checkout 一 writer"
    lease_seconds INTEGER NOT NULL CHECK(lease_seconds BETWEEN 60 AND 86400),  -- 默认 7200（D3）
    lease_expires_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
    request_key TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, node_key, driver_id, request_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key),
    FOREIGN KEY(driver_id) REFERENCES drivers(driver_id)
);
-- 每个节点最多一个 live 的 implement/plan claim（排他）；review/investigate 可并存
CREATE UNIQUE INDEX IF NOT EXISTS state_node_claims_one_exclusive
ON state_node_claims(project_id,node_key) WHERE status='live' AND purpose IN ('implement','plan');

ALTER TABLE state_attempts ADD COLUMN owner_driver_id TEXT;        -- NULL = legacy
ALTER TABLE state_attempts ADD COLUMN owner_fence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE state_attempts ADD COLUMN lease_expires_at TEXT;       -- NULL = legacy、无租约
ALTER TABLE state_attempts ADD COLUMN last_heartbeat_at TEXT;
```

心跳**不写** `state_events`：心跳次数 × N 个 attempt 会把事件流冲成噪音，
这和 scheduler tick log 合并空闲行的理由相同（AGENTS.md「Scheduler tick log」）。
心跳只更新 `last_heartbeat_at` / `lease_expires_at` 两列，不提升 `observation_version`，因此也不会和 observe 的 CAS 冲突。
只有**状态变化**才产生事件：`claim_acquired`、`claim_released`、`lease_expired`、`claim_transferred`、
`attempt_abandoned`、`attempt_ownership_transferred`。

### 4.3 生命周期

```text
claim_node(purpose=implement) ──► live ──heartbeat──► live ...
        │                          │
        │ start_external_attempt / start_attempt（必须持有 live implement claim，且 fence 一致）
        ▼                          │
   attempt.owner_driver_id = claim.driver_id，attempt.lease 跟随 claim
        │
        ├─ report candidate/failed（quiescent=true）──► claim 自动 released
        ├─ release_claim（attempt 仍 active 时拒绝；先交接或回收）
        ├─ offer/accept_handoff ──► transferred，新 owner fence+1
        └─ lease 超时 ──► 事件 lease_expired（attempt 仍是 ACTIVE，不改状态）
                              │  + grace（默认 = 1 个租期）
                              ▼
                     reclaimable：任意项目成员可以调用
                     abandon_external_attempt / take_over_attempt（见 4.4）
```

默认值（D3；可按项目在 `state_project_policy` 里覆盖）：

- **所有 claim 和 active attempt：租期 2 小时（7200 秒），可无限续租。** 建议每 20–30 分钟续一次，最晚在剩余 30 分钟前续。
  过期后有一段 grace（建议默认 15 分钟，见 Q7），grace 结束才进入 reclaimable。
- SkillFlow attempt：租约约束的是 **controller**，即负责回答 checkpoint、调用 reconcile 的 driver，**不是** run 本身。
  run 的存活由引擎的 `skillflow_active_ops`、`owner_lost_at` 判断，本设计不碰。

#### 4.3a subagent 的续租（D3）

- attempt 和 claim 的 owner 永远是**父 driver**。subagent 只是父 driver 手下的执行者，没有自己的凭据。
  但 subagent 要在 `driver_subagents` 表登记（4.6），这样才能被续租、被交接、被接管（D8）。
- **父 driver 负责为自己名下所有 subagent 续租**（attempt 和 subagent 记录一起续）：一次 `heartbeat(attempt_ids=[...], subagent_ids=[...], fences=[...])`
  可以批量续最多 100 个，所以父 driver 只需要一个循环。推荐把它放在 client 进程里，和 `wait_for_state_change`
  的等待循环放在一起（`docs/state-agent-driver.md`「Keep the wait loop in the tool/client process」），不要每次续租都开一个新的模型回合。
- 父 driver 自己下线、上下文被压缩或会话结束之前：要么把 attempt 交接出去（第 6 节），要么在报告 settle 之后释放。
  做不到时就让租约自然过期。过期的意思是"没人负责了，请其他人处理"，正是本机制要表达的信号。
- 心跳只证明**父 driver 还在负责**，**不**证明 subagent 进程还活着（State 仍然不探测远程进程）。
  父 driver 只应在确实还在监管 subagent 时续租，不能用一个独立脚本无条件续租，否则遗留 attempt 会被永远续命，
  又回到 1.3 的问题。详见 12.2。

### 4.4 回收：不假装 worker 已停

现有不变量（`docs/state-external-harness.md:128-133`、`docs/state-graph.md:149-153`）：超时或丢失回调都不能证明 worker 已经停止，
系统里也**故意不提供** force-reset。本设计保留这一点，只是在"有人负责"和"无人负责"之间加一个显式、可审计的中间态。

新增两个写操作，都要求租约已过期且已过 grace，调用者是**任意项目成员**（D2，不需要角色）：

1. `abandon_external_attempt(attempt_id, expected_owner_fence, quiescence, reason, report_ref?, report_sha256?)`
   - `quiescence ∈ {"attested","unknown"}`：
     - `attested`：调用方证明原 worker 已经静止（例如 Codex 端确认进程已退出、工作树已 settle）。必须附报告的引用和 hash，
       走 `core/state_report_integrity.retain_report` 这条现有路径。
     - `unknown`：坦白"不知道"。
   - 效果：attempt 变为新的**终止状态 `abandoned`**（要扩展 `state_attempts.status` 的 CHECK，并加入
     `state_external_owners.status`）。`abandoned ≠ failed`：它**不能**携带 failed attempt 的 criterion 诊断 evidence，
     也**不**改动节点的 VERIFIED 受理。它释放 `state_attempts_one_active` 槽位，fence 加 1，
     写 `attempt_abandoned` 事件，并向原 owner 的 **driver inbox**（5.2）发一条通知。
   - 原 owner 之后迟到的 observe：仍然**追加记录**到 `state_external_observations`（带 `late_after_abandon=true`），
     不丢弃报告字节，但 `resulting_status` 强制为 `superseded`，不能把节点改成 CANDIDATE。
     原 owner 如果真做完了，要另开一个新 attempt 重新登记同一产物。这和现有"stale inputs → superseded"的处理一致。
   - `quiescence=unknown` 时的额外约束：同一节点的下一个 attempt **必须**提供 `base_sha`，并在 claim 里声明一个和旧 attempt
     不同的 `workspace`，从而避免两个可能还活着的 worker 写同一个 checkout（AGENTS.md："One writer per checkout"）。
     服务端只能核对声明，无法验证远端是否遵守。

2. `take_over_attempt(attempt_id, expected_owner_fence, reason)` —— 即 D8 所说的"回收"
   - 效果：`owner_driver_id` 改为调用者，fence 加 1，`reporting_actor` 改为调用者的 actor，写 `attempt_ownership_transferred` 事件，
     向原 owner 的 driver inbox 发通知。此后原 owner 带旧 fence 的 observe 都被拒（`stale_fence`）。
   - **该 attempt 下登记的所有 subagent 一并转给回收方**，每个 subagent 再按 4.6 的可观察性分类处理。
   - 两条路怎么选：回收方要继续这项工作，就用 `take_over_attempt`（接管 subagent）；不打算继续，就用 `abandon`。

**用 AMI 的例子走一遍**（假设当时已经上线本设计）：

- 10-04 12:03Z：`codex-macbook-air` 登记 attempt-4547…，租期 2 小时。
- 14:03Z（巴黎时间 16:03）：没有心跳，产生 `lease_expired` 事件。所有在等这个项目的 driver（`wait_for_state_change` 的 actionable 事件）立刻被唤醒；
  overview 里这个节点的 readiness 从 `in_progress` 变成新的派生值 `in_progress_lease_expired`。
- 14:18Z（巴黎时间 16:18，按 15 分钟 grace）：grace 结束，进入 reclaimable。
- 另一个 driver（或 Codex 自己在下一个会话里）看到之后，二选一：
  - 确认 Mac 上的清理修复确实没在跑：`abandon_external_attempt(quiescence="attested", report=...)`；
  - 确认不了：`quiescence="unknown"`，下一个 attempt 必须用新的 worktree。
- 节点当天下午就能回到 ready，不会占住 5 天；所有判断都留在审计里。
- 原 handoff entry `72aaab718d13` 里写的"new local cleanup repair admitted"也有了归宿：
  它对应的 attempt 状态会是 `abandoned`，而不是一直 running。

### 4.5 overview 与 wait 的变化

- `project_overview` / `get_node`：每个 active attempt 和 claim 附带 `owner_driver_id`、`lease_expires_at`、
  `lease_state ∈ {healthy, expired, reclaimable, legacy_unleased}`。
- `readiness_counts` 增加 `in_progress_lease_expired`，旧客户端会把它当成未知字符串，因此需要一并更新 driver guide。
- `wait_for_state_change(return_when_idle=true)`：如果作用域内只剩 `reclaimable` 的 attempt，返回 `reason=action_required`，
  并附上这些 attempt 的 ID，**不**返回 `nothing_to_wait`，因为确实有事要做。
- `project_run_summary` 的 `running_external` 增加 `owner_driver_id` 和 `lease_state`。

### 4.6 subagent 登记与接管（D8）

D8 要求回收 attempt 时把原 driver 的 subagent 交给回收方。State 从不启动、检查或终止远程进程（`core/state_external.py` 文件头），
所以"接管 subagent"在这里的含义是：**接管它的记录、上下文、工作目录和续租责任；能控制进程就控制，控制不了就隔离它，并在新的工作目录里续做。**

#### 登记

```sql
CREATE TABLE IF NOT EXISTS driver_subagents (
    subagent_id TEXT PRIMARY KEY,               -- '<driver_id>/<label>'，如 'codex-macbook-air/q4-cleanup'
    owner_driver_id TEXT NOT NULL,              -- 当前负责的 driver；接管时改变
    origin_driver_id TEXT NOT NULL,             -- 最初启动它的 driver；不变
    project_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    host TEXT NOT NULL,                         -- 运行位置，如 'macbook-air'、'linxuhaserver'
    runtime TEXT NOT NULL CHECK(runtime IN ('local_process','server_process','skillflow_run','remote_session')),
    control_handle TEXT NOT NULL DEFAULT '',    -- 非密文的控制线索：tmux 会话名、Codex thread id、run_id；不得含凭据
    workspace TEXT NOT NULL,                    -- host:path#branch；每个 subagent 必须写自己的分支
    context_ref TEXT NOT NULL, context_sha256 TEXT NOT NULL,  -- 下发给它的冻结指令/上下文（走 retain_report）
    checkpoint_ref TEXT, checkpoint_sha256 TEXT,              -- 最近一次可续做的快照：分支 head、暂存文件清单 digest
    status TEXT NOT NULL CHECK(status IN ('active','settled','adopted','orphaned_unobservable','terminated')),
    fence INTEGER NOT NULL, lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(owner_driver_id) REFERENCES drivers(driver_id),
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
```

- `register_subagent`：父 driver 启动 subagent **之前**登记（和 `start_external_attempt` 必须在派 worker 之前调用的规则一致）。
  在 `multi_driver=on` 的项目里，凡是会写 checkout 或产出 evidence 的 subagent 都必须登记，见 Q8。没登记的 subagent 对系统不可见，也就无法被接管。
- `update_subagent_checkpoint`：父 driver 在 subagent 的阶段性节点更新 `checkpoint_*`，这决定了别人接手时能从哪里续做。
- **每个 subagent 写自己的分支**（`workspace` 里的 `#branch`）。这是接管时能够隔离旧进程的前提。

#### 接管流程

`take_over_attempt` 成功后，回收方对每个 `active` 的 subagent 调用 `adopt_subagent(subagent_id, fence, observability, reason)`，
`observability` 三选一：

| 分类 | 例子 | 回收方怎么做 | subagent 状态 |
|---|---|---|---|
| `controllable` | 跑在 `linxuhaserver` 上的 tmux 进程，或 SkillFlow run，回收方能看到也能停 | 直接接手监管：继续让它跑或停掉，从此由回收方续租 | `adopted` |
| `observable_only` | 能看到输出（比如它往服务器写报告），但停不了 | 只观察，等它 settle 后记录结果；回收方续租 | `adopted`（带只读标记） |
| `unobservable` | 原 driver 机器上的本地进程，例如 MacBook Air 上的 Codex 子代理，而回收方是 Grok Bot | **不续租、不假装它停了**：标为 `orphaned_unobservable`；fence 加 1，它之后的所有 State 写入都被拒；回收方从 `checkpoint_ref` 起，在**新的分支和工作目录**里另起一个 subagent 续做；旧分支只作参考，不经检查不得合并 | `orphaned_unobservable` |

能否归入 `controllable` / `observable_only`，由回收方根据自己的 `capabilities.observable_hosts` 与 subagent 的 `host` 是否匹配**自行声明**。
服务端只核对声明，不验证真假。

收尾：

- 对每个 `orphaned_unobservable` 的 subagent，系统都向原 driver 的 driver inbox 发一条 standing 通知：**"你的 subagent X 已被接管；回来后请停掉它，并用 `report_subagent_settled` 报告。"**
- `report_subagent_settled(subagent_id, quiescent=true, report)` 是唯一一个允许旧 fence 调用的写操作，它**只能**把 orphan 关成
  `terminated`，不能写 attempt 或 evidence。
- 在 orphan 被关掉之前，overview 一直显示"有 N 个未确认停止的旧 subagent"，提醒还存在两个 writer 的风险。

**用 AMI 的例子走一遍**：attempt-4547 的 subagent 是 MacBook Air 上的 Codex 子代理。Grok Bot 回收后，它属于 `unobservable`：
- Grok Bot 从 10-04 12:01Z 那次候选的 checkpoint（artifact `57746658…`）起，在服务器上的新分支续做"C1 清理缺陷"修复；
- Codex 下次上线时，从自己的 driver inbox 看到通知，确认 Mac 上没有进程在跑，然后报 settled。

自愿交接（第 6 节）和这里不同：交接时原 driver 还在，所以 6.3 要求要么 subagent 已经 settle，要么接收方能观察到它。
回收则是原 driver 已经失联，只能走上表的分类。

## 5. 两种 inbox 与 per-driver 私有笔记（D7）

D7 把"发给项目的事"和"发给某个 driver 的事"分开。这样还带来一个实现上的好处：**项目 inbox 不用改表结构**。
初稿里给项目 inbox 加按 driver 投递、因此必须重建 `state_director_deliveries` 表的方案作废，P2 的迁移风险随之下降（12.1）。

### 5.1 项目 inbox（State DAG director messages）：保持 v2

- 协议保持 v2 的四个动作、transient / standing 生命周期、幂等规则和 PostCompact 投影。D12 新增的 ack 模式是一个增量，
  见 5.1a；因为 v2 是封闭 schema，增量要以 `aitelier.director-messaging.v3` 发布，v2 请求按默认值继续可用。
- 另有两处小改：
  1. `state_director_messages` 加一列 `sender_driver_id`，记下是哪个 driver 发的；
  2. 同项目定向消息仍然拒绝。省略协议版本或显式 v2 保留封闭 v2 响应及原有 `invalid_request` code / detail.message；显式 v3 返回 `use_driver_inbox`，提示改用 5.2。
     v2 的 schemaTag、code 和 detail.message 均为封闭枚举，不加入新字段或说明文字；改用 driver inbox 的说明放在 API / driver guide 中。
- 多个成员共享同一个项目 inbox：`acknowledge` 的含义明确为"**我（这个 driver）接手处理这条**"，事件里记录是哪个 driver ack 的；
  其他成员仍能在列表里看到它，以及谁接了。这本身就是自助餐模式下的认领（Q6，**已确认**）。按 driver 记录的 ack 见 5.1a（D12）。
- `novel-lingwu-deputy` 这类"只当邮箱"的项目在 driver inbox 上线后归档（`set_dispatch(archive)`），保留全部记录；
  driver guide 里写明：**不要为 driver 身份开新项目**。

### 5.1a 项目 inbox 的 ack 模式（D12）

**两种模式**（发送方选择，作用于该消息的每个目标项目的 delivery）：

| `ack_mode` | 含义 | 何时满足 | 用途 |
|---|---|---|---|
| `at_least_n`（默认，`ack_quorum` 默认 1） | 至少 N 个**不同**的 driver ack | ack 记录数 ≥ N | "有人接一下"：任务、请求、问题 |
| `broadcast` | 目标项目的**每个成员 driver**都要各自 ack | 发送时快照的成员全部 ack（之后被移除的成员不再计入） | 规则变更、通知 |

**命名冲突，务必区分**：v2 已有一个布尔参数 `broadcast`，意思是"**跨项目**群发给所有其他项目"。D12 的 `ack_mode="broadcast"`
是"**项目内**每个 driver 都要 ack"。两者正交：`broadcast=true, ack_mode="broadcast"` 表示发给所有其他项目，并且每个项目里的每个 driver 都要 ack。
文档、错误信息和 driver guide 一律写全称 `ack_mode=broadcast`，避免混淆。

**数据模型**（只加列、加表，不重建现有表）：

```sql
ALTER TABLE state_director_messages ADD COLUMN ack_mode TEXT NOT NULL DEFAULT 'at_least_n'
    CHECK(ack_mode IN ('at_least_n','broadcast'));
ALTER TABLE state_director_messages ADD COLUMN ack_quorum INTEGER NOT NULL DEFAULT 1 CHECK(ack_quorum >= 1);
CREATE TABLE IF NOT EXISTS state_director_delivery_acks (
    delivery_id TEXT NOT NULL,
    driver_id TEXT NOT NULL,                  -- 'owner' 表示所有者本人（CF 邮箱），owner-cli 按普通 driver 计
    required INTEGER NOT NULL CHECK(required IN (0,1)),  -- broadcast：发送时快照的成员为 1
    acked_at TEXT,                            -- NULL = 还没 ack（只有 required=1 的行会出现 NULL）
    ack_event_seq INTEGER,
    removed_at TEXT,                          -- 成员在 ack 前被移出项目：不再计入
    PRIMARY KEY(delivery_id, driver_id),
    FOREIGN KEY(delivery_id) REFERENCES state_director_deliveries(delivery_id)
);
-- 触发器：acked_at 一旦写入就不能再改，也不能删行（与其他审计表一致）
```

**规则**：
- **发送**：
  - `ack_mode=broadcast` 时，事务内按目标项目当时的 `project_drivers`（status=member）为每个成员插入一行 `required=1, acked_at=NULL`。
    `public` 默认不是成员（Q5），因此不在其中。
  - `at_least_n` 时，如果 `ack_quorum` 大于目标项目当前的成员数，拒绝，错误码 `ack_quorum_unreachable`。
  - 跨项目群发（`broadcast=true`）时，对每个目标项目分别检查。
- **ack**：
  - `acknowledge_director_message(delivery_id, expected_version, request_key)` 的含义变为"**调用者这个 driver** ack"。
  - 只有目标项目的成员或所有者可以 ack。同一 driver 重复 ack 视为幂等重放，返回原结果，不增加计数。
  - 每次 ack 都写一行（或填上那行的 `acked_at`）和一条事件，并给 delivery 的 `version` 加 1，沿用 v2 的 CAS。
    并发 ack 时输家得到 `version_conflict`，重读后再 ack 即可，因为每个 driver 只需成功一次。
- **完成规则**：quorum 满足时（`at_least_n` 为 ack 数 ≥ N，`broadcast` 为所有 `required=1` 且未 `removed_at` 的行都已 ack），
  - **transient**：系统在同一事务里把 delivery 从 `unread`/`acknowledged` 置为 **`resolved`**。这就是 D12 说的"处理完毕"，事件记录全部 ack 者。
  - **standing**：quorum 满足后，delivery 置为 `acknowledged`，但**不**自动 `resolved`，仍然留在 PostCompact 投影里。
    原因是 standing 是持续生效的规则，自动 resolved 会让规则在所有人确认后从恢复上下文里消失，这与 v2 standing 的定义冲突。
    它要等任一成员或所有者显式 `resolve_director_message` 才退出。
    这是对 D12 的**解释**：严格按字面，standing 在 quorum 后也应 resolved；如果所有者要字面语义，只改这一条即可。
  - quorum 满足之前，delivery 保持 `unread`（还没人 ack）或 `acknowledged`（有部分人 ack）。
  - 显式的 `resolve_director_message` 仍然可用：任一成员或所有者可以提前关闭，事件里注明 quorum 未满足。
- **可见性（修复共享 ack 问题）**：`list_director_messages` 的每个 item 增加 `ack_mode`、`ack_quorum`、`acks: [{driver_id, acked_at}]`、
  `acked_count`、`pending_drivers`（broadcast 时还没 ack 的成员）和 `acked_by_me`。
  - 新增过滤参数 `needs_my_ack: bool`：broadcast 下"我还没 ack"，或 at_least_n 下"quorum 未满足且我没 ack"。
  - PostCompact 投影：standing 消息照旧；`broadcast` 模式的 transient 消息，在**本 driver 未 ack** 时也进入投影（仍受 8 条、3000 字符上限），
    确保每个 driver 都看到规则变更。`at_least_n` 的 transient 保持 v2 行为，不注入。
- **成员变动**：成员在 ack 前被 `set_project_driver(removed)` 时，填上 `removed_at`，然后重新判断 quorum。
  `at_least_n` 的成员数降到 N 以下时，不自动降低 N，overview 标出 `ack_quorum_unreachable`，由任一成员或所有者显式 resolve。
- **迁移**：现有消息全部为 `ack_mode=at_least_n, ack_quorum=1`。现有 delivery 的 status 和 version **不变**，不回填 ack 行，因为历史 ack 者都是
  `authorized-state-operator`，猜出来的身份就是伪造的出处。已 `acknowledged`/`resolved` 的历史 delivery 视为 quorum 已满足，
  列表里 `acks=[]`，标 `legacy_ack=true`。
- **兼容**：不带新参数的 v2 请求等价于 `at_least_n, N=1`。对 transient 而言，唯一的行为变化是第一个 ack 会直接 resolved，
  而 v2 需要 ack 后再显式 resolve。需要"做完再关"的跟踪放到 State（claim / issue），driver guide 同步说明。

### 5.2 per-driver inbox（新增）

```sql
CREATE TABLE IF NOT EXISTS driver_inbox_messages (
    message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
    sender_driver_id TEXT,                      -- NULL = 系统通知（租约过期、被接管、handoff）
    project_id TEXT,                            -- 可选：消息涉及的项目
    kind TEXT NOT NULL CHECK(kind IN ('note','request','review_request','handoff_offer','handoff_reply',
                                      'lease_notice','takeover_notice','subagent_orphaned')),
    delivery_mode TEXT NOT NULL CHECK(delivery_mode IN ('transient','standing')),
    subject TEXT NOT NULL, body TEXT NOT NULL, refs_json TEXT NOT NULL DEFAULT '{}',
    reply_to_message_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_inbox_deliveries (
    delivery_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, target_driver_id TEXT NOT NULL,
    seq INTEGER NOT NULL,                       -- 按 target_driver_id 单调递增，作为该 driver 的 wait 游标
    status TEXT NOT NULL CHECK(status IN ('unread','acknowledged','resolved')),
    version INTEGER NOT NULL,
    UNIQUE(message_id,target_driver_id), UNIQUE(target_driver_id,seq),
    FOREIGN KEY(target_driver_id) REFERENCES drivers(driver_id)
);
```

- **寻址**：`target_driver_id`（一个 driver）或 `project_members=<project_id>`（该项目除发送者外的全部成员）。
  每个收件人一行 delivery，各自走 `unread → acknowledged → resolved`，复用 `_transition` 的 CAS 写法（`expected_version`）。
  只有收件人本人（或 break-glass）能改它的状态。
- **transient / standing** 语义照搬 v2。standing 进入**该 driver 自己的** PostCompact 投影
  （`.codex/hooks/postcompact_driver_state.py` 增加 driver inbox 来源；上限仍是 8 条、3000 字符）。
- **系统通知**：租约过期、被回收、subagent 变成 orphan、handoff 请求，都由服务端写进相关 driver 的 inbox（`sender_driver_id=NULL`）。
- **refs 校验**：引用的 attempt / node / issue / claim / subagent 必须存在。消息正文仍是不可信输出，不能授予权限，也不能替代 State 记录。
- **等待**：新 read action `wait_for_driver_inbox(after, timeout_seconds, return_when_idle)`，游标是该 driver 的 `seq`。
  另外，`wait_for_state_change` 增加 `include_driver_inbox=true`，可以在一次长等待里同时等项目事件和自己的 inbox。
  这需要两套游标，响应里分别返回 `next_after` 和 `next_inbox_after`。
- **读权限**：私有。只有收件人本人和所有者能读；不进 `PUBLIC_READS`。
- **`public` driver 的 inbox 由所有公网 agent 共用**（D6 的直接后果，见 12.3）。

### 5.3 per-driver 私有笔记（D7 已确认；D10：复用 driver notebook，目录模式）

**做法**：不另起一套私有笔记。现有的 entry 式 driver notebook（`core/state_driver_notes.py` 568 行、`core/state_driver_index.py` 207 行）
今天"一个 State project 一本"。把它泛化为"**一个 notebook 作用域一本**"，作用域有两种：

| 作用域 | 地址 | 写 | 读 | 能写什么 |
|---|---|---|---|---|
| `project:<project_id>`（现有，不变） | `note://<project_id>/<12 hex>` | 任意项目成员 | 项目开放后公开（现有 `PUBLIC_READS` 规则） | **只能写规则**：`force=in_force`，informational 被拒（2026-10-06 ruling） |
| `driver:<driver_id>`（新增，私有） | `dnote://<driver_id>/<12 hex>` | **只有该 driver 本人**（以及 break-glass） | 所有已注册 driver 和所有者，**只读**；**永不公开** | 进度、草稿、待办：**允许 `force=informational`**（默认值就是它），也可以写 `in_force`，表示这个 driver 给自己定的规则 |

**目录模式**：driver notebook 的"目录"就是 `drivers` 表，每个 driver 恰好一本，不需要创建。
新增一个读动作 `list_driver_notebooks()`，返回每个 driver 的 notebook 地址、条目数（listed / superseded / delisted）和最后写入时间，作为浏览入口。

**存储**：
- 复用 `ENTRY_SCHEMA` 的列定义和两个触发器（assertion / body 不可改，禁止删除），另建一张同形状的表 `driver_note_entries`，主键是 `(driver_id, entry_id)`，外键指向 `drivers`。
- 不把私有条目塞进 `state_driver_note_entries`：那张表的外键指向 `state_projects`，混放会破坏项目 notebook 的公开读规则。
- 在 `state_driver_index.py` 里把建表 SQL 改成按"表名 + 作用域列"生成的模板，两张表共用一份定义，以后改结构也只改一处。

**代码复用**：
- 把 `StateDriverNotes` 里写死的 `project_id` 换成一个 `NotebookScope`，包括：作用域类型、key、表名、地址前缀、写权限检查、force 策略、读权限检查。
- `write_entry` / `supersede_entry` / `delist_entry` / `get_entry` / `entry_index` / `search_entries` / `check_index`，以及 `_redact`、
  `_unresolved`（地址必须能解析）、`index_line`、excerpt 和游标逻辑**全部原样复用**。
- 动作名**不变**：`write_driver_note_entry`、`supersede_driver_note_entry`、`delist_driver_note_entry`、`get_driver_note_entry`、
  `driver_note_index`、`search_driver_note_entries`、`check_driver_note_index`。只是增加一个可选参数 `driver_id`，和 `project_id` 二选一。
  不传 `driver_id` 时行为与现在逐字节相同。
- `_unresolved` 同时识别 `note://` 和 `dnote://`。私有条目可以引用项目规则，项目规则**不得**引用私有条目，否则公开读会遇到解析不了的地址。
- `get_driver_note`、`driver_note_history`、`search_driver_note_history` 只服务于项目 notebook 已关闭的 free-text 历史，driver 作用域不支持。
  `update_driver_note` 照旧关闭。

**权限**：
- 写入时，调用者推导出的 `driver_id` 必须等于作用域里的 `driver_id`，否则返回 `not_notebook_owner`；只有 break-glass 例外，并做标记。
  `director_identity` 可以是 subagent 子标签（`codex-macbook-air/sub-3`），以便区分是哪个 subagent 写的。
- 读取要求调用者是已注册的 driver 或所有者。`public` 只有在被所有者加入某个项目之后才能读（Q5 默认不加，所以默认读不到）。
- `writer_only_read` 装饰器和 `transaction(notebook=...)` 要从"按项目"扩展成"按作用域"。driver 作用域**不进** `PUBLIC_READS`。

**其他规则**：
- 引用 State ID：私有条目里出现的 attempt / node / claim / subagent 等引用，写入时校验它们存在。State 永远是权威；
  其他 driver 读到的私有条目是**不可信输出**。
- 不删除：沿用 notebook 的规则，supersede 或 delist，不提供删除。
- 事件：私有写入**不进**项目事件流（`driver_note_updated` 只属于项目 notebook），也不唤醒任何 wait。overview、frontier、wait 都不读私有笔记，
  "现在谁在做什么"只看 claim / attempt / subagent 表，私有笔记只回答"这个 driver 打算怎么做、做到哪了"。
- **回收时（D8）**：别人本来就能读，不需要复制。`take_over_attempt` 会把原 driver notebook 里引用了该 attempt 的 `dnote://` 地址
  列进 handoff 记录。回收方不能改原 driver 的笔记，只能在自己的 notebook 里写续做进度。
- `public` 的 notebook 由所有公网 agent 共写（D6 的后果，见 12.3）。

## 6. 交接协议（handoff）

### 6.1 什么时候用

- 一个 driver 要下线、上下文快满了，或者让给更合适的 driver（比如 review 需要指定评委模型）；
- 所有者重新安排工作（通过消息请某个 driver 交出）；
- 一个 attempt 需要换 controller，例如 SkillFlow run 的 checkpoint 要由另一个 driver 来批。

### 6.2 操作

1. `offer_handoff(project_id, subject: {attempt_id | claim_id}, to: {driver_id | "any"}, package, expected_owner_fence, request_key)`
   - `to="any"`：放进项目公共池，任何成员都可以 accept（自助餐，D2）。
   - 同时向接收方（`to="any"` 时为全体成员）的 driver inbox 发一条 `kind=handoff_offer` 的 transient 消息，`refs` 指向对象。
   - `package`（结构化，单个字段有上限，总量不超过 16 KB）：

     ```json
     {
       "context_hash": "<attempt 冻结 context 的 64-hex>",
       "observation_version": 3,
       "event_cursor": 20342,
       "source": {"repo": "...", "branch": "...", "base_sha": "...", "head_sha": "..."},
       "workspace": "linxuhaserver:/home/linxuhao/.AItelier/worktrees/...",
       "workers": {"quiescent": true, "detail": "所有 writer/reviewer 进程已终止"},
       "pending_checkpoint": null,
       "reports": [{"ref": "...", "sha256": "..."}],
       "open_issue_ids": ["iss-..."],
       "note_entries": ["note://wuxia-myth/72aaab718d13"],
       "private_notes": ["dnote://codex-macbook-air/<12 hex>"],
       "subagents": ["codex-macbook-air/q4-cleanup"],
       "next_step": "≤ 2000 字：我做到哪、下一步是什么"
     }
     ```

   - 服务端校验 `context_hash` 等于 attempt 的冻结 context、`observation_version` 等于当前值、`event_cursor` 不超过项目 high-water，
     报告引用走 `retain_report`。**package 不进 notebook**（in-flight 状态不进 notebook，2026-10-06 ruling），
     它存在 `state_handoffs` 表里，并被 `attempt_detail` 引用。
2. `accept_handoff(handoff_id, expected_owner_fence)`：原子地转移所有权（attempt：`owner_driver_id`、`reporting_actor`、
   fence+1、租约重置；claim：status=transferred，同时为接收方新建一个 live claim），写 `attempt_ownership_transferred` 事件，
   回一条 `handoff_reply` 消息。attempt 下登记的 subagent 随之转移（`owner_driver_id` 变更、fence 加 1），由接收方续租。
3. `decline_handoff(handoff_id, reason)`；`withdraw_handoff(handoff_id)`（只有发起方可以撤回，且必须在被接受之前）。
4. offer 本身也有租约（默认 24 小时）。过期后原 owner 仍是 owner，**不会**自动转移。

### 6.3 运行中的 worker 能不能一起交

- `workers.quiescent=false` 时，只有当接收方的 `capabilities.can_reach_checkout` / `harnesses` 覆盖原 worker 所在的位置时，
  才允许 accept。否则 `accept_handoff` 返回 `receiver_cannot_observe_workers`。
  例：Codex 在 MacBook Air 上跑的本地 Codex 子代理，Grok Bot 无法观察，必须等 Codex 那边 settle 之后才能交接。
- 这是**自愿**交接（原 driver 在场）的规则。原 driver 已失联时走 4.4 的回收加 4.6 的 subagent 接管（D8），
  观察不到的 subagent 按 orphan 处理，不受本条限制。
- 已有的 `register_relay_handoff`（`core/state_external.py:35`）服务于"失败 run 的 relay"，保持不变。
  handoff package 可以在 `source` 里引用 relay inventory 的 digest。

## 7. 自助餐模式（D2）与并发写冲突规则

### 7.1 没有强制角色

- 项目成员一律平等，**任何成员都可以 claim 任何 ready 的节点**，用途可以是 implement、review、investigate 或 plan。
- 不设 lead，也不设基于角色的权限；所有者（`is_admin` 的 LAN driver，默认只有 `owner-cli`；以及 CF Access 所有者邮箱）是唯一的特殊身份，负责：注册和移除 driver、
  `set_project_driver`、`set_dispatch`（项目级 active/hold/archive）、break-glass。
- driver 的 `capabilities` 只用于**自选**：driver 自己判断能不能做，比如需要特定评委模型的 review。系统不据此拒绝。

### 7.2 review 独立性：可选、advisory、默认关闭

项目策略 `review_independence ∈ {off, advisory}`，**默认 `off`**。没有 `required`（D2）。

- `off`：与现在完全相同。
- `advisory`：`verify_node` 照常放行。如果某个 `kind=review` 的 criterion 的最新 evidence 来自实现者本人
  （`evidence.reviewer_driver = attempt.owner_driver_id`，或者等于曾经拥有过这个 attempt 的任何 driver），
  就在 acceptance receipt 的 `provenance_json` 里标 `self_reviewed=true`，并在 overview 里显示一个计数。
  这只是可见性，不是拦截。
- 不论设置为何，产品自己的验收条件（criterion 文本里写的"independent gpt-6.1-sol review"之类）仍然由 criterion 本身
  和填 evidence 的 driver 负责，和现在一样。

### 7.3 冲突规则

1. **dispatch**：在 `multi_driver=on` 的项目里，`start_attempt` / `start_external_attempt` 要求调用者持有该节点的 live
   `implement` claim，且 fence 一致；否则拒绝，错误码 `claim_required`。这条排他由 `state_node_claims_one_exclusive`
   唯一索引在数据库层面保证，**只有一个赢家**，输家得到 `claimed_by_other`，并附上对方的 driver_id 和租约到期时间。
   `claim_node` 本身就是"抢单"：先到先得，不需要任何人分配。
2. **observe**：必须是 attempt 的 owner，且 fence 一致。
3. **evidence**：任何成员都可以对任何 CANDIDATE 记录 evidence（和现在一样）。建议在 review 前先 `claim_node(review)`，
   让别人看到"有人在审"，避免两人重复审查。review claim 可以并存，不排他。
4. **结构写**（revise/split/supersede/facet、改依赖）：任何成员都可以写，仍然靠现有 CAS（`expected_revision`）。
   如果目标节点或其上游有**别人**的 live claim 或 active attempt，必须附 `override_reason`，否则拒绝（`claimed_by_other`）。
   带了理由就照常执行，原有失效语义不变（revision 变更 → 下游 STALE、旧 attempt 迟到 → superseded），
   同时自动向受影响 owner 的 driver inbox 发一条 `kind=lease_notice` 通知。
5. **hold**：`set_node_hold` 记录 `driver_id`。任何成员都可以放或解，但解别人放的 hold 需要 `override_reason`
   并通知原放置者。hold 回归本义（禁止 dispatch），"有人在做"改由 claim 表达。
6. **priority**：任何成员都可以改，仍是 CAS（`expected_priority`），事件记录 driver。
7. **notebook**：保持"同 revision 只有一个赢家"。entry 带 author driver；supersede/delist 别人的 entry 时通知原作者。
8. **一个 checkout 只能有一个 writer**：`claim_node(implement, workspace=...)` 时，如果同一个 `workspace` 字符串已有别的 live claim，
   就拒绝（`workspace_in_use`）。这只是声明层面的检查，服务端不读远端文件系统。
9. **一个 run 只能有一个 controller**：SkillFlow attempt 的 checkpoint 决策要求调用者是 attempt 的 owner；换人走 handoff。
10. **所有拒绝都有稳定的错误码**：`claim_required`、`claimed_by_other`、`stale_fence`、`lease_not_expired`、`override_reason_required`、
    `workspace_in_use`、`use_driver_inbox`、`receiver_cannot_observe_workers`，方便 driver 程序化处理。

## 8. API 变更（MCP 与 REST 同一套 typed contract）

所有新动作都进入 `core/state_commands.py` 的读/写表和 `_handlers`（第 758 行）。MCP 仍然只有 `state_graph_read` / `state_graph_write`
两个入口（`api/state_graph_tools.py`），不新增工具。REST 走现有的 `/api/state/query/{read_action}` 和
`/api/state/commands/{write_action}`。新增的 GET 路由都要声明 action，并由 `api/state_http.py` 的路由级守卫
统一分类：**全部私有**，不加入 `PUBLIC_READS`。`tests/unit/test_state_read_visibility.py::TestExhaustiveDoors`
会自动枚举这些新路由。

### 新 read actions

| action | 参数 | 说明 |
|---|---|---|
| `whoami` | — | 调用方的 driver_id、kind、is_admin、所属项目、capabilities |
| `list_drivers` / `get_driver` | — / `driver_id` | 全局 driver 列表（不含 token 哈希），含 `last_seen_at` |
| `project_drivers` | `project_id` | 项目成员 |
| `list_claims` | `project_id`、`node_keys?`、`status?`、`driver_id?` | claim 及其租约状态 |
| `list_subagents` | `project_id?`、`attempt_id?`、`owner_driver_id?`、`status?` | subagent 登记（4.6） |
| `get_handoff` / `list_handoffs` | `project_id`、`status?` | handoff 及 package |
| `list_driver_inbox` / `wait_for_driver_inbox` | `after`、`statuses?`、`kinds?`、`timeout_seconds` | 只能读自己的 inbox（所有者可指定 `driver_id`） |
| `list_driver_notebooks` | — | 目录模式入口：每个 driver 的 notebook 地址和条目计数（5.3） |
| `driver_note_index` / `get_driver_note_entry` / `search_driver_note_entries` / `check_driver_note_index` | 新增可选参数 `driver_id`（与 `project_id` 二选一） | driver 作用域：任意已注册 driver 和所有者可读，不公开 |
| `wait_for_state_change` | 新增 `include_driver_inbox?`、`include_lease_events`（默认 true） | 见 5.2 和 4.5 |

REST GET（私有）：`/api/drivers`、`/api/drivers/me`、`/api/drivers/{id}/private-notes`、`/api/state/projects/{id}/drivers`、
`/api/state/projects/{id}/claims`、`/api/state/projects/{id}/subagents`、`/api/state/projects/{id}/handoffs`。

### 新 write actions

| action | 关键参数 | 权限 |
|---|---|---|
| `register_driver` / `rotate_driver_token` / `suspend_driver` / `retire_driver` / `set_driver_admin` | `driver_id`、`expected_revision` | admin |
| `set_project_driver` | `project_id`、`driver_id`、`status`、`expected_revision`、`reason` | admin |
| `claim_node` | `project_id`、`node_key`、`purpose`、`expected_revision`、`lease_seconds`（默认 7200）、`workspace`、`request_key` | 任意成员 |
| `heartbeat` | `claim_ids[]` / `attempt_ids[]` / `subagent_ids[]`、各自的 `fence` | owner（父 driver 代 subagent 续，D3）；批量，单次最多 100 个 |
| `release_claim` | `claim_id`、`fence`、`reason` | owner；或 admin |
| `register_subagent` / `update_subagent_checkpoint` | 见 4.6 | 父 driver |
| `adopt_subagent` | `subagent_id`、`fence`、`observability`、`reason` | 回收方（attempt 的新 owner） |
| `report_subagent_settled` | `subagent_id`、`quiescent=true`、`report_ref`、`report_sha256` | 原 driver（允许旧 fence，只能关 orphan） |
| `abandon_external_attempt` / `take_over_attempt` | 见 4.4 | 任意成员，租约过期并过了 grace |
| `offer_handoff` / `accept_handoff` / `decline_handoff` / `withdraw_handoff` | 见 6.2 | owner / 接收方 |
| `send_driver_message` / `acknowledge_driver_message` / `resolve_driver_message` / `retract_driver_message` | 见 5.2 | 发送方：任意已注册 driver；状态变更：收件人本人 |
| `write_driver_note_entry` / `supersede_driver_note_entry` / `delist_driver_note_entry`（现有动作） | 新增可选参数 `driver_id`（5.3） | driver 作用域：仅该 driver 本人；允许 `force=informational` |

### 现有 action 的变化

- `start_attempt` / `start_external_attempt`：新增可选参数 `claim_id` 和 `fence`。多 driver 模式下必填。
- `report_external_attempt`：新增可选参数 `fence`。多 driver 模式下必填。
- `record_evidence`、`verify_node`：服务端自动填写 `reviewer_driver`。`director_identity` 按 3.2 的规则校验。`advisory` 模式下 receipt 带 `self_reviewed`。
- `set_node_hold`、`set_node_priority`、共享 notebook 写入、项目 inbox 发信：自动记录 `driver_id`。
- `send_director_message`：新增 `ack_mode`（`at_least_n` | `broadcast`，默认 `at_least_n`）和 `ack_quorum`（默认 1，仅 `at_least_n` 有效）；
  `acknowledge_director_message` 改为按 driver 计数；`list_director_messages` 增加 ack 字段和 `needs_my_ack` 过滤（5.1a，D12）。
  新错误码：`ack_quorum_unreachable`、`not_project_member`。
- `state_graph_help`：schema 和 driver guide 增加「Multi-driver」一节（`core/state_driver_guide.py`）。
  MCP prompt `state_graph_driver` 加入"先 whoami、再 claim、再 dispatch、按期 heartbeat"的循环。

## 9. 数据模型与迁移

### 9.1 表

新的全局表（D9，不带 `state_` 前缀）：`drivers`、`project_drivers`、`driver_audit`、`driver_subagents`、`driver_inbox_messages`、
`driver_inbox_deliveries`、`driver_note_entries`（与 `state_driver_note_entries` 同一份模板，5.3）。新的 State 表：`state_node_claims`、`state_handoffs`，以及
`state_claim_history`（append-only，记录 claim 的每次状态变化，带触发器禁止 UPDATE/DELETE，与 evidence/receipt 表的做法一致）。

扩列：

- `state_attempts`：`owner_driver_id`、`owner_fence`、`lease_expires_at`、`last_heartbeat_at`，status 的 CHECK 增加 `abandoned`；
- `state_external_owners`：status 增加 `abandoned`；
- `state_external_observations`：`late_after_abandon`、`fence`；
- `state_evidence`：`reviewer_driver`；
- `state_node_holds`、`state_driver_note_entries`：`driver_id`；
- `state_director_messages`：加 `sender_driver_id`、`ack_mode`、`ack_quorum` 三列（5.1、5.1a）；`state_director_deliveries` **不动**；
  新表 `state_director_delivery_acks`（D12）；
- `state_project_policy`：`multi_driver ∈ {off,on}`（默认 off）、`review_independence ∈ {off,advisory}`（默认 off）、`lease_defaults_json`（默认 7200 秒，grace 900 秒）。

### 9.2 迁移步骤（与 `docs/state-external-harness.md`「Schema migration」一致）

1. 先备份（`~/.AItelier/backups/`），协调所有 State writer 暂停写入，同时运行的新旧版本不能共用一个库。
2. 一次事务内完成：
   - `ALTER TABLE ... ADD COLUMN`；
   - 一处需要改 CHECK 的表（`state_attempts` 的 status，以及较小的 `state_external_owners`）做事务性重建，
     保留 seq 高水位、索引和触发器，并做外键检查；
   - 迁移幂等，遇到损坏或不完整的输入就整体失败，不丢原数据。
3. 插入 `owner-cli`（`is_admin=1`，写入现有 `AITELIER_ADMIN_TOKEN` 的哈希）和 `public` 两个 driver。**不回填**历史行的 `owner_driver_id`，保持 NULL：
   历史上 `authorized-state-operator` 背后究竟是 Codex、Claude 还是 ChatGPT，State 并不知道，猜出来的身份就是伪造的出处。
4. 现有 active/paused 的 attempt（当前 8 个）全部标记为 `lease_state=legacy_unleased`：没有租约、不会过期，行为与现在完全一样。
   要把它们纳入多 driver 体系，只能显式操作：任意成员在说明理由后 `take_over_attempt`，或者 `abandon_external_attempt(quiescence=...)`。
   legacy 行没有租约，所以不需要等过期，但必须附 `override_reason`，并经所有者确认。AMI 那个 attempt 应当是第一个由人工裁决的对象（见 Q11）。
5. 历史项目 inbox 消息的 `sender_driver_id` 保持 NULL，ack 语义不变。

### 9.3 兼容性

- `multi_driver=off` 的项目：所有新参数都可选，不要求 claim、不要求 fence，消息的同项目拒收仍然生效，但错误码改为明确的
  `use_driver_inbox`。除了错误码更具体以外，行为和现在逐字节一致。这一点要用回归测试钉住：现有
  `tests/unit/test_state_graph.py`、`test_state_attempts.py` 和 director messaging 的测试原样通过。
- admin token：现有 `AITELIER_ADMIN_TOKEN` 迁移后作为 `owner-cli` 的 token 继续可用，请求头 `X-AItelier-Admin-Token` 作为别名保留；它是 break-glass。AGENTS.md 说"开发期不需要向后兼容"，但 State 库里有生产历史（evidence、receipt、
  事件都是 append-only），所以**数据**必须兼容；**API** 可以在过渡期之后收紧（例如多 driver 项目拒绝不带 fence 的 observe）。
- 旧客户端读到新的 readiness 值（`in_progress_lease_expired`）或 attempt 状态（`abandoned`）：
  driver guide 和 `.codex/hooks/postcompact_driver_state.py` 要同步更新。旧的 Codex 会话把未知值当成"非 ready、非 closed"即可，不会误派发。
- 回滚：旧代码遇到新增的列和表会忽略它们；但 `abandoned` 状态不在旧 CHECK 里，所以旧代码无法写 attempt 表。
  因此回滚前必须确认没有 `abandoned` 行，或者接受回滚后对应节点只读。这一点写进部署清单。

## 10. 上线计划

按 AGENTS.md：测试只在一次性容器里跑（`docker run --rm ... aitelier:latest`、`--network none`、2 CPU / 2 GiB、单 worker、
同时最多 4 个），不在生产 `aitelier` 容器里跑。部署走正常流程（review 过的 commit → `docker compose build aitelier && up -d`）；
磁盘上有 commit 不代表正在运行的 worker 已经加载它。

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| P0 身份 | 全局 `drivers` / `project_drivers` / `driver_audit`；多 token 查表（authz、admin_routers、state_only 及各调用方）；公网 = `driver:public`；`whoami`；`director_identity` 校验；LAN driver 共用 `linxuhao` 经 SSH 访问 127.0.0.1:4444（D11）；`~/.aitelier-drivers/` 下每个 driver 一个 0600 token 文件；driver guide 写入 3.2a 的约定；不改其他行为 | Codex 和 Grok 各自经 SSH、用自己的 LAN token 调 `whoami` 并返回正确；公网 MCP 被认成 `public`；`owner-cli` 用旧 token 照常可用 |
| P1 租约（只告警） | attempt / claim 的租约（2 小时）、`heartbeat`、`lease_expired` 事件、overview 字段；**不强制** claim | 一周内统计：心跳到达率、误报的过期次数（driver 实际还活着却过期）、父 driver 能否稳定地为 subagent 续租 |
| P2 driver inbox + 私有笔记 + 项目 inbox ack 模式 | 5.2；5.3（driver notebook 泛化为按作用域）；5.1a（`at_least_n` / `broadcast`，v3）；`wait_for_driver_inbox`；PostCompact 按 driver 投影 | Codex ↔ Grok 互发 transient/standing；一方 ack 不影响另一方；各自写私有笔记，对方能读不能写 |
| P3 claim 强制 + 回收 + subagent 接管 + handoff | `multi_driver=on` 先只对 `aitelier` 开启；`abandon`、`take_over`、`driver_subagents`、`adopt_subagent`、`offer/accept_handoff` | 用 AMI 那条 legacy attempt 走一遍人工回收（需所有者批准），包括一个 `unobservable` subagent 的 orphan 流程；一次真实的 Codex → Grok handoff |
| P4 扩展到 wuxia | `wuxia-myth` 开启 `multi_driver`；可选开启 `review_independence=advisory` | 一轮 wuxia 批次中没有出现重复 dispatch；`self_reviewed` 计数可见 |
| P5 清理 | 归档 `novel-lingwu-deputy` 这类邮箱项目；driver guide 删除旧的绕过说明 | — |

每个阶段的度量：

- 遗留 attempt 的数量，以及从"最后一次心跳"到"被回收或接管"的时长（基线：AMI 是 5 天以上）；
- `claimed_by_other` / `stale_fence` 拒绝的次数：这些是被拦下来的并发冲突，越早出现越好，不应为 0；
- 被误吃的消息数（目标为 0）；
- 两个 driver 同时 dispatch 同一节点的次数（目标为 0，由唯一索引保证）。

## 11. 已裁决的问题（原待决问题）

2026-10-09 所有者答复：Q1、Q6 确认；Q4 由 D11 解决；Q5 维持默认；Q10 随 D10 解决；**其余 12 题（下表）16:11 全部按推荐默认值批准**。
目前没有待决问题。实施中出现的新问题按 D4 经由 Grok Bot 提交给所有者。

| # | 问题 | 裁决（= 原推荐默认值） |
|---|---|---|
| Q2 | `public` driver 能否也用 REST（`/api/state/query`、`/commands`）？现在经 tunnel 的 REST 写入只认 Access 邮箱，不认 external token。 | **允许**，用同一个 external token。否则公网 Codex 绕过 MCP 300 秒截断的直连 HTTP 长等待（`docs/state-agent-driver.md` 第 20 行）就用不了。 |
| Q3 | 以后是否需要在公网区分多个 driver？ | **暂不需要**。主力 driver 都走 LAN；公网只给临时 agent 用。需要时再引入按 driver 的公网 token。 |
| Q7 | 租约过期后的 grace 多长？ | **15 分钟**。 |
| Q8 | `register_subagent` 是否强制？ | **在 `multi_driver=on` 的项目里强制**，范围是会写 checkout 或产出 evidence 的 subagent；纯只读的调研 subagent 可以不登记。 |
| Q9 | 回收时遇到观察不到的 subagent（`unobservable`），回收方能否立即在新分支续做，还是要等原 driver 确认它已停？ | **可以立即续做**（新分支 + 新工作目录），旧 subagent 的写入被 fence 挡住，旧分支不经检查不合并。 |
| Q11 | AMI 遗留 attempt `attempt-4547c5dc…` 怎么处理？ | **上线 P3 后**：由 Codex 确认 Mac 上没在跑再 `abandon(attested)`；或者由回收方 `take_over`，把它的子代理当 `unobservable` 处理。在那之前不动。 |
| Q12 | 回收别人过期的 attempt 前是否要额外等待？ | **不额外等**（grace 本身就是等待期），系统自动通知原 driver。 |
| Q13 | admin（`owner-cli`、所有者邮箱）是否受 claim 约束？ | **不受约束**，但每次写入都标 `break_glass=true` 并通知受影响的 owner。 |
| Q14 | SkillFlow checkpoint 由谁批准？ | **只由 attempt owner 批准**；要换人走 handoff 或回收。 |
| Q15 | 文档位置？ | **保留 `design/multi-driver-coop.md`**，进入实现阶段时再决定是否迁移成 State design item。 |
| Q16 | 私有 notebook 是否允许 `force=in_force`（driver 给自己定的规则），还是只允许 informational？ | **两者都允许**，默认 informational；`in_force` 的私有条目只约束该 driver 自己，不进任何人的 PostCompact 投影。 |
| Q17 | 项目规则（`note://`）能否引用私有条目（`dnote://`）？ | **不能**（公开读时解析不了）；反方向可以。 |

## 12. 评估（基于现有代码的实话）

### 12.1 实现难度

现有相关代码规模：`core/state_attempts.py` 772 行、`core/state_external.py` 227、`core/director_messaging.py` 428、
`core/state_commands.py` 900、`core/state_changes.py` 327、`core/state_service.py` 1256、`core/state_graph.py` 882、
`core/state_portfolio.py` 249、`api/authz.py` 195、`api/mcp_router.py` 2155、`api/state_http.py` 477、
`.codex/hooks/postcompact_driver_state.py` 750。相关单测：`test_state_attempts.py` 922 行、`test_state_external.py` 640、
`test_state_read_visibility.py` 439、`test_state_changes.py` 292 等，`tests/unit` 里与 state / director / mcp 相关的测试文件共 26 个。

| 阶段 | 涉及模块 | 新表 / 改表 | 迁移风险 | 测试工作量 | 粗估 |
|---|---|---|---|---|---|
| P0 身份 | 新 `core/drivers.py`；`api/authz.py`（单 token → 查表）、`api/admin_routers.py`、`api/state_only.py:_BearerAuth`、`api/state_graph_routers.py`、`api/mcp_router.py:_authorize`（公网 → `public`）、`core/state_commands.py`；admin token 的约 9 个调用方（cli、scripts、hook、dsh 补丁）改用别名请求头；**不再需要改 `core/cf_access.py`** | 新：`drivers`、`project_drivers`、`driver_audit` | 低（纯新增），但**切换有风险**：老 token 必须先迁移成 `owner-cli`，否则所有者的 CLI 和正在跑的 director 会被锁在门外 | 中：认证矩阵（LAN token × 多个 driver、external token、Access 邮箱 × tunnel 与非 tunnel × MCP 与 REST）；约 30 个测试文件引用 admin token | 约 600–900 行代码 + 同量测试 |
| P1 租约 | `state_attempts._reserve`、`state_external.observe`、`state_changes`（新事件、`wait_disposition`）、overview / readiness 派生、`state_run_summary` | 扩列：`state_attempts` 的 4 列；新：`state_node_claims`、`state_claim_history` | 低到中：只加列，不改 CHECK | 中：时间相关测试（注入 clock）、过期 / grace 边界、心跳不产生事件 | 约 500–800 行 |
| P2 driver inbox + 私有笔记 + ack 模式 | 新 `core/driver_inbox.py`（可借鉴 `core/director_messaging.py` 的 428 行）、`core/state_driver_notes.py` / `core/state_driver_index.py` 泛化为按作用域（D10，复用而非新写）；项目 inbox 只加一列、改一个错误码；PostCompact hook 增加 driver inbox 来源 | 新：`driver_inbox_messages`、`driver_inbox_deliveries`、`driver_note_entries`、`state_director_delivery_acks`；`state_director_messages` 加三列 | **低**（只加列和表）（D7 之后**不再重建** delivery 表，现有 258 条 delivery 不动） | 中：两种 inbox 并存、两套游标的组合等待、notebook 泛化后**现有项目 notebook 测试必须原样通过**（回归风险集中在这里）、driver 作用域的「本人写 / 他人读 / 不公开」矩阵、PostCompact hook 测试；D12 的 quorum / 并发 ack / 成员变动 / v2→v3 兼容测试 | 约 800–1100 行（D10 省约 200 行，D12 加约 300–400 行） |
| P3 claim 强制、回收、subagent 接管、handoff | `_reserve`（claim / fence 检查）、`observe`（fence、迟到报告）、新 `abandon` / `take_over` / handoff、`driver_subagents` 与 `adopt_subagent` / `report_subagent_settled`、`state_service` 中的结构写（override_reason）、`set_node_hold` | **重建** `state_attempts`（status CHECK 增加 `abandoned`）、`state_external_owners`；新 `state_handoffs`、`driver_subagents` | **高**：`state_attempts` 是核心表，有部分唯一索引、外键和 2555 行历史；重建表的先例在 `core/state_attempt_schema.py` 里，可以复用 | 高：并发竞态（两个 driver 同时 claim / take_over）、fence 拒绝、迟到 observe 变成 superseded、`abandoned` 对 readiness / frontier / wait / run-summary 的全部影响，再加上 subagent 的三种可观察性分类和 orphan 的关闭路径 | 约 1600–2400 行（比上一版多出 subagent 接管的约 400–600 行） |
| P4 / P5 | 策略开关、advisory 标注、文档、driver guide、归档邮箱项目 | 扩列 `state_evidence.reviewer_driver` | 低 | 低 | 约 200–400 行 |

**最难的几处**：

1. **新增 `abandoned` 状态**：`ACTIVE` 元组、部分唯一索引、`_eligible_candidate`、`_pins_current`、readiness 派生、
   `wait_disposition`（`nothing_to_wait` / `action_required`）、run-summary、PostCompact hook、web UI 徽章，
   全都对 attempt 状态做穷举。漏掉一处，就会出现"节点显示 ready 但 dispatch 被拒"或者"wait 永远不返回"。
   现有代码的注释已经记录过类似的教训（`docs/state-agent-driver.md` 第 26 行关于 candidate 过滤的长段）。
2. **身份迁移**：admin token 从单个环境变量改成查表，涉及约 9 个调用方和约 30 个测试文件。必须先把现有 token 迁移成 `owner-cli`、
   登记好两个 LAN driver，再切换，否则会把所有者的 CLI 和正在运行的 director 锁在门外。
5. **subagent 接管（D8）**：要靠 driver 主动登记 subagent、及时更新 checkpoint；接管时"能否观察"由回收方自行声明。
   这部分的正确性主要取决于 driver 是否遵守协议，测试只能覆盖服务端的状态机。
3. **表重建**：`state_attempts` 和 `state_director_deliveries` 都要重建，而 State 表靠触发器保证 append-only。
   迁移必须在一个事务里完成，并在一次性容器里用生产库的副本演练。
4. **时间语义**：租约依赖服务器时钟和注入的 clock；所有新代码都要走同一个 `now()`，测试才可控。

总体：P0–P2 属于"大但直白"的工作；D7 让 P2 的迁移风险从"中"降到"低"；D8 让 P3 变大、变难。按这个代码库的节奏
（每个变更都要独立 review 加一次性容器测试），P0–P2 大约各需 1–2 个批次，P3 需要 3–4 个批次（上一版是 2–3）。

### 12.2 自助餐模式下的协调难度

**会出现的竞争和问题**：

1. **抢单竞态**：两个 driver 看到同一个 ready 节点，同时 claim。数据库唯一索引保证只有一个赢家，输家收到 `claimed_by_other`。
   **这一点由机制完全解决。**
2. **重复劳动（claim 之前）**：两个 driver 都在 claim 之前花了时间读代码、写计划。claim 只防重复 dispatch，不防重复思考。
   缓解：**先 claim、再研究**（`purpose=investigate` 或 `plan`，开销很低）；只读调查用 `investigate`。**这需要纪律。**
3. **语义重复（不同节点、同一件事）**：比如两个 driver 分别 `add_nodes` 加了含义相同的新节点，或者分别修同一个 bug 的两个症状节点。
   claim 是按 node_key 排他的，看不出语义重叠。缓解：加节点前先 `search_nodes`（driver guide 已经要求），
   并把"我要加 X"发到项目成员的 driver inbox。**这需要纪律，机制只能部分帮忙。**
4. **结构写踩到别人的工作**：A 在做 X，B 修改了 X 的上游 contract，X 变成 STALE，A 的工作白做。机制会要求 B 附 `override_reason`，
   并自动通知 A，但不阻止。**半解决**：最终仍取决于 B 是否克制。
5. **review 空档**：没有强制角色之后，CANDIDATE 可能没人审。wuxia 现在已有 7 个 `candidate_review` 等待中。
   缓解：overview 列出"无 review claim 的 CANDIDATE"及其等待时长；建议约定每个 driver 在开新 implement 前先清一个 review。
   `review_independence=advisory` 让自审变得可见。**需要纪律，机制只提供可见性。**
6. **共享 checkout 和运行时资源**：wuxia 的 Godot 引擎锁、CPU gate 队列、私有 origin、`linxuhaserver` 上的同一个 checkout。
   claim 的 `workspace` 只能挡住声明了同一个路径的情况。没声明、或者声明不同但实际是同一个目录的情况，挡不住。
   AGENTS.md 已经规定"一个 checkout 一个 writer""不 `commit -a`""不做广泛 kill"，这些在多 driver 下**更重要**，仍然**全靠纪律**。
7. **租约续命**：D3 要求父 driver 为 subagent 续租。如果某个 driver 用后台脚本无条件续租，即使 subagent 早已死掉，
   租约也永远不会过期，1.3 的问题又回来了。**机制无法区分"真在监管"和"脚本在续"**，只能靠约定：续租必须与实际监管绑定
   （例如由 wait 循环在确认 subagent 状态后才续）。
8. **跨 driver 的发布与部署**：AGENTS.md 规定公开发布和 force push 需要所有者明确授权。两个 driver 各自"以为得到了授权"的风险上升。
   建议：发布类节点（`release.*`）由所有者在 standing 消息里点名一个 driver。这是约定，不是机制。
9. **上下文分裂**：两个 driver 各自的长期记忆不同步（Codex 的 goal / notebook vs Grok Bot 的记忆）。
   缓解：State（attempt、issue、hold）和 notebook 是唯一共享事实；driver 私有记忆只能放偏好，不能放"现在谁在做什么"。

10. **公网 driver 内部的冲突（D6）**：所有公网 agent 共用 `public` 这一个身份。它们之间的 claim、fence、inbox、私有笔记都分不开，
    两个公网 agent 可能互相续租、互相 ack、互相覆盖进度。**机制对此无能为力**：主力 driver 必须走 LAN（Q3、Q5）。
11. **subagent 接管后的双 writer（D8）**：`unobservable` 的旧 subagent 可能还在 MacBook Air 上跑，同时回收方在新分支续做。
    fence 能挡住旧 subagent 的 State 写入，"每个 subagent 写自己的分支"能挡住 git 冲突，但**挡不住它去 push 到共享远端、
    或者动共享资源**（Godot 引擎锁、CPU gate、私有 origin）。原 driver 回来后必须及时处理 orphan 通知。
12. **私有笔记与 State 不一致（D7）**：私有笔记允许写进度，可能和 attempt / claim 的实际状态对不上。规则是 State 永远优先；
    其他 driver 读到的私有笔记只能当线索。
13. **两种 inbox 漏看**：driver 要同时等项目 inbox 和自己的 driver inbox，漏掉一个就会错过系统通知（租约过期、被接管）。
    `wait_for_state_change(include_driver_inbox=true)` 把两者合到一次等待里，driver guide 应把它设为默认用法。

**结论**：claim + lease + fence 能可靠地解决**同一节点**上的冲突，包括重复 dispatch、写入时的身份混淆、遗留占位和交接后旧主人的迟到写入。
这些正是现在完全没有保护的部分。机制**解决不了**的是：语义重复、review 没人认领、共享运行时资源，以及"真监管 vs 假续租"。
这几条需要写进 driver guide 的「Multi-driver」一节，作为两个 driver 都遵守的规则：

- 先 claim 再研究；
- 加节点前先搜索并广播；
- 开新 implement 前先认领一个 review；
- 只在真正监管 subagent 时续租；启动 subagent 前先登记，并在阶段性节点更新 checkpoint；
- 收到 orphan 通知后第一时间停掉旧 subagent 并报 settled；
- 不碰别人声明的 workspace；
- 发布类工作只由所有者点名的 driver 执行。

### 12.3 D5–D12 带来的新风险与矛盾

1. **D6"公网 = 一个 driver" vs 所有者的浏览器会话**：所有者经 Cloudflare Access 登录 web UI 做的写入，严格按 D6 也属于"公网流量"。
   **已解决（Q1 已确认）**：认成所有者本人。
2. **D6 重新引入了一小块身份塌缩**：1.1 的问题在 LAN 上解决了，但所有公网 agent 仍共享一个身份。只要 Codex 或 Grok
   有一方改走公网，就会和其他公网 agent 混在一起。缓解：主力 driver 固定走 LAN；`public` 默认不是项目成员（Q5）。
3. **D6 vs 现有 REST 能力**：经 tunnel 的 REST 写入现在只认 Access 邮箱，external token 只对 MCP 有效。`public` driver
   因此没有直连 HTTP 长等待可用，而这正是文档推荐的绕过 Codex MCP 300 秒截断的办法（Q2）。
4. **D5"非公网 = Tailscale LAN" vs 实际部署**：4444 端口只绑定在 `127.0.0.1`（`docker-compose.yml` 第 143–144 行），
   **已解决（D11）**：保持只绑本机，LAN driver 共用 `linxuhao` 经 SSH 访问，不需要基础设施改动。代价见第 11 条。
5. **D6 多个 admin token**：持有 admin 的 driver 越多，越过 claim 的 break-glass 写入就越容易发生。建议只给 `owner-cli`（Q13）。
6. **D7 私有笔记 vs 2026-10-06 ruling**：那条 ruling 规定 in-flight 状态不进 notebook，私有笔记恰恰是用来放进度的。
   二者不冲突的前提是：ruling 只管共享 notebook；私有笔记明确是非权威的；overview 和 wait 不读它（5.3）。
   需要所有者知晓这一边界。
7. **D7"别人只读"与隐私**：私有笔记对所有已注册 driver 可读，包括 `public`（如果它被注册且算 driver）。如果将来 `public` 指向
   不受信任的第三方 agent，就会看到所有 LAN driver 的草稿。建议：`public` 能读私有笔记的前提是它是同一个项目的成员（Q5 默认不加）。
8. **D8 vs"State 不检查远程进程"及"不假装 worker 已停"**：接管观察不到的 subagent，只能接管它的**记录和工作**，不能接管**进程**。
   本文用 `orphaned_unobservable` 加新分支续做来兑现 D8，但这意味着在原 driver 回来之前，可能有两个 writer 同时在跑（12.2 第 11 条）。
9. **D8 依赖登记纪律**：没登记的 subagent 无法被接管。现有 2026 条 external attempt 都是"一个 attempt = 一个不透明 worker"，
   迁移后它们的 subagent 都不可见，只能按 attempt 整体处理。
10. **D3 + D8 续租责任转移**：接管后由回收方续租，但它只能为 `adopted` 的 subagent 续租。如果它为 `unobservable` 的 subagent 续租，
    就是 12.2 第 7 条说的"假续租"。本文因此规定 orphan 不续租。

11. **共用账号 vs token 身份（D11，所有者已接受）**：所有 LAN driver 共用 `linxuhao`，任一 driver 都能读别人的 token 和 admin token、
    直接写数据库。D6 的"每个 driver 一个 token"、D7 的"本人才能写"、claim 和 fence 在 LAN 内部都只是**协作约定**，不是强制边界。
    详见 3.2a 的五条接受风险。和 D8 叠加后：同机的回收方技术上总能结束原 subagent 的进程，是否这样做，只能由协议约束。
12. **D10 复用的回归风险**：项目 notebook 有公开读、2026-10-06 ruling 等现有规则，泛化 `StateDriverNotes` 时任何一处作用域判断写错，
    都可能把私有条目暴露给公开读，或者让项目 notebook 重新接受 informational。缓解：两个作用域分表存储，`PUBLIC_READS` 只认项目作用域，
    回归测试原样通过。
13. **D12 与 v2 的交叉点**：
    - transient 消息在 N=1 时首个 ack 即 resolved，改变了 v2 的"先 ack、后 resolve"两步；需要"做完再关"的跟踪，应放到 State（claim / issue），不放在 inbox。
    - standing 消息不随 quorum 自动 resolved，这是本文对 D12 的解释（5.1a），需所有者知晓。
    - `ack_mode=broadcast` 和 v2 的跨项目 `broadcast` 参数同名，容易误用。

## 附：引用索引

| 位置 | 内容 |
|---|---|
| `api/state_graph_routers.py:12-18` `authenticated_actor` | 非 CF 调用方一律是 `authorized-state-operator` |
| `api/authz.py:95-118` `write_denial_reason` | admin token 只在 off-tunnel 时有效；CF 邮箱白名单 |
| `core/cf_access.py:84, 122-127` `verify`、`email_from_request_headers` | 只提取 `email` claim；D6 之后只服务于所有者的浏览器会话 |
| `docker-compose.yml:143-144` | 4444 只发布在 127.0.0.1（保持不变，LAN driver 共用 `linxuhao` 经 SSH 访问，D11） |
| `core/state_driver_notes.py`（568 行）、`core/state_driver_index.py`（207 行，`ENTRY_SCHEMA` 第 53–81 行、`address_of` 第 88 行） | D10 要泛化复用的 notebook 实现 |
| `api/state_only.py:24-47` `_BearerAuth` | 独立 State 服务器的单 token 认证 |
| `api/authz.py:68-90` `is_via_cloudflare`、`is_via_tunnel` | 防重放规则与 tunnel 判定 |
| `api/mcp_router.py:153, 171-215` `_EXTERNAL_TOKEN`、`_authorize` | MCP 的 tunnel 闸门和逐工具授权 |
| `core/director_messaging.py:213-305` `send_director_message` | 同项目拒收（第 234 行）、broadcast 排除自身、reply 规则 |
| `core/director_messaging.py:354-382` `project_active_standing` | standing 的 PostCompact 投影（≤ 8 条，≤ 3000 字符） |
| `core/director_messaging.py:392-428` `_transition` | 按项目一份的 ack/resolve CAS |
| `core/director_messaging_protocol.py` | v2 schema id、动作、错误码 |
| `core/state_attempts.py:21, 36-37` | `ACTIVE` 集合；每个节点只能有一个 active attempt 的唯一索引 |
| `core/state_attempts.py:177-295` `_reserve` | dispatch 守卫：`require_dispatch`、revision、依赖、external owner 登记 |
| `core/state_attempts.py:401` `retire_reservation` | 只能退役 `reserved` |
| `core/state_external.py:58-214` `observe` | reporting_actor 相等、context_hash、observation version、terminal 要求 quiescent |
| `core/state_metadata.py:1-67` | project policy、node hold、`require_dispatch` |
| `core/state_service.py:1189, 1223` | `set_node_hold`（受保护 run 检查）、`set_node_priority`（director_identity 只是出处） |
| `core/state_changes.py:42-86, 89-166` | `scan`（第 69 行消息绕过 filter）、`wait_for_state_change` |
| `core/state_commands.py:139-149, 615, 758, 818` | `SendDirectorMessage` 模型、`PUBLIC_READS`、`_handlers`、`execute` |
| `.codex/hooks/postcompact_driver_state.py` | Codex 的 PostCompact 恢复 hook |
| `docs/state-agent-driver.md` | 等待、交接、notebook、"hold 不取消 worker" |
| `docs/state-external-harness.md:122-140, 277-290` | external 状态、No expiry、共享 token 等于一个调用方 |
| `docs/state-graph.md:162-198, 572-576` | 信任边界；"one active attempt per goal"等当前边界 |
