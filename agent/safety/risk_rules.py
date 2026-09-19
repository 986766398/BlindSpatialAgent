"""确定性安全规则（Safety Rules）—— 不依赖大模型，不依赖网络。

★为什么必须把安全判断从"决策"里拆出来★

任务书第八节的强制验收标准是：

    人为让 LLM sleep 5 秒，Safety Loop 仍然必须继续工作。

如果安全判断藏在提示词里、或藏在大模型的回复里，那么 LLM 卡住的那 5 秒里
用户正前方的墙是没人管的。所以安全判断必须是**纯函数**：

    输入 SpatialState（+ 当前事件 + 阈值），输出一个判定。

本模块**只做判断**：
    - 不播报（Interaction Policy / Agent 的事）
    - 不修改 SpatialState（只读）
    - 不发任何网络请求、不调用 LLM

规则用数据描述（`SafetyRule` 列表）而不是一大坨 if-else，理由是：
    安全规则的集合将来要能被审查、能被测试逐条覆盖、能按配置开关
    （真实设备接入后，某些规则要换成 LiDAR/深度版本的阈值）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from events.event_types import AgentEvent, EventType
from spatial.spatial_state import RiskLevel, SpatialState


class SafetyLevel(str, Enum):
    """安全等级。数值越大越严重（用于取最大值）。"""

    OK = "ok"                # 无异常
    CAUTION = "caution"      # 值得留意，但不打断用户
    WARNING = "warning"      # 需要提醒（允许继续走）
    EMERGENCY = "emergency"  # 必须立即干预（不允许继续走）


_LEVEL_ORDER: dict[SafetyLevel, int] = {
    SafetyLevel.OK: 0,
    SafetyLevel.CAUTION: 1,
    SafetyLevel.WARNING: 2,
    SafetyLevel.EMERGENCY: 3,
}


class SafetyAction(str, Enum):
    """安全层建议的干预方式（Stage 8 的 Action Schema 会把它翻译成用户可感知的动作）。"""

    NONE = "none"    # 无需干预
    NOTICE = "notice"  # 记一笔，不打扰
    ALERT = "alert"  # 提醒（可继续走）
    STOP = "stop"    # 阻止前进


@dataclass(frozen=True)
class SafetyThresholds:
    """安全阈值（全部来自 config，代码里不写死数值）。"""

    critical_distance_m: float
    high_distance_m: float
    narrow_width_m: float
    min_localization: float
    min_perception: float
    # 坠落 / 台阶一旦被检出，就直接按最高优先级处理：这两类是"不可恢复"的事故
    dropoff_is_emergency: bool = True
    stairs_is_warning: bool = True
    # 摄像头断流是否算安全问题：不算。断流只降级视觉，导航应当继续（验收 Demo 明确要求）
    camera_loss_is_safety: bool = False

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "SafetyThresholds":
        risk = cfg["agent"]["risk"]
        conf = cfg["agent"]["confidence"]
        safety = cfg.get("safety", {}) or {}

        # ⚠️ 配置里写 null 表示"沿用 agent.risk 的口径"（见 config.yaml）。
        #    直接 float(None) 会在启动瞬间炸掉，这里必须显式兜底。
        crit = safety.get("critical_distance_m")
        high = safety.get("high_distance_m")
        return cls(
            critical_distance_m=float(crit if crit is not None else risk["critical_distance_m"]),
            high_distance_m=float(high if high is not None else risk["high_distance_m"]),
            narrow_width_m=float(safety.get("narrow_width_m", 0.9)),
            min_localization=float(conf["min_localization"]),
            min_perception=float(safety.get("min_perception_confidence", 0.5)),
            dropoff_is_emergency=bool(safety.get("dropoff_is_emergency", True)),
            stairs_is_warning=bool(safety.get("stairs_is_warning", True)),
            camera_loss_is_safety=bool(safety.get("camera_loss_is_safety", False)),
        )


@dataclass(frozen=True)
class RuleContext:
    """规则求值所需的一切（显式传入，规则函数因此是纯函数、易测）。"""

    state: SpatialState
    events: tuple[AgentEvent, ...]
    thresholds: SafetyThresholds

    def has_event(self, etype: EventType) -> bool:
        return any(e.event_type is etype for e in self.events)

    def highest_event_severity(self) -> str:
        if not self.events:
            return ""
        order = ["info", "notice", "warning", "critical"]
        return max((e.severity.value for e in self.events), key=order.index)


@dataclass(frozen=True)
class SafetyRule:
    """一条安全规则：什么时候触发（predicate）+ 触发时的判定（level/action/话术）。"""

    name: str
    level: SafetyLevel
    action: SafetyAction
    predicate: Callable[[RuleContext], bool]
    message: Callable[[SpatialState], str]
    reason: str = ""
    cooldown_s: float = 2.0
    # emergency 规则不受冷却约束（与事件总线的 critical 豁免同理）


# =====================================================================
# 规则实现（每条都只读 state / events）
# =====================================================================
def _dropoff(ctx: RuleContext) -> bool:
    return bool(ctx.state.environment.dropoff)


def _collision_imminent(ctx: RuleContext) -> bool:
    """正前方近距离有东西，或融合层已把风险判成 CRITICAL。"""
    env = ctx.state.environment
    if ctx.state.risk.level == RiskLevel.CRITICAL:
        return True
    return (not env.front_clear) and env.front_distance <= ctx.thresholds.critical_distance_m


def _stairs(ctx: RuleContext) -> bool:
    return bool(ctx.state.environment.stairs)


def _front_blocked(ctx: RuleContext) -> bool:
    env = ctx.state.environment
    if ctx.state.risk.level == RiskLevel.HIGH:
        return True
    return (not env.front_clear) and env.front_distance <= ctx.thresholds.high_distance_m


def _severe_deviation(ctx: RuleContext) -> bool:
    return bool(ctx.state.navigation.off_route)


def _localization_untrusted(ctx: RuleContext) -> bool:
    st = ctx.state
    return (not st.user.valid) or (
        st.confidence.localization_confidence < ctx.thresholds.min_localization
    )


def _perception_untrusted(ctx: RuleContext) -> bool:
    return ctx.state.confidence.perception_confidence < ctx.thresholds.min_perception


def _sensor_map_conflict(ctx: RuleContext) -> bool:
    return ctx.has_event(EventType.MAP_SENSOR_CONFLICT)


def _narrow_passage(ctx: RuleContext) -> bool:
    env = ctx.state.environment
    if not env.narrow_passage:
        return False
    return env.corridor_width is None or env.corridor_width <= ctx.thresholds.narrow_width_m


def _obstacle_approaching(ctx: RuleContext) -> bool:
    return ctx.has_event(EventType.OBSTACLE_APPROACHING)


#: ★规则表★（顺序即"同级别时的优先级"，越靠前越先被选中）
#: 新增规则请同时补自检用例 —— 没人跑的规则等于死代码。
DEFAULT_RULES: tuple[SafetyRule, ...] = (
    SafetyRule(
        name="dropoff_ahead",
        level=SafetyLevel.EMERGENCY,
        action=SafetyAction.STOP,
        predicate=_dropoff,
        message=lambda st: "前方地面有落差，立即停下",
        reason="前方检出坠落风险（悬空/坑洞）",
        cooldown_s=1.0,
    ),
    SafetyRule(
        name="collision_imminent",
        level=SafetyLevel.EMERGENCY,
        action=SafetyAction.STOP,
        predicate=_collision_imminent,
        message=lambda st: f"请停下，前方{st.environment.front_distance:.1f}米有障碍",
        reason="正前方近距离障碍，继续前进会碰撞",
        cooldown_s=1.0,
    ),
    SafetyRule(
        name="stairs_ahead",
        level=SafetyLevel.WARNING,
        action=SafetyAction.ALERT,
        predicate=_stairs,
        message=lambda st: "前方有台阶，请抬脚慢行",
        reason="前方检出台阶",
        cooldown_s=6.0,
    ),
    SafetyRule(
        name="front_blocked",
        level=SafetyLevel.WARNING,
        action=SafetyAction.ALERT,
        predicate=_front_blocked,
        message=lambda st: f"前方{st.environment.front_distance:.1f}米有障碍，请慢行",
        reason="高风险：前方受阻但尚未到碰撞距离",
        cooldown_s=4.0,
    ),
    SafetyRule(
        name="severe_deviation",
        level=SafetyLevel.WARNING,
        action=SafetyAction.ALERT,
        predicate=_severe_deviation,
        message=lambda st: "已偏离路线，正在重新规划",
        reason="严重偏航",
        cooldown_s=5.0,
    ),
    SafetyRule(
        name="localization_untrusted",
        level=SafetyLevel.WARNING,
        action=SafetyAction.ALERT,
        predicate=_localization_untrusted,
        message=lambda st: "定位信号不太好，您现在是在走廊里吗？",
        reason="定位置信度低于阈值或位姿被标记为无效",
        cooldown_s=30.0,
    ),
    SafetyRule(
        name="obstacle_approaching",
        level=SafetyLevel.WARNING,
        action=SafetyAction.ALERT,
        predicate=_obstacle_approaching,
        message=lambda st: "有移动障碍正在接近，请放慢脚步",
        reason="世界模型观察到动态障碍距离在缩短",
        cooldown_s=3.0,
    ),
    SafetyRule(
        name="sensor_map_conflict",
        level=SafetyLevel.CAUTION,
        action=SafetyAction.NOTICE,
        predicate=_sensor_map_conflict,
        message=lambda st: "地图与现实不一致，以实时感知为准",
        reason="地图标记可通行但传感器报告受阻（或反之）",
        cooldown_s=10.0,
    ),
    SafetyRule(
        name="perception_untrusted",
        level=SafetyLevel.CAUTION,
        action=SafetyAction.NOTICE,
        predicate=_perception_untrusted,
        message=lambda st: "感知质量下降，我会放慢一点",
        reason="感知链路置信度低（深度/雷达数据不可信）",
        cooldown_s=20.0,
    ),
    SafetyRule(
        name="narrow_passage",
        level=SafetyLevel.CAUTION,
        action=SafetyAction.NOTICE,
        predicate=_narrow_passage,
        message=lambda st: f"通道变窄到{st.environment.corridor_width or 0:.1f}米，请慢行",
        reason="可通行宽度接近单人通过极限",
        cooldown_s=8.0,
    ),
)


def max_level(levels: list[SafetyLevel]) -> SafetyLevel:
    """取最严重的等级。"""
    if not levels:
        return SafetyLevel.OK
    return max(levels, key=lambda lv: _LEVEL_ORDER[lv])


__all__ = [
    "DEFAULT_RULES",
    "RuleContext",
    "SafetyAction",
    "SafetyLevel",
    "SafetyRule",
    "SafetyThresholds",
    "max_level",
]
