#!/usr/bin/env python3
"""iPhone 推流模拟器（真机不在手时的替身）。

作用：**在没有 iPhone 的情况下**，用同一套协议把 JPEG 以 1 fps 推给 Python 服务端，
      用来验证 `camera/` + `api/` + `agent/` 这条链路是否通。

协议与 iOS App `BSACameraStreamer` 完全一致（见 ios/BSACameraStreamer/.../FrameStreamClient.swift）：
    上行 连接后立即发  {"type": "hello", "device": "iPhoneStub", ...}
    上行 二进制帧       <JPEG 字节>
    上行 定时心跳       {"type": "ping"}
    下行 服务端消息     {"type": "hello"/"ack"/"pong"/"status"/"error"}

画面来源两种：
    --source auto      优先用本机摄像头，取不到就用合成画面（默认）
    --source camera    强制用本机摄像头（把电脑摄像头当成"手机摄像头"）
    --source synthetic 强制合成画面（无摄像头环境，例如 CI / 远程机器）

用法：
    python tools/iphone_stub.py                                   # 合成画面，1 fps，连本机 8000 端口
    python tools/iphone_stub.py --source camera                    # 用本机摄像头
    python tools/iphone_stub.py --host 192.168.1.23 --frames 30    # 连到局域网内的服务端
    python tools/iphone_stub.py --fps 2 --quality 60               # 2 fps
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# 允许直接以 `python tools/iphone_stub.py` 运行（把项目根目录加入模块搜索路径）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import websockets  # noqa: E402

from camera.image_processor import encode  # noqa: E402

WIDTH, HEIGHT = 640, 480


# =====================================================================
# 画面来源
# =====================================================================
class SyntheticSource:
    """合成画面：带时间戳 + 来回移动的"障碍物"，与 App 模拟器降级路径行为一致。"""

    name = "synthetic"

    def __init__(self) -> None:
        self.tick = 0

    def read(self) -> np.ndarray:
        self.tick += 1
        img = np.full((HEIGHT, WIDTH, 3), 26, dtype=np.uint8)
        cv2.line(img, (0, 360), (WIDTH, 360), (56, 56, 56), 3)
        x = 30 + (self.tick * 43) % 520
        cv2.rectangle(img, (x, 280), (x + 90, 364), (216, 132, 62), -1)  # BGR
        lines = [
            "BSA IPHONE STUB",
            f"frame #{self.tick}",
            datetime.now().strftime("%H:%M:%S"),
        ]
        for i, text in enumerate(lines):
            cv2.putText(
                img, text, (28, 60 + i * 42),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (235, 235, 235), 2, cv2.LINE_AA,
            )
        return img

    def close(self) -> None:
        pass


class OpenCVSource:
    """本机摄像头（把电脑摄像头当成手机摄像头）。"""

    name = "camera"

    def __init__(self, index: int) -> None:
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开摄像头 index={index}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)

    def read(self) -> np.ndarray | None:
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        return cv2.resize(frame, (WIDTH, HEIGHT), interpolation=cv2.INTER_AREA)

    def close(self) -> None:
        self.cap.release()


def make_source(kind: str, camera_index: int) -> object:
    if kind == "synthetic":
        return SyntheticSource()
    if kind == "camera":
        return OpenCVSource(camera_index)
    # auto
    try:
        return OpenCVSource(camera_index)
    except Exception as e:  # noqa: BLE001
        print(f"[stub] 本机摄像头不可用（{e}），改用合成画面")
        return SyntheticSource()


# =====================================================================
# 主流程
# =====================================================================
async def upload(args: argparse.Namespace) -> int:
    src = make_source(args.source, args.camera_index)
    url = f"ws://{args.host}:{args.port}{args.path}"
    interval = 1.0 / max(0.1, args.fps)

    print(f"[stub] 画面来源 = {getattr(src, 'name', '?')}")
    print(f"[stub] 目标     = {url}")
    print(f"[stub] 帧率     = {args.fps} fps（间隔 {interval * 1000:.0f} ms）")
    print(f"[stub] 计划发送 = {'不限' if args.frames <= 0 else args.frames} 帧\n")

    sent = 0
    acked = 0
    bytes_sent = 0
    sizes: list[int] = []
    started = time.perf_counter()
    stop = asyncio.Event()

    async def receive_loop(ws) -> None:
        """消费下行消息，避免接收缓冲区堆积。"""
        nonlocal acked
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    print(f"[stub] 下行（非 JSON）：{str(raw)[:80]}")
                    continue
                mtype = msg.get("type", "?")
                if mtype == "hello":
                    print(
                        f"[stub] 握手成功：server={msg.get('server')} v{msg.get('version')} "
                        f"max_fps={msg.get('max_fps')} max_side={msg.get('max_image_side')}"
                    )
                elif mtype == "ack":
                    acked = int(msg.get("accepted", acked))
                    if not msg.get("ok"):
                        print(f"[stub] 服务端拒绝该帧：{msg.get('reason')}")
                elif mtype == "pong":
                    pass
                elif mtype in ("welcome", "status"):
                    print(f"[stub] {mtype}: {msg}")
                else:
                    print(f"[stub] 下行 {mtype}: {msg}")
        except Exception:  # noqa: BLE001 - 连接关闭属正常
            pass

    try:
        # proxy=None：显式禁用系统代理。
        # 局域网直连（192.168.x.x / 127.0.0.1）绝不能走 HTTP 代理，
        # 否则会被代理以 502 拒绝；而 websockets 默认会读取 http_proxy 环境变量。
        async with websockets.connect(
            url,
            max_size=8 * 1024 * 1024,
            ping_interval=None,
            proxy=None,
        ) as ws:
            # ① 与 iOS App 一致：先打招呼
            await ws.send(json.dumps({
                "type": "hello",
                "device": "iPhoneStub",
                "app": "tools/iphone_stub.py",
                "app_version": "1.0",
                "system": f"python {sys.version_info.major}.{sys.version_info.minor}",
            }))

            rx = asyncio.create_task(receive_loop(ws))

            # ② 按固定节拍发送 JPEG
            next_at = time.perf_counter()
            while not stop.is_set():
                if args.frames > 0 and sent >= args.frames:
                    break
                if args.duration > 0 and time.perf_counter() - started >= args.duration:
                    break

                frame = src.read()
                if frame is None:
                    print("[stub] 读帧失败，退出")
                    break
                jpeg = encode(frame, args.quality)
                if not jpeg:
                    print("[stub] JPEG 编码失败，跳过该帧")
                else:
                    await ws.send(jpeg)
                    sent += 1
                    bytes_sent += len(jpeg)
                    sizes.append(len(jpeg))
                    if sent % 5 == 1 or sent == args.frames:
                        print(
                            f"[stub] 已发送 {sent:>3} 帧  "
                            f"本帧 {len(jpeg) / 1024:5.1f} KB  "
                            f"服务端接受 {acked} 帧"
                        )
                    # 每 5 帧发一次心跳，模拟 App 的定时 ping
                    if sent % 5 == 0:
                        await ws.send(json.dumps({"type": "ping"}))

                next_at += interval
                await asyncio.sleep(max(0.0, next_at - time.perf_counter()))

            stop.set()
            rx.cancel()
            try:
                await rx
            except asyncio.CancelledError:
                pass
    finally:
        src.close()

    elapsed = time.perf_counter() - started
    print("\n--- 推流统计 ---")
    print(f"发送帧数 : {sent}")
    print(f"服务端接受: {acked}")
    print(f"总流量   : {bytes_sent / 1024:.1f} KB")
    if sizes:
        print(f"单帧大小 : 最小 {min(sizes) / 1024:.1f} KB / 最大 {max(sizes) / 1024:.1f} KB "
              f"/ 平均 {sum(sizes) / len(sizes) / 1024:.1f} KB")
    if elapsed > 0:
        print(f"实际帧率 : {sent / elapsed:.2f} fps（耗时 {elapsed:.1f}s）")
    return 0 if sent > 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="iPhone 推流模拟器（BSACameraStreamer 的替身）")
    p.add_argument("--host", default="127.0.0.1", help="服务端地址（真机联调填电脑的局域网 IP）")
    p.add_argument("--port", type=int, default=8000, help="服务端端口")
    p.add_argument("--path", default="/ws/camera", help="图片上行路径")
    p.add_argument("--fps", type=float, default=1.0, help="发送帧率")
    p.add_argument("--quality", type=int, default=50, help="JPEG 质量 0~100")
    p.add_argument("--source", choices=("auto", "camera", "synthetic"), default="auto",
                   help="画面来源")
    p.add_argument("--camera-index", type=int, default=0, help="本机摄像头序号")
    p.add_argument("--frames", type=int, default=15, help="发送多少帧后退出（0=不限）")
    p.add_argument("--duration", type=float, default=0.0, help="最长推流秒数（0=不限）")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(upload(parse_args())))
