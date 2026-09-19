"""iPhone 摄像头接收器。

阶段一：用电脑浏览器上传图片来模拟 iPhone 摄像头（不需要开发 iOS App）。
阶段二：iPhone 用 Safari 打开同一个网页并开启后置摄像头，或直接跑一个极简上传脚本，
        数据格式完全一致 —— Agent 侧一行都不用改。

职责：
    1. 接收 JPEG 字节（来自 WebSocket 二进制帧或 HTTP 表单）
    2. 校验并预处理（缩放 + 重新编码）
    3. 在内存中保存"最新一帧"，按节流频率落盘到 logs/latest.jpg
    4. 对外提供 snapshot()（元数据）与 latest_bytes()（图像数据）

线程安全：API 服务在事件循环里写帧，主循环在线程里读帧，因此用锁保护。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from camera import image_processor as ip
from config.loader import resolve_path

LOG = logging.getLogger("bsa.camera")


class IphoneReceiver:
    """摄像头帧接收器（内存缓存 + 落盘 + 新鲜度）。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        c = cfg["camera"]
        self.cfg = cfg
        self.max_side: int = int(c.get("max_image_side", 768))
        self.quality: int = int(c.get("jpeg_quality", 60))
        self.stale_after: float = float(c.get("stale_after_s", 3.0))
        self.save_interval: float = float(c.get("save_interval_s", 1.0))
        self.max_fps: int = int(c.get("max_fps", 5))
        self.latest_path: Path = resolve_path(c["latest_image_path"])

        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._raw: bytes | None = None
        self._timestamp: float | None = None
        self._datetime: datetime | None = None
        self._source: str = "none"
        self._frame_id: int = 0
        self._last_save: float = 0.0
        self._last_accept: float = 0.0

        self.received: int = 0
        self.accepted: int = 0
        self.rejected: int = 0
        self.throttled: int = 0

    # -----------------------------------------------------------------
    # 写入
    # -----------------------------------------------------------------
    def submit(self, data: bytes, source: str = "websocket", throttle: bool = True) -> dict[str, Any]:
        """接收一帧。返回处理结果（含是否被接受）。"""
        self.received += 1
        now = time.time()

        if throttle and self.max_fps > 0:
            min_gap = 1.0 / self.max_fps
            if now - self._last_accept < min_gap:
                self.throttled += 1
                return {"accepted": False, "reason": "rate_limited"}

        if not data:
            self.rejected += 1
            return {"accepted": False, "reason": "empty"}

        proc = ip.prepare_for_llm(data, self.max_side, self.quality)
        if proc is None:
            self.rejected += 1
            LOG.warning("收到无法解码的图像数据（%d 字节）", len(data))
            return {"accepted": False, "reason": "decode_failed"}

        info = ip.image_info(proc)
        with self._lock:
            self._latest = proc
            self._raw = data
            self._timestamp = now
            self._datetime = datetime.now()
            self._source = source
            self._frame_id += 1
            frame_id = self._frame_id
            self._last_accept = now
            should_save = (now - self._last_save) >= self.save_interval
            if should_save:
                self._last_save = now

        self.accepted += 1
        if should_save:
            self._save(proc)

        return {
            "accepted": True,
            "frame_id": frame_id,
            "source": source,
            "size": info,
            "saved": should_save,
        }

    def _save(self, data: bytes) -> None:
        try:
            self.latest_path.parent.mkdir(parents=True, exist_ok=True)
            self.latest_path.write_bytes(data)
        except OSError as e:  # 落盘失败不应影响主流程
            LOG.warning("latest.jpg 落盘失败: %s", e)

    def clear(self) -> None:
        with self._lock:
            self._latest = None
            self._raw = None
            self._timestamp = None
            self._datetime = None
            self._source = "none"
            self._frame_id = 0

    # -----------------------------------------------------------------
    # 读取
    # -----------------------------------------------------------------
    def age(self) -> float | None:
        with self._lock:
            if self._timestamp is None:
                return None
            return time.time() - self._timestamp

    def snapshot(self) -> dict[str, Any]:
        """返回帧元数据，字段与 SpatialState.camera 对应。"""
        with self._lock:
            age = None if self._timestamp is None else time.time() - self._timestamp
            fresh = age is not None and age <= self.stale_after
            return {
                "image_available": self._latest is not None and fresh,
                "timestamp": self._datetime,
                "age_s": age,
                "source": self._source,
                "frame_id": self._frame_id,
                "path": str(self.latest_path),
                "stale": (age is not None and age > self.stale_after),
                "bytes": 0 if self._latest is None else len(self._latest),
            }

    def latest_bytes(self) -> bytes | None:
        """返回已预处理的最新帧（供多模态模型使用）。"""
        with self._lock:
            if self._latest is None:
                return None
            age = None if self._timestamp is None else time.time() - self._timestamp
            if age is not None and age > self.stale_after:
                return None  # 过期图片不送模型，避免模型看到"过去的画面"
            return self._latest

    def preview_bytes(self, max_age_s: float = 60.0) -> bytes | None:
        """返回最新帧原图，**供前端预览**（与 latest_bytes 的关键区别：允许过期）。

        为什么要单独开一个方法而不是复用 latest_bytes：
        - latest_bytes 是喂给多模态模型的，一旦过期必须返回 None，
          否则模型会对着"过去的画面"做决策；
        - 而前端预览恰恰需要显示"已经过期的那一帧" —— 用户才能区分
          「从来没收到过画面」和「推流断了，最后看到的是这张」。
        超过 max_age_s 才当作没有，避免页面长期展示一张很老的图造成误导。
        """
        with self._lock:
            if self._latest is None:
                return None
            age = None if self._timestamp is None else time.time() - self._timestamp
            if age is not None and age > max_age_s:
                return None
            return self._latest

    def stats(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "throttled": self.throttled,
            "frame_id": self._frame_id,
            "source": self._source,
            "latest_path": str(self.latest_path),
            "max_fps": self.max_fps,
        }


__all__ = ["IphoneReceiver"]
