"""模拟器层：替代真实硬件产生数据流（阶段一无硬件）。

Step 3 落地文件：
    map_simulator.py        —— 室内二维地图、语义物体、可通行区域
    navigation_simulator.py —— 路径规划 + 用户沿路线移动
    sensor_simulator.py     —— UWB / IMU / LiDAR 噪声与丢帧
    obstacle_simulator.py   —— 动态障碍（椅子被推入、行人经过、通道变窄）
"""
