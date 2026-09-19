"""决策层：规则兜底决策引擎（Action 数据结构的定义已迁往 `agent/action_schema.py`）。

为什么必须有规则引擎：
1. **大模型不是永远可用**：没配 API Key、网络抖动、超时，都不能让用户站在走廊里等。
2. **大模型会出错**：输出非法 JSON、给出与传感器矛盾的建议。
   规则引擎负责安全兜底，并在模型结论与几何事实冲突时以安全为准。
3. 实时性要求高的场景（0.3 米内有障碍）不适合等大模型的推理延迟。

大模型负责「理解与表达」，规则引擎负责「安全底线」。

★v0.3 Stage 8 的两点变化★
    ① `Action` / `ActionType` / `MOTION_ALLOWED` 的定义搬到了 `agent/action_schema.py`
       （10 种行动 + priority/confidence/expires_at/metadata），本模块**原样转出**
       以保证 `from agent.decision import Action, ActionType, RuleDecisionEngine`
       这类既有写法（含自检与 e2e）继续有效。
    ② `_say()` 里"要不要播报"的两道闸（重复 / 最小间隔）移交 `InteractionPolicy`。
       **语义逐字保持**（先 trim 再判重的顺序、两条 reason 文案、urgent 直通规则都不变），
       所以 120 轮回归快照仍然零差异 —— 这次迁移是"换实现位置"，不是"换行为"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent.action_schema import (  # noqa: F401 - 转出以保兼容
    MOTION_ALLOWED,
    Action,
    ActionType,
    Urgency,
)
from agent.interaction_policy import InteractionPolicy
from agent.memory import AgentMemory
from spatial.spatial_state import RiskLevel, SpatialState, WalkingStatus


@dataclass
class _NavState:
    """规则引擎内部状态。"""

    last_nav_key: str = ""
    last_nav_speak: float = -1e9
    off_route_since: float | None = None
    replan_attempts: int = 0
    off_route_asked: bool = False      # 本次偏航是否已经向用户求过助
    off_route_ask_at: float = -1e9     # 上次求助的时刻（冷却判据）
    arrival_announced: bool = False    # 本次到达是否已经播报过
    asked_localization: bool = False
    asked_spin: bool = False


class RuleDecisionEngine:
    """基于几何事实与交互策略的确定性决策。"""

    NAV_REFRESH_INTERVAL_S = 12.0   # 方向不变时，每隔这么久复述一次导航
    OFF_ROUTE_REPLAN_LIMIT = 2      # 连续重规划超过该次数就转为向用户求助
    #: 求助过之后的追问冷却。★为什么必须有★ "向用户求助"是**等答复**，不是"每秒问一次"：
    #: 没有冷却时，一旦偏航状态持续（例如位姿被手动驾驶改过、或真的被堵死），
    #: 同一句话会被每秒重复一遍 —— 实测一次 3 分钟的实测里念了 62 遍。
    OFF_ROUTE_ASK_COOLDOWN_S = 30.0

    def __init__(self, cfg: dict[str, Any], memory: AgentMemory,
                 policy: InteractionPolicy | None = None) -> None:
        self.cfg = cfg
        self.memory = memory
        # 交互策略：本引擎只消费它的"说不说/说多长"，不承担它的决策职责
        self.policy = policy or InteractionPolicy(cfg)
        # ★Stage 8：这三个值改为从策略层读，避免"同一参数两处解析、改一处漏一处"★
        # 保留同名属性是因为历史调用方（自检 / 诊断脚本）会直接读 engine.min_interval。
        self.min_interval = self.policy.min_interval
        self.max_chars = self.policy.max_chars
        self.urgent_bypass = self.policy.urgent_bypass
        risk = cfg["agent"]["risk"]
        self.high_d = float(risk["high_distance_m"])
        self.critical_d = float(risk["critical_distance_m"])
        self.min_loc_conf = float(cfg["agent"]["confidence"]["min_localization"])
        self._nav = _NavState()

    # -----------------------------------------------------------------
    # 对外入口
    # -----------------------------------------------------------------
    def decide(
        self, state: SpatialState, elapsed: float, user_query: str | None = None
    ) -> Action:
        """给出下一步行动。"""
        if user_query:
            return self._answer_query(state, user_query, elapsed)

        # 1) 已到达
        # ★到达只播一次★ 这一条必须用 force=True 才能盖过"最小播报间隔"（到站是大事，
        #   不能因为刚说过别的话就吞掉）；但 force 同时也绕过了"重复检测"，
        #   于是到达状态只要持续（人站着不动就是持续），就会**每秒念一遍**"已到达目的地"
        #   —— 实测一次 3 分钟的实测里念了 31 遍。用一条标记把它钉成"一次事件"。
        arrived = (
            state.user.walking_status == WalkingStatus.ARRIVED
            or state.navigation.route_progress >= 0.999
        )
        if arrived:
            if not self._nav.arrival_announced:
                self._nav.arrival_announced = True
                return self._say(
                    "已到达目的地", "normal", elapsed, "已到达目的地", force=True
                )
            return Action(
                action_type=ActionType.CONTINUE,
                message="",
                urgency="low",
                reason="到达已播报过，同一次到达不重复打扰",
                source="rule",
            )
        self._nav.arrival_announced = False

        # 2) 即将碰撞 —— 最高优先级，突破打扰间隔
        if state.risk.level == RiskLevel.CRITICAL:
            d = state.environment.front_distance
            return self._stop(
                f"请停下，前方{d:.1f}米有障碍",
                "critical",
                elapsed,
                f"正前方 {d:.2f} 米内有障碍",
            )

        # 3) 无路可走 —— 先尝试重规划，多次失败后向用户求助（★求助只问一次★）
        if state.navigation.off_route:
            if self._nav.off_route_since is None:
                self._nav.off_route_since = elapsed
                self._nav.replan_attempts = 0
                self._nav.off_route_asked = False
            if self._nav.replan_attempts < self.OFF_ROUTE_REPLAN_LIMIT:
                self._nav.replan_attempts += 1
                return Action(
                    action_type=ActionType.REPLAN,
                    message="正在为您重新规划路线",
                    urgency="high",
                    reason=f"当前路线不可行，第 {self._nav.replan_attempts} 次重规划",
                    source="rule",
                )
            # ★求助 = 等答复，不是每秒问一遍★
            #   偏航状态可能持续很久（真的被堵死、或位姿刚被手动驾驶改过），
            #   没有冷却就会把同一句问话刷成噪音 —— 用户反而听不出"系统卡住了"这件事。
            #   所以问过一次后保持安静（CONTINUE：用户仍可自己继续走），
            #   只有冷却到期、或状态先恢复再重新偏航，才允许再问。
            if (
                not self._nav.off_route_asked
                or elapsed - self._nav.off_route_ask_at >= self.OFF_ROUTE_ASK_COOLDOWN_S
            ):
                self._nav.off_route_asked = True
                self._nav.off_route_ask_at = elapsed
                return Action(
                    action_type=ActionType.ASK_USER,
                    message="前方通路被挡住了，需要我换一条路吗？",
                    urgency="high",
                    reason="连续重规划失败，需要用户确认",
                    source="rule",
                )
            return Action(
                action_type=ActionType.CONTINUE,
                message="",
                urgency="low",
                reason=(
                    "已向用户求助，等待答复"
                    f"（距上次追问 {elapsed - self._nav.off_route_ask_at:.0f}s，"
                    f"冷却 {self.OFF_ROUTE_ASK_COOLDOWN_S:.0f}s）"
                ),
                source="rule",
            )
        self._nav.off_route_since = None
        self._nav.replan_attempts = 0
        self._nav.off_route_asked = False

        # 4) 高风险 —— 主动提醒但允许继续（配合减速）
        if state.risk.level == RiskLevel.HIGH:
            d = state.environment.front_distance
            who = state.environment.obstacles[0].type.value if state.environment.obstacles else "障碍物"
            return self._say(
                f"前方{d:.1f}米有{who}，请慢行",
                "high",
                elapsed,
                f"高风险：{state.risk.reason}",
            )

        # 5) 通道变窄
        if state.environment.narrow_passage:
            w = state.environment.corridor_width or 0.0
            left = state.environment.left_distance or 0.0
            right = state.environment.right_distance or 0.0
            side = "靠左" if left > right else "靠右"
            return self._say(
                f"通道变窄到{w:.1f}米，请{side}慢行",
                "high",
                elapsed,
                f"可通行宽度仅 {w:.2f} 米",
            )

        # 6) 定位置信度低 —— 主动询问而不是猜
        if (
            state.confidence.localization_confidence < self.min_loc_conf
            and not self._nav.asked_localization
        ):
            self._nav.asked_localization = True
            return Action(
                action_type=ActionType.ASK_USER,
                message="定位信号不太好，您现在是在走廊里吗？",
                urgency="normal",
                reason=f"定位置信度 {state.confidence.localization_confidence:.2f} 偏低",
                source="rule",
            )

        # 7) 正常导航 —— 只在方向变化或超时未复述时才说话
        instruction = state.navigation.next_instruction
        if instruction:
            key = self._nav_key(instruction)
            changed = key != self._nav.last_nav_key
            overdue = elapsed - self._nav.last_nav_speak >= self.NAV_REFRESH_INTERVAL_S
            if changed or overdue:
                action = self._say(
                    instruction, "normal", elapsed, "导航指令更新" if changed else "定期复述导航"
                )
                # 只有真的播报出去了才记账：被节流拦下时下一轮重试
                if action.action_type == ActionType.SPEAK:
                    self._nav.last_nav_key = key
                    self._nav.last_nav_speak = elapsed
                return action

        # 9) 一切正常 —— 保持安静
        return Action(
            action_type=ActionType.CONTINUE,
            message="",
            urgency="low",
            reason="前方通畅且导航方向正确，不打扰用户",
            source="rule",
        )

    # -----------------------------------------------------------------
    # 用户提问
    # -----------------------------------------------------------------
    def _answer_query(self, state: SpatialState, query: str, elapsed: float) -> Action:
        """无大模型时的用户提问兜底回答。"""
        q = query.strip()
        nav = state.navigation
        env = state.environment
        pos = state.user.position

        if any(k in q for k in ("还有多远", "多远", "多久")):
            msg = f"距离{nav.destination}还有约{nav.distance_to_goal:.0f}米"
        elif any(k in q for k in ("在哪", "位置", "哪里")):
            msg = f"您现在位于({pos.x:.0f},{pos.y:.0f})"
        elif any(k in q for k in ("前面", "前方", "有没有")):
            msg = f"前方{env.front_distance:.1f}米内{'通畅' if env.front_clear else '有障碍'}"
        elif any(k in q for k in ("停", "等一下", "慢")):
            return Action(
                action_type=ActionType.WAIT,
                message="好的，已停下，您说",
                urgency="normal",
                reason="用户要求暂停",
                source="user",
            )
        elif any(k in q for k in ("继续", "走吧", "好的", "明白")):
            msg = "好的，继续前进"
        elif any(k in q for k in ("出口", "门")):
            msg = f"出口在{nav.destination}，当前进度{nav.route_progress * 100:.0f}%"
        else:
            msg = f"我在。当前前往{nav.destination}，剩余约{nav.distance_to_goal:.0f}米"
        return self._say(msg, "normal", elapsed, "回应用户提问", force=True, source="user")

    # -----------------------------------------------------------------
    # 工具
    # -----------------------------------------------------------------
    def _say(
        self,
        text: str,
        urgency: Urgency,
        elapsed: float,
        reason: str,
        force: bool = False,
        source: Literal["rule", "llm", "user"] = "rule",
    ) -> Action:
        """按打扰策略决定「说」还是「闭嘴继续走」。

        ★Stage 8：判定搬到 `InteractionPolicy.allow()`★
            本方法只剩"把判定翻译成 Action"这一件事。语义（先裁剪再判重的顺序、
            两条 reason 文案、urgent 直通间隔的规则）与 v0.2 逐字一致 —— 120 轮
            回归快照能守住 0 差异，正是靠这一点。
        """
        d = self.policy.allow(
            text, urgency, elapsed=elapsed, memory=self.memory, force=force
        )
        if d.speak:
            return Action(
                action_type=ActionType.SPEAK,
                message=d.text,
                urgency=urgency,
                reason=reason,
                source=source,
            )
        # 不发声也要出门一个行动：CONTINUE（用户照常往前走，只是不给提示）
        return Action(
            action_type=ActionType.CONTINUE,
            message="",
            urgency="low",
            reason=d.reason,
            source=source,
        )

    def _stop(self, text: str, urgency: Urgency, elapsed: float, reason: str) -> Action:
        """要求用户停下（WAIT）。同一句话不重复播报，但停步指令照常生效。"""
        d = self.policy.allow_stop(text, memory=self.memory, urgency=urgency)
        return Action(
            action_type=ActionType.WAIT,
            message=d.text,
            urgency=urgency,
            reason=reason if d.speak else f"{reason}（{d.reason}）",
            source="rule",
        )

    def _trim(self, text: str) -> str:
        return self.policy.trim(text)

    @staticmethod
    def _nav_key(instruction: str) -> str:
        """提取导航指令的动作关键词，用于判断方向是否变了。"""
        for sep in ("约", "，", "到", "前"):
            if sep in instruction:
                head = instruction.split(sep)[0]
                if head:
                    return head
        return instruction[:3]

    def reset(self) -> None:
        self._nav = _NavState()


__all__ = ["Action", "ActionType", "MOTION_ALLOWED", "RuleDecisionEngine", "Urgency"]
