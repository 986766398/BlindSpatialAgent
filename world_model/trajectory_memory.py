"""轨迹记忆（Trajectory Memory）—— "我从哪来、走了多少路、是不是在打转"。

为什么单独成模块：
    "原地打转"是视障导航里最需要被发现的失败模式之一（用户迷路后会绕着
    一小片区域转圈，而每一帧的感知都完全正常）。它的判据需要一段时间的
    轨迹，属于"记忆"而不是"状态"。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any


class TrajectoryMemory:
    """按时间窗口保存的轨迹点序列。

    判据（与 v0.2 一致，刻意不改）：**走了不少路，但净位移很小**。
    不能只看"活动范围小" —— 走得慢的人范围也小，会误报。
    """

    def __init__(self, trail_len: int = 120) -> None:
        self.trail: deque[tuple[float, float]] = deque(maxlen=trail_len)
        self.trail_times: deque[float] = deque(maxlen=trail_len)
        self.visited_zones: list[str] = []
        self.spin_warning: bool = False

    # -----------------------------------------------------------------
    def add(self, x: float, y: float, t: float, zone: str | None = None) -> bool:
        """登记一次位姿观测。返回是否发生了"进入新区域"这一事件。"""
        self.trail.append((x, y))
        self.trail_times.append(t)
        entered = False
        if zone and (not self.visited_zones or self.visited_zones[-1] != zone):
            self.visited_zones.append(zone)
            entered = True
        self.spin_warning = self.detect_spin()
        return entered

    # -----------------------------------------------------------------
    def detect_spin(
        self,
        window_s: float = 20.0,
        net_threshold_m: float = 1.5,
        path_min_m: float = 2.0,
    ) -> bool:
        """窗口内路径长度够长、净位移却很小 ⇒ 打转。"""
        if len(self.trail) < 6 or len(self.trail_times) < 6:
            return False
        t_now = self.trail_times[-1]
        pts = [p for p, tt in zip(self.trail, self.trail_times) if t_now - tt <= window_s]
        if len(pts) < 6:
            return False
        path_len = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        net = math.dist(pts[0], pts[-1])
        return path_len >= path_min_m and net <= net_threshold_m

    def distance_travelled(self) -> float:
        pts = list(self.trail)
        return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    def points_since(self, t_now: float, window_s: float = 10.0) -> list[tuple[float, float]]:
        """最近 window_s 秒内的轨迹点。"""
        return [p for p, tt in zip(self.trail, self.trail_times) if t_now - tt <= window_s]

    def heading_change(self, window_s: float = 10.0) -> float:
        """窗口内的航向变化幅度（度）。用于区分"沿走廊直行"与"在原地转身找方向"。"""
        pts = self.points_since(self.trail_times[-1] if self.trail_times else 0.0, window_s)
        if len(pts) < 3:
            return 0.0
        total = 0.0
        for i in range(1, len(pts) - 1):
            a = math.atan2(pts[i][1] - pts[i - 1][1], pts[i][0] - pts[i - 1][0])
            b = math.atan2(pts[i + 1][1] - pts[i][1], pts[i + 1][0] - pts[i][0])
            d = abs((b - a + math.pi) % (2 * math.pi) - math.pi)
            total += math.degrees(d)
        return round(total, 1)

    def current_zone(self) -> str | None:
        return self.visited_zones[-1] if self.visited_zones else None

    def reset(self) -> None:
        self.trail.clear()
        self.trail_times.clear()
        self.visited_zones.clear()
        self.spin_warning = False

    def stats(self) -> dict[str, Any]:
        return {
            "trail_points": len(self.trail),
            "distance_travelled_m": round(self.distance_travelled(), 2),
            "visited_zones": list(self.visited_zones),
            "spin_warning": self.spin_warning,
        }


__all__ = ["TrajectoryMemory"]
