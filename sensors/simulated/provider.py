"""模拟传感器提供者：把仿真环境包装成"传感器 + 世界推进器"。

★这是 Stage 3 的核心交付物★ —— 它出现的唯一目的，是让融合层与 Agent
从此不再写 `from simulator import ...`：

    v0.2：  agent_core ──直接 new──> MapSimulator / NavigationSimulator / …
            state_manager ──直接读──> 四个模拟器对象
            tools         ──直接读──> 三个模拟器对象
            ⇒ 换硬件要改三个文件，融合层认识模拟器。

    v0.3：  agent_core ──装配──> SimulatedProvider（本类）
            state_manager ──只认──> SensorProvider 协议
            tools         ──只认──> SensorProvider + WorldStepper 协议
            ⇒ 换硬件只替换本文件（或换成 sensors/future/ 下的实现）。

为什么本类**同时**实现 `SensorProvider` 与 `WorldStepper`：
    仿真里"感知"与"推进世界"用的是同一套配置、同一套 RNG、同一份地图，
    硬拆成两个对象只会带来"两个对象要共享同一个 nav"的耦合。
    两个协议本身仍是分开的 —— 接真机时 `UwbProvider` 只实现前者，
    推进世界的部分变成空实现（用户自己在走）。

⚠️ `self.map` / `self.nav` / `self.obstacles` / `self.sensors` 是**内部实现细节**
    （继承自 `SimulatedEnvironment`），仅供自检与调试工具直接访问。
    业务代码（`spatial/` `agent/`）一律走本文件的方法。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sensors.base import (
    MapObjectInfo,
    MapSnapshot,
    RawPerception,
    SensorHealth,
    SensorKind,
    SensorStatus,
    WorldReference,
)
from sensors.simulated.environment import SimulatedEnvironment
from spatial.spatial_state import NavigationState, PoseState, Position

# 静态阻挡物体的稳定 ID 起点。
# 与动态障碍（`_next_id` 从 1 开始）隔开，保证两类物体在同一个世界模型里
# 能被跨帧追踪而不撞号。v0.2 用的就是这个常量，保持不变以保证行为一致。
STATIC_OID_BASE = 100000


class SimulatedProvider(SimulatedEnvironment):
    """阶段一/二的 `SensorProvider` + `WorldStepper` 实现。"""

    SOURCE = "simulated"

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        seed: int | None = None,
        enable_obstacles: bool = True,
        camera: Any | None = None,
    ) -> None:
        super().__init__(cfg, seed=seed, enable_obstacles=enable_obstacles)
        # 摄像头不是"模拟器"，它是真实数据源（iPhone 推流），所以只持有引用。
        self.camera = camera

    # =================================================================
    # (甲) 加噪读数 —— 会消耗传感器随机数，融合层必须按固定顺序调用
    # =================================================================
    def read_pose(self, now: datetime | None = None) -> PoseState:
        """UWB 定位 + IMU 姿态 → 归一化位姿。

        调用顺序刻意与 v0.2 一致（UWB → 朝向 → 速度），因为每步都从
        `SensorSimulator.rng` 取数；顺序一变，噪声序列就错位，
        同一 seed 下的轨迹会与 v0.2 不一致（行为回归测试会红）。
        """
        mpos = self.sensors.uwb_measure(self.nav.pos)
        mheading = self.sensors.imu_heading(self.nav.heading)
        mspeed = self.sensors.imu_speed(self.nav.speed)
        return PoseState(
            position=Position(x=round(mpos[0], 3), y=round(mpos[1], 3), floor=self.map.floor),
            heading=round(mheading, 2),
            speed=round(mspeed, 3),
            walking_status=self.nav.status,
            confidence=round(self.sensors.localization_confidence(), 3),
            timestamp=now or datetime.now(),
            source=self.SOURCE,
        )

    def measure_depth(self, distance: float) -> float | None:
        """深度/LiDAR 测量：加噪 + 可能丢帧。真机上这里应直接返回入参。"""
        return self.sensors.lidar_measure(distance)

    def scan_wall_ahead(self) -> float | None:
        return self.sensors.wall_ahead_scan(self.nav.pos, self.nav.heading)

    def scan_sides(self) -> tuple[float | None, float | None]:
        return self.sensors.side_scan(self.nav.pos, self.nav.heading)

    # =================================================================
    # (乙) 候选枚举 —— 纯读取，不消耗随机数
    # =================================================================
    def perceive(self) -> RawPerception:
        """枚举"可能的障碍与语义物体"，不做任何测量、不下任何结论。

        候选的组装顺序必须是：**先动态障碍、后阻挡型静态物体**。
        这既是 v0.2 的顺序，也是融合层逐障碍调 `measure_depth()` 的顺序；
        顺序决定了噪声怎么分配，必须保持稳定。
        """
        candidates: list[dict[str, Any]] = []
        for ob in self.obstacles.obstacles:
            candidates.append(
                {
                    "type": ob.type,
                    "x": ob.x,
                    "y": ob.y,
                    "radius": ob.radius,
                    "is_dynamic": True,
                    "oid": ob.oid,
                    "speed": ob.speed,
                }
            )
        # 阻挡型静态物体（桌子/椅子/区域围挡）也进避障；门、路人只进语义场景，不进这里。
        for idx, mo in enumerate(self.map.objects):
            if not mo.blocking:
                continue
            candidates.append(
                {
                    "type": mo.type,
                    "x": mo.x,
                    "y": mo.y,
                    "radius": mo.radius,
                    "hx": mo.hx,
                    "hy": mo.hy,
                    "is_dynamic": False,
                    "oid": STATIC_OID_BASE + idx,
                    "speed": 0.0,
                }
            )

        sem = [
            MapObjectInfo(
                type=o.type, x=o.x, y=o.y, radius=o.radius,
                blocking=o.blocking, hx=o.hx, hy=o.hy,
            )
            for o in self.map.objects
        ]
        return RawPerception(
            pose_reference=(self.nav.pos[0], self.nav.pos[1]),
            heading_reference=self.nav.heading,
            depth_range=self.sensors.lidar_range,
            obstacle_candidates=candidates,
            semantic_candidates=sem,
            obstacle_ids=[int(c["oid"]) for c in candidates],
            timestamp=datetime.now(),
            source=self.SOURCE,
        )

    # =================================================================
    # (丙) 静态知识与质量
    # =================================================================
    def map_snapshot(self) -> MapSnapshot:
        m = self.map
        return MapSnapshot(
            name=m.name,
            floor=m.floor,
            bounds={"x_min": m.x_min, "x_max": m.x_max, "y_min": m.y_min, "y_max": m.y_max},
            grid={"nx": m.nx, "ny": m.ny, "res": m.res},
            start=dict(m.start),
            destination=m.destination,
            route=[str(p["name"]) for p in m.landmarks],
            zones=[dict(z) for z in m.zones],
            landmarks=[dict(p) for p in m.landmarks],
            objects=[
                MapObjectInfo(
                    type=o.type, x=o.x, y=o.y, radius=o.radius,
                    blocking=o.blocking, hx=o.hx, hy=o.hy,
                )
                for o in m.objects
            ],
        )

    def zone_at(self, x: float, y: float) -> str | None:
        return self.map.zone_at(x, y)

    def floor(self) -> int:
        return int(self.map.floor)

    def read_navigation(self, now: datetime | None = None) -> NavigationState:
        """规划器输出 → 归一化导航状态。

        `route_confidence` 留默认值，由融合层回填（见 `SensorProvider.read_navigation`）。
        """
        return NavigationState(
            destination=self.nav.destination,
            current_route=self.nav.current_route(),
            next_instruction=self.nav.next_instruction(),
            distance_to_goal=round(self.nav.remaining_distance(), 2),
            route_progress=round(self.nav.route_progress(), 4),
            current_landmark=str(self.nav.target_landmark["name"]) if not self.nav.arrived else None,
            off_route=self.nav.off_route,
            replan_count=self.nav.replan_count,
            timestamp=now or datetime.now(),
            source="planner",
        )

    def confidence(self) -> tuple[float, float]:
        return (
            self.sensors.localization_confidence(),
            self.sensors.perception_confidence(),
        )

    def health(self) -> dict[SensorKind, SensorHealth]:
        """各通道健康度。事件引擎据此判断 UWB 失效 / 摄像头断流。"""
        loc, perc = self.confidence()
        drop = self.sensors.drop_rate()
        # 丢帧率超过 2% 视为"能读但质量下降"（配置默认 drop_frame_prob=0.02）
        depth_status = SensorStatus.OK if drop <= 0.02 else SensorStatus.DEGRADED

        cam = SensorHealth(kind=SensorKind.CAMERA, status=SensorStatus.ABSENT, detail="未接入摄像头")
        if self.camera is not None:
            try:
                snap = self.camera.snapshot() or {}
            except Exception:  # noqa: BLE001 - 摄像头坏了不该拖垮健康度查询
                snap = {}
            age = snap.get("age_s")
            if snap.get("image_available"):
                cam = SensorHealth(
                    kind=SensorKind.CAMERA,
                    status=SensorStatus.OK,
                    confidence=1.0,
                    age_s=float(age) if age is not None else 0.0,
                    detail="推流中",
                )
            elif snap.get("frame_id", 0):
                # 收到过帧但现在拿不到 —— "有但断了"，与"没有这个硬件"必须区分
                cam = SensorHealth(
                    kind=SensorKind.CAMERA,
                    status=SensorStatus.LOST,
                    confidence=0.0,
                    age_s=float(age) if age is not None else None,
                    detail=f"推流中断（最后帧已 {age:.1f}s 前）" if age is not None else "推流中断",
                )

        return {
            SensorKind.UWB: SensorHealth(
                kind=SensorKind.UWB, status=SensorStatus.OK, confidence=round(loc, 3),
                age_s=0.0, detail=f"噪声 {self.sensors.uwb_noise:.2f}m",
            ),
            SensorKind.IMU: SensorHealth(
                kind=SensorKind.IMU, status=SensorStatus.OK,
                age_s=0.0, detail=f"朝向噪声 {self.sensors.heading_noise:.1f}°",
            ),
            SensorKind.LIDAR: SensorHealth(
                kind=SensorKind.LIDAR, status=depth_status, confidence=round(perc, 3),
                age_s=0.0, detail=f"丢帧率 {drop * 100:.1f}%",
            ),
            SensorKind.MAP: SensorHealth(
                kind=SensorKind.MAP, status=SensorStatus.OK, confidence=1.0, age_s=None,
                detail=f"{self.map.name} / {self.map.floor}F",
            ),
            SensorKind.PLANNER: SensorHealth(
                kind=SensorKind.PLANNER,
                status=SensorStatus.OK if self.nav.path_found else SensorStatus.DEGRADED,
                confidence=1.0,
                age_s=0.0,
                detail="有可行路线" if self.nav.path_found else "当前无可行路线",
            ),
            SensorKind.CAMERA: cam,
        }

    def sensor_stats(self) -> dict[str, Any]:
        """传感器侧统计。

        ⚠️ 不要改名成 `stats()`：本类同时实现 `WorldStepper`，而它也有 `stats()`
        （世界运行统计：到达/进度/障碍数…）。同名会让世界统计被顶掉，
        `SpatialAgentSystem.stats()` 会直接 `KeyError: 'arrived'`（实测踩到）。
        """
        return self.sensors.stats()

    # =================================================================
    # 参考读数（继承自 SimulatedEnvironment，此处显式声明以强调契约归属）
    # =================================================================
    def world_reference(self) -> WorldReference:  # noqa: D102 - 见基类
        return super().world_reference()


__all__ = ["STATIC_OID_BASE", "SimulatedProvider"]
