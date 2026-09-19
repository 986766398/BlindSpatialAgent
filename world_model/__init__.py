"""空间世界模型（Spatial World Model）—— Agent 的持续空间记忆。

    世界模型 = 当前状态（fusion 给）+ 短期记忆 + 长期知识 + 运行期经验

    world_model.py        门面：统一入口 + v0.2 兼容属性
    trajectory_memory.py  我从哪来、走了多少路、是不是在打转
    object_memory.py      这把椅子是同一把，而且它一直在
    spatial_memory.py     最近 5~30 秒的数值趋势（在接近还是在远离）
    semantic_world.py     地图知道什么 × 刚看到什么
    affordance_world.py   能不能走 + 历史佐证（持续存在/长期问题/还剩几秒）

★为什么分成六个文件而不是一个大类★
    v0.2 的 `WorldModel` 一个类里同时管轨迹、障碍、事件、区域、统计。
    它的问题是**单测无从下手**：想验"原地打转判据"就得先塞进 40 个位姿点。
    拆开之后每个记忆都能独立构造与断言（见 `tests/selftest.py` 用例 52~55）。

★与 `recording/` 的区别★
    本包在内存里滚动淘汰，服务"现在该怎么走"；
    `recording/` 落盘保存整段实验，服务"事后复现与对比模型"。
    两者刻意不做成一个东西。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AffordanceWorld",
    "ObjectMemoryStore",
    "ObstacleMemory",
    "SemanticWorld",
    "SpatialMemory",
    "TrackStats",
    "TrajectoryMemory",
    "WorldModel",
]

_LAZY: dict[str, str] = {
    "AffordanceWorld": "world_model.affordance_world",
    "SemanticWorld": "world_model.semantic_world",
    "SpatialMemory": "world_model.spatial_memory",
    "TrajectoryMemory": "world_model.trajectory_memory",
    "ObjectMemoryStore": "world_model.object_memory",
    "ObstacleMemory": "world_model.object_memory",
    "TrackStats": "world_model.object_memory",
    "WorldModel": "world_model.world_model",
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 导入机制
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)
