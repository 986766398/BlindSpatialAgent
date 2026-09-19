"""模拟环境（stage 一/二）：把四个模拟器捆成一个可被"推进"的世界。

职责边界（重要）：
    本文件只做 **推进与状态持有**，不做任何感知加工、不产出 SpatialState。
    感知由 `SimulatedProvider` 负责，融合由 `fusion/` 负责。

为什么把 map / nav / obstacles / sensors 四个模拟器放在同一个对象里：
    它们互相依赖（导航要地图、障碍要地图、传感器要地图），
    在 v0.2 里这三处依赖散落在 `agent_core` / `state_manager` / `tools` 三个文件里。
    收拢到这里之后，**上层只需要一个 `SimulatedEnvironment` 对象**，
    换成 UE5 数字孪生时替换本文件即可。
"""

from __future__ import annotations

from typing import Any

from sensors.base import WorldReference
from simulator.map_simulator import MapSimulator
from simulator.navigation_simulator import NavigationSimulator
from simulator.obstacle_simulator import ObstacleSimulator
from simulator.sensor_simulator import SensorSimulator


class SimulatedEnvironment:
    """二维栅格地图 + 用户运动学 + 动态障碍 + 传感器噪声模型。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        seed: int | None = None,
        enable_obstacles: bool = True,
    ) -> None:
        self.cfg = cfg
        # --- 四个模拟器（本类的内部实现细节，上层不要直接依赖其类型） ---
        self.map = MapSimulator(cfg)
        self.nav = NavigationSimulator(cfg, self.map)
        self.obstacles = ObstacleSimulator(cfg, self.map, seed=seed)
        self.sensors = SensorSimulator(cfg, self.map, seed=seed)
        if not enable_obstacles:
            self.obstacles.enabled = False
        self.elapsed: float = 0.0

    # -----------------------------------------------------------------
    # WorldStepper 契约
    # -----------------------------------------------------------------
    def advance_environment(self, dt: float) -> None:
        """推进环境：障碍移动、生命周期到期、按概率生成新障碍。"""
        self.obstacles.update(dt, self.nav.pos, self.nav.heading, self.elapsed)

    def advance_user(self, dt: float, *, motion_allowed: bool, front_distance: float) -> None:
        """推进用户位姿。

        `motion_allowed` 由 Agent 决策给出（`Action.permits_motion`）——
        这是人机协同的关键开关：Agent 说停，仿真里的用户就真的停。

        ★手动驾驶（操作员按 WASD）时例外★：位姿由人接管，交给 `nav.drive()`，
        不再看 `motion_allowed`。Agent 依旧照常感知/决策/播报，只是不再决定
        用户走不走 —— 这是"实验员接管仿真"，不是"绕过安全底线"（安全层本身
        仍在每轮评估，只是它的停步建议不再驱动这个被接管的位姿）。
        """
        if self.nav.manual:
            self.nav.drive(dt)
            return
        self.nav.tick(dt, motion_allowed, front_distance, self.obstacles.centers())

    def set_manual(self, enabled: bool) -> None:
        """切换手动驾驶模式（WASD）。"""
        self.nav.set_manual(enabled)

    def submit_drive(self, forward: int, turn: int) -> None:
        """线程安全：只把按键状态塞进日志，真正的积分由主循环做。"""
        self.nav.submit_drive(forward, turn)

    def replan(self) -> bool:
        return bool(self.nav.replan(self.obstacles.centers()))

    def reset(self) -> None:
        """回到初始状态。

        刻意用「原地重新初始化」而不是重建对象：`SimulatedProvider` 持有的是这些
        对象的引用，原地重置能让引用继续有效（v0.2 的 `agent_core.reset()` 同款做法）。
        """
        self.nav.__init__(self.cfg, self.map)  # type: ignore[misc]
        self.obstacles.clear()
        self.elapsed = 0.0

    def obstacle_centers(self) -> list[tuple[float, float]]:
        return self.obstacles.centers()

    def arrived(self) -> bool:
        return bool(self.nav.arrived)

    def world_reference(self) -> WorldReference:
        """★不加噪★ 参考读数：位姿 / 路线 / 进度。

        与 `perceive()` 里的 `pose_reference` 同源（都取 `nav.pos`），
        但用途不同：前者给工具与可视化，后者是融合层做几何计算时的基准。
        刻意不复用同一个对象，是为了让"工具读的是什么"在调用点一目了然。
        """
        nav = self.nav
        return WorldReference(
            x=nav.pos[0],
            y=nav.pos[1],
            heading=nav.heading,
            speed=nav.speed,
            floor=self.map.floor,
            zone=self.map.zone_at(nav.pos[0], nav.pos[1]),
            walking_status=nav.status,
            arrived=bool(nav.arrived),
            destination=nav.destination,
            current_landmark=str(nav.target_landmark["name"]),
            next_instruction=nav.next_instruction(),
            off_route=bool(nav.off_route),
            replan_count=int(nav.replan_count),
            remaining_distance=nav.remaining_distance(),
            route_progress=nav.route_progress(),
            route=list(nav.current_route()),
            local_path_points=len(nav.local_path),
        )

    def map_info(self) -> dict[str, Any]:
        """地图元信息（供 stats / 终端横幅；结构沿用 v0.2 `MapSimulator.info()`）。"""
        return self.map.info()

    def debug_ground_truth(self) -> dict[str, Any]:
        """★仅供调试可视化★ 仿真真值：绝对坐标、完整路径、动态障碍位置。

        实时小地图必须画真实几何才能定位问题，所以保留这个出口；
        但给模型/用户的一切都必须走 SpatialState。命名带 debug 是为了让
        业务逻辑里误用它的代码一眼可见。
        """
        nav = self.nav
        pts: list[list[float]] = [[float(nav.pos[0]), float(nav.pos[1])]]
        pts += [[float(x), float(y)] for x, y in (nav.local_path or [])[:240]]
        for lm in list(nav.landmarks or [])[int(nav.landmark_idx or 0):]:
            pts.append([float(lm["x"]), float(lm["y"])])
        return {
            "pos": [round(float(nav.pos[0]), 2), round(float(nav.pos[1]), 2)],
            "heading": round(float(nav.heading), 1),
            "route": pts[:240],
            "obstacles": [
                {
                    "type": str(getattr(ob, "type", "unknown")),
                    "x": round(float(getattr(ob, "x", 0.0)), 2),
                    "y": round(float(getattr(ob, "y", 0.0)), 2),
                    "ttl": round(float(getattr(ob, "ttl", 0.0)), 1),
                }
                for ob in (self.obstacles.obstacles or [])
            ],
        }

    def stats(self) -> dict[str, Any]:
        return {
            "map": self.map.info(),
            "sensors": self.sensors.stats(),
            "obstacles_active": self.obstacles.total(),
            "obstacles_spawned": self.obstacles.spawn_total,
            "replans": self.nav.replan_count,
            "drive": self.nav.drive_state(),
            "arrived": bool(self.nav.arrived),
            "progress": round(self.nav.route_progress(), 3),
            "distance_to_goal_m": round(self.nav.remaining_distance(), 2),
            "elapsed": round(self.elapsed, 2),
            "travelled_m": round(float(getattr(self.nav, "travelled", 0.0)), 2),
        }


__all__ = ["SimulatedEnvironment"]
