#!/usr/bin/env python3
"""用**真实大模型**验证「画面 → 决策」这条链路（A/B 对照，不需要真机）。

为什么需要它：
    tools/verify_image_link.py 证明的是「请求体里带了图」（用假模型抓包），
    tools/e2e_test.py 用的是本地假模型。两者都不回答最关键的问题：
    **真实模型看到真实画面后，决策会不会真的改变？**

做法（A/B 对照，消除时序与状态残留干扰）：
    构造两套**互相独立**的 Agent（同一配置、同一起点），分别只喂一张图：
        A 组：空旷地面（安全场景）
        B 组：正前方起火（危险场景，且火情在 SpatialState 里**完全不存在**）
    然后分别 step 一轮，比较真实模型产出的决策。
    只要 B 组识别出火/烟/危险、且与 A 组明显不同，即证明视觉信息真的进了决策。

    用「火」是因为它在结构化状态里没有任何来源 —— 模型不可能凭 LiDAR 猜出来，
    因此这个对照比「图里有个箱子」这类信息强得多（箱子模拟场景里本来就有）。

用法：
    python tools/verify_real_vision.py            # 需要 .env 里配好真实 Key
    python tools/verify_real_vision.py --keep     # 顺带把两张测试图写到 /tmp 便于肉眼核对

退出码：0 = 通过；1 = 未通过；2 = 环境问题（无 Key 等）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.agent_core import SpatialAgentSystem, SystemConfig  # noqa: E402
from camera.image_processor import encode  # noqa: E402
from camera.iphone_receiver import IphoneReceiver  # noqa: E402
from config.loader import load_config  # noqa: E402

# 判定 B 组是否识别出危险的关键词（火情绝不可能来自结构化状态）
DANGER_WORDS = ("火", "烟", "燃", "火灾", "危险", "fire", "FIRE", "明火", "火焰")


def _make_plain_frame() -> bytes:
    """A 组：空旷地面（安全）。"""
    import numpy as np  # noqa: PLC0415

    w, h = 640, 480
    img = np.full((h, w, 3), 150, dtype=np.uint8)     # 素色地面
    img[:150, :] = (125, 125, 130)                     # 远处略暗
    return encode(img, 85) or b""


def _make_fire_frame() -> bytes:
    """B 组：正前方起火（危险，且结构化状态里没有这条信息）。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    w, h = 640, 480
    img = np.full((h, w, 3), 90, dtype=np.uint8)
    img[150:, :] = (70, 80, 95)
    # 多层火焰三角，外橙内黄
    for cx, bw, bh, col in (
        (320, 150, 230, (0, 60, 230)),
        (250, 90, 150, (0, 140, 255)),
        (390, 90, 160, (10, 90, 245)),
        (320, 60, 200, (60, 220, 255)),
    ):
        pts = np.array([[cx, 470 - bh], [cx - bw // 2, 470], [cx + bw // 2, 470]], np.int32)
        cv2.fillPoly(img, [pts], col)
    cv2.putText(img, "FIRE !", (190, 110), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (40, 40, 240), 5, cv2.LINE_AA)
    return encode(img, 88) or b""


def _decide_with_image(cfg: dict, frame: bytes, seed: int) -> dict:
    """用一套全新 Agent 喂一张图，跑一轮，返回决策与视觉状态。"""
    camera = IphoneReceiver(cfg)
    system = SpatialAgentSystem(
        cfg, camera=camera, options=SystemConfig(seed=seed, enable_llm=True)
    )
    camera.submit(frame, source="websocket")
    step = system.step(1.0)
    a = step.action
    return {
        "source": a.source,
        "action_type": a.action_type,
        "urgency": a.urgency,
        "message": a.message or "",
        "reason": a.reason or "",
        "image_available": bool(step.state.camera.image_available),
        "age_s": step.state.camera.age_s,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用真实大模型验证画面→决策链路")
    ap.add_argument("--keep", action="store_true", help="把两张测试图写到 /tmp 便于肉眼核对")
    ap.add_argument("--seed", type=int, default=20260917)
    args = ap.parse_args(argv)

    cfg = load_config()
    if not cfg["llm"].get("api_key"):
        print("未配置大模型 Key，无法做真实推理验证。")
        print("请先运行：.venv/bin/python tools/set_llm_key.py")
        return 2

    plain, fire = _make_plain_frame(), _make_fire_frame()
    if args.keep:
        Path("/tmp/bsa_ab_plain.jpg").write_bytes(plain)
        Path("/tmp/bsa_ab_fire.jpg").write_bytes(fire)
        print("已写出对照图：/tmp/bsa_ab_plain.jpg  /tmp/bsa_ab_fire.jpg\n")

    print("=" * 68)
    print(f"模型 {cfg['llm']['model']} @ {cfg['llm']['base_url']}")
    print("=" * 68)

    print("\n[A 组] 喂入「空旷地面」，跑一轮…")
    a = _decide_with_image(cfg, plain, args.seed)
    print(f"  视觉可用 = {a['image_available']}  (age {a['age_s']:.2f}s)")
    print(f"  source={a['source']}  type={a['action_type']}  urgency={a['urgency']}")
    print(f"  依据: {a['reason'][:160]}")

    print("\n[B 组] 喂入「正前方起火」，跑一轮…")
    b = _decide_with_image(cfg, fire, args.seed)
    print(f"  视觉可用 = {b['image_available']}  (age {b['age_s']:.2f}s)")
    print(f"  source={b['source']}  type={b['action_type']}  urgency={b['urgency']}")
    print(f"  播报: {b['message'][:160]}")
    print(f"  依据: {b['reason'][:160]}")

    # ---------------- 断言 ----------------
    print("\n" + "=" * 68)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  → ' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    blob_b = b["message"] + b["reason"]
    check(
        "两组都被状态融合层视为「有画面」",
        a["image_available"] and b["image_available"],
        f"A={a['image_available']} B={b['image_available']}",
    )
    check(
        "决策来自大模型而非规则兜底",
        a["source"] == "llm" and b["source"] == "llm",
        f"A={a['source']} B={b['source']}",
    )
    check(
        "B 组识别出火情/危险（结构化状态里没有这条信息）",
        any(w in blob_b for w in DANGER_WORDS),
        f"命中词={[w for w in DANGER_WORDS if w in blob_b]}",
    )
    check(
        "A/B 两组决策不同（说明画面真的参与了推理）",
        (a["action_type"], a["reason"]) != (b["action_type"], b["reason"]),
    )

    print("=" * 68)
    if fails:
        print(f"结果：未通过（{len(fails)} 项）—— 真实视觉链路可能有问题")
        return 1
    print("结果：全部通过 —— 真实大模型确实「看到」了画面并据此改变决策。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
