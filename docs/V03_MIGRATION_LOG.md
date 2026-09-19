# BlindSpatialAgent v0.3 迁移日志

> 对应任务书第十七节：「每阶段输出：修改文件 / 修改原因 / 测试结果 / 下一阶段」
> 阅读顺序建议：先看 `docs/V02_ARCHITECTURE_AUDIT.md`（为什么要改），再看本文件（改了什么）。

## 进度总览

| Stage | 内容 | 状态 | 回归基线 |
|---|---|---|---|
| 1 | 架构审计 | ✅ 完成 | `docs/V02_ARCHITECTURE_AUDIT.md` |
| 2 | 数据模型（Egocentric Spatial State） | ✅ 完成 | 自检 **45/45**、前端 **34/34** |
| 3 | Sensor Adapter Layer | ✅ 完成 | 自检 **47/47**、前端 **34/34**、e2e 全通过、**行为逐轮一致 120/120** |
| 4 | State Fusion | ✅ 完成 | 自检 **51/51**、前端 **34/34**、**Action 逐轮一致 120/120** |
| 5 | Spatial World Model + 地图接口 | ✅ 完成 | 自检 **55/55**、**Action 逐轮一致 120/120** |
| 6 | Event Engine | ✅ 完成 | 自检 **58/58**、**Action 逐轮一致 120/120** |
| 7 | Safety / Cognitive 双循环 | ✅ 完成 | 自检 **63/63**、前端 **34/34**、e2e 全通过、**规则模式 Action 逐轮一致 120/120**；★硬验收：LLM 睡 5s 时循环仍 1 Hz★ |
| 8 | Interaction Policy + Action Schema | ✅ 完成 | 自检 **70/70**、前端 **34/34**、e2e 全通过、**规则模式 Action 逐轮一致 120/120**；★Gate 9：事件可触发 Agent★ |
| 9 | Replay | ✅ 完成 | 自检 **74/74**、**录制 30 轮 → 回放 30/30（100%）**、回放器零 `import simulator` |
| 10 | Active Perception | ✅ 完成 | 自检 **74/74**、三条触发条件各自命中、`on_demand` 省图、一次性握手 |
| 11 | 十项验收场景 + 验收 Demo + 文档 + 版本升级 | ✅ 完成 | 验收 **10/10**、七拍 Demo **7/7**、自检 **74/74**、前端 **44/44**、e2e 全通过、**规则模式 Action 逐轮一致 120/120** |

> 版本号 `config/config.yaml:system.version` 已在 **Stage 11 收尾时由 `0.2.0` 一次性升到 `0.3.0`**
> （Stage 1~10 全部通过、验收 Demo 通过之后才升，避免中途把"半成品"标成 v0.3）。

---

## Stage 2：数据模型升级（Egocentric Spatial State）

### 修改文件

| 文件 | 改动 |
|---|---|
| `spatial/spatial_state.py` | **重写**（341 → 约 560 行）：新增 `PoseState` / `AffordanceState` / `BlockedRegion` / `UncertaintyState` / `FrameFreshness`；扩展 `NavigationState` / `EnvironmentState` / `Obstacle` / `SceneObject` / `SemanticScene` / `CameraState` / `RiskState`；新增 2 条自洽性校验；`to_prompt_dict()` 增加 `affordance` 与 5 维置信度 |
| `spatial/state_manager.py` | 构造并填充所有新字段；新增 `_build_affordance()` / `_severity_of()` / `_bypass_side()` / `_route_confidence()` / `_blocking_ratio()`；`_camera_state()` 增加三态新鲜度；`_evaluate_risk()` 增加台阶/坠落分支并补齐 confidence/source/timestamp |
| `spatial/__init__.py` | 惰性导出表补 6 个新名称（沿用 PEP 562 约定，不引入循环引用） |
| `agent/prompt_template.py` | `render_state()` 新增 `[可行动]` 行；`[置信度]` 行从 2 维扩到 5 维 |
| `tests/selftest.py` | 新增 **5 项**用例（41~45）；导入新结构。40 → **45 项** |

### 修改原因（逐条对应审计报告的技术债编号）

| 技术债 | 本阶段的处置 |
|---|---|
| **P0-6 `AffordanceState` 缺失** —— 系统只能回答"有什么"，不能回答"能不能行动" | 新增 `AffordanceState` + `BlockedRegion`，并让融合层由环境几何推导出 `can_move_forward` / `passable_width_m` / `preferred_direction` / `blocked_regions` / 一句话摘要 |
| **P0-7 置信度是配置常量（恒 0.9）** | `UncertaintyState` 扩到 5 维（定位/感知/语义/路线/综合）。**注意：本阶段只是把"维度位子"和"自动补全口径"建好** —— 真正让定位置信度变成估计值属于 Stage 4 的融合职责，本阶段不谎称已解决 |
| 5.2 状态缺 `timestamp`/`source`/`confidence` | 给 `PoseState` / `NavigationState` / `EnvironmentState` / `Obstacle` / `SceneObject` / `SemanticScene` / `RiskState` 全部补齐三件套 |
| 5.2 `NavigationState` 缺 `route_confidence` | 新增 `route_confidence`，由"当前是否偏离 + 累计重规划次数（封顶）"推导 |
| 5.2 `EnvironmentState` 缺 `blocking_ratio` / `walkable_width` / `stairs` / `dropoff` | 全部新增。`blocking_ratio` 用**角度占比**（`atan2(radius, distance)` 累加）而不是障碍个数 —— 1 米外的椅子和 4 米外的椅子对"前方有多堵"贡献完全不同 |
| 5.1⑥ 摄像头无法区分"从未有过"与"曾有过但断了" | 新增 `FrameFreshness` 三态（`fresh`/`stale`/`none`），这是 `CAMERA_LOST` 事件（Stage 6）的前置条件 |
| 5.1⑦ LLM 输出缺 `confidence` | `RiskState` 先补 `confidence`（Action Schema 的 `confidence` 在 Stage 8 做） |
| P1-6 `to_prompt_dict()` 内联在状态模型里 | **本阶段暂不动**（避免与 Stage 8 的提示词升级撞车），已在文件里标注 |

### ★关键设计决策：只增不删，别名保兼容★

任务书把 `UserState` 叫 `PoseState`、把 `ConfidenceState` 叫 `UncertaintyState`。
直接改名会打断 ~30 处调用点（`state.user.position` 遍布 decision / tools / prompt / banner / 测试）。

采取的方案是**类名升级 + 别名兜底 + 字段名保持不变 + 只读属性转发**：

```
PoseState            ← 新类名
UserState = PoseState            # 别名，isinstance 与类型标注继续成立
SpatialState.user: PoseState      # 字段名不变 → 所有 state.user.x 不用改
SpatialState.pose → user          # 只读属性，可用新命名访问
```

同理：`UncertaintyState` / `confidence` / `uncertainty`，`SemanticState` / `SemanticScene`，
`CameraFrameMetadata` / `CameraState`。

**副产品**：因为只加默认值字段，v0.2 的所有构造调用与断言原样成立 ——
40 项自检在改完 `spatial_state.py` 后**直接零改动通过**，再补的新用例。

### ★踩坑记录（本阶段真正花了时间的两处）★

1. **不能用 `@computed_field`**。
   `tests/selftest.py:743` 的端到端用例会做
   `SpatialState.model_validate(state.model_dump())` 往返校验。
   `computed_field` 会出现在 `model_dump()` 里、却不是合法入参，而模型是
   `extra="forbid"` ⇒ 往返立刻报 `1 validation error for SpatialState / Extra inputs are not permitted`。
   **改用普通字段 + `model_validator` 补全**（`overall_confidence` 默认 `None`，构造后必定非 `None`）。
   → 这也是为什么先读测试再写模型，而不是写完了再跑。

2. **运行时才发现的两个指标缺陷**（静态读代码看不出来）：
   - `route_confidence` 第一版用**累计** `replan_count` 直接扣分，
     跑几十轮后永远钉在 0.1 地板、再也不回升 —— 等于给了个"重规划过一次就永久不信这条路"的假信号。
     改为**按偏离 + 次数封顶**，现在能回到 0.7。
   - 前方被**墙**挡住时 `blocked_regions` 为空（墙不是 `Obstacle` 条目，
     它来自窄视场墙体扫描）⇒ 摘要退化成光秃秃的"正前方不可通行"，
     用户既不知道多远、也不知道被什么挡住。补了一条 `source_type="wall"` 的阻挡后变成
     「正前方不可通行（0.6 米内有 chair），建议向右」。

### 测试结果

```
python main.py --selftest          →  通过 45 / 45 | 失败 0 | 耗时 6.0s   （离线，零真实请求）
python tools/ui_smoke_test.py      →  通过 34 / 34
```

新增 5 项用例与实测输出：

| # | 用例 | 实测结果 |
|---|---|---|
| 41 | v0.3 状态契约（别名 + 往返幂等 + 新字段） | 别名兼容 / 往返幂等 / 9 个新字段就位 / 5 维置信度 |
| 42 | 五维不确定性（自动补全 + 权重正确） | 加权补全 = 0.65；显式值保留；往返幂等；5 维齐全 |
| 43 | 可行动性推导（AffordanceState） | 受阻→不可直行/建议向左/critical；两侧窄→无建议；通畅→可直行 |
| 44 | 摄像头新鲜度三态（none / fresh / stale） | 三态判定正确，且 stale 不误标可用 |
| 45 | 新增自洽性校验（高危地形 / 零宽度） | 台阶/坠落必须配 ≥high 风险；宽度为 0 不得可直行 |

**服务端实测**（`GET /api/state`，服务已加载新代码）：

```json
"affordance": {"can_move_forward": false, "passable_width_m": 3.91,
               "preferred_direction": "right",
               "blocked": [{"direction":"front","distance":0.58,"type":"chair","prefer":"right"}],
               "summary": "正前方不可通行（0.6 米内有chair），建议向右"}
"confidence": {"localization":0.9, "perception":1.0, "semantic":0.75, "route":0.7, "overall":0.86}
"camera":     {"image_available":false, "age_s":null, "freshness":"none"}
```

提示词渲染长度 221 → 252 字符（新增 `[可行动]` 行 + 置信度扩到 5 维）：

```
[可行动] 不可直行  可通行宽度=3.16m  建议方位=left
[置信度] 定位=0.9 感知=0.86 语义=0.91 路线=0.1 综合=0.73
```

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `localization_confidence` 仍是配置常量 0.9（低置信度分支仍不可触发） | **Stage 4**（融合层做真实估计） |
| `stairs` / `dropoff` 恒为 `False`（模拟地图里确实没有这两种地形） | Stage 4（接深度相机/LiDAR 后判定） |
| `AffordanceState` 只做了**几何层**推导，没有结合语义物体与历史记忆 | **Stage 5**（`affordance_world.py`） |
| `risk` 仍留在 `SpatialState` 里由融合层预判 | **Stage 7**（搬进 Safety Layer） |
| `to_prompt_dict()` 仍在状态模型内联 | Stage 8 |

### 下一阶段

**Stage 3：Sensor Adapter Layer** —— 建立 `sensors/base.py` + `sensors/simulated/` + `sensors/future/`，
把 `StateManager` / `AgentTools` / `SpatialAgentSystem` 三处对具体模拟器类的硬依赖
（审计报告 P0-2）收敛到一个 `SensorProvider` 抽象之后，让 Agent 侧彻底看不见数据来源。

**验收标准**：40+ 项自检与 34 项前端冒烟保持全绿；`state_manager.py` 与 `tools.py` 的顶层
`from simulator.*` 全部消失；改用 `SimulatedProvider` 装配后系统行为逐轮一致。

---

## Stage 3：Sensor Adapter Layer（传感器适配层）

### 修改文件

| 文件 | 改动 |
|---|---|
| `sensors/base.py` | **新增**（抽象契约）：`SensorProvider` / `WorldStepper` / `CameraProvider` 三个协议；`RawPerception` / `WorldReference` 两个数据类；`SensorKind` / `SensorStatus` / `SensorHealth` / `MapSnapshot` / `MapObjectInfo` |
| `sensors/simulated/environment.py` | **新增**：`SimulatedEnvironment` —— 把 map/nav/obstacles/sensors 四个模拟器收拢成一个对象，实现 `WorldStepper`（`advance_environment` / `advance_user` / `replan` / `reset` / `arrived` / `world_reference` / `debug_ground_truth` / `stats`） |
| `sensors/simulated/provider.py` | **新增**：`SimulatedProvider(SimulatedEnvironment)` —— 实现 `SensorProvider`（`read_pose` / `read_navigation` / `perceive` / `scan_wall_ahead` / `scan_sides` / `measure_depth` / `map_snapshot` / `zone_at` / `floor` / `confidence` / `health` / `sensor_stats`）。一个对象同时满足两个协议 |
| `sensors/simulated/__init__.py`、`sensors/__init__.py`、`sensors/future/__init__.py` | **新增**：导出与"阶段三接真机"的接口约定（只写契约不写假桩） |
| `spatial/geometry.py` | **新增**：把 `bearing_deg` / `angle_delta` 从 `simulator/navigation_simulator.py` 抽出来。融合层为了算一个夹角而 import 模拟器，是审计里"耦合"的最典型症状 |
| `simulator/navigation_simulator.py` | 改为从 `spatial.geometry` 导入并 **re-export**，旧调用点（含自检）不破 |
| `spatial/state_manager.py` | **重写装配方式**：`__init__(cfg, map_sim, nav, obstacle_sim, sensor_sim, ...)` → `__init__(cfg, provider, world, camera)`；`_build_environment()` / `_build_semantic()` 改为吃 `RawPerception`；`_route_confidence()` 改为吃 `NavigationState` 并由本层回填；新增 `reset()`；`stats()` 增加 `health` |
| `agent/tools.py` | **重写装配方式**：`__init__(cfg, map_sim, nav, obstacle_sim, ...)` → `__init__(cfg, provider, stepper, ...)`；工具读 `stepper.world_reference()`（不加噪）而非直接摸模拟器 |
| `agent/agent_core.py` | **重写装配方式**：改为构造 `SimulatedProvider` 一个对象；`step()` 改为 `env.advance_environment()` / `env.advance_user()`；保留 `system.map/nav/obstacles/sensors` 四个**兼容属性**（供 main.py / api / 旧自检读取）；新增 `provider` / `stepper` 注入点 |
| `spatial/__init__.py` | `CameraProvider` 的惰性导出源从 `spatial.state_manager` 改为 `sensors.base`（摄像头是硬件契约，不属于融合层） |
| `tests/selftest.py` | 新增 `fresh_env()` 装配入口；`fresh()` 保持 v0.2 的 7 元组返回（4 个内部模拟器原样摊出，供"构造场景"类用例继续使用）；4 处构造调用改为新签名；新增 **2 项**用例（46~47）。45 → **47 项** |

### 修改原因（逐条对应审计报告的技术债编号）

| 技术债 | 本阶段的处置 |
|---|---|
| **P0-2 三处硬耦合模拟器** —— `agent_core` / `state_manager` / `tools` 各自 `new` 出模拟器、各自 `from simulator import`，换硬件要改三个文件 | 收敛为 **一个装配点**（`agent/agent_core.py` 的 `SpatialAgentSystem.__init__`）+ **两个协议**。融合层与工具层的 `from simulator.*` 归零 |
| **P0-3 融合层不认识"传感器健康与否"** | `SensorProvider.health()` 给出每个通道的 `SensorStatus`（`ok` / `degraded` / `lost` / `absent`）。这是 Stage 6 `CAMERA_LOST` / `UWB_LOST` 事件的直接前置条件 |
| 感知与"推进世界"混在一起 | 拆成 `SensorProvider`（只读）与 `WorldStepper`（推进）。真机下后者是空实现（用户自己走路），这就是必须拆的理由 |
| 6.3 `world_model` 之类的调试真值出口散落 | 收拢为 `WorldStepper.debug_ground_truth()`，命名里带 `debug` 让误用一眼可见；另加 `world_reference()` 作为"不加噪、供工具查询"的正式出口 |

### ★三个必须说清的设计决策★

**1. 为什么 `SimulatedProvider` 同时实现两个协议，而不是拆成两个对象？**

仿真里感知与推世界用的是同一套配置、同一套 RNG、同一份地图。硬拆成
`Provider` + `Stepper` 两个类，必然要求"两个对象共享同一个 nav"，反而更耦合。
两个**协议**本身是分开的 —— 接真机时 `UwbProvider` 只实现前者，
`NoopStepper` 只实现后者（`advance_*` 全 pass）。

**2. 为什么 `perceive()` 不负责测量，扫描要单列成 `scan_*()`？**

因为**对 provider 的调用顺序 = 随机数消耗顺序**。v0.2 的逐障碍 `lidar_measure()`
夹在"前墙扫描"与"左右侧扫描"之间：

```
v0.2:  uwb → imu → imu → wall_ahead → [obs1..obsN] → side_left → side_right
```

如果把三次扫描并进一个 `perceive()`，顺序就变成
`wall → side → side → obs1..obsN`，噪声分配错位 ⇒ 同一 seed 下轨迹与 v0.2 不一致。
所以 `perceive()` 只做**候选枚举**（纯读取、零随机数），扫描与测距各自单列，
由融合层按 v0.2 的原顺序调用。这条不变式已写成自检用例 46 的一部分。

**3. 为什么工具读 `world_reference()` 而不是 `read_pose()`？**

工具的回答（"我在哪、还有多远"）是程序内部记账用的几何量，再走一遍 UWB 噪声没有意义。
更关键的是**可复现性**：工具调用次数由大模型决定；如果每调一次工具就消耗一次传感器随机数，
"模型多调一次工具"就会改变整条仿真轨迹，回归测试失去意义。
`world_reference()` 明确标注 **不加噪、不消耗随机数**，并由用例 46 用
`random.Random.getstate()` 前后对比钉死。

### ★踩坑记录（本阶段真正花时间的三处）★

1. **`stats()` 同名冲突导致世界统计被顶掉。**
   `SensorProvider` 原本也有个 `stats()`（传感器统计），而 `WorldStepper` 的 `stats()`
   是"世界运行统计"（到达/进度/障碍数…）。`SimulatedProvider` 两个协议都要满足，
   后定义的那个把先定义的覆盖掉 ⇒ `SpatialAgentSystem.stats()` 直接
   `KeyError: 'arrived'`，而且报错点在 REST 处理器里、只留下一行
   `Agent 通道异常: 'arrived'`，非常难定位。
   → 传感器那个改名为 `sensor_stats()`，并在两边都写了注释说明为什么不能同名。

2. **抽出 `spatial/geometry.py` 是"看起来多余"但必须做的一步。**
   一开始想着"夹角函数留在 navigation_simulator 就行，re-export 也够用"，
   但那样 `state_manager` 仍然得写 `from simulator.navigation_simulator import angle_delta`
   —— 分层铁律的自检会红。纯数学就该住在纯数学的模块里。

3. **兼容属性是必要的妥协，不是偷懒。**
   `main.py`（终端横幅）、`api/websocket_server.py`（前端小地图要 `nav.local_path`
   与 `obstacles.obstacles` 的**绝对坐标**）、以及 20+ 条旧自检断言，都直接读
   `system.map` / `system.nav` / `system.obstacles` / `system.sensors`。
   直接删掉会连带推翻前端与自检。折中方案是在 `SpatialAgentSystem` 上保留四个
   转发属性，并注明"**新增业务逻辑不要用**，业务代码请用 `self.provider` / `self.env`"，
   同时把"融合层/工具层不得 import simulator"写成硬自检（用例 47）守住边界。

### 测试结果

```
python main.py --selftest          →  通过 47 / 47 | 失败 0 | 耗时 6.0s
python tools/ui_smoke_test.py      →  通过 34 / 34
python tools/e2e_test.py           →  全部通过（图片流→SpatialState→多模态模型→导航行动）
```

新增 2 项用例：

| # | 用例 | 实测结果 |
|---|---|---|
| 46 | 传感器适配层契约（双协议 + 无副作用 + 健康度） | 两协议齐备 / 参考读数零随机数消耗 / 健康度 `absent` ≠ `lost` |
| 47 | 分层铁律（融合层/工具层不得依赖 simulator） | 融合层 / 工具层 / Agent 核心已与 simulator 解耦，装配点唯一 |

**★核心证据：重构前后行为逐轮一致★**

这是纯重构（不改行为）唯一的验收方式 —— 把 120 轮 `SpatialState` + `Action`
序列化成 JSON 逐字段比对（工具：`/tmp/snap_states.py`）：

```
重构前基线 /tmp/baseline_pre_stage3.json  （371 KB，120 轮）
重构后      /tmp/after_stage3.json

逐轮严格比对（剔除 timestamp/age_s/elapsed）：
  不一致 0 / 120 轮
  ✅ 120 轮状态与 Action 逐字段完全一致 —— 本次重构行为零改变
```

> 为什么剔除 `timestamp`：`datetime.now()` 两次运行必然不同，它不是被重构的对象。
> 剔除后 120 轮**零差异**，说明位姿、障碍、风险、可行动性、Action 全部逐字段一致。

**分层验收（用例 47 钉死的铁律）**：

```
$ 全仓搜索 `^\s*(from|import)\s+simulator`
  tests/selftest.py            ← 测试，允许（要直接构造场景）
  sensors/simulated/environment.py   ← 适配层实现，这里本就该有
  simulator/*.py               ← 模拟器内部互相引用，正常

  spatial/state_manager.py   ✅ 无
  agent/tools.py             ✅ 无
  agent/agent_core.py        ✅ 无
  sensors/base.py            ✅ 无（抽象层零模拟器依赖）
```

**服务端实测**（`GET /api/stats`，键与 v0.2 完全一致）：

```json
{"system":{"tick":2,"elapsed":2.0,"arrived":false,"progress":0.053,
           "distance_to_goal_m":39.06,"replans":2,
           "map":{"name":"实验室-1F","floor":1,"size":"24x32m","grid":"128x96@0.25m",
                  "walkable_ratio":0.352,"zones":5,"objects":8,"landmarks":5},
           "sensors":{"uwb_noise_m":0.15,"heading_noise_deg":2.0,"lidar_noise_m":0.05,
                      "lidar_range_m":5.0,"drop_rate":0.0,"localization_confidence":0.9, ...}}}
```

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `system.map` / `system.nav` 等兼容属性仍在（前端与旧自检依赖） | Stage 9~11 逐步把调用点迁到 `world_reference()` / `debug_ground_truth()` 后再删 |
| 风险判定仍写在 `state_manager._evaluate_risk()` | **Stage 7**（搬进 Safety Layer） |
| `SensorStatus` 已产出但还没有消费者（没人对 `lost` 做反应） | **Stage 6**（事件引擎消费它产生 `CAMERA_LOST`） |
| 融合层仍是"一个 `StateManager` 类干完所有融合" | **Stage 4**（拆出 `fusion/` 子模块 + 置信度估计） |

### 下一阶段

**Stage 4：State Fusion** —— 建立 `fusion/state_fusion.py` + `confidence.py` + `freshness.py`：

1. 把融合逻辑从 `state_manager` 拆成可单独测试的算子；
2. **修掉 P0-7**：`localization_confidence` 不再是配置常量 0.9，
   改为由"UWB 噪声参数 + 近期定位残差 + 采样连续性"估计出来的真值；
3. 让 `stairs` / `dropoff` 有真实来源（当前恒 `False`）。

**验收标准**：47 项自检保持全绿；新增融合算子单测；
`localization_confidence` 在定位退化时**确实会下降**（可构造低置信度分支并触发降级行为）。

---

## Stage 4：State Fusion（状态融合层）

### 修改文件

| 文件 | 改动 |
|---|---|
| `fusion/__init__.py` | **新增**：PEP 562 惰性导出（`state_fusion` 依赖 `spatial.world_model`，急切导入会成环） |
| `fusion/freshness.py` | **新增**：`ChannelFreshness`（fresh/stale/**missing** 三态）、`FreshnessReading`（含 `usable`）、`FreshnessPolicy`（阈值全部可配） |
| `fusion/confidence.py` | **新增**：`ConfidenceEstimator` —— 用「传感器噪声纸面值 + **实测残差** + 新鲜度」估计定位/感知/语义/路线/相机五条置信度，并给出「为什么不确定」 |
| `fusion/state_fusion.py` | **新增**：`StateFusion` —— 从 `StateManager` **原样搬来**的融合组装逻辑（含 `_build_environment` / `_build_semantic` / `_camera_state` / `_evaluate_risk` / `_build_affordance` / `_blocking_ratio`） |
| `spatial/state_manager.py` | **重写为薄门面**（659 行 → 约 180 行）：只做属性/方法转发，不再有融合逻辑 |
| `spatial/spatial_state.py` | `PoseState` 增 `valid`/`age_ms`；`EnvironmentState` 增 `valid`/`age_ms`；`UncertaintyState` 增 `camera_freshness`/`uncertainty_reason`（5 → 7 字段）；`to_prompt_dict()` 增相机维度与原因；新增第 4 条自洽性校验 |
| `agent/agent_core.py` | `state_manager.build(...)` 增加传递 `dt`（置信度残差要用"速度×dt"作预期位移） |
| `config/config.yaml` | 新增 `fusion.freshness.*` 与 `fusion.confidence.*` 两个配置段 |
| `tests/selftest.py` | 用例 41 断言从「5 维」改为「7 字段 + 12 个新字段就位」；新增 **4 项**用例（48~51）。47 → **51 项** |

### 修改原因（逐条对应审计报告的技术债编号）

| 技术债 | 本阶段的处置 |
|---|---|
| **P0-7 定位置信度是配置常量（恒 0.9）** —— 低置信度分支是死代码，任务书第 14 节第 6 项场景无法构造 | `ConfidenceEstimator` 用三条证据估计：① 噪声纸面值；② **实测残差**（相邻帧位移 − 速度×dt 预期位移，除以 √2·噪声）；③ 新鲜度。实测分布 **0.68~0.95（中位 0.79，25 种取值）**，把 UWB 噪声调到 1.5m 时最低 **0.341 < 0.4** ⇒ 「询问用户」分支**真的会触发** |
| 5.1⑨⑩ 数据缺 freshness / confidence / valid | `FreshnessPolicy` 三态 + `PoseState.valid`/`age_ms` + `EnvironmentState.valid`；**过期 ≠ 缺失**（前者降置信度并可能报警，后者静默降级） |
| 5.1⑪ `UncertaintyState` 缺"为什么不确定" | 新增 `uncertainty_reason`（如「UWB 噪声偏大(1.50m)；摄像头推流中断」），随状态与提示词一起给模型 |
| P1-6 融合层是"一个类干完所有融合"，算子无法单独测试 | 拆成 `freshness`（纯函数）/ `confidence`（有状态估计器）/ `state_fusion`（按顺序组装）三层；`ConfidenceEstimator` 与 `FreshnessPolicy` 现在都能**脱离模拟器**直接单测 |

### ★三个必须说清的设计决策★

**1. 为什么必须用「实测残差」，不能只用配置里的噪声参数？**

`SensorSimulator.localization_confidence()` 是 `1 - uwb_noise/1.5` —— 它读的是**纸面参数**，
不是**现场表现**。真实系统里 UWB 会因为基站遮挡、多径、金属反射而漂移，
而这些都不会改变配置文件里的 `uwb_noise_m`。

残差项（`|实测位移 − 速度×dt| / (√2·噪声)`）抓的是另一类错误：**跳变与漂移**。
用例 49 直接喂了 6 帧"速度声称 0 却每帧瞬移 5 米"的观测，置信度立刻从 0.95 掉下来 ——
这类证据是噪声参数永远给不出的。

**2. 为什么 `camera_freshness` 不进综合置信度的加权？**

`_CONFIDENCE_WEIGHTS` 仍是 4 项（定位 .30 / 感知 .30 / 语义 .20 / 路线 .20）。视觉是
**按需使用**的通道（主动感知触发时才要图，见 Stage 10）；把它计入综合值，
"没接摄像头"会一路把整体置信度压低，反而掩盖真正影响安全的两维（定位/感知）。
所以它单独成维度、单独判断（`camera<=0.3` 即视为"看不清"）。

**3. 为什么 `state_manager.py` 保留而不是直接删掉？**

`system.state_manager.camera = receiver`（服务启动后热插拔）、`sm._build_affordance(env)`
（自检直接用）、`sm.build(t, elapsed)`（测试用）这些写法散落在 api / 自检 / 工具里。
逐个改调用点只是把改动面铺大，不增加任何能力 —— 而且 `session_manager._build_affordance`
这种名字一旦消失，就等于把对应的**测试一起删了**。门面让「重构」与「调用点迁移」解耦，
同时用用例 51 钉死"门面必须是真的转发、不是两份实现"。

### ★踩坑记录★

1. **配置文件里的键名 ≠ 内部通道名。**
   `fusion.freshness` 段为可读性写成 `pose_stale_s`，而内部按通道名 `pose` 索引。
   第一版直接把 `pose_stale_s` 塞进字典，于是 `threshold("pose")` 仍然返回默认值 ——
   **改了配置但完全没生效**，而且不报错。现在两种写法都接受（剥掉 `_stale_s` 后缀），
   并由用例 48 用"把阈值放宽到 5s 后 1s 前的读数应重新判为新鲜"来防回归。

2. **`ever_seen` 的语义一开始写反了。**
   第一版 `timestamp is None` 时返回 `FRESH if not ever_seen else STALE` —— 即
   "从来没读到过"被判成**新鲜**。这在真机上等于"没有这个传感器 = 数据可用"，
   是最危险的默认值。改成 `MISSING if not ever_seen else STALE`。

3. **`dt` 必须从主循环传到融合层。**
   残差 = `|位移 − 速度×dt|`。主循环一次 `step(1.0)` 而 dt 写死 1.0 时看不出问题，
   但一旦改成 20Hz 的快速循环（Stage 7），dt=0.05 却仍按 1.0 算预期位移 ⇒
   **正常行走被判成"定位漂移 0.95 米"**。所以 `build()` 现在显式接收 dt。

### 测试结果

```
python main.py --selftest          →  通过 51 / 51 | 失败 0 | 耗时 7.6s   （离线，零真实请求）
```

新增 4 项用例：

| # | 用例 | 实测结果 |
|---|---|---|
| 48 | 数据新鲜度三态 + 阈值可配 | 三态判定正确 / 阈值改配置真的生效 / 与 `camera.stale_after_s` 对齐 |
| 49 | **置信度估计（P0-7）** | 估计值随数据变化（极差 0.209，25 种取值）/ 跳变显著降分 / `mode:fixed` 可回退到 0.90 |
| 50 | **低定位置信度可构造并触发降级** | UWB 噪声 1.5m → 最低 0.341 → 触发「定位信号不太好，您现在是在走廊里吗？」 |
| 51 | 融合层分层与门面转发 | `fusion/` 零模拟器依赖 / 门面确为转发 / camera 热插拔可达实现层 |

**★核心证据：决策行为逐轮未变★**

Stage 4 是"搬代码 + 加估计"，**不是改行为**。仍用 120 轮状态 + Action 逐字段比对：

```
比对 120 轮（剔除 timestamp / age_s / elapsed）：
  confidence    差异 120 轮   ← 预期：新增 camera_freshness / uncertainty_reason，loc 0.9 → 估计值
  environment   差异 120 轮   ← 预期：新增 valid / age_ms（纯加法）
  user          差异 120 轮   ← 预期：新增 valid / age_ms，source simulated → fused，confidence → 估计值
  _action       差异   0 轮   ← ★决策行为完全未变★
```

对照明细（第 0 轮）：

```
A(Stage3) confidence = {loc:0.9, perception:1.0, semantic:0.8, route:0.9, overall:0.91}
B(Stage4) confidence = {loc:0.95, perception:1.0, semantic:0.8, route:0.9, overall:0.925,
                        camera_freshness:0.0, uncertainty_reason:"无摄像头数据"}
A(Stage3) user = {confidence:0.9,  source:"simulated"}
B(Stage4) user = {confidence:0.95, source:"fused", valid:true, age_ms:0.0}
```

位姿、朝向、障碍、风险、可行动性、导航**逐字段一致**；Action 序列零差异 ⇒
"只加了估计维度，没有动任何决策" 是被证明的，不是被声称的。

**实跑验证**（`--no-llm --brief --ticks 8 --seed 7`）：正常直行、沉默、按指令播报 1 条，
与 Stage 3 表现一致。

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `stairs` / `dropoff` 仍恒 `False`（模拟地图里确实没有这两种地形） | 保留为"接深度/LiDAR 后由深度算子判定"，不在模拟器里造假 |
| 感知/语义两维的实际权重仍是"provider 纸面值 × 新鲜度"，未引入残差 | Stage 5（世界模型给出历史一致性证据后才有意义） |
| `risk` 仍留在 `SpatialState` 由融合层产出 | **Stage 7**（干预决策搬进 Safety Layer，`risk` 保留为状态字段） |
| `world` 仍是单一 `WorldModel` 类，未按 当前/短期/长期 分层 | **Stage 5** |

### 下一阶段

**Stage 5：Spatial World Model** —— 建立 `world_model/`（世界模型 / 短期空间记忆 / 对象记忆 /
轨迹记忆 / 语义世界 / 可行动性世界）与 `maps/`（地图接口边界），
让 Stage 4 留下的两个尾巴有归宿：
① 感知/语义置信度能用"历史一致性"作证；② `risk` 之外的状态也有"当前/短期/长期"三层记忆。



---

## Stage 5：Spatial World Model + 地图接口

### 修改文件

| 文件 | 改动 |
|---|---|
| `maps/metric_map.py` | **新增**：度量地图 —— 边界/栅格/区域/可通行性（只答几何问题） |
| `maps/semantic_map.py` | **新增**：语义地图 —— 长期物体清单，blocking 与非 blocking 严格分开 |
| `maps/navigation_graph.py` | **新增**：导航图 —— 路点拓扑/剩余路点/最近路点（不做寻路） |
| `maps/affordance_map.py` | **新增**：可行动性经验库 —— 证据累积/反向抵消/TTL 淘汰/刷新不清空 |
| `maps/map_provider.py` | **新增**：四张地图的统一装配入口 + `area_description()` 组合查询 |
| `maps/__init__.py` | **新增**：惰性导出 |
| `world_model/world_model.py` | **新增**：门面 —— 按时间尺度分层（当前/短期/长期/经验），保留全部 v0.2 属性 |
| `world_model/trajectory_memory.py` | **新增**：轨迹记忆（打转检测/航向变化/走过区域） |
| `world_model/object_memory.py` | **新增**：对象记忆 —— 空间聚合 + 稳定命名（chair_001）+ 持续性 + TTL 淘汰 |
| `world_model/spatial_memory.py` | **新增**：短期状态记忆 —— 前向距离趋势/风险峰值/数值查询 |
| `world_model/semantic_world.py` | **新增**：语义世界 —— 地图知识 × 实时观察（带 TTL 的观察缓存） |
| `world_model/affordance_world.py` | **新增**：可行动性世界 —— 几何结论 + 历史佐证（持续存在/长期问题/接触倒计时） |
| `world_model/__init__.py` | **新增**：惰性导出 |
| `spatial/world_model.py` | **重写为兼容转出**（200 → 20 行）：`from spatial.world_model import WorldModel` 继续可用 |
| `fusion/state_fusion.py` | `build()` 末尾新增 `world.observe_state(state)`（第 12 步，纯记账） |
| `agent/agent_core.py` | 装配时 `world.bind_map(provider.map_snapshot())`；`reset()` 改用 `world.clear_runtime()`（保留地图绑定） |
| `tests/selftest.py` | 新增 **4 项**用例（52~55）。51 → **55 项** |

### 修改原因（逐条对应审计报告的技术债编号）

| 技术债 | 本阶段的处置 |
|---|---|
| **P0-5 世界模型只是 memory，无持续空间认知** | 按任务书第七节拆成 当前状态（fusion 给）/ 短期记忆 / 长期知识 / 运行期经验 四层；每层独立可单测 |
| P1-3 对象无跨帧持续性（每帧都是"新椅子"） | `ObjectMemoryStore`：oid 索引优先 + 0.5m 网格兜底；同一物体重复观察**不再算新发现** |
| P1-4 地图数据驱动但接口散落 | `MapProvider` 四张地图一个入口；换 UE5/BIM 只需产出同样的 `MapSnapshot` |
| 任务书第 11 节"地图架构提前升级" | 度量/语义/拓扑/经验四层就位；经验层支持累积与抵消 |

### ★三个必须说清的设计决策★

**1. 为什么"经验"（AffordanceMap）不放在占用栅格里？**

占用栅格描述**几何**，本层描述**经验**："这个位置连续三个来回都被椅子挡住"是统计事实，
可累积、可被反向证据抵消、可 TTL 淘汰。塞进栅格会把"一次误报"永久固化成"这里是墙"。
用例 54 验证了 3 次阻挡证据 → 判长期阻挡 → 2 次通行证据 → 抵消回"非长期阻挡"。

**2. 为什么 `AffordanceWorld.notes()` 返回字符串列表，而不是改 `SpatialState.affordance`？**

fusion 的 affordance 是**几何结论**（此帧、确定、必须快）；AffordanceWorld 是**历史佐证**
（跨帧、带不确定性）。两者混在一个字段里，模型分不清"现在被挡"和"这里常被挡"。
分开后 prompt 里各自独立成行，纯记账不改变决策输入 —— 这是本阶段 120 轮零差异的前提。

**3. 为什么 `reset()` 从 `world.__init__()` 改成 `clear_runtime()`？**

旧写法把整个世界模型推倒重建，会连 `bind_map()` 装载的静态知识一起清掉。
`clear_runtime()` 只清轨迹/对象/短期/观察/经验五类运行期记忆，地图绑定保留。
用例 55 钉死：reset 后 `trail==0` 且 `maps is not None`。

### ★踩坑记录★

1. **`observe_obstacles` 的事件口径不能变。**
   v0.2 在"首次发现某物体"时写 `events` 并累计 `obstacle_encounters`。
   新实现必须**只在真正的新对象**时返回名字 —— 否则事件被刷爆、
   `summarize()` 变形、端到端断言（播报条数）连锁红。
   用例 52 用"同一椅子观察 3 次，new 只在第 1 次非空"钉死。

2. **自检用例 24 依赖 `w.tracks[1].trend()`。**
   保留 `tracks` 属性转发到 `ObjectMemoryStore.tracks`（同一 dict 对象），
   且 `TrackStats` 从 object_memory 导出 —— v0.2 断言零改动通过。

### 测试结果

```
python main.py --selftest          →  通过 55 / 55 | 失败 0 | 耗时 7.6s
行为快照（120 轮，剔除时间字段）     →  与 Stage 4 完全一致（不一致 0 轮）
```

新增 4 项用例：

| # | 用例 | 实测结果 |
|---|---|---|
| 52 | 对象记忆 | 跨帧持续 / 稳定命名 chair_001 / TTL 淘汰 / 无 oid 网格聚合 |
| 53 | 短期空间记忆 | closing 趋势 / 最小距离 / blocked 记录 / 窗口淘汰 / 风险峰值 |
| 54 | 地图四层接口 | 度量/语义/拓扑/经验齐备；经验累积可抵消；刷新不清经验 |
| 55 | 世界模型门面 | v0.2 属性齐备 / 五层统计 / reset 保地图 / 包入口一致 |

**为什么 120 轮零差异是可能的**：本阶段对决策输入（SpatialState 字段、Action 计算）
**一个字节都没改**。世界模型的三条新写入（`observe_state` / `bind_map` /
`clear_runtime`）都是纯记账，不回写状态、不消耗随机数、不参与规则引擎。

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `world.affordance_notes()` 已产出但还没有消费者（没人读它） | **Stage 8**（context_builder 进提示词）+ 工具 `query_world_model` |
| `SpatialMemory` 只在 `observe_state` 里写，趋势查询还没进决策 | **Stage 6**（事件引擎用 closing 趋势产生 OBSTACLE_APPROACHING） |
| 语义世界观察缓存空的（模拟器语义全来自 map，无 VLM 观察） | Stage 10（主动感知后由 VLM 观察填充） |

### 下一阶段

**Stage 6：Event Engine** —— `events/`（event_types / event_detector / event_bus /
event_history）。消费 Stage 4 的 `SensorHealth` 与 Stage 5 的对象持续性/趋势，
产出 15 类事件，带 debounce/cooldown/状态迁移去重。

---

## Stage 6：Event Engine（事件引擎）

> 目标链路：`Sensor → Adapter → Fusion → World Model → **Event Engine** → Safety → Cognitive`
> 本阶段只解决一件事：**把 v0.2 的"状态日志"变成 v0.3 的"状态迁移事件流"。**

### 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `events/__init__.py` | 新增 | PEP 562 惰性导出 `EventEngine` / `EventBus` / `EventDetector` / `EventHistory` / `AgentEvent` / `EventType` |
| `events/event_types.py` | 新增 | `EventType`（**15 类**）、`Severity`（4 级）、`DEFAULT_SEVERITY`、`DEFAULT_COOLDOWN_S`、`AgentEvent`（Pydantic，含 `event_id` / `key()` / `is_critical()` / `describe()` / `as_dict()`） |
| `events/event_bus.py` | 新增 | `EventBus`：订阅/发布 + **冷却闸**（同 `key()` 在冷却期内被吞掉并计入 `suppressed_count`；`critical` 豁免、**被吞也记账**）+ `history` / `stats()` |
| `events/event_detector.py` | 新增 | `EventDetector`：**状态迁移 + 边沿检测**，6 个只读检测器 |
| `events/event_history.py` | 新增 | `EventHistory`：`add_all` / `recent` / `since_tick` / `by_type` / `has_critical` / `summary_for_prompt` |
| `events/event_engine.py` | 新增 | `EventEngine`：把 detector → bus → history 串起来，对外只暴露 `process(state, t)` / `subscribe` / `reset` / `stats` |
| `agent/agent_core.py` | 修改 | `__init__` 建 `self.events = EventEngine(cfg, self.world)`；`step()` 在融合之后调 `self.events.process(state, self.elapsed)`；`reset()` 一并重置事件引擎 |
| `config/config.yaml` | 修改 | 新增 `events:` 段（`history_len`、`cooldown_s` 按事件类型逐项配置），取代代码里写死的冷却表 |
| `tests/selftest.py` | 修改 | 新增用例 **56 / 57 / 58**（47→58） |

### 修改原因（对上审计报告的技术债）

| 审计技术债 | 本阶段如何还 |
|---|---|
| **T-A1 状态当日志**：前方有椅子这件事每秒重新报告一次（提示词里塞满重复信息） | 事件化：`absent→present` 才发 `OBSTACLE_APPEARED`（一次），`present→present` 什么都不发 |
| **T-A2 无事件概念**：Agent 每轮盲目看全量状态，没有"发生了什么" | 15 类事件 + `Severity` + `subject`，Safety/Cognitive（Stage 7/8）终于有"触发源" |
| **T-C1 无去重/无冷却**：同一件事可以连续刷屏 | **三道闸**：① 边沿检测（检测器）② 冷却（事件总线，同 key 短期压制）③ 主体区分（椅子 A ≠ 椅子 B） |
| **T-D3 调试时"漏了什么"无从知道** | 被冷却吞掉的事件**必须记账**（`suppressed_count` + `stats()`），漏报可查 |
| **T-E2 危险被冷却吞掉** | `critical` 事件**豁免冷却**——宁可重复报警也不能吞掉危险 |

### ★三个必须说清的设计决策★

**① 边沿检测需要"上一帧"，所以首帧是基线而非事件源。**
最初的实现用 `state.tick > 2` 这个魔法数来"跳过启动瞬间"，被自检用例 56 当场打脸
（首帧该不该报"出现"这件事，代码和测试说不一致）。改成显式 `_baselined` 标志：
**第一帧只登记 `_seen_objects`、不发声**，从第二帧起 `absent→present` 才算"新出现"。
这不是冷却、也不是时间窗，语义上就是"没有参照系就谈不上变化"。
副作用：启动时视野里本来就有的（含地图存量）物体不会刷一排 `OBSTACLE_APPEARED`，
正好符合验收 Demo 的"正常直行时 Agent 保持安静"。
**注意**：这只对"障碍清单"记账生效——`HIGH_RISK` / `ROUTE_BLOCKED` 这类**安全迁移首帧照常发声**
（`front_clear` / `risk.level` 的初值就是参照系），安全底线不受影响。

**② 事件引擎是纯记账，绝不回写 SpatialState。**
`EventDetector` 的 6 个检测器全部**只读** state 与 world，不 `model_copy`、不消耗随机数。
这是 Stage 6 能做到「120 轮 Action 零差异」的根本原因——事件流是新增的下游消费者，
不是决策输入。**事件流要到 Stage 7 才第一次成为决策输入**（那时零差异会被打破，属预期）。

**③ 检测器只检测、不决策、不播报。**
"要不要说、怎么说"是 Interaction Policy（Stage 8）的事。本阶段刻意连一句面向用户的话
都不生成（`description` 是给日志和提示词用的，不是 TTS 文本）——职责边界写进 docstring。

**④ 冷却时长必须来自配置，不能写死在代码里。**
`EventBus` 早就支持 `cooldowns` 入参，但最初 `EventEngine` 没传——等于配置能力是死的。
现在 `config.yaml:events.cooldown_s`（键 = `EventType` 的字符串值）逐项可调，
未知键只告警不抛异常（事件类型演进时，老配置文件不该让系统起不来）。
用例 56 新增一条断言：把 `HIGH_RISK` 冷却配成 `0.0` 后同 key 连发两次必须都通过——
否则就是"阈值没读配置"。

### ★踩坑记录★

- **`tick > 2` 魔法数**（见决策 ①）：看起来能跑，但语义不可解释，且测试无从验证。
  换成显式基线标志后，用例 56 才成为一条**真正的边沿检测测试**（基线帧 → 出现 → 持续不重报 → 离开成对）。
- **首帧基线标志必须在 `detect()` 末尾统一置位**，不能放在 `_detect_obstacles` 里：
  否则"本轮没有障碍物"时标志不置位，会导致后续每帧都当基线、永远不发 `APPEARED`。
- **`reset()` 必须清 `_baselined`**：否则系统重置后，新一轮的首帧会被当成"老会话的后续帧"，
  把重置后视野存量误报为"新出现"。用例 58 的 reset 断言覆盖了这条。
- **`CLEARED` 要求存在 > 1s**（`persisted > 1.0`）：一闪而过的测距抖动不应产生
  "出现→消失"事件对，否则又变回刷屏。用例 56 用 `t=1.0 → t=5.0` 验证成对。
- **低置信度事件带滞回 0.1**（`min_loc_conf` 上升 0.1 才算恢复）：
  否则置信度在阈值附近抖动时会反复报 `LOW_LOCALIZATION_CONFIDENCE`。
- **配置键名用 `EventType` 的字符串值而非属性名**：`str(EventType.X)` 走的是 `.value`
  （大写 `OBSTACLE_APPEARED`），YAML 里写小写 snake_case 会全部落到"未知键"告警上，
  表现为"配了但没生效"（与 Stage 4 freshness 键名踩的是同一类坑）。

### 数据流变化

```
Stage 5  :  ... → World Model → DecisionEngine（规则/LLM）
Stage 6  :  ... → World Model → EventDetector → EventBus(冷却) → EventHistory
                                              ↘（暂无人消费）
                                       ┌───────────────────────────────┐
                                       │ Stage 7 起：Safety 订阅事件总线 │
                                       └───────────────────────────────┘
```
`SpatialState` / `Action` 的计算链路**未变**——所以回归基线必须、也确实保持不变。

### 测试结果

```
python main.py --selftest          →  通过 58 / 58 | 失败 0 | 耗时 7.7s
行为快照（120 轮，剔除时间字段）     →  与 Stage 5 完全一致（不一致 0 轮）
行为快照（120 轮）                 →  与"改检测器之前"也一致（不一致 0 轮）
```

新增 3 项用例：

| # | 用例 | 实测结果 |
|---|---|---|
| 56 | 事件去重三道闸 | 基线帧不发声 / 出现只报 1 次 / 持续不重报 / 冷却生效且记账 / **冷却阈值确实读配置** / critical 豁免 / 主体区分 / 清除成对 |
| 57 | 事件覆盖面 | 偏离路线 / 到达 / 摄像头丢失与恢复 / 低定位置信度 / 地图-传感器冲突 均能产生；同冲突不逐轮重报 |
| 58 | 事件引擎接入运行时 | 40 轮产生 **44 个事件、7 种类型**（不刷屏）；订阅链路通；`reset()` 清空存档 |

**用例 58 的关键读数**：40 轮 44 事件 ≈ 1.1 事件/轮，而"每轮一个 `OBSTACLE_APPEARED`"
的旧行为会是 ≥40 个同类事件——去重确实生效（断言 `appeared <= 25` 有充足余量）。

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| 事件已产出但**无人消费**（`EventBus` 的订阅者列表是空的） | **Stage 7**（Safety Loop 订阅） |
| `OBSTACLE_APPROACHING` 依赖 `world.approaching_obstacles()`，已通 | 已闭环 |
| 事件进提示词（`summary_for_prompt`）但没有调用点 | **Stage 8**（`context_builder`） |
| `USER_QUESTION` / `REQUEST_VISUAL` 两类事件只有枚举，没有产生点 | **Stage 10**（主动感知 + 语音问答） |
| `EventHistory` 只在内存，进程退出即丢 | **Stage 9**（Replay 落盘） |

### 下一阶段

**Stage 7：Safety / Cognitive 双循环** —— `agent/orchestrator.py` +
`agent/safety/{safety_engine,risk_rules}.py`。
Safety Loop 订阅 `EventBus`（本阶段刚铺好订阅接口），**独立于 LLM 运行**。
硬验收：**LLM 睡 5 秒（模拟超时）期间，Safety 仍能正常产生并播报预警**（Gate 7）。

---

## Stage 7：Safety / Cognitive 双循环

> 任务书第八节称这是"最重要的架构升级之一"，Gate 7 是
> **"Safety Loop 独立于 LLM"**，强制验收标准是
> **"人为让 LLM sleep 5 秒，Safety Loop 必须继续工作"**。

### 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `agent/safety/__init__.py` | 新增 | PEP 562 惰性导出 |
| `agent/safety/risk_rules.py` | 新增 | `SafetyLevel`（4 级）/`SafetyAction`（4 种）/`SafetyThresholds`（全来自 config）/`SafetyRule`（**纯函数规则表**）/`DEFAULT_RULES`（**10 条**） |
| `agent/safety/safety_engine.py` | 新增 | `SafetyEngine.evaluate(state, events, t) -> SafetyVerdict`：每 tick 求值、逐规则冷却、EMERGENCY 豁免冷却、告警历史 + `alert_sink` 回调 |
| `agent/orchestrator.py` | 新增 | `CognitiveTrigger`（事件驱动 + 心跳 + 最小间隔）/`CognitiveLoop`（**独立工作线程** + 快照输入 + 结果 TTL）/`AgentOrchestrator`（三层合成：安全 > 认知 > 规则） |
| `agent/agent_core.py` | 修改 | `SpatialAgent` 拆出 `baseline()`（确定性）与 `think()`（认知）；`SpatialAgentSystem` 建 `state_lock` + `orchestrator`，`step()` 改为「安全快循环 + 非阻塞提交认知 + 规则基线」，新增 `close()` |
| `agent/tools.py` | 修改 | `AgentTools` 接受可重入 `lock`，`call()` 内部串行化「工具执行」（**网络等待期间不持锁**） |
| `agent/llm_client.py` | 修改 | ★P0 修复★ 显式 `max_retries=0`（SDK 默认 2）+ `total_deadline_s` 单轮总闸门；`stats()` 暴露这两个参数 |
| `events/event_engine.py` | 修改 | 新增 `summary()` / `active_since()`（供认知循环写提示词与触发判据） |
| `api/websocket_server.py` | 修改 | lifespan 收尾时调用 `system.close()`，避免 `--reload` 下认知线程堆积 |
| `config/config.yaml` | 修改 | 新增 `safety:` / `cognitive:` 两段 + `llm.max_retries` / `llm.total_deadline_s` |
| `tests/selftest.py` | 修改 | 新增用例 **59 / 60 / 61 / 62 / 63**（58→63） |

### 修改原因（对上审计报告的技术债）

| 审计技术债 | 本阶段如何还 |
|---|---|
| **P0-① 主循环实测只有 0.52 Hz**（配置 1.0）：`sleep(max(0, next_t-elapsed))` 被 LLM 阻塞吃掉 | 认知循环搬进工作线程；实测服务端 `last_tick_ms ≈ 1.99 ms`，LLM 单次 2.5~3.0 s 时主循环仍稳定 1 Hz |
| **P0-③ `OpenAI` 客户端未设 `max_retries`（实测默认 = 2）**：单轮 step 最坏 360 s | 显式 `max_retries=0` + `total_deadline_s=20` 总闸门；用例 62 钉死 |
| **T-B1 安全判断与大模型耦合**：v0.2 里"要不要停下"与"要不要说话"混在规则引擎的 if-else 里 | 抽出 `agent/safety/`：**纯函数规则表**，"停下"与"提醒"分离（`SafetyAction.STOP/ALERT`） |
| **T-B2 每帧都调 LLM**（`step()` 无条件 `agent.decide()`） | 事件驱动 + 5 s 心跳；实测 **12 轮快循环 : 3 次认知调用** |
| **T-B3 没有并发模型**：单线程里 LLM 一慢全线停 | 单写者 + 快照传递（见决策 ②） |

### ★四个必须说清的设计决策★

**① 为什么必须是"真线程"，而不是"把 timeout 调小"。**
单线程里 LLM 超时无论设多短，等待期间主循环都是停着的。停下来的安全层对视障用户
等于不存在——3 秒够走 3 米，够撞上去。而且 `time.sleep()` 与网络阻塞都会释放 GIL，
所以工作线程并不会拖慢快循环。**这是 Gate 7 唯一可行的实现方式。**

**② 线程安全靠「单写者 + 快照」，不靠加锁守全局。**
- 主线程是**唯一**修改 WorldModel / StateManager / memory / 模拟器的线程；
- 交给工作线程的是 `state.model_copy(deep=True)` 快照 + 事件副本，工作线程**只读**；
- 工作线程只写自己的 `_result`，主线程 `poll()` 取走并清空（结果带 4 s TTL，过期丢弃）；
- 工具层是唯一的例外（模型 function calling 会在工作线程里执行工具），
  因此 `AgentTools.call()` 用一把 `RLock` 把「工具执行」与「主循环推进」串行化——
  **锁只在执行的几毫秒内持有，绝不跨越网络等待**，所以不会把快循环堵住。

**③ 安全层的价值是"保证拦得住且不等 LLM"，不是"换一套话术"。**
合成优先级是「安全 EMERGENCY > 认知结果 > 规则基线」，但 EMERGENCY 时**直接采用基线动作**。
原因：规则基线在同样条件下本来就会拦（`risk.level == CRITICAL` ⇒ `WAIT`），
如果安全层另造一句话术，同一场景会因"LLM 是否卡住"而播报不同内容——
那是更难查的不可复现 bug。这样做的直接收益是：
**规则模式的 120 轮 Action 与 Stage 6 逐轮完全一致（0 差异）**，
即"旧 Demo 能力还在"（Gate 2）可以用快照严格证明，而不是靠感觉。

**④ 认知循环用「快照 + TTL」而不是「队列」。**
待办只保留最新一帧（`_pending` 被覆盖时计 `dropped`）：一个 3 秒前才想出来的
"前方有椅子"没有价值，排队只会让 Agent 越来越滞后于现实。

### ★踩坑记录★

- **`CognitiveLoop` 忘记 `start()` ⇒ 静默失效**：用例 61 直接驱动 `orchestrator.decide()`
  而没调 `step()`，线程从未启动，表现为"认知结果永远取不到"。
  修法：`decide()` 里**惰性自启动**（`start()` 幂等）。这类"链路不通但没报错"的故障
  正是本项目最花时间的一类（对照 Stage 3 的 `stats()` 撞名）。
- **`safety.critical_distance_m: null` 会在启动瞬间炸**：YAML 里 `null` 表示"沿用
  `agent.risk` 的口径"，但 `float(None)` 抛 TypeError。必须在 `from_cfg` 里显式兜底
  （`crit if crit is not None else risk[...]`）。**配置里写 `null` 一定要在读取处处理。**
- **`total_deadline_s` 必须放在发起请求之前检查**：工具调用是多轮循环，每轮都有自己的
  `timeout_s`，累加起来轻松过分钟级。用例 62 用一个"一旦被调用就抛 AssertionError"的
  假客户端证明超预算时**一次请求都不发**。
- **测试里替换 `sys_.llm` 不够，还要替换 `sys_.agent.llm`**：认知路径读的是
  `SpatialAgent.llm`。少了这一步会真的去连外网（在没有 Key 时表现为"什么都没发生"）。
- **`max_retries` 是 openai SDK 的隐式默认值**：`OpenAI(...)` 不传就等于 2。
  这类"库的默认值"必须显式覆盖；**凡是"时间预算"相关的参数，都要问一句"库的默认是什么"。**

### 数据流变化

```
Stage 6：  ... → World Model → EventDetector → EventBus → EventHistory（无人消费）
Stage 7：                        ┌─────────────────────────────────────────┐
                                 ↓                                         │
  Sensor → Fusion → Event Engine → SafetyEngine(每 tick，纯计算) ──┐        │
                                       │                          │        │
                                       └→ CognitiveTrigger ──→ [工作线程]   │
                                                                   LLM      │
                                       ↑                            │        │
                                       └──── poll(结果 @ TTL) ──────┘        │
                                                                    ↓        │
                            规则基线 ────────────────────────→ 合成 → Action ─┘
```
`SpatialState` / 规则引擎的**判定逻辑未变**，变的只是"谁在什么时候调用它"。

### 测试结果

```
python main.py --selftest        →  通过 63 / 63 | 失败 0 | 耗时 5.9s
tools/ui_smoke_test.py           →  通过 34 / 34
tools/e2e_test.py                →  全部通过（iPhone 图片流 → SpatialState → 多模态 → 导航行动）
行为快照（120 轮，规则模式）      →  与 Stage 6 完全一致（不一致 0 轮）
服务端实测（真实 LLM，1 Hz）      →  last_tick_ms ≈ 1.99 ms；safety.evaluations == tick
                                    cognitive.runs = 3 / 12 轮；llm.last_latency ≈ 2.5~3.0 s
```

新增 5 项用例：

| # | 用例 | 实测结果 |
|---|---|---|
| 59 | 安全规则表 | 纯函数 / 阈值可配 / 紧急豁免冷却 / 坠落优先 / 单规则异常隔离 |
| 60 | **★硬验收★** LLM 卡 5 秒 | 6 轮决策最坏 **0.000 s**；3 轮 `step()` 共 **0.003 s**；安全求值 **9 次**；LLM 仍在睡眠且只发起 1 次调用 |
| 61 | 认知循环 | 异步产出并被后续轮次取用 / 过期结果丢弃 / 提问不抢答 / 触发策略正确 |
| 62 | 大模型预算 | `max_retries=0` / 总预算闸门生效（超预算 0 次请求）/ 无 Key 不重试 |
| 63 | P0 复核：循环速率 | LLM 睡 3 s 时 10 轮仅 **0.005 s**（≈1900 Hz ≫ 目标 1 Hz） |

**用例 60 的读法**：`0.000 s` 不是"测得不准"，而是快循环本来就快（纯计算 0.21 ms 量级），
关键在于**它没有被那 5 秒拖住**——改造前同样场景下每一轮都要等满 5 秒。

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| 安全判定已产出，但"说不说/怎么说/用哪个通道"还没独立成层（当前由规则引擎的消息决定） | **Stage 8**（Interaction Policy + Action Schema） |
| `ActionType` 仍是 v0.2 的 5 种（SPEAK/CONTINUE/REPLAN/WAIT/ASK_USER），任务书要求 10 种 | **Stage 8** |
| `SafetyVerdict` 尚未进提示词（模型看不到"安全层刚刚判了什么"） | **Stage 8**（context_builder） |
| 认知循环的触发集合是硬编码的 `IMPORTANT` 常量，未从配置读 | Stage 11 统一收口 |
| 事件历史与认知结果还没落盘 | **Stage 9**（Recording / Replay） |

### 下一阶段

**Stage 8：Interaction Policy + Action Schema**
—— `agent/action_schema.py`（10 种 `ActionType` + `priority`/`expires_at`/`metadata`，所有
LLM 输出必须 Pydantic 校验，解析失败即 fallback）、`agent/interaction_policy.py`
（说不说 / 说多少 / 用哪个通道：VOICE / SILENT / CONSOLE_ALERT / HAPTIC）、
`agent/context_builder.py`（把 `SpatialState` + 事件 + `SafetyVerdict` + 世界模型摘要
组织成受控的信息面），`agent/decision.py` 退化为门面。
Gate 9：**Event 可以触发 Agent**（本阶段已铺好订阅链路与工作线程，Stage 8 让它闭环）。

---

## Stage 8：Interaction Policy + Action Schema

> 目标（任务书第十/十一节）：把「说什么」和「能不能说出口」拆开，并把大模型的输出
> 收进一个受优先级与有效期约束的结构化契约。
> 本阶段结束时的架构位置：

```
Sensor → Adapter → Fusion → World Model → Event Engine → Safety Layer
       → Context Builder → Cognitive Agent → Interaction Policy → Action Executor
```

### 修改文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `agent/action_schema.py` | **新增** | 10 种 `ActionType` + `ActionChannel`(5 种) + `Action`(priority/confidence/expires_at/metadata) + `AgentDecision` + `Action.from_llm()` |
| `agent/interaction_policy.py` | **新增** | 说不说 / 说多少 / 用哪个通道；`allow()`（规则层两道闸）/ `allow_stop()` / `gate()`（认知层完整闸门）/ `apply()` / 闸门计数 |
| `agent/context_builder.py` | **新增** | 受控信息面：`SpatialState` 渲染 + 事件摘要 + 安全层判定 + 世界模型摘要 + 最新一帧图 |
| `agent/cognitive_agent.py` | **新增** | 一次多模态调用 → 严格校验 → `AgentDecision`；`information_gaps` / `needs_visual`（Stage 10 的触发依据） |
| `agent/decision.py` | **改造为门面** | `Action`/`ActionType`/`MOTION_ALLOWED`/`Urgency` 原样转出；`_say()`/`_stop()`/`_trim()` 的实作移交 `InteractionPolicy`，语义逐字保持 |
| `agent/agent_core.py` | 修改 | 装配 `InteractionPolicy`/`ContextBuilder`/`CognitiveAgent`；`llm` 改为属性并**同步到认知层**；`think_bundle()` 供工作线程用；`gate_expression()` 统一表达审查；`_apply()` 判据 `message` → `speaks`；`set_user_speaking()` |
| `agent/orchestrator.py` | 修改 | 工作线程改为接收**主线程打好的 ContextBundle**；合成前插入表达审查；不可信结论整条退回基线 |
| `agent/prompt_template.py` | 修改 | 系统提示词补齐 10 种行动 + 「什么时候什么都不说」；`render_state()` 增加 `[最近事件]` / `[安全层]` 段 |
| `agent/safety/safety_engine.py` | 修改 | 缓存 `last_verdict`，供上下文构建器把"安全层刚判了什么"写进提示词 |
| `agent/__init__.py` | 修改 | 惰性导出新增 5 个类型 |
| `config/config.yaml` | 修改 | 新增 `interaction:` 段（巡航静默 / 用户说话 / 置信度门槛 / 通道开关）；`cognitive:` 补 `event_summary_n` / `history_n` |
| `events/event_engine.py` | 修正 | `subscribe()` 被压成一行的格式错误（不影响功能，但不可读） |
| `tests/selftest.py` | 修改 | 新增用例 **64~70**（Action Schema / 交互策略 / 上下文构建器 / 认知智能体 / Gate 9 / 规则模式中性 / 不可信结论不得否决移动） |

### 修改原因（对上审计报告的技术债）

| 技术债 / 任务书要求 | 本阶段如何解决 |
|---|---|
| 「说不说/说多少/用什么方式」散落在 `RuleDecisionEngine._say()` | 收成一个可单测的纯判定 `InteractionPolicy`，**规则层与认知层共用同一实例**（口径唯一） |
| `ActionType` 只有 5 种，模型想"请求一张画面"只能硬塞进 SPEAK | 扩到任务书要求的 10 种；`REQUEST_VISUAL` 让"看不清"变成一等公民（Stage 10 接主动感知） |
| 缺少 `priority` / `expires_at`：多个候选无仲裁依据，3 秒前的结论照样播 | 补齐 7 个字段；`effective_priority()` 让安全类在仲裁中永远压过交互类；`expired()` 阻断过期结论 |
| 大模型输出无 schema 校验（`_validate` 手写散装判断） | `Action.from_llm()`：**宽进严出**，不合格一律返回 `None` → 退回规则，系统不崩 |
| `SafetyVerdict` / 事件不进步提示词，模型只能从环境字段反推 | `context_builder` 把事件摘要与安全层判定显式写进 `[最近事件]` / `[安全层]` |
| 「先直行 20 m 状态正常 → Agent 应该静默，而不是复读『继续直行』」 | `InteractionPolicy.gate()` 的 `quiet_cruise` 闸门 + 提示词里的"什么时候什么都不说" |
| 每帧组装提示词、直接读 memory/events/camera（潜在竞态） | 组装收进 `ContextBuilder.build()`，**只在主线程调用**，跨线程只传不可变副本 |

### ★五个必须说清的设计决策★

**① 新能力只加在非确定路径上，确定性路径保持可证明不变。**

这条是整个 v0.3 迁移的方法论。`InteractionPolicy` 提供两套入口，刻意分开：

- `allow()` / `allow_stop()` —— 规则基线用的两道闸，**语义与 v0.2 逐字一致**
  （先 trim 再判重、两条 reason 文案、urgent 直通间隔、停步不受间隔约束）。
  `decision.py` 的 `_say()`/`_stop()` 现在只是"把判定翻译成 Action"。
- `gate()` —— 认知层用的完整闸门，在 `allow()` 之上再加 `expired` / `silent` /
  `user_speaking` / `low_confidence` / `quiet_cruise` / `channel_disabled`。
  它**只作用于大模型输出**，所以调这些开关不会改变 120 轮回归快照。

结论：`120/120 零差异` 不是"跑过了"，而是**结构上不可能变**。

**② ★不可信的结论不得拥有物理否决权★（本阶段最重要的安全修正）。**

这是真实模型跑出来的缺陷，不是推演：

> `kimi-k2.5` 返回 `SAFETY_ALERT`（`permits_motion=False`）"前方有移动箱子，小心"，
> 该结果因过期被策略拦下 —— 但旧实现只清空了 `message`、**保留了整条行动**，
> 于是 `advance_user(motion_allowed=False)` 被这条"没人听见、也没人复核"的过期结论
> 持续否决，**用户被钉在走廊原地**。

修法是把两类拦截分开：

| 类别 | code | 处理 |
|---|---|---|
| **结论已不可信** | `expired` / `low_confidence` | **整条丢弃，退回规则基线**（`InteractionDecision.discard`） |
| **此刻不方便说** | `repeated` / `too_frequent` / `quiet_cruise` / `user_speaking` / `channel_disabled` / `silent` | 只清空话术，**保留行动的物理语义** |

理由：不可信的结论不该拥有物理否决权；而安全性并不依赖它 —— 真危险时
安全层（快循环，每 tick 必跑）与规则基线本来就会拦。用例 70 把这条钉死了：
过期/低置信度 → 退回基线且**不冻结用户**；可信时（same payload, 门槛放宽）→ 采纳并
保留"停步提问"的语义；临界风险下安全层仍独立生效。

**③ 上下文组装必须在主线程做（跨线程只传不可变副本）。**

Stage 7 交给工作线程的是 `state.model_copy(deep=True)` + 事件列表。Stage 8 的
`ContextBuilder` 还要读 memory / 事件存档 / 相机缓存 —— 这三个都是**主线程独占写**。
在工作线程里 `list(deque)` 期间主线程 `append` 会抛
`RuntimeError: deque mutated during iteration`（只在真并发下偶发，最难查的那类）。
所以改成：主线程 `build()` 出 `ContextBundle`（内容全是字符串 / 字节 / 字典副本），
工作线程**只拿 bundle 调模型**。

**④ `Action.as_dict()` 是冻结的线格式契约。**

前端 `test_page.html`、`tools/e2e_test.py`、120 轮回归快照都读它。
新字段一律走 `to_dict()`（日志/诊断/控制台）。用例 64 直接断言键集合等于
`{action_type, message, urgency, reason, source, permits_motion}` —— 谁改了谁红。

**⑤ `_apply()` 的发声判据从「有 message」升级为「`action.speaks`」。**

`CONSOLE_ALERT` 是"记一笔但不打扰用户"，绝不能被念出来。规则基线的 Action
只要 `message` 非空，`model_post_init` 就必然给它 `VOICE` 通道 ⇒ 两者在
确定性路径上等价。用例 69 用 120 轮断言了这一点（只允许 VOICE/SILENT 两种通道，
且 `bool(message) == speaks`）。

### ★踩坑记录（本阶段真正花时间的四处）★

1. **`Action.from_llm()` 的"数组话术"容错是死代码。**
   先写 `message = str(raw.get("message","")).strip()`，于是
   `["前方","有障碍"]` 被 `str()` 变成 `"['前方', '有障碍']"`。
   校验器里的 `_coerce_message` 永远看不到数组。**这个问题是"测试断言 message"
   才发现的** —— 只断言"有 message"的测试会全部放过它。

2. **过期的 `SAFETY_ALERT` 冻结用户**（见设计决策 ②）。发现路径值得记：
   跑真实模型时看 `policy.stats()['blocked_by']` 发现 `expired`，
   再回头查最终 Action 的 `action_type` 里混进了 `SAFETY_ALERT` ——
   而规则基线根本不产生这个类型。**是统计口径把问题暴露出来的**，
   所以闸门计数不是装饰，是诊断手段。

3. **`submitted_at + last_latency` 是量纲混用（仿真秒 + 墙钟秒）。**
   仿真步长与真实经过时间一旦不相等（测试、回放、快循环空转都会），
   就会把"没过期"的结论判成过期 —— 表现为"模型明明答了却永远不生效"。
   改为只用**仿真时刻**打时间戳（`_result_at = job.submitted_at`），
   语义上也更对：结论描述的是**提交那一刻**的世界。

4. **替换 `agent.llm` 必须同步到 `agent.cognitive.llm`。**
   Stage 7 已经踩过一次（替身不生效）。这次把 `SpatialAgent.llm` 改成属性，
   setter 里强制同步，并用例 67 直接断言 `ag.cognitive.llm is good`。

> 另：真实模型验证时必须让**仿真秒 ≈ 墙钟秒**（`step(1.0)` 后 sleep 补足 1 s）。
> 否则 `t` 跑得比墙钟快，认知结果回来时已越过 TTL，"模型答了但不生效"，
> 很容易误判成代码错。

### 数据流变化

```
[快循环 / 主线程] 每 tick
  advance_environment → StateManager.build → EventEngine.process
    → SafetyEngine.evaluate（必跑）
    → AgentOrchestrator.decide
         baseline ← RuleDecisionEngine（内部经 InteractionPolicy.allow/allow_stop）
         cognitive ← CognitiveLoop.poll()   ← 上一轮工作线程的结论（可能无/可能过期）
         if cognitive:
             gate_expression()  ← InteractionPolicy.gate() + apply()
             discard ? → 退回 baseline（不可信结论不得否决移动）
                     : → _merge_with_safety(baseline, gated)（安全底线不可被推翻）
    → _apply（只有 action.speaks 才调 speak 工具）
    → env.advance_user(motion_allowed=action.permits_motion)

[慢循环 / 工作线程]
  CognitiveLoop._run
    ← 主线程 ContextBuilder.build() 打好的 ContextBundle（状态副本+历史副本+图字节+事件摘要）
    → CognitiveAgent.decide() → llm.chat_multimodal() → Action.from_llm()（严格校验）
    → AgentDecision → _result（主线程 poll 取用）
```

### ★Gate 9 验收：Event 可以触发 Agent★

用例 68 用真实运行时链路（`step()` 逐个推进，60 轮）证明：

- 仿真环境确实产生了事件（`events.history.stats()["total"] > 0`）；
- 事件触发了认知调用（`agent.cognitive.calls ≥ 1`，实际 21 次）；
- 认知产出被主循环**取用**（`r.llm_used` 为真的轮数 ≥ 1）；
- 模型复读的同一句话**被策略拦下**（`policy.stats()["blocked_by"]["repeated"] ≥ 1`），
  即"能说话"不等于"该说话"在认知路径上同样成立；
- `user_speaking=True` 时模型的话被换成静默、`user_speaking=False` 时原样保留，
  且**不就地修改原 Action**。

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `python main.py --selftest` | **70 / 70**（新增 64~70 共 7 条） |
| 行为回归 | `/tmp/snap_states.py` + `/tmp/cmp_snap.py`（120 轮） | **不一致 0 轮**（对比 Stage 7 基线 `after_stage7b.json`） |
| 端到端 | `python tools/e2e_test.py` | 全部通过（iPhone 图流 → SpatialState → 多模态模型 → 导航行动） |
| 测试页冒烟 | `python tools/ui_smoke_test.py` | **34 / 34** |
| 真实大模型 | `kimi-k2.5`（Token Plan） | 24 轮：`calls=3`、`parse_failures=0`、`blocked_by={silent:1}`、最终行动 1 次来自 `llm`；模型自发产出过 `SAFETY_ALERT`（priority=85 / confidence=0.9 / expires_at 齐备）⇒ 新 schema 真的在被使用 |
| 循环速率 | 用例 63（LLM 睡 3 s） | 10 轮 0.005 s，1884 Hz ≫ 目标 1 Hz |

新增用例清单：

| # | 名称 | 关键断言 |
|---|---|---|
| 64 | Action Schema | 10 种类型齐备 / `as_dict()` 键集合冻结 / 6 类非法输出全被拒 / `"high"`→80、`85`→0.85 / `permits_motion` 与 v0.2 一致 |
| 65 | 交互策略 | `allow()` 两条 reason 文案逐字一致 / `allow_stop()` 不受间隔约束 / 5 道新闸门 / `discard` 分类正确 / 静音不改物理语义 |
| 66 | 上下文构建器 | 字段白名单 / 提示词零原始传感器泄露 / 事件与安全层进提示词 / 过期图不算图 |
| 67 | 认知智能体 | 合法→`AgentDecision` / 非法→`None` / 异常就地吞掉 / `information_gaps` 与 `needs_visual` / 换模型同步 |
| 68 | ★Gate 9★ | 事件触发 Agent / 认知结果被取用 / 复读被拦下 / 用户说话时不打断 |
| 69 | 规则模式中性 | 120 轮只出现 v0.2 的 5 种行动、只用 VOICE/SILENT、`bool(message)==speaks`、未起认知线程 |
| 70 | ★安全底线复核★ | 过期/低置信度 → 退回基线且不冻结用户 / 可信时采纳并保留停步 / 安全层仍独立生效 |

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `agent/safety/risk_rules.py` 的 `SAFETY_ALERT` 目前只作为模型侧类型存在，安全层自身的告警仍走"基线话术" | Stage 11 验收时统一（安全层 → `SAFETY_ALERT` 通道化） |
| `HAPTIC` / `AUDIO_CUE` 通道只有枚举与开关，没有真实输出设备（任务书明确禁止腰带设备） | 不在 v0.3 范围 |
| `needs_visual` 已产出但无人消费（应当是主动请求一张新画面） | **Stage 10**（Active Perception / `request_visual_observation`） |
| 上下文与认知结果尚未落盘，无法逐轮归因 | **Stage 9**（Recording / Replay） |
| `CognitiveTrigger.IMPORTANT` 仍是硬编码常量 | Stage 11 统一收口 |
| `docs/技术交付文档.md` 里"MOTION_ALLOWED 只有 SPEAK/CONTINUE"的描述已随本阶段扩展失效 | Stage 11 统一更新交付文档 |

### 下一阶段

**Stage 9：Recording / Replay**
—— 把每轮的 `SpatialState` 快照、事件、安全判定、`AgentDecision`、最终 `Action` 落盘，
并提供 `--mode replay` 用同一份记录重放（不联网、不用摄像头）。
本阶段已把"要录什么"准备好了：`ContextBundle` 与 `AgentDecision.to_dict()` 都是
**设计成可完整落盘**的对象（`ContextBundle.describe()` 已可用于诊断行）。


## Stage 9：Recording / Replay（录制与回放）

任务书第十五节。目标不是"记日志"，而是**让一次真实盲人实验可以被反复复算**：
换模型、换 Prompt、换 policy 之后，"到底哪几轮判断变了"必须可比。

### 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `recording/schemas.py` | **新增** | `SCHEMA_VERSION`、`SessionMeta` / `StateRecord` / `EventRecord` / `ActionRecord` / `LlmRecord` 五个数据类；`append_jsonl()`（只追加）/ `read_jsonl()`（坏行跳过）/ `new_session_id()` / `is_valid_session_id()`（挡路径穿越） |
| `recording/session_recorder.py` | **新增** | `SessionRecorder`：`start/attach_map/close` + `record_state/events/action/llm` + `save_image/note`。全程 `try/except` 吞异常 —— 录制失败绝不能影响主循环 |
| `recording/session_player.py` | **新增** | `ReplaySource`（鸭子类型同时扮演 SensorProvider / WorldStepper / StateManager，所有推进都是空实现）+ `ReplayReport` + `SessionPlayer` |
| `recording/__init__.py` | **新增** | PEP 562 惰性导出（与项目其他包一致） |
| `agent/agent_core.py` | 修改 | ① `SpatialAgentSystem` 接受 `recorder=`，装配处把 `provider.map_snapshot()` 交给录制器；② `step()` 里在**决策之前**写时钟、并落状态/事件/行动；③ ★把 `_apply` 的实现搬到 `SpatialAgent.apply_action()`★（见下）；④ `stats()` 加 `recording` 段 |
| `agent/llm_client.py` | 修改 | 落盘每次调用的输入摘要与结构化输出（`_request_summary()`），所有返回路径都记账；`last_tool_calls` 每轮重置 |
| `main.py` | 修改 | `--mode replay` / `--record [目录]` / `--session` / `--session-id` / `--record-notes` / `--keep-images` / `--no-images` / `--max-images` / `--replay-out` / `--replay-llm` / `--replay-ticks`；新增 `make_recorder()` 与 `run_replay()` |
| `config/config.yaml` | 修改 | 新增 `recording: {root, keep_images, max_images}` |
| `.gitignore` | 修改 | 忽略 `recordings/`，保留 `.gitkeep` |
| `tests/selftest.py` | 修改 | 新增用例 **72 录制** / **73 回放**；用例 47 的分层铁律**加挂 `recording/*`** |

### 修改原因（对上审计报告的技术债）

| 审计发现 | 本阶段的处置 |
|---|---|
| 上下文与认知结果不落盘 ⇒ 出问题只能靠日志文字猜 | 四类 JSONL 全量落盘 + `meta.json` + `map.json` |
| 无法回答"换模型之后哪几轮变了" | `ReplaySource` 冻结世界，逐轮把新决策与录制里的原决策比对，产出 `matched/compared` 与差异列表 |
| 回放若接模拟器就退化成"自己造世界再跑"，验证价值归零 | 分层铁律把 `recording/session_player.py` 纳入零 `simulator` 依赖清单 |

### ★四个必须说清的设计决策★

**1. 把执行语义从 `SpatialAgentSystem._apply` 搬到 `SpatialAgent.apply_action()`。**

回放**不接模拟器**，自然也就没有 `SpatialAgentSystem`。但"重放 Agent"必须包含
"说完话之后记忆要跟着变"这一半 —— 交互策略的"是否重复 / 距上次播报多久"全靠
memory 里的播报记录。把执行逻辑挂在决策器上，回放器和主循环才能共用同一条路径。

这条不是理论洁癖，是**实测出来的**：不重放 `apply_action` 时，同一个会话回放只有
**82.5%** 一致；补上之后升到 95%。

**2. 只保存"可公开"字段，用白名单而不是黑名单。**

任务书明确要求**不要保存隐藏 chain-of-thought**。`_public_decision()` / `_public_gate()`
都是**逐个字段列白名单**：黑名单（"过滤掉 reasoning_content"）在 SDK 加字段时必然静默泄露，
白名单则默认什么都不泄露。自检用例 72 的字段白名单**直接从数据类声明里取**
（`dataclasses.fields`），所以它断言的是"落盘字段集合 == 声明的可公开字段集合"，
而不是一份会过期的手抄清单。

**3. 图片只存"引用"，不存进 JSONL。**

`llm.jsonl` 里的 `image_ref` 只是 `images/` 下的文件名。把 base64 塞进 JSONL 会让
单行膨胀到几十 KB，`grep`/`jq` 全部失效。图片另外受 `keep_images` / `max_images` 控制
（1 Hz × 20 KB ≈ 70 MB/小时，真实长跑必须能关）。

**4. `ReplaySource` 用鸭子类型扮演三个角色，而不是继承任何真实实现。**

装配处那两行（`provider=SimulatedProvider(...)`）只换成 `provider=ReplaySource(...)`，
下游一行不改 —— Stage 3 留的这条缝在这里兑现。所有 `advance_*` 都是空实现。
唯一有副作用的是 `replan()`：它只**登记**并返回成功（世界一动不动才是回放），
真实结论看下一帧的历史状态。

### ★踩坑记录（本阶段真正花时间的三处）★

**① `0.0 or x` 吞掉首帧时刻 —— 整条回放时钟偏移 1 秒。**

```python
elapsed = float(self.states_raw[i].get("t") or state.tick)   # ✗
```
首帧的 `t` 恰好是 `0.0`（falsy），于是静默回退成 tick 号，整条回放时钟整体错位 1 秒。
后果不是"差一点"，而是所有"距上次播报过了多久"的判据全部错位：
`NAV_REFRESH_INTERVAL_S = 12.0` 只差 1 秒就会让"定期复述导航"推迟一轮。
表现为一致性**卡在 95% 上不去**。改成显式判 `None` 之后立刻 100%。
—— 与项目记忆里那条"仿真秒 ≠ 墙钟秒"同属量纲类陷阱，但更隐蔽：它连报错都不报。

**② 回放漏了 `apply_action`，一致性只有 82.5%。**

见上面设计决策 1。根因是交互策略的判据**带状态**，而状态由 `apply_action` 更新。
"回放只重算决策"这个直觉是错的：决策器的**输入**里就有"上一轮说了什么"。

**③ 顶层的 `tick` 与 `state.tick` 不是同一个东西。**

`record_state()` 写两个 tick：顶层的是**系统轮次**，`state` 里的是**状态管理器自己的计数**，
两者起点不同。自检里拿顶层 tick 去对状态里的 tick 会误报"不一致"。
用例 73 现在断言的是"状态与原记录逐字段一致"，而不是猜某个具体数值。

### 数据流变化

```
每轮 step():
  融合状态 → [recorder.record_state]  ← 完整 dump（不是给模型看的裁剪视图）
  事件引擎 → [recorder.record_events]
  决策      → [llm_client 内部 recorder.record_llm]
  执行      → [recorder.record_action]

回放（--mode replay）:
  读 states.jsonl → SpatialState.model_validate（extra="forbid" 逼着 dump 必须完整）
                → ReplaySource.advance_to(state)（冻结世界）
                → agent.decide()  → agent.apply_action()（与主循环同一条路径）
                → 与 actions.jsonl 逐轮比对 → ReplayReport
```

### ★Gate 10 验收：Replay 可以读取历史数据★

`python main.py --mode replay --session <id>` 全程**不 import simulator**（用例 47 + 73 双重确认），
并且**同配置回放逐轮复现**：

| 会话 | 轮次 | 行动分布 | 与原录制一致 |
|---|---|---|---|
| CLI 实测 `smoke_rec` | 40 | `{SPEAK: 6, CONTINUE: 34}` | **40 / 40（100.0%）** |
| 自检用例 73 | 30 | `{SPEAK: 3, CONTINUE: 27}` | **30 / 30（100%）** |

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `python main.py --selftest` | **74 / 74** |
| 录制→回放实跑 | `--record /tmp/... --ticks 40` → `--mode replay` | 100% 一致，无模拟器 |
| 中性 A/B | `/tmp/ab_stage910.py`（120 轮，不录制 vs 录制） | **不一致 0 轮** |

新增用例：

| # | 名称 | 关键断言 |
|---|---|---|
| 72 | 录制 | 目录契约（`meta/states/events/actions/map/images`）/ `meta` 版本与地图名 / **只追加** / 字段白名单（`dataclasses.fields`，含嵌套 `decision`、`gate`）/ 未开启录制时零副作用 |
| 73 | 回放 | 源码零 `simulator` import / 历史状态逐字段重建 / `ReplaySource` 三协议齐备 / 推进全为空操作（世界不被动过）/ **30 轮零差异** / `--replay-out` 产出可再回放会话 / 坏行跳过不毁整段 |
| 47（扩展） | 分层铁律 | 加挂 `recording/session_player.py`、`recording/session_recorder.py` |

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `recordings/` 没有自动清理策略（只按 `max_images` 限图片） | Stage 11 视需要加保留期 |
| 回放时 `enable_llm=True` 的"换模型对比"尚未在验收 Demo 里跑一遍 | Stage 11 验收 Demo |
| 回放对大模型调用的响应只按状态流重算，不重放录制里的模型原文 | 设计如此（要的就是"用新模型重算"）；如需逐字复现可后续加 `--replay-canned` |

### 下一阶段

**Stage 10：Active Perception**
—— 消费 Stage 8 就已经产出但无人接的 `AgentDecision.needs_visual`：
把它变成"真的去要一张新画面"，并且**只在事件 / 置信度不足 / 用户询问环境时才要**。


## Stage 10：Active Perception（主动感知）

任务书第十三节。三条触发条件、以及一句很关键的话：**不要每轮都上传图片**。
"每轮喂一张图"会把多模态退化成 1 Hz 截图流：延迟、带宽、配额全按固定频率烧掉，
而绝大多数轮次（笔直走走廊）画面根本不会改变结论。

### 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `agent/active_perception.py` | **新增** | `VisualNeed`（判定结果，纯数据可落盘）+ `VisualRequestPolicy`（**纯函数**：`triggers()` / `evaluate()` / `from_cfg()` / `stats()`）；`DEFAULT_VISUAL_EVENT_TYPES` / `DEFAULT_ENV_QUERY_KEYWORDS` |
| `agent/context_builder.py` | 修改 | 持有 `VisualRequestPolicy`；`_recent_events()` 向事件引擎**回捞一个时间窗**；`build()` 里先判定"该不该看"再决定要不要读相机；`ContextBundle.visual_need` |
| `agent/cognitive_agent.py` | 修改 | `build_context(force_visual=...)`；`decide()` 把 `bundle.visual_need` 折进 `information_gaps` 并抬升 `needs_visual` |
| `agent/tools.py` | 修改 | 新增 `request_visual_observation()` 工具（一次性标志 + 回一份视觉链路状态）+ `consume_visual_request()` / `visual_request_pending`；工具 schema 与 `stats()` 同步 |
| `agent/agent_core.py` | 修改 | `SpatialAgent.build_context()` **取走**一次性取帧请求（取走即清，只生效一次）；`stats()` 加 `perception` 段 |
| `config/config.yaml` | 修改 | 新增 `agent.perception:`（mode / 置信度阈值 / 帧过期阈值 / 节流间隔 / 事件窗 / 事件类型 / 关键词） |
| `tests/selftest.py` | 修改 | 新增用例 **74 主动感知**；用例 69 增加"中性可计数证明" |

### 修改原因（对上审计报告的技术债）

| 审计发现 | 本阶段的处置 |
|---|---|
| Stage 8 已产出 `needs_visual`，但**无人消费** | 两条通路都接上：模型显式调 `request_visual_observation()`；策略层在三条条件下独立登记取帧请求 |
| 缺少"什么时候该看一眼"的可审计判据 | 收敛成一个**纯函数** `VisualRequestPolicy`，判定结果可落盘、可断言、可回放 |
| 认知循环是**节流**的（事件驱动 + 心跳），只看"本轮事件"会大量漏判 | `_recent_events()` 按 `event_window_s` 向事件引擎回捞；窗口长度是配置项，不是魔法数字 |

### ★四个必须说清的设计决策★

**1. 架构铁律 7 的落点：本层只作用于认知（非确定）路径。**

`evaluate()` 只被 `ContextBuilder.build()` 调用，而规则基线
（`baseline()` → `policy.observe()` + `rules.decide()`）**根本不经过它**。
所以"Stage 10 不改确定性行为"不是靠"跑一遍看起来没变"，而是**可以计数证明**：
自检用例 69 断言规则模式 120 轮后 `perception.stats()["evaluated"] == 0`
—— 这一层压根没被调用过，于是它在**结构上**不可能改变任何一位确定性输出。

**2. 三种模式，默认 `auto`；省 token 的是 `on_demand`。**

| 模式 | 行为 | 用途 |
|---|---|---|
| `always` | 有帧就附 | 回归对照（= v0.2） |
| `auto`（默认） | 有帧就附；**同时**判定"是否真的需要"并登记一次取帧请求 | 兼容模式：不省 token，但已验收的多模态链路不受影响 |
| `on_demand` | **只在需要时才附** | 真实部署推荐 |

默认选 `auto` 是刻意的：Stage 10 是"加一层能力"，不该顺手改变已经验收过的
多模态链路行为。要省成本就把配置改成 `on_demand`（自检用例 74 两个模式都覆盖）。

**3. `camera_freshness > 3s` 是触发条件，"根本没有相机"不是。**

任务书给的判据只有三条。一开始把 `camera.image_available == False` 也当成"需要视觉"，
这在无相机部署里会把 `needs_visual` **永久点亮成假信号**（启动前几轮尤其明显）——
和项目记忆里 `route_confidence` 被钉在地板那次是同一类错误：指标类字段一旦变成常量，
它就不再传递信息。

语义上也是错的：**"没有相机"是"无法获得视觉"，不是"需要视觉"**。帧由手机端推上来，
Agent 侧要不到，登记一次取帧请求不可被满足。真正的"链路掉了"由
`CAMERA_LOST` 事件覆盖（本来就在事件触发那一条里）。
现在保留为**诊断信息**（`suppressed_by="camera_unavailable"` + `stats()["camera_absent"]`），
但不构成触发。自检用例 74 专门钉死这一条。

**4. 取帧请求是"一次性"的，且必须由主线程取走。**

工具在工作线程里执行（function calling），而相机缓存是主线程独占写的。
所以 `request_visual_observation()` **只举手**：置一次性标志 + 回一份链路状态，
真正的取图统一由主线程的 `ContextBuilder.take_image()` 做（用 `latest_bytes()`，
**过期必须返回 None** —— 把过期图喂给模型比不给图更糟）。
标志在 `SpatialAgent.build_context()` 里被 `consume_visual_request()` 取走即清，
所以"模型说要看一眼"只影响**下一轮**，不会退化成每轮都带图（用例 74 断言了第二次不含该理由）。

### ★踩坑记录（本阶段真正花时间的两处）★

**① 无相机被当成"需要视觉" ⇒ 假信号（见上面决策 3）。**

发现方式是自检用例 67 变红：`SPEAK 不该被标记为需要新画面`。
一开始想直接改断言"接受新语义"，但顺着判据查下去才看清——
真正的问题不是"语义变了"，而是**这个触发条件本身就不该存在**。
教训：测试变红时先问"是我的断言过期了，还是被测逻辑错了"，不要条件反射改断言。

**② 自检里测"该不该取图"必须先给一份新鲜帧。**

`expect(b1.image is not None, "低置信度时应取图")` 第一版直接失败：
`take_image()` 除了要看"该不该取"，还要 `state.camera.image_available` 为真。
状态里没相机 ⇒ 不管判定多正确都返回 None，用例会**假通过**（或假失败）。
现在 `st()` 辅助函数**默认给一份新鲜帧**，只在测"无相机"时才显式覆盖。

### ★Gate 10 之外的收获：`needs_visual` 从"模型自述"升级为"模型 ∪ 策略"★

`CognitiveAgent.decide()` 里：

```python
needs_visual=bool((gaps and action.action_type is REQUEST_VISUAL) or need_visual)
```

为什么不能只看模型有没有吐 `REQUEST_VISUAL`：**模型常常"看不清也照样瞎说一句"**。
策略层的判定是独立于模型输出的旁证，两者取或，才能真正表达"我现在信息不足"。
这与 Stage 8"模型决定说什么、策略决定能不能说出口"是同一套分工。

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `python main.py --selftest` | **74 / 74** |
| 中性 A/B | `/tmp/ab_stage910.py`（120 轮，`mode=never` vs `mode=auto`） | **不一致 0 轮** |
| 中性 A/B | 同上（不录制 vs 录制） | **不一致 0 轮** |
| 端到端 | `python tools/e2e_test.py` | 全部通过 |
| 测试页冒烟 | `python tools/ui_smoke_test.py` | **44 / 44** |

新增用例：

| # | 名称 | 关键断言 |
|---|---|---|
| 74 | ★Gate 10★ 主动感知 | 三条触发条件各自命中（事件 / 置信度 / 用户问环境）/ "还有多远"不触发 / 帧过期触发 / **无相机不算** / 节流 + 显式请求豁免 / 四种模式语义 / 缺配置回落默认 / `on_demand` 真的省图 / **一次性握手只生效一次** / 策略判定进 `information_gaps` 且不改 `permits_motion` |
| 69（扩展） | 规则模式中性 | 增加 `perception.evaluated == 0`（可计数证明）与 `recording.enabled == False` |

### 未解决 / 明确留到后续阶段

| 遗留 | 归属 |
|---|---|
| `on_demand` 模式尚未在真实多模态链路（真机 iPhone + 真实模型）上跑过 | Stage 11 验收 Demo |
| 取帧请求只做"登记"，没有把请求下发给手机端（v0.3 不增加 iOS 侧协议） | 不在 v0.3 范围（任务书：v0.3 只需"请求获取新的 iPhone frame"） |
| `DEFAULT_ENV_QUERY_KEYWORDS` 是小词表，中文口语变体可能漏 | Stage 11 视验收场景补词 |

### 下一阶段

**Stage 11**：十项验收场景 + 验收 Demo + 9 份文档 + 版本号 `0.2.0 → 0.3.0`。


## Stage 11：验收收尾（十项场景 + 验收 Demo + 九份文档 + 版本升级）

> 任务书里**没有**名为 "Stage 11" 的阶段 —— 它到 Stage 10（Active Perception）+「运行完整 Demo」
> 为止（第二十七节）。本节是**收尾阶段**的落地，范围由任务书的验收条款反推得到：
> 第二十八节 Gate 1~10、第二十九节最终 Demo 剧本、第三十三节文档要求、第三十四节交付报告、
> 第三十五节 v0.4 预留。

### 修改文件

| 文件 | 变更 | 说明 |
|---|---|---|
| `tests/acceptance.py` | 新增 | 10 个验收场景（S01~S10），逐条对应 Gate 1~10；输出 Pass/Fail + 证据行 |
| `tools/acceptance_demo.py` | 新增 | 任务书第二十九节「七拍剧本」的一次连续会话演出 |
| `main.py` | 修改 | 新增 `--acceptance` / `--acceptance-demo` / `--demo-out` 三个开关与 `run_acceptance` / `run_acceptance_demo` |
| `agent/interaction_policy.py` | 修改 | `gate()` / `_gate_impl()` 增加 `answering_user` 参数，转成 `force=` 逃逸口 |
| `agent/agent_core.py` | 修改 | `gate_expression()` 增加 `*, answering_user: bool = False` 并透传 |
| `agent/orchestrator.py` | 修改 | `decide()` 合成认知结果时按 `self._query_t0 is not None` 传入 `answering_user` |
| `config/config.yaml` | 修改 | `system.version` `0.2.0` → `0.3.0`；为两个预留的 `visual_event_types` 加注释 |
| `events/event_types.py`、`events/event_engine.py`、`events/__init__.py` | 修改 | docstring 里「15 类事件」更正为 **16 类**（枚举实际成员数） |
| `agent/active_perception.py` | 修改 | 标注两个永不命中的预留事件名（只改注释，无行为变更） |
| `README.md` | 重写 | 补 v0.3 架构、七条铁律、验收命令、录制/回放、文档索引、WASD 手动驾驶 |
| `V0.3_DELIVERY_REPORT.md` | 新增 | 任务书第三十四节要求的十项交付内容 |
| `docs/ARCHITECTURE_V03.md`、`SPATIAL_STATE_SCHEMA.md`、`EVENT_SYSTEM.md`、`SAFETY_ARCHITECTURE.md`、`REPLAY_SYSTEM.md`、`TEST_PLAN.md`、`MIGRATION_V02_TO_V03.md` | 新增 | 任务书第三十三节要求的专项文档 |
| `docs/V04_REAL_SENSOR_INTEGRATION_PLAN.md` | 新增 | 任务书第三十五节 v0.4 预留（只写计划，不实现） |
| `docs/V03_DEMO_TRANSCRIPT.md` | 新增（产物） | 七拍 Demo 的可复现证据转写 |

### 修改原因

1. **Gate 1~10 必须逐条可判定** —— 原有 74 项自检是"按层"组织的细粒度断言，改代码时用；
   但验收需要的是"任务书那十句话到底满不满足"。所以另建 10 个**端到端场景**，每个场景给出
   PASS/FAIL 与一行证据。
2. **第二十九节要求"演一遍"** —— 验收场景是 10 个**互相独立**的用例，证明不了
   "这些能力能在同一条时间线上串起来"。故另写七拍 Demo，一次连续会话按剧本顺序演出。
3. ★**发现真实缺陷：用户提问的回答被表达层吞掉**★
   S06 首轮失败，症状是"答非所问/没有回答"。定位到 `interaction_policy` 的
   `too_frequent` 闸门（`min_interval_s = 4.0`）把回答静音了。
   **这是设计冲突而非实现错误**：闸门的目的是"别打扰"，但**回答用户刚刚提出的问题**属于
   "应所求"，不是打扰。修法是给闸门一个逃逸口，并且**只作用于 LLM 输出路径** ——
   规则路径（`RuleDecisionEngine → allow`）一字未动，因此不违反"规则路径可证明不变"。
4. **文档既是交付物也是防腐层** —— 任务书第三十三节点名要求的 8 份文档 + 交付报告，
   写的过程本身就是复核：写 `docs/EVENT_SYSTEM.md` 时才发现

   ★**源码 docstring 写"15 类事件"，而 `EventType` 枚举实际有 16 个成员**★（多出 `HIGH_RISK`）。
   这类"文档与代码不一致"只有真去逐条抄写才会暴露。

### 数据流变化

**主链路零变化。** 唯一改动在表达层：

```
认知结果 → gate_expression(action, state, t, events, answering_user=?)
                ↓
        InteractionPolicy.gate(..., force=answering_user)
                ↓
        base = allow(message, urgency, elapsed, memory, force=force)
                ↓
         force=True 时跳过 repeated / too_frequent 两道闸门
```

即：**"这条结果是不是在回答用户刚问的问题"** 这个信息，从编排层一路显式传到了闸门，
而不是靠猜（猜法是错的，因为"刚问过"和"总在问"在闸门看来长得一样）。

### ★踩坑记录（本阶段真正花时间的四处）★

1. ★**`step()` 里的 `state` 比真值晚一拍**★
   `step()` 顺序是「融合建 state → 决策 → 执行 → `advance_user()`」，所以
   `state.user.position` 描述的是**本 tick 移动之前**的位姿（1 m/s 下差 1.1 米）。
   验收剧本要求"椅子出现在正前方 X 米"，用 state 去算"正前方 0.5 米"，
   实际摆到了用户**身后**。症状极具误导性：**障碍事件产生了，但前方距离与风险毫无变化**。
   ⇒ 位置类断言必须读仿真器真值 `system.nav.pos` / `system.nav.heading`。

2. ★**认知循环是「工作线程 + 20 ms 轮询」，测试不给时间片就永远轮不到**★
   仿真 1 Hz 的 `step()` 只要几毫秒；若测试连续跑 20 轮 step 而不 sleep，
   总耗时 < 20 ms，工作线程一次都轮不到。症状是"模型链路不通"，
   实际只是测试没让出时间片。⇒ 等待认知结果时每轮 step 之间 `time.sleep(0.05)`。

3. ★**`events.jsonl` 是一行一条事件，不是一行一轮**★
   S10 最初断言"30 轮应有 30 条事件记录"直接失败。没有事件的轮次**不写行**。
   ⇒ 断言只能用"本轮实际发布了多少条事件"。

4. ★**被测智能体会"原地转身避让"，会让"安全层每轮都拦"的断言失效**★
   第 7 拍把椅子摆在正前方 0.20 m 后，前 3 轮都是 `emergency`，第 4 轮突然 `ok` 且放行。
   排查发现用户朝向从 0° 变成 **282°** —— 智能体原地转身把障碍甩出了前向锥，
   安全层随即**正确地**判 `ok`（现实中盲人被挡住后转身另寻路也是对的）。
   ⇒ 两种正确做法：① 每轮把障碍重新锚定到**当前**朝向的正前方；② 把断言换成更精确的不变式
   ——「**只要前方净空进入危险档，就必须 emergency 且禁止前进**」。
   本阶段采用 ②（更本质，且不会掩盖真实问题）。

### 测试结果

| 测试 | 结果 |
|---|---|
| `python main.py --selftest` | **74 / 74 通过**（12.3 s） |
| `python main.py --acceptance` | **10 / 10 通过**（1.0~1.2 s） |
| `python main.py --acceptance-demo` | **7 / 7 拍通过** |
| `tools/ui_smoke_test.py`（先起 `--serve`） | **44 / 44 通过** |
| `tools/e2e_test.py` | 全部通过 |
| 录制→回放一致率（S10） | 30 轮 → **30/30（100%）**，回放器零 `import simulator` |
| 行为回归（规则模式，120 轮逐轮比对） | **零差异**（轨迹指纹 `4eb56a3fb5db1380876ab49d0520f883`） |

Gate 1~10 → 场景对照：G1→S01；G2→S01/S02/S09；G3→S06/S07；G4→S09；G5→全场景 `enable_llm=False` + S06/S08；
G6→S08；G7→S08；G8→S01；G9→S03/S05；G10→S10。

### 兼容性问题

- 数据模型、状态字段、`Action.as_dict()` 键集**均未改动**。
- `config.yaml` 的 `system.version` 只被 FastAPI 标题与 `/api/*` 的展示字段消费，
  **不参与任何行为分支**（已全仓确认），因此升版本对行为零影响。
- 表达层逃逸口只作用于 LLM 输出路径；规则路径语义逐字不变（120 轮零差异佐证）。

### 下一阶段

**v0.4**：真实 UWB / 眼镜 IMU / iPhone LiDAR / UE5 接入 —— 详见
`docs/V04_REAL_SENSOR_INTEGRATION_PLAN.md`（v0.3 只预留接口，不实现）。


## 附加（非 Stage）：更大更复杂的室内场景 + WASD 手动驾驶

这两项来自本轮的直接需求，不属于任务书的 Stage 序列，一并记录以免后人找不到出处。

### 场景：`办公楼-1F`（40 × 46 m）

`config/config.yaml` 的 zones / landmarks / static_objects 数据驱动，**代码零改动**。
含 16 个区域、43 个静态物体、7 个地标：入口大厅 / 核心办公区（办公桌）/ 北走廊 /
南走廊 / 东走廊 / 茶水间 / 会议室 / 打印区 / 前台 / 卫生间（男、女）/ 设备间（空调）/
休息区（沙发）/ 电梯厅 / 楼梯口 / 消防通道。
栅格 184 × 160 @ 0.25 m，可通行比例 0.61。

### 手动驾驶（WASD）

**线程模型**（这是本条目的重点）：外部（HTTP / WS）线程只调 `submit_drive(f, t)`，
往 `_drive_log` 里 **append 一条带时间戳的键位记录**；时间积分由主循环的
`_consume_drive(dt)` 完成。单次 `append` 在 CPython 里是原子的 ⇒ **不加锁也安全**，
"主线程独占写仿真"这条不变量得以保住（见项目记忆的"单写者 + 快照"）。

接口：`POST /api/control {"action":"manual","enabled":true}`、
`{"action":"drive","forward":1,"turn":0}`，回读在 `/api/state` 的 `stats.drive`
（`manual / forward / turn / pending_s`）。前端 `api/test_page.html` 监听 WASD，
按 W 时自动切手动（**先切成功再发键位** —— `set_manual()` 会清空按键日志，
顺序反了会把第一次按键吞掉），小地图给被接管的用户点画一个专用色高亮圈
（`#00e5a8` + `data-manual-ring` 属性，避免与路标同色导致冒烟测试无法区分）。

**实测（真实 HTTP 往返）**：

| 场景 | 结果 |
|---|---|
| 手动空转 | 读数位移 0.04 m（UWB 噪声量级）；**进程内实测真值 0.000 m** |
| 按住 W 3.0 s | 位移 **3.24 m**（1.1 m/s × 3 s ≈ 3.3 m） |
| 松手 | 按键状态清零，追加位移 0.28 m（噪声）后**停住** |
| 按住 A 0.9 s | 朝向 **−79.3°**（原地左转，位置漂移 0.25 m = 噪声） |
| 回到自动 | `manual=false`，按键清零，恢复自动导航 |

**一个已知特性（不是 bug）**：手动驾驶的时间积分发生在**仿真 tick**（1 Hz）上，
所以最后一次按键的时长最多**延迟一轮**才体现到位移上。进程内测得：
按住 2.2 s 只在第 1 轮结算 2.2 s 中的 1.0 s，第 2 轮再结算 1.0 s，
松手后还会结算最后 1 个 tick 内真正按住的那 1.0 s —— 总位移约 3.3 m ≈ 2.2 s × 1.1 m/s **+ 1 tick**，
之后立刻归零。也就是说**位移总量是准的，只是最后一秒晚一轮到位**。
在 1 Hz 的仿真里这是固有的（前端本身就 1 Hz 刷新），要消除必须做子 tick 积分，
那会破坏"快循环固定 1 Hz"已验收的保证，故**刻意不改**。

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `python main.py --selftest` | **74 / 74**（新增用例 71 手动驾驶） |
| 测试页冒烟 | `python tools/ui_smoke_test.py` | **44 / 44**（含手动驾驶 7 条：默认自动 / W 切手动 / W+D / 松 D / 全松 / 输入框不劫持 / 高亮圈画出与消失 / 按钮绑定） |
| 真实 API | 上表 6 项 | 全部通过 |


---

## Stage 12 —— 地图 v2 与「区域型障碍」（v0.3.0 收尾后增强）

> 触发：实测反馈「卫生间到走廊走不过去，被拦住了，没有门」，并要求
> 「场景更复杂、障碍物更多、加入区域型障碍」。

### 诊断结论（先证伪，再动手）

用三种互相独立的方式验证，结论一致：**地图连通性是好的，"走不过去"不是 bug。**

| 验证方式 | 结果 |
|---|---|
| 栅格渲染（`docs/map_v2_check.png` 同款脚本） | 7 个门洞全部实际存在，橙色标记清晰可见 |
| A\* 连通性探针 | 卫生间↔北走廊、入口→卫生间、各房间↔走廊 **全部 OK** |
| 无头实跑（规则模式，600 步上限） | **45 步精确到达 (11.5, 42.0)**，无卡死、无原地打转 |

**真正的原因**：卫生间只在**南墙**开了一扇门（`卫生间门口`，y=38~39）。
用户截图里智能体朝向 **282°（正西）**，往西推是卫生间的西墙 ——
**撞墙是正确行为，不是"没有门"**。这一点已写进 `config.yaml` 的门洞注释与
`docs/MAP_DESIGN_GUIDE.md`，避免下次再被误判。

### 改动清单

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `config/config.yaml` | ① 7 个门洞统一加宽到 **2.6m**（中心不变）；② 新增 **`area_objects`** 7 处区域型障碍；③ 新增 10 个点状障碍 | 门更宽更不容易被误判；场景更复杂；区域型障碍是新能力 |
| `simulator/map_simulator.py` | `MapObject` 增加 `hx/hy` + `is_area` + `distance_to`；`_build_walkable` 支持矩形扣除；`area_objects` 装配；新增 `door_effective_widths()` / `connectivity_report()` / `_flood_from()` | 让"一片区域"形状的障碍能被真实表达，并提供连通性体检 |
| `sensors/base.py` | `MapObjectInfo` 增加 `hx/hy` + `distance_to()`；新增模块级 `shape_distance()` | 区域障碍必须原样传到语义层，否则下游只会看到一个"中心点" |
| `fusion/state_fusion.py` | 障碍候选与语义物体改走 `shape_distance()` | 矩形障碍若按"圆心距 − 半径"算，6m×1m 围挡会被当成一个点 |
| `sensors/simulated/provider.py` | 障碍候选 / 语义候选 / 地图快照透传 `hx/hy` | 同上 |
| `maps/semantic_map.py` | `near()` / `describe()` 改按"到物体表面距离"排序 | 贴着围挡边时不该因为"中心 3m 远"而漏报 |
| `api/websocket_server.py` | `/api/map` 的 objects 增加 `hx/hy` | 前端才能把区域障碍画成矩形 |
| `api/test_page.html` | 区域障碍画矩形 + 7 个新中文标签 | 画成圆会让走廊"凭空被堵"一大截，和真实可通行区域对不上 |
| `main.py` | 启动自检打印区域障碍数量 | 让改动在启动时就可见 |
| `tests/selftest.py` | **新增用例 75**：房间可达 + 封闭房间有门 + 门洞有效宽度 + 无孤岛 | 把"房间被悄悄堵死"这类静默故障固化成硬门槛 |
| `docs/MAP_DESIGN_GUIDE.md` | **新建**：地图怎么改 + 验证三连 + 5 个已知陷阱 | 地图是数据驱动的，需要一份"改之前必读" |
| `README.md` / `docs/TEST_PLAN.md` / `.vscode/launch.json` | 自检项数 74 → **75**；小地图图例补充矩形障碍 | 数字与描述同步 |

### 区域型障碍为什么必须单开一类

圆形物体用 `x, y, radius` 描述就够了，但**围挡、柜墙、沙发组、工位岛是一整片区域**。
用圆形近似会同时错两件事：

1. **几何错**：6m×1m 的围挡近似成半径 3m 的圆 → 走廊中间凭空多出一堵圆墙，
   可通行面积被吃掉一大半；
2. **语义错**：用户贴着围挡边走时，系统按"圆心距 − 半径"算出"距离 3 米"，
   于是既不会提醒、也不会预警。

所以 `area_objects` 在**占用栅格 / 避障距离 / 语义场景 / 地图快照 / 前端渲染**
五处全部按矩形处理。形状判定统一走 `is_area()`，**不要用 `radius > 0` 反推**。

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `.venv/bin/python main.py --selftest` | **75 / 75**（新增用例 75：5 个房间全可达 / 7 个门洞有效宽 2.00m / 无孤岛） |
| 验收场景 | `.venv/bin/python main.py --acceptance` | **10 / 10**（S09 全程到达卫生间 46 轮） |
| 七拍 Demo | `.venv/bin/python main.py --acceptance-demo` | **7 / 7**（第 2 拍直行 12.1m、播报 1 条、重复 0 次） |
| 前端冒烟 | `.venv/bin/python tools/ui_smoke_test.py` | **44 / 44** |
| 端到端 | `.venv/bin/python tools/e2e_test.py` | 全部通过（图流 → 状态 → 多模态 → 行动） |
| 关键路径实跑 | 无头规则模式导航 | 45 步到达 (11.5, 42.0) |

### 本次踩到的坑

1. ★**门加宽会动到用例 7 的前提**★：用例 7 用 `landmarks[2]→[3]` 之间的路段找
   1m 内的门洞。所以加宽门洞**必须保持中心点不变**，否则前提不成立、用例会
   变成"测的是一条不存在的路径"。同理，**不要重排/插入 `route_landmarks`**。
2. ★**新障碍不能摆在中央通行带 x∈[18,22]**★：Demo 第 2 拍的断言是
   「12 秒直行 > 6m 且播报 ≤4 条无重复」。在主干道上摆障碍会把它变成
   "边走边绕"，直接测坏直行剧本。所有新增障碍都避开了这条带。
3. ★**"孤岛占比 0.0" 是个很值得守住的口径**★：一次收敛到的体检报告里，
   可通行格 17169 / 主连通分量 17169，说明没有任何家具围出的小密室。
   一旦这个数抬头，通常意味着某个矩形障碍把通道吃干净了。
4. **测量口径要对齐栅格**：门宽按"门的几何尺寸等分采样"会有半格相位差，
   同一扇门在不同分辨率下能差一格。改成**严格按栅格中心采样**后才稳定。
5. **`semantic_map.py` 的 `math` 在改完后变成未使用** —— 顺手删掉，
   避免留下"这里还算着什么几何"的误导。





---

## Stage 13 —— 「一直说正在为您重新规划路线」的两处根因 + 障碍密度回调

> 触发：用户实测后反馈「障碍又太多了，一直说为您重新规划路线、前面有障碍这类的话」。
> 结论：**用户听到的三句话分别来自三个不同的 bug，其中两句与障碍密度无关。**

### 诊断（先量化，再改）

对 `logs/BSA-2026-09-19.log`（一次 92 分钟的实测）做统计：

| 播报类型 | 次数 | 集中在哪 |
|---|---|---|
| `[WAIT]`（请停下…） | 206 | 全程，手动驾驶撞障碍时密集 |
| `[SPEAK]` | 277 | 全程 |
| `[ASK_USER]`（前方通路被挡住了…） | 62 | ★12:57–13:01 与 14:09–14:12 两段各 ~50 次/分钟★ |
| `[REPLAN]`（正在为您重新规划路线） | 6 | 同上两段 |
| 通道记录（被策略拦下但仍记录） | 281 × "前方通路被挡住了…" + 168 × "已到达目的地" | 同上 |

两段"每分钟 50 次"的时段是**卡死**：偏航状态一旦进入就再也出不来，决策层每 tick 都
重新问一遍同一句话。

### 根因 1（主因）★手动驾驶把"路径缓存作废"写成了"off_route"★

`NavigationSimulator.drive()` 里原本是：

```python
self.local_path = []      # 位姿被动过 —— 缓存路径作废
self.off_route = True     # ← 这一行是 bug
```

`off_route` 的语义是 **"A* 找不到可行路径"**，它经 `WorldReference.off_route` 直达
`RuleDecisionEngine.decide()` 第 3 步，直接命中「无路可走 → 重规划 → 向用户求助」。
而手动驾驶期间规划器**根本没被问过**：操作员按住 W，每走一步就把 `off_route` 置 True，
而手动模式不调用 `tick()`（`advance_user()` 走的是 `nav.drive()`），所以**没有任何代码
负责把它复位**。于是测试页上一按 WASD，系统就开始每秒念一遍
"正在为您重新规划路线 / 前方通路被挡住了"。

复现（`/tmp/bsa_offroute_repro.py`，改前）：

```
[手动按W] 轮0: kind=SPEAK  off_route=True   ← 只走了一步就"没路了"
[手动按W] 轮1: kind=REPLAN off_route=True
```

**修复**：`drive()` 只作废缓存，不改"有没有路"的结论；`next_instruction()` 在手动模式
如实说明"由操作员接管移动"，不再谎报重规划。

### 根因 2 ★"到达"与"求助"都被当成持续状态，于是每秒播一遍★

* `decide()` 第 1 步（到达）用 `force=True` 盖过最小播报间隔 —— 但 `force` 同时绕过了
  **重复检测**，而"到站后站着不动"意味着到达状态是**持续**的 ⇒ 一次实测里
  `已到达目的地` 被念了 **31 遍**，另有 168 条通道记录。
* 第 3 步（偏航）在 `OFF_ROUTE_REPLAN_LIMIT` 用尽后返回 `ASK_USER`，而 `ASK_USER` 的
  执行不触发重规划 ⇒ `off_route` 依然是 True ⇒ 下一 tick 再问同一句，**无限追问**。

**修复**：两条路径都加"只发生一次"的标记 —— 到达只播一次（后续 `CONTINUE` 静默）；
求助只问一次，之后保持安静，`OFF_ROUTE_ASK_COOLDOWN_S = 30s` 冷却到期（或偏航先恢复
再重新偏航）才允许再问。**行动语义不变**（该停的还停、该重规划的还重规划）。

### 根因 3（用户直接要求的部分）障碍密度回调

上一版（Stage 12）为了"场景更复杂"，一次性加了 7 个区域障碍 + 10 个点状障碍，
并把动态障碍开到 `max_active=4 / 间隔 6s / 寿命 6~18s`。
动态障碍**专门生成在用户正前方 2.5~5.5m 的 ±40° 锥内**，于是"每走两步就被按住一次"。

| 项 | 改前 | 改后 |
|---|---|---|
| `area_objects` | 7（cordon / cabinet / shelf / sofa_zone / desk_island / printer_zone / crowd_zone） | **4**（保留 cordon / cabinet / sofa_zone / desk_island） |
| 走廊点状障碍 | 10 | **4**（西走廊垃圾桶、东走廊垃圾桶、东走廊纸箱、办公区东侧纸箱） |
| `obstacles.max_active` | 4 | **2** |
| `obstacles.spawn_interval_s` | 6.0 | **10.0** |
| `obstacles.lifetime_s` | [6, 18] | **[5, 12]** |
| `obstacles.types` | chair / person / **narrow_passage** / box | chair / person / box |

去掉 `narrow_passage` 的理由：它一次生成**左右两个**障碍来人为制造窄缝，
是本项目里最像"系统故意堵路"的一类事件（一次触发"通道变窄"提醒 + 两次停步）。
逻辑本身保留（用例 11 仍用 `spawn(kind="narrow_passage")` 单测）。

> ⚠️ **诚实说明**：A/B 实测（10 个种子 × 无头规则模式）显示，
> 密度回调对**规则模式**的停步次数影响很小（WAIT 9 → 8，到达率都是 10/10）——
> 因为规则模式只在障碍真的进到 0.45m 内才停。用户感受到的"障碍太多"，
> 主要来自**根因 1/2 那两句被刷屏的话**。密度回调是按用户要求做的体验调整，
> 不是那两句刷屏的病因。

### 改动文件

| 文件 | 改了什么 |
|---|---|
| `simulator/navigation_simulator.py` | `drive()` 不再伪造 `off_route`；`next_instruction()` 增加手动模式分支 |
| `agent/decision.py` | 到达只播一次；偏航求助只问一次 + 30s 追问冷却；`_NavState` 增加 3 个标记 |
| `config/config.yaml` | 区域障碍 7→4、点状障碍 10→4、动态障碍三参数 + types 调整（均含"为什么"注释） |
| `tests/selftest.py` | 用例 18 增"到达只播一次"；用例 19 增"求助只问一次 + 冷却 + 恢复后可再问"；用例 71 增"手动驾驶不得伪造 off_route" |
| `tests/acceptance.py` | S09 改为收集整段导航的行动话术（到达只播一次后，末轮本来就安静，只看末句会误报"没告知"），并顺手钉住"到达话术不重复" |
| `tools/render_map.py` | **新增**：把 `config.yaml` 的地图画成 `docs/map_check.png`，肉眼核对障碍布局 |
| `docs/MAP_DESIGN_GUIDE.md` | 验证流程插入"先画图"；新增两条陷阱（障碍密度是验收项 / `area_objects` 只表达"一整片"） |

### 测试结果

| 关卡 | 命令 | 结果 |
|---|---|---|
| 自检 | `main.py --selftest` | **75 / 75** |
| 验收场景 | `main.py --acceptance` | **10 / 10**（S09 46 轮到达，到达话术 `已到达目的地` 只播 1 次） |
| 七拍 Demo | `main.py --acceptance-demo` | **7 / 7**（第 2 拍直行 12.1m、播报 1 条） |
| 前端冒烟 | `tools/ui_smoke_test.py` | **44 / 44** |
| 端到端 | `tools/e2e_test.py` | 全部通过 |
| 手动噪音 A/B | `/tmp/bsa_manual_ab.py`（60 轮按住 W） | 重规划+求助 **4 → 0** |
| 地图体检 | `tools/render_map.py` | 30 静态 + 4 区域障碍 / 7 门洞有效宽 2.00m / 孤岛比 0.000 |

### 本次踩到的坑

1. ★**同一个字段不要承担两种语义**★。`off_route` 被当成"路径缓存失效"用，是这次
   事故的全部成因。判断"要不要复用某个字段"的标准是：
   **它的下游消费者会怎么解读它** —— `off_route` 的下游是"要不要向盲人求救"。
2. ★**`force=True` 会同时绕过"重复检测"**★，所以任何**持续状态**引发的播报都不能用
   `force`，必须自己加"只发生一次"的标记。否则"N 遍"取决于用户站多久。
3. ★**"求助"必须有冷却**★。凡是"等对方回答"的动作，在没有冷却的情况下都会退化成
   噪音 —— 而且刷得越快，用户越听不出"系统卡住了"。
4. ★**只在最后一轮断言"有没有说话"是脆的**★。到达只播一次后，验收 S09 里"末轮话术"
   必然是空的；正确写法是收集整段行动的话术再断言。断言口径要跟着语义一起改。
5. ★**手动模式的行为也要有护栏**★。手动 WASD 是"实验员接管"，很容易被当成"不重要的
   调试通道"而漏掉断言 —— 这次的事故就发生在那里（用例 71 已补上 off_route 断言）。
