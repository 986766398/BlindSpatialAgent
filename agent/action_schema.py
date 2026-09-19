"""行动模式（Action Schema）—— 大模型不得返回任意自然语言。

任务书第十一节：

    「不要让 LLM 返回任意自然语言。建立严格：AgentDecision / Action。
      ActionType: SPEAK / SAFETY_ALERT / REPLAN / REQUEST_VISUAL / ASK_USER /
                  CONTINUE / WAIT / PAUSE_NAVIGATION / RESUME_NAVIGATION / TURN_GUIDANCE
      Action: type / priority / message / reason / confidence / expires_at / metadata
      所有 LLM 输出必须 Pydantic validate。如果解析失败：fallback。不要直接让系统崩。」

为什么要"类型 + 优先级 + 有效期"三件事一起上：

    v0.2 的 Action 只有 5 种类型 + 一句话。于是出现了两个必然的坏味道：
      ① 大模型想说"我信息不够，想看一眼画面"时，只能硬塞进 SPEAK 里
         （于是下游分不清"这是让我说话"还是"这是它在自述"）；
      ② 一段 3 秒前生成的播报，第 5 秒才被说出来（世界已经变了）。
    priority 解决"多条行动同时存在时谁说了算"，
    expires_at 解决"这条结论过期没有"。

★与 v0.2 的兼容（很重要）★
    1. `as_dict()` 是 v0.2 的**线格式契约** —— 前端 `test_page.html`、`tools/e2e_test.py`
       与 120 轮回归快照都在读它。**它的键集合被冻结，不加不减。**
       新增字段通过 `to_dict()` 暴露（日志、诊断、控制台）。
    2. `MOTION_ALLOWED` 里原有的 SPEAK / CONTINUE 语义不变，只**新增**几个允许前进的类型，
       保证"用户能不能往前走"这条物理副作用的判定与 v0.2 完全一致。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spatial.spatial_state import RiskLevel, WalkingStatus

LOG = logging.getLogger("bsa.action")

Urgency = Literal["low", "normal", "high", "critical"]


class ActionType(str, Enum):
    """Agent 可以采取的行动类型（任务书第十一节的 10 种）。"""

    SPEAK = "SPEAK"                        # 播报一句话，用户可继续前进
    SAFETY_ALERT = "SAFETY_ALERT"          # 安全层要求的即时提醒（可带音频告警通道）
    REPLAN = "REPLAN"                      # 重新规划路线
    REQUEST_VISUAL = "REQUEST_VISUAL"      # 信息不足，主动请求一张新画面（Stage 10）
    ASK_USER = "ASK_USER"                  # 向用户提问（用户会停下回答）
    CONTINUE = "CONTINUE"                  # 无需多言，继续按现有指令行进
    WAIT = "WAIT"                          # 让用户原地停下等待
    PAUSE_NAVIGATION = "PAUSE_NAVIGATION"  # 暂停导航（保留路线，不推进）
    RESUME_NAVIGATION = "RESUME_NAVIGATION"  # 恢复导航
    TURN_GUIDANCE = "TURN_GUIDANCE"        # 转弯/朝向指导（"向左转 30 度"）


class ActionChannel(str, Enum):
    """交互通道（任务书第十节：说不说 / 说多少 / 用什么方式）。"""

    VOICE = "voice"                # 语音（当前唯一真正播报的通道）
    SILENT = "silent"              # 什么都不做（Agent 静默）
    CONSOLE_ALERT = "console_alert"  # 只打日志/控制台，不打扰用户
    HAPTIC = "haptic"              # 预留：振动腰带
    AUDIO_CUE = "audio_cue"        # 预留：方位提示音


#: 哪些行动允许用户继续走路（人机协同的关键开关）。
#: ⚠️ 原有 SPEAK / CONTINUE 保持不变；新增项只做"允许"，不做"禁止"，
#:    所以 v0.2 轨迹不会因为本表的扩展而改变（见用例 64 的逐轮比对）。
MOTION_ALLOWED: frozenset[ActionType] = frozenset(
    {
        ActionType.SPEAK,
        ActionType.CONTINUE,
        ActionType.TURN_GUIDANCE,
        ActionType.REQUEST_VISUAL,
        ActionType.RESUME_NAVIGATION,
    }
)

#: 默认优先级（0~100，越大越"抢话"）。同一轮里若有多条候选，取优先级最高者。
DEFAULT_PRIORITY: dict[ActionType, int] = {
    ActionType.WAIT: 100,                 # 停步永远是第一优先
    ActionType.SAFETY_ALERT: 95,
    ActionType.PAUSE_NAVIGATION: 90,
    ActionType.REPLAN: 80,
    ActionType.ASK_USER: 70,
    ActionType.TURN_GUIDANCE: 60,
    ActionType.REQUEST_VISUAL: 55,
    ActionType.SPEAK: 50,
    ActionType.RESUME_NAVIGATION: 45,
    ActionType.CONTINUE: 10,
}

#: 必须带话术的类型（其余类型可以静默）
NEEDS_MESSAGE: frozenset[ActionType] = frozenset(
    {ActionType.SPEAK, ActionType.ASK_USER, ActionType.SAFETY_ALERT, ActionType.TURN_GUIDANCE}
)


class Action(BaseModel):
    """Agent 的输出：一个结构化、有优先级、有有效期的行动。

    字段说明（任务书第十一节要求的 7 项全部就位）：
        action_type  做什么
        priority     多急（0~100）
        message      要朗读给用户的话（可为空 = 保持安静）
        reason       决策依据（只进日志，不朗读）
        confidence   这一步判断的可信度
        expires_at   这条行动的失效时刻（仿真秒；None = 永不过期）
        metadata     附加信息（轮次、触发的规则名、通道细节…）
    另加两个工程必需字段：
        urgency      语速/打断策略用的粗粒度等级（v0.2 既有语义）
        channel      用哪个通道（语音 / 静默 / 控制台告警）
    """

    model_config = ConfigDict(extra="ignore")

    action_type: ActionType
    message: str = Field("", description="要朗读给用户的话")
    priority: int | None = Field(None, ge=0, le=100, description="0~100，越大越优先")
    urgency: Urgency = "normal"
    reason: str = Field("", description="决策依据，用于日志与调试，不朗读")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="该行动的可信度")
    expires_at: float | None = Field(None, description="失效时刻（仿真秒）；None=不过期")
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: Literal["rule", "llm", "user", "safety"] = "rule"
    # None = "由 message 推导"（见 model_validator），显式赋值则尊重调用方
    channel: ActionChannel | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    # -----------------------------------------------------------------
    @field_validator("priority", "expires_at", mode="before")
    @classmethod
    def _coerce_number(cls, v: Any) -> Any:
        """模型偶尔会把 priority 写成 "high" / "urgent" 这类词。

        ⚠️ 这类"差不多能用"的输出不该让整条行动被丢弃（丢弃=退回规则，用户听到
        更差的话术），所以就地降级为 None 走默认值，而不是抛 ValidationError。
        """
        if v is None or isinstance(v, (int, float)):
            return v
        text = str(v).strip().lower()
        words = {"critical": 95, "urgent": 90, "high": 80, "normal": 50, "medium": 50, "low": 20}
        return words.get(text)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_conf(cls, v: Any) -> Any:
        if v is None:
            return 1.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 1.0
        # 模型爱写百分数（85 表示 85%）
        if f > 1.0:
            f = f / 100.0
        return min(1.0, max(0.0, f))

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, v: Any) -> Any:
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return " ".join(str(x) for x in v)
        return str(v)

    def model_post_init(self, __context: Any) -> None:  # noqa: ANN401
        if self.priority is None:
            self.priority = DEFAULT_PRIORITY.get(self.action_type, 50)
        if self.channel is None:
            self.channel = (
                ActionChannel.VOICE if self.message else ActionChannel.SILENT
            )
        if not self.reason:
            self.reason = self.action_type.value

    # -----------------------------------------------------------------
    @property
    def permits_motion(self) -> bool:
        """该行动是否允许用户继续前进（人机协同的关键开关）。"""
        return self.action_type in MOTION_ALLOWED

    @property
    def speaks(self) -> bool:
        """是否真的会对用户发声（有内容 + 通道能出声）。"""
        return bool(self.message) and self.channel in (
            ActionChannel.VOICE,
            ActionChannel.AUDIO_CUE,
        )

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now > self.expires_at

    def effective_priority(self) -> int:
        """参与仲裁时的优先级：安全类永远压过交互类。"""
        base = self.priority if self.priority is not None else 50
        if self.source == "safety" or self.action_type in (
            ActionType.WAIT,
            ActionType.SAFETY_ALERT,
        ):
            base += 1000
        return base

    # -----------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        """★v0.2 线格式契约（键集合已冻结，不要增删）★

        前端 test_page.html、tools/e2e_test.py 与 120 轮回归快照都读这个视图。
        新字段请看 `to_dict()`。
        """
        return {
            "action_type": self.action_type.value,
            "message": self.message,
            "urgency": self.urgency,
            "reason": self.reason,
            "source": self.source,
            "permits_motion": self.permits_motion,
        }

    def to_dict(self) -> dict[str, Any]:
        """完整 schema 视图（日志 / 诊断 / 控制台用）。"""
        out = self.as_dict()
        out.update(
            {
                "priority": self.priority,
                "confidence": round(self.confidence, 3),
                "expires_at": None if self.expires_at is None else round(self.expires_at, 2),
                "channel": (self.channel or ActionChannel.SILENT).value,
                "metadata": self.metadata,
                "speaks": self.speaks,
            }
        )
        return out

    def __str__(self) -> str:
        return f"{self.action_type.value}: {self.message or '(无播报)'}"

    # -----------------------------------------------------------------
    # 大模型输出的严格入口
    # -----------------------------------------------------------------
    @classmethod
    def from_llm(
        cls,
        raw: dict[str, Any] | None,
        *,
        max_chars: int = 40,
        now: float = 0.0,
        ttl: float | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> "Action | None":
        """把大模型的原始 dict 变成受控 Action。**不合格就返回 None**（调用方退回规则）。

        刻意保持"宽进严出"：
            宽进 —— 多余的键忽略、类型的写法容错、message 支持数组；
            严出 —— action_type 必须是枚举内的值；需要说话的类型的 message 不能为空。
        """
        if not raw or not isinstance(raw, dict):
            LOG.warning("模型输出为空或不是对象：%r", raw)
            return None

        raw_type = str(raw.get("action_type", raw.get("type", ""))).strip().upper()
        if raw_type not in {t.value for t in ActionType}:
            LOG.warning("模型返回了未知 action_type: %r", raw.get("action_type"))
            return None

        # ⚠️ 这里**不要**先 str() 再交给校验器：`str(["前方","有障碍"])`
        #    会得到 "['前方', '有障碍']" 这种带引号的字符串，把"数组拼接"这条容错
        #    直接变成死代码（踩过：测试断言 message 才发现的）。数组就地处理。
        raw_message = raw.get("message", "")
        if isinstance(raw_message, (list, tuple)):
            message = " ".join(str(x) for x in raw_message).strip()
        elif raw_message is None:
            message = ""
        else:
            message = str(raw_message).strip()
        if len(message) > max_chars:
            message = message[: max_chars - 1] + "…"

        urgency = str(raw.get("urgency", "normal")).lower()
        if urgency not in ("low", "normal", "high", "critical"):
            urgency = "normal"

        atype = ActionType(raw_type)
        if atype in NEEDS_MESSAGE and not message:
            LOG.warning("模型给出 %s 但没有 message，丢弃该行动", atype.value)
            return None

        meta = raw.get("metadata")
        if not isinstance(meta, dict):
            meta = {}

        try:
            return cls(
                action_type=atype,
                message=message,
                priority=raw.get("priority"),
                urgency=urgency,  # type: ignore[arg-type]
                reason=str(raw.get("reason", "") or "").strip() or "大模型决策",
                confidence=raw.get("confidence", 1.0),
                expires_at=(now + ttl) if (ttl is not None and raw.get("expires_at") is None)
                else raw.get("expires_at"),
                metadata=meta,
                source="llm",
                tool_calls=list(tool_calls or []),
            )
        except Exception as e:  # noqa: BLE001 - 模型输出不可信，任何校验失败都只能降级
            LOG.warning("模型输出未通过 Action 校验，已丢弃：%s（原文 %r）", e, raw)
            return None


class AgentDecision(BaseModel):
    """认知层的完整产出（任务书第十一节："建立严格 AgentDecision"）。

    为什么不直接返回 Action：Action 只回答"做什么"，而认知层还必须能表达
    "我依据什么"与"我还缺什么信息"。后者正是 Stage 10 主动感知的触发依据
    —— `needs_visual=True` 时 Agent 应该请求一张新画面，而不是瞎猜。
    """

    model_config = ConfigDict(extra="ignore")

    action: Action
    rationale: str = Field("", description="一句话说明为什么这么做（进日志，不朗读）")
    information_gaps: list[str] = Field(default_factory=list, description="当前缺失的信息")
    needs_visual: bool = Field(False, description="是否需要一张新画面才能判断")
    model: str = ""
    latency_s: float = 0.0
    prompt_chars: int = 0
    image_attached: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "rationale": self.rationale,
            "information_gaps": self.information_gaps,
            "needs_visual": self.needs_visual,
            "model": self.model,
            "latency_s": round(self.latency_s, 3),
            "prompt_chars": self.prompt_chars,
            "image_attached": self.image_attached,
        }


def default_channel_for(action_type: ActionType) -> ActionChannel:
    """按类型给出默认通道（安全类走语音/告警，其余静默）。"""
    if action_type in (ActionType.SAFETY_ALERT, ActionType.WAIT, ActionType.TURN_GUIDANCE):
        return ActionChannel.VOICE
    if action_type in (ActionType.REPLAN, ActionType.PAUSE_NAVIGATION):
        return ActionChannel.CONSOLE_ALERT
    return ActionChannel.SILENT


def nav_phase(state: Any) -> str:
    """当前导航阶段（Interaction Policy 的输入之一）。

    cruise / approach_turn / blocked / arrived / paused
    """
    if state.user.walking_status is WalkingStatus.ARRIVED or state.navigation.route_progress >= 0.999:
        return "arrived"
    if state.risk.level in (RiskLevel.HIGH, RiskLevel.CRITICAL) or not state.environment.front_clear:
        return "blocked"
    if state.user.walking_status is WalkingStatus.STANDING or state.user.speed <= 0.05:
        return "paused"
    return "cruise"


__all__ = [
    "Action",
    "ActionChannel",
    "ActionType",
    "AgentDecision",
    "DEFAULT_PRIORITY",
    "MOTION_ALLOWED",
    "NEEDS_MESSAGE",
    "Urgency",
    "default_channel_for",
    "nav_phase",
]
