"""Spatial State 层：把多源传感器数据统一成结构化空间状态。

    spatial_state.py    —— Pydantic 数据模型（SpatialState 及其子结构）
    state_manager.py    —— 多源融合：SensorProvider -> SpatialState
    world_model.py      —— 世界模型：短期空间记忆（轨迹 / 障碍记忆 / 已探索区域）
    geometry.py         —— 纯几何工具（方位角 / 夹角），零依赖

注意：本包的 __init__ 刻意**不做**急切导入。
因为 `sensors.base` 需要 `spatial.spatial_state`，而 `state_manager` 又需要
`sensors.base` 的协议，急切导入会形成循环引用。这里用 PEP 562 的模块级
`__getattr__` 做惰性导出，既保持 `from spatial import StateManager` 这种写法可用，
又不会引入环。
"""

from typing import Any

_LAZY: dict[str, str] = {
    # 名称 -> 所在子模块
    "AffordanceState": "spatial.spatial_state",
    "BlockedRegion": "spatial.spatial_state",
    "CameraFrameMetadata": "spatial.spatial_state",
    "CameraState": "spatial.spatial_state",
    "ConfidenceState": "spatial.spatial_state",
    "EnvironmentState": "spatial.spatial_state",
    "FrameFreshness": "spatial.spatial_state",
    "NavigationState": "spatial.spatial_state",
    "Obstacle": "spatial.spatial_state",
    "ObstacleDirection": "spatial.spatial_state",
    "ObstacleKind": "spatial.spatial_state",
    "PoseState": "spatial.spatial_state",
    "Position": "spatial.spatial_state",
    "RiskLevel": "spatial.spatial_state",
    "RiskState": "spatial.spatial_state",
    "RoutePoint": "spatial.spatial_state",
    "SceneObject": "spatial.spatial_state",
    "SemanticScene": "spatial.spatial_state",
    "SemanticState": "spatial.spatial_state",
    "SpatialState": "spatial.spatial_state",
    "UncertaintyState": "spatial.spatial_state",
    "UserState": "spatial.spatial_state",
    "WalkingStatus": "spatial.spatial_state",
    # 摄像头协议已随 Stage 3 移到传感器适配层（`spatial` 只做融合，不再定义硬件契约）
    "CameraProvider": "sensors.base",
    "StateManager": "spatial.state_manager",
    "ObstacleMemory": "spatial.world_model",
    "TrackStats": "spatial.world_model",
    "WorldModel": "spatial.world_model",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:  # pragma: no cover - 简单转发
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module 'spatial' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)
