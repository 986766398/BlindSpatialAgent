"""融合层（State Fusion Layer）—— 多源观测 → 一个可被 Agent 消费的空间状态。

分层位置（任务书第二节）：

    Sensor Adapter (sensors/)
          ↓
    **State Fusion（本包）**
          ↓
    Spatial World Model (world_model/)  ← 同时由本层写入
          ↓
    Event Engine → Safety Layer → Cognitive Agent → Interaction Policy

三个模块的分工：

    freshness.py      纯函数：这条读数过期了没有（含通道阈值策略）
    confidence.py     有状态：这条读数有多可信（噪声纸面值 + 实测残差 + 新鲜度）
    state_fusion.py   组装：按固定顺序调用 provider，把算子结果拼成 SpatialState

★为什么"组装"和"算子"必须分开★
    `state_fusion` 里对 provider 的调用顺序**就是随机数消耗顺序**，
    一旦被无关逻辑打散，同一 seed 下的仿真轨迹就不再可复现。
    把纯计算挪进 freshness/confidence 之后，组装函数只剩下"按顺序读、按顺序拼"，
    回归测试（逐轮行为快照比对）才有意义。

本包对上层暴露的主要入口是 `StateFusion`；`spatial/state_manager.py` 是它的
兼容门面（v0.2 的类名与私有方法名继续可用，前端/测试/工具零改动）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ChannelFreshness",
    "ConfidenceEstimator",
    "FreshnessPolicy",
    "FreshnessReading",
    "StateFusion",
]

# ---------------------------------------------------------------------
# PEP 562 惰性导出：`fusion.state_fusion` 会 import `spatial.world_model`，
# 而 `spatial/__init__.py` 又可能间接引用本包。急切导入会成环，
# 因此统一改成访问时再导入（与项目内其它包一致）。
# ---------------------------------------------------------------------
_LAZY: dict[str, str] = {
    "ChannelFreshness": "fusion.freshness",
    "FreshnessPolicy": "fusion.freshness",
    "FreshnessReading": "fusion.freshness",
    "ConfidenceEstimator": "fusion.confidence",
    "StateFusion": "fusion.state_fusion",
}


def __getattr__(name: str) -> Any:  # pragma: no cover - 导入机制
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module), name)


def __dir__() -> list[str]:  # pragma: no cover - 便于交互式补全
    return sorted(__all__)
