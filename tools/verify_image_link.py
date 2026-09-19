#!/usr/bin/env python3
"""验证「图片确实进入了多模态大模型的输入」。

为什么需要这个脚本：
    iPhone 推上来的图，最终必须出现在送模型的 messages 里，否则 Agent 的
    "结合图片 + SpatialState 给导航建议" 就是空话。这一环如果只在有 API Key
    时才能验证，日常开发就没法回归。

做法：
    用假的大模型客户端替换 OpenAI 客户端（不联网、不花钱、不需要 Key），
    把真正会被发送的请求体抓下来，逐项断言：
        1. SpatialState.camera.image_available 为 True（图被状态融合层看到）
        2. 请求体最后一条 user 消息同时包含 text 与 image_url 两个 part
        3. image_url 是 data:image/jpeg;base64, 且解码后字节数与接收器一致
        4. 模型返回的 JSON 能被解析成受控 Action（SPEAK），且 source == "llm"

用法：
    python tools/verify_image_link.py
    python tools/verify_image_link.py --image logs/latest.jpg   # 用真实图片文件
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.agent_core import SpatialAgentSystem, SystemConfig  # noqa: E402
from camera.image_processor import make_test_frame  # noqa: E402
from camera.iphone_receiver import IphoneReceiver  # noqa: E402
from config.loader import load_config  # noqa: E402

# 模型"应当"返回的决策（用真实字段格式，验证解析链路）
FAKE_MODEL_OUTPUT = json.dumps(
    {
        "action_type": "SPEAK",
        "message": "前方一点五米有障碍物，请先停一下，我带你从左侧绕过去。",
        "urgency": "high",
        "reason": "图片显示正前方地面有深色障碍物，与左侧通行空间充足的状态一致",
    },
    ensure_ascii=False,
)


class _RecordingCompletions:
    """假的 chat.completions：只记录请求体，返回固定响应。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.requests.append(kwargs)
        message = SimpleNamespace(content=FAKE_MODEL_OUTPUT, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeOpenAI:
    """最小可用的 OpenAI 客户端替身。"""

    def __init__(self, recorder: _RecordingCompletions) -> None:
        self.chat = SimpleNamespace(completions=recorder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证图片是否进入多模态模型输入")
    parser.add_argument("--image", default=None, help="用作测试帧的 JPEG 文件；缺省则用合成图")
    parser.add_argument("--seed", type=int, default=20260917, help="固定随机种子")
    args = parser.parse_args(argv)

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  → ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # ---------------------------------------------------------------
    cfg = load_config()
    camera = IphoneReceiver(cfg)
    system = SpatialAgentSystem(
        cfg, camera=camera, options=SystemConfig(seed=args.seed, enable_llm=True)
    )
    llm = system.llm
    assert llm is not None, "enable_llm=True 时 LLMClient 不应为 None"

    # 造一个可用的假模型：只为了走通 chat_multimodal 的完整代码路径
    llm.api_key = "sk-fake-for-verification"
    llm.model = cfg["llm"]["model"]
    recorder = _RecordingCompletions()
    llm._ensure_client = lambda: _FakeOpenAI(recorder)  # type: ignore[method-assign]

    # ---------------------------------------------------------------
    print("=== 1. 注入一帧（模拟 iPhone 推上来的图）===")
    if args.image:
        raw = Path(args.image).read_bytes()
        source_desc = f"文件 {args.image}"
    else:
        raw = make_test_frame(text="BSA IMAGE LINK TEST")
        source_desc = "合成测试图"
    result = camera.submit(raw, source="websocket")
    check("接收器接受该帧", bool(result.get("accepted")), f"source={source_desc}")

    # ---------------------------------------------------------------
    print("\n=== 2. 状态融合层是否看到了这张图 ===")
    step = system.step(1.0)
    cam_state = step.state.camera
    check("SpatialState.camera.image_available", cam_state.image_available is True)
    check("SpatialState.camera.source", cam_state.source == "websocket", str(cam_state.source))
    check(
        "SpatialState.camera.age_s 新鲜",
        cam_state.age_s is not None and cam_state.age_s < 1.5,
        f"{cam_state.age_s:.3f}s" if cam_state.age_s is not None else "None",
    )

    # ---------------------------------------------------------------
    print("\n=== 3. 送模型的请求体里是否带图 ===")
    check("确实调用了一次模型", len(recorder.requests) == 1, f"调用次数={len(recorder.requests)}")
    if recorder.requests:
        messages = recorder.requests[0]["messages"]
        last = messages[-1]
        check("最后一条是 user 消息", last.get("role") == "user")
        content = last.get("content")
        check("content 是多模态数组", isinstance(content, list), f"type={type(content).__name__}")

        parts = content if isinstance(content, list) else []
        text_parts = [p for p in parts if p.get("type") == "text"]
        image_parts = [p for p in parts if p.get("type") == "image_url"]

        check("包含 text part", len(text_parts) == 1)
        check("包含 image_url part", len(image_parts) == 1)

        if image_parts:
            url = image_parts[0]["image_url"]["url"]
            prefix_ok = url.startswith("data:image/jpeg;base64,")
            check("image_url 是内联 base64 JPEG", prefix_ok, f"前缀={url[:32]}…")
            if prefix_ok:
                decoded = base64.b64decode(url.split(",", 1)[1])
                sent = camera.latest_bytes() or b""
                check(
                    "base64 解码后与接收器缓存的帧一致",
                    decoded == sent,
                    f"载荷 {len(decoded)} B / 缓存 {len(sent)} B",
                )
                check("图片不是空图", len(decoded) > 1024, f"{len(decoded)} B")

        if text_parts:
            text = text_parts[0]["text"]
            # 提示词是给人看的结构化中文文本（见 agent/prompt_template.py），
            # 因此断言各段标题是否齐全，而不是 JSON 字段名。
            required_sections = ("[用户]", "[目标]", "[导航指令]", "[环境]", "[视觉]", "[风险]")
            missing = [s for s in required_sections if s not in text]
            check(
                "文本里带完整结构化 SpatialState",
                not missing,
                "缺失段落：" + ", ".join(missing) if missing else f"{len(text)} 字符",
            )
            check(
                "文本里声明了实时画面",
                "有实时画面" in text or "画面" in text,
                next((ln for ln in text.splitlines() if ln.startswith("[视觉]")), "未找到 [视觉] 段"),
            )
            print("\n--- 送模型的状态文本（前 600 字）---")
            print(text[:600] + ("\n…（截断）" if len(text) > 600 else ""))

        print("\n--- 请求摘要 ---")
        print(f"system 提示词长度 : {len(messages[0]['content'])} 字符")
        print(f"消息条数          : {len(messages)}")
        print(f"tools（工具数）    : {len(recorder.requests[0].get('tools', []))}")
        print(f"模型              : {recorder.requests[0].get('model')}")

    # ---------------------------------------------------------------
    print("\n=== 4. 模型输出是否被解析成受控 Action ===")
    action = step.action
    check("action.source == 'llm'", action.source == "llm", action.source)
    check("action_type == SPEAK", action.action_type.value == "SPEAK", action.action_type.value)
    print(f"\n  播报内容: {action.message}")
    print(f"  紧急级别: {action.urgency}")
    print(f"  决策依据: {action.reason}")

    # ---------------------------------------------------------------
    print("\n" + "=" * 66)
    if failures:
        print(f"结果：{len(failures)} 项未通过 → {', '.join(failures)}")
        print("=" * 66)
        return 1
    print("结果：全部通过 —— 图片已确认进入多模态模型输入，且输出被正确转为 Agent 行动。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
