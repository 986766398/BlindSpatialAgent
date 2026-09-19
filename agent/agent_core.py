"""Agent 核心：决策器 + 完整运行时。

两层结构：
    SpatialAgent        只做决策：状态 + 用户输入 -> Action（不含任何模拟器）
    SpatialAgentSystem  完整运行时：感知前端 -> 状态融合 -> Agent 决策 -> 行动 -> 世界推进

主循环（与真实机器人控制循环同构）：
    感知 Perception（SensorProvider）
      ↓
    空间状态融合 Spatial State Fusion
      ↓
    Agent Reasoning（多模态大模型 + 工具调用，失败则规则兜底）
      ↓
    Action Decision
      ↓
    世界推进（WorldStepper：用户是否被允许前进）
      ↓
    下一轮

★v0.3 Stage 3：本文件是全项目**唯一**装配仿真器的地方★
    其它模块（`spatial/`、`agent/tools.py`）都只认 `sensors/base.py` 的协议。
    将来接真机 / UE5 数字孪生，只需改 `SpatialAgentSystem.__init__` 里的这两行：
        self.env      = SimulatedProvider(cfg, ...)
        self.provider = self.env
    换成对应的 Provider 即可，下游一行不动。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.action_schema import Action, ActionChannel, ActionType, AgentDecision, nav_phase
from agent.cognitive_agent import CognitiveAgent
from agent.context_builder import ContextBuilder, ContextBundle
from agent.decision import RuleDecisionEngine
from agent.interaction_policy import InteractionPolicy
from agent.llm_client import LLMClient
from agent.memory import AgentMemory
from agent.orchestrator import AgentOrchestrator
from agent.safety import SafetyEngine
from agent.tools import AgentTools
from events.event_engine import EventEngine
from sensors.simulated import SimulatedProvider
from spatial.spatial_state import SpatialState
from spatial.state_manager import StateManager
from spatial.world_model import WorldModel

LOG = logging.getLogger("bsa.agent")


@dataclass
class StepResult:
    """主循环一步的产出。"""

    state: SpatialState
    action: Action
    elapsed: float
    llm_used: bool = False
    safety: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "elapsed": round(self.elapsed, 2),
            "llm_used": self.llm_used,
            "state": self.state.to_prompt_dict(),
            "action": self.action.as_dict(),
        }
        if self.safety is not None:
            out["safety"] = self.safety
        return out


# =====================================================================
# 决策器
# =====================================================================
class SpatialAgent:
    """空间智能体的决策部分：多模态大模型 + 规则安全兜底。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        tools: AgentTools,
        memory: AgentMemory,
        world: WorldModel,
        llm: LLMClient | None = None,
        camera: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.tools = tools
        self.memory = memory
        self.world = world
        self._llm = llm
        self.camera = camera

        # ★Stage 8 的三件套★（v0.2 里这三件事全挤在 `_llm_decide` 里）
        #   策略：说不说 / 说多长 / 走哪个通道 —— 规则层与认知层**共用同一个实例**，
        #         这样"刚说过什么"的口径只有一处，不会两套阈值互相打架。
        self.policy = InteractionPolicy(cfg)
        #   规则引擎退化为"策略的消费者"：它只决定说什么内容，不再自己管闸门。
        self.rules = RuleDecisionEngine(cfg, memory, policy=self.policy)
        #   上下文构建器：模型能看到的**受控信息面**（不是原始传感器数据）。
        self.context = ContextBuilder(cfg, world, memory, camera=camera)
        #   认知智能体：一次多模态调用 → 严格校验后的 AgentDecision（失败返回 None）。
        self.cognitive = CognitiveAgent(cfg, llm, self.context)

        # 事件引擎（Stage 6 产出）：认知路径读它的摘要进提示词。
        # 用 setter 注入而不是构造参数，避免 SpatialAgent ↔ EventEngine 的循环依赖。
        self.events: Any | None = None
        #: 用户此刻是否在说话（由语音前端写入）。交互策略据此决定"别打断"。
        self.user_speaking: bool = False
        self.last_action: Action | None = None
        self.last_decision: AgentDecision | None = None
        #: 最近一次表达审查的结果（诊断用：为什么这句话没说出口）
        self.last_gate: Any | None = None
        self.llm_used_count = 0
        self.llm_fallback_count = 0

    # -----------------------------------------------------------------
    @property
    def llm(self) -> LLMClient | None:
        return self._llm

    @llm.setter
    def llm(self, value: LLMClient | None) -> None:
        """换模型必须**同时**换到认知层。

        ⚠️ 踩过的坑：只替换 `agent.llm` 而不替换 `agent.cognitive.llm` 时，
           认知路径仍在用旧客户端 —— 表现为"换了模型但延迟/输出没变"，
           在测试里则表现为"替身根本没被调用"。所以这里强制同步。
        """
        self._llm = value
        cog = getattr(self, "cognitive", None)
        if cog is not None:
            cog.llm = value

    # -----------------------------------------------------------------
    def decide(self, state: SpatialState, elapsed: float, user_query: str | None = None) -> Action:
        """【v0.2 语义】同步决策：先算规则结论做安全基线，再让大模型做理解与表达。

        v0.3 主循环已改为走 `AgentOrchestrator`（快循环 + 工作线程里的慢循环），
        本方法保留为**单线程可用路径**：自检、规则模式、以及"不需要并发"的调用方
        仍然可以直接调它，行为与 v0.2 完全一致。
        """
        baseline = self.baseline(state, elapsed, user_query)

        llm_action: Action | None = None
        # usable = 已配置 且 未熔断。用 usable 而非 available，否则 Key 失效时
        # 会以每帧一次的频率无限重试（浪费配额 + 拖慢实时性 + 刷爆日志）。
        if self.llm is not None and self.llm.usable:
            llm_action = self.think(state, elapsed, user_query, [])

        if llm_action is None:
            if self.llm is not None and self.llm.usable:
                self.llm_fallback_count += 1
            action = baseline
        else:
            gated = self.gate_expression(llm_action, state, elapsed, [])
            # 与编排器同一条规则：不可信的结论整条退回基线，不得带走"停住用户"的否决权
            if self.last_gate is not None and self.last_gate.discard:
                self.llm_fallback_count += 1
                action = baseline
            else:
                self.llm_used_count += 1
                action = self._merge_with_safety(baseline, gated)

        self.last_action = action
        return action

    # -----------------------------------------------------------------
    def baseline(self, state: SpatialState, elapsed: float, user_query: str | None = None) -> Action:
        """确定性基线：规则引擎的结论。

        ★这一层永远在跑★ 无论大模型是否可用、是否卡住。它是快循环的骨架，
        也是"安全底线不可被模型推翻"的实现位置。
        """
        self.tools.elapsed = elapsed
        # ★Stage 8★ 快循环是**唯一**推进"巡航时长"的地方（单写者）：
        # 认知层只读 cruise_seconds()，不推进它。否则工作线程的调用节奏
        # 会反过来影响"连续直行多久了"这个判据。
        self.policy.observe(state, elapsed)
        return self.rules.decide(state, elapsed, user_query)

    # -----------------------------------------------------------------
    def build_context(
        self,
        snapshot: SpatialState,
        *,
        elapsed: float,
        user_query: str | None = None,
        events: list[Any] | None = None,
    ) -> ContextBundle:
        """在主线程把"模型能看的那一面"打成快照。**必须在主线程调用。**"""
        # ★Stage 10★ 取走一次性取帧请求：模型上一次调 `request_visual_observation()`
        #   说"我要看一眼"，在这里生效，且**只生效一次**（取走即清）。
        consume = getattr(self.tools, "consume_visual_request", None)
        force_visual = bool(consume()) if callable(consume) else False
        return self.cognitive.build_context(
            snapshot,
            elapsed=elapsed,
            user_query=user_query,
            events=events,
            force_visual=force_visual,
        )

    def think(
        self,
        state: SpatialState,
        elapsed: float,
        user_query: str | None = None,
        events: list[Any] | None = None,
    ) -> Action | None:
        """【v0.2 语义】单线程认知入口：自己组上下文、自己调模型、返回 Action。

        v0.3 主循环走 `AgentOrchestrator` → `CognitiveLoop`（工作线程只调
        `think_bundle`），本方法保留给"不需要并发"的调用方（自检 / 脚本 / API 单发）。
        """
        bundle = self.build_context(
            state, elapsed=elapsed, user_query=user_query, events=events or []
        )
        decision = self.think_bundle(bundle)
        return None if decision is None else decision.action

    def think_bundle(self, bundle: ContextBundle) -> AgentDecision | None:
        """认知路径：对**已经打好的快照**做一次多模态推理。

        ⚠️ 本方法在**认知循环的工作线程**里被调用。因此：
            - 只允许读 `bundle`（其中每个字段都是主线程拷好的副本），
              不得再去碰 memory / events / camera / world；
            - 工具调用由 `AgentTools` 的锁保护（网络等待期间不持锁）；
            - 任何异常都必须就地吞掉并返回 None —— 认知失败不能影响快循环。
        """
        try:
            decision = self.cognitive.decide(bundle)
        except Exception as e:  # noqa: BLE001 - 认知失败必须降级，不能拖垮主循环
            LOG.warning("认知循环决策异常，已降级到规则基线：%s", e)
            return None
        if decision is not None:
            self.last_decision = decision
        return decision

    def _merge_with_safety(self, rule: Action, llm: Action) -> Action:
        """安全底线不可被模型推翻：规则判定"立即停下"时，模型不能让用户继续走。"""
        if rule.action_type == ActionType.WAIT and rule.urgency == "critical" and llm.permits_motion:
            LOG.warning("模型建议 %s，但规则判定为碰撞风险，采用安全决策", llm.action_type.value)
            return rule.model_copy(
                update={"reason": f"安全覆盖模型决策（模型建议 {llm.action_type.value}）：{rule.reason}"}
            )
        return llm

    # -----------------------------------------------------------------
    def apply_action(self, action: Action, elapsed: float) -> None:
        """执行行动（**所有对外副作用的唯一出口**）。

        ★为什么它长在 `SpatialAgent` 而不是 `SpatialAgentSystem` 上★
            回放（Stage 9）**不接模拟器**，自然也就没有 `SpatialAgentSystem`。
            但"重放 Agent"必须包含"说完话之后记忆要变"这一半 ——
            否则打扰频率控制、重复检测这些**带状态**的规则在回放里会全部走偏。
            把它放在决策器上，回放器和主循环就能共用同一条路径。

        架构约束：**所有对外副作用一律通过 tools.call() 这个唯一入口派发**
        （speak / ask_user / replan_route）。这样无论是大模型决策还是规则兜底，
        副作用都经过同一套参数校验、异常隔离与记账，
        也保证「Agent 不直接输出答案，必须通过工具」这条原则在规则模式下同样成立。

        ★Stage 8★ 发声判据由「有 message」升级为「`action.speaks`（有内容 且 通道能出声）」：
            控制台告警（CONSOLE_ALERT）是"记一笔但不打扰用户"，绝不能被念出来。
            规则基线的 Action 只要 message 非空，通道必然是 VOICE ⇒ 行为与 v0.2 一致。
        """
        self.tools.elapsed = elapsed
        if action.speaks and not self.memory.repeated(action.message):
            if action.action_type == ActionType.ASK_USER:
                res = self.tools.call("ask_user", {"question": action.message})
            else:
                res = self.tools.call("speak", {"message": action.message, "urgency": action.urgency})
            action.tool_calls.append({"tool": action.action_type.value.lower(), "result": res})
            LOG.info("播报[%s] %s", action.action_type.value, action.message)
        elif action.message:
            channel = (action.channel or ActionChannel.SILENT).value
            LOG.info("[仅记录·%s] %s", channel, action.message)

        if action.message:
            self.memory.add_agent(action.message, elapsed, action.action_type.value)

        if action.action_type == ActionType.REPLAN:
            res = self.tools.call("replan_route", {"reason": action.reason or "Agent 触发重规划"})
            action.tool_calls.append({"tool": "replan_route", "result": res})

    def gate_expression(
        self,
        action: Action,
        state: SpatialState,
        elapsed: float,
        events: list[Any] | None = None,
        *,
        answering_user: bool = False,
    ) -> Action:
        """交互策略对**认知层产出**做表达审查：说不说 / 说多长 / 走哪个通道。

        ⚠️ 规则基线**不经过**这里 —— 它的两道闸已经在 `InteractionPolicy.allow()`
           里实现了（语义与 v0.2 一致）。这里多出来的闸门（巡航静默、用户正在说话、
           结果过期、置信度不足、通道未启用）**只作用于大模型输出**，
           所以 120 轮确定性回归快照不会因为本层而改变。

        ⚠️ 调用方必须检查 `self.last_gate.discard`：
           返回的 Action 已经"清过话术"，但 `expired` / `low_confidence` 这两类
           是**结论本身不可信**，必须整条退回规则基线，否则一个
           `permits_motion=False` 的过期行动会继续把用户钉在原地。
           （参考 `AgentOrchestrator.decide()` 里的用法。）
        """
        event_types: tuple[str, ...] = tuple(
            e.event_type.value if hasattr(e, "event_type") else str(e) for e in (events or [])
        )
        decision = self.policy.gate(
            action,
            elapsed=elapsed,
            memory=self.memory,
            risk_level=state.risk.level,
            phase=nav_phase(state),
            user_speaking=self.user_speaking,
            event_types=event_types,
            # ★Stage 11★ 这条行动是不是在回答用户刚刚的提问。
            #   是的话，"别打扰"两道闸让路（应所求 ≠ 打扰）；见 InteractionPolicy.gate()。
            answering_user=answering_user,
        )
        gated = self.policy.apply(decision, action)
        self.last_gate = decision
        if not decision.speak:
            LOG.info(
                "表达被拦下（%s）：%s ← %r",
                decision.code, decision.reason, action.message[:20] if action.message else "",
            )
        return gated


# =====================================================================
# 完整运行时
# =====================================================================
@dataclass
class SystemConfig:
    """运行时装配参数。"""

    seed: int | None = None
    enable_obstacles: bool = True
    enable_llm: bool = True


class SpatialAgentSystem:
    """把感知前端、状态融合、Agent 串成一个可持续运行的系统。

    可注入点（为后续接真机 / UE5 预留）：
        provider  传自定义 `SensorProvider`（默认 `SimulatedProvider`）
        stepper   传自定义 `WorldStepper`（默认为同一个 SimulatedProvider）
        camera    传 `CameraProvider`（真实实现是 iPhone 推流接收器）

    ★这是全项目唯一认识 `simulator/*` 的地方★
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        camera: Any | None = None,
        options: SystemConfig | None = None,
        provider: Any | None = None,
        stepper: Any | None = None,
        recorder: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.options = options or SystemConfig()
        # ★Stage 9★ 可选录制器（`recording.SessionRecorder`，鸭子类型）。
        #   默认 None ⇒ 与 v0.2 逐位一致；挂了也只在录制器内部吞掉异常。
        self.recorder = recorder

        # --- 感知前端（未来替换为真实硬件：只改这一段） ---
        # SimulatedProvider 同时满足 SensorProvider 与 WorldStepper 两个协议。
        # 仿真里感知与推世界用的是同一套配置与 RNG，硬拆成两个对象反而更耦合。
        if provider is None:
            provider = SimulatedProvider(
                cfg,
                seed=self.options.seed,
                enable_obstacles=self.options.enable_obstacles,
                camera=camera,
            )
        self.provider: Any = provider
        self.env: Any = stepper or provider

        # --- 状态融合 ---
        self.world = WorldModel(max_events=int(cfg["agent"]["memory"]["keep_events"]), cfg=cfg)
        map_snap = provider.map_snapshot()
        self.world.bind_map(map_snap)  # 装载长期空间知识（静态，一次即可）
        if self.recorder is not None:
            # 地图必须随会话落盘：回放时没有模拟器，`zone_at()` 只能靠这份快照
            self.recorder.attach_map(map_snap)
        self.camera = camera
        self.state_manager = StateManager(cfg, self.provider, world=self.world, camera=camera)

        # --- Agent ---
        # ★v0.3 Stage 7：快循环与工作线程（认知循环）会同时触碰模拟器与工具。
        #   一把可重入锁把「工具执行 / 主循环推进」串行化；锁只在计算的那几毫秒内
        #   持有，绝不跨越网络等待，所以 LLM 再慢也挡不住快循环。
        self.state_lock = threading.RLock()
        self.memory = AgentMemory(cfg)
        # --- 事件引擎（Stage 6）：融合层与 Agent 之间的那一段 ---
        # 每轮 step() 里在融合之后调用 process(state)：状态迁移 → 事件 → 总线。
        self.events = EventEngine(cfg, self.world)
        self.tools = AgentTools(
            cfg, self.provider, self.env, self.state_manager, self.memory, self.world, camera,
            lock=self.state_lock,
        )
        self.llm: LLMClient | None = LLMClient(cfg, self.tools) if self.options.enable_llm else None
        if self.llm is not None and self.recorder is not None:
            # 大模型调用发生在认知工作线程里，录制器内部自带锁（见 session_recorder）
            self.llm.recorder = self.recorder
        self.agent = SpatialAgent(cfg, self.tools, self.memory, self.world, llm=self.llm, camera=camera)
        self.agent.events = self.events

        # --- 双循环编排（Stage 7）：安全快循环 + 认知慢循环 ---
        # 没有大模型时把认知循环整个关掉：进程内不建线程，行为与 v0.2 规则模式逐轮一致。
        self.safety = SafetyEngine(cfg)
        # ★Stage 8：把 Stage 6/7 的产出接到"模型能看到的那一面"上★
        # ContextBuilder 是在 Agent 之前构造的，此时 events/safety 还不存在，
        # 所以只能后置绑定（这样也顺带避免了 agent_core ↔ events 的循环导入）。
        self.agent.context.bind(
            events=self.events, safety=self.safety, policy=self.agent.policy
        )
        self.orchestrator = AgentOrchestrator(cfg, self.agent, safety=self.safety)
        if self.llm is None:
            self.orchestrator.cognitive.enabled = False
        self._orchestrator_started = False

        # --- 循环状态 ---
        self.elapsed: float = 0.0
        self.tick: int = 0
        self.history: list[StepResult] = []
        self._pending_query: str | None = None
        self.finished: bool = False

    # -----------------------------------------------------------------
    # v0.2 兼容层
    # -----------------------------------------------------------------
    # v0.2 的 `system.map` / `system.nav` / `system.obstacles` / `system.sensors`
    # 被 main.py、api/websocket_server.py 与自检大量使用（含 `system.nav.pos`、
    # `system.sensors.drop_rate()` 这类调试读法）。为不破坏它们，这里保留转发。
    #
    # ⚠️ 新增业务逻辑**不要**用这几个属性 —— 它们让调用方重新认识模拟器，
    #    正是 Stage 3 要消除的耦合。新代码请用 `self.provider` / `self.env`。
    @property
    def map(self) -> Any:
        return getattr(self.env, "map", None)

    @property
    def nav(self) -> Any:
        return getattr(self.env, "nav", None)

    @property
    def obstacles(self) -> Any:
        return getattr(self.env, "obstacles", None)

    @property
    def sensors(self) -> Any:
        return getattr(self.env, "sensors", None)

    def _zone_of(self, state: SpatialState) -> str | None:
        """安全取区域名（录制用）。provider 没这能力时返回 None —— **绝不抛异常**。

        录制是"旁路"，绝不能因为它把主循环带崩。
        """
        try:
            return self.provider.zone_at(state.user.position.x, state.user.position.y)
        except Exception:  # noqa: BLE001
            return None

    # -----------------------------------------------------------------
    # 主循环
    # -----------------------------------------------------------------
    def step(self, dt: float | None = None) -> StepResult:
        """推进一轮：感知 -> 融合 -> 事件 -> 安全(快) -> 决策 -> 行动 -> 世界推进。

        ★快循环的保证★ 本方法内**不存在任何可能长时间阻塞的调用**：
        大模型推理被提交给认知工作线程（`orchestrator.decide` 内部只是非阻塞 submit），
        本轮行动先由安全层 + 规则基线给出，认知结果在**后续轮次**被取用。
        因此"LLM 卡住 5 秒"不会让这一轮变慢，安全判定照常每轮发生。
        """
        dt = dt if dt is not None else 1.0 / float(self.cfg["system"]["tick_hz"])

        # 惰性启动认知线程：只在真的要用大模型时才建
        if not self._orchestrator_started:
            self.orchestrator.start()
            self._orchestrator_started = True

        with self.state_lock:
            # 1) 环境先变化（用户还没动，障碍在动）
            self.env.advance_environment(dt)

            # 2) 感知 + 空间状态融合
            #    dt 必须传下去：置信度估计器要用"位移 - 速度×dt 预期"算残差，
            #    dt 错了会把正常行走误判成定位漂移。
            state = self.state_manager.build(self.elapsed, self.elapsed, datetime.now(), dt)
            self.memory.add_state_snapshot(state)

            # ★Stage 9 录制★ 时钟要在决策**之前**写：认知工作线程会在
            #   `orchestrator.decide()` 内部立刻发起大模型调用，它读的就是这个时钟。
            if self.llm is not None:
                self.llm.record_clock = (self.elapsed, self.tick)
            if self.recorder is not None:
                self.recorder.record_state(
                    state, elapsed=self.elapsed, tick=self.tick, zone=self._zone_of(state)
                )

            # 2.5) 事件引擎：状态迁移 → 事件（只读状态）
            events = self.events.process(state, self.elapsed)
            if self.recorder is not None:
                self.recorder.record_events(events, elapsed=self.elapsed, tick=self.tick)

            # 3) 编排决策：安全快循环（每轮必跑）+ 认知慢循环结果（可能没有）+ 规则基线
            #    ← 安全层在**任何 LLM 结果之前**被求值，且永远会被求值。
            query, self._pending_query = self._pending_query, None
            outcome = self.orchestrator.decide(state, events, self.elapsed, self.tick, query)
            action = outcome.action

            # 4) 执行行动
            self._apply(action)

            # 5) 世界推进：用户是否被允许前进（人机协同的关键开关）
            self.env.advance_user(
                dt,
                motion_allowed=action.permits_motion,
                front_distance=state.environment.front_distance,
            )

        result = StepResult(
            state=state,
            action=action,
            elapsed=self.elapsed,
            llm_used=outcome.llm_used or action.source == "llm",
            safety=outcome.verdict.as_dict() if outcome.verdict.level.value != "ok" else None,
        )
        if self.recorder is not None:
            dec = self.agent.last_decision
            self.recorder.record_action(
                action,
                elapsed=self.elapsed,
                tick=self.tick,
                source=str(action.source),
                llm_used=bool(result.llm_used),
                decision=dec,
                gate=self.agent.last_gate,
                latency_s=getattr(dec, "latency_s", None),
                visual_need=(
                    {"needs_visual": bool(getattr(dec, "needs_visual", False))}
                    if dec is not None
                    else None
                ),
            )
        self.history.append(result)
        if len(self.history) > 200:
            self.history.pop(0)

        self.elapsed += dt
        self.tick += 1
        if self.env.arrived():
            self.finished = True
        return result

    def _apply(self, action: Action) -> None:
        """执行行动（v0.2 的调用点）。

        ★Stage 9★ 实现已搬到 `SpatialAgent.apply_action()` —— 回放器（不接模拟器，
        没有 SpatialAgentSystem）需要**完全相同**的执行语义。这里只做转发。
        """
        self.agent.apply_action(action, self.elapsed)

    # -----------------------------------------------------------------
    # 外部输入
    # -----------------------------------------------------------------
    def submit_query(self, text: str) -> None:
        """提交一条用户语音/文本输入，下一轮决策时处理。"""
        text = (text or "").strip()
        if not text:
            return
        self.memory.add_user(text, self.elapsed)
        self._pending_query = text

    def set_user_speaking(self, speaking: bool) -> None:
        """告知 Agent「用户此刻是否正在说话」。

        交互策略的输入之一（任务书第十节）：用户正说话时不打断，
        但要说的内容仍然留在行动里（下一轮若不再重复就会被说出来）。
        真实语音前端接入后由 VAD/ASR 写这里；仿真与测试可直接调用。
        """
        self.agent.user_speaking = bool(speaking)

    # --- 手动驾驶（操作员用 WASD 接管仿真里的"盲人"） ---
    def set_manual(self, enabled: bool) -> bool:
        """切换手动/自动驾驶。返回切换后的状态。"""
        self.env.set_manual(bool(enabled))
        return bool(self.env.nav.manual)

    def drive(self, forward: int, turn: int) -> None:
        """提交一次按键状态（线程安全：只是一次 append）。

        forward/turn ∈ {-1,0,1}。位移的时间积分由仿真主循环完成，
        所以这个调用可以来自 HTTP/WS 线程而不会与主循环抢状态。
        """
        self.env.submit_drive(forward, turn)

    def manual(self) -> bool:
        return bool(self.env.nav.manual)

    # -----------------------------------------------------------------
    # 输出
    # -----------------------------------------------------------------
    def last_state(self) -> SpatialState | None:
        return self.state_manager.last_state

    def stats(self) -> dict[str, Any]:
        """运行统计。结构沿用 v0.2（前端与测试都依赖这些键）。"""
        env = self.env.stats()
        return {
            "tick": self.tick,
            "elapsed": round(self.elapsed, 2),
            "arrived": env["arrived"],
            "progress": env["progress"],
            "distance_to_goal_m": env["distance_to_goal_m"],
            "replans": env["replans"],
            # ★手动驾驶（WASD）：前端靠这个字段知道"现在这个人由操作员控制"★
            #   纯只读转发，规则路径（自动导航）里它恒为 {manual: False, ...}。
            "drive": env["drive"],
            "map": env["map"],
            "sensors": env["sensors"],
            "world": self.world.stats(),
            "memory": self.memory.stats(),
            "tools": self.tools.stats(),
            "llm": self.llm.stats() if self.llm else {"available": False, "reason": "已禁用"},
            "agent": {
                "llm_used": self.agent.llm_used_count,
                "rule_fallback": self.agent.llm_fallback_count,
            },
            # ★Stage 8：交互策略与认知层的健康度★
            #   交互策略的闸门计数 + 认知输出的 schema 校验失败次数，
            #   是判断"模型是不是在瞎说"最快的两个数字。
            "interaction": self.agent.policy.stats(),
            "cognitive": self.agent.cognitive.stats(),
            # ★Stage 10：主动感知（什么时候主动要一张画面）★
            "perception": self.agent.context.perception.stats(),
            # ★Stage 9：录制状态（enabled/session_id/各文件条数）★
            "recording": (
                self.recorder.stats()
                if self.recorder is not None
                else {"enabled": False}
            ),
            # ★Stage 7：双循环的健康度★ 快循环次数 vs 认知循环次数，
            # 这两个数差得越多说明"省下来的 LLM 调用"越多（也说明事件驱动在起作用）。
            "loops": self.orchestrator.stats(),
            "obstacles_active": env["obstacles_active"],
            "obstacles_spawned": env["obstacles_spawned"],
        }

    def reset(self) -> None:
        """重置到初始状态（用于测试与重复演示）。

        这里刻意对子对象做「原地重新初始化」而不是重建对象：
        AgentTools / SpatialAgent 持有的是这些对象的引用，原地重置能让引用保持有效。
        `env.reset()` 内部同样只重置 nav 与障碍列表，**不重建 provider 本身**
        —— 重建会连带把传感器 RNG 复位，那是另一回事（会让"重置后重跑"变成
        与首次运行完全一致的轨迹，掩盖偶发问题）。
        """
        self.env.reset()
        self.state_manager.reset()
        # v0.3 Stage 5：世界模型用 clear_runtime() 清运行期记忆（轨迹/对象/短期/
        # 语义观察/经验），但**保留地图绑定**—— 静态知识不该跟着重置一起丢掉。
        # 旧的 __init__ 重置会连 bind_map 的成果一起清空。
        self.world.clear_runtime()
        self.events.reset()
        self.orchestrator.reset()
        self.memory.__init__(self.cfg)  # type: ignore[misc]
        self.tools.call_log.clear()
        self.tools.utterance_log.clear()
        self.agent.rules.reset()
        # ★Stage 8★ 策略与认知层的运行期记忆也要清：巡航时长、解析失败计数。
        # 漏掉 policy.reset() 会让"重置后重新演示"从"已巡航 20 秒"的状态开始，
        # 于是第一轮就静默 —— 这类残留在演示时最容易被当成 bug。
        self.agent.policy.reset()
        self.agent.cognitive.reset()
        self.agent.user_speaking = False
        self.agent.last_action = None
        self.agent.last_decision = None
        self.elapsed = 0.0
        self.tick = 0
        self.history.clear()
        self.finished = False

    def close(self) -> None:
        """停止后台认知线程（服务退出 / 测试收尾时调用，避免线程泄漏）。"""
        try:
            self.orchestrator.stop()
        except Exception as e:  # noqa: BLE001 - 收尾失败不应影响进程退出
            LOG.warning("停止认知循环失败：%s", e)
        self._orchestrator_started = False
        if self.recorder is not None:
            try:
                self.recorder.close()   # 写 closed_at 与计数，会话即可被回放
            except Exception as e:  # noqa: BLE001
                LOG.warning("关闭录制器失败：%s", e)

    # -----------------------------------------------------------------
    # 终端展示
    # -----------------------------------------------------------------
    def banner(self) -> str:
        """终端横幅（格式与最终交付一致）。"""
        st = self.last_state()
        if st is None:
            return "======== Spatial Agent ========\n（尚未产生状态）"
        width = 64
        lines = [
            "=" * width,
            f"{'Spatial Agent':^{width}}",
            "=" * width,
            f"Time        : {st.timestamp:%H:%M:%S}",
            f"Tick        : {st.tick}",
            f"User        : position=({st.user.position.x:.1f},{st.user.position.y:.1f})  "
            f"floor={st.user.position.floor}  heading={st.user.heading:.0f}°  "
            f"status={st.user.walking_status.value}",
            f"Zone        : {self.provider.zone_at(st.user.position.x, st.user.position.y) or '未知'}",
            f"Goal        : {st.navigation.destination}  剩余 {st.navigation.distance_to_goal:.1f}m  "
            f"进度 {st.navigation.route_progress * 100:.0f}%",
            f"Environment : front_clear={st.environment.front_clear}  "
            f"front_distance={st.environment.front_distance:.2f}m  "
            f"width={st.environment.corridor_width or 0:.2f}m  "
            f"obstacles={len(st.environment.obstacles)}",
            f"Risk        : level={st.risk.level.value}  ({st.risk.reason})",
            f"Camera      : {'available' if st.camera.image_available else 'unavailable'}",
        ]
        action = self.agent.last_action
        if action is not None:
            lines.append(f"Agent       : [{action.action_type.value}] \"{action.message or '(保持安静)'}\"")
            lines.append(f"  reason    : {action.reason}")
        lines.append("=" * width)
        return "\n".join(lines)


__all__ = ["SpatialAgent", "SpatialAgentSystem", "StepResult", "SystemConfig"]
