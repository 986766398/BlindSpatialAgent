"""会话录制器（v0.3 Stage 9）。

任务书第十五节：把一次实验完整落盘，之后可以**反复回放**去对比
不同模型 / 不同 Prompt / 不同 policy —— 这是科研与调试的核心能力。
一次真实盲人实验不该只能看一遍。

## 三条设计纪律

1. **录制不得改变行为**。所有挂钩都在 `try/except` 里，失败只写一行 warn 日志；
   录制器挂掉 = 少一份数据，绝不能影响导航主循环。
2. **线程**：`record_llm` 可能在认知工作线程里被调用，而 `record_state/action`
   在主线程。所以内部分段加锁（锁只护住一次 append 与计数），不跨 IO 长持。
3. **图片不进 JSONL**。画面写 `images/`，JSONL 里只留文件名（见 `save_image`）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from recording.schemas import (
    SCHEMA_VERSION,
    ActionRecord,
    EventRecord,
    LlmRecord,
    SessionMeta,
    StateRecord,
    append_jsonl,
    is_valid_session_id,
    jsonable,
    new_session_id,
    utc_now,
)

LOG = logging.getLogger("bsa.recording")

DEFAULT_ROOT = "recordings"


class SessionRecorder:
    """把一轮轮的状态 / 事件 / 行动 / 大模型输入输出写进 `recordings/session_*`。"""

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        root: str | Path | None = None,
        session_id: str | None = None,
        notes: str = "",
        seed: int | None = None,
    ) -> None:
        cfg = cfg or {}
        self.cfg = cfg
        node = (cfg.get("recording", {}) or {})
        self.root = Path(root or node.get("root") or DEFAULT_ROOT)
        sid = session_id or new_session_id()
        if not is_valid_session_id(sid):
            raise ValueError(f"非法 session id：{sid!r}（只允许字母数字与 _.-）")
        self.session_id = sid
        self.notes = notes
        self.seed = seed
        #: 是否连图片一起保存（大图会很快把磁盘吃满：1 Hz × 20 KB ≈ 70 MB/小时）
        self.keep_images = bool(node.get("keep_images", True))
        self.max_images = int(node.get("max_images", 2000))

        self.dir: Path = self.root / sid
        self.started = False
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._img_seq = 0
        self.meta: SessionMeta | None = None
        self._pending_events: int = 0

    # -----------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------
    def start(self) -> Path:
        """创建目录并写 `meta.json`。幂等：重复调用只返回目录。"""
        if self.started:
            return self.dir
        # ⚠️ 对已存在目录调 mkdir(exist_ok=True) 在本项目环境里会抛 EEXIST，
        #    所以显式判存在（历史踩坑，见项目记忆）
        if not self.dir.is_dir():
            self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "images").mkdir(parents=True, exist_ok=True)
        sys_cfg = self.cfg.get("system", {}) or {}
        llm_cfg = self.cfg.get("llm", {}) or {}
        self.meta = SessionMeta(
            session_id=self.session_id,
            schema_version=SCHEMA_VERSION,
            created_at=utc_now().isoformat(timespec="seconds"),
            system_version=str(sys_cfg.get("version", "")),
            seed=self.seed,
            llm_enabled=bool(llm_cfg.get("api_key")),
            llm_model=str(llm_cfg.get("model", "")),
            llm_base_url=str(llm_cfg.get("base_url", "")),
            notes=self.notes,
        )
        self.started = True
        self._write_meta()
        LOG.info("开始录制：%s", self.dir)
        return self.dir

    def attach_map(self, map_info: Any = None) -> None:
        """把静态地图信息记进 `map.json`（回放时 `zone_at()` 要靠它）。

        接受任意形状：`MapSnapshot`（有 model_dump）、它的 dump 字典、
        或 `/api/map` 那种带 `zones/landmarks/bounds` 的字典。
        """
        try:
            import json

            self.start()   # 目录/图片子目录必须先存在（本方法可能在 __init__ 期间就被调用）
            info = jsonable(map_info)
            if not isinstance(info, dict):
                info = {}
            bounds = info.get("bounds") if isinstance(info.get("bounds"), dict) else {}
            size = str(info.get("size") or "")
            if not size and bounds:
                try:
                    size = (
                        f"{float(bounds.get('x_max', 0)) - float(bounds.get('x_min', 0)):.0f}"
                        f"x{float(bounds.get('y_max', 0)) - float(bounds.get('y_min', 0)):.0f}m"
                    )
                except (TypeError, ValueError):
                    size = ""
            if self.meta is not None:
                self.meta.map_name = str(info.get("name", ""))
                self.meta.map_size = size
                self.meta.destination = str(info.get("destination", ""))
            # 完整 map snapshot 单独一份：回放器需要 zones/landmarks 才能回答 zone_at
            path = self.dir / "map.json"
            path.write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._write_meta()
        except Exception as e:  # noqa: BLE001
            LOG.warning("记录地图快照失败：%s", e)

    def close(self) -> dict[str, Any]:
        """收尾：写 closed_at 与计数。**幂等**。"""
        if not self.started:
            return self.stats()
        self._write_meta(closed=True)
        self.started = False
        LOG.info("录制结束：%s（%s）", self.dir, self._counts)
        return self.stats()

    # -----------------------------------------------------------------
    # 记录
    # -----------------------------------------------------------------
    def record_state(
        self,
        state: Any,
        *,
        elapsed: float | None = None,
        tick: int | None = None,
        zone: str | None = None,
    ) -> None:
        try:
            self.start()
            t = float(elapsed if elapsed is not None else getattr(state, "tick", 0.0))
            rec = StateRecord(
                t=round(t, 3),
                tick=int(tick if tick is not None else getattr(state, "tick", 0)),
                zone=zone,
                # 完整 dump（不是给模型看的裁剪视图）—— 回放要能往返重建
                state=jsonable(state.model_dump(mode="json")),
            )
            with self._lock:
                append_jsonl(self.dir / "states.jsonl", rec)
                self._bump("states")
        except Exception as e:  # noqa: BLE001 - 录制失败绝不能影响主循环
            LOG.warning("录制状态失败：%s", e)

    def record_events(self, events: Iterable[Any] | None, *, elapsed: float, tick: int) -> int:
        """记录一批事件，返回写入条数。"""
        n = 0
        try:
            self.start()
            for e in events or []:
                rec = EventRecord(
                    t=round(float(elapsed), 3),
                    tick=int(tick),
                    event_type=str(getattr(getattr(e, "event_type", None), "value", "") or ""),
                    severity=str(getattr(getattr(e, "severity", None), "value", "") or ""),
                    source=str(getattr(e, "source", "") or ""),
                    subject=str(e.key()) if hasattr(e, "key") else "",
                    payload=jsonable(getattr(e, "payload", {}) or {}),
                )
                with self._lock:
                    append_jsonl(self.dir / "events.jsonl", rec)
                    self._bump("events")
                n += 1
        except Exception as e:  # noqa: BLE001
            LOG.warning("录制事件失败：%s", e)
        return n

    def record_action(
        self,
        action: Any,
        *,
        elapsed: float,
        tick: int,
        source: str = "",
        llm_used: bool = False,
        decision: Any | None = None,
        gate: Any | None = None,
        latency_s: float | None = None,
        visual_need: Any | None = None,
        error: str | None = None,
    ) -> None:
        try:
            self.start()
            as_dict = getattr(action, "as_dict", None)
            rec = ActionRecord(
                t=round(float(elapsed), 3),
                tick=int(tick),
                action=jsonable(as_dict() if callable(as_dict) else action),
                source=str(source or getattr(action, "source", "") or ""),
                llm_used=bool(llm_used),
                decision=self._public_decision(decision),
                gate=self._public_gate(gate),
                latency_s=None if latency_s is None else round(float(latency_s), 3),
                visual_need=jsonable(visual_need) if visual_need else None,
                error=error,
            )
            with self._lock:
                append_jsonl(self.dir / "actions.jsonl", rec)
                self._bump("actions")
        except Exception as e:  # noqa: BLE001
            LOG.warning("录制行动失败：%s", e)

    def record_llm(
        self,
        *,
        kind: str,
        model: str,
        ok: bool,
        latency_s: float,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        elapsed: float = 0.0,
        tick: int = 0,
        image: bytes | None = None,
        image_tag: str = "cognitive",
    ) -> None:
        """记录一次大模型调用。

        ★只存模型返回的结构化决策与可公开 reason，**不存隐藏思维链**★
          实现方式：`response` 只放解析后的 JSON（`_parse()` 的产物）与模型的
          `content` 文本，绝不落 SDK 消息对象里的 `reasoning_content` 等字段。
        """
        try:
            self.start()
            req = dict(request or {})
            if image:
                ref = self.save_image(image, tag=image_tag)
                req["image_ref"] = ref
                req["image_bytes"] = len(image)
            else:
                req["image_ref"] = None
            rec = LlmRecord(
                t=round(float(elapsed), 3),
                tick=int(tick),
                kind=str(kind),
                model=str(model),
                ok=bool(ok),
                latency_s=round(float(latency_s), 3),
                error=error,
                request=jsonable(req),
                response=jsonable(response or {}),
            )
            with self._lock:
                append_jsonl(self.dir / "llm.jsonl", rec)
                self._bump("llm")
        except Exception as e:  # noqa: BLE001
            LOG.warning("录制大模型调用失败：%s", e)

    # -----------------------------------------------------------------
    def save_image(self, data: bytes, *, tag: str = "frame", ext: str = "jpg") -> str | None:
        """把一帧画面写进 `images/`，返回**相对文件名**（写进 JSONL 的引用）。

        ⚠️ 返回相对名而不是绝对路径：会话目录可能被移动/打包/上传，
           绝对路径一挪就失效。回放器用 `session_dir / ref` 去还原。
        """
        if not self.keep_images or not data:
            return None
        try:
            self.start()
            with self._lock:
                if self._img_seq >= self.max_images:
                    return None
                self._img_seq += 1
                seq = self._img_seq
            name = f"{tag}_{seq:05d}.{ext}"
            (self.dir / "images" / name).write_bytes(data)
            with self._lock:
                self._bump("images")
            return name
        except Exception as e:  # noqa: BLE001
            LOG.warning("保存画面失败：%s", e)
            return None

    def note(self, key: str, value: Any) -> None:
        """往 meta.extra 里记一条实验备注（例如"这次换了 prompt v3"）。"""
        if self.meta is None:
            self.start()
        if self.meta is not None:
            self.meta.extra[str(key)] = jsonable(value)
            self._write_meta()

    # -----------------------------------------------------------------
    # 内部
    # -----------------------------------------------------------------
    def _bump(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    def _public_decision(self, decision: Any | None) -> dict[str, Any] | None:
        """把 `AgentDecision` 收敛成"可公开"的字段集合。

        ⚠️ 白名单而不是黑名单：黑名单（"去掉 reasoning_content"）在 SDK 加字段时
           会静默泄露；白名单则默认什么都不泄露。
        """
        if decision is None:
            return None
        out: dict[str, Any] = {}
        for k in (
            "rationale",
            "information_gaps",
            "needs_visual",
            "model",
            "latency_s",
            "prompt_chars",
            "image_attached",
        ):
            if hasattr(decision, k):
                out[k] = jsonable(getattr(decision, k))
        act = getattr(decision, "action", None)
        if act is not None:
            out["action_type"] = str(getattr(getattr(act, "action_type", None), "value", ""))
            out["reason"] = str(getattr(act, "reason", "") or "")
        return out

    def _public_gate(self, gate: Any | None) -> dict[str, Any] | None:
        if gate is None:
            return None
        out: dict[str, Any] = {}
        for k in ("speak", "discard", "code", "reason", "channel"):
            if hasattr(gate, k):
                v = getattr(gate, k)
                out[k] = str(getattr(v, "value", v))
        return out or None

    def _write_meta(self, *, closed: bool = False) -> None:
        if self.meta is None:
            return
        try:
            import json

            self.meta.counts = dict(self._counts)
            if closed:
                self.meta.closed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            (self.dir / "meta.json").write_text(
                json.dumps(self.meta.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            LOG.warning("写 meta.json 失败：%s", e)

    # -----------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.started

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.started,
            "session_id": self.session_id,
            "dir": str(self.dir),
            "root": str(self.root),
            "keep_images": self.keep_images,
            "counts": self.counts(),
        }


# 供类型标注用的占位（避免把 Any 传进来时的名字困惑）
__all__ = ["DEFAULT_ROOT", "SessionRecorder"]
