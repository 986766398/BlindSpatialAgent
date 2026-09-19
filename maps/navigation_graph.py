"""导航图（Navigation Graph）—— 路点与路段的长期拓扑。

职责边界：
    只回答"通往目的地的路点顺序是什么、从当前路点出发还剩哪些、离我最近的是哪个"。
    **不做**寻路（寻路是规划器的事，见 `simulator/` 或未来的 NavMesh 服务）。

为什么要有这一层：
    v0.2 里"剩余路点 / 当前路点 / 路线名称"散落在 `NavigationSimulator` 内部，
    前端与工具各自猜字段名。把拓扑单独抽出来之后，
    接 UE5 NavMesh / 真实室内地图时，只要它们能产出同样结构的节点与邻接关系，
    上层工具（`query_navigation_graph()`）与提示词渲染一行不改。
"""

from __future__ import annotations

import math
from typing import Any

from spatial.spatial_state import RoutePoint


class NavigationGraph:
    """路点序列 + 邻接关系。"""

    def __init__(self, landmarks: list[dict[str, Any]] | None = None) -> None:
        self.nodes: list[RoutePoint] = []
        self.edges: list[tuple[int, int]] = []
        self.load(landmarks or [])

    # -----------------------------------------------------------------
    def load(self, landmarks: list[dict[str, Any]]) -> None:
        """从路点表重建拓扑。

        串联式路点（模拟器用的就是这种）直接按顺序连边；
        未来接真实 NavMesh 时把 `edges` 换成显式的邻接矩阵即可，
        本类的查询接口不变。
        """
        self.nodes = [
            RoutePoint(x=float(p["x"]), y=float(p["y"]), name=str(p.get("name", "")))
            for p in landmarks
        ]
        self.edges = [(i, i + 1) for i in range(max(0, len(self.nodes) - 1))]

    # -----------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return [str(n.name) for n in self.nodes]

    def nearest(self, x: float, y: float) -> tuple[str, float]:
        """离给定坐标最近的路点名与距离。"""
        if not self.nodes:
            return "", float("inf")
        best = min(self.nodes, key=lambda n: math.hypot(n.x - x, n.y - y))
        return str(best.name), math.hypot(best.x - x, best.y - y)

    def index_of(self, name: str) -> int:
        for i, n in enumerate(self.nodes):
            if n.name == name:
                return i
        return -1

    def remaining_from(self, name: str) -> list[str]:
        """从某个路点起，后面还剩哪些路点（含自身）。"""
        i = self.index_of(name)
        if i < 0:
            return self.names
        return [str(n.name) for n in self.nodes[i:]]

    def remaining_points(self, name: str) -> list[RoutePoint]:
        i = self.index_of(name)
        return list(self.nodes[max(0, i):])

    def total_length(self) -> float:
        total = 0.0
        for i, j in self.edges:
            a, b = self.nodes[i], self.nodes[j]
            total += math.hypot(b.x - a.x, b.y - a.y)
        return total

    def segment_length(self, i: int) -> float:
        """第 i 段路段的长度。"""
        if i < 0 or i + 1 >= len(self.nodes):
            return 0.0
        a, b = self.nodes[i], self.nodes[i + 1]
        return math.hypot(b.x - a.x, b.y - a.y)

    def next_node(self, name: str) -> RoutePoint | None:
        i = self.index_of(name)
        if i < 0 or i + 1 >= len(self.nodes):
            return None
        return self.nodes[i + 1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [{"name": n.name, "x": round(n.x, 2), "y": round(n.y, 2)} for n in self.nodes],
            "edges": [list(e) for e in self.edges],
            "total_length_m": round(self.total_length(), 2),
        }

    def stats(self) -> dict[str, Any]:
        return {"nodes": len(self.nodes), "edges": len(self.edges)}


__all__ = ["NavigationGraph"]
