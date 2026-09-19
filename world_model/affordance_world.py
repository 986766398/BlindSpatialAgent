"""可行动性世界（Affordance World）—— 给"能不能走"补上历史与经验。

与 `fusion/state_fusion._build_affordance()` 的分工（关键，别混）：

    fusion 的 affordance  = **几何结论**：此帧前方 1.2 米被挡 → 不可直行、建议向左
                            （只依赖当下这一帧，必须快、必须确定）

    AffordanceWorld       = **历史佐证**：
                            · 这把椅子已经在那儿 8 秒了（持续存在 ≠ 一闪而过）
                            · 这个位置以前也堵过，而且是同一个位置（长期问题）
                            · 前方的距离正在缩小，速度 0.4 m/s → 还剩约 3 秒
                            （依赖一段时间的观测，会慢、会有不确定性）

为什么值得单独一层：
    "前方有障碍"与"前方这个障碍**一直**在，而且**越走越近**"对用户的意义完全不同 ——
    前者可以直接绕过去；后者要么该绕行、要么该停下问路。
    这类判断在 v0.2 里只能靠大模型从几行状态里猜。

★本模块**不修改** `SpatialState.affordance`★
    它产出的是**注解（notes）**，由上下文构建器（Stage 8 的 `context_builder.py`）
    与 `get_world_memory()` 工具消费。这样"几何结论"与"历史佐证"两条信息
    在提示词里各自独立、不会互相污染，回归对比也才能干净。
"""

from __future__ import annotations

from typing import Any

from maps.affordance_map import AffordanceKind, AffordanceMap
from spatial.spatial_state import (
    AffordanceState,
    FrameFreshness,
    ObstacleDirection,
    SpatialState,
    WalkingStatus,
)
from world_model.object_memory import ObjectMemoryStore
from world_model.spatial_memory import SpatialMemory


class AffordanceWorld:
    """把几何可行动性 + 对象持续性 + 短期趋势合成"行动建议"。"""

    def __init__(
        self,
        affordance_map: AffordanceMap | None = None,
        *,
        persistent_seen: int = 3,
        approach_speed_threshold: float = 0.05,
    ) -> None:
        self.map = affordance_map if affordance_map is not None else AffordanceMap()
        self.persistent_seen = int(persistent_seen)
        self.approach_speed_threshold = float(approach_speed_threshold)
        # 上一次"阻塞证据"写在哪个网格，避免每帧都往经验库里灌同一个点
        self._last_block_cell: tuple[float, float] | None = None
        self._notes: list[str] = []

    # -----------------------------------------------------------------
    # 观测：把当前状态沉淀成经验
    # -----------------------------------------------------------------
    def observe(
        self,
        state: SpatialState,
        t: float,
        objects: ObjectMemoryStore | None = None,
    ) -> None:
        """登记一轮可行动性证据。"""
        aff = state.affordance
        env = state.environment
        pos = state.user.position
        key = (round(pos.x), round(pos.y))

        if not aff.can_move_forward:
            # 同一米级网格只记一次，避免站着不动就把 evidence 刷到几百
            if self._last_block_cell != key:
                blocker = aff.most_blocking()
                kind = (
                    AffordanceKind.NARROW
                    if blocker is not None and blocker.source_type == "narrow_passage"
                    else AffordanceKind.BLOCKED
                )
                self.map.observe(
                    kind,
                    pos.x,
                    pos.y,
                    t,
                    detail=(blocker.source_type if blocker else ""),
                )
                self._last_block_cell = key
        elif aff.can_move_forward and self._last_block_cell == key:
            # 从"挡住"变成"通畅"：记一次正向证据（会抵消同一处的阻挡证据）
            self.map.observe(AffordanceKind.PASSABLE, pos.x, pos.y, t)
            self._last_block_cell = None

        if env.narrow_passage:
            self.map.observe(AffordanceKind.NARROW, pos.x, pos.y, t, detail="narrow")

        self.map.prune(t)

    # -----------------------------------------------------------------
    # 推理：产出注解
    # -----------------------------------------------------------------
    def notes(
        self,
        state: SpatialState,
        objects: ObjectMemoryStore | None = None,
        short_term: SpatialMemory | None = None,
    ) -> list[str]:
        """返回一组"行动相关"的注解（给上下文构建器/工具用）。

        刻意返回可读字符串列表而不是结构化对象：这些注解会被直接写进
        提示词，让模型据此组织语言；结构化反而多一层无用的映射。
        """
        out: list[str] = []
        aff: AffordanceState = state.affordance
        pos = state.user.position

        # 1) 持续存在性：是"一直在"还是"刚出现"
        if objects is not None:
            near = objects.near(pos.x, pos.y, radius=3.0)
            for m in near[:2]:
                if m.persistent and m.seen_count >= self.persistent_seen:
                    out.append(
                        f"{m.name}（{m.type}）已持续 {m.age_s:.0f} 秒，是稳定障碍物，"
                        f"不要指望它自己消失"
                    )
                elif m.seen_count == 1:
                    out.append(f"刚发现 {m.name}（{m.type}），出现时间很短，可能很快移开")

        # 2) 长期经验：这个地方以前也堵过
        hist = self.map.summary_near(pos.x, pos.y, radius=2.0)
        if hist:
            out.append(f"该位置的历史观察：{hist}")

        # 3) 接近趋势：还能走多远、还剩多少时间
        if short_term is not None:
            trend = short_term.front_distance_trend(window_s=5.0)
            if trend == "closing" and not aff.can_move_forward:
                d = state.environment.front_distance
                spd = max(state.user.speed, 1e-6)
                out.append(
                    f"前方距离持续缩短至 {d:.2f} 米（约 {d / max(spd, 0.1):.0f} 秒后接触），"
                    f"必须立即处理"
                )
            elif trend == "opening":
                out.append("前方正在变开阔，可以继续按原方向前进")

        # 4) 不可通行但没有任何可绕行方向 —— 这时不该说"稍向右绕行"
        if not aff.can_move_forward and aff.preferred_direction is None:
            out.append("两侧空间都不足，不要建议侧移，应停下并询问用户")

        # 5) 视觉通道不可用时，明确"我看不见"，不要编造
        if state.camera.freshness is not FrameFreshness.FRESH and not aff.can_move_forward:
            out.append("当前没有可用画面，判断仅基于距离传感器")

        # 6) 用户已停步：说明 Agent 正在等
        if state.user.walking_status in (WalkingStatus.PAUSED, WalkingStatus.BLOCKED):
            out.append("用户已停下，等待指令")

        self._notes = out
        return out

    def last_notes(self) -> list[str]:
        return list(self._notes)

    def as_prompt_text(self, **kwargs: Any) -> str:
        """注解拼成一行，直接进提示词。"""
        return "；".join(self.notes(**kwargs))

    def preferred_direction_hint(self, aff: AffordanceState) -> str:
        """把建议方位翻译成给用户的说法（避免"建议方位=left"这种机器话）。"""
        table = {
            ObstacleDirection.LEFT: "稍向左",
            ObstacleDirection.RIGHT: "稍向右",
            ObstacleDirection.FRONT_LEFT: "向左前方",
            ObstacleDirection.FRONT_RIGHT: "向右前方",
            ObstacleDirection.FRONT: "保持直行",
            ObstacleDirection.BEHIND: "掉头",
        }
        if aff.preferred_direction is None:
            return "原地等待"
        return table.get(aff.preferred_direction, "绕行")

    def stats(self) -> dict[str, Any]:
        return {"map": self.map.stats(), "notes": len(self._notes)}


__all__ = ["AffordanceWorld"]
