# BlindSpatialAgent 迁移文档：v0.2 → v0.3

> 本文记录 v0.2 架构的问题、v0.3 的分阶段改造、兼容性策略、回归方法论、概念对照、踩坑与检查清单。所有数值与文件名均经源码与 `docs/V03_MIGRATION_LOG.md` 核对。
>
> 版本说明：`config/config.yaml:system.version` 已由 `0.2.0` 升到 `0.3.0`（Stage 11 收尾、验收 Demo 通过后一次性升级）。因此「v0.3」指已经交付的 Stage 1~10 代码形态 + Stage 11 验收收尾，「v0.2」指改造前形态。

---

## 1. v0.2 架构简述与三个 P0 实测问题

### 1.1 架构简述

v0.2 是一个「单线程、规则为主、可选 LLM」的原型：

- `SpatialAgentSystem` 主循环 `step()` 直接 `agent.decide()`，每轮无条件调用 LLM；
- `StateManager` 是 659 行的「上帝类」，自己读模拟器、自己算风险、自己推可行动性；
- `state.user`（当时叫 `UserState`）等状态缺 `timestamp/source/confidence` 三件套，`localization_confidence` 是写死的常量 `0.9`；
- `Action` 只有 5 种类型 + 一句话，没有优先级/有效期；
- 事件能力缺失：状态被当成日志反复打，没有「状态迁移边沿」概念；
- `from simulator import` 散落在 `state_manager` / `tools` / `agent_core` 三处，换硬件要改三个文件。

### 1.2 三个 P0 实测问题（Stage 1 审计实测确认）

| 问题 | 实测数值 | 根因 | v0.3 修复落脚点 |
|---|---|---|---|
| **主循环实测只有 0.52 Hz**（配置 1.0 Hz） | 纯计算只要 `0.21 ms`/轮 ⇒ 与算力无关 | `sleep(max(0, next_t-elapsed))` 被 LLM 阻塞吃掉；单线程里 LLM 一慢全线停 | Stage 7：认知循环搬进工作线程，快循环纯计算 |
| **`localization_confidence` 恒为 0.9**（写死常量） | 低置信度分支是死代码，任务书第 14 节场景无法构造 | 配置常量直接当置信度用 | Stage 4：`ConfidenceEstimator` 用「噪声纸面值 + 实测残差 + 新鲜度」估计，**真实范围 0.68~0.95**；UWB 噪声调到 1.5m 时最低 `0.341 < 0.4` ⇒ 低置信分支真的触发 |
| **`OpenAI` 客户端未设 `max_retries`** | 实测默认 `2` ⇒ 一次「30s 超时」最坏变成 3 次尝试 ≈ `90s`；叠加工具调用轮次后单轮 `step` 最坏 `360s` | openai SDK 默认重试，未显式覆盖 | Stage 7：`max_retries=0`（config `llm.max_retries`）+ `total_deadline_s=20` 总预算闸门；用例 62 钉死 |

---

## 2. 迁移分阶段总表（Stage 2~10）

回归基线说明：「行为快照逐轮比对」做法见第 4 节。Stage 3~8 均取得**规则模式 120 轮 Action 逐轮零差异**（Stage 9 起新增录制/回放通道，但中性 A/B 同样 0 差异）。

| Stage | 目标 | 关键落点（文件） | 回归基线 |
|---|---|---|---|
| 2 | 数据模型（Egocentric Spatial State） | `spatial/spatial_state.py` 重写（新增 `PoseState`/`AffordanceState`/`UncertaintyState`/`FrameFreshness` + 2 条自洽性校验）；`state_manager` 填充新字段 | 自检 45/45；前端 34/34 |
| 3 | Sensor Adapter Layer | 新增 `sensors/base.py`（三协议）；`sensors/simulated/` 实现；`state_manager`/`tools`/`agent_core` 改为吃协议；抽 `spatial/geometry.py` | 自检 **47/47**；e2e 全通过；**120 轮状态+Action 逐字段一致** |
| 4 | State Fusion | 新增 `fusion/state_fusion.py`/`confidence.py`/`freshness.py`；`state_manager` 退化薄门面；修 P0-7 | 自检 51/51；**Action 逐轮 0 差异**（confidence 维度预期变化） |
| 5 | Spatial World Model + 地图接口 | 新增 `world_model/`（四层）+ `maps/`（四张地图）；`reset()` 改 `clear_runtime()`（保留地图绑定） | 自检 55/55；**Action 0 差异**（纯记账不改决策输入） |
| 6 | Event Engine | 新增 `events/`（types/detector/bus/history/engine）；`agent_core` 接 `process()`；配置 `events.cooldown_s` | 自检 58/58；事件无人消费时 **Action 0 差异** |
| 7 | Safety / Cognitive 双循环 | 新增 `agent/orchestrator.py` + `agent/safety/*`；`CognitiveLoop` 工作线程；`LLMClient` 加熔断+总预算；`step()` 改非阻塞提交 | 自检 63/63；前端 34/34；e2e 全通过；**规则模式 120 轮 0 差异**；★硬验收：LLM 睡 5s 循环仍 1 Hz★ |
| 8 | Interaction Policy + Action Schema | 新增 `agent/action_schema.py`（10 种 ActionType）/ `interaction_policy.py` / `context_builder.py` / `cognitive_agent.py`；`decision.py` 退化为门面 | 自检 70/70；**120 轮 0 差异**；★Gate 9：事件可触发 Agent★ |
| 9 | Recording / Replay | 新增 `recording/`（schemas/recorder/player）；执行语义从 `_apply` 搬到 `apply_action`；`--mode replay` 不接模拟器 | 自检 74/74；**回放 40/40、30/30 100% 一致**；中性 A/B 0 差异 |
| 10 | Active Perception | 新增 `agent/active_perception.py`（`VisualRequestPolicy` 纯函数）；`tools.request_visual_observation()`；配置 `agent.perception` | 自检 74/74（新增用例 74）；中性 A/B（mode=never vs auto）0 差异；测试页 44/44 |

当前测试现状（本次实测）：`--selftest` 74/74、`--acceptance` 10/10、`--acceptance-demo` 7/7 拍、`ui_smoke_test.py` 44/44（需先起 `--serve`）、`e2e_test.py` 全通过。

---

## 3. 兼容性策略详解

v0.3 对 v0.2 **只增不删 + 类名别名 + 字段名不变 + 只读属性转发**，目的是让老构造调用与老断言零改动通过。

### 3.1 具体做法（以 `spatial/spatial_state.py` 为例）

任务书把 `UserState` 改叫 `PoseState`、`ConfidenceState` 改叫 `UncertaintyState`。直接改名会打断约 30 处调用点（`state.user.position` 遍布 decision/tools/prompt/banner/测试）。采用：

```
PoseState            ← 新类名
UserState = PoseState            # 别名，isinstance 与类型标注继续成立
SpatialState.user: PoseState      # 字段名不变 → 所有 state.user.x 不用改
SpatialState.pose → user          # 只读属性，可用新命名访问
```

同理：`UncertaintyState`/`confidence`/`uncertainty`、`SemanticState`/`SemanticScene`、`CameraFrameMetadata`/`CameraState`。新增字段一律给默认值，因此 v0.2 的全部构造调用与断言原样成立。

### 3.2 为什么不能直接删旧实现

- **别名与字段名不变**：老代码 `state.user.position`、老断言、前端 `test_page.html`、120 轮回归快照都依赖这些名字。删掉 = 把这些消费者一起删掉，或被迫大规模改调用点（铺设改动面却不增加能力）。
- **状态门面 `StateManager` 保留为薄门面**：`system.state_manager.camera = receiver`、`sm._build_affordance(env)`、`sm.build(t, elapsed)` 散落在 api/自检/工具里；逐个改只是把改动面铺大。门面让「重构」与「调用点迁移」解耦，同时用用例 51 钉死「门面必须是真转发，不是两份实现」。
- **`SpatialAgentSystem.map/nav/obstacles/sensors` 四个兼容属性保留**：`main.py`（横幅）、`api/websocket_server.py`（小地图要 `nav.local_path`、`obstacles.obstacles` 绝对坐标）、20+ 条旧自检断言直接读它们。折中：保留转发但注明「新增业务逻辑不要用，请用 `self.provider`/`self.env`」。
- **`Action.as_dict()` 键集合冻结**：前端、`e2e_test`、`speak` 等读它；新字段走 `Action.to_dict()`。谁改 `as_dict` 谁红（用例 64）。

---

## 4. 回归方法论：行为快照逐轮比对

这是「纯重构不改行为」唯一的验收方式（Stage 3 起每阶段都用）。

**序列化什么**：
- 把 N 轮 `SpatialState.model_dump()` 与 `Action.as_dict()` 序列化为 JSON（工具 `/tmp/snap_states.py` + `/tmp/cmp_snap.py`）。

**剔除哪些字段，为什么**：
| 字段 | 为什么剔除 |
|---|---|
| `timestamp` | `datetime.now()` 两次运行必然不同，它不是被重构的对象 |
| `age_s` | 依赖当前墙钟与采集时刻之差，每次运行不同 |
| `elapsed` | 运行累计时刻，只取决于运行的绝对计时，与行为无关 |

剔除后若仍出现差异，才是真实行为回归。**判断结果**：Stage 3~8 均取得「规则模式 120 轮 Action 逐轮零差异」（不一致 0 轮）。例如 Stage 4 把 `localization_confidence` 从常量改成估计值，`confidence/user` 字段**预期变化**（新增 `camera_freshness`/`uncertainty_reason`、source 由 `simulated`→`fused`），但 `_action` 差异 **0 轮**——证明「只加了估计维度，没动任何决策」是被证明的，不是被声称的。

**注意事项**：快照比对要在**规则模式**（`enable_llm=False`）下进行，避免 LLM 随机性干扰；比对前先固化 `seed`。

---

## 5. v0.2 → v0.3 关键概念对照表

| 旧叫法（v0.2） | 新叫法 / 新字段（v0.3） | 说明 |
|---|---|---|
| `UserState` | `PoseState`（别名 `UserState` 保留） | 类名升级 + 别名；字段仍叫 `user` |
| `state.user` | `state.user`（+ `state.pose` 只读属性） | 字段名不变 |
| `ConfidenceState` | `UncertaintyState`（别名保留） | 升级为 5 维 + `camera_freshness` + `uncertainty_reason` |
| `state.confidence` | `state.confidence`（+ `state.uncertainty` 转发） | 字段名不变；综合值由加权均值补全 |
| `SemanticScene` | `SemanticScene`（别名 `SemanticState`） | 仅增别名 |
| `CameraState` | `CameraState`（别名 `CameraFrameMetadata`） | 增 `freshness` 三态 |
| `localization_confidence = 0.9`（常量） | `ConfidenceEstimator.localization()` 估计值 | 真实范围 0.68~0.95 |
| `Action` 5 种类型 | `ActionType` 10 种 + `priority`/`expires_at`/`metadata` | 新增 REQUEST_VISUAL/PAUSE_NAVIGATION/RESUME_NAVIGATION/TURN_GUIDANCE/SAFETY_ALERT |
| `Action.from_llm` 手写校验 | `Action.from_llm()` 宽进严出 + Pydantic 校验 | 不合格返回 `None` → 规则兜底 |
| `StateManager`（上帝类） | `StateManager`（门面）+ `fusion/` | 逻辑拆到 `fusion/confidence`/`freshness`/`state_fusion` |
| 直接 `from simulator import` | `sensors/base.py` 协议 + `SimulatedProvider` | 装配点唯一（`agent_core.__init__`） |
| 状态日志（每轮重报） | `events/`（16 类事件 + 冷却 + 边沿） | `absent→present` 才发一次 |
| `agent.decide()` 每轮同步调 LLM | 快循环 + `CognitiveLoop` 工作线程（非阻塞 submit） | LLM 不在主线程 |
| 无安全层 | `agent/safety/safety_engine.py` + `risk_rules.py` | 纯函数规则表，每 tick 求值 |
| 无世界模型分层 | `world_model/`（当前/短期/长期/经验）+ `maps/` | `reset()` 用 `clear_runtime()` 保留地图 |
| 无录制 | `recording/`（schemas/recorder/player） | `--record` / `--mode replay` |

---

## 6. 最容易改坏的地方（来自真实踩坑，至少 5 条）

1. **循环 import 导致包 `__init__` 必须用 PEP 562 惰性导出**。`fusion/__init__.py`、`world_model/__init__.py`、`events/__init__.py`、`agent/__init__.py`、`spatial/__init__.py` 都用了惰性导出：`state_fusion` 依赖 `spatial.world_model`，急切导入会成环。急切 `from ... import` 会让包在 import 阶段就 `ImportError`，且只在某种调用顺序下触发，极难定位。

2. **一个类同时实现两个 Protocol 时方法名相撞会静默顶掉**。`SimulatedProvider` 同时实现 `SensorProvider` 与 `WorldStepper`，两者都有「统计」方法，但**故意不同名**：`sensor_stats()`（传感器统计）与 `stats()`（世界运行统计，含 `arrived`）。若同名，后定义的覆盖先定义的 ⇒ `stats()` 返回里没有 `arrived` ⇒ `SpatialAgentSystem.stats()` 直接 `KeyError: 'arrived'`，报错点却在 REST 处理器里，只留下一行 `Agent 通道异常: 'arrived'`，难定位。

3. **融合层调 provider 的顺序等于随机数消耗顺序，多调一次就改变整条轨迹**。`build()` 内 `read_pose → perceive → scan_wall_ahead → 逐障碍 measure_depth → scan_sides → read_navigation` 顺序固定。把三次扫描并进一个 `perceive()`，或让工具去走一遍 `read_pose`（加噪），都会让同一 seed 下噪声序列错位，行为回归立刻红。工具因此只读 `WorldStepper.world_reference()`（明确标注不加噪、不消耗随机数，用例 46 用 `Random.getstate()` 前后对比钉死）。

4. **`max_retries` 是 openai SDK 的隐式默认值（=2）**。`OpenAI(...)` 不传就等于 2，单轮最坏 360s。凡是「时间预算」相关参数都要问一句「库的默认是什么」，必须显式 `max_retries=0` + `total_deadline_s` 总闸门（用例 62 证明超预算时一次请求都不发）。

5. **过期结论若只清话术会冻结用户**。`kimi-k2.5` 返回过期 `SAFETY_ALERT(permits_motion=False)` 被 `gate()` 只清了 `message`、却保留整条行动 ⇒ `advance_user(motion_allowed=False)` 把用户钉在原地。必须把 `expired`/`low_confidence` 与 `repeated`/`too_frequent`/`quiet_curse`/`user_speaking`/`channel_disabled`/`silent` 分开：前者整条退回基线，后者只清话术（用例 70 钉死）。

6. **`submitted_at + last_latency` 是量纲混用（仿真秒 + 墙钟秒）**。仿真步长与真实经过时间不相等时（测试/回放/快循环空转），会把「没过期」判成过期 ⇒ 「模型答了却永远不生效」。改为只用仿真时刻：`_result_at = job.submitted_at`。

7. **替换 `agent.llm` 必须同步到 `agent.cognitive.llm`**。认知路径读的是 `SpatialAgent.llm`，只替换外层 `sys.llm` 会让替身不生效（表现为「换了模型但延迟/输出没变」）。已将 `SpatialAgent.llm` 改成属性，setter 强制同步（用例 67 断言 `ag.cognitive.llm is good`）。

8. **回放漏了 `apply_action` 一致性只有 82.5%**。交互策略的「是否重复/距上次播报多久」判据带状态，状态由 `apply_action` 更新。把执行语义从 `_apply` 搬到 `SpatialAgent.apply_action()` 后，回放器（不接模拟器）与主循环共用同一条路径，一致性升到 95%→100%。

9. **回放 `0.0 or x` 吞掉首帧时刻，整条时钟偏移 1 秒**。首帧 `t=0.0`（falsy）被静默回退成 tick 号，导致所有「距上次播报多久」判据错位。必须显式判 `None`。

---

## 7. 给后人的迁移检查清单（可勾选）

**架构与分层**
- [ ] 除 `SpatialAgentSystem.__init__` 外，任何模块未新增 `from simulator import` / `import simulator`（全仓 `^\s*(from|import)\s+simulator` 扫描，`spatial/state_manager.py`/`agent/tools.py`/`agent/agent_core.py`/`sensors/base.py`/`recording/*` 必须为零）
- [ ] 新增传感器只实现 `sensors/base.py` 协议，未在 fusion/agent/tools 里出现具体模拟器类
- [ ] 所有对外副作用（speak/ask_user/replan_route）仅经 `AgentTools.call()` 派发
- [ ] LLM 仍在认知工作线程，**主线程 `step()` 内无任何可能长阻塞的调用**

**状态契约兼容性**
- [ ] 修改 `SpatialState` 前确认：v0.2 字段名不变；新字段给默认值；别名 + 只读属性转发
- [ ] 未使用 `@computed_field`（会破坏 `model_validate(model_dump())` 往返；用例 41/743）
- [ ] `Action.as_dict()` 键集合未增删（用例 64 钉死）

**确定性不变性**
- [ ] 新增能力只加在认知（非确定）路径；规则基线 `allow()/allow_stop()` 语义与 v0.2 逐字一致
- [ ] 新增 neutral 用例证明规则模式 120 轮 `Action` 零差异，且 `perception.evaluated == 0`（结构上不可能变）

**安全底线**
- [ ] `_merge_with_safety()` 仍保证：规则判定 `WAIT(critical)` 时模型不能让用户继续走
- [ ] `gate_expression()` 的 `discard`（过期/低置信）整条退回基线，未让不可信结论否决移动

**LLM 接入**
- [ ] `llm.max_retries` 显式设为 `0`（非 SDK 默认 2）
- [ ] 设置 `total_deadline_s` 总预算；超预算时一次请求都不发
- [ ] `LLMClient.usable` = 有 key 且未熔断；无 Key/失败自动降级规则
- [ ] 熔断冷却：认证/模型名失败 600s、限流 30s、网络 15s，成功即解除

**融合层**
- [ ] `StateFusion.build()` 对 provider 的调用顺序未改动（随机消耗顺序不变）
- [ ] 工具只读 `world_reference()`（不加噪），未让工具消耗传感器随机数
- [ ] `build()` 显式接收并传递 `dt`（置信度残差要用 速度×dt 作预期位移）

**事件引擎**
- [ ] `events.cooldown_s` 键名 = `EventType.value`（大写）；未知键只告警不抛异常
- [ ] `critical` 事件豁免冷却；首帧只做基线登记；`reset()` 清 `_baselined`
- [ ] 配置里写 `null` 的阈值（如 `safety.critical_distance_m`）在读取处显式兜底 `float(None)`

**录制/回放**
- [ ] `recording/*` 零 `simulator` import（用例 47 + 73 双重确认）
- [ ] 落盘只用白名单字段（`dataclasses.fields`），不保存隐藏思维链
- [ ] `advance_*` 在 `ReplaySource` 中为全空实现（世界不动才是回放）

**测试门槛**
- [ ] `python main.py --selftest` 全绿（当前 74/74）
- [ ] `python main.py --acceptance` 全绿（10/10）
- [ ] `python tools/ui_smoke_test.py` 在 `--serve` 起后全绿（44/44）
- [ ] `python tools/e2e_test.py` 全通过
- [ ] 行为快照 120 轮（规则模式、固定 seed）逐字段比对零差异

---

## 8. 版本号升级提示

`config/config.yaml:system.version` 已由 `0.2.0` 升到 `0.3.0` —— Stage 11（十项验收场景 + 验收 Demo + 9 份文档）全部完成后**一次性**升级，避免中途把半成品标成 v0.3。本次升级已同步刷新 `README.md`、`docs/技术交付文档.md` 与各专项文档中已失效的描述。
