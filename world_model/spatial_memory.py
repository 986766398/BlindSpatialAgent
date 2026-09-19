"""短期空间记忆（Spatial Memory）—— 最近 5~30 秒的状态序列。

为什么需要它（任务书第七节：WorldModel 要区分 当前 / 短期 / 长期）：

    "当前状态"（SpatialState 本身）只说明**此刻**；
    但很多判断只能靠一段时间才有意义：
        - 前方距离是在**接近**还是在**远离**？
        - 3 秒前这里是不是通畅的（说明障碍是刚出现的）？
        - 用户是不是一直在同一个区域里绕？

    v0.2 把这三类信息散落在 `AgentMemory.state_snapshots`（只存了一行摘要字符串，
    没法做数值趋势）与各处临时变量里。本模块把"短期记忆"做成一个有窗口、
    有淘汰、可数值查询的结构。

★与 recording/ 的区别★
    短期记忆在**内存**里滚动淘汰（几十秒），用于决策；
    录制（`recording/`）**落盘**保存整段实验，用于复现与科研。
    两者刻意不做成一个东西（任务书第二十三节最后一句）。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from spatial.spatial_state import SpatialState


class SpatialMemory:
    """按时间窗口滚动的状态序列。"""

    def __init__(self, window_s: float = 30.0, max_items: int = 300) -> None:
        self.window_s = float(window_s)
        self._items: deque[SpatialState] = deque(maxlen=max_items)

    # -----------------------------------------------------------------
    def observe(self, state: SpatialState) -> None:
        self._items.append(state)
        self._prune(state.timestamp.timestamp())

    def _prune(self, t_now: float) -> None:
        """淘汰窗口之外的状态。

        两道闸：时间窗口（语义）与 `maxlen`（内存保险）。
        只留时间窗口是不够的 —— 高频循环下 30 秒可能有几千帧。
        """
        while self._items and t_now - self._items[0].timestamp.timestamp() > self.window_s:
            self._items.popleft()

    # -----------------------------------------------------------------
    def recent(self, n: int = 5) -> list[SpatialState]:
        return list(self._items)[-n:]

    def latest(self) -> SpatialState | None:
        return self._items[-1] if self._items else None

    def states_since(self, t_now: float, window_s: float) -> list[SpatialState]:
        return [s for s in self._items if t_now - s.timestamp.timestamp() <= window_s]

    # -----------------------------------------------------------------
    # 数值趋势：这些是"记忆"真正的价值所在
    # -----------------------------------------------------------------
    def front_distance_trend(self, window_s: float = 5.0) -> str:
        """前方距离的变化趋势：closing（在逼近）/ opening / stable / unknown。"""
        t_now = self._items[-1].timestamp.timestamp() if self._items else 0.0
        xs = [s.environment.front_distance for s in self.states_since(t_now, window_s)]
        if len(xs) < 3:
            return "unknown"
        delta = xs[-1] - xs[0]
        if delta < -0.30:
            return "closing"
        if delta > 0.30:
            return "opening"
        return "stable"

    def min_front_distance(self, window_s: float = 5.0) -> float | None:
        """窗口内最近一次前向距离的最小值（"刚才最险的时候有多近"）。"""
        t_now = self._items[-1].timestamp.timestamp() if self._items else 0.0
        xs = [s.environment.front_distance for s in self.states_since(t_now, window_s)]
        return min(xs) if xs else None

    def was_blocked(self, window_s: float = 5.0) -> bool:
        """窗口内是否出现过"前方不可通行"。"""
        t_now = self._items[-1].timestamp.timestamp() if self._items else 0.0
        return any(not s.environment.front_clear for s in self.states_since(t_now, window_s))

    def risk_peak(self, window_s: float = 10.0) -> str:
        """窗口内出现过的最高风险等级（用于"刚才很危险，现在要说明白"）。"""
        order = ["low", "medium", "high", "critical"]
        t_now = self._items[-1].timestamp.timestamp() if self._items else 0.0
        levels = [s.risk.level.value for s in self.states_since(t_now, window_s)]
        if not levels:
            return "unknown"
        return max(levels, key=lambda v: order.index(v) if v in order else 0)

    def seen_types(self, window_s: float = 15.0) -> dict[str, int]:
        """窗口内出现过的物体类型计数（"刚才一路过来碰到过什么"）。"""
        t_now = self._items[-1].timestamp.timestamp() if self._items else 0.0
        out: dict[str, int] = {}
        for s in self.states_since(t_now, window_s):
            for o in s.environment.obstacles:
                out[o.type.value] = out.get(o.type.value, 0) + 1
        return out

    def summarize(self, window_s: float = 10.0) -> str:
        """一句人话摘要，供提示词使用。"""
        if not self._items:
            return "暂无短期记忆"
        parts: list[str] = []
        trend = self.front_distance_trend(window_s)
        label = {"closing": "前方距离在缩短", "opening": "前方越来越开阔", "stable": "前方距离稳定"}
        if trend in label:
            parts.append(label[trend])
        if self.was_blocked(window_s):
            parts.append("近期出现过不可通行的路段")
        peak = self.risk_peak(window_s)
        if peak in ("high", "critical"):
            parts.append(f"近 {window_s:.0f} 秒最高风险 {peak}")
        return "；".join(parts) if parts else "近况平稳"

    def clear(self) -> None:
        self._items.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "short_term_states": len(self._items),
            "window_s": self.window_s,
            "front_trend": self.front_distance_trend(),
        }


__all__ = ["SpatialMemory"]
