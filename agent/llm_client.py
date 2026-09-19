"""实时多模态大模型接口层。

设计目标：**模型可替换，Agent 不变。**

    chat_multimodal(state, image, user_query) -> dict | None

统一走 OpenAI 兼容协议（/chat/completions），因此以下都能直接用：
    - OpenAI GPT-4o / GPT-Realtime
    - 通义千问 Qwen-Omni（dashscope 兼容模式）
    - Gemini（OpenAI 兼容端点）
    - 本地 vLLM / Ollama

关键约定：
1. 送进去的**不是原始传感器数据**，而是 prompt_template 渲染出的结构化文本状态 + 一张图。
2. 支持 function calling；模型调用工具后由 AgentTools 执行并把结果回灌。
3. 任何失败（无 Key、超时、限流、非法 JSON）都返回 None，由规则引擎兜底 —— 绝不抛异常打断主循环。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from agent.prompt_template import SYSTEM_PROMPT, build_user_prompt, extract_json
from agent.tools import AgentTools
from spatial.spatial_state import SpatialState

LOG = logging.getLogger("bsa.llm")


class LLMClient:
    """OpenAI 兼容的多模态对话客户端（含工具调用循环）。"""

    # --- 熔断冷却时长（秒）------------------------------------------------
    # 设计动机：Key 无效 / 模型名错误这类失败**不会自愈**。若不熔断，主循环会以
    # 1 Hz 无限重试：白白消耗配额与实时性，还会把日志刷爆（实测 600 轮稳定性
    # 测试因此跑了数分钟并被误判为"卡死"）。网络抖动同理，也应退避而非每帧硬撞。
    COOLDOWN_AUTH = 600.0        # 认证失败：长时间静默，等人工修 Key
    COOLDOWN_NOT_FOUND = 600.0   # base_url / 模型名错误：同理
    COOLDOWN_RATE_LIMIT = 30.0   # 限流或额度不足：歇一会儿再试
    COOLDOWN_NETWORK = 15.0      # 网络抖动：短暂退避

    def __init__(self, cfg: dict[str, Any], tools: AgentTools | None = None) -> None:
        self.cfg = cfg
        self.tools = tools
        c = cfg["llm"]
        self.provider: str = c.get("provider", "openai_compatible")
        self.base_url: str = c.get("base_url", "")
        self.api_key: str = c.get("api_key", "") or ""
        self.model: str = c.get("model", "")
        self.temperature: float = float(c.get("temperature", 0.2))
        self.max_tokens: int = int(c.get("max_tokens", 512))
        self.timeout: float = float(c.get("timeout_s", 30))
        # ★P0 修复（v0.3 Stage 7 实测确认）★
        # openai SDK 的 `max_retries` **默认是 2**。也就是说一次"看起来 30 秒超时"
        # 的调用最坏会变成 3 次尝试 ≈ 90 秒；叠加工具调用轮次后单轮 step 最坏 360 秒。
        # 对实时系统这是不可接受的：用户会站在走廊里等 1 分钟。
        # 显式设 0，把重试交给上层的熔断机制（它更懂"该退避多久"）。
        self.max_retries: int = int(c.get("max_retries", 0))
        # 单轮决策的总预算：超过就放弃这次推理，让规则基线兜底（快循环不受影响）。
        self.total_deadline_s: float = float(c.get("total_deadline_s", self.timeout * 2))
        self.use_tools: bool = bool(c.get("enable_tool_calling", True))
        self.max_tool_rounds: int = int(c.get("max_tool_rounds", 3))

        self._client: Any | None = None
        self.calls: int = 0
        self.failures: int = 0
        self.tool_call_total: int = 0
        self.last_latency: float = 0.0
        self.last_error: str = ""
        self.last_tool_calls: list[Any] = []

        # ★Stage 9 录制★ 可选挂载：`SessionRecorder`（鸭子类型，只要有 record_llm 就行）。
        #   默认 None = 完全不录制，行为与 v0.2 逐位相同。
        self.recorder: Any | None = None
        #: 供录制打时间戳的（仿真秒, tick）。由主循环每轮写入。
        self.record_clock: tuple[float, int] = (0.0, 0)

        # 熔断状态
        self.consecutive_failures: int = 0
        self.disabled_until: float = 0.0
        self.disabled_reason: str = ""
        self.circuit_trips: int = 0

    # -----------------------------------------------------------------
    # 可用性
    # -----------------------------------------------------------------
    @property
    def available(self) -> bool:
        """是否具备调用真实大模型的条件（有 key + 有模型名）。"""
        return bool(self.api_key and self.model)

    @property
    def circuit_open(self) -> bool:
        """熔断中？连续失败后暂停发起请求，避免无意义地反复撞墙。"""
        return time.time() < self.disabled_until

    @property
    def usable(self) -> bool:
        """真正可以发起调用的判定：已配置 **且** 未熔断。

        `decide()` 用这个而不是 `available`，否则熔断形同虚设。
        """
        return self.available and not self.circuit_open

    def _note_success(self) -> None:
        """一次成功即宣告服务恢复，清空熔断与连续失败计数。"""
        self.consecutive_failures = 0
        if self.disabled_until or self.disabled_reason:
            LOG.info("大模型已恢复，解除熔断")
        self.disabled_until = 0.0
        self.disabled_reason = ""

    def _note_failure(self, exc: BaseException) -> None:
        """登记一次失败，并按原因决定熔断冷却时长。"""
        self.consecutive_failures += 1
        text = f"{type(exc).__name__}: {exc}".lower()

        if any(k in text for k in ("authentication", "invalid_api_key", "401", "403", "permission")):
            cooldown, reason = self.COOLDOWN_AUTH, "认证失败（Key 无效，或与该 base_url 的服务商不匹配）"
        elif any(k in text for k in ("notfound", "not_found", "404", "does not exist", "unknown model")):
            cooldown, reason = self.COOLDOWN_NOT_FOUND, "接口路径或模型名不存在"
        elif any(k in text for k in ("ratelimit", "rate_limit", "429", "quota", "insufficient")):
            cooldown, reason = self.COOLDOWN_RATE_LIMIT, "限流或额度不足"
        else:
            cooldown, reason = self.COOLDOWN_NETWORK, "网络或服务端异常"

        self.disabled_until = time.time() + cooldown
        self.disabled_reason = reason
        self.circuit_trips += 1
        LOG.warning(
            "大模型熔断 %.0fs：%s（连续失败 %d 次，累计熔断 %d 次）",
            cooldown, reason, self.consecutive_failures, self.circuit_trips,
        )

    def _ensure_client(self) -> Any:
        """惰性创建客户端；openai 未安装或参数错误时抛异常给上层捕获。"""
        if self._client is None:
            from openai import OpenAI  # 延迟导入：没装也不会影响规则模式运行

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url or None,
                timeout=self.timeout,
                max_retries=self.max_retries,  # 显式 0：把重试决策交给熔断器
            )
        return self._client

    # -----------------------------------------------------------------
    # 主接口
    # -----------------------------------------------------------------
    def chat_multimodal(
        self,
        state: SpatialState,
        image: bytes | None = None,
        user_query: str | None = None,
        extra: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        """一次完整的多模态决策调用。返回解析后的 Action dict；失败返回 None。"""
        # usable = 已配置 且 未熔断；熔断期间直接跳过，不再发起真实请求
        if not self.usable:
            return None

        # 每次调用重新收集本轮工具调用（否则上一轮的会串到这一轮的 Action 里）
        self.last_tool_calls = []

        try:
            client = self._ensure_client()
            messages = self._build_messages(state, image, user_query, extra, history)
            schemas = self.tools.schemas() if (self.tools and self.use_tools) else None
            # 供 Stage 9 录制：把"送进去的是什么"整理成可公开的摘要（图走 images/，不塞 JSON）
            req = self._request_summary(messages, image, schemas, history)

            rounds = 0
            started = time.time()
            last_content: str | None = None
            while True:
                # ★总预算★：工具调用可能来回多轮，每轮都有自己的超时，
                # 累加起来可能远超"单轮 timeout"。实时系统必须有总闸门。
                if time.time() - started > self.total_deadline_s:
                    LOG.warning(
                        "大模型单轮决策超出总预算 %.1fs（已用 %.1fs），放弃本次推理",
                        self.total_deadline_s, time.time() - started,
                    )
                    self.last_latency = time.time() - started
                    parsed = self._parse(last_content) if last_content else None
                    self._record_llm(
                        ok=False, started=started, req=req, parsed=parsed,
                        content=last_content or "", error="total_deadline_exceeded",
                        image=image,
                    )
                    return parsed

                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }
                if schemas:
                    kwargs["tools"] = schemas
                    kwargs["tool_choice"] = "auto"

                resp = client.chat.completions.create(**kwargs)
                self.calls += 1
                self._note_success()  # 打通一次即解除熔断
                msg = resp.choices[0].message
                last_content = msg.content

                tool_calls = getattr(msg, "tool_calls", None)
                if not tool_calls:
                    self.last_latency = time.time() - started
                    parsed = self._parse(msg.content)
                    self._record_llm(
                        ok=True, started=started, req=req, parsed=parsed,
                        content=msg.content or "", image=image,
                    )
                    return parsed

                # --- 执行工具并回灌 ---
                rounds += 1
                if rounds > self.max_tool_rounds:
                    LOG.warning("工具调用轮次超过上限 %d，停止继续调用", self.max_tool_rounds)
                    self.last_latency = time.time() - started
                    parsed = self._parse(msg.content)
                    self._record_llm(
                        ok=True, started=started, req=req, parsed=parsed,
                        content=msg.content or "", image=image,
                        error=f"tool_rounds_exceeded({self.max_tool_rounds})",
                    )
                    return parsed

                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
                for tc in tool_calls:
                    result = self._execute_tool(tc.function.name, tc.function.arguments)
                    self.tool_call_total += 1
                    self.last_tool_calls.append(
                        {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                            "result": result,
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(result),
                        }
                    )
        except Exception as e:  # noqa: BLE001 - 大模型失败必须降级，不能中断系统
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self._note_failure(e)
            LOG.warning("大模型调用失败，将降级到规则决策：%s", self.last_error)
            try:
                self._record_llm(
                    ok=False, started=started, req={}, parsed=None,
                    content="", error=self.last_error,
                )
            except UnboundLocalError:  # started 还没赋值就炸了
                pass
            return None

    # -----------------------------------------------------------------
    # 内部
    # -----------------------------------------------------------------
    def _build_messages(
        self,
        state: SpatialState,
        image: bytes | None,
        user_query: str | None,
        extra: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in history or []:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})

        text = build_user_prompt(state, user_query, extra)
        if image:
            b64 = base64.b64encode(image).decode("ascii")
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": text})
        return messages

    def _execute_tool(self, name: str, raw_args: str) -> dict[str, Any]:
        if self.tools is None:
            return {"error": "未注册工具集"}
        import json

        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
        return self.tools.call(name, args if isinstance(args, dict) else {})

    @staticmethod
    def _parse(content: str | None) -> dict[str, Any] | None:
        data = extract_json(content or "")
        if data is None:
            LOG.warning("模型输出无法解析为 JSON：%s", (content or "")[:120])
        return data

    # -----------------------------------------------------------------
    # Stage 9 录制辅助（**完全被动**：出错只记一条日志，绝不影响主循环）
    # -----------------------------------------------------------------
    def _request_summary(
        self,
        messages: list[dict[str, Any]],
        image: bytes | None,
        schemas: list[dict[str, Any]] | None,
        history: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        """把"送进去的是什么"整理成可落盘的摘要（**不含任何隐藏思维链**）。

        图片只留一个引用（`record_llm` 会把字节写进 `images/`），
        提示词文本原样保留 —— 它就是我们自己拼出来的输入，
        "换 Prompt 前后哪一轮判断变了"正是靠它才可比。
        """
        prompt_text = ""
        for m in messages:
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                prompt_text = c
            elif isinstance(c, list):
                prompt_text = " ".join(
                    str(p.get("text", "")) for p in c if isinstance(p, dict)
                )
        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tools_enabled": bool(schemas),
            "tool_count": len(schemas or []),
            "has_image": bool(image),
            "image_bytes": len(image or b""),
            "history_turns": len(history or []),
            "prompt_text": prompt_text,
            "message_count": len(messages),
        }

    def _record_llm(
        self,
        *,
        ok: bool,
        started: float,
        req: dict[str, Any],
        parsed: dict[str, Any] | None,
        content: str,
        image: bytes | None = None,
        error: str | None = None,
    ) -> None:
        """落一次录制。**任何异常都吞掉** —— 录制挂掉不能让导航挂掉。"""
        rec = self.recorder
        if rec is None:
            return
        try:
            elapsed, tick = self.record_clock
            rec.record_llm(
                kind="cognitive",
                model=self.model,
                ok=bool(ok),
                latency_s=time.time() - started,
                request=req,
                # ⚠️ 只存**结构化决策**与模型自己的公开 reason；不落 SDK 消息里的
                #    reasoning_content 等隐藏字段（白名单式组装，见 SessionRecorder）
                response={
                    "parsed": parsed,
                    "content": (content or "")[:4000],
                    "reason": (parsed or {}).get("reason", "") if isinstance(parsed, dict) else "",
                    "tool_calls": list(self.last_tool_calls or []),
                },
                error=error,
                elapsed=elapsed,
                tick=tick,
                image=image,
            )
        except Exception as e:  # noqa: BLE001
            LOG.debug("录制大模型调用失败（不影响调用本身）：%s", e)

    # -----------------------------------------------------------------
    # 诊断
    # -----------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "usable": self.usable,
            "circuit_open": self.circuit_open,
            "disabled_reason": self.disabled_reason,
            "disabled_for_s": round(max(0.0, self.disabled_until - time.time()), 1),
            "circuit_trips": self.circuit_trips,
            "consecutive_failures": self.consecutive_failures,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "calls": self.calls,
            "failures": self.failures,
            "tool_calls": self.tool_call_total,
            "max_retries": self.max_retries,
            "total_deadline_s": self.total_deadline_s,
            "last_latency_s": round(self.last_latency, 3),
            "last_error": self.last_error,
        }


__all__ = ["LLMClient"]
