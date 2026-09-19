"""事件总线（Event Bus）—— 发布 / 订阅 / 冷却去重。

为什么需要总线而不是让检测器直接调用消费者：
    检测器不该知道"谁在关心这件事"；消费者也不该轮询检测器。
    Stage 7 之后 Safety Layer 与 Cognitive Agent 会**同时**订阅事件
    （前者要即时响应，后者按事件触发推理），直接调用会把两者焊死。

冷却（cooldown）语义：
    同一个 key（类型+主体）在冷却期内**再次发生也不发布**。
    这是"同一把椅子连续 100 次 OBSTACLE_APPEARED"的最后一道闸。
    前两道闸在检测器里：状态迁移（appearing→present 不再算"出现"）与 debounce。

⚠️ critical 事件不受冷却约束★
    "前方 0.3 米有障碍"重复发生时，宁可重复报警也不能吞掉 ——
    冷却机制防的是噪音，不是危险。
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Callable

from events.event_types import DEFAULT_COOLDOWN_S, AgentEvent, EventType, Severity

LOG = logging.getLogger("bsa.events")


class EventBus:
    """同步发布/订阅 + 冷却去重。"""

    def __init__(self, history_len: int = 200, cooldowns: dict[EventType, float] | None = None) -> None:
        self._subscribers: dict[str, list[Callable[[AgentEvent], None]]] = {}
        self._last_emit: dict[str, float] = {}
        self._cooldowns: dict[EventType, float] = dict(DEFAULT_COOLDOWN_S)
        if cooldowns:
            self._cooldowns.update(cooldowns)
        # 全部历史（含被冷却吞掉的，标记 suppressed —— 调试时要知道"漏了多少"）
        self.history: deque[AgentEvent] = deque(maxlen=history_len)
        self.suppressed: deque[AgentEvent] = deque(maxlen=history_len)
        self.published_count = 0
        self.suppressed_count = 0

    # -----------------------------------------------------------------
    # 订阅
    # -----------------------------------------------------------------
    def subscribe(self, callback: Callable[[AgentEvent], None], *, channel: str = "all") -> None:
        """订阅事件。channel 预留分组能力（如 'safety' / 'cognitive'），当前全部走 'all'。"""
        self._subscribers.setdefault(channel, []).append(callback)

    def unsubscribe(self, callback: Callable[[AgentEvent], None], *, channel: str = "all") -> None:
        lst = self._subscribers.get(channel, [])
        if callback in lst:
            lst.remove(callback)

    # -----------------------------------------------------------------
    # 发布
    # -----------------------------------------------------------------
    def publish(self, event: AgentEvent) -> bool:
        """发布一条事件。返回是否真的发出（False = 被冷却吞掉）。"""
        self.history.append(event)
        if self._in_cooldown(event):
            self.suppressed.append(event)
            self.suppressed_count += 1
            return False

        key = event.key()
        self._last_emit[key] = event.timestamp
        self.published_count += 1
        for cb in list(self._subscribers.get("all", [])):
            try:
                cb(event)
            except Exception as e:  # noqa: BLE001 - 订阅者异常绝不能打断发布方
                LOG.warning("事件订阅者异常（%s）：%s", getattr(cb, "__name__", cb), e)
        return True

    def _in_cooldown(self, event: AgentEvent) -> bool:
        if event.is_critical():
            return False  # critical 不冷却 —— 冷却防的是噪音，不是危险
        cooldown = self._cooldowns.get(event.event_type, 2.0)
        if cooldown <= 0.0:
            return False
        last = self._last_emit.get(event.key())
        if last is None:
            return False
        return (event.timestamp - last) < cooldown

    # -----------------------------------------------------------------
    # 查询
    # -----------------------------------------------------------------
    def recent(self, n: int = 10, min_severity: Severity | None = None) -> list[AgentEvent]:
        """最近 n 条已发布事件（新→旧按时间正序返回尾部）。"""
        order = ["info", "notice", "warning", "critical"]
        out = list(self.history)
        if min_severity is not None:
            floor = order.index(min_severity.value)
            out = [e for e in out if order.index(e.severity.value) >= floor]
        return out[-n:]

    def active_since(self, t: float, min_severity: Severity | None = None) -> list[AgentEvent]:
        """t 时刻之后发生的事件（Cognitive Agent 的触发判据）。"""
        return [e for e in self.recent(10**9, min_severity) if e.timestamp >= t]

    def last_of(self, event_type: EventType) -> AgentEvent | None:
        for e in reversed(self.history):
            if e.event_type is event_type:
                return e
        return None

    def clear(self) -> None:
        self._last_emit.clear()
        self.history.clear()
        self.suppressed.clear()

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for e in self.history:
            by_type[e.event_type.value] = by_type.get(e.event_type.value, 0) + 1
        return {
            "published": self.published_count,
            "suppressed": self.suppressed_count,
            "history": len(self.history),
            "by_type": by_type,
            "subscribers": sum(len(v) for v in self._subscribers.values()),
        }


__all__ = ["EventBus"]
