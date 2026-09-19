"""交互策略（Interaction Policy）—— 决定「说不说 / 说多少 / 用什么方式」。

任务书第十节：

    「这是非常重要的一层。它决定：说不说。说多少。用什么方式。
      未来：VOICE / HAPTIC / AUDIO_CUE / SILENT；当前可以先支持 VOICE / SILENT / CONSOLE_ALERT。
      输入考虑：risk / event / last_message / message_frequency / user_speaking /
                navigation phase / information novelty / confidence
      例如：连续直行 20m、状态正常 → Agent 应该 SILENT，
            而不是：继续直行 / 继续直行 / 继续直行。」

★为什么"能说话"不等于"该说话"★
    对盲人用户来说，语音是**独占**通道：一句废话会盖掉脚下的一声异响。
    v0.2 把"要不要播报"的判断散落在规则引擎的 `_say()` 里，规则之外没有第二处把关 ——
    于是大模型返回什么就直接播什么，一段"继续直行"可以反复刷。
    本层把这件事收成**一个可测试的纯判定**，并且**同时**给规则层与认知层使用。

★两种用法（这是本层的设计要点）★
    allow()  —— 规则层用：只做 v0.2 既有的两道闸（重复 / 最小间隔）+ 长度裁剪。
                **语义与 v0.2 逐字保持一致**，所以 120 轮回归快照能守住 0 差异。
    gate()   —— 认知层用：在 allow() 之上再加"巡航静默 / 用户在说话 / 结果过期 /
                置信度不足"这几道**新**闸门。它只作用于大模型输出（本来就不确定），
                因此不会改动规则层的确定性轨迹。

    ⇒ 一句话：**新能力加在非确定路径上，确定性路径保持可证明不变。**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.action_schema import (
    Action,
    ActionChannel,
    ActionType,
    default_channel_for,
    nav_phase,
)
from spatial.spatial_state import RiskLevel, SpatialState

LOG = logging.getLogger("bsa.policy")

#: 需要"打断优先级"的紧急等级（与 v0.2 的 urgent 判定一致）
URGENT_LEVELS = ("high", "critical")


@dataclass
class InteractionContext:
    """交互判定的输入集合（任务书列出的 8 项输入都在这里）。"""

    elapsed: float
    risk_level: RiskLevel = RiskLevel.LOW
    nav_phase_name: str = "unknown"
    urgency: str = "normal"
    last_message: str | None = None
    seconds_since_speak: float = 0.0
    repeat_count: int = 0
    user_speaking: bool = False
    confidence: float = 1.0
    event_types: tuple[str, ...] = ()
    cruise_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk_level.value,
            "phase": self.nav_phase_name,
            "urgency": self.urgency,
            "seconds_since_speak": round(self.seconds_since_speak, 1),
            "user_speaking": self.user_speaking,
            "confidence": round(self.confidence, 2),
            "events": list(self.event_types),
            "cruise_s": round(self.cruise_s, 1),
        }


@dataclass
class InteractionDecision:
    """判定结果：说什么、用哪个通道、为什么。"""

    speak: bool
    channel: ActionChannel
    text: str = ""
    reason: str = ""
    code: str = "ok"  # Machine-readable：ok / repeated / too_frequent / quiet_cruise / …
    max_chars: int = 40
    detail: dict[str, Any] = field(default_factory=dict)

    #: 这些 code 意味着「这条结论已经不可信」，不是「现在不方便说」。
    #: 二者必须区别对待，否则会出现很危险的组合：
    #: 一条 3 秒前的过期 SAFETY_ALERT（permits_motion=False）如果只被清空话术而保留行动，
    #: 它就会**继续否决用户的移动** —— 用户被一条没人听见、也没人复核的过期结论钉在原地。
    DISCARD_CODES: frozenset[str] = frozenset({"expired", "low_confidence"})

    @property
    def discard(self) -> bool:
        """是否应**整条丢弃**（退回规则基线），而不是"只清空话术"。"""
        return self.code in self.DISCARD_CODES

    def as_dict(self) -> dict[str, Any]:
        return {
            "speak": self.speak,
            "channel": self.channel.value,
            "text": self.text,
            "code": self.code,
            "reason": self.reason,
            "discard": self.discard,
        }


class InteractionPolicy:
    """交互策略：能不能说、说多长、走哪个通道。**纯判定，不动状态。**"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        sp = cfg["agent"]["speak_policy"]
        self.min_interval = float(sp["min_interval_s"])
        self.max_chars = int(sp["max_length_chars"])
        self.urgent_bypass = bool(sp.get("urgent_bypass_interval", True))

        it = cfg.get("interaction", {}) or {}
        # ★新增闸门（只作用于认知层，见模块 docstring）★
        self.quiet_when_cruising = bool(it.get("quiet_when_cruising", True))
        self.cruise_quiet_after_s = float(it.get("cruise_quiet_after_s", 20.0))
        self.silence_when_user_speaks = bool(it.get("silence_when_user_speaks", True))
        self.min_confidence = float(it.get("min_confidence", 0.35))
        self.console_alert = bool(it.get("console_alert", True))
        self.channels: dict[str, bool] = dict(
            it.get("channels", {}) or {"voice": True, "console_alert": True}
        )
        self._cruise_since: float | None = None
        #: 闸门计数：{code: 次数}。"策略到底拦下过什么"是排查"为什么刚才没说话"的第一手证据。
        self.counters: dict[str, int] = {}
        self.gates: int = 0

    def _count(self, code: str) -> None:
        self.gates += 1
        self.counters[code] = self.counters.get(code, 0) + 1

    # -----------------------------------------------------------------
    # 基础工具
    # -----------------------------------------------------------------
    def trim(self, text: str) -> str:
        """长度裁剪。**与 v0.2 的 `_trim` 逐字一致**（含省略号规则）。"""
        text = (text or "").strip()
        return text if len(text) <= self.max_chars else text[: self.max_chars - 1] + "…"

    def channel_available(self, channel: ActionChannel) -> bool:
        if channel in (ActionChannel.VOICE, ActionChannel.SILENT):
            return True
        if channel is ActionChannel.CONSOLE_ALERT:
            return self.console_alert
        return bool(self.channels.get(channel.value, False))

    # -----------------------------------------------------------------
    # 导航阶段与"巡航时长"
    # -----------------------------------------------------------------
    def observe(self, state: SpatialState, elapsed: float) -> str:
        """每轮更新阶段记忆，返回当前阶段名。只被快循环调用。"""
        phase = nav_phase(state)
        if phase == "cruise":
            if self._cruise_since is None:
                self._cruise_since = elapsed
        else:
            self._cruise_since = None
        return phase

    def cruise_seconds(self, elapsed: float) -> float:
        if self._cruise_since is None:
            return 0.0
        return max(0.0, elapsed - self._cruise_since)

    # -----------------------------------------------------------------
    # allow()：规则层用的两道闸（语义 = v0.2）
    # -----------------------------------------------------------------
    def allow(
        self,
        text: str,
        urgency: str,
        *,
        elapsed: float,
        memory: Any,
        force: bool = False,
    ) -> InteractionDecision:
        """要不要说这句话（只含 v0.2 既有的重复/间隔两道闸 + 裁剪）。

        ⚠️ 顺序必须与 v0.2 一致：**先 trim，再判重**。
           若反过来，长度不同的同一句话会被当成两句不同的话而重复播报。
        """
        trimmed = self.trim(text)
        if force:
            return InteractionDecision(True, ActionChannel.VOICE, trimmed, "强制播报", "forced",
                                       self.max_chars)

        if memory is not None and memory.repeated(trimmed):
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "与上次播报重复，不打扰用户", "repeated",
                self.max_chars,
            )

        urgent = urgency in URGENT_LEVELS
        since = memory.seconds_since_speak(elapsed) if memory is not None else 1e9
        if not (urgent and self.urgent_bypass) and since < self.min_interval:
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "未超过最小播报间隔，保持安静", "too_frequent",
                self.max_chars,
                {"seconds_since_speak": round(since, 2), "min_interval": self.min_interval},
            )

        return InteractionDecision(True, ActionChannel.VOICE, trimmed, "允许播报", "ok",
                                   self.max_chars)

    def allow_stop(
        self, text: str, *, memory: Any, urgency: str = "critical"
    ) -> InteractionDecision:
        """停步指令要不要带话术（语义 = v0.2 的 `_stop()`）。

        ⚠️ 与 allow() 的**关键区别**：停步指令**不受最小播报间隔约束**。
           因为 WAIT 的物理副作用（不让用户往前走）与"说不说这句话"是两件事：
           同一句"前方有障碍，请停下"连续两轮命中时，第二轮仍然必须拦住用户，
           只是不必再念一遍 —— 所以这里只判重复，不判间隔。
        """
        trimmed = self.trim(text)
        if memory is not None and memory.repeated(trimmed):
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "同一警告已播报，保持停步", "repeated",
                self.max_chars,
            )
        return InteractionDecision(True, ActionChannel.VOICE, trimmed, "停步指令", "ok",
                                   self.max_chars)

    # -----------------------------------------------------------------
    # gate()：认知层用的完整闸门
    # -----------------------------------------------------------------
    def gate(
        self,
        action: Action,
        *,
        elapsed: float,
        memory: Any,
        risk_level: RiskLevel = RiskLevel.LOW,
        phase: str = "unknown",
        user_speaking: bool = False,
        event_types: tuple[str, ...] = (),
        answering_user: bool = False,
    ) -> InteractionDecision:
        """审查一条候选行动（通常来自大模型）能否对用户发声（带闸门计数）。

        `answering_user`：这条行动是**在回答用户刚刚的提问**。
        ★为什么需要它★ 任务书第二十九节的验收剧本是「用户问『我旁边有什么？』→ 取图 →
          简短回答」。但 `agent.speak_policy.min_interval_s`（默认 4s）会把这条回答判成
          `too_frequent` 静音 —— 于是画面取了、模型答了，用户**一个字都没听到**。
          最小间隔的本意是"别主动打扰"，而回答一个刚刚被问到的问题是**应所求**，
          不是打扰。所以这里把它转成 `allow(force=True)`。
        ⚠️ 该开关**只作用于认知（大模型）输出**：规则基线走的是 `RuleDecisionEngine`
          → `allow()`（不带 force），语义与 v0.2 逐字一致，确定性回归不受影响。
        """
        d = self._gate_impl(
            action,
            elapsed=elapsed,
            memory=memory,
            risk_level=risk_level,
            phase=phase,
            user_speaking=user_speaking,
            event_types=event_types,
            answering_user=answering_user,
        )
        self._count(d.code)
        return d

    def _gate_impl(
        self,
        action: Action,
        *,
        elapsed: float,
        memory: Any,
        risk_level: RiskLevel = RiskLevel.LOW,
        phase: str = "unknown",
        user_speaking: bool = False,
        event_types: tuple[str, ...] = (),
        answering_user: bool = False,
    ) -> InteractionDecision:
        """闸门的实际判定。每个分支给一个机器可读的 code，便于统计与归因。"""
        if action.expired(elapsed):
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "该行动已过期（世界变了，不再播报）", "expired",
                self.max_chars,
                {"expires_at": action.expires_at, "now": round(elapsed, 2)},
            )

        if not action.message:
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "本来就无需出声", "silent", self.max_chars
            )

        # 安全类不受"别打扰"类闸门约束：冷却防噪音，不防危险
        safety_class = action.action_type in (ActionType.SAFETY_ALERT, ActionType.WAIT)

        if user_speaking and self.silence_when_user_speaks and not safety_class:
            return InteractionDecision(
                False, ActionChannel.SILENT, "", "用户正在说话，先不打断", "user_speaking",
                self.max_chars,
            )

        if action.confidence < self.min_confidence and not safety_class:
            return InteractionDecision(
                False, ActionChannel.CONSOLE_ALERT, action.message,
                f"置信度 {action.confidence:.2f} 过低，只记控制台不打扰用户", "low_confidence",
                self.max_chars,
            )

        # ★任务书第十节的例子★：连续直行、状态正常、没有任何事件 → 静默
        if (
            self.quiet_when_cruising
            and phase == "cruise"
            and risk_level is RiskLevel.LOW
            and not event_types
            and not safety_class
            and action.action_type
            in (ActionType.SPEAK, ActionType.CONTINUE, ActionType.TURN_GUIDANCE)
            and memory is not None
            and memory.repeated(action.message)
        ):
            return InteractionDecision(
                False, ActionChannel.SILENT, "",
                f"巡航中且内容与上次相同（已直行 {self.cruise_seconds(elapsed):.0f}s），保持安静",
                "quiet_cruise",
                self.max_chars,
            )

        base = self.allow(
            action.message,
            action.urgency,
            elapsed=elapsed,
            memory=memory,
            # ★回答用户提问 = 应所求，不受"重复 / 最小间隔"两道打扰闸约束★
            force=answering_user,
        )
        if not base.speak and base.code == "repeated":
            return base
        if not base.speak:
            return base

        channel = action.channel or default_channel_for(action.action_type)
        if not self.channel_available(channel):
            LOG.info("通道 %s 未启用，降级为控制台告警", channel.value)
            return InteractionDecision(
                False, ActionChannel.CONSOLE_ALERT, base.text, f"通道 {channel.value} 未启用",
                "channel_disabled", self.max_chars,
            )
        return InteractionDecision(True, channel, base.text, base.reason, "ok", self.max_chars)

    # -----------------------------------------------------------------
    def apply(self, decision: InteractionDecision, action: Action) -> Action:
        """把判定落到 Action 上：通道、裁剪后的文本，或降级为静默。

        ⚠️ 这里**只处理"不方便说"这一类**（清空话术、保留行动的物理语义）。
           "结论已不可信"这一类（`decision.discard`）必须由调用方**整条丢掉并退回规则基线**，
           不能在这里悄悄把 message 清成空就完事 —— 那会把"停住用户"的否决权留在系统里。
           见 `AgentOrchestrator.decide()` 与 `SpatialAgent.decide()` 的处理。
        """
        if decision.speak:
            return action.model_copy(
                update={
                    "message": decision.text,
                    "channel": decision.channel,
                    "priority": action.priority
                    if action.priority is not None
                    else 50,
                }
            )
        # 不发声：消息清空、通道归位、优先级降到最低（避免抢掉真正要说的行动）
        return action.model_copy(
            update={
                "message": "",
                "channel": decision.channel,
                "priority": min(action.priority or 50, 20),
                "metadata": {**action.metadata, "silenced_by": decision.code},
            }
        )

    # -----------------------------------------------------------------
    def context_for(
        self,
        state: SpatialState,
        elapsed: float,
        *,
        urgency: str = "normal",
        user_speaking: bool = False,
        event_types: tuple[str, ...] = (),
    ) -> InteractionContext:
        """把当前状态收成任务书要求的 8 项输入（诊断用 / 可落盘）。

        这不是决策路径上的必需品，但把"策略看了哪些输入"变成一个**可打印的对象**，
        是排除"为什么刚才没说话"这类问题的唯一省力办法。
        """
        last = None
        return InteractionContext(
            elapsed=elapsed,
            risk_level=state.risk.level,
            nav_phase_name=nav_phase(state),
            urgency=urgency,
            last_message=last,
            seconds_since_speak=0.0,
            repeat_count=0,
            user_speaking=user_speaking,
            confidence=state.confidence.overall_confidence,
            event_types=event_types,
            cruise_s=self.cruise_seconds(elapsed),
        )

    def reset(self) -> None:
        self._cruise_since = None
        self.counters.clear()
        self.gates = 0

    def stats(self) -> dict[str, Any]:
        blocked = {k: v for k, v in self.counters.items() if k not in ("ok", "forced")}
        return {
            "min_interval_s": self.min_interval,
            "max_chars": self.max_chars,
            "urgent_bypass_interval": self.urgent_bypass,
            "quiet_when_cruising": self.quiet_when_cruising,
            "cruise_quiet_after_s": self.cruise_quiet_after_s,
            "min_confidence": self.min_confidence,
            "console_alert": self.console_alert,
            "gates": self.gates,
            "blocked": sum(blocked.values()),
            "blocked_by": blocked,
        }


__all__ = ["InteractionContext", "InteractionDecision", "InteractionPolicy", "URGENT_LEVELS"]
