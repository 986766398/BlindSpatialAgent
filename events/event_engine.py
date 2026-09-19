"""事件引擎（Event Engine）—— 状态流 → 事件流。

分层位置（任务书第二节）：

    State Fusion → **Event Engine** → Safety Layer → Cognitive Agent

三个组件的分工：

    event_types.py     词表：16 类事件 + 严重度 + 冷却默认值
    event_detector.py  检测：状态迁移/边沿检测（只读，不决策）
    event_bus.py       分发：发布/订阅 + 冷却去重
    event_history.py   存档：按轮次可查询（供提示词与录制）

★为什么事件层必须独立于 Agent★
    v0.2 里"什么时候该说话"散落在规则引擎的 if-else 里，与"该说什么"搅在一起。
    拆出事件之后：
      - Safety Layer（Stage 7）订阅 critical 事件做即时干预；
      - Cognitive Agent（Stage 8）被 notice/warning 事件触发思考；
      - 什么都不发生时（无事件），Agent 天然静默 —— 这就是任务书第六节
        "没有任何变化时 Agent 静默"的机制化实现。

★去重的三道闸★
    1. 状态迁移（detector）：present→present 不发声；
    2. 冷却（bus）：同 key 短期内不重复发布（critical 豁免）；
    3. 主体区分（event.key）：椅子A和椅子B是两件事，同把椅子是同一件事。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from events.event_bus import EventBus
from events.event_detector import EventDetector
from events.event_history import EventHistory
from events.event_types import AgentEvent, EventType, Severity
from spatial.spatial_state import SpatialState
from world_model.world_model import WorldModel

LOG = logging.getLogger("bsa.events")


class EventEngine:
    """检测 + 分发 + 存档的一体化入口（融合层与 Agent 之间的那一段）。"""

    def __init__(self, cfg: dict[str, Any], world: WorldModel) -> None:
        ev = (cfg or {}).get("events", {}) or {}
        history_len = int(ev.get("history_len", 200))
        self.bus = EventBus(history_len=history_len, cooldowns=_cooldowns_from_cfg(ev))
        self.detector = EventDetector(cfg, world)
        self.history = EventHistory(maxlen=max(history_len, 200))

    # -----------------------------------------------------------------
    # 每轮入口（由主循环在融合之后调用）
    # -----------------------------------------------------------------
    def process(self, state: SpatialState, t: float) -> list[AgentEvent]:
        """检测当前状态并发布事件。返回本轮**真正发布**的事件。"""
        detected = self.detector.detect(state, t)
        published: list[AgentEvent] = []
        for e in detected:
            self.history.add(e)
            if self.bus.publish(e):
                published.append(e)
        return published

    # -----------------------------------------------------------------
    # 订阅（Safety / Cognitive / 录制都从这里挂回调）
    # -----------------------------------------------------------------
    def subscribe(self, callback: Callable[[AgentEvent], None]) -> None:
        self.bus.subscribe(callback)

    def unsubscribe(self, callback: Callable[[AgentEvent], None]) -> None:
        self.bus.unsubscribe(callback)

    # -----------------------------------------------------------------
    def reset(self) -> None:
        self.bus.clear()
        self.detector.reset()
        self.history.clear()

    def summary(self, n: int = 5) -> str:
        """最近 n 条事件的文本摘要（供认知循环写进提示词）。"""
        return self.history.summary_for_prompt(n)

    def active_since(self, t: float, *, min_severity: Severity | None = None) -> list[AgentEvent]:
        """t 之后发生的事件（认知触发判据 / 诊断用）。"""
        return self.bus.active_since(t, min_severity)

    def stats(self) -> dict[str, Any]:
        return {
            "bus": self.bus.stats(),
            "detector": self.detector.stats(),
            "archive": self.history.stats(),
        }


__all__ = [
    "AgentEvent",
    "EventBus",
    "EventDetector",
    "EventEngine",
    "EventHistory",
    "EventType",
    "Severity",
]


def _cooldowns_from_cfg(ev: dict[str, Any]) -> dict[EventType, float]:
    """把 config 的 `events.cooldown_s`（键=类型名字符串）转成 {EventType: 秒}。

    未知键只告警不抛异常 —— 事件类型演进时，老配置文件不该让系统起不来。
    """
    out: dict[EventType, float] = {}
    for name, value in (ev.get("cooldown_s") or {}).items():
        try:
            out[EventType(str(name))] = float(value)
        except ValueError:
            LOG.warning("配置里有未知事件类型，已忽略：events.cooldown_s.%s", name)
    return out
