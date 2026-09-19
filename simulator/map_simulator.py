"""室内地图模拟器。

职责（对应真实的 3D 地图 / NavMesh / UE5 数字孪生）：
1. 把 config.yaml 里的矩形区域（zones）栅格化成占用栅格（occupancy grid）。
2. 提供可通行性判断、到墙距离、通道宽度等几何查询 —— 等价于真实系统的 NavMesh 查询。
3. 提供 A* 局部寻路 —— 等价于真实系统的 NavMesh 路径搜索。
4. 维护静态语义物体（桌子/椅子/门/人）。

坐标约定：x 向东，y 向北；heading 0°=+y，顺时针为正。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

# 8 邻域： (di, dj, cost)
_NEIGHBORS: tuple[tuple[int, int, float], ...] = (
    (1, 0, 1.0),
    (-1, 0, 1.0),
    (0, 1, 1.0),
    (0, -1, 1.0),
    (1, 1, math.sqrt(2)),
    (1, -1, math.sqrt(2)),
    (-1, 1, math.sqrt(2)),
    (-1, -1, math.sqrt(2)),
)


def _longest_run(flags: list[bool]) -> int:
    """`flags` 中最长连续 True 的长度（用于量门洞的有效通行宽度）。"""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


@dataclass(frozen=True)
class MapObject:
    """静态语义物体。

    支持两种形状（与 `sensors.base.MapObjectInfo` 保持一致）：
      * 圆形：`radius > 0`，`hx == hy == 0`；
      * 区域型矩形：`hx > 0 and hy > 0`（此时 `radius` 恒为 0）。
    判定一律用 `is_area()`，不要用 `radius > 0` 反推形状。
    """

    type: str
    x: float
    y: float
    radius: float
    blocking: bool
    hx: float = 0.0
    hy: float = 0.0

    @property
    def is_area(self) -> bool:
        """是否为区域型（矩形）障碍。"""
        return self.hx > 0.0 and self.hy > 0.0

    def distance_to(self, x: float, y: float) -> float:
        """点 (x,y) 到物体表面的距离（内部为 0）。"""
        if self.is_area:
            dx = max(abs(x - self.x) - self.hx, 0.0)
            dy = max(abs(y - self.y) - self.hy, 0.0)
            return math.hypot(dx, dy)
        return math.hypot(x - self.x, y - self.y) - self.radius


class MapSimulator:
    """二维室内地图 + 占用栅格 + A* 寻路。"""

    def __init__(self, cfg: dict[str, Any]) -> None:
        m = cfg["simulator"]["map"]
        self.name: str = m["name"]
        self.floor: int = int(m["floor"])
        self.res: float = float(m.get("grid_resolution_m", 0.25))

        b = m["bounds"]
        self.x_min, self.x_max = float(b["x_min"]), float(b["x_max"])
        self.y_min, self.y_max = float(b["y_min"]), float(b["y_max"])

        self.start = m["start"]
        self.destination: str = m["destination"]
        self.zones: list[dict[str, Any]] = list(m["zones"])
        self.landmarks: list[dict[str, Any]] = list(m["route_landmarks"])
        # 规划路径需要与墙保持的安全间隙（人 + 盲杖的通行宽度）
        self.path_clearance: float = float(cfg["simulator"]["motion"].get("path_clearance_m", 0.45))

        # --- 建栅格 ---
        self.nx = int(round((self.x_max - self.x_min) / self.res))
        self.ny = int(round((self.y_max - self.y_min) / self.res))

        # 圆形静态物体（桌/椅/沙发/箱子…）
        circles: list[MapObject] = [
            MapObject(
                type=o["type"],
                x=float(o["x"]),
                y=float(o["y"]),
                radius=float(o.get("radius", 0.3)),
                blocking=bool(o.get("blocking", True)),
            )
            for o in m.get("static_objects", [])
        ]
        # 区域型障碍（施工围挡 / 储物柜墙 / 拥堵人流区…）：轴对齐矩形
        #   config 里写 x_min/x_max/y_min/y_max，这里换算成"中心 + 半宽半高"，
        #   让上层（语义层、快照、前端）只需认识一种物体结构。
        areas: list[MapObject] = []
        for a in m.get("area_objects", []):
            x0, x1 = float(a["x_min"]), float(a["x_max"])
            y0, y1 = float(a["y_min"]), float(a["y_max"])
            areas.append(
                MapObject(
                    type=a["type"],
                    x=(x0 + x1) / 2.0,
                    y=(y0 + y1) / 2.0,
                    radius=0.0,
                    blocking=bool(a.get("blocking", True)),
                    hx=(x1 - x0) / 2.0,
                    hy=(y1 - y0) / 2.0,
                )
            )
        self.objects: list[MapObject] = circles + areas

        self.walkable = self._build_walkable()
        self.blocked = ~self.walkable
        # 预计算：栅格上每个可通行点到最近墙体的距离（用于快速估算通道宽度）
        self._wall_dist = self._compute_wall_distance()
        self._obj_dist = self._compute_object_distance()  # 到最近“阻挡型静态物体”的距离

    # -----------------------------------------------------------------
    # 栅格构建
    # -----------------------------------------------------------------
    def _build_walkable(self) -> np.ndarray:
        """用矩形区域的并集生成可通行掩膜。"""
        xs = self.x_min + (np.arange(self.nx) + 0.5) * self.res
        ys = self.y_min + (np.arange(self.ny) + 0.5) * self.res
        gx, gy = np.meshgrid(xs, ys)  # shape (ny, nx)

        mask = np.zeros((self.ny, self.nx), dtype=bool)
        for z in self.zones:
            inside = (
                (gx >= float(z["x_min"]))
                & (gx <= float(z["x_max"]))
                & (gy >= float(z["y_min"]))
                & (gy <= float(z["y_max"]))
            )
            mask |= inside

        # 扣除阻挡型静态物体：圆形按半径，区域型按矩形（两者都要真实挡路）
        for obj in self.objects:
            if not obj.blocking:
                continue
            if obj.is_area:
                inside = (
                    (gx >= obj.x - obj.hx)
                    & (gx <= obj.x + obj.hx)
                    & (gy >= obj.y - obj.hy)
                    & (gy <= obj.y + obj.hy)
                )
            else:
                r = obj.radius
                inside = (gx - obj.x) ** 2 + (gy - obj.y) ** 2 <= r * r
            mask &= ~inside

        return mask

    def _compute_wall_distance(self) -> np.ndarray:
        """精确到墙距离（Chamfer 两遍扫描，足够室内导航使用）。"""
        inf = 1e9
        d = np.where(self.walkable, inf, 0.0)
        diag = self.res * math.sqrt(2)

        # 正向扫描
        for i in range(self.ny):
            for j in range(self.nx):
                v = d[i, j]
                if i > 0:
                    v = min(v, d[i - 1, j] + self.res)
                    if j > 0:
                        v = min(v, d[i - 1, j - 1] + diag)
                    if j < self.nx - 1:
                        v = min(v, d[i - 1, j + 1] + diag)
                if j > 0:
                    v = min(v, d[i, j - 1] + self.res)
                d[i, j] = v
        # 反向扫描
        for i in range(self.ny - 1, -1, -1):
            for j in range(self.nx - 1, -1, -1):
                v = d[i, j]
                if i < self.ny - 1:
                    v = min(v, d[i + 1, j] + self.res)
                    if j > 0:
                        v = min(v, d[i + 1, j - 1] + diag)
                    if j < self.nx - 1:
                        v = min(v, d[i + 1, j + 1] + diag)
                if j < self.nx - 1:
                    v = min(v, d[i, j + 1] + self.res)
                d[i, j] = v
        return d

    def _compute_object_distance(self) -> np.ndarray:
        """到最近阻挡型静态物体的距离（无阻挡物体时为 -1）。"""
        blockers = [o for o in self.objects if o.blocking]
        if not blockers:
            return np.full((self.ny, self.nx), -1.0)
        xs = self.x_min + (np.arange(self.nx) + 0.5) * self.res
        ys = self.y_min + (np.arange(self.ny) + 0.5) * self.res
        gx, gy = np.meshgrid(xs, ys)
        dist = np.full((self.ny, self.nx), np.inf)
        for o in blockers:
            if o.is_area:
                dx = np.maximum(np.abs(gx - o.x) - o.hx, 0.0)
                dy = np.maximum(np.abs(gy - o.y) - o.hy, 0.0)
                d = np.hypot(dx, dy)
            else:
                d = np.hypot(gx - o.x, gy - o.y) - o.radius
            dist = np.minimum(dist, d)
        dist[~self.walkable] = -1.0
        return dist

    # -----------------------------------------------------------------
    # 坐标转换
    # -----------------------------------------------------------------
    def in_bounds(self, x: float, y: float) -> bool:
        return self.x_min <= x < self.x_max and self.y_min <= y < self.y_max

    def world_to_cell(self, x: float, y: float) -> tuple[int, int]:
        """世界坐标 -> (row=i(对应y), col=j(对应x))。"""
        j = int((x - self.x_min) / self.res)
        i = int((y - self.y_min) / self.res)
        return max(0, min(self.ny - 1, i)), max(0, min(self.nx - 1, j))

    def cell_to_world(self, i: int, j: int) -> tuple[float, float]:
        return (self.x_min + (j + 0.5) * self.res, self.y_min + (i + 0.5) * self.res)

    def is_walkable(self, x: float, y: float) -> bool:
        if not self.in_bounds(x, y):
            return False
        i, j = self.world_to_cell(x, y)
        return bool(self.walkable[i, j])

    def zone_at(self, x: float, y: float) -> str | None:
        """返回该点所属的语义区域名（入口区 / 走廊北段 / 实验室区域 ...）。"""
        for z in self.zones:
            if float(z["x_min"]) <= x <= float(z["x_max"]) and float(z["y_min"]) <= y <= float(z["y_max"]):
                return str(z["name"])
        return None

    # -----------------------------------------------------------------
    # 几何查询（等价于真实系统的 NavMesh / 深度感知查询）
    # -----------------------------------------------------------------
    def free_distance(self, x: float, y: float, angle_deg: float, max_range: float) -> float:
        """从 (x,y) 沿 angle_deg 射线前进，返回撞到墙或阻挡物体的距离。"""
        rad = math.radians(angle_deg)
        dx, dy = math.sin(rad), math.cos(rad)
        step = self.res * 0.5
        dist = 0.0
        while dist < max_range:
            dist += step
            px, py = x + dx * dist, y + dy * dist
            if not self.is_walkable(px, py):
                return max(0.0, dist - step)  # 回退半格，避免报出“已进入墙内”的距离
        return max_range

    def sector_distance(
        self, x: float, y: float, heading_deg: float, fov_deg: float, max_range: float, n_rays: int = 9
    ) -> float:
        """扇形视场内最近障碍距离（模拟 LiDAR 前向视场）。"""
        if n_rays <= 1:
            return self.free_distance(x, y, heading_deg, max_range)
        half = fov_deg / 2.0
        best = max_range
        for k in range(n_rays):
            a = heading_deg - half + fov_deg * k / (n_rays - 1)
            best = min(best, self.free_distance(x, y, a, max_range))
        return best

    def corridor_width(self, x: float, y: float, heading_deg: float, max_range: float) -> float:
        """左右两侧到墙的距离之和，估算当前可通行宽度。"""
        left = self.free_distance(x, y, heading_deg - 90.0, max_range)
        right = self.free_distance(x, y, heading_deg + 90.0, max_range)
        return left + right

    def distance_to_nearest_object(self, x: float, y: float) -> tuple[float, MapObject] | None:
        """返回最近的阻挡型静态物体及其表面距离。"""
        blockers = [o for o in self.objects if o.blocking]
        if not blockers:
            return None
        best_o: MapObject | None = None
        best_d = float("inf")
        for o in blockers:
            d = o.distance_to(x, y)
            if d < best_d:
                best_d, best_o = d, o
        if best_o is None:
            return None
        return max(0.0, best_d), best_o

    # -----------------------------------------------------------------
    # A* 寻路
    # -----------------------------------------------------------------
    def find_path(
        self,
        start: tuple[float, float],
        goal: tuple[float, float],
        extra_blocked: Iterable[tuple[float, float]] | None = None,
        obstacle_radius: float = 0.35,
        clearance: float | None = None,
    ) -> list[tuple[float, float]]:
        """在栅格上做 A*，返回世界坐标路径点（含起点与终点）。

        extra_blocked: 动态障碍中心列表，按 obstacle_radius 膨胀成临时障碍。
        clearance:    路径与墙体保持的安全间隙，默认取 map.path_clearance。
                      没有间隙会导致路径贴着墙角走（人根本过不去）。
        找不到路径时返回空列表。
        """
        grid = self.build_blocked_grid(extra_blocked, obstacle_radius, clearance)
        return self.find_path_on(grid, start, goal)

    def build_blocked_grid(
        self,
        extra_blocked: Iterable[tuple[float, float]] | None = None,
        obstacle_radius: float = 0.35,
        clearance: float | None = None,
    ) -> np.ndarray:
        """生成寻路用的阻挡掩膜：墙体 + 安全间隙 + 膨胀后的动态障碍。"""
        clr = self.path_clearance if clearance is None else clearance
        grid = (self.blocked | (self._wall_dist < clr)).copy()

        for (ox, oy) in extra_blocked or ():
            ci, cj = self.world_to_cell(ox, oy)
            rad_cells = int(math.ceil(obstacle_radius / self.res))
            i0, i1 = max(0, ci - rad_cells), min(self.ny - 1, ci + rad_cells)
            j0, j1 = max(0, cj - rad_cells), min(self.nx - 1, cj + rad_cells)
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    wx, wy = self.cell_to_world(i, j)
                    if math.hypot(wx - ox, wy - oy) <= obstacle_radius + clr:
                        grid[i, j] = True
        return grid

    def find_path_on(
        self, grid: np.ndarray, start: tuple[float, float], goal: tuple[float, float]
    ) -> list[tuple[float, float]]:
        """在给定的阻挡掩膜上跑 A*。"""
        si, sj = self.world_to_cell(*start)
        gi, gj = self.world_to_cell(*goal)

        # 起点/终点被间隙或障碍压住时，就近找一个可通行格
        si, sj = self._nearest_free(grid, si, sj)
        gi, gj = self._nearest_free(grid, gi, gj)

        if (si, sj) == (gi, gj):
            return [start, goal]

        open_heap: list[tuple[float, int, int]] = [(0.0, si, sj)]
        g_score: dict[tuple[int, int], float] = {(si, sj): 0.0}
        came: dict[tuple[int, int], tuple[int, int]] = {}
        closed: set[tuple[int, int]] = set()

        while open_heap:
            _, i, j = heapq.heappop(open_heap)
            if (i, j) in closed:
                continue
            closed.add((i, j))
            if (i, j) == (gi, gj):
                break
            for di, dj, cost in _NEIGHBORS:
                ni, nj = i + di, j + dj
                if not (0 <= ni < self.ny and 0 <= nj < self.nx):
                    continue
                if grid[ni, nj]:
                    continue
                # 禁止对角穿角：斜向移动的两个正交邻格都必须可通行
                if di != 0 and dj != 0 and (grid[i + di, j] or grid[i, j + dj]):
                    continue
                ng = g_score[(i, j)] + cost
                if ng < g_score.get((ni, nj), float("inf")):
                    g_score[(ni, nj)] = ng
                    came[(ni, nj)] = (i, j)
                    h = math.hypot(ni - gi, nj - gj)
                    heapq.heappush(open_heap, (ng + h, ni, nj))

        if (gi, gj) not in g_score:
            return []

        # 回溯
        cells: list[tuple[int, int]] = [(gi, gj)]
        cur = (gi, gj)
        while cur in came:
            cur = came[cur]
            cells.append(cur)
        cells.reverse()

        raw = [self.cell_to_world(i, j) for i, j in cells]
        raw[0] = start          # 用真实起点替换栅格中心
        raw[-1] = goal
        return self._smooth(raw, grid)

    def _nearest_free(self, grid: np.ndarray, i: int, j: int, max_r: int = 6) -> tuple[int, int]:
        """在 (i,j) 附近螺旋搜索一个可通行格。"""
        if not grid[i, j]:
            return i, j
        for r in range(1, max_r + 1):
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    ni, nj = i + di, j + dj
                    if 0 <= ni < self.ny and 0 <= nj < self.nx and not grid[ni, nj]:
                        return ni, nj
        return i, j  # 找不到就原样返回，交给上层判定失败

    def _line_free(self, a: tuple[float, float], b: tuple[float, float], grid: np.ndarray) -> bool:
        """两点之间栅格是否直线可达（用于路径平滑）。"""
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / (self.res * 0.5)))
        for k in range(n + 1):
            t = k / n
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            i, j = self.world_to_cell(x, y)
            if grid[i, j]:
                return False
        return True

    def _smooth(self, path: list[tuple[float, float]], grid: np.ndarray) -> list[tuple[float, float]]:
        """贪心视线平滑：让路径更接近自然行走轨迹。"""
        if len(path) <= 2:
            return path
        out = [path[0]]
        i = 0
        while i < len(path) - 1:
            j = len(path) - 1
            while j > i + 1 and not self._line_free(path[i], path[j], grid):
                j -= 1
            out.append(path[j])
            i = j
        return out

    # -----------------------------------------------------------------
    # 辅助
    # -----------------------------------------------------------------
    def landmarks_by_name(self) -> dict[str, dict[str, Any]]:
        return {str(p["name"]): p for p in self.landmarks}

    def route_names(self) -> list[str]:
        return [str(p["name"]) for p in self.landmarks]

    # -----------------------------------------------------------------
    # 地图连通性体检（改完地图必看）
    # -----------------------------------------------------------------
    def _flood_from(self, grid: np.ndarray, si: int, sj: int) -> np.ndarray:
        """在阻挡栅格 `grid`（True=不可通行）上做与 A* 同规则的连通洪泛。

        规则必须和 `find_path_on()` 完全一致（8 邻域 + 禁止对角穿角），否则会出现
        "体检说不可达、A* 却能找到路"这种自相矛盾的结论，反而更难排查。
        """
        seen = np.zeros((self.ny, self.nx), dtype=bool)
        if grid[si, sj]:
            si, sj = self._nearest_free(grid, si, sj)
        if grid[si, sj]:
            return seen
        stack = [(si, sj)]
        seen[si, sj] = True
        while stack:
            i, j = stack.pop()
            for di, dj, _cost in _NEIGHBORS:
                ni, nj = i + di, j + dj
                if not (0 <= ni < self.ny and 0 <= nj < self.nx):
                    continue
                if seen[ni, nj] or grid[ni, nj]:
                    continue
                if di != 0 and dj != 0 and (grid[i + di, j] or grid[i, j + dj]):
                    continue
                seen[ni, nj] = True
                stack.append((ni, nj))
        return seen

    def door_effective_widths(self, clearance: float | None = None) -> dict[str, float]:
        """每个门洞在**寻路栅格**上的实际可通行宽度（米）。

        门洞只有 1m 厚，扣掉两侧 `path_clearance` 后真正能过的宽度远小于名义门宽；
        更糟的是家具挪动后门可能被挡掉一半。这里取门洞内沿门宽方向的
        **最长连续可通行段** —— 名义 2.6m 的门被家具吃掉一半，就该报出 1.2m。

        采样严格按**栅格中心**取（不是按门的几何尺寸等分），否则会出现半格相位差，
        同一个门在不同地图分辨率下报出的宽度能差一格。返回值单位是格宽 × res，
        即"栅格意义上的可通行段长度"，会向上取整到整格。
        """
        grid = self.build_blocked_grid(clearance=clearance)
        out: dict[str, float] = {}
        for z in self.zones:
            name = str(z["name"])
            if not name.endswith(("门", "门口")):
                continue
            x0, x1 = float(z["x_min"]), float(z["x_max"])
            y0, y1 = float(z["y_min"]), float(z["y_max"])
            i0, j0 = self.world_to_cell(x0, y0)
            i1, j1 = self.world_to_cell(x1, y1)
            rows, cols = i1 - i0 + 1, j1 - j0 + 1
            free = np.zeros((rows, cols), dtype=bool)
            for r, i in enumerate(range(i0, i1 + 1)):
                for c, j in enumerate(range(j0, j1 + 1)):
                    wx, wy = self.cell_to_world(i, j)
                    # 只统计格中心落在门洞矩形内的格；门外的格直接记 False
                    if x0 <= wx <= x1 and y0 <= wy <= y1:
                        free[r, c] = not grid[i, j]
            best = 0
            if (x1 - x0) >= (y1 - y0):          # 门宽方向 = 矩形长边
                for r in range(rows):
                    best = max(best, _longest_run(free[r].tolist()))
            else:
                for c in range(cols):
                    best = max(best, _longest_run(free[:, c].tolist()))
            out[name] = round(best * self.res, 2)
        return out

    def connectivity_report(self, clearance: float | None = None) -> dict[str, Any]:
        """地图连通性体检：房间可达性 / 门洞有效宽度 / 是否有"孤岛"。

        这是数据驱动地图最典型的静默故障兜底 —— 房间被家具悄悄封死时，
        A* 不会报错，只会让智能体在原地打转，从日志上完全看不出原因。
        """
        clr = self.path_clearance if clearance is None else clearance
        grid = self.build_blocked_grid(clearance=clr)
        start = (float(self.start["x"]), float(self.start["y"]))
        reached = self._flood_from(grid, *self.world_to_cell(*start))

        xs = self.x_min + (np.arange(self.nx) + 0.5) * self.res
        ys = self.y_min + (np.arange(self.ny) + 0.5) * self.res
        gx, gy = np.meshgrid(xs, ys)

        rooms: dict[str, bool] = {}
        for z in self.zones:
            name = str(z["name"])
            if "走廊" in name or name.endswith(("门", "门口")):
                continue
            inside = (
                (gx >= float(z["x_min"])) & (gx <= float(z["x_max"]))
                & (gy >= float(z["y_min"])) & (gy <= float(z["y_max"]))
            )
            rooms[name] = bool(np.any(inside & reached))

        # 再在**原始**可通行掩膜上洪泛一次：这里不带安全间隙，纯看几何上
        # 有没有"与主区域完全脱开的小岛"（墙、家具围出来的一小块）。
        raw_seen = self._flood_from(~self.walkable, *self.world_to_cell(*start))
        total_free = int(self.walkable.sum())
        main_free = int((raw_seen & self.walkable).sum())
        orphan = 1.0 - (main_free / total_free) if total_free else 0.0

        return {
            "rooms": rooms,
            "doors": self.door_effective_widths(clr),
            "free_cells": total_free,
            "main_component_cells": main_free,
            "orphan_ratio": round(orphan, 4),
            "clearance": clr,
        }

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "floor": self.floor,
            "size": f"{self.x_max - self.x_min:.0f}x{self.y_max - self.y_min:.0f}m",
            "grid": f"{self.ny}x{self.nx}@{self.res}m",
            "walkable_ratio": round(float(self.walkable.mean()), 3),
            "zones": len(self.zones),
            "objects": len(self.objects),
            "area_objects": sum(1 for o in self.objects if o.is_area),
            "landmarks": len(self.landmarks),
        }


__all__ = ["MapObject", "MapSimulator"]
