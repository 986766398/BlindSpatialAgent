"""传感器抽象层（Sensor Adapter Layer）。

存在的唯一理由：**让上层不认识硬件**。

分层约定（任务书第四节）：
    sensors/base.py        本文件 —— 抽象契约，零硬件依赖
    sensors/simulated/     阶段一/二：接到现有模拟器上
    sensors/future/        阶段三：UWB / 眼镜 IMU / iPhone LiDAR / UE5 的接口预留

三条铁律：
1. **融合层与 Agent 只认本文件的接口**。`fusion` / `agent` / `agent.tools` 里
   不允许出现 `from simulator import ...`。
2. **感知与"推进世界"分开**。`SensorProvider` 只负责读；推进仿真（移动用户、
   更新障碍）是 `WorldStepper` 的事，真实硬件下它就是个空实现。
3. **每个读数都要能回答"多新、多可信、还活着吗"**。所以位姿带 confidence/timestamp，
   整体带 `health()`（供 CAMERA_LOST / UWB 失效这类事件判定）。

为什么 `RawPerception.pose_reference` 不叫 `true_pose`：
    模拟器给的是几何真值，真实硬件根本没有"真值"这东西 —— 只有最近一次位姿估计。
    同一个字段在两种实现下含义都是"做几何计算时的参考位姿"，所以用中性命名，
    免得对接真机时被字段名误导。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from spatial.spatial_state import (
    NavigationState,
    PoseState,
    RoutePoint,
    WalkingStatus,
)

# =====================================================================
# 传感器身份与健康度
# =====================================================================


class SensorKind(str, Enum):
    """系统认识的传感器种类。"""

    UWB = "uwb"          # 定位
    IMU = "imu"          # 姿态 / 步速
    LIDAR = "lidar"      # 前向深度
    DEPTH = "depth"      # 深度相机（阶段三）
    CAMERA = "camera"    # 摄像头（多模态大模型的眼睛）
    MAP = "map"          # 静态空间知识（地图/数字孪生）
    PLANNER = "planner"  # 路径规划器


class SensorStatus(str, Enum):
    """单个传感器通道的存活状态。

    三态而不是布尔量，是因为「没有这个硬件」和「有但坏了」的处理方式完全不同：
    前者应降级但不报警，后者必须产生事件并告知用户。
    """

    OK = "ok"              # 正常工作
    DEGRADED = "degraded"  # 能读但质量下降（噪声大 / 丢帧多）
    LOST = "lost"          # 曾经工作过，现在断了
    ABSENT = "absent"      # 本系统就没接这个传感器


class SensorHealth(BaseModel):
    """单个传感器的健康快照。事件引擎（Stage 6）据此产出 CAMERA_LOST 等事件。"""

    model_config = ConfigDict(extra="forbid")

    kind: SensorKind
    status: SensorStatus = SensorStatus.OK
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    age_s: float | None = Field(None, ge=0.0, description="距上次有效读数的秒数")
    detail: str = ""

    @property
    def usable(self) -> bool:
        """还能不能用它的数据做决策。"""
        return self.status in (SensorStatus.OK, SensorStatus.DEGRADED)


# =====================================================================
# 静态空间知识（地图快照）
# =====================================================================


def shape_distance(
    px: float,
    py: float,
    cx: float,
    cy: float,
    radius: float,
    hx: float = 0.0,
    hy: float = 0.0,
) -> float:
    """点到"地图物体表面"的距离（内部为 0）。

    `hx > 0 and hy > 0` 时按轴对齐矩形算，否则按圆算。
    障碍候选在融合层是裸 dict（不走 Pydantic），所以把这段几何独立成函数，
    让 `MapObjectInfo.distance_to()` 与融合层共用同一份实现 —— 免得两处对
    矩形的理解悄悄跑偏（一个按中心点、一个按矩形边）。
    """
    if hx > 0.0 and hy > 0.0:
        dx = max(abs(px - cx) - hx, 0.0)
        dy = max(abs(py - cy) - hy, 0.0)
        return math.hypot(dx, dy)
    return math.hypot(px - cx, py - cy) - radius


class MapObjectInfo(BaseModel):
    """地图上的一个静态物体（语义地标或阻挡物）。

    两种形状：
      * 圆形物体（桌/椅/绿植/垃圾桶…）—— 用 `x, y, radius` 描述。
      * 区域型障碍（施工围挡、沙发组、储物柜墙、拥堵人流区…）—— 用 `x, y` 作中心
        另加半宽 `hx` / 半高 `hy` 描述一个轴对齐矩形。区域障碍不可能是"一个点"，
        用圆形近似会把 6m×1m 的围挡画成半径 3m 的大圆，语义与避障都会失真。

    `radius` 对区域型障碍恒为 0：判定形状一律走 `is_area()`，不要看 radius。
    """

    model_config = ConfigDict(extra="forbid")

    type: str
    x: float
    y: float
    radius: float
    blocking: bool = Field(False, description="是否真实阻挡通行（进占用栅格）")
    hx: float = Field(0.0, description="区域型障碍的半宽（>0 表示矩形障碍）")
    hy: float = Field(0.0, description="区域型障碍的半高（>0 表示矩形障碍）")

    def is_area(self) -> bool:
        """是否为区域型（矩形）障碍。"""
        return self.hx > 0.0 and self.hy > 0.0

    def distance_to(self, x: float, y: float) -> float:
        """点 (x,y) 到该物体**表面**的距离（内部为 0，不会是负数）。

        圆形：欧氏距离 - 半径；矩形：点到矩形的最近距离。
        统一走 `shape_distance()`，免得调用方各自用"圆心距 - 半径"去套矩形。
        """
        return shape_distance(x, y, self.x, self.y, self.radius, self.hx, self.hy)


class MapSnapshot(BaseModel):
    """静态空间知识快照 —— 不随时间变化，因此可以缓存。

    来自二维地图 / UE5 数字孪生 / 预建 3D 地图，都属于"长期空间知识"。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    floor: int
    bounds: dict[str, float]
    grid: dict[str, float]
    start: dict[str, Any]
    destination: str
    route: list[str] = Field(default_factory=list, description="全局路点名称序列")
    zones: list[dict[str, Any]] = Field(default_factory=list)
    landmarks: list[dict[str, Any]] = Field(default_factory=list)
    objects: list[MapObjectInfo] = Field(default_factory=list)

    def blocking_objects(self) -> list[MapObjectInfo]:
        """只取会真实挡路的物体（桌子/椅子），门与路人不在其中。"""
        return [o for o in self.objects if o.blocking]


# =====================================================================
# 原始感知结果
# =====================================================================


@dataclass
class RawPerception:
    """一轮"未融合"的原始感知输入。

    这里刻意只放**候选与读数**，不放结论：
        "前方 1.2 米有个椅子" 是融合层算出来的，"前方测到 1.2 米、方位偏右 8°"才是感知。
    把结论留给融合层，才能保证"换传感器不换决策"。
    """

    pose_reference: tuple[float, float] = (0.0, 0.0)
    heading_reference: float = 0.0
    depth_range: float = 5.0
    wall_ahead: float | None = None
    side_left: float | None = None
    side_right: float | None = None
    obstacle_candidates: list[dict[str, Any]] = field(default_factory=list)
    semantic_candidates: list[dict[str, Any]] = field(default_factory=list)
    obstacle_ids: list[int] = field(default_factory=list)
    timestamp: datetime | None = None
    source: str = "unknown"


@dataclass
class WorldReference:
    """**不加噪、不消耗传感器随机数**的世界参考快照。

    给谁用：
        - `agent/tools.py`：工具要回答"我在哪、还有多远"，这些是程序内部
          记账用的几何量，不该再走一遍 UWB 噪声（否则每调一次工具就多消耗
          一次随机数，仿真可复现性会被工具调用次数影响）。
        - 调试可视化。

    给谁**不**用：给大模型/用户的任何事实都必须来自 `SpatialState`。
    所以这里的字段名统一带 `reference` 语义（本类名即是），不要把它当成
    "真实位姿"去对外播报 —— 真机没有"真值"，只有最近一次位姿估计。
    """

    x: float
    y: float
    heading: float
    speed: float
    floor: int = 1
    zone: str | None = None
    walking_status: WalkingStatus = WalkingStatus.STANDING
    arrived: bool = False
    destination: str = ""
    current_landmark: str = ""
    next_instruction: str = ""
    off_route: bool = False
    replan_count: int = 0
    remaining_distance: float = 0.0
    route_progress: float = 0.0
    route: list[RoutePoint] = field(default_factory=list)
    local_path_points: int = 0


# =====================================================================
# 抽象契约
# =====================================================================


@runtime_checkable
class SensorProvider(Protocol):
    """感知侧契约 —— **只读**，不推进世界。

    实现方：`sensors.simulated.SimulatedProvider`（阶段一/二）、
    未来的 `UwbProvider` / `IPhoneLidarProvider` / `UE5Provider`。

    ★方法分成三组，调用顺序有讲究★
        （甲）加噪读数：`read_pose()` / `measure_depth()` / `scan_*()`
             —— 会消耗传感器随机数，**必须由融合层按固定顺序调用**，
                否则噪声序列变了，仿真就不再可复现。
        （乙）候选枚举：`perceive()`
             —— 纯读取，不消耗随机数。只给"有什么候选"，不给结论。
        （丙）静态知识与质量：`map_snapshot()` / `zone_at()` / `health()` …
             —— 不随时间变化或只反映健康状况。
    """

    # --- (甲) 加噪读数 -------------------------------------------------
    def read_pose(self, now: datetime | None = None) -> PoseState:
        """当前位姿（已含噪声、已带 confidence/timestamp/source）。"""

    def measure_depth(self, distance: float) -> float | None:
        """对给定距离施加深度传感器的噪声与丢帧；丢帧返回 None。

        为什么由 provider 来加噪而不是融合层：
        噪声模型是**传感器的属性**（LiDAR 的噪声方差、丢帧率），不是融合逻辑。
        换成真实硬件后这里直接返回传入值（真值即读数）。
        """

    def scan_wall_ahead(self) -> float | None:
        """窄视场（正前方）测墙距离：用于判断"走廊是不是走到头了"。

        为什么必须是独立方法、而不能并进 `perceive()`：
        宽视场扫描会把走廊侧墙也算进来；更关键的是，它与 `scan_sides()`
        **之间**还要穿插逐障碍的 `measure_depth()`（v0.2 就是这个顺序），
        合并会让随机数消耗顺序改变，噪声序列随之错位。
        """

    def scan_sides(self) -> tuple[float | None, float | None]:
        """左/右侧扫描，用于估算可通行宽度。"""

    # --- (乙) 候选枚举（无噪声） ---------------------------------------
    def perceive(self) -> RawPerception:
        """采集一轮**候选**（障碍候选、语义候选、参考位姿、雷达量程）。"""

    # --- (丙) 静态知识与质量 -------------------------------------------
    def map_snapshot(self) -> MapSnapshot:
        """静态空间知识快照。"""

    def zone_at(self, x: float, y: float) -> str | None:
        """查询某个坐标属于哪个区域（入口区 / 走廊北段 …）。"""

    def floor(self) -> int:
        """当前楼层。"""

    def read_navigation(self, now: datetime | None = None) -> NavigationState:
        """当前导航状态（来自规划器，不含传感器噪声）。

        `route_confidence` 由融合层算好后回填 —— 它是"这条路线还可不可信"的
        估计，属于融合层的判断，不属于规划器的输出。
        """

    def confidence(self) -> tuple[float, float]:
        """返回 (定位置信度, 感知置信度)。"""

    def health(self) -> dict[SensorKind, SensorHealth]:
        """各传感器通道的健康状态。事件引擎（Stage 6）据此产出 CAMERA_LOST 等。"""

    def sensor_stats(self) -> dict[str, Any]:
        """传感器运行统计（噪声参数、丢帧率、置信度）。

        ⚠️ 刻意**不叫** `stats()`：`WorldStepper` 也有一个 `stats()` 表示"世界运行统计"。
        仿真实现（`SimulatedProvider`）两个协议都要满足，方法同名会互相覆盖 ——
        实测踩到过（`KeyError: 'arrived'`：世界统计被传感器统计顶掉了）。
        名字分开之后，"谁在问哪种统计"在调用点一目了然。
        """


@runtime_checkable
class WorldStepper(Protocol):
    """世界推进 + 参考读数契约 —— 只有模拟器/数字孪生才需要实现。

    真实硬件下用户是自己走路的，没有"推进"这回事，实现成空方法即可；
    这也正是把它和 `SensorProvider` 拆开的原因。

    `world_reference()` 是这里唯一的"读"接口，且刻意**不加噪**：
    给工具与可视化用的几何量不应再走一遍噪声模型（见 `WorldReference` 文档）。
    """

    def advance_environment(self, dt: float) -> None:
        """推进环境本身（动态障碍移动、生命周期、新事件生成）。"""

    def advance_user(self, dt: float, *, motion_allowed: bool, front_distance: float) -> None:
        """推进用户位姿。`motion_allowed` 来自 Agent 决策（人机协同的关键开关）。"""

    def replan(self) -> bool:
        """触发重规划，返回是否成功。"""

    def reset(self) -> None:
        """回到初始状态。"""

    def obstacle_centers(self) -> list[tuple[float, float]]:
        """动态障碍的绝对坐标（供规划器避让）。"""

    def arrived(self) -> bool:
        """用户是否已到达目的地（主循环据此判定收工）。"""

    def world_reference(self) -> WorldReference:
        """★不加噪★ 参考读数：位姿 / 路线 / 进度 / 本地路径长度。

        供工具查询与可视化使用；给模型与用户的结论一律取 `SpatialState`。
        """

    def debug_ground_truth(self) -> dict[str, Any]:
        """★仅供调试可视化★ 输出仿真真值（绝对坐标、完整路径、障碍位置）。

        为什么允许这个"后门"：实时小地图要画真实几何才能定位问题，
        而给用户/模型的消息必须走 SpatialState。
        命名里带 debug 是故意的 —— 谁在业务逻辑里用它，一眼就能被 review 出来。
        """

    def stats(self) -> dict[str, Any]:
        """运行统计。**必须**包含这些键（前端与自检依赖它们）：

            arrived(bool) / progress(0~1) / distance_to_goal_m / replans(int)
            map(dict) / sensors(dict)
            obstacles_active(int) / obstacles_spawned(int)
        """


@runtime_checkable
class CameraProvider(Protocol):
    """摄像头数据来源的协议（真实实现：`camera.iphone_receiver.IphoneReceiver`）。"""

    def snapshot(self) -> dict[str, Any]:
        """返回 image_available / timestamp / age_s / source / frame_id。"""

    def latest_bytes(self) -> bytes | None:
        """返回可送多模态模型的最新帧；过期必须返回 None。"""


__all__ = [
    "CameraProvider",
    "MapObjectInfo",
    "MapSnapshot",
    "RawPerception",
    "SensorHealth",
    "SensorKind",
    "SensorProvider",
    "SensorStatus",
    "WorldReference",
    "WorldStepper",
    "shape_distance",
]
