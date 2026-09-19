"""对象记忆（Object Memory）—— "这把椅子是同一把，而且它一直在那儿"。

要解决的问题（任务书第七节）：
    v0.2 每帧重新发现障碍：`chair` 在 1.8m 出现一次、1.3m 又一次、0.9m 再一次，
    世界模型里就记成"遇到过 3 次 chair"。结果是 Agent 每帧都在"发现新障碍"，
    播报与事件都会重复。

    v0.3 的目标不是完整 tracking（任务书明确禁止复杂 object tracking），
    而是**简单而有效**的三件事：
      1. 空间聚合：0.5 米网格内的同类物体视为同一物体；
      2. 稳定编号：每个新物体分配 `chair_001` 这样的可读 ID（能跨帧引用）；
      3. 时间持续性：first_seen / last_seen / seen_count / 接近或远离趋势。

★为什么保留"网格聚合"这一层而不是只用 oid★
    动态障碍有 `oid`，静态阻挡物用的是 `STATIC_OID_BASE + idx`，都对得上；
    但未来接真实传感器时，检测器**不会给稳定 ID**（每帧都是新的检测框）。
    网格聚合是那种场景下唯一可用的兜底，所以两套并存：有 oid 走 oid，没有走网格。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ObstacleMemory:
    """一条障碍记忆。"""

    type: str
    x: float
    y: float
    radius: float
    first_seen: float
    last_seen: float
    seen_count: int = 1
    oid: int | None = None
    name: str = ""          # 稳定可读 ID，如 chair_001
    is_dynamic: bool = False

    @property
    def age_s(self) -> float:
        """从首次出现到现在持续了多久（秒）。"""
        return max(0.0, self.last_seen - self.first_seen)

    @property
    def confidence(self) -> float:
        """被反复看到的物体更可信（上限 0.98：静态地图知识才配 1.0）。"""
        return round(min(0.98, 0.55 + 0.05 * self.seen_count + min(self.age_s, 30.0) * 0.01), 3)

    @property
    def persistent(self) -> bool:
        """是否算"持续存在"（看过 ≥3 次且跨过 3 秒）。"""
        return self.seen_count >= 3 and self.age_s >= 3.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "seen_count": self.seen_count,
            # v0.3 新增：可读 ID 与持续性，供模型引用"同一把椅子"
            "name": self.name,
            "age_s": round(self.age_s, 1),
            "confidence": self.confidence,
            "dynamic": self.is_dynamic,
        }


@dataclass
class TrackStats:
    """某个动态障碍的运动趋势。"""

    oid: int
    distances: deque[float] = field(default_factory=deque)

    def add(self, d: float, window: int = 5) -> None:
        self.distances.append(d)
        while len(self.distances) > window:
            self.distances.popleft()

    def trend(self) -> str:
        """approaching / receding / stable。"""
        if len(self.distances) < 3:
            return "unknown"
        delta = self.distances[-1] - self.distances[0]
        if delta < -0.25:
            return "approaching"
        if delta > 0.25:
            return "receding"
        return "stable"


class ObjectMemoryStore:
    """空间聚合 + 稳定编号 + 时间持续性的对象记忆库。"""

    def __init__(self, evict_after_s: float = 60.0) -> None:
        self.memories: dict[tuple[int, int], ObstacleMemory] = {}
        self.encounters: dict[str, int] = {}
        self.tracks: dict[int, TrackStats] = {}
        self.evict_after_s = float(evict_after_s)
        self._by_oid: dict[int, tuple[int, int]] = {}
        self._name_counter: dict[str, int] = {}
        self.evicted: int = 0

    # -----------------------------------------------------------------
    @staticmethod
    def _key(x: float, y: float) -> tuple[int, int]:
        return (int(round(x * 2)), int(round(y * 2)))  # 0.5m 网格聚合

    def _assign_name(self, otype: str) -> str:
        n = self._name_counter.get(otype, 0) + 1
        self._name_counter[otype] = n
        return f"{otype}_{n:03d}"

    # -----------------------------------------------------------------
    def observe(self, obstacles: list[dict[str, Any]], t: float) -> list[str]:
        """登记一轮视野内的物体，返回**本轮新发现**的对象名。

        返回值刻意是"新发现的名字"而不是数量：事件引擎要靠它判断
        "这个椅子是第一次出现"还是"又是它"，从而只产生一次 OBSTACLE_APPEARED。
        """
        new_names: list[str] = []
        for o in obstacles:
            if o.get("x") is None or o.get("y") is None:
                continue
            oid = o.get("oid")
            key: tuple[int, int] | None = None
            if oid is not None:
                key = self._by_oid.get(int(oid))
            if key is None:
                key = self._key(float(o["x"]), float(o["y"]))
                if oid is not None:
                    self._by_oid[int(oid)] = key

            mem = self.memories.get(key)
            if mem is None:
                otype = str(o.get("type", "unknown"))
                mem = ObstacleMemory(
                    type=otype,
                    x=float(o["x"]),
                    y=float(o["y"]),
                    radius=float(o.get("radius", 0.3)),
                    first_seen=t,
                    last_seen=t,
                    oid=int(oid) if oid is not None else None,
                    name=self._assign_name(otype),
                    is_dynamic=bool(o.get("is_dynamic")),
                )
                self.memories[key] = mem
                self.encounters[otype] = self.encounters.get(otype, 0) + 1
                new_names.append(mem.name)
            else:
                mem.last_seen = t
                mem.seen_count += 1
                mem.x, mem.y = float(o["x"]), float(o["y"])

            if oid is not None and o.get("distance") is not None and o.get("is_dynamic"):
                self.tracks.setdefault(int(oid), TrackStats(int(oid))).add(float(o["distance"]))

        self.evict(t)
        return new_names

    def evict(self, t: float) -> int:
        """淘汰久未复现的对象（"世界模型不是数据库垃圾桶"，任务书第二十三节）。"""
        stale = [k for k, m in self.memories.items() if t - m.last_seen > self.evict_after_s]
        for k in stale:
            m = self.memories.pop(k, None)
            if m is not None and m.oid is not None:
                self._by_oid.pop(m.oid, None)
                self.tracks.pop(m.oid, None)
        self.evicted += len(stale)
        return len(stale)

    # -----------------------------------------------------------------
    def near(self, x: float, y: float, radius: float = 3.0) -> list[ObstacleMemory]:
        out = [
            m for m in self.memories.values() if math.hypot(m.x - x, m.y - y) <= radius
        ]
        out.sort(key=lambda m: math.hypot(m.x - x, m.y - y))
        return out

    def persistent_objects(self, min_seen: int = 3) -> list[ObstacleMemory]:
        """被反复看到、值得当作"长期存在"的物体。"""
        return [m for m in self.memories.values() if m.seen_count >= min_seen]

    def approaching(self) -> list[int]:
        return [oid for oid, tr in self.tracks.items() if tr.trend() == "approaching"]

    def is_known(self, name: str) -> bool:
        return any(m.name == name for m in self.memories.values())

    def clear(self) -> None:
        self.memories.clear()
        self.encounters.clear()
        self.tracks.clear()
        self._by_oid.clear()
        self._name_counter.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "obstacle_memories": len(self.memories),
            "persistent": len(self.persistent_objects()),
            "evicted": self.evicted,
            "encounters": dict(self.encounters),
        }


__all__ = ["ObstacleMemory", "ObjectMemoryStore", "TrackStats"]
