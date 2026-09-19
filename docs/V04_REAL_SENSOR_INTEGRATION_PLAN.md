# v0.4 真实传感器接入计划（v0.3 只预留、不实现）

> 对应任务书**第三十五节**。
> **本文档不包含任何实现**，只回答一个问题：接真实硬件时，每一件东西应该接在哪一层、要满足什么契约。
>
> v0.3 的任务书第十节明确禁止接入真实 UWB / LiDAR / 完整 UE5 / 完整 SLAM。
> 因此 v0.3 只做了一件事：**把接口面留出来**，并让"换硬件不改 Agent"变成可被自检强制的规则。

---

## 0. 一句话原则

> **换硬件只改 `sensors/` 里的实现，以及在装配处改一行。**
> `agent/`、`spatial/`、`fusion/`、`world_model/`、`events/` 一行都不动。

这条原则现在**不是口号**，而是被两件事强制的：

1. **接口面唯一**：`sensors/base.py` 定义了三个 `Protocol`，融合层与 Agent 只认它们。
2. **分层铁律**（自检用例 47）：全仓扫描 `^\s*(from|import)\s+simulator`，
   以下文件必须为零 —— `spatial/state_manager.py`、`agent/tools.py`、`agent/agent_core.py`、
   `sensors/base.py`、`recording/*`。**唯一**允许出现模拟器导入的地方是装配处
   `SpatialAgentSystem.__init__`（且只允许 `from sensors.simulated import SimulatedProvider`）。

所以 v0.4 的工作可以概括为：**在 `sensors/future/` 下写几个新文件，然后改装配处那一行。**

---

## 1. 现有的三个契约（v0.4 必须满足的就是它们）

来自 `sensors/base.py`：

| Protocol | 职责 | 谁实现 |
| --- | --- | --- |
| `SensorProvider` | **只读**感知：位姿、深度、左右扫描、候选枚举、静态地图快照、置信度、健康度 | 仿真：`sensors.simulated.SimulatedProvider`；真机：UWB / LiDAR / UE5 |
| `WorldStepper` | **推进世界**：障碍移动、用户位姿推进、重规划、重置、到达判定、无噪参考读数 | 仿真：模拟器；真机：`noop_stepper`（用户自己走路）或 UE5 孪生 |
| `CameraProvider` | 摄像头帧：`snapshot()` / `latest_bytes()` | 已由 `camera/iphone_receiver.py` 真实实现（v0.3 就在用） |

### 1.1 三组方法，调用顺序有讲究

`SensorProvider` 的方法刻意分成三组（`sensors/base.py` 的 docstring 已写明）：

| 组 | 方法 | 关键约束 |
| --- | --- | --- |
| 甲 · 加噪读数 | `read_pose()` / `measure_depth()` / `scan_wall_ahead()` / `scan_sides()` | 会消耗传感器随机数，**必须由融合层按固定顺序调用**。顺序一变，噪声序列错位，仿真不再可复现 |
| 乙 · 候选枚举 | `perceive()` | 纯读取、**不消耗随机数**，只给候选不给结论 |
| 丙 · 静态知识与质量 | `map_snapshot()` / `zone_at()` / `floor()` / `read_navigation()` / `confidence()` / `health()` / `sensor_stats()` | 不随时间变化或只反映健康状况 |

⚠️ **两个必须记住的坑**（v0.3 都踩过）：

- `perceive()` **绝不能**内部调用 `lidar_measure` 一类加噪方法，否则"融合层调了几次"就不再是唯一决定噪声序列的东西。
- 统计方法叫 `sensor_stats()` 而**不是** `stats()` —— 因为 `WorldStepper` 也有一个 `stats()`，
  一个类同时满足两个 Protocol 时同名方法会互相覆盖（v0.3 实测报 `KeyError: 'arrived'`，
  且被 REST 处理器吞成一行日志，极难定位）。

### 1.2 `SensorStatus` 为什么是三态而不是布尔

`OK` / `DEGRADED` / `LOST` / `ABSENT` —— 因为「**本系统就没接这个传感器**」与
「**接了但坏了**」的处理方式完全不同：前者应静默降级，后者必须产生事件并告知用户
（`events/event_types.py` 里有 `CAMERA_LOST` / `LOW_LOCALIZATION_CONFIDENCE` 等）。
真机接入时，`health()` 是事件引擎的输入源，不要图省事返回固定 `OK`。

---

## 2. UWB 应该接在哪

| 项 | 结论 |
| --- | --- |
| 接入层 | `SensorProvider`（新增 `sensors/future/uwb_provider.py`） |
| 取代 | `simulator/sensor_simulator.py` 里 UWB 加噪那一部分 |
| 必须产出 | `read_pose() -> PoseState`（含 `position` / `heading` / `speed` / `confidence` / `timestamp` / `source`） |
| 必须实现 | `health()` —— 标签失联 → `SensorStatus.LOST`；`confidence()` → 定位置信度 |
| 下游影响 | `fusion/confidence.py` 的估计器会消费它；`LOW_LOCALIZATION_CONFIDENCE` 事件依赖它 |
| 不许做 | 不要在 provider 里做"轨迹平滑/滤波"以外的业务判断；结论留给融合层 |

**风险最高的一项。** 原因：位姿是所有几何计算与地图匹配的基准，一旦漂移，
世界模型的物体记忆、导航进度、障碍方位会一起错。建议 v0.4 第一个做，并且先做
「同一段真实走廊来回走 10 次」的重复性验证，再看 Agent 行为。

**真机与仿真的关键差异**（`sensors/future/__init__.py` 已写明）：
真机**没有 `debug_ground_truth()`** —— 真实硬件没有"真值"这个东西，只有最近一次位姿估计。
所以 v0.3 特意把模拟器给的真值命名成 `pose_reference` / `world_reference`（中性名），
避免对接真机时被字段名误导。

## 3. IMU 应该接在哪

| 项 | 结论 |
| --- | --- |
| 接入层 | 与 UWB 同一个 `SensorProvider` 实现内融合（`uwb_provider.py` 一并解算），**不单独开一个 Provider** |
| 理由 | UWB 给低频绝对位置、IMU 给高频相对运动，两者必须**在同一处做时间对齐与融合**才有意义；拆成两个 Provider 会把"谁先调用"变成随机数/时序问题 |
| 必须产出 | 位姿中的 `heading` 与 `speed` |
| 必须实现 | `health()` —— IMU 断流 → `DEGRADED`（可继续用 UWB 低频更新） |
| 下游影响 | `TrajectoryMemory`（打转检测）、`TurnApproaching` 事件、前进速度估计 |
| 注意 | 眼镜 IMU 与手机 IMU 的坐标系不同，**统一到 `spatial/spatial_state.py` 的坐标系**（x 向东、y 向北、`heading` 0° = +y、顺时针为正）后再交给融合层 |

## 4. LiDAR 应该接在哪

| 项 | 结论 |
| --- | --- |
| 接入层 | `SensorProvider` 的深度部分（新增 `sensors/future/iphone_lidar_provider.py`） |
| 取代 | `simulator/obstacle_simulator.py` 与 `simulator/sensor_simulator.py` 中产出障碍与净空的那部分 |
| 必须实现 | `scan_wall_ahead()`（窄视场正前方测墙）、`scan_sides()`（左右侧，用于估算可通行宽度）、`measure_depth()`（**直接返回入参** —— 真值即读数，不需要再加噪） |
| 必须实现 | `health()` 依据 `ARSession` 状态 |
| 下游影响 | `environment` 的障碍与净空、`affordance` 的通过性、`NARROW_PASSAGE_DETECTED` / `OBSTACLE_APPEARED` 类事件、安全层的高/临界距离判定 |
| 注意 | 三条扫描方法**必须分开**实现，不能合并成一个"宽视场扫描" —— 融合层的调用顺序是 `前墙 → [逐障碍测距] → 左扫 → 右扫`，合并会改变随机数（真机上则是改变时序与缓存命中）语义 |

iPhone LiDAR 的价值在于：**它是把"画面里有没有东西"变成"前面 1.2 米有东西"的那一步**，
而这个数值化结论正是安全层（不想依赖大模型）所需要的。

## 5. UE5 应该接在哪

| 项 | 结论 |
| --- | --- |
| 接入层 | **同时**实现 `SensorProvider` + `WorldStepper`（新增 `sensors/future/ue5_twin_provider.py`） |
| 理由 | 数字孪生既是"数据源"（静态空间知识、动态行人）也是"世界"（推进仿真时间）。这是**唯一**允许保留 `debug_ground_truth()` 的实现，因为孪生本来就有真值 |
| 必须产出 | `map_snapshot()`（边界 / 栅格 / 分区 / 路点 / 静态物体，形状见 `MapSnapshot`）、`obstacle_centers()`、`advance_environment()`、`advance_user()` |
| 下游影响 | `maps/` 的度量层与导航图；`MapSnapshot` 是数据驱动的，因此**地图换掉不需要改 Agent** |
| 注意 | `WorldStepper.stats()` 必须包含这些键（前端与自检依赖）：`arrived` / `progress` / `distance_to_goal_m` / `replans` / `map` / `sensors` / `obstacles_active` / `obstacles_spawned` |

## 6. 另外两项（任务书第三十五节提到）

### 6.1 真实用户语音

| 项 | 结论 |
| --- | --- |
| 输入 | 替换 `api` 的用户指令通道（现为 `POST /api/query`），把 ASR 结果送进同一入口 |
| 输出 | 替换 `agent/tools.py::speak()` 的实现，接 TTS |
| 硬约束 | **保持工具签名不变**（铁律 3：所有副作用必须经 `tools.call()`）。Agent 不应知道这句话是"打字来的"还是"说出来的" |
| 现成通路 | 用户提问已经能驱动主动感知（`USER_QUESTION` → `REQUEST_VISUAL` → 取图 → 缩答），语音只需接到同一个入口 |

### 6.2 iPhone「安心行」输出结构化障碍状态

| 项 | 结论 |
| --- | --- |
| 目标 | 让手机端直接产出结构化障碍状态，**绕过视觉大模型的延迟**（模型实测 1.9~2.9 s，对 1 Hz 循环偏重） |
| 接入层 | 作为"深度/障碍"的另一个 `SensorProvider` 实现，或作为 `camera` 通道上的一种结构化附带消息 |
| 收益 | 安全相关判断不再经过大模型；大模型只负责"理解与表达" |
| 注意 | 若走 `camera` 通道，需扩展 `/ws/camera` 协议（v0.3 刻意没有改 iOS 侧协议）；若走 `SensorProvider`，则零协议改动 |

---

## 7. 接入顺序建议（按"风险 × 价值"排序）

| 序 | 内容 | 为什么这个顺序 |
| --- | --- | --- |
| 1 | UWB（含 IMU 融合） | 位姿是一切几何的基准；先把它做稳，后面才有可比性 |
| 2 | iPhone LiDAR | 直接提升安全性（数值化净空），且不依赖大模型 |
| 3 | 「安心行」结构化障碍 | 价值同 2，但需要 iOS 侧配合 |
| 4 | UE5 数字孪生 | 工程量大，但接口最清晰（`MapSnapshot` 数据驱动） |
| 5 | 真实语音 | 体验提升，风险低 |

---

## 8. v0.4 开工前必须先做的事

1. **先跑通 v0.3 的全部验收**（自检 74 项 / 验收 10 场景 / 七拍 Demo），把它当作 v0.4 的基线。
2. **先定"同一段实验"的口径**：用 `--record` 录一段真实硬件数据，用 `--mode replay` 复现，
   确保录播一致率达到 100% —— 这是后面做 Model A / B / Baseline 对比的前提
   （见 `docs/REPLAY_SYSTEM.md`）。
3. **先把 `noop_stepper` 用起来**：真机下用户自己走路，`advance_environment` / `advance_user`
   都应是空实现。它存在的意义是让装配处可以无条件调用推进方法，而不必到处写 `if 是仿真`。
4. **不要引入完整 SLAM / VIO / 3D occupancy / 多 Agent 互相聊天 / 数据库集群 / 微服务化**
   —— 任务书第三十一节明确把这批列为"暂时不要做"，理由是它们只会增加复杂度，
   而当前目标是**打牢 Agent 系统骨架**。
5. **顺手关掉 v0.3 的两个已知小问题**：绕行话术里的英文物品名中文化
   （`agent/decision.py` 取的是 `obstacle.type.value`），以及物体/事件名词统一词表。

---

## 9. 与 v0.3 的边界（明确声明）

- 本文档**只是计划**。v0.3 的代码里**没有**任何真实硬件驱动：
  `sensors/future/__init__.py` 的 `__all__` 是空列表，只有接口约定，没有"看起来能跑"的桩代码。
- v0.3 交付的是**可运行原型**，硬件接入是 v0.4 的工作。
- 任何违反铁律 7（除装配处外不得 `import simulator`）的接入方式，都会被自检用例 47 直接拦下 ——
  这是本次架构升级留给 v0.4 最有价值的东西：**约束是可执行的，不是纸面的。**
