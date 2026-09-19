"""世界模型（兼容转出层）。

★v0.3 Stage 5 起，世界模型已经搬到 `world_model/` 包★

    spatial/world_model.py  （本文件）  只是转出，保证 v0.2 的
        `from spatial.world_model import WorldModel` 继续可用；
    world_model/                         才是真正的实现，按时间尺度分五层。

为什么保留这个文件而不是把 import 全改一遍：
    `spatial/state_manager.py`、`agent/agent_core.py`、`agent/tools.py`
    与自检都写着 `from spatial.world_model import WorldModel`。
    改这些 import 不增加任何能力，却会让 diff 里混进大量无意义的行 ——
    与 Stage 4 保留 `StateManager` 门面同理。

新代码请直接 `from world_model import WorldModel`（或按需引入具体记忆层）。
"""

from __future__ import annotations

from world_model.object_memory import ObstacleMemory, TrackStats
from world_model.world_model import WorldModel

__all__ = ["ObstacleMemory", "TrackStats", "WorldModel"]
