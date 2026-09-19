# BlindSpatialAgent 系统架构文档（v0.3）

> 适用版本：`config/config.yaml:system.version = 0.3.0`（Stage 11 收尾后已由 `0.2.0` 升级）。本文描述的是 Stage 1~10 已交付、Stage 11 收尾完成后的架构。所有类名、字段名、函数名均经源码核对。
>
> 设计总纲（任务书第三十六节）：
>
> ```
> Raw Sensors → Structured Observations → State Fusion → Egocentric Spatial State
> → Spatial World Model → Events / Risk / Uncertainty → Agent Reasoning
> → Interaction Decision → Human Action → New Perception
> ```

---

## 1. 系统整体图

系统严格分层，箭头表示数据流向（自底向上为感知→决策→执行，右侧为旁路通道）。除 `SpatialAgentSystem.__init__` 外，任何模块不得 `import simulator`（铁律 7）。

```
+=====================================================================================+
|  外部硬件 / 仿真器（未来可替换）                                                     |
|  UWB · IMU · LiDAR/深度相机 · iPhone摄像头 · UE5数字孪生                              |
+====================+================================================================+
|  simulator/        |  map_simulator / navigation_simulator / obstacle_simulator       |
|  （仅此层认识真相  |  / sensor_simulator —— 几何真值、随机噪声、动态障碍生成           |
|   与随机源）       |                                                              |
+====================+=======[ 只在此边界被装配成 SensorProvider/WorldStepper ]======+
|  传感器适配层            |  sensors/base.py                                              |
|  Sensor Adapter Layer    |  Protocol: SensorProvider（只读）/ WorldStepper（推进世界）    |
|                         |  CameraProvider（摄像头）                                      |
|                         |  实现: sensors/simulated/SimulatedProvider（同时实现两协议）    |
+=========================+==============================================================+
|  感知融合层              |  fusion/state_fusion.py（组装）                                |
|  Perceptual Fusion       |  fusion/confidence.py（置信度估计）                            |
|                         |  fusion/freshness.py（新鲜度三态）                             |
|                         |  spatial/state_manager.py（薄门面，只转发）                    |
+=========================+==============================================================+
|  世界模型层              |  world_model/（当前/短期/长期/经验四层）                       |
|  Spatial World Model     |  maps/（度量/语义/拓扑/经验四张地图）                          |
+=========================+==============================================================+
|  状态契约                |  spatial/spatial_state.py → SpatialState（唯一状态契约）        |
+=========================+==============================================================+
|  事件引擎                |  events/event_types · event_detector · event_bus ·            |
|  Event Engine           |  event_history · event_engine                                  |
+=========+===============+==============================================================+
|  双循环编排  |  快循环（主线程）：agent/safety/safety_engine.py               |
|  Agent Core|             agent/orchestrator.py → CognitiveLoop 调度          |
|            |  慢循环（工作线程）：agent/cognitive_agent.py（多模态LLM）      |
+=========+===============+==============================================================+
|  表达层      |  agent/interaction_policy.py（说不说/说多少/走哪通道）         |
|  Expression |  agent/action_schema.py（ActionType 10种 + Urgency）           |
+============+==============================================================+
|  执行层      |  agent/tools.py（AgentTools.call，唯一副作用出口 + RLock）     |
|  Execution  |  agent/agent_core.py → SpatialAgent.apply_action()             |
+============+==============================================================+
     ↓ 旁路通道（不回写状态、不改变决策输入）
     camera/iphone_receiver.py · camera/image_processor.py（图像预处理）
     recording/（schemas · session_recorder · session_player）
     api/websocket_server.py（FastAPI：/api/state · /api/control · 测试页）
```

---

## 2. 数据流（从传感器到行动）

每轮 `SpatialAgentSystem.step()` 的固定顺序（见 `agent/agent_core.py::SpatialAgentSystem.step`，已核对源码）：

```
1. env.advance_environment(dt)      ← 先推进环境（障碍移动），此时用户还没动
2. state_manager.build(...)         ← 感知 + 融合，产出 SpatialState；
                                     memory.add_state_snapshot(state)
3. events.process(state, elapsed)   ← 事件引擎：状态迁移 → 事件
4. orchestrator.decide(...)         ← 安全快循环(每轮必跑) + 认知慢循环结果(可能无)
                                     + 规则基线，三者合成一个 Action
5. agent.apply_action(action)       ← 执行行动（经 tools.call 派发副作用）
6. env.advance_user(dt,            ← 推进用户位姿（人机协同关键开关：
            motion_allowed=...,        motion_allowed = action.permits_motion）
            front_distance=...)
```

注意（重要）：第 2 步产出的 `state` 描述的是「本 tick 移动之前」的位姿，比仿真器真值**晚一拍**。真值只能读 `system.nav.pos` / `system.nav.heading`（来自 `WorldStepper.world_reference()`），不要拿 `SpatialState.user` 当真值。

逐段说明：

- **传感器读数 → 候选**：`SensorProvider.read_pose()` / `perceive()` / `scan_wall_ahead()` / 逐障碍 `measure_depth()` / `scan_sides()`。注意调用顺序即随机数消耗顺序（见铁律与 `fusion/state_fusion.py` 注释），不可调整。
- **融合 → SpatialState**：`StateFusion.build()` 把多源读数归一成结构化状态：位姿、导航、环境、语义、摄像头、风险、五维不确定性、可行动性。这一步是「大模型只认 SpatialState」的落实点。
- **世界模型记账**：融合末尾 `world.observe_state(state)` 等纯记账调用，不回写状态、不消耗随机数。
- **事件 → 决策输入**：`EventEngine.process()` 只读 state/world，产出 16 类事件（`events/event_types.py::EventType` 枚举实际成员数；源码 docstring 写的「15 类」是历史描述），经总线冷却后交给 Safety 与认知触发判定。
- **决策 → Action**：`AgentOrchestrator.decide()` 合成安全层、认知结果、规则基线。
- **执行 → 副作用**：`SpatialAgent.apply_action()` 经 `AgentTools.call()` 派发 `speak` / `ask_user` / `replan_route`，这是**唯一**对外副作用出口。
- **行动 → 世界推进**：`WorldStepper.advance_user(motion_allowed=action.permits_motion, front_distance=...)`，把「用户能否前进」回灌给世界。

---

## 3. Fast Loop（安全快循环）

**定位**：主线程每 tick 必跑的纯计算路径，是盲人在场安全的保障。

- **频率**：由 `config.yaml:system.tick_hz = 1.0` 决定，默认 1 Hz。实测主循环纯计算约 `0.21 ms`/轮，远高于 1 Hz 所需。
- **每轮做什么**（见 `step()` 步骤 1~6 与 `AgentOrchestrator.decide`）：
  1. 推进环境（用户未动）。
  2. 融合出 `SpatialState`。
  3. 事件引擎求值（只读）。
  4. `SafetyEngine.evaluate(state, events, t)` 先做安全判定（纯计算、不等 IO）。
  5. `CognitiveLoop.poll(t)` 取上轮认知结果（可能为空/过期）。
  6. `RuleDecisionEngine` 算规则基线（始终在跑）。
  7. 合成 → `apply_action` → `advance_user`。

- **为什么不能阻塞**：大模型推理被**异步提交**给认知工作线程（`orchestrator.decide` 内部只是非阻塞 `submit`），本轮行动先由安全层 + 规则基线给出，认知结果在**后续轮次**被 `poll()` 取用。因此「LLM 卡住 5 秒」不会让这一轮变慢——安全判定照常每轮发生（硬验收：LLM 睡 5 秒时快循环仍 1 Hz，见 `tests/selftest.py` 用例 60/63）。

- **SystemConfig**（`agent/agent_core.py::SystemConfig`）：
  | 字段 | 含义 |
  |---|---|
  | `seed` | 随机种子（可复现仿真） |
  | `enable_obstacles` | 是否启用动态障碍 |
  | `enable_llm` | 是否启用大模型（关闭则关掉认知线程，回退纯规则模式） |

- **线程模型**：主线程是**唯一**修改 WorldModel / StateManager / memory / 模拟器的线程；认知工作线程只读写 `Bundle` 快照与自己的 `_result`。工具执行由 `AgentTools.call()` 内的一把 `threading.RLock` 串行化，**锁只在执行的几毫秒内持有，绝不跨越网络等待**，因此 LLM 再慢也不挡快循环。

---

## 4. Cognitive Loop（认知慢循环）

**定位**：独立工作线程里的低频多模态推理，产出可被主线程取用的 `Action`。

- **工作线程**：`agent/orchestrator.py::CognitiveLoop`，守护线程 `bsa-cognitive`。主线程在 `SpatialAgent.build_context()`（或 `orchestrator.submit`）把 `state.model_copy(deep=True)` + 事件副本 + 历史副本 + 图字节打成 `ContextBundle` 交给它；工作线程只读 `bundle.model_copy` 的内容，不碰 memory/events/camera 原始对象（避免 `RuntimeError: deque mutated during iteration`）。
- **轮询周期**：由 `CognitiveTrigger` 决定「值不值得想一次」：
  - 用户提问 → 必想；
  - 命中 `CognitiveTrigger.IMPORTANT` 事件集 → 想；
  - 否则按心跳：`cognitive.heartbeat_s = 5.0`；
  - 且 `cognitive.min_interval_s = 2.0` 最小间隔（防事件风暴）。
- **快照只读**：`CognitiveAgent.decide(bundle)` 只允许读 `bundle`，不得再访问 memory/events/camera/world；任何异常就地吞掉返回 `None`，认知失败不影响快循环。
- **结果 TTL**：`cognitive.result_ttl_s = 4.0`。主线程 `poll(t)` 取走即清空；提交后超过 4 个仿真秒的结果被丢弃（避免「3 秒前的结论」误导现在）。时间戳只用**仿真时刻**（`_result_at = job.submitted_at`），严禁把墙钟延迟加进去（量纲混用会导致「答了却不生效」）。
- **非阻塞 submit**：`CognitiveLoop.submit()` 即时组装上下文并写入 `_pending`（只保留最新一帧，过时的被覆盖并计 `dropped`），立刻返回，不等待 LLM。

配置段（`config.yaml`）：

| 键 | 默认 | 含义 |
|---|---|---|
| `cognitive.enabled` | true | 关闭则完全回到规则模式，进程内不建线程 |
| `cognitive.heartbeat_s` | 5.0 | 无事件时也定期想一次 |
| `cognitive.min_interval_s` | 2.0 | 两次认知最小间隔 |
| `cognitive.result_ttl_s` | 4.0 | 认知结果保鲜期 |
| `cognitive.query_timeout_s` | 3.0 | 用户提问后等大模型多久，超时由规则基线抢答 |

---

## 5. Event Flow（事件流）

链路：`状态迁移 → 检测 → 总线 → 订阅者 → 触发 Agent / 触发视觉请求`。

- **状态迁移 → 检测**：`events/event_detector.py` 的 6 个检测器**只读** `state` 与 `world`，做边沿检测（`absent→present` 才报 `OBSTACLE_APPEARED`，`present→present` 不报）。首帧只做基线登记，不发声（安全类迁移如 `HIGH_RISK`/`ROUTE_BLOCKED` 首帧照常）。
- **总线**：`events/event_bus.py::EventBus` 用三道闸去重：① 边沿检测（检测器）；② 冷却（同 `key()` 在 `cooldown_s` 内被吞并记 `suppressed_count`）；③ 主体区分（椅子 A ≠ 椅子 B）。`critical` 事件**豁免冷却**。
- **订阅者 / 消费方**：`EventEngine.process(state, t)` 返回落定事件列表，被两处消费：
  - `SafetyEngine.evaluate(state, events, t)` —— 安全层直接读事件做干预判定；
  - `CognitiveLoop.should_think(events, ...)` —— 命中 `IMPORTANT` 集才触发认知调用。
- **触发 Agent / 触发视觉请求**：事件进入认知提示词（`events` 摘要），并经 `agent/active_perception.py::VisualRequestPolicy` 在「地图-传感器冲突 / 低定位置信度 / 摄像头丢失」等条件下登记一次取帧请求（详见第 7 节）。

16 类 `EventType`（见 `events/event_types.py`，源码 docstring 标注「15 类」为历史描述，实际枚举成员为 16）、4 级 `Severity`、`DEFAULT_COOLDOWN_S` 与 `DEFAULT_SEVERITY` 均定义在该文件，冷却时长可由 `config.yaml:events.cooldown_s` 逐项覆盖（键名 = `EventType.value`，大写；未知键只告警不抛异常）。

---

## 6. Agent Flow（决策流）

合成优先级（高 → 低，`AgentOrchestrator` 与 `SpatialAgent`）：

```
1. 规则基线（RuleDecisionEngine）
      ↓ 永远在跑（确定性），是安全骨架
2. 安全层 EMERGENCY
      ↓ intervene=True 时直接采用基线动作（保证拦得住且不等 LLM）
3. 认知慢循环结果（若在有效期内且未过期/未低置信）
      ↓ gate_expression() 表达审查 → _merge_with_safety() 安全仲裁
4. 表达层闸门（InteractionPolicy.gate）
      ↓ 只作用于大模型输出，不改变物理语义（铁律 5）
5. 执行（SpatialAgent.apply_action → tools.call）
```

- **规则基线**：`SpatialAgent.baseline()` → `RuleDecisionEngine.decide()`，经 `InteractionPolicy.allow()/allow_stop()`（两道闸语义与 v0.2 一致）。它是快循环的骨架，也是「安全底线不可被模型推翻」的实现位置。
- **认知决策**：`CognitiveLoop.poll()` 取回的 `Action`，先过 `gate_expression()`（说不说/说多少/走哪通道）。若闸门判定 `discard`（过期 `expired` 或低置信 `low_confidence`），**整条退回规则基线**（不可信结论不得拥有物理否决权）；其余闸门（重复/最小间隔/巡航静默/用户说话/通道禁用/静音）只清话术、保留行动的物理语义。
- **合并**：`_merge_with_safety()`——规则判定 `WAIT(critical)` 时，模型不能让用户继续走（`permits_motion` 不被推翻）。
- **执行**：`SpatialAgent.apply_action()` 只把 `action.speaks`（有内容且通道能出声）的播报经 `tools.call("speak"/"ask_user")` 派发；`REPLAN` 经 `tools.call("replan_route")`。所有对外副作用唯一出口是 `AgentTools.call()`（铁律 3）。

---

## 7. Camera Flow（摄像头流）

链路：`手机推流 → 接收器 → 帧存储 → 主动感知按需取帧 → 预览`。

- **手机推流**：iPhone 经 `WS /ws/camera` 上传 JPEG（`api/websocket_server.py`），或浏览器表单 `POST /api/image`。
- **接收器**：`camera/iphone_receiver.py::IphoneReceiver.submit()` 校验 → `camera/image_processor.py::prepare_for_llm()` 缩放(最长边 `max_image_side=768`) + 重新编码(`jpeg_quality=60`) → 落内存 `_latest`（已处理，供模型）与 `_raw`（原图，保留）。按 `max_fps=8` 节流（判据 `now - 上次接受 < 1/max_fps`，给客户端负抖动留余量）。
- **帧存储元数据**：`snapshot()` 返回 `image_available / timestamp / age_s / source / frame_id / stale / freshness`；`freshness` 三态（`FrameFreshness.FRESH/STALE/NONE`）区分「从未有过」与「曾有过但断了」。
- **`latest_bytes()` vs `preview_bytes()`（语义相反，已核对源码）**：
  - `latest_bytes()`：返回**已预处理、供多模态模型**的帧；**过期必须返回 None**（避免模型对着过去画面决策）。阈值 `camera.stale_after_s = 3.0`。
  - `preview_bytes(max_age_s=60.0)`：返回**最新帧（与 `latest_bytes` 同源的已处理缓存 `_latest`，但把过期阈值放宽到 60s），供前端预览**；允许过期（用户需区分「从来没图」与「推流断了最后看到这张」），超过 60 秒才视作无。（注意：它返回的是已处理帧 `_latest`，非原始 `_raw`；`_raw` 仅保留、不对外。）
- **主动感知按需取帧**：`agent/active_perception.py::VisualRequestPolicy` 决定「该不该看一眼」，只在①事件需要视觉②置信度不足③用户问环境时登记取帧请求。`SpatialAgent.build_context()` 取走一次性标志（`consume_visual_request()`，只生效一次），由主线程 `ContextBuilder.take_image()` 用 `latest_bytes()` 取最新帧——过期返回 None，比不给图更糟的是给过期图。`agent/tools.py::request_visual_observation()` 在工作线程里只举手（置一次性标志），不真正取图（相机缓存是主线程独占写）。
- **预览**：前端 `api/test_page.html` 经 `GET /api/frame`（或 WS `/ws/camera`）取图，使用 `preview_bytes()` 语义。

---

## 8. 未来真实设备接入位置

只描述替换/插入哪一层与接口，不写实现。核心约束：换硬件只改 `SpatialAgentSystem.__init__` 装配处的两行（或一处 Provider 注入），下游一行不动（铁律 2）。

| 真实设备 | 替换/插入层 | 接口 | 说明 |
|---|---|---|---|
| **UWB 定位** | `sensors/` 新增 `UwbProvider`，实现 `SensorProvider.read_pose()`（返回带噪 `PoseState`） | `SensorProvider` | 只换感知读数的噪声模型，`build()` 组装顺序不变。真机「无真值」语义：`WorldReference` 仍仅供工具/可视化，不对外播报。 |
| **IMU（姿态/步速）** | 同上 `UwbProvider` 一并实现 `read_pose()` 的 heading/speed 部分 | `SensorProvider.read_pose` | 与 UWB 同属位姿通道，合并进一个 Provider。 |
| **iPhone LiDAR / 深度相机** | 新增 Provider 实现 `measure_depth()` / `scan_wall_ahead()` / `scan_sides()`（`SensorKind.DEPTH`） | `SensorProvider` 测量组方法 | 噪声模型由真机硬件属性决定，写在 Provider 内。 |
| **深度相机（台阶/坠落）** | 在 `ConfidenceEstimator` / `FreshnessPolicy` 基础上让 `EnvironmentState.stairs/dropoff` 由真实深度算子判定（当前恒 `False`） | `fusion/` 算子 | 接入后「高危地形」不再恒假，将真实触发 `RiskState` 与自洽性校验。 |
| **UE5 数字孪生** | 新增 Provider 同时实现 `SensorProvider` + `WorldStepper`（或拆 `NoopStepper`）；`world.bind_map()` 接收由 UE5 产出的 `MapSnapshot` | 双协议 + `MapSnapshot` | 仿真里「感知与推世界同 RNG」所以一个对象实现两协议；真机下后者多为空实现（用户自己走路）。 |

接入通用步骤：
1. 在 `sensors/` 下实现对应协议（`SensorProvider` / `WorldStepper` / `CameraProvider`），零依赖 `simulator`。
2. 在 `SpatialAgentSystem.__init__` 把 `provider = SimulatedProvider(...)` 换成你的 Provider，仅此一处。
3. 若提供地图，让 `provider.map_snapshot()` 返回符合 `MapSnapshot` schema 的静态知识（或在装配处直接 `world.bind_map(ue5_map)`）。
4. 真机下 `WorldStepper.advance_*` 多为空实现；「用户是否前进」仍由 `Action.permits_motion` 控制。

---

## 9. 模块职责对照表

| 模块 | 关键文件 | 职责 | 依赖方向（箭头 = 被谁依赖） |
|---|---|---|---|
| 传感器协议层 | `sensors/base.py` | 抽象契约：`SensorProvider` / `WorldStepper` / `CameraProvider` 三协议 + `RawPerception` / `WorldReference` / `MapSnapshot` / `SensorHealth`。零硬件依赖 | ← fusion、agent、state_manager、tools |
| 仿真适配实现 | `sensors/simulated/*.py` | `SimulatedProvider`（同时实现两协议），把四个模拟器收拢 | 依赖 simulator/*；被 agent_core 装配 |
| 仿真器 | `simulator/*.py` | 几何真值、噪声、动态障碍。仅此层持有随机源 | 仅被 sensors/simulated 引用 |
| 融合层 | `fusion/state_fusion.py` `fusion/confidence.py` `fusion/freshness.py` | 多源读数 → `SpatialState`；置信度估计（含实测残差）；新鲜度三态 | 依赖 sensors/base、spatial、world_model |
| 融合门面 | `spatial/state_manager.py` | 薄门面，只转发 `build()/camera/reset` 等兼容访问点 | 依赖 fusion |
| 状态契约 | `spatial/spatial_state.py` | `SpatialState` 及其子结构（唯一状态契约） | 被全局依赖 |
| 世界模型 | `world_model/*.py` `maps/*.py` | 当前/短期/长期/经验四层 + 度量/语义/拓扑/经验四张地图 | 依赖 sensors/base（MapSnapshot）；被 fusion、agent 使用 |
| 事件引擎 | `events/*.py` | 16 类事件、边沿检测、总线冷却、历史、订阅 | 依赖 spatial（只读）；被 agent、orchestrator 消费 |
| 主循环 + 决策 | `agent/agent_core.py` `agent/orchestrator.py` `agent/cognitive_agent.py` `agent/context_builder.py` `agent/decision.py` `agent/prompt_template.py` `agent/memory.py` | `SpatialAgentSystem` 运行时 + 双循环编排 + 认知 Agent + 受控信息面 | 依赖 sensors/base、fusion、spatial、events、world_model、safety |
| 安全层 | `agent/safety/safety_engine.py` `agent/safety/risk_rules.py` | 纯函数安全规则表 + 每 tick 求值（独立 LLM） | 被 orchestrator 调用 |
| 表达层 | `agent/interaction_policy.py` `agent/action_schema.py` | 说不说/说多少/走哪通道；10 种 ActionType + Urgency + AgentDecision | 被 agent_core、orchestrator 调用 |
| 主动感知 | `agent/active_perception.py` | `VisualRequestPolicy`（纯函数，判定何时取帧） | 被 context_builder、cognitive_agent 调用 |
| 工具层 | `agent/tools.py` `agent/llm_client.py` | `AgentTools.call`（唯一副作用出口 + RLock）；`LLMClient`（OpenAI 兼容 + 熔断 + 总预算） | 依赖 sensors/base、state_manager、world_model |
| 摄像头 | `camera/iphone_receiver.py` `camera/image_processor.py` | 帧接收/落盘/新鲜度；解码/缩放/编码 | 被 api、agent 使用 |
| 接口层 | `api/websocket_server.py` | FastAPI：`/api/state` `/api/control` `/api/stats` `/api/camera` `/api/frame` `/api/map` `/api/query` `/api/image`；WS `/ws/agent` `/ws/camera`；测试页 `api/test_page.html` | 依赖 agent、camera |
| 录制/回放 | `recording/schemas.py` `recording/session_recorder.py` `recording/session_player.py` | JSONL 落盘 + 回放（不接模拟器） | 零 simulator 依赖 |
| 入口 | `main.py` | `--selftest` / `--acceptance` / `--acceptance-demo` / `--serve` / `--demo` / `--mode replay` / `--record` | 装配所有模块 |
| 测试 | `tests/selftest.py`（74 项） `tests/acceptance.py`（10 场景） `tools/e2e_test.py` `tools/ui_smoke_test.py`（44 项） `tools/acceptance_demo.py` `tools/verify_real_vision.py` `tools/verify_live_camera.py` `tools/set_llm_key.py` `tools/fake_llm_server.py` | 验证与演示 | — |

---

## 10. 七条架构铁律及强制手段

| # | 铁律 | 含义 | 强制手段（哪个自检用例 / 哪段代码保证） |
|---|---|---|---|
| 1 | 大模型不直接读传感器，一切先归一化为 `SpatialState` | LLM 只接收 `ContextBuilder` 渲染的 `SpatialState` 文本视图（`to_prompt_dict()`）+ 一帧图，绝不接触 `SensorProvider` 原始读数 | 设计契约：`spatial/spatial_state.py` docstring + `ContextBuilder`（跨线程只传不可变副本）；由铁律 7（用例 47 全仓扫描）间接保证 `agent/*` 不直连 `simulator` |
| 2 | 换硬件不改 Agent（只换 `sensors/` 实现与装配处一行） | 上层只认 `sensors/base.py` 协议；装配点唯一 | `sensors/base.py` 三类协议；`sensors/simulated/` 实现；`agent/agent_core.py::__init__` 唯一装配；用例 46（双协议 + 零随机数消耗 + 健康度三态）、用例 47（分层铁律） |
| 3 | 所有副作用必须经 `tools.call()` 派发 | `speak/ask_user/replan_route` 一律经 `AgentTools.call()`（带 RLock 串行化、异常隔离、记账） | `agent/tools.py::AgentTools.call()`；`SpatialAgent.apply_action()` 仅此一个出口；规则模式与 LLM 模式同源 |
| 4 | 安全底线不可被模型推翻 | 规则判定 `WAIT(critical)` 时 `_merge_with_safety()` 强制采用基线，模型不能让用户继续走 | `agent/agent_core.py::_merge_with_safety`；用例 70「安全底线复核」（可信时采纳、不可信/过期退回基线且不冻结用户） |
| 5 | 表达层的拦截不得改变物理层的后果 | `gate_expression()` 的 `discard` 只清空话术；过期/低置信整条退回基线；其余只清话术、保留 `permits_motion` | `agent/agent_core.py::gate_expression` + `InteractionPolicy`；用例 65（静音不改物理语义）、用例 70 |
| 6 | 新能力只加在非确定路径（LLM）；确定性规则路径必须可证明不变 | 表达层 `gate()` 只作用于认知输出；规则基线 `allow()/allow_stop()` 语义与 v0.2 逐字一致 | `agent/interaction_policy.py` 两套入口；用例 69「规则模式中性」：120 轮只出现 v0.2 的 5 种行动、只用 VOICE/SILENT、`perception.evaluated == 0`（结构上不可能变） |
| 7 | 分层铁律：除 `SpatialAgentSystem.__init__` 外任何模块不得 `import simulator`（全仓扫描 `^\s*(from|import)\s+simulator`） | `spatial/state_manager.py`、`agent/tools.py`、`agent/agent_core.py`、`sensors/base.py`、`recording/*` 必须为零 | `tests/selftest.py` 用例 47：全仓正则扫描，上述文件不得出现 simulator import；审计允许 `tests/`、`sensors/simulated/`、`simulator/` 自身 |

---

## 11. 关键事实核对（写作时抽查的源码）

- `agent/agent_core.py::SpatialAgentSystem.step`：6 步顺序与「state 晚一拍」已核对；真值读 `system.nav.pos`/`heading`。
- `agent/orchestrator.py`：快/慢循环、`CognitiveTrigger.IMPORTANT`、`CognitiveLoop`（`result_ttl_s`/`heartbeat_s`/`min_interval_s`）、非阻塞 `submit`/`poll` 已核对。
- `sensors/base.py`：三协议与「调用顺序 = 随机数消耗顺序」注释已核对；`sensor_stats()` 与 `WorldStepper.stats()` 故意不同名（防覆盖）。
- `fusion/state_fusion.py`：调用顺序 `read_pose → perceive → scan_wall_ahead → 逐障碍 measure_depth → scan_sides → read_navigation` 已核对。
- `spatial/spatial_state.py`：兼容别名 `UserState = PoseState`、字段 `user` 不变、`pose` 只读属性；`ConfidenceState = UncertaintyState`、`confidence` + `uncertainty` 转发，已核对。
- `agent/action_schema.py`：`ActionType` 10 种、`MOTION_ALLOWED`、`as_dict()` 冻结键集合，已核对。
- `camera/iphone_receiver.py`：`latest_bytes()`（过期→None，给模型）与 `preview_bytes()`（允许过期到 60s，给预览）语义相反，已核对。
- `events/event_types.py`：16 类 `EventType` + 4 级 `Severity` + 冷却/严重度表，已核对（源码 docstring 标注「15 类」为历史描述，实际枚举成员数为 16）。
- `config/config.yaml`：版本 `0.3.0`、`tick_hz=1.0`、`max_retries=0`、`total_deadline_s=20`、`stale_after_s=3.0`、`max_fps=8`、地图 `办公楼-1F` 40×46m，已核对。
