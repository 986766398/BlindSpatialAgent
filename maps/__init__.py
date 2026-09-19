"""地图接口层（Maps）—— 把"空间知识从哪来"固定成一个边界。

分层位置（任务书第二节 / 第十一节）：

    预建地图 / UE5 / BIM / 点云 / NavMesh
                  ↓
        **maps/MapProvider**   ← 四张地图的统一入口
                  ↓
    world_model/（长期知识 + 短期记忆）→ fusion → agent

四张地图各答一个问题：

    metric_map.py        这里在哪、能不能走          （几何）
    semantic_map.py      这里有什么                  （类别）
    navigation_graph.py  路点与路段拓扑、怎么走        （拓扑）
    affordance_map.py    哪里会出问题（运行期学习）    （经验）

★三条不变量★
1. 本包**不依赖** `simulator/`：数据只能通过 `SensorSnapshot`（`sensors.base.MapSnapshot`）进来。
   这样将来换成 UE5 数字孪生，只需要它产出同样结构的快照。
2. 本包**不做**三维地图、不做 SLAM、不做点云配准（任务书第三十一节明确禁止）。
3. 「经验」与「几何」分开存：`AffordanceMap` 是运行期学习的结果，
   刷新地图时**不清空**（见 `MapProvider.refresh()`）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AffordanceAnnotation",
    "AffordanceKind",
    "AffordanceMap",
    "MapProvider",
    "MetricMap",
    "NavigationGraph",
    "SemanticMap",
]

_LAZY: dict[str, str] = {
    "AffordanceAnnotation": "maps.affordance_map",
    "AffordanceKind": "maps.affordance_map",
    "AffordanceMap": "maps.affordance_map",
    "MapProvider": "maps.map_provider",
    "MetricMap": "maps.metric_map",
    "NavigationGraph": "maps.navigation_graph",
    "SemanticMap": "maps.semantic_map",
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 导入机制
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)
