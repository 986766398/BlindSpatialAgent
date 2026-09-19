"""地图提供者（MapProvider）—— 上层唯一的"问地图"入口。

    ┌──────────────────────────────────────────────┐
    │               MapProvider                    │
    │  metric  语义  graph  affordance  ← 四张地图  │
    └───────────────▲──────────────────────────────┘
                    │ 一台装配，一次装载
        MapSnapshot │ （来自 SimulatedProvider / UE5 / BIM / 点云）
                    │
           预建地图服务 / 数字孪生

为什么需要这一层（任务书第十一节）：
    如果 `state_manager` / `tools` / `world_model` 各自去读地图的一个字段
    （有人读 zones、有人读 landmarks、有人读 objects），那么**换地图数据源时
    要改三处**，与 Stage 3 之前"三处各自 new 模拟器"是同一个病。
    收成一个 provider 之后，换源只改 `from_snapshot()` 这一处。

★v0.3 的边界★
    只定义接口与查询，**不做**三维地图、不做 SLAM、不做点云配准。
"""

from __future__ import annotations

from typing import Any

from maps.affordance_map import AffordanceMap
from maps.metric_map import MetricMap
from maps.navigation_graph import NavigationGraph
from maps.semantic_map import SemanticMap
from sensors.base import MapSnapshot


class MapProvider:
    """四张地图的装配与统一查询入口。

    参数 `affordance` 可外部注入：可行动性经验是**运行期学习**出来的
    （见 `world_model/affordance_world.py`），不能随地图快照一起重建，
    否则每轮刷新地图就把积累的经验清空了。
    """

    def __init__(
        self,
        snapshot: MapSnapshot,
        affordance: AffordanceMap | None = None,
    ) -> None:
        self.metric = MetricMap(snapshot)
        self.semantic = SemanticMap(snapshot.objects)
        self.graph = NavigationGraph(snapshot.landmarks)
        self.affordance = affordance if affordance is not None else AffordanceMap()
        self.snapshot = snapshot

    # -----------------------------------------------------------------
    @classmethod
    def from_provider(cls, provider: Any, affordance: AffordanceMap | None = None) -> "MapProvider":
        """从任意 `SensorProvider` 装载地图（只依赖协议，不依赖具体实现）。"""
        return cls(provider.map_snapshot(), affordance=affordance)

    def refresh(self, snapshot: MapSnapshot) -> None:
        """地图热更新（数字孪生推送新版本时调用）。经验库**保留**。"""
        self.snapshot = snapshot
        self.metric = MetricMap(snapshot)
        self.semantic.load(snapshot.objects)
        self.graph.load(snapshot.landmarks)

    # -----------------------------------------------------------------
    # 组合查询（上层最常用的几个问题）
    # -----------------------------------------------------------------
    def zone_at(self, x: float, y: float) -> str | None:
        return self.metric.zone_at(x, y)

    def is_walkable(self, x: float, y: float) -> bool:
        return self.metric.is_walkable(x, y)

    def destination(self) -> str:
        return self.metric.destination

    def area_description(self, x: float, y: float) -> str:
        """给模型用的一句区域描述：区域名 + 附近地标 + 经验提醒。

        任务书第二十四节要求提示词按需裁剪；这里把"地图视角"的三类信息
        合成一句，避免模型自己去拼 zones / objects / annotations 三份数据。
        """
        parts: list[str] = []
        zone = self.zone_at(x, y)
        if zone:
            parts.append(f"位于{zone}")
        parts.append(self.semantic.describe(x, y, radius=6.0))
        note = self.affordance.summary_near(x, y, radius=2.0)
        if note:
            parts.append(f"该处{note}")
        name, dist = self.graph.nearest(x, y)
        if name:
            parts.append(f"最近路点 {name}（{dist:.1f}米）")
        return "；".join(p for p in parts if p)

    def stats(self) -> dict[str, Any]:
        return {
            "metric": self.metric.info(),
            "semantic": self.semantic.stats(),
            "graph": self.graph.stats(),
            "affordance": self.affordance.stats(),
        }


__all__ = ["MapProvider"]
