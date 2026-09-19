"""config 包：对外只暴露配置加载能力。"""

from config.loader import (
    PROJECT_ROOT,
    ConfigError,
    get_config,
    llm_ready,
    load_config,
    resolve_path,
)

__all__ = [
    "PROJECT_ROOT",
    "ConfigError",
    "get_config",
    "llm_ready",
    "load_config",
    "resolve_path",
]
