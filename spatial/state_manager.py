"""状态融合门面（StateManager）—— 融合层的**兼容入口**。

★★ 本文件从 v0.3 Stage 4 起不再包含融合逻辑 ★★

    v0.2：StateManager 是一个 659 行的类，自己读传感器、自己算风险、自己推可行动性。
          后果：想单测"定位置信度怎么算"就得先把整个模拟器装配起来。

    v0.3：融合逻辑全部搬进 `fusion/`（freshness / confidence / state_fusion 三个模块），
          本文件退化为**薄门面**：只做三件事 ——
            1. 把构造参数转交给 `StateFusion`；
            2. 用属性转发 `tick` / `history` / `last_state` / `camera` 等既有访问点，
               让 v0.2 的调用方（`agent/tools.py`、`api/websocket_server.py`、
               20+ 条旧自检断言）**零改动**继续工作；
            3. 转发几个私有方法名（`_build_affordance` / `_camera_state` …），
               它们在自检里被直接调用，删掉就等于把测试一起删了。

为什么要保留门面而不是直接改调用点：
    `system.state_manager.camera = receiver`、`sm._build_affordance(env)` 这类写法
    散落在 api / 自检 / 工具里。逐个改成 `state_manager.fusion.xxx` 只是把改动面
    铺大，却不增加任何能力。门面让"重构"与"调用点迁移"两件事解耦 ——
    新代码请直接用 `SpatialAgentSystem.fusion`（或 `state_manager.fusion`）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fusion.state_fusion import StateFusion
from sensors.base import CameraProvider, RawPerception, SensorProvider
from spatial.spatial_state import (
    AffordanceState,
    EnvironmentState,
    NavigationState,
    PoseState,
    RiskState,
    SemanticScene,
    SpatialState,
)
from spatial.world_model import WorldModel


class StateManager:
    """融合层门面：只转发，不做计算。

    ★只接收 `SensorProvider`，不认识任何模拟器★
        模拟器细节全部封在 `sensors/simulated/provider.py` 里。
        换 UE5 数字孪生或真机时，本类一行不改。
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        provider: SensorProvider,
        world: WorldModel | None = None,
        camera: CameraProvider | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.world = world or WorldModel()
        self.fusion = StateFusion(cfg, provider, world=self.world, camera=camera)

    # -----------------------------------------------------------------
    # 属性转发：v0.2 访问点的兼容层
    # -----------------------------------------------------------------
    @property
    def camera(self) -> CameraProvider | None:
        return self.fusion.camera

    @camera.setter
    def camera(self, value: CameraProvider | None) -> None:
        """摄像头是运行期可热插拔的（`attach_camera()` 会在服务启动后才接上）。"""
        self.fusion.camera = value

    @property
    def tick(self) -> int:
        return self.fusion.tick

    @property
    def history(self) -> list[SpatialState]:
        return self.fusion.history

    @property
    def last_state(self) -> SpatialState | None:
        return self.fusion.last_state

    @property
    def estimator(self) -> Any:
        """置信度估计器（Stage 4 新增）—— 暴露给诊断接口与测试。"""
        return self.fusion.estimator

    @property
    def freshness(self) -> Any:
        """新鲜度策略（Stage 4 新增）。"""
        return self.fusion.freshness

    @property
    def _last_front_distance(self) -> float | None:
        return self.fusion._last_front_distance  # noqa: SLF001 - 兼容旧断言

    @property
    def _last_obstacle_ids(self) -> list[int]:
        return self.fusion._last_obstacle_ids  # noqa: SLF001 - 兼容旧断言

    # -----------------------------------------------------------------
    # 主入口转发
    # -----------------------------------------------------------------
    def build(
        self,
        t_now: float,
        elapsed: float,
        wall_clock: datetime | None = None,
        dt: float | None = None,
    ) -> SpatialState:
        """构造当前时刻的 SpatialState（实现见 `fusion/state_fusion.py`）。"""
        return self.fusion.build(t_now, elapsed, wall_clock, dt)

    def _build_environment(self, raw: RawPerception, now: datetime) -> tuple[EnvironmentState, float]:
        return self.fusion._build_environment(raw, now)  # noqa: SLF001

    def _build_semantic(
        self, raw: RawPerception, env: EnvironmentState, now: datetime
    ) -> SemanticScene:
        return self.fusion._build_semantic(raw, env, now)  # noqa: SLF001

    def _camera_state(self, now: datetime) -> Any:
        return self.fusion._camera_state(now)  # noqa: SLF001

    def _evaluate_risk(
        self, env: EnvironmentState, nav: NavigationState, pose: PoseState, now: datetime
    ) -> RiskState:
        return self.fusion._evaluate_risk(env, nav, pose, now)  # noqa: SLF001

    def _build_affordance(self, env: EnvironmentState) -> AffordanceState:
        return self.fusion._build_affordance(env)  # noqa: SLF001

    def _blocking_ratio(
        self, pos: tuple[float, float], heading: float, obstacles: list[Any]
    ) -> float:
        return self.fusion._blocking_ratio(pos, heading, obstacles)  # noqa: SLF001

    def _severity_of(self, distance: float) -> Any:
        return self.fusion._severity_of(distance)  # noqa: SLF001

    def _route_confidence(self, nav: NavigationState) -> float:
        """⚠️ 保留旧方法名：路线可信度的实现已移到 `fusion/confidence.py` 的
        静态方法 `ConfidenceEstimator.route()`（纯函数，可单测）。"""
        return self.fusion.estimator.route(nav)

    # -----------------------------------------------------------------
    # 辅助
    # -----------------------------------------------------------------
    def reset(self) -> None:
        """清空运行期状态（tick / 历史 / 上一轮结果 / 置信度窗口）。"""
        self.fusion.reset()

    def front_distance(self) -> float:
        return self.fusion.front_distance()

    def stats(self) -> dict[str, Any]:
        return self.fusion.stats()


__all__ = ["CameraProvider", "StateFusion", "StateManager"]
