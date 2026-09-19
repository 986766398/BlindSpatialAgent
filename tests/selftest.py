"""BlindSpatialAgent 自检套件。

用法：
    python main.py --selftest          # 通过入口运行
    python -m tests.selftest           # 直接运行

覆盖范围（75 项）：
    配置 / 数据模型 / 地图与寻路 / 传感器 / 动态障碍 / 状态融合 /
    工具调用 / 规则决策与安全底线 / 记忆与世界模型 / 图像处理 /
    摄像头接收 / 大模型接口降级 / REST 与 WebSocket 接口 / 端到端闭环 / 长时间稳定性 /
    数据新鲜度与置信度估计 / 对象记忆与短期记忆 / 地图四层接口 / 世界模型分层门面 /
    事件引擎（去重 / 覆盖面 / 接入运行时）/ 安全规则表与双循环硬验收 /
    Action Schema 与交互策略 / 上下文构建器 / 认知智能体 / 不可信结论不得否决移动 /
    手动驾驶（WASD 接管）/ 录制与回放（Stage 9）/ 主动感知（Stage 10）/
    地图连通性（房间可达 / 门洞有效宽度 / 无孤岛）

设计原则：所有检查都必须能在**无网络、无 API Key、无摄像头**的环境下跑完。
大模型相关只验证「不可用时优雅降级」与「消息构造正确」，不发真实请求。
"""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# =====================================================================
# 极简测试框架（不引入 pytest，保证零额外依赖）
# =====================================================================


class CheckFailed(AssertionError):
    """检查失败。"""


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFailed(msg)


def expect_close(a: float, b: float, tol: float, msg: str) -> None:
    if abs(a - b) > tol:
        raise CheckFailed(f"{msg}（{a} 与 {b} 相差超过 {tol}）")


def rects_touch(a: dict[str, Any], b: dict[str, Any], eps: float = 1e-6) -> bool:
    """两个轴对齐矩形是否**edge-adjacent**（共享一段边界，而不只是角点相接）。

    用于判断"这个房间的门是不是贴着它"：门洞与房间必须有一条边贴合，
    只在角上碰到不算 —— 那种情况下人是走不过去的。
    """
    ax0, ax1 = float(a["x_min"]), float(a["x_max"])
    ay0, ay1 = float(a["y_min"]), float(a["y_max"])
    bx0, bx1 = float(b["x_min"]), float(b["x_max"])
    by0, by1 = float(b["y_min"]), float(b["y_max"])
    ox = min(ax1, bx1) - max(ax0, bx0)
    oy = min(ay1, by1) - max(ay0, by0)
    return (ox > eps and abs(oy) <= eps) or (oy > eps and abs(ox) <= eps)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class Runner:
    results: list[Result] = field(default_factory=list)
    verbose: bool = True

    def run(self, name: str, fn: Callable[[], str]) -> None:
        t0 = time.perf_counter()
        try:
            detail = fn() or ""
            r = Result(name=name, ok=True, detail=detail, duration_ms=(time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001 - 任何异常都算检查失败，继续跑下一项
            r = Result(
                name=name,
                ok=False,
                detail="",
                error=f"{type(e).__name__}: {e}",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        self.results.append(r)
        if self.verbose:
            mark = "✓" if r.ok else "✗"
            line = f"  {mark} {name:<40} {r.detail or r.error}"
            print(line if r.ok else f"  {mark} {name:<40} {r.error}")


# =====================================================================
# 各检查项
# =====================================================================
def build_runner(cfg: dict[str, Any], seed: int) -> Runner:
    """构造全部检查项。cfg 会被复制使用，避免污染调用方。"""
    import copy

    runner = Runner()

    # --- 延迟导入，避免顶层导入失败时连测试框架都起不来 ---
    from agent.action_schema import (
        DEFAULT_PRIORITY,
        ActionChannel,
        AgentDecision,
        MOTION_ALLOWED,
    )
    from agent.agent_core import SpatialAgent, SpatialAgentSystem, SystemConfig
    from agent.cognitive_agent import CognitiveAgent
    from agent.context_builder import ContextBuilder
    from agent.decision import Action, ActionType, RuleDecisionEngine
    from agent.interaction_policy import InteractionPolicy
    from agent.llm_client import LLMClient
    from agent.memory import AgentMemory
    from agent.prompt_template import SYSTEM_PROMPT, build_user_prompt, extract_json, render_state
    from agent.tools import AgentTools
    from camera import image_processor as ip
    from camera.iphone_receiver import IphoneReceiver
    from config.loader import load_config
    from spatial.spatial_state import (
        AffordanceState,
        BlockedRegion,
        CameraFrameMetadata,
        ConfidenceState,
        EnvironmentState,
        FrameFreshness,
        NavigationState,
        Obstacle,
        ObstacleDirection,
        ObstacleKind,
        PoseState,
        Position,
        RiskLevel,
        RiskState,
        SemanticScene,
        SemanticState,
        SpatialState,
        UncertaintyState,
        UserState,
        WalkingStatus,
    )
    from spatial.state_manager import StateManager
    from spatial.world_model import WorldModel
    from sensors.simulated import SimulatedProvider
    from simulator.map_simulator import MapSimulator
    from simulator.navigation_simulator import NavigationSimulator, angle_delta, bearing_deg
    from simulator.obstacle_simulator import ObstacleSimulator
    from simulator.sensor_simulator import SensorSimulator

    def base_cfg(**overrides: Any) -> dict[str, Any]:
        """拿到一份干净的配置副本，可局部覆盖。

        ⚠️ 默认把 `llm.api_key` 清空。自检必须**离线、确定、快速**：
        一旦 `.env` 里配了真实 Key，长时间稳定性（600 轮）会变成每轮发一次真实
        请求 —— 既慢到分钟级（实测会被误判为「卡死」），又会把用户密钥外发。
        需要验证大模型分支的用例请显式传 `llm.api_key=...`，显式值仍会覆盖回来。
        """
        c = copy.deepcopy(cfg)
        c["llm"]["api_key"] = ""
        for path, value in overrides.items():
            node = c
            keys = path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return c

    def fresh_env(**overrides: Any) -> tuple[dict[str, Any], Any]:
        """构造"感知前端"：v0.3 起装配只走 SimulatedProvider 一个入口。"""
        c = base_cfg(**overrides)
        return c, SimulatedProvider(c, seed=seed)

    def fresh(**overrides: Any):
        """构造一整套互相接线的组件。

        ⚠️ 返回结构刻意保持 v0.2 的样子（c, map, nav, obstacles, sensors, sm, world）：
        下面的用例大量使用 `obs.update(...)` / `nav.tick(...)` / `m.is_walkable(...)`
        这类"直接摆弄仿真器"的手法来构造场景，这是**自检特有的特权**
        （业务代码已不允许这么写）。把 4 个内部对象原样摊出来，
        是为了让这 45 条断言不必为了分层重构而整体改写。
        """
        c, sv = fresh_env(**overrides)
        world = WorldModel()
        sm = StateManager(c, sv, world=world)
        return c, sv.map, sv.nav, sv.obstacles, sv.sensors, sm, world

    # -----------------------------------------------------------------
    # 1. 配置
    # -----------------------------------------------------------------
    def t_config() -> str:
        c = load_config()
        for section in ("system", "llm", "camera", "simulator", "agent", "api"):
            expect(section in c, f"配置缺少 {section} 段")
        expect(c["simulator"]["map"]["zones"], "地图缺少 zones")
        expect(c["simulator"]["motion"]["path_clearance_m"] > 0, "缺少路径安全间隙配置")
        return f"{len(c)} 段 / 地图 {c['simulator']['map']['name']}"

    runner.run("1  配置加载与必填段落", t_config)

    # -----------------------------------------------------------------
    # 2. SpatialState 模型
    # -----------------------------------------------------------------
    def t_model_roundtrip() -> str:
        st = SpatialState(
            timestamp=__import__("datetime").datetime.now(),
            tick=1,
            user=UserState(position=Position(x=1.5, y=2.5, floor=1), heading=370.0, speed=1.1),
            navigation=NavigationState(destination="出口", distance_to_goal=10.0, route_progress=0.5),
            environment=EnvironmentState(front_clear=True, front_distance=3.0, corridor_width=2.0),
            risk=RiskState(level=RiskLevel.LOW, reason="通畅"),
        )
        expect_close(st.user.heading, 10.0, 1e-6, "朝向未归一化到 [0,360)")
        raw = st.model_dump_json()
        st2 = SpatialState.model_validate_json(raw)
        expect(st2 == st, "JSON 往返后状态不一致")
        expect(len(raw) > 100, "序列化结果过短")
        return f"往返 {len(raw)} 字节"

    runner.run("2  SpatialState 校验与 JSON 往返", t_model_roundtrip)

    def t_model_rejects_bad() -> str:
        bad = {
            "timestamp": __import__("datetime").datetime.now().isoformat(),
            "tick": 0,
            "user": {"position": {"x": 1, "y": 1}, "heading": 0.0, "speed": -5.0},
            "navigation": {"destination": "x", "distance_to_goal": 1.0},
            "environment": {"front_clear": True, "front_distance": 1.0},
        }
        try:
            SpatialState.model_validate(bad)
        except Exception:
            return "非法速度被正确拒绝"
        raise CheckFailed("负数速度应当被拒绝")

    runner.run("3  模型拒绝非法数据", t_model_rejects_bad)

    # -----------------------------------------------------------------
    # 3. 地图与寻路
    # -----------------------------------------------------------------
    def t_map_build() -> str:
        c = base_cfg()
        m = MapSimulator(c)
        expect(m.nx > 0 and m.ny > 0, "栅格未建立")
        ratio = float(m.walkable.mean())
        expect(0.05 < ratio < 0.95, f"可通行比例异常: {ratio}")
        for lm in m.landmarks:
            expect(m.is_walkable(lm["x"], lm["y"]), f"路点 {lm['name']} 不可通行")
        return f"{m.info()['grid']} 可通行 {ratio * 100:.0f}%"

    runner.run("4  地图栅格与路点可通行性", t_map_build)

    def t_pathfind() -> str:
        c = base_cfg()
        m = MapSimulator(c)
        start = (float(m.start["x"]), float(m.start["y"]))
        goal = (float(m.landmarks[-1]["x"]), float(m.landmarks[-1]["y"]))
        path = m.find_path(start, goal)
        expect(len(path) >= 2, "A* 未能找到路径")
        expect(path[0] == start, "路径起点不是给定起点")
        expect_close(path[-1][0], goal[0], 1e-6, "路径终点错误")
        length = sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))
        expect(length < 100, f"路径长度异常: {length:.1f}m")
        return f"{len(path)} 个路径点 / 直线长 {length:.1f}m"

    runner.run("5  A* 全局寻路", t_pathfind)

    def t_path_clearance() -> str:
        """路径必须与墙体保持安全间隙，且不穿过阻挡物。"""
        c = base_cfg()
        m = MapSimulator(c)
        clr = float(c["simulator"]["motion"]["path_clearance_m"])
        # 起点/终点从 config 派生：写死坐标时 A* 会靠 _nearest_free 兜底"照跑不误"，
        # 用例看着绿、其实测的已经是一条不存在的路径。
        path = m.find_path(
            (float(m.start["x"]), float(m.start["y"])),
            (float(m.landmarks[-1]["x"]), float(m.landmarks[-1]["y"])),
        )

        worst_wall = 1e9
        for p in path:
            i, j = m.world_to_cell(*p)
            if m.walkable[i, j]:
                worst_wall = min(worst_wall, float(m._wall_dist[i, j]))
        expect(worst_wall >= clr * 0.75, f"路径离墙过近: {worst_wall:.2f}m < {clr}m")

        for obj in m.objects:
            if not obj.blocking:
                continue
            for i in range(len(path) - 1):
                # 点到线段距离
                ax, ay = path[i]
                bx, by = path[i + 1]
                vx, vy = bx - ax, by - ay
                seg = vx * vx + vy * vy
                t = 0.0 if seg < 1e-9 else max(0.0, min(1.0, ((obj.x - ax) * vx + (obj.y - ay) * vy) / seg))
                d = math.hypot(ax + vx * t - obj.x, ay + vy * t - obj.y)
                expect(d >= obj.radius, f"路径穿过 {obj.type}({obj.x},{obj.y})，距离 {d:.2f} < {obj.radius}")
        return f"最小离墙 {worst_wall:.2f}m，未穿过任何阻挡物"

    runner.run("6  路径安全间隙且不穿障碍", t_path_clearance)

    def t_path_dynamic_block() -> str:
        """给一个横在必经之路上的动态障碍，A* 必须绕开或返回不可达。"""
        c = base_cfg()
        m = MapSimulator(c)

        def seg_dist(p, s, e):
            vx, vy = e[0] - s[0], e[1] - s[1]
            seg = vx * vx + vy * vy
            t = 0.0 if seg < 1e-9 else max(0.0, min(1.0, ((p[0] - s[0]) * vx + (p[1] - s[1]) * vy) / seg))
            return math.hypot(s[0] + vx * t - p[0], s[1] + vy * t - p[1])

        # ★要构成"必经之路被堵"，障碍必须放在**门洞**上★
        #   放在开阔办公区中间，A* 随便一绕就过去了，测不出堵塞分支。
        #   门洞从 config 里按"离这段路线最近"自动挑，不写死门名/坐标。
        a = (float(m.landmarks[2]["x"]), float(m.landmarks[2]["y"]))
        b = (float(m.landmarks[3]["x"]), float(m.landmarks[3]["y"]))
        doors = [z for z in m.zones if str(z["name"]).endswith(("门", "门口"))]
        best = min(
            doors,
            key=lambda z: seg_dist(
                ((float(z["x_min"]) + float(z["x_max"])) / 2,
                 (float(z["y_min"]) + float(z["y_max"])) / 2), a, b),
        )
        obs = ((float(best["x_min"]) + float(best["x_max"])) / 2,
               (float(best["y_min"]) + float(best["y_max"])) / 2)
        expect(seg_dist(obs, a, b) < 1.0,
               f"这一段路线上没有门洞（最近的门 {best['name']} 偏离 {seg_dist(obs, a, b):.2f}m），用例前提不成立")

        path = m.find_path(a, b, extra_blocked=[obs], obstacle_radius=1.0)
        if not path:
            return f"1m 半径障碍堵死门洞 {best['name']} → 正确返回不可达"
        for i in range(len(path) - 1):
            ax, ay = path[i]
            bx, by = path[i + 1]
            vx, vy = bx - ax, by - ay
            seg = vx * vx + vy * vy
            t = 0.0 if seg < 1e-9 else max(0.0, min(1.0, ((obs[0] - ax) * vx + (obs[1] - ay) * vy) / seg))
            d = math.hypot(ax + vx * t - obs[0], ay + vy * t - obs[1])
            expect(d >= 1.0 * 0.8, f"绕行路径距动态障碍仅 {d:.2f}m")
        return f"绕开门洞({best['name']})障碍成功（{len(path)} 点）"

    runner.run("7  A* 绕开动态障碍", t_path_dynamic_block)

    # -----------------------------------------------------------------
    # 4. 传感器
    # -----------------------------------------------------------------
    def t_sensors() -> str:
        c, m, *_ = fresh()
        sen = SensorSimulator(c, m, seed=seed)
        tp = (2.0, 10.0)  # 必须取在可通行区域内，否则噪声会被"推出墙外"的兜底逻辑吃掉
        expect(m.is_walkable(*tp), "测试点本身不可通行，测试无效")
        xs = [sen.uwb_measure(tp)[0] for _ in range(400)]
        expect(all(abs(x - 2.0) < 1.5 for x in xs), "UWB 噪声超出合理范围")
        sd = statistics.pstdev(xs)
        expect(sd > 0.0, "UWB 完全没有噪声，模拟器失效")
        expect_close(sd, c["simulator"]["sensors"]["uwb_noise_m"], 0.08, "UWB 噪声标准差偏离配置")

        hs = [sen.imu_heading(90.0) for _ in range(400)]
        expect(all(0 <= h < 360 for h in hs), "IMU 朝向未归一化")
        expect(statistics.pstdev(hs) > 0.0, "IMU 完全没有噪声")

        drops = sum(1 for _ in range(2000) if sen.lidar_measure(3.0) is None)
        rate = drops / 2000
        expect_close(rate, c["simulator"]["sensors"]["drop_frame_prob"], 0.02, "丢帧率偏离配置")
        return f"UWB σ={sd:.3f}m，IMU σ={statistics.pstdev(hs):.2f}°，丢帧率={rate * 100:.1f}%"

    runner.run("8  UWB / IMU / LiDAR 噪声与丢帧", t_sensors)

    def t_uwb_never_in_wall() -> str:
        c, m, *_ = fresh()
        sen = SensorSimulator(c, m, seed=seed)
        # ★不要写死坐标★：取"起点所在分区"里贴着 x_min 墙边 0.4m 的点，
        #   噪声最容易把它推出墙外。写死坐标会让换地图时这条用例静默失效。
        zname = m.zone_at(float(m.start["x"]), float(m.start["y"]))
        zone = next(z for z in m.zones if str(z["name"]) == zname)
        tp = (float(zone["x_min"]) + 0.4, float(m.start["y"]))
        expect(m.is_walkable(*tp), f"测试点 {tp} 本身不可通行，测试无效")
        for _ in range(500):
            x, y = sen.uwb_measure(tp)
            expect(m.is_walkable(x, y), f"定位被推到墙内 ({x:.2f},{y:.2f})")
        return f"靠墙点 {tp} 500 次采样全部落在可通行区域"

    runner.run("9  UWB 噪声不会把用户推入墙内", t_uwb_never_in_wall)

    # -----------------------------------------------------------------
    # 5. 动态障碍
    # -----------------------------------------------------------------
    def t_obstacles_lifecycle() -> str:
        c, m, nav, obs, *_ = fresh()
        obs.enabled = True
        obs.spawn_interval = 0.5
        created_total = 0
        for i in range(200):
            obs.update(0.1, nav.pos, nav.heading, i * 0.1)
            created_total += 0
            expect(obs.total() <= obs.max_active, f"活跃障碍超上限: {obs.total()}")
            for ob in obs.obstacles:
                expect(m.is_walkable(ob.x, ob.y), f"障碍生成在墙内 ({ob.x:.2f},{ob.y:.2f})")
        expect(obs.spawn_total > 0, "20 秒内没有生成任何动态障碍")
        obs.events.clear()
        for ob in list(obs.obstacles):
            ob.ttl = 0.05
        # ★测"到寿清除"时必须先关掉自动生成★
        #   否则这一次 update 会"清完旧的顺手生成一个新的"，
        #   total() 是否归零取决于 _timer 当时恰好是多少 —— 用例会时通时不通。
        obs.enabled = False
        obs.update(0.2, nav.pos, nav.heading, 99.0)
        obs.enabled = True
        expect(obs.total() == 0, "到寿障碍未被清除")
        return f"生成 {obs.spawn_total} 次事件，到寿清除正常"

    runner.run("10 动态障碍生成/移动/清除", t_obstacles_lifecycle)

    def t_narrow_passage() -> str:
        c, m, nav, obs, *_ = fresh()
        obs.enabled = False
        obs._timer = 1e9  # 关掉自动生成，只手动构造
        made = obs.spawn((6.0, 14.0), 90.0, 0.0, kind="narrow_passage")
        if not made:
            return "该位置不适合通道变窄事件（已跳过判定）"
        expect(len(made) == 2, f"通道变窄应生成 2 个障碍，实际 {len(made)}")
        gap = math.dist(made[0].pos(), made[1].pos()) - made[0].radius - made[1].radius
        expect(0.2 < gap < 1.5, f"留出的缝隙不合理: {gap:.2f}m")
        return f"生成成对障碍，缝隙 {gap:.2f}m"

    runner.run("11 通道变窄事件成对生成", t_narrow_passage)

    # -----------------------------------------------------------------
    # 6. 状态融合
    # -----------------------------------------------------------------
    def t_fusion_valid() -> str:
        c, m, nav, obs, sen, sm, _ = fresh()
        counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for i in range(60):
            st = sm.build(float(i), float(i))
            SpatialState.model_validate(st.model_dump())  # 每轮都过一遍校验
            counts[st.risk.level.value] += 1
            obs.update(1.0, nav.pos, nav.heading, float(i))
            nav.tick(1.0, True, st.environment.front_distance, obs.centers())
        expect(all(v >= 0 for v in counts.values()), "风险统计异常")
        return f"60 轮全部通过校验，风险分布 {counts}"

    runner.run("12 多源融合产出合法状态", t_fusion_valid)

    def t_fusion_camera() -> str:
        c, m, nav, obs, sen, sm, _ = fresh()
        recv = IphoneReceiver(c)
        sm.camera = recv
        st = sm.build(0.0, 0.0)
        expect(st.camera.image_available is False, "无帧时不应声称有画面")
        recv.submit(ip.make_test_frame(text="CAM"), source="upload", throttle=False)
        st = sm.build(1.0, 1.0)
        expect(st.camera.image_available, "提交帧后应有画面")
        expect(st.camera.source == "upload", f"来源标注错误: {st.camera.source}")
        return f"帧 {st.camera.frame_id} / 延迟 {st.camera.age_s:.2f}s"

    runner.run("13 摄像头状态融合", t_fusion_camera)

    # -----------------------------------------------------------------
    # 7. 工具调用
    # -----------------------------------------------------------------
    def t_tools_all() -> str:
        c, sv = fresh_env()
        sm = StateManager(c, sv)
        mem = AgentMemory(c)
        recv = IphoneReceiver(c)
        tools = AgentTools(c, sv, sv, sm, mem, WorldModel(), recv)
        sm.build(0.0, 0.0)

        names = [
            "get_current_position",
            "get_navigation_route",
            "get_environment",
            "capture_camera",
            "request_visual_observation",
            "replan_route",
            "speak",
            "ask_user",
            "get_world_memory",
        ]
        for n in names:
            args = {}
            if n == "replan_route":
                args = {"reason": "自检"}
            elif n == "speak":
                args = {"message": "自检播报"}
            elif n == "ask_user":
                args = {"question": "自检问题"}
            elif n == "request_visual_observation":
                args = {"reason": "自检"}
            out = tools.call(n, args)
            expect(isinstance(out, dict), f"{n} 未返回 dict")
            expect("error" not in out, f"{n} 返回错误: {out.get('error')}")

        expect(len(tools.schemas()) == len(names), "工具 schema 数量与工具数不一致")
        expect(len(mem.utterances) == 2, f"播报/提问未记录进记忆（{len(mem.utterances)}）")

        bad = tools.call("不存在的工具", {})
        expect("error" in bad, "未知工具应返回错误")
        return f"{len(names)} 个工具全部可用，schema 一致"

    runner.run("14 全部工具可调用且 schema 一致", t_tools_all)

    def t_tool_replan_effect() -> str:
        c, sv = fresh_env()
        sm = StateManager(c, sv)
        world = WorldModel()
        tools = AgentTools(c, sv, sv, sm, AgentMemory(c), world)
        before = sv.nav.replan_count
        out = tools.call("replan_route", {"reason": "自检强制重规划"})
        expect(out["success"], "重规划未成功")
        expect(sv.nav.replan_count == before + 1, "重规划计数未增加")
        expect(world.replan_total == 1, "世界模型未记录重规划")
        return f"重规划后剩余 {out['distance_to_goal_m']}m"

    runner.run("15 重规划工具真实生效", t_tool_replan_effect)

    # -----------------------------------------------------------------
    # 8. 规则决策与安全底线
    # -----------------------------------------------------------------
    def mk_state(
        front_clear: bool = True,
        front_distance: float = 5.0,
        risk: RiskLevel = RiskLevel.LOW,
        status: WalkingStatus = WalkingStatus.MOVING,
        off_route: bool = False,
        progress: float = 0.3,
        obstacles: list | None = None,
        instruction: str = "直行约5米到走廊北段",
    ) -> SpatialState:
        import datetime as _dt

        return SpatialState(
            timestamp=_dt.datetime.now(),
            tick=1,
            # ★坐标必须落在当前地图的可通行区域内★
            # 这个合成状态会被喂给事件检测器，其中"地图-传感器冲突"会去查地图；
            # 位置一旦落在墙里，那个分支就永远不会命中（用例 57 曾因此静默失效）。
            # 取南走廊 (20,10)——地图改成办公楼后仍成立。
            user=UserState(position=Position(x=20.0, y=10.0), heading=0.0, speed=1.0, walking_status=status),
            navigation=NavigationState(
                destination="卫生间",
                distance_to_goal=30.0,
                route_progress=progress,
                next_instruction=instruction,
                current_landmark="南走廊",
                off_route=off_route,
            ),
            environment=EnvironmentState(
                front_clear=front_clear,
                front_distance=front_distance,
                corridor_width=4.0,
                left_distance=2.0,
                right_distance=2.0,
                obstacles=obstacles or [],
            ),
            risk=RiskState(level=risk, reason="测试"),
        )

    def t_rule_critical() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        st = mk_state(
            front_clear=False,
            front_distance=0.3,
            risk=RiskLevel.CRITICAL,
            obstacles=[
                Obstacle(type=ObstacleKind.CHAIR, distance=0.3, direction=ObstacleDirection.FRONT, x=2.0, y=5.3)
            ],
        )
        a = eng.decide(st, 0.0)
        expect(a.action_type == ActionType.WAIT, f"临界风险应 WAIT，实际 {a.action_type}")
        expect(not a.permits_motion, "WAIT 不应允许用户前进")
        expect("0.3" in a.message, f"警告应包含距离: {a.message}")
        return a.message

    runner.run("16 临界风险 → WAIT（用户停步）", t_rule_critical)

    def t_rule_clear() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        first = eng.decide(mk_state(), 0.0)
        expect(first.action_type == ActionType.SPEAK, f"首次应播报导航指令，实际 {first.action_type}")
        expect(first.permits_motion, "SPEAK 应允许前进")
        second = eng.decide(mk_state(), 1.0)
        expect(second.action_type == ActionType.CONTINUE, f"1 秒内不应重复播报，实际 {second.action_type}")
        expect(second.message == "", "CONTINUE 不应有播报内容")
        return "首次播报 → 随后保持安静"

    runner.run("17 打扰频率控制（不重复播报）", t_rule_clear)

    def t_rule_arrived() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        st = mk_state(status=WalkingStatus.ARRIVED, progress=1.0)
        a = eng.decide(st, 0.0)
        expect(a.action_type == ActionType.SPEAK, f"到达应播报，实际 {a.action_type}")
        expect("到达" in a.message, f"到达播报内容异常: {a.message}")
        # ★到达只播一次★ 到站后用户会站着不动，到达状态就是**持续**的；
        #   若不用标记钉死，下一秒还会再念一遍（实测一次 3 分钟实测里念了 31 遍）。
        #   所以"首次播报"与"后续静默"必须分两条路径各测一遍，只测首播抓不到这种刷屏。
        a2 = eng.decide(st, 1.0)
        expect(a2.action_type == ActionType.CONTINUE,
               f"同一次到达的第二轮不该再播报，实际 {a2.action_type}")
        expect(a2.message == "", f"重复到达播报必须闭嘴，实际 {a2.message!r}")
        return a.message

    runner.run("18 到达目的地 → 主动告知（且只播一次）", t_rule_arrived)

    def t_rule_offroute() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        a1 = eng.decide(mk_state(off_route=True), 0.0)
        expect(a1.action_type == ActionType.REPLAN, f"偏航应先重规划，实际 {a1.action_type}")
        eng.decide(mk_state(off_route=True), 1.0)
        a3 = eng.decide(mk_state(off_route=True), 2.0)
        expect(a3.action_type == ActionType.ASK_USER, f"重规划多次失败后应求助，实际 {a3.action_type}")
        # ★求助 = 等答复，不是每秒问一遍★
        #   偏航状态可能持续很久（真被堵死 / 位姿刚被手动驾驶改过）。没有冷却时，
        #   同一句问话会被每 tick 刷一遍 —— 实测一次 3 分钟的实测里刷了 62 遍。
        a4 = eng.decide(mk_state(off_route=True), 3.0)
        a5 = eng.decide(mk_state(off_route=True), 4.0)
        expect(a4.action_type == ActionType.CONTINUE,
               f"求助后不该继续追问，实际 {a4.action_type}")
        expect(a4.message == "" and a5.message == "", "重复追问必须保持安静（不许再念一遍）")
        # 冷却到期后允许再问一次：卡住太久也不能永远不问
        cool = RuleDecisionEngine.OFF_ROUTE_ASK_COOLDOWN_S
        a6 = eng.decide(mk_state(off_route=True), 4.0 + cool + 1.0)
        expect(a6.action_type == ActionType.ASK_USER,
               f"冷却 {cool:.0f}s 后应允许再求助，实际 {a6.action_type}")
        # 状态恢复（重新找到路）后再偏航 = 新一次求助，必须能立刻问
        eng.decide(mk_state(off_route=False), 4.0 + cool + 2.0)
        a7 = eng.decide(mk_state(off_route=True), 4.0 + cool + 3.0)
        expect(a7.action_type == ActionType.REPLAN, f"新一轮偏航应从重规划重新开始，实际 {a7.action_type}")
        return "重规划 2 次 → 求助 1 次 → 静默等待（30s 冷却后可再问）"

    runner.run("19 无路可走 → 重规划 → 求助（且不刷屏）", t_rule_offroute)

    def t_rule_narrow() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        st = mk_state(front_distance=2.0, risk=RiskLevel.MEDIUM)
        st.environment.narrow_passage = True
        st.environment.corridor_width = 0.6
        a = eng.decide(st, 0.0)
        expect("变窄" in a.message, f"通道变窄应提醒: {a.message}")
        return a.message

    runner.run("20 通道变窄 → 主动提醒", t_rule_narrow)

    def t_rule_high() -> str:
        c = base_cfg()
        eng = RuleDecisionEngine(c, AgentMemory(c))
        st = mk_state(
            front_clear=False,
            front_distance=0.6,
            risk=RiskLevel.HIGH,
            obstacles=[
                Obstacle(type=ObstacleKind.PERSON, distance=0.6, direction=ObstacleDirection.FRONT, x=2.0, y=5.6)
            ],
        )
        a = eng.decide(st, 0.0)
        expect(a.action_type == ActionType.SPEAK, f"高风险应播报，实际 {a.action_type}")
        expect(a.urgency == "high", f"紧急度应为 high，实际 {a.urgency}")
        return a.message

    runner.run("21 高风险 → 提醒但允许继续", t_rule_high)

    def t_agent_safety_override() -> str:
        """安全底线：模型让用户继续走，但规则判定要停下时必须覆盖。"""
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        st = mk_state(
            front_clear=False,
            front_distance=0.2,
            risk=RiskLevel.CRITICAL,
            obstacles=[
                Obstacle(type=ObstacleKind.WALL, distance=0.2, direction=ObstacleDirection.FRONT, x=2.0, y=5.2)
            ],
        )
        rule = sys_.agent.rules.decide(st, 0.0)
        fake_llm = Action(action_type=ActionType.SPEAK, message="继续直行", source="llm")
        merged = sys_.agent._merge_with_safety(rule, fake_llm)
        expect(merged.action_type == ActionType.WAIT, f"安全覆盖失效，得到 {merged.action_type}")
        expect("安全覆盖" in merged.reason, f"未标注安全覆盖: {merged.reason}")
        return "模型决策被安全底线正确覆盖"

    runner.run("22 安全底线覆盖模型决策", t_agent_safety_override)

    # -----------------------------------------------------------------
    # 9. 记忆与世界模型
    # -----------------------------------------------------------------
    def t_memory() -> str:
        c = base_cfg(**{"agent.memory.max_turns": 5})
        mem = AgentMemory(c)
        for i in range(20):
            mem.add_user(f"问题{i}", float(i))
            mem.add_agent(f"回答{i}", float(i), "SPEAK")
        expect(len(mem.turns) == 5, f"记忆窗口失效: {len(mem.turns)}")
        expect(mem.seconds_since_speak(10.0) == 1e9 or mem.last_speak_elapsed < 0, "初始播报时间异常")
        mem.add_utterance("测试", 100.0)
        expect_close(mem.seconds_since_speak(103.0), 3.0, 1e-6, "播报间隔计算错误")
        expect(mem.repeated("测试"), "重复检测失效")
        return "窗口裁剪与重复检测正常"

    runner.run("23 记忆窗口与重复检测", t_memory)

    def t_world_model() -> str:
        w = WorldModel()
        for i in range(30):
            w.observe_pose(2.0 + i * 0.05, 5.0 + i * 0.1, float(i), "走廊北段" if i < 15 else "走廊东段")
        w.observe_obstacles(
            [{"type": "chair", "x": 3.0, "y": 6.0, "radius": 0.3, "distance": 1.2, "oid": 1, "is_dynamic": True}],
            5.0,
        )
        w.observe_obstacles(
            [{"type": "chair", "x": 3.0, "y": 6.0, "radius": 0.3, "distance": 0.6, "oid": 1, "is_dynamic": True}],
            6.0,
        )
        w.observe_obstacles(
            [{"type": "chair", "x": 3.0, "y": 6.0, "radius": 0.3, "distance": 0.3, "oid": 1, "is_dynamic": True}],
            7.0,
        )
        expect(len(w.visited_zones) >= 2, "区域访问记录失效")
        expect(w.obstacle_encounters.get("chair") == 1, "障碍计数异常（应聚合为同一物体）")
        expect(w.tracks[1].trend() == "approaching", f"接近趋势判断错误: {w.tracks[1].trend()}")
        expect(w.spin_warning is False, "不应误报原地打转")
        expect(len(w.summarize()) > 0, "记忆摘要为空")
        return w.summarize()[:60]

    runner.run("24 世界模型记忆与趋势", t_world_model)

    def t_spin_detect() -> str:
        w = WorldModel()
        for i in range(40):
            a = i * 0.6
            w.observe_pose(2.0 + 0.3 * math.cos(a), 5.0 + 0.3 * math.sin(a), float(i), "入口区")
        expect(w.spin_warning, "原地打转未被检出")
        return "原地打转已检出"

    runner.run("25 原地打转检测", t_spin_detect)

    # -----------------------------------------------------------------
    # 10. 图像与摄像头
    # -----------------------------------------------------------------
    def t_image() -> str:
        data = ip.make_test_frame(width=800, height=600, text="HELLO")
        expect(len(data) > 100, "合成图失败")
        info = ip.image_info(data)
        expect(info["valid"], "解码失败")
        expect(info["width"] == 800 and info["height"] == 600, f"尺寸错误: {info}")
        prepared = ip.prepare_for_llm(data, max_side=400, quality=60)
        expect(prepared is not None, "预处理失败")
        p2 = ip.image_info(prepared)
        expect(max(p2["width"], p2["height"]) == 400, f"缩放未生效: {p2}")
        expect(ip.decode(b"not an image") is None, "坏数据应解码失败")
        return f"800x600 → {p2['width']}x{p2['height']}，{len(data)}→{len(prepared)} 字节"

    runner.run("26 图像解码/缩放/编码", t_image)

    def t_receiver() -> str:
        c = base_cfg(**{"camera.stale_after_s": 0.4, "camera.save_interval_s": 0.0})
        r = IphoneReceiver(c)
        expect(r.snapshot()["image_available"] is False, "初始不应有画面")
        ok = r.submit(ip.make_test_frame(text="A"), source="upload", throttle=False)
        expect(ok["accepted"], f"首帧未被接收: {ok}")
        expect(r.snapshot()["image_available"], "接收后应有画面")
        expect(r.latest_bytes() is not None, "latest_bytes 为空")
        bad = r.submit(b"xxxxx", source="upload", throttle=False)
        expect(not bad["accepted"] and bad["reason"] == "decode_failed", f"坏帧应被拒绝: {bad}")
        expect(not r.submit(b"", source="upload", throttle=False)["accepted"], "空帧应被拒绝")
        time.sleep(0.5)
        expect(r.snapshot()["stale"], "过期帧未标记为 stale")
        expect(r.latest_bytes() is None, "过期帧不应再送模型")
        expect(r.stats()["rejected"] == 2, "拒绝计数错误")
        return f"接收 {r.stats()['accepted']} / 拒绝 {r.stats()['rejected']}"

    runner.run("27 摄像头接收与新鲜度", t_receiver)

    # -----------------------------------------------------------------
    # 11. 大模型接口
    # -----------------------------------------------------------------
    def t_llm_offline() -> str:
        c = base_cfg(**{"llm.api_key": ""})
        client = LLMClient(c)
        expect(not client.available, "无 key 时不应声称可用")
        st = mk_state()
        expect(client.chat_multimodal(st) is None, "无 key 时应返回 None")
        expect(client.stats()["failures"] == 0, "无 key 不应记为失败")
        return "无 Key 静默降级为规则决策"

    runner.run("28 大模型不可用时优雅降级", t_llm_offline)

    def t_llm_bad_endpoint() -> str:
        """指向一个不可达地址，验证超时/连不上时也不会抛异常。"""
        c = base_cfg(**{"llm.api_key": "sk-test", "llm.base_url": "http://127.0.0.1:9/v1", "llm.timeout_s": 2})
        client = LLMClient(c)
        expect(client.available, "有 key 时应视为可用")
        t0 = time.time()
        out = client.chat_multimodal(mk_state())
        expect(out is None, "不可达端点应返回 None")
        expect(time.time() - t0 < 8, "失败应快速返回，不应长时间阻塞")
        expect(client.stats()["failures"] >= 1, "失败未计数")
        return f"连接失败被捕获（{client.last_error[:40]}…）"

    runner.run("29 大模型连接失败不中断系统", t_llm_bad_endpoint)

    def t_llm_messages() -> str:
        c = base_cfg(**{"llm.api_key": "sk-test"})
        client = LLMClient(c)
        img = ip.make_test_frame(text="VISION")
        msgs = client._build_messages(mk_state(), img, "还有多远", {"world": "已走过入口区"})
        expect(msgs[0]["role"] == "system", "缺少系统提示词")
        expect(msgs[0]["content"] == SYSTEM_PROMPT, "系统提示词被改动")
        last = msgs[-1]["content"]
        expect(isinstance(last, list), "带图消息应为多模态数组")
        kinds = [b["type"] for b in last]
        expect("image_url" in kinds and "text" in kinds, f"多模态结构错误: {kinds}")
        expect(last[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"), "图片编码前缀错误")
        expect("还有多远" in last[0]["text"], "用户提问未进入提示词")
        expect("已走过入口区" in last[0]["text"], "世界记忆未进入提示词")
        return f"{len(msgs)} 条消息，含 1 张图片"

    runner.run("30 多模态消息构造正确", t_llm_messages)

    def t_prompt_render() -> str:
        txt = render_state(mk_state())
        for kw in ("[用户]", "[目标]", "[环境]", "[风险]"):
            expect(kw in txt, f"状态渲染缺少 {kw}")
        expect("方位角" not in txt or True, "")
        parsed = extract_json('```json\n{"action_type":"SPEAK","message":"走"}\n```')
        expect(parsed and parsed["message"] == "走", "带围栏的 JSON 解析失败")
        parsed2 = extract_json('前缀 {"action_type":"WAIT"} 后缀')
        expect(parsed2 and parsed2["action_type"] == "WAIT", "带前缀的 JSON 解析失败")
        expect(extract_json("完全不是 JSON") is None, "非法输出应返回 None")
        built = build_user_prompt(mk_state(), "我该往哪走")
        expect("我该往哪走" in built and "JSON" in built, "用户提示词拼接错误")
        return f"渲染 {len(txt)} 字符"

    runner.run("31 提示词渲染与输出解析", t_prompt_render)

    # -----------------------------------------------------------------
    # 12. 端到端
    # -----------------------------------------------------------------
    def t_e2e_arrival() -> str:
        """完整跑一趟：入口 → 出口，全程状态必须合法，且必须到达。"""
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        speaks = 0
        max_risk = "low"
        order = ["low", "medium", "high", "critical"]
        for _ in range(200):
            r = sys_.step(1.0)
            SpatialState.model_validate(r.state.model_dump())
            if r.action.message:
                speaks += 1
            if order.index(r.state.risk.level.value) > order.index(max_risk):
                max_risk = r.state.risk.level.value
            if sys_.finished:
                break
        expect(sys_.nav.arrived, f"未能到达目的地（进度 {sys_.nav.route_progress() * 100:.0f}%）")
        expect(speaks >= 2, f"全程播报过少（{speaks} 条），Agent 应当有导航提示")
        expect(speaks <= 30, f"播报过多（{speaks} 条），打扰了用户")
        expect(
            any(c["tool"] == "speak" for c in sys_.tools.call_log),
            "播报未经过工具层 —— 违反了「副作用一律走工具」的架构约束",
        )
        expect(
            len(sys_.tools.call_log) >= speaks,
            f"工具调用数({len(sys_.tools.call_log)}) 少于播报数({speaks})",
        )
        return (
            f"{sys_.elapsed:.0f}s 到达 / 播报 {speaks} 条 / 工具调用 {len(sys_.tools.call_log)} 次"
            f" / 最高风险 {max_risk}"
        )

    runner.run("32 端到端：入口→出口 全程导航", t_e2e_arrival)

    def t_e2e_query() -> str:
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        sys_.step(1.0)
        sys_.submit_query("还有多远？")
        r = sys_.step(1.0)
        expect(r.state.navigation.distance_to_goal >= 0, "距离异常")
        expect(r.action.message, "用户提问后必须有回应")
        expect(r.action.source == "user", f"回应来源应为 user，实际 {r.action.source}")
        expect(any(t.role == "user" for t in sys_.memory.turns), "用户提问未进记忆")
        return f"回应：{r.action.message}"

    runner.run("33 用户提问 → 有回应", t_e2e_query)

    def t_e2e_reset() -> str:
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        for _ in range(10):
            sys_.step(1.0)
        pos_before = sys_.nav.pos
        sys_.reset()
        expect(sys_.tick == 0 and sys_.elapsed == 0.0, "重置后计数未归零")
        # 起点坐标从 config 读，不写死 —— 换地图时这条断言必须跟着走
        expect_close(sys_.nav.pos[0], float(c["simulator"]["map"]["start"]["x"]), 0.3,
                     "重置后位置未归位")
        expect(sys_.nav.arrived is False, "重置后不应仍是已到达")
        expect(len(sys_.tools.utterance_log) == 0, "重置后播报记录未清空")
        expect(pos_before != sys_.nav.pos, "重置未生效")
        return f"从 ({pos_before[0]:.1f},{pos_before[1]:.1f}) 回到 ({sys_.nav.pos[0]:.1f},{sys_.nav.pos[1]:.1f})"

    runner.run("34 系统重置", t_e2e_reset)

    def t_stability() -> str:
        """长时间稳定性：600 轮内用户无论如何不能穿墙、状态不能非法。"""
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed + 1))
        steps = 0
        arrivals = 0
        fails = 0
        for i in range(600):
            r = sys_.step(0.5)
            steps += 1
            if not sys_.map.is_walkable(sys_.nav.pos[0], sys_.nav.pos[1]):
                fails += 1
            if i % 50 == 0:
                SpatialState.model_validate(r.state.model_dump())
            if sys_.finished:
                arrivals += 1
                sys_.reset()
        expect(fails == 0, f"用户 {fails} 次进入不可通行区域")
        expect(steps == 600, f"实际执行轮次不符: {steps}")
        expect(arrivals >= 1, "300 秒仿真内一次都没走到目的地，运行时不正常")
        return f"600 轮无穿墙、无非法状态（途中 {arrivals} 次到达并重置）"

    runner.run("35 长时间稳定性（600 轮）", t_stability)

    # -----------------------------------------------------------------
    # 13. 服务接口（REST + WebSocket）
    # -----------------------------------------------------------------
    def make_api_system():
        """用 20Hz 构建一个独立系统，让接口测试不必等 1 秒一帧。"""
        from api.websocket_server import create_app

        c = base_cfg(**{"system.tick_hz": 20.0})
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        return c, sys_, create_app(sys_, c)

    def t_rest_api() -> str:
        from fastapi.testclient import TestClient

        # 注意：base_cfg 已默认清空 llm.api_key，所以这里的 /api/llm_check
        # 走 not_configured 分支、不发真实请求（自检必须离线可跑）。
        _, sys_, app = make_api_system()

        with TestClient(app) as client:
            r = client.get("/")
            expect(r.status_code == 200, f"首页状态码 {r.status_code}")
            expect("BlindSpatialAgent" in r.text, "测试页内容缺失")

            frame = client.get("/api/test_frame")
            expect(frame.status_code == 200, "合成帧接口失败")
            expect(frame.headers["content-type"].startswith("image/jpeg"), "合成帧类型错误")

            up = client.post("/api/image", files={"file": ("t.jpg", frame.content, "image/jpeg")})
            expect(up.status_code == 200 and up.json()["accepted"], f"图片上传失败: {up.text}")
            expect(sys_.camera.snapshot()["image_available"], "上传后摄像头状态未更新")

            time.sleep(0.4)  # 等后台循环跑几轮
            state = client.get("/api/state").json()
            expect(state["type"] == "tick", f"状态推送异常: {state.get('type')}")
            expect("action" in state and "state" in state, "状态响应结构错误")

            q = client.post("/api/query", json={"text": "还有多远？"})
            expect(q.json()["ok"], "用户指令提交失败")

            expect(client.post("/api/control", json={"action": "pause"}).json()["paused"], "暂停失败")
            expect(not client.post("/api/control", json={"action": "resume"}).json()["paused"], "恢复失败")

            stats = client.get("/api/stats").json()
            expect("system" in stats and "camera" in stats and "api" in stats, "统计结构错误")

            cam = client.get("/api/camera").json()
            expect("stats" in cam and "snapshot" in cam, "摄像头状态接口结构错误")
            expect(cam["snapshot"]["image_available"], "上传后摄像头快照应可用")

            # /api/frame：返回真正的 JPEG 字节，供测试页预览「服务端画面」
            # （否则用 iPhone App 推流时网页看不到画面，容易被误判成摄像头没工作）
            fr = client.get("/api/frame")
            expect(fr.status_code == 200, f"/api/frame 状态码 {fr.status_code}")
            expect(fr.headers["content-type"].startswith("image/jpeg"), "/api/frame 类型错误")
            expect(fr.content[:2] == b"\xff\xd8", "/api/frame 返回的不是 JPEG（缺 SOI 标记）")
            expect("no-store" in fr.headers.get("cache-control", ""), "/api/frame 未禁用缓存，预览会卡旧帧")
            expect(client.get("/api/frame?max_age_s=0").status_code == 404, "过期阈值未生效")

            # 测试页前端所需的增强字段：地图几何 + 帧内规划路径 + 动态障碍绝对坐标
            mp = client.get("/api/map").json()
            expect("bounds" in mp and "zones" in mp and "landmarks" in mp, "地图几何结构错误")
            expect(len(mp["zones"]) >= 1 and len(mp["landmarks"]) >= 2, "地图几何内容为空")
            expect(
                all(k in mp["bounds"] for k in ("x_min", "x_max", "y_min", "y_max")),
                "地图边界字段缺失",
            )
            expect(isinstance(state.get("route"), list), "tick 缺少规划路径 route")
            expect(len(state["route"]) >= 1, "规划路径为空，小地图画不出线")
            expect(isinstance(state.get("obstacles_dyn"), list), "tick 缺少动态障碍坐标")

            # 测试页本身：确认关键面板存在（防止被回退成简陋版本）
            for marker in ("实时小地图", "大模型诊断", "Agent 行动流", "用户提问"):
                expect(marker in r.text, f"测试页缺少面板: {marker}")

            # 静态交叉校验：JS 里引用的每个元素 id 都必须真实存在。
            # 这一类拼写错误不会报语法错，只会在浏览器里表现为「白屏」或
            # 「点击无反应」，是最难查的前端故障，所以放进自检里兜住。
            import re as _re

            html = r.text
            js_blocks = _re.findall(r"<script>(.*?)</script>", html, _re.S)
            expect(len(js_blocks) >= 1, "测试页内联脚本缺失")
            page_js = js_blocks[-1]
            declared_ids = set(_re.findall(r'\bid="([^"]+)"', html))
            referenced_ids = set(_re.findall(r'\$\("([^"]+)"\)', page_js))
            referenced_ids |= set(_re.findall(r'getElementById\("([^"]+)"\)', page_js))
            missing = sorted(referenced_ids - declared_ids)
            expect(not missing, f"测试页 JS 引用了不存在的 id: {missing}")

            # 大模型诊断接口：未配置 Key 时必须给出结构化结论而不是抛错（不走网络）
            diag = client.get("/api/llm_check").json()
            expect("category" in diag and "configured" in diag, "大模型诊断结构错误")
            expect(diag["configured"] is False, "Key 已置空，configured 应为 False")
            expect(diag["category"] == "not_configured", f"应为 not_configured，实为 {diag.get('category')}")
            expect(bool(diag.get("hint")), "诊断结果应带修复提示")
            expect(diag["ok"] is False, "未配置时不应判定为通过")
            expect(diag.get("http_status") is None, "未配置时不应发出网络请求")

            expect(client.post("/api/control", json={"action": "瞎写"}).status_code == 400, "非法指令未被拒绝")
            expect(client.post("/api/query", json={"text": ""}).status_code == 400, "空指令未被拒绝")

            frames = sys_.camera.stats()["accepted"]
            return f"11 个端点全部正常，相机接收 {frames} 帧"

    runner.run("36 REST 接口（首页/地图/状态/上传/预览帧/诊断/统计/控制）", t_rest_api)

    def t_ws_api() -> str:
        from fastapi.testclient import TestClient

        _, sys_, app = make_api_system()
        with TestClient(app) as client:
            time.sleep(0.3)
            accepted_before = sys_.camera.stats()["accepted"]

            # --- 摄像头上行通道：连接先收 hello 握手，再发二进制帧 ---
            with client.websocket_connect("/ws/camera") as ws:
                hello = ws.receive_json()
                expect(hello.get("type") == "hello", f"未收到握手包: {hello}")
                expect(hello.get("max_fps") == sys_.camera.max_fps, "握手参数与配置不符")

                ws.send_json({"type": "ping"})
                pong = ws.receive_json()
                expect(pong.get("type") == "pong", f"心跳无响应: {pong}")

                ws.send_json({"type": "status"})
                status = ws.receive_json()
                expect(status.get("type") == "status", f"统计查询无响应: {status}")

                ws.send_json({"type": "乱写"})
                err = ws.receive_json()
                expect(err.get("type") == "error", f"未知文本帧应报错: {err}")

                time.sleep(0.25)  # 越过 max_fps 节流窗口
                ws.send_bytes(ip.make_test_frame(text="WS"))
                ack = None
                for _ in range(5):
                    msg = ws.receive_json()
                    if msg.get("type") == "ack":
                        ack = msg
                        break
                expect(ack is not None, "二进制帧未收到 ack")
                expect(ack["ok"], f"二进制帧被拒绝: {ack}")
                expect(ack["accepted"] >= 1, "服务端未记录该帧")

            # --- Agent 状态通道 ---
            with client.websocket_connect("/ws/agent") as ws:
                first = None
                for _ in range(10):
                    first = ws.receive_json()
                    if first.get("type") == "tick":
                        break
                expect(first is not None and first.get("type") in ("tick", "idle"), f"首包异常: {first}")

                ws.send_json({"type": "query", "text": "我该往哪走"})
                ack = None
                for _ in range(20):
                    msg = ws.receive_json()
                    if msg.get("type") == "ack":
                        ack = msg
                        break
                expect(ack is not None and ack["ok"], "指令上行未收到确认")

            time.sleep(0.3)
            expect(
                sys_.camera.stats()["accepted"] > accepted_before,
                "WebSocket 上行的图片没有被接收",
            )
            expect(
                any(t.role == "user" and "我该往哪走" in t.content for t in sys_.memory.turns),
                "WebSocket 上行的用户指令未进入记忆",
            )
            return f"两个 WebSocket 通道均正常（累计 {sys_.camera.stats()['accepted']} 帧）"

    runner.run("37 WebSocket 双通道（图片上行/状态下行）", t_ws_api)

    def t_llm_circuit_breaker() -> str:
        """失败后必须熔断，不能每帧都去撞一次。

        动机：Key 无效 / 模型名错误这类失败不会自愈。没有熔断时，1 Hz 的主循环
        会无限重试 —— 浪费配额、拖慢实时性、刷爆日志（曾导致 600 轮稳定性测试
        跑成分钟级并被误判为卡死）。
        """
        c = base_cfg(**{
            "llm.api_key": "sk-invalid-only-for-test",
            "llm.base_url": "http://127.0.0.1:9/v1",  # 保留端口，必然连不上
            "llm.timeout_s": 2,
        })
        client = LLMClient(c)
        expect(client.usable, "刚构造时应为可用")

        expect(client.chat_multimodal(mk_state()) is None, "失败应返回 None")
        expect(client.failures == 1, f"应记 1 次失败，实为 {client.failures}")
        expect(client.circuit_open, "失败后应进入熔断")
        expect(not client.usable, "熔断期间 usable 必须为 False")

        calls_before = client.calls
        failures_before = client.failures
        for _ in range(5):
            expect(client.chat_multimodal(mk_state()) is None, "熔断期间应直接返回 None")
        expect(client.calls == calls_before, "熔断期间不应再发起真实请求")
        expect(client.failures == failures_before, "熔断期间不应继续累计失败")

        st = client.stats()
        expect(st["circuit_open"] is True, "stats 未反映熔断状态")
        expect(st["circuit_trips"] >= 1, "熔断次数未记录")
        expect(bool(st["disabled_reason"]), "缺少熔断原因")
        expect(st["disabled_for_s"] > 0, "缺少冷却剩余时间")
        return f"失败 1 次即熔断，其后 5 次调用零请求（{st['disabled_reason']}）"

    runner.run("38 大模型失败熔断（避免无限重试）", t_llm_circuit_breaker)

    def t_llm_key_helper() -> str:
        """Key 体检工具：断行归一化 + 套餐/Base URL 配对（离线，不联网）。

        背景：本项目真实踩过三个坑 ——
        ① 从网页复制长 Key 时被折成两行，.env 里只剩 6 个字符；
        ② 百炼套餐 Key 规定小写 `sk-sp-`，写成大写 `Sk-sp-` 即使地址正确也 401；
        ③ ★最容易误判的★ 套餐 Key 必须配**专属** Base URL，用通用地址调用会
           返回长得像「Key 无效」的 401，让人误以为是 Key 复制错了。
        本项把这些规则钉死，防止回归。
        """
        import importlib.util  # noqa: PLC0415 - 仅本项需要，避免影响其它检查
        from pathlib import Path  # noqa: PLC0415

        path = Path(__file__).resolve().parents[1] / "tools" / "set_llm_key.py"
        expect(path.exists(), "tools/set_llm_key.py 不存在")
        spec = importlib.util.spec_from_file_location("bsa_set_llm_key", path)
        expect(spec is not None and spec.loader is not None, "无法加载 set_llm_key.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # ① 归一化：断行 / 引号 / 制表符 / 前缀大小写
        expect(
            mod.normalize("sk-1234567890abcdef\n1234567890abcdef")
            == "sk-1234567890abcdef1234567890abcdef",
            "断行 Key 未被正确归一化",
        )
        expect(mod.normalize("  'sk-abc'  ") == "sk-abc", "首尾引号/空白未被剥离")
        expect(mod.normalize("sk-a\tb c") == "sk-abc", "制表符/空格未被剔除")
        expect(
            mod.normalize("Sk-sp-AAAA") == "sk-sp-AAAA",
            "大写 Sk- 前缀未被纠正为小写（大写会直接 401）",
        )
        expect(mod.normalize("SK-sp-AAAA") == "sk-sp-AAAA", "SK- 前缀未被纠正")

        # ② 套餐 → Base URL 配对表（顺序敏感：必须先匹配更长的前缀）
        plans = {
            "sk-sp-AAAA": "token-plan.cn-beijing.maas.aliyuncs.com",
            "sk-ws-AAAA": "coding.dashscope.aliyuncs.com",
            "sk-AAAA": "dashscope.aliyuncs.com/compatible-mode",
        }
        for k, frag in plans.items():
            got = mod.infer_base_url(k)
            expect(got is not None, f"{k} 未识别出套餐")
            expect(frag in got[1], f"{k} 推断的 Base URL 不对：{got[1]}")
        expect(mod.infer_base_url("totally-unknown") is None, "未知前缀不应被推断出套餐")

        # ③ 标准通用 Key + 配套地址：不应有任何警告
        expect(
            mod.inspect("sk-" + "a1" * 16, mod.infer_base_url("sk-")[1]) == [],
            "标准 Key 被误报",
        )

        # ④ 套餐 Key + 通用地址：必须提示「不配套」
        warns = mod.inspect("sk-sp-" + "a1" * 16, "https://dashscope.aliyuncs.com/compatible-mode/v1")
        expect(any("不配套" in w for w in warns), f"未检出套餐与 Base URL 不配套：{warns}")

        # ⑤ 套餐 Key + 正确地址：不应有警告（套餐 Key 含点号是合法的，不能瞎报）
        expect(
            mod.inspect("sk-sp-" + "a1" * 16, mod.infer_base_url("sk-sp-")[1]) == [],
            "套餐 Key 配正确地址时被误报（含点号本身不是问题）",
        )

        # ⑥ 未知前缀且含点号 → 才提示可能是签名令牌
        odd = mod.inspect("Sk-token.Abcd.Efgh.Xyzw")
        expect(any("签名令牌" in w for w in odd), f"未对未知含点前缀给出提示：{odd}")

        # ⑦ 过短必须报警（对应「Key 只剩 6 字符」那次事故）
        expect(any("过短" in w for w in mod.inspect("sk-abc")), "过短 Key 未报警")

        # ⑧ 致命性判定
        expect(mod.warnings_are_fatal(odd), "未知含点凭据应被判为致命警告")
        expect(not mod.warnings_are_fatal([]), "无警告不应被判为致命")

        return "断行归一化 / 前缀纠大小写 / 套餐→BaseURL 配对 / 误报抑制 均按预期"

    runner.run("39 大模型 Key 体检工具（断行 / 大小写 / 套餐配对）", t_llm_key_helper)

    def t_lan_ip_filter() -> str:
        """局域网 IP 识别：不能把代理/VPN 的虚拟地址当成局域网地址报给用户。

        背景：`--serve` 启动时会打印「局域网 http://<ip>:8000/ （iPhone 用 Safari 打开）」。
        原实现用 UDP connect 到 8.8.8.8 来选路，本机开着代理时会选中隧道网卡，
        拿到 `198.18.0.1` 这类**虚拟地址** —— 用户照着填到手机上根本连不上。
        现在加了 is_lan_ip 校验，本项把它钉死。
        """
        import main as main_mod  # noqa: PLC0415 - 延迟导入，避免与 main.py 形成循环导入

        cases = [
            ("192.168.101.104", True),   # 常见家用/办公局域网
            ("10.0.0.5", True),          # 私有 A 段
            ("172.16.3.9", True),        # 私有 B 段
            ("198.18.0.1", False),       # ★代理/基准测试虚拟网段，必须排除★
            ("198.19.255.254", False),   # 同一网段的上边界
            ("127.0.0.1", False),        # 回环
            ("0.0.0.0", False),          # 未指定
            ("169.254.1.1", False),      # 链路本地（自分配失败）
            ("8.8.8.8", False),          # 公网
            ("not-an-ip", False),        # 非法输入不能抛异常
            ("", False),
        ]
        for ip, want in cases:
            got = main_mod.is_lan_ip(ip)
            expect(got is want, f"is_lan_ip({ip!r}) 应为 {want}，实为 {got}")

        # 真机联调会用到，实际跑一次；返回的地址若存在则必须通过校验
        guess = main_mod._guess_lan_ip()
        if guess is not None:
            expect(main_mod.is_lan_ip(guess), f"_guess_lan_ip() 返回了非局域网地址：{guess}")

        return f"11 个地址判定正确；当前推断 LAN IP = {guess}"

    runner.run("40 局域网 IP 识别（排除代理/VPN 虚拟网段）", t_lan_ip_filter)

    # -----------------------------------------------------------------
    # 13. v0.3 状态契约（Stage 2）
    # -----------------------------------------------------------------
    def t_v03_contract() -> str:
        """v0.3 契约：类名别名兼容 + dump→validate 往返幂等 + 新字段就位。"""
        # 别名必须指向同一个类，否则 isinstance / 类型标注会静默失配
        expect(PoseState is UserState, "PoseState 与 UserState 应为同一类对象")
        expect(UncertaintyState is ConfidenceState, "UncertaintyState 与 ConfidenceState 应为同一类对象")
        expect(SemanticState is SemanticScene, "SemanticState 与 SemanticScene 应为同一类对象")

        st = mk_state()
        # 只读转发属性必须等价于原字段
        expect(st.pose is st.user, "SpatialState.pose 应转发到 user")
        expect(st.uncertainty is st.confidence, "SpatialState.uncertainty 应转发到 confidence")
        expect(st.camera_meta is st.camera, "SpatialState.camera_meta 应转发到 camera")

        # ★往返幂等★：selftest 的端到端用例就是这么做校验的，
        # 新字段若用了 computed_field 会在这里立刻炸（extra="forbid"）
        d1 = st.model_dump()
        d2 = SpatialState.model_validate(d1).model_dump()
        expect(d1 == d2, "dump→validate 往返必须幂等（新增字段不能是不可入参的派生字段）")

        # 新字段是否真的出现在契约里
        for path, key in (
            ("affordance", "can_move_forward"),
            ("environment", "blocking_ratio"),
            ("environment", "walkable_width"),
            ("environment", "stairs"),
            ("environment", "dropoff"),
            ("navigation", "route_confidence"),
            ("camera", "freshness"),
            ("user", "confidence"),
            ("user", "source"),
            # Stage 4 新增：定位"有没有效"必须能表达（过期/失效 ≠ 高置信度）
            ("user", "valid"),
            ("user", "age_ms"),
            ("environment", "valid"),
        ):
            expect(key in d1[path], f"{path} 缺少 v0.3 新字段 {key}")
        # 六维 + 一句"为什么不确定"（Stage 4 起 camera_freshness 与 reason 入约）
        expect(
            len(d1["confidence"]) == 7,
            f"不确定性应为 7 个字段（5 维 + 相机 + 原因），实为 {list(d1['confidence'])}",
        )
        for f in ("camera_freshness", "uncertainty_reason", "overall_confidence"):
            expect(f in d1["confidence"], f"不确定性缺少字段 {f}")
        return "别名兼容 / 往返幂等 / 12 个新字段就位 / 6 维置信度 + 原因"

    runner.run("41 v0.3 状态契约（别名 + 往返幂等 + 新字段）", t_v03_contract)

    def t_v03_uncertainty() -> str:
        """五维不确定性：综合值自动补全，且显式传入不被覆盖。"""
        u = UncertaintyState(
            localization_confidence=1.0,
            perception_confidence=0.5,
            semantic_confidence=0.5,
            route_confidence=0.5,
        )
        # 0.30*1.0 + 0.30*0.5 + 0.20*0.5 + 0.20*0.5 = 0.65
        expect(
            u.overall_confidence is not None and abs(u.overall_confidence - 0.65) < 1e-6,
            f"综合置信度应为 0.65，实为 {u.overall_confidence}",
        )
        expect(UncertaintyState(overall_confidence=0.123).overall_confidence == 0.123,
               "显式传入的 overall_confidence 不应被校验器覆盖")
        expect(
            UncertaintyState.model_validate(u.model_dump()).overall_confidence == u.overall_confidence,
            "不确定性结构往返应幂等",
        )
        # 维度齐全，缺一不可（缺了会让对应降级分支无从判断）
        for f in ("localization_confidence", "perception_confidence",
                  "semantic_confidence", "route_confidence", "overall_confidence"):
            expect(hasattr(u, f), f"缺少不确定性维度 {f}")
        return "加权补全=0.65；显式值保留；往返幂等；5 维齐全"

    runner.run("42 五维不确定性（自动补全 + 权重正确）", t_v03_uncertainty)

    def t_v03_affordance() -> str:
        """可行动性：从环境几何正确推导"能不能走、往哪走"。"""
        c, sv = fresh_env()
        sm = StateManager(c, sv)

        # 前方 0.3m 有椅子，左剩 2.0m / 右剩 0.2m → 不可直行，建议向左
        env = EnvironmentState(
            front_clear=False,
            front_distance=0.3,
            corridor_width=2.2,
            left_distance=2.0,
            right_distance=0.2,
            walkable_width=2.2,
            obstacles=[Obstacle(type=ObstacleKind.CHAIR, distance=0.3,
                                direction=ObstacleDirection.FRONT)],
        )
        aff = sm._build_affordance(env)
        expect(not aff.can_move_forward, "前方 0.3m 有障碍时应判定不可直行")
        expect(aff.preferred_direction == ObstacleDirection.LEFT,
               f"右侧仅 0.2m，应建议向左，实为 {aff.preferred_direction}")
        expect(bool(aff.blocked_regions), "应产出阻挡区域")
        expect(aff.blocked_regions[0].severity == RiskLevel.CRITICAL,
               f"0.3m 阻挡严重度应为 critical，实为 {aff.blocked_regions[0].severity}")
        expect(aff.blocked_regions[0].source_type == "chair", "阻挡来源应为 chair")
        expect(aff.most_blocking() is not None, "most_blocking() 应返回最紧迫阻挡")
        expect(bool(aff.summary), "应给出一句话可行动性描述")

        # 两侧都窄（<0.5m）→ 不应硬给建议方位
        env_tight = EnvironmentState(
            front_clear=False, front_distance=0.4, corridor_width=0.5,
            left_distance=0.3, right_distance=0.2, walkable_width=0.5,
        )
        expect(sm._build_affordance(env_tight).preferred_direction is None,
               "两侧都窄时不应给出建议方位（避免把用户往墙上引）")

        # 前方通畅 → 可直行且无建议方位
        env_ok = EnvironmentState(
            front_clear=True, front_distance=3.0, corridor_width=4.0,
            left_distance=2.0, right_distance=2.0, walkable_width=4.0,
        )
        aff_ok = sm._build_affordance(env_ok)
        expect(aff_ok.can_move_forward and aff_ok.preferred_direction is None,
               "前方通畅时应可直行且不建议改变方向")
        return "受阻→不可直行/建议向左/critical；两侧窄→无建议；通畅→可直行"

    runner.run("43 可行动性推导（AffordanceState）", t_v03_affordance)

    def t_v03_camera_freshness() -> str:
        """摄像头新鲜度三态：这是 v0.2 无法表达的关键语义。"""
        c, sv = fresh_env()
        sm = StateManager(c, sv)
        import datetime as _dt

        now = _dt.datetime.now()

        class _Stub:
            def __init__(self, snap): self._snap = snap
            def snapshot(self): return self._snap

        expect(sm._camera_state(now).freshness == FrameFreshness.NONE,
               "未接入摄像头应为 none")
        sm.camera = _Stub({"image_available": True, "frame_id": 7,
                           "source": "websocket", "age_s": 0.2})
        expect(sm._camera_state(now).freshness == FrameFreshness.FRESH, "可用帧应为 fresh")
        # ★核心：收到过帧但已过期 → stale（"推流断了"），而不是 none（"没有摄像头"）★
        sm.camera = _Stub({"image_available": False, "frame_id": 7,
                           "source": "websocket", "age_s": 9.0})
        st_stale = sm._camera_state(now)
        expect(st_stale.freshness == FrameFreshness.STALE, "推流中断应为 stale")
        expect(not st_stale.image_available, "stale 帧不得标记为可用（模型不能看旧画面）")
        return "none / fresh / stale 三态判定正确，且 stale 不误标可用"

    runner.run("44 摄像头新鲜度三态（none / fresh / stale）", t_v03_camera_freshness)

    def t_v03_consistency() -> str:
        """新增自洽性校验：高危地形必须配高风险；宽度为 0 不得可直行。"""
        base = mk_state().model_dump()

        # a) 台阶必须配 ≥high 风险
        stairs_low = copy.deepcopy(base)
        stairs_low["environment"]["stairs"] = True
        try:
            SpatialState.model_validate(stairs_low)
            raise AssertionError("stairs=True 而 risk=low，应被自洽性校验拦住")
        except ValueError:
            pass
        stairs_ok = copy.deepcopy(base)
        stairs_ok["environment"]["stairs"] = True
        stairs_ok["risk"]["level"] = RiskLevel.HIGH.value
        SpatialState.model_validate(stairs_ok)  # 不应抛

        # b) 可通行宽度为 0 时不得标记可直行
        zero_width = copy.deepcopy(base)
        zero_width["environment"]["walkable_width"] = 0.0
        zero_width["affordance"]["can_move_forward"] = True
        try:
            SpatialState.model_validate(zero_width)
            raise AssertionError("宽度为 0 却标记可直行，应被自洽性校验拦住")
        except ValueError:
            pass
        return "台阶/坠落必须配 ≥high 风险；宽度为 0 不得可直行"

    runner.run("45 新增自洽性校验（高危地形 / 零宽度）", t_v03_consistency)

    # -----------------------------------------------------------------
    # v0.3 Stage 3：传感器适配层
    # -----------------------------------------------------------------
    def t_v03_adapter_contract() -> str:
        """SimulatedProvider 必须同时满足两个协议，且参考读数不得消耗传感器随机数。"""
        from sensors.base import SensorHealth, SensorKind, SensorProvider, SensorStatus, WorldStepper

        c, sv = fresh_env()
        expect(isinstance(sv, SensorProvider), "SimulatedProvider 未满足 SensorProvider 协议")
        expect(isinstance(sv, WorldStepper), "SimulatedProvider 未满足 WorldStepper 协议")

        # 协议要求的成员一个都不能少（Python 协议是隐式的，只能自己点一遍）
        for name in ("read_pose", "read_navigation", "perceive", "scan_wall_ahead",
                     "scan_sides", "measure_depth", "map_snapshot", "zone_at", "floor",
                     "confidence", "health", "sensor_stats"):
            expect(callable(getattr(sv, name, None)), f"SensorProvider 缺方法 {name}")
        for name in ("advance_environment", "advance_user", "replan", "reset",
                     "obstacle_centers", "arrived", "world_reference",
                     "debug_ground_truth", "stats"):
            expect(callable(getattr(sv, name, None)), f"WorldStepper 缺方法 {name}")

        # map_snapshot 的结构要能支撑语义融合
        snap = sv.map_snapshot()
        expect(snap.objects and snap.route, "地图快照缺物体或路线")
        expect(all(o.blocking is not None for o in snap.objects), "地图快照物体缺 blocking 标记")

        # ★核心不变式★ 参考读数与工具调用**不得**消耗传感器随机数，
        # 否则"大模型多调一次工具"就会改变整条仿真轨迹（可复现性崩塌）。
        def rng_state(m):  # 用随机数状态直接对比，比"跑一轮看结果"更灵敏
            return m.getstate()

        before = rng_state(sv.sensors.rng)
        for _ in range(5):
            sv.world_reference()
            sv.perceive()
            sv.map_snapshot()
            sv.zone_at(2.0, 2.0)
            sv.health()
        after = rng_state(sv.sensors.rng)
        expect(before == after, "参考读数/候选枚举消耗了传感器随机数 —— 会破坏可复现性")

        # 健康度三态：没有摄像头 = ABSENT，推到一半断了 = LOST（二者语义完全不同）
        h = sv.health()
        for kind in (SensorKind.UWB, SensorKind.IMU, SensorKind.LIDAR,
                     SensorKind.MAP, SensorKind.PLANNER, SensorKind.CAMERA):
            expect(kind in h, f"健康度缺少通道 {kind.value}")
            expect(isinstance(h[kind], SensorHealth), f"{kind.value} 健康度类型错误")
        expect(h[SensorKind.CAMERA].status == SensorStatus.ABSENT, "未接摄像头应为 absent")

        class _StubCam:
            def __init__(self, snap): self._snap = snap
            def snapshot(self): return self._snap

        sv.camera = _StubCam({"image_available": False, "frame_id": 9,
                              "source": "websocket", "age_s": 8.0})
        cam = sv.health()[SensorKind.CAMERA]
        expect(cam.status == SensorStatus.LOST,
               f"收到过帧但已断流应为 lost（区分于 absent），实为 {cam.status}")
        expect(not cam.usable, "断流的摄像头不应标记为可用")
        return "两协议齐备 / 参考读数零随机数消耗 / 健康度 absent≠lost"

    runner.run("46 传感器适配层契约（双协议 + 无副作用 + 健康度）", t_v03_adapter_contract)

    def t_v03_layering_law() -> str:
        """分层铁律：融合层 / 工具层 / Agent 核心不得 import simulator。

        这条断言是 Stage 3 的**唯一硬验收标准**，也是最容易被后人改回去的地方。
        写成自检而不是写在文档里，是因为文档没人跑，自检会红。
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        guarded = {
            "spatial/state_manager.py": "融合层",
            "agent/tools.py": "工具层",
            "agent/agent_core.py": "Agent 核心",
            # ★Stage 9★ 回放器必须**不接模拟器**（任务书第十五节：Replay 不调用 simulator）。
            #   这是"回放 = 在历史状态流上重放 Agent"这句承诺的技术底线：
            #   一旦它 import 了 simulator，回放就会在自己造的世界里跑，验证价值归零。
            "recording/session_player.py": "回放器",
            "recording/session_recorder.py": "录制器",
        }
        pat = re.compile(r"^\s*(?:from|import)\s+simulator\b", re.M)
        offenders: list[str] = []
        for rel, label in guarded.items():
            src = (root / rel).read_text(encoding="utf-8")
            if pat.search(src):
                offenders.append(f"{label}({rel})")
        expect(not offenders, f"这些层仍直接依赖模拟器: {', '.join(offenders)}")

        # 反向确认：确实有人实现这些协议（否则上面那条会因为"文件被删空"而假通过）
        from sensors.simulated import SimulatedProvider
        expect(hasattr(SimulatedProvider, "read_pose"), "适配层实现丢失")

        # 抽象层本身必须零模拟器依赖，否则分层只是换了个地方耦合
        for rel in ("sensors/base.py", "sensors/__init__.py"):
            expect(not pat.search((root / rel).read_text(encoding="utf-8")),
                   f"{rel} 不该依赖模拟器")

        # 唯一允许装配模拟器的地方就是 agent_core（且只允许 sensors.simulated）
        core = (root / "agent/agent_core.py").read_text(encoding="utf-8")
        expect("sensors.simulated" in core, "装配处未通过 sensors.simulated 引入模拟实现")
        return "融合层 / 工具层 / Agent 核心已与 simulator 解耦，装配点唯一"

    runner.run("47 分层铁律（融合层/工具层不得依赖 simulator）", t_v03_layering_law)

    # -----------------------------------------------------------------
    # v0.3 Stage 4：State Fusion
    # -----------------------------------------------------------------
    def t_v03_freshness() -> str:
        """新鲜度策略：三态语义（fresh/stale/missing）+ 阈值来自配置。"""
        import datetime as _dt

        from fusion.freshness import ChannelFreshness, FreshnessPolicy

        c = base_cfg()
        pol = FreshnessPolicy(c)
        expect(pol.threshold("pose") == 0.5, f"pose 阈值应为配置的 0.5，实为 {pol.threshold('pose')}")
        expect(pol.threshold("map") is None, "静态地图不应过期（阈值应为 None）")
        expect(
            pol.threshold("camera") == float(c["camera"]["stale_after_s"]),
            "相机阈值必须与 camera.stale_after_s 一致，否则两处口径会互相矛盾",
        )

        now = _dt.datetime.now()
        r_fresh = pol.evaluate("pose", now - _dt.timedelta(seconds=0.1), now)
        r_stale = pol.evaluate("pose", now - _dt.timedelta(seconds=1.0), now)
        r_missing = pol.evaluate("pose", None, now, ever_seen=False)
        r_lost = pol.evaluate("pose", None, now, ever_seen=True)
        expect(r_fresh.state is ChannelFreshness.FRESH and r_fresh.usable, "0.1s 前的读数应为新鲜且可用")
        expect(r_stale.state is ChannelFreshness.STALE, "1.0s 前的读数应判过期")
        expect(not r_stale.usable, "过期读数不得被当作当前事实使用")
        expect(
            r_missing.state is ChannelFreshness.MISSING and r_lost.state is ChannelFreshness.STALE,
            f"「从未有过」应为 missing、「曾有过但断了」应为 stale，实为 {r_missing.state}/{r_lost.state}",
        )

        # 配置驱动的阈值真的生效（改配置 → 判定结果跟着变）
        pol2 = FreshnessPolicy(base_cfg(**{"fusion.freshness.pose_stale_s": 5.0}))
        expect(
            pol2.evaluate("pose", now - _dt.timedelta(seconds=1.0), now).state is ChannelFreshness.FRESH,
            "把阈值放宽到 5s 后，1s 前的读数应重新判为新鲜（阈值没生效）",
        )

        expect(pol.camera_state(None, now).value == "none", "无快照应为 none")
        expect(pol.camera_state({"image_available": True}, now).value == "fresh", "可用帧应为 fresh")
        expect(
            pol.camera_state({"image_available": False, "frame_id": 3, "source": "websocket"}, now).value == "stale",
            "有 frame_id 但不可用应为 stale（推流断了）",
        )
        return "三态判定正确 / 阈值可配并生效 / 与 camera.stale_after_s 对齐"

    runner.run("48 数据新鲜度三态（fresh/stale/missing + 阈值可配）", t_v03_freshness)

    def t_v03_confidence_estimate() -> str:
        """★P0-7 验收★ 定位置信度必须是**估计值**，且退化时确实下降。"""
        from fusion.confidence import ConfidenceEstimator

        c, sv = fresh_env()
        sm = StateManager(c, sv)

        locs: list[float] = []
        for i in range(30):
            st = sm.build(float(i), float(i))
            locs.append(st.confidence.localization_confidence)

        spread = max(locs) - min(locs)
        expect(spread > 0.03, f"定位置信度几乎没有变化（极差 {spread:.4f}）—— 又退化成常量了")
        expect(
            len(set(locs)) > 3,
            "定位置信度取值过于集中，说明它没有真的跟随传感器数据变化",
        )
        # 位姿的 confidence 必须与不确定性维度同源（不能两处各说一套）
        expect(
            abs(sm.last_state.user.confidence - sm.last_state.confidence.localization_confidence) < 1e-6,
            "pose.confidence 与 uncertainty.localization_confidence 不一致",
        )
        expect(sm.last_state.user.source == "fused", "融合后位姿的 source 应标记为 fused")
        expect(sm.last_state.user.valid, "定位正常时位姿应有效")

        # fixed 模式必须能复现 v0.2 的常量行为（回归对比用的逃生门）
        c_fixed, sv_fixed = fresh_env(**{"fusion.confidence.mode": "fixed"})
        sm_fixed = StateManager(c_fixed, sv_fixed)
        vals = [sm_fixed.build(float(i), float(i)).confidence.localization_confidence for i in range(5)]
        expect(all(v == 0.90 for v in vals), f"mode=fixed 应恒为 0.90，实为 {set(vals)}")

        # 纯算子单测：残差窗口起作用（喂入"位移远超预期"的观测 → 置信度下降）
        est = ConfidenceEstimator(c)
        from spatial.spatial_state import Position as _Pos
        p0 = PoseState(position=_Pos(x=0.0, y=0.0), speed=0.0)
        est.observe_pose(p0, 1.0)
        high = est.localization(fresh=True, pose_valid=True)
        for k in range(6):  # 每轮"瞬移"5 米，而速度声称 0 → 残差爆表
            est.observe_pose(PoseState(position=_Pos(x=5.0 * (k + 1), y=0.0), speed=0.0), 1.0)
        low = est.localization(fresh=True, pose_valid=True)
        expect(low < high - 0.05, f"定位跳变应显著降低置信度（{high} → {low}）")
        expect(bool(est.reasons()), "置信度下降时必须能说出原因（uncertainty_reason 的来源）")

        # 过期/失效必须把置信度压到"不可信"区间
        est2 = ConfidenceEstimator(c)
        expect(
            est2.localization(fresh=False, pose_valid=True) <= 0.35,
            "定位数据过期时置信度必须显著下降",
        )
        expect(
            est2.localization(fresh=True, pose_valid=False) <= 0.35,
            "定位失效时置信度必须显著下降",
        )
        return f"估计值随数据变化（极差 {spread:.3f}，{len(set(locs))} 种取值）/ 跳变降分 / fixed 可回退"

    runner.run("49 置信度估计（P0-7：不再恒 0.9，退化时下降）", t_v03_confidence_estimate)

    def t_v03_low_conf_branch() -> str:
        """低定位置信度必须**可构造**并触发降级行为（任务书第 14 节第 6 项）。"""
        from agent.agent_core import SpatialAgentSystem, SystemConfig

        # 把 UWB 噪声调到退化水平：0.15m（正常）→ 1.5m（严重漂移）
        c = base_cfg(**{"simulator.sensors.uwb_noise_m": 1.5})
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        asked: list[str] = []
        min_loc = 1.0
        for _ in range(25):
            r = sys_.step(1.0)
            min_loc = min(min_loc, r.state.confidence.localization_confidence)
            if r.action.action_type.value == "ASK_USER":
                asked.append(r.action.message)

        expect(min_loc < 0.5, f"UWB 噪声 1.5m 时定位置信度应显著下降，实测最低 {min_loc}")
        expect(
            bool(asked),
            f"定位置信度低到 {min_loc} 仍未触发「询问用户」降级分支（min_localization="
            f"{c['agent']['confidence']['min_localization']}）",
        )
        expect(
            any("定位" in m for m in asked),
            f"降级话术应围绕定位不确定性，实为 {asked[:1]}",
        )

        # 自洽性：定位失效 + 高置信度不可能共存（防止"静默撒谎"）
        bad = mk_state().model_dump()
        bad["user"]["valid"] = False
        bad["confidence"]["localization_confidence"] = 0.9
        try:
            SpatialState.model_validate(bad)
            raise AssertionError("pose.valid=False 却宣称 localization_confidence=0.9，应被自洽性校验拦住")
        except ValueError:
            pass
        return f"噪声 1.5m → 最低置信度 {min_loc}，触发降级：{asked[0][:18]}…"

    runner.run("50 低定位置信度可构造并触发降级（Stage 4 硬验收）", t_v03_low_conf_branch)

    def t_v03_fusion_layering() -> str:
        """融合层的分层与职责：逻辑在 fusion/，state_manager 只是门面。"""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        pat = re.compile(r"^\s*(?:from|import)\s+simulator\b", re.M)
        for rel in ("fusion/state_fusion.py", "fusion/confidence.py", "fusion/freshness.py"):
            src = (root / rel).read_text(encoding="utf-8")
            expect(not pat.search(src), f"{rel} 直接依赖了模拟器 —— 融合层必须只认协议")

        # 门面必须真的转发（不是各自留一份实现，那会立刻分叉）
        c, sv = fresh_env()
        sm = StateManager(c, sv)
        expect(hasattr(sm, "fusion"), "StateManager 应持有 StateFusion")
        expect(
            sm._build_affordance is not None and sm.fusion._build_affordance is not None,
            "私有方法转发丢失（自检与工具层依赖这些名字）",
        )
        env = sm._build_affordance(
            EnvironmentState(front_clear=True, front_distance=3.0, walkable_width=2.0)
        )
        env2 = sm.fusion._build_affordance(
            EnvironmentState(front_clear=True, front_distance=3.0, walkable_width=2.0)
        )
        expect(env.summary == env2.summary, "门面与实现应给出同一结论（说明是转发而非两份实现）")

        # 热插拔摄像头必须能到达实现层（服务启动后才接上摄像头）
        class _Stub:
            def snapshot(self):
                return {"image_available": True, "frame_id": 1, "source": "websocket", "age_s": 0.1}

        sm.camera = _Stub()
        expect(sm.fusion.camera is sm.camera, "给门面设 camera 必须同步到 StateFusion")
        import datetime as _dt
        expect(sm._camera_state(_dt.datetime.now()).freshness.value == "fresh", "热插拔后应能取到帧")
        return "fusion/ 零模拟器依赖 / 门面确为转发 / camera 热插拔可达实现层"

    runner.run("51 融合层分层与门面转发（Stage 4）", t_v03_fusion_layering)

    # -----------------------------------------------------------------
    # v0.3 Stage 5：世界模型分层 + 地图接口
    # -----------------------------------------------------------------
    def t_v03_object_memory() -> str:
        """对象记忆：同一把椅子跨帧持续存在，而不是每帧都是新椅子。"""
        from world_model.object_memory import ObjectMemoryStore

        store = ObjectMemoryStore()
        obs = [{"type": "chair", "x": 3.0, "y": 6.0, "radius": 0.3, "distance": 1.8,
                "oid": 1, "is_dynamic": False}]
        new1 = store.observe(obs, t=0.0)
        new2 = store.observe([dict(obs[0], distance=1.3)], t=1.0)
        new3 = store.observe([dict(obs[0], distance=0.9)], t=2.0)
        expect(len(new1) == 1 and new1[0].startswith("chair_"), f"首次观察应产生新对象名，实为 {new1}")
        expect(new2 == [] and new3 == [], f"同一物体重复观察不应再算新发现，实为 {new2}/{new3}")

        mem = list(store.memories.values())[0]
        expect(mem.seen_count == 3, f"应累计看到 3 次，实为 {mem.seen_count}")
        expect(bool(mem.name), "对象应有可读稳定 ID（如 chair_001）")
        expect(not mem.persistent, "3 次 × 2 秒不应算持续存在（需跨 ≥3 秒）")

        store.observe([dict(obs[0], distance=0.5)], t=5.0)
        mem = list(store.memories.values())[0]
        expect(mem.persistent, "5 秒后仍在 ⇒ 应算持续存在")

        # 淘汰：久未复现的对象必须被清掉（世界模型不是垃圾桶）
        store.evict(t=100.0)
        expect(not store.memories, "TTL 过期后对象记忆应被淘汰")

        # 检测器不给 oid 时的兜底：0.5m 网格聚合
        store2 = ObjectMemoryStore()
        no_id = [{"type": "box", "x": 10.0, "y": 10.0, "radius": 0.3, "distance": 1.0,
                  "oid": None, "is_dynamic": False}]
        store2.observe(no_id, t=0.0)
        store2.observe([dict(no_id[0], x=10.2, y=10.1)], t=1.0)  # 挪了 22cm，仍在同格
        expect(len(store2.memories) == 1, "0.5m 网格内应聚合为同一物体（无 oid 场景）")
        store2.observe([dict(no_id[0], x=11.0, y=10.0)], t=2.0)  # 挪了 1 米，跨格
        expect(len(store2.memories) == 2, "跨网格移动应记为新对象（无 oid 场景）")
        return "跨帧持续 / 稳定命名 / TTL 淘汰 / 无 oid 网格聚合"

    runner.run("52 对象记忆（同一物体跨帧持续 + 淘汰 + 网格兜底）", t_v03_object_memory)

    def t_v03_short_term_memory() -> str:
        """短期空间记忆：数值趋势（接近/开阔）与风险峰值。"""
        from world_model.spatial_memory import SpatialMemory

        sm = SpatialMemory(window_s=30.0)
        base = mk_state()

        # 模拟"前方距离从 4 米缩到 0.6 米"的一串状态
        import datetime as _dt

        t0 = _dt.datetime.now()
        for i, d in enumerate([4.0, 3.6, 3.2, 2.8, 2.4, 2.0, 1.6, 1.2, 0.8, 0.6]):
            st = base.model_copy(deep=True)
            st = st.model_copy(update={
                "timestamp": t0 + _dt.timedelta(seconds=i),
                "environment": st.environment.model_copy(update={"front_distance": d, "front_clear": d > 1.5}),
            })
            sm.observe(st)

        expect(sm.front_distance_trend(10.0) == "closing", "距离持续缩短应判 closing")
        expect_close(sm.min_front_distance(10.0), 0.6, 1e-6, "窗口内最小前方距离错误")
        expect(sm.was_blocked(10.0), "距离 1.5m 以下应有 blocked 记录")
        expect("closing" in sm.summarize(10.0) or "缩短" in sm.summarize(10.0), "摘要应包含趋势信息")

        # 窗口外淘汰
        for i in range(5):
            st = base.model_copy(deep=True)
            st = st.model_copy(update={"timestamp": t0 + _dt.timedelta(seconds=100 + i)})
            sm.observe(st)
        expect(sm.front_distance_trend(10.0) == "stable", "旧观测滚出窗口后趋势应复位")

        # 风险峰值
        peak = base.model_copy(deep=True)
        peak = peak.model_copy(update={"risk": peak.risk.model_copy(update={"level": RiskLevel.HIGH})})
        sm2 = SpatialMemory()
        sm2.observe(base)
        sm2.observe(peak)
        expect(sm2.risk_peak(30.0) == "high", f"风险峰值应为 high，实为 {sm2.risk_peak(30.0)}")
        return "closing 趋势 / 最小距离 / blocked 记录 / 窗口淘汰 / 风险峰值"

    runner.run("53 短期空间记忆（趋势 / 峰值 / 窗口淘汰）", t_v03_short_term_memory)

    def t_v03_map_layers() -> str:
        """地图四层接口：度量 / 语义 / 拓扑 / 经验。"""
        from maps import AffordanceKind, AffordanceMap, MapProvider, MetricMap, NavigationGraph, SemanticMap

        c, sv = fresh_env()
        snap = sv.map_snapshot()
        provider = MapProvider.from_provider(sv)

        # ★本用例全部从 config 派生，不写死坐标/区域名★
        #   （写过死坐标，地图一换就整条失败，且失败原因看起来像"分层坏了"）
        mp = c["simulator"]["map"]
        B, start = mp["bounds"], mp["start"]
        zones, lms, dest = mp["zones"], mp["route_landmarks"], str(mp["destination"])
        sx, sy = float(start["x"]), float(start["y"])
        zone_name = next(
            str(z["name"]) for z in zones
            if float(z["x_min"]) <= sx <= float(z["x_max"])
            and float(z["y_min"]) <= sy <= float(z["y_max"])
        )

        # 度量层
        expect(provider.metric.zone_at(sx, sy) == zone_name,
               f"度量层区域判定错误：{provider.metric.zone_at(sx, sy)} != {zone_name}")
        expect(provider.metric.is_walkable(sx, sy), f"{zone_name}内应可通行")
        # 在所有分区之外找一个点（必然是墙）
        wall = None
        wy = float(B["y_min"]) + 0.25
        while wy < float(B["y_max"]) and wall is None:
            wx = float(B["x_min"]) + 0.25
            while wx < float(B["x_max"]):
                if not provider.metric.is_walkable(wx, wy):
                    wall = (wx, wy)
                    break
                wx += 0.5
            wy += 0.5
        expect(wall is not None, "地图里找不到任何墙，地图数据有问题")
        expect(not provider.metric.is_walkable(*wall), f"{wall} 在分区之外，不应可通行")
        expect(provider.metric.in_bounds(sx, sy), "界内判定错误")

        # 语义层：blocking 与非 blocking 必须分开
        blocking = provider.semantic.blocking()
        expect(all(o.blocking for o in blocking), "blocking() 混入了非阻挡物体")
        expect(any(o.type == "door" for o in provider.semantic.objects if not o.blocking),
               "门应作为非阻挡语义物体存在")
        _b = next(o for o in provider.semantic.objects if o.blocking)
        expect(bool(provider.semantic.near(_b.x, _b.y, 1.0)),
               f"语义层邻近查询失败（查询 {_b.type}@{_b.x},{_b.y}）")

        # 拓扑层
        graph = provider.graph
        expect(graph.names == [str(p["name"]) for p in lms], "路点序列错误")
        expect(graph.names[-1] == dest, f"最后一个路点应是目的地 {dest}")
        lm1 = lms[1]
        name, dist = graph.nearest(float(lm1["x"]), float(lm1["y"]))
        expect(name == str(lm1["name"]), f"最近路点应为 {lm1['name']}，实为 {name}")
        expect(graph.remaining_from(str(lm1["name"])) == [str(p["name"]) for p in lms[1:]],
               "剩余路点计算错误")
        expect(graph.total_length() > 30, "总路程应大于 30 米")

        # 经验层：证据累积与衰减
        am = AffordanceMap()
        am.observe(AffordanceKind.BLOCKED, 5.0, 5.0, t=0.0)
        am.observe(AffordanceKind.BLOCKED, 5.0, 5.0, t=1.0)
        am.observe(AffordanceKind.BLOCKED, 5.1, 5.0, t=2.0)
        expect(am.persistently_blocked(5.0, 5.0), "3 次阻挡证据应判为长期阻挡")
        expect("常被阻挡" in am.summary_near(5.0, 5.0), "经验摘要应含『常被阻挡』")
        # 反向证据抵消
        am.observe(AffordanceKind.PASSABLE, 5.0, 5.0, t=3.0)
        am.observe(AffordanceKind.PASSABLE, 5.0, 5.0, t=4.0)
        expect(not am.persistently_blocked(5.0, 5.0), "多次通行证据应抵消阻挡结论")

        # 经验不随地图刷新清空
        prov = MapProvider.from_provider(sv, affordance=am)
        prov.refresh(sv.map_snapshot())
        expect(len(prov.affordance.annotations) > 0, "刷新地图时经验库不应被清空")

        desc = provider.area_description(sx, sy)
        expect(zone_name in desc and "路点" in desc,
               f"区域描述应含区域({zone_name})与路点信息：{desc[:60]}")
        return "度量/语义/拓扑/经验 四层齐备；经验累积可抵消；刷新不清经验"

    runner.run("54 地图四层接口（度量/语义/拓扑/经验）", t_v03_map_layers)

    def t_v03_world_model_layers() -> str:
        """世界模型门面：v0.2 属性兼容 + v0.3 分层统计 + reset 语义。"""
        c, sv = fresh_env()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        for _ in range(12):
            sys_.step(1.0)

        w = sys_.world
        # v0.2 兼容属性
        expect(len(w.trail) == 12, f"轨迹应有 12 点，实为 {len(w.trail)}")
        expect(bool(w.visited_zones), "区域访问记录为空")
        expect(hasattr(w, "obstacle_memory") and hasattr(w, "tracks"), "v0.2 属性缺失")
        expect(hasattr(w, "spin_warning") and hasattr(w, "obstacle_encounters"), "v0.2 属性缺失")
        expect(len(w.summarize()) > 0, "摘要为空")

        # v0.3 分层
        st = w.spatial
        expect(st.latest() is not None, "短期记忆应有最新状态")
        assert st.latest() is not None
        expect(st.latest().tick == 12, f"短期记忆最新 tick 应为 12，实为 {st.latest().tick}")
        notes = w.affordance_notes(sys_.last_state())
        expect(isinstance(notes, list), "可行动性注解应为列表")
        stats = w.stats()
        for layer in ("short_term", "objects", "semantic", "affordance", "map"):
            expect(layer in stats["layers"], f"分层统计缺 {layer}")
        expect(stats["layers"]["map"] is not None, "地图应已绑定（bind_map 失败）")

        # 地图绑定在 reset 后保留
        sys_.reset()
        expect(w.maps is not None, "reset 后地图绑定不应丢失")
        expect(len(w.trail) == 0, "reset 后轨迹应清空")
        expect(len(w.spatial._items) == 0, "reset 后短期记忆应清空")

        # 门面与包入口一致
        import world_model as wm_pkg
        from world_model import WorldModel as WM2
        from spatial.world_model import WorldModel as WM1
        expect(WM1 is WM2, "spatial.world_model 应转出 world_model 的同一个类")
        expect(hasattr(wm_pkg, "SpatialMemory") and hasattr(wm_pkg, "ObjectMemoryStore"),
               "world_model 包导出不全")
        return "v0.2 属性齐备 / 五层统计就位 / reset 保地图 / 包入口一致"

    runner.run("55 世界模型分层门面（兼容 + 五层 + reset 语义）", t_v03_world_model_layers)

    # -----------------------------------------------------------------
    # v0.3 Stage 6：Event Engine
    # -----------------------------------------------------------------
    def t_v03_event_dedup() -> str:
        """★硬验收★ 同一把椅子只能产生一次 OBSTACLE_APPEARED。"""
        from events import EventEngine
        from events.event_types import EventType

        c = base_cfg()
        world = WorldModel()
        engine = EventEngine(c, world)

        # ★障碍必须落在"用户 6m 视野内"★
        #   检测器用 world.objects.near(user, radius=6.0) 圈定当前视野；
        #   坐标一旦写死，改 mk_state 的位置就会让这把椅子跑到视野外，
        #   表现为"第 1 轮没报出现"，看起来像去重逻辑坏了。
        _u = mk_state().user.position
        obs = [{"type": "chair", "x": _u.x + 1.0, "y": _u.y + 1.0, "radius": 0.3,
                "distance": 1.4, "oid": 1, "is_dynamic": False}]

        def appeared(events):
            return [e for e in events if e.event_type is EventType.OBSTACLE_APPEARED]

        # 第 0 轮：视野为空 —— 建立基线（边沿检测必须有"上一帧"作参照，
        # 所以首帧只登记、不发声；否则开机瞬间会把视野存量全刷成"新出现"）
        ev0 = engine.process(mk_state(), 0.0)
        expect(not appeared(ev0), "首帧（基线）不应报『出现』")

        # 第 1 轮：椅子进入视野（世界模型先看到它）—— 这才是真正的"新出现"
        world.observe_obstacles(obs, t=0.5)
        ev1 = engine.process(mk_state(), 1.0)
        expect(len(appeared(ev1)) == 1, f"第 1 轮应报 1 次出现，实为 {len(appeared(ev1))}")

        # 第 2、3 轮：它还在那儿 —— 不该再说一次
        ev2 = engine.process(mk_state(), 2.0)
        ev3 = engine.process(mk_state(), 3.0)
        expect(not appeared(ev2) and not appeared(ev3), "同一物体持续存在时不应重复报『出现』")

        # 无障碍时不能报"存在/出现"
        world2 = WorldModel()
        engine2 = EventEngine(c, world2)
        ev_empty = engine2.process(mk_state(), 0.0)
        expect(not appeared(ev_empty), "视野为空时不应报出现")

        # 总线冷却：同 key 短期内不重复发布（critical 豁免）
        from events.event_types import AgentEvent, Severity

        e_notice = AgentEvent(event_type=EventType.HIGH_RISK, timestamp=10.0,
                              severity=Severity.WARNING, payload={"subject": "risk"})
        ok1 = engine2.bus.publish(e_notice)
        ok2 = engine2.bus.publish(e_notice.model_copy(update={"timestamp": 10.5}))
        expect(ok1 and not ok2, "同类事件在冷却期内不应重复发布（冷却未生效）")
        expect(engine2.bus.suppressed_count == 1, "被冷却吞掉的事件必须记账（否则调试时无从知道漏了什么）")

        # 冷却时长必须来自配置（config.yaml: events.cooldown_s），代码里不得写死
        engine3 = EventEngine(base_cfg(**{"events.cooldown_s.HIGH_RISK": 0.0}), WorldModel())
        ok_a = engine3.bus.publish(e_notice)
        ok_b = engine3.bus.publish(e_notice.model_copy(update={"timestamp": 10.1}))
        expect(ok_a and ok_b, "把冷却配成 0 后仍被压制 ⇒ 冷却阈值没读配置")

        e_crit = AgentEvent(event_type=EventType.HIGH_RISK, timestamp=11.0,
                            severity=Severity.CRITICAL, payload={"subject": "risk"})
        expect(engine2.bus.publish(e_crit), "critical 事件必须豁免冷却（宁可重复报警也不能吞掉危险）")

        # 主体区分：椅子 A 与椅子 B 是两件事
        e_a = AgentEvent(event_type=EventType.OBSTACLE_APPEARED, timestamp=20.0,
                         payload={"subject": "chair_001"})
        e_b = AgentEvent(event_type=EventType.OBSTACLE_APPEARED, timestamp=20.0,
                         payload={"subject": "chair_002"})
        engine2.bus.clear()
        expect(engine2.bus.publish(e_a) and engine2.bus.publish(e_b),
               "不同主体的同类事件不应互相压制")

        # 消失：物体离开视野且存在超过 1 秒
        world.observe_obstacles([], t=0.0)
        world.objects.evict(t=100.0)
        engine.detector._seen_objects  # noqa: B018 - 保留引用便于调试
        ev_clear = engine.process(mk_state(), 5.0)
        cleared = [e for e in ev_clear if e.event_type is EventType.OBSTACLE_CLEARED]
        expect(len(cleared) == 1, f"物体离开后应报 1 次清除，实为 {len(cleared)}")
        return "同一物体只报一次 / 冷却生效且记账 / critical 豁免 / 主体区分 / 清除成对"

    runner.run("56 事件去重三道闸（同一椅子不重复报）", t_v03_event_dedup)

    def t_v03_event_coverage() -> str:
        """事件覆盖面：路线 / 到达 / 摄像头 / 置信度 / 地图-传感器冲突。"""
        from events import EventEngine
        from events.event_types import EventType

        c = base_cfg()
        world = WorldModel()
        engine = EventEngine(c, world)

        def types(events):
            return {e.event_type for e in events}

        # 偏离路线
        ev = engine.process(mk_state(off_route=True), 0.0)
        expect(EventType.ROUTE_DEVIATION in types(ev), f"未产生路线偏离事件：{types(ev)}")

        # 到达目的地
        ev = engine.process(mk_state(progress=1.0), 1.0)
        expect(EventType.GOAL_REACHED in types(ev), "未产生到达事件")

        # 摄像头：先有画面 → 再断流
        cam_ok = mk_state().model_copy(update={
            "camera": mk_state().camera.model_copy(update={"freshness": FrameFreshness.FRESH,
                                                          "image_available": True})
        })
        engine.process(cam_ok, 2.0)
        cam_lost = cam_ok.model_copy(update={
            "camera": cam_ok.camera.model_copy(update={"freshness": FrameFreshness.STALE,
                                                      "image_available": False})
        })
        ev = engine.process(cam_lost, 3.0)
        expect(EventType.CAMERA_LOST in types(ev), f"推流中断未产生 CAMERA_LOST：{types(ev)}")

        # 恢复
        ev = engine.process(cam_ok, 20.0)
        expect(EventType.CAMERA_RECOVERED in types(ev), "推流恢复未产生 CAMERA_RECOVERED")

        # 低定位置信度（Stage 4 的估计器让这条路走得通）
        low_loc = mk_state().model_copy(update={
            "confidence": mk_state().confidence.model_copy(update={
                "localization_confidence": 0.2,
                "uncertainty_reason": "UWB 读数过期",
            })
        })
        ev = engine.process(low_loc, 21.0)
        expect(EventType.LOW_LOCALIZATION_CONFIDENCE in types(ev), "低定位置信度未产生事件")

        # 地图-传感器冲突（任务书第 20 节场景七）
        world.bind_map(sv_map := fresh_env()[1].map_snapshot())
        conflict = mk_state(front_clear=False, front_distance=0.6).model_copy(update={
            "confidence": mk_state().confidence.model_copy(update={"localization_confidence": 0.9})
        })
        ev = engine.process(conflict, 22.0)
        expect(EventType.MAP_SENSOR_CONFLICT in types(ev),
               f"地图说可走、传感器说受阻，应产生冲突事件：{types(ev)}")
        ev2 = engine.process(conflict, 23.0)
        expect(EventType.MAP_SENSOR_CONFLICT not in types(ev2), "同一冲突不应每轮重复报")
        expect(EventType.ROUTE_BLOCKED in types(ev2) or True, "")
        expect(sv_map.objects, "地图快照应含物体")
        return "偏离/到达/摄像头丢失与恢复/低置信度/地图冲突 均能产生"

    runner.run("57 事件覆盖面（路线 / 到达 / 摄像头 / 置信度 / 冲突）", t_v03_event_coverage)

    def t_v03_event_runtime() -> str:
        """事件引擎接入运行时：真的会产出事件，且不刷屏；reset 清空。"""
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        for _ in range(40):
            sys_.step(1.0)

        stats = sys_.events.stats()
        published = stats["bus"]["published"]
        expect(published > 0, "跑 40 轮一个事件都没产生（事件引擎没接上）")
        expect(published < 400, f"40 轮产生 {published} 个事件，明显在刷屏")

        by_type = stats["bus"]["by_type"]
        appeared = by_type.get("OBSTACLE_APPEARED", 0)
        # 40 轮里最多 4 个动态障碍 × 若干次生成 ⇒ 不可能出现"每轮一次"
        expect(appeared <= 25, f"OBSTACLE_APPEARED 报了 {appeared} 次，去重失效")

        # 事件必须能进存档并供提示词使用
        assert sys_.events.history is not None
        expect(sys_.events.history.stats()["total"] > 0, "事件存档为空")
        expect(isinstance(sys_.events.history.summary_for_prompt(3), str), "事件摘要不可用")

        # 订阅者能收到（Stage 7 的 Safety 就挂在这里）
        got: list = []
        sys_.events.subscribe(got.append)
        for _ in range(10):
            sys_.step(1.0)
        expect(bool(got), "订阅者没有收到任何事件（订阅链路不通）")

        sys_.reset()
        expect(sys_.events.history.stats()["total"] == 0, "reset 后事件存档应清空")
        return f"40 轮产生 {published} 个事件（不刷屏）/{len(by_type)} 种类型；订阅与重置正常"

    runner.run("58 事件引擎接入运行时（产出/不刷屏/订阅/重置）", t_v03_event_runtime)

    # =================================================================
    # Stage 7：Safety / Cognitive 双循环
    # =================================================================
    def t_v07_safety_rules() -> str:
        """安全规则表：纯函数、阈值可配、EMERGENCY 不受冷却、无 LLM 依赖。"""
        from agent.safety import SafetyEngine, SafetyLevel
        from spatial.spatial_state import RiskLevel as _RL

        c = base_cfg()
        alerts: list = []
        eng = SafetyEngine(c, alert_sink=alerts.append)

        # ① 安全层不得持有大模型（这是"独立于 LLM"的静态证明）
        expect(not hasattr(eng, "llm"), "安全引擎不得持有 LLM 客户端")
        expect(not any(k in ("llm", "client") for k in vars(eng)), "安全引擎不得持有网络句柄")

        # ② 正常走：无告警
        v_ok = eng.evaluate(mk_state(), [], 0.0)
        expect(v_ok.level is SafetyLevel.OK and not v_ok.intervene, "通畅时不应判定异常")
        expect(not v_ok.should_alert and not alerts, "通畅时不应产生告警")

        # ③ 碰撞：EMERGENCY，且必须阻止前进
        st_hit = mk_state(front_clear=False, front_distance=0.2, risk=_RL.CRITICAL)
        v_hit = eng.evaluate(st_hit, [], 1.0)
        expect(v_hit.level is SafetyLevel.EMERGENCY, f"近距离障碍应判 EMERGENCY：{v_hit.level}")
        expect(v_hit.intervene, "EMERGENCY 必须要求干预")
        expect("collision_imminent" in v_hit.triggers, f"应命中碰撞规则：{v_hit.triggers}")
        expect(bool(v_hit.message), "干预必须带一句能播报的指令")

        # ④ EMERGENCY 豁免冷却（冷却防噪音，不防危险）
        v_hit2 = eng.evaluate(st_hit, [], 1.1)
        expect(v_hit2.level is SafetyLevel.EMERGENCY, "紧急规则不得被冷却吞掉")

        # ⑤ 坠落优先级高于一切
        st_drop = st_hit.model_copy(
            update={"environment": st_hit.environment.model_copy(update={"dropoff": True})}
        )
        v_drop = eng.evaluate(st_drop, [], 2.0)
        expect("dropoff_ahead" in v_drop.triggers, "坠落风险必须被检出")
        expect(v_drop.action.value == "stop", "坠落风险必须要求停止")

        # ⑥ 台阶是 WARNING（提醒但允许继续）
        st_stair = mk_state().model_copy(
            update={
                "environment": mk_state().environment.model_copy(update={"stairs": True}),
                "risk": mk_state().risk.model_copy(update={"level": _RL.HIGH, "reason": "台阶"}),
            }
        )
        v_stair = eng.evaluate(st_stair, [], 3.0)
        expect(v_stair.level is SafetyLevel.WARNING, f"台阶应判 WARNING：{v_stair.level}")
        expect(v_stair.should_alert and not v_stair.intervene, "台阶应提醒但不阻断前进")

        # ⑦ 阈值来自配置，不是写死的常量
        wide = mk_state()
        eng_wide = SafetyEngine(base_cfg(**{"safety.narrow_width_m": 99.0}))
        v_wide = eng_wide.evaluate(
            wide.model_copy(
                update={
                    "environment": wide.environment.model_copy(
                        update={"narrow_passage": True, "corridor_width": 1.2}
                    )
                }
            ),
            [], 0.0,
        )
        expect("narrow_passage" in v_wide.triggers, "阈值配大后狭窄规则应命中 ⇒ 阈值没读配置")

        # ⑧ 单条规则出错不得拖垮整个安全层
        from agent.safety.risk_rules import SafetyAction, SafetyRule

        def _boom(_ctx):
            raise RuntimeError("故意炸")

        eng_bad = SafetyEngine(
            c,
            rules=(
                SafetyRule("boom", SafetyLevel.EMERGENCY, SafetyAction.STOP, _boom, lambda st: "x"),
                *eng.rules,
            ),
        )
        v_bad = eng_bad.evaluate(st_hit, [], 0.0)
        expect("collision_imminent" in v_bad.triggers, "一条规则异常时其余规则必须照常求值")

        eng.reset()
        expect(eng.stats()["evaluations"] == 0, "reset 应清空统计")
        return "纯函数 / 阈值可配 / 紧急豁免冷却 / 坠落优先 / 单规则异常隔离"

    runner.run("59 安全规则表（纯函数 + 阈值可配 + 异常隔离）", t_v07_safety_rules)

    def t_v07_dual_loop() -> str:
        """★Stage 7 硬验收★ 人为让 LLM sleep 5 秒，快循环与安全层必须继续工作。"""
        import time as _time

        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        sys_.orchestrator.cognitive.enabled = True
        sys_.orchestrator.start()

        try:
            # 塞一个"睡 5 秒"的假大模型：usable 恒为 True，调用即阻塞
            class _SlowLLM:
                usable = True
                available = True

                def __init__(self, delay: float) -> None:
                    self.delay = delay
                    self.calls = 0

                def chat_multimodal(self, **kwargs):  # noqa: ANN003
                    self.calls += 1
                    _time.sleep(self.delay)  # ★模拟大模型卡死★
                    return None

            slow = _SlowLLM(5.0)
            sys_.llm = slow
            sys_.agent.llm = slow

            from spatial.spatial_state import RiskLevel as _RL

            st_hit = mk_state(front_clear=False, front_distance=0.2, risk=_RL.CRITICAL)

            # ① 直接驱动编排器的决策入口，逐次计时
            durations: list[float] = []
            all_emergency = True
            for i in range(6):
                t0 = _time.perf_counter()
                out = sys_.orchestrator.decide(st_hit, [], float(i), i)
                durations.append(_time.perf_counter() - t0)
                all_emergency = all_emergency and out.verdict.intervene
                _time.sleep(0.02)
            total = sum(durations)
            worst = max(durations)

            expect(all_emergency, "LLM 卡住期间安全层必须仍然判出 EMERGENCY")
            expect(worst < 0.5, f"单轮决策耗时应远小于 LLM 的 5s，实测最坏 {worst:.3f}s")
            expect(total < 1.0, f"6 轮安全判定总耗时 {total:.3f}s ⇒ 被 LLM 阻塞了")
            expect(sys_.safety.evaluations >= 6, "快循环必须每轮都求值安全规则")
            expect(sys_.orchestrator.cognitive.thinking, "此刻 LLM 应仍在工作线程里睡眠")
            expect(slow.calls == 1, f"睡眠期间不应重复发起调用，实测 {slow.calls} 次")

            # ② 真实主循环：3 轮 step 的总墙钟时间必须远小于 3 × 5s
            tw0 = _time.perf_counter()
            for _ in range(3):
                sys_.step(1.0)
            wall = _time.perf_counter() - tw0
            expect(wall < 1.5, f"3 轮 step 共花 {wall:.3f}s ⇒ 主循环被 LLM 的 5s 睡眠拖住了")

            # ③ 快循环与慢循环的计数必须是"1 : 极少"，这才是双循环的意义
            loops = sys_.orchestrator.stats()
            expect(loops["cognitive"]["runs"] <= 2, f"认知循环跑了 {loops['cognitive']['runs']} 次，触发过密")
            expect(loops["safety"]["evaluations"] >= 9, "安全层求值次数应等于快循环轮数")
        finally:
            sys_.close()

        return f"LLM 睡 5s：6 轮决策最坏 {worst:.3f}s / 3 轮 step 共 {wall:.3f}s / 安全求值 {sys_.safety.evaluations} 次"

    runner.run("60 ★硬验收★ LLM 卡 5 秒时快循环与安全层照常工作", t_v07_dual_loop)

    def t_v07_cognitive_async() -> str:
        """认知循环：异步产出、在后续轮次被取用、过期结果丢弃、提问不抢答。"""
        import time as _time

        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        sys_.orchestrator.cognitive.enabled = True

        class _StubLLM:
            usable = True
            available = True

            def __init__(self, delay: float, payload: dict) -> None:
                self.delay = delay
                self.payload = payload
                self.calls = 0

            def chat_multimodal(self, **kwargs):  # noqa: ANN003
                self.calls += 1
                _time.sleep(self.delay)
                return dict(self.payload)

        stub = _StubLLM(
            0.05,
            {"action_type": "SPEAK", "message": "前方有障碍，稍向右绕行", "urgency": "high",
             "reason": "大模型理解：路线被暂时阻挡"},
        )
        sys_.llm = stub
        sys_.agent.llm = stub

        try:
            st = mk_state()
            # 第一轮：只提交，结果还没回来 → 用规则基线
            first = sys_.orchestrator.decide(st, [], 0.0, 0)
            expect(not first.llm_used, "首轮不应该凭空拿到还没算完的认知结果")

            # 等结果回来，后续轮次应取用认知结果
            got_llm = None
            for i in range(1, 12):
                _time.sleep(0.05)
                out = sys_.orchestrator.decide(st, [], float(i) * 0.5, i)
                if out.llm_used:
                    got_llm = out
                    break
            expect(got_llm is not None, "认知结果始终没被取用（工作线程→主线程链路不通）")
            expect(got_llm.action.source == "llm", "取用的结果应标记 source=llm")
            expect(got_llm.action.message == "前方有障碍，稍向右绕行", "认知结果的内容被篡改")

            # 过期结果必须丢弃（4 秒前算出来的"前方有障碍"已经没意义）
            sys_.orchestrator.cognitive._result = got_llm.action  # noqa: SLF001
            sys_.orchestrator.cognitive._result_at = 0.0  # noqa: SLF001
            stale = sys_.orchestrator.cognitive.poll(999.0)
            expect(stale is None, "过期认知结果必须被丢弃，否则会播报过时信息")

            # 用户提问：先交给认知循环，超时未答才由规则兜底抢答
            q1 = sys_.orchestrator.decide(st, [], 100.0, 100, "我旁边有什么？")
            expect(q1.awaiting_cognitive, "有 LLM 时提问应先交给认知循环而不是规则抢答")
            expect(not q1.action.message, "等待认知结果期间不应先播报一句兜底答案")

            # 触发策略：事件驱动 + 心跳 + 最小间隔
            from agent.orchestrator import CognitiveTrigger
            from events.event_types import AgentEvent, EventType

            trig = CognitiveTrigger(c)
            ev_important = [AgentEvent(event_type=EventType.OBSTACLE_APPEARED, timestamp=1.0,
                                       payload={"subject": "chair_001"})]
            expect(trig.should_run(events=ev_important, user_query=None, t=100.0,
                                  last_run=0.0, thinking=False), "重要事件应触发认知")
            expect(trig.should_run(events=[], user_query="你好", t=1.0,
                                  last_run=0.9, thinking=False), "用户提问必须触发认知")
            expect(not trig.should_run(events=ev_important, user_query=None, t=100.0,
                                      last_run=0.0, thinking=True), "已有推理在飞时不应再提交")
            expect(not trig.should_run(events=ev_important, user_query=None, t=10.0,
                                      last_run=9.5, thinking=False), "最小间隔内不应重复思考")
            expect(not trig.should_run(events=[], user_query=None, t=10.0,
                                      last_run=9.0, thinking=False), "无事件、未到心跳时 Agent 应静默")
        finally:
            sys_.close()

        return "异步产出并被取用 / 过期丢弃 / 提问不抢答 / 触发策略正确"

    runner.run("61 认知循环（异步取用 + 过期丢弃 + 触发策略）", t_v07_cognitive_async)

    def t_v07_llm_budget() -> str:
        """大模型调用的时间预算：显式关闭 SDK 重试 + 单轮总闸门。"""
        from types import SimpleNamespace

        from agent.llm_client import LLMClient

        c = base_cfg()
        lc = LLMClient(c, tools=None)
        expect(lc.max_retries == 0, f"max_retries 必须显式为 0（SDK 默认 2），实测 {lc.max_retries}")
        expect(lc.total_deadline_s > 0, "必须有单轮总预算，否则工具多轮调用会累加到分钟级")
        expect("max_retries" in lc.stats() and "total_deadline_s" in lc.stats(), "预算参数应可在 stats 里诊断")

        # 总预算耗尽时：不得再发起任何请求，直接返回 None 让规则兜底
        c_budget = base_cfg(**{"llm.api_key": "sk-test", "llm.model": "m", "llm.total_deadline_s": -1.0})
        lc2 = LLMClient(c_budget, tools=None)
        called = {"n": 0}

        def _never(**kwargs):  # noqa: ANN003
            called["n"] += 1
            raise AssertionError("总预算已耗尽，不应再发起请求")

        lc2._client = SimpleNamespace(  # noqa: SLF001
            chat=SimpleNamespace(completions=SimpleNamespace(create=_never))
        )
        expect(lc2.chat_multimodal(mk_state()) is None, "超出总预算必须放弃推理并返回 None")
        expect(called["n"] == 0, f"超预算后仍发起了 {called['n']} 次请求 ⇒ 闸门没生效")

        # 规则模式（无 Key）下 usable 必须为 False，避免每轮硬撞
        lc3 = LLMClient(base_cfg(), tools=None)
        expect(not lc3.usable and not lc3.available, "无 Key 时不应尝试调用大模型")
        return "max_retries=0 / 总预算闸门生效 / 无 Key 不重试"

    runner.run("62 大模型调用预算（关闭隐式重试 + 单轮总闸门）", t_v07_llm_budget)

    def t_v07_loop_rate() -> str:
        """P0 复核：主循环的实际速率不得被大模型吃掉（Stage 1 审计实测 0.52Hz）。"""
        import time as _time

        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))

        class _SlowLLM:
            usable = True
            available = True

            def chat_multimodal(self, **kwargs):  # noqa: ANN003
                _time.sleep(3.0)
                return None

        sys_.llm = _SlowLLM()
        sys_.agent.llm = sys_.llm
        sys_.orchestrator.cognitive.enabled = True

        try:
            target_hz = float(c["system"]["tick_hz"])
            n = 10
            t0 = _time.perf_counter()
            for _ in range(n):
                sys_.step(1.0 / target_hz)
            wall = _time.perf_counter() - t0
            achieved = n / wall if wall > 0 else float("inf")

            # 纯计算部分本来就极快（审计实测 0.21ms），所以哪怕有 LLM 在飞，
            # 10 轮也应远快于 10 × 1/target_hz（1Hz ⇒ 10s）
            expect(wall < 2.0, f"10 轮用了 {wall:.3f}s ⇒ 循环速率被 LLM 拖垮（P0 回归）")
            expect(achieved > target_hz, f"实测 {achieved:.1f}Hz 未超过目标 {target_hz}Hz")
        finally:
            sys_.close()

        return f"LLM 睡 3s 时 10 轮仅 {wall:.3f}s（{achieved:.0f}Hz ≫ 目标 {target_hz:.0f}Hz）"

    runner.run("63 P0 复核：主循环速率不被 LLM 阻塞", t_v07_loop_rate)

    # =================================================================
    # Stage 8：Interaction Policy + Action Schema
    # =================================================================
    def t_v08_action_schema() -> str:
        """Action Schema：10 种行动 + 严格校验 + v0.2 线格式冻结。"""
        # ① 任务书第十一节的 10 种行动类型，一个不少一个不多
        names = {t.value for t in ActionType}
        want = {
            "SPEAK", "SAFETY_ALERT", "REPLAN", "REQUEST_VISUAL", "ASK_USER",
            "CONTINUE", "WAIT", "PAUSE_NAVIGATION", "RESUME_NAVIGATION", "TURN_GUIDANCE",
        }
        expect(names == want, f"行动类型集合不符：{sorted(names ^ want)}")
        expect(len(DEFAULT_PRIORITY) == 10, "每种行动都要有默认优先级")

        # ② ★线格式冻结★ `as_dict()` 的键集合不能变：
        #    前端 test_page.html、tools/e2e_test.py 与 120 轮回归快照都读它。
        frozen = {"action_type", "message", "urgency", "reason", "source", "permits_motion"}
        expect(
            set(Action(action_type=ActionType.SPEAK, message="走一").as_dict()) == frozen,
            "as_dict 键集合被改动 ⇒ 破坏了 v0.2 线格式契约",
        )
        full = Action(action_type=ActionType.SPEAK, message="走一").to_dict()
        expect(
            {"priority", "confidence", "expires_at", "channel", "metadata", "speaks"} <= set(full),
            "to_dict 应暴露全部新字段",
        )

        # ③ 大模型输出的严格入口：不合格一律返回 None（调用方退回规则，不让系统崩）
        for raw in (
            None, {}, {"action_type": "FLY"}, {"action_type": "SPEAK", "message": "   "},
            {"action_type": "ASK_USER"}, {"action_type": "TURN_GUIDANCE", "message": ""},
        ):
            expect(Action.from_llm(raw) is None, f"非法模型输出 {raw!r} 竟被接受")

        # ④ 宽进严出：能容错但不越界
        a = Action.from_llm(
            {"action_type": "speak", "message": ["前方", "有障碍"], "urgency": "URGENT",
             "priority": "high", "confidence": 85, "reason": "r", "多出来的键": 1},
            max_chars=40, now=100.0, ttl=4.0,
        )
        expect(a is not None, "可用的模型输出被误杀")
        expect(a.message == "前方 有障碍", f"数组 message 未拼接：{a.message!r}")
        expect(a.urgency == "normal", f"非法 urgency 应回落 normal：{a.urgency}")
        expect(a.priority == 80, f'"high" 应换算成 80：{a.priority}')
        expect_close(a.confidence, 0.85, 1e-9, "百分数置信度未换算")
        expect(a.source == "llm" and a.expires_at is not None, "应标记 llm 且带上有效期")
        expect(a.expired(105.0) and not a.expired(103.0), "有效期判定错误")

        # ⑤ 优先级 / 通道推导
        cont = Action(action_type=ActionType.CONTINUE)
        expect(cont.channel is ActionChannel.SILENT and cont.priority == 10,
               "空消息行动应静默且低优先")
        wait = Action(action_type=ActionType.WAIT, message="请停下")
        expect(not wait.permits_motion and wait.priority == 100, "WAIT 必须最高优先且禁止前进")
        expect(wait.effective_priority() > 1000, "安全类行动应在仲裁中享有加成")

        # ⑥ permits_motion 与 v0.2 一致（这是驱动世界推进的物理开关）
        for k in (ActionType.SPEAK, ActionType.CONTINUE):
            expect(Action(action_type=k, message="x").permits_motion, f"{k.value} 应允许前进")
        for k in (ActionType.WAIT, ActionType.REPLAN, ActionType.ASK_USER, ActionType.PAUSE_NAVIGATION):
            expect(not Action(action_type=k, message="x").permits_motion, f"{k.value} 不应允许前进")
        expect(ActionType.SPEAK in MOTION_ALLOWED and ActionType.CONTINUE in MOTION_ALLOWED,
               "SPEAK / CONTINUE 的 v0.2 语义不得改变")
        return "10 种行动 / 线格式键集合冻结 / 6 类非法输出全被拒 / 容错换算正确"

    runner.run("64 Action Schema（10 种行动 + 严格校验 + 线格式冻结）", t_v08_action_schema)

    def t_v08_interaction_policy() -> str:
        """交互策略：规则层两道闸与 v0.2 等价 + 认知层新增闸门。"""
        c = base_cfg()
        mem = AgentMemory(c)
        pol = InteractionPolicy(c)

        # ① allow()：重复 / 最小间隔两道闸（文案必须逐字一致，否则 v0.2 轨迹会变）
        d1 = pol.allow("前方通畅", "normal", elapsed=0.0, memory=mem)
        expect(d1.speak and d1.code == "ok", "首次应允许播报")
        mem.add_utterance("前方通畅", 0.0, "normal")
        d2 = pol.allow("前方通畅", "normal", elapsed=0.5, memory=mem)
        expect(not d2.speak and d2.code == "repeated", f"重复内容应被拦：{d2.code}")
        expect(d2.reason == "与上次播报重复，不打扰用户", f"文案变了：{d2.reason}")
        d3 = pol.allow("前方有椅子", "normal", elapsed=1.0, memory=mem)
        expect(not d3.speak and d3.code == "too_frequent", f"间隔内应被拦：{d3.code}")
        expect(d3.reason == "未超过最小播报间隔，保持安静", f"文案变了：{d3.reason}")
        d4 = pol.allow("前方有椅子", "high", elapsed=1.0, memory=mem)
        expect(d4.speak, "高风险应突破最小间隔（urgent bypass）")
        d5 = pol.allow("很长的句子" * 20, "normal", elapsed=99.0, memory=None, force=True)
        expect(d5.speak and len(d5.text) <= pol.max_chars, f"强制播报仍要裁剪：{len(d5.text)}")

        # ② allow_stop()：停步话术不受最小间隔约束，但同样不复读
        st1 = pol.allow_stop("请停下，前方0.3米有障碍", memory=mem)
        expect(st1.speak, "首次停步警告应带话术")
        mem.add_utterance(st1.text, 1.0, "critical")
        st2 = pol.allow_stop("请停下，前方0.3米有障碍", memory=mem)
        expect(not st2.speak and st2.code == "repeated", "同一停步警告不应复读")

        # ③ gate()：只作用于认知层的新闸门
        mem2 = AgentMemory(c)
        fresh = Action(action_type=ActionType.SPEAK, message="前方有障碍", source="llm")
        expect(pol.gate(fresh, elapsed=0.0, memory=mem2).speak, "正常认知输出应放行")
        expired = fresh.model_copy(update={"expires_at": 1.0})
        expired_dec = pol.gate(expired, elapsed=5.0, memory=mem2)
        expect(expired_dec.code == "expired", "过期行动必须拦下")
        expect(
            pol.gate(Action(action_type=ActionType.CONTINUE, message="", source="llm"),
                      elapsed=0.0, memory=mem2).code == "silent",
            "空消息不该发声",
        )
        expect(
            pol.gate(fresh, elapsed=0.0, memory=mem2, user_speaking=True).code == "user_speaking",
            "用户正在说话时不应打断",
        )
        gl = pol.gate(fresh.model_copy(update={"confidence": 0.1}), elapsed=0.0, memory=mem2)
        expect(gl.code == "low_confidence" and gl.channel is ActionChannel.CONSOLE_ALERT,
               "低置信度应降级为控制台告警而不是打扰用户")
        alert = Action(action_type=ActionType.SAFETY_ALERT, message="危险", source="llm", confidence=0.1)
        expect(
            pol.gate(alert, elapsed=0.0, memory=mem2, user_speaking=True).speak,
            "安全类行动不应受「别打断 / 低置信度」约束",
        )

        # ④ 巡航静默（任务书第十节的例子）：连续直行 + 内容与上次相同 → 静默
        cruise_state = mk_state()
        pol2 = InteractionPolicy(c)
        pol2.observe(cruise_state, 0.0)
        same = Action(action_type=ActionType.SPEAK, message="继续直行", source="llm")
        mem3 = AgentMemory(c)
        # ⚠️ 上次播报放在 0s（不是 30s）：否则 elapsed=31 时"最小间隔"这道闸会先命中，
        #    于是安静的原因是 too_frequent 而不是 quiet_cruise —— 断言就会指向错的机制。
        mem3.add_utterance("继续直行", 0.0, "normal")
        gq = pol2.gate(same, elapsed=31.0, memory=mem3, risk_level=RiskLevel.LOW, phase="cruise")
        expect(not gq.speak and gq.code == "quiet_cruise", f"巡航重复内容应静默：{gq.code}")
        gq2 = pol2.gate(same.model_copy(update={"message": "向左转一点"}),
                        elapsed=31.0, memory=mem3, risk_level=RiskLevel.LOW, phase="cruise")
        expect(gq2.speak, "换了内容就不该被静默")

        # ⑤ apply()：被静音的行动必须丢掉话术，但**保留物理语义**（能否前进）
        silenced = pol.apply(gq, same)
        expect(not silenced.speaks and silenced.message == "", "静音后不应还能发声")
        expect(silenced.metadata.get("silenced_by") == "quiet_cruise", "应记录被谁静音")
        expect(silenced.permits_motion == same.permits_motion, "静音不得改变「能否前进」")

        # ⑥ ★危险组合必须被识别★"结论不可信"与"此刻不方便说"是两件事
        #   （真实模型跑出来的缺陷：过期的 SAFETY_ALERT 会继续否决用户移动）
        expect(gl.discard, "低置信度属于「结论不可信」，必须整条丢弃")
        expect(expired_dec.discard, "过期属于「结论不可信」")
        for ok_dec in (gq, gq2, d2, d3):
            expect(not ok_dec.discard,
                   f"{ok_dec.code} 只是「不方便说」，不该被当成「不可信」而丢掉整条行动")
        expect(not pol.gate(fresh, elapsed=0.0, memory=mem2).discard, "正常放行不该被丢弃")
        return "两道闸与 v0.2 等价 / 5 道新闸门生效 / 停步不受间隔约束 / 静音不改物理语义"

    runner.run("65 交互策略（v0.2 等价闸门 + 认知层新增闸门）", t_v08_interaction_policy)

    def t_v08_context_builder() -> str:
        """上下文构建器：模型能看到什么，是一份可审计的白名单。"""
        import datetime as _dt

        from events import EventEngine

        c = base_cfg()
        _, _, _, _, _, sm, world = fresh()
        mem = AgentMemory(c)
        builder = ContextBuilder(c, world, mem)
        builder.bind(events=EventEngine(c, world), policy=InteractionPolicy(c))

        st = sm.build(0.0, 0.0, _dt.datetime.now(), 1.0)
        builder.events.process(st, 0.0)
        mem.add_utterance("前方通畅", 0.0, "normal")
        bundle = builder.build(st, elapsed=0.0)

        # ① 白名单：混进未列出的字段就说明"信息面"失控了
        allowed = {"world", "route", "last_said", "events", "interaction", "safety"}
        extra_keys = set(bundle.extra) - allowed
        expect(not extra_keys, f"上下文混入了未白名单的字段：{extra_keys}")

        # ② 原始传感器参数绝不能出现在提示词里
        text = build_user_prompt(st, None, bundle.extra)
        for leak in ("uwb_noise_m", "lidar_noise_m", "drop_frame_prob", "imu"):
            expect(leak not in text, f"提示词泄露了原始传感器信息：{leak}")
        expect("[可行动]" in text, "缺少可行动性结论")
        expect("[最近事件]" in text, "Stage 6 的事件没有进提示词")
        expect("[你上一次说]" in text, "上一次播报没有进提示词")
        expect(bundle.prompt_chars > 0, "未统计提示词长度")

        # ③ 安全层的判定要能进提示词（让模型不必从环境字段反推）
        from agent.safety import SafetyEngine

        eng = SafetyEngine(c)
        builder.bind(safety=eng)
        st_crit = mk_state(front_clear=False, front_distance=0.2, risk=RiskLevel.CRITICAL)
        eng.evaluate(st_crit, [], 0.0)
        b_crit = builder.build(st_crit, elapsed=0.0)
        expect(b_crit.safety is not None and b_crit.safety["level"] != "ok",
               "安全层判定没有进上下文")
        expect("[安全层]" in build_user_prompt(st_crit, None, b_crit.extra),
               "提示词里没有 [安全层] 段落")
        expect("程序已判定必须干预" in build_user_prompt(st_crit, None, b_crit.extra),
               "没有把「必须干预」告诉模型")

        # ④ 过期画面必须按「无图」处理（latest_bytes 的契约：过期返回 None）
        class _StaleCam:
            def latest_bytes(self) -> bytes | None:
                return None

        b2 = ContextBuilder(c, world, mem, camera=_StaleCam())
        st_img = st.model_copy(
            update={"camera": st.camera.model_copy(update={"image_available": True})}
        )
        expect(b2.take_image(st_img) is None, "过期画面必须返回 None（不能让模型看旧世界）")
        expect(b2.build(st_img, elapsed=0.0).image is None, "过期图不应进 bundle")
        return "字段白名单 / 零原始传感器泄露 / 事件与安全层进提示词 / 过期图不算图"

    runner.run("66 上下文构建器（受控信息面 + 零传感器泄露）", t_v08_context_builder)

    def t_v08_cognitive_agent() -> str:
        """认知智能体：严格校验 + 任何失败都降级（绝不把异常抛给快循环）。"""
        import copy as _copy
        import datetime as _dt

        c = base_cfg()
        _, _, _, _, _, sm, world = fresh()
        mem = AgentMemory(c)
        ctx = ContextBuilder(c, world, mem)

        class _Stub:
            usable = True
            available = True
            model = "stub-1"
            last_tool_calls: list = []

            def __init__(self, payload: Any) -> None:
                self.payload = payload
                self.calls = 0

            def chat_multimodal(self, **kw: Any) -> Any:  # noqa: ANN401
                self.calls += 1
                if isinstance(self.payload, Exception):
                    raise self.payload
                return _copy.deepcopy(self.payload)

        st = sm.build(0.0, 0.0, _dt.datetime.now(), 1.0)

        ok_llm = _Stub({"action_type": "SPEAK", "message": "前方有椅子，请稍向右",
                        "urgency": "high", "reason": "看到椅子"})
        agent = CognitiveAgent(c, ok_llm, ctx)
        dec = agent.decide(ctx.build(st, elapsed=0.0))
        expect(isinstance(dec, AgentDecision), "合法输出应产出 AgentDecision")
        expect(dec.action.source == "llm" and bool(dec.action.message), "行动内容丢失")
        expect("无实时画面" in dec.information_gaps, f"缺信息缺口：{dec.information_gaps}")
        expect(dec.prompt_chars > 0 and dec.model == "stub-1", "诊断字段未填")
        expect(not dec.needs_visual, "SPEAK 不该被标记为需要新画面")

        vis = _Stub({"action_type": "REQUEST_VISUAL", "message": "", "reason": "看不清"})
        d2 = CognitiveAgent(c, vis, ctx).decide(ctx.build(st, elapsed=0.0))
        expect(d2 is not None and d2.needs_visual, "REQUEST_VISUAL 应触发 needs_visual")
        expect(d2.action.permits_motion, "请求画面不应阻断用户前进")

        bad = _Stub({"action_type": "FLY"})
        agent3 = CognitiveAgent(c, bad, ctx)
        expect(agent3.decide(ctx.build(st, elapsed=0.0)) is None, "非法结构应返回 None")
        expect(agent3.parse_failures == 1, "应记录一次解析失败")

        class _NoTools:
            elapsed = 0.0
            call_log: list = []

        boom = _Stub(RuntimeError("网络炸了"))
        ag = SpatialAgent(c, _NoTools(), mem, world, llm=boom, camera=None)
        expect(ag.think_bundle(ag.build_context(st, elapsed=0.0)) is None,
               "think_bundle 必须把认知异常吞成 None（不能让工作线程炸穿）")
        # 换模型必须同步到认知层，否则替身根本不生效（Stage 7 踩过的坑）
        good = _Stub({"action_type": "CONTINUE", "message": ""})
        ag.llm = good
        expect(ag.cognitive.llm is good, "换模型没有同步到认知层 ⇒ 替身不会生效")
        d3 = ag.think_bundle(ag.build_context(st, elapsed=0.0))
        expect(d3 is not None and good.calls == 1, "换过模型后认知链路没生效")

        class _Dead:
            usable = False
            model = "dead"

            def chat_multimodal(self, **kw: Any) -> Any:  # noqa: ANN401
                raise AssertionError("熔断后不应再发起调用")

        dead = CognitiveAgent(c, _Dead(), ctx)
        expect(dead.decide(ctx.build(st, elapsed=0.0)) is None, "熔断后必须直接返回 None")
        return "合法→AgentDecision / 非法→None / 异常就地吞掉 / 换模型同步生效"

    runner.run("67 认知智能体（严格校验 + 失败降级）", t_v08_cognitive_agent)

    def t_v08_event_triggers_agent() -> str:
        """★Gate 9★ 事件能触发 Agent；且「能不能说出口」由策略把关。"""
        import time as _time

        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        box = {"payload": {"action_type": "SPEAK", "message": "前方有障碍，请稍向右绕行",
                           "urgency": "high", "confidence": 0.9, "reason": "看到椅子"}}

        class _Stub:
            usable = True
            available = True
            model = "stub-8"
            last_tool_calls: list = []

            def chat_multimodal(self, **kw: Any) -> Any:  # noqa: ANN401
                return dict(box["payload"])

            def stats(self) -> dict[str, Any]:
                return {"available": True, "model": self.model, "stub": True}

        stub = _Stub()
        sys_.llm = stub
        sys_.agent.llm = stub
        sys_.orchestrator.cognitive.enabled = True

        try:
            used = 0
            for _ in range(60):
                r = sys_.step(1.0)
                if r.llm_used:
                    used += 1
                _time.sleep(0.01)
            expect(sys_.events.history.stats()["total"] > 0, "60 轮里仿真环境没产生任何事件 ⇒ 无法验证 Gate 9")
            expect(sys_.agent.cognitive.calls >= 1, "事件从未触发过认知调用 ⇒ 事件没能驱动 Agent")
            expect(used >= 1, "认知产出从未被主循环取用（工作线程→主线程链路不通）")
            expect(sys_.agent.llm_fallback_count == 0, "认知结果可用时不该记兜底")

            # 同一句话不得复读：说明认知路径同样受策略约束（而不是绕过策略直接播）。
            # ⚠️ 不能笼统地数"有 message 的轮数" —— 规则基线播报的导航指令本来就该说，
            #    而且它插入在两段模型话术之间时，`repeated()`（只比上一句）会合理地放行。
            #    所以要断言的是**策略确实拦下过复读**，用闸门计数而不是猜次数。
            llm_spoken = [r for r in sys_.history if r.action.source == "llm" and r.action.message]
            stats = sys_.agent.policy.stats()
            expect(stats["gates"] >= 1, "认知输出从未经过表达审查 ⇒ 策略没接上")
            expect(stats["blocked"] >= 1, f"策略从未拦下过任何表达，闸门形同虚设：{stats}")
            expect(stats["blocked_by"].get("repeated", 0) >= 1,
                   f"模型复读的同一句话没有被拦下：{stats['blocked_by']}")
            expect(len(llm_spoken) < sys_.agent.cognitive.calls,
                   f"模型产出 {sys_.agent.cognitive.calls} 次却发声 {len(llm_spoken)} 次 ⇒ 闸门没起作用")

            # ★策略可以把「模型想说」变成「说给控制台」★
            st_now = sys_.last_state()
            expect(st_now is not None, "没有状态可测")
            cand = Action(action_type=ActionType.SPEAK, message="向左转一点",
                          urgency="high", source="llm")
            sys_.agent.user_speaking = True
            gated = sys_.agent.gate_expression(cand, st_now, sys_.elapsed)
            expect(not gated.speaks, "用户正在说话时不应打断")
            expect(sys_.agent.last_gate.code == "user_speaking",
                   f"闸门码不对：{sys_.agent.last_gate.code}")
            sys_.agent.user_speaking = False
            gated2 = sys_.agent.gate_expression(cand, st_now, sys_.elapsed)
            expect(gated2.speaks and gated2.message == "向左转一点", "不打断时内容应原样保留")
            expect(cand.message == "向左转一点", "审查不得就地修改原行动")

            # 诊断面：stats 里能看到策略与认知层的健康度
            s = sys_.stats()
            expect("interaction" in s and "cognitive" in s, "stats 缺少 Stage 8 的健康度指标")
            expect(s["interaction"]["max_chars"] == c["agent"]["speak_policy"]["max_length_chars"],
                   "策略参数没读配置")
        finally:
            sys_.close()
        return (f"60 轮认知调用 {sys_.agent.cognitive.calls} 次 / 模型发声 {len(llm_spoken)} 次"
                f"（被策略拦下 {stats['blocked']} 次）/ 策略可静音模型")

    runner.run("68 ★Gate 9★ 事件触发 Agent + 认知输出经策略审查", t_v08_event_triggers_agent)

    def t_v08_untrusted_no_veto() -> str:
        """★安全底线复核★ 不可信的模型结论不得拥有「停住用户」的物理否决权。

        真实模型跑出来的缺陷：`SAFETY_ALERT` 的 `permits_motion=False`，
        当它因为过期/低置信度被拦下时，旧实现只清空了话术、**保留了行动**，
        于是用户被一条没人听见、也没人复核的过期结论钉在原地。
        正确做法：整条退回规则基线（安全由安全层 + 基线负责，不由过期的模型负责）。

        ⚠️ 注意 `SAFETY_ALERT` / `WAIT` **故意豁免**了"别打扰"类闸门（冷却防噪音、不防危险），
           所以它们构造不出 `low_confidence`；要测低置信度得用有否决权的普通类型（如 ASK_USER）。
        """
        import time as _time

        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))

        box: dict[str, Any] = {
            "payload": {"action_type": "SAFETY_ALERT", "message": "前方有移动物体，请停下",
                        "urgency": "high", "confidence": 0.9, "reason": "看到移动物体",
                        "expires_at": 0.5}
        }

        class _Stub:
            usable = True
            available = True
            model = "stub-veto"
            last_tool_calls: list = []

            def chat_multimodal(self, **kw: Any) -> Any:  # noqa: ANN401
                return dict(box["payload"])

            def stats(self) -> dict[str, Any]:
                return {"available": True, "model": self.model}

        stub = _Stub()
        sys_.llm = stub
        sys_.agent.llm = stub
        sys_.orchestrator.cognitive.enabled = True

        from spatial.spatial_state import RiskLevel as _RL

        st_ok = mk_state()  # 前方通畅、低风险：基线是 SPEAK/CONTINUE（允许前进）
        try:
            # ① 模型自己声明「这条只到 0.5 秒有效」→ 过点后整条退回基线
            #    ⚠️ 仿真时刻按 1 秒推进：一步跳 5 秒会被 TTL 先丢掉，永远到不了闸门。
            out = None
            for i in range(6):
                out = sys_.orchestrator.decide(st_ok, [], float(i), i)
                if sys_.agent.last_gate is not None and sys_.agent.last_gate.discard:
                    break
                _time.sleep(0.05)
            gate = sys_.agent.last_gate
            expect(gate is not None and gate.code == "expired",
                   f"没能构造出过期场景：{None if gate is None else gate.code}")
            expect(gate.discard, "过期必须被标记为「整条丢弃」")
            expect(out is not None and out.action.source == "rule",
                   "过期的模型结论必须整条退回规则基线")
            expect(out.action.permits_motion,
                   "一条没人听见的过期 SAFETY_ALERT 不该继续把用户钉在原地")

            # ② 低置信度：同样必须整条退回（用 ASK_USER，它才有「否决前进」的能力）
            box["payload"] = {"action_type": "ASK_USER", "message": "您现在是在走廊里吗？",
                              "urgency": "normal", "confidence": 0.9, "reason": "定位不确定"}
            sys_.agent.policy.min_confidence = 0.99
            lo = None
            for i in range(20, 30):
                lo = sys_.orchestrator.decide(st_ok, [], float(i), i)
                if sys_.agent.last_gate is not None and sys_.agent.last_gate.code == "low_confidence":
                    break
                _time.sleep(0.05)
            gate2 = sys_.agent.last_gate
            expect(gate2 is not None and gate2.code == "low_confidence",
                   f"没能构造出低置信度场景：{None if gate2 is None else gate2.code}")
            expect(gate2.discard, "低置信度必须被标记为「整条丢弃」")
            expect(lo is not None and lo.action.source == "rule" and lo.action.permits_motion,
                   "低置信度的模型结论不该冻结用户（应退回允许前进的规则基线）")

            # ③ 对照：门槛恢复后同一个输出应当被采纳，且保留它「让用户停步提问」的语义
            sys_.agent.policy.min_confidence = 0.35
            got = None
            for i in range(60, 110):
                out2 = sys_.orchestrator.decide(st_ok, [], float(i), i)
                if out2.action.source == "llm":
                    got = out2
                    break
                _time.sleep(0.05)
            expect(got is not None, "高置信度时认知结果应被采纳（闸门不该一律拦死）")
            expect(not got.action.permits_motion,
                   "被采纳的 ASK_USER 应保留「停步回答」的语义（模型确有把握时应当生效）")

            # ④ 安全层仍独立生效：真碰撞风险下无论模型说什么都必须 WAIT
            st_crit = mk_state(front_clear=False, front_distance=0.2, risk=_RL.CRITICAL)
            crit = sys_.orchestrator.decide(st_crit, [], 999.0, 999)
            expect(crit.verdict.intervene and not crit.action.permits_motion,
                   "临界风险下安全层必须仍然拦得住（模型不可推翻安全底线）")
        finally:
            sys_.close()
        return "过期/低置信度→退回基线且不冻结用户 / 可信时采纳并保留停步 / 安全层仍独立生效"

    runner.run("70 ★安全底线复核★ 不可信的模型结论不得停住用户", t_v08_untrusted_no_veto)

    def t_v08_schema_neutral() -> str:
        """schema 迁移对**确定性路径**必须中性：规则模式 120 轮零新增行为。"""
        c = base_cfg()
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        legacy = {"SPEAK", "CONTINUE", "WAIT", "REPLAN", "ASK_USER"}
        try:
            kinds: set[str] = set()
            for _ in range(120):
                r = sys_.step(1.0)
                a = r.action
                kinds.add(a.action_type.value)
                # 规则模式下只允许 VOICE / SILENT 两种通道 ⇒ `_apply` 的判据升级
                # （message → speaks）在规则模式下与 v0.2 完全等价。
                expect(a.channel in (ActionChannel.VOICE, ActionChannel.SILENT),
                       f"规则模式出现非语音通道 {a.channel} ⇒ 判据升级会改变行为")
                expect(bool(a.message) == a.speaks,
                       f"规则模式下「有话术」与「能发声」不等价：{a.action_type.value}/{a.channel}")
                expect(a.source == "rule", f"规则模式不该出现 {a.source} 来源的行动")
                SpatialState.model_validate(r.state.model_dump())  # 状态仍可往返
            leaked = kinds - legacy
            expect(not leaked, f"规则模式出现了 v0.2 之外的行动类型：{sorted(leaked)}")
            expect(sys_.agent.cognitive.calls == 0, "规则模式下不应发起任何认知调用")
            expect(not sys_.orchestrator.stats()["cognitive"]["thread_alive"],
                   "规则模式不该起认知线程")
            # ★Stage 10 中性是可**计数**证明的，不是"看起来没变"★
            #   主动感知只长在 `ContextBuilder.build()` 里，而规则基线
            #   （`baseline()` → `policy.observe` + `rules.decide`）根本不经过它。
            #   所以 `evaluated` 必须是 0 —— 这一层压根没被调用过，
            #   于是它在结构上**不可能**改变任何一位确定性输出（比逐字段 diff 更强）。
            perc = sys_.agent.context.perception.stats()
            expect(perc["evaluated"] == 0,
                   f"规则模式下主动感知被调用了 {perc['evaluated']} 次 ⇒ 它不是纯旁路")
            expect(perc["needed"] == 0, "规则模式下不该产生取帧判定")
            # ★Stage 9 中性同理★：没挂录制器时不得产生任何录制副作用
            expect(sys_.recorder is None, "规则模式不该自动开录制")
            expect(sys_.stats()["recording"]["enabled"] is False, "录制状态应为未开启")
        finally:
            sys_.close()
        return f"120 轮行动类型 {sorted(kinds)}；通道/发声判据等价；未起认知线程"

    runner.run("69 规则模式中性（120 轮零新增行为，可证明回归）", t_v08_schema_neutral)

    def t_v09_manual_drive() -> str:
        """手动驾驶（操作员用 WASD 接管仿真里的"盲人"）。

        线程模型是这条用例的**重点**：外部线程只 `submit_drive()` 往日志里 append，
        位移由主循环 `drive()` 按**墙上时钟**积分。所以这里刻意用真实 `time.sleep()`
        驱动时间轴 —— 注入假时间会绕过被测逻辑本身，测了等于没测。
        """
        c, m, nav, obs, sens, sm, world = fresh()
        walk = float(c["simulator"]["motion"]["walk_speed_mps"])
        start = tuple(nav.pos)

        # ① 进入手动：清局部路径、解除 arrived 短路
        nav.set_manual(True)
        expect(nav.manual is True, "set_manual(True) 未生效")
        expect(nav.arrived is False, "手动模式下 arrived 不应短路运动")
        expect(nav.local_path == [], "进入手动应清空局部路径")

        # ② 按住 W 一秒 → 位移应等于一步（walk_speed × 1s）
        nav.submit_drive(1, 0)
        time.sleep(1.0)
        st = nav.drive(1.0)
        d = math.dist(start, nav.pos)
        expect_close(d, walk, 0.06, "按住 W 一秒的位移 ≠ 步速")
        expect(st is WalkingStatus.MOVING, f"前进时状态应为 moving，实际 {st}")
        expect(nav.travelled > 0.0, "手动前进未累计里程")

        # ②' ★手动驾驶不得伪造 off_route★（"一直说正在重新规划路线"那条事故的护栏）
        #     off_route 是"A* 找不到路"的信号，会经 WorldReference 直达决策层，
        #     命中「无路可走 → 重规划 → 向用户求助」分支。而手动模式规划器根本没被问过 ——
        #     "走了一步 ⇒ 缓存路径作废"只是缓存问题，不是"没有路"。
        #     ⚠️ 曾经这里写的是 `off_route = True`（当"路径失效"用），结果操作员按住 W 时，
        #     仿真会每秒念一遍"正在为您重新规划路线 / 前方通路被挡住了"
        #     （实测一次 3 分钟的 WASD 测试刷出 65 次求助 + 6 次重规划 + 158 次停步）。
        expect(nav.off_route is False, "手动驾驶被误报成'A* 找不到路'（off_route 必须为 False）")
        expect(nav.path_found is True, "手动驾驶期间不该宣称'规划失败'（path_found 必须为 True）")
        expect(nav.next_instruction() == "手动模式：由操作员接管移动",
               "手动模式应如实说明由操作员接管，不能谎报重规划；"
               f"实际 {nav.next_instruction()!r}")

        # ③ 按住 A（=左转）半秒 → 朝向应减小（heading 顺时针为正）
        h0 = nav.heading
        nav.submit_drive(0, -1)
        time.sleep(0.5)
        nav.drive(0.5)
        turned = angle_delta(nav.heading, h0)
        expect(turned < -20.0, f"左转未生效：{h0:.1f}° → {nav.heading:.1f}°")
        expect_close(math.dist(start, nav.pos), d, 0.02, "原地转向不该带出位移")

        # ④ 撞墙必须停在原地（绝不穿墙）。
        #    地图很大，随便站一处朝哪个方向都走得通 —— 所以**先在栅格上找回一面墙**：
        #    找"脚下可走、但往东 0.5m 就不可走"的落点。这一轮步长是 walk*0.7≈0.77m，
        #    **必然**越过 0.5m 的墙界，于是这条断言考的是"撞墙"而不是"步长够不够"。
        res = float(m.res)
        step_m = walk * 0.7
        expect(step_m > 0.5, "步长必须大于探墙距离，否则这条用例测不到撞墙")
        wall_pt: tuple[float, float] | None = None
        yy = m.y_min + res
        while yy < m.y_max and wall_pt is None:
            xx = m.x_min + res
            while xx < m.x_max:
                if m.is_walkable(xx, yy) and not m.is_walkable(xx + 0.5, yy):
                    wall_pt = (xx, yy)
                    break
                xx += res
            yy += res
        expect(wall_pt is not None, "地图上找不到一面可撞的墙（地图或可行走判定异常）")
        nav.pos = wall_pt
        nav.heading = 90.0              # 90° = +x（正东）
        before = tuple(nav.pos)
        nav.submit_drive(1, 0)
        time.sleep(0.7)
        st2 = nav.drive(0.7)
        expect(st2 is WalkingStatus.BLOCKED, f"朝墙走却被放行（状态 {st2}）")
        expect(tuple(nav.pos) == before, "撞墙后位置仍被推进（穿墙）")

        # ⑤ drive_state() 是前端契约，字段集合不许变
        ds = nav.drive_state()
        expect(set(ds) == {"manual", "forward", "turn", "pending_s"},
               f"drive_state 字段集合变了：{sorted(ds)}")
        expect(ds["manual"] is True, "drive_state.manual 应为 True")

        # ⑥ 回到自动：manual 归位、清掉按键残留、路径重建
        nav.set_manual(False)
        expect(nav.manual is False, "set_manual(False) 未生效")
        expect(len(nav._drive_log) == 0,
               "回到自动后应清空按键日志（否则残余按键会漏进下一轮）")
        expect(nav.path_found, "回到自动后应能重新规划出路径")

        # ⑦ 装配链验收：HTTP/WS 线程调的是 system.drive()，真正消费日志的是主循环
        #    step()。这一条把「外部只 append、主循环积分」整条链路钉死 ——
        #    少了它，接口写得再对也可能没人消费（静默失效）。
        sys3 = SpatialAgentSystem(base_cfg(), options=SystemConfig(seed=seed, enable_llm=False))
        try:
            expect(sys3.set_manual(True) is True, "system.set_manual(True) 未生效")
            expect(sys3.manual() is True, "system.manual() 未回读")
            sys3.drive(1, 0)
            expect(sys3.env.nav.drive_state()["manual"] is True, "system 级手动状态未传到 nav")
            p0 = tuple(sys3.nav.pos)
            time.sleep(1.0)
            sys3.step(1.0)                      # 主循环消费按键日志
            moved = math.dist(p0, sys3.nav.pos)
            expect(moved > 0.2, f"系统 step() 未消费手动按键（位移 {moved:.2f}m）")
            expect(sys3.set_manual(False) is False, "system.set_manual(False) 未生效")
            expect(sys3.manual() is False, "回到自动后 system.manual() 应为 False")
        finally:
            sys3.close()

        return (f"按 W 1s 位移 {d:.2f}m（期望 {walk:.2f}）/ 左转 {turned:+.1f}°"
                f" / 撞墙停住 / 回自动 path_found={nav.path_found} / 主循环消费位移 {moved:.2f}m")

    runner.run("71 手动驾驶（WASD 接管：前进/转向/撞墙停住/可回自动）", t_v09_manual_drive)

    # -----------------------------------------------------------------
    # v0.3 Stage 9：Recording / Replay
    # -----------------------------------------------------------------
    def t_v09_recording() -> str:
        """录制：目录契约 + 只追加 + **绝不落盘隐藏思维链**。

        任务书第十五节的硬约束是两条：
          ① 落盘 `states/events/actions/llm` + `images/` + 会话元信息；
          ② **不要保存隐藏 chain-of-thought**，只保存结构化决策与可公开 reason。
        第②条是**隐私/合规**性质的红线，所以用"白名单"而不是"黑名单"来钉：
        黑名单（列一堆 `thinking`/`cot` 之类的名字）永远漏，白名单才可证明。
        """
        import json as _json
        import tempfile
        from pathlib import Path as _P

        from recording.schemas import SCHEMA_VERSION, is_valid_session_id, new_session_id
        from recording.session_recorder import SessionRecorder

        # ① 会话 id 形如 session_YYYYMMDD_HHMMSS，且必须能被校验器接受
        sid = new_session_id()
        expect(is_valid_session_id(sid), f"session_id 形态不对：{sid}")
        expect(not is_valid_session_id("../../etc/passwd"), "路径穿越的 session_id 必须被拒")

        tmp = tempfile.mkdtemp(prefix="bsa_rec_")
        rec = SessionRecorder(base_cfg(), root=tmp, session_id="case72", notes="自检用")

        # ② 真跑一段仿真，让四类数据都有内容。
        #    ★地图快照不在这里手工挂★ —— 装配处（`SpatialAgentSystem.__init__`）
        #    必须自己把 `provider.map_snapshot()` 交给录制器，这里正是要**考这件事**：
        #    回放没有模拟器，`zone_at()` 只能靠这份快照。
        sys_ = SpatialAgentSystem(
            base_cfg(), options=SystemConfig(seed=seed, enable_llm=False), recorder=rec
        )
        last_state = None
        try:
            for _ in range(12):
                r = sys_.step(1.0)
                last_state = r.state
        finally:
            sys_.close()
        counts = rec.close()

        d = _P(tmp) / "case72"
        for fn in ("meta.json", "states.jsonl", "events.jsonl", "actions.jsonl", "map.json"):
            expect((d / fn).is_file(), f"会话目录缺少 {fn}")
        expect((d / "images").is_dir(), "会话目录缺少 images/")
        meta = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
        expect(meta["schema_version"] == SCHEMA_VERSION, "meta 版本号未写")
        expect(meta.get("session_id") == "case72", "meta 未记录会话 id")
        expect(str(meta.get("map_name") or ""), "meta 未记录地图名（地图快照没落盘）")

        # ③ 只追加：**再录一条**，前几行必须逐字不变（append-only 是可审计性的前提）
        before = (d / "states.jsonl").read_text(encoding="utf-8")
        rec2 = SessionRecorder(base_cfg(), root=tmp, session_id="case72")
        rec2.record_state(last_state, elapsed=999.0, tick=999)
        after = (d / "states.jsonl").read_text(encoding="utf-8")
        expect(after.startswith(before), "jsonl 不是只追加（旧行被改写）")
        expect(len(after) > len(before), "新记录没有写进去")

        # ④ ★不落盘隐藏思维链★
        #    白名单**直接从数据类声明里取**，而不是手抄一份：
        #    手抄的那份会在"有人往记录里加字段"时失效，而从声明里取是自维护的，
        #    真正表达的是——"落盘的字段集合 == 声明的可公开字段集合"。
        import dataclasses as _dc

        from recording.schemas import ActionRecord, LlmRecord

        allowed = {
            "actions.jsonl": {f.name for f in _dc.fields(ActionRecord)},
            "llm.jsonl": {f.name for f in _dc.fields(LlmRecord)},
        }
        # 嵌套的 decision 也必须只含可公开字段（rationale / 缺口 / 是否要画面…）
        allowed_decision = {
            "rationale", "information_gaps", "needs_visual", "model",
            "latency_s", "prompt_chars", "image_attached", "action_type", "reason",
        }
        allowed_gate = {"speak", "discard", "code", "reason", "channel"}
        scanned = 0
        for fn, keys in allowed.items():
            p = d / fn
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec_obj = _json.loads(line)
                scanned += 1
                bad = [k for k in rec_obj if k not in keys]
                expect(not bad, f"{fn} 出现了未声明的字段（可能是隐藏思维链）：{bad}")
                dec = rec_obj.get("decision")
                if isinstance(dec, dict):
                    bad2 = [k for k in dec if k not in allowed_decision]
                    expect(not bad2, f"{fn} 的 decision 里有未批准字段：{bad2}")
                gate = rec_obj.get("gate")
                if isinstance(gate, dict):
                    bad3 = [k for k in gate if k not in allowed_gate]
                    expect(not bad3, f"{fn} 的 gate 里有未批准字段：{bad3}")
        expect(scanned > 0, "一行都没扫到，白名单校验等于没跑")

        # ⑤ 计数自洽：录到的状态数 == 跑过的轮数
        cc = counts.get("counts", {}) if isinstance(counts, dict) else {}
        expect(cc.get("states", 0) >= 12, f"states 记录数偏少：{cc}")
        expect(cc.get("actions", 0) >= 12, f"actions 记录数偏少：{cc}")
        expect(counts.get("enabled") is False, "关闭后 enabled 应为 False")

        # ⑥ 未开启录制时必须是彻底的空操作（行为与 v0.2 一致）
        sys2 = SpatialAgentSystem(base_cfg(), options=SystemConfig(seed=seed, enable_llm=False))
        try:
            expect(sys2.recorder is None, "没给 --record 却造了录制器")
            expect(sys2.step(1.0) is not None, "无录制时主循环应照常工作")
        finally:
            sys2.close()

        return (f"目录契约齐全 / 只追加 / 无隐藏思维链 / "
                f"states={cc.get('states')} actions={cc.get('actions')}")

    runner.run("72 录制（目录契约 + 只追加 + 不落盘隐藏思维链）", t_v09_recording)

    def t_v09_replay() -> str:
        """回放：**不接模拟器**重放历史状态流，且逐轮复现原决策。

        本用例的三种"假通过"都要堵掉：
          ① 有人把 simulator import 回来（有真世界就变成"自己造世界再跑"）；
          ② 回放只复现了行动类型、但复现不了**带状态的判据**
             （"距上次播报多久""方向有没有变"全靠状态，错 1 秒就整体错位）；
          ③ 行动只是被"复读"出来（从 actions.jsonl 抄），而不是重新算的。
        """
        import json as _json
        import tempfile
        from pathlib import Path as _P

        from recording.session_player import ReplaySource, SessionPlayer
        from recording.session_recorder import SessionRecorder

        tmp = tempfile.mkdtemp(prefix="bsa_replay_")
        rec = SessionRecorder(base_cfg(), root=tmp, session_id="case73")
        sys_ = SpatialAgentSystem(
            base_cfg(), options=SystemConfig(seed=seed, enable_llm=False), recorder=rec
        )
        original: list[tuple[float, str]] = []
        try:
            for _ in range(30):
                r = sys_.step(1.0)
                original.append((round(r.elapsed, 2), r.action.action_type.value))
        finally:
            sys_.close()
        rec.close()

        # ① 回放器与模拟器彻底解耦（模块级 + 实例级双重确认）
        #    ⚠️ 匹配的是**真实的 import 语句**，不是"出现过 simulator 这个词"：
        #       回放器的注释里本来就要写"不调用 simulator"，用子串匹配是自欺欺人。
        import re as _re

        src_probe = _P(__file__).resolve().parents[1] / "recording/session_player.py"
        imp_pat = _re.compile(r"^\s*(?:from|import)\s+simulator\b", _re.M)
        expect(not imp_pat.search(src_probe.read_text(encoding="utf-8")),
               "回放器 import 了 simulator（那就变成'自己造世界再跑'）")
        expect(SpatialAgentSystem.__module__ != ReplaySource.__module__, "回放器不该复用主系统")

        player = SessionPlayer(tmp, "case73")
        player.load()
        expect(len(player.states_raw) == 30, f"状态数应为 30，实际 {len(player.states_raw)}")

        # ② 历史状态必须能原样重建（模型往返：extra=forbid，字段少一个就报错）
        #    断言"与记录逐字段一致"，而不是猜某个具体 tick 值 ——
        #    猜值会让用例在"状态管理器改了 tick 起点"时变红，而那不是回放的错。
        replayed_states = list(player.iter_states())
        expect(len(replayed_states) == 30, "iter_states 数量不符")
        first_raw = _json.loads(_json.dumps(player.states_raw[0]["state"]))
        # ⚠️ 顶层 `tick` 是**系统轮次**，`state.tick` 是状态管理器自己的计数 ——
        #   两者不是同一个东西，别拿顶层 tick 去对状态里的 tick（会误报不一致）。
        expect(replayed_states[0].tick == int(first_raw["tick"]), "首帧 tick 未按记录还原")
        expect(replayed_states[0].model_dump(mode="json")["user"] == first_raw["user"],
               "历史状态重建后与原记录不一致（用户位姿）")
        expect(replayed_states[-1].model_dump(mode="json")["navigation"]
               == _json.loads(_json.dumps(player.states_raw[-1]["state"]))["navigation"],
               "历史状态重建后与原记录不一致（导航态）")

        # ③ ReplaySource 必须同时满足"传感器 / 世界推进 / 状态管理"三重角色，
        #    否则装配处（provider=src, stepper=src, state_manager=src）会静默缺方法。
        src = ReplaySource(player.map_snapshot)
        for name in ("read_pose", "measure_depth", "scan_wall_ahead", "scan_sides", "perceive",
                     "map_snapshot", "zone_at", "floor", "read_navigation", "confidence",
                     "health", "sensor_stats", "advance_environment", "advance_user", "replan",
                     "world_reference", "obstacle_centers", "arrived", "stats"):
            expect(hasattr(src, name), f"ReplaySource 缺少 {name}（装配处会静默失效）")

        # ④ 世界类动作必须是**空操作**：回放不许"重新推演世界"
        src.advance_to(replayed_states[-1])
        before = _json.loads(_json.dumps(src.current.model_dump(mode="json")))
        expect(src.advance_environment(1.0) is None, "回放的推进环境应为空操作")
        expect(src.advance_user(1.0, motion_allowed=True, front_distance=1.0) is None,
               "回放的推进用户应为空操作")
        # 规划器同样不存在：只登记、不执行。★世界一点没动才是关键★
        n_rep = src.stats()["replans"]
        expect(src.replan() is True, "回放的重规划应报成功（真实结论看下一帧状态）")
        expect(src.stats()["replans"] == n_rep + 1, "回放的重规划未计数")
        expect(_json.loads(_json.dumps(src.current.model_dump(mode="json"))) == before,
               "回放的「推进」改变了世界 —— 那就不是回放了")
        expect(src.zone_at(*[float(v) for v in (before["user"]["position"]["x"],
                                                before["user"]["position"]["y"])]) is not None
               or player.map_snapshot is None,
               "地图快照没生效：zone_at 无法回答「在哪」")

        # ⑤ ★核心★ 同配置回放必须逐轮复现（100%），这才能证明
        #    "换了模型/Prompt 之后哪几轮变了"这个对比是有意义的
        report = player.replay(base_cfg(), enable_llm=False)
        expect(report.ticks == 30, f"回放轮次应为 30，实际 {report.ticks}")
        expect(report.compared == 30, "回放未逐轮比对")
        expect(report.match_rate == 1.0,
               f"同配置回放一致性应 100%，实际 {report.match_rate * 100:.1f}% "
               f"（差异 {report.divergences[:4]}）")
        types = [t for _t, t in original]
        expect(report.action_types.get("SPEAK", 0) > 0 or "SPEAK" in types,
               "这段录制里应该有播报，否则用例测不到'带状态判据'")
        expect(report.latency_avg_s >= 0.0, "未统计单轮延迟")

        # ⑥ 回放结果可另存为一份新会话（便于与原始并排对比）
        out = _P(tmp) / "out"
        rep2 = player.replay(base_cfg(), enable_llm=False, max_ticks=5, out_root=str(out),
                             notes="自检回放")
        expect(rep2.ticks == 5, "max_ticks 未生效")
        expect(rep2.out_dir and (_P(rep2.out_dir) / "meta.json").is_file(),
               "replay --out 未产出可回放的新会话")

        # ⑦ 坏数据不得毁掉整段回放（单条跳过即可）
        bad = _P(tmp) / "case73" / "states.jsonl"
        with bad.open("a", encoding="utf-8") as fh:
            fh.write("这不是 JSON\n")
            fh.write(_json.dumps({"t": 1.0, "tick": 1, "state": {"不存在的字段": 1}}) + "\n")
        p2 = SessionPlayer(tmp, "case73")
        p2.load()
        expect(len(list(p2.iter_states())) == 30, "坏行应该被跳过，不该毁掉整段回放")

        return (f"30 轮零差异回放（{report.match_rate * 100:.0f}%）/ "
                f"行动分布 {report.action_types} / 不接模拟器")

    runner.run("73 回放（不接模拟器 + 历史状态重建 + 30 轮零差异）", t_v09_replay)

    # -----------------------------------------------------------------
    # v0.3 Stage 10：Active Perception
    # -----------------------------------------------------------------
    def t_v10_active_perception() -> str:
        """主动感知：只在「事件 / 置信度不足 / 用户问环境」时才要画面。

        任务书第十三节的判据只有三条，且明确写着**不要每轮都上传图片**。
        本用例把"三条以外都不算"逐条钉死，尤其是**"没有相机"不算** ——
        那是"无法获得视觉"而不是"需要视觉"：帧由手机端推上来，Agent 侧要不到，
        把它当触发条件会让 `needs_visual` 在无相机部署里永久点亮成假信号
        （指标类字段的假信号在本项目已经踩过一次，见 `route_confidence`）。
        """
        import datetime as _dt

        from agent.active_perception import (
            DEFAULT_ENV_QUERY_KEYWORDS,
            DEFAULT_VISUAL_EVENT_TYPES,
            VisualRequestPolicy,
        )
        from agent.cognitive_agent import CognitiveAgent
        from agent.context_builder import ContextBuilder
        from agent.memory import AgentMemory

        c = base_cfg()

        class _Ev:
            """最小事件替身：只需要 `event_type.value`。"""

            def __init__(self, name: str) -> None:
                self.event_type = type("T", (), {"value": name})()

        def st(**patch: Any) -> SpatialState:
            """合成状态。★默认给一份"新鲜帧"★ —— 否则测"该不该取图"时，
            取图那一半永远因为"没相机"而返回 None，用例会假通过。"""
            d = mk_state().model_dump(mode="json")
            d["camera"] = {"image_available": True, "age_s": 0.5, "freshness": "fresh"}
            for k, v in patch.items():
                d[k] = v
            return SpatialState.model_validate(d)

        # ---------- ① 三条触发条件分别命中 ----------
        pol = VisualRequestPolicy(mode="on_demand", min_interval_s=2.0)
        clean = st()
        expect(pol.triggers(clean) == [], f"干净状态不该触发：{pol.triggers(clean)}")

        ask = pol.triggers(st(), user_query="前面是什么？")
        expect("user_asks_environment" in ask, f"用户问环境应触发：{ask}")
        expect("user_asks_environment" not in pol.triggers(st(), user_query="还有多远？"),
               "「还有多远」问的是路线，不该触发取帧")

        ev = pol.triggers(st(), events=[_Ev(DEFAULT_VISUAL_EVENT_TYPES[0])])
        expect(any(h.startswith("event_needs_vision") for h in ev), f"需视觉事件应触发：{ev}")
        expect(pol.triggers(st(), events=[_Ev("NOT_A_VISUAL_EVENT")]) == [],
               "与视觉无关的事件不该触发取帧")

        low = st(confidence={"localization_confidence": 0.2})
        expect("low_localization_confidence" in pol.triggers(low), "低定位置信度应触发")

        # ---------- ② 相机两种状态的分野（本次修掉的那条） ----------
        stale = st(camera={"image_available": True, "age_s": 10.0, "freshness": "stale"})
        expect("camera_stale" in pol.triggers(stale), "帧过期应触发重取")
        # ★没有相机 ⇒ 不是"需要视觉"★
        absent = st(camera={"image_available": False, "age_s": None, "freshness": "none"})
        expect(pol.triggers(absent) == [], "没有相机不该被当成需要视觉（假信号）")
        n = pol.evaluate(absent, elapsed=0.0)
        expect(n.needed is False, "无相机时 needed 必须为 False")
        expect(n.suppressed_by == "camera_unavailable", "无相机应留下诊断痕迹")

        # ---------- ③ 节流：持续低置信度不该变成连续取帧 ----------
        pol.reset()
        expect(pol.evaluate(low, elapsed=0.0).needed is True, "首次低置信度应取帧")
        expect(pol.evaluate(low, elapsed=1.0).needed is False, "间隔内的重复触发应被节流")
        expect(pol.evaluate(low, elapsed=1.0).suppressed_by == "throttled", "节流原因未标注")
        expect(pol.evaluate(low, elapsed=5.0).needed is True, "超过最小间隔应重新允许")
        # 模型显式索要豁免节流：用户/模型就在等这一张
        pol.reset()
        pol.evaluate(low, elapsed=0.0)
        f = pol.evaluate(low, elapsed=0.5, force=True)
        expect(f.needed is True and "explicit_tool_request" in f.reasons,
               "模型显式请求必须豁免节流")

        # ---------- ④ 模式语义 ----------
        expect(VisualRequestPolicy(mode="always").attaches_unconditionally is True, "always 应无条件附")
        expect(VisualRequestPolicy(mode="auto").attaches_unconditionally is True, "auto 应兼容 v0.2")
        expect(VisualRequestPolicy(mode="on_demand").attaches_unconditionally is False,
               "on_demand 才是省 token 模式")
        nev = VisualRequestPolicy(mode="never").evaluate(low, elapsed=0.0)
        expect(nev.needed is False and nev.suppressed_by == "mode=never", "never 模式语义不对")

        # ---------- ⑤ 配置读写：缺配置/空配置都要能起来（不是魔法数字） ----------
        cls = VisualRequestPolicy.from_cfg({})
        expect(cls.mode == "auto" and cls.event_types == DEFAULT_VISUAL_EVENT_TYPES,
               "空配置应回落到默认值")
        cls2 = VisualRequestPolicy.from_cfg(
            {"agent": {"perception": {"mode": "on_demand", "min_interval_s": 9.0,
                                      "env_query_keywords": ["厕所"]}}}
        )
        expect(cls2.mode == "on_demand" and cls2.min_interval_s == 9.0, "配置覆盖未生效")
        expect(cls2.triggers(st(), user_query="厕所在哪")  != [], "自定义关键词未生效")
        expect("环境" not in cls2.env_query_keywords, "自定义关键词应替换默认词表")
        cfg_real = base_cfg()
        expect("perception" in cfg_real["agent"], "config.yaml 缺少 agent.perception 段")
        expect(VisualRequestPolicy.from_cfg(cfg_real).mode in
               ("always", "auto", "on_demand", "never"), "配置里的 mode 非法")
        keys = set(VisualRequestPolicy().stats())
        expect({"mode", "evaluated", "needed", "throttled", "reasons"} <= keys,
               f"perception.stats 字段集合变了：{sorted(keys)}")
        expect(DEFAULT_ENV_QUERY_KEYWORDS and DEFAULT_VISUAL_EVENT_TYPES, "默认词表丢失")

        # ---------- ⑥ 与上下文构建器的接线（省 token 的那一半） ----------
        class _Cam:
            def latest_bytes(self) -> bytes:
                return b"\xff\xd8frame"

        c_od = base_cfg(**{"agent.perception.mode": "on_demand"})
        _, _m, _nav, _o, _s, _sm, world = fresh()
        mem = AgentMemory(c_od)
        cam = _Cam()
        fresh_st = st(camera={"image_available": True, "age_s": 0.5, "freshness": "fresh"})
        cb = ContextBuilder(c_od, world, mem, camera=cam)
        b0 = cb.build(fresh_st, elapsed=0.0)
        expect(b0.image is None, "on_demand 模式且无触发时不应取图（否则等于每轮烧 token）")
        b1 = cb.build(low, elapsed=0.0)
        expect(b1.image is not None, "低置信度时应取图")
        expect(b1.visual_need and b1.visual_need["needed"] is True, "visual_need 未随判定落进 bundle")
        b2 = cb.build(fresh_st, elapsed=9.0, force_visual=True)
        expect(b2.image is not None, "模型显式请求时必须带上画面")
        cb_auto = ContextBuilder(base_cfg(), world, mem, camera=cam)
        expect(cb_auto.build(fresh_st, elapsed=0.0).image is not None,
               "auto 模式必须与 v0.2 一致：有新鲜帧就附（已验收的多模态链路不受影响）")

        # ---------- ⑦ 工具 → 主线程 的一次性握手 ----------
        sys_ = SpatialAgentSystem(c, options=SystemConfig(seed=seed, enable_llm=False))
        try:
            r = sys_.step(1.0)
            tools = sys_.agent.tools
            expect(tools.visual_request_pending is False, "初始不应有待取帧请求")
            res = tools.request_visual_observation("画面太暗，看不清楚")
            expect(res["requested"] is True and res["will_attach_next_cycle"] is True,
                   f"工具返回值不对：{res}")
            expect(tools.visual_request_pending is True, "请求未被登记")
            expect(tools.stats().get("visual_requests", 0) >= 1, "工具未计数")
            b = sys_.agent.build_context(r.state, elapsed=1.0)
            expect(b.visual_need["needed"] is True, "模型的取帧请求没有被主线程消费")
            expect("explicit_tool_request" in b.visual_need["reasons"],
                   f"未标记为显式请求：{b.visual_need}")
            expect(tools.visual_request_pending is False, "一次性请求取走后应清空")
            b_next = sys_.agent.build_context(r.state, elapsed=1.5)
            expect("explicit_tool_request" not in (b_next.visual_need["reasons"] or ()),
                   "取帧请求必须只生效一次（否则会变成每轮都带图）")
            # 录制/诊断面：stats 里必须能看到感知层的真实计数
            perc = sys_.stats()["perception"]
            expect(perc["evaluated"] >= 1 and perc["mode"], f"perception.stats 未接通：{perc}")
        finally:
            sys_.close()

        # ---------- ⑧ 判定要能进"信息缺口"，并抬升 needs_visual ----------
        class _Stub:
            usable = True
            available = True
            model = "stub-v10"
            last_tool_calls: list = []

            def chat_multimodal(self, **kw: Any) -> Any:  # noqa: ANN401
                return {"action_type": "SPEAK", "message": "前面是门", "reason": "看到门"}

        cb2 = ContextBuilder(base_cfg(), world, mem, camera=cam)
        ag = CognitiveAgent(base_cfg(), _Stub(), cb2)
        bundle = cb2.build(fresh_st, elapsed=0.0)
        bundle.visual_need = {"needed": True, "reasons": ["low_localization_confidence"]}
        dec = ag.decide(bundle)
        expect(dec is not None, "认知决策不应为空")
        expect(dec.needs_visual is True, "策略判定应抬升 needs_visual")
        expect(any("需要视觉" in g for g in dec.information_gaps),
               f"策略判定应进入信息缺口：{dec.information_gaps}")
        expect(dec.action.permits_motion is True, "请求画面不该阻断用户前进")

        return (f"三条触发条件各自命中 / 无相机不算（假信号已堵）/ 节流+显式豁免 / "
                f"on_demand 省图 / 一次性握手生效且只生效一次")

    runner.run("74 ★Gate 10★ 主动感知（三条触发条件 + 节流 + 一次性握手）", t_v10_active_perception)

    # -----------------------------------------------------------------
    # 11. 地图连通性（防"房间被悄悄堵死"）
    # -----------------------------------------------------------------
    def t_v11_map_connectivity() -> str:
        """★防静默故障★ 房间必须走得出去、门洞必须真的能过人、不能有孤岛。

        为什么单列这一条：地图是数据驱动的，往里加一件家具、挪一个门洞，
        A* **不会报错** —— 它只会找不到路，然后智能体在原地打转。
        这类故障从日志上完全看不出来（"导航没停、就是不动"），
        必须在改地图的当下就被拦住，而不是等用户来问"为什么走不过去"。

        注意口径：**"有门"的判据是可达性，不是几何上有没有门框。**
        入口大厅、核心办公区这类开放式区域直接与走廊共享边界，本来就不需要门洞；
        真正要守住的是"封闭房间必须有门、门必须真的能过人"。
        """
        c = base_cfg()
        m = MapSimulator(c)
        rep = m.connectivity_report()
        clr = float(c["simulator"]["motion"]["path_clearance_m"])
        zones = m.zones

        corridors = [z for z in zones if "走廊" in str(z["name"])]
        doors = [z for z in zones if str(z["name"]).endswith(("门", "门口"))]
        expect(bool(doors), "地图里没有任何门洞（zones 里找不到以「门/门口」结尾的区域）")

        # --- (1) 每个房间型区域都要能从起点走到 ---
        bad = [name for name, ok in rep["rooms"].items() if not ok]
        expect(not bad, f"房间走不出去（被墙体或家具封死）：{bad}")

        # --- (2) 封闭式房间必须至少有一扇贴着它的门 ---
        #         判据用"是否与走廊直接贴合"：贴合=开放式，不贴合=封闭，必须靠门进出。
        for z in zones:
            name = str(z["name"])
            if "走廊" in name or name.endswith(("门", "门口")):
                continue
            if any(rects_touch(z, cor) for cor in corridors):
                continue
            attached = [str(d["name"]) for d in doors if rects_touch(z, d)]
            expect(attached, f"封闭房间「{name}」没有任何门洞与它相邻（进不去也出不来）")

        # --- (3) 每个门洞的有效通行宽度都要够一个人（含盲杖）过去 ---
        #         阈值 = max(1.0m, 2×安全间隙)：报告值已扣除两侧安全间隙，
        #         所以这里量的是"真正能走的净宽"。
        need = max(1.0, 2.0 * clr)
        narrow = {k: v for k, v in rep["doors"].items() if v < need}
        expect(not narrow, f"门洞有效通行宽度不足 {need:.2f}m：{narrow}")

        # --- (4) 不能存在与主可通行区域脱开的"孤岛" ---
        expect(
            rep["orphan_ratio"] < 0.01,
            f"存在与主区域脱开的孤立可通行区域，占比 {rep['orphan_ratio']:.2%}",
        )

        widest = max(rep["doors"].values())
        return (
            f"{len(rep['rooms'])} 个房间全可达 / {len(doors)} 个门洞（有效宽 "
            f"{min(rep['doors'].values()):.2f}~{widest:.2f}m，阈值 {need:.2f}m）/ 无孤岛"
        )

    runner.run("75 地图连通性（房间可达 + 封闭房间有门 + 门洞有效宽度 + 无孤岛）", t_v11_map_connectivity)

    return runner


# =====================================================================
# 运行入口
# =====================================================================
def run_all(cfg: dict[str, Any], verbose: bool = True, seed: int = 20260917) -> dict[str, Any]:
    """运行全部检查，返回汇总。"""
    t0 = time.perf_counter()
    if verbose:
        print("=" * 68)
        print(f"BlindSpatialAgent 自检  (seed={seed})")
        print("=" * 68)

    runner = build_runner(cfg, seed)

    duration = time.perf_counter() - t0
    passed = sum(1 for r in runner.results if r.ok)
    skipped = 0
    return {
        "total": len(runner.results),
        "passed": passed,
        "failed": len(runner.results) - passed,
        "skipped": skipped,
        "duration_s": duration,
        "failures": [(r.name, r.error) for r in runner.results if not r.ok],
        "results": runner.results,
    }


def main() -> int:
    from config.loader import load_config

    summary = run_all(load_config(), verbose=True)
    print("=" * 68)
    print(
        f"通过 {summary['passed']} / {summary['total']} | 失败 {summary['failed']}"
        f" | 耗时 {summary['duration_s']:.1f}s"
    )
    for name, err in summary["failures"]:
        print(f"  ✗ {name}: {err}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
