"""阶段一/二的传感器适配实现：接到现有仿真器上。

    environment.py  SimulatedEnvironment —— 四个模拟器的持有与推进（WorldStepper）
    provider.py     SimulatedProvider    —— 归一化读数（SensorProvider）

`SimulatedProvider` 继承 `SimulatedEnvironment`，因此这一个对象同时满足
`SensorProvider` 与 `WorldStepper` 两个协议 —— 仿真里感知与推世界本就是一体两面。

接真实硬件时的替换方式：在 `sensors/future/` 下写新的 Provider，
装配处（`agent/agent_core.py` 的 `SpatialAgentSystem.__init__`）换一行即可，
融合层与 Agent 一行都不用改。
"""

from sensors.simulated.environment import SimulatedEnvironment
from sensors.simulated.provider import STATIC_OID_BASE, SimulatedProvider

__all__ = ["STATIC_OID_BASE", "SimulatedEnvironment", "SimulatedProvider"]
