"""统一空间状态模型（SpatialState）—— v0.3 Egocentric Spatial State。

设计要点：
1. **大模型永远不直接读传感器**。所有感知结果必须先归一化为本文件定义的结构化状态。
2. 本文件是整个系统的契约层：模拟器负责生产它，Agent 只消费它。
   未来接入真实 UWB / LiDAR / IMU / UE5 数字孪生时，只要还能产出同样结构的
   SpatialState，Agent 层一行代码都不用改。
3. v0.3 起，状态升级为 **自我中心空间状态（Egocentric Spatial State）**：
   每个子结构都带上 `timestamp` / `source` / `confidence` 三件套，
   并新增 `AffordanceState`（能不能行动）与 `UncertaintyState`（五维不确定性）。

★ 兼容性契约（改本文件前必读）★
    本文件对 v0.2 只做 **加法**，不做减法与改名：
    - `UserState`  → 类名改为 `PoseState`，但保留 `UserState = PoseState` 别名，
                     且 `SpatialState.user` 字段名不变（另有 `pose` 只读属性转发）。
    - `ConfidenceState` → 类名改为 `UncertaintyState`，保留别名，
                     且 `SpatialState.confidence` 字段名不变（另有 `uncertainty` 转发）。
    - `SemanticScene` → 保留，新增 `SemanticState` 别名。
    - `CameraState`   → 保留，新增 `CameraFrameMetadata` 别名，补齐 freshness/heading。
    新增字段**一律给默认值**，因此 v0.2 的构造调用与既有断言全部继续成立。

    ⚠️ 不要用 `@computed_field`：`tests/selftest.py:743` 会做
    `SpatialState.model_validate(state.model_dump())` 往返校验，而本模型
    `extra="forbid"` —— computed_field 会出现在 dump 里、却不是合法输入字段，
    往返立刻报错。需要"算出来的字段"请用普通字段 + `model_validator` 补全。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# =====================================================================
# 枚举
# =====================================================================


class WalkingStatus(str, Enum):
    """用户行走状态。"""

    STANDING = "standing"   # 站立未动
    MOVING = "moving"       # 正常行走
    SLOWING = "slowing"     # 因前方障碍减速
    BLOCKED = "blocked"     # 被阻挡，无法前进
    PAUSED = "paused"       # 用户主动停下（Agent 要求 WAIT）
    ARRIVED = "arrived"     # 已到达目的地


class RiskLevel(str, Enum):
    """风险等级，决定 Agent 的打扰强度。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ObstacleDirection(str, Enum):
    """障碍相对用户的方位（以用户朝向为基准）。"""

    FRONT = "front"
    FRONT_LEFT = "front_left"
    FRONT_RIGHT = "front_right"
    LEFT = "left"
    RIGHT = "right"
    BEHIND = "behind"


class ObstacleKind(str, Enum):
    """障碍类型。静态语义物体与动态障碍共用。"""

    PERSON = "person"
    CHAIR = "chair"
    DESK = "desk"
    DOOR = "door"
    BOX = "box"
    WALL = "wall"
    NARROW_PASSAGE = "narrow_passage"
    UNKNOWN = "unknown"


class FrameFreshness(str, Enum):
    """摄像头帧新鲜度。区分「从未有过」「有过但断了」「新鲜」。"""

    FRESH = "fresh"   # 新鲜，可送模型
    STALE = "stale"   # 曾经收到过，但已过期（推流可能断了）
    NONE = "none"     # 从未收到过任何帧


# =====================================================================
# 子结构
# =====================================================================


class Position(BaseModel):
    """三维位置（楼层用整数，室内同一层内 x/y 即够用）。"""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(..., description="东向坐标，单位米")
    y: float = Field(..., description="北向坐标，单位米")
    floor: int = Field(1, description="楼层")

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"({self.x:.1f},{self.y:.1f})"


class PoseState(BaseModel):
    """位姿状态（v0.2 中的 UserState）。

    来源：UWB（位置）+ IMU（朝向/速度）+ 步态（行走状态）。
    新增 `confidence`/`timestamp`/`source` 用于表达"这个位姿有多可信、多新、谁给的"。
    """

    model_config = ConfigDict(extra="forbid")

    position: Position
    heading: float = Field(0.0, description="朝向角度，0°=正北(+y)，顺时针为正，范围 [0,360)")
    speed: float = Field(0.0, ge=0.0, description="当前速度 m/s")
    walking_status: WalkingStatus = WalkingStatus.STANDING
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="位姿估计置信度（由融合层估计，非配置常量）")
    timestamp: datetime | None = Field(None, description="该位姿对应的采集时刻")
    source: str = Field("simulated", description="数据来源：simulated / uwb / imu / fused")
    valid: bool = Field(
        True,
        description="该位姿当前是否有效。定位过期/失效时必须置 False，"
        "而不是继续拿旧位姿当现状（任务书第 14 节第 6 项）",
    )
    age_ms: float | None = Field(None, ge=0.0, description="该位姿距融合时刻的毫秒数")

    @field_validator("heading")
    @classmethod
    def _normalize_heading(cls, v: float) -> float:
        return v % 360.0


# v0.2 兼容别名：老代码里的 UserState 继续可用
UserState = PoseState


class RoutePoint(BaseModel):
    """路线上的一个点（路点或导航节点）。"""

    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    name: str | None = None


class NavigationState(BaseModel):
    """导航状态：来自路径规划器（NavMesh / 重规划）。"""

    model_config = ConfigDict(extra="forbid")

    destination: str
    current_route: list[RoutePoint] = Field(default_factory=list, description="全局路线（剩余路点）")
    next_instruction: str = Field("", description="给用户的下一步指令，简短明确")
    distance_to_goal: float = Field(0.0, ge=0.0, description="剩余距离，米")
    route_progress: float = Field(0.0, description="整体进度 0~1")
    current_landmark: str | None = Field(None, description="当前正在前往的路点名称")
    off_route: bool = Field(False, description="是否偏离规划路线")
    replan_count: int = Field(0, ge=0, description="本轮累计重规划次数")
    route_confidence: float = Field(
        1.0, ge=0.0, le=1.0, description="路线可信度（重规划次数/偏离程度越低越高）"
    )
    timestamp: datetime | None = Field(None, description="该规划结果的生成时刻")
    source: str = Field("planner", description="数据来源：planner / navmesh / ue5")

    @property
    def route(self) -> list[RoutePoint]:
        """任务书命名的别名：`route` ⇄ `current_route`。"""
        return self.current_route

    @field_validator("route_progress")
    @classmethod
    def _clamp_progress(cls, v: float) -> float:
        return min(max(v, 0.0), 1.0)


class Obstacle(BaseModel):
    """单个障碍（静态或动态）。"""

    model_config = ConfigDict(extra="forbid")

    type: ObstacleKind = ObstacleKind.UNKNOWN
    distance: float = Field(..., ge=0.0, description="到用户的直线距离，米")
    direction: ObstacleDirection = ObstacleDirection.FRONT
    x: float | None = None
    y: float | None = None
    radius: float = Field(0.3, ge=0.0, description="近似半径，米")
    is_dynamic: bool = Field(False, description="是否动态障碍")
    speed: float = Field(0.0, ge=0.0, description="障碍自身移动速度 m/s")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="该障碍被检出的置信度")
    timestamp: datetime | None = Field(None, description="检出时刻")


class EnvironmentState(BaseModel):
    """环境状态：来自 LiDAR 深度 + 可通行区域判断。"""

    model_config = ConfigDict(extra="forbid")

    front_clear: bool = Field(..., description="正前方是否可通行")
    front_distance: float = Field(..., ge=0.0, description="正前方最近障碍/墙体距离，米；无障碍时等于雷达量程")
    left_obstacle: bool = Field(False, description="左侧近距离内是否有物体（不含正常走廊侧墙）")
    right_obstacle: bool = Field(False, description="右侧近距离内是否有物体（不含正常走廊侧墙）")
    left_distance: float | None = Field(None, ge=0.0)
    right_distance: float | None = Field(None, ge=0.0)
    corridor_width: float | None = Field(None, ge=0.0, description="估计的可通行宽度，米")
    obstacles: list[Obstacle] = Field(default_factory=list)
    narrow_passage: bool = Field(False, description="是否处于变窄通道")

    # --- v0.3 新增：可通行性量化 + 高危地形 ---
    walkable_width: float | None = Field(
        None, ge=0.0, description="可通行宽度（corridor_width 的语义化命名，二者同源）"
    )
    blocking_ratio: float = Field(
        0.0, ge=0.0, le=1.0, description="前方视场内被阻挡的角度占比 0~1，越大越堵"
    )
    stairs: bool = Field(False, description="前方是否存在台阶")
    dropoff: bool = Field(False, description="前方是否存在坠落风险（悬空/坑洞）")
    timestamp: datetime | None = Field(None, description="该环境快照的采集时刻")
    source: str = Field("simulated", description="数据来源：simulated / lidar / depth / ue5")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="环境感知置信度")
    valid: bool = Field(True, description="该环境快照是否有效（深度链路断掉时置 False）")
    age_ms: float | None = Field(None, ge=0.0, description="该快照距融合时刻的毫秒数")


class SceneObject(BaseModel):
    """语义物体（多模态大模型或视觉检测的产物）。"""

    model_config = ConfigDict(extra="forbid")

    type: str
    description: str | None = None
    distance: float | None = Field(None, ge=0.0)
    direction: ObstacleDirection | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    label: str | None = Field(None, description="人类可读标签（默认取 type）")
    timestamp: datetime | None = Field(None, description="检出时刻")
    source: str = Field("map", description="数据来源：map / vlm / detector")


class SemanticScene(BaseModel):
    """语义场景理解结果。"""

    model_config = ConfigDict(extra="forbid")

    objects: list[SceneObject] = Field(default_factory=list)
    summary: str = Field("", description="一句话场景描述，供模型快速理解")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="整体语义置信度")
    timestamp: datetime | None = Field(None, description="该语义快照的生成时刻")
    source: str = Field("map", description="数据来源：map / vlm / detector")

    def types(self) -> list[str]:
        return [o.type for o in self.objects]


# 任务书命名别名
SemanticState = SemanticScene


class CameraState(BaseModel):
    """摄像头状态（iPhone 图片流）—— 即任务书中的 CameraFrameMetadata。"""

    model_config = ConfigDict(extra="forbid")

    image_available: bool = False
    timestamp: datetime | None = None
    age_s: float | None = Field(None, ge=0.0, description="图片距现在多久，秒")
    source: Literal["none", "websocket", "upload", "simulated"] = "none"
    frame_id: int = Field(0, ge=0)
    freshness: FrameFreshness = Field(
        FrameFreshness.NONE,
        description="新鲜度：fresh 可送模型 / stale 曾有过但断了 / none 从未收到",
    )
    heading: float | None = Field(
        None, ge=0.0, lt=360.0, description="拍摄时的朝向（如果设备能提供）"
    )


# 任务书命名别名
CameraFrameMetadata = CameraState


class RiskState(BaseModel):
    """风险判断结果。

    ⚠️ v0.3 迁移提示：按任务书第七节，风险判定属于 **Safety Layer**，
    不应由融合层预判。本结构在 v0.4 里会从 SpatialState 迁出，
    这里先补齐 `confidence`/`source`，保留字段以维持兼容。
    """

    model_config = ConfigDict(extra="forbid")

    level: RiskLevel = RiskLevel.LOW
    reason: str = ""
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="该风险判定的置信度")
    source: str = Field("fusion", description="数据来源：fusion / safety")
    timestamp: datetime | None = Field(None, description="判定时刻")


# 综合置信度的加权系数：定位与感知是行动安全的直接依据，权重更高。
# 刻意放在模块级而不是类属性 —— Pydantic v2 会把下游线开头的类属性当成私有属性，
# 可变默认值（dict）会触发告警，不如直接放模块常量干净。
#
# `camera_freshness` **刻意不参与**这条加权：视觉是"按需使用"的通道
# （主动感知触发时才要图），把它计入综合值会让"没接摄像头"一路拉低整体置信度，
# 反而掩盖真正的安全维度（定位/感知）。它单独成维度、单独判断。
_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "localization_confidence": 0.30,
    "perception_confidence": 0.30,
    "semantic_confidence": 0.20,
    "route_confidence": 0.20,
}


class UncertaintyState(BaseModel):
    """五维不确定性（v0.2 中的 ConfidenceState）。

    为什么要五维而不是一个总分：**不同维度的"低"触发完全不同的动作** ——
    定位低 → 询问用户位置；感知低 → 减速或请求重新观察；
    语义低 → 不要据语义下结论；路线低 → 考虑重规划。
    合成一个总分会让这些分支无从判断。
    """

    model_config = ConfigDict(extra="forbid")

    localization_confidence: float = Field(1.0, ge=0.0, le=1.0, description="定位（UWB）可信度")
    perception_confidence: float = Field(1.0, ge=0.0, le=1.0, description="感知（LiDAR/深度）可信度")
    semantic_confidence: float = Field(1.0, ge=0.0, le=1.0, description="语义理解可信度")
    route_confidence: float = Field(1.0, ge=0.0, le=1.0, description="路线可信度")
    camera_freshness: float = Field(
        1.0, ge=0.0, le=1.0,
        description="视觉通道新鲜度分数（fresh=1.0 / stale=0.3 / none=0.0）。"
        "与 CameraState.freshness 同源，但这里是可参与阈值判断的数值。",
    )
    uncertainty_reason: str = Field(
        "", description="「为什么不确定」的可读原因，如 'UWB 读数过期；摄像头推流中断'"
    )
    overall_confidence: float | None = Field(
        None, ge=0.0, le=1.0,
        description="综合置信度。留空时由校验器按加权均值补全（构造后必定非 None）。",
    )

    @model_validator(mode="after")
    def _fill_overall(self) -> "UncertaintyState":
        """补全综合置信度。

        用普通字段 + 校验器而不是 `@computed_field`：
        computed_field 会进入 `model_dump()` 却不是合法入参，而本模型
        `extra="forbid"` —— 上游做 dump→validate 往返时会直接报错（踩过）。
        """
        if self.overall_confidence is None:
            self.overall_confidence = round(
                sum(getattr(self, k) * w for k, w in _CONFIDENCE_WEIGHTS.items()), 4
            )
        return self


# v0.2 兼容别名
ConfidenceState = UncertaintyState


class BlockedRegion(BaseModel):
    """一块"过不去"的区域 —— Affordance 的基本单元。"""

    model_config = ConfigDict(extra="forbid")

    direction: ObstacleDirection = Field(..., description="该阻挡所在的方位")
    distance: float = Field(..., ge=0.0, description="该阻挡离用户多近，米")
    source_type: str = Field("unknown", description="造成阻挡的物体类型（chair/desk/wall…）")
    severity: RiskLevel = Field(RiskLevel.MEDIUM, description="阻挡的严重程度")
    preferred_direction: ObstacleDirection | None = Field(
        None, description="建议的绕行方位（None 表示没有更好的选择）"
    )


class AffordanceState(BaseModel):
    """可行动性状态 —— v0.3 新增的核心。

    与 `EnvironmentState` 的分工：
        EnvironmentState 回答 **"有什么"**（前方 1.2 米处有把椅子）
        AffordanceState  回答 **"能不能行动、往哪动"**（正前方被挡，建议向左）

    前者是感知事实，后者是**面向行动的结论**。Agent 与 Safety Layer 直接消费后者，
    这样"从感知到行动"的语义推理不会散落在各个 if-else 里。
    """

    model_config = ConfigDict(extra="forbid")

    can_move_forward: bool = Field(True, description="当前是否允许继续直行")
    passable_width_m: float | None = Field(None, ge=0.0, description="可通行宽度，米")
    blocked_regions: list[BlockedRegion] = Field(default_factory=list, description="无法通过的区域")
    preferred_direction: ObstacleDirection | None = Field(
        None, description="综合建议的移动方位（None 表示按原方向即可）"
    )
    summary: str = Field("", description="一句话可行动性描述，供模型快速理解")

    def most_blocking(self) -> BlockedRegion | None:
        """返回最紧迫的阻挡（距离最近的）。"""
        if not self.blocked_regions:
            return None
        return min(self.blocked_regions, key=lambda r: r.distance)


# =====================================================================
# 顶层状态
# =====================================================================


class SpatialState(BaseModel):
    """统一空间状态 —— 系统内唯一的状态契约。

    数据流：
        模拟器/真实硬件  ->  State Fusion  ->  SpatialState  ->  Agent（含多模态大模型）
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    tick: int = Field(0, ge=0, description="状态序号，每轮感知 +1")

    # 字段名保持 v0.2 的 `user` / `confidence`，类型已升级为 v0.3 的
    # PoseState / UncertaintyState。另外提供 pose / uncertainty 只读属性转发。
    user: PoseState
    navigation: NavigationState
    environment: EnvironmentState
    semantic_scene: SemanticScene = Field(default_factory=SemanticScene)
    camera: CameraState = Field(default_factory=CameraState)
    risk: RiskState = Field(default_factory=RiskState)
    confidence: UncertaintyState = Field(default_factory=UncertaintyState)
    affordance: AffordanceState = Field(default_factory=AffordanceState)

    # -----------------------------------------------------------------
    # 任务书命名的只读转发（属性不参与 dump，因此不影响往返校验）
    # -----------------------------------------------------------------
    @property
    def pose(self) -> PoseState:
        return self.user

    @property
    def uncertainty(self) -> UncertaintyState:
        return self.confidence

    @property
    def camera_meta(self) -> CameraState:
        return self.camera

    @model_validator(mode="after")
    def _consistency(self) -> "SpatialState":
        """状态自洽性校验：发现矛盾立刻暴露，而不是让错误悄悄流到模型层。"""
        env = self.environment

        # 1) front_distance 必须是「正前方锥形区域内障碍的最近距离」，否则说明融合层算错了
        front_obstacles = [o for o in env.obstacles if o.direction == ObstacleDirection.FRONT]
        if front_obstacles:
            nearest = min(o.distance for o in front_obstacles)
            if nearest < env.front_distance - 1e-6:
                raise ValueError(
                    f"环境状态不自洽: front_distance={env.front_distance:.2f} "
                    f"小于前方最近障碍距离 {nearest:.2f}"
                )

        # 2) 台阶/坠落属于高危地形，一旦置位，风险等级不得低于 high。
        #    融合层必须同时给出风险判定，否则这条会立刻暴露"感知到了却没当回事"的 bug。
        if (env.stairs or env.dropoff) and self.risk.level in (RiskLevel.LOW, RiskLevel.MEDIUM):
            raise ValueError(
                f"安全状态不自洽: stairs={env.stairs} dropoff={env.dropoff} "
                f"属于高危地形，但 risk.level={self.risk.level.value}"
            )

        # 3) 前后矛盾检测：可通行宽度为 0 时不可能还能直行
        if env.walkable_width is not None and env.walkable_width <= 0.0 and self.affordance.can_move_forward:
            raise ValueError(
                "可行动性不自洽: 可通行宽度为 0，却仍标记 can_move_forward=True"
            )

        # 4) 定位失效（valid=False）时不得同时宣称高定位置信度。
        #    这条防的是"UWB 断了但 confidence 还挂着 0.9"这类静默撒谎 ——
        #    Agent 会据此回答"您已到达走廊北段"，而用户其实不知道自己在哪。
        if not self.user.valid and self.confidence.localization_confidence > 0.6:
            raise ValueError(
                "定位状态不自洽: pose.valid=False，"
                f"但 localization_confidence={self.confidence.localization_confidence}"
            )
        return self

    # -----------------------------------------------------------------
    # 供 Agent / 大模型使用的轻量视图
    # -----------------------------------------------------------------

    def to_prompt_dict(self) -> dict:
        """精简版状态：只保留决策必需字段，降低 token 消耗。"""
        env = self.environment
        aff = self.affordance
        return {
            "tick": self.tick,
            "time": self.timestamp.strftime("%H:%M:%S"),
            "user": {
                "position": [round(self.user.position.x, 2), round(self.user.position.y, 2)],
                "floor": self.user.position.floor,
                "heading_deg": round(self.user.heading, 1),
                "speed": round(self.user.speed, 2),
                "walking_status": self.user.walking_status.value,
            },
            "navigation": {
                "destination": self.navigation.destination,
                "next_instruction": self.navigation.next_instruction,
                "distance_to_goal": round(self.navigation.distance_to_goal, 2),
                "route_progress": round(self.navigation.route_progress, 3),
                "current_landmark": self.navigation.current_landmark,
                "off_route": self.navigation.off_route,
                "route_confidence": round(self.navigation.route_confidence, 2),
            },
            "environment": {
                "front_clear": env.front_clear,
                "front_distance": round(env.front_distance, 2),
                "left_obstacle": env.left_obstacle,
                "right_obstacle": env.right_obstacle,
                "corridor_width": None if env.corridor_width is None else round(env.corridor_width, 2),
                "narrow_passage": env.narrow_passage,
                "blocking_ratio": round(env.blocking_ratio, 2),
                "stairs": env.stairs,
                "dropoff": env.dropoff,
                "obstacles": [
                    {
                        "type": o.type.value,
                        "distance": round(o.distance, 2),
                        "direction": o.direction.value,
                        "dynamic": o.is_dynamic,
                    }
                    for o in env.obstacles[:6]
                ],
            },
            "affordance": {
                "can_move_forward": aff.can_move_forward,
                "passable_width_m": None if aff.passable_width_m is None else round(aff.passable_width_m, 2),
                "preferred_direction": (
                    aff.preferred_direction.value if aff.preferred_direction else None
                ),
                "blocked": [
                    {"direction": r.direction.value, "distance": round(r.distance, 2),
                     "type": r.source_type, "prefer": r.preferred_direction.value if r.preferred_direction else None}
                    for r in aff.blocked_regions[:4]
                ],
                "summary": aff.summary,
            },
            "semantic_scene": {
                "objects": self.semantic_scene.types(),
                "summary": self.semantic_scene.summary,
            },
            "camera": {
                "image_available": self.camera.image_available,
                "age_s": None if self.camera.age_s is None else round(self.camera.age_s, 1),
                "freshness": self.camera.freshness.value,
            },
            "risk": {"level": self.risk.level.value, "reason": self.risk.reason},
            # 兼容键：v0.2 的 [置信度] 渲染行读的是 localization / perception
            "confidence": {
                "localization": round(self.confidence.localization_confidence, 2),
                "perception": round(self.confidence.perception_confidence, 2),
                "semantic": round(self.confidence.semantic_confidence, 2),
                "route": round(self.confidence.route_confidence, 2),
                "camera": round(self.confidence.camera_freshness, 2),
                "overall": round(self.confidence.overall_confidence or 0.0, 2),
            },
            # 「为什么不确定」—— 只给数值不给原因，模型无从判断该怎么办
            "uncertainty_reason": self.confidence.uncertainty_reason,
        }

    def summary_line(self) -> str:
        """单行摘要，用于日志与终端输出。"""
        heading = round(self.user.heading) % 360
        return (
            f"tick={self.tick} pos={self.user.position} heading={heading}° "
            f"front={'clear' if self.environment.front_clear else f'{self.environment.front_distance:.2f}m'} "
            f"risk={self.risk.level.value} status={self.user.walking_status.value}"
        )

    def affordance_line(self) -> str:
        """可行动性单行摘要（终端/日志用）。"""
        aff = self.affordance
        if aff.can_move_forward and not aff.blocked_regions:
            return "可直行"
        prefer = aff.preferred_direction.value if aff.preferred_direction else "无更好方向"
        return f"{'可直行' if aff.can_move_forward else '不可直行'}，建议方位={prefer}，{aff.summary}"


__all__ = [
    "AffordanceState",
    "BlockedRegion",
    "CameraFrameMetadata",
    "CameraState",
    "ConfidenceState",
    "EnvironmentState",
    "FrameFreshness",
    "NavigationState",
    "Obstacle",
    "ObstacleDirection",
    "ObstacleKind",
    "PoseState",
    "Position",
    "RiskLevel",
    "RiskState",
    "RoutePoint",
    "SceneObject",
    "SemanticScene",
    "SemanticState",
    "SpatialState",
    "UncertaintyState",
    "UserState",
    "WalkingStatus",
]
