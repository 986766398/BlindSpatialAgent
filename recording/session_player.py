"""会话回放器（v0.3 Stage 9）。

任务书第十五节：

    Replay 时：
        **不调用 simulator。**
        读取历史 state。
        重放 Agent。

## 为什么要"不调用 simulator"

回放的价值在于**把"世界"冻结住**，只让"判断"变。这样同一段真实数据
可以反复跑：换模型、换 Prompt、换交互策略、调阈值，然后逐轮比对
"这次和上次哪一轮不一致"。如果回放时还跑模拟器，障碍会重新随机、
用户位姿会重新积分，两次结果就没有可比性了。

## 实现方式

`ReplaySource` 是一个**不接模拟器**的替身，同时扮演三个角色（鸭子类型）：

    感知提供者 SensorProvider     —— 只回答"现在能看到什么"
    世界推进器 WorldStepper       —— 推进方法全是空实现，`world_reference()` 读录制值
    状态管理器（只需 last_state） —— `AgentTools` 只用到这一个属性

于是 `SpatialAgent` / `AgentTools` / `ContextBuilder` 一行都不用改，
就能在"历史状态流"上跑起来 —— 这正是 Stage 3 分层带来的红利。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from recording.schemas import SCHEMA_VERSION, read_jsonl
from sensors.base import MapSnapshot, RawPerception, WorldReference
from spatial.spatial_state import (
    NavigationState,
    PoseState,
    SpatialState,
    WalkingStatus,
)

LOG = logging.getLogger("bsa.replay")


# =====================================================================
# 回放用的替身
# =====================================================================
class ReplaySource:
    """把录制下来的状态端出来当作"感知"。**不接任何模拟器。**"""

    def __init__(self, map_snapshot: MapSnapshot | None = None) -> None:
        self.map_snap = map_snapshot
        self._state: SpatialState | None = None
        self._replans = 0
        self._frames = 0

    # --- 状态装配 ---------------------------------------------------
    def advance_to(self, state: SpatialState) -> None:
        """切到下一帧历史状态。**只赋值，不做任何积分。**"""
        self._state = state
        self._frames += 1

    @property
    def current(self) -> SpatialState | None:
        return self._state

    # --- 角色一：StateManager（AgentTools 只用到这一个属性） --------
    @property
    def last_state(self) -> SpatialState | None:
        return self._state

    # --- 角色二：SensorProvider（只读） -----------------------------
    def read_pose(self, now: Any = None) -> PoseState:
        st = self._state
        if st is None:
            raise RuntimeError("回放尚未开始（没有状态可读）")
        return st.user

    def measure_depth(self, distance: float) -> float | None:
        return distance

    def scan_wall_ahead(self) -> float | None:
        return None

    def scan_sides(self) -> tuple[float | None, float | None]:
        return (None, None)

    def perceive(self) -> RawPerception:
        return RawPerception(source="replay")

    def map_snapshot(self) -> MapSnapshot:
        if self.map_snap is None:
            raise RuntimeError("回放缺少地图快照（录制时未写 map.json）")
        return self.map_snap

    def zone_at(self, x: float, y: float) -> str | None:
        snap = self.map_snap
        if snap is None:
            return None
        for z in snap.zones or []:
            try:
                if (
                    float(z.get("x_min", 0.0)) <= x <= float(z.get("x_max", 0.0))
                    and float(z.get("y_min", 0.0)) <= y <= float(z.get("y_max", 0.0))
                ):
                    return str(z.get("name") or "") or None
            except (TypeError, ValueError):
                continue
        return None

    def floor(self) -> int:
        st = self._state
        return int(getattr(st.user.position, "floor", 1)) if st is not None else 1

    def read_navigation(self, now: Any = None) -> NavigationState:
        st = self._state
        if st is None:
            raise RuntimeError("回放尚未开始（没有导航状态可读）")
        return st.navigation

    def confidence(self) -> tuple[float, float]:
        st = self._state
        if st is None:
            return (1.0, 1.0)
        return (
            float(st.confidence.localization_confidence),
            float(st.confidence.perception_confidence),
        )

    def health(self) -> dict[Any, Any]:
        return {}

    def sensor_stats(self) -> dict[str, Any]:
        return {"mode": "replay", "frames": self._frames}

    # --- 角色三：WorldStepper（推进全是空实现） ---------------------
    def advance_environment(self, dt: float) -> None:
        """★空实现★ 回放的世界是冻结的 —— 这正是"不调用 simulator"的落点。"""

    def advance_user(self, dt: float, *, motion_allowed: bool, front_distance: float) -> None:
        """★空实现★ 用户位姿直接来自下一帧历史状态，不由 Agent 决策驱动。"""

    def replan(self) -> bool:
        """回放里没有规划器：登记一次并返回成功（真实结论看下一帧状态）。"""
        self._replans += 1
        return True

    def reset(self) -> None:
        return None

    def obstacle_centers(self) -> list[tuple[float, float]]:
        st = self._state
        if st is None:
            return []
        return [(float(o.x), float(o.y)) for o in st.environment.obstacles if hasattr(o, "x")]

    def arrived(self) -> bool:
        st = self._state
        if st is None:
            return False
        return st.user.walking_status is WalkingStatus.ARRIVED

    def world_reference(self) -> WorldReference:
        st = self._state
        if st is None:
            raise RuntimeError("回放尚未开始（没有参考读数）")
        u, nav = st.user, st.navigation
        x, y = float(u.position.x), float(u.position.y)
        return WorldReference(
            x=x,
            y=y,
            heading=float(u.heading),
            speed=float(u.speed),
            floor=int(getattr(u.position, "floor", 1)),
            zone=self.zone_at(x, y),
            walking_status=u.walking_status,
            arrived=self.arrived(),
            destination=str(nav.destination),
            current_landmark=str(nav.current_landmark or ""),
            next_instruction=str(nav.next_instruction),
            off_route=bool(nav.off_route),
            replan_count=int(nav.replan_count),
            remaining_distance=float(nav.distance_to_goal),
            route_progress=float(nav.route_progress),
            route=list(nav.current_route),
            local_path_points=0,
        )

    def debug_ground_truth(self) -> dict[str, Any]:
        return {"mode": "replay", "frames": self._frames}

    def stats(self) -> dict[str, Any]:
        """必须包含前端与自检依赖的键（见 `WorldStepper.stats` 的契约）。"""
        st = self._state
        nav = st.navigation if st is not None else None
        area = (self.map_snap.bounds.get("x_max", 0.0) if self.map_snap else 0.0)
        height = (self.map_snap.bounds.get("y_max", 0.0) if self.map_snap else 0.0)
        return {
            "arrived": self.arrived(),
            "progress": float(nav.route_progress) if nav else 0.0,
            "distance_to_goal_m": float(nav.distance_to_goal) if nav else 0.0,
            "replans": self._replans,
            "map": {
                "name": (self.map_snap.name if self.map_snap else "replay"),
                "size": f"{area:.0f}x{height:.0f}m" if self.map_snap else "",
            },
            "sensors": self.sensor_stats(),
            "obstacles_active": 0,
            "obstacles_spawned": 0,
            "drive": {"manual": False, "forward": 0, "turn": 0, "pending_s": [0.0, 0.0]},
        }


# =====================================================================
# 报告
# =====================================================================
@dataclass
class ReplayReport:
    """一次回放的结果摘要。"""

    session_id: str
    ticks: int = 0
    action_types: dict[str, int] = field(default_factory=dict)
    compared: int = 0
    matched: int = 0
    divergences: list[dict[str, Any]] = field(default_factory=list)
    llm_used: int = 0
    images_attached: int = 0
    needs_visual: int = 0
    latency_avg_s: float = 0.0
    out_dir: str | None = None

    @property
    def match_rate(self) -> float:
        return (self.matched / self.compared) if self.compared else 1.0

    def as_dict(self) -> dict[str, Any]:
        d = {
            "session_id": self.session_id,
            "ticks": self.ticks,
            "action_types": dict(self.action_types),
            "compared": self.compared,
            "matched": self.matched,
            "match_rate": round(self.match_rate, 4),
            "llm_used": self.llm_used,
            "images_attached": self.images_attached,
            "needs_visual": self.needs_visual,
            "latency_avg_s": round(self.latency_avg_s, 3),
            "out_dir": self.out_dir,
        }
        if self.divergences:
            d["divergences"] = self.divergences[:20]
        return d


# =====================================================================
# 播放器
# =====================================================================
class SessionPlayer:
    """读一个录制会话，并把 Agent 在历史状态流上重跑一遍。"""

    def __init__(self, root: str | Path = "recordings", session_id: str | None = None) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.dir = self._resolve_dir()
        self.meta: dict[str, Any] = {}
        self.states_raw: list[dict[str, Any]] = []
        self.actions_raw: list[dict[str, Any]] = []
        self.events_raw: list[dict[str, Any]] = []
        self.llm_raw: list[dict[str, Any]] = []
        self.map_snapshot: MapSnapshot | None = None

    # -----------------------------------------------------------------
    def _resolve_dir(self) -> Path:
        if self.session_id:
            d = self.root / self.session_id
            if not d.is_dir():
                raise FileNotFoundError(f"找不到会话目录：{d}")
            return d
        # 不给 id 就取最近一个 session_*（按名字排序即时间序）
        cands = sorted(
            (p for p in self.root.glob("session_*") if p.is_dir()),
            key=lambda p: p.name,
        )
        if not cands:
            raise FileNotFoundError(f"{self.root} 下没有任何 session_* 会话")
        return cands[-1]

    # -----------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取整个会话。meta.json 缺失/版本不符会直接报错（宁可报错也别读出半个会话）。"""
        import json

        meta_path = self.dir / "meta.json"
        if not meta_path.is_file():
            raise FileNotFoundError(f"缺少 meta.json：{meta_path}")
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        ver = str(self.meta.get("schema_version", ""))
        if ver and ver.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"录制格式版本不兼容：文件 {ver}，当前支持 {SCHEMA_VERSION}"
            )
        self.session_id = str(self.meta.get("session_id") or self.dir.name)

        self.states_raw = read_jsonl(self.dir / "states.jsonl")
        self.actions_raw = read_jsonl(self.dir / "actions.jsonl")
        self.events_raw = read_jsonl(self.dir / "events.jsonl")
        self.llm_raw = read_jsonl(self.dir / "llm.jsonl")

        map_path = self.dir / "map.json"
        if map_path.is_file():
            try:
                self.map_snapshot = MapSnapshot.model_validate(
                    json.loads(map_path.read_text(encoding="utf-8"))
                )
            except Exception as e:  # noqa: BLE001 - 地图读不出来也能回放（只是 zone 缺失）
                LOG.warning("读取地图快照失败，回放将缺少区域信息：%s", e)
                self.map_snapshot = None
        return self.meta

    # -----------------------------------------------------------------
    def iter_states(self, limit: int | None = None) -> Iterator[SpatialState]:
        """把 `states.jsonl` 逐条重建为 `SpatialState`。"""
        n = 0
        for rec in self.states_raw:
            dump = rec.get("state")
            if not isinstance(dump, dict):
                continue
            try:
                yield SpatialState.model_validate(dump)
            except Exception as e:  # noqa: BLE001 - 单条坏数据跳过，不毁掉整段回放
                LOG.warning("跳过无法重建的状态记录（t=%s）：%s", rec.get("t"), e)
                continue
            n += 1
            if limit is not None and n >= limit:
                return

    # -----------------------------------------------------------------
    def build_agent(self, cfg: dict[str, Any], *, enable_llm: bool = False) -> tuple[Any, ReplaySource]:
        """搭一个"没有模拟器"的 Agent。

        这是本模块最核心的一行抽象：`SpatialAgentSystem` 里那两行装配
        （`provider=SimulatedProvider(...)`）换成 `ReplaySource`，
        下游一行不改 —— Stage 3 把这条缝留对了。
        """
        from agent.agent_core import SpatialAgent
        from agent.llm_client import LLMClient
        from agent.memory import AgentMemory
        from agent.safety import SafetyEngine
        from agent.tools import AgentTools
        from spatial.world_model import WorldModel

        src = ReplaySource(self.map_snapshot)
        world = WorldModel(max_events=int(cfg["agent"]["memory"]["keep_events"]), cfg=cfg)
        if self.map_snapshot is not None:
            world.bind_map(self.map_snapshot)
        memory = AgentMemory(cfg)
        # provider / stepper / state_manager 三个角色都由 src 扮演（鸭子类型）
        tools = AgentTools(cfg, src, src, src, memory, world, None)  # type: ignore[arg-type]
        llm = LLMClient(cfg, tools) if enable_llm else None
        agent = SpatialAgent(cfg, tools, memory, world, llm=llm, camera=None)
        agent.context.bind(safety=SafetyEngine(cfg), policy=agent.policy)
        return agent, src

    # -----------------------------------------------------------------
    def replay(
        self,
        cfg: dict[str, Any],
        *,
        enable_llm: bool = False,
        max_ticks: int | None = None,
        out_root: str | Path | None = None,
        notes: str = "",
    ) -> ReplayReport:
        """把历史状态流重放一遍。**不调用 simulator。**

        逐轮把新决策与录制里的原决策比对，产出 `ReplayReport` ——
        "换模型/换 Prompt 之后，到底有哪几轮判断变了"是这份报告要回答的问题。
        """
        import time as _time

        from recording.session_recorder import SessionRecorder

        if not self.meta:
            self.load()

        agent, src = self.build_agent(cfg, enable_llm=enable_llm)

        out_rec: SessionRecorder | None = None
        if out_root is not None:
            sid = f"replay_{self.session_id}"[:60]
            out_rec = SessionRecorder(
                cfg, root=out_root, session_id=sid, notes=notes or f"replay of {self.session_id}"
            )
            out_rec.start()
            if self.map_snapshot is not None:
                out_rec.attach_map(self.map_snapshot.model_dump(mode="json"))

        report = ReplayReport(session_id=self.session_id)
        if out_rec is not None:
            report.out_dir = str(out_rec.dir)

        lat_sum = 0.0
        for i, state in enumerate(self.iter_states(limit=max_ticks)):
            # ⚠️ `0.0 or state.tick` 是**错的** —— 首帧的 t 恰好是 0.0（falsy），
            #    会静默回退成 tick 号，于是整条回放时钟整体偏移 1 秒，
            #    所有"距上次播报过了多久"的判据全部错位（表现为 ~95% 一致而非 100%）。
            #    必须显式判 None。
            raw_t = self.states_raw[i].get("t")
            elapsed = float(raw_t) if raw_t is not None else float(state.tick)
            src.advance_to(state)

            t0 = _time.perf_counter()
            action = agent.decide(state, elapsed, None)
            latency = _time.perf_counter() - t0

            # ★必须有这一步★：`apply_action` 是"说完话之后记忆要跟着变"的那一半。
            #   交互策略的"是否重复 / 距上次播报多久"全靠 memory 里的播报记录，
            #   不重放这一半，回放里的 SPEAK/CONTINUE 判据会整体走偏（实测只有 82% 一致）。
            #   放在 `SpatialAgent` 上正是为了这里能复用同一条路径（主循环同理）。
            agent.apply_action(action, elapsed)

            at = str(getattr(action.action_type, "value", action.action_type))
            report.action_types[at] = report.action_types.get(at, 0) + 1
            report.ticks += 1
            lat_sum += latency
            if action.source == "llm":
                report.llm_used += 1

            dec = agent.last_decision
            vn = None
            if dec is not None:
                if getattr(dec, "image_attached", False):
                    report.images_attached += 1
                if getattr(dec, "needs_visual", False):
                    report.needs_visual += 1
                vn = {"needs_visual": bool(getattr(dec, "needs_visual", False))}

            # --- 与原决策逐轮比对 ---
            original = self.actions_raw[i] if i < len(self.actions_raw) else None
            if original is not None:
                report.compared += 1
                was = str((original.get("action") or {}).get("action_type") or "")
                if was == at:
                    report.matched += 1
                elif len(report.divergences) < 200:
                    report.divergences.append(
                        {"tick": original.get("tick"), "t": original.get("t"),
                         "original": was, "replay": at}
                    )

            if out_rec is not None:
                out_rec.record_state(state, elapsed=elapsed, tick=state.tick)
                out_rec.record_action(
                    action,
                    elapsed=elapsed,
                    tick=state.tick,
                    source=str(action.source),
                    llm_used=bool(action.source == "llm"),
                    decision=dec,
                    gate=agent.last_gate,
                    latency_s=latency,
                    visual_need=vn,
                )

        if out_rec is not None:
            out_rec.close()
        if report.ticks:
            report.latency_avg_s = lat_sum / report.ticks
        return report

    # -----------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """不重放，只报"这个会话里有什么"。"""
        if not self.meta:
            self.load()
        wait = self.llm_raw
        return {
            "session_id": self.session_id,
            "dir": str(self.dir),
            "created_at": self.meta.get("created_at"),
            "system_version": self.meta.get("system_version"),
            "schema_version": self.meta.get("schema_version"),
            "map": self.meta.get("map_name"),
            "destination": self.meta.get("destination"),
            "states": len(self.states_raw),
            "events": len(self.events_raw),
            "actions": len(self.actions_raw),
            "llm_calls": len(wait),
            "llm_errors": sum(1 for r in wait if not r.get("ok")),
            "images": len(list((self.dir / "images").glob("*"))) if (self.dir / "images").is_dir() else 0,
        }


__all__ = ["ReplayReport", "ReplaySource", "SessionPlayer"]
