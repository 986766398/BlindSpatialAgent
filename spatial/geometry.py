"""纯几何工具：方位角与夹角。

为什么单独抽一个模块（v0.3 Stage 3 的副产品之一）：

    这两个函数原本住在 `simulator/navigation_simulator.py` 里。融合层
    （`spatial/state_manager.py`）为了算一个夹角，不得不写
    `from simulator.navigation_simulator import angle_delta, bearing_deg`
    —— 融合层就此认识了自己本不该认识的模拟器。

    夹角是纯数学，与"数据从哪来"毫无关系，所以搬到本模块。
    `navigation_simulator` 仍然 re-export 这两个名字，旧调用点不会破。

坐标约定（全项目统一）：x 向东、y 向北；heading 0°=+y（正北），顺时针为正。
"""

from __future__ import annotations

import math


def bearing_deg(from_xy: tuple[float, float], to_xy: tuple[float, float]) -> float:
    """返回从 from 指向 to 的方位角（0°=北/+y，顺时针），范围 [0,360)。"""
    dx = to_xy[0] - from_xy[0]
    dy = to_xy[1] - from_xy[1]
    return math.degrees(math.atan2(dx, dy)) % 360.0


def angle_delta(target: float, current: float) -> float:
    """返回 target 相对 current 的有符号夹角，范围 (-180, 180]。正数=向右转。"""
    return (target - current + 180.0) % 360.0 - 180.0


__all__ = ["angle_delta", "bearing_deg"]
