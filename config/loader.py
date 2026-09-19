"""配置加载器 —— 全项目唯一的配置读取入口。

设计原则：
1. 任何模块都不允许自己 open('config.yaml')，统一走 get_config()。
2. 未来接入真实硬件，只改 config.yaml 的 simulator 段，代码不动。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# 项目根目录（本文件位于 <root>/config/loader.py）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "config.yaml"

# 允许用环境变量覆盖 API Key，避免把密钥写进配置文件
ENV_API_KEY: str = "BSA_LLM_API_KEY"
ENV_BASE_URL: str = "BSA_LLM_BASE_URL"
ENV_MODEL: str = "BSA_LLM_MODEL"

# 项目根目录下的 .env（已在 .gitignore 中忽略，密钥不进版本库）
DOTENV_PATH: Path = PROJECT_ROOT / ".env"
# 只加载一次，避免每次 get_config() 都读盘
_dotenv_loaded: bool = False


class ConfigError(RuntimeError):
    """配置缺失或格式错误。"""


def _load_dotenv(path: Path = DOTENV_PATH) -> int:
    """加载项目根目录的 .env 到 os.environ，返回成功注入的键数量。

    实现要点（刻意不用 python-dotenv，避免引入多余依赖）：
    - 已存在的真实环境变量**优先**，不被 .env 覆盖（与主流 dotenv 语义一致），
      这样 `BSA_LLM_API_KEY=xxx python main.py` 这种临时覆盖仍然有效。
    - 支持 `KEY=VALUE`、`export KEY=VALUE`、`# 注释`、空行、值两侧的引号。
    - 文件不存在是正常情况（默认识别为规则决策模式），不报错。
    """
    if not path.exists():
        return 0

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不了就静默跳过，绝不因为一个可选文件让整个程序起不来
        return 0

    injected = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        # 去掉成对的引号（'...' 或 "..."）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        # 真实环境变量优先，不覆盖
        if key not in os.environ:
            os.environ[key] = value
            injected += 1

    return injected


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取 YAML 配置并做最小校验，返回 dict。"""
    # 先加载 .env（只做一次），再走环境变量覆盖，保证优先级：
    # 真实环境变量 > .env > config.yaml
    global _dotenv_loaded
    if not _dotenv_loaded:
        _load_dotenv()
        _dotenv_loaded = True

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if not isinstance(cfg, dict):
        raise ConfigError(f"配置根节点必须是映射结构: {cfg_path}")

    for required in ("system", "llm", "camera", "simulator", "agent", "api"):
        if required not in cfg:
            raise ConfigError(f"配置缺少必需段落: `{required}`")

    # 环境变量优先级高于配置文件（便于切模型 / 换 key）
    llm = cfg["llm"]
    if os.getenv(ENV_API_KEY):
        llm["api_key"] = os.environ[ENV_API_KEY]
    if os.getenv(ENV_BASE_URL):
        llm["base_url"] = os.environ[ENV_BASE_URL]
    if os.getenv(ENV_MODEL):
        llm["model"] = os.environ[ENV_MODEL]

    return cfg


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """带缓存的全局配置获取（默认配置文件）。"""
    return load_config()


def resolve_path(relative: str | Path) -> Path:
    """把配置里的相对路径解析为基于项目根目录的绝对路径。"""
    p = Path(relative)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def llm_ready(cfg: dict[str, Any] | None = None) -> bool:
    """是否具备调用真实多模态大模型的条件。"""
    cfg = cfg or get_config()
    return bool(cfg["llm"].get("api_key"))
