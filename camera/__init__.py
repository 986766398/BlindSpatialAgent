"""摄像头层：接收 iPhone 图片流。

    iphone_receiver.py —— 接收帧（WebSocket 二进制 / HTTP 表单）、缓存最新帧、落盘
    image_processor.py —— 解码 / 缩放 / 编码 / 合成测试图

阶段一用浏览器网页模拟 iPhone 摄像头；阶段二换成真机，接口不变。
"""

from camera.image_processor import (
    decode,
    encode,
    image_info,
    make_test_frame,
    prepare_for_llm,
    resize_max_side,
)
from camera.iphone_receiver import IphoneReceiver

__all__ = [
    "IphoneReceiver",
    "decode",
    "encode",
    "image_info",
    "make_test_frame",
    "prepare_for_llm",
    "resize_max_side",
]
