"""度量地图（Metric Map）—— 空间的"坐标与可通行性"事实。

职责边界（刻意收窄）：
    本类只回答几何问题：某个坐标在哪、能不能走、属于哪个区域、有多远。
    它**不认识**语义（"这是茶水间"看 `semantic_map.py`）、不认识路径
    （看 `navigation_graph.py`）、不认识障碍历史（看 `affordance_map.py`）。

为什么要有这一层（任务书第十一节的核心诉求）：
    v0.2 的几何事实与模拟器实现是**同一个对象**（`MapSimulator` 既算栅格、
    又答区域、又被导航器直接引用）。未来换成 UE5 数字孪生 / BIM / 点云时，
    上层会被迫认识新对象。现在把"地图能回答什么"固定成接口，
    数据来源是二维栅格还是三维网格就只是**实现细节**。
"""

from __future__ import annotations

import math
from typing import Any

from sensors.base import MapSnapshot


class MetricMap:
    """空间的度量层：边界 / 栅格 / 区域 / 可通行性。

    与 `MapSnapshot` 的分工：快照是**数据**（可序列化、可缓存、可来自任意来源），
    本类是**查询接口**（给上层用）。二者一一对应，转换只在构造函数里发生。
    """

    def __init__(self, snapshot: MapSnapshot) -> None:
        self.snapshot = snapshot
        self.name: str = snapshot.name
        self.floor: int = int(snapshot.floor)
        b = snapshot.bounds
        self.x_min = float(b["x_min"])
        self.x_max = float(b["x_max"])
        self.y_min = float(b["y_min"])
        self.y_max = float(b["y_max"])
        g = snapshot.grid
        self.nx = int(g.get("nx", 0))
        self.ny = int(g.get("ny", 0))
        self.res = float(g.get("res", 0.25))
        self.zones: list[dict[str, Any]] = [dict(z) for z in snapshot.zones]
        self.start: dict[str, Any] = dict(snapshot.start)
        self.destination: str = str(snapshot.destination)

    # -----------------------------------------------------------------
    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min

    def in_bounds(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    def zone_at(self, x: float, y: float) -> str | None:
        """坐标属于哪个区域。区域是矩形并集，取第一个命中的。"""
        for z in self.zones:
            if (z["x_min"] <= x <= z["x_max"]) and (z["y_min"] <= y <= z["y_max"]):
                return str(z.get("name", "")) or None
        return None

    def is_walkable(self, x: float, y: float) -> bool:
        """是否落在可通行区域内。

        ⚠️ 这是**静态地图**的说法。地图说可通行 ≠ 现在真的能走 ——
        后者必须结合实时感知（任务书第二十节：局部感知优先于静态地图）。
        """
        if not self.in_bounds(x, y):
            return False
        return self.zone_at(x, y) is not None

    def clamp(self, x: float, y: float) -> tuple[float, float]:
        """把坐标夹到地图范围内。"""
        return (
            min(max(x, self.x_min), self.x_max),
            min(max(y, self.y_min), self.y_max),
        )

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """世界坐标 → 栅格下标。"""
        i = int((x - self.x_min) / self.res) if self.res > 0 else 0
        j = int((y - self.y_min) / self.res) if self.res > 0 else 0
        return max(0, min(i, max(self.nx - 1, 0))), max(0, min(j, max(self.ny - 1, 0)))

    def cell_to_world(self, i: int, j: int) -> tuple[float, float]:
        """栅格下标 → 世界坐标（格子中心）。"""
        return (self.x_min + (i + 0.5) * self.res, self.y_min + (j + 0.5) * self.res)

    def zones_near(self, x: float, y: float, radius: float) -> list[str]:
        """某点附近（曼哈顿意义）可能属于的区域名。"""
        out: list[str] = []
        for z in self.zones:
            cx = min(max(x, z["x_min"]), z["x_max"])
            cy = min(max(y, z["y_min"]), z["y_max"])
            if math.hypot(cx - x, cy - y) <= radius and str(z.get("name", "")):
                out.append(str(z["name"]))
        return out

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "floor": self.floor,
            "size": f"{self.width:.0f}x{self.height:.0f}m",
            "grid": f"{self.nx}x{self.ny}@{self.res}m",
            "zones": len(self.zones),
        }


__all__ = ["MetricMap"]
