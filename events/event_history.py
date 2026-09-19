"""事件历史（Event History）—— 事件的可查询存档。

与 `EventBus.history` 的分工：
    总线里的 deque 是**工作集**（容量有限、被冷却吞掉也记、给触发判断用）；
    本类是**按轮次索引的存档**（给提示词、录制、诊断用），
    提供三种典型查询：最近 N 条 / 某时刻之后 / 按类型筛选。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from events.event_types import AgentEvent, EventType, Severity


class EventHistory:
    """带索引的事件存档。"""

    def __init__(self, maxlen: int = 500) -> None:
        self._events: deque[AgentEvent] = deque(maxlen=maxlen)

    def add(self, event: AgentEvent) -> None:
        self._events.append(event)

    def add_all(self, events: list[AgentEvent]) -> None:
        for e in events:
            self.add(e)

    # -----------------------------------------------------------------
    def recent(self, n: int = 8) -> list[AgentEvent]:
        return list(self._events)[-n:]

    def since_tick(self, tick: int) -> list[AgentEvent]:
        """某轮之后发生的事件（Cognitive Agent 的"自上次思考以来"）。"""
        return [e for e in self._events if e.tick > tick]

    def by_type(self, etype: EventType, n: int = 5) -> list[AgentEvent]:
        out = [e for e in self._events if e.event_type is etype]
        return out[-n:]

    def has_critical(self, since_tick: int = 0) -> bool:
        return any(e.is_critical() and e.tick > since_tick for e in self._events)

    def summary_for_prompt(self, n: int = 5) -> str:
        """给提示词的一段事件摘要（最近 n 条，一行一个）。"""
        evs = self.recent(n)
        if not evs:
            return "无事件"
        return "\n".join(f"- {e.describe()}（{e.timestamp:.0f}s 前）" for e in evs)

    def clear(self) -> None:
        self._events.clear()

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for e in self._events:
            by_type[e.event_type.value] = by_type.get(e.event_type.value, 0) + 1
        return {"total": len(self._events), "by_type": by_type}


__all__ = ["EventHistory"]
