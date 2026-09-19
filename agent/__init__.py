"""Agent 层：空间理解 → 风险判断 → 行动决策 → 工具调用。

    decision.py            —— 规则兜底决策引擎（Action 定义见 action_schema.py）
    action_schema.py       —— Action / ActionType / AgentDecision（结构化输出契约）
    interaction_policy.py  —— 说不说 / 说多少 / 用什么通道
    context_builder.py     —— 受控信息面（模型能看什么）
    cognitive_agent.py     —— 实时多模态大模型的"理解与表达"
    tools.py               —— 工具集（get_current_position / replan_route / speak ...）
    prompt_template.py     —— 系统提示词与状态渲染 + 输出解析
    llm_client.py          —— 实时多模态大模型接口（OpenAI 兼容，可切 Qwen Omni / Gemini）
    memory.py              —— 对话、播报、行动历史
    orchestrator.py        —— 双循环编排（安全快循环 + 认知慢循环）
    agent_core.py          —— SpatialAgent（决策器）+ SpatialAgentSystem（完整运行时）

与 spatial 包同样采用惰性导出，避免任何潜在的循环引用。
"""

from typing import Any

_LAZY: dict[str, str] = {
    "Action": "agent.decision",
    "ActionType": "agent.decision",
    "MOTION_ALLOWED": "agent.decision",
    "RuleDecisionEngine": "agent.decision",
    "ActionChannel": "agent.action_schema",
    "AgentDecision": "agent.action_schema",
    "DEFAULT_PRIORITY": "agent.action_schema",
    "InteractionPolicy": "agent.interaction_policy",
    "InteractionDecision": "agent.interaction_policy",
    "ContextBuilder": "agent.context_builder",
    "ContextBundle": "agent.context_builder",
    "CognitiveAgent": "agent.cognitive_agent",
    "AgentMemory": "agent.memory",
    "Turn": "agent.memory",
    "Utterance": "agent.memory",
    "AgentTools": "agent.tools",
    "LLMClient": "agent.llm_client",
    "SYSTEM_PROMPT": "agent.prompt_template",
    "build_user_prompt": "agent.prompt_template",
    "render_state": "agent.prompt_template",
    "SpatialAgent": "agent.agent_core",
    "SpatialAgentSystem": "agent.agent_core",
    "StepResult": "agent.agent_core",
    "SystemConfig": "agent.agent_core",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module 'agent' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_path), name)
