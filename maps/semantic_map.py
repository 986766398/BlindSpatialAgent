"""语义地图（Semantic Map）—— 空间里"有什么东西"的长期知识。

与 `MetricMap` 的分工：
    MetricMap    回答"这里能不能走"（几何）
    SemanticMap  回答"这里有什么"（类别、位置、是否真实挡路）

与 `SemanticScene`（`spatial/spatial_state.py`）的分工：
    SemanticScene 是**这一刻**从当前位置看到的语义快照（自我中心，随移动变化）；
    SemanticMap   是**长期**知道的物体清单（世界坐标，不随用户移动）。
    前者用于"我现在旁边有什么"，后者用于"我接下来会经过什么"。

★blocking 标记不能混用★
    门、路人也是物体，但它们不构成物理阻挡；只有 `blocking=True` 的
    （桌子/椅子等）才参与避障与路径规划。把两者混在一起，
    Agent 会开始绕着路人规划路线 —— 这在视障导航里是纯粹的噪音。
"""

from __future__ import annotations

from typing import Any

from sensors.base import MapObjectInfo


class SemanticMap:
    """静态物体的长期清单（数据来源：预建地图 / UE5 / BIM / 点云）。"""

    def __init__(self, objects: list[MapObjectInfo] | None = None) -> None:
        self.objects: list[MapObjectInfo] = list(objects or [])

    def load(self, objects: list[MapObjectInfo]) -> None:
        """整表替换（地图热更新时调用）。"""
        self.objects = list(objects)

    # -----------------------------------------------------------------
    def blocking(self) -> list[MapObjectInfo]:
        """只取会真实挡路的物体（进避障与路径规划）。"""
        return [o for o in self.objects if o.blocking]

    def non_blocking_labels(self) -> list[str]:
        """非阻挡物体的类别（门、路人之类）—— 只作语义提示，不参与避障。"""
        return [o.type for o in self.objects if not o.blocking]

    def by_type(self, kind: str) -> list[MapObjectInfo]:
        return [o for o in self.objects if o.type == kind]

    def near(self, x: float, y: float, radius: float = 5.0) -> list[MapObjectInfo]:
        """按**到物体表面**的距离（近到远）返回附近的物体。

        用 `distance_to()` 而不是圆心距：区域型障碍（施工围挡等）的"中心"可能在
        3 米外、但人已经贴着它的边了，按圆心距会把这种物体漏掉。
        """
        out = [o for o in self.objects if o.distance_to(x, y) <= radius]
        out.sort(key=lambda o: o.distance_to(x, y))
        return out

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.objects:
            out[o.type] = out.get(o.type, 0) + 1
        return out

    def describe(self, x: float, y: float, radius: float = 6.0) -> str:
        """一句话描述某点附近的地标，供提示词/播报使用。"""
        near = self.near(x, y, radius)
        if not near:
            return "附近没有已知物体"
        parts = [f"{o.type}({o.distance_to(x, y):.1f}米)" for o in near[:3]]
        return "附近有：" + "、".join(parts)

    def stats(self) -> dict[str, Any]:
        return {
            "objects": len(self.objects),
            "blocking": len(self.blocking()),
            "types": self.counts(),
        }


__all__ = ["SemanticMap"]
