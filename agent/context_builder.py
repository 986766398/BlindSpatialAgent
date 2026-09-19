"""上下文构建器（Context Builder）—— 受控的信息面。

任务书第九/十节要求"Cognitive Agent 的输入 = SpatialState 摘要 + 重要历史 +
active events + 必要图片 + user intent"，并且**不让模型看到原始传感器数据**。

本模块就是这条边界线的实现处：把"模型能看什么"收敛成一个可审计的函数，
而不是散落在 Agent 里到处 `f"{...}"` 拼字符串。

    ┌─ 允许进提示词 ────────────────────────────────────────┐
    │  SpatialState 渲染（结构化摘要，非原始帧）              │
    │  事件摘要（Stage 6 的产出，模型终于能看到"刚才发生了什么"）│
    │  安全层判定（Stage 7 的产出：现在有多危险）              │
    │  世界模型摘要 + 剩余路线 + 上一次说了什么                │
    │  一张最新的第一视角图（若链路新鲜）                       │
    └───────────────────────────────────────────────────────┘
    ┌─ 明确不进提示词 ──────────────────────────────────────┐
    │  UWB/IMU/LiDAR 的原始读数与噪声参数                     │
    │  其他候选行动、工具调用日志、内部阈值                    │
    └───────────────────────────────────────────────────────┘

★为什么要显式"绑定"而不是构造注入★
    ContextBuilder 需要 EventEngine 与 SafetyEngine，而它们由 `SpatialAgentSystem`
    在 Agent 之后创建（装配顺序问题）。用 `bind()` 后置绑定可以避免构造参数打结，
    也避免 `agent_core` ↔ `events` 之间的循环导入。

★线程约定（Stage 8 复核过，很重要）★
    `build()` **只在主线程（快循环）被调用** —— 它要读 memory / events / camera
    这几个"主线程独占写"的对象。调用方（`CognitiveLoop.submit`）在主线程把结果
    打成 `ContextBundle` 快照后才交给工作线程，工作线程只拿这个 bundle 去调大模型。
    ⚠️ 不要在 `CognitiveAgent` 里反过来调 `build()`：`deque` 边遍历边 append 会抛
       `RuntimeError: deque mutated during iteration`，而这类竞态只在真并发下偶发。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.active_perception import VisualNeed, VisualRequestPolicy
from spatial.spatial_state import SpatialState

LOG = logging.getLogger("bsa.context")


@dataclass
class ContextBundle:
    """一次认知调用的全部输入（可完整落盘，用于 Replay 与事后归因）。"""

    state: SpatialState
    elapsed: float = 0.0
    user_query: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)
    image: bytes | None = None
    safety: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)
    prompt_chars: int = 0
    #: ★Stage 10★ 本轮"该不该看"的判定（诊断 / 录制用，**不进提示词**）
    visual_need: dict[str, Any] | None = None

    def describe(self) -> str:
        parts = [
            f"t={self.elapsed:.1f}s",
            f"tick={self.state.tick}",
            f"image={'有' if self.image else '无'}",
            f"events={len(self.events)}",
            f"history={len(self.history)}",
            f"safety={self.safety.get('level') if self.safety else 'none'}",
        ]
        if self.visual_need and self.visual_need.get("needed"):
            parts.append(f"vision={'+'.join(self.visual_need.get('reasons') or [])}")
        if self.user_query:
            parts.append(f"query={self.user_query!r}")
        return " ".join(parts)


class ContextBuilder:
    """把当前状态 + 各层结论组织成"模型能看的那一面"。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        world: Any,
        memory: Any,
        camera: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.world = world
        self.memory = memory
        self.camera = camera
        self.events: Any | None = None
        self.safety: Any | None = None
        self.policy: Any | None = None
        # ★Stage 10★ 主动感知：决定"这一轮要不要一张新画面"（见 agent/active_perception.py）
        self.perception = VisualRequestPolicy.from_cfg(cfg)
        c = cfg.get("cognitive", {}) or {}
        self.event_summary_n = int(c.get("event_summary_n", 5))
        self.history_n = int(c.get("history_n", 6))

    # -----------------------------------------------------------------
    def bind(
        self,
        *,
        events: Any = None,
        safety: Any = None,
        policy: Any = None,
        perception: Any = None,
    ) -> None:
        """后置绑定事件引擎 / 安全引擎 / 交互策略（装配顺序决定它们晚于本对象存在）。"""
        if events is not None:
            self.events = events
        if safety is not None:
            self.safety = safety
        if policy is not None:
            self.policy = policy
        if perception is not None:
            self.perception = perception

    # -----------------------------------------------------------------
    def _recent_events(self, elapsed: float, events: list[Any] | None) -> list[Any]:
        """收集"最近一小段时间内"的事件对象，供主动感知判定。

        ⚠️ 光看本轮事件会**大量漏判**：认知循环本身是节流的（事件驱动 + 心跳），
           事件很可能发生在两次认知调用之间。所以向事件引擎回捞一个窗口，
           窗口长度是配置项（`agent.perception.event_window_s`），不是魔法数字。
        """
        out: list[Any] = list(events or [])
        eng = self.events
        if eng is not None and hasattr(eng, "active_since"):
            try:
                out.extend(
                    eng.active_since(max(0.0, elapsed - self.perception.event_window_s))
                )
            except Exception as e:  # noqa: BLE001 - 回捞失败不影响本轮上下文
                LOG.debug("回捞最近事件失败：%s", e)
        return out

    # -----------------------------------------------------------------
    def take_image(self, state: SpatialState) -> bytes | None:
        """取一张最新画面。

        ⚠️ 用 `latest_bytes()`（**过期必须返回 None**），而不是 `preview_bytes()`
           —— 后者是给网页预览用的，过期也会返回旧图。
           把过期图喂给模型 = 让它根据 30 秒前的世界下判断，比不给图更糟。
        """
        if self.camera is None or not state.camera.image_available:
            return None
        try:
            return self.camera.latest_bytes()
        except Exception as e:  # noqa: BLE001 - 取图失败必须降级为"无图"，不能中断认知
            LOG.warning("取最新画面失败，本次按无图处理：%s", e)
            return None

    # -----------------------------------------------------------------
    def build(
        self,
        state: SpatialState,
        *,
        elapsed: float,
        user_query: str | None = None,
        events: list[Any] | None = None,
        safety_verdict: Any | None = None,
        with_image: bool = True,
        force_visual: bool = False,
    ) -> ContextBundle:
        last = self.memory.last_utterance() if self.memory is not None else None
        extra: dict[str, Any] = {
            "world": self.world.summarize() if self.world is not None else "",
            "route": [str(p.name) for p in state.navigation.current_route[1:]][:5],
            "last_said": last.text if last is not None else None,
        }

        ev_text = ""
        ev_list: list[str] = []
        if self.events is not None:
            # 主线程读自己的事件引擎：优先用**存档摘要**（"最近发生了什么"），
            # 而不是本轮 publish 出来的那几条（本轮常常是空的）。
            ev_text = self.events.summary(self.event_summary_n)
            ev_list = [line.lstrip("- ").split("（")[0] for line in ev_text.splitlines() if line.strip()]
        elif events:
            ev_list = [e.describe() for e in events]
            ev_text = "\n".join(f"- {t}" for t in ev_list)
        if ev_text:
            extra["events"] = ev_text

        safety_dict: dict[str, Any] | None = None
        verdict = safety_verdict
        if verdict is None and self.safety is not None:
            # 调用方没传就取安全层的最后一次判定缓存（主线程写、主线程读，无竞态）
            verdict = getattr(self.safety, "last_verdict", None)
        if verdict is not None:
            safety_dict = verdict.as_dict() if hasattr(verdict, "as_dict") else dict(verdict)
            extra["safety"] = safety_dict

        if self.policy is not None:
            try:
                extra["interaction"] = {
                    "phase": self.policy.observe(state, elapsed),
                    "cruise_s": round(self.policy.cruise_seconds(elapsed), 1),
                }
            except Exception as e:  # noqa: BLE001
                LOG.debug("取交互状态失败：%s", e)

        history = self.memory.to_llm_messages(self.history_n) if self.memory is not None else []

        # ★Stage 10 主动感知★ 先判定"这一轮该不该看"，再决定要不要去读相机。
        #   判定是纯函数（不碰相机、不落盘），所以可以放心地对每一轮都算。
        need = self.perception.evaluate(
            state,
            elapsed=elapsed,
            events=self._recent_events(elapsed, events),
            user_query=user_query,
            force=force_visual,
        )
        # `always` / `auto` 模式下只要有新鲜帧就附（与 v0.2 行为一致，保证
        # 已验收的多模态链路不受影响）；`on_demand` 只在真的需要时才附。
        want_image = with_image and (self.perception.attaches_unconditionally or need.needed)

        bundle = ContextBundle(
            state=state,
            elapsed=elapsed,
            user_query=user_query,
            extra=extra,
            history=history,
            image=self.take_image(state) if want_image else None,
            safety=safety_dict,
            events=ev_list,
            visual_need=need.as_dict(),
        )
        # 粗略估算送入模型的文本量（诊断用：提示词膨胀会直接抬高延迟）
        try:
            from agent.prompt_template import build_user_prompt

            bundle.prompt_chars = len(build_user_prompt(state, user_query, extra))
        except Exception:  # noqa: BLE001
            bundle.prompt_chars = 0
        return bundle

    def stats(self) -> dict[str, Any]:
        return {
            "event_summary_n": self.event_summary_n,
            "history_n": self.history_n,
            "camera_linked": self.camera is not None,
            "events_linked": self.events is not None,
            "safety_linked": self.safety is not None,
            "perception": self.perception.stats(),
        }


__all__ = ["ContextBuilder", "ContextBundle"]
