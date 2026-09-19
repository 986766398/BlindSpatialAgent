"""安全层（Safety Layer）—— v0.3 的分层起点：安全不依赖大模型。

    risk_rules.py    纯函数规则表（阈值全部来自 config）
    safety_engine.py 快循环执行体：每 tick 求值 → SafetyVerdict

对外只用 `from agent.safety import SafetyEngine, SafetyVerdict, SafetyLevel`。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_RULES",
    "RuleContext",
    "SafetyAction",
    "SafetyEngine",
    "SafetyLevel",
    "SafetyRule",
    "SafetyThresholds",
    "SafetyVerdict",
]

_LAZY: dict[str, tuple[str, str]] = {
    "SafetyEngine": ("agent.safety.safety_engine", "SafetyEngine"),
    "SafetyVerdict": ("agent.safety.safety_engine", "SafetyVerdict"),
    "DEFAULT_RULES": ("agent.safety.risk_rules", "DEFAULT_RULES"),
    "RuleContext": ("agent.safety.risk_rules", "RuleContext"),
    "SafetyAction": ("agent.safety.risk_rules", "SafetyAction"),
    "SafetyLevel": ("agent.safety.risk_rules", "SafetyLevel"),
    "SafetyRule": ("agent.safety.risk_rules", "SafetyRule"),
    "SafetyThresholds": ("agent.safety.risk_rules", "SafetyThresholds"),
}


def __getattr__(name: str) -> Any:  # PEP 562：惰性导出，避免包之间急切导入成环
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module_name, attr = target
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value
