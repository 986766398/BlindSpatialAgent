"""可行动性地图（Affordance Map）—— 把"哪里不好走"沉淀成长期知识。

这是 v0.3 新增的第四张地图，也是最"新"的一张：
    MetricMap      说"能不能走"（几何）
    SemanticMap    说"有什么"（类别）
    NavigationGraph 说"怎么走"（拓扑）
    **AffordanceMap 说"哪里会出问题"（经验）**

典型用法：
    同一个位置连续三个来回都被椅子挡住 → 记一次"此处易阻挡"的证据；
    下次用户走近时，即使当前视野里恰好没看到那把椅子，
    Agent 也能提前说"前方常有人堆放杂物，请慢行"。

★这不是占用栅格★
    占用栅格描述几何，本类描述**经验**：证据可累积、可衰减、可被后续观察推翻。
    刻意不做成栅格，是为了将来接入真实数据时能直接把"用户投诉/人工标注"
    也作为一类证据写进来。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AffordanceKind(str, Enum):
    """长期可行动性标注的类别。"""

    BLOCKED = "blocked"        # 经常过不去
    NARROW = "narrow"          # 经常很窄
    PASSABLE = "passable"      # 通常可通行（正向证据）
    DOORWAY = "doorway"        # 门洞/开口（需要精确对准）
    ROUGH = "rough"            # 地面不平（未来接深度数据）


@dataclass
class AffordanceAnnotation:
    """某处的一条可行动性标注。"""

    kind: AffordanceKind
    x: float
    y: float
    radius: float
    first_seen: float
    last_seen: float
    evidence: int = 1
    detail: str = ""

    @property
    def confidence(self) -> float:
        """证据越多越可信，但**有上限**（0.95）：经验永远不该被当成确定事实。"""
        return round(min(0.95, 0.30 + 0.15 * self.evidence), 3)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "evidence": self.evidence,
            "confidence": self.confidence,
            "detail": self.detail,
        }


class AffordanceMap:
    """按 1 米网格聚合的可行动性经验库。"""

    def __init__(self, cell_m: float = 1.0) -> None:
        self.cell_m = float(cell_m)
        self.annotations: dict[tuple[int, int], AffordanceAnnotation] = {}

    def _key(self, x: float, y: float) -> tuple[int, int]:
        c = max(self.cell_m, 1e-6)
        return int(round(x / c)), int(round(y / c))

    # -----------------------------------------------------------------
    # 写入
    # -----------------------------------------------------------------
    def observe(
        self,
        kind: AffordanceKind,
        x: float,
        y: float,
        t: float,
        *,
        radius: float = 0.6,
        detail: str = "",
    ) -> AffordanceAnnotation:
        """登记一次证据。同一网格内同类证据累加，异类证据互相削弱。"""
        k = self._key(x, y)
        cur = self.annotations.get(k)
        if cur is None:
            cur = AffordanceAnnotation(
                kind=kind, x=x, y=y, radius=radius, first_seen=t, last_seen=t, detail=detail
            )
            self.annotations[k] = cur
            return cur

        if cur.kind is kind:
            cur.evidence += 1
            cur.detail = detail or cur.detail
        else:
            # 同一处出现相反的观察（先"堵"后"通"）—— 证据抵消而不是直接覆盖，
            # 因为一次侥幸通过不足以推翻"常年堆着东西"。
            cur.evidence -= 1
            if cur.evidence <= 0:
                cur.kind = kind
                cur.evidence = 1
                cur.detail = detail
        cur.last_seen = t
        cur.x, cur.y = x, y
        return cur

    def prune(self, t: float, ttl_s: float = 600.0) -> int:
        """淘汰久未复现的标注（防止经验库变成垃圾桶 —— 任务书第二十三节）。"""
        stale = [k for k, a in self.annotations.items() if t - a.last_seen > ttl_s]
        for k in stale:
            self.annotations.pop(k, None)
        return len(stale)

    # -----------------------------------------------------------------
    # 查询
    # -----------------------------------------------------------------
    def near(self, x: float, y: float, radius: float = 2.0) -> list[AffordanceAnnotation]:
        out = [
            a
            for a in self.annotations.values()
            if math.hypot(a.x - x, a.y - y) <= radius
        ]
        out.sort(key=lambda a: -a.confidence)
        return out

    def persistently_blocked(self, x: float, y: float, radius: float = 1.5, min_evidence: int = 2) -> bool:
        """该处是否"经常过不去"（证据足够多才算，避免一次误报）。"""
        return any(
            a.kind in (AffordanceKind.BLOCKED, AffordanceKind.NARROW) and a.evidence >= min_evidence
            for a in self.near(x, y, radius)
        )

    def summary_near(self, x: float, y: float, radius: float = 2.0) -> str:
        near = self.near(x, y, radius)
        if not near:
            return ""
        label = {
            AffordanceKind.BLOCKED: "常被阻挡",
            AffordanceKind.NARROW: "常很狭窄",
            AffordanceKind.PASSABLE: "通常可通行",
            AffordanceKind.DOORWAY: "门洞需对准",
            AffordanceKind.ROUGH: "地面不平",
        }
        return "、".join(f"{label.get(a.kind, a.kind.value)}({a.evidence}次)" for a in near[:3])

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for a in self.annotations.values():
            kinds[a.kind.value] = kinds.get(a.kind.value, 0) + 1
        return {"annotations": len(self.annotations), "by_kind": kinds}


__all__ = ["AffordanceAnnotation", "AffordanceKind", "AffordanceMap"]
