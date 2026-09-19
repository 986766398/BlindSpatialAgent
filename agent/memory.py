"""Agent 记忆：对话历史 + 行动历史 + 已播报内容。

为什么需要它：
1. 大模型本身无状态，跨轮次的连贯性只能靠记忆喂回去。
2. 「不要频繁打扰用户」这条原则要求 Agent 知道自己刚说过什么、什么时候说的。
3. 用户问「我刚才是不是走过一个门口」这类问题，需要回溯历史。

记忆只保存摘要级信息，不保存原始传感器数据。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from spatial.spatial_state import SpatialState


@dataclass
class Turn:
    """一轮交互记录。"""

    role: str  # user / agent / system
    content: str
    elapsed: float
    action_type: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, str]:
        return {"role": "assistant" if self.role == "agent" else self.role, "content": self.content}


@dataclass
class Utterance:
    """一次语音播报。"""

    text: str
    elapsed: float
    urgency: str = "normal"
    action_type: str = "SPEAK"


class AgentMemory:
    """有窗口限制的记忆体。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        m = cfg["agent"]["memory"]
        max_turns = int(m.get("max_turns", 30))
        self.turns: deque[Turn] = deque(maxlen=max_turns)
        self.utterances: deque[Utterance] = deque(maxlen=max_turns)
        self.state_snapshots: deque[str] = deque(maxlen=20)
        self.last_speak_elapsed: float = -1e9
        self.last_action_type: str | None = None
        self.action_counts: dict[str, int] = {}

    # -----------------------------------------------------------------
    # 写入
    # -----------------------------------------------------------------
    def add_user(self, text: str, elapsed: float) -> None:
        self.turns.append(Turn(role="user", content=text, elapsed=elapsed))

    def add_agent(self, text: str, elapsed: float, action_type: str | None = None, **meta: Any) -> None:
        self.turns.append(
            Turn(role="agent", content=text, elapsed=elapsed, action_type=action_type, meta=meta)
        )
        self.last_action_type = action_type
        if action_type:
            self.action_counts[action_type] = self.action_counts.get(action_type, 0) + 1

    def add_utterance(self, text: str, elapsed: float, urgency: str = "normal", action_type: str = "SPEAK") -> None:
        """记录一次真实播报（用于打扰频率控制）。"""
        self.utterances.append(Utterance(text=text, elapsed=elapsed, urgency=urgency, action_type=action_type))
        self.last_speak_elapsed = elapsed

    def add_state_snapshot(self, state: SpatialState) -> None:
        self.state_snapshots.append(state.summary_line())

    # -----------------------------------------------------------------
    # 查询
    # -----------------------------------------------------------------
    def seconds_since_speak(self, elapsed: float) -> float:
        if self.last_speak_elapsed < -1e8:
            return 1e9
        return elapsed - self.last_speak_elapsed

    def last_utterance(self) -> Utterance | None:
        return self.utterances[-1] if self.utterances else None

    def repeated(self, text: str) -> bool:
        """是否与上一条播报重复（避免复读机）。"""
        last = self.last_utterance()
        return last is not None and last.text == text

    def recent_turns(self, n: int = 6) -> list[Turn]:
        return list(self.turns)[-n:]

    # -----------------------------------------------------------------
    # 导出
    # -----------------------------------------------------------------
    def to_llm_messages(self, n: int = 8) -> list[dict[str, str]]:
        return [t.as_message() for t in self.recent_turns(n)]

    def summary(self) -> str:
        if not self.utterances:
            return "尚未与用户交互"
        recent = list(self.utterances)[-3:]
        return "；".join(f"[{u.elapsed:.0f}s] {u.text}" for u in recent)

    def stats(self) -> dict[str, Any]:
        return {
            "turns": len(self.turns),
            "utterances": len(self.utterances),
            "action_counts": dict(self.action_counts),
            "last_utterance": self.last_utterance().text if self.utterances else None,
        }


__all__ = ["AgentMemory", "Turn", "Utterance"]
