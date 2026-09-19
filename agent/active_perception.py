"""主动感知（Active Perception）—— 决定"这一轮该不该看"。

任务书第十三节（Phase 10）：

    Agent 只有在
        ① 事件需要视觉
        ② 置信度不足
        ③ 用户询问环境
    时才请求图片。**不要固定：每轮都上传图片。**

## 为什么值得单独一层

"每轮都喂一张图"会把多模态退化成"1 Hz 截图流"：延迟、带宽、配额全按固定频率烧掉，
而绝大多数轮次（笔直走走廊）画面根本不会改变结论。把"要不要看"收敛成一个
**可审计的纯函数**，才能既省成本，又保证"该看的时候一定看"。

## 三种模式（`config.yaml: agent.perception.mode`）

| 模式 | 行为 | 用途 |
|---|---|---|
| `always` | 有帧就附（v0.2 行为） | 回归对照 |
| `auto`（默认） | 有帧就附；**同时**判定"是否真的需要"并登记一次取帧请求 | 兼容模式：不省 token，但已验收的多模态链路不受影响 |
| `on_demand` | **只在需要时才附**（触发条件见下） | 省 token / 省带宽，真实部署推荐 |

★边界（架构铁律 7）★
    本模块只作用于**认知（非确定）路径**。规则基线不读它，
    所以 120 轮确定性回归快照不会因为本层而改变任何一位。

★"请求画面"在这套系统里是什么★
    帧是手机端推上来的。Agent 侧的杠杆有两个：
      · `request_visual_observation()` 工具 —— 模型自己说"我看不清，来一张"；
      · 本策略 —— 事件 / 置信度 / 用户提问驱动的一次取帧请求。
    两者都只做**登记**（供录制与前端提示），不阻塞主循环。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from spatial.spatial_state import SpatialState

LOG = logging.getLogger("bsa.perception")

#: 默认"需要视觉"的事件类型（Stage 6 的 16 种事件里挑出来的）。
#: 判据是："这一条事件的结论，光靠 LiDAR/UWB 说不清，必须看一眼画面"。
#: ⚠️ 下面两条是**预留项**：`EventType` 枚举里当前并没有 `OBSTACLE_DISAPPEARED` /
#:    `NARROW_PASSAGE_DETECTED`（障碍消失对应的是 `OBSTACLE_CLEARED`），因此它们
#:    **永远不会命中**。保留只是为将来新增这两种事件时不必再改这里。
#:    不要据此以为"障碍消失会触发取图"—— 当前 `OBSTACLE_CLEARED` 并不触发。
DEFAULT_VISUAL_EVENT_TYPES: tuple[str, ...] = (
    "MAP_SENSOR_CONFLICT",          # 地图说有路/Sensor 说没路 —— 谁对？看图
    "LOW_LOCALIZATION_CONFIDENCE",  # 定位置信度掉了 —— 看图找地标
    "OBSTACLE_APPEARED",            # 新障碍出现 —— 是什么东西需要看
    "OBSTACLE_DISAPPEARED",         # 【预留】枚举中暂无同名事件，不会命中
    "NARROW_PASSAGE_DETECTED",      # 【预留】枚举中暂无同名事件，不会命中
    "CAMERA_LOST",                  # 视觉链路掉了 —— 提醒但无害
    "ROUTE_DEVIATION",              # 偏离路线 —— 看一眼确认在哪里
)

#: "用户在问环境"的关键词。刻意做成小词表而不是"包含'什么'就算" ——
#: "还有多远" 也是问句，但它问的是路线，不需要画面。
DEFAULT_ENV_QUERY_KEYWORDS: tuple[str, ...] = (
    "前面", "前方", "周围", "环境", "看到", "画面", "什么样",
    "是什么", "有什么", "旁边", "附近", "左边", "右边", "脚下",
)


@dataclass(frozen=True)
class VisualNeed:
    """一次"该不该看"的判定结果（纯数据，可落盘、可断言）。"""

    needed: bool = False
    reasons: tuple[str, ...] = ()
    #: 供诊断：本轮为什么**没有**取帧（节流 / 不需要 / 无相机）
    suppressed_by: str | None = None

    @property
    def reason_text(self) -> str:
        return "+".join(self.reasons) if self.reasons else "无需视觉"

    def as_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "reasons": list(self.reasons),
            "suppressed_by": self.suppressed_by,
        }


@dataclass
class VisualRequestPolicy:
    """把"什么时候需要一张画面"写成可配置的纯函数。

    ⚠️ 时间是**仿真秒**（`elapsed`），不是墙钟 —— 回放 / 测试 / 快循环空转时
       两者差异极大，用墙钟会把"没过多久"判成"已经过了很久"。
    """

    mode: str = "auto"
    localization_confidence_min: float = 0.60
    perception_confidence_min: float = 0.50
    camera_stale_after_s: float = 3.0
    min_interval_s: float = 2.0
    #: "最近多久内发生过需要视觉的事件"才算数。为什么需要这个窗口：
    #: 认知循环本身是节流的（事件驱动 + 心跳），只拿"本轮事件列表"会大量漏判 ——
    #: 事件发生在两次认知调用之间就永远看不到。用仿真秒。
    event_window_s: float = 8.0
    event_types: tuple[str, ...] = DEFAULT_VISUAL_EVENT_TYPES
    env_query_keywords: tuple[str, ...] = DEFAULT_ENV_QUERY_KEYWORDS

    #: 运行计数（诊断用）
    stats_seen: int = 0
    stats_needed: int = 0
    stats_throttled: int = 0
    stats_camera_absent: int = 0
    stats_reasons: dict[str, int] = field(default_factory=dict)
    _last_auto_at: float | None = None

    # -----------------------------------------------------------------
    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> "VisualRequestPolicy":
        node = ((cfg or {}).get("agent", {}) or {}).get("perception", {}) or {}
        ev = node.get("visual_event_types")
        kw = node.get("env_query_keywords")
        return cls(
            mode=str(node.get("mode", "auto")).lower(),
            localization_confidence_min=float(
                node.get("localization_confidence_min", 0.60)
            ),
            perception_confidence_min=float(node.get("perception_confidence_min", 0.50)),
            camera_stale_after_s=float(node.get("camera_stale_after_s", 3.0)),
            min_interval_s=float(node.get("min_interval_s", 2.0)),
            event_window_s=float(node.get("event_window_s", 8.0)),
            event_types=tuple(ev) if ev else DEFAULT_VISUAL_EVENT_TYPES,
            env_query_keywords=tuple(kw) if kw else DEFAULT_ENV_QUERY_KEYWORDS,
        )

    @property
    def attaches_unconditionally(self) -> bool:
        """`always` / `auto` 这两种模式下，只要有新鲜帧就附（不省 token）。"""
        return self.mode in ("always", "auto")

    # -----------------------------------------------------------------
    def triggers(
        self,
        state: SpatialState,
        *,
        events: Iterable[Any] | None = None,
        user_query: str | None = None,
    ) -> list[str]:
        """列出本轮**命中**的触发条件（不看节流，只看"该不该看"）。"""
        hits: list[str] = []

        if user_query and any(k in user_query for k in self.env_query_keywords):
            hits.append("user_asks_environment")

        if events:
            seen: list[str] = []
            for e in events:
                name = getattr(getattr(e, "event_type", None), "value", None) or str(e)
                if name in self.event_types and name not in seen:
                    seen.append(name)
            if seen:
                hits.append("event_needs_vision:" + ",".join(seen))

        conf = getattr(state, "confidence", None)
        if conf is not None:
            if conf.localization_confidence < self.localization_confidence_min:
                hits.append("low_localization_confidence")
            if conf.perception_confidence < self.perception_confidence_min:
                hits.append("low_perception_confidence")

        # ★只看"帧还在但旧了"，不把"根本没有相机"当成"需要视觉"★
        #   任务书给的触发条件只有三条：事件 / 置信度不足 / 用户询问环境。
        #   "没有相机"是**无法获得视觉**，不是**需要视觉** —— 它既不可被满足
        #   （帧由手机端推上来，Agent 侧要不到），又会在无相机部署里把
        #   `needs_visual` 永久点亮成假信号（启动前几轮尤其明显）。真正的
        #   "链路掉了"由 `CAMERA_LOST` 事件覆盖，属于上面的事件触发。
        #   诊断信息仍保留：见 `evaluate()` 里的 `suppressed_by="camera_unavailable"`。
        cam = getattr(state, "camera", None)
        if cam is not None and getattr(cam, "image_available", False):
            age = getattr(cam, "age_s", None)
            if age is not None and age > self.camera_stale_after_s:
                hits.append("camera_stale")

        return hits

    # -----------------------------------------------------------------
    @staticmethod
    def _camera_absent(state: SpatialState) -> bool:
        """相机存在但从没给过帧（诊断用，不构成触发条件）。"""
        cam = getattr(state, "camera", None)
        return cam is not None and not getattr(cam, "image_available", False)

    # -----------------------------------------------------------------
    def evaluate(
        self,
        state: SpatialState,
        *,
        elapsed: float,
        events: Iterable[Any] | None = None,
        user_query: str | None = None,
        force: bool = False,
    ) -> VisualNeed:
        """判定本轮是否需要视觉。

        `force` 来自 `request_visual_observation()`（模型显式索要）——
        它**不受节流限制**：模型主动说看不清时，必须立刻给。
        """
        self.stats_seen += 1
        if self.mode == "never":
            return VisualNeed(False, (), "mode=never")

        hits = self.triggers(state, events=events, user_query=user_query)

        if force:
            hits = ["explicit_tool_request"] + hits

        if not hits:
            # `always` / `auto`：不需要也给（兼容 v0.2 的每轮看图行为）
            suppressed = None if self.attaches_unconditionally else "no_trigger"
            if self._camera_absent(state):
                # 诊断留痕：本轮没取帧的真正原因是"压根没有帧可取"
                suppressed = "camera_unavailable"
                self.stats_camera_absent += 1
            return VisualNeed(False, (), suppressed)

        # ★节流★：持续 10 秒的低置信度不该变成连续 10 次取帧。
        #   显式请求与用户提问豁免 —— 那是"用户就在等这句话"。
        demand = any(
            h == "explicit_tool_request" or h == "user_asks_environment" for h in hits
        )
        if not demand and self._last_auto_at is not None:
            if elapsed - self._last_auto_at < self.min_interval_s:
                self.stats_throttled += 1
                return VisualNeed(False, tuple(hits), "throttled")

        self._last_auto_at = elapsed
        self.stats_needed += 1
        for h in hits:
            key = h.split(":", 1)[0]
            self.stats_reasons[key] = self.stats_reasons.get(key, 0) + 1
        return VisualNeed(True, tuple(hits))

    # -----------------------------------------------------------------
    def reset(self) -> None:
        self.stats_seen = 0
        self.stats_needed = 0
        self.stats_throttled = 0
        self.stats_camera_absent = 0
        self.stats_reasons = {}
        self._last_auto_at = None

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "evaluated": self.stats_seen,
            "needed": self.stats_needed,
            "throttled": self.stats_throttled,
            "camera_absent": self.stats_camera_absent,
            "reasons": dict(self.stats_reasons),
            "localization_confidence_min": self.localization_confidence_min,
            "perception_confidence_min": self.perception_confidence_min,
            "camera_stale_after_s": self.camera_stale_after_s,
            "min_interval_s": self.min_interval_s,
            "event_window_s": self.event_window_s,
        }


__all__ = [
    "DEFAULT_ENV_QUERY_KEYWORDS",
    "DEFAULT_VISUAL_EVENT_TYPES",
    "VisualNeed",
    "VisualRequestPolicy",
]
