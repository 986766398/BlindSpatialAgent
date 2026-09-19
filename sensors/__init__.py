"""传感器适配层（Sensor Adapter Layer）。

    base.py       抽象契约：SensorProvider（只读感知）/ WorldStepper（推进世界）
                  / CameraProvider / SensorHealth / MapSnapshot / RawPerception
    simulated/    阶段一/二实现：接到现有四个模拟器上
    future/       阶段三预留：UWB / 眼镜 IMU / iPhone LiDAR / UE5 数字孪生的接口说明

分层的意义只有一句话：**换硬件不改 Agent**。
融合层（`spatial/state_manager.py`）、工具层（`agent/tools.py`）与
Agent 核心（`agent/agent_core.py`）里不允许出现 `from simulator import ...`，
只允许 import 本包。

⚠️ 本包的 `__init__` 只导出抽象契约，**不导出 `simulated` 子包**。
   原因：`simulated` 会拉起 `simulator/*`（含 numpy 栅格计算），
   而抽象契约必须零重依赖；同时这也避免"顺手从 sensors 顶层拿到模拟器"。
   需要模拟实现时请显式 `from sensors.simulated import SimulatedProvider`。
"""

from sensors.base import (
    CameraProvider,
    MapObjectInfo,
    MapSnapshot,
    RawPerception,
    SensorHealth,
    SensorKind,
    SensorProvider,
    SensorStatus,
    WorldReference,
    WorldStepper,
    shape_distance,
)

__all__ = [
    "CameraProvider",
    "MapObjectInfo",
    "MapSnapshot",
    "RawPerception",
    "SensorHealth",
    "SensorKind",
    "SensorProvider",
    "SensorStatus",
    "WorldReference",
    "WorldStepper",
    "shape_distance",
]
