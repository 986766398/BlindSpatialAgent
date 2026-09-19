"""数据新鲜度（freshness）——「这条读数有多新、还能不能用」。

为什么单独成模块（而不是散在融合函数里）：

1. 现实系统的传感器频率差着两个数量级（IMU 100Hz / UWB 10Hz / LiDAR 30Hz /
   相机 1~10Hz / LLM 0.5~2Hz），融合层**不能假设所有数据每轮都更新**。
   "过期了怎么办"必须是显式的、可配置的、能被单测覆盖的策略。
2. 过期与缺失是两件事：`stale`（曾经有，现在过期）应当降置信度并在必要时
   告知用户，`missing`（本系统就没这个源）应当静默降级不报警。
   把它们混成一个布尔量，是 v0.2 最典型的盲点。

本模块是纯函数式的：给"时刻 + 阈值"，返回"新不新"，不持有任何状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from spatial.spatial_state import FrameFreshness


class ChannelFreshness(str, Enum):
    """单个数据通道的新鲜度。"""

    FRESH = "fresh"      # 在阈值内，可直接用于决策
    STALE = "stale"      # 曾经读到过，但已过期（链路可能断了）
    MISSING = "missing"  # 从来没有过（未接入 / 从未收到）


@dataclass(frozen=True)
class FreshnessReading:
    """一个通道的新鲜度读数。"""

    channel: str
    state: ChannelFreshness
    age_s: float | None
    threshold_s: float | None

    @property
    def usable(self) -> bool:
        """能否把它的数据当作"当前事实"用。

        `stale` 刻意不算 usable —— 拿过期数据当现状是视障导航里最危险的错误。
        """
        return self.state is ChannelFreshness.FRESH

    def describe(self) -> str:
        if self.state is ChannelFreshness.MISSING:
            return f"{self.channel} 缺失"
        if self.state is ChannelFreshness.STALE:
            return f"{self.channel} 过期（{self.age_s:.1f}s）"
        return f"{self.channel} 新鲜（{self.age_s:.1f}s）"


class FreshnessPolicy:
    """按通道判定新鲜度。阈值全部来自 `config.yaml:fusion.freshness`。

    默认值刻意与 `camera.stale_after_s`（3.0s）等既有配置对齐，
    避免出现"摄像头在相机层算新鲜、在融合层算过期"这种自相矛盾。
    """

    DEFAULT_THRESHOLDS: dict[str, float | None] = {
        "pose": 0.50,        # UWB 10Hz 级别；超过 0.5s 没更新说明定位链路有问题
        "depth": 0.50,       # LiDAR 30Hz
        "navigation": 2.00,  # 规划器不常更新，2s 内都算有效
        "semantic": 5.00,    # 语义理解是慢变量
        "camera": 3.00,      # 与 camera.stale_after_s 对齐
        "map": None,         # 静态知识，永不过期
    }

    def __init__(self, cfg: dict[str, Any]) -> None:
        section = (cfg.get("fusion") or {}).get("freshness") or {}
        self.thresholds: dict[str, float | None] = dict(self.DEFAULT_THRESHOLDS)
        for raw_key, value in section.items():
            # 配置里写成 `pose_stale_s`（可读性优先），内部按通道名 `pose` 索引。
            # 两种写法都接受，避免改配置的人因为后缀差异而静默失效。
            key = str(raw_key)
            if key.endswith("_stale_s"):
                key = key[: -len("_stale_s")]
            self.thresholds[key] = None if value is None else float(value)
        # 摄像头阈值以 camera 段为准（那里是用户/App 直接感知到的参数）
        cam = (cfg.get("camera") or {}).get("stale_after_s")
        if cam is not None:
            self.thresholds["camera"] = float(cam)

    def threshold(self, channel: str) -> float | None:
        return self.thresholds.get(channel)

    def evaluate(
        self,
        channel: str,
        timestamp: datetime | None,
        now: datetime,
        *,
        ever_seen: bool = True,
    ) -> FreshnessReading:
        """判定某个通道在 `now` 时刻的新鲜度。

        `ever_seen=False` 表示"这个源从来没有产生过数据" —— 这是 `missing`
        而不是 `stale`，两者的处置方式完全不同（后者要报警，前者不用）。
        """
        limit = self.threshold(channel)
        if timestamp is None:
            # 没有时间戳有两种含义，必须区分：
            #   ever_seen=False → 这个源从来没给过数据（missing，静默降级）
            #   ever_seen=True  → 以前给过、这次没给（stale，链路可能断了，要报警）
            state = ChannelFreshness.STALE if ever_seen else ChannelFreshness.MISSING
            return FreshnessReading(
                channel=channel, state=state, age_s=None, threshold_s=limit
            )
        age = max(0.0, (now - timestamp).total_seconds())
        if limit is None:
            return FreshnessReading(channel, ChannelFreshness.FRESH, age, None)
        state = ChannelFreshness.FRESH if age <= limit else ChannelFreshness.STALE
        return FreshnessReading(channel, state, age, limit)

    def camera_state(self, snapshot: dict[str, Any] | None, now: datetime) -> FrameFreshness:
        """把相机快照映射成三态。

        与 `StateManager._camera_state()` 的分工：这里只回答"新不新"，
        不构造 `CameraState` 对象。之所以仍放在 fusion 里，是为了让
        "怎么算过期"这件事只有一个实现（相机层 push、融合层读，两边口径一致）。
        """
        if not snapshot:
            return FrameFreshness.NONE
        if snapshot.get("image_available"):
            return FrameFreshness.FRESH
        if int(snapshot.get("frame_id", 0) or 0) > 0 or snapshot.get("source") not in (None, "none"):
            return FrameFreshness.STALE
        return FrameFreshness.NONE

    def describe_all(self, readings: list[FreshnessReading]) -> str:
        """给日志/调试用的一行摘要。"""
        bad = [r.describe() for r in readings if not r.usable]
        return "；".join(bad) if bad else "全部新鲜"


__all__ = ["ChannelFreshness", "FreshnessPolicy", "FreshnessReading"]
