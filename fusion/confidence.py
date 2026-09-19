"""置信度估计（confidence）—— 把"传感器自报的质量 + 实测残差 + 数据新鲜度"合成估计值。

★本模块修的是审计报告 P0-7★

v0.2 的 `localization_confidence` 是**配置常量**：`SensorSimulator.localization_confidence()`
直接由 `uwb_noise_m` 算出 0.9，只要不改配置文件就永远是 0.9。后果有两个：

1. `RuleDecisionEngine` 里"定位置信度低 → 询问用户"的分支是**死代码**，
   任何真实退化（UWB 漂移、基站遮挡、跳变）都不会让它触发；
2. 任务书第 14 节"低定位置信度"场景根本无法构造，回归测试无从下手。

本模块的做法（刻意不做卡尔曼/EKF，v0.3 只要架构正确）：

    q_noise : 传感器自报的噪声水平与参考值之比
    q_innov : **实测残差** —— 相邻两帧位移与"速度×dt"预期位移之差，
              除以测量噪声标准差。定位跳变/漂移会直接把这一项打下来，
              它才是"真的出问题了"的证据（噪声参数是纸面值，残差是现场值）。
    q_fresh : 数据新鲜度（过期即失效）

三者加权后乘以基准值。**权重与阈值全部可配**，且在 `config.yaml:fusion.confidence`
里能一键切回 `mode: fixed` 复现 v0.2 的常量行为（便于回归对比）。
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any

from spatial.spatial_state import (
    FrameFreshness,
    NavigationState,
    PoseState,
    SemanticScene,
)

_MISSING_CONF = 0.30  # 数据缺失时的定位置信度上限（"不知道自己在哪"应当被显式表达）


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class ConfidenceEstimator:
    """多源置信度估计器（有状态：维护一小段残差窗口）。

    为什么要有状态：单帧噪声无法与"真实移动"区分 —— 用户走路时相邻帧本来就该
    差 1 米。把"位移 - 预期位移"作为残差，静止与行走两种情形都能得到同一口径。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        noise_m: float | None = None,
        heading_noise_deg: float | None = None,
    ) -> None:
        c = ((cfg.get("fusion") or {}).get("confidence")) or {}
        self.mode: str = str(c.get("mode", "estimated"))
        loc = c.get("localization") or {}
        self.base: float = float(loc.get("base", 0.95))
        self.noise_ref_m: float = float(loc.get("noise_ref_m", 0.30))
        self.noise_span_m: float = float(loc.get("noise_span_m", 1.20))
        self.innov_ref: float = float(loc.get("innovation_ref", 2.50))
        self.w_noise: float = float(loc.get("w_noise", 0.45))
        self.w_innov: float = float(loc.get("w_innov", 0.40))
        self.w_fresh: float = float(loc.get("w_fresh", 0.15))
        self.floor: float = float(loc.get("floor", 0.05))
        self.window: int = int(loc.get("window", 12))
        self.fixed_value: float = float(loc.get("fixed_value", 0.90))

        sens = (cfg.get("simulator") or {}).get("sensors") or {}
        self.noise_m: float = float(noise_m if noise_m is not None else sens.get("uwb_noise_m", 0.15))
        self.heading_noise_deg: float = float(
            heading_noise_deg if heading_noise_deg is not None else sens.get("heading_noise_deg", 2.0)
        )

        self._innovations: deque[float] = deque(maxlen=self.window)
        self._prev_pos: tuple[float, float] | None = None
        self._prev_heading: float | None = None
        self._last_reason: list[str] = []

    # -----------------------------------------------------------------
    # 观测（每轮由融合层调用一次）
    # -----------------------------------------------------------------
    def observe_pose(self, pose: PoseState, dt: float) -> None:
        """登记一次位姿观测，更新残差窗口。

        `dt` 为距上一次观测的秒数（仿真里就是主循环周期）。
        """
        pos = (pose.position.x, pose.position.y)
        if self._prev_pos is not None and dt > 1e-6:
            expected = max(0.0, pose.speed) * dt
            step = math.dist(self._prev_pos, pos)
            self._innovations.append(abs(step - expected))
        self._prev_pos = pos
        self._prev_heading = pose.heading

    # -----------------------------------------------------------------
    # 各维度估计
    # -----------------------------------------------------------------
    @property
    def mean_innovation(self) -> float:
        return sum(self._innovations) / len(self._innovations) if self._innovations else 0.0

    def localization(self, *, fresh: bool = True, pose_valid: bool = True) -> float:
        """定位置信度估计。

        三条证据各管一段：噪声水平（纸面能力）、残差（现场表现）、新鲜度（可用性）。
        任何一条塌了都不该继续宣称"我很清楚你在哪"。
        """
        if self.mode == "fixed":
            return self.fixed_value

        q_noise = _clamp01(1.0 - (self.noise_m - self.noise_ref_m) / max(self.noise_span_m, 1e-6))
        q_innov = _clamp01(1.0 - self.mean_innovation / max(self.innov_ref * max(self.noise_m, 0.05) * math.sqrt(2.0), 1e-6))
        q_fresh = 1.0 if fresh else 0.0

        conf = self.base * (
            self.w_noise * q_noise + self.w_innov * q_innov + self.w_fresh * q_fresh
        )
        if not pose_valid:
            conf = min(conf, _MISSING_CONF)
        if not fresh:
            conf = min(conf, _MISSING_CONF)

        self._last_reason = []
        if q_noise < 0.5:
            self._last_reason.append(f"UWB 噪声偏大({self.noise_m:.2f}m)")
        if q_innov < 0.5:
            self._last_reason.append("定位残差异常（可能漂移或跳变）")
        if not fresh:
            self._last_reason.append("定位数据过期")
        if not pose_valid:
            self._last_reason.append("定位失效")
        return round(max(self.floor, min(1.0, conf)), 3)

    def perception(self, prior: float, *, drop_rate: float = 0.0, fresh: bool = True) -> float:
        """感知置信度：取传感器自报值与新鲜度的较弱者。

        `prior` 由 provider 给出（丢帧率驱动的纸面质量），本函数只补"过期"这一维度：
        数据过期时不该继续声称"我看得很清楚"。
        """
        conf = float(prior)
        if not fresh:
            conf *= 0.5
        if drop_rate > 0.0:
            conf = min(conf, _clamp01(1.0 - drop_rate * 5.0))
        return round(max(0.0, min(1.0, conf)), 3)

    def semantic(self, scene: SemanticScene, *, fresh: bool = True) -> float:
        """语义置信度：识别结果自身置信度 × 新鲜度。"""
        conf = float(scene.confidence)
        if not fresh:
            conf *= 0.6
        return round(max(0.0, min(1.0, conf)), 3)

    @staticmethod
    def route(nav: NavigationState) -> float:
        """路线可信度：越偏离、重规划越多，越不可信。

        ⚠️ 刻意不用**累计**重规划次数直接扣分：第一版就是拿它直接扣，
        跑几十轮后永远钉在 0.1 地板、再也不回升，等于给了一条
        "一旦重规划过就永久不信这条路"的假信号（实跑踩到）。
        现在按"当前是否偏离 + 次数上限封顶"，指标能随路况好转而回升。
        """
        conf = 1.0 - min(int(nav.replan_count), 3) * 0.10
        if nav.off_route:
            conf -= 0.35
        return round(max(0.25, min(1.0, conf)), 3)

    @staticmethod
    def camera(freshness: FrameFreshness) -> float:
        """相机新鲜度分数（新增维度）。

        与 `CameraState.freshness` 的差别：那里是三态语义，这里是可参与
        加权与阈值判断的数值。二者同源，不会互相矛盾。
        """
        return {
            FrameFreshness.FRESH: 1.0,
            FrameFreshness.STALE: 0.3,
            FrameFreshness.NONE: 0.0,
        }[freshness]

    # -----------------------------------------------------------------
    # 解释与重置
    # -----------------------------------------------------------------
    def reasons(self) -> list[str]:
        """最近一次 `localization()` 给出的"为什么不确定"。"""
        return list(self._last_reason)

    def reason_text(self, extra: list[str] | None = None) -> str:
        parts = list(self._last_reason) + list(extra or [])
        return "；".join(parts)

    def reset(self) -> None:
        self._innovations.clear()
        self._prev_pos = None
        self._prev_heading = None
        self._last_reason = []

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "noise_m": self.noise_m,
            "mean_innovation_m": round(self.mean_innovation, 3),
            "samples": len(self._innovations),
        }


__all__ = ["ConfidenceEstimator"]
