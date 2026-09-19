"""事件检测器（Event Detector）—— 把状态流变成事件流。

核心方法论：**状态迁移 + 边沿检测**。

    v0.2 的问题不是"没有信息"，而是"把状态当日志"：
    前方有椅子这件事每秒都被重新报告一次。

    本检测器为每类事件维护一个**上一帧状态**，只在状态发生**迁移**时发声：
        absent  → present   ⇒ OBSTACLE_APPEARED（一次）
        present → present   ⇒ 什么都不发（它还在那儿，世界模型的活）
        present → absent    ⇒ OBSTACLE_CLEARED（一次）
        present → closing   ⇒ OBSTACLE_APPROACHING（趋势事件）

    配合 EventBus 的冷却（第二道闸），"同一把椅子"最多产生一次
    APPEARED + 一次 APPROACHING + 一次 CLEARED。

输入（每轮一次 `detect()`）：
    state    融合后的 SpatialState（当前帧）
    world    世界模型（对象持续性、短期趋势 —— Stage 5 的产出终于有了消费者）

输出：
    本轮产生的事件列表（已按 priority 排序，调用方取第一个或全要均可）

刻意**不做**的事：
    - 不做决策（Safety/Cognitive 的事）
    - 不播报（Interaction Policy 的事）
    - 不修改 SpatialState（检测是只读的）
"""

from __future__ import annotations

import logging
import math
from typing import Any

from events.event_types import AgentEvent, EventType, Severity
from spatial.spatial_state import FrameFreshness, RiskLevel, SpatialState
from world_model.world_model import WorldModel

LOG = logging.getLogger("bsa.events")


class EventDetector:
    """从状态流检测事件（有状态：保存各类事件的上一帧判定）。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        world: WorldModel,
        *,
        critical_distance_m: float | None = None,
        high_distance_m: float | None = None,
        turn_distance_m: float = 3.0,
    ) -> None:
        self.cfg = cfg
        self.world = world
        risk = cfg["agent"]["risk"]
        self.critical_d = float(critical_distance_m or risk["critical_distance_m"])
        self.high_d = float(high_distance_m or risk["high_distance_m"])
        self.turn_distance_m = float(turn_distance_m)
        self.min_loc_conf = float(cfg["agent"]["confidence"]["min_localization"])
        self.min_perc_conf = 0.5  # 感知置信度阈值（低于即认为感知不可信）

        # --- 上一帧状态（状态迁移检测的记忆） ---
        self._seen_objects: dict[str, float] = {}     # 对象名 → 首次见到的时间
        self._camera_was: FrameFreshness = FrameFreshness.NONE
        self._risk_level_was: RiskLevel = RiskLevel.LOW
        self._off_route_was: bool = False
        self._goal_reached: bool = False
        self._loc_conf_ok: bool = True
        self._perc_conf_ok: bool = True
        self._front_clear_was: bool = True
        self._turn_announced: str = ""
        self._scene_key_was: str = ""
        # 首帧基线：边界检测需要"上一帧"才能判定"出现"。
        # 系统启动时视野里本来就有的物体（含地图存量）不算"新出现"，
        # 否则开机瞬间会把它们全刷成 OBSTACLE_APPEARED。
        # 这不是"冷却"也不是"魔法 tick 数"，就是一次基线采集。
        self._baselined: bool = False
        self.emitted: int = 0

    # -----------------------------------------------------------------
    # 主入口
    # -----------------------------------------------------------------
    def detect(self, state: SpatialState, t: float) -> list[AgentEvent]:
        """检测一轮。返回本轮新产生的事件（时间序）。"""
        out: list[AgentEvent] = []
        out += self._detect_obstacles(state, t)
        out += self._detect_risk(state, t)
        out += self._detect_route(state, t)
        out += self._detect_confidence(state, t)
        out += self._detect_camera(state, t)
        out += self._detect_scene(state, t)
        self.emitted += len(out)
        self._baselined = True  # 首帧之后才具备"状态迁移"的参照系
        return out

    # -----------------------------------------------------------------
    # 各检测器（全部只读 state / world，不修改任何东西）
    # -----------------------------------------------------------------
    def _mk(
        self,
        etype: EventType,
        t: float,
        state: SpatialState,
        *,
        severity: Severity | None = None,
        confidence: float = 1.0,
        payload: dict[str, Any] | None = None,
        source: str = "detector",
    ) -> AgentEvent:
        from events.event_types import DEFAULT_SEVERITY

        return AgentEvent(
            event_type=etype,
            timestamp=t,
            severity=severity or DEFAULT_SEVERITY[etype],
            source=source,
            payload=payload or {},
            confidence=confidence,
            tick=state.tick,
        )

    # --- 障碍：出现 / 消失 / 接近 ------------------------------------
    def _detect_obstacles(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        # 当前帧视野里的对象名集合（世界模型里距离 5m 内的）
        near = self.world.objects.near(
            state.user.position.x, state.user.position.y, radius=6.0
        )
        current: dict[str, dict[str, Any]] = {}
        for m in near:
            current[m.name] = {
                "type": m.type,
                "x": round(m.x, 2),
                "y": round(m.y, 2),
                "seen": m.seen_count,
                "dynamic": m.is_dynamic,
            }

        # 出现：present 且之前 absent
        for name, info in current.items():
            if name not in self._seen_objects:
                if self._baselined:
                    # 首帧已过 ⇒ 这是真正的"新出现"（含走近后才进入视野的物体）
                    sev = Severity.WARNING if info["dynamic"] else Severity.NOTICE
                    d = self._distance_to(state, info["x"], info["y"])
                    if d is not None and d <= self.critical_d:
                        sev = Severity.CRITICAL
                    out.append(
                        self._mk(
                            EventType.OBSTACLE_APPEARED,
                            t,
                            state,
                            severity=sev,
                            confidence=0.9,
                            payload={
                                "subject": name,
                                "type": info["type"],
                                "distance_m": round(d, 2) if d is not None else None,
                                "dynamic": info["dynamic"],
                                "description": (
                                    f"{'动态' if info['dynamic'] else '静态'}{info['type']}出现"
                                    + (f"（约 {d:.1f} 米）" if d is not None else "")
                                ),
                            },
                        )
                    )
                # 首帧只登记、不发声（建立基线）
                self._seen_objects[name] = t

        # 消失：之前 present，现在不在视野里（且已被世界模型淘汰或超出半径）
        for name in list(self._seen_objects):
            if name not in current:
                first = self._seen_objects.pop(name)
                persisted = t - first
                if persisted > 1.0:  # 一闪而过的（<1s）不值得报
                    out.append(
                        self._mk(
                            EventType.OBSTACLE_CLEARED,
                            t,
                            state,
                            confidence=0.8,
                            payload={
                                "subject": name,
                                "existed_s": round(persisted, 1),
                                "description": f"障碍 {name} 已离开视野（存在了 {persisted:.0f} 秒）",
                            },
                        )
                    )

        # 接近：动态障碍距离在缩短（世界模型的 tracks 有趋势）
        for oid in self.world.approaching_obstacles():
            tr = self.world.tracks.get(oid)
            if tr is None:
                continue
            d = tr.distances[-1] if tr.distances else None
            if d is None or d > self.high_d * 3:
                continue
            out.append(
                self._mk(
                    EventType.OBSTACLE_APPROACHING,
                    t,
                    state,
                    severity=Severity.WARNING if d > self.high_d else Severity.CRITICAL,
                    confidence=0.85,
                    payload={
                        "subject": f"oid{oid}",
                        "distance_m": round(d, 2),
                        "description": f"动态障碍正在接近（当前约 {d:.1f} 米）",
                    },
                )
            )

        # 前方通畅性迁移：clear → blocked（配合地图判定冲突）
        front_clear = state.environment.front_clear
        if self._front_clear_was and not front_clear:
            out.append(
                self._mk(
                    EventType.ROUTE_BLOCKED,
                    t,
                    state,
                    confidence=0.9,
                    payload={
                        "subject": "front",
                        "distance_m": round(state.environment.front_distance, 2),
                        "description": f"前方变为不可通行（{state.environment.front_distance:.1f} 米）",
                    },
                )
            )
        self._front_clear_was = front_clear
        return out

    # --- 风险等级迁移 -----------------------------------------------
    def _detect_risk(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        level = state.risk.level
        was = self._risk_level_was
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        if order.index(level) > order.index(was):
            sev = Severity.CRITICAL if level in (RiskLevel.CRITICAL,) else Severity.WARNING
            out.append(
                self._mk(
                    EventType.HIGH_RISK,
                    t,
                    state,
                    severity=sev,
                    confidence=state.risk.confidence,
                    payload={
                        "subject": "risk",
                        "level": level.value,
                        "reason": state.risk.reason,
                        "description": f"风险升高至 {level.value}：{state.risk.reason}",
                    },
                )
            )
        self._risk_level_was = level
        return out

    # --- 路线：偏离 / 到达 / 转弯 -----------------------------------
    def _detect_route(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        nav = state.navigation

        if nav.off_route and not self._off_route_was:
            out.append(
                self._mk(
                    EventType.ROUTE_DEVIATION,
                    t,
                    state,
                    confidence=0.85,
                    payload={
                        "subject": "route",
                        "description": "已偏离规划路线",
                        "route_confidence": round(nav.route_confidence, 2),
                    },
                )
            )
        self._off_route_was = nav.off_route

        if (not self._goal_reached) and (
            state.user.walking_status.value == "arrived" or nav.route_progress >= 0.999
        ):
            self._goal_reached = True
            out.append(
                self._mk(
                    EventType.GOAL_REACHED,
                    t,
                    state,
                    payload={
                        "subject": "goal",
                        "destination": nav.destination,
                        "description": f"已到达 {nav.destination}",
                    },
                )
            )

        # 转弯预告：下一个路点方向与当前朝向偏差大，且距离在预告阈值内
        if self.world.maps is not None:
            nxt = self.world.maps.graph.next_node(nav.current_landmark or "")
            if nxt is not None:
                dx, dy = nxt.x - state.user.position.x, nxt.y - state.user.position.y
                dist = (dx * dx + dy * dy) ** 0.5
                if dist <= self.turn_distance_m and nxt.name and nxt.name != self._turn_announced:
                    import math

                    bearing = math.degrees(math.atan2(dx, dy)) % 360.0
                    delta = (bearing - state.user.heading + 180.0) % 360.0 - 180.0
                    if abs(delta) >= 45.0:
                        self._turn_announced = nxt.name
                        direction = "左" if delta < 0 else "右"
                        out.append(
                            self._mk(
                                EventType.TURN_APPROACHING,
                                t,
                                state,
                                confidence=0.8,
                                payload={
                                    "subject": f"turn:{nxt.name}",
                                    "next_node": nxt.name,
                                    "turn_direction": direction,
                                    "distance_m": round(dist, 1),
                                    "description": f"接近 {nxt.name}，需向{direction}转",
                                },
                            )
                        )
        return out

    # --- 置信度 ------------------------------------------------------
    def _detect_confidence(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        loc = state.confidence.localization_confidence
        if self._loc_conf_ok and loc < self.min_loc_conf:
            self._loc_conf_ok = False
            out.append(
                self._mk(
                    EventType.LOW_LOCALIZATION_CONFIDENCE,
                    t,
                    state,
                    confidence=0.9,
                    payload={
                        "subject": "localization",
                        "value": round(loc, 2),
                        "reason": state.confidence.uncertainty_reason,
                        "description": f"定位置信度低（{loc:.2f}）：{state.confidence.uncertainty_reason}",
                    },
                )
            )
        elif not self._loc_conf_ok and loc >= self.min_loc_conf + 0.1:
            self._loc_conf_ok = True  # 恢复（滞回 0.1 防止在阈值附近抖动）

        perc = state.confidence.perception_confidence
        if self._perc_conf_ok and perc < self.min_perc_conf:
            self._perc_conf_ok = False
            out.append(
                self._mk(
                    EventType.LOW_PERCEPTION_CONFIDENCE,
                    t,
                    state,
                    payload={
                        "subject": "perception",
                        "value": round(perc, 2),
                        "description": f"感知置信度低（{perc:.2f}）",
                    },
                )
            )
        elif not self._perc_conf_ok and perc >= self.min_perc_conf + 0.1:
            self._perc_conf_ok = True
        return out

    # --- 摄像头断流 / 恢复 -------------------------------------------
    def _detect_camera(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        cur = state.camera.freshness
        if cur is FrameFreshness.STALE and self._camera_was is FrameFreshness.FRESH:
            out.append(
                self._mk(
                    EventType.CAMERA_LOST,
                    t,
                    state,
                    confidence=1.0,
                    payload={
                        "subject": "camera",
                        "last_age_s": state.camera.age_s,
                        "description": "摄像头推流中断（曾正常，现过期）",
                    },
                )
            )
        elif cur is FrameFreshness.FRESH and self._camera_was is not FrameFreshness.FRESH:
            out.append(
                self._mk(
                    EventType.CAMERA_RECOVERED,
                    t,
                    state,
                    payload={"subject": "camera", "description": "摄像头恢复推流"},
                )
            )
        self._camera_was = cur
        return out

    # --- 场景变化 & 地图-传感器冲突 ---------------------------------
    def _detect_scene(self, state: SpatialState, t: float) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        # 场景键：视野内物体类型的多重集（变了即"场景变了"）
        key = ",".join(sorted(o.type.value for o in state.environment.obstacles))
        if self._scene_key_was and key != self._scene_key_was:
            out.append(
                self._mk(
                    EventType.SCENE_CHANGED,
                    t,
                    state,
                    confidence=0.7,
                    payload={
                        "subject": "scene",
                        "was": self._scene_key_was,
                        "now": key,
                        "description": "视野内障碍构成发生变化",
                    },
                )
            )
        self._scene_key_was = key

        # 地图-传感器冲突：地图说可走，传感器说过不去（任务书第二十节场景七）
        if self.world.maps is not None and not state.environment.front_clear:
            # ★"前方"必须沿当前朝向探，不能写死 +x★
            #   本事件的 subject 就是 "front"，而 +x 只是"东"。用户朝北走时，
            #   原来的写法实际在问"我右边能不能走" —— 南北向走廊里会漏报
            #   （南走廊那条线上 x+0.5 恒为可走，冲突永远命中；换到西/东走廊
            #   又恒为不可走，冲突永远不命中）。地图扩成办公楼后两种朝向都有，
            #   这个方向错误才暴露出来。
            rad = math.radians(float(getattr(state.user, "heading", 0.0) or 0.0))
            walkable = self.world.maps.metric.is_walkable(
                state.user.position.x + math.sin(rad) * 0.5,
                state.user.position.y + math.cos(rad) * 0.5,
            )
            if walkable:
                out.append(
                    self._mk(
                        EventType.MAP_SENSOR_CONFLICT,
                        t,
                        state,
                        confidence=0.75,
                        payload={
                            "subject": "front",
                            "map_says": "walkable",
                            "sensor_says": "blocked",
                            "distance_m": round(state.environment.front_distance, 2),
                            "description": (
                                "地图标记可通行，但传感器报告受阻 —— 以实时感知为准"
                            ),
                        },
                    )
                )
        return out

    # -----------------------------------------------------------------
    # 辅助
    # -----------------------------------------------------------------
    @staticmethod
    def _distance_to(state: SpatialState, x: float, y: float) -> float | None:
        pos = state.user.position
        return ((x - pos.x) ** 2 + (y - pos.y) ** 2) ** 0.5

    def reset(self) -> None:
        """清空迁移记忆（系统重置时调用）。"""
        self._seen_objects.clear()
        self._camera_was = FrameFreshness.NONE
        self._risk_level_was = RiskLevel.LOW
        self._off_route_was = False
        self._goal_reached = False
        self._loc_conf_ok = True
        self._perc_conf_ok = True
        self._front_clear_was = True
        self._turn_announced = ""
        self._scene_key_was = ""
        self._baselined = False
        self.emitted = 0

    def stats(self) -> dict[str, Any]:
        return {"emitted": self.emitted, "tracked_objects": len(self._seen_objects)}


__all__ = ["EventDetector"]
