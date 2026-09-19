#!/usr/bin/env python3
"""BlindSpatialAgent v0.3 最终验收 Demo —— 任务书第二十九节「七拍剧本」。

本文件与 `main.py --acceptance`（`tests/acceptance.py`）的分工：

    --acceptance        10 个**互相独立**的判定场景，逐条回答 Gate 1~10；
    --acceptance-demo   一次**连续**会话，按任务书剧本顺序演出 7 拍，
                        证明这些能力能在**同一条时间线**上串起来。

七拍（逐字对照任务书第二十九节）：

    第 1 拍  用户：「带我去 <目的地>。」            → navigation active
    第 2 拍  正常直行                              → Agent 保持安静
    第 3 拍  椅子突然出现在前方                     → OBSTACLE_APPEARED
                                                    → Safety 近距离立即提醒
                                                    → 路线被暂时阻挡 → 绕行建议
    第 4 拍  障碍消失                              → OBSTACLE_CLEARED
                                                    → 不重复播报大量信息
    第 5 拍  用户：「我旁边有什么？」               → USER_QUESTION
                                                    → REQUEST_VISUAL → 取最新画面
                                                    → VLM 分析 → 简短回答
    第 6 拍  模拟 camera 断线                      → 继续导航，camera unavailable
    第 7 拍  模拟 LLM timeout 5 秒                 → Safety Loop 继续正常

全程**离线**：无网络、无 API Key、无真机 iPhone。
大模型用替身（stub），摄像头用可按需断线的替身 —— 与 `tests/acceptance.py` 共用，
避免两套 stub 各自漂移。

用法：
    python main.py --acceptance-demo
    python -m tools.acceptance_demo
    python -m tools.acceptance_demo --out docs/V03_DEMO_TRANSCRIPT.md
"""

from __future__ import annotations

import argparse
import copy
import math
import sys
import time
from pathlib import Path
from typing import Any

# 复用验收场景的替身与工具（单一来源，防止两处实现不一致）
from tests.acceptance import (  # noqa: E402
    SlowVLM,
    StubCamera,
    StubVLM,
    place_obstacle_ahead,
    tiny_jpeg,
)


class Demo:
    """一次连续会话的七拍剧本演出 + 文本转写。"""

    def __init__(self, cfg: dict[str, Any], seed: int, out: Path | None) -> None:
        self.cfg = copy.deepcopy(cfg)
        # 离线、可复现：清空 Key（否则认知循环会真的外发请求）
        self.cfg["llm"]["api_key"] = ""
        # 感知触发改为「按需」：只有用户提问才取图，保证第 5 拍是画面唯一的来源
        self.cfg["agent"]["perception"]["mode"] = "on_demand"
        # 障碍改为手动摆放：剧本要求"椅子出现在正前方 X 米"每跑必现
        self.cfg["simulator"]["obstacles"]["enabled"] = False
        self.seed = seed
        self.out = out

        self.lines: list[str] = []
        self.beats: list[tuple[str, bool, str]] = []
        self._checks: list[tuple[bool, str]] = []
        self.system: Any = None

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def say(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)

    def check(self, ok: bool, msg: str) -> bool:
        self._checks.append((bool(ok), msg))
        return bool(ok)

    def beat(self, idx: int, title: str, evidence: str) -> None:
        failed = [m for ok, m in self._checks if not ok]
        ok = not failed
        self.beats.append((f"第 {idx} 拍", ok, evidence))
        self.say(f"  {'✓ PASS' if ok else '✗ FAIL'}  {title}")
        for m in failed:
            self.say(f"         └─ {m}")
        self._checks.clear()
        self.say()

    def utterances(self) -> list[str]:
        return [u["text"] for u in self.system.tools.utterance_log]

    def new_utterances(self, since: int) -> list[str]:
        return self.utterances()[since:]

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self) -> int:
        from agent.agent_core import SpatialAgentSystem, SystemConfig

        dest = self.cfg["simulator"]["map"]["destination"]
        jpeg = tiny_jpeg()
        if jpeg is None:
            self.say("本机 OpenCV 无法编码 JPEG，无法演出画面相关的拍子。")
            return 2

        cam = StubCamera(jpeg)
        self.say("=" * 72)
        self.say(" BlindSpatialAgent v0.3 · 最终验收 Demo（任务书第二十九节 七拍剧本）")
        self.say("=" * 72)
        self.say(f"  目的地        {dest}")
        self.say(f"  地图          40×46 m / 16 个功能区")
        self.say(f"  大模型        离线替身（enable_llm=False，不联网）")
        self.say(f"  摄像头        替身（可模拟断线）")
        self.say(f"  随机种子      {self.seed}")
        self.say("-" * 72)
        self.say()

        self.system = SpatialAgentSystem(
            self.cfg, options=SystemConfig(seed=self.seed, enable_llm=False), camera=cam
        )
        try:
            self.beat_1_command(dest)
            self.beat_2_quiet_cruise()
            self.beat_3_obstacle_appears()
            self.beat_4_obstacle_cleared()
            self.beat_5_user_question(cam)
            self.beat_6_camera_lost(cam, dest)
            self.beat_7_llm_timeout()
        finally:
            self.system.close()

        return self.summary()

    # ------------------------------------------------------------------
    def step(self, n: int = 1, dt: float = 1.0, pause: float = 0.0) -> Any:
        r = None
        for _ in range(n):
            r = self.system.step(dt)
            if pause:
                # ⚠️ 认知循环是「工作线程 + 20 ms 轮询」：仿真 1 Hz 的 step 只要几毫秒，
                #    不放时间片的话工作线程一次都轮不到，症状长得像"模型链路不通"。
                time.sleep(pause)
        return r

    def note_utterance(self, since: int) -> None:
        for t in self.new_utterances(since):
            self.say(f'   播报: 「{t}」')

    # ------------------------------------------------------------------
    # 第 1 拍：用户下指令 → 导航激活
    # ------------------------------------------------------------------
    def beat_1_command(self, dest: str) -> None:
        self.say(f"【第 1 拍】用户：「带我去{dest}。」")
        self.system.submit_query(f"带我去{dest}")
        t0 = len(self.system.tools.utterance_log)
        r = self.step()
        st = r.state

        self.say(
            f"          t={self.system.elapsed:5.1f}s  导航激活 · "
            f"目的地={st.navigation.destination} · 剩余{st.navigation.distance_to_goal:.1f}m · "
            f"路线{len(st.navigation.current_route)}点"
        )
        self.note_utterance(t0)

        self.check(st.navigation.destination == dest, "目的地未装载")
        self.check(bool(st.navigation.current_route), "未规划路线")
        self.check(st.navigation.distance_to_goal > 0, "启动即认为已到达")
        self.check(r.action.permits_motion, "导航激活后应允许前进")
        self.check(bool(r.action.message), "应给出首条引导")

        self.beat(
            1,
            "导航激活",
            f"目的地={st.navigation.destination} 剩余{st.navigation.distance_to_goal:.1f}m "
            f"路线{len(st.navigation.current_route)}点",
        )

    # ------------------------------------------------------------------
    # 第 2 拍：正常直行 → Agent 保持安静
    # ------------------------------------------------------------------
    def beat_2_quiet_cruise(self) -> None:
        self.say("【第 2 拍】正常直行 —— Agent 应保持安静（不刷屏）")
        y0 = self.system.nav.pos[1]
        t0 = len(self.system.tools.utterance_log)
        r = self.step(12)
        y1 = self.system.nav.pos[1]
        texts = [u["text"] for u in self.system.tools.utterance_log]
        dup = sum(1 for a, b in zip(texts, texts[1:]) if a == b)
        said = self.new_utterances(t0)

        self.say(f"          t={self.system.elapsed:5.1f}s  直行 {y1 - y0:.1f} m")
        self.say(f"          本轮播报 {len(said)} 条，连续重复 {dup} 次")
        self.note_utterance(t0)

        self.check(y1 > y0 + 6.0, f"12 秒只推进 {y1 - y0:.1f}m，直行异常")
        self.check(dup == 0, f"出现 {dup} 次连续重复播报（防刷屏失效）")
        self.check(len(said) <= 4, f"平直无事件路段播报 {len(said)} 条，过于聒噪")
        self.check(not self.system.finished, "12 秒不该已经到达")

        self.beat(2, "保持安静", f"直行 {y1 - y0:.1f}m / 播报 {len(said)} 条 / 重复 {dup} 次")

    # ------------------------------------------------------------------
    # 第 3 拍：椅子突现 → 事件 + 安全层 + 绕行
    # ------------------------------------------------------------------
    def beat_3_obstacle_appears(self) -> None:
        from events.event_types import EventType

        self.say("【第 3 拍】椅子突然出现在前方")
        seen: list[Any] = []
        self.system.events.subscribe(lambda e: seen.append(e))

        # --- 3a：中距离受阻 ⇒ 提醒 + 允许继续 + 路线被暂时阻挡 + 重规划（绕行）---
        before_replans = self.system.stats()["replans"]
        t0 = len(self.system.tools.utterance_log)
        place_obstacle_ahead(self.system, ahead_m=0.95)
        r = self.step()
        st = r.state
        kinds_mid = [e.event_type.value for e in seen]

        self.say(
            f"          t={self.system.elapsed:5.1f}s  前方净空 0.65 m ⇒ "
            f"风险={st.risk.level.value} 安全={r.safety.get('level')} "
            f"放行={r.action.permits_motion}"
        )
        self.note_utterance(t0)

        self.check(
            "OBSTACLE_APPEARED" in kinds_mid,
            f"未产生 OBSTACLE_APPEARED（本轮：{kinds_mid or '无'}）",
        )
        self.check(r.safety is not None, "应触发安全判定")
        self.check(r.action.permits_motion, "尚在预警档，不应把用户钉住")
        self.check(bool(r.action.message), "高风险必须提醒")

        # 重规划（= 绕行）要等导航器察觉原路线走不通，给一个窗口
        replanned_at = None
        for i in range(8):
            self.step()
            if self.system.stats()["replans"] > before_replans:
                replanned_at = i
                break
        self.say(f"          绕行：重规划在第 {replanned_at + 1 if replanned_at is not None else '—'} 轮触发")
        self.check(replanned_at is not None, "前方被挡后 8 轮内未触发重规划（无绕行）")

        # --- 3b：近距离 ⇒ 安全层立即制止 ---
        place_obstacle_ahead(self.system, ahead_m=0.35)
        t0 = len(self.system.tools.utterance_log)
        r2 = self.step()
        self.say(
            f"          t={self.system.elapsed:5.1f}s  前方净空 0.05 m ⇒ "
            f"安全={r2.safety.get('level')} 放行={r2.action.permits_motion}"
        )
        self.note_utterance(t0)

        self.check(
            r2.safety is not None and r2.safety.get("level") == "emergency",
            f"近距离应判 emergency，实为 {r2.safety and r2.safety.get('level')}",
        )
        self.check(not r2.action.permits_motion, "碰撞在即时不得放行前进")
        self.check(bool(r2.action.message), "紧急状态必须当场提醒用户")

        self.beat(
            3,
            "障碍突现 → 事件/安全/绕行",
            f"OBSTACLE_APPEARED → 预警档提醒并放行 → 第 {replanned_at + 1 if replanned_at is not None else '—'} 轮绕行重规划"
            f" → 近距离 emergency 停行",
        )

    # ------------------------------------------------------------------
    # 第 4 拍：障碍消失 → OBSTACLE_CLEARED，且不重复播报
    # ------------------------------------------------------------------
    def beat_4_obstacle_cleared(self) -> None:
        from events.event_types import EventType

        self.say("【第 4 拍】障碍消失（椅子被搬走）—— 不要重复播报大量信息")
        seen: list[Any] = []
        self.system.events.subscribe(lambda e: seen.append(e))
        n0 = len(self.system.tools.utterance_log)

        self.system.obstacles.clear()

        # ⚠️ 世界模型对物体有**时间持续性记忆**（ObjectMemoryStore.evict_after_s=60s），
        #    "障碍离开视野"不是清空列表那一刻发生的，而是用户走远到 6 m 感知窗口
        #    之外才被确认。所以必须继续前进若干轮，而不是清空后立刻断言。
        cleared_at = None
        for i in range(16):
            self.step()
            if any(e.event_type is EventType.OBSTACLE_CLEARED for e in seen):
                cleared_at = i
                break
        self.say(
            f"          t={self.system.elapsed:5.1f}s  "
            f"OBSTACLE_CLEARED 在第 {cleared_at + 1 if cleared_at is not None else '—'} 轮落地"
        )

        self.check(cleared_at is not None, "走离障碍后仍未产生 OBSTACLE_CLEARED")
        self.check(self.system.last_state().environment.front_clear, "障碍消失后前方应恢复通畅")

        added = self.new_utterances(n0)
        self.say(f"          障碍消失后新增播报 {len(added)} 条")
        self.check(
            len(added) == 0,
            f"障碍消失后重复播报 {len(added)} 条（任务书：不要重复播报大量信息）",
        )

        self.beat(
            4,
            "障碍消失不刷屏",
            f"OBSTACLE_CLEARED 第 {cleared_at + 1 if cleared_at is not None else '—'} 轮落地 / 新增播报 {len(added)} 条",
        )

    # ------------------------------------------------------------------
    # 第 5 拍：用户提问 → 主动取图 → VLM → 简短回答
    # ------------------------------------------------------------------
    def beat_5_user_question(self, cam: StubCamera) -> None:
        self.say("【第 5 拍】用户：「我旁边有什么？」")
        self.say("          链路：USER_QUESTION → REQUEST_VISUAL → 取最新画面 → VLM 分析 → 简短回答")

        stub = StubVLM(
            {
                "action_type": "SPEAK",
                "message": "您左边有一张椅子和一盆绿植",
                "urgency": "normal",
                "reason": "画面显示左侧有家具与植物",
            }
        )
        self.system.llm = stub
        self.system.agent.llm = stub
        self.system.orchestrator.cognitive.enabled = True

        t0 = len(self.system.tools.utterance_log)
        stub.seen_images = 0  # 只统计"提问之后"的取图
        self.system.submit_query("我旁边有什么？")

        got = None
        for _ in range(20):
            r = self.step(pause=0.05)
            if r.llm_used:
                got = r
                break

        pstats = self.system.agent.context.perception.stats()
        self.say(
            f"          t={self.system.elapsed:5.1f}s  取图 {stub.seen_images} 次"
            f"（{stub.image_bytes} B）· 触发理由={pstats['reasons']}"
        )
        if got is not None:
            self.say(f"          回答: 「{got.action.message}」（{len(got.action.message)} 字）")
        self.note_utterance(t0)

        self.check(got is not None, "提问后认知结果始终未被取用（工作线程链路不通）")
        if got is not None:
            self.check(stub.seen_images >= 1, "用户问环境时必须带上最新画面")
            self.check(stub.image_bytes > 0, "带上的画面是空的")
            self.check(bool(got.action.message), "用户提问必须有回答")
            self.check(
                "user_asks_environment" in pstats["reasons"],
                f"未登记 user_asks_environment（实际 {pstats['reasons']}）",
            )
            self.check(
                len(got.action.message) <= int(self.cfg["agent"]["speak_policy"]["max_length_chars"]),
                f"回答 {len(got.action.message)} 字，超过一句上限",
            )
        self.check(cam.connected, "提问阶段摄像头应仍在线")

        self.beat(
            5,
            "提问 → 取图 → 简短回答",
            f"REQUEST_VISUAL → 带图 {stub.seen_images} 次（{stub.image_bytes} B）→ "
            f"回答 {len(got.action.message) if got else 0} 字",
        )

    # ------------------------------------------------------------------
    # 第 6 拍：camera 断线 → 继续导航
    # ------------------------------------------------------------------
    def beat_6_camera_lost(self, cam: StubCamera, dest: str) -> None:
        self.say("【第 6 拍】模拟 camera 断线 —— 系统应继续导航（camera unavailable）")
        # 断线拍不涉及大模型，关掉认知循环以免产生无关调用
        self.system.orchestrator.cognitive.enabled = False

        self.check(cam.connected, "断线前摄像头应在线")
        y0 = self.system.nav.pos[1]
        # ⚠️ `recent_alerts()` 返回的是**整个会话**的告警历史，前面第 3 拍
        #    因为障碍合法地产生过 warning/emergency。这里只关心"断线之后"新增的告警，
        #    否则会把第 3 拍的旧告警误算成"把断线误判成安全问题"。
        n_alerts = len(self.system.safety.recent_alerts())
        cam.disconnect()
        r = self.step(5)
        st = self.system.last_state()

        self.say(
            f"          t={self.system.elapsed:5.1f}s  camera.image_available="
            f"{st.camera.image_available} · freshness={st.camera.freshness.value} · "
            f"断流后前进 {self.system.nav.pos[1] - y0:.1f} m"
        )
        alerts = [v.level.value for v in self.system.safety.recent_alerts()[n_alerts:]]

        self.check(not st.camera.image_available, "断线后不得再标记有画面")
        self.check(
            st.camera.freshness.value == "stale",
            f"收到过帧后断流应判 stale（不是 none），实为 {st.camera.freshness.value}",
        )
        self.check(r.action.permits_motion, "摄像头断流不得停住用户")
        self.check(
            self.system.nav.pos[1] > y0 + 2.0,
            f"断流后只走了 {self.system.nav.pos[1] - y0:.1f}m，导航没有继续",
        )
        self.check("emergency" not in alerts, f"摄像头断流被误判为安全问题（alerts={alerts}）")

        self.beat(
            6,
            "断线继续导航",
            f"camera unavailable（freshness=stale）/ 断流后前进 {self.system.nav.pos[1] - y0:.1f}m / 放行=True",
        )

    # ------------------------------------------------------------------
    # 第 7 拍：LLM 卡 5 秒 → Safety Loop 照常
    # ------------------------------------------------------------------
    def beat_7_llm_timeout(self) -> None:
        self.say("【第 7 拍】模拟 LLM timeout 5 秒 —— Safety Loop 应继续正常")
        slow = SlowVLM(5.0)
        self.system.llm = slow
        self.system.agent.llm = slow
        self.system.orchestrator.cognitive.enabled = True

        self.step()
        place_obstacle_ahead(self.system, ahead_m=0.20)  # 净空 -0.10 m ⇒ 必判 emergency
        ev0 = self.system.safety.evaluations

        t0 = time.perf_counter()
        rows = []
        for _ in range(5):
            # ★把椅子钉在"当前朝向的正前方"★
            #   被挡住后智能体会**原地转身**另寻出路；障碍一旦离开前向锥，安全层就会
            #   正确地判 ok —— 那是合法避让，不是失效。为让本拍结论干净，
            #   每轮把椅子重新锚定到当前正前方 0.20 m。
            ob = self.system.obstacles.obstacles[0]
            ux, uy = self.system.nav.pos
            rad = math.radians(float(self.system.nav.heading))
            ob.x, ob.y = ux + math.sin(rad) * 0.20, uy + math.cos(rad) * 0.20

            r = self.system.step(1.0)
            rows.append(
                (
                    (r.safety or {}).get("level", "ok"),
                    bool(r.action.permits_motion),
                    r.state.risk.level.value,
                    r.state.environment.front_distance,
                    self.system.nav.heading,
                )
            )
        wall = time.perf_counter() - t0

        # ⚠️ `StepResult.safety` 在判定为 ok 时是 None（"本轮无 verdict"），
        #    不是异常 —— 统一归一化成 level 字符串再断言，避免把 None 当字典。
        evals = self.system.safety.evaluations - ev0

        self.say(
            f"          t={self.system.elapsed:5.1f}s  5 轮 step 共 {wall:.2f}s"
            f"（LLM 在工作线程里睡 {slow.delay:.0f}s，主循环未被拖住）"
        )
        for i, (lv, al, rk, fd, hd) in enumerate(rows):
            self.say(
                f"          轮{i + 1}: 安全={lv:<9} 放行={al!s:<5} 风险={rk:<9} "
                f"前方={fd:.2f}m 朝向={hd:.0f}°"
            )
        self.say(f"          安全层本轮求值 {evals} 次 / LLM 调用 {slow.calls} 次")

        # ★本拍真正要证明的不变式★
        #   不是"每一轮都必须 emergency"—— 智能体原地转身把障碍甩出前向锥后，
        #   安全层判 ok 是**正确的**（现实中盲人被挡住后转身另找路也是对的）。
        #   真正的不变式是：「只要前方净空进入危险档，安全层就必须判 emergency
        #   并禁止前进」+「快循环每轮照常求值」+「主循环不被 5 秒阻塞拖住」。
        danger = [(lv, al) for lv, al, rk, _fd, _hd in rows if rk == "critical"]
        leak = any(al for lv, al, rk, _fd, _hd in rows if rk == "critical" and lv != "emergency")
        self.say(f"          危险档轮次 {len(danger)}/{len(rows)}")

        self.check(wall < 1.0, f"LLM 卡 5s 期间 5 轮 step 花 {wall:.2f}s ⇒ 主循环被拖住")
        self.check(evals >= 5, f"快循环必须每轮求值安全规则，本轮只求值 {evals} 次")
        self.check(len(danger) >= 3, f"危险档轮次仅 {len(danger)}，本拍失去意义")
        self.check(
            all(lv == "emergency" for lv, _al in danger),
            f"危险档必须判 emergency，实为 {[lv for lv, _al in danger]}",
        )
        self.check(not leak, "危险档不得在没有 emergency 结论时放行前进")
        self.check(slow.calls <= 2, f"睡眠期间不应反复发起调用（实测 {slow.calls} 次）")

        self.beat(
            7,
            "LLM 卡死不影响安全",
            f"5 轮 step 仅 {wall:.2f}s（LLM 睡 5s）/ 安全求值 {evals} 次 / "
            f"危险档 {len(danger)} 轮全部 emergency+禁行 / 调用 {slow.calls} 次",
        )

    # ------------------------------------------------------------------
    def summary(self) -> int:
        failed = [b for b in self.beats if not b[1]]
        self.say("=" * 72)
        self.say(" 七拍结果")
        self.say("=" * 72)
        for name, ok, ev in self.beats:
            self.say(f"  {'✓' if ok else '✗'} {name}  {ev}")
        self.say("-" * 72)
        self.say(f" 合计：{len(self.beats) - len(failed)} / {len(self.beats)} 通过")
        self.say("=" * 72)
        self.say()
        if failed:
            self.say("结论：v0.3 最终验收 Demo **未通过**。")
        else:
            self.say("结论：v0.3 最终验收 Demo **通过** —— 七拍剧本全部演出成功。")

        if self.out is not None:
            self.out.parent.mkdir(parents=True, exist_ok=True)
            self.out.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
            print(f"（转写已保存：{self.out}）", flush=True)

        return 0 if not failed else 1


# =====================================================================
# 入口
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    from config.loader import load_config

    p = argparse.ArgumentParser(description="BlindSpatialAgent v0.3 最终验收 Demo（七拍剧本）")
    p.add_argument("--seed", type=int, default=20260917, help="随机种子（可复现）")
    p.add_argument(
        "--out",
        default="docs/V03_DEMO_TRANSCRIPT.md",
        help="把完整转写另存为文件（默认 docs/V03_DEMO_TRANSCRIPT.md；传空串则不存）",
    )
    args = p.parse_args(argv)

    out = Path(args.out) if args.out else None
    return Demo(load_config(), seed=args.seed, out=out).run()


if __name__ == "__main__":
    sys.exit(main())
