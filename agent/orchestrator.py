"""双循环编排器（Orchestrator）—— 快循环 / 慢循环的合流点。

任务书第八节把这一层列为"最重要的架构升级之一"，因为它同时解决三个问题：

    1. **禁止每帧调用 LLM**（成本 + 延迟 + 实时性）；
    2. **安全不能等大模型**（强制验收：LLM sleep 5 秒，Safety Loop 必须继续工作）；
    3. **没有变化时 Agent 静默**（事件驱动，而不是"每秒重新思考整个世界"）。

┌─ 快循环 Fast Loop（本进程主线程，每 tick）────────────────────────────┐
│  Sensor → Fusion → Event Engine → **SafetyEngine.evaluate()**         │
│  → 与规则基线合成 → 执行 → 世界推进                                    │
│  纯计算、不联网、不等 IO —— 所以 LLM 再慢也不影响它。                   │
└───────────────────────────────────────────────────────────────────────┘
┌─ 慢循环 Cognitive Loop（工作线程，事件驱动 + 心跳）────────────────────┐
│  SpatialState 快照 + 事件 + 用户提问 → 多模态大模型 → Action           │
│  只产出 Action，由主线程在下一轮 poll 取用。                            │
└───────────────────────────────────────────────────────────────────────┘

★为什么必须是真线程，而不是"把 timeout 调小"★
    单线程里 LLM 调用无论超时多短，那段时间主循环都是停着的。
    "停下来的安全层"对盲人用户等于不存在 —— 3 秒够走 3 米，够撞上去。
    所以 LLM 必须离开主线程。`time.sleep()` 与网络阻塞都会释放 GIL，
    快循环因此能继续按自己的节奏跑。

★线程安全约定：单写者 + 快照传递★
    - **主线程是唯一修改** WorldModel / StateManager / memory / 模拟器的线程；
    - ★Stage 8 修正★ 交给工作线程的不再是"裸状态 + 裸事件"，而是主线程用
      `ContextBuilder.build()` 打好的 **ContextBundle**。原因是 v0.2→v0.3 期间
      ContextBuilder 要读 memory（deque）/ 事件存档（deque）/ 相机缓存，
      这三个都是主线程独占写；在工作线程里边遍历边被 append 会抛
      `RuntimeError: deque mutated during iteration`。把它们统一在主线程读完，
      跨线程传递的就全是**字符串、字节、字典副本**，不需要任何业务锁。
    - 工作线程只写自己的 `_result` / `last_decision`，主线程 `poll()` 取走并清空。
    - ★Stage 8 新增★ 认知结果在合成前要过 `InteractionPolicy.gate()`：
      大模型说什么由它决定，**能不能说出口由策略决定**。
    因此不存在两个线程同时改一个业务对象的情况。
    工具层仍有极小概率与主线程争用模拟器，用 `state_lock` 串行化（见 `AgentTools`）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent.decision import Action, ActionType
from agent.safety.safety_engine import SafetyEngine, SafetyVerdict
from events.event_types import AgentEvent, EventType, Severity

LOG = logging.getLogger("bsa.orchestrator")


class CognitiveTrigger:
    """决定"这一轮值不值得让大模型想一次"。"""

    #: 这些事件出现时值得花一次推理（其余事件只进历史，不触发）
    IMPORTANT: frozenset[EventType] = frozenset(
        {
            EventType.USER_COMMAND,
            EventType.USER_QUESTION,
            EventType.OBSTACLE_APPEARED,
            EventType.OBSTACLE_CLEARED,
            EventType.OBSTACLE_APPROACHING,
            EventType.ROUTE_DEVIATION,
            EventType.ROUTE_BLOCKED,
            EventType.GOAL_REACHED,
            EventType.TURN_APPROACHING,
            EventType.MAP_SENSOR_CONFLICT,
            EventType.LOW_LOCALIZATION_CONFIDENCE,
            EventType.CAMERA_LOST,
            EventType.CAMERA_RECOVERED,
            EventType.HIGH_RISK,
        }
    )

    def __init__(self, cfg: dict[str, Any]) -> None:
        c = cfg.get("cognitive", {}) or {}
        self.enabled: bool = bool(c.get("enabled", True))
        self.heartbeat_s: float = float(c.get("heartbeat_s", 5.0))
        self.min_interval_s: float = float(c.get("min_interval_s", 2.0))

    def should_run(
        self,
        *,
        events: list[AgentEvent],
        user_query: str | None,
        t: float,
        last_run: float,
        thinking: bool,
    ) -> bool:
        if not self.enabled or thinking:
            return False
        if user_query:  # 用户问了话，必须想
            return True
        if t - last_run < self.min_interval_s:  # 至少间隔，防止事件风暴
            return False
        if any(e.event_type in self.IMPORTANT for e in events):
            return True
        # 心跳：什么都没有也要偶尔确认一下世界还在预期内
        return (t - last_run) >= self.heartbeat_s


@dataclass
class _Job:
    """一份交给工作线程的**只读快照**（主线程组装完毕后不再被任何人修改）。"""

    bundle: Any  # ContextBundle：状态副本 + 历史副本 + 图字节 + 事件摘要
    submitted_at: float
    tick: int


class CognitiveLoop:
    """低频认知循环：独立线程 + 快照输入 + 结果回投。"""

    def __init__(self, cfg: dict[str, Any], agent: Any) -> None:
        self.cfg = cfg
        self.agent = agent
        self.trigger = CognitiveTrigger(cfg)
        c = cfg.get("cognitive", {}) or {}
        # 结果的有效期：算完 4 秒后才知道"前方有椅子"，那已经晚了
        self.result_ttl_s: float = float(c.get("result_ttl_s", 4.0))
        self.enabled: bool = self.trigger.enabled

        self._lock = threading.Lock()
        self._pending: _Job | None = None
        self._result: Action | None = None
        self._result_at: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        #: 最近一次成功的认知产出（含 information_gaps / needs_visual，Stage 10 要用）
        self.last_decision: Any | None = None
        self.thinking: bool = False
        self.runs: int = 0
        self.dropped: int = 0
        self.failures: int = 0
        self.last_latency: float = 0.0
        self.last_run_at: float = -1e9
        self.last_bundle_desc: str = ""

    # -----------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="bsa-cognitive", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            th.join(timeout=timeout)
        self._thread = None

    # -----------------------------------------------------------------
    # 主线程侧
    # -----------------------------------------------------------------
    def should_think(self, events: list[AgentEvent], user_query: str | None, t: float) -> bool:
        with self._lock:
            thinking = self.thinking
        return self.trigger.should_run(
            events=events, user_query=user_query, t=t, last_run=self.last_run_at, thinking=thinking
        )

    def submit(
        self,
        state: Any,
        events: list[AgentEvent],
        user_query: str | None,
        t: float,
        tick: int,
    ) -> bool:
        """在主线程把一份上下文快照交给工作线程。**非阻塞**：无论 LLM 多慢都立刻返回。"""
        if not self.enabled:
            return False
        # ★主线程组装上下文★ 见模块 docstring：memory / events / camera 都是
        # 主线程独占写的对象，只能在这里读；打完包后工作线程不再碰它们。
        try:
            bundle = self.agent.build_context(
                state.model_copy(deep=True),
                elapsed=t,
                user_query=user_query,
                events=list(events),
            )
        except Exception as e:  # noqa: BLE001 - 组装失败就这一轮不思考，不能影响快循环
            self.failures += 1
            LOG.warning("组装认知上下文失败，本轮跳过思考：%s", e)
            with self._lock:
                self.thinking = False
            return False
        job = _Job(bundle=bundle, submitted_at=t, tick=tick)
        with self._lock:
            if self._pending is not None:
                self.dropped += 1  # 只保留最新一帧：过时的世界不值得再想
            self._pending = job
            self.thinking = True
        self.last_run_at = t
        self.last_bundle_desc = bundle.describe()
        return True

    def poll(self, t: float) -> Action | None:
        """取回认知结果（若有且未过期）。取走即清空。

        `t` 与 `result_ttl_s` 都是**仿真秒**（见 `_run` 里的量纲说明），
        这样"取用"与 `InteractionPolicy` 的 `expires_at` 判定用的是同一把尺子。
        """
        with self._lock:
            action, at = self._result, self._result_at
            self._result = None
        if action is None:
            return None
        if t - at > self.result_ttl_s:
            LOG.debug("认知结果已过期（提交后 %.1fs），丢弃", t - at)
            self.dropped += 1
            return None
        return action

    # -----------------------------------------------------------------
    # 工作线程侧
    # -----------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                job, self._pending = self._pending, None
            if job is None:
                self._stop.wait(0.02)
                continue

            started = time.perf_counter()
            try:
                decision = self.agent.think_bundle(job.bundle)
            except Exception as e:  # noqa: BLE001 - 认知失败绝不能影响快循环
                self.failures += 1
                LOG.warning("认知循环异常，本轮放弃（快循环不受影响）：%s", e)
                decision = None
            self.last_latency = time.perf_counter() - started
            self.runs += 1

            with self._lock:
                if decision is not None:
                    self.last_decision = decision
                    self._result = decision.action
                    # ⚠️ 只用**仿真时刻**打时间戳，不要把 `last_latency`（墙钟秒）加进来。
                    #    踩过的坑：`submitted_at + latency` 是"仿真秒 + 墙钟秒"的量纲混用。
                    #    仿真步长与真实经过时间一旦不相等（测试、快循环空转、回放模式都会），
                    #    就会把"没过期"的结论判成过期，表现为"模型明明答了却永远不生效"。
                    #    语义上也该如此：结论描述的是**提交那一刻**的世界，
                    #    所以新鲜度就该按"距提交过了多少个仿真秒"来算。
                    self._result_at = job.submitted_at
                self.thinking = False

    # -----------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._pending = None
            self._result = None
            self._result_at = 0.0
            self.thinking = False
            self.last_decision = None
        self.runs = 0
        self.dropped = 0
        self.failures = 0
        self.last_latency = 0.0
        self.last_run_at = -1e9
        self.last_bundle_desc = ""

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "runs": self.runs,
            "thinking": self.thinking,
            "dropped": self.dropped,
            "failures": self.failures,
            "last_latency_s": round(self.last_latency, 3),
            "heartbeat_s": self.trigger.heartbeat_s,
            "min_interval_s": self.trigger.min_interval_s,
            "last_context": self.last_bundle_desc,
        }


@dataclass
class OrchestratorResult:
    """一轮决策的产出（含各层的痕迹，便于诊断与录制）。"""

    action: Action
    verdict: SafetyVerdict
    llm_used: bool = False
    awaiting_cognitive: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class AgentOrchestrator:
    """把安全层、认知循环、规则基线合成唯一一个 Action。

    合成优先级（高 → 低）：

        1. **安全层 EMERGENCY**：必须干预。规则基线在同样条件下本来就会拦，
           所以直接采用基线动作 —— 安全层的价值是"保证拦得住且不等 LLM"，
           而不是换一套话术（换话术只会让同一场景的播报前后不一致）。
        2. **认知循环结果**（若在有效期内）：理解与表达交回大模型。
        3. **规则基线**：确定性兜底，任何时候都在跑。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        agent: Any,
        *,
        safety: SafetyEngine | None = None,
        query_timeout_s: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.agent = agent
        self.safety = safety or SafetyEngine(cfg)
        self.cognitive = CognitiveLoop(cfg, agent)
        c = cfg.get("cognitive", {}) or {}
        self.query_timeout_s = float(
            query_timeout_s if query_timeout_s is not None else c.get("query_timeout_s", 3.0)
        )
        self._query_t0: float | None = None

    # -----------------------------------------------------------------
    def start(self) -> None:
        self.cognitive.start()

    def stop(self) -> None:
        self.cognitive.stop()

    # -----------------------------------------------------------------
    def decide(
        self,
        state: Any,
        events: list[AgentEvent],
        t: float,
        tick: int,
        user_query: str | None = None,
    ) -> OrchestratorResult:
        """快循环每轮调用一次。**内部的 LLM 调用是非阻塞提交**。"""
        # 惰性自启动：调用方忘了 start() 也不该导致"认知循环永远不工作"（静默失效最难查）
        self.start()

        # ① 快循环：安全判定（纯计算，不等任何 IO）
        verdict = self.safety.evaluate(state, events, t)

        # ② 取出上一轮认知循环的成果（可能没有 / 可能已过期）
        cognitive = self.cognitive.poll(t)

        # ③ 规则基线（确定性）
        baseline = self.agent.baseline(state, t, user_query if cognitive is None else None)

        # ④ 用户提问：交给认知循环回答，短暂等待；超时则规则兜底抢答
        if user_query and self.cognitive.enabled:
            if self._query_t0 is None:
                self._query_t0 = t
            self.cognitive.submit(state, events, user_query, t, tick)
            if cognitive is None and (t - self._query_t0) < self.query_timeout_s:
                waiting = Action(
                    action_type=ActionType.CONTINUE,
                    message="",
                    urgency="low",
                    reason=f"等待认知循环回答用户提问（已等 {t - self._query_t0:.1f}s）",
                    source="rule",
                )
                self.agent.last_action = waiting
                return OrchestratorResult(
                    action=waiting,
                    verdict=verdict,
                    awaiting_cognitive=True,
                    meta={"query": user_query},
                )

        # ⑤ 合成
        gate_code = "n/a"
        if verdict.intervene:
            action = baseline
            llm_used = False
        elif cognitive is not None:
            # ★Stage 8：表达审查★ 模型决定"说什么"，交互策略决定"能不能说出口"。
            #   顺序很关键：先审查表达，再让安全层做最终仲裁 —— 否则"被静音"的
            #   行动带着完整的 message 进入仲裁，安全层看到的就是一个它读不懂的对象。
            gated = self.agent.gate_expression(
                cognitive,
                state,
                t,
                events,
                # ★Stage 11★ 本轮取用的认知结果，是不是在回答用户提问？
                #   `_query_t0` 在提问那一轮被置位、在消费结果这一轮被清空，
                #   所以它非 None 恰好等价于"这条结论是给用户问题的答复"。
                #   是的话放行"最小播报间隔/重复"两道闸 —— 否则会出现
                #   "画面取了、模型答了、用户一个字没听到"（见 InteractionPolicy.gate）。
                answering_user=self._query_t0 is not None,
            )
            gate = self.agent.last_gate
            gate_code = getattr(gate, "code", "n/a")
            # ★不可信的结论不得拥有物理否决权★
            #   `expired` / `low_confidence` 意味着"这条结论本身已经不可信"，
            #   此时必须**整条退回规则基线**。只清空话术是不够的：
            #   一个 permits_motion=False 的过期 SAFETY_ALERT 会把用户永久钉在原地。
            #   其它 code（repeated / too_frequent / quiet_cruise / user_speaking /
            #   channel_disabled / silent）只是"此刻不方便说"，结论仍然有效，
            #   所以保留行动、只清话术。
            if gate is not None and gate.discard:
                if gated.message:
                    LOG.info("[认知层·仅记录·%s] %s", gate.code, gated.message)
                action = baseline
                llm_used = False
            else:
                action = self.agent._merge_with_safety(baseline, gated)  # noqa: SLF001
                llm_used = True
                self.agent.llm_used_count += 1
            self._query_t0 = None
        else:
            action = baseline
            llm_used = False
            if user_query:
                self._query_t0 = None

        # ⑥ 提交下一轮思考（事件驱动 + 心跳）
        if self.cognitive.should_think(events, user_query, t):
            self.cognitive.submit(state, events, user_query, t, tick)

        self.agent.last_action = action
        return OrchestratorResult(
            action=action,
            verdict=verdict,
            llm_used=llm_used,
            meta={
                "baseline": baseline.action_type.value,
                "safety": verdict.level.value,
                "gate": gate_code,
            },
        )

    def reset(self) -> None:
        self.safety.reset()
        self.cognitive.reset()
        self._query_t0 = None

    def stats(self) -> dict[str, Any]:
        return {
            "safety": self.safety.stats(),
            "cognitive": self.cognitive.stats(),
        }


__all__ = [
    "AgentOrchestrator",
    "CognitiveLoop",
    "CognitiveTrigger",
    "OrchestratorResult",
]
