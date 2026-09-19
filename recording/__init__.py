"""录放系统（v0.3 Stage 9）—— 一次真实实验，可以反复回放。

    python main.py --serve --record logs/run1   # 边跑边录
    python main.py --mode replay --session recordings/session_20260917_193000

为什么这个能力重要（任务书第十五节的原话）：
    一次真实盲人实验，可以反复测试不同模型 / 不同 Prompt / 不同 policy。

## 分层

    schemas.py          磁盘格式（对外契约，改字段含义要动 SCHEMA_VERSION）
    session_recorder.py 写：把每轮的状态/事件/行动/大模型输入输出追加到 JSONL
    session_player.py   读：**不接模拟器**，把历史状态流重新喂给 Agent

★包 `__init__` 用 PEP 562 惰性导出★
    理由与 `simulator/`、`spatial/` 一致：`session_player` 会（延迟）导入
    `agent.agent_core`，而 `agent.agent_core` 又可能反过来用到本包。
    急切导入容易成环，所以这里只在真正取属性时才 import。
"""

from __future__ import annotations

from typing import Any

from recording.schemas import SCHEMA_VERSION, new_session_id

__all__ = [
    "SCHEMA_VERSION",
    "SessionMeta",
    "SessionPlayer",
    "SessionRecorder",
    "ReplayReport",
    "ReplaySource",
    "StateRecord",
    "ActionRecord",
    "EventRecord",
    "LlmRecord",
    "append_jsonl",
    "new_session_id",
    "read_jsonl",
]

_LAZY: dict[str, tuple[str, str]] = {
    "SessionMeta": ("recording.schemas", "SessionMeta"),
    "StateRecord": ("recording.schemas", "StateRecord"),
    "ActionRecord": ("recording.schemas", "ActionRecord"),
    "EventRecord": ("recording.schemas", "EventRecord"),
    "LlmRecord": ("recording.schemas", "LlmRecord"),
    "append_jsonl": ("recording.schemas", "append_jsonl"),
    "read_jsonl": ("recording.schemas", "read_jsonl"),
    "SessionRecorder": ("recording.session_recorder", "SessionRecorder"),
    "SessionPlayer": ("recording.session_player", "SessionPlayer"),
    "ReplayReport": ("recording.session_player", "ReplayReport"),
    "ReplaySource": ("recording.session_player", "ReplaySource"),
}


def __getattr__(name: str) -> Any:  # PEP 562
    hit = _LAZY.get(name)
    if hit is None:
        raise AttributeError(f"module 'recording' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(hit[0]), hit[1])


def __dir__() -> list[str]:
    return sorted(__all__)
