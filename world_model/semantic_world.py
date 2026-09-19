"""语义世界（Semantic World）—— 长期语义知识 × 实时语义观察的合并视图。

两类语义知识必须分开对待（这是 v0.2 混在一起的地方）：

    (甲) 长期知识：来自预建地图，坐标是世界系的，不随时间变化
         "实验室区域东侧有一张桌子" —— 一个小时前为真，现在仍为真。
    (乙) 实时观察：来自多模态模型/检测器，是**自我中心**的、会过期
         "我右前方 1.2 米是门" —— 只在那一刻为真。

    本模块把两者合并成一句可读的"我现在所处的环境是什么样"，
    并按**证据来源**给不同置信度（地图 0.9+，模型观察 0.6~0.8），
    避免模型把"自己刚猜的"当成"地图上写着的"。

★为什么不做物体级融合（把观察贴到地图物体上）★
    那属于 SLAM / 长期建图，任务书第三十一节明确划到 v0.4 之后。
    v0.3 只保证两件事：**分类清楚**、**过期能丢**。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any

from maps.semantic_map import SemanticMap
from sensors.base import MapSnapshot
from spatial.spatial_state import SceneObject, SemanticScene


class SemanticWorld:
    """长期语义知识 + 近期语义观察。"""

    def __init__(self, semantic_map: SemanticMap | None = None, observation_ttl_s: float = 30.0) -> None:
        self.semantic_map = semantic_map or SemanticMap()
        self.observation_ttl_s = float(observation_ttl_s)
        # (时间戳, 物体) —— 观察带时间，才能过期
        self.observations: deque[tuple[float, SceneObject]] = deque(maxlen=200)

    # -----------------------------------------------------------------
    def load_map(self, snapshot: MapSnapshot) -> None:
        """装载长期知识（地图刷新时调用）。观察缓存**保留**。"""
        self.semantic_map.load(snapshot.objects)

    def observe_scene(self, scene: SemanticScene, t: float) -> int:
        """登记一轮实时语义观察，返回本轮新增观察条数。"""
        added = 0
        for obj in scene.objects:
            # 地图来源的物体已经进了长期知识，不重复记进观察缓存
            if obj.source == "map":
                continue
            self.observations.append((t, obj))
            added += 1
        self.prune(t)
        return added

    def prune(self, t: float) -> int:
        """丢掉过期的观察（"刚才看到的那把椅子"不该在 5 分钟后还被引用）。"""
        before = len(self.observations)
        self.observations = deque(
            ((ts, o) for ts, o in self.observations if t - ts <= self.observation_ttl_s),
            maxlen=self.observations.maxlen,
        )
        return before - len(self.observations)

    # -----------------------------------------------------------------
    def recent_observations(self, n: int = 5) -> list[SceneObject]:
        return [o for _, o in list(self.observations)[-n:]]

    def known_objects(self) -> list[dict[str, Any]]:
        """长期知识里的物体（给 `query_room()` / `query_nearby_poi()` 用）。"""
        return [
            {
                "type": o.type,
                "x": round(o.x, 2),
                "y": round(o.y, 2),
                "blocking": o.blocking,
                "source": "map",
            }
            for o in self.semantic_map.objects
        ]

    def objects_near(self, x: float, y: float, radius: float = 5.0) -> list[dict[str, Any]]:
        return [
            {
                "type": o.type,
                "x": round(o.x, 2),
                "y": round(o.y, 2),
                "distance_m": round(math.hypot(o.x - x, o.y - y), 2),
                "blocking": o.blocking,
                "source": "map",
            }
            for o in self.semantic_map.near(x, y, radius)
        ]

    def describe(self, x: float, y: float) -> str:
        """一句话：地图知识与最近观察的合并描述。"""
        parts: list[str] = []
        parts.append(self.semantic_map.describe(x, y, radius=6.0))
        obs = self.recent_observations(3)
        if obs:
            parts.append("刚看到：" + "、".join(f"{o.type}" for o in obs))
        return "；".join(p for p in parts if p)

    def summary(self) -> str:
        counts = self.semantic_map.counts()
        if not counts:
            return "地图上没有已知物体"
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        return "地图物体：" + "、".join(f"{k}×{v}" for k, v in top)

    def stats(self) -> dict[str, Any]:
        return {
            "map_objects": len(self.semantic_map.objects),
            "observations": len(self.observations),
            "ttl_s": self.observation_ttl_s,
        }


__all__ = ["SemanticWorld"]
