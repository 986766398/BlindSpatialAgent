"""图像处理：iPhone 图片流的解码、缩放、编码与新鲜度判断。

为什么需要单独一层：
1. 大模型不需要（也吃不下）原始分辨率图，必须先缩放再编码，控制 token 与延迟。
2. 上行数据可能是任意格式/损坏数据，必须在入口处挡掉，不能让坏数据流进 Agent。
3. 未来接真实 iPhone / 智能眼镜时，这里就是统一的图像预处理入口。
"""

from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np

LOG = logging.getLogger("bsa.camera")


def decode(data: bytes) -> np.ndarray | None:
    """把 JPEG/PNG 字节解码成 BGR 图像；失败返回 None。"""
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def encode(img: np.ndarray, quality: int = 60) -> bytes | None:
    """把图像编码成 JPEG 字节。"""
    if img is None or img.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else None


def resize_max_side(img: np.ndarray, max_side: int) -> np.ndarray:
    """按最长边等比缩放，只缩小不放大。"""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_side or longest == 0:
        return img
    scale = max_side / float(longest)
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


def prepare_for_llm(data: bytes, max_side: int = 768, quality: int = 60) -> bytes | None:
    """把上行图片处理成适合送多模态模型的 JPEG。"""
    img = decode(data)
    if img is None:
        return None
    img = resize_max_side(img, max_side)
    return encode(img, quality)


def image_info(data: bytes) -> dict[str, Any]:
    """返回图像的基本信息，用于日志与自检。"""
    img = decode(data)
    if img is None:
        return {"valid": False, "bytes": len(data)}
    h, w = img.shape[:2]
    return {
        "valid": True,
        "width": int(w),
        "height": int(h),
        "channels": int(img.shape[2]) if img.ndim == 3 else 1,
        "bytes": len(data),
        "mean_brightness": round(float(img.mean()), 1),
    }


def make_test_frame(width: int = 640, height: int = 480, text: str = "TEST") -> bytes:
    """生成一张合成测试图（用于自检，不需要真实摄像头）。"""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:] = (40, 40, 40)
    cv2.rectangle(img, (40, height - 120), (width - 40, height - 60), (60, 120, 200), -1)
    cv2.putText(img, text, (60, 140), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (240, 240, 240), 3, cv2.LINE_AA)
    out = encode(img, 80)
    return out or b""


__all__ = [
    "decode",
    "encode",
    "image_info",
    "make_test_frame",
    "prepare_for_llm",
    "resize_max_side",
]
