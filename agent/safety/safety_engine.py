"""安全引擎（Safety Engine）—— 高速安全循环的执行体。

架构位置（任务书第二节）：

    Event Engine → **Safety Layer（本文件）** → Cognitive Agent → Interaction Policy

★快循环 vs 慢循环★

    快循环（本层）：每一次 tick 都跑，纯计算（实测 ~0.05 ms），不联网、不等 IO。
                    职责 = 障碍距离 / 落差 / 台阶 / 严重偏航 / 高风险 / 置信度退化。
    慢循环（orchestrator）：事件驱动 + 心跳，可能要等大模型几秒。

    两者必须**真并发**：单线程里无论把 LLM 超时设多小，等待期间快循环都是停的，
    而"停下来的安全层"对盲人用户等于不存在。所以 LLM 必须离开主线程
    （见 `agent/orchestrator.py` 的 CognitiveLoop）。

★本层的输出是"判定"，不是"播报"★

    SafetyVerdict 描述「现在有多危险、是否必须干预、为什么」。
    要不要说话、说多少、用什么通道（语音/振动）是 Interaction Policy（Stage 8）的事。
    本层只保证一件事：**危险不会因为大模型慢而被漏掉**。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.safety.risk_rules import (
    DEFAULT_RULES,
    RuleContext,
    SafetyAction,
    SafetyLevel,
    SafetyRule,
    SafetyThresholds,
    max_level,
)
from events.event_types import AgentEvent
from spatial.spatial_state import SpatialState

LOG = logging.getLogger("bsa.safety")


@dataclass
class SafetyVerdict:
    """一轮安全判定。"""

    level: SafetyLevel = SafetyLevel.OK
    action: SafetyAction = SafetyAction.NONE
    reason: str = ""
    message: str = ""
    triggers: list[str] = field(default_factory=list)  # 命中的规则名
    t: float = 0.0

    @property
    def intervene(self) -> bool:
        """是否必须立即干预（阻止继续前进）。"""
        return self.level is SafetyLevel.EMERGENCY

    @property
    def should_alert(self) -> bool:
        return self.level in (SafetyLevel.WARNING, SafetyLevel.EMERGENCY)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "action": self.action.value,
            "reason": self.reason,
            "message": self.message,
            "triggers": self.triggers,
            "intervene": self.intervene,
        }


class SafetyEngine:
    """确定性安全层：每个 tick 求值一次规则表，产出 SafetyVerdict。

    ★不持有 LLM、不持有网络句柄、不写 SpatialState★
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        rules: tuple[SafetyRule, ...] | None = None,
        alert_sink: Callable[[SafetyVerdict], None] | None = None,
        history_len: int = 200,
    ) -> None:
        self.cfg = cfg
        self.rules: tuple[SafetyRule, ...] = rules if rules is not None else DEFAULT_RULES
        thresholds = SafetyThresholds.from_cfg(cfg)
        self.thresholds = thresholds
        self.alert_sink = alert_sink
        self._last_fire: dict[str, float] = {}
        self.alerts: deque[SafetyVerdict] = deque(maxlen=history_len)
        self.evaluations: int = 0
        self.emergencies: int = 0
        #: 最近一次判定（只读缓存）：ContextBuilder（Stage 8）要把它写进提示词，
        #: 让模型知道"安全层刚刚怎么判的"，而不是让模型自己从环境字段重新猜。
        self.last_verdict: SafetyVerdict | None = None

    # -----------------------------------------------------------------
    # 主入口（快循环调用；实测远低于 1 ms）
    # -----------------------------------------------------------------
    def evaluate(
        self,
        state: SpatialState,
        events: list[AgentEvent] | tuple[AgentEvent, ...] = (),
        t: float = 0.0,
    ) -> SafetyVerdict:
        ctx = RuleContext(state=state, events=tuple(events), thresholds=self.thresholds)
        self.evaluations += 1

        hits: list[SafetyRule] = []
        for rule in self.rules:
            try:
                if not rule.predicate(ctx):
                    continue
            except Exception as e:  # noqa: BLE001 - 单条规则出错不能拖垮整个安全层
                LOG.warning("安全规则 %s 求值异常，已跳过：%s", rule.name, e)
                continue
            # EMERGENCY 豁免冷却：冷却防的是噪音，不是危险
            if rule.level is not SafetyLevel.EMERGENCY and self._in_cooldown(rule, t):
                continue
            hits.append(rule)

        if not hits:
            verdict = SafetyVerdict(t=t)
            self.last_verdict = verdict
            return verdict

        level = max_level([r.level for r in hits])
        # 只保留达到最高级别的那几条（低级别命中对本次判定没有信息量）
        top = [r for r in hits if r.level is level]
        chosen = top[0]  # DEFAULT_RULES 的顺序即同级优先级
        for r in top:
            self._last_fire[r.name] = t

        verdict = SafetyVerdict(
            level=level,
            action=chosen.action,
            reason=chosen.reason or chosen.name,
            message=chosen.message(state),
            triggers=[r.name for r in hits],
            t=t,
        )
        self.last_verdict = verdict
        if verdict.should_alert:
            self.alerts.append(verdict)
            if verdict.intervene:
                self.emergencies += 1
            LOG.debug(
                "安全判定 %s（%s）：%s",
                verdict.level.value, ",".join(verdict.triggers), verdict.message,
            )
            if self.alert_sink is not None:
                try:
                    self.alert_sink(verdict)
                except Exception as e:  # noqa: BLE001 - 上报通道异常不能影响安全判断
                    LOG.warning("安全告警上报失败：%s", e)
        return verdict

    # -----------------------------------------------------------------
    def _in_cooldown(self, rule: SafetyRule, t: float) -> bool:
        last = self._last_fire.get(rule.name)
        if last is None or rule.cooldown_s <= 0.0:
            return False
        return (t - last) < rule.cooldown_s

    def recent_alerts(self, n: int = 5) -> list[SafetyVerdict]:
        return list(self.alerts)[-n:]

    def reset(self) -> None:
        self._last_fire.clear()
        self.alerts.clear()
        self.evaluations = 0
        self.emergencies = 0

    def stats(self) -> dict[str, Any]:
        return {
            "evaluations": self.evaluations,
            "emergencies": self.emergencies,
            "alerts": len(self.alerts),
            "rules": len(self.rules),
            "emergency_rules": sum(1 for r in self.rules if r.level is SafetyLevel.EMERGENCY),
        }


__all__ = ["SafetyEngine", "SafetyVerdict"]
