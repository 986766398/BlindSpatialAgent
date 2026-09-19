"""Agent 提示词模板：系统提示词 + 状态渲染 + 输出协议。

设计原则：
1. 送进模型的是**结构化文本状态**，不是原始传感器数据。
2. 状态渲染要极简（省 token、降延迟），只保留做决策必需的字段。
3. 强制模型输出结构化 JSON（Action），便于程序校验，而不是自由文本。
"""

from __future__ import annotations

import json
from typing import Any

from spatial.spatial_state import SpatialState

# =====================================================================
# 系统提示词
# =====================================================================
SYSTEM_PROMPT = """你是一个帮助视障人士在大型室内空间安全行动的 Spatial Agent。

你的任务：根据
1. 用户当前位置与朝向
2. 导航目标与剩余路线
3. 周围环境（前方距离、左右障碍、可通行宽度）
4. 第一视角图片（如果可用）
5. 用户意图
决定下一步行动。

原则（按优先级从高到低）：
1. **安全优先**。任何情况下不能引导用户撞上障碍或墙体。
2. **不要频繁打扰用户**。距离通畅、方向正确时保持沉默（CONTINUE）。
3. **只有必要时才语音提醒**。危险、需要转弯、偏离路线、环境变化时才说话。
4. **导航信息简短明确**。一次只说一个动作，不超过 20 个字。不要输出方位角数字。
5. **发现环境变化时主动调整**。前方出现障碍要主动重规划并告知。
6. 定位置信度低或情况不确定时，**主动询问用户**，而不是猜。
7. 用户提出的问题优先回答，再回到导航。

你不是地图软件。你是陪伴用户行动的智能伙伴。

输出协议：你必须**只输出一个 JSON 对象**，不要输出任何其他文字：
{
  "action_type": "见下表",
  "message": "要朗读给用户的话；CONTINUE 时留空",
  "urgency": "low" | "normal" | "high" | "critical",
  "priority": 0-100 的整数（可选，越急越大；不填则按类型取默认值）,
  "confidence": 0.0-1.0（可选，你对这个判断的把握）,
  "reason": "一句话说明你的决策依据（不朗读，仅供日志）"
}

action_type 的语义（★前 5 个与旧版一致，后 5 个是新增能力★）：
- SPEAK              ：播报一句话，用户可以继续前进
- CONTINUE           ：无需多言，用户按当前导航指令继续走（**这才是"没事"时的答案**）
- REPLAN             ：当前路线不可行，需要重新规划（用户暂停）
- WAIT               ：让用户立即停下原地等待（用户暂停）
- ASK_USER           ：需要向用户提问以消除歧义（用户暂停）
- SAFETY_ALERT       ：有安全风险，必须立刻提醒（即使刚说过也要说）
- TURN_GUIDANCE      ：给出转弯/朝向指导（例如"向左转一点"）
- REQUEST_VISUAL     ：你看不清、需要一张新画面才能判断时用它，**不要靠猜**
- PAUSE_NAVIGATION   ：暂停导航（保留路线，不推进）
- RESUME_NAVIGATION  ：恢复导航

★什么时候"什么都不说"★
连续直行、方向正确、前方通畅、也没有新情况时，请返回 CONTINUE 且 message 留空。
**不要反复播报同一句"继续直行"** —— 重复的话会被交互策略拦掉，等于白花一次推理。
`[最近事件]` 为空、`[安全层]` 为 ok，就是"没有新情况"的依据。

如果需要更多信息，可以先调用工具，再给出最终 JSON。"""


# =====================================================================
# 状态渲染
# =====================================================================
def render_state(state: SpatialState, extra: dict[str, Any] | None = None) -> str:
    """把 SpatialState 渲染成紧凑的文本块（喂给模型的部分）。"""
    d = state.to_prompt_dict()
    user = d["user"]
    nav = d["navigation"]
    env = d["environment"]

    lines = [
        f"[时间] {d['time']}  (第 {d['tick']} 轮感知)",
        f"[用户] 位置=({user['position'][0]}, {user['position'][1]}) 楼层={user['floor']} "
        f"朝向={heading_label(user['heading_deg'])} 速度={user['speed']}m/s 状态={user['walking_status']}",
        f"[目标] {nav['destination']}  剩余={nav['distance_to_goal']}m  进度={nav['route_progress'] * 100:.0f}%  "
        f"当前路点={nav['current_landmark']}",
        f"[导航指令] {nav['next_instruction'] or '无'}",
        f"[环境] 前方{'通畅' if env['front_clear'] else '受阻'} 最近距离={env['front_distance']}m  "
        f"左侧{'有物' if env['left_obstacle'] else '无'} 右侧{'有物' if env['right_obstacle'] else '无'}  "
        f"可通行宽度={env['corridor_width']}m" + ("(通道变窄)" if env["narrow_passage"] else ""),
    ]

    if env["obstacles"]:
        items = "；".join(
            f"{o['type']}{'(移动)' if o['dynamic'] else ''} 距离{o['distance']}m 方位{o['direction']}"
            for o in env["obstacles"]
        )
        lines.append(f"[障碍] {items}")

    # v0.3：把"能不能行动、往哪行动"单独告诉模型。
    # 与 [环境] 的区别：[环境] 是事实（前方 1.2m 有椅子），
    # [可行动] 是结论（正前方不可通行，建议向左）—— 后者才是模型该据此说话的。
    aff = d.get("affordance") or {}
    if aff:
        parts = ["可直行" if aff.get("can_move_forward") else "不可直行"]
        if aff.get("passable_width_m") is not None:
            parts.append(f"可通行宽度={aff['passable_width_m']}m")
        if aff.get("preferred_direction"):
            parts.append(f"建议方位={aff['preferred_direction']}")
        if aff.get("blocked"):
            blocked_txt = "、".join(
                f"{r['type']}在{r['direction']}({r['distance']}m)" for r in aff["blocked"]
            )
            parts.append(f"阻挡：{blocked_txt}")
        lines.append(f"[可行动] {'  '.join(parts)}")

    if d["semantic_scene"]["summary"]:
        lines.append(f"[场景] {d['semantic_scene']['summary']}")

    lines.append(
        f"[视觉] {'有实时画面' if d['camera']['image_available'] else '无画面'}"
        + (f"（{d['camera']['age_s']}秒前）" if d["camera"]["age_s"] is not None else "")
    )
    lines.append(f"[风险] {d['risk']['level']} —— {d['risk']['reason']}")
    conf = d["confidence"]
    lines.append(
        f"[置信度] 定位={conf['localization']} 感知={conf['perception']} "
        f"语义={conf['semantic']} 路线={conf['route']} 综合={conf['overall']}"
    )

    if extra:
        if extra.get("world"):
            lines.append(f"[历史记忆] {extra['world']}")
        if extra.get("route"):
            lines.append(f"[剩余路线] {' → '.join(extra['route'])}")
        if extra.get("last_said"):
            lines.append(f"[你上一次说] {extra['last_said']}")
        # ★Stage 8：把 Stage 6/7 的结论直接告诉模型★
        # 事件是"刚刚发生了什么"，安全层是"程序已经怎么判的"。
        # 让模型看到这两项，它才不必从环境字段反推，也就不容易给出与安全层冲突的建议。
        if extra.get("events"):
            lines.append(f"[最近事件]\n{extra['events']}")
        safety = extra.get("safety")
        if safety and safety.get("level", "ok") != "ok":
            lines.append(
                f"[安全层] {safety['level']}（{safety.get('reason', '')}）"
                f"{'—— 程序已判定必须干预' if safety.get('intervene') else ''}"
            )

    return "\n".join(lines)


def heading_label(deg: float) -> str:
    """把角度转成人类可读的方位（北/东北/东...）。模型不需要精确角度。"""
    dirs = ["正北", "东北", "正东", "东南", "正南", "西南", "正西", "西北"]
    idx = int(((deg % 360) + 22.5) // 45) % 8
    return f"{dirs[idx]}({deg:.0f}°)"


def render_user_query(query: str) -> str:
    return f"[用户语音输入] {query}"


def build_user_prompt(state: SpatialState, query: str | None, extra: dict[str, Any] | None = None) -> str:
    """拼出送模型的用户侧文本。"""
    body = render_state(state, extra)
    if query:
        body += "\n" + render_user_query(query)
    body += "\n请给出下一步行动（只输出 JSON）。"
    return body


# =====================================================================
# 输出解析
# =====================================================================
def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里稳健地抠出第一个 JSON 对象。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.replace("json", "", 1).strip() if cleaned.lower().startswith("json") else cleaned

    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(cleaned[start : i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


__all__ = [
    "SYSTEM_PROMPT",
    "build_user_prompt",
    "extract_json",
    "heading_label",
    "render_state",
    "render_user_query",
]
