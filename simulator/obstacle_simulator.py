"""动态障碍模拟器。

模拟真实室内不断变化的环境：
    - 椅子突然被推入通道（静态新障碍）
    - 行人从旁边经过（移动障碍）
    - 通道变窄（左右两侧同时出现障碍物）
    - 箱子临时堆放在路上

对应真实系统的 iPhone LiDAR / 深度相机实时避障输入：
本文件产出「动态障碍列表」，StateManager 再把它与静态地图融合成 EnvironmentState。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from simulator.map_simulator import MapSimulator

# 各类动态障碍的默认半径与运动倾向
_TYPE_PROFILE: dict[str, dict[str, float]] = {
    "person": {"radius": 0.32, "move_prob": 0.85},
    "chair": {"radius": 0.30, "move_prob": 0.05},
    "box": {"radius": 0.35, "move_prob": 0.0},
    "narrow_passage": {"radius": 0.28, "move_prob": 0.0},
}


@dataclass
class DynamicObstacle:
    """一个动态障碍实例。"""

    oid: int
    type: str
    x: float
    y: float
    radius: float
    vx: float = 0.0
    vy: float = 0.0
    ttl: float = 10.0
    max_ttl: float = 10.0
    spawned_at: float = 0.0
    group: int = 0  # 同一次事件生成的障碍共享 group（用于"通道变窄"）

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def pos(self) -> tuple[float, float]:
        return (self.x, self.y)


class ObstacleSimulator:
    """按概率不断制造环境变化的障碍生成器。"""

    def __init__(self, cfg: dict[str, Any], map_sim: MapSimulator, seed: int | None = None) -> None:
        o = cfg["simulator"]["obstacles"]
        self.map = map_sim
        self.enabled: bool = bool(o.get("enabled", True))
        self.spawn_interval: float = float(o.get("spawn_interval_s", 6.0))
        self.max_active: int = int(o.get("max_active", 4))
        self.lifetime: tuple[float, float] = tuple(o.get("lifetime_s", [6.0, 18.0]))  # type: ignore[assignment]
        self.types: list[str] = list(o.get("types", ["chair"]))
        self.move_speed_range: tuple[float, float] = tuple(o.get("move_speed_mps", [0.0, 0.9]))  # type: ignore[assignment]
        self.narrow_width: float = float(o.get("narrow_passage_width_m", 0.6))

        self.rng = random.Random(seed)
        self.obstacles: list[DynamicObstacle] = []
        self._next_id = 1
        self._next_group = 1
        self._timer = self.spawn_interval * 0.5  # 首次稍早触发，便于观察
        self.events: list[str] = []  # 本轮新增的环境变化事件
        self.last_spawn_t: float = 0.0
        self.spawn_total: int = 0

    # -----------------------------------------------------------------
    # 内部工具
    # -----------------------------------------------------------------
    def _random_near_user(self, user_pos: tuple[float, float], heading: float) -> tuple[float, float] | None:
        """在用户前方 2.5~5.5 米、可通行的位置取点。"""
        for _ in range(24):
            dist = self.rng.uniform(2.5, 5.5)
            ang = heading + self.rng.uniform(-40.0, 40.0)
            rad = math.radians(ang)
            x = user_pos[0] + math.sin(rad) * dist
            y = user_pos[1] + math.cos(rad) * dist
            if self.map.is_walkable(x, y) and self.map.zone_at(x, y) is not None:
                return (x, y)
        return None

    def _lateral_offsets(self, heading: float, half_gap: float) -> tuple[tuple[float, float], tuple[float, float]]:
        """返回相对朝向左右垂直方向的两个偏移单位向量。"""
        rad = math.radians(heading + 90.0)
        ux, uy = math.sin(rad), math.cos(rad)
        return ((ux * half_gap, uy * half_gap), (-ux * half_gap, -uy * half_gap))

    # -----------------------------------------------------------------
    # 生成
    # -----------------------------------------------------------------
    def spawn(self, user_pos: tuple[float, float], heading: float, t: float, kind: str | None = None) -> list[DynamicObstacle]:
        """在指定类型下生成一次环境变化事件，返回新增障碍。"""
        kind = kind or self.rng.choice(self.types)
        profile = _TYPE_PROFILE.get(kind, _TYPE_PROFILE["chair"])
        created: list[DynamicObstacle] = []

        # ★名额检查必须放在这里★：下面 `update()` 的准入条件是
        #   `len(obstacles) < max_active`，即默认"这次最多加 1 个"；
        #   但"通道变窄"一次要加 2 个（左右各一），于是 max_active=4 时
        #   只要已有 3 个就会被顶成 5 个 —— 上限失效。
        #   名额不够就整体放弃这次事件（而不是只放半边），
        #   否则会留下"只堵一侧"的怪状态（narrow_passage 分支下面也坚持这一点）。
        if kind == "narrow_passage" and len(self.obstacles) + 2 > self.max_active:
            return []

        group = self._next_group
        self._next_group += 1

        if kind == "narrow_passage":
            # 通道变窄：左右各放一个障碍，中间只留出窄缝
            anchor = self._random_near_user(user_pos, heading)
            if anchor is None:
                return []
            half_gap = max(0.4, self.narrow_width / 2.0 + profile["radius"])
            slots = [
                (anchor[0] + ox, anchor[1] + oy) for ox, oy in self._lateral_offsets(heading, half_gap)
            ]
            if not all(self.map.is_walkable(px, py) for px, py in slots):
                return []  # 两侧落不稳就放弃这次事件，避免出现"半堵"的怪状态
            for px, py in slots:
                ttl = self.rng.uniform(self.lifetime[0], self.lifetime[1]) * 1.4
                created.append(
                    DynamicObstacle(
                        oid=self._next_id,
                        type="box",
                        x=px,
                        y=py,
                        radius=profile["radius"],
                        ttl=ttl,
                        max_ttl=ttl,
                        spawned_at=t,
                        group=group,
                    )
                )
                self._next_id += 1
        else:
            pos = self._random_near_user(user_pos, heading)
            if pos is None:
                return []
            ttl = self.rng.uniform(self.lifetime[0], self.lifetime[1])
            sp = self.rng.uniform(*self.move_speed_range) if self.rng.random() < profile["move_prob"] else 0.0
            ang = self.rng.uniform(0, 360)
            created.append(
                DynamicObstacle(
                    oid=self._next_id,
                    type=kind,
                    x=pos[0],
                    y=pos[1],
                    radius=profile["radius"],
                    vx=math.sin(math.radians(ang)) * sp,
                    vy=math.cos(math.radians(ang)) * sp,
                    ttl=ttl,
                    max_ttl=ttl,
                    spawned_at=t,
                    group=group,
                )
            )
            self._next_id += 1

        self.obstacles.extend(created)
        if created:
            self.spawn_total += 1
            self.events.append(f"{kind} 出现在前方 {math.dist(user_pos, created[0].pos()):.1f} 米处")
        return created

    # -----------------------------------------------------------------
    # 推进
    # -----------------------------------------------------------------
    def update(self, dt: float, user_pos: tuple[float, float], heading: float, t: float) -> None:
        """推进所有动态障碍：移动、到寿清除、按节奏生成新的。"""
        # 移动 + 衰减
        alive: list[DynamicObstacle] = []
        for ob in self.obstacles:
            ob.ttl -= dt
            if ob.ttl <= 0:
                continue
            if ob.speed > 1e-6:
                nx = ob.x + ob.vx * dt
                ny = ob.y + ob.vy * dt
                if self.map.is_walkable(nx, ny):
                    ob.x, ob.y = nx, ny
                else:
                    ob.vx, ob.vy = -ob.vx, -ob.vy  # 撞墙折返
            alive.append(ob)
        self.obstacles = alive

        if not self.enabled:
            return

        self._timer -= dt
        if self._timer <= 0.0 and len(self.obstacles) < self.max_active:
            self._timer = self.spawn_interval * self.rng.uniform(0.7, 1.4)
            self.spawn(user_pos, heading, t)

    def clear(self) -> None:
        self.obstacles.clear()
        self.events.clear()

    # -----------------------------------------------------------------
    # 查询
    # -----------------------------------------------------------------
    def centers(self) -> list[tuple[float, float]]:
        return [ob.pos() for ob in self.obstacles]

    def nearest(self, pos: tuple[float, float]) -> tuple[DynamicObstacle, float] | None:
        if not self.obstacles:
            return None
        best = min(self.obstacles, key=lambda o: math.dist(pos, o.pos()) - o.radius)
        return best, max(0.0, math.dist(pos, best.pos()) - best.radius)

    def total(self) -> int:
        return len(self.obstacles)


__all__ = ["DynamicObstacle", "ObstacleSimulator"]
