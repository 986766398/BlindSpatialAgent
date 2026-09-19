"""事件类型（Event Types）—— 系统内所有"值得知道的事"的词表。

设计原则（任务书第六节）：

1. **事件是状态变化的边沿，不是状态的重复描述**。
   "前方 0.8 米有椅子"是状态；"出现了一把新椅子"是事件。
   把状态当日志打是 v0.2 的问题之一 —— 同一个椅子每秒被"发现"一次。

2. **severity 决定下游的响应强度**：
      info    → 记录即可，不打扰任何人
      notice  → Agent 应当考虑要不要说
      warning → Agent 应当说 / Safety 应当评估
      critical→ Safety 必须立即响应（且不受打扰间隔约束）

3. 每个事件都带 confidence —— 下游要能区分"确定发生了"与"可能发生了"。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """事件类型全集（16 类）。"""

    # --- 用户侧 ---
    USER_COMMAND = "USER_COMMAND"          # 用户下了指令（去出口 / 停下）
    USER_QUESTION = "USER_QUESTION"        # 用户问了个问题（旁边有什么）

    # --- 障碍侧 ---
    OBSTACLE_APPEARED = "OBSTACLE_APPEARED"  # 出现了新障碍
    OBSTACLE_CLEARED = "OBSTACLE_CLEARED"    # 障碍消失了
    OBSTACLE_APPROACHING = "OBSTACLE_APPROACHING"  # 障碍在快速接近

    # --- 路线侧 ---
    ROUTE_DEVIATION = "ROUTE_DEVIATION"    # 偏离路线
    ROUTE_BLOCKED = "ROUTE_BLOCKED"        # 路线被挡住
    GOAL_REACHED = "GOAL_REACHED"          # 到达目的地
    TURN_APPROACHING = "TURN_APPROACHING"  # 即将需要转弯

    # --- 置信度侧 ---
    LOW_LOCALIZATION_CONFIDENCE = "LOW_LOCALIZATION_CONFIDENCE"
    LOW_PERCEPTION_CONFIDENCE = "LOW_PERCEPTION_CONFIDENCE"

    # --- 传感器侧 ---
    CAMERA_LOST = "CAMERA_LOST"            # 摄像头断流（曾有过）
    CAMERA_RECOVERED = "CAMERA_RECOVERED"  # 摄像头恢复

    # --- 冲突与场景 ---
    MAP_SENSOR_CONFLICT = "MAP_SENSOR_CONFLICT"  # 地图说能走，传感器说不能
    SCENE_CHANGED = "SCENE_CHANGED"        # 场景语义发生显著变化

    # --- 安全侧 ---
    HIGH_RISK = "HIGH_RISK"                # 高风险状态出现


class Severity(str, Enum):
    """事件严重度。"""

    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


# 事件类型 → 默认严重度。检测器可以按 payload 提升（如障碍 0.4m 内出现 → critical），
# 但**不允许降级**到低于这张表的值（安全语义不允许"报轻了"）。
DEFAULT_SEVERITY: dict[EventType, Severity] = {
    EventType.USER_COMMAND: Severity.NOTICE,
    EventType.USER_QUESTION: Severity.NOTICE,
    EventType.OBSTACLE_APPEARED: Severity.NOTICE,
    EventType.OBSTACLE_CLEARED: Severity.INFO,
    EventType.OBSTACLE_APPROACHING: Severity.WARNING,
    EventType.ROUTE_DEVIATION: Severity.WARNING,
    EventType.ROUTE_BLOCKED: Severity.WARNING,
    EventType.GOAL_REACHED: Severity.NOTICE,
    EventType.TURN_APPROACHING: Severity.NOTICE,
    EventType.LOW_LOCALIZATION_CONFIDENCE: Severity.WARNING,
    EventType.LOW_PERCEPTION_CONFIDENCE: Severity.WARNING,
    EventType.CAMERA_LOST: Severity.NOTICE,
    EventType.CAMERA_RECOVERED: Severity.INFO,
    EventType.MAP_SENSOR_CONFLICT: Severity.WARNING,
    EventType.SCENE_CHANGED: Severity.INFO,
    EventType.HIGH_RISK: Severity.CRITICAL,
}

# 各事件类型的冷却时间（秒）：同一类型在冷却期内不重复产生。
# 这是"防刷屏"的第一道闸；检测器内部还有"状态迁移"这道闸（见 event_detector.py）。
DEFAULT_COOLDOWN_S: dict[EventType, float] = {
    EventType.USER_COMMAND: 0.0,
    EventType.USER_QUESTION: 0.0,
    EventType.OBSTACLE_APPEARED: 2.0,
    EventType.OBSTACLE_CLEARED: 2.0,
    EventType.OBSTACLE_APPROACHING: 1.5,
    EventType.ROUTE_DEVIATION: 5.0,
    EventType.ROUTE_BLOCKED: 5.0,
    EventType.GOAL_REACHED: 0.0,
    EventType.TURN_APPROACHING: 8.0,
    EventType.LOW_LOCALIZATION_CONFIDENCE: 30.0,
    EventType.LOW_PERCEPTION_CONFIDENCE: 30.0,
    EventType.CAMERA_LOST: 10.0,
    EventType.CAMERA_RECOVERED: 0.0,
    EventType.MAP_SENSOR_CONFLICT: 10.0,
    EventType.SCENE_CHANGED: 5.0,
    EventType.HIGH_RISK: 1.0,
}


class AgentEvent(BaseModel):
    """一条事件。字段即任务书第六节的要求清单。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: EventType
    timestamp: float = Field(default_factory=time.time, description="事件时刻（elapsed 秒）")
    wall_time: float = Field(default_factory=time.time, description="墙钟时刻（unix 秒）")
    severity: Severity = Severity.NOTICE
    source: str = Field("detector", description="产生者：detector / user / safety / fusion")
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    tick: int = Field(0, ge=0, description="产生事件时的感知轮次")

    # -----------------------------------------------------------------
    def key(self) -> str:
        """去重键：类型 + 主体（payload 里的 subject 字段，缺省为类型本身）。

        同一把椅子出现两次、同一摄像头断两次 —— 主体相同才算"同一件事"。
        """
        subject = self.payload.get("subject", "")
        return f"{self.event_type.value}:{subject}"

    def is_critical(self) -> bool:
        return self.severity is Severity.CRITICAL

    def describe(self) -> str:
        base = self.payload.get("description", self.event_type.value)
        return f"[{self.severity.value}] {base}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.event_type.value,
            "timestamp": round(self.timestamp, 2),
            "severity": self.severity.value,
            "source": self.source,
            "confidence": round(self.confidence, 2),
            "payload": self.payload,
            "tick": self.tick,
        }

    def __str__(self) -> str:  # pragma: no cover - 日志友好
        return f"{self.event_type.value}({self.severity.value}) {self.payload.get('description', '')}"


__all__ = [
    "AgentEvent",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_SEVERITY",
    "EventType",
    "Severity",
]
