"""传感器模拟器：给理想几何量加上真实传感器的毛病。

对应关系：
    UWB 定位        -> uwb_measure()    : 高斯噪声
    IMU 姿态        -> imu_heading()    : 朝向噪声
    LiDAR / 深度相机 -> lidar_measure() : 距离噪声 + 丢帧

这里刻意把「真值」与「测量值」分开：模拟器内部用真值推进物理，
交给 Agent 的一律是加了噪声、可能丢帧的测量值。
将来换成真实硬件时，把本文件的实现替换成 SDK 调用即可。
"""

from __future__ import annotations

import random
from typing import Any

from simulator.map_simulator import MapSimulator


class SensorSimulator:
    """传感器噪声与丢帧模型。"""

    def __init__(self, cfg: dict[str, Any], map_sim: MapSimulator, seed: int | None = None) -> None:
        s = cfg["simulator"]["sensors"]
        self.map = map_sim
        self.uwb_noise: float = float(s.get("uwb_noise_m", 0.15))
        self.heading_noise: float = float(s.get("heading_noise_deg", 2.0))
        self.lidar_noise: float = float(s.get("lidar_noise_m", 0.05))
        self.lidar_range: float = float(s.get("lidar_range_m", 5.0))
        self.lidar_fov: float = float(s.get("lidar_fov_deg", 70.0))
        self.front_wall_fov: float = float(s.get("front_wall_fov_deg", 20.0))
        self.side_fov: float = float(s.get("side_fov_deg", 45.0))
        self.side_obstacle_range: float = float(s.get("side_obstacle_range_m", 1.0))
        self.drop_prob: float = float(s.get("drop_frame_prob", 0.02))

        self.rng = random.Random(seed)
        self.drop_count = 0
        self.sample_count = 0

    # -----------------------------------------------------------------
    # 单传感器
    # -----------------------------------------------------------------
    def uwb_measure(self, true_pos: tuple[float, float]) -> tuple[float, float]:
        """UWB 定位测量：真值 + 高斯噪声，结果必定落在可通行区域内。"""
        for _ in range(8):
            x = true_pos[0] + self.rng.gauss(0.0, self.uwb_noise)
            y = true_pos[1] + self.rng.gauss(0.0, self.uwb_noise)
            if self.map.is_walkable(x, y):
                return (x, y)
        # 噪声把人推出墙外时保守处理：直接返回真值
        return true_pos

    def imu_heading(self, true_heading: float) -> float:
        """IMU 朝向测量：真值 + 噪声，归一化到 [0,360)。"""
        return (true_heading + self.rng.gauss(0.0, self.heading_noise)) % 360.0

    def imu_speed(self, true_speed: float) -> float:
        """IMU/步态推算速度：非负，带小幅噪声。"""
        return max(0.0, true_speed + self.rng.gauss(0.0, 0.05))

    def lidar_measure(self, true_distance: float) -> float | None:
        """深度测量：返回带噪声的距离；帧丢失时返回 None。"""
        self.sample_count += 1
        if self.rng.random() < self.drop_prob:
            self.drop_count += 1
            return None
        d = true_distance + self.rng.gauss(0.0, self.lidar_noise)
        return max(0.0, min(self.lidar_range, d))

    def front_scan(self, pos: tuple[float, float], heading: float) -> float | None:
        """用地图真值做一次前向扇形扫描，再过一遍噪声模型。"""
        true_d = self.map.sector_distance(pos[0], pos[1], heading, self.lidar_fov, self.lidar_range)
        return self.lidar_measure(true_d)

    def wall_ahead_scan(self, pos: tuple[float, float], heading: float) -> float | None:
        """只朝正前方（窄视场）测墙：判断走廊是不是走到头了。

        用宽视场会把走廊侧墙也算进来，导致"正常直行"被误判成"前方有障碍"。
        """
        true_d = self.map.sector_distance(
            pos[0], pos[1], heading, self.front_wall_fov, self.lidar_range, n_rays=5
        )
        return self.lidar_measure(true_d)

    def side_scan(self, pos: tuple[float, float], heading: float) -> tuple[float | None, float | None]:
        """左/右侧扫描（用于估算通道宽度）。"""
        left_true = self.map.sector_distance(pos[0], pos[1], heading - 90.0, self.side_fov, self.lidar_range)
        right_true = self.map.sector_distance(pos[0], pos[1], heading + 90.0, self.side_fov, self.lidar_range)
        return self.lidar_measure(left_true), self.lidar_measure(right_true)

    # -----------------------------------------------------------------
    # 质量评估
    # -----------------------------------------------------------------
    def localization_confidence(self) -> float:
        """定位置信度：噪声越大越低。0.15m 噪声约对应 0.9。"""
        return max(0.0, min(1.0, 1.0 - self.uwb_noise / 1.5))

    def perception_confidence(self) -> float:
        """感知置信度：丢帧率越高越低。"""
        if self.sample_count == 0:
            return 1.0
        loss = self.drop_count / self.sample_count
        return max(0.0, min(1.0, 1.0 - loss * 5.0))

    def drop_rate(self) -> float:
        return 0.0 if self.sample_count == 0 else self.drop_count / self.sample_count

    def stats(self) -> dict[str, float]:
        return {
            "uwb_noise_m": self.uwb_noise,
            "heading_noise_deg": self.heading_noise,
            "lidar_noise_m": self.lidar_noise,
            "lidar_range_m": self.lidar_range,
            "drop_rate": round(self.drop_rate(), 4),
            "localization_confidence": round(self.localization_confidence(), 3),
            "perception_confidence": round(self.perception_confidence(), 3),
        }


__all__ = ["SensorSimulator"]
