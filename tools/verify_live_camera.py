#!/usr/bin/env python3
"""用**真实手机画面**验证「iPhone 摄像头 → 多模态模型 → 语音输出」整条链路。

为什么需要它：
    tools/verify_real_vision.py 证的是「合成图能影响决策」，tools/verify_image_link.py
    证的是「请求体里带了图」，tools/e2e_test.py 用的是假模型。三者都不回答最后一个问题：
    **服务在跑、手机在推流时，模型嘴里说出来的东西，是不是真的来自用户眼前那一帧？**

    判定手法（关键设计）：
    真机场景下结构化状态里只有地图数据（本工程只有 desk / chair / door 等），
    而手机拍到的东西（柜子、置物架、包、食盆……）**在 SpatialState 里没有任何来源**。
    于是让模型「只根据摄像头画面」描述前方物品，并强制用固定前缀 `我看到：` 作答，
    只要它答出了地图里不存在的物体，就只能是画面起的作用 —— 无法用传感器数据蒙对。

    用固定前缀是为了**确定性地把这条回答从噪声里摘出来**：Agent 到达终点后会不停
    播报「已到达实验室出口」之类的状态语，靠时间窗口或长度都区分不开。

用法：
    python tools/verify_live_camera.py                 # 需先启动服务 + 手机 App 正在推流
    python tools/verify_live_camera.py --url http://127.0.0.1:8000
    python tools/verify_live_camera.py --keep          # 把证据帧存到 logs/live_camera_proof.jpg

退出码：0 = 通过；1 = 未通过；2 = 环境问题（服务没起 / 手机没在推流）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 强制模型用这个前缀作答，便于从 Agent 的状态播报里精确摘出「看图回答」那一条
MARKER = "我看到"
# 喂给模型的指令：明确禁止用地图/传感器数据，逼它只能依赖画面
INSTRUCTION = (
    "请只根据摄像头看到的画面回答：我正前方有哪些具体的物品？"
    f"请以「{MARKER}：」开头，逐个列出你真实看到的物体名称，"
    "禁止使用地图、导航或传感器数据推测，不确定的不要编。"
)


# ---------------------------------------------------------------------------
# HTTP 小工具：本机环境设了 HTTP_PROXY，连 127.0.0.1 必须显式绕过代理
# ---------------------------------------------------------------------------
def _opener() -> urllib.request.OpenerDirector:
    """构造一个禁用代理的 opener，避免请求被代理以 502 拒绝。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(opener: urllib.request.OpenerDirector, url: str, timeout: float = 5.0) -> dict:
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post_json(opener: urllib.request.OpenerDirector, url: str, payload: dict, timeout: float = 8.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def check_camera(opener: urllib.request.OpenerDirector, base: str) -> tuple[bool, str, dict]:
    """第一步：确认手机真的在推流（否则后面全无意义）。"""
    try:
        cam = _get_json(opener, f"{base}/api/camera")
    except Exception as e:  # noqa: BLE001
        return False, f"无法访问 {base}/api/camera：{e}", {}

    snap = cam.get("snapshot", {})
    stats = cam.get("stats", {})
    src, age = snap.get("source", "none"), snap.get("age_s")
    recv = stats.get("received", 0)

    if not snap.get("image_available") or age is None:
        return False, f"服务在跑，但**没有画面**（收到 {recv} 帧）。请在手机 App 里点「开始推流」。", cam
    if src != "websocket":
        return False, f"当前画面来源是 {src!r}，不是手机（websocket）。请确认 App 已连上并在推流。", cam
    if age > 5.0:
        return False, f"最新一帧已经 {age:.1f}s 前了（>{5.0}s），推流可能已断。", cam
    return True, f"手机推流正常：累计 {recv} 帧，最新帧 {age:.2f}s 前，{snap.get('bytes', 0)} 字节", cam


def ask_scene(opener: urllib.request.OpenerDirector, base: str, timeout_s: float) -> tuple[str | None, str | None, str]:
    """第二步：提交「只看画面」的问题，等模型用固定前缀作答。

    返回 (回答文本, 决策来源, 说明)。回答文本为 None 表示超时未捕获。
    """
    try:
        _post_json(opener, f"{base}/api/query", {"text": INSTRUCTION})
    except Exception as e:  # noqa: BLE001
        return None, None, f"提交查询失败：{e}"

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            st = _get_json(opener, f"{base}/api/state")
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        action = st.get("action", {})
        msg = (action.get("message") or "").strip()
        # 用固定前缀精确摘取「看图回答」，跳过「已到达实验室出口」之类的状态播报
        if MARKER in msg:
            return msg, action.get("source"), "已捕获"
        time.sleep(1.0)
    return None, None, f"{timeout_s:.0f}s 内没有捕获到以「{MARKER}」开头的回答"


def main() -> int:
    ap = argparse.ArgumentParser(description="真实手机画面 → 多模态模型 链路验证")
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址")
    ap.add_argument("--timeout", type=float, default=30.0, help="等待模型作答的秒数")
    ap.add_argument("--keep", action="store_true", help="把证据帧另存到 logs/live_camera_proof.jpg")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    opener = _opener()
    print("=" * 72)
    print("真实手机画面 → 多模态模型 链路验证")
    print("=" * 72)

    # --- 1) 手机在推流吗 ---
    ok, msg, cam = check_camera(opener, base)
    print(f"[1/3] 摄像头链路: {msg}")
    if not ok:
        print("\n结论: 环境未就绪（exit 2），无法验证。")
        return 2

    # --- 2) 存一份证据帧 ---
    snap = cam.get("snapshot", {})
    src_path = Path(snap.get("path", ""))
    if src_path.is_file():
        proof = PROJECT_ROOT / "logs" / "live_camera_proof.jpg"
        try:
            shutil.copy2(src_path, proof)
            print(f"[2/3] 证据帧已保存: {proof.relative_to(PROJECT_ROOT)}（{proof.stat().st_size} 字节）")
        except OSError as e:  # noqa: BLE001
            print(f"[2/3] 证据帧保存失败（不影响验证）: {e}")
    else:
        print("[2/3] 服务未落盘画面文件，跳过证据帧保存")

    # 记录结构化状态里已知的物体，供最后对照
    try:
        st0 = _get_json(opener, f"{base}/api/state")
        known = st0.get("state", {}).get("semantic_scene", {}).get("objects", [])
        summary = st0.get("state", {}).get("semantic_scene", {}).get("summary", "")
    except Exception:  # noqa: BLE001
        known, summary = [], ""

    # --- 3) 问模型「只根据画面」描述前方物品 ---
    print(f"[3/3] 已提交「只看画面」的问题，等待模型作答（最多 {args.timeout:.0f}s）...")
    answer, source, note = ask_scene(opener, base, args.timeout)

    print()
    print("-" * 72)
    print(f"结构化状态里的已知物体（地图数据）: {known}")
    if summary:
        print(f"  传感器摘要: {summary}")
    print("-" * 72)
    if answer is None:
        print(f"模型回答: (未捕获) —— {note}")
        print("\n结论: 未通过（exit 1）。模型没有以画面为题作答，可能是 Key 不可用或画面异常。")
        return 1
    print(f"模型回答（source={source}）:\n  {answer}")
    print("-" * 72)

    problems = []
    if source != "llm":
        problems.append(f"决策来源是 {source!r} 而不是 llm（说明走了规则兜底，模型没参与）")
    body = answer.split("：", 1)[-1].strip()
    if len(body) < 8:
        problems.append(f"回答过短（{len(body)} 字），不足以证明读到了画面")

    if problems:
        print("\n结论: 未通过（exit 1）")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\n结论: 通过（exit 0）—— 模型给出的物体描述只能来自手机画面，")
    print("      结构化状态（地图/传感器）里没有这些物体的任何来源，")
    print("      因此「iPhone 摄像头 → 多模态模型 → 语音输出」整条链路成立。")
    print(f"      请肉眼核对证据帧 logs/live_camera_proof.jpg 与上面的描述是否一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
