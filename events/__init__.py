"""事件系统（Event Engine）—— 状态流到事件流的一整段。

    事件 = 状态变化的边沿。状态本身由 SpatialState 表达；
    事件回答"刚刚**发生了什么**"。

    event_engine.py    一体化入口（process/subscribe/reset）
    event_types.py     16 类事件词表 + 严重度 + 冷却
    event_detector.py  状态迁移检测（出现/消失/接近/偏离/断流/冲突…）
    event_bus.py       发布订阅 + 冷却（critical 豁免）
    event_history.py   按轮次可查询存档

★去重三道闸（防止"同一把椅子报 100 次"）★
    ① 检测器的状态迁移：还在那儿就不算新闻；
    ② 总线的冷却：同 key 短期不重复（danger 豁免 —— 宁可重复报警）；
    ③ key 含主体：椅子A与椅子B分开算。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentEvent",
    "EventBus",
    "EventDetector",
    "EventEngine",
    "EventHistory",
    "EventType",
    "Severity",
]

_LAZY: dict[str, str] = {
    "AgentEvent": "events.event_types",
    "EventType": "events.event_types",
    "Severity": "events.event_types",
    "EventBus": "events.event_bus",
    "EventDetector": "events.event_detector",
    "EventHistory": "events.event_history",
    "EventEngine": "events.event_engine",
}


def __getattr__(name: str) -> Any:  # pragma: no cover
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:  # pragma: no cover
    return sorted(__all__)
