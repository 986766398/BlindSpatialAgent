"""状态融合层：多源观测 → 一个 SpatialState。

这是整个架构里最关键的一层。它的存在保证了：
    - Agent 不认识 UWB，也不认识 LiDAR，只认识 SpatialState；
    - 未来换成真实硬件，只要替换 `SensorProvider` 的实现，Agent 完全不动。

融合内容：
    UWB 定位 + IMU 姿态                  -> pose（v0.2 中的 user）
    NavMesh 路线 + 重规划                 -> navigation
    静态地图 + LiDAR 扇形扫描 + 动态障碍   -> environment
    地图上的语义物体                      -> semantic_scene
    摄像头帧状态                          -> camera
    距离 / 通道宽度 / 路线状态             -> risk（保留字段，干预决策见 agent/safety）
    噪声水平 + 实测残差 + 新鲜度           -> uncertainty（五维 + 相机 + 原因）
    环境几何 + 障碍分布                    -> affordance（v0.3 新增）

★本层只依赖 `SensorProvider` 协议★
    文件里不出现任何 `from simulator import ...`。它拿到的是
    `sensors.base.SensorProvider`，由装配处（`agent/agent_core.py` 或 `agent/orchestrator.py`）
    注入 `sensors.simulated.SimulatedProvider`。换 UE5 / 真机只换装配处那一行。

★两条容易被改坏的铁律★

1. **对 provider 的调用顺序 = 随机数消耗顺序**：
       read_pose → perceive → scan_wall_ahead → 逐障碍 measure_depth → scan_sides
   顺序一变，同一 seed 下噪声序列错位，行为回归测试立刻红。

2. **纯函数算子与组装分开**：
   新鲜度判定在 `fusion/freshness.py`，置信度估计在 `fusion/confidence.py`，
   本文件只负责"按什么顺序把它们串成一个状态"。这样每个算子都能单独测。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fusion.confidence import ConfidenceEstimator
from fusion.freshness import FreshnessPolicy
from sensors.base import CameraProvider, RawPerception, SensorProvider, shape_distance
from spatial.geometry import angle_delta, bearing_deg
from spatial.spatial_state import (
    AffordanceState,
    BlockedRegion,
    CameraState,
    EnvironmentState,
    FrameFreshness,
    NavigationState,
    Obstacle,
    ObstacleDirection,
    ObstacleKind,
    PoseState,
    RiskLevel,
    RiskState,
    SceneObject,
    SemanticScene,
    SpatialState,
    UncertaintyState,
    WalkingStatus,
)
from spatial.world_model import WorldModel

# 方位角映射：|delta| <= FRONT_HALF 视为正前方
FRONT_HALF = 45.0
SIDE_LIMIT = 135.0

# 语义物体只报告这个距离内的（8 米约等于"盲人听得清方位"的有效范围）
SEMANTIC_RANGE_M = 8.0
# 环境状态里最多保留这么多个障碍（按距离从近到远）
MAX_OBSTACLES = 8
# 前方被挡住的判定：front_distance 大于该值才算"通畅"
# （真实取值来自 config.simulator.obstacles.clear_range_m，此处仅作类型默认）


def _direction_of(delta: float) -> ObstacleDirection:
    """把相对夹角（正=右侧）映射成方位枚举。"""
    if abs(delta) <= FRONT_HALF:
        return ObstacleDirection.FRONT
    if FRONT_HALF < delta <= SIDE_LIMIT:
        return ObstacleDirection.FRONT_RIGHT
    if -SIDE_LIMIT <= delta < -FRONT_HALF:
        return ObstacleDirection.FRONT_LEFT
    return ObstacleDirection.BEHIND


def _kind_of(name: str) -> ObstacleKind:
    try:
        return ObstacleKind(name)
    except ValueError:
        return ObstacleKind.UNKNOWN


class StateFusion:
    """多源融合 -> SpatialState（v0.3 Stage 4 从 StateManager 中拆出）。

    ★只接收 `SensorProvider`，不认识任何模拟器★
        模拟器细节全部封在 `sensors/simulated/provider.py` 里。
        换 UE5 数字孪生或真机时，本类一行不改。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        provider: SensorProvider,
        world: WorldModel | None = None,
        camera: CameraProvider | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.world = world or WorldModel()
        self.camera = camera

        risk_cfg = cfg["agent"]["risk"]
        self.critical_d = float(risk_cfg["critical_distance_m"])
        self.high_d = float(risk_cfg["high_distance_m"])
        self.medium_d = float(risk_cfg["medium_distance_m"])
        self.narrow_w = float(risk_cfg["narrow_corridor_m"])
        self.clear_range = float(cfg["simulator"]["obstacles"]["clear_range_m"])
        self.side_obstacle_range = float(cfg["simulator"]["sensors"].get("side_obstacle_range_m", 1.0))
        self.min_loc_conf = float(cfg["agent"]["confidence"]["min_localization"])

        # --- Stage 4 新增：新鲜度策略 + 置信度估计 ---
        self.freshness = FreshnessPolicy(cfg)
        self.estimator = ConfidenceEstimator(cfg)

        self.tick: int = 0
        self.history: list[SpatialState] = []
        self.last_state: SpatialState | None = None
        # None = 还没跑过一轮。这样"没 build 就问前置距离"会走回退分支，
        # 而不是悄悄返回一个 0.0（那会让下游以为前方贴着障碍）。
        self._last_front_distance: float | None = None
        self._last_obstacle_ids: list[int] = []
        self._last_dt: float = 1.0

    # -----------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------
    def build(self, t_now: float, elapsed: float, wall_clock: datetime | None = None,
              dt: float | None = None) -> SpatialState:
        """构造当前时刻的 SpatialState。

        ★对 provider 的调用顺序 = 随机数消耗顺序，不要调整★
            read_pose → perceive → (环境融合: scan_wall_ahead → 逐障碍
            measure_depth → scan_sides) → read_navigation → 语义 → 摄像头
        这与 v0.2 逐行一致；顺序一变，同 seed 下噪声序列错位、行为就不再可复现。
        """
        self.tick += 1
        now = wall_clock or datetime.now()
        step_dt = self._last_dt if dt is None else float(dt)
        self._last_dt = step_dt

        # ---------- 1. 位姿状态（UWB + IMU，带噪声）----------
        pose = self.provider.read_pose(now)
        pose_fresh = self.freshness.evaluate("pose", pose.timestamp, now)
        self.estimator.observe_pose(pose, step_dt)

        # ---------- 2. 原始候选（纯读取，无噪声）----------
        raw = self.provider.perceive()
        ref_pos = raw.pose_reference
        ref_heading = raw.heading_reference

        # ---------- 3. 环境状态（静态地图 + LiDAR + 动态障碍）----------
        env, front_distance = self._build_environment(raw, now)
        self._last_front_distance = front_distance

        # ---------- 4. 导航状态 ----------
        # route_confidence 是"这条路还可不可信"的估计，属于融合层的判断，
        # 所以先取规划器输出，再由本层回填。
        navigation = self.provider.read_navigation(now)
        nav_fresh = self.freshness.evaluate("navigation", navigation.timestamp, now)
        navigation = navigation.model_copy(
            update={"route_confidence": self.estimator.route(navigation)}
        )

        # ---------- 5. 语义场景 ----------
        semantic = self._build_semantic(raw, env, now)

        # ---------- 6. 摄像头 ----------
        camera_state = self._camera_state(now)

        # ---------- 7. 位姿回填：把估计出来的置信度写回位姿 ----------
        # 分工说明：provider 给"读数"，本层给"这个读数有多可信"。
        # 定位真的退化时，这里会明显低于 0.9，从而让"低置信度 → 询问用户"
        # 这条分支从死代码变成活代码（P0-7）。
        loc_conf = self.estimator.localization(
            fresh=pose_fresh.usable, pose_valid=pose.valid
        )
        pose = pose.model_copy(
            update={
                "confidence": loc_conf,
                "source": "fused",
                "age_ms": None if pose_fresh.age_s is None else round(pose_fresh.age_s * 1000.0, 1),
                "valid": pose.valid and pose_fresh.usable,
            }
        )

        # ---------- 8. 风险 ----------
        risk = self._evaluate_risk(env, navigation, pose, now)

        # ---------- 9. 不确定性（五维 + 相机维度 + 原因）----------
        _prior_loc, prior_perc = self.provider.confidence()
        uncertainty = UncertaintyState(
            localization_confidence=loc_conf,
            perception_confidence=self.estimator.perception(
                prior_perc, drop_rate=self._drop_rate(), fresh=env.valid
            ),
            semantic_confidence=self.estimator.semantic(semantic),
            route_confidence=navigation.route_confidence,
            camera_freshness=self.estimator.camera(camera_state.freshness),
            uncertainty_reason=self._uncertainty_reason(
                pose_fresh.usable, nav_fresh.usable, camera_state.freshness
            ),
            # overall_confidence 留空，由校验器按加权均值补全
        )

        # ---------- 10. 可行动性（"能不能走、往哪走"）----------
        affordance = self._build_affordance(env)

        state = SpatialState(
            timestamp=now,
            tick=self.tick,
            user=pose,
            navigation=navigation,
            environment=env,
            semantic_scene=semantic,
            camera=camera_state,
            risk=risk,
            confidence=uncertainty,
            affordance=affordance,
        )

        # ---------- 11. 写入世界模型 ----------
        self.world.observe_pose(ref_pos[0], ref_pos[1], elapsed, self.provider.zone_at(*ref_pos))
        self.world.observe_obstacles(
            [
                {
                    "type": o.type.value,
                    "x": o.x,
                    "y": o.y,
                    "radius": o.radius,
                    "distance": o.distance,
                    "oid": oid,
                    "is_dynamic": o.is_dynamic,
                }
                for oid, o in zip(self._last_obstacle_ids, env.obstacles)
            ],
            elapsed,
        )
        # ---------- 12. 登记整帧状态（短期记忆 / 语义观察 / 可行动性经验） ----------
        # v0.3 Stage 5：世界模型按时间尺度分层。上面两个 observe_* 保持 v0.2
        # 的"零件级"口径（轨迹 + 障碍），这里补上融合后的整帧 —— 供
        # SpatialMemory（趋势）/ SemanticWorld（观察缓存）/ AffordanceWorld（经验）使用。
        # 纯记账，不消耗随机数，不改变任何决策输入。
        self.world.observe_state(state)

        self.last_state = state
        self.history.append(state)
        if len(self.history) > 60:
            self.history.pop(0)
        return state

    # -----------------------------------------------------------------
    # 环境融合
    # -----------------------------------------------------------------
    def _build_environment(
        self, raw: RawPerception, now: datetime
    ) -> tuple[EnvironmentState, float]:
        """把"候选 + 一次扫描"融合成环境事实。

        ★三次测量的先后顺序（墙 → 逐障碍 → 两侧）不可改动★
            每次测量都会消耗 provider 的随机数，顺序决定噪声分配。
        """
        pos = raw.pose_reference
        heading = raw.heading_reference
        max_r = raw.depth_range

        # (a) 静态地图：正前方窄视场测墙（走廊有没有走到头）
        wall_front = self.provider.scan_wall_ahead()

        # (b) 会真实挡路的物体：动态障碍 + 阻挡型静态物体（桌子/椅子）。
        #     非阻挡型语义物体（门、路人）只进 semantic_scene，不参与避障。
        candidates = raw.obstacle_candidates

        obstacle_list: list[Obstacle] = []
        obstacle_ids: list[int] = []
        front_min = max_r if wall_front is None else wall_front
        left_min = max_r
        right_min = max_r

        for c in candidates:
            # ★区域型障碍不是"圆心距 - 半径"★：矩形必须按点到矩形边算，
            #   否则 6m×1m 的围挡会被当成一个点，用户贴着它走时系统还报"距离 3 米"。
            d = shape_distance(
                pos[0], pos[1], c["x"], c["y"],
                float(c["radius"]), float(c.get("hx", 0.0)), float(c.get("hy", 0.0)),
            )
            if d > max_r:
                continue
            d = max(0.0, d)
            delta = angle_delta(bearing_deg(pos, (c["x"], c["y"])), heading)
            direction = _direction_of(delta)
            measured = self.provider.measure_depth(d)
            if measured is None:
                continue  # 丢帧：这一帧看不到它（真实系统里就是这样）
            obstacle_list.append(
                Obstacle(
                    type=_kind_of(c["type"]),
                    distance=round(measured, 3),
                    direction=direction,
                    x=round(c["x"], 2),
                    y=round(c["y"], 2),
                    radius=float(c["radius"]),
                    is_dynamic=bool(c["is_dynamic"]),
                    speed=round(float(c["speed"]), 2),
                )
            )
            obstacle_ids.append(int(c["oid"]))
            if direction == ObstacleDirection.FRONT:
                front_min = min(front_min, measured)
            elif direction == ObstacleDirection.FRONT_LEFT:
                left_min = min(left_min, measured)
            elif direction == ObstacleDirection.FRONT_RIGHT:
                right_min = min(right_min, measured)

        order = sorted(range(len(obstacle_list)), key=lambda k: obstacle_list[k].distance)[:MAX_OBSTACLES]
        obstacle_list = [obstacle_list[k] for k in order]
        obstacle_ids = [obstacle_ids[k] for k in order]
        self._last_obstacle_ids = obstacle_ids

        # 前方清空判定
        front_clear = front_min > self.clear_range

        # (c) 左右侧距离：墙体侧向扫描与侧向障碍取较小值
        stat_left, stat_right = self.provider.scan_sides()
        left_dist = min(left_min, stat_left if stat_left is not None else max_r)
        right_dist = min(right_min, stat_right if stat_right is not None else max_r)
        corridor_width = left_dist + right_dist

        perc_conf = round(self.provider.confidence()[1], 3)
        env = EnvironmentState(
            front_clear=front_clear,
            front_distance=round(front_min, 3),
            left_obstacle=left_dist < self.side_obstacle_range,
            right_obstacle=right_dist < self.side_obstacle_range,
            left_distance=round(left_dist, 3),
            right_distance=round(right_dist, 3),
            corridor_width=round(corridor_width, 3),
            obstacles=obstacle_list,
            narrow_passage=corridor_width < self.narrow_w,
            # --- v0.3 新增 ---
            # walkable_width 与 corridor_width 同源，前者是给"可行动性"用的语义化命名
            walkable_width=round(corridor_width, 3),
            blocking_ratio=round(self._blocking_ratio(pos, heading, obstacle_list), 3),
            # 台阶 / 坠落：当前模拟地图里没有这两种地形，先如实置 False。
            # 接深度相机 / 真实 LiDAR 后由 `fusion.freshness` + 深度算子判定。
            stairs=False,
            dropoff=False,
            timestamp=now,
            source="simulated",
            confidence=perc_conf,
            valid=True,
            age_ms=0.0,
        )
        return env, front_min

    def _blocking_ratio(
        self, pos: tuple[float, float], heading: float, obstacles: list[Obstacle]
    ) -> float:
        """前方视场内被障碍"占掉"的角度比例，0~1。

        算法：对每个障碍，用它到用户的距离与近似半径算出**角半宽**
        `atan2(radius, distance)`；只统计落在前方视场（±FRONT_HALF）内的部分，
        累加后除以视场角度。

        为什么用角度而不是个数：1 米外一把椅子和 4 米外一把椅子对"前方有多堵"
        的贡献完全不同，按个数算会失真。角度占比能直接表达"视野被挡了多少"。
        """
        fov = 2.0 * FRONT_HALF  # 前方视场总角度
        covered = 0.0
        for ob in obstacles:
            if ob.x is None or ob.y is None:
                continue
            rng = math.dist(pos, (ob.x, ob.y))
            if rng <= 1e-6:
                continue
            half_angle = math.degrees(math.atan2(max(ob.radius, 1e-3), rng))
            delta = abs(angle_delta(bearing_deg(pos, (ob.x, ob.y)), heading))
            # 障碍角窗与前方视场的重叠长度
            overlap = max(0.0, min(delta + half_angle, FRONT_HALF) - max(delta - half_angle, -FRONT_HALF))
            covered += overlap
        return max(0.0, min(1.0, covered / fov))

    # -----------------------------------------------------------------
    # 语义场景
    # -----------------------------------------------------------------
    def _build_semantic(
        self, raw: RawPerception, env: EnvironmentState, now: datetime
    ) -> SemanticScene:
        """静态语义物体 + 动态障碍 → 一句话场景描述。

        语义物体来自地图（`semantic_candidates`），动态障碍来自已融合的
        `env.obstacles`。两者置信度不同：地图是"确定的"，检测器是"估计的"。
        """
        pos = raw.pose_reference
        heading = raw.heading_reference
        objects: list[SceneObject] = []
        seen: set[str] = set()
        for mo in raw.semantic_candidates:
            d = mo.distance_to(pos[0], pos[1])
            if d > SEMANTIC_RANGE_M:
                continue
            delta = angle_delta(bearing_deg(pos, (mo.x, mo.y)), heading)
            objects.append(
                SceneObject(
                    type=mo.type,
                    label=mo.type,
                    description=f"{mo.type}，距{d:.1f}米",
                    distance=round(max(0.0, d), 2),
                    direction=_direction_of(delta),
                    confidence=0.95 if mo.blocking else 0.8,
                    timestamp=now,
                    source="map",
                )
            )
            seen.add(mo.type)
        for ob in env.obstacles:
            if ob.is_dynamic:
                objects.append(
                    SceneObject(
                        type=ob.type.value,
                        label=ob.type.value,
                        description=f"动态{ob.type.value}，距{ob.distance:.1f}米",
                        distance=ob.distance,
                        direction=ob.direction,
                        confidence=0.7,
                        timestamp=now,
                        source="detector",
                    )
                )
                seen.add(ob.type.value)

        near = sorted(
            [o for o in objects if o.distance is not None], key=lambda o: o.distance or 0.0
        )[:3]
        summary = (
            "附近：" + "、".join(f"{o.type}({o.distance:.1f}米)" for o in near)
            if near
            else "近处没有明显物体"
        )
        kept = objects[:10]
        # 整体语义置信度 = 已识别物体的平均置信度；一个都没看到时不算"不确定"，
        # 而是"确定地什么都没有"，给 1.0 会掩盖问题，给 0 会误报低置信，折中给 0.9。
        sem_conf = round(sum(o.confidence for o in kept) / len(kept), 3) if kept else 0.9
        return SemanticScene(
            objects=kept,
            summary=summary,
            confidence=sem_conf,
            timestamp=now,
            source="map",
        )

    # -----------------------------------------------------------------
    # 摄像头
    # -----------------------------------------------------------------
    def _camera_state(self, now: datetime) -> CameraState:
        """构造摄像头帧元数据。

        `freshness` 三态是关键语义，解决了 v0.2 的一个盲点：
        过去只用 `image_available` 一个布尔量，导致「从来没推过流」和
        「推过、但断了」在状态里长得一模一样。而这两种情况对 Agent 的意义
        完全不同 —— 后者应该降级并考虑告知用户"我看不见了"。
        """
        if self.camera is None:
            return CameraState(image_available=False, source="none", freshness=FrameFreshness.NONE)
        snap = self.camera.snapshot()
        available = bool(snap.get("image_available", False))
        frame_id = int(snap.get("frame_id", 0))
        source = snap.get("source", "none")

        freshness = self.freshness.camera_state(snap, now)

        return CameraState(
            image_available=available,
            timestamp=snap.get("timestamp") or now,
            age_s=snap.get("age_s"),
            source=source,
            frame_id=frame_id,
            freshness=freshness,
            # 拍摄朝向：iPhone 目前不上报，留 None；接入带姿态的设备后由 Adapter 填充
            heading=None,
        )

    # -----------------------------------------------------------------
    # 风险
    # -----------------------------------------------------------------
    def _evaluate_risk(
        self, env: EnvironmentState, nav: NavigationState, pose: PoseState, now: datetime
    ) -> RiskState:
        """风险判定（**状态字段**，非干预决策）。

        ⚠️ 分工说明（v0.3 Stage 7 起明确）：
            本函数产出的是 `SpatialState.risk` —— "当前处境有多危险"的**状态描述**，
            供展示与提示词使用；
            真正的**干预决策**（要不要立刻叫停、发什么 SafetyEvent）在
            `agent/safety/safety_engine.py`，那里才是"安全层"。
            两者共用 `config.agent.risk.*` 的同一套阈值，口径一致。
        """

        def _make(level: RiskLevel, reason: str) -> RiskState:
            # 风险判定的置信度取"环境感知置信度"：几何算得再准，
            # 传感器丢帧严重时我们对"什么在那"本身就没把握。
            return RiskState(
                level=level,
                reason=reason,
                confidence=round(env.confidence, 3),
                source="fusion",
                timestamp=now,
            )

        # 0) 高危地形优先：台阶与坠落的风险与距离阈值无关，一律按 HIGH 起步。
        #    这条同时是 SpatialState 自洽性校验的前置条件（stairs/dropoff 置位时
        #    risk 不得低于 high），保证"感知到了却没当回事"不可能发生。
        if env.dropoff:
            return _make(RiskLevel.HIGH, "前方存在坠落风险")
        if env.stairs:
            return _make(RiskLevel.HIGH, "前方存在台阶")

        d = env.front_distance
        if d <= self.critical_d:
            return _make(RiskLevel.CRITICAL, f"正前方 {d:.2f} 米有障碍，即将碰撞")
        if d <= self.high_d:
            return _make(RiskLevel.HIGH, f"正前方 {d:.2f} 米有障碍")
        if env.narrow_passage:
            w = env.corridor_width or 0.0
            return _make(RiskLevel.MEDIUM, f"通道变窄，可通行宽度约 {w:.2f} 米")
        if d <= self.medium_d:
            return _make(RiskLevel.MEDIUM, f"前方 {d:.2f} 米有障碍")
        if env.left_obstacle and env.right_obstacle:
            return _make(RiskLevel.MEDIUM, "左右两侧均有障碍，需保持在中间")
        if nav.off_route:
            return _make(RiskLevel.MEDIUM, "已偏离规划路线")
        if pose.walking_status == WalkingStatus.BLOCKED:
            return _make(RiskLevel.HIGH, "用户被阻挡，无法前进")
        return _make(RiskLevel.LOW, "前方通畅")

    # -----------------------------------------------------------------
    # 可行动性
    # -----------------------------------------------------------------
    def _build_affordance(self, env: EnvironmentState) -> AffordanceState:
        """把"环境事实"翻译成"能不能行动、往哪行动"。

        与 `_evaluate_risk` 的分工：
            risk       回答"危险吗"（供 Safety 决定要不要马上叫停）
            affordance 回答"能走吗、往哪走"（供决策与表达使用）

        本层只做**几何层**的可行动性（由距离与通道宽度直接推导）。
        `world_model/affordance_world.py` 会把它升级为结合语义物体、
        历史记忆与用户意图的推理版本。
        """
        front_blocked = not env.front_clear
        left_free = env.left_distance or 0.0
        right_free = env.right_distance or 0.0

        blocked: list[BlockedRegion] = []
        for ob in env.obstacles:
            if ob.direction == ObstacleDirection.FRONT:
                blocked.append(
                    BlockedRegion(
                        direction=ob.direction,
                        distance=ob.distance,
                        source_type=ob.type.value,
                        severity=self._severity_of(ob.distance),
                        preferred_direction=self._bypass_side(left_free, right_free),
                    )
                )
        # 通道过窄也构成"强行通过有风险"，但不算完全阻挡 —— 按可通行宽度判定
        if env.walkable_width is not None and env.walkable_width < self.narrow_w:
            blocked.append(
                BlockedRegion(
                    direction=ObstacleDirection.FRONT,
                    distance=env.front_distance,
                    source_type="narrow_passage",
                    severity=RiskLevel.MEDIUM,
                    preferred_direction=self._bypass_side(left_free, right_free),
                )
            )

        # 前方被**墙**挡住时，obstacles 列表里没有对应条目 —— 墙不是"障碍物"，
        # 它来自窄视场墙体扫描。不补这一条，summary 会退化成光秃秃的
        # "正前方不可通行"，用户既不知道多远、也不知道被什么挡住（实跑踩到）。
        if front_blocked and not any(r.direction == ObstacleDirection.FRONT for r in blocked):
            blocked.append(
                BlockedRegion(
                    direction=ObstacleDirection.FRONT,
                    distance=env.front_distance,
                    source_type="wall",
                    severity=self._severity_of(env.front_distance),
                    preferred_direction=self._bypass_side(left_free, right_free),
                )
            )

        can_forward = front_blocked is False
        # 前方通畅但通道窄到过不去时，也不算"可以直行"
        if env.walkable_width is not None and env.walkable_width <= 0.0:
            can_forward = False

        preferred = self._bypass_side(left_free, right_free) if not can_forward else None

        if can_forward and not blocked:
            summary = "前方通畅，可按原方向前进"
        elif can_forward:
            summary = f"可直行，但前方 {min(r.distance for r in blocked):.1f} 米内有阻挡物"
        elif blocked:
            nearest = min(blocked, key=lambda r: r.distance)
            side = {ObstacleDirection.LEFT: "向左", ObstacleDirection.RIGHT: "向右",
                    ObstacleDirection.FRONT_LEFT: "向左前", ObstacleDirection.FRONT_RIGHT: "向右前"}
            summary = (
                f"正前方不可通行（{nearest.distance:.1f} 米内有{nearest.source_type}）"
                + (f"，建议{side.get(preferred, '绕行')}" if preferred else "，两侧也不宽裕")
            )
        else:
            summary = "正前方不可通行"

        return AffordanceState(
            can_move_forward=can_forward,
            passable_width_m=env.walkable_width,
            blocked_regions=blocked[:6],
            preferred_direction=preferred,
            summary=summary,
        )

    def _severity_of(self, distance: float) -> RiskLevel:
        """按距离给阻挡定严重程度（复用与风险判定同一套阈值，口径一致）。"""
        if distance <= self.critical_d:
            return RiskLevel.CRITICAL
        if distance <= self.high_d:
            return RiskLevel.HIGH
        if distance <= self.medium_d:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _bypass_side(left_free: float, right_free: float) -> ObstacleDirection | None:
        """按左右两侧剩余空间选一个更好的绕行方向。两侧都很窄时返回 None。

        0.5 米是保守经验值：低于它，盲杖摆动空间已经不够安全地侧移。
        """
        margin = 0.5
        left_ok = left_free >= margin
        right_ok = right_free >= margin
        if left_ok and right_ok:
            return ObstacleDirection.LEFT if left_free >= right_free else ObstacleDirection.RIGHT
        if left_ok:
            return ObstacleDirection.LEFT
        if right_ok:
            return ObstacleDirection.RIGHT
        return None

    # -----------------------------------------------------------------
    # 辅助
    # -----------------------------------------------------------------
    def _drop_rate(self) -> float:
        """丢帧率。provider 可能没实现这个可选接口，取不到就当作 0。"""
        try:
            stats = self.provider.sensor_stats()
        except Exception:  # noqa: BLE001 - 统计失败不该影响融合
            return 0.0
        return float(stats.get("drop_rate", 0.0) or 0.0)

    def _uncertainty_reason(
        self, pose_fresh: bool, nav_fresh: bool, camera: FrameFreshness
    ) -> str:
        """把"为什么不确定"写成一句人话，进状态与日志。

        任务书第 14 节第 6 项要求 Agent 必须能知道"不确定"，而不是永远假设数据正确；
        只有维度数值没有原因，模型与人都无从判断该怎么办。
        """
        parts: list[str] = list(self.estimator.reasons())
        if not pose_fresh:
            parts.append("UWB 读数过期")
        if not nav_fresh:
            parts.append("路线信息未更新")
        if camera is FrameFreshness.STALE:
            parts.append("摄像头推流中断")
        elif camera is FrameFreshness.NONE:
            parts.append("无摄像头数据")
        # 去重且保持顺序
        seen: set[str] = set()
        out: list[str] = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return "；".join(out)

    def reset(self) -> None:
        """清空运行期状态（tick / 历史 / 上一轮结果 / 置信度窗口）。"""
        self.tick = 0
        self.history.clear()
        self.last_state = None
        self._last_front_distance = None
        self._last_obstacle_ids = []
        self._last_dt = 1.0
        self.estimator.reset()

    def front_distance(self) -> float:
        """最近一轮融合得到的前方距离。

        还没跑过 `build()` 时退回雷达量程 —— 与 v0.2 初始化
        `_last_front_distance = sensors.lidar_range` 的语义一致，
        避免下游把"没数据"误读成"前方贴着障碍"。
        """
        if self._last_front_distance is None:
            return self.provider.perceive().depth_range
        return self._last_front_distance

    def stats(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "history": len(self.history),
            "world": self.world.stats(),
            "sensors": self.provider.sensor_stats(),
            "health": {
                k.value: v.status.value for k, v in self.provider.health().items()
            },
            "confidence": self.estimator.stats(),
        }


__all__ = ["CameraProvider", "StateFusion"]
