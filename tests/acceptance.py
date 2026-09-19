"""BlindSpatialAgent v0.3 验收场景（Stage 11 收尾）。

任务书里没有名为 "Stage 11" 的阶段 —— 它到 **Stage 10（Active Perception）+「运行完整 Demo」**
为止（第二十七节）。本模块是**收尾阶段**的落地：

    第二十八节  Gate 1~10     → 必须逐条满足
    第二十九节  最终验收 Demo → 七拍连续剧本

我们把这七拍与十个 Gate 落成 **10 个可判定场景**，每个场景给出 `PASS/FAIL` 与证据行。
全部**离线**运行：无网络、无 API Key、无真机 iPhone。
大模型侧一律用替身（stub），摄像头侧一律用可按需断线的替身。

    Gate 1  项目可以启动                      → S01
    Gate 2  旧 Demo 基本能力还存在            → S01 / S02 / S09
    Gate 3  iPhone 图片仍然可以接收           → S06 / S07（真实上行链路另见 tools/e2e_test.py）
    Gate 4  Simulator 可以运行                → S09
    Gate 5  LLM 可以关闭并用 Mock 测试        → 全部场景（enable_llm=False）+ S06 / S08（stub）
    Gate 6  LLM 失败不会让系统崩              → S08
    Gate 7  Safety Loop 独立于 LLM            → S08
    Gate 8  SpatialState 有 timestamp/source/confidence → S01
    Gate 9  Event 可以触发 Agent              → S03 / S05
    Gate 10 Replay 可以读取历史数据           → S10

用法：
    python main.py --acceptance
    python -m tests.acceptance
"""

from __future__ import annotations

import copy
import math
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from tests.selftest import Runner, expect

# =====================================================================
# Gate ↔ 场景 对照表（打印给验收人看）
# =====================================================================
GATE_MATRIX: tuple[tuple[str, str, str], ...] = (
    ("Gate 1", "项目可以启动", "S01"),
    ("Gate 2", "旧 Demo 基本能力还存在", "S01 / S02 / S09"),
    ("Gate 3", "iPhone 图片仍然可以接收", "S06 / S07（真机上行另见 tools/e2e_test.py）"),
    ("Gate 4", "Simulator 可以运行", "S09"),
    ("Gate 5", "LLM 可以关闭并用 Mock 测试", "全场景 enable_llm=False + S06 / S08"),
    ("Gate 6", "LLM 失败不会让系统崩", "S08"),
    ("Gate 7", "Safety Loop 独立于 LLM", "S08"),
    ("Gate 8", "SpatialState 有 timestamp/source/confidence", "S01"),
    ("Gate 9", "Event 可以触发 Agent", "S03 / S05"),
    ("Gate 10", "Replay 可以读取历史数据", "S10"),
)


# =====================================================================
# 替身（stub）
# =====================================================================
class StubCamera:
    """摄像头替身：协议同 `sensors.base.CameraProvider`，可按需"断线"。

    ★为什么需要它★ 任务书第二十九节要求"模拟 camera 断线，系统继续导航"。
      真机拔不掉，只能在协议层做。
    """

    def __init__(self, jpeg: bytes | None = None) -> None:
        self.jpeg = jpeg
        self.frame_id = 0
        self.connected = jpeg is not None
        self.age_s = 0.1

    def snapshot(self) -> dict[str, Any]:
        if not self.connected:
            # ⚠️ 断线时 frame_id 必须保留 > 0：融合层据此区分
            #    "收到过帧但断流"（stale）与"从没有摄像头"（none）。
            return {
                "image_available": False,
                "frame_id": self.frame_id,
                "source": "websocket",
                "age_s": self.age_s,
            }
        self.frame_id += 1
        return {
            "image_available": True,
            "frame_id": self.frame_id,
            "source": "websocket",
            "age_s": self.age_s,
        }

    def latest_bytes(self) -> bytes | None:
        """喂模型用：断线**必须返回 None**（过期图比没图更糟）。"""
        return self.jpeg if self.connected else None

    def preview_bytes(self) -> bytes | None:
        """给网页预览用：过期也允许返回（语义与 latest_bytes 相反）。"""
        return self.jpeg

    def disconnect(self) -> None:
        self.connected = False
        self.age_s = 30.0


class StubVLM:
    """多模态大模型替身：记录"这次有没有带图"，返回一段固定 JSON。"""

    usable = True
    available = True
    model = "stub-vlm"
    last_tool_calls: list[Any] = []

    def __init__(self, payload: dict[str, Any], delay: float = 0.05) -> None:
        self.payload = payload
        self.delay = delay
        self.calls = 0
        self.seen_images = 0
        self.image_bytes = 0

    def chat_multimodal(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls += 1
        img = kwargs.get("image")
        if img:
            self.seen_images += 1
            self.image_bytes = len(img) if hasattr(img, "__len__") else 0
        if self.delay:
            time.sleep(self.delay)
        return dict(self.payload)


class SlowVLM:
    """"卡死 5 秒"的大模型替身（任务书第八节的强制验收标准）。"""

    usable = True
    available = True
    model = "stub-slow"
    last_tool_calls: list[Any] = []

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.calls = 0

    def chat_multimodal(self, **kwargs: Any) -> dict[str, Any] | None:
        self.calls += 1
        time.sleep(self.delay)
        return None


def tiny_jpeg(width: int = 320, height: int = 240) -> bytes | None:
    """造一张真实可解码的 JPEG（多模态消息里的图片必须是真图）。"""
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        return None
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[::4, :, 1] = 200
    img[:, ::4, 2] = 180
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes() if ok else None


def place_obstacle_ahead(system: Any, ahead_m: float, kind: str = "chair") -> tuple[float, float]:
    """在用户**当前真实朝向**的正前方 `ahead_m` 处直接摆一个障碍，返回落点坐标。

    为什么不用 `obstacles.spawn()`：那个是随机的（落点、速度、生命期都抽签），
    验收剧本要求"椅子突然出现在正前方 X 米"必须**每跑必现**。

    ⚠️ 为什么读 `nav.pos` 而不是 `last_state().user.position`★
        `step()` 的顺序是：融合建 `state` → 决策 → 执行 → `advance_user()`。
        也就是说 `state` 描述的是**本 tick 移动之前**的位姿，比真实位姿晚一拍
        （1 m/s 下差 1.1 米）。用 state 去算"正前方 0.3 米"，摆出来其实在
        用户**身后 0.8 米** —— 症状是"障碍事件产生了，但前方距离/风险毫无变化"。
        验收剧本里位置是硬要求，所以必须读仿真器的真值。
    """
    from simulator.obstacle_simulator import DynamicObstacle

    ux, uy = system.nav.pos
    rad = math.radians(float(system.nav.heading))
    x = ux + math.sin(rad) * ahead_m
    y = uy + math.cos(rad) * ahead_m
    system.obstacles.obstacles.append(
        DynamicObstacle(
            oid=900_000 + int(ahead_m * 100),
            type=kind,
            x=x,
            y=y,
            radius=0.30,
            ttl=600.0,
            max_ttl=600.0,
            spawned_at=system.elapsed,
        )
    )
    return (x, y)


# =====================================================================
# 场景
# =====================================================================
def build_scenarios(cfg: dict[str, Any], seed: int) -> Runner:
    """构造全部验收场景（与 `tests/selftest.py` 共用同一套极简 Runner）。"""
    from agent.agent_core import SpatialAgentSystem, SystemConfig
    from events.event_types import EventType

    runner = Runner()

    # -----------------------------------------------------------------
    def _cfg(**overrides: Any) -> dict[str, Any]:
        """干净配置副本；默认清空 api_key（验收必须离线、可复现）。"""
        c = copy.deepcopy(cfg)
        c["llm"]["api_key"] = ""
        for path, value in overrides.items():
            node = c
            keys = path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return c

    def _system(c: dict[str, Any], **kwargs: Any) -> Any:
        return SpatialAgentSystem(
            c, options=SystemConfig(seed=seed, enable_llm=False), **kwargs
        )

    def _place_obstacle(system: Any, ahead_m: float, kind: str = "chair") -> None:
        place_obstacle_ahead(system, ahead_m, kind)

    # =================================================================
    # S01 ★Gate 1/2/8★ 启动 + 导航激活 + 状态契约
    # =================================================================
    def s01_startup() -> str:
        c = _cfg()
        system = _system(c)
        try:
            expect(system.last_state() is None, "首轮之前不应存在状态")
            r = system.step(1.0)
            st = r.state

            # --- Gate 8：timestamp / source / confidence 齐备 ---
            expect(st.timestamp is not None, "SpatialState 缺 timestamp")
            for label, node in (
                ("user", st.user),
                ("environment", st.environment),
                ("navigation", st.navigation),
                ("semantic_scene", st.semantic_scene),
            ):
                expect(
                    hasattr(node, "source") and str(node.source) != "",
                    f"{label} 缺 source",
                )
            for label, node in (
                ("user", st.user),
                ("environment", st.environment),
                ("camera", st.camera),
            ):
                expect(hasattr(node, "timestamp"), f"{label} 缺 timestamp")
            expect(
                0.0 <= float(st.confidence.localization_confidence) <= 1.0,
                "localization_confidence 越界",
            )
            expect(
                0.0 <= float(st.confidence.perception_confidence) <= 1.0,
                "perception_confidence 越界",
            )

            # --- Gate 1/2：导航激活 ---
            expect(
                st.navigation.destination == c["simulator"]["map"]["destination"],
                "目的地未装载",
            )
            expect(st.navigation.distance_to_goal > 0.0, "启动瞬间就认为已到达")
            expect(st.navigation.current_route, "启动后应有规划路线")
            expect(r.action.permits_motion, "启动后应允许前进")
            expect(r.action.message, "启动后应给出首条引导")

            # --- 目的地指令（任务书第二十九节第一拍）---
            system.submit_query("带我去卫生间")
            r2 = system.step(1.0)
            expect(r2.state.navigation.destination, "下达指令后目的地丢失")
            expect(r2.action.permits_motion, "下达指令后不应停住用户")
        finally:
            system.close()

        return (
            f"目的地={st.navigation.destination} 剩余{st.navigation.distance_to_goal:.1f}m "
            f"路线{len(st.navigation.current_route)}点 首条='{r.action.message}'"
        )

    runner.run("S01 ★Gate 1/2/8★ 启动 + 导航激活 + 状态契约", s01_startup)

    # =================================================================
    # S02 ★Gate 2★ 正常直行 → Agent 保持安静
    # =================================================================
    def s02_quiet_cruise() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        try:
            y0 = None
            for i in range(13):
                r = system.step(1.0)
                if i == 0:
                    y0 = r.state.user.position.y
            y1 = system.last_state().user.position.y
            texts = [u["text"] for u in system.tools.utterance_log]
            dup = sum(1 for a, b in zip(texts, texts[1:]) if a == b)

            expect(y1 > y0 + 5.0, f"13 秒只推进 {y1 - y0:.1f}m，直行异常")
            expect(dup == 0, f"出现 {dup} 次连续重复播报（防刷屏失效）")
            expect(len(texts) <= 4, f"平直无事件路段播报了 {len(texts)} 条，过于聒噪")
            expect(not system.finished, "13 秒不该已经到达")
        finally:
            system.close()

        return f"13 轮直行 {y1 - y0:.1f}m / 播报 {len(texts)} 条且无重复"

    runner.run("S02 ★Gate 2★ 正常直行时保持安静（不刷屏）", s02_quiet_cruise)

    # =================================================================
    # S03 ★Gate 9★ 前方突然出现椅子 → 事件 + 安全层立即干预
    # =================================================================
    def s03_obstacle_appears() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        seen: list[Any] = []
        system.events.subscribe(lambda e: seen.append(e))
        try:
            system.step(1.0)          # 基线帧：事件检测器首帧只登记，不发声
            seen.clear()
            # 0.50m 中心距 − 0.30m 半径 = 前方净空 0.20m < critical 0.35m ⇒ 碰撞在即
            _place_obstacle(system, ahead_m=0.50)
            r = system.step(1.0)

            kinds = [e.event_type.value for e in seen]
            expect(
                EventType.OBSTACLE_APPEARED.value in kinds,
                f"未产生 OBSTACLE_APPEARED（本轮事件：{kinds or '无'}）",
            )
            expect(r.safety is not None, "近距离障碍必须触发安全判定")
            expect(
                r.safety.get("level") == "emergency",
                f"前方净空 0.20m 应判 emergency，实为 {r.safety.get('level')}",
            )
            expect(r.safety.get("intervene") is True, "emergency 必须要求干预")
            expect(not r.action.permits_motion, "碰撞在即时不得放行前进")
            expect(r.action.message, "必须当场提醒用户")
        finally:
            system.close()

        return (
            f"事件=OBSTACLE_APPEARED 安全={r.safety['level']} "
            f"放行={r.action.permits_motion} 播报='{r.action.message}'"
        )

    runner.run("S03 ★Gate 9★ 障碍突现 → 事件 + 安全层立即干预", s03_obstacle_appears)

    # =================================================================
    # S04 路线被暂时阻挡 → 提醒但允许继续 + 触发重规划
    # =================================================================
    def s04_route_blocked() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        seen: list[Any] = []
        system.events.subscribe(lambda e: seen.append(e))
        try:
            system.step(1.0)
            seen.clear()
            before = system.stats()["replans"]
            # 0.95m 中心距 − 0.30m 半径 = 前方净空 0.65m：已受阻（< high 0.70m），
            # 但没到碰撞距离（> critical 0.35m）—— 正是"提醒但不禁行"的那一档。
            _place_obstacle(system, ahead_m=0.95)
            r = system.step(1.0)
            st = r.state
            kinds = [e.event_type.value for e in seen]

            expect(
                st.risk.level.value in ("high", "critical"),
                f"前方净空 0.65m 风险应 ≥high，实为 {st.risk.level.value}",
            )
            expect(
                r.safety is not None and r.safety.get("level") == "warning",
                f"前方受阻但未碰撞应判 warning，实为 {r.safety and r.safety.get('level')}",
            )
            expect(r.action.permits_motion, "尚未到碰撞距离，不应把用户钉住")
            expect(r.action.message, "高风险必须提醒用户")
            expect(
                EventType.ROUTE_BLOCKED.value in kinds,
                f"应产生 ROUTE_BLOCKED（本轮事件：{kinds or '无'}）",
            )

            # 重规划要等导航器察觉"原路线走不通"才会触发（实测在第 5 轮左右），
            # 所以给一个窗口逐轮观察，而不是只看下一轮。
            replanned_at = None
            for i in range(8):
                system.step(1.0)
                if system.stats()["replans"] > before:
                    replanned_at = i
                    break
            expect(
                replanned_at is not None,
                f"前方被挡后 8 轮内未触发重规划（始终为 {before}）",
            )
        finally:
            system.close()

        return (
            f"风险={st.risk.level.value} 安全={r.safety.get('level')} "
            f"放行={r.action.permits_motion} 事件=ROUTE_BLOCKED "
            f"播报='{r.action.message}' 重规划在第 {replanned_at + 1} 轮触发"
        )

    runner.run("S04 路线被阻挡 → 提醒 + 允许继续 + 重规划", s04_route_blocked)

    # =================================================================
    # S05 ★Gate 9★ 障碍消失 → OBSTACLE_CLEARED 且不重复播报
    # =================================================================
    def s05_obstacle_cleared() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        seen: list[Any] = []
        system.events.subscribe(lambda e: seen.append(e))
        try:
            system.step(1.0)
            system.set_manual(True)      # 先冻结用户，让"障碍一直挡在前面"持续成立
            _place_obstacle(system, ahead_m=0.95)
            seen.clear()
            for _ in range(2):
                system.step(1.0)
            expect(
                any(e.event_type is EventType.OBSTACLE_APPEARED for e in seen),
                "前置条件失败：障碍未产生 OBSTACLE_APPEARED",
            )
            expect(
                not system.last_state().environment.front_clear,
                "前置条件失败：障碍没有出现在正前方",
            )

            # ① 障碍消失（椅子被搬走）
            system.obstacles.clear()
            system.set_manual(False)     # 用户恢复自动导航，继续往前走
            seen.clear()
            n0 = len(system.tools.utterance_log)

            # ② ★关键认知★：世界模型对物体有**时间持续性记忆**
            #    （`ObjectMemoryStore.evict_after_s = 60s`），所以"障碍离开视野"这件事
            #    不是清空列表那一刻发生的，而是**用户走远到 6 米感知窗口之外**时
            #    才被确认。把断言绑在"清空列表"上会永远等不到事件。
            cleared_at = None
            for i in range(14):
                system.step(1.0)
                if any(e.event_type is EventType.OBSTACLE_CLEARED for e in seen):
                    cleared_at = i
                    break

            expect(
                cleared_at is not None,
                f"走离障碍所在地后仍未产生 OBSTACLE_CLEARED"
                f"（本轮事件：{[e.event_type.value for e in seen] or '无'}）",
            )
            expect(
                system.last_state().environment.front_clear,
                "障碍消失后前方应恢复通畅",
            )
            added = len(system.tools.utterance_log) - n0
            expect(
                added == 0,
                f"障碍消失后重复播报了 {added} 条（任务书：不要重复播报大量信息）",
            )
        finally:
            system.close()

        return f"OBSTACLE_CLEARED 在第 {cleared_at + 1} 轮落地 / 之后新增播报 {added} 条"

    runner.run("S05 ★Gate 9★ 障碍消失 → 事件 + 不重复刷播报", s05_obstacle_cleared)

    # =================================================================
    # S06 ★Gate 3/5★ 提问「我旁边有什么？」→ 主动取图 → 简短回答
    # =================================================================
    def s06_question_triggers_visual() -> str:
        jpeg = tiny_jpeg()
        expect(jpeg is not None, "本机 OpenCV 无法编码 JPEG，本场景无法进行")
        cam = StubCamera(jpeg)
        c = _cfg(**{"agent.perception.mode": "on_demand"})
        system = _system(c, camera=cam)
        system.orchestrator.cognitive.enabled = True
        stub = StubVLM(
            {
                "action_type": "SPEAK",
                "message": "您左边有一张椅子和一盆绿植",
                "urgency": "normal",
                "reason": "画面显示左侧有家具与植物",
            }
        )
        system.llm = stub
        system.agent.llm = stub
        try:
            system.step(1.0)
            stub.seen_images = 0          # 只统计"提问之后"的取图

            system.submit_query("我旁边有什么？")
            got = None
            # ⚠️ 认知循环是「工作线程 + 20 ms 轮询」（`CognitiveLoop._run`）。
            #    仿真 1 Hz 的 `step()` 只要几毫秒，若不加等待，20 轮 step 会在
            #    20 ms 内跑完，工作线程一次都没轮到 —— 症状是"模型链路不通"，
            #    实际只是测试没给线程机会。这里必须显式让出时间片。
            for _ in range(20):
                r = system.step(1.0)
                if r.llm_used:
                    got = r
                    break
                time.sleep(0.05)

            expect(got is not None, "提问后认知结果始终未被取用（工作线程链路不通）")
            expect(
                stub.seen_images >= 1,
                "用户问环境时必须带上最新画面（on_demand 下主动感知未生效）",
            )
            expect(stub.image_bytes > 0, "带上的画面是空的")
            expect(got.action.message, "用户提问必须有回答")
            expect(
                len(got.action.message) <= int(c["agent"]["speak_policy"]["max_length_chars"]),
                f"回答 {len(got.action.message)} 字，超过一句上限",
            )
            pstats = system.agent.context.perception.stats()
            expect(
                "user_asks_environment" in pstats["reasons"],
                f"未登记 user_asks_environment（实际 {pstats['reasons']}）",
            )
            expect(pstats["mode"] == "on_demand", "模式未生效")
        finally:
            system.close()

        return (
            f"带图 {stub.seen_images} 次（{stub.image_bytes} B）"
            f" 回答='{got.action.message}'（{len(got.action.message)} 字）"
        )

    runner.run("S06 ★Gate 3/5★ 用户问环境 → 主动取图 → 简短回答", s06_question_triggers_visual)

    # =================================================================
    # S07 ★Gate 3★ 摄像头断线 → 继续导航 + camera unavailable
    # =================================================================
    def s07_camera_lost() -> str:
        jpeg = tiny_jpeg()
        expect(jpeg is not None, "本机 OpenCV 无法编码 JPEG，本场景无法进行")
        cam = StubCamera(jpeg)
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c, camera=cam)
        try:
            for _ in range(3):
                system.step(1.0)
            expect(system.last_state().camera.image_available, "断线前应能取到画面")
            y0 = system.last_state().user.position.y

            cam.disconnect()
            r = None
            for _ in range(5):
                r = system.step(1.0)
            st = system.last_state()

            expect(not st.camera.image_available, "断线后不得再标记有画面")
            expect(
                st.camera.freshness.value == "stale",
                f"收到过帧后断流应判 stale（不是 none），实为 {st.camera.freshness.value}",
            )
            expect(
                r.action.permits_motion,
                "摄像头断流不得停住用户（config: safety.camera_loss_is_safety=false）",
            )
            expect(
                st.user.position.y > y0 + 2.0,
                f"断流后只走了 {st.user.position.y - y0:.1f}m，导航没有继续",
            )
            alerts = [v.level.value for v in system.safety.recent_alerts()]
            expect(
                "emergency" not in alerts,
                f"摄像头断流被误判为安全问题（alerts={alerts}）",
            )
        finally:
            system.close()

        return (
            f"freshness={st.camera.freshness.value} 断流后前进 {st.user.position.y - y0:.1f}m "
            f"放行={r.action.permits_motion}"
        )

    runner.run("S07 ★Gate 3★ 摄像头断线 → 继续导航不误报", s07_camera_lost)

    # =================================================================
    # S08 ★Gate 6/7★ LLM 卡 5 秒 → 安全快循环照常工作
    # =================================================================
    def s08_llm_stall() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        system.orchestrator.cognitive.enabled = True
        slow = SlowVLM(5.0)
        system.llm = slow
        system.agent.llm = slow
        try:
            system.step(1.0)
            _place_obstacle(system, ahead_m=0.20)

            t0 = time.perf_counter()
            results = [system.step(1.0) for _ in range(5)]
            wall = time.perf_counter() - t0

            expect(wall < 1.0, f"LLM 卡 5s 期间 5 轮 step 共花 {wall:.2f}s ⇒ 主循环被拖住")
            expect(
                all(
                    r.safety is not None and r.safety.get("level") == "emergency"
                    for r in results
                ),
                "卡住期间安全层必须**每轮**都判出 emergency",
            )
            expect(
                all(not r.action.permits_motion for r in results),
                "紧急状态下不得放行前进",
            )
            expect(system.safety.evaluations >= 6, "快循环必须每轮求值安全规则")
            expect(
                system.orchestrator.cognitive.thinking,
                "此刻大模型应仍在工作线程里睡眠（证明真的并发了）",
            )
            expect(
                slow.calls <= 2,
                f"睡眠期间不应反复发起调用（实测 {slow.calls} 次）",
            )
        finally:
            system.close()

        return (
            f"5 轮 step 共 {wall:.2f}s（LLM 睡 {slow.delay:.0f}s）/ "
            f"安全求值 {system.safety.evaluations} 次 / 调用 {slow.calls} 次"
        )

    runner.run("S08 ★Gate 6/7★ LLM 卡 5 秒 → 安全快循环照常工作", s08_llm_stall)

    # =================================================================
    # S09 ★Gate 4/2★ 全程导航 → 到达目的地并主动告知
    # =================================================================
    def s09_goal_reached() -> str:
        c = _cfg(**{"simulator.obstacles.enabled": False})
        system = _system(c)
        seen: list[Any] = []
        system.events.subscribe(lambda e: seen.append(e))
        try:
            last = None
            ticks = 0
            # 整段导航里每次行动"说了什么"。★为什么必须收集而不是只看末轮★
            #   到达播报现在**只播一次**（到站后用户站着不动、到达状态是持续的，
            #   若把"持续"当"事件"就会每秒念一遍；见 RuleDecisionEngine 第 1 步）。
            #   而本场景到达后还会再多跑 3 轮，那时系统是安静的 ——
            #   只看 `last.action.message` 会得出"到了却没报"的错误结论。
            said: list[str] = []
            for _ in range(240):
                last = system.step(1.0)
                said.append(last.action.message or "")
                ticks += 1
                if system.finished:
                    break

            expect(
                system.finished,
                f"240 轮内未到达（进度 {last.state.navigation.route_progress * 100:.0f}%）",
            )
            # ⚠️ `finished` 来自仿真器的 `arrive_radius_m`，比事件检测器的
            #    GOAL_REACHED 判据（`walking_status=arrived` 或 `progress>=0.999`）宽松。
            #    到达那一轮事件还没到边沿，必须再走几轮让它落地 ——
            #    否则会得出"到了却没报"的错误结论。
            for _ in range(3):
                last = system.step(1.0)
                said.append(last.action.message or "")
                ticks += 1
            kinds = [e.event_type.value for e in seen]
            expect(
                EventType.GOAL_REACHED.value in kinds,
                f"未产生 GOAL_REACHED（实际 {sorted(set(kinds))}）",
            )
            dest = c["simulator"]["map"]["destination"]
            # ★断言必须收窄到"到达"这两个字★ 本场景是**规则模式**（无大模型），
            #   到达话术就是规则引擎的 `已到达目的地`。若把"消息里出现目的地名"也算过，
            #   那么一句普通的"前进约9米到卫生间门口"就能骗过断言 —— 等于没测。
            arrival_said = [m for m in said if m and "到达" in m]
            expect(arrival_said, f"到达目的地必须主动告知（跑过 {ticks} 轮却从未播报'到达'）")
            # ★顺手钉住"不刷屏"★ 到达之后连续几轮里，同一句到达话术不得重复出现。
            #   曾出现过到站后每秒念一遍"已到达目的地"（一次实测里念了 31 遍）。
            repeat = len([m for m in said[-4:] if m and "到达" in m])
            expect(repeat <= 1, f"到达话术在最后 4 轮里重复播报 {repeat} 次（应只播一次）")
        finally:
            system.close()

        return (
            f"{ticks} 轮到达『{c['simulator']['map']['destination']}』/ "
            f"播报 {len(system.tools.utterance_log)} 条 / 到达话术='{arrival_said[0]}'"
        )

    runner.run("S09 ★Gate 4/2★ 全程导航到达 + 主动告知", s09_goal_reached)

    # =================================================================
    # S10 ★Gate 10★ 录制 → 回放 100% 一致（不接模拟器）
    # =================================================================
    def s10_replay_roundtrip() -> str:
        from recording.session_player import SessionPlayer
        from recording.session_recorder import SessionRecorder

        root = Path(tempfile.mkdtemp(prefix="bsa_accept_"))
        c = _cfg()
        system = None
        try:
            rec = SessionRecorder(
                c, root=str(root), session_id="acceptance", notes="Stage 11 验收", seed=seed
            )
            system = _system(c, recorder=rec)
            # ⚠️ `events.jsonl` 是**一行一条事件**，不是一行一轮：没有事件的轮次不会写行。
            #    所以断言只能用"实际发布了多少条事件"，不能用"跑了多少轮"。
            published: list[Any] = []
            system.events.subscribe(lambda e: published.append(e))
            for _ in range(30):
                system.step(1.0)
            expect(len(published) > 0, "30 轮里一条事件都没有，本场景失去意义")
            system.close()
            system = None

            player = SessionPlayer(str(root), "acceptance")
            player.load()
            info = player.summary()
            expect(info["states"] == 30, f"应录到 30 条状态，实为 {info['states']}")
            expect(info["actions"] == 30, f"应录到 30 条行动，实为 {info['actions']}")
            expect(
                info["events"] == len(published),
                f"录制事件行 {info['events']} ≠ 本轮实际发布 {len(published)}",
            )

            report = player.replay(c, enable_llm=False)
            expect(report.compared == 30, f"应比对 30 轮，实为 {report.compared}")
            expect(
                report.match_rate == 1.0,
                f"回放一致率 {report.match_rate * 100:.1f}%，应 100%",
            )

            src = (
                Path(__file__).resolve().parents[1] / "recording" / "session_player.py"
            ).read_text("utf-8")
            expect(
                not re.search(r"^\s*(?:from|import)\s+simulator\b", src, re.M),
                "回放器不得 import simulator（任务书第十五节）",
            )
        finally:
            if system is not None:
                system.close()
            shutil.rmtree(root, ignore_errors=True)

        return f"录制 30 轮 → 回放 {report.matched}/{report.compared}（{report.match_rate * 100:.0f}%）零模拟器"

    runner.run("S10 ★Gate 10★ 录制 → 回放 100% 一致（不接模拟器）", s10_replay_roundtrip)

    return runner


# =====================================================================
# 运行入口
# =====================================================================
def run_all(
    cfg: dict[str, Any], verbose: bool = True, seed: int = 20260917
) -> dict[str, Any]:
    """运行全部验收场景，返回汇总（结构与 `tests.selftest.run_all` 一致）。"""
    t0 = time.perf_counter()
    if verbose:
        print("=" * 68)
        print(f"BlindSpatialAgent v0.3 验收场景  (seed={seed})")
        print("=" * 68)

    runner = build_scenarios(cfg, seed)

    duration = time.perf_counter() - t0
    passed = sum(1 for r in runner.results if r.ok)
    return {
        "total": len(runner.results),
        "passed": passed,
        "failed": len(runner.results) - passed,
        "skipped": 0,
        "duration_s": duration,
        "failures": [(r.name, r.error) for r in runner.results if not r.ok],
        "results": runner.results,
    }


def gate_report() -> str:
    """打印 Gate 1~10 ↔ 场景对照表。"""
    lines = ["--- 任务书 Gate 1~10 覆盖情况 ---"]
    for gate, desc, cover in GATE_MATRIX:
        lines.append(f"  {gate:<8} {desc:<38} → {cover}")
    return "\n".join(lines)


def main() -> int:
    from config.loader import load_config

    summary = run_all(load_config(), verbose=True)
    print()
    print(gate_report())
    print("=" * 68)
    status = "全部通过 ✅" if summary["failed"] == 0 else "存在失败项"
    print(
        f"验收场景: 通过 {summary['passed']} / {summary['total']}"
        f" | 失败 {summary['failed']} | 耗时 {summary['duration_s']:.1f}s | {status}"
    )
    for name, err in summary["failures"]:
        print(f"  ✗ {name}\n    {err}")
    print("=" * 68)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
