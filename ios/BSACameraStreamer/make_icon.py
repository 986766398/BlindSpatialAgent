#!/usr/bin/env python3
"""生成 BSACameraStreamer 的 App 图标（1024x1024，不透明，无圆角）。

设计概念：**「会照路的镜头」**
    外圈  = 相机镜头（光圈环）
    内部  = 一支朝前的粗箭头（Agent 给视障用户带路的方向指令）

为什么是这个构图（而不是更花哨的光圈叶片）：
    桌面图标实际显示尺寸约 60pt，任何细节在缩到 40px 后都会糊成一团。
    所以只保留两个元素：环 = 相机，箭头 = 导航。对比度高、一眼可辨。

为什么用代码画而不是找图 / 生成图：
    1. 尺寸、不透明度、无 alpha 这些硬性要求可以精确控制；
    2. 可复现 —— 改配色只需改本文件顶部的常量；
    3. 不依赖任何素材版权，也不消耗图像生成额度。

技术要点：
    - 先按 4 倍分辨率（4096）绘制，再用 INTER_AREA 缩到 1024 —— 相当于 4x 超采样，
      因为 cv2 的 LINE_AA 在斜线上仍有肉眼可见的阶梯。
    - 输出必须是**不透明** 8bit BGR。iOS 自己做圆角遮罩，
      所以图里不能预画圆角，圆角外也不能是透明区。

用法：
    python ios/BSACameraStreamer/make_icon.py
    python ios/BSACameraStreamer/make_icon.py --out /tmp/icon.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------
# 设计常量（改这里即可换配色 / 调比例）
# ---------------------------------------------------------------------
FINAL = 1024            # 输出边长
SS = 4                  # 超采样倍率

BG_TOP = (0x33, 0x20, 0x0C)      # BGR —— #0C2033 深海军蓝
BG_BOTTOM = (0x52, 0x36, 0x12)   # BGR —— #123652
GLOW = (0xF0, 0xA0, 0x30)        # BGR —— #30A0F0 中心柔光
RING = (0xF5, 0xCE, 0x5A)        # BGR —— #5ACEF5 光圈环
CHEVRON = (0xF8, 0xF6, 0xF2)     # BGR —— #F2F6F8 近白
CHEVRON_EDGE = (0xF0, 0xB4, 0x46)  # BGR —— #46B4F0 箭头描边（青）

RING_R = 0.318          # 光圈环半径（相对边长）
RING_W = 0.030          # 环线宽

ARROW_HALF_W = 0.150    # 箭头半宽
ARROW_TOP = -0.142      # 箭头顶点 y 偏移（相对边长，负 = 向上）
ARROW_BOTTOM = 0.148    # 箭头两脚 y 偏移
ARROW_W = 0.062         # 箭头笔画粗细


def build(size: int) -> np.ndarray:
    """在 size x size 画布上绘制图标（BGR, uint8）。"""
    s = float(size)

    # ---------- 1. 垂直渐变背景 ----------
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None, None]
    top = np.array(BG_TOP, np.float32)[None, None, :]
    bottom = np.array(BG_BOTTOM, np.float32)[None, None, :]
    img = np.repeat((top * (1.0 - t) + bottom * t), size, axis=1)

    cx = cy = size / 2.0

    # ---------- 2. 中心柔光（径向衰减，叠加） ----------
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (s * 0.66)
    glow = np.clip(1.0 - dist, 0.0, 1.0) ** 2.6
    img += glow[..., None] * np.array(GLOW, np.float32)[None, None, :] * 0.38

    out = np.clip(img, 0, 255).astype(np.uint8)

    # ---------- 3. 前进箭头（先画外面那层青色描边，再压上白色主体） ----------
    apex = (int(round(cx)), int(round(cy + ARROW_TOP * s)))
    left = (int(round(cx - ARROW_HALF_W * s)), int(round(cy + ARROW_BOTTOM * s)))
    right = (int(round(cx + ARROW_HALF_W * s)), int(round(cy + ARROW_BOTTOM * s)))
    # ⚠️ 顺序必须是 左→顶点→右，否则 isClosed=False 时左腿不会被绘制
    pts = np.array([left, apex, right], np.int32)

    # 描边层：整条折线外加圆头，避免折点出现缺口
    edge_w = int(round((ARROW_W + 0.022) * s))
    cv2.polylines(out, [pts], False, CHEVRON_EDGE, edge_w, cv2.LINE_AA)
    for p in (apex, left, right):
        cv2.circle(out, p, edge_w // 2, CHEVRON_EDGE, -1, cv2.LINE_AA)

    # 主体层：近白色
    body_w = int(round(ARROW_W * s))
    cv2.polylines(out, [pts], False, CHEVRON, body_w, cv2.LINE_AA)
    for p in (apex, left, right):
        cv2.circle(out, p, body_w // 2, CHEVRON, -1, cv2.LINE_AA)

    # ---------- 4. 光圈环 ----------
    ring_r = int(round(RING_R * s))
    ring_w = int(round(RING_W * s))
    cv2.circle(out, (int(cx), int(cy)), ring_r, RING, ring_w, cv2.LINE_AA)

    # 环内侧一条极细的亮线，让环有"金属厚度"的层次
    cv2.circle(
        out,
        (int(cx), int(cy)),
        ring_r - int(ring_w * 1.15),
        (0xE2, 0xB6, 0x52),
        max(2, int(ring_w * 0.20)),
        cv2.LINE_AA,
    )

    return out


def main() -> int:
    default_out = (Path(__file__).with_name("BSACameraStreamer")
                   / "Assets.xcassets" / "AppIcon.appiconset" / "AppIcon-1024.png")
    parser = argparse.ArgumentParser(description="生成 BSACameraStreamer App 图标")
    parser.add_argument("--out", default=str(default_out), help="输出 PNG 路径")
    args = parser.parse_args()

    icon = cv2.resize(build(FINAL * SS), (FINAL, FINAL), interpolation=cv2.INTER_AREA)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out_path), icon):
        print(f"[icon] 写入失败: {out_path}")
        return 1

    h, w = icon.shape[:2]
    print(f"[icon] 已生成 {w}x{h} → {out_path}")
    print(f"[icon] 通道数={icon.shape[2]}（3 = 不透明，iOS 要求）  "
          f"大小={out_path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
