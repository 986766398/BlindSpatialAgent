# 事件系统（Event System）文档

版本：v0.3（Stage 6 事件层）
对应源码：`events/event_types.py`、`events/event_detector.py`、`events/event_engine.py`、
`events/event_bus.py`、`events/event_history.py`、`agent/orchestrator.py`、`agent/active_perception.py`

本文档所有枚举值、严重度、冷却时间、检测逻辑均经 `Read` 源码核对。

## 1. 事件层的定位

数据流（任务书第二节分层）：

```
Raw Sensors → Structured Observations → State Fusion → SpatialState
→ Event Engine → Agent Reasoning → Interaction Decision → Human Action
```

事件 = 状态变化的**边沿**（"出现了新椅子"），不是状态的重复描述（"前方 0.8 米有椅子"）。
v0.2 把状态当日志打，同一把椅子每秒被"发现"一次；v0.3 用边沿检测只产生一次
（`event_detector.py:8`）。

组件分工（`event_engine.py:7`）：

| 组件 | 职责 |
| --- | --- |
| `event_types.py` | 词表：事件类型 + 默认严重度 + 默认冷却 |
| `event_detector.py` | 状态迁移 / 边沿检测（只读，不决策、不播报、不改 `SpatialState`） |
| `event_bus.py` | 发布 / 订阅 + 冷却去重 |
| `event_history.py` | 按轮次可查询存档（供提示词 / 录制 / 诊断） |
| `event_engine.py` | 一体化入口：`process()` / `subscribe()` / `reset()` |

## 2. EventType 完整清单（16 种）

> 注意：源码 `event_types.py:29`、`event_engine.py:9`、`events/__init__.py:7` 的注释都写
> "15 类"，但 `EventType` 枚举**实际定义了 16 个成员**（多出 `HIGH_RISK`）。下表按源码
> 真实枚举逐条列出，不按注释的 15 计。

`AgentEvent` 公共字段（`event_types.py:114`）：`event_id`、`event_type`、`timestamp`（elapsed 秒）、
`wall_time`（墙钟秒）、`severity`、`source`（`detector`/`user`/`safety`/`fusion`）、`payload`、`confidence`、`tick`。
去重键 `AgentEvent.key()` = `事件类型:主体`（payload 里的 `subject`，缺省为类型本身）。

| # | 枚举值 | 语义 | 触发条件（边沿 / 状态迁移） | 默认严重度 | 默认冷却(s) |
| --- | --- | --- | --- | --- | --- |
| 1 | `USER_COMMAND` | 用户下了指令（去出口/停下） | 用户提交指令类 query | notice | 0.0 |
| 2 | `USER_QUESTION` | 用户问了问题（旁边有什么） | 用户提交提问类 query | notice | 0.0 |
| 3 | `OBSTACLE_APPEARED` | 出现新障碍 | 对象进入视野（`_seen_objects` 未见）且首帧基线已建立 | 默认 notice；动态障碍→warning；距离 ≤critical_distance_m→critical（覆盖） | 2.0 |
| 4 | `OBSTACLE_CLEARED` | 障碍消失 | 之前在视野、现不在（且存在 >1.0s）；依赖世界模型时间记忆 + 走出感知窗口 | info | 2.0 |
| 5 | `OBSTACLE_APPROACHING` | 动态障碍在快速接近 | 世界模型 tracks 趋势 approaching；≤high 距离→critical | warning | 1.5 |
| 6 | `ROUTE_DEVIATION` | 偏离路线 | `off_route` 由 False→True 迁移 | warning | 5.0 |
| 7 | `ROUTE_BLOCKED` | 路线被挡住 | `front_clear` 由 True→False 迁移 | warning | 5.0 |
| 8 | `GOAL_REACHED` | 到达目的地 | `walking_status=arrived` 或 `route_progress>=0.999`（边沿，只发一次） | notice | 0.0 |
| 9 | `TURN_APPROACHING` | 即将需要转弯 | 下一节点与当前朝向偏差 ≥45° 且距 `turn_distance_m`（默认 3.0m）内 | notice | 8.0 |
| 10 | `LOW_LOCALIZATION_CONFIDENCE` | 定位置信度低 | `localization_confidence < min_localization` 上升沿（带 0.1 滞回恢复） | warning | 30.0 |
| 11 | `LOW_PERCEPTION_CONFIDENCE` | 感知置信度低 | `perception_confidence < 0.5` 上升沿（带 0.1 滞回恢复） | warning | 30.0 |
| 12 | `CAMERA_LOST` | 摄像头断流 | `freshness` 由 FRESH→STALE 迁移 | notice | 10.0 |
| 13 | `CAMERA_RECOVERED` | 摄像头恢复 | `freshness` 由非 FRESH→FRESH 迁移 | info | 0.0 |
| 14 | `MAP_SENSOR_CONFLICT` | 地图说可走、传感器说不能 | `front_clear=False` 且沿当前朝向探 0.5m 地图标记 walkable（任务书第 20 节场景七） | warning | 10.0 |
| 15 | `SCENE_CHANGED` | 场景语义显著变化 | 视野内障碍类型多重集（scene key）变化 | info | 5.0 |
| 16 | `HIGH_RISK` | 高风险状态出现 | `risk.level` 升高（到 critical 时 severity=critical，否则 warning） | **critical** | 1.0 |

严重度语义（`event_types.py:9`）：`info`=只记录；`notice`=Agent 应考虑是否说；
`warning`=Agent 应说 / Safety 应评估；`critical`=Safety 必须立即响应且不受冷却约束。
检测器可按 payload 提升严重度，但**不允许降到低于 `DEFAULT_SEVERITY` 表值**
（`event_types.py:72`）。

## 3. 事件产生机制：边沿检测

检测器 `EventDetector` 为有状态对象，为每个事件类型维护**上一帧**判定
（`event_detector.py:64`）：`_seen_objects`、`_camera_was`、`_risk_level_was`、
`_off_route_was`、`_goal_reached`、`_loc_conf_ok`、`_perc_conf_ok`、`_front_clear_was`、
`_turn_announced`、`_scene_key_was`。只在**状态发生迁移**时发声：

```
absent → present   ⇒ OBSTACLE_APPEARED（一次）
present → present  ⇒ 不发（它还在那儿，是 world model 的活）
present → absent    ⇒ OBSTACLE_CLEARED（一次）
present → closing   ⇒ OBSTACLE_APPROACHING（趋势事件）
```

**首帧基线（`_baselined`）**：检测器启动、系统视野里本来就有的物体（含地图存量）**不算
"新出现"**，否则开机瞬间会把它们全刷成 `OBSTACLE_APPEARED`
（`event_detector.py:78`）。`_baselined` 初始 `False`，在 `detect()` 末尾**统一置位**
（`event_detector.py:95`）：

```python
def detect(self, state, t):
    out = []
    out += self._detect_obstacles(state, t)
    out += self._detect_risk(state, t)
    out += self._detect_route(state, t)
    out += self._detect_confidence(state, t)
    out += self._detect_camera(state, t)
    out += self._detect_scene(state, t)
    self.emitted += len(out)
    self._baselined = True   # 首帧之后才具备"状态迁移"的参照系
    return out
```

`_detect_obstacles` 在首帧只做登记（`self._seen_objects[name]=t`）不发声，
只有 `_baselined` 为 True 时才真正 `OBSTACLE_APPEARED`（`event_detector.py:143-170`）。
这保证了"开机那一帧"安静。

去重三道闸（`event_engine.py:22`）：
1. 状态迁移（detector）：present→present 不发声；
2. 冷却（bus）：同 key 短期不重复（critical 豁免）；
3. 主体区分（event.key）：椅子 A 与椅子 B 是两件事。

## 4. 事件总线（EventBus）

### 4.1 订阅 / 发布模型

`EventBus.subscribe(callback, channel="all")` / `unsubscribe(...)`（`event_bus.py:47`）。
当前所有消费者走 `"all"` 通道（分组能力预留）。`EventEngine.process()` 每轮：

```python
def process(self, state, t):
    detected = self.detector.detect(state, t)
    published = []
    for e in detected:
        self.history.add(e)          # 即使被冷却吞掉也进存档
        if self.bus.publish(e):
            published.append(e)
    return published
```

`EventBus.publish()`（`event_bus.py:59`）：
- 先 `self.history.append(event)`（工作集 deque，容量有限）；
- 若 `_in_cooldown(event)`：移入 `suppressed`、计数 +1、返回 `False`（不发）；
- 否则 `self._last_emit[key]=event.timestamp`、遍历 `self._subscribers["all"]` 调 `cb(event)`，
  单个订阅者异常被捕获并 `LOG.warning`，**绝不打断发布方**。

### 4.2 冷却（cooldown）

`_in_cooldown`（`event_bus.py:77`）：
- **critical 事件豁免冷却**（`event_bus.py:78`）——"前方 0.3 米有障碍"重复发生时宁可重复报警，
  冷却防的是噪音不是危险（注释 `event_bus.py:13`）；
- 其它事件：同 `key`（类型+主体）在 `DEFAULT_COOLDOWN_S[event_type]` 秒内再发生则吞掉；
- 冷却可从 `config.yaml:events.cooldown_s`（键=类型名字符串）覆盖
  （`event_engine.py:_cooldowns_from_cfg`），未知键只告警不抛异常。

### 4.3 一个事件被多个订阅者消费

发布时遍历 `self._subscribers["all"]` 列表（`event_bus.py:70`），每个回调都会收到**同一个
`AgentEvent` 实例**——Safety Layer 与 Cognitive Agent 可同时订阅、各自响应
（Safety 即时干预、Cognitive 按事件触发推理）。这正是"事件层独立于 Agent"的意义
（`event_engine.py:14`）：无事件时 Agent 天然静默。

### 4.4 event_history 的作用

`EventHistory`（`event_history.py`）是**按轮次索引的存档**（与 `EventBus.history` 工作集分工）：
提供 `since_tick(tick)`、`by_type(etype)`、`has_critical(since_tick)`、`summary_for_prompt(n)`。
它给提示词、录制、诊断用；`EventEngine.summary(n)` 调 `history.summary_for_prompt(n)`
生成"最近 n 条事件"文本喂给认知循环。

## 5. 事件 → Agent（任务书 Gate 9）

### 5.1 进入决策

主循环 `agent_core.py:514`：

```python
events = self.events.process(state, self.elapsed)
```

`AgentOrchestrator.decide(state, events, t, tick, user_query=None)`
（`orchestrator.py:335`）每轮被调用，`events: list[AgentEvent]` 是入参之一。两条消费路径：

1. **安全快循环**：`verdict = self.safety.evaluate(state, events, t)`（`orchestrator.py:348`）
   —— Safety Layer 订阅 critical 事件做即时干预，不等 LLM。
2. **认知慢循环触发**：`CognitiveTrigger.IMPORTANT`（`orchestrator.py:59`）是一组
   `frozenset[EventType]`，`should_run()` 中 `any(e.event_type in self.IMPORTANT for e in events)`
   为真则提交认知线程思考（`orchestrator.py:99`）。`CognitiveTrigger.IMPORTANT` 含
   `USER_COMMAND/USER_QUESTION/OBSTACLE_APPEARED/OBSTACLE_CLEARED/OBSTACLE_APPROACHING/
   ROUTE_DEVIATION/ROUTE_BLOCKED/GOAL_REACHED/TURN_APPROACHING/MAP_SENSOR_CONFLICT/
   LOW_LOCALIZATION_CONFIDENCE/CAMERA_LOST/CAMERA_RECOVERED/HIGH_RISK`（注意 `SCENE_CHANGED`、
   `LOW_PERCEPTION_CONFIDENCE` 未列入 IMPORTANT，只进历史、不触发认知）。

### 5.2 事件如何改变行动或表达（验收证据）

**S03（Gate 9：障碍突现 → 事件 + 安全层立即干预，`acceptance.py:320`）**：
前置 `set_manual` 关掉障碍物，首帧建立基线后，在用户真实朝向正前方 0.50m 摆障碍
（净空 0.20m < critical 0.35m）。判定方式：

```python
kinds = [e.event_type.value for e in seen]
expect(EventType.OBSTACLE_APPEARED.value in kinds, "未产生 OBSTACLE_APPEARED")
expect(r.safety.get("level") == "emergency", "前方净空 0.20m 应判 emergency")
expect(r.safety.get("intervene") is True, "emergency 必须要求干预")
expect(not r.action.permits_motion, "碰撞在即时不得放行前进")
expect(r.action.message, "必须当场提醒用户")
```

即：`OBSTACLE_APPEARED`（≤critical 距离升为 critical）→ Safety Engine 判 emergency →
`verdict.intervene=True` → `decide()` 采用规则基线动作 → `action.permits_motion=False` 且带提醒。

**S05（Gate 9：障碍消失 → 事件 + 不重复播报，`acceptance.py:417`）**：
障碍 0.95m 处产生 `OBSTACLE_APPEARED` 后 `obstacles.clear()` 并恢复导航，逐轮走到感知窗口外。
判定方式：

```python
expect(any(e.event_type is EventType.OBSTACLE_CLEARED for e in seen),
       "走离障碍所在地后仍未产生 OBSTACLE_CLEARED")
expect(system.last_state().environment.front_clear, "障碍消失后前方应恢复通畅")
added = len(system.tools.utterance_log) - n0
expect(added == 0, "障碍消失后重复播报了 %d 条" % added)
```

即：`OBSTACLE_CLEARED` 触发后，前端只播报一次（或不播），证明事件驱动避免重复刷屏。

## 6. 事件 → 主动取图（Active Perception）

`agent/active_perception.py` 决定"这一轮该不该看一眼画面"
（铁律：不要每轮都上传图片，`active_perception.py:5`）。

`VisualRequestPolicy.event_types`（默认 `DEFAULT_VISUAL_EVENT_TYPES`，`active_perception.py:48`）
列出"结论必须看图才能定"的事件类型：`MAP_SENSOR_CONFLICT`、`LOW_LOCALIZATION_CONFIDENCE`、
`OBSTACLE_APPEARED`、`OBSTACLE_DISAPPEARED`、`NARROW_PASSAGE_DETECTED`、`CAMERA_LOST`、
`ROUTE_DEVIATION`。

`VisualRequestPolicy.triggers()`（`active_perception.py:140`）遍历传入的 `events`，
若 `event_type.value in self.event_types` 则记 `event_needs_vision:...` 命中。

与 `event_window_s` / `min_interval_s` 的关系：

- `min_interval_s`（默认 2.0s）：**取帧节流间隔**。`evaluate()` 中若非"显式请求/用户提问"
  豁免，且 `elapsed - self._last_auto_at < min_interval_s`，则返回 `throttled` 不取帧
  （`active_perception.py:229`）——持续 10 秒低置信度不会变成连续 10 次取帧。
- `event_window_s`（默认 8.0s）：文档语义是"只认最近 event_window_s 仿真秒内发生过的
  视觉事件"。在当前实现里 `triggers()` 直接判断**传入的那一轮 `events` 列表**；
  真正的"时间窗口"由认知循环装配上下文时控制（只把近期事件纳入）。`event_window_s` 作为配置项
  存在并出现在 `stats()`，调用方应使用仿真秒（非墙钟）传递近期事件
  （`active_perception.py:91` 注释强调用 elapsed 而非墙钟）。

> 提醒：`DEFAULT_VISUAL_EVENT_TYPES` 里 `OBSTACLE_DISAPPEARED`、`NARROW_PASSAGE_DETECTED`
> **并不存在于 `EventType` 枚举**（真实枚举无这两个值）。它们是无对应事件的占位/预留项，
> 实际不会触发取图，只在配置层面作为可扩展点存在。

时间量纲：所有节流/窗口判断都用**仿真秒**（`elapsed`），因为回放、测试、快循环空转时仿真秒
与墙钟秒差异极大，用墙钟会把"没过多久"误判成"已经过了很久"（`active_perception.py:90`）。

## 7. 时序陷阱

1. **`events.jsonl` 是一行一条事件，不是一行一轮**。没有事件的轮次不写行
   （`acceptance.py:702` 注释）。断言只能数"实际发布了多少条事件"，不能数"跑了多少轮"。
   `EventBus.history` / `EventHistory` 同样只记录真实发生的事件。

2. **`GOAL_REACHED` 比仿真器的 `finished` 晚若干轮**。仿真器 `finished` 来自
   `arrive_radius_m`，比事件检测器的判据（`walking_status=arrived` 或 `progress>=0.999`）
   宽松；到达那一轮事件还没到边沿，要再走几轮才落地（`acceptance.py:659`）。
   若绑在 `finished` 上会得出"到了却没报"的错误结论。

3. **`OBSTACLE_CLEARED` 依赖世界模型的时间记忆，不是 `obstacles.clear()` 那一刻**。
   `ObjectMemoryStore.evict_after_s=60.0`（`object_memory.py:99`），且检测器只在对象
   **移出感知窗口（near 半径 6.0m，`event_detector.py:129`）或已淘汰**且存在 >1.0s 时才判
   消失（`event_detector.py:173`）。所以"障碍离开视野"是在用户走远到 6 米之外、世界模型确认
   其已不在近期记忆时才发生（`acceptance.py:444`）。把断言绑在"清空列表"上会永远等不到事件。

## 8. 新增一种事件类型（分步清单）

1. 在 `events/event_types.py` 的 `EventType`（`str, Enum`）里加一个新成员（值=成员名字符串）。
2. 在 `DEFAULT_SEVERITY`（同文件，`:73`）加映射——严重度不得低于真实安全需要
   （检测器只能提升、不能降到低于表值）。
3. 在 `DEFAULT_COOLDOWN_S`（同文件，`:94`）加冷却秒数（防刷屏的第一道闸）。
4. 在 `events/event_detector.py` 对应 `_detect_*` 方法里用 `self._mk(...)` 产生事件；
   **必须边沿检测**：维护上一帧状态、首帧（`_baselined` 置位前）只登记不发声；
   检测器全程只读 `state`/`world`，不改 `SpatialState`。
5. 若需触发认知循环：把新枚举加入 `agent/orchestrator.py` 的
   `CognitiveTrigger.IMPORTANT`（`orchestrator.py:59`）。
6. 若需触发主动取图：把枚举值**字符串**（必须是真实存在的 `EventType` 值）加入
   `agent/active_perception.py` 的 `DEFAULT_VISUAL_EVENT_TYPES`——切勿写不存在的枚举名
   （参照第 6 节 `OBSTACLE_DISAPPEARED` 的坑）。
7. 跑 `python -m tests.selftest` 与 `python -m tests.acceptance`；若涉及 Gate 8/9，确认
   S01（三件套）/ S03 / S05 仍通过。冷却被 `config.yaml:events.cooldown_s` 覆盖时，未知键只告警。
8. 不要为事件加"每轮重复"语义——事件本就是边沿；需要持续状态请读 `SpatialState` 而非事件流。
