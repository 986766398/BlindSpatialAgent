"""导航模拟器：全局路线 + 局部重规划 + 用户沿路线行走。

对应真实系统的三层结构：
    UWB/INS 定位   -> 本文件维护的 (x, y, heading)
    NavMesh 全局路径 -> route_landmarks（路点序列）
    局部避障路径    -> A* 求出的 local_path（每帧可重规划）

人机协同的关键点：**用户只有在 Agent 允许时才前进**。
Agent 给出 WAIT / ASK_USER 时用户停步；给出 CONTINUE / SPEAK 时恢复行走。
这样「Agent 决策 → 用户行动 → 状态更新」的闭环才真实存在。
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Iterable

from spatial.geometry import angle_delta, bearing_deg  # noqa: F401  (re-export，兼容旧调用点)
from spatial.spatial_state import RoutePoint, WalkingStatus
from simulator.map_simulator import MapSimulator

TURN_RATE_DEG_S = 90.0  # 用户转身速度，度/秒
DRIVE_LOG_MAX = 512     # 手动驾驶事件日志上限（防呆：主循环停摆时也不会无限涨）

# `bearing_deg` / `angle_delta` 已搬到 `spatial/geometry.py`（纯数学，不属于模拟器）。
# 这里保留 re-export，使 `from simulator.navigation_simulator import bearing_deg` 继续可用。


class NavigationSimulator:
    """用户位姿 + 路线规划 + 沿路线移动。"""

    def __init__(self, cfg: dict[str, Any], map_sim: MapSimulator) -> None:
        motion = cfg["simulator"]["motion"]
        self.map = map_sim
        self.walk_speed: float = float(motion["walk_speed_mps"])
        self.slow_speed: float = float(motion["slow_speed_mps"])
        self.stop_distance: float = float(motion["stop_distance_m"])
        self.slow_distance: float = float(motion["slow_distance_m"])
        self.arrive_radius: float = float(motion["arrive_radius_m"])
        self.replan_deviation: float = float(motion.get("replan_deviation_m", 1.0))

        start = map_sim.start
        self.pos: tuple[float, float] = (float(start["x"]), float(start["y"]))
        self.heading: float = float(start.get("heading_deg", 0.0))
        self.speed: float = 0.0
        self.status: WalkingStatus = WalkingStatus.STANDING

        # 全局路线：路点名称序列
        self.landmarks: list[dict[str, Any]] = list(map_sim.landmarks)
        self.destination: str = map_sim.destination
        self.landmark_idx: int = 1 if len(self.landmarks) > 1 else 0

        self.local_path: list[tuple[float, float]] = []
        self.off_route: bool = False
        self.path_found: bool = True          # 上一次规划是否成功；False 表示无路可走
        self.replan_count: int = 0
        self.arrived: bool = False
        self.paused: bool = False
        self._replan_cooldown: float = 0.0    # 重规划限流，避免每帧跑 A*

        # --- 手动驾驶（操作员用 WASD 驱赶仿真里的"盲人"） ---
        # ★线程模型：外部（FastAPI 线程）只做一件事 —— 往 _drive_log 里 append；
        #   所有状态读取/积分都发生在仿真线程的 drive() 里。
        #   deque.append 在 CPython 下是原子的，所以不需要锁，也不违反
        #   "仿真状态只有一个写者（主循环）"这条架构铁律。★
        self.manual: bool = False
        self._drive_log: deque[tuple[float, int, int]] = deque(maxlen=DRIVE_LOG_MAX)
        self._drive_acc: list[float] = [0.0, 0.0]      # [前进秒数(带符号), 转向秒数(带符号)]
        self._cur_drive: tuple[int, int] = (0, 0)      # 仿真线程视角的"当前键状态"
        self._drive_t0: float = time.monotonic()       # 上一次积分到的时刻

        self.travelled: float = 0.0
        self._full_route_len: float = self._route_length_from(0)

        self.replan()

    # -----------------------------------------------------------------
    # 路线信息
    # -----------------------------------------------------------------
    def _route_length_from(self, idx: int) -> float:
        """从第 idx 个路点开始，沿路点序列的总长度。"""
        pts = [(float(p["x"]), float(p["y"])) for p in self.landmarks[idx:]]
        chain = [self.pos] + pts
        return sum(math.dist(chain[k], chain[k + 1]) for k in range(len(chain) - 1))

    @property
    def target_landmark(self) -> dict[str, Any]:
        return self.landmarks[min(self.landmark_idx, len(self.landmarks) - 1)]

    def current_route(self, max_points: int = 8) -> list[RoutePoint]:
        """剩余路线（当前位置 → 剩余路点），供状态输出与模型理解。"""
        pts: list[RoutePoint] = [RoutePoint(x=self.pos[0], y=self.pos[1], name="当前位置")]
        for p in self.landmarks[self.landmark_idx :]:
            pts.append(RoutePoint(x=float(p["x"]), y=float(p["y"]), name=str(p["name"])))
        if self.local_path and len(self.local_path) > 2:
            # 用局部路径细化到下一个路点的那一段
            pts[1:1] = [RoutePoint(x=x, y=y, name="局部避障点") for x, y in self.local_path[1:-1]]
        return pts[:max_points]

    def remaining_distance(self) -> float:
        """剩余距离 = 到局部路径终点的距离 + 剩余路点链长度。"""
        if self.arrived:
            return 0.0
        d = 0.0
        if len(self.local_path) >= 2:
            chain = [self.pos] + self.local_path[1:]
            d += sum(math.dist(chain[k], chain[k + 1]) for k in range(len(chain) - 1))
            # 局部路径终点即当前目标路点，剩余路点从其后再算
            d += sum(
                math.dist(
                    (float(self.landmarks[k]["x"]), float(self.landmarks[k]["y"])),
                    (float(self.landmarks[k + 1]["x"]), float(self.landmarks[k + 1]["y"])),
                )
                for k in range(self.landmark_idx, len(self.landmarks) - 1)
            )
        else:
            d = self._route_length_from(self.landmark_idx)
        return max(0.0, d)

    def route_progress(self) -> float:
        if self.arrived:
            return 1.0
        remain = self.remaining_distance()
        total = self.travelled + remain
        return 0.0 if total <= 1e-6 else min(1.0, self.travelled / total)

    def next_instruction(self) -> str:
        """生成简短明确的导航指令（面向盲人，字数尽量少）。"""
        if self.arrived:
            return "已到达目的地"
        if self.manual:
            # ★手动模式必须单独说清楚★ 位姿由操作员接管，`local_path` 每走一步就被作废，
            #   若落到下面的分支就会输出"正在重新规划路线" —— 把操作员的正常操作
            #   说成系统故障，既不诚实也会让大模型跟着复述"正在重规划"。
            return "手动模式：由操作员接管移动"
        if not self.local_path:
            return "前方无法通行，请原地等待" if not self.path_found else "正在重新规划路线"

        look = self._lookahead_point(2.0)
        delta = angle_delta(bearing_deg(self.pos, look), self.heading)
        target_name = str(self.target_landmark["name"])

        if abs(delta) <= 15:
            turn = "直行"
        elif abs(delta) <= 45:
            turn = "稍向右" if delta > 0 else "稍向左"
        elif abs(delta) <= 100:
            turn = "向右转" if delta > 0 else "向左转"
        else:
            turn = "向后转"

        remain_to_landmark = math.dist(
            self.pos, (float(self.target_landmark["x"]), float(self.target_landmark["y"]))
        )
        remain_to_landmark = max(1.0, remain_to_landmark)  # 不说"约0米"，对盲人没有意义
        if turn == "直行":
            return f"直行约{remain_to_landmark:.0f}米到{target_name}"
        return f"{turn}，前进约{remain_to_landmark:.0f}米到{target_name}"

    def _lookahead_point(self, distance: float) -> tuple[float, float]:
        """沿局部路径向前取 distance 米的点，用于判断"该往哪走"。"""
        if len(self.local_path) < 2:
            return self.local_path[-1] if self.local_path else (
                float(self.target_landmark["x"]),
                float(self.target_landmark["y"]),
            )
        acc = 0.0
        for k in range(1, len(self.local_path)):
            a, b = self.local_path[k - 1], self.local_path[k]
            seg = math.dist(a, b)
            if acc + seg >= distance:
                t = (distance - acc) / seg if seg > 1e-6 else 0.0
                return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            acc += seg
        return self.local_path[-1]

    # -----------------------------------------------------------------
    # 重规划
    # -----------------------------------------------------------------
    def replan(self, obstacles: Iterable[tuple[float, float]] | None = None, *, count: bool = True) -> bool:
        """重算到当前目标路点的局部路径。返回是否成功找到路。

        找不到路时**不会**退化成"硬闯直线"：盲人不能挤过规划器判定不可通行的缝隙。
        这种情况下 path_found=False，用户停步，由 Agent 决定等待、绕行还是求助。

        ★`count` 为什么存在（别把它当"要不要计数"的小开关）★
        本模拟器的局部路径是**以"当前位置"为起点**设计的：`_lookahead_point()`、
        `next_instruction()`、`current_route()` 都默认 `local_path[0] ≈ 当前位置`，
        所以每 tick 都要把路径重新锚定到脚下，顺便把新出现的动态障碍纳入。
        这一步是**例行刷新**，不是"路线被挡需要绕行"那种重规划；
        混在一起计数会让 `replan_count` 每轮 +1（实测 27 轮报 26 次），
        并顺着 `WorldReference.replan_count` 传到 `fusion/confidence.py`，
        把 `route_confidence` 永久压在 0.7 地板 —— 指标变成假信号。
        所以例行重锚定传 `count=False`；只有**外部真正要求重算**的调用点
        （初始化 / 路点切换 / 被挡 / 撞墙 / Agent 的 replan_route 工具）才计数。
        """
        goal = (float(self.target_landmark["x"]), float(self.target_landmark["y"]))
        path = self.map.find_path(self.pos, goal, extra_blocked=obstacles)
        if count:
            self.replan_count += 1

        if path:
            self.local_path = path
            self.off_route = False
            self.path_found = True
            return True

        self.local_path = []
        self.off_route = True
        self.path_found = False
        return False

    def skip_to_next_landmark(self) -> bool:
        """放弃当前路点，直接前往下一个（用于局部死锁）。"""
        if self.landmark_idx < len(self.landmarks) - 1:
            self.landmark_idx += 1
            self.replan()
            return True
        return False

    # -----------------------------------------------------------------
    # 手动驾驶（WASD）
    # -----------------------------------------------------------------
    def set_manual(self, enabled: bool) -> None:
        """切换手动/自动驾驶。

        手动模式下 `tick()` 不再推进位姿，改由 `drive()` 按操作员按键驱赶 ——
        仿真里的"盲人"此时由人接管，Agent 仍然照常感知/决策/播报，
        只是它的 `permits_motion` 不再决定用户走不走。
        """
        self.manual = bool(enabled)
        # 进入手动时重置计时：否则会把"自动驾驶期间流逝的时间"算成按键时长
        self._drive_t0 = time.monotonic()
        self._drive_acc = [0.0, 0.0]
        self._cur_drive = (0, 0)
        self._drive_log.clear()
        if self.manual:
            # 手动模式下"到达"不再是终点，否则用户一按 W 就被 arrived 短路掉
            self.arrived = False
            self.off_route = False
            self.local_path = []
            self.path_found = True
            self.status = WalkingStatus.STANDING
            self.speed = 0.0
        else:
            self.replan()

    def submit_drive(self, forward: int, turn: int) -> None:
        """外部提交一次按键状态。**只做一次 append**，不碰任何仿真状态。

        forward / turn 取值 -1/0/1：前进/后退、左转/右转。
        调用方（FastAPI 线程）在每次 keydown/keyup 时调用一次即可，
        时间积分由仿真线程的 `_consume_drive()` 完成。
        """
        f = 0 if not forward else (1 if forward > 0 else -1)
        t = 0 if not turn else (1 if turn > 0 else -1)
        self._drive_log.append((time.monotonic(), f, t))

    def _consume_drive(self, dt: float) -> tuple[float, float]:
        """把按键时间线折算成本轮该走/该转的**秒数**（带符号，已按 dt 截断）。

        时间线形如 [(t0, f0, u0), (t1, f1, u1), ...]，含义是"从 t_i 起键位变成 (f_i,u_i)"。
        于是 [t_i, t_{i+1}) 这段时间里生效的是**上一条**记录的状态。
        """
        now = time.monotonic()
        events = list(self._drive_log)
        self._drive_log.clear()

        cur_f, cur_u = self._cur_drive
        cursor = self._drive_t0
        for t_ev, f, u in events:
            span = t_ev - cursor
            if span > 0.0:
                self._drive_acc[0] += cur_f * span
                self._drive_acc[1] += cur_u * span
            cur_f, cur_u = f, u
            cursor = t_ev
        span = now - cursor
        if span > 0.0:
            self._drive_acc[0] += cur_f * span
            self._drive_acc[1] += cur_u * span
        self._drive_t0 = now
        self._cur_drive = (cur_f, cur_u)

        fwd = max(-dt, min(dt, self._drive_acc[0]))
        turn = max(-dt, min(dt, self._drive_acc[1]))
        # 只在按键时长上做截断，不丢"积累"（一次 tick 最多走 dt 秒）
        self._drive_acc[0] = max(-dt, min(dt, self._drive_acc[0] - fwd))
        self._drive_acc[1] = max(-dt, min(dt, self._drive_acc[1] - turn))
        return fwd, turn

    def drive(self, dt: float) -> WalkingStatus:
        """手动模式下的每帧推进：转向 + 前进，撞墙停在原地。"""
        fwd_s, turn_s = self._consume_drive(dt)

        if turn_s:
            self.heading = (self.heading + TURN_RATE_DEG_S * turn_s) % 360.0

        if not fwd_s:
            self.speed = 0.0
            self.status = WalkingStatus.STANDING
            return self.status

        step = self.walk_speed * fwd_s
        rad = math.radians(self.heading)
        new_pos = (self.pos[0] + math.sin(rad) * step, self.pos[1] + math.cos(rad) * step)
        if not self.map.is_walkable(new_pos[0], new_pos[1]):
            self.speed = 0.0
            self.status = WalkingStatus.BLOCKED
            return self.status

        self.pos = new_pos
        self.travelled += abs(step)
        self.speed = self.walk_speed * (1.0 if step > 0 else -1.0)
        self.status = WalkingStatus.MOVING
        # 位姿被动过，旧局部路径作废；恢复自动导航时下一 tick 会重新锚定
        self.local_path = []
        # ★不要把"缓存路径作废"误报成"A* 找不到路"★
        #   `off_route` 是**规划器失败**的信号，它会经 WorldReference 直达决策层，
        #   命中「无路可走 → 重规划 → 向用户求助」分支（agent/decision.py 第 3 步）。
        #   而手动驾驶期间规划器根本没被问过 —— 操作员按住 W 每走一步就把 off_route
        #   置 True 的话，仿真里会每秒念一遍"正在为您重新规划路线 / 前方通路被挡住了"
        #   （实测：一次 3 分钟的 WASD 测试刷出 65 次求助 + 158 次停步）。
        #   所以这里只作废缓存，**不改变"到底有没有路"这个结论**：
        #   手动模式不存在"规划失败"，path_found 保持 True、off_route 保持 False。
        self.path_found = True
        self.off_route = False
        return self.status

    def drive_state(self) -> dict[str, Any]:
        """手动驾驶状态（供健康检查 / 前端显示）。"""
        return {
            "manual": bool(self.manual),
            "forward": int(self._cur_drive[0]),
            "turn": int(self._cur_drive[1]),
            "pending_s": [round(self._drive_acc[0], 3), round(self._drive_acc[1], 3)],
        }

    # -----------------------------------------------------------------
    # 每帧推进
    # -----------------------------------------------------------------
    def tick(
        self,
        dt: float,
        permitted_to_move: bool,
        front_distance: float,
        obstacle_centers: Iterable[tuple[float, float]] | None = None,
    ) -> WalkingStatus:
        """推进用户运动。permitted_to_move 由 Agent 的决策决定。"""
        if self.arrived:
            self.speed = 0.0
            self.status = WalkingStatus.ARRIVED
            return self.status

        if self._replan_cooldown > 0.0:
            self._replan_cooldown -= dt

        # --- 路径失效：先尝试重规划，重规划也失败就停步等 Agent 决策 ---
        if not self.path_found or len(self.local_path) < 2:
            if self._replan_cooldown <= 0.0:
                self.replan(obstacle_centers)
                if not self.path_found:
                    self._replan_cooldown = 1.0  # 限流，避免每帧都跑 A*
            if not self.path_found:
                self.speed = 0.0
                self.status = WalkingStatus.BLOCKED
                return self.status

        if not permitted_to_move:
            self.speed = 0.0
            self.status = WalkingStatus.PAUSED
            return self.status

        # --- 例行重锚定局部路径（★不是"绕行重规划"，所以 count=False★） ---
        # 条件用的是"到 local_path[0]（路径起点）的距离"：本模拟器的路径起点就是
        # 上一 tick 的落脚点，一步 1.1m > 阈值 1.0m，所以这里**每 tick 都会成立**。
        # 这是既有物理行为的一部分（让路径始终以脚下为起点、并顺带吸收新出现的
        # 动态障碍），一字不改；只是不再把它算进 replan_count。
        # 详见 replan() 的 docstring（它会污染 route_confidence，不是显示问题）。
        if math.dist(self.pos, self.local_path[0]) > self.replan_deviation and self._replan_cooldown <= 0.0:
            self.replan(obstacle_centers, count=False)
            self._replan_cooldown = 0.5

        # --- 依据前方距离决定步速 ---
        if front_distance <= self.stop_distance:
            self.speed = 0.0
            self.status = WalkingStatus.BLOCKED
            return self.status
        if front_distance <= self.slow_distance:
            self.speed = self.slow_speed
            self.status = WalkingStatus.SLOWING
        else:
            self.speed = self.walk_speed
            self.status = WalkingStatus.MOVING

        step = self.speed * dt
        target = self.local_path[1]
        dist_to_target = math.dist(self.pos, target)

        # --- 先转向，再沿朝向前进（模拟盲人持杖行走的真实感） ---
        want = bearing_deg(self.pos, target)
        delta = angle_delta(want, self.heading)
        max_turn = TURN_RATE_DEG_S * dt
        if abs(delta) > max_turn:
            self.heading = (self.heading + math.copysign(max_turn, delta)) % 360.0
            turn_penalty = 0.35  # 转身时前进变慢
        else:
            self.heading = want % 360.0
            turn_penalty = 1.0

        move = min(step * turn_penalty, dist_to_target)
        rad = math.radians(self.heading)
        new_pos = (self.pos[0] + math.sin(rad) * move, self.pos[1] + math.cos(rad) * move)

        if not self.map.is_walkable(new_pos[0], new_pos[1]):
            # 撞墙：退回并触发重规划
            self.speed = 0.0
            self.status = WalkingStatus.BLOCKED
            self.replan(obstacle_centers)
            self._replan_cooldown = 0.5
            return self.status

        self.pos = new_pos
        self.travelled += move

        # --- 到达当前路点则切下一个；到达最后一个路点则完成 ---
        if dist_to_target <= max(self.arrive_radius, move):
            self.local_path.pop(0)
            if len(self.local_path) <= 1:
                if self.landmark_idx >= len(self.landmarks) - 1:
                    self.arrived = True
                    self.status = WalkingStatus.ARRIVED
                    self.speed = 0.0
                    return self.status
                self.landmark_idx += 1
                self.replan(obstacle_centers)

        return self.status

    # -----------------------------------------------------------------
    # 输出
    # -----------------------------------------------------------------
    def pose(self) -> dict[str, float]:
        return {"x": self.pos[0], "y": self.pos[1], "heading": self.heading, "speed": self.speed}


__all__ = ["NavigationSimulator", "angle_delta", "bearing_deg"]
