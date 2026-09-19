# SpatialState 状态模式（Schema）文档

版本：v0.3（Egocentric Spatial State）
对应源码：`spatial/spatial_state.py`、`spatial/state_manager.py`、`fusion/confidence.py`、`fusion/freshness.py`

本文档描述的是系统内**唯一的状态契约层**：模拟器 / 真实硬件生产 `SpatialState`，
Agent（含多模态大模型）只消费 `SpatialState`。所有字段名、类型、来源均经 `Read`
源码核对，未做推测。

## 1. 概述

架构铁律第一条：大模型不直接读传感器，一切先归一化为 `SpatialState`
（`spatial/spatial_state.py:4`）。v0.3 起状态升级为"自我中心空间状态"：
每个子结构尽量带 `timestamp` / `source` / `confidence` 三件套（Gate 8 要求），
并新增 `AffordanceState`（能不能行动）与 `UncertaintyState`（五维不确定性）。

所有 Pydantic 子模型均配置 `model_config = ConfigDict(extra="forbid")`
（如 `spatial_state.py:101`）。这带来一条硬约束：**任何新增字段必须是普通字段 +
默认值，绝不能用 `@computed_field`**（见第 6 节），否则端到端
`model_validate(model_dump())` 往返会直接报错。

## 2. 顶层字段树

```
SpatialState
├── timestamp: datetime              # 顶层，本帧采集时刻
├── tick: int                        # 状态序号，每轮感知 +1
├── user: PoseState                  # 位姿（别名 UserState）
├── navigation: NavigationState      # 导航状态
├── environment: EnvironmentState     # 环境状态（LiDAR/深度）
├── semantic_scene: SemanticScene     # 语义场景（别名 SemanticState）
├── camera: CameraState               # 摄像头帧元数据（别名 CameraFrameMetadata）
├── risk: RiskState                   # 风险判定
├── confidence: UncertaintyState      # 五维不确定性（别名 ConfidenceState）
└── affordance: AffordanceState       # 可行动性（v0.3 新增）
```

兼容别名与只读转发（在 `SpatialState` 上以 `@property` 提供，不进 dump）：

| 字段名（v0.2 沿用） | 实际类型 | 兼容别名（类级） | 只读转发属性 |
| --- | --- | --- | --- |
| `user` | `PoseState` | `UserState = PoseState` | `pose` |
| `confidence` | `UncertaintyState` | `ConfidenceState = UncertaintyState` | `uncertainty` |
| `semantic_scene` | `SemanticScene` | `SemanticState = SemanticScene` | — |
| `camera` | `CameraState` | `CameraFrameMetadata = CameraState` | `camera_meta` |

字段名本身一律未改（`user` 仍叫 `user`、`confidence` 仍叫 `confidence`），升级只换类型。

## 3. 逐子模型字段说明

约定列：
- 类型：Python/Pydantic 类型
- 含义：字段语义
- 数据来源：`source` 字段的默认取值，或融合层取值通道
- 三件套：`timestamp` / `source` / `confidence` 的携带情况（Y=有，N=无）

### 3.1 user（PoseState / 别名 UserState）

来源：UWB（位置）+ IMU（朝向/速度）+ 步态（行走状态）。三件套：Y / Y / Y。

| 字段 | 类型 | 含义 | 数据来源 | 三件套 |
| --- | --- | --- | --- | --- |
| `position` | `Position{x,y,floor}` | 东向 x、北向 y（米）、楼层 floor（默认 1） | UWB + 地图 | — |
| `heading` | `float` | 朝向角，0°=正北(+y)，顺时针为正，[0,360) | IMU | — |
| `speed` | `float` | 当前速度 m/s（ge=0） | IMU / 步态 | — |
| `walking_status` | `WalkingStatus` | standing/moving/slowing/blocked/paused/arrived | 融合 | — |
| `confidence` | `float[0,1]` | 位姿估计置信度（融合层估计，非配置常量） | 融合层估计器 | confidence=Y |
| `timestamp` | `datetime|None` | 该位姿对应的采集时刻 | 融合层打戳 | timestamp=Y |
| `source` | `str` | 默认 "simulated"（simulated/uwb/imu/fused） | 装配处 | source=Y |
| `valid` | `bool` | 位姿当前是否有效；定位过期必须置 False | 融合层 | — |
| `age_ms` | `float|None` | 该位姿距融合时刻的毫秒数 | 融合层 | — |

注：`Position` 提供 `as_tuple()`、`__str__`；`heading` 经 `field_validator` 归一到 [0,360)。

### 3.2 navigation（NavigationState）

来源：路径规划器（planner / navmesh / ue5）。三件套：有 `timestamp`/`source`，
但**没有统一的 `confidence` 字段**——置信度以专用字段 `route_confidence` 表达。

| 字段 | 类型 | 含义 | 数据来源 | 三件套 |
| --- | --- | --- | --- | --- |
| `destination` | `str` | 目的地名称 | 规划器 | — |
| `current_route` | `list[RoutePoint]` | 剩余路点（别名 `route`，只读属性） | 规划器 | — |
| `next_instruction` | `str` | 给用户的下一步指令 | 规划器 | — |
| `distance_to_goal` | `float` | 剩余距离（米，ge=0） | 规划器 | — |
| `route_progress` | `float` | 整体进度 0~1（校验器夹紧） | 规划器 | — |
| `current_landmark` | `str|None` | 当前前往的路点名 | 规划器 | — |
| `off_route` | `bool` | 是否偏离规划路线 | 规划器 | — |
| `replan_count` | `int` | 本轮累计重规划次数 | 规划器 | — |
| `route_confidence` | `float[0,1]` | 路线可信度（重规划/偏离越低越高） | `ConfidenceEstimator.route()` | confidence（专用名） |
| `timestamp` | `datetime|None` | 规划结果生成时刻 | 融合层 | timestamp=Y |
| `source` | `str` | 默认 "planner" | 装配处 | source=Y |

`RoutePoint`：`x:float, y:float, name:str|None`。

### 3.3 environment（EnvironmentState）

来源：LiDAR 深度 + 可通行区域判断。三件套：Y / Y / Y。

| 字段 | 类型 | 含义 | 数据来源 | 三件套 |
| --- | --- | --- | --- | --- |
| `front_clear` | `bool` | 正前方是否可通行 | LiDAR/深度 | — |
| `front_distance` | `float` | 正前方最近障碍/墙距离（无障碍=雷达量程） | LiDAR/深度 | — |
| `left_obstacle` | `bool` | 左侧近距是否有物体（不含正常侧墙） | LiDAR/深度 | — |
| `right_obstacle` | `bool` | 右侧近距是否有物体（不含正常侧墙） | LiDAR/深度 | — |
| `left_distance` | `float|None` | 左侧距离 | LiDAR/深度 | — |
| `right_distance` | `float|None` | 右侧距离 | LiDAR/深度 | — |
| `corridor_width` | `float|None` | 估计可通行宽度（米） | 融合 | — |
| `obstacles` | `list[Obstacle]` | 视野内障碍列表 | 世界模型 + 传感器 | — |
| `narrow_passage` | `bool` | 是否处于变窄通道 | 融合 | — |
| `walkable_width` | `float|None` | 可通行宽度（`corridor_width` 的语义化命名，二者同源） | 融合 | — |
| `blocking_ratio` | `float[0,1]` | 前方视场内被阻挡角度占比 | 融合 `_blocking_ratio` | — |
| `stairs` | `bool` | 前方是否有台阶（高危地形） | 融合 | — |
| `dropoff` | `bool` | 前方是否坠落风险（悬空/坑洞） | 融合 | — |
| `timestamp` | `datetime|None` | 快照采集时刻 | 融合层 | timestamp=Y |
| `source` | `str` | 默认 "simulated"（lidar/depth/ue5） | 装配处 | source=Y |
| `confidence` | `float[0,1]` | 环境感知置信度 | 融合层 | confidence=Y |
| `valid` | `bool` | 深度链路断掉时置 False | 融合层 | — |
| `age_ms` | `float|None` | 快照距融合时刻毫秒数 | 融合层 | — |

`Obstacle`（子结构，三件套：有 `confidence`/`timestamp`，无 `source`）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `type` | `ObstacleKind` | person/chair/desk/door/box/wall/narrow_passage/unknown |
| `distance` | `float` | 到用户直线距离（米，ge=0） |
| `direction` | `ObstacleDirection` | front/front_left/front_right/left/right/behind |
| `x`,`y` | `float|None` | 绝对坐标 |
| `radius` | `float` | 近似半径（米，默认 0.3） |
| `is_dynamic` | `bool` | 是否动态障碍 |
| `speed` | `float` | 障碍自身速度 m/s |
| `confidence` | `float[0,1]` | 检出置信度 |
| `timestamp` | `datetime|None` | 检出时刻 |

### 3.4 semantic_scene（SemanticScene / 别名 SemanticState）

来源：多模态/视觉检测（vlm/detector）或地图（map）。三件套：Y / Y / Y。

| 字段 | 类型 | 含义 | 数据来源 | 三件套 |
| --- | --- | --- | --- | --- |
| `objects` | `list[SceneObject]` | 语义物体列表 | vlm/detector/map | — |
| `summary` | `str` | 一句话场景描述（供模型快速理解） | 融合 | — |
| `confidence` | `float[0,1]` | 整体语义置信度 | 融合层 | confidence=Y |
| `timestamp` | `datetime|None` | 语义快照生成时刻 | 融合层 | timestamp=Y |
| `source` | `str` | 默认 "map"（map/vlm/detector） | 装配处 | source=Y |

`SceneObject`（三件套：有 `confidence`/`timestamp`/`source`）：`type:str`、`description:str|None`、
`distance:float|None`、`direction:ObstacleDirection|None`、`confidence:float[0,1]`（默认 0.5）、
`label:str|None`（默认取 type）、`timestamp`、`source`。

### 3.5 camera（CameraState / 别名 CameraFrameMetadata）

来源：iPhone 图片推流（websocket / upload / simulated）。三件套：**有 `timestamp`/`source`，
无 `confidence` 字段**——新鲜度以 `freshness`（`FrameFreshness`）+ `age_s` 表达，
刻意不进综合置信度的加权（`spatial_state.py:313` 注释）。

| 字段 | 类型 | 含义 | 三件套 |
| --- | --- | --- | --- |
| `image_available` | `bool` | 是否收到可用帧 | — |
| `timestamp` | `datetime|None` | 帧时刻 | timestamp=Y |
| `age_s` | `float|None` | 图片距现在多久（秒） | — |
| `source` | `Literal["none","websocket","upload","simulated"]` | 帧来源 | source=Y |
| `frame_id` | `int` | 帧序号（断流时保留 >0 以区分 stale/none） | — |
| `freshness` | `FrameFreshness` | fresh/stale/none | — |
| `heading` | `float|None` | 拍摄时朝向（若设备提供） | — |

### 3.6 risk（RiskState）

来源：融合层 / Safety（v0.4 会从 `SpatialState` 迁出，见 `spatial_state.py:293`）。
三件套：Y / Y / Y。

| 字段 | 类型 | 含义 | 三件套 |
| --- | --- | --- | --- |
| `level` | `RiskLevel` | low/medium/high/critical | — |
| `reason` | `str` | 风险原因 | — |
| `confidence` | `float[0,1]` | 该风险判定置信度 | confidence=Y |
| `source` | `str` | 默认 "fusion"（fusion/safety） | source=Y |
| `timestamp` | `datetime|None` | 判定时刻 | timestamp=Y |

### 3.7 confidence（UncertaintyState / 别名 ConfidenceState）

来源：融合层估计器（`fusion/confidence.py` 的 `ConfidenceEstimator`）。
**它本身就是"置信度"容器，因此不再有独立的 `source`/`timestamp` 三件套字段**，
`overall_confidence` 由 `model_validator` 补全（见第 6 节，非 `@computed_field`）。

| 字段 | 类型 | 含义 | 取值来源 |
| --- | --- | --- | --- |
| `localization_confidence` | `float[0,1]` | 定位（UWB）可信度 | `ConfidenceEstimator.localization()` |
| `perception_confidence` | `float[0,1]` | 感知（LiDAR/深度）可信度 | `ConfidenceEstimator.perception()` |
| `semantic_confidence` | `float[0,1]` | 语义理解可信度 | `ConfidenceEstimator.semantic()` |
| `route_confidence` | `float[0,1]` | 路线可信度 | `ConfidenceEstimator.route()` |
| `camera_freshness` | `float[0,1]` | 视觉通道新鲜度（fresh=1.0/stale=0.3/none=0.0） | `ConfidenceEstimator.camera()` |
| `uncertainty_reason` | `str` | "为什么不确定"的可读原因 | 估计器 `reasons()` |
| `overall_confidence` | `float|None` | 综合置信度（构造后必非 None） | 加权均值 + `model_validator` |

加权系数（`_CONFIDENCE_WEIGHTS`，`spatial_state.py:316`）：
`localization_confidence=0.30`、`perception_confidence=0.30`、
`semantic_confidence=0.20`、`route_confidence=0.20`；`camera_freshness` 不参与加权。

### 3.8 affordance（AffordanceState，v0.3 新增）

来源：融合层 `_build_affordance(env)`（由 `EnvironmentState` 推导面向行动的结论）。
三件套：**无 `timestamp`/`source`/`confidence`**（它是面向行动的结论，不是感知事实）。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `can_move_forward` | `bool` | 当前是否允许继续直行（默认 True） |
| `passable_width_m` | `float|None` | 可通行宽度（米）——等价 `environment.walkable_width` 的行动侧表达 |
| `blocked_regions` | `list[BlockedRegion]` | 无法通过的区域 |
| `preferred_direction` | `ObstacleDirection|None` | 综合建议移动方位（None=按原方向） |
| `summary` | `str` | 一句话可行动性描述 |

`BlockedRegion`：`direction:ObstacleDirection`、`distance:float`、`source_type:str`、
`severity:RiskLevel`、`preferred_direction:ObstacleDirection|None`。提供
`most_blocking()` 取最近阻挡。

`EnvironmentState` 回答"有什么"，`AffordanceState` 回答"能不能行动、往哪动"
（`spatial_state.py:388`），Safety Layer 与 Agent 直接消费后者。

## 4. 三件套契约（timestamp / source / confidence）

### 4.1 Gate 8 要求与现状对照

Gate 8（验收 S01，`tests/acceptance.py:240`）要求状态带齐三件套。实测断言覆盖：
`user`/`environment`/`navigation`/`semantic_scene` 的 `source`；
`user`/`environment`/`camera` 的 `timestamp`；以及 `confidence.localization_confidence`
与 `confidence.perception_confidence` 取值在 [0,1]。各子模型实际携带情况：

| 子模型 | timestamp | source | confidence |
| --- | --- | --- | --- |
| `user` (PoseState) | Y | Y | Y（`confidence`） |
| `navigation` (NavigationState) | Y | Y | 专用 `route_confidence`（无统一 confidence 字段） |
| `environment` (EnvironmentState) | Y | Y | Y（`confidence`） |
| `semantic_scene` (SemanticScene) | Y | Y | Y（`confidence`） |
| `camera` (CameraState) | Y | Y | N（用 `freshness`/`age_s` 代替） |
| `risk` (RiskState) | Y | Y | Y（`confidence`） |
| `confidence` (UncertaintyState) | N（本身即置信度容器） | N | 即自身 |
| `affordance` (AffordanceState) | N | N | N（行动结论，非感知） |
| `Obstacle` | Y（部分） | N | Y（`confidence`） |
| `SceneObject` | Y | Y | Y（`confidence`） |

结论：三件套在**感知类子模型（user/environment/semantic/risk/obstacle/sceneobject）**
上完整齐备；`camera` 用新鲜度代替 `confidence`；`navigation` 的置信度用专用字段
`route_confidence`；`confidence` 子模型与 `affordance` 子模型天然不带三件套（前者是
置信度本身，后者是行动结论）。

### 4.2 localization_confidence / perception_confidence 的取值来源

两者均来自 v0.3 Stage 4 引入的 `ConfidenceEstimator`（`fusion/confidence.py`，
`state_manager.py:88` 经 `estimator` 属性暴露）：

- `localization_confidence`（`ConfidenceEstimator.localization`，`confidence.py:107`）：
  综合三条证据加权（权重见 `confidence.py:66-68`）：
  - `q_noise`：UWB 自报噪声水平与参考值之比（`noise_ref_m=0.30`，`noise_span_m=1.20`）；
  - `q_innov`：相邻两帧**实测残差**（位移 − 速度×dt 预期位移）与噪声标准差之比——
    这是"真的出问题"的现场证据，而非纸面噪声；
  - `q_fresh`：数据新鲜度（过期即 0）。
  `mode="fixed"` 时返回常量 `fixed_value=0.90`（复现 v0.2 行为）。当 `pose_valid=False`
  或 `fresh=False` 时夹到 `_MISSING_CONF=0.30` 上限。**实测：定位噪声 1.5 m 时
  localization_confidence 可降到 0.341**。
- `perception_confidence`（`ConfidenceEstimator.perception`，`confidence.py:139`）：
  取 provider 自报先验值与新鲜度的较弱者；过期时 ×0.5，丢帧率 >0 时进一步下压。

来源链路：融合层 `StateFusion` 每轮建状态时调用估计器，结果写入 `UncertaintyState`
对应字段；`state_manager.build()`（`state_manager.py:108`）是统一入口。

## 5. to_prompt_dict() 裁剪视图 vs 完整 model_dump()

`to_prompt_dict()`（`spatial_state.py:499`）是**送大模型提示词**与 **`/api/state`**
的轻量视图（经 `StepResult.as_dict()` 嵌入，`agent_core.py:69`；提示词渲染见
`prompt_template.py:75`）。`model_dump()` 是完整结构。

**为什么不同：省 token、降噪声。** `to_prompt_dict()` 只保留决策必需字段、对浮点四舍五入、
截断障碍列表（`obstacles[:6]`）、截断阻挡区（`blocked_regions[:4]`）。完整 `model_dump()`
用于录制/回放（`recording/schemas.py:155` 明确要求完整 dump，因为要
`model_validate(...)` 往返重建；`to_prompt_dict()` 缺字段会导致 validate 失败）。

两者差异示例：

| 字段 | to_prompt_dict() | model_dump() |
| --- | --- | --- |
| `environment.walkable_width` | **不含**（裁剪掉了） | 含（完整字段） |
| `affordance.passable_width_m` | 含（等价信息在此） | 含 |
| `environment.left_distance` / `right_distance` | 不含 | 含 |
| `environment.valid` / `age_ms` / `confidence` / `source` / `timestamp` | 不含 | 含 |
| `navigation.current_route` / `replan_count` / `timestamp` / `source` | 不含 | 含 |
| `user.confidence` / `valid` / `age_ms` / `timestamp` / `source` | 不含 | 含 |
| `obstacles` 全字段（x/y/radius/is_dynamic/speed/confidence/timestamp） | 仅 type/distance/direction/dynamic，且 ≤6 条 | 完整，可超出 6 条 |
| `camera.frame_id` / `heading` / `source` | 不含（只 image_available/age_s/freshness） | 含 |
| `semantic_scene.objects` 完整字段 | 仅类型名列表 `types()` + summary | 含完整 SceneObject |
| `risk.confidence` / `source` / `timestamp` | 不含（只 level/reason） | 含 |
| `confidence` 五维 | 以短键 `localization/perception/semantic/route/camera/overall` 给出 | 字段原值 + `overall_confidence` |
| 顶层 `timestamp`（datetime） | 转为 `"time":"HH:MM:SS"` 字符串 | 原始 datetime |

注意 `to_prompt_dict()` 顶部兼容键：`confidence` 子对象里 `camera` 实际对应
`camera_freshness`；`uncertainty_reason` 提到顶层便于模型判断"怎么办"。

## 6. 字段命名与演进守则

1. **`extra="forbid"`**：每个子模型都有。任何未声明的字段在构造/`model_validate` 时直接报错。
2. **禁止 `@computed_field`**：见 `spatial_state.py:22` 与 `UncertaintyState._fill_overall`
   （`spatial_state.py:352`）。`computed_field` 会出现在 `model_dump()` 却不是合法入参，
   `model_validate(state.model_dump())` 往返（`tests/selftest.py:743`）立刻失败。
   需要"算出来的字段"用**普通字段 + `model_validator(mode="after")`** 补全。
3. **只增不删 + 字段名不变**：v0.3 对 v0.2 只做加法。`user`/`confidence` 字段名不变，
   类型升级为 `PoseState`/`UncertaintyState`。
4. **类名别名**：`UserState=PoseState`、`ConfidenceState=UncertaintyState`、
   `SemanticState=SemanticScene`、`CameraFrameMetadata=CameraState`。
5. **只读属性转发**：`pose`、`uncertainty`、`camera_meta` 是 `@property`，
   **不参与 dump**，因此不影响往返校验。
6. **`Action.as_dict()` 冻结**：`agent/action_schema.py:208` 的 `Action.as_dict()` 键集合
   被 `tests/selftest.py:2424`（用例 64）钉死
   `{action_type, message, urgency, reason, source, permits_motion}`。新增字段只能走
   `to_dict()`（`action_schema.py:223`），不得改 `as_dict()`。

## 7. 如何安全地给状态加一个新字段（分步清单）

目标：不破坏 v0.2 构造、不破坏 `model_validate(model_dump())` 往返、不破坏 `Action.as_dict()` 冻结线。

1. 在**对应子模型**里加一个**普通字段并给默认值**（`Field(default=...)` 或 `default_factory`），
   保证既有构造调用与断言继续成立（兼容式演进）。**不要**用 `@computed_field`。
2. 若字段是"算出来的"（依赖其它字段），用 `model_validator(mode="after")` 补全，
   参照 `UncertaintyState._fill_overall`（`spatial_state.py:352`）。
3. 若字段需要三件套，显式加 `timestamp`/`source`/`confidence`；
   `camera` 这类用新鲜度代替 `confidence` 的，写明原因（参照第 4.1 节）。
4. 若需要保留旧名访问点：保留旧字段名 + 加 `@property` 只读转发；**不要删旧字段**。
5. 跑测试：
   - `python -m tests.selftest` —— 确认用例 47（分层铁律：融合/工具/Agent 核心不 import simulator）
     与用例 64（`Action.as_dict()` 键集合冻结）仍通过；
   - `python -m tests.acceptance` —— 确认 S01（Gate 8 三件套）通过，即
     `SpatialState.model_validate(state.model_dump())` 往返无碍；
   - 若新字段影响决策，补一条针对该字段的断言。
6. 决定该字段是否进 `to_prompt_dict()`：进则确认 token 预算可控；不进则它只在
   `model_dump()`/录制里出现（如 `environment.walkable_width` 这类）。
7. 录制/回放不受影响：`extra="forbid"` + 普通字段即可保证 `model_dump()`→
   `model_validate()` 往返成功（`recording/schemas.py:157`）。
