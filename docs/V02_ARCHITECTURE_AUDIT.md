# BlindSpatialAgent v0.2.0 架构审计报告

> 任务书对应：**Phase 0 / Stage 1 架构审计**
> 审计基准：`v0.2.0`（`config/config.yaml:10`）
> 审计方式：全量静态阅读 + 运行期行为复核，**未修改任何代码**
> 审计日期：2026-09-18

---

## 0. 审计范围与方法

### 0.1 阅读清单（按行数）

| 模块 | 文件 | 行数 |
|---|---|---|
| 入口 | `main.py` | 480 |
| 配置 | `config/loader.py` | 131 |
| 状态契约 | `spatial/spatial_state.py` | 341 |
| 状态融合 | `spatial/state_manager.py` | 405 |
| 世界模型 | `spatial/world_model.py` | 200 |
| 模拟器 | `simulator/map_simulator.py` | 404 |
| 模拟器 | `simulator/navigation_simulator.py` | 303 |
| 模拟器 | `simulator/obstacle_simulator.py` | 216 |
| 模拟器 | `simulator/sensor_simulator.py` | 121 |
| 决策 | `agent/agent_core.py` | 388 |
| 决策 | `agent/decision.py` | 313 |
| 决策 | `agent/llm_client.py` | 292 |
| 工具 | `agent/tools.py` | 348 |
| 提示词 | `agent/prompt_template.py` | 167 |
| 记忆 | `agent/memory.py` | 119 |
| 摄像头 | `camera/iphone_receiver.py` | 191 |
| 摄像头 | `camera/image_processor.py` | 89 |
| 服务 | `api/websocket_server.py` | 564 |
| 测试 | `tests/selftest.py` | 1191 |
| **Python 合计** | | **6423** |

另含 `api/test_page.html`（1462 行）、`ios/BSACameraStreamer/`（5 个 Swift 源文件）、`docs/技术交付文档.md`、`README.md`、`.vscode/launch.json`。

### 0.2 方法

1. **静态阅读**：逐文件读完全文，不依赖函数名推断行为。
2. **运行期复核**：核对「代码写的」与「实际跑的」是否一致（下文 P0-1、P0-2 均为运行期实测发现）。
3. **依赖方向比对**：用 `import` 关系反推模块的真实依赖图，与设计意图对照。
4. **实证**：所有结论均给出 `文件:行号` 证据；涉及库默认行为的结论均已用本机环境实测（见 P0-4）。

> 本次审计**不改代码**。Stage 2 起才动手。

---

## 1. 当前真实架构图

下面是**实际的**依赖关系（不是设计意图图）。实线 = 真实 import 依赖，`⚠` 标注越界依赖。

```
                         ┌──────────────────────────────────────────┐
                         │              main.py (480)               │
                         │   --live / --serve / --demo / --selftest │
                         └───────────────┬──────────────────────────┘
                                         │ 构造并注入
                                         ▼
        ┌────────────────────────────────────────────────────────────────────┐
        │        SpatialAgentSystem  (agent/agent_core.py:185)               │
        │                                                                    │
        │  ⚠ 直接 new 具体模拟器类（只此一处装配点，无抽象层）                 │
        │    MapSimulator / NavigationSimulator /                          │
        │    ObstacleSimulator / SensorSimulator                            │
        └───────┬───────────────────────────────────┬────────────────────────┘
                │                                   │
                ▼                                   ▼
   ┌────────────────────────────┐     ┌──────────────────────────────────────┐
   │  simulator/  (数据生产者)   │     │   spatial/  (融合层)                  │
   │                            │     │                                      │
   │  map_simulator   MapSim    │◄────┤ StateManager (state_manager.py:76)   │
   │  navigation_sim  NavSim    │◄────┤  ⚠ 构造签名接收 4 个模拟器具体类型     │
   │  obstacle_sim    ObsSim    │◄────┤  ⚠ state_manager.py:118-119 读真值:   │
   │  sensor_sim      SenSim    │◄────┤     self.nav.pos / self.nav.heading  │
   └────────────────────────────┘     │                                      │
                                     │  → SpatialState（唯一状态契约）        │
                                     │  → WorldModel（世界模型，单文件）      │
                                     └──────────────┬───────────────────────┘
                                                    │ SpatialState
                                                    ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │                     agent/  (决策层)                                    │
   │                                                                        │
   │  SpatialAgent (agent_core.py:64)                                       │
   │    ├── RuleDecisionEngine (decision.py:83)  ← 规则兜底 / 安全底线        │
   │    └── LLMClient (llm_client.py:33)         ← 多模态大模型 + 熔断        │
   │                                                                        │
   │  ⚠ AgentTools (tools.py:34) 直接持有 MapSim / NavSim / ObsSim 具体对象   │
   │  ⚠ tools.py:78-86 get_current_position() 读 self.nav.pos（仿真真值）    │
   └──────────────┬─────────────────────────────────────────────────────────┘
                  │ Action
                  ▼
   ┌────────────────────────────┐
   │  副作用出口（唯一）          │   tools.call()  → speak / ask_user /
   │  agent_core.py:267 _apply() │                  replan_route
   └──────────────┬─────────────┘
                  │
                  ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │   api/websocket_server.py (564)     FastAPI + 2×WebSocket              │
   │   ⚠:241-256 route_points()     直接读 system.nav.pos / local_path      │
   │   ⚠:258-271 dynamic_obstacles() 直接读 system.obstacles.obstacles      │
   │   → 模拟器真值绕过 SpatialState 直接进前端                             │
   └────────────────────────────────────────────────────────────────────────┘
                  ▲
                  │ JPEG 上行
   ┌──────────────┴─────────────┐
   │ camera/iphone_receiver.py  │  帧接收 / 节流 / 新鲜度 / 内存缓存
   │ ⚠:161-167 attach_camera()  │  手工把同一对象赋值给 4 个不同持有者
   └────────────────────────────┘
```

### 1.1 与任务书目标架构的差距对照

| 任务书要求的层 | 当前实现 | 状态 |
|---|---|---|
| Sensor | `simulator/` 四个具体类 | ⚠ 有，但无抽象 |
| **Sensor Adapter Layer** | **不存在** | ❌ 缺失 |
| State Fusion | `spatial/state_manager.py` | ⚠ 有，但与模拟器硬耦合 |
| **Spatial World Model** | `spatial/world_model.py` 单文件 | ⚠ 有，未分层 |
| **Event Engine** | **不存在** | ❌ 缺失 |
| **Safety Layer** | 混在 `agent/decision.py` 的 if-else 链里 | ❌ 未分离 |
| Cognitive Agent | `SpatialAgent` + `LLMClient` | ⚠ 有，但与规则未分离 |
| **Interaction Policy** | 散落在 `memory.repeated()` + `_say()` + `_apply()` 三处 | ❌ 未收拢 |
| Action Executor | `agent_core.py:267 _apply()` | ✅ 有，且是唯一出口 |

---

## 2. 数据流

### 2.1 一轮 tick 的真实路径（Terminal 与 Service 两条路）

```
[1] 障碍先动        obstacles.update(dt, nav.pos, nav.heading, elapsed)
                    agent_core.py:235

[2] 感知 + 融合      state_manager.build(elapsed, elapsed, now)
                    ├─ sensors.uwb_measure(nav.pos)      → UserState（加噪）★
                    ├─ sensors.imu_heading/imu_speed     → UserState（加噪）★
                    ├─ _build_environment(true_pos, true_heading)
                    │    ├─ sensors.wall_ahead_scan()      加噪 ✔
                    │    ├─ 静态阻挡物 + 动态障碍按真值算 distance/bearing ⚠ 未加噪
                    │    └─ sensors.lidar_measure()         加噪 ✔（只是给距离加噪）
                    ├─ _build_semantic(true_pos, true_heading)
                    │    └─ 直接用地图真值算 distance/direction  ⚠ 完全未加噪
                    ├─ _camera_state()                     camera.snapshot()
                    ├─ _evaluate_risk()                    ← 风险判定在融合层算好
                    └─ ConfidenceState(localization, perception)  ← 两个"配置常量"
                    state_manager.py:115-197

[3] Agent 决策      agent.decide(state, elapsed, query)      ★ 每轮都走
                    ├─ rules.decide()  → rule_action（安全基线）
                    └─ if llm.usable: _llm_decide()  → chat_multimodal()  ★ 每轮一次 HTTP
                    agent_core.py:88-108

[4] 行动            _apply(action)
                    ├─ tools.call("speak" | "ask_user")
                    └─ tools.call("replan_route")
                    agent_core.py:267-288

[5] 用户是否前进     nav.tick(dt, action.permits_motion, front_distance, obs_centers)
                    agent_core.py:249

[6] 广播（仅服务模式）broadcaster.broadcast(tick_payload())    ← 只在 step() 返回后才发
                    websocket_server.py:296
```

★ = 每轮必然发生；⚠ = 真实值未经过噪声模型。

### 2.2 数据流上的三个关键事实

1. **`SpatialState` 是唯一的跨层契约**，这一点守住了：`llm_client._build_messages()`（`llm_client.py:223-249`）只接收 `SpatialState` + 图片 + `extra` 摘要，**没有任何模拟器对象进入模型上下文**。
2. **真值绕过契约泄漏到了两处**：融合层内部的 `_build_environment` / `_build_semantic`（用真值算几何），以及 API 层的 `route_points()` / `dynamic_obstacles()`（把真值直接推给前端）。
3. **`risk` 是"预判结论"而非"感知事实"**：`_evaluate_risk()`（`state_manager.py:369-388`）在融合阶段就把风险等级算完，规则引擎只做读取。这让 Safety 逻辑无法独立演进，也无法表达不确定性。

---

## 3. 模块职责

| 模块 | 声明的职责 | 实际职责 | 评价 |
|---|---|---|---|
| `main.py` | 入口与模式分发 | 入口 + 6 个自检函数 + LAN IP 探测 + 终端渲染 | ⚠ 承担了 UI 与自检，建议拆 `cli/` |
| `config/loader.py` | 配置加载 | YAML 加载 + 零依赖 `.env` 解析 + 路径解析 | ✅ 干净 |
| `spatial/spatial_state.py` | 状态契约 | 状态定义 + 自洽性校验 + **`to_prompt_dict()`（提示词视图）** | ⚠ 提示词渲染越界 |
| `spatial/state_manager.py` | 多源融合 | 融合 + **风险判定** + **语义场景构造** + 世界模型写入 | ⚠ 四项职责过载 |
| `spatial/world_model.py` | 空间记忆 | 轨迹 + 障碍追踪 + 事件日志 + 原地打转检测 | ✅ 职责清晰，但未分层 |
| `simulator/*` | 数据生产 | 地图/导航/障碍/传感器噪声 | ✅ 边界清晰（作为可替换整体） |
| `agent/agent_core.py` | 决策 + 运行时 | 决策 + 系统装配 + 主循环 + 终端横幅 + `reset()` 就地重建 | ⚠ 装配与展示应外移 |
| `agent/decision.py` | 规则兜底 | Action 定义 + 规则引擎 + **打扰策略** | ⚠ 打扰策略应在 Interaction Policy |
| `agent/llm_client.py` | 模型接口 | 多模态调用 + 工具调用循环 + 熔断 | ✅ 设计良好 |
| `agent/tools.py` | 工具层 | 8 个工具 + 调用记账 | ⚠ 直接持有模拟器对象 |
| `agent/memory.py` | 记忆 | 对话 + 播报 + 状态快照 | ✅ 干净 |
| `camera/iphone_receiver.py` | 帧接收 | 接收 + 节流 + 新鲜度 + 落盘 | ✅ 双取帧语义设计优秀 |
| `api/websocket_server.py` | 服务层 | 14 个端点 + 主循环 + **模拟器内省** | ⚠ 主循环与内省越界 |

---

## 4. 已正确设计部分

这些是 v0.3 升级中**必须保住、不能重构掉**的资产：

### 4.1 四条架构约束在代码里是真的（不是文档口号）

| 约束 | 证据 | 结论 |
|---|---|---|
| 大模型不直接读传感器 | `llm_client.py:14-15` 注释 + `:223-249` 只组装 `SpatialState`/图片/摘要 | ✅ 成立 |
| 换硬件不改 Agent | `spatial_state.py:5-7` 契约层；Agent 侧无硬件 import | ⚠ 部分成立（融合层与工具层没做到） |
| 副作用必经工具层 | `agent_core.py:270-274` 注释 + `:275-279` 规则模式也走 `tools.call()` | ✅ 成立且一致 |
| 安全底线不可被模型推翻 | `decision.py:37` `MOTION_ALLOWED` + `agent_core.py:163-170` `_merge_with_safety()` | ⚠ 机制在，但覆盖条件过窄（见 P0-5） |

### 4.2 状态自洽性校验

`spatial_state.py:244-258` 用 `@model_validator(mode="after")` 校验 `front_distance` 不小于前方最近障碍距离。**发现矛盾立刻抛错而不是让它流到模型层**——这是很成熟的做法，v0.3 扩展字段时应继续沿用并扩充校验规则。

### 4.3 熔断机制

`llm_client.py:36-43, 75-121`：
- 区分 `available`（有配置）与 `usable`（可调用且未熔断），并在 `decide()` 里用 `usable`（`agent_core.py:94-96` 有明确注释说明为什么）；
- 按失败类型分档冷却：认证 600s / 404 600s / 限流 30s / 网络 15s；
- 一次成功即解除熔断。

这条设计避免了「Key 失效时 1 Hz 无限重试烧配额」，v0.3 必须保留。

### 4.4 双取帧语义

`iphone_receiver.py:150-176`：`latest_bytes()`（喂模型，过期必须 `None`）与 `preview_bytes()`（给前端，过期要能取到）。两者语义相反、**不可合并**，注释把理由写清楚了。这是全项目注释质量最高的地方。

### 4.5 工具层异常隔离

`tools.py:232-247`：所有工具调用包在 `try/except` 里，`TypeError` 单独处理成"参数错误"，其余异常转结构化错误。**模型乱传参数不会打崩主循环**。v0.3 分四类工具时应复用这个 `call()` 入口。

### 4.6 测试基线

`tests/selftest.py` 40 项离线自检（6.1s 跑完）+ `tools/ui_smoke_test.py` 34 项前端冒烟 + `tools/e2e_test.py` 11 项端到端。**升级期间的回归护栏已经存在**，这是 v0.3 敢做大改的前提。

---

## 5. 当前架构隐患

### 5.1 对照任务书八项重点检查

#### ① LLM 是否和模拟器耦合？—— **部分耦合：LLM 侧干净，融合层与工具层耦合**

- ✅ `LLMClient` 本身不 import 任何 `simulator.*`。
- ⚠ `spatial/state_manager.py:41-44` 在模块顶层 import 四个具体模拟器类，构造函数签名（`:79-88`）也要求具体类型：
  ```python
  def __init__(self, cfg, map_sim: MapSimulator, nav: NavigationSimulator,
               obstacle_sim: ObstacleSimulator, sensor_sim: SensorSimulator, ...)
  ```
- ⚠ `agent/tools.py:29-31` + `:40-46` 同样持有 `MapSimulator` / `NavigationSimulator` / `ObstacleSimulator` 具体对象。
- ⚠ `agent/agent_core.py:35-38` 在运行时直接 `new` 具体类。
- ⚠ `api/websocket_server.py:241-271` 穿透到 `system.nav` / `system.obstacles` 内部字段。

**结论**：所谓"换硬件不改 Agent"，当前只在 **LLM 决策这一条链路上**成立；一旦换成真实 UWB，需要同时改 `StateManager`、`AgentTools`、`SpatialAgentSystem`、`websocket_server` 四处。**这正是任务书第四节要建 Sensor Adapter Layer 的原因。**

#### ② SpatialState 是否职责过重？—— **是，四个层面都过重**

| 问题 | 证据 |
|---|---|
| 混入了"决策结论" | `risk` 由融合层 `_evaluate_risk()` 直接算完（`state_manager.py:369-388`），Agent 只是读取 |
| 混入了"提示词视图" | `to_prompt_dict()`（`spatial_state.py:264-312`）48 行渲染逻辑内联在状态模型里 |
| 缺任务书要求的六个子状态 | 见下表 5.2 |
| 未区分"事实"与"推断" | `navigation` 是规划器结论、`risk` 是规则结论、`semantic_scene` 是感知结果，三者混在同一层，无 `source` 标记 |

#### ③ 是否每次循环调用 LLM？—— **是，每秒一次多模态请求**

- `agent_core.py:243`：`step()` 每轮无条件调 `self.agent.decide(...)`。
- `agent_core.py:96-97`：`decide()` 只要 `self.llm.usable` 就调 `_llm_decide()`。
- `llm_client.py:169`：`_llm_decide` 内 `client.chat.completions.create(**kwargs)` 发真实 HTTP。
- `websocket_server.py:293`：服务模式 `await asyncio.to_thread(system.step, next_t)` 每轮触发。

**后果（实测，可复现）**：

| 指标 | 配置值 | 实测值 | 复现方式 |
|---|---|---|---|
| 循环频率（接真实慢模型） | `tick_hz: 1.0` | **0.52 Hz** | 假模型 stall=1.9s，复刻 `run_loop` 节拍公式 |
| 单轮 `step()` 耗时 | — | 1.91s（全部是 LLM 等待） | 同上 |
| 单轮 `step()` **纯计算**耗时 | — | **0.21 ms**（无 LLM，4698 Hz 能力） | 200 轮取平均 |
| 每轮 LLM 请求次数 | — | **1.0 次/轮**（6 轮 = 6 次） | 假模型计数 |

实测输出（`--live` 与 `--serve` 共用同一节拍公式）：

```
配置 tick_hz = 1.0  → next_t = 1.0s
复刻 websocket_server.py:287-302 / main.py:249-270 的循环节拍公式：

  第1轮 step 耗时 1.91s → sleep(0.00s)
  第2轮 step 耗时 1.90s → sleep(0.00s)
  ...
  第6轮 step 耗时 1.90s → sleep(0.00s)

6 轮实际墙钟 11.44s → 实测循环频率 0.52 Hz（配置 1.00 Hz）
LLM 调用次数 = 6（6 轮 = 6 次真实请求 → 每轮一次）
```

根因：`websocket_server.py:302` 与 `main.py:270` 都是
`sleep(max(0.0, next_t - elapsed))`；当 `elapsed (1.9s) > next_t (1.0s)` 时退化为 `sleep(0)`，
**循环频率被 LLM 调用时长直接决定**。同时前端状态只在 `step()` 返回后广播（`websocket_server.py:296`），
所以**画面与小地图的刷新率也被 LLM 延迟绑架**。

> **关键结论：瓶颈与算力无关。** 纯计算只要 0.21 ms，占一帧预算的 0.02%。
> 把循环卡在 0.52 Hz 的**唯一原因**就是那次同步阻塞的 HTTP 请求。

这直接对应任务书第八节的硬要求："不要每秒调用 LLM，事件触发 Agent"。

#### ④ 是否缺少 timestamp / source / confidence？—— **大面积缺失，且置信度是"假值"**

| 子结构 | timestamp | source | confidence | freshness |
|---|---|---|---|---|
| `SpatialState` 顶层 | ✅ | ❌ | ❌ | ❌ |
| `UserState` (PoseState) | ❌ | ❌ | ❌ | ❌ |
| `NavigationState` | ❌ | ❌ | ❌ | ❌ |
| `EnvironmentState` | ❌ | ❌ | ❌ | ❌ |
| `Obstacle` | ❌ | ❌ | ❌ | ❌ |
| `SceneObject` | ❌ | ❌ | ✅ 仅有 | ❌ |
| `RiskState` | ❌ | ❌ | ❌ | ❌ |
| `CameraState` | ✅ | ✅ | ❌ | ⚠ 用 `age_s` 间接表达 |

**更严重的是置信度失真**（已实测确认）：

```python
# simulator/sensor_simulator.py:95-97
def localization_confidence(self) -> float:
    return max(0.0, min(1.0, 1.0 - self.uwb_noise / 1.5))
```
`self.uwb_noise` 是配置常量（`config.yaml:117` = 0.15）⇒ `localization_confidence()` **恒等于 0.9**。
而规则阈值为 `min_localization: 0.4`（`config.yaml:144`）。

实测（跑 50 轮、收集所有出现过的置信度组合）：

```
50 轮中出现过的 (定位, 感知) 置信度组合：[(0.9, 0.167), (0.9, 0.5), ... (0.9, 1.0)]
                                          → 共 43 种，但第 1 位**全部是 0.9**
规则阈值 min_localization = 0.4
→ 低置信度分支是否可能触发 : False
```

**⇒ `decision.py:176-188` 的「定位置信度低 → 询问用户」分支在仿真里永不触发，是死分支。**
这也是任务书第十四节第 6 项「定位低置信度」测试场景当前**无法构造**的根本原因。

> 注：`perception_confidence` 是可变的（它用累计丢帧率算，实测在 0.167~1.0 之间），
> 所以"感知置信度"这一维是真的；**只有"定位置信度"是配置常量化出来的假值**。
> 换言之：**五维不确定性里，我们目前只有半维是真的。**

#### ⑤ 网络异常处理？—— **设计良好，但缺少"总时长预算"**

- ✅ `probe_llm()`（`websocket_server.py:117-126`）暴露原始状态码与错误正文，便于排查。
- ✅ `LLMClient` 熔断分档冷却（见 4.3）。
- ❌ **缺总时长预算**。这是本次审计发现的第二个 P0：

  `_ensure_client()`（`llm_client.py:123-133`）创建 `OpenAI` 客户端时**未指定 `max_retries`**。
  本机实测（`openai 3.14.1`）：
  ```
  OpenAI.__init__  max_retries 默认值 = 2
  ```
  ⇒ 单次 `create()` 最坏耗时 = `timeout_s (30s) × (1 + 2 次重试) = 90s`；
  再叠加工具调用循环上限 `max_tool_rounds: 3`（`config.yaml:32`）⇒ 单轮 `step()` 最坏
  **≈ 4 × 90s = 360s**。
  由于循环是 `asyncio.to_thread(system.step)` 同步等待，这 6 分钟里**整个主循环完全停摆**，
  且 UI 不会有任何更新 —— 用户会认为"系统死了"。

#### ⑥ Camera 断开处理？—— **不会崩，但"不会说话"**

- ✅ 过期判定：`snapshot()` 用 `stale_after_s`（`iphone_receiver.py:134-148`），过期即 `image_available=False`。
- ✅ 喂模型前再查一次新鲜度：`latest_bytes()` 过期返回 `None`（`:150-158`），模型不会看到旧画面。
- ✅ 断开捕获完整：`camera_ws` 同时处理 `WebSocketDisconnect` 与 `RuntimeError`（`websocket_server.py:524-530`），并打印累计帧数。
- ✅ `attach_camera()`（`:161-167`）在启动时保证四个使用方都能拿到 receiver。
- ❌ **缺 `CAMERA_LOST` 语义与事件**：断开后系统只是静默降级（`image_available=False`），
  Agent 既不会告知用户"我现在看不见了"，也不会切换到"仅依赖几何"的降级策略。
- ❌ **`CameraState` 无法区分"从未有过画面"与"曾经有过但断了"**：两者都是 `image_available=False`，
  只有 `source`（`"none"` vs `"websocket"`）勉强区分，语义不明确。
- ⚠ **计数器竞态（P2）**：`submit()` 里 `self.received += 1`（`:64`）、`self.throttled += 1`（`:71`）、
  `self.rejected += 1`（`:74`）、`self.accepted += 1`（`:97`）都在 `with self._lock` **之外**；
  而 `self._last_accept` 的读取（`:69`）也在锁外。事件循环线程与主循环线程并发写 ⇒ 统计数存在漂移。

#### ⑦ LLM 失败处理？—— **降级链完整，但"安全一票否决"名不副实**

- ✅ `decide()`（`agent_core.py:99-105`）：LLM 返回 `None` → 退回 `rule_action`，并记 `llm_fallback_count`。
- ✅ `_validate()`（`:131-161`）：未知 `action_type`、SPEAK/ASK_USER 空消息 → 一律丢弃并退回规则。
- ✅ 消息长度约束（`:146-148`）+ `urgency` 白名单校验（`:141-143`）。
- ❌ **`_merge_with_safety()` 的覆盖条件过窄**（`:163-170`）：
  ```python
  if rule.action_type == ActionType.WAIT and rule.urgency == "critical" and llm.permits_motion:
      return rule  # 只有这一种组合会被拦住
  return llm      # 其余一律采用模型结论
  ```
  也就是说：规则判 **REPLAN**（路线被挡、需要绕行）而模型说 **SPEAK"继续走"**，
  会采用模型结论；规则判 **ASK_USER** 而模型说继续走，也会采用模型。
  这与"安全底线不可被模型推翻"的目标不符 —— 当前实际是"**只否决一种组合**"。
- ❌ LLM 输出没有 `priority` / `confidence` 字段（`prompt_template.py:41-46` 的输出协议只有 4 个字段），
  使得"按优先级合并规则与模型结论"这件事无从下手。

#### ⑧ Agent 重复播报问题？—— **存在，且节流逻辑分散在三处**

| 位置 | 机制 | 问题 |
|---|---|---|
| `decision.py:264-271` `_say()` | `memory.repeated(text)` → 转 CONTINUE | 只比较**上一条**播报 |
| `decision.py:272-279` `_say()` | `seconds_since_speak < min_interval_s (4.0)` | 全局单一间隔，无按事件类型区分 |
| `decision.py:201-203` | 只有真播报出去才记 `last_nav_key/last_nav_speak` | ⚠ 已踩过坑，注释可见 |
| `agent_core.py:275` `_apply()` | 再查一次 `not memory.repeated(action.message)` | **与规则层重复**，语义不同源 |

**具体缺陷**：
1. `memory.repeated()`（`memory.py:90-93`）**只与最后一条比较** ⇒ `A → B → A` 的交替复读检测不到。
2. **无"事件 ID / 冷却窗口"概念**：同一障碍持续存在时，只要文本表述变化（例如障碍类型从 `chair` 变 `person`），就会重新播报；文本没变时又会被 `repeated` 拦掉，**用户可能压根没听到关键提示**（这是更危险的失败方向）。
3. **三处节流互不知情**：规则层放行的播报，可能被执行层拦下，两处记账口径不一致（`_apply` 拦下时 `memory` 不记录，但 `decision` 层已把 `last_nav_speak` 记账 → 出现"该说话时不说"的静默窗口）。
4. **无 `vibrate` / 提示音通道**：任务书第九节要求 Interaction Policy 决定"说/不说/震动/提示音"，当前只有"说"。

### 5.2 对照任务书第三节：`SpatialState` 缺什么

| 任务书要求 | 当前状态 |
|---|---|
| `PoseState`（position/floor/heading/speed/**walking_state**/confidence/timestamp/source） | ⚠ `UserState` 有前 4 项 + walking_status，缺 confidence/timestamp/source |
| `NavigationState`（…/**route_confidence**） | ⚠ 有 destination/route/next_instruction/distance/progress，缺 `route_confidence` |
| `EnvironmentState`（…/**blocking_ratio**/**walkable_width**/**stairs**/**dropoff**） | ⚠ `corridor_width` 勉强对应 walkable_width；`blocking_ratio`/`stairs`/`dropoff` **全缺** |
| `SemanticState`（objects/label/confidence/direction） | ✅ 基本对应 `SemanticScene` / `SceneObject` |
| **`AffordanceState`**（blocked_region / preferred_direction） | ❌ **完全缺失** —— 当前只能回答"有什么"，不能回答"能不能行动" |
| **`UncertaintyState`**（localization/perception/**semantic**/**route**/**overall**） | ⚠ `ConfidenceState` 只有前 2 项，且是常量假值（见 ⑤④） |
| `CameraFrameMetadata`（timestamp/frame_id/source/freshness/heading) | ✅ 最完整（缺 heading，但可接受） |

---

## 6. 技术债清单（P0 / P1 / P2）

### P0 —— 必须在本轮 v0.3 解决（阻塞任务书目标）

| # | 技术债 | 证据 | 影响 |
|---|---|---|---|
| **P0-1** | **每秒调用 LLM**，循环频率被模型延迟绑架（0.49 Hz vs 配置 1.0 Hz） | `agent_core.py:243`、`:96`；`websocket_server.py:296,302`；`main.py:270` | 实时性、配额消耗、UI 卡顿；直接违反任务书第八节 |
| **P0-2** | **无 Sensor Adapter Layer**，融合层/工具层/API 层三处硬耦合具体模拟器 | `state_manager.py:41-44,79-88`；`tools.py:29-31,40-46`；`websocket_server.py:241-271` | 接真实硬件要改四处，违反"换硬件不改 Agent" |
| **P0-3** | **无 Event Engine**，无事件类型/检测器/总线 | 全局无 `events/` 包 | 无法实现"事件触发 Agent"，也无法表达"障碍出现/清除"这类状态跃迁 |
| **P0-4** | **无总时长预算**：`OpenAI` 客户端未设 `max_retries`（实测默认 2）⇒ 单轮 step 最坏 360s | `llm_client.py:123-133`；实测 `openai 3.14.1 max_retries=2`；`config.yaml:29,32` | 主循环可能停摆数分钟且无任何反馈 |
| **P0-5** | **安全一票否决名不副实**，`_merge_with_safety` 只覆盖一种组合 | `agent_core.py:163-170` | 模型可推翻规则的非 critical 结论，安全性不可论证 |
| **P0-6** | **`AffordanceState` 缺失**，系统只能回答"有什么"，不能回答"能不能行动" | `spatial_state.py` 无该结构 | 任务书第三节新增核心；也是阶段二"安心行"的基础 |
| **P0-7** | **置信度是配置常量（恒 0.9）**，导致低置信度分支为死代码 | `sensor_simulator.py:95-97`；`config.yaml:117,144`；`decision.py:176-188` | 无法测试任务书第 14 节第 6 项场景 |

### P1 —— 应在 v0.3 内改善

| # | 技术债 | 证据 | 影响 |
|---|---|---|---|
| P1-1 | 环境/语义两路**未过噪声模型**，用仿真真值算几何 | `state_manager.py:133,149`（传 `true_pos/true_heading`） | 感知置信度失真；仿真过于乐观 |
| P1-2 | `risk` 在融合层预判，Safety 逻辑无法独立演进 | `state_manager.py:369-388` | 安全策略变更要改状态层 |
| P1-3 | 打扰策略分散三处（`_say` / `_apply` / `memory.repeated`），口径不一致 | 见 5.1⑧ | 该说时不说（静默窗口），或不该说时复读 |
| P1-4 | `memory.repeated()` 只比对上一条，`A→B→A` 复读检测不到 | `memory.py:90-93` | 复读体验差 |
| P1-5 | Action Schema 缺 `priority` / `confidence`，类型缺 `ALERT` / `REQUEST_VISUAL` | `decision.py:26-33,40-65`；`prompt_template.py:41-46` | 无法排序、无法主动感知 |
| P1-6 | `SpatialState.to_prompt_dict()` 把提示词逻辑内联进状态模型 | `spatial_state.py:264-312` | 状态模型职责外溢 |
| P1-7 | 无 Replay 能力，实验无法复现与跨模型对比 | 无 `recording/` | 无法做"同一实验回放不同模型" |
| P1-8 | `reset()` 用 `obj.__init__(...)` 就地重建 | `agent_core.py:335-341` | 脆弱：新增字段若忘了重置会静默串味 |
| P1-9 | `attach_camera()` 手工给 4 个持有者赋值 | `websocket_server.py:161-167` | 新增使用方容易漏接 |

### P2 —— 可延后

| # | 技术债 | 证据 |
|---|---|---|
| P2-1 | `IphoneReceiver` 计数器与 `_last_accept` 读写部分在锁外，存在竞态 | `iphone_receiver.py:64,69,71,74,97` |
| P2-2 | `main.py` 混入 6 个自检函数 + LAN IP 探测 + 终端渲染 | `main.py:87-213,319-386` |
| P2-3 | API 层穿透模拟器内部（`route_points` / `dynamic_obstacles`），把真值推给前端 | `websocket_server.py:241-271` |
| P2-4 | `SYSTEM_PROMPT` 与输出协议为硬编码字符串，无版本号、无 A/B 能力 | `prompt_template.py:19-55` |
| P2-5 | `OpenAI` 客户端创建后未显式关闭（长生命周期复用，实际影响小） | `llm_client.py:128-132` |
| P2-6 | `world_model` 只用 `elapsed`（仿真时间），与 `CameraState` 的墙钟时间混用 | `world_model.py:84` vs `iphone_receiver.py:88` |

---

## 7. v0.3 迁移方案

### 7.1 总原则：**打桩不换血**

1. **新层以"并列"方式引入，不删除老实现**。`sensors/simulated/` 包装现有四个模拟器，
   `fusion/` 包装现有 `StateManager`，`events/` 包装现有规则判定 —— 先让新架构跑通，
   再逐步把老逻辑搬进去。
2. **`SpatialState` 只增不删**。所有新字段给默认值，`extra="forbid"` 保持不变；
   老测试因此可以继续通过。
3. **每一步都用 40 项自检 + 34 项前端冒烟守门**，红了就回滚，不做"一次性大重构"。
4. **循环频率与安全链路优先**：P0-1 / P0-4 / P0-5 属于"系统能不能用"级别，先于架构美化。

### 7.2 阶段映射（对应任务书第十七节）

| Stage | 内容 | 解决的技术债 | 关键产出 |
|---|---|---|---|
| **1** | 架构审计 | — | 本文件 |
| **2** | 数据模型升级 | P0-6, P0-7, 5.2 全部 | `PoseState`/`AffordanceState`/`UncertaintyState` 等，附 `timestamp`/`source`/`confidence` |
| **3** | Sensor Adapter | P0-2 | `sensors/base.py` + `sensors/simulated/` + `sensors/future/` |
| **4** | State Fusion | P1-1, P1-2 | `fusion/state_fusion.py` + `confidence.py` + `freshness.py`；risk 移出融合层 |
| **5** | World Model | P1-7（部分） | `world_model/` 五个子模块；当前状态 / 30 秒短期 / 长期空间知识三层 |
| **6** | Event Engine | P0-3 | `events/event_types.py` + `event_detector.py` + `event_bus.py`，8 类事件 |
| **7** | 双循环分离 | **P0-1, P0-4, P0-5** | `agent/safety_agent.py`（高频、不依赖 LLM）+ `agent/cognitive_agent.py`（低频） |
| **8** | Interaction Policy | P1-3, P1-4, P1-5 | `agent/interaction_policy.py` + 新 `Action` Schema（含 `priority`/`confidence`） |
| **9** | Replay | P1-7 | `recording/session_recorder.py` + `session_player.py` |
| **10** | Active Perception | P0-6（应用） | `request_visual_observation()` 工具 + `REQUEST_VISUAL` 动作 |

### 7.3 针对 P0-1 的具体设计（最高优先级）

**问题**：`step()` 同步阻塞 LLM，把感知频率锁死在模型延迟上。

**方案**：把 `step()` 拆成两个独立节奏的循环，二者通过事件队列解耦。

```
┌─ Safety Loop（固定 10 Hz，纯几何，不依赖 LLM、不依赖网络）──────────────┐
│  sensors → fusion → SafetyAgent.decide() → 立即执行 ALERT/WAIT          │
│  保证：即使 LLM 卡 6 分钟，用户侧的安全提醒照常                               │
└───────────────────────────────────────────────────────────────────────┘
                              │ 事件入队（非阻塞）
                              ▼
┌─ Event Bus（内存队列，带去重与冷却窗口）────────────────────────────────┐
└───────────────────────────────────────────────────────────────────────┘
                              │ 仅"值得推理"的事件触发
                              ▼
┌─ Cognitive Loop（单线程 worker，串行消费，带总时长预算）─────────────────┐
│  从队列取事件 → CognitiveAgent.reason() → 产出 Action                    │
│  预算：单次 LLM 调用 max_retries=0 + 总 deadline ≈ 3s，超时即放弃推理      │
└───────────────────────────────────────────────────────────────────────┘
```

要点：
1. **Safety Loop 不 await 任何网络**，只跑纯几何判定（距离/通道宽度/台阶/坠落）。
2. **Cognitive Loop 独立线程/任务**，用 `queue.Queue` 消费事件；队列空就闲置 —— 自然实现"不每秒调 LLM"。
3. **LLM 调用加总预算**：`OpenAI(max_retries=0, timeout=2.5)` + `deadline` 兜底；超时走规则降级。
4. **广播解耦**：`run_loop` 的广播不再挂在 `step()` 之后，而是按固定 4~8 Hz 独立推送最新状态。

### 7.4 风险与取舍

| 风险 | 缓解 |
|---|---|
| 新老两套状态定义并存，易混淆 | `SpatialState` 只增字段、不改语义；新增字段一律给默认值 |
| 双循环引入并发 bug | Safety Loop 单线程；Cognitive Loop 单 worker 串行；共享状态只读快照 |
| 事件去重不当导致丢事件 | 事件带 ID + 冷却窗口；去重键 = `(事件类型, 目标实体 ID)` |
| 40 项自检写死了老行为 | 先跑基线，逐项标注"哪些断言需要随语义更新"，不改判定标准只改期望值 |
| Token Plan 额度消耗 | 事件驱动后 LLM 调用频次应从 ~1 Hz 降到"每次环境变化一次"，实测对比写入文档 |

---

## 8. 修改文件列表

### 8.1 新增（预期）

| 路径 | Stage | 说明 |
|---|---|---|
| `sensors/__init__.py` | 3 | 惰性导出（沿用现有 PEP 562 约定） |
| `sensors/base.py` | 3 | `SensorProvider` 抽象基类 + `RawObservation` 契约 |
| `sensors/simulated/__init__.py` | 3 | |
| `sensors/simulated/provider.py` | 3 | `SimulatedProvider`，包装现有四个模拟器 |
| `sensors/future/__init__.py` | 3 | UWB / GlassesIMU / IPhoneLidar / UE5 的占位与接口约定 |
| `fusion/__init__.py` | 4 | |
| `fusion/state_fusion.py` | 4 | 多源异构频率融合 |
| `fusion/confidence.py` | 4 | 五维置信度计算（替换常量假值） |
| `fusion/freshness.py` | 4 | 过期/缺失判定 |
| `world_model/__init__.py` | 5 | |
| `world_model/world_model.py` | 5 | 顶层门面 |
| `world_model/spatial_memory.py` | 5 | 长期空间知识（房间/门/POI） |
| `world_model/trajectory_memory.py` | 5 | 轨迹与 30 秒短期历史 |
| `world_model/object_memory.py` | 5 | 物体追踪 |
| `world_model/semantic_world.py` | 5 | 语义层 |
| `world_model/affordance_world.py` | 5 | 可行动性推理 |
| `events/__init__.py` | 6 | |
| `events/event_types.py` | 6 | 8 类事件枚举 + 事件数据类 |
| `events/event_detector.py` | 6 | 状态跃迁 → 事件 |
| `events/event_bus.py` | 6 | 队列 + 去重 + 冷却 |
| `agent/safety_agent.py` | 7 | 高速确定性安全循环 |
| `agent/cognitive_agent.py` | 7 | 低频多模态推理 |
| `agent/interaction_policy.py` | 8 | 说/不说/震动/提示音 |
| `agent/action_schema.py` | 8 | 新 Action（含 priority/confidence） |
| `recording/__init__.py` | 9 | |
| `recording/session_recorder.py` | 9 | state/event/action/image/latency 落盘 |
| `recording/session_player.py` | 9 | 回放与跨模型对比 |
| `tests/test_v03_*.py` | 2–10 | 各阶段新增测试 |

### 8.2 修改（预期）

| 路径 | Stage | 改动要点 | 回归风险 |
|---|---|---|---|
| `config/config.yaml` | 2,4,6,7 | 新增 `fusion` / `events` / `loops` / `affordance` 段；`llm.max_retries`、`llm.total_deadline_s` | 低（只增段） |
| `spatial/spatial_state.py` | 2 | 新增 6 个子状态；`risk` 从契约降级为 Safety 层产物（保留字段做兼容） | **高**（自检多处在用） |
| `spatial/state_manager.py` | 4 | 改为 `fusion/` 的门面；移除 `_evaluate_risk`；接收 provider 而非具体模拟器 | **高** |
| `spatial/world_model.py` | 5 | 逻辑迁入 `world_model/`，本文件保留为兼容 re-export | 中 |
| `agent/agent_core.py` | 3,7 | 装配改为注入 provider；`step()` 拆分为 Safety/Cognitive 两侧驱动 | **高** |
| `agent/decision.py` | 7,8 | 规则拆为 `SafetyAgent` + `InteractionPolicy`；保留 `RuleDecisionEngine` 作为兼容层 | **高** |
| `agent/tools.py` | 3,10 | 改为依赖 provider/fusion；工具分四类；新增 `request_visual_observation()` | 中 |
| `agent/memory.py` | 8 | `repeated()` 改为窗口内匹配；新增事件冷却查询 | 中 |
| `agent/llm_client.py` | 7 | `max_retries=0` + 总 deadline；输出协议加 `priority`/`confidence` | 低 |
| `agent/prompt_template.py` | 8 | 输出协议升级；`to_prompt_dict()` 逻辑迁入此处 | 中 |
| `api/websocket_server.py` | 7 | 主循环改为双循环 + 独立广播节奏；`route_points` 改走 fusion/地图工具 | 中 |
| `camera/iphone_receiver.py` | 4,6 | 计数器加锁；新增"曾有过/已断开"状态；发 `CAMERA_LOST` | 低 |
| `main.py` | 7 | `run_live` 频率与 `--serve` 对齐；新增 `--replay` | 低 |
| `tests/selftest.py` | 2–10 | 40 项 → 预计 60+ 项 | — |
| `README.md` / `docs/*` | 全程 | 同步更新 | — |

### 8.3 明确不动

- `simulator/` 四个文件：**整体保留为 `SimulatedProvider` 的数据后端**，仅在必要时补"真值出口"开关。
- `config/loader.py`：零依赖 `.env` 解析已验证可用，不动。
- `camera/image_processor.py`：接口稳定，不动。
- `api/test_page.html`：前端契约（3 条摄像头预览路径）不动，只做增量适配。
- `ios/BSACameraStreamer/`：不涉及。

---

## 9. 审计结论

**一句话**：v0.2.0 在**契约层（`SpatialState`）与安全兜底（熔断、工具出口、规则降级）**上打下了扎实基础，
但**层次划分只做了一半** —— 契约之上仍是一个"融合层兼做风险判定、决策层兼做装配与显示、
循环频率被模型延迟绑架"的整体式实现。

**v0.3 的真正目标**因此不是"加功能"，而是**把已经写在注释里的四条架构约束，
落实成代码里的依赖方向**：让 Agent 真的看不见硬件、让 Safety 真的不依赖 LLM、
让每一次打扰都经过同一套策略。

**最高优先级的三个动作**（按收益排序）：
1. **拆双循环 + 给 LLM 加总预算**（P0-1 / P0-4）—— 让系统在最坏情况下仍然可用。
2. **建 Sensor Adapter + 事件引擎**（P0-2 / P0-3）—— 这是"接真实硬件"与"事件驱动"的地基。
3. **补 `AffordanceState` + 真实置信度**（P0-6 / P0-7）—— 从"有什么"进化到"能不能行动"，
   同时让低置信度这条安全策略真正活起来。

---

**审计结束。** 下一步：**Stage 2 数据模型升级**（`SpatialState` 扩展为 Egocentric Spatial State）。
> Action Schema（任务书第十节）按第十七节的顺序放在 **Stage 8** 与 Interaction Policy 一起做，
> 不提前到本阶段 —— 避免在双循环尚未拆分时先改动作协议，导致改动面互相纠缠。
> 各阶段的「修改文件 / 修改原因 / 测试结果 / 下一阶段」记录见 `docs/V03_MIGRATION_LOG.md`。
