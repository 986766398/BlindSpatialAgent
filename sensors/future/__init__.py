"""阶段三预留位：真实硬件的传感器适配实现（尚未开始）。

按任务书第十节的约束，v0.3 **不接真实硬件**，所以这里只留接口约定，
不写任何会误导人的"看起来能跑"的桩代码。

将来每个文件要满足的契约（都在 `sensors/base.py`）：

    uwb_provider.py      SensorProvider
        read_pose()          从 UWB 定位标签 + IMU 解算位姿
        health()             标签失联 → SensorStatus.LOST
        measure_depth()      直接返回入参（真值即读数，不需要加噪）
        map_snapshot()       预建 3D 地图导出的静态知识
    真机关键差异：**没有 `debug_ground_truth()`**，`WorldStepper` 的
    advance_* 全部是空实现（用户自己在走），`replan()` 只重算路径不移动任何人。

    iphone_lidar_provider.py  SensorProvider（深度部分）
        scan_wall_ahead() / scan_sides() 走 ARKit 深度图反投影
        health() 依据 ARSession 状态

    ue5_twin_provider.py      SensorProvider + WorldStepper
        UE5 数字孪生既是"数据源"也是"世界"（推进仿真时间、生成动态行人）
        debug_ground_truth() 在这里是合法的 —— 孪生本来就有真值

    noop_stepper.py           WorldStepper 的空实现
        真实硬件下用户自己走路，`advance_environment/advance_user` 都是 pass，
        `reset()` 只清空统计。它存在的意义是让装配处可以无条件调用推进方法，
        而不必到处写 `if 是仿真`。
"""

__all__: list[str] = []
