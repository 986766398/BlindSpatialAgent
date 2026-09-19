"""世界模型（World Model）—— Agent 的持续空间记忆。

真实系统里，大模型是无状态的；能跨轮次保持空间连贯性的东西就是这个模块。

★v0.3 起按"时间尺度"分成三层（任务书第七节）★

    ① Current State        当前这一刻            → `SpatialState`（由 fusion 产出）
    ② Short-Term History   最近 5~30 秒          → `spatial_memory.SpatialMemory`
                                                   `trajectory_memory.TrajectoryMemory`
                                                   `object_memory.ObjectMemoryStore`
    ③ Persistent Knowledge 预建地图提供的长期信息 → `semantic_world.SemanticWorld`
                                                   `maps/MapProvider`

    另外还有一层**经验**：`affordance_world.AffordanceWorld` ——
    "这个位置以前也堵过"这类由运行期学习得到、且会衰减的知识。

★不是数据库垃圾桶（任务书第二十三节）★
    - 短期记忆：时间窗口 + maxlen 双闸淘汰；
    - 对象记忆：`evict()` 按 TTL 淘汰，并同步清理 oid 索引与趋势；
    - 可行动性经验：`prune()` 按 TTL 淘汰；
    - 长期知识：只在 `bind_map()` 时装载。
    落盘的实验数据属于 `recording/`，**与记忆刻意分开**。

★兼容性★
    本类是 v0.2 `spatial/world_model.py` 的门面：`trail` / `trail_times` /
    `obstacle_memory` / `tracks` / `visited_zones` / `obstacle_encounters` /
    `spin_warning` / `replans` 等属性与 `observe_*` / `summarize()` / `stats()`
    等方法全部保留原语义，因此 v0.2 的调用点与断言零改动。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from maps.affordance_map import AffordanceMap
from maps.map_provider import MapProvider
from sensors.base import MapSnapshot
from spatial.spatial_state import SpatialState
from world_model.affordance_world import AffordanceWorld
from world_model.object_memory import ObjectMemoryStore, ObstacleMemory, TrackStats
from world_model.semantic_world import SemanticWorld
from world_model.spatial_memory import SpatialMemory
from world_model.trajectory_memory import TrajectoryMemory


class WorldModel:
    """持续空间记忆：轨迹 / 对象 / 短期状态 / 语义知识 / 可行动性经验。"""

    def __init__(
        self,
        max_events: int = 50,
        trail_len: int = 120,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg or {}
        wm_cfg = (self.cfg.get("world_model") or {}) if isinstance(self.cfg, dict) else {}

        # --- 短期记忆 ---
        self.trajectory = TrajectoryMemory(trail_len=trail_len)
        self.objects = ObjectMemoryStore(evict_after_s=float(wm_cfg.get("object_ttl_s", 60.0)))
        self.spatial = SpatialMemory(
            window_s=float(wm_cfg.get("short_term_window_s", 30.0)),
            max_items=int(wm_cfg.get("short_term_max", 300)),
        )
        # --- 长期知识与经验 ---
        self.semantic = SemanticWorld(
            observation_ttl_s=float(wm_cfg.get("semantic_observation_ttl_s", 30.0))
        )
        self.affordance = AffordanceWorld(AffordanceMap())
        self.maps: MapProvider | None = None
        # --- 事件 ---
        self.events: deque[tuple[float, str]] = deque(maxlen=max_events)
        self.replan_total: int = 0

    # =================================================================
    # 兼容属性（v0.2 直接访问这些字段）
    # =================================================================
    @property
    def trail(self) -> deque[tuple[float, float]]:
        return self.trajectory.trail

    @property
    def trail_times(self) -> deque[float]:
        return self.trajectory.trail_times

    @property
    def obstacle_memory(self) -> dict[tuple[int, int], ObstacleMemory]:
        return self.objects.memories

    @property
    def tracks(self) -> dict[int, TrackStats]:
        return self.objects.tracks

    @property
    def obstacle_encounters(self) -> dict[str, int]:
        return self.objects.encounters

    @property
    def visited_zones(self) -> list[str]:
        return self.trajectory.visited_zones

    @property
    def spin_warning(self) -> bool:
        return self.trajectory.spin_warning

    # =================================================================
    # 长期知识装载
    # =================================================================
    def bind_map(self, snapshot: MapSnapshot) -> MapProvider:
        """装载预建地图（长期空间知识）。可行动性**经验保留**。"""
        self.maps = MapProvider(snapshot, affordance=self.affordance.map)
        self.semantic.load_map(snapshot)
        return self.maps

    def map_snapshot(self) -> MapSnapshot | None:
        return self.maps.snapshot if self.maps is not None else None

    # =================================================================
    # 写入
    # =================================================================
    def observe_pose(self, x: float, y: float, t: float, zone: str | None) -> None:
        """记录一次位姿观测。"""
        if self.trajectory.add(x, y, t, zone):
            self.add_event(t, f"进入{zone}")

    def observe_obstacles(self, obstacles: list[dict[str, Any]], t: float) -> None:
        """记录当前视野内的障碍（含动态障碍的接近趋势与持续性）。"""
        for name in self.objects.observe(obstacles, t):
            otype = name.rsplit("_", 1)[0]
            self.add_event(t, f"发现{otype}")

    def observe_state(self, state: SpatialState) -> None:
        """登记一整帧空间状态（v0.3 新增）。

        与 `observe_pose` / `observe_obstacles` 的关系：那两者是 v0.2 的"零件级"
        写入（融合层按老口径调用，保证行为不变）；本方法登记**融合后**的完整状态，
        供短期记忆（趋势）、语义世界（观察缓存）与可行动性世界（经验）使用。
        """
        t = state.timestamp.timestamp()
        self.spatial.observe(state)
        self.semantic.observe_scene(state.semantic_scene, t)
        self.affordance.observe(state, t, objects=self.objects)

    def note_replan(self, t: float, reason: str = "") -> None:
        self.replan_total += 1
        self.add_event(t, f"重规划路线{('：' + reason) if reason else ''}")

    def add_event(self, t: float, text: str) -> None:
        self.events.append((t, text))

    # =================================================================
    # 派生信息
    # =================================================================
    def recent_events(self, n: int = 5) -> list[str]:
        return [e[1] for e in list(self.events)[-n:]]

    def obstacles_near(self, x: float, y: float, radius: float = 3.0) -> list[ObstacleMemory]:
        return self.objects.near(x, y, radius)

    def approaching_obstacles(self) -> list[int]:
        return self.objects.approaching()

    def distance_travelled(self) -> float:
        return self.trajectory.distance_travelled()

    def affordance_notes(self, state: SpatialState) -> list[str]:
        """行动注解（几何结论 + 历史佐证）。

        刻意不改 `SpatialState.affordance` —— 见 `affordance_world.py`
        顶部关于"几何结论 vs 历史佐证"的分工说明。
        """
        return self.affordance.notes(state, objects=self.objects, short_term=self.spatial)

    def area_context(self, x: float, y: float) -> str:
        """地图视角的区域描述（长期知识 + 经验）。"""
        if self.maps is None:
            return ""
        return self.maps.area_description(x, y)

    def summarize(self) -> str:
        """一段简短的场景记忆摘要，直接喂给大模型。"""
        parts: list[str] = []
        if self.visited_zones:
            parts.append("已走过：" + " → ".join(self.visited_zones[-4:]))
        if self.obstacle_encounters:
            top = sorted(self.obstacle_encounters.items(), key=lambda kv: -kv[1])[:3]
            parts.append("遇到障碍：" + "、".join(f"{k}×{v}" for k, v in top))
        if self.replan_total:
            parts.append(f"已重规划 {self.replan_total} 次")
        if self.spin_warning:
            parts.append("检测到原地打转")
        if self.recent_events(3):
            parts.append("近期：" + "；".join(self.recent_events(3)))
        return "；".join(parts) if parts else "尚无历史"

    def clear_runtime(self) -> None:
        """清空运行期记忆（等价于 v0.2 的 `__init__` 重置），保留地图绑定。"""
        self.trajectory.reset()
        self.objects.clear()
        self.spatial.clear()
        self.semantic.observations.clear()
        self.affordance.map.annotations.clear()
        self.events.clear()
        self.replan_total = 0

    def stats(self) -> dict[str, Any]:
        """统计（v0.2 的键全部保留；v0.3 新增四层记忆的分项统计）。"""
        return {
            "trail_points": len(self.trail),
            "distance_travelled_m": round(self.distance_travelled(), 2),
            "obstacle_memories": len(self.obstacle_memory),
            "visited_zones": self.visited_zones,
            "replan_total": self.replan_total,
            "spin_warning": self.spin_warning,
            "events": len(self.events),
            # --- v0.3 新增分层 ---
            "layers": {
                "short_term": self.spatial.stats(),
                "objects": self.objects.stats(),
                "semantic": self.semantic.stats(),
                "affordance": self.affordance.stats(),
                "map": None if self.maps is None else self.maps.stats(),
            },
        }


__all__ = ["ObstacleMemory", "TrackStats", "WorldModel"]
