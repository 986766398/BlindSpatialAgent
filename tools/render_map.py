#!/usr/bin/env python3
"""把 `config/config.yaml` 里的地图**画成一张 PNG**，用于人工核对。

## 为什么需要它

v0.3 起地图是**数据驱动**的（zones / route_landmarks / static_objects / area_objects），
改障碍"看不见后果"：摆一串圆形障碍或一条矩形围挡，只有真正跑起来才发现
"走廊被切成两半""门洞被家具堵死"。用例 75 能兜住"房间不可达"这类硬故障，
但兜不住"绕得要死但还能过" —— 那正是"障碍太多、一直重规划"的成因。

所以每次改地图后：`python tools/render_map.py` && 打开 `docs/map_check.png` 看一眼。

## 用法

    .venv/bin/python tools/render_map.py                     # 输出 docs/map_check.png
    .venv/bin/python tools/render_map.py --out /tmp/m.png    # 指定路径
    .venv/bin/python tools/render_map.py --scale 30          # 像素/米

图例：灰=可通行区域，深灰=墙体（不可通行），橙框=区域型障碍(area_objects)，
      棕圆=静态障碍(blocking)，浅圆=语义物体(不挡路)，绿线=门洞，
      蓝线+蓝点=全局路线/路点，绿点=起点，红星=目的地。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import load_config  # noqa: E402
from simulator.map_simulator import MapSimulator  # noqa: E402

BG = (245, 245, 245)
WALL = (90, 90, 90)
WALK = (210, 210, 210)
AREA = (40, 130, 235)      # 橙（BGR）
STATIC = (60, 90, 150)     # 棕
GHOST = (190, 200, 210)    # 浅
DOOR = (80, 190, 80)       # 绿
ROUTE = (200, 140, 40)     # 蓝
USER = (90, 200, 90)
GOAL = (60, 60, 220)


def to_px(x: float, y: float, m: MapSimulator, scale: int, pad: int) -> tuple[int, int]:
    """世界坐标 → 像素（y 轴翻转：地图上"北"画在上方）。"""
    return int(pad + (x - m.x_min) * scale), int(pad + (m.y_max - y) * scale)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "map_check.png"))
    ap.add_argument("--scale", type=int, default=20, help="像素/米")
    args = ap.parse_args()

    cfg = load_config()
    m = MapSimulator(cfg)
    scale, pad = args.scale, 24
    W = int((m.x_max - m.x_min) * scale) + pad * 2
    H = int((m.y_max - m.y_min) * scale) + pad * 2
    img = np.full((H, W, 3), BG, dtype=np.uint8)

    # --- 1. 占用栅格：深灰=阻挡（含 0.45m 安全间隙），浅灰=可通行 ---
    blocked = m.build_blocked_grid()
    for i in range(m.ny):
        for j in range(m.nx):
            x, y = m.cell_to_world(i, j)
            p = to_px(x, y, m, scale, pad)
            if blocked[i, j]:
                cv2.rectangle(img, p, (p[0] + scale, p[1] + scale), WALL, -1)
            elif m.walkable[i, j]:
                cv2.rectangle(img, p, (p[0] + scale, p[1] + scale), WALK, -1)

    # --- 2. 区域型障碍：橙框（虚线感：画粗边 + 半透明内衬） ---
    for obj in m.objects:
        if not obj.is_area:
            continue
        p1 = to_px(obj.x - obj.hx, obj.y + obj.hy, m, scale, pad)
        p2 = to_px(obj.x + obj.hx, obj.y - obj.hy, m, scale, pad)
        overlay = img.copy()
        cv2.rectangle(overlay, p1, p2, AREA, -1)
        cv2.addWeighted(overlay, 0.45, img, 0.55, 0, img)
        cv2.rectangle(img, p1, p2, AREA, 2)
        cv2.putText(img, obj.type, (p1[0] + 3, p1[1] + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)

    # --- 3. 静态障碍 / 语义物体 ---
    for obj in m.objects:
        if obj.is_area:
            continue
        c = to_px(obj.x, obj.y, m, scale, pad)
        r = max(3, int(round(obj.radius * scale)))
        col, th = (STATIC, -1) if obj.blocking else (GHOST, 2)
        cv2.circle(img, c, r, col, th)

    # --- 4. 门洞（zones 里名字带"门"的矩形） ---
    doors = [z for z in m.zones if "门" in str(z["name"])]
    for z in doors:
        p1 = to_px(z["x_min"], z["y_max"], m, scale, pad)
        p2 = to_px(z["x_max"], z["y_min"], m, scale, pad)
        cv2.rectangle(img, p1, p2, DOOR, 2)
        w = float(z["x_max"]) - float(z["x_min"])
        h = float(z["y_max"]) - float(z["y_min"])
        cv2.putText(img, f"{z['name']} {max(w, h):.1f}m",
                    (p1[0] - 8, p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (30, 120, 30), 1,
                    cv2.LINE_AA)

    # --- 5. 全局路线 + 路点 ---
    pts = [(float(p["x"]), float(p["y"])) for p in m.landmarks]
    for a, b in zip(pts, pts[1:]):
        cv2.line(img, to_px(*a, m, scale, pad), to_px(*b, m, scale, pad), ROUTE, 1, cv2.LINE_AA)
    for (x, y), lm in zip(pts, m.landmarks):
        cv2.circle(img, to_px(x, y, m, scale, pad), 3, ROUTE, -1)

    sx, sy = float(m.start["x"]), float(m.start["y"])
    cv2.circle(img, to_px(sx, sy, m, scale, pad), 5, USER, -1)
    cv2.putText(img, "start", to_px(sx, sy, m, scale, pad),
                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (20, 90, 20), 1, cv2.LINE_AA)
    tgt = pts[-1] if pts else (sx, sy)
    cv2.drawMarker(img, to_px(*tgt, m, scale, pad), GOAL, cv2.MARKER_STAR, 14, 2)

    # --- 6. 标题 ---
    n_area = sum(1 for o in m.objects if o.is_area)
    n_block = sum(1 for o in m.objects if o.blocking and not o.is_area)
    rep = m.connectivity_report()
    dw = list(rep["doors"].values())
    head = (f"{m.name}  |  {n_block} 静态障碍 + {n_area} 区域障碍  |  "
            f"门洞有效宽 {min(dw):.2f}~{max(dw):.2f}m（clr={rep['clearance']:.2f}m）"
            f"  |  孤岛比 {rep['orphan_ratio']:.3f}")
    cv2.putText(img, head, (pad, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (30, 30, 30), 1, cv2.LINE_AA)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), img)
    print(f"已写出 {out}  （{img.shape[1]}x{img.shape[0]}）")
    print(f"  可通行格 {int(m.walkable.sum())} / 阻挡格 {int(m.build_blocked_grid().sum())}")
    print(f"  {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
