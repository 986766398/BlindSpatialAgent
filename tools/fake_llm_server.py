#!/usr/bin/env python3
"""本地假大模型服务（OpenAI 兼容），用于零成本联调。

为什么要有它：
    「图片 + SpatialState → 导航建议」这条链路如果必须花钱或有 Key 才能验证，
    日常改动就没法回归。本服务实现最小可用的 /v1/chat/completions，
    自动判断请求里有没有带图，并返回一段格式合法的 Agent 决策 JSON。

它不是玩具：Agent 走的是**真实**的 LLMClient → HTTP → 本服务 这条路径，
    因此能验证 base_url、鉴权头、多模态消息体、工具 schema、JSON 解析全部环节。

用法：
    python tools/fake_llm_server.py                       # 监听 127.0.0.1:8900
    python tools/fake_llm_server.py --port 8900

配合主程序：
    export BSA_LLM_API_KEY=anything
    export BSA_LLM_BASE_URL=http://127.0.0.1:8900/v1
    export BSA_LLM_MODEL=fake-multimodal
    python main.py --serve

查看统计：
    curl http://127.0.0.1:8900/_stats
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="BSA Fake LLM")

STATS: dict[str, Any] = {
    "calls": 0,
    "calls_with_image": 0,
    "last_image_bytes": 0,
    "max_image_bytes": 0,
    "last_text_chars": 0,
    "last_model": None,
    "last_tool_count": 0,
    "started_at": time.time(),
}


def _extract_modal_parts(messages: list[dict[str, Any]]) -> tuple[int, int, int]:
    """返回 (图片数, 最后一张图的 base64 字节数, 最后一条文本长度)。"""
    images = 0
    image_bytes = 0
    text_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            text_chars = len(content)
        elif isinstance(content, list):
            for part in content:
                ptype = part.get("type")
                if ptype == "text":
                    text_chars = len(part.get("text", ""))
                elif ptype == "image_url":
                    images += 1
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:") and "," in url:
                        # base64 长度 * 3/4 即原始字节数的近似值
                        image_bytes = int(len(url.split(",", 1)[1]) * 3 / 4)
    return images, image_bytes, text_chars


def _decide(images: int) -> dict[str, Any]:
    """根据"有没有图"给出不同的决策，方便一眼看出多模态是否真的生效。"""
    if images > 0:
        return {
            "action_type": "SPEAK",
            "message": "前方一点五米处有障碍物，请先停一下，我带你从左侧绕过去。",
            "urgency": "high",
            "reason": "实时画面显示正前方地面存在深色障碍物，且左侧通行空间充足",
        }
    return {
        "action_type": "CONTINUE",
        "message": "前方通畅，继续直行。",
        "urgency": "normal",
        "reason": "未收到实时画面，仅依据 LiDAR 与环境状态判断前方通畅",
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    body = await request.json()
    messages = body.get("messages", []) or []
    images, image_bytes, text_chars = _extract_modal_parts(messages)

    STATS["calls"] += 1
    # 注意：只有"这一轮真的带了图"时才更新 image 相关统计。
    # 否则最后一轮恰好没图（图片过期）会把之前的证据覆盖成 0。
    if images:
        STATS["calls_with_image"] += 1
        STATS["last_image_bytes"] = image_bytes
        STATS["max_image_bytes"] = max(STATS["max_image_bytes"], image_bytes)
    STATS["last_text_chars"] = text_chars
    STATS["last_model"] = body.get("model")
    STATS["last_tool_count"] = len(body.get("tools", []) or [])

    decision = _decide(images)
    return JSONResponse(
        {
            "id": f"chatcmpl-fake-{STATS['calls']}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake-multimodal"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(decision, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )


@app.get("/_stats")
async def stats() -> JSONResponse:
    return JSONResponse(STATS)


@app.post("/_reset")
async def reset() -> JSONResponse:
    STATS.update(
        {
            "calls": 0,
            "calls_with_image": 0,
            "last_image_bytes": 0,
            "last_text_chars": 0,
            "last_model": None,
            "last_tool_count": 0,
        }
    )
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地假大模型服务（OpenAI 兼容）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900)
    args = parser.parse_args()

    print(f"[fake-llm] 监听 http://{args.host}:{args.port}")
    print(f"[fake-llm] OpenAI 兼容端点：http://{args.host}:{args.port}/v1/chat/completions")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
