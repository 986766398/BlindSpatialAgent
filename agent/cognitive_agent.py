"""认知智能体（Cognitive Agent）—— 用实时多模态大模型做"理解与表达"。

任务书第十节：

    「Cognitive Agent 使用实时多模态 LLM。
      输入：SpatialState 摘要 / 重要历史 / active events / 必要图片 / user intent
      输出：Decision。
      **不要让它直接 print。**」

最后一句是这一层的全部纪律：它**只能返回一个结构化 Decision**，
不能直接播报、不能直接改世界、不能直接输出自然语言给用户。
副作用一律由编排层经 `tools.call()` 派发（见 `SpatialAgentSystem._apply`）。

★分层关系★
    ContextBuilder  决定"它能看到什么"          （信息面）
    CognitiveAgent  决定"它想到了什么"          （推理 + 严格 schema 校验）
    InteractionPolicy 决定"它能不能说、怎么说"  （表达闸门）
    AgentOrchestrator 把上面三件事合起来          （编排）

把这三件事分开的直接收益：想换大模型只动本文件；想改"什么时候别说话"只动策略；
想让它少看一点信息只动构建器。v0.2 里这三件事全挤在 `SpatialAgent._llm_decide` 里。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.action_schema import Action, ActionType, AgentDecision
from agent.context_builder import ContextBuilder, ContextBundle

LOG = logging.getLogger("bsa.cognitive")


class CognitiveAgent:
    """大模型侧的理解与表达层。任何失败都返回 None，由规则基线兜底。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        llm: Any,
        context: ContextBuilder,
        *,
        name: str = "cognitive",
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.context = context
        self.name = name
        sp = cfg["agent"]["speak_policy"]
        self.max_chars = int(sp["max_length_chars"])
        c = cfg.get("cognitive", {}) or {}
        # 认知结果的有效期：与认知循环的 result_ttl_s 同源，保证"两处判过期"口径一致
        self.action_ttl_s: float | None = float(c.get("result_ttl_s", 4.0))
        self.request_visual_when_blind = bool(c.get("request_visual_when_blind", True))
        self.calls = 0
        self.parse_failures = 0
        self.last_latency = 0.0

    # -----------------------------------------------------------------
    @property
    def usable(self) -> bool:
        return self.llm is not None and bool(getattr(self.llm, "usable", False))

    # -----------------------------------------------------------------
    def build_context(
        self,
        state: Any,
        *,
        elapsed: float,
        user_query: str | None = None,
        events: list[Any] | None = None,
        safety_verdict: Any | None = None,
        force_visual: bool = False,
    ) -> ContextBundle:
        """`force_visual` 来自 `request_visual_observation()`（模型显式索要一张新画面）。

        ★它必须由调用方用**一次性**语义传进来★ —— 取走的是一次性标志，
          否则"请求一次"会变成"从此每轮都请求"。
        """
        return self.context.build(
            state,
            elapsed=elapsed,
            user_query=user_query,
            events=events,
            safety_verdict=safety_verdict,
            force_visual=force_visual,
        )

    # -----------------------------------------------------------------
    def decide(self, bundle: ContextBundle) -> AgentDecision | None:
        """一次完整认知调用：取上下文 → 调模型 → 严格校验 → AgentDecision。"""
        if not self.usable:
            return None

        started = time.perf_counter()
        self.calls += 1
        raw = self.llm.chat_multimodal(
            state=bundle.state,
            image=bundle.image,
            user_query=bundle.user_query,
            extra=bundle.extra,
            history=bundle.history,
        )
        latency = time.perf_counter() - started
        self.last_latency = latency

        action = Action.from_llm(
            raw,
            max_chars=self.max_chars,
            now=bundle.elapsed,
            ttl=self.action_ttl_s,
            tool_calls=list(getattr(self.llm, "last_tool_calls", []) or []),
        )
        if action is None:
            self.parse_failures += 1
            LOG.warning("认知输出未通过 schema 校验，本轮退回规则基线（%s）", bundle.describe())
            return None

        gaps: list[str] = []
        if bundle.image is None and self.request_visual_when_blind:
            gaps.append("无实时画面")
        if bundle.state.confidence.localization_confidence < 0.6:
            gaps.append("定位置信度低")
        if not bundle.state.environment.front_clear and bundle.state.environment.front_distance > 3.0:
            gaps.append("前方不可通行原因不明")

        # ★Stage 10★ 主动感知的判定也进"信息缺口"，并单独抬升 needs_visual。
        #   为什么不能只看模型有没有吐 REQUEST_VISUAL：模型常常"看不清也照样瞎说一句"。
        #   策略层的判定是**独立于模型输出**的旁证，两者取或，才能真正表达"我现在信息不足"。
        vn = bundle.visual_need or {}
        need_visual = bool(vn.get("needed"))
        if need_visual:
            reasons = "、".join(vn.get("reasons") or [])
            gap = f"需要视觉（{reasons}）" if reasons else "需要视觉"
            if gap not in gaps:
                gaps.append(gap)

        return AgentDecision(
            action=action,
            rationale=f"{action.reason}",
            information_gaps=gaps,
            needs_visual=bool(
                (gaps and action.action_type is ActionType.REQUEST_VISUAL) or need_visual
            ),
            model=str(getattr(self.llm, "model", "")),
            latency_s=latency,
            prompt_chars=bundle.prompt_chars,
            image_attached=bundle.image is not None,
        )

    # -----------------------------------------------------------------
    def reset(self) -> None:
        self.calls = 0
        self.parse_failures = 0
        self.last_latency = 0.0

    def stats(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "usable": self.usable,
            "calls": self.calls,
            "parse_failures": self.parse_failures,
            "last_latency_s": round(self.last_latency, 3),
            "action_ttl_s": self.action_ttl_s,
            "context": self.context.stats(),
        }


__all__ = ["CognitiveAgent"]
