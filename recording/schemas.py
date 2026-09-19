"""录制 / 回放的数据契约（v0.3 Stage 9）。

任务书第十五节（Phase 12 —— Replay / Recording System）：

    每次实验保存：session_id / timestamp / SpatialState / events /
    camera frame reference / LLM input / LLM response / decision / action /
    latency / error

    **不要保存隐藏 chain-of-thought。**
    **只保存**模型返回的结构化决策和可公开的 reason 字段。

    目录：
        recordings/session_YYYYMMDD_HHMMSS/
            states.jsonl    events.jsonl    actions.jsonl    llm.jsonl
            images/         meta.json       map.json

## 为什么用 JSONL 而不是一个大 JSON

实验动辄几百上千轮。JSONL 可以边跑边追加、崩了也只丢最后一行，还能
`grep`/`head` 直接看；一次 dump 的 JSON 必须写完整才有效，而且几十 MB 起。

## 为什么把 schema 单独一个文件

录制格式一旦被写进磁盘就是**对外契约**：回放器、分析脚本、论文附录都读它。
把"格式"和"写盘动作"分开，改格式时能一眼看到所有受影响的字段，
而不是在一坨 `open()/write()` 里找。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

#: 录制格式版本。**改了字段含义就要动它** —— 回放器据此判断能否读取。
SCHEMA_VERSION = "1.0"

SESSION_DIR_PREFIX = "session_"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]+$")


# =====================================================================
# 时间与 id
# =====================================================================
def utc_now() -> datetime:
    """带本地时区的当前时间（日志里看得懂"几点几分"比 UTC 有用）。"""
    return datetime.now().astimezone()


def new_session_id(now: datetime | None = None) -> str:
    """`session_YYYYMMDD_HHMMSS` —— 目录名天然按时间排序。"""
    t = now or utc_now()
    return f"{SESSION_DIR_PREFIX}{t.strftime('%Y%m%d_%H%M%S')}"


def is_valid_session_id(sid: str) -> bool:
    """只允许安全字符：会话 id 会被拼进路径，必须挡住 `../` 这类穿越。"""
    return bool(sid) and bool(_SAFE_ID.match(sid)) and ".." not in sid


# =====================================================================
# 通用序列化
# =====================================================================
def jsonable(obj: Any) -> Any:
    """把任意对象转成可 JSON 化的结构。

    覆盖本项目里所有"会进录制"的类型：dataclass / pydantic / Enum / datetime /
    bytes（转成 `<N bytes>` 占位，**绝不把图片塞进 JSONL**）。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in asdict(obj).items()}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):  # pydantic v2
        try:
            return jsonable(dump(mode="json"))
        except Exception:  # noqa: BLE001 - 兜底走 __dict__
            return jsonable({k: v for k, v in vars(obj).items() if not k.startswith("_")})
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return repr(obj)


def append_jsonl(path: Path, obj: Any) -> None:
    """追加一行 JSON。**只追加，不重写** —— 崩了也最多丢最后一行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(jsonable(obj), ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读 JSONL。**坏行跳过而不是抛异常**：录制文件常被手工编辑过。"""
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# =====================================================================
# 记录类型
# =====================================================================
@dataclass
class SessionMeta:
    """会话元信息（`meta.json`）。回放器先读它，判断格式是否可读。"""

    session_id: str
    schema_version: str = SCHEMA_VERSION
    created_at: str = ""
    closed_at: str | None = None
    system_version: str = ""
    map_name: str = ""
    map_size: str = ""
    destination: str = ""
    seed: int | None = None
    llm_model: str = ""
    llm_base_url: str = ""
    llm_enabled: bool = False
    notes: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass
class StateRecord:
    """一轮的完整 `SpatialState`（完整 dump，不是给模型看的裁剪视图）。

    ⚠️ 必须是**完整 dump**：回放要能 `SpatialState.model_validate(...)` 往返重建，
       `to_prompt_dict()` 那种裁剪视图会缺字段、validate 直接失败。
    """

    t: float
    tick: int
    zone: str | None
    state: dict[str, Any]


@dataclass
class EventRecord:
    """一条事件（Stage 6 的产出）。"""

    t: float
    tick: int
    event_type: str
    severity: str
    source: str
    subject: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionRecord:
    """一次决策结果。

    `decision` 只存**可公开**的部分（rationale / 信息缺口 / 是否要画面 / 时延），
    **不存**模型的隐藏思维链。
    """

    t: float
    tick: int
    action: dict[str, Any]
    source: str = ""
    llm_used: bool = False
    decision: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    latency_s: float | None = None
    visual_need: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class LlmRecord:
    """一次大模型调用的输入/输出（**不含隐藏思维链**）。

    `request.image_ref` 指向 `images/` 下的文件名 —— 图片本身单独落盘，
    不让 base64 撑爆 JSONL。
    """

    t: float
    tick: int
    kind: str                     # cognitive / tools
    model: str
    ok: bool
    latency_s: float
    error: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ActionRecord",
    "EventRecord",
    "LlmRecord",
    "SCHEMA_VERSION",
    "SESSION_DIR_PREFIX",
    "SessionMeta",
    "StateRecord",
    "append_jsonl",
    "is_valid_session_id",
    "jsonable",
    "new_session_id",
    "read_jsonl",
    "utc_now",
]
